"""Read-only view of the Kotak demat account: funds, holdings, today's positions and orders, and the
trades the app manages (orders.py). Only read calls are made; nothing here can place or change an order.

Kotak responses are cached for CACHE_SEC so dashboard polling doesn't hammer the API.
Holdings / positions field names are not confirmed yet (the account was empty when this was written), so
the normalisers try several likely names and every payload keeps the raw rows for checking."""
import logging
import time
from datetime import datetime

from . import broker, kotak

log = logging.getLogger(__name__)
CACHE_SEC = 60
PENDING_SEC = 5   # while an order is pending, the order book alone is read again this often
_cache: dict = {"at": None, "data": None}


def _num(row: dict, *names, default=None):
    """First of names present in row as a number."""
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def _text(row: dict, *names, default="") -> str:
    for n in names:
        v = row.get(n)
        if v not in (None, ""):
            return str(v)
    return default


def _symbol(row: dict) -> str:
    s = _text(row, "displaySymbol", "symbol", "trdSym", "tradingSymbol", "sym", "instrumentName")
    return s.strip().upper().removesuffix("-EQ")


def _rows(res) -> list[dict]:
    data = res.get("data") if isinstance(res, dict) else res
    return [r for r in (data or []) if isinstance(r, dict)] if isinstance(data, list) else []


def norm_holding(row: dict) -> dict:
    qty = _num(row, "quantity", "qty", "holdingQty", "totalQty", "sellableQuantity", default=0.0)
    avg = _num(row, "averagePrice", "avgPrice", "avgPrc", "buyAvgPrice", "averagePrc")
    prev = _num(row, "closingPrice", "prevClose", "previousClose", "closePrice")
    ltp = _num(row, "ltp", "lastPrice", "lastTradedPrice", "ltpPrice")
    value = _num(row, "mktValue", "marketValue", "currentValue")
    if ltp is None and value is not None and qty:
        ltp = value / qty
    invested = _num(row, "holdingCost", "investedValue", "totalCost")
    if invested is None and avg is not None:
        invested = qty * avg
    # Sellable now: excludes shares not yet settled / pledged when Kotak reports it separately
    sellable = _num(row, "sellableQuantity", "sellableQty", "freeQty", default=qty)
    return {"symbol": _symbol(row), "qty": qty, "avg": avg, "ltp": ltp, "prev_close": prev,
            "invested": invested, "value": value, "sellable": min(sellable, qty)}


def _finish_holding(h: dict) -> dict:
    """Value, P&L and day change once the LTP is known."""
    if h["ltp"] is not None:
        h["value"] = h["qty"] * h["ltp"]
    elif h["value"] is None and h["prev_close"] is not None:
        h["value"] = h["qty"] * h["prev_close"]       # no live price: last close
    h["pnl"] = h["value"] - h["invested"] if h["value"] is not None and h["invested"] is not None else None
    h["pnl_pct"] = h["pnl"] / h["invested"] * 100 if h["pnl"] is not None and h["invested"] else None
    h["day_pnl"] = (h["qty"] * (h["ltp"] - h["prev_close"])
                    if h["ltp"] is not None and h["prev_close"] is not None else None)
    h["day_pct"] = (h["ltp"] - h["prev_close"]) / h["prev_close"] * 100 if h["day_pnl"] is not None and h["prev_close"] else None
    return h


def norm_position(row: dict) -> dict:
    buy_qty = _num(row, "flBuyQty", default=0.0) + _num(row, "cfBuyQty", default=0.0)
    sell_qty = _num(row, "flSellQty", default=0.0) + _num(row, "cfSellQty", default=0.0)
    if not buy_qty and not sell_qty:
        buy_qty, sell_qty = _num(row, "buyQty", default=0.0), _num(row, "sellQty", default=0.0)
    buy_amt = _num(row, "buyAmt", default=0.0) + _num(row, "cfBuyAmt", default=0.0)
    sell_amt = _num(row, "sellAmt", default=0.0) + _num(row, "cfSellAmt", default=0.0)
    net = _num(row, "netQty", default=buy_qty - sell_qty)
    tsym = _text(row, "trdSym", "tradingSymbol").strip().upper()
    seg = _text(row, "exSeg", "exchangeSegment").strip().lower() or ("nse_cm" if tsym.endswith("-EQ") else "")
    return {"symbol": _symbol(row), "product": _text(row, "prod", "product"), "net_qty": net,
            "tsym": tsym, "segment": seg,
            "buy_qty": buy_qty, "sell_qty": sell_qty,
            "buy_avg": buy_amt / buy_qty if buy_qty and buy_amt else _num(row, "buyAvgPrc", "avgBuyPrice"),
            "sell_avg": sell_amt / sell_qty if sell_qty and sell_amt else _num(row, "sellAvgPrc", "avgSellPrice"),
            "buy_amt": buy_amt, "sell_amt": sell_amt, "ltp": _num(row, "ltp", "lastPrice")}


def _finish_position(p: dict) -> dict:
    p["pnl"] = (p["sell_amt"] - p["buy_amt"] + p["net_qty"] * p["ltp"]
                if p["ltp"] is not None and (p["buy_amt"] or p["sell_amt"]) else None)
    return p


# order_state() names -> the dashboard's order filters
ORDER_STATE = {"OPEN": "PENDING", "FILLED": "COMPLETED", "CANCELLED": "CANCELLED", "REJECTED": "REJECTED"}


def norm_order(row: dict) -> dict:
    from .orders import order_state
    side = _text(row, "trnsTp", "transactionType").upper()
    nonzero = lambda v: v or None   # Kotak sends 0 for "no price" (market / SL-M), "no trigger" and "not filled"
    return {"order_id": _text(row, "nOrdNo", "orderId"), "symbol": _symbol(row),
            "side": "BUY" if side in ("B", "BUY") else "SELL" if side in ("S", "SELL") else side,
            "type": _text(row, "prcTp", "orderType"), "product": _text(row, "prod", "product"),
            "qty": _num(row, "qty", "quantity", default=0.0), "price": nonzero(_num(row, "prc", "price")),
            "trigger": nonzero(_num(row, "trgPrc", "triggerPrice")), "status": _text(row, "ordSt", "status"),
            "state": ORDER_STATE[order_state(row)],
            "filled_qty": _num(row, "fldQty", "filledQty", default=0.0),
            "avg_price": nonzero(_num(row, "avgPrc", "avgPrice")),
            "reason": _text(row, "rejRsn", "rejReason"), "time": _text(row, "ordDtTm", "ordEntTm", "orderTime")}


def funds(limits: dict) -> dict:
    return {"available": _num(limits, "Net"), "margin_used": _num(limits, "MarginUsed", "MarginUsedPrsnt"),
            "collateral": _num(limits, "CollateralValue", "Collateral"), "payin": _num(limits, "RmsPayInAmt"),
            "category": _text(limits, "Category")}


def totals(holdings: list[dict], cash: float | None) -> dict:
    known = [h for h in holdings if h["invested"] is not None and h["value"] is not None]
    invested = sum(h["invested"] for h in known)
    value = sum(h["value"] for h in known)
    days = [h["day_pnl"] for h in holdings if h["day_pnl"] is not None]
    return {"invested": invested, "value": value, "pnl": value - invested,
            "pnl_pct": (value - invested) / invested * 100 if invested else None,
            "day_pnl": sum(days) if days else None, "cash": cash, "count": len(holdings)}


def _fill_ltp(items: list[dict]) -> None:
    """Live prices from Kotak quotes where the account data has none."""
    missing = [x["symbol"] for x in items if x.get("ltp") is None and x["symbol"]]
    if not missing or not kotak.enabled():
        return
    try:
        q = kotak.quotes(sorted(set(missing)))
    except Exception:
        log.exception("Quotes for the portfolio failed")
        return
    for x in items:
        if x.get("ltp") is None and x["symbol"] in q:
            x["ltp"] = q[x["symbol"]]["ltp"]


def _app_trades() -> list[dict]:
    from . import orders   # read-only: all_trades() only
    try:
        return orders.all_trades()
    except Exception:
        log.exception("Reading app trades failed")
        return []


def _option_trades() -> list[dict]:
    from . import options_auto   # read-only: all_trades() only
    try:
        return options_auto.all_trades()
    except Exception:
        log.exception("Reading option trades failed")
        return []


def _orders(rows: list[dict]) -> list[dict]:
    """Normalised orders, each with who placed it (Auto-trade, a manual order, or outside the app)."""
    from . import orderbook
    try:
        known = orderbook.sources()
    except Exception:
        log.exception("Order sources unavailable")
        known = {}
    out = [{**o, "source": orderbook.source_of(o["order_id"], known)} for o in map(norm_order, rows)]
    try:
        orderbook.save_history(out)   # Kotak's book is today-only: keep a copy for Orders > History
    except Exception:
        log.exception("Saving order history failed")
    return out


def _fetch() -> dict:
    lim = broker.call("limits")
    raw = {"holdings": _rows(broker.call("holdings")), "positions": _rows(broker.call("positions")),
           "orders": _rows(broker.call("order_report"))}
    holdings = [norm_holding(r) for r in raw["holdings"]]
    positions = [norm_position(r) for r in raw["positions"]]
    _fill_ltp(holdings + positions)
    holdings = [_finish_holding(h) for h in holdings]
    positions = [_finish_position(p) for p in positions]
    f = funds(lim)
    return {"account": broker._session.get("name"), "funds": f, "holdings": holdings, "positions": positions,
            "orders": _orders(raw["orders"]), "totals": totals(holdings, f["available"]),
            "raw": raw}


def _refresh_pending(now: float) -> None:
    """Statuses move fast while an order is pending: read the order book again (only it) every PENDING_SEC."""
    data = _cache["data"]
    if (not any(o["state"] == "PENDING" for o in data["orders"])
            or now - _cache.get("orders_at", _cache["at"]) < PENDING_SEC):
        return
    try:
        rows = _rows(broker.call("order_report"))
    except broker.BrokerError as exc:
        log.warning("Order book refresh failed: %s", exc)
        return
    _cache["data"] = {**data, "orders": _orders(rows), "raw": {**data["raw"], "orders": rows}}
    _cache["orders_at"] = now


LIVE_SEC = 3   # live quotes are shared for this long, so several open tabs make one quote call
_live: dict = {"at": None, "symbols": None, "quotes": {}}


def live_quotes(symbols: list[str]) -> dict[str, dict]:
    """Fresh Kotak quotes {symbol: {ltp, time, ...}} for the portfolio, cached LIVE_SEC."""
    symbols = sorted({s for s in symbols if s})
    if not symbols or not kotak.enabled():
        return {}
    now = time.monotonic()
    from . import livefeed
    ttl = 0.2 if livefeed.connected() else LIVE_SEC   # the stream answers from memory, so share for less time
    if _live["at"] is not None and now - _live["at"] < ttl and set(symbols) <= set(_live["symbols"]):
        return _live["quotes"]
    try:
        q = kotak.quotes(symbols)
    except Exception:
        log.exception("Live quotes for the portfolio failed")
        return {}
    _live.update(at=now, symbols=symbols, quotes=q)
    return q


def _apply_live(out: dict) -> None:
    """Overlay fresh quotes on holdings, positions and open app trades, and recompute P&L and totals.
    Works on copies, so the cached account data keeps Kotak's own figures."""
    open_trades = [t for t in out["trades"] if t["state"] in ("OPEN", "EXITING")]
    q = live_quotes([h["symbol"] for h in out["holdings"]] + [p["symbol"] for p in out["positions"]]
                    + [t["symbol"] for t in open_trades])
    if not q:
        out["live"] = None
        return
    out["holdings"] = [_finish_holding({**h, "ltp": q[h["symbol"]]["ltp"]}) if h["symbol"] in q else h
                       for h in out["holdings"]]
    out["positions"] = [_finish_position({**p, "ltp": q[p["symbol"]]["ltp"]}) if p["symbol"] in q else p
                        for p in out["positions"]]
    out["trades"] = [dict(t) for t in out["trades"]]
    for t in out["trades"]:
        if t["state"] in ("OPEN", "EXITING") and t["symbol"] in q and t["fill_price"] is not None:
            t["ltp"] = q[t["symbol"]]["ltp"]
            t["unrealized"] = (t["ltp"] - t["fill_price"]) * t["qty"]
    out["totals"] = totals(out["holdings"], (out["funds"] or {}).get("available"))
    times = [v["time"] for v in q.values() if v.get("time")]
    out["live"] = {"source": "Kotak Neo", "count": len(q),
                   "as_of": max(times).isoformat(timespec="seconds") if times else None}


def report(refresh: bool = False, live: bool = False) -> dict:
    """Portfolio payload. Kotak data is cached CACHE_SEC (refresh=True bypasses); app trades are always fresh.
    live=True overlays prices fetched now (cached LIVE_SEC) on every holding, position and open app trade."""
    out = {"error": None, "credentials": not broker.missing_credentials(), "cached": False}
    if not out["credentials"]:
        out["error"] = "Kotak login details are missing in .env, so the demat account can't be read."
    else:
        now = time.monotonic()
        if not refresh and _cache["data"] is not None and now - _cache["at"] < CACHE_SEC:
            _refresh_pending(now)
            out.update(_cache["data"], cached=True)
        else:
            try:
                data = {**_fetch(), "as_of": datetime.now().isoformat(timespec="seconds")}
                _cache.update(at=now, data=data, orders_at=now)
                out.update(data)
            except broker.BrokerError as exc:
                out["error"] = f"Couldn't read the Kotak account: {exc}"
    out.setdefault("account", None)
    out.setdefault("as_of", None)
    out.setdefault("funds", None)
    for k in ("holdings", "positions", "orders"):
        out.setdefault(k, [])
    out.setdefault("totals", totals([], None))
    out.setdefault("raw", {})
    out["trades"] = _app_trades()
    out["option_trades"] = _option_trades()
    out["live"] = None
    if live:
        _apply_live(out)
    return out
