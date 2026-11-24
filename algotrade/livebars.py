"""India intraday bars for the scanner: Yahoo's history up to yesterday + today's bars from Kotak, in real time.

Yahoo's NSE intraday bars run ~15 minutes late, so a scan judged its 5m / 15m bars on old prices. Kotak's own
history is real time but comes one symbol per (rate-limited) request, and has no stock daily bars. So, while the
NSE session is on:

- history: Yahoo, everything before today, downloaded once a day per timeframe and kept in memory;
- today: Kotak's 5-minute bars, one request per symbol, reused by every timeframe of the same scan round
  (until the next 5-minute boundary); 15m / 30m / 1h bars are built from them, aligned to the 09:15 open;
- a built bar whose 5-minute pieces haven't all arrived is dropped, so it is never judged half-made.

A symbol Kotak can't serve (no token, an error, no bars, or the negative-volume glitch Kotak's history shows near
the close) gets Yahoo's bars as before. If the first PROBE symbols all fail (a holiday, an outage), Kotak is left
alone for SKIP_MIN minutes. Daily bars, the backtest, the tracker and the US market don't come here."""
import logging
from datetime import datetime, time, timedelta

import pandas as pd

from . import kotak, markets
from .universe import INDEX_YAHOO

log = logging.getLogger(__name__)

TIMEFRAMES = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}
BASE = pd.Timedelta(minutes=5)   # Kotak's bars that every timeframe is built from
PROBE = 5        # this many failures in a row at the start of a round = Kotak has nothing today
SKIP_MIN = 15    # ...and is then left alone this long
COLS = ["Open", "High", "Low", "Close", "Volume"]

_history: dict[str, dict] = {}         # timeframe -> {"day": date, "frames": {symbol: DataFrame or None}}
_today: dict[str, tuple] = {}          # symbol -> (fetched at, exchange time; today's 5-minute bars)
_skip_until: list = [None]


def active(timeframe: str, market: str, now: datetime) -> bool:
    """Is the hybrid feed used for this scan? India, an intraday timeframe, Kotak set up, a weekday session that
    has opened (not a holiday), and Kotak not found empty in the last SKIP_MIN minutes."""
    if market != "IN" or timeframe not in TIMEFRAMES or not kotak.enabled():
        return False
    m = markets.get("IN")
    if not m.trading_day(now.date()) or now.time() < m.session[0]:
        return False
    return _skip_until[0] is None or now >= _skip_until[0]


def neosymbol(sym: str) -> str | None:
    """Kotak's history name: "nse_cm|<token>" for a stock, "nse_cm|Nifty 50" for an index (NIFTY or ^NSEI)."""
    from .fno import INDEX_QUOTE
    idx = {v: k for k, v in INDEX_YAHOO.items()}.get(sym, sym)   # ^NSEI -> NIFTY
    if idx in INDEX_QUOTE:
        return f"nse_cm|{INDEX_QUOTE[idx]}"
    token = kotak.tokens().get(sym)
    return f"nse_cm|{token}" if token else None


def _kotak_today(sym: str, now: datetime) -> pd.DataFrame | None:
    """Today's 5-minute bars of sym from Kotak, or None when Kotak can't give them. A fetch made after the latest
    5-minute boundary already holds every finished bar, so it is reused until the next one."""
    hit = _today.get(sym)
    boundary = pd.Timestamp(now).floor("5min")
    if hit and hit[0] >= boundary:
        return hit[1]
    name = neosymbol(sym)
    if not name:
        return None
    from .charts import kotak_history   # rate-limited, shared with the Chart view
    try:
        df = kotak_history(name, "5min", 0, now.date())
    except Exception as exc:
        log.debug("Kotak 5-minute bars for %s failed: %s", sym, exc)
        return None
    df = df[df.index.date == now.date()] if not df.empty else df
    if df.empty or (df["Volume"] < 0).any():   # nothing yet, or the closing-volume glitch: Yahoo's bars instead
        return None
    _today[sym] = (pd.Timestamp(now), df[COLS])
    return _today[sym][1]


def build(df5: pd.DataFrame, timeframe: str, day) -> pd.DataFrame:
    """Today's bars on the timeframe from 5-minute ones, aligned to the session open. The last built bar is
    dropped if the 5-minute bars stop before it ends (capped at the close): it is still forming or incomplete."""
    m = markets.get("IN")
    open_, close = (pd.Timestamp.combine(day, t) for t in m.session)
    if timeframe == "5m":
        return df5
    rule = TIMEFRAMES[timeframe]
    out = df5.resample(rule, origin=open_, label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(subset=["Open"])
    if not out.empty:
        end = min(out.index[-1] + pd.Timedelta(rule), close)
        if df5.index[-1] + BASE < end:
            out = out.iloc[:-1]
    return out


def _history_frames(symbols: list[str], timeframe: str, day, yahoo, stop) -> dict:
    """Yahoo bars before today, downloaded once a day (a symbol Yahoo had nothing for is not asked again)."""
    h = _history.get(timeframe)
    if not h or h["day"] != day:
        h = _history[timeframe] = {"day": day, "frames": {}}
    missing = [s for s in symbols if s not in h["frames"]]
    if missing:
        got = yahoo(missing, timeframe, stop=stop)
        for s in missing:
            df = got.get(s)
            h["frames"][s] = None if df is None else df[df.index.date < day]
        log.info("Yahoo %s history up to yesterday for %d/%d symbols (kept for the day)",
                 timeframe, sum(got.get(s) is not None for s in missing), len(missing))
    return h["frames"]


def fetch(symbols: list[str], timeframe: str, now: datetime, yahoo, stop=None) -> dict[str, pd.DataFrame]:
    """{symbol: bars} for the scanner; yahoo(symbols, timeframe, stop=) is data's plain Yahoo download, used for
    the history and for every symbol Kotak can't serve today."""
    day = now.date()
    hist = _history_frames(symbols, timeframe, day, yahoo, stop)
    out, fallback, streak, live = {}, [], 0, True
    for i, sym in enumerate(symbols):
        if stop:
            stop()
        if hist.get(sym) is None:
            fallback.append(sym)
            continue
        df5 = _kotak_today(sym, now) if live else None
        if df5 is None:
            fallback.append(sym)
            streak += 1
            if live and streak == i + 1 == PROBE:   # the first PROBE all failed: nothing from Kotak today
                live = False
                _skip_until[0] = now + timedelta(minutes=SKIP_MIN)
                log.warning("Kotak has no %s bars for the first %d symbols; Yahoo only for %d min",
                            day, PROBE, SKIP_MIN)
            continue
        today = build(df5, timeframe, day)
        out[sym] = pd.concat([hist[sym][COLS], today]) if not today.empty else hist[sym][COLS]
    if fallback:
        out.update(yahoo(fallback, timeframe, stop=stop))
    log.info("%s bars: today's from Kotak for %d/%d symbols, Yahoo for %d",
             timeframe, len(symbols) - len(fallback), len(symbols), len(fallback))
    return out
