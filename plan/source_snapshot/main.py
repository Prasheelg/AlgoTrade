"""NSE + US scanner -> Telegram trade suggestions.

Both markets are scanned with the same settings (timeframe, strategies, min score, interval);
each runs on its own exchange hours. --market IN or --market US limits a command to one market.

Usage:
  python main.py --test-telegram        send a test message
  python main.py --once                 scan now and send new signals
  python main.py --once --dry-run       scan and print, don't send
  python main.py                        run continuously during market hours
  python main.py --backtest             backtest strategy on the universe
  python main.py --ui                   open the signals dashboard (http://127.0.0.1:8050)
"""
import argparse
import logging
import time

from algotrade import config, markets, telegram_bot, tracker
from algotrade.scanner import scan, scan_and_alert
from algotrade.universe import get_universe

TZ_LABEL = {"IN": "IST", "US": "ET"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
log = logging.getLogger("main")


def universe_of(code: str) -> str:
    return config.US_UNIVERSE if code == "US" else config.UNIVERSE


def run_scan(code: str, dry_run: bool = False) -> None:
    if dry_run:
        for s in scan(market=code):
            print(telegram_bot.format_signal(s), "\n" + "-" * 40)
        return
    scan_and_alert(market=code)


def run_loop(codes: list[str]) -> None:
    lines = []
    for code in codes:
        m = markets.get(code)
        start, end = (t.strftime("%H:%M") for t in m.session)
        lines.append(f"{m.flag} {m.name}: {universe_of(code)} · {start}–{end} {TZ_LABEL[code]}")
    telegram_bot.send_message(
        "🤖 <b>Scanner started</b>\n" + "\n".join(lines) +
        f"\nTF: {config.TIMEFRAME} · every {config.SCAN_INTERVAL_MIN} min · min score {config.MIN_SCORE}")
    summary_sent_for: dict[str, object] = {}
    while True:
        for code in codes:
            m = markets.get(code)
            now = m.now()  # exchange time
            try:
                if m.is_open(now):
                    tracker.update_all(market=code)
                    run_scan(code)
                else:
                    log.info("%s market closed (%s %s); waiting", m.name, now.strftime("%a %H:%M"), TZ_LABEL[code])
                    # One end-of-day scan after close for next-day swing setups
                    if m.is_after_close(now) and summary_sent_for.get(code) != now.date():
                        telegram_bot.send_message(f"{m.flag} 📊 <b>{m.name} end-of-day scan</b> — setups for next session:")
                        run_scan(code)
                        summary_sent_for[code] = now.date()
            except Exception:
                log.exception("%s scan failed", m.name)
        time.sleep(config.SCAN_INTERVAL_MIN * 60)


def run_backtest(code: str, symbols: str | None) -> None:
    from algotrade import backtest
    syms = get_universe(symbols, market=code)
    log.info("Backtesting %d %s symbols on %s ...", len(syms), markets.get(code).name, config.TIMEFRAME)
    trades = backtest.run(syms, config.TIMEFRAME, period="5y" if config.TIMEFRAME == "1d" else "60d", market=code)
    print(f"\n=== {markets.get(code).name} ===")
    print(backtest.summarize(trades))
    if not trades.empty:
        out = config.DATA_DIR / ("backtest_trades.csv" if code == "IN" else f"backtest_trades_{code}.csv")
        trades.to_csv(out, index=False)
        print(f"\nTrades saved to {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="NSE + US algo scanner with Telegram alerts")
    p.add_argument("--once", action="store_true", help="run a single scan and exit")
    p.add_argument("--dry-run", action="store_true", help="print signals instead of sending")
    p.add_argument("--test-telegram", action="store_true", help="send a test message")
    p.add_argument("--backtest", action="store_true", help="backtest the strategy")
    p.add_argument("--ui", action="store_true", help="run the signals dashboard")
    p.add_argument("--port", type=int, default=8050, help="dashboard port")
    p.add_argument("--market", choices=["IN", "US", "ALL"], default="ALL", type=str.upper,
                   help="market to scan / backtest: IN, US or all (default: all)")
    p.add_argument("--symbols", help="override universe, e.g. NIFTY50, SP500 or 'RELIANCE,TCS' (needs --market)")
    args = p.parse_args()

    codes = list(markets.MARKETS) if args.market == "ALL" else [args.market]
    if args.symbols:
        if len(codes) > 1:
            p.error("--symbols needs --market IN or --market US")
        if codes[0] == "US":
            config.US_UNIVERSE = args.symbols
        else:
            config.UNIVERSE = args.symbols

    if args.test_telegram:
        ok = telegram_bot.send_message("✅ AlgoTrade bot is connected.")
        print("Sent" if ok else "Failed — check token/chat id and that the bot is in the group")
    elif args.ui:
        from algotrade import web
        web.serve(port=args.port)
    elif args.backtest:
        for code in codes:
            run_backtest(code, args.symbols)
    elif args.once or args.dry_run:
        for code in codes:
            if len(codes) > 1:
                print(f"\n===== {markets.get(code).flag} {markets.get(code).name} =====")
            run_scan(code, dry_run=args.dry_run)
    else:
        run_loop(codes)


if __name__ == "__main__":
    main()
