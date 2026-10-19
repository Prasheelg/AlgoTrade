"""Real-time NSE quotes from the Kotak Neo Trade API (read-only).

Only the consumer key (KOTAK_CONSUMER_KEY in .env) is used: quotes and the scrip master
authenticate with it alone, so no TOTP/MPIN login happens and no trading access exists.
This module only ever calls scrip_master() and quotes(); never order or portfolio functions. Prices the live
stream (livefeed.py) already has are taken from it instead of a quote call.
Any failure returns what was fetched so far, and callers fall back to Yahoo data."""
import io
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from . import config, livefeed

log = logging.getLogger(__name__)
SEGMENT = "nse_cm"
BATCH = 49            # quotes per request: Kotak rejects 50 ("max value to 50"), so 49 is the real maximum
PAUSE_SEC = 1.1       # between requests; the API rate-limits back-to-back calls
RETRIES = 3           # scrip master lookups sometimes fail transiently
RETRY_AFTER_SEC = 300 # after a failed scrip master lookup, use Yahoo for this long before asking again
IST = ZoneInfo("Asia/Kolkata")

_client = None
_tokens: dict[str, str] = {}
_ticks: dict[str, float] = {}
_tokens_day: date | None = None
_failed_at: float | None = None


def enabled() -> bool:
    return bool(config.KOTAK_CONSUMER_KEY)


def _neo():
    """Lazily created SDK client; consumer key only (no login)."""
    global _client
    if _client is None:
        from neo_api_client import NeoAPI
        logging.getLogger("neo_api_client").setLevel(logging.CRITICAL)  # keep SDK request logs quiet
        _client = NeoAPI(consumer_key=config.KOTAK_CONSUMER_KEY, environment="prod")
    return _client


def _download_csv(url: str) -> str:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.text


def parse_scrip_master(csv_text: str) -> dict[str, str]:
    """NSE equity symbol (e.g. RELIANCE) -> Kotak instrument token, from the EQ series rows."""
    m = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False)
    m = m[m["pGroup"].str.strip() == "EQ"]
    syms = m["pTrdSymbol"].str.strip().str.removesuffix("-EQ")
    return dict(zip(syms, m["pSymbol"].str.strip()))


def parse_tick_sizes(csv_text: str) -> dict[str, float]:
    """NSE equity symbol -> tick size in rupees (dTickSize is in paise; NSE ticks run from 0.01 to 5 by price)."""
    m = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False)
    m.columns = m.columns.str.strip()
    if "dTickSize" not in m:
        return {}
    m = m[m["pGroup"].str.strip() == "EQ"]
    ticks = pd.to_numeric(m["dTickSize"], errors="coerce") / 100
    syms = m["pTrdSymbol"].str.strip().str.removesuffix("-EQ")
    return {s: float(t) for s, t in zip(syms, ticks) if t > 0}


def tick_size(symbol: str) -> float | None:
    """The stock's NSE tick size from today's scrip master, or None if unknown."""
    tokens()
    return _ticks.get(symbol)


def _scrip_master_url() -> str:
    global _failed_at
    if _failed_at is not None and time.monotonic() - _failed_at < RETRY_AFTER_SEC:
        raise RuntimeError("scrip master failed recently; waiting before retrying")
    res = None
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(2)
        res = _neo().scrip_master(exchange_segment=SEGMENT)
        if isinstance(res, str) and res.startswith("http"):
            _failed_at = None
            return res
    _failed_at = time.monotonic()
    raise RuntimeError(f"Unexpected scrip master response: {str(res)[:200]}")


def tokens() -> dict[str, str]:
    """Symbol -> token map, downloaded once per day and cached in DATA_DIR across restarts."""
    global _tokens, _ticks, _tokens_day
    today = datetime.now(IST).date()
    if _tokens_day != today:
        cache = config.DATA_DIR / f"kotak_{SEGMENT}_{today}.csv"
        if cache.exists():
            text = cache.read_text(encoding="utf-8")
        else:
            text = _download_csv(_scrip_master_url())
            cache.write_text(text, encoding="utf-8")
            for old in config.DATA_DIR.glob(f"kotak_{SEGMENT}_*.csv"):
                if old != cache:
                    old.unlink(missing_ok=True)
        _tokens, _ticks, _tokens_day = parse_scrip_master(text), parse_tick_sizes(text), today
        log.info("Kotak scrip master: %d NSE equities", len(_tokens))
    return _tokens


def quote_time(q: dict) -> datetime | None:
    """Last update as naive IST. Kotak sends epoch seconds, but a text date ("Mon Jan 01 1900 ...") for a
    contract that has never traded."""
    try:
        sec = int(q.get("lstup_time") or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(sec, IST).replace(tzinfo=None) if sec > 0 else None


def parse_quote(q: dict) -> dict:
    ohlc = q.get("ohlc") or {}
    ts = quote_time(q)
    prev = ohlc.get("close")
    return {"ltp": float(q["ltp"]), "open": float(ohlc.get("open") or q["ltp"]),
            "high": float(ohlc.get("high") or q["ltp"]), "low": float(ohlc.get("low") or q["ltp"]),
            "volume": float(q.get("last_volume") or 0), "time": ts,
            "prev_close": float(prev) if prev not in (None, "") and float(prev) > 0 else None}


def quotes(symbols: list[str]) -> dict[str, dict]:
    """Live quotes {symbol: {ltp, open, high, low, volume, time}} (time = naive IST).
    Unknown symbols and failed batches are skipped (logged)."""
    if not enabled() or not symbols:
        return {}
    try:
        tok = tokens()
    except Exception as exc:
        log.warning("Kotak scrip master unavailable (%s); using Yahoo prices", exc)
        return {}
    known = [s for s in symbols if s in tok]
    if len(known) < len(symbols):
        log.debug("No Kotak token for %s", sorted(set(symbols) - set(known)))
    by_token = {tok[s]: s for s in known}
    out: dict[str, dict] = {}
    for t, q in livefeed.lookup(SEGMENT, [tok[s] for s in known]).items():
        out[by_token[t]] = parse_quote(q)
    known = [s for s in known if s not in out]
    for n in range(0, len(known), BATCH):
        if n:
            time.sleep(PAUSE_SEC)
        batch = [{"instrument_token": tok[s], "exchange_segment": SEGMENT} for s in known[n:n + BATCH]]
        try:
            res = _neo().quotes(instrument_tokens=batch, quote_type="all")
            if not isinstance(res, list):
                raise RuntimeError(str(res)[:200])
            for q in res:
                sym = by_token.get(str(q.get("exchange_token")))
                if sym and q.get("ltp") not in (None, ""):
                    out[sym] = parse_quote(q)
        except Exception as exc:
            log.warning("Kotak quotes failed for %d symbols (%s); using Yahoo prices for them", len(batch), exc)
    return out


def index_quote(name: str) -> dict | None:
    """Live quote of an NSE index by its Kotak name (e.g. "Nifty 50"): last, previous close and update time
    (naive IST). None if Kotak is off or the quote fails."""
    if not enabled():
        return None
    try:
        q = livefeed.lookup(SEGMENT, [name]).get(name)
        if q is None:
            res = _neo().quotes(instrument_tokens=[{"instrument_token": name, "exchange_segment": SEGMENT}],
                                quote_type="all")
            q = res[0] if isinstance(res, list) and res else None
        if not q or q.get("ltp") in (None, ""):
            raise RuntimeError(str(res)[:200])
    except Exception as exc:
        log.warning("Kotak index quote for %s failed (%s)", name, exc)
        return None
    prev = (q.get("ohlc") or {}).get("close")
    ts = quote_time(q)
    return {"ltp": float(q["ltp"]), "prev_close": float(prev) if prev not in (None, "") else None, "time": ts}


def index_quotes(names: list[str]) -> dict[str, dict]:
    """Several NSE indices in one call: {name: {ltp, prev_close}}; names Kotak doesn't know are left out."""
    if not enabled() or not names:
        return {}
    live = livefeed.lookup(SEGMENT, names)
    res = list(live.values())
    rest = [n for n in names if n not in live]
    try:
        if rest:
            more = _neo().quotes(instrument_tokens=[{"instrument_token": n, "exchange_segment": SEGMENT} for n in rest],
                                 quote_type="all")
            if not isinstance(more, list):
                raise RuntimeError(str(more)[:200])
            res += more
    except Exception as exc:
        log.warning("Kotak index quotes failed (%s)", exc)
    out = {}
    for q in res:
        name, prev = str(q.get("exchange_token")), (q.get("ohlc") or {}).get("close")
        if name in names and q.get("ltp") not in (None, ""):
            out[name] = {"ltp": float(q["ltp"]), "prev_close": float(prev) if prev not in (None, "") else None}
    return out


def apply_live_bar(df: pd.DataFrame, q: dict, timeframe: str) -> pd.DataFrame:
    """Return df with the live quote folded in as the newest bar.
    Daily: today's bar becomes the quote's day open/high/low/last/volume (replacing Yahoo's delayed one).
    Intraday: a bar at the quote time with open=high=low=close=last is appended (if newer than the last bar)."""
    t = q.get("time")
    if t is None or df.empty:
        return df
    if timeframe == "1d":
        day = pd.Timestamp(t.date())
        if df.index[-1] > day:
            return df
        bar = pd.DataFrame({"Open": [q["open"]], "High": [q["high"]], "Low": [q["low"]], "Close": [q["ltp"]],
                            "Volume": [q["volume"]]}, index=[day])
        return pd.concat([df[df.index < day], bar])
    ts = pd.Timestamp(t)
    if ts <= df.index[-1]:
        return df
    px = q["ltp"]
    bar = pd.DataFrame({"Open": [px], "High": [px], "Low": [px], "Close": [px], "Volume": [0.0]}, index=[ts])
    return pd.concat([df, bar])
