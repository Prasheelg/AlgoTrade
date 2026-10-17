"""Manual option orders from the dashboard's Strategy Builder tab (index and stock options, NSE F&O).

Your own orders, separate from Auto-trade (which trades equity only). Safeguards:
- quantity is whole lots, at most OPTIONS_MAX_LOTS per order;
- SELL is only accepted to close options you hold (net long position in that contract, from Kotak's
  positions), so a slip can never write an option (unlimited risk, large margin);
- limit prices are rounded to the contract's tick; outside market hours the order goes in as AMO;
- SEBI allows no market orders via the API for retail clients: a MARKET order is sent as a limit a little past
  the live price (marketable limit, see protect()), so it fills at once but can never run away.
Every order is recorded (manual_orders table) and posted to Telegram."""
import logging
import math
import time
from datetime import datetime

from . import broker, config, fno, markets, state, telegram_bot

log = logging.getLogger(__name__)
PRODUCTS = ("NRML", "MIS")   # NRML = carry forward to expiry, MIS = intraday (squared off by the broker)
ORDER_TYPES = {"LIMIT": "L", "MARKET": "MKT"}


def _conn():
    con = state._conn()
    con.execute("""CREATE TABLE IF NOT EXISTS manual_orders (id INTEGER PRIMARY KEY AUTOINCREMENT, placed_at TEXT,
        tsym TEXT, underlying TEXT, side TEXT, lots INTEGER, qty INTEGER, order_type TEXT, price REAL,
        product TEXT, amo TEXT, order_id TEXT, error TEXT)""")
    return con


def recent(limit: int = 50) -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM manual_orders ORDER BY id DESC LIMIT ?", (limit,))]


def _record(**row) -> None:
    with _conn() as con:
        con.execute(f"INSERT INTO manual_orders ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                    tuple(row.values()))


def held_qty(tsym: str) -> int:
    """Net long quantity of a contract in today's Kotak positions (0 if none or short)."""
    from .portfolio import _rows, norm_position
    rows = _rows(broker.call("positions"))
    net = sum(norm_position(r)["net_qty"] for r in rows
              if str(r.get("trdSym") or r.get("tradingSymbol") or "").strip().upper() == tsym.upper())
    return max(int(net), 0)


def _round_tick(px: float, tick: float) -> float:
    return round(round(px / tick) * tick, 2)


def protect(ref: float, side: str, pct: float, tick: float = 0.05) -> float:
    """Marketable limit for a "market" order: pct % above the reference price for a buy, below it for a sell,
    on the tick grid (rounded away from the reference, so it stays marketable)."""
    n = ref * (1 + pct / 100 if side == "BUY" else 1 - pct / 100) / tick
    n = math.ceil(n - 1e-9) if side == "BUY" else math.floor(n + 1e-9)
    return round(max(n, 1) * tick, 2)


def _stock_ltp(symbol: str) -> float:
    from . import kotak
    ltp = (kotak.quotes([symbol]).get(symbol) or {}).get("ltp")
    if not ltp:
        raise ValueError(f"No live price for {symbol}, so a market order can't be priced; use a limit")
    return float(ltp)


def _market_note(px) -> str:
    return f"market (limit ₹{px:,.2f})"


def _validate(tsym: str, side: str, lots, order_type: str, price, product: str) -> dict:
    """Check one option order; returns the contract and normalised fields. Raises ValueError."""
    c = fno.contract(tsym)
    if c is None:
        raise ValueError(f"Unknown option contract {tsym}")
    side, order_type, product = side.upper(), order_type.upper(), product.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("Side must be BUY or SELL")
    if order_type not in ORDER_TYPES:
        raise ValueError("Order type must be LIMIT or MARKET")
    if product not in PRODUCTS:
        raise ValueError("Product must be NRML or MIS")
    if not isinstance(lots, int) or isinstance(lots, bool) or not 1 <= lots <= config.OPTIONS_MAX_LOTS:
        raise ValueError(f"Lots must be a whole number from 1 to {config.OPTIONS_MAX_LOTS} (OPTIONS_MAX_LOTS)")
    px = None
    if order_type == "LIMIT":
        if price is None or float(price) <= 0:
            raise ValueError(f"A limit order needs a price above 0 ({tsym})")
        px = _round_tick(float(price), float(c["tick"]))
    return {"c": c, "tsym": tsym, "side": side, "lots": lots, "qty": lots * int(c["lot"]), "order_type": order_type,
            "px": px, "product": product}


def place_option_order(tsym: str, side: str, lots: int, order_type: str = "LIMIT", price: float | None = None,
                       product: str = "NRML") -> dict:
    """Validate and place one option order. Raises ValueError for a bad request, BrokerError if Kotak refuses."""
    v = _validate(tsym, side, lots, order_type, price, product)
    if v["side"] == "SELL":
        held = held_qty(tsym)
        if held < v["qty"]:
            raise ValueError(f"You can only sell options you hold: you hold {held} of {tsym}, the order is for "
                             f"{v['qty']}. Writing (shorting) options isn't allowed here.")
    return _send(v)


def _send(v: dict, tag: str = "") -> dict:
    """Place a validated order, record it and post it to Telegram."""
    c, tsym, side, lots, qty, order_type, px, product = (v[k] for k in ("c", "tsym", "side", "lots", "qty",
                                                                         "order_type", "px", "product"))
    amo = "NO" if markets.get("IN").is_open() else "YES"
    if order_type == "MARKET":   # marketable limit off the best ask (buy) / bid (sell), else the last price
        try:
            q = fno.option_quote(tsym)
        except Exception as exc:
            raise ValueError(f"No live price for {tsym} ({exc}), so a market order can't be priced; use a limit")
        ref = (q.get("ask") if side == "BUY" else q.get("bid")) or q.get("ltp")
        if not ref:
            raise ValueError(f"No live price for {tsym}, so a market order can't be priced; use a limit")
        px = protect(float(ref), side, config.OPTION_PROTECTION_PCT, float(c["tick"]))
    row = dict(placed_at=datetime.now().isoformat(timespec="seconds"), tsym=tsym, underlying=c["underlying"],
               side=side, lots=lots, qty=qty, order_type=order_type, price=px, product=product, amo=amo)
    try:
        res = broker.call("place_order", exchange_segment=fno.SEGMENT, product=product, validity="DAY", amo=amo,
                          order_type="L", price=f"{px:.2f}", trigger_price="0",
                          quantity=str(qty), trading_symbol=tsym, transaction_type=side[0])
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        oid = res.get("nOrdNo") or data.get("nOrdNo")
        if not oid:
            raise broker.BrokerError(f"place_order: no order number in {str(res)[:200]}")
    except broker.BrokerError as exc:
        _record(**row, error=str(exc)[:300])
        raise
    _record(**row, order_id=str(oid))
    try:
        telegram_bot.send_message(
            f"🖐️ <b>Manual {side} {tsym}</b>{tag} × {lots} lot{'s' if lots > 1 else ''} ({qty}) · "
            f"{_market_note(px) if order_type == 'MARKET' else f'limit ₹{px:,.2f}'} · {product}"
            f"{' · AMO' if amo == 'YES' else ''} · order {oid}")
    except Exception:
        log.exception("Telegram notice for a manual order failed")
    return {**row, "order_id": str(oid)}


# ---- strategies (several legs, one expiry) ------------------------------------------------------

FILL_WAIT_SEC = 20   # how long to wait for bought legs to fill before the sold legs go in


def _filled(order_ids: list[str], wait: float) -> bool:
    """True once every order is completely filled in Kotak's order book (polled until `wait` seconds)."""
    from .orders import order_state
    deadline = time.monotonic() + wait
    while True:
        book = {str(o.get("nOrdNo")): o for o in (broker.call("order_report").get("data") or [])}
        states = [order_state(book.get(oid)) for oid in order_ids]
        if all(s == "FILLED" for s in states):
            return True
        if any(s in ("REJECTED", "CANCELLED") for s in states) or time.monotonic() >= deadline:
            return False
        time.sleep(1)


def place_strategy(legs: list[dict], order_type: str = "LIMIT", product: str = "NRML",
                   wait: float = FILL_WAIT_SEC) -> dict:
    """Place a multi-leg strategy: bought legs first, sold legs only after every bought leg has filled.
    Only defined-risk strategies (no option sold uncovered), all legs on one underlying and expiry.
    Returns what was placed; raises ValueError before anything is sent if the strategy isn't allowed."""
    from .strategy import defined_risk
    if not legs:
        raise ValueError("The strategy has no legs")
    vs = [_validate(str(g.get("tsym", "")), str(g.get("side", "")), g.get("lots"), order_type,
                    g.get("price"), product) for g in legs]
    if len({(v["c"]["underlying"], str(v["c"]["expiry"])) for v in vs}) > 1:
        raise ValueError("All legs must be on the same underlying and expiry")
    if not defined_risk([{"opt": v["c"]["opt"], "side": v["side"], "qty": v["qty"]} for v in vs]):
        raise ValueError("This strategy sells options without buying cover (unlimited or very large risk), "
                         "so it can't be placed here. It can still be analysed.")
    buys = [v for v in vs if v["side"] == "BUY"]
    sells = [v for v in vs if v["side"] == "SELL"]
    if sells and not markets.get("IN").is_open():
        raise ValueError("Strategies with sold legs can only be placed while the market is open, so the bought "
                         "legs can fill first.")
    name = f" · strategy {len(vs)} legs"
    placed: list[dict] = []
    out = {"placed": placed, "sells_placed": False, "message": ""}
    for v in buys:
        placed.append(_send(v, name))   # a BrokerError here stops the strategy; earlier legs stay placed
    if not sells:
        out["message"] = f"{len(placed)} buy order{'s' if len(placed) > 1 else ''} placed."
        return out
    if not _filled([p["order_id"] for p in placed], wait):
        out["message"] = (f"Bought legs placed but not all filled within {wait:.0f} s, so the sold legs were NOT "
                          "placed (they would be uncovered). Check Portfolio > Orders today.")
        try:
            telegram_bot.send_message(f"⚠️ Strategy: sold legs not placed, bought legs not filled in {wait:.0f} s")
        except Exception:
            log.exception("Telegram notice failed")
        return out
    for v in sells:
        placed.append(_send(v, name))
    out["sells_placed"] = True
    out["message"] = f"All {len(placed)} legs placed (bought legs filled first)."
    return out


# ---- selling demat holdings (Portfolio > Holdings > Sell) ---------------------------------------

def sell_holding(symbol: str, qty, order_type: str = "LIMIT", price=None) -> dict:
    """Sell shares from the demat account (delivery, NSE). The quantity is checked against a fresh read of
    Kotak's holdings. Stocks the app manages (Auto-trade) are refused: close those under App trades, so their
    stop-loss is cancelled too."""
    from .orders import ACTIVE_STATES, all_trades
    from .portfolio import _rows, norm_holding
    symbol, order_type = symbol.strip().upper(), order_type.upper()
    if order_type not in ORDER_TYPES:
        raise ValueError("Order type must be LIMIT or MARKET")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
        raise ValueError("Quantity must be a whole number of shares, at least 1")
    if any(t["symbol"] == symbol and t["state"] in ACTIVE_STATES for t in all_trades()):
        raise ValueError(f"{symbol} is managed by Auto-trade: close it under Portfolio > App trades, "
                         "which also cancels its stop-loss.")
    held = [norm_holding(r) for r in _rows(broker.call("holdings"))]
    h = next((x for x in held if x["symbol"] == symbol), None)
    sellable = int(h["sellable"]) if h else 0
    if qty > sellable:
        raise ValueError(f"You can sell at most {sellable} {symbol} now (holdings, sellable quantity)")
    if order_type == "LIMIT":
        if price is None or float(price) <= 0:
            raise ValueError("A limit order needs a price above 0")
        px = _round_tick(float(price), 0.05)
    else:
        px = protect(_stock_ltp(symbol), "SELL", config.MARKET_PROTECTION_PCT)
    amo = "NO" if markets.get("IN").is_open() else "YES"
    row = dict(placed_at=datetime.now().isoformat(timespec="seconds"), tsym=f"{symbol}-EQ", underlying=symbol,
               side="SELL", lots=None, qty=qty, order_type=order_type, price=px, product="CNC", amo=amo)
    try:
        res = broker.call("place_order", exchange_segment="nse_cm", product="CNC", validity="DAY", amo=amo,
                          order_type="L", price=f"{px:.2f}", trigger_price="0",
                          quantity=str(qty), trading_symbol=f"{symbol}-EQ", transaction_type="S")
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        oid = res.get("nOrdNo") or data.get("nOrdNo")
        if not oid:
            raise broker.BrokerError(f"place_order: no order number in {str(res)[:200]}")
    except broker.BrokerError as exc:
        _record(**row, error=str(exc)[:300])
        raise
    _record(**row, order_id=str(oid))
    try:
        telegram_bot.send_message(f"🖐️ <b>Manual SELL {symbol}</b> × {qty} (holdings) · "
                                  f"{_market_note(px) if order_type == 'MARKET' else f'limit ₹{px:,.2f}'} · CNC"
                                  f"{' · AMO' if amo == 'YES' else ''} · order {oid}")
    except Exception:
        log.exception("Telegram notice for a holdings sell failed")
    return {**row, "order_id": str(oid)}


# ---- buying stocks (Portfolio > Buy stock / Holdings > Buy) -------------------------------------

STOCK_PRODUCTS = ("CNC", "MIS")   # CNC = delivery into demat, MIS = intraday (squared off by the broker)


def stock_quote(symbol: str) -> dict:
    """Live NSE quote for a stock (read-only)."""
    from . import kotak
    symbol = symbol.strip().upper()
    if symbol not in kotak.tokens():
        raise ValueError(f"{symbol} is not an NSE equity")
    q = kotak.quotes([symbol]).get(symbol)
    if not q:
        raise ValueError(f"No live price for {symbol}")
    return {"symbol": symbol, **q, "time": q["time"].isoformat(timespec="seconds") if q.get("time") else None}


def buy_stock(symbol: str, qty, order_type: str = "LIMIT", price=None, product: str = "CNC") -> dict:
    """Buy shares on NSE: delivery (CNC) or intraday (MIS). The order value (limit price; for a market order the
    marketable limit above the live price) is capped at STOCK_MAX_ORDER_VALUE against fat-finger mistakes."""
    from . import kotak
    symbol, order_type, product = symbol.strip().upper(), order_type.upper(), product.upper()
    if symbol not in kotak.tokens():
        raise ValueError(f"{symbol} is not an NSE equity")
    if order_type not in ORDER_TYPES:
        raise ValueError("Order type must be LIMIT or MARKET")
    if product not in STOCK_PRODUCTS:
        raise ValueError("Product must be CNC (delivery) or MIS (intraday)")
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
        raise ValueError("Quantity must be a whole number of shares, at least 1")
    if order_type == "LIMIT":
        if price is None or float(price) <= 0:
            raise ValueError("A limit order needs a price above 0")
        px = _round_tick(float(price), 0.05)
    else:
        px = protect(_stock_ltp(symbol), "BUY", config.MARKET_PROTECTION_PCT)
    ref = px   # the most the order can cost per share
    if ref * qty > config.STOCK_MAX_ORDER_VALUE:
        raise ValueError(f"Order value ≈ ₹{ref * qty:,.0f} is above STOCK_MAX_ORDER_VALUE "
                         f"₹{config.STOCK_MAX_ORDER_VALUE:,.0f} (change it in .env)")
    amo = "NO" if markets.get("IN").is_open() else "YES"
    row = dict(placed_at=datetime.now().isoformat(timespec="seconds"), tsym=f"{symbol}-EQ", underlying=symbol,
               side="BUY", lots=None, qty=qty, order_type=order_type, price=px, product=product, amo=amo)
    try:
        res = broker.call("place_order", exchange_segment="nse_cm", product=product, validity="DAY", amo=amo,
                          order_type="L", price=f"{px:.2f}", trigger_price="0",
                          quantity=str(qty), trading_symbol=f"{symbol}-EQ", transaction_type="B")
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        oid = res.get("nOrdNo") or data.get("nOrdNo")
        if not oid:
            raise broker.BrokerError(f"place_order: no order number in {str(res)[:200]}")
    except broker.BrokerError as exc:
        _record(**row, error=str(exc)[:300])
        raise
    _record(**row, order_id=str(oid))
    try:
        telegram_bot.send_message(f"🖐️ <b>Manual BUY {symbol}</b> × {qty} · "
                                  f"{_market_note(px) if order_type == 'MARKET' else f'limit ₹{px:,.2f}'} · {product}"
                                  f"{' · AMO' if amo == 'YES' else ''} · order {oid}")
    except Exception:
        log.exception("Telegram notice for a stock buy failed")
    return {**row, "order_id": str(oid), "value": round(ref * qty, 2)}
