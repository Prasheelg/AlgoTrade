"""Daily summary on Telegram after the NSE close: the account's day, trades closed today, open positions with
their distance to stop and target, new signals, and today's orders. One message instead of scattered alerts.

Sent once per trading day after SUMMARY_AFTER IST by whichever process (dashboard or scanner) gets there first;
DAILY_SUMMARY=false turns it off. /summary on Telegram sends it on demand."""
import html
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from . import config, markets, state, telegram_bot

log = logging.getLogger(__name__)
SUMMARY_AFTER = time(15, 45)   # IST
KEY = "summary_last"
MAX_LINES = 12


def _pct(a: float, b: float) -> float:
    return (a - b) / b * 100 if b else 0.0


def _account() -> list[str]:
    from . import broker, portfolio
    if broker.missing_credentials():
        return []
    try:
        p = portfolio.report(refresh=True)
    except Exception:
        log.exception("Summary: account unavailable")
        return []
    if p.get("error"):
        return [f"Account: {html.escape(p['error'][:120])}"]
    t, f = p["totals"], p["funds"] or {}
    rows = []
    if t.get("count"):
        rows.append(["Holdings", f"₹{t['value']:,.0f}"])
        if t.get("day_pnl") is not None:
            rows.append(["Day P&L", f"{t['day_pnl']:+,.0f}"])
        if t.get("pnl") is not None:
            rows.append(["Total P&L", f"{t['pnl']:+,.0f}"])
    if f.get("available") is not None:
        rows.append(["Cash available", f"₹{f['available']:,.0f}"])
    return ["💼 <b>Account</b>", telegram_bot.table(rows)] if rows else []



def _signals(day: str) -> list[str]:
    from . import performance
    out = []
    closed = [t for t in performance.closed_trades("IN") if t["closed_at"] == day]
    if closed:
        tot = sum(t["net_r"] for t in closed)
        out.append(f"✅ <b>Closed today</b> ({len(closed)}, {tot:+.2f}R after costs)")
        out.append(telegram_bot.table([[t["symbol"], performance.EXIT_TEXT[t["status"]].lower(), f"{t['net_r']:+.2f}R"]
                                       for t in closed[:MAX_LINES]], header=["Stock", "Exit", "R"], align="llr"))
        out.append(telegram_bot.more(len(closed), MAX_LINES))
    sigs = state.all_signals("IN")
    new = [s for s in sigs if (s["sent_at"] or "")[:10] == day]
    if new:
        out.append(f"\n🆕 <b>New signals</b> ({len(new)})")
        out.append(telegram_bot.table([[s["symbol"], s["side"], s["timeframe"], telegram_bot.px(s["entry"])] for s in new[:MAX_LINES]],
                                      header=["Stock", "Side", "TF", "Entry"], align="lllr"))
        out.append(telegram_bot.more(len(new), MAX_LINES))
    active = [s for s in sigs if s["status"] == "ACTIVE"]
    if active:
        out.append(f"\n📈 <b>Open signals</b> ({len(active)}) · % to stop / target")
        rows = []
        for s in active[:MAX_LINES]:
            px, fill = s["ltp"] or s["entry"], s["fill_price"] or s["entry"]
            sign = 1 if s["side"] == "BUY" else -1
            rows.append([s["symbol"], f"{sign * _pct(px, fill):+.1f}%", f"{abs(_pct(s['stop_loss'], px)):.1f}%",
                         f"{abs(_pct(s['target1'], px)):.1f}%"])
        out.append(telegram_bot.table(rows, header=["Stock", "P&L", "Stop", "Target"]))
        out.append(telegram_bot.more(len(active), MAX_LINES))
    pending = [s for s in sigs if s["status"] == "PENDING"]
    if pending:
        out.append(f"Pending entries: {len(pending)}")
    return out


def _app_trades() -> list[str]:
    from . import orders
    live = [t for t in orders.all_trades() if t["state"] in (orders.OPEN, orders.EXITING)]
    if not live:
        return []
    out = [f"🤖 <b>Auto-trade positions</b> ({len(live)}, ₹{orders.open_risk():,.0f} at risk at the stops)"]
    out.append(telegram_bot.table([[t["symbol"], t["qty"], telegram_bot.px(t["fill_price"] or t["entry"]),
                                    "NO STOP" if (t["note"] or "").startswith("NO STOP") else telegram_bot.px(t["stop"])]
                                   for t in live[:MAX_LINES]], header=["Stock", "Qty", "Fill", "Stop"]))
    nostop = [t["symbol"] for t in live if (t["note"] or "").startswith("NO STOP")]
    if nostop:
        out.append("⚠️ No stop order: " + ", ".join(html.escape(x) for x in nostop))
    return out


def _orders(day: str) -> list[str]:
    from . import orderbook
    rows = [o for o in orderbook.history(2) if o["day"] == day]
    if not rows:
        return []
    count = {}
    for o in rows:
        count[o["state"]] = count.get(o["state"], 0) + 1
    out = ["🧾 <b>Orders today</b>", telegram_bot.table([[k.lower().replace("_", " "), n] for k, n in sorted(count.items())])]
    out += [f"  ⛔ {o['side']} {o['symbol']}: {html.escape((o['reason'] or '')[:90])}"
            for o in rows if o["state"] == "REJECTED"][:5]
    return out


def build(now: datetime | None = None) -> str:
    now = now or datetime.now(ZoneInfo(markets.get("IN").tz))
    day = now.date().isoformat()
    parts = [f"📋 <b>Daily summary</b> · {now:%a %d %b}"]
    for section in (_account, lambda: _signals(day), _app_trades, lambda: _orders(day)):
        try:
            lines = section()
        except Exception:
            log.exception("Summary section failed")
            lines = []
        lines = [x for x in lines if x]
        if lines:
            parts.append("\n".join(lines))
    try:
        from . import health
        probs = health.problems()
        if probs:
            parts.append("⚠️ <b>Needs attention</b>\n" + "\n".join(f"  {html.escape(p['text'])}" for p in probs))
    except Exception:
        log.exception("Summary: health check failed")
    if len(parts) == 1:
        parts.append("A quiet day: no trades, signals or orders.")
    return "\n\n".join(parts)


def daily(now: datetime | None = None) -> bool:
    """Send today's summary if it's due (weekday, after SUMMARY_AFTER) and not sent yet."""
    if not config.DAILY_SUMMARY:
        return False
    now = now or datetime.now(ZoneInfo(markets.get("IN").tz))
    day = now.date().isoformat()
    if now.weekday() >= 5 or now.time() < SUMMARY_AFTER or (state.get_setting(KEY, {})[0] or {}).get("day") == day:
        return False
    state.set_setting(KEY, {"day": day})   # first, so the other process doesn't send it too
    text = build(now)
    telegram_bot.send_message(text)
    try:
        from . import push
        push.send("Daily summary", "Today's trading summary is on Telegram", "/#IN/performance")
    except Exception:
        log.exception("Summary push failed")
    return True
