"""Automatic Kotak orders for India signals from the scanner: entry, protective stop, exits.

Scope: India, daily timeframe, BUY only, delivery (CNC), signals found by the scanner (run.bat / --once).
- New entries need the dashboard's Auto-trade switch ON and respect MAX_ORDERS_PER_DAY (new trades per day)
  and MAX_ORDER_VALUE (quantity is cut to fit; a share dearer than the cap is skipped).
- Entry = the signal's pending order: limit at the entry (pullback) or stop-limit triggered at it (breakout).
  Kotak orders last one day, so an unfilled entry is placed again each session until the tracker cancels the
  signal. Outside market hours orders go in as AMO. Before (re)placing an entry the order book must show no
  open buy order for that stock, so a position is never doubled.
- After a fill a stop-loss protects the position, placed again every session. At target 1 it moves to the fill
  price; at target 2 or the time exit the position is sold.
- SEBI allows no market orders via the API for retail clients, so nothing here is a market order: the stop is a
  stop-limit (limit SL_LIMIT_BUFFER below the trigger) and sells are limits MARKET_PROTECTION_PCT below the live
  price. If price gaps through a stop's limit the position is sold at a fresh price; an unfilled sell is
  re-priced after EXIT_REPRICE_SEC. A stop Kotak rejects STOP_MAX_FAILS times in a row is given up on, loudly.
- Switching Auto-trade OFF cancels unfilled entries; open positions keep their stop and exits.
Kotak's order book is the source of truth for fills. Every event is posted to Telegram.
Order statuses and field names follow the Kotak Neo order book (ordSt, fldQty, avgPrc, rejRsn, nOrdNo)."""
import json
import logging
import math
import re
from datetime import datetime

from . import broker, config, markets, state, telegram_bot

log = logging.getLogger(__name__)
SEGMENT = "nse_cm"
TICK = 0.05                 # every valid NSE price is a multiple of 0.05
STOP_LIMIT_BUFFER = 0.005   # breakout entry: limit 0.5% above the trigger
SL_LIMIT_BUFFER = 0.01      # stop-loss = stop-limit: limit 1% below the trigger
EXIT_REPRICE_SEC = 120      # an unfilled sell is cancelled and placed again at the live price after this long
STOP_MAX_FAILS = 3          # stop-loss rejections in a row before the app stops retrying (and says so)

# Live trade states
NEW, ENTRY, OPEN, EXITING = "NEW", "ENTRY", "OPEN", "EXITING"            # in progress
CLOSED, CANCELLED, REJECTED, SKIPPED = "CLOSED", "CANCELLED", "REJECTED", "SKIPPED"  # final
ACTIVE_STATES = (NEW, ENTRY, OPEN, EXITING)

_COLUMNS = """key TEXT PRIMARY KEY, symbol TEXT, state TEXT, qty INTEGER, entry_type TEXT, entry REAL,
    stop REAL, entry_order TEXT, stop_order TEXT, exit_order TEXT, fill_price REAL, exit_price REAL,
    be_moved INTEGER DEFAULT 0, created_day TEXT, opened_at TEXT, closed_at TEXT, note TEXT, alerted TEXT,
    updated_at TEXT, exit_at TEXT, stop_fails INTEGER DEFAULT 0"""
_ADDED = {"exit_at": "TEXT", "stop_fails": "INTEGER DEFAULT 0"}   # columns newer than the first version


def _conn():
    con = state._conn()
    con.execute(f"CREATE TABLE IF NOT EXISTS live_trades ({_COLUMNS})")
    cols = {r[1] for r in con.execute("PRAGMA table_info(live_trades)")}
    for name, typ in _ADDED.items():
        if name not in cols:
            con.execute(f"ALTER TABLE live_trades ADD COLUMN {name} {typ}")
    return con


def all_trades() -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM live_trades ORDER BY created_day DESC, key")]


def _get(key: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM live_trades WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def _save(key: str, **fields) -> None:
    fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
    with _conn() as con:
        if con.execute("SELECT 1 FROM live_trades WHERE key = ?", (key,)).fetchone():
            con.execute(f"UPDATE live_trades SET {', '.join(f'{k} = ?' for k in fields)} WHERE key = ?",
                        (*fields.values(), key))
        else:
            con.execute(f"INSERT INTO live_trades (key, {', '.join(fields)}) VALUES (?, {', '.join('?' * len(fields))})",
                        (key, *fields.values()))


def _notify(text: str) -> None:
    try:
        telegram_bot.send_message(f"💰 {text}")
    except Exception:
        log.exception("Telegram order notice failed")
    from . import push
    push.send("Auto-trade", re.sub(r"<[^>]+>", "", text), "/#IN/portfolio")


def _today() -> str:
    return markets.get("IN").now().date().isoformat()


def _tick(px: float, up: bool) -> float:
    n = px / TICK
    return round((math.ceil(n - 1e-9) if up else math.floor(n + 1e-9)) * TICK, 2)


def _market_open() -> bool:
    return markets.get("IN").is_open()


def _amo() -> str:
    return "NO" if _market_open() else "YES"


# ---- order book -------------------------------------------------------------------------------

def _book() -> dict[str, dict]:
    """Today's Kotak order book by order number (also saved to data/ for inspection)."""
    res = broker.call("order_report")
    rows = res.get("data") or []
    try:
        (config.DATA_DIR / "kotak_orders_last.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    except OSError:
        pass
    return {str(o.get("nOrdNo")): o for o in rows if o.get("nOrdNo")}


def order_state(o: dict | None) -> str:
    """FILLED / REJECTED / CANCELLED / OPEN, or GONE when the order isn't in today's book (expired)."""
    if o is None:
        return "GONE"
    s = str(o.get("ordSt") or o.get("status") or "").lower()
    if s in ("complete", "completed", "traded", "filled"):
        return "FILLED"
    if "reject" in s:
        return "REJECTED"
    if "cancel" in s or "expire" in s or "lapse" in s:
        return "CANCELLED"
    return "OPEN"


def _filled(o: dict) -> tuple[int, float | None]:
    qty = int(float(o.get("fldQty") or 0))
    avg = float(o.get("avgPrc") or 0) or None
    return qty, avg


def _reason(o: dict) -> str:
    return str(o.get("rejRsn") or o.get("rejReason") or o.get("ordSt") or "unknown")[:200]


def _open_buy_for(book: dict[str, dict], symbol: str) -> bool:
    ts = f"{symbol}-EQ"
    return any(order_state(o) == "OPEN" and str(o.get("trdSym", "")).upper() == ts
               and str(o.get("trnsTp", "")).upper() in ("B", "BUY") for o in book.values())


def _place(**kw) -> str:
    res = broker.call("place_order", exchange_segment=SEGMENT, product="CNC", validity="DAY", amo=_amo(), **kw)
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    oid = res.get("nOrdNo") or data.get("nOrdNo")
    if not oid:
        raise broker.BrokerError(f"place_order: no order number in {str(res)[:200]}")
    return str(oid)


def _place_entry(t: dict) -> str:
    kw = dict(trading_symbol=f"{t['symbol']}-EQ", transaction_type="B", quantity=str(t["qty"]))
    if t["entry_type"] == "stop":   # breakout: stop-limit triggered at the entry
        return _place(order_type="SL", trigger_price=f"{_tick(t['entry'], True):.2f}",
                      price=f"{_tick(t['entry'] * (1 + STOP_LIMIT_BUFFER), True):.2f}", **kw)
    return _place(order_type="L", price=f"{_tick(t['entry'], False):.2f}", trigger_price="0", **kw)


def _stop_limit(stop: float) -> float:
    return _tick(stop * (1 - SL_LIMIT_BUFFER), False)


def _place_stop(t: dict, qty: int, stop: float) -> str:
    """Stop-loss as a stop-limit: triggers at the stop, sells down to SL_LIMIT_BUFFER below it (no SL-M via API)."""
    return _place(order_type="SL", price=f"{_stop_limit(stop):.2f}", trigger_price=f"{_tick(stop, False):.2f}",
                  trading_symbol=f"{t['symbol']}-EQ", transaction_type="S", quantity=str(qty))


def _ltp(symbol: str) -> float:
    from . import kotak   # read-only quotes
    q = kotak.quotes([symbol]).get(symbol)
    if not q or not q.get("ltp"):
        raise broker.BrokerError(f"no live price for {symbol}, so the sell can't be priced (market orders aren't "
                                 "allowed via the API)")
    return float(q["ltp"])


def sell_price(ltp: float) -> float:
    """A sell that fills like a market order: a limit MARKET_PROTECTION_PCT below the live price."""
    return _tick(ltp * (1 - config.MARKET_PROTECTION_PCT / 100), False)


def _place_exit(t: dict, qty: int | None = None, ltp: float | None = None) -> str:
    px = sell_price(ltp or _ltp(t["symbol"]))
    return _place(order_type="L", price=f"{px:.2f}", trigger_price="0", trading_symbol=f"{t['symbol']}-EQ",
                  transaction_type="S", quantity=str(qty or t["qty"]))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _age(ts: str | None) -> float:
    return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() if ts else 1e9


def _cancel(oid: str | None) -> None:
    if oid:
        try:
            broker.call("cancel_order", order_id=oid)
        except broker.BrokerError as exc:
            log.warning("Cancel of order %s failed: %s", oid, exc)


# ---- entries ----------------------------------------------------------------------------------

def trade_risk(t: dict) -> float:
    """₹ lost if this trade (pending entry or open position) hits its stop: (entry or fill - stop) x qty."""
    if t["state"] not in ACTIVE_STATES or not t["qty"] or t["stop"] is None:
        return 0.0
    return max((t["fill_price"] or t["entry"]) - t["stop"], 0.0) * t["qty"]


def open_risk() -> float:
    """Total ₹ at risk at the stops across every live app trade (MAX_OPEN_RISK_PCT caps it)."""
    return round(sum(trade_risk(t) for t in all_trades()), 2)



def _eligible(sig: dict) -> str | None:
    """Why a signal can't be traded automatically, or None."""
    if sig["market"] != "IN":
        return "not an India signal"
    if sig["side"] != "BUY":
        return "only BUY signals are traded (delivery)"
    if (sig["timeframe"] or "1d") != "1d":
        return "only daily signals are traded"
    if sig["status"] not in ("PENDING", "ACTIVE"):
        return f"signal already {sig['status']}"
    return None


def enter(keys: list[str]) -> list[dict]:
    """Place entry orders for new scanner signals. Returns the trades created (placed, skipped or rejected)."""
    if not keys or not broker.trading_enabled():
        return []
    sigs = {s["key"]: s for s in state.all_signals("IN")}
    out = []
    for key in keys:
        sig = sigs.get(key)
        if sig is None or _get(key):
            continue
        t = {"symbol": sig["symbol"], "entry_type": sig["entry_type"] or "limit", "entry": sig["entry"],
             "stop": sig["stop_loss"], "created_day": _today()}
        why = _eligible(sig)
        today = sum(1 for x in all_trades() if x["created_day"] == t["created_day"] and x["entry_order"])
        risk = abs(sig["entry"] - sig["stop_loss"])
        mkt = markets.get("IN")
        qty = int((mkt.capital * config.RISK_PER_TRADE_PCT / 100) // risk) if risk else 0
        qty = min(qty, int(mkt.capital // sig["entry"]), int(config.MAX_ORDER_VALUE // sig["entry"]))
        if why is None and today >= config.MAX_ORDERS_PER_DAY:
            why = f"daily limit of {config.MAX_ORDERS_PER_DAY} orders reached"
        if why is None and qty < 1:
            why = f"one share costs more than MAX_ORDER_VALUE ₹{config.MAX_ORDER_VALUE:,.0f}"
        cap = mkt.capital * config.MAX_OPEN_RISK_PCT / 100
        if why is None and open_risk() + risk * qty > cap:
            why = (f"portfolio risk limit: ₹{open_risk():,.0f} already at risk, this trade adds ₹{risk * qty:,.0f}, "
                   f"limit ₹{cap:,.0f} (MAX_OPEN_RISK_PCT {config.MAX_OPEN_RISK_PCT:g}%)")
        if why:
            _save(key, **t, state=SKIPPED, qty=0, note=why)
            log.info("Not trading %s: %s", key, why)
            out.append(_get(key))
            continue
        t["qty"] = qty
        _save(key, **t, state=NEW)
        out.append(_try_entry(key, book=None))
    return out


def trade_new(keys: list[str]) -> list[dict]:
    """Entries for new signals (only while Auto-trade is ON), then bring every live trade up to date. Returns the
    trades created. Order problems are logged and sent to Telegram; they never stop the caller."""
    try:
        made = enter(keys)
        reconcile()
        return made
    except Exception:
        log.exception("Automatic orders failed")
        return []


def _try_entry(key: str, book: dict | None) -> dict:
    t = _get(key)
    try:
        book = _book() if book is None else book
        if _open_buy_for(book, t["symbol"]):
            _save(key, note="an open buy order for this stock already exists; not placing another")
            return _get(key)
        oid = _place_entry(t)
    except broker.BrokerError as exc:
        _save(key, note=f"entry not placed yet: {exc}")
        if t["alerted"] != "entry_error":
            _notify(f"⚠️ <b>{t['symbol']}</b> entry order failed: {exc}. Retrying on the next loop.")
            _save(key, alerted="entry_error")
        return _get(key)
    first = not t["entry_order"]
    _save(key, state=ENTRY, entry_order=oid, note=None, alerted=None)
    if first:
        _notify(f"🛒 <b>BUY {t['symbol']}</b> × {t['qty']} · {t['entry_type']} @ ₹{t['entry']:,.2f}"
                f"{' (AMO)' if _amo() == 'YES' else ''} · stop ₹{t['stop']:,.2f} · order {oid}")
    return _get(key)


# ---- reconcile ----------------------------------------------------------------------------------

def reconcile() -> None:
    """Bring every live trade in line with Kotak's order book and the signal tracker. Safe to call often."""
    live = [t for t in all_trades() if t["state"] in ACTIVE_STATES]
    if not live:
        return
    book = _book()
    sigs = {s["key"]: s for s in state.all_signals("IN")}
    enabled = broker.trading_enabled()
    ltps = _live_prices([t["symbol"] for t in live if t["state"] == OPEN])
    for t in live:
        try:
            _step(t, book, sigs.get(t["key"]), enabled, ltps.get(t["symbol"]))
        except broker.BrokerError as exc:
            log.warning("Order step for %s failed: %s", t["key"], exc)
            _save(t["key"], note=str(exc)[:300])
        except Exception:
            log.exception("Order step for %s crashed", t["key"])


def _live_prices(symbols: list[str]) -> dict[str, float]:
    """Live prices of open positions (to spot a stop that price gapped through); {} if quotes fail."""
    if not symbols or not _market_open():
        return {}
    try:
        from . import kotak
        return {s: float(q["ltp"]) for s, q in kotak.quotes(symbols).items() if q.get("ltp")}
    except Exception:
        log.exception("Live prices for open positions failed")
        return {}


def _step(t: dict, book: dict, sig: dict | None, enabled: bool, ltp: float | None = None) -> None:
    key, st = t["key"], t["state"]
    sig_status = sig["status"] if sig else "CANCELLED"
    if st == NEW:
        if not enabled or sig_status not in ("PENDING", "ACTIVE"):
            _save(key, state=CANCELLED, note="auto-trade off" if not enabled else f"signal {sig_status}")
        else:
            _try_entry(key, book)
        return
    if st == ENTRY:
        _step_entry(t, book, sig_status, enabled)
    elif st == OPEN:
        _step_open(t, book, sig_status, ltp)
    elif st == EXITING:
        _step_exit(t, book)


def _step_entry(t: dict, book: dict, sig_status: str, enabled: bool) -> None:
    key, o = t["key"], book.get(t["entry_order"])
    os_ = order_state(o)
    qty, avg = _filled(o) if o else (0, None)
    if os_ == "FILLED" or (qty and os_ != "OPEN"):
        _opened(t, qty or t["qty"], avg or t["entry"])
        return
    if os_ == "REJECTED":
        _save(key, state=REJECTED, note=_reason(o))
        _notify(f"❌ <b>{t['symbol']}</b> entry rejected by Kotak: {_reason(o)}")
        return
    if not enabled or sig_status not in ("PENDING", "ACTIVE"):
        if os_ == "OPEN":
            _cancel(t["entry_order"])
        why = "auto-trade switched off" if not enabled else f"signal {sig_status.lower()}"
        _save(key, state=CANCELLED, note=why)
        _notify(f"✖️ <b>{t['symbol']}</b> entry cancelled ({why})")
        return
    # Kotak orders last a day: place it again while the signal is pending. An order missing from the book
    # (the book only holds today's orders) is re-placed only in market hours, when last night's AMO would show.
    if os_ == "CANCELLED" or (os_ == "GONE" and _market_open()):
        _try_entry(key, book)


def _opened(t: dict, qty: int, fill: float) -> None:
    key = t["key"]
    _save(key, state=OPEN, qty=qty, fill_price=fill, opened_at=datetime.now().isoformat(timespec="seconds"),
          stop_order=None, stop_fails=0)
    _notify(f"✅ <b>{t['symbol']}</b> bought × {qty} @ ₹{fill:,.2f}")
    _ensure_stop(_get(key), {})   # no stop order yet: placed straight away (AMO if the market is closed)


def _ensure_stop(t: dict, book: dict) -> None:
    """Keep a working stop-loss order for an open position."""
    o = book.get(t["stop_order"]) if t["stop_order"] else None
    os_ = order_state(o) if t["stop_order"] else "NONE"
    if os_ == "OPEN" or (os_ == "GONE" and not _market_open()):   # see _step_entry on GONE
        return
    if os_ == "FILLED":
        qty, avg = _filled(o)
        _closed(t, avg or t["stop"], "stop-loss")
        return
    fails = (t["stop_fails"] or 0) + (os_ == "REJECTED")
    if fails >= STOP_MAX_FAILS:
        if t["alerted"] != "stop_gave_up":
            why = _reason(o) if o else "rejected"
            _save(t["key"], stop_fails=fails, alerted="stop_gave_up",
                  note=f"NO STOP: Kotak rejected the stop-loss {fails} times ({why})")
            _notify(f"🚨 <b>{t['symbol']}</b> has NO stop-loss: Kotak rejected it {fails} times ({why}). "
                    "The app has stopped retrying. Place a stop in the Kotak app, or Close the trade on the dashboard.")
        return
    try:
        oid = _place_stop(t, t["qty"], t["stop"])
    except broker.BrokerError as exc:
        _save(t["key"], note=f"NO STOP: {exc}")
        if t["alerted"] != "no_stop":
            _notify(f"🚨 <b>{t['symbol']}</b> has NO stop-loss: {exc}. Retrying every loop; check the position.")
            _save(t["key"], alerted="no_stop")
        return
    _save(t["key"], stop_order=oid, note=None, alerted=None, stop_fails=fails)
    if os_ == "REJECTED":
        _notify(f"⚠️ <b>{t['symbol']}</b> stop-loss rejected by Kotak ({_reason(o)}); placed again "
                f"(try {fails + 1} of {STOP_MAX_FAILS}) · order {oid}")
    elif os_ in ("CANCELLED", "GONE") and _market_open():
        _notify(f"⚠️ <b>{t['symbol']}</b> stop-loss order {t['stop_order']} was cancelled (in the Kotak app?) "
                f"during market hours; placed again @ ₹{t['stop']:,.2f} · order {oid}")
    elif not t["stop_order"]:
        _notify(f"🛡️ <b>{t['symbol']}</b> stop-loss @ ₹{t['stop']:,.2f} placed · order {oid}")


def _step_open(t: dict, book: dict, sig_status: str, ltp: float | None = None) -> None:
    key = t["key"]
    stop_o = book.get(t["stop_order"]) if t["stop_order"] else None
    if order_state(stop_o) == "FILLED":
        _, avg = _filled(stop_o)
        _closed(t, avg or t["stop"], "stop-loss")
        return
    gapped = ltp is not None and order_state(stop_o) == "OPEN" and ltp < _stop_limit(t["stop"])
    if sig_status in ("T2_HIT", "EXPIRED") or gapped:
        why = "stop-loss (price gapped below its limit)" if gapped else "target 2" if sig_status == "T2_HIT" else "time exit"
        oid = _place_exit(t, ltp=ltp)   # the sell goes in before the stop is cancelled
        if order_state(stop_o) == "OPEN":
            _cancel(t["stop_order"])
        _save(key, state=EXITING, exit_order=oid, exit_at=_now(), note=why)
        _notify(f"📤 <b>{t['symbol']}</b> selling × {t['qty']} ({why}) · limit ₹{sell_price(ltp or _ltp(t['symbol'])):,.2f}"
                if ltp else f"📤 <b>{t['symbol']}</b> selling × {t['qty']} ({why})")
        return
    if sig_status == "T1_HIT" and not t["be_moved"]:
        new_stop = max(t["stop"], t["fill_price"] or t["entry"])
        if order_state(stop_o) == "OPEN":
            _cancel(t["stop_order"])
        _save(key, stop=new_stop, be_moved=1, stop_order=None, stop_fails=0)
        _notify(f"🎯 <b>{t['symbol']}</b> target 1 reached; stop moved to ₹{new_stop:,.2f}")
        t = _get(key)
    _ensure_stop(t, book)


def _step_exit(t: dict, book: dict) -> None:
    o = book.get(t["exit_order"])
    os_ = order_state(o)
    if os_ == "FILLED":
        _, avg = _filled(o)
        _closed(t, avg, t["note"] or "exit")
        return
    if os_ == "OPEN":
        if not (_market_open() and _age(t["exit_at"]) > EXIT_REPRICE_SEC):
            return
        _cancel(t["exit_order"])   # price moved away from the limit: sell again at a fresh price
        o = _book().get(t["exit_order"])
        os_ = order_state(o)
        if os_ == "FILLED":        # filled just before the cancel
            _, avg = _filled(o)
            _closed(t, avg, t["note"] or "exit")
            return
        if os_ == "OPEN":
            return                 # cancel not through yet: next loop
    if os_ in ("REJECTED", "CANCELLED") or (os_ == "GONE" and _market_open()):
        if os_ == "REJECTED":
            _notify(f"⚠️ <b>{t['symbol']}</b> exit rejected ({_reason(o)}); trying again")
        sold, _ = _filled(o) if o else (0, None)   # a partly filled sell: only the rest is sold again
        left = t["qty"] - sold
        if sold:
            _save(t["key"], qty=left, note=f"{t['note'] or 'exit'} · {sold} sold earlier")
        _save(t["key"], exit_order=_place_exit(t, qty=left), exit_at=_now())


CLOSE_NOTE = "closed by you"


def close_trade(key: str) -> dict:
    """Close an app trade by hand, whatever the Auto-trade switch says. An unfilled entry is cancelled (shares
    from a partial fill are sold); an open position is sold (AMO if the market is closed). The sell
    goes in before the stop is cancelled, so the scanner never sees a position with neither. The sell is a limit
    MARKET_PROTECTION_PCT below the live price (re-priced by reconcile if it doesn't fill)."""
    t = _get(key)
    if t is None:
        raise ValueError("Unknown trade")
    if t["state"] == EXITING:
        raise ValueError(f"{t['symbol']} is already being sold")
    if t["state"] not in (NEW, ENTRY, OPEN):
        raise ValueError(f"{t['symbol']} is not open ({t['state'].lower()})")
    if t["state"] in (NEW, ENTRY):
        if t["entry_order"]:
            o = _book().get(t["entry_order"])
            if order_state(o) == "OPEN":
                _cancel(t["entry_order"])
                o = _book().get(t["entry_order"])
            qty, avg = _filled(o) if o else (0, None)
            if qty:   # partly / fully filled before the cancel: those shares are sold below
                _save(key, state=OPEN, qty=qty, fill_price=avg or t["entry"],
                      opened_at=datetime.now().isoformat(timespec="seconds"))
                t = _get(key)
        if t["state"] != OPEN:
            _save(key, state=CANCELLED, note=CLOSE_NOTE)
            _notify(f"✖️ <b>{t['symbol']}</b> entry cancelled ({CLOSE_NOTE})")
            return _get(key)
    oid = _place_exit(t)
    _save(key, state=EXITING, exit_order=oid, exit_at=_now(), note=CLOSE_NOTE)
    if t["stop_order"]:
        _cancel(t["stop_order"])
    _notify(f"📤 <b>{t['symbol']}</b> selling × {t['qty']} ({CLOSE_NOTE}"
            f"{', AMO: executes at the next open' if _amo() == 'YES' else ''})")
    return _get(key)


def _closed(t: dict, price: float | None, why: str) -> None:
    fill = t["fill_price"] or t["entry"]
    _save(t["key"], state=CLOSED, exit_price=price, closed_at=datetime.now().isoformat(timespec="seconds"), note=why)
    pnl = (price - fill) * t["qty"] if price else None
    _notify(f"🏁 <b>{t['symbol']}</b> closed ({why}) @ ₹{price or 0:,.2f}"
            + (f" · P&L ₹{pnl:,.0f} before charges" if pnl is not None else ""))
