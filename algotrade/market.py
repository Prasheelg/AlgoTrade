"""Market overview (dashboard Watchlist > Market today): main and sector indices, NIFTY 100 top gainers and losers,
sector moves, and the previous day's FII / DII cash flows. India only, read-only.

Indices and stocks: Kotak live quotes (change vs the previous close), cached CACHE_SEC. FII / DII: NSE's public
API (₹ crore, provisional, published after the close), cached FLOWS_SEC; None when NSE can't be reached."""
import logging
import time

import requests

from . import kotak, universe

log = logging.getLogger(__name__)
CACHE_SEC = 30
LIVE_CACHE_SEC = 0.2   # while the live stream is connected (quotes come from memory)
FLOWS_SEC = 1800
MAIN = ["Nifty 50", "Nifty Bank", "Nifty Next 50", "NIFTY MIDCAP 100", "NIFTY SMLCAP 100", "INDIA VIX"]
SECTORS = ["Nifty IT", "Nifty Bank", "Nifty Fin Service", "Nifty Auto", "Nifty Pharma", "Nifty FMCG", "Nifty Metal",
           "Nifty Energy", "Nifty Realty", "Nifty PSU Bank", "Nifty Media", "Nifty Infra"]
TOP = 6
NSE = "https://www.nseindia.com"
_cache: dict = {"at": 0.0, "data": None, "flows_at": 0.0, "flows": None, "universe": None, "universe_day": None}


def _chg(q: dict) -> dict:
    ltp, prev = q.get("ltp"), q.get("prev_close")
    return {"ltp": ltp, "change": round(ltp - prev, 2) if ltp and prev else None,
            "change_pct": round((ltp - prev) / prev * 100, 2) if ltp and prev else None}


def flows() -> dict | None:
    """Latest FII / DII cash-market flows from NSE: {date, fii: {buy, sell, net}, dii: {...}} in ₹ crore."""
    now = time.monotonic()
    if _cache["flows"] is not None and now - _cache["flows_at"] < FLOWS_SEC:
        return _cache["flows"]
    try:
        s = requests.Session()
        s.headers.update({**universe.HEADERS, "Referer": NSE + "/", "Accept": "application/json"})
        try:
            s.get(NSE, timeout=10)   # cookies, when NSE hands them out
        except requests.RequestException:
            pass
        rows = s.get(NSE + "/api/fiidiiTradeReact", timeout=15).json()
        out = {"date": rows[0]["date"]}
        for r in rows:
            k = "fii" if "FII" in r["category"].upper() else "dii"
            out[k] = {x: float(r[f"{x}Value"]) for x in ("buy", "sell", "net")}
        _cache.update(flows=out, flows_at=now)
        return out
    except Exception as exc:
        log.warning("FII/DII flows unavailable (%s)", exc)
        _cache.update(flows_at=now - FLOWS_SEC + 300)   # retry in 5 minutes
        return _cache["flows"]


def _stocks() -> list[str]:
    from datetime import date
    if _cache["universe_day"] != date.today():
        _cache.update(universe=universe.get_universe("NIFTY100", market="IN"), universe_day=date.today())
    return _cache["universe"]


def overview() -> dict:
    now = time.monotonic()
    from . import livefeed
    if _cache["data"] is not None and now - _cache["at"] < (LIVE_CACHE_SEC if livefeed.connected() else CACHE_SEC):
        return {**_cache["data"], "flows": flows()}
    idx = kotak.index_quotes(list(dict.fromkeys(MAIN + SECTORS)))
    q = kotak.quotes(_stocks())
    stocks = [{"symbol": s, **_chg(x)} for s, x in q.items() if _chg(x)["change_pct"] is not None]
    stocks.sort(key=lambda r: -r["change_pct"])
    sect = universe.sectors()
    groups: dict[str, list[float]] = {}
    for r in stocks:
        groups.setdefault(sect.get(r["symbol"], "Other"), []).append(r["change_pct"])
    breadth = {"up": sum(r["change_pct"] > 0 for r in stocks), "down": sum(r["change_pct"] < 0 for r in stocks),
               "total": len(stocks)}
    data = {"indices": [{"name": n, **_chg(idx[n])} for n in MAIN if n in idx],
            "sectors": sorted(({"name": n.replace("Nifty ", ""), **_chg(idx[n])} for n in SECTORS if n in idx),
                              key=lambda r: -(r["change_pct"] or 0)),
            "industries": sorted(({"name": k, "stocks": len(v), "change_pct": round(sum(v) / len(v), 2)}
                                  for k, v in groups.items()), key=lambda r: -r["change_pct"]),
            "gainers": stocks[:TOP], "losers": stocks[::-1][:TOP], "breadth": breadth,
            "live": bool(idx or q), "as_of": time.strftime("%Y-%m-%d %H:%M:%S")}
    _cache.update(at=now, data=data)
    return {**data, "flows": flows()}
