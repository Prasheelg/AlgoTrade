"""Your own orders in Kotak's order book (dashboard Portfolio > Orders today): who placed each order, cancel /
modify of pending ones, and Telegram notices when a manual order fills, is rejected or is cancelled.

Orders that belong to an Auto-trade trade (entry, stop-loss, exit) can't be cancelled or modified here: the
trade engine (orders.py) would lose track of them. Close those under Portfolio > App trades.
Modify can change the price and trigger and lower the quantity, never raise it; a stock buy's new value must
stay within STOCK_MAX_ORDER_VALUE."""
import logging
import re
import threading
import time
from datetime import date, datetime

from . import broker, config, markets, state, telegram_bot

log = logging.getLogger(__name__)
WATCH_SEC = 15   # how often the dashboard checks the order book while manual orders are unresolved
FINAL = ("FILLED", "REJECTED", "CANCELLED")
_ROLE = {"entry_order": "entry", "stop_order": "stop-loss", "exit_order": "exit"}


def _manual_conn():
    from .manual import _conn
    con = _conn()
    if "final_state" not in {r[1] for r in con.execute("PRAGMA table_info(manual_orders)")}:
        con.execute("ALTER TABLE manual_orders ADD COLUMN final_state TEXT")   # set once Telegram was told
    return con


def _manual_kind(r: dict) -> str:
    if r.get("lots") is not None:
        return "Options"
    return "Manual buy" if r.get("side") == "BUY" else "Holdings sell"


def sources() -> dict[str, dict]:
    """{order number: {"kind": auto | manual, "label", "trade"?}} for orders the app placed."""
    from .orders import all_trades
    out: dict[str, dict] = {}
    with _manual_conn() as con:
        for r in con.execute("SELECT order_id, lots, side FROM manual_orders WHERE order_id IS NOT NULL"):
            out[str(r["order_id"])] = {"kind": "manual", "label": _manual_kind(dict(r))}
    for t in all_trades():
        for col, role in _ROLE.items():
            if t.get(col):
                out[str(t[col])] = {"kind": "auto", "label": f"Auto-trade · {role}", "trade": t["key"]}
    return out


def source_of(order_id: str, known: dict[str, dict] | None = None) -> dict:
    """Who placed an order: the app (auto / manual) or something else (Kotak app or website)."""
    return (known if known is not None else sources()).get(str(order_id)) or {"kind": "external",
                                                                              "label": "Kotak app / web"}


def _book() -> dict[str, dict]:
    return {str(o.get("nOrdNo")): o for o in (broker.call("order_report").get("data") or []) if o.get("nOrdNo")}


def _amo() -> str:
    return "NO" if markets.get("IN").is_open() else "YES"


def _pending(order_id: str) -> dict:
    """Today's book row of a pending order that may be changed here. Raises ValueError otherwise."""
    from .orders import order_state
    src = source_of(order_id)
    if src["kind"] == "auto":
        raise ValueError(f"Order {order_id} belongs to an Auto-trade trade ({src['label']}). Close or cancel it "
                         "under Portfolio > App trades, so the app keeps track of it.")
    o = _book().get(str(order_id))
    if o is None:
        raise ValueError(f"Order {order_id} isn't in today's order book")
    st = order_state(o)
    if st != "OPEN":
        raise ValueError(f"Order {order_id} is no longer pending ({st.lower()})")
    return o


def cancel(order_id: str) -> dict:
    """Cancel one of your pending orders. Raises ValueError if it can't be, BrokerError if Kotak refuses."""
    o = _pending(order_id)
    broker.call("cancel_order", order_id=str(order_id), amo=_amo())
    sym = str(o.get("trdSym", ""))
    log.info("Order %s (%s) cancelled from the dashboard", order_id, sym)
    _send(f"✖️ <b>Cancel sent</b> · {sym} order {order_id}")
    return {"order_id": str(order_id), "symbol": sym}


def _tick_of(tsym: str, segment: str) -> float:
    if "fo" in segment.lower():
        from . import fno
        c = fno.contract(tsym)
        if c:
            return float(c["tick"])
    return 0.05


def _round(px: float, tick: float) -> float:
    return round(round(px / tick) * tick, 2)


def modify(order_id: str, price=None, trigger=None, qty=None) -> dict:
    """Change a pending order's price / trigger, or lower its quantity (by its unfilled part).
    The order type stays as it is: a market order has no price, a stop order needs a trigger."""
    o = _pending(order_id)
    otype = str(o.get("prcTp") or "")
    tsym, seg = str(o.get("trdSym", "")), str(o.get("exSeg") or o.get("exchangeSegment") or "")
    old_qty = int(float(o.get("qty") or 0))
    filled = int(float(o.get("fldQty") or 0))
    tick = _tick_of(tsym, seg)
    new_qty = old_qty if qty is None else qty
    if not isinstance(new_qty, int) or isinstance(new_qty, bool) or new_qty < 1:
        raise ValueError("Quantity must be a whole number, at least 1")
    if new_qty > old_qty:
        raise ValueError(f"Quantity can only be lowered here ({old_qty} now). Place a new order for more.")
    if new_qty <= filled:
        raise ValueError(f"{filled} are already filled; the new quantity must be above that")
    px = float(o.get("prc") or 0)
    if otype in ("L", "SL"):
        if price is not None:
            if float(price) <= 0:
                raise ValueError("The price must be above 0")
            px = _round(float(price), tick)
    elif price is not None:
        raise ValueError("A market order has no price to change")
    trg = float(o.get("trgPrc") or 0)
    if otype in ("SL", "SL-M"):
        if trigger is not None:
            if float(trigger) <= 0:
                raise ValueError("The trigger must be above 0")
            trg = _round(float(trigger), tick)
    elif trigger is not None:
        raise ValueError("Only stop orders have a trigger")
    side = str(o.get("trnsTp", "")).upper()
    if side in ("B", "BUY") and tsym.upper().endswith("-EQ") and px and px * new_qty > config.STOCK_MAX_ORDER_VALUE:
        raise ValueError(f"Order value ≈ ₹{px * new_qty:,.0f} is above STOCK_MAX_ORDER_VALUE "
                         f"₹{config.STOCK_MAX_ORDER_VALUE:,.0f}")
    broker.call("modify_order", order_id=str(order_id), price=f"{px:.2f}" if otype in ("L", "SL") else "0",
                order_type=otype, quantity=str(new_qty), validity=str(o.get("vldt") or "DAY"),
                trigger_price=f"{trg:.2f}" if otype in ("SL", "SL-M") else "0", amo=_amo())
    what = ", ".join(x for x in (f"price ₹{px:,.2f}" if otype in ("L", "SL") else "",
                                 f"trigger ₹{trg:,.2f}" if otype in ("SL", "SL-M") else "",
                                 f"qty {new_qty}") if x)
    log.info("Order %s (%s) modified from the dashboard: %s", order_id, tsym, what)
    _send(f"✏️ <b>Modify sent</b> · {tsym} order {order_id} · {what}")
    return {"order_id": str(order_id), "symbol": tsym, "price": px or None, "trigger": trg or None, "qty": new_qty}


# ---- order history (Kotak's book only holds today's orders) ------------------------------------

HISTORY_DAYS = 1100   # ~3 years: the tax report needs buys over a year old (long-term gains)
SNAPSHOT_SEC = 300   # the dashboard saves the order book this often in market hours (and once after the close)
_HIST_COLS = ("order_id", "day", "time", "symbol", "side", "type", "product", "qty", "filled_qty", "price",
              "trigger", "avg_price", "state", "status", "reason", "source")


def _history_conn():
    con = state._conn()
    con.execute("""CREATE TABLE IF NOT EXISTS order_history (order_id TEXT PRIMARY KEY, day TEXT, time TEXT,
        symbol TEXT, side TEXT, type TEXT, product TEXT, qty REAL, filled_qty REAL, price REAL, trigger REAL,
        avg_price REAL, state TEXT, status TEXT, reason TEXT, source TEXT, updated_at TEXT)""")
    return con


def _when(t: str) -> datetime:
    """Kotak's order time ("16-Oct-2026 10:04:16"); now if missing or in another shape."""
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(t).strip(), fmt)
        except ValueError:
            pass
    return datetime.now()


def save_history(orders: list[dict]) -> None:
    """Keep normalised orders (portfolio.norm_order + source) for the history; later reads update them."""
    if not orders:
        return
    now = datetime.now().isoformat(timespec="seconds")
    rows = []
    for o in orders:
        if not o.get("order_id"):
            continue
        w = _when(o.get("time"))
        rows.append((o["order_id"], w.date().isoformat(), w.isoformat(timespec="seconds"), o.get("symbol"),
                     o.get("side"), o.get("type"), o.get("product"), o.get("qty"), o.get("filled_qty"),
                     o.get("price"), o.get("trigger"), o.get("avg_price"), o.get("state"), o.get("status"),
                     o.get("reason"), (o.get("source") or {}).get("label"), now))
    with _history_conn() as con:
        con.executemany(f"INSERT OR REPLACE INTO order_history ({', '.join(_HIST_COLS)}, updated_at) "
                        f"VALUES ({', '.join('?' * (len(_HIST_COLS) + 1))})", rows)
        con.execute("DELETE FROM order_history WHERE day < ?",
                    (date.fromordinal(date.today().toordinal() - HISTORY_DAYS).isoformat(),))


def history(days: int = 30) -> list[dict]:
    since = date.fromordinal(date.today().toordinal() - max(1, min(days, HISTORY_DAYS))).isoformat()
    with _history_conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM order_history WHERE day >= ? ORDER BY time DESC", (since,))]


def snapshot() -> int:
    """Save today's whole order book (with who placed each order) to the history. Returns how many orders."""
    from .portfolio import norm_order
    known = sources()
    rows = [{**o, "source": source_of(o["order_id"], known)}
            for o in map(norm_order, broker.call("order_report").get("data") or [])]
    save_history(rows)
    return len(rows)


# ---- Telegram notices for manual orders ---------------------------------------------------------

def _send(text: str) -> None:
    try:
        telegram_bot.send_message(text)
    except Exception:
        log.exception("Telegram order notice failed")


def _unresolved() -> list[dict]:
    """Manual orders from today and yesterday (AMO placed the evening before) not yet reported as final."""
    since = date.fromordinal(date.today().toordinal() - 1).isoformat()
    with _manual_conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM manual_orders WHERE order_id IS NOT NULL AND final_state IS NULL AND placed_at >= ?",
            (since,))]


def _mark(row_id: int, final: str) -> None:
    with _manual_conn() as con:
        con.execute("UPDATE manual_orders SET final_state = ? WHERE id = ?", (final, row_id))


def notice(r: dict, o: dict, st: str) -> str:
    sym, oid = r["tsym"], r["order_id"]
    qty, filled = int(float(o.get("qty") or r["qty"] or 0)), int(float(o.get("fldQty") or 0))
    avg = float(o.get("avgPrc") or 0)
    head = f"{r['side']} {sym}"
    if st == "FILLED":
        return f"✅ <b>Filled</b> · {head} × {filled or qty} @ ₹{avg:,.2f} · order {oid}"
    if st == "REJECTED":
        why = str(o.get("rejRsn") or o.get("ordSt") or "no reason given")[:200]
        return f"⛔ <b>Rejected</b> · {head} × {qty} · order {oid}\n{why}"
    part = f" ({filled} of {qty} filled @ ₹{avg:,.2f})" if filled else ""
    return f"✖️ <b>Cancelled</b> · {head} × {qty}{part} · order {oid}"


def check_manual() -> int:
    """Post each manual order that reached a final state since the last check. Returns how many were posted."""
    from .orders import order_state
    rows = _unresolved()
    if not rows:
        return 0
    book = _book()
    today = date.today().isoformat()
    sent = 0
    for r in rows:
        o = book.get(str(r["order_id"]))
        st = order_state(o)
        if st in FINAL:
            _send(notice(r, o, st))
            from . import push
            push.send(f"Order {st.lower()}", re.sub(r"<[^>]+>", "", notice(r, o, st)), "/#IN/portfolio")
            _mark(r["id"], st)
            sent += 1
        elif st == "GONE" and r["placed_at"][:10] < today:
            _mark(r["id"], "GONE")   # an earlier day's order that isn't in today's book: expired
    return sent


def _watch() -> None:
    while True:
        try:
            if not broker.missing_credentials():
                check_manual()
        except Exception:
            log.exception("Manual order watch failed")
        time.sleep(WATCH_SEC)


def start_watch() -> None:
    threading.Thread(target=_watch, name="manual-order-watch", daemon=True).start()
    log.info("Watching manual orders for fills and rejections (every %d s)", WATCH_SEC)
