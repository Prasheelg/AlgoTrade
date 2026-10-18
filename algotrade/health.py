"""Health checks, so a silent failure gets noticed: shown as a banner on the dashboard (/api/health) and posted to
Telegram when a problem starts and when it clears.

- Auto-trade positions without a working stop-loss (critical);
- the Kotak login failing (critical);
- the scanner not running while Auto-trade is ON, since the scanner places and watches its orders (critical);
- live prices not updating during India market hours (warning);
- the live price stream (livefeed.py) down for FEED_DOWN_MIN in market hours (warning);
- the daily database backup missing for two days, when DB_BACKUP is on (warning).
The scanner writes a heartbeat every loop (beat()); the dashboard runs the checks (watch_once())."""
import logging
import os
import sys
from datetime import datetime, timedelta

from . import backup, broker, config, kotak, markets, state, telegram_bot

log = logging.getLogger(__name__)
HEARTBEAT = "scanner_heartbeat"
OPEN_GRACE_MIN = 20       # after the open, give the scanner time to place the day's stops before flagging them
PRICE_STALE_MIN = 5       # Kotak's index quote older than this in market hours = prices not updating
BACKUP_STALE_DAYS = 2
FEED_DOWN_MIN = 3

_alerted: dict[str, dict] = {}   # problem id -> problem, as last posted to Telegram (this process)


def beat() -> None:
    """Called by the scanner every loop."""
    state.set_setting(HEARTBEAT, {"pid": os.getpid()})


def gone() -> None:
    """Called by the scanner as it exits, so it shows as stopped at once rather than after two loops."""
    state.set_setting(HEARTBEAT, {"pid": None, "stopped": True})


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return not ok or code.value == 259             # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _age_min(ts: str | None, now: datetime) -> float | None:
    return (now - datetime.fromisoformat(ts)).total_seconds() / 60 if ts else None


def scanner_age_min(now: datetime | None = None) -> float | None:
    """Minutes since the scanner's last heartbeat, or None if it was never seen."""
    _, at = state.get_setting(HEARTBEAT, None)
    return _age_min(at, now or datetime.now())


def scanner_running(now: datetime | None = None) -> bool:
    """The scanner beat within two loops (plus slack): it is there to place stops and watch Auto-trade orders.
    A scanner that exited (closed window, switched off) doesn't count, however recent its last beat."""
    age = scanner_age_min(now)
    if age is None or age > 2 * config.SCAN_INTERVAL_MIN + 5:
        return False
    beat_ = state.get_setting(HEARTBEAT, None)[0] or {}
    if beat_.get("stopped"):
        return False
    return not beat_.get("pid") or _pid_alive(beat_["pid"])


def _stops(now_ist: datetime, open_: bool) -> list[dict]:
    from . import orders
    live = [t for t in orders.all_trades() if t["state"] == orders.OPEN]
    if not live:
        return []
    out = []
    opened_at = datetime.combine(now_ist.date(), markets.get("IN").session[0])
    settled = open_ and now_ist.replace(tzinfo=None) >= opened_at + timedelta(minutes=OPEN_GRACE_MIN)
    book = None
    if settled:
        try:
            book = {str(o.get("nOrdNo")): o for o in (broker.call("order_report").get("data") or [])}
        except Exception as exc:
            log.warning("Health: order book unavailable (%s)", exc)
    for t in live:
        sym = t["symbol"]
        if (t["note"] or "").startswith("NO STOP"):
            out.append({"id": f"stop:{t['key']}", "level": "critical",
                        "text": f"{sym}: no stop-loss ({t['note'][9:].strip() or 'not placed'}). "
                                "Place one in the Kotak app or Close the trade."})
        elif book is not None:
            st = orders.order_state(book.get(str(t["stop_order"]))) if t["stop_order"] else "NONE"
            if st != "OPEN":
                out.append({"id": f"stop:{t['key']}", "level": "critical",
                            "text": f"{sym}: the stop-loss order isn't working ({st.lower()}). The scanner re-places "
                                    "it on its next loop; check it's running."})
    return out


def problems(last_refresh: str | None = None, now: datetime | None = None) -> list[dict]:
    """Current problems, most serious first: [{id, level: critical | warning, text}]."""
    now = now or datetime.now()
    india = markets.get("IN")
    now_ist = india.now()
    open_ = india.is_open(now_ist)
    out: list[dict] = []
    kotak_on = not broker.missing_credentials()
    if kotak_on:
        lg = broker.login_status()
        if not lg.get("ok"):
            out.append({"id": "kotak_login", "level": "critical",
                        "text": f"Kotak login is failing: {lg.get('error') or 'unknown error'}"})
        try:
            out += _stops(now_ist, open_)
        except Exception:
            log.exception("Health: stop-loss check failed")
    if broker.trading_enabled() and not scanner_running(now):
        age = scanner_age_min(now)
        out.append({"id": "scanner", "level": "critical",
                    "text": "Auto-trade is ON but the scanner isn't running "
                            + (f"(last seen {age:.0f} min ago)" if age is not None else "(never seen)")
                            + ", so nothing places or watches its orders. Start run.bat."})
    if open_ and kotak.enabled():
        q = kotak.index_quote("Nifty 50")
        age = _age_min(q["time"].isoformat(), now_ist.replace(tzinfo=None)) if q and q.get("time") else None
        if age is None or age > PRICE_STALE_MIN:
            out.append({"id": "prices", "level": "warning",
                        "text": "Live prices aren't updating (Kotak quotes " +
                                ("failing)" if age is None else f"{age:.0f} min old)")})
    from . import livefeed
    down = livefeed.down_minutes()
    if open_ and down is not None and down >= FEED_DOWN_MIN:
        err = livefeed.status()["error"]
        out.append({"id": "feed", "level": "warning",
                    "text": f"The live price stream has been down for {down:.0f} min" + (f" ({err})" if err else "")
                            + "; prices are fetched every few seconds instead"})
    if open_ and last_refresh:
        age = _age_min(last_refresh.replace(" ", "T"), now)
        if age is not None and age > 20:
            out.append({"id": "tracker", "level": "warning",
                        "text": f"Open signals were last re-checked {age:.0f} min ago"})
    b = backup.status()
    if config.DB_BACKUP and b.get("day"):
        days = (now_ist.date() - datetime.fromisoformat(b["day"]).date()).days
        if days >= BACKUP_STALE_DAYS:
            out.append({"id": "backup", "level": "warning", "text": f"The last database backup is {days} days old"})
    out.sort(key=lambda p: p["level"] != "critical")
    return out


def watch_once(last_refresh: str | None = None) -> list[dict]:
    """Run the checks; post new problems and cleared ones to Telegram. Returns the current problems."""
    now = problems(last_refresh)
    ids = {p["id"] for p in now}
    for p in now:
        if p["id"] not in _alerted:
            _send(("🚨 " if p["level"] == "critical" else "⚠️ ") + f"<b>Health</b> · {p['text']}")
            if p["level"] == "critical":
                from . import push
                push.send("AlgoTrade needs attention", p["text"])
            _alerted[p["id"]] = p
    for pid in [k for k in _alerted if k not in ids]:
        _send(f"✅ <b>Health</b> · resolved: {_alerted.pop(pid)['text']}")
    return now


def _send(text: str) -> None:
    try:
        telegram_bot.send_message(text)
    except Exception:
        log.exception("Telegram health notice failed")
