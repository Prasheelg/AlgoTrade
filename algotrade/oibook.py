"""Change in open interest for option contracts.

Kotak's quotes and stream carry only the current OI, not the previous session's. The previous session's
closing OI comes from NSE's F&O bhavcopy (one zip a day, every contract; its FinInstrmId is Kotak's token),
cached in DATA_DIR. As a fallback when NSE can't be reached, this also keeps its own record: for every
contract a chain has quoted, the last OI seen on its latest trading day and on the day before (each dated by
the quote's own time). After the close, the chains looked at that day are read once more.

Change in OI = the quote's OI - the previous session's closing OI. The own record is saved to
DATA_DIR/oi_book.json; contracts not seen for PRUNE_DAYS are dropped."""
import csv
import io
import json
import logging
import threading
import time
import zipfile
from datetime import date, datetime, timedelta

import requests

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

BHAV_URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d}_F_0000.csv.zip"
BHAV_LOOKBACK_DAYS = 7         # holidays and weekends: walk back this far for the previous session
BHAV_RETRY_SEC = 900           # after a failed download, don't ask NSE for that day again for this long
_bhav: dict = {}               # "YYYY-MM-DD" -> {token: closing oi}, or None (not published / failed)
_bhav_miss: dict = {}          # "YYYY-MM-DD" -> monotonic time of the last failed try
_bhav_lock = threading.Lock()


def parse_bhavcopy(csv_text: str) -> dict:
    """{token: closing OI} for the options in an NSE F&O bhavcopy (UDiFF format)."""
    out = {}
    for r in csv.DictReader(io.StringIO(csv_text)):
        if r.get("OptnTp") in ("CE", "PE") and r.get("FinInstrmId") and r.get("OpnIntrst"):
            try:
                out[r["FinInstrmId"].strip()] = float(r["OpnIntrst"])
            except ValueError:
                pass
    return out


def _bhav_day(day: str) -> dict | None:
    """Closing OI of every option on `day` (from the disk cache, else NSE); None if there is none yet."""
    if day in _bhav:
        return _bhav[day]
    cache = config.DATA_DIR / f"nse_fo_oi_{day}.json"
    try:
        _bhav[day] = json.loads(cache.read_text(encoding="utf-8"))
        return _bhav[day]
    except (OSError, ValueError):
        pass
    if time.monotonic() - _bhav_miss.get(day, -1e9) < BHAV_RETRY_SEC:
        return None
    try:
        r = requests.get(BHAV_URL.format(d=day.replace("-", "")), timeout=20,
                         headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
        if r.status_code != 200 or not r.content.startswith(b"PK"):
            raise ValueError(f"HTTP {r.status_code}")
        z = zipfile.ZipFile(io.BytesIO(r.content))
        oi = parse_bhavcopy(z.read(z.namelist()[0]).decode("utf-8", "replace"))
    except Exception as exc:
        _bhav_miss[day] = time.monotonic()
        log.debug("No NSE F&O bhavcopy for %s: %s", day, exc)
        return None
    _bhav[day] = oi
    try:
        cache.write_text(json.dumps(oi, separators=(",", ":")), encoding="utf-8")
        for old in sorted(config.DATA_DIR.glob("nse_fo_oi_*.json"))[:-PRUNE_DAYS]:
            old.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("Could not cache the F&O bhavcopy: %s", exc)
    return oi


def previous_close(day: str) -> tuple[str, dict] | tuple[None, None]:
    """The latest session before `day` with a published bhavcopy: (its date, {token: closing OI})."""
    d = date.fromisoformat(day)
    with _bhav_lock:
        for back in range(1, BHAV_LOOKBACK_DAYS + 1):
            prev = d - timedelta(days=back)
            if prev.weekday() >= 5:
                continue
            oi = _bhav_day(prev.isoformat())
            if oi:
                return prev.isoformat(), oi
    return None, None


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
        own = (e["pd"], e.get("poi"))
    pday, prev = previous_close(day)
    if prev is not None and str(token) in prev:
        own = (pday, prev[str(token)])
    if own[1] is None:
        return none
    return {"oi_chg": oi - own[1], "oi_prev": own[1], "oi_prev_day": own[0]}


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
