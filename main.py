"""NSE + US scanner -> Telegram trade suggestions.

Every SCAN_INTERVAL_MIN minutes within each market's scan window (India 09:00-15:30 IST, US 09:30-16:00 ET) the
scanner runs every SCAN_TIMEFRAMES timeframe with that market's scan panel settings from the dashboard
(strategies, min votes, filters, Telegram on/off; .env until the panel is first saved), read afresh each loop.
--market IN or --market US limits a command to one market. The dashboard's Scanner switch starts this loop, or
stops it (also one started with run.bat): it exits within a few seconds, ending a scan in progress at its next step.

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
import threading
from datetime import datetime, timedelta
from time import monotonic

from algotrade import config, markets, state, telegram_bot, tracker
from algotrade.scanner import ScanStopped, request_stop, scan, scan_and_alert, set_status
from algotrade.universe import get_universe

TZ_LABEL = {"IN": "IST", "US": "ET"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
log = logging.getLogger("main")
_quit = threading.Event()   # set when the dashboard switches the scanner off (runner.STOP_KEY)


def universe_of(code: str) -> str:
    return config.US_UNIVERSE if code == "US" else config.UNIVERSE


def scanner_kw(code: str, chosen: dict | None = None) -> dict:
    """scan() settings for a market: the scan panel's choices (else .env) minus the strategies switched off in
    the dashboard's strategy scorecard."""
    from algotrade import performance, settings
    chosen = chosen or settings.scan_settings(code)
    off = set(performance.strategies_off(code))
    return {"strategies": [n for n in chosen["strategies"] if n not in off], "min_score": chosen["min_score"],
            "filters": list(chosen["filters"])}


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_scan(code: str, dry_run: bool = False) -> None:
    """Scan every SCAN_TIMEFRAMES timeframe with the market's current panel settings. India's new daily BUY
    signals then go to Auto-trade. Progress and the result are stored for the dashboard's scan status."""
    from algotrade import settings
    chosen = settings.scan_settings(code)
    kw = scanner_kw(code, chosen)
    if not kw["strategies"]:
        log.warning("%s: no strategies are on (scan panel / scorecard); nothing to scan", code)
        if not dry_run:
            set_status(code, running=False, finished_at=_stamp(), timeframe=None,
                       error="No strategies are ticked in the scan panel (or all are off in the scorecard)")
        return
    if dry_run:
        for tf in config.SCAN_TIMEFRAMES:
            for s in scan(market=code, timeframe=tf, **kw):
                print(telegram_bot.format_signal(s), "\n" + "-" * 40)
        return
    before = {s["key"] for s in state.all_signals(code)}
    set_status(code, running=True, started_at=_stamp(), timeframe=None, error=None)
    total, errors = {"found": 0, "new": 0, "sent": 0, "symbols": []}, []
    for tf in config.SCAN_TIMEFRAMES:   # one failing timeframe doesn't stop the others
        set_status(code, timeframe=tf)
        try:
            r = scan_and_alert(market=code, timeframe=tf, send=chosen["send"], **kw) or {}
        except ScanStopped:   # switched off: keep what was found, skip Auto-trade
            set_status(code, running=False, finished_at=_stamp(), timeframe=None, result=total, error=None)
            log.info("%s scan stopped: the scanner is switching off", code)
            return
        except Exception as exc:
            log.exception("%s %s scan failed", code, tf)
            errors.append(f"{tf}: {exc}")
            continue
        for k in ("found", "new", "sent"):
            total[k] += r.get(k, 0)
        total["symbols"] += [f"{x} ({tf})" for x in r.get("symbols", [])]
    set_status(code, running=False, finished_at=_stamp(), timeframe=None, result=total,
               error="; ".join(errors) or None)
    if code == "IN":   # Auto-trade only buys India daily setups
        trade([x["key"] for x in state.all_signals("IN")
               if x["key"] not in before and x["timeframe"] == "1d" and x["side"] == "BUY"])
    if errors:
        raise RuntimeError(f"{code} scan failed on {', '.join(e.split(':')[0] for e in errors)}")


def trade(new_keys: list[str]) -> None:
    """Automatic Kotak orders (India): entries for new signals (only while Auto-trade is ON), then bring every
    live trade up to date. Order problems are logged and sent to Telegram; they never stop the scanner."""
    from algotrade import orders
    orders.trade_new(new_keys)


def scan_settings(code: str) -> str:
    """One-line summary of what the scanner runs for a market."""
    from algotrade.strategies import DEFAULT_STRATEGIES
    kw = scanner_kw(code)
    names = "default strategies" if kw["strategies"] == list(DEFAULT_STRATEGIES) else ", ".join(kw["strategies"])
    return (f"{names or 'no strategies'} · min score {kw['min_score']}"
            + (f" · filters {', '.join(kw['filters'])}" if kw["filters"] else ""))


def housekeeping() -> None:
    """Each loop: pick up settings changed on the dashboard, tell its health check the scanner is alive, and send
    the daily backup / summary if they're due."""
    from algotrade import backup, health, settings, summary
    for job in (settings.apply, health.beat, backup.daily, summary.daily):
        try:
            job()
        except Exception:
            log.exception("Housekeeping (%s) failed", job.__name__)


def _watch_stop(done: threading.Event) -> None:
    """Poll the dashboard's off switch; on it, stop the scan in progress and end the loop."""
    from algotrade import runner
    while not done.wait(runner.STOP_POLL_SEC):
        try:
            if runner.stop_requested():
                for code in markets.MARKETS:
                    request_stop(code)
                _quit.set()
                return
        except Exception:
            log.exception("Checking the scanner's off switch failed")


def _pause(seconds: float) -> None:
    """Wait for the next loop; returns at once when the scanner is switched off."""
    _quit.wait(seconds)


def run_loop(codes: list[str]) -> None:
    from algotrade import health, runner
    runner.clear_stop()   # an off request left from before this start doesn't count
    _quit.clear()
    done = threading.Event()
    threading.Thread(target=_watch_stop, args=(done,), name="stop-watch", daemon=True).start()
    try:
        _loop(codes)
    finally:
        done.set()
        health.gone()
        if _quit.is_set():
            for code in codes:
                set_status(code, running=False, timeframe=None, next_at=None)
            runner.clear_stop()
            log.info("Scanner switched off from the dashboard")
            telegram_bot.send_message("🛑 <b>Scanner stopped</b> from the dashboard")


def _loop(codes: list[str]) -> None:
    lines = []
    for code in codes:
        m = markets.get(code)
        start, end = (t.strftime("%H:%M") for t in m.scan_window)
        lines.append(f"{m.flag} {m.name}: {universe_of(code)} · {start}–{end} {TZ_LABEL[code]}\n   {scan_settings(code)}")
    telegram_bot.send_message(
        "🤖 <b>Scanner started</b>\n" + "\n".join(lines) +
        f"\nTF: {', '.join(config.SCAN_TIMEFRAMES)} · every {config.SCAN_INTERVAL_MIN} min")
    summary_sent_for: dict[str, object] = {}  # market -> exchange date whose EOD scan succeeded
    header_sent_for: dict[str, object] = {}   # market -> exchange date whose EOD header was sent
    while not _quit.is_set():
        began = monotonic()
        housekeeping()
        for code in codes:
            if _quit.is_set():
                break
            m = markets.get(code)
            now = m.now()  # exchange time
            try:
                if m.in_scan_window(now):
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
        # The interval runs from the start of a loop, so scans stay SCAN_INTERVAL_MIN apart however long they take
        wait = max(30, round(config.SCAN_INTERVAL_MIN * 60 - (monotonic() - began)))
        for code in codes:
            try:
                set_status(code, next_at=(datetime.now() + timedelta(seconds=wait)).isoformat(timespec="seconds"))
            except Exception:
                log.exception("Saving the scan status failed")
        _pause(wait)


def run_backtest(code: str, symbols: str | None) -> None:
    from algotrade import backtest, costs
    syms = get_universe(symbols, market=code)
    log.info("Backtesting %d %s symbols on %s ...", len(syms), markets.get(code).name, config.TIMEFRAME)
    trades = backtest.run(syms, config.TIMEFRAME, period="5y" if config.TIMEFRAME == "1d" else "60d", market=code,
                          costs=costs.model_for(code, config.TIMEFRAME), **scanner_kw(code))
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
