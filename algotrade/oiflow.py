"""Open-interest change by strike over a time window (the Strategy Builder's OI change chart).

While the NSE is open, a background thread reads the tracked option chains once a minute and keeps each
contract's OI (and the underlying's price) per minute, so the change over the last 5 minutes, hour, etc. can be
worked out. Tracked: NIFTY and BANKNIFTY's nearest expiry from the open, plus every underlying / expiry the
chart has been asked for today. "Full day" needs no history: it is the change since the previous session's
close (oibook, from NSE's bhavcopy). The minute history is saved to DATA_DIR/oi_intraday_<day>.json so a
restart keeps it."""
import json
import logging
import threading
import time
from datetime import datetime, timedelta

from . import config, fno, markets

log = logging.getLogger(__name__)
SNAP_SEC = 60
TRACK_WIDTH = 25                     # strikes either side of the money recorded per expiry
DEFAULT_TRACK = ("NIFTY", "BANKNIFTY")
SAVE_EVERY_SEC = 300
WINDOWS = (5, 10, 15, 30, 60, 120, 180)   # minutes the chart offers, besides the full day

_lock = threading.Lock()
_hist = {"day": None, "times": [], "oi": {}, "spot": {}}   # oi: tsym -> {"HH:MM": oi}; spot: underlying -> {"HH:MM": ltp}
_track: dict = {}                    # (underlying, expiry) -> day it was asked for
_state = {"saved": 0.0, "dirty": False, "thread": None}


def _nse():
    return markets.MARKETS["IN"]


def _path(day: str):
    return config.DATA_DIR / f"oi_intraday_{day}.json"


def _load(day: str) -> None:
    _hist.update(day=day, times=[], oi={}, spot={})
    try:
        saved = json.loads(_path(day).read_text(encoding="utf-8"))
        _hist.update(times=saved["times"], oi=saved["oi"], spot=saved["spot"])
    except (OSError, ValueError, KeyError):
        pass


def _ensure_day(day: str) -> None:
    """Start recording `day` (from disk after a restart); keep only the last few days' files."""
    if _hist["day"] == day:
        return
    _load(day)
    for old in sorted(config.DATA_DIR.glob("oi_intraday_*.json"))[:-3]:
        old.unlink(missing_ok=True)


def _ensure_latest() -> None:
    """Outside market hours: the most recent session's history (e.g. Friday's on a Saturday)."""
    if _hist["day"] is None:
        files = sorted(config.DATA_DIR.glob("oi_intraday_*.json"))
        if files:
            _load(files[-1].stem.removeprefix("oi_intraday_"))


def _save(force: bool = False) -> None:
    if not _state["dirty"] or (not force and time.monotonic() - _state["saved"] < SAVE_EVERY_SEC):
        return
    try:
        tmp = _path(_hist["day"]).with_suffix(".tmp")
        tmp.write_text(json.dumps({k: _hist[k] for k in ("times", "oi", "spot")}, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_path(_hist["day"]))
        _state.update(saved=time.monotonic(), dirty=False)
    except OSError as exc:
        log.warning("Could not save the OI history: %s", exc)


def track(underlying: str, expiry: str) -> None:
    _track[(underlying, expiry)] = _nse().now().date().isoformat()


def tracked(today: str) -> list[tuple[str, str]]:
    out = [k for k, d in _track.items() if d == today]
    for u in DEFAULT_TRACK:
        try:
            exp = fno.expiries(u)
        except Exception:
            continue
        if exp and (u, exp[0]) not in out:
            out.append((u, exp[0]))
    return out


def record(chains: list[dict], stamp: str) -> None:
    """Store one minute's OI of these chains (fno.chain results) under `stamp` ("HH:MM")."""
    with _lock:
        if not _hist["times"] or _hist["times"][-1] != stamp:
            _hist["times"].append(stamp)
        for c in chains:
            if c["spot"].get("ltp") is not None:
                _hist["spot"].setdefault(c["underlying"], {})[stamp] = c["spot"]["ltp"]
            for r in c["rows"]:
                for leg in (r["ce"], r["pe"]):
                    if leg and leg.get("oi") is not None:
                        _hist["oi"].setdefault(leg["tsym"], {})[stamp] = leg["oi"]
        _state["dirty"] = True
        _save()


def snapshot(now: datetime | None = None) -> int:
    """Read every tracked chain once (market hours only). Returns how many were recorded."""
    now = now or _nse().now()
    if not _nse().is_open(now):
        return 0
    today = now.date().isoformat()
    with _lock:
        _ensure_day(today)
    chains = []
    for u, exp in tracked(today):
        try:
            chains.append(fno.chain(u, exp, TRACK_WIDTH))
        except Exception as exc:
            log.debug("OI snapshot of %s %s failed: %s", u, exp, exc)
    if chains:
        record(chains, now.strftime("%H:%M"))
    return len(chains)


def run_forever() -> None:
    while True:
        t0 = time.monotonic()
        try:
            snapshot()
        except Exception:
            log.exception("OI snapshot failed")
        with _lock:
            if _hist["day"] and not _nse().is_open():
                _save(force=True)
        time.sleep(max(5.0, SNAP_SEC - (time.monotonic() - t0)))


def start() -> None:
    if _state["thread"] is None:
        _state["thread"] = threading.Thread(target=run_forever, name="oi-history", daemon=True)
        _state["thread"].start()


def oi_at_open(tsym: str, day: str) -> float | None:
    """A contract's OI at the first minute recorded on `day` (the open, for NIFTY's nearest expiry), or None."""
    with _lock:
        if _hist["day"] != day:
            return None
        series = _hist["oi"].get(tsym) or {}
        return series[min(series)] if series else None


def _at(series: dict, stamp: str):
    """The value recorded at or just before `stamp` ("HH:MM"), or None."""
    best = None
    for t in sorted(series):
        if t > stamp:
            break
        best = series[t]
    return best


def change(underlying: str, expiries: list[str], width: int, window: int | None,
           now: datetime | None = None) -> dict:
    """OI and its change per strike, summed over `expiries`, for the `width` strikes either side of the money.
    window = minutes back (one of WINDOWS), or None for the full day (since the previous session's close)."""
    now = now or _nse().now()
    today = now.date().isoformat()
    is_open = _nse().is_open(now)
    with _lock:
        _ensure_day(today) if is_open else _ensure_latest()
    for e in expiries:
        track(underlying, e)
    chains = [fno.chain(underlying, e, max(width, 1)) for e in expiries]
    base = chains[0]
    since, spot_from, note = None, None, None
    if window:
        times = list(_hist["times"])
        # after the close the window ends at the last minute recorded, not at the clock
        end = now if is_open or not times else datetime.combine(now.date(), datetime.strptime(times[-1], "%H:%M").time())
        want = (end - timedelta(minutes=window)).strftime("%H:%M")
        earlier = [t for t in times if t <= want]
        since = earlier[-1] if earlier else (times[0] if times else None)
        if since is None:
            note = ("Nothing recorded yet today: OI is recorded every minute and this window fills in shortly." if is_open else
                    "Intraday OI is recorded every minute while the NSE is open, so the short windows fill in from the next session. "
                    "Full day works now.")
        elif not earlier:
            note = f"Recording began at {since}, so this shows the change since then."
        spot_from = _at(_hist["spot"].get(underlying, {}), since) if since else None
    else:
        sp = base["spot"]
        spot_from = sp["ltp"] - sp["change"] if sp.get("ltp") is not None and sp.get("change") is not None else None
    rows: dict = {}
    for c in chains:
        for r in c["rows"]:
            row = rows.setdefault(r["strike"], {"strike": r["strike"], "ce_oi": 0.0, "ce_chg": 0.0, "pe_oi": 0.0, "pe_chg": 0.0,
                                                "ce_known": True, "pe_known": True})
            for k in ("ce", "pe"):
                leg = r[k]
                if not leg or leg.get("oi") is None:
                    continue
                row[f"{k}_oi"] += leg["oi"]
                if window:
                    prev = _at(_hist["oi"].get(leg["tsym"], {}), since) if since else None
                    chg = leg["oi"] - prev if prev is not None else None
                else:
                    chg = leg.get("oi_chg")
                if chg is None:
                    row[f"{k}_known"] = False
                else:
                    row[f"{k}_chg"] += chg
    out_rows = [rows[k] for k in sorted(rows)]
    tot = lambda f: sum(r[f] for r in out_rows)
    ce_oi, pe_oi = tot("ce_oi"), tot("pe_oi")
    return {"underlying": underlying, "expiries": expiries, "width": width, "window": window,
            "spot": base["spot"].get("ltp"), "spot_from": spot_from, "atm": base["atm"],
            "since": since, "prev_day": next((leg.get("oi_prev_day") for r in base["rows"] for leg in (r["ce"], r["pe"])
                                              if leg and leg.get("oi_prev_day")), None),
            "rows": out_rows, "ce_chg": tot("ce_chg"), "pe_chg": tot("pe_chg"), "ce_oi": ce_oi, "pe_oi": pe_oi,
            "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None, "note": note,
            "history_from": _hist["times"][0] if _hist["times"] else None,
            "history_to": _hist["times"][-1] if _hist["times"] else None, "history_day": _hist["day"],
            "as_of": now.strftime("%H:%M:%S"), "is_open": is_open}
