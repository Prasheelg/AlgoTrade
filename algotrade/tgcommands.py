"""Commands to the Telegram bot, answered by the dashboard (TELEGRAM_COMMANDS=true):

/positions  open signals and Auto-trade positions      /pnl      today's account P&L and closed trades
/signals    today's new signals                         /summary  the daily summary, now
/status     health check                                /stop     switch Auto-trade OFF
/watch TCS · /unwatch TCS · /alert TCS above 2500 · /alerts · /help

Only messages from the configured group (TELEGRAM_CHAT_ID) are answered; everyone in it can use them. Nothing
here places an order or switches Auto-trade on: the most it does is switch trading off. Updates are read
by long polling (getUpdates); the last one seen is kept in signals.db so nothing is answered twice."""
import html
import logging
import time

import requests

from . import broker, config, state, telegram_bot

log = logging.getLogger(__name__)
OFFSET_KEY = "telegram_update_offset"
POLL_SEC = 25

HELP = ("<b>AlgoTrade commands</b>\n/positions · open signals and Auto-trade positions\n/pnl · today's P&amp;L\n"
        "/signals · today's new signals\n/summary · the daily summary now\n/status · health check\n"
        "/stop · switch Auto-trade OFF\n/watch TCS · /unwatch TCS · add / remove from the watchlist\n"
        "/alert TCS above 2500 · price alert (or below)\n/alerts · active price alerts")


def _positions(_args) -> str:
    from . import orders
    sigs = [s for s in state.all_signals("IN") if s["status"] in ("ACTIVE", "T1_HIT", "PENDING")]
    lines = []
    for s in sigs[:20]:
        px, fill = s["ltp"] or s["entry"], s["fill_price"] or s["entry"]
        pnl = (px - fill) / fill * 100 * (1 if s["side"] == "BUY" else -1)
        lines.append(f"{s['side']} {s['symbol']} · {s['status'].replace('_', ' ').lower()}"
                     + ("" if s["status"] == "PENDING" else f" · {pnl:+.1f}%") + f" · stop ₹{s['stop_loss']:,.2f}")
    live = [t for t in orders.all_trades() if t["state"] in orders.ACTIVE_STATES]
    out = [f"<b>Open signals</b> ({len(sigs)})"] + (lines or ["none"])
    if live:
        out += [f"\n<b>Auto-trade</b> ({len(live)}, ₹{orders.open_risk():,.0f} at risk)"] + \
               [f"{t['symbol']} × {t['qty']} · {t['state'].lower()} · stop ₹{t['stop']:,.2f}" for t in live]
    return "\n".join(out)


def _pnl(_args) -> str:
    from . import summary
    from datetime import date
    lines = summary._account() + summary._signals(date.today().isoformat())[:6]
    return "<b>Today</b>\n" + ("\n".join(lines) if lines else "Nothing to report yet.")


def _signals(_args) -> str:
    from datetime import date
    today = date.today().isoformat()
    new = [s for s in state.all_signals() if (s["sent_at"] or "")[:10] == today]
    return (f"<b>New signals today</b> ({len(new)})\n" +
            "\n".join(f"{s['side']} {s['symbol']} ({s['market']}, {s['timeframe']}) · entry {s['entry']:,.2f} · "
                      f"stop {s['stop_loss']:,.2f}" for s in new[:20])) if new else "No new signals today."


def _summary(_args) -> str:
    from . import summary
    return summary.build()


def _status(_args) -> str:
    from . import health
    probs = health.problems()
    trading = "ON" if broker.trading_enabled() else "off"
    return f"Auto-trade: <b>{trading}</b>\n" + ("\n".join(("🚨 " if p["level"] == "critical" else "⚠️ ") + html.escape(p["text"])
                                                     for p in probs) if probs else "✅ All checks OK")


def _stop(_args) -> str:
    if not broker.trading_enabled():
        return "Auto-trade is already off."
    broker.set_trading(False, by="Telegram /stop")
    return "🛑 Auto-trade switched <b>OFF</b>. Unfilled entries are cancelled on the scanner's next loop; open positions keep their stops."


def _watch(args) -> str:
    from . import watchlist
    if not args:
        return "Usage: /watch TCS"
    return f"Added {watchlist.add(args[0])} to the watchlist."


def _unwatch(args) -> str:
    from . import watchlist
    if not args:
        return "Usage: /unwatch TCS"
    watchlist.remove(args[0])
    return f"Removed {args[0].upper()} from the watchlist."


def _alert(args) -> str:
    from . import watchlist
    if len(args) < 3:
        return "Usage: /alert TCS above 2500 (or below)"
    a = watchlist.add_alert(args[0], args[1], args[2].replace(",", "").lstrip("₹"), " ".join(args[3:]))
    return f"🔔 Alert set: {a['symbol']} {a['op']} ₹{a['price']:,.2f}"


def _alerts(_args) -> str:
    from . import watchlist
    act = [a for a in watchlist.alerts() if not a["triggered_at"]]
    return ("<b>Active price alerts</b>\n" + "\n".join(f"{a['symbol']} {a['op']} ₹{a['price']:,.2f}" for a in act)
            if act else "No active price alerts.")


COMMANDS = {"help": lambda a: HELP, "start": lambda a: HELP, "positions": _positions, "pnl": _pnl,
            "signals": _signals, "summary": _summary, "status": _status, "stop": _stop, "watch": _watch,
            "unwatch": _unwatch, "alert": _alert, "alerts": _alerts}


def answer(text: str) -> str | None:
    """Reply to one message, or None if it isn't a command for us."""
    if not text or not text.startswith("/"):
        return None
    word, *args = text.split()
    name = word[1:].split("@")[0].lower()
    fn = COMMANDS.get(name)
    if fn is None:
        return None
    try:
        return fn(args)
    except ValueError as exc:
        return f"⚠️ {html.escape(str(exc))}"
    except Exception as exc:
        log.exception("Telegram command /%s failed", name)
        return f"⚠️ /{name} failed: {html.escape(str(exc)[:200])}"


def poll_once(timeout: int = POLL_SEC) -> int:
    """Read new updates and answer commands from our group. Returns how many were answered."""
    if not (config.TELEGRAM_COMMANDS and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
        return 0
    offset = state.get_setting(OFFSET_KEY, 0)[0] or 0
    url = telegram_bot.API.format(token=config.TELEGRAM_BOT_TOKEN, method="getUpdates")
    resp = requests.get(url, params={"offset": offset, "timeout": timeout, "allowed_updates": '["message"]'},
                        timeout=timeout + 10)
    if not resp.ok:
        log.warning("Telegram getUpdates %s: %s", resp.status_code, resp.text[:200])
        return 0
    answered = 0
    for u in resp.json().get("result", []):
        offset = max(offset, int(u["update_id"]) + 1)
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(config.TELEGRAM_CHAT_ID):
            continue   # only our group
        reply = answer(msg.get("text") or "")
        if reply:
            telegram_bot.send_message(reply)
            answered += 1
    state.set_setting(OFFSET_KEY, offset)
    return answered


def run_forever() -> None:
    while True:
        if not (config.TELEGRAM_COMMANDS and config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID):
            time.sleep(60)   # off or not configured: check again later (Settings can switch it on)
            continue
        try:
            poll_once()
        except requests.RequestException as exc:
            log.warning("Telegram commands: %s", exc)
            time.sleep(15)
        except Exception:
            log.exception("Telegram commands failed")
            time.sleep(30)
