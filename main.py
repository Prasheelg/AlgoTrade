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

from algotrade import config, markets, state, telegram_bot, tracker
from algotrade.scanner import scan, scan_and_alert
from algotrade.universe import get_universe

TZ_LABEL = {"IN": "IST", "US": "ET"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
log = logging.getLogger("main")


def universe_of(code: str) -> str:
    return config.US_UNIVERSE if code == "US" else config.UNIVERSE


def scanner_kw(code: str) -> dict:
    """scan() settings for a market: .env (per-market) minus the strategies switched off in the dashboard's
    strategy scorecard."""
    from algotrade import performance
    from algotrade.strategies import DEFAULT_STRATEGIES
    kw = dict(config.MARKET_SCAN.get(code, {}))  # per-market strategies / min score / filters from .env
    off = set(performance.strategies_off(code))
    if off:
        names = kw.get("strategies") or config.STRATEGIES or list(DEFAULT_STRATEGIES)
        kw["strategies"] = [n for n in names if n not in off]
    return kw


def run_scan(code: str, dry_run: bool = False) -> None:
    kw = scanner_kw(code)
    if "strategies" in kw and not kw["strategies"]:
        log.warning("%s: every strategy is switched off in the scorecard; nothing to scan", code)
        return
    if dry_run:
        for s in scan(market=code, **kw):
            print(telegram_bot.format_signal(s), "\n" + "-" * 40)
        return
    if code != "IN":
        scan_and_alert(market=code, **kw)
        return
    before = {s["key"] for s in state.all_signals("IN")}
    scan_and_alert(market=code, **kw)
    new = [s["key"] for s in state.all_signals("IN") if s["key"] not in before]
    trade(new)


def trade(new_keys: list[str]) -> None:
    """Automatic Kotak orders (India): entries for new signals (only while Auto-trade is ON), then bring every
    live trade up to date. Order problems are logged and sent to Telegram; they never stop the scanner."""
    from algotrade import orders
    orders.trade_new(new_keys)


def scan_settings(code: str) -> str:
    """One-line summary of what the scanner runs for a market."""
    kw = scanner_kw(code)
    names = kw.get("strategies") or config.STRATEGIES
    filters = kw.get("filters", config.FILTERS)
    return (f"{', '.join(names) if names else 'default strategies'} · min score {kw.get('min_score', config.MIN_SCORE)}"
            + (f" · filters {', '.join(filters)}" if filters else ""))


def housekeeping() -> None:
    """Each loop: pick up settings changed on the dashboard, tell its health check the scanner is alive, and send
    the daily backup / summary if they're due."""
    from algotrade import backup, health, settings, summary
    for job in (settings.apply, health.beat, backup.daily, summary.daily):
        try:
            job()
        except Exception:
            log.exception("Housekeeping (%s) failed", job.__name__)


def run_loop(codes: list[str]) -> None:
    lines = []
    for code in codes:
        m = markets.get(code)
        start, end = (t.strftime("%H:%M") for t in m.session)
        lines.append(f"{m.flag} {m.name}: {universe_of(code)} · {start}–{end} {TZ_LABEL[code]}\n   {scan_settings(code)}")
    telegram_bot.send_message(
        "🤖 <b>Scanner started</b>\n" + "\n".join(lines) +
        f"\nTF: {config.TIMEFRAME} · every {config.SCAN_INTERVAL_MIN} min")
    summary_sent_for: dict[str, object] = {}  # market -> exchange date whose EOD scan succeeded
    header_sent_for: dict[str, object] = {}   # market -> exchange date whose EOD header was sent
    while True:
        housekeeping()
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
                        # header once per date; a failed scan is retried next loop without re-sending it
                        if header_sent_for.get(code) != now.date():
                            telegram_bot.send_message(f"{m.flag} 📊 <b>{m.name} end-of-day scan</b> — setups for next session:")
                            header_sent_for[code] = now.date()
                        run_scan(code)
                        summary_sent_for[code] = now.date()
                    elif code == "IN":
                        trade([])  # keep live orders in line overnight (e.g. stops re-placed as AMO)
            except Exception:
                log.exception("%s scan failed", m.name)
        time.sleep(config.SCAN_INTERVAL_MIN * 60)


def run_backtest(code: str, symbols: str | None) -> None:
    from algotrade import backtest, costs
    syms = get_universe(symbols, market=code)
    log.info("Backtesting %d %s symbols on %s ...", len(syms), markets.get(code).name, config.TIMEFRAME)
    trades = backtest.run(syms, config.TIMEFRAME, period="5y" if config.TIMEFRAME == "1d" else "60d", market=code,
                          costs=costs.model_for(code, config.TIMEFRAME), **config.MARKET_SCAN.get(code, {}))
    print(f"\n=== {markets.get(code).name} ===")
    print(backtest.summarize(trades))
    if not trades.empty:
        out = config.DATA_DIR / ("backtest_trades.csv" if code == "IN" else f"backtest_trades_{code}.csv")
        trades.to_csv(out, index=False)
        print(f"\nTrades saved to {out}")


def kotak_check() -> None:
    """Log in to Kotak (unattended) and show the account; saves the raw responses for inspection. No orders."""
    import json
    from algotrade import broker
    missing = broker.missing_credentials()
    if missing:
        print(f"Missing in .env: {', '.join(missing)} (see README, Kotak trading login)")
        return
    try:
        acc = broker.account()
    except broker.BrokerError as exc:
        print(f"Kotak login / account read failed: {exc}")
        return
    out = config.DATA_DIR / "kotak_account_check.json"
    out.write_text(json.dumps(acc, indent=1, default=str), encoding="utf-8")
    count = lambda r: len(r.get("data") or []) if isinstance(r.get("data"), list) else "?"
    print(f"Logged in as {acc['name'] or config.KOTAK_UCC}")
    ip = broker.public_ip()
    print(f"This PC's IP as Kotak sees it: {ip or 'unknown'} (register it for Trade API orders if Kotak requires it)")
    print(f"Holdings: {count(acc['holdings'])} · positions: {count(acc['positions'])} · orders today: {count(acc['orders'])}")
    print(f"Funds (raw): {json.dumps(acc['limits'], default=str)[:600]}")
    print(f"Full responses saved to {out}")


def main() -> None:
    p = argparse.ArgumentParser(description="NSE + US algo scanner with Telegram alerts")
    p.add_argument("--once", action="store_true", help="run a single scan and exit")
    p.add_argument("--dry-run", action="store_true", help="print signals instead of sending")
    p.add_argument("--test-telegram", action="store_true", help="send a test message")
    p.add_argument("--backtest", action="store_true", help="backtest the strategy")
    p.add_argument("--ui", action="store_true", help="run the signals dashboard")
    p.add_argument("--kotak-check", action="store_true", help="log in to Kotak and show the account (no orders)")
    p.add_argument("--port", type=int, default=8050, help="dashboard port")
    p.add_argument("--host", default="127.0.0.1",
                   help="dashboard address (default: this PC only); for the phone app use tailscale serve, see README > Phone app")
    p.add_argument("--market", choices=["IN", "US", "ALL"], default="ALL", type=str.upper,
                   help="market to scan / backtest: IN, US or all (default: all)")
    p.add_argument("--symbols", help="override universe, e.g. NIFTY50, SP500 or 'RELIANCE,TCS' (needs --market)")
    args = p.parse_args()
    from algotrade import settings
    settings.apply()   # values changed on the dashboard's Settings screen override .env

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
    elif args.kotak_check:
        kotak_check()
    elif args.ui:
        from algotrade import web
        web.serve(host=args.host, port=args.port)
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
