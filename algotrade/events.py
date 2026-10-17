"""Upcoming company events that can make a stock jump: quarterly results and ex-dividend dates.

From Yahoo Finance's calendar (free; Indian coverage is good for large caps, patchy for smaller ones). Looked up
only for stocks that have a signal, cached for the day. Used by the "events" scan filter (skip signals with an
event within EVENT_WINDOW_DAYS) and shown in the signal detail view."""
import logging
from datetime import date, datetime

import yfinance as yf

from . import config
from .universe import to_yahoo

log = logging.getLogger(__name__)
_cache: dict[tuple, dict] = {}   # (market, symbol, day) -> {"results": date|None, "ex_dividend": date|None}


def _as_date(v) -> date | None:
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def upcoming(symbol: str, market: str = "IN") -> dict:
    """{"results": date | None, "ex_dividend": date | None}; both None if Yahoo has nothing or fails."""
    key = (market, symbol, date.today())
    if key in _cache:
        return _cache[key]
    out = {"results": None, "ex_dividend": None}
    try:
        cal = yf.Ticker(to_yahoo(symbol, market)).calendar or {}
        out = {"results": _as_date(cal.get("Earnings Date")), "ex_dividend": _as_date(cal.get("Ex-Dividend Date"))}
    except Exception as exc:
        log.debug("No event calendar for %s (%s)", symbol, exc)
    _cache[key] = out
    return out


def soon(symbol: str, market: str = "IN", days: int | None = None, today: date | None = None) -> str | None:
    """Why the stock should be avoided now ("results in 2 days"), or None."""
    days = config.EVENT_WINDOW_DAYS if days is None else days
    today = today or date.today()
    ev = upcoming(symbol, market)
    for name, label in (("results", "results"), ("ex_dividend", "ex-dividend")):
        d = ev.get(name)
        if d and 0 <= (d - today).days <= days:
            n = (d - today).days
            return f"{label} {'today' if n == 0 else 'tomorrow' if n == 1 else f'in {n} days'} ({d:%d %b})"
    return None


def describe(symbol: str, market: str = "IN") -> dict:
    ev = upcoming(symbol, market)
    return {"results": ev["results"].isoformat() if ev["results"] else None,
            "ex_dividend": ev["ex_dividend"].isoformat() if ev["ex_dividend"] else None,
            "warning": soon(symbol, market)}
