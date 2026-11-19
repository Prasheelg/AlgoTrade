"""Change in open interest for option contracts.

Kotak's quotes and stream carry only the current OI, not the previous session's, so this keeps it: for every
contract a chain has quoted, the last OI seen on its latest trading day and on the trading day before that
(each dated by the quote's own time, so a quote read after the close or before the open counts for the session
it belongs to). Change in OI = today's OI - the previous day's last OI. After the close, the chains looked at
that day are read once more so tomorrow's change starts from the final figure.

Saved to DATA_DIR/oi_book.json; contracts not seen for PRUNE_DAYS are dropped."""
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta

from . import config

log = logging.getLogger(__name__)
PATH = config.DATA_DIR / "oi_book.json"
SAVE_EVERY_SEC = 30
PRUNE_DAYS = 10
CLOSE_SNAPSHOT_AT = (15, 32)   # IST: OI is final for the day a little after 15:30

_lock = threading.Lock()
_book: dict | None = None      # token -> {"d": day, "oi": oi, "pd": previous day, "poi": previous day's oi}
_state = {"dirty": False, "saved": 0.0, "snapshot_day": None}
_viewed: dict = {}             # (underlying, expiry, width) -> day it was last looked at


def _load() -> dict:
    global _book
    if _book is None:
        try:
            _book = json.loads(PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _book = {}
    return _book


def _save(force: bool = False) -> None:
    if not _state["dirty"] or (not force and time.monotonic() - _state["saved"] < SAVE_EVERY_SEC):
        return
    cutoff = (date.today() - timedelta(days=PRUNE_DAYS)).isoformat()
    book = {k: v for k, v in _load().items() if v["d"] >= cutoff}
    try:
        tmp = PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(book, separators=(",", ":")), encoding="utf-8")
        tmp.replace(PATH)
        _state.update(dirty=False, saved=time.monotonic())
    except OSError as exc:
        log.warning("Could not save the OI book: %s", exc)


def record(token: str, quote_time: str | None, oi: float | None) -> dict:
    """Note a contract's OI as of its quote time; return {"oi_chg", "oi_prev", "oi_prev_day"} (None if unknown)."""
    none = {"oi_chg": None, "oi_prev": None, "oi_prev_day": None}
    if oi is None or not quote_time:
        return none
    day = quote_time[:10]
    with _lock:
        book = _load()
        e = book.get(token)
        if e is None or day > e["d"]:
            e = book[token] = {"d": day, "oi": oi, "pd": e["d"] if e else None, "poi": e["oi"] if e else None}
            _state["dirty"] = True
        elif day == e["d"]:
            if e["oi"] != oi:
                e["oi"] = oi
                _state["dirty"] = True
        else:          # an older quote than one already seen (e.g. a stale stream value): ignore
            return none
        _save()
        if e.get("poi") is None:
            return none
        return {"oi_chg": oi - e["poi"], "oi_prev": e["poi"], "oi_prev_day": e["pd"]}


def viewed(underlying: str, expiry: str, width: int) -> None:
    _viewed[(underlying, expiry, width)] = date.today().isoformat()


def close_snapshot(read_chain, now: datetime | None = None) -> int:
    """Once a day after the close, re-read every chain looked at today (read_chain(u, expiry, width)) so the
    book holds each contract's final OI. Returns how many chains were read."""
    now = now or datetime.now()
    today = now.date().isoformat()
    if _state["snapshot_day"] == today or now.weekday() >= 5 or (now.hour, now.minute) < CLOSE_SNAPSHOT_AT:
        return 0
    _state["snapshot_day"] = today
    n = 0
    for (u, expiry, width), day in list(_viewed.items()):
        if day != today:
            continue
        try:
            read_chain(u, expiry, width, fresh=True)
            n += 1
        except Exception as exc:
            log.warning("Closing OI snapshot of %s %s failed: %s", u, expiry, exc)
    with _lock:
        _save(force=True)
    if n:
        log.info("Closing OI snapshot: %d option chain(s)", n)
    return n
