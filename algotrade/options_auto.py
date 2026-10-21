"""Options Auto-trade: India signals on F&O stocks and indices bought as an out-of-the-money option.

A BUY signal buys a call, a SELL signal a put; options are only ever bought (the most a trade can lose is its
premium), never written. The option follows the signal on the underlying's live price:
- Entry: once the underlying reaches the signal's entry (the same pending order the stock trade uses: a limit
  for pullbacks, a stop for breakouts; or the tracker has filled the signal), the contract is picked on the spot
  (fno.otm_contract: nearest expiry at least OPTIONS_MIN_DAYS away, the OPTIONS_OTM_STEPS-th strike out of the
  money) and bought with a marketable limit off the best ask (OPTION_PROTECTION_PCT; SEBI allows no market
  orders via the API). Lots = OPTIONS_MAX_PREMIUM / premium of one lot, at most OPTIONS_MAX_LOTS; the premium of
  all open option trades stays under OPTIONS_MAX_OPEN_PREMIUM. A contract with no two-sided quote or a bid-ask
  spread over OPTIONS_MAX_SPREAD_PCT is skipped. Market hours only (no AMO: the price would be stale).
- Exit: the option is sold (marketable limit off the best bid) when the underlying reaches the signal's stop or
  target 1, when the tracker closes the signal (time exit, or a stop / target it saw first), on the option's
  expiry day from INTRADAY_SQUARE_OFF, and for intraday signals (MIS) at INTRADAY_SQUARE_OFF. No exit orders
  rest at Kotak, so the scanner must be running for exits to happen.
- Needs OPTIONS_AUTO_TRADE and the dashboard's Auto-trade switch ON; intraday signals also need
  INTRADAY_AUTO_TRADE and enter only until INTRADAY_LAST_ENTRY. One option trade per underlying at a time.
- The scanner runs one watcher thread for all option trades (watch), checking every orders.WATCH_SEC in market
  hours. Unfilled buys / sells are re-priced after REPRICE_SEC.
Every event is posted to Telegram. Kotak's order book is the source of truth for fills."""
import logging
import threading
from datetime import date, datetime

from . import broker, config, fno, markets, orders, state
from .manual import protect
from .orders import order_state, _filled, _reason

log = logging.getLogger(__name__)
REPRICE_SEC = 60            # an unfilled buy / sell is cancelled after this long (a sell is placed again at once)
_lock = threading.RLock()

# Trade states
WAIT, BUYING, OPEN, SELLING = "WAIT", "BUYING", "OPEN", "SELLING"                  # in progress
CLOSED, CANCELLED, REJECTED, SKIPPED = "CLOSED", "CANCELLED", "REJECTED", "SKIPPED"  # final
ACTIVE_STATES = (WAIT, BUYING, OPEN, SELLING)

_COLUMNS = """key TEXT PRIMARY KEY, underlying TEXT, side TEXT, opt TEXT, state TEXT, timeframe TEXT, product TEXT,
    u_entry REAL, u_entry_type TEXT, u_stop REAL, u_target REAL, tsym TEXT, expiry TEXT, strike REAL, lot INTEGER,
    lots INTEGER, qty INTEGER, buy_order TEXT, buy_price REAL, buy_at TEXT, fill_price REAL, sell_order TEXT,
    sell_at TEXT, exit_price REAL, created_day TEXT, opened_at TEXT, closed_at TEXT, note TEXT, updated_at TEXT"""


def _conn():
    con = state._conn()
    con.execute(f"CREATE TABLE IF NOT EXISTS option_trades ({_COLUMNS})")
    return con


def all_trades() -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM option_trades ORDER BY created_day DESC, key")]


def _get(key: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM option_trades WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def _save(key: str, **fields) -> None:
    fields["updated_at"] = _now()
    with _conn() as con:
        if con.execute("SELECT 1 FROM option_trades WHERE key = ?", (key,)).fetchone():
            con.execute(f"UPDATE option_trades SET {', '.join(f'{k} = ?' for k in fields)} WHERE key = ?",
                        (*fields.values(), key))
        else:
            con.execute(f"INSERT INTO option_trades (key, {', '.join(fields)}) VALUES "
                        f"(?, {', '.join('?' * len(fields))})", (key, *fields.values()))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _age(ts: str | None) -> float:
    return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() if ts else 1e9


def _mkt():
    return markets.get("IN")


def _today() -> str:
    return _mkt().now().date().isoformat()


def _notify(text: str) -> None:
    orders._notify(f"🎟️ {text}")


def intraday(t: dict) -> bool:
    return (t.get("timeframe") or "1d") != "1d"


def _name(t: dict) -> str:
    return t["tsym"] or f"{t['underlying']} {'call' if t['opt'] == 'CE' else 'put'}"


def open_premium() -> float:
    """₹ of premium in live option trades (bought or being bought): the most they can lose."""
    return round(sum((t["fill_price"] or t["buy_price"] or 0) * (t["qty"] or 0) for t in all_trades()
                     if t["state"] in (BUYING, OPEN, SELLING)), 2)


# ---- new signals ----------------------------------------------------------------------------------

def _has_options(symbol: str) -> bool:
    try:
        return symbol in {u["name"] for u in fno.underlyings()}
    except Exception:
        log.exception("F&O contract list unavailable")
        return False


def _eligible(sig: dict) -> str | None:
    """Why a signal on an F&O underlying can't become an option trade, or None."""
    if sig["status"] not in ("PENDING", "ACTIVE"):
        return f"signal already {sig['status']}"
    if (sig["timeframe"] or "1d") != "1d":
        if not config.INTRADAY_AUTO_TRADE:
            return "intraday signals aren't traded (Auto-trade intraday signals is off in Settings)"
        if _mkt().now().time() >= orders._hhmm(config.INTRADAY_LAST_ENTRY):
            return f"too late for an intraday entry (after {config.INTRADAY_LAST_ENTRY})"
    if any(t["underlying"] == sig["symbol"] and t["state"] in ACTIVE_STATES for t in all_trades()):
        return f"already trading {sig['symbol']} options (from another signal)"
    return None


def trade_new(keys: list[str]) -> list[dict]:
    """Option trades for new India signals on F&O underlyings (only while OPTIONS_AUTO_TRADE and Auto-trade are
    on). Each waits (WAIT) for the underlying to reach the signal's entry; the watcher buys it then. Signals on
    stocks without options are ignored. Returns the trades created. Never raises."""
    try:
        if not keys or not config.OPTIONS_AUTO_TRADE or not broker.trading_enabled():
            return []
        sigs = {s["key"]: s for s in state.all_signals("IN")}
        out = []
        with _lock:
            for key in keys:
                sig = sigs.get(key)
                if sig is None or _get(key) or not _has_options(sig["symbol"]):
                    continue
                t = {"underlying": sig["symbol"], "side": sig["side"], "opt": "CE" if sig["side"] == "BUY" else "PE",
                     "timeframe": sig["timeframe"] or "1d", "u_entry": sig["entry"],
                     "u_entry_type": sig["entry_type"] or "limit", "u_stop": sig["stop_loss"],
                     "u_target": sig["target1"], "created_day": _today()}
                t["product"] = "MIS" if intraday(t) else "NRML"
                why = _eligible(sig)
                _save(key, **t, state=SKIPPED if why else WAIT, note=why)
                if why:
                    log.info("No option trade for %s: %s", key, why)
                out.append(_get(key))
        return out
    except Exception:
        log.exception("Options Auto-trade failed")
        return []


# ---- the watcher ------------------------------------------------------------------------------------

def watch(done: threading.Event) -> None:
    """The scanner's thread for every option trade, until `done` is set. Idle (no live trades): a look every
    orders.GUARD_SEC for new ones."""
    while not done.is_set():
        live = step_all()
        done.wait(orders.GUARD_SEC if not live else orders.WATCH_SEC if _mkt().is_open() else orders.WATCH_IDLE_SEC)


def step_all() -> bool:
    """Bring every live option trade in line with the underlying's price and Kotak's order book. True if there
    were any."""
    try:
        with _lock:
            live = [t for t in all_trades() if t["state"] in ACTIVE_STATES]
            if not live:
                return False
            book = orders._book() if any(t["state"] in (BUYING, SELLING) for t in live) else {}
            sigs = {s["key"]: s for s in state.all_signals("IN")}
            enabled = broker.trading_enabled() and config.OPTIONS_AUTO_TRADE
            for t in live:
                try:
                    _step(t, book, sigs.get(t["key"]), enabled)
                except broker.BrokerError as exc:
                    log.warning("Option trade %s: %s", t["key"], exc)
                    _save(t["key"], note=str(exc)[:300])
                except Exception:
                    log.exception("Option trade %s crashed", t["key"])
            return True
    except Exception:
        log.exception("Option trades watcher failed")
        return True


def _spot(underlying: str) -> float | None:
    try:
        return fno.spot(underlying)["ltp"]
    except Exception:
        log.exception("Live price for %s unavailable", underlying)
        return None


def _step(t: dict, book: dict, sig: dict | None, enabled: bool) -> None:
    st, m = t["state"], _mkt()
    sig_status = sig["status"] if sig else "CANCELLED"
    if st != WAIT and t["expiry"] and date.fromisoformat(t["expiry"]) < m.now().date():
        _save(t["key"], state=CLOSED, closed_at=_now(), note="the contract expired while the app held it; "
              "check the trade in the Kotak app")
        _notify(f"⚠️ <b>{_name(t)}</b> expired while the app still held it. Check it in the Kotak app.")
        return
    if intraday(t) and (t["created_day"] < _today() or (not m.is_open() and m.is_after_close())):
        _day_over(t)
        return
    if st == WAIT:
        _step_wait(t, sig_status, enabled)
    elif st == BUYING:
        _step_buying(t, book, enabled)
    elif st == OPEN:
        _step_open(t, sig_status)
    elif st == SELLING:
        _step_selling(t, book)


def _day_over(t: dict) -> None:
    """An intraday option trade still live after its day: Kotak squared off any MIS position itself."""
    if t["state"] in (WAIT, BUYING):
        if t["buy_order"]:
            orders._cancel(t["buy_order"])
        _save(t["key"], state=CANCELLED, note="intraday entry: the trading day is over")
        return
    _save(t["key"], state=CLOSED, closed_at=_now(),
          note="intraday option after the close: squared off by Kotak (see its P&L there)")
    _notify(f"⚠️ <b>{_name(t)}</b> intraday option from {t['created_day']} was still open in the app after the "
            "close; Kotak squares these off itself. Check it in the Kotak app.")


def _reached(t: dict, u: float) -> bool:
    """The underlying is at the signal's entry: a limit (pullback) at or through it, a stop (breakout) past it."""
    buy = t["side"] == "BUY"
    if t["u_entry_type"] == "stop":
        return u >= t["u_entry"] if buy else u <= t["u_entry"]
    return u <= t["u_entry"] if buy else u >= t["u_entry"]


def _past(level: float, u: float, side: str, toward_target: bool) -> bool:
    """The underlying has reached a level: the target side (up for a BUY) or the stop side."""
    return (u >= level) if (side == "BUY") == toward_target else (u <= level)


def _step_wait(t: dict, sig_status: str, enabled: bool) -> None:
    key = t["key"]
    if not enabled or sig_status not in ("PENDING", "ACTIVE"):
        why = "auto-trade switched off" if not enabled else f"signal {sig_status.lower()}"
        _save(key, state=CANCELLED, note=why)
        return
    m = _mkt()
    if not m.is_open():
        return
    if intraday(t) and m.now().time() >= orders._hhmm(config.INTRADAY_LAST_ENTRY):
        _save(key, state=CANCELLED, note=f"entry not reached by {config.INTRADAY_LAST_ENTRY}")
        return
    u = _spot(t["underlying"])   # the contract is picked when the entry is reached, around this price
    if u is None or not (sig_status == "ACTIVE" or _reached(t, u)):
        return
    if _past(t["u_stop"], u, t["side"], False):
        _save(key, state=CANCELLED, note=f"{t['underlying']} already past the stop ({u:,.2f})")
        return
    if t["u_target"] is not None and _past(t["u_target"], u, t["side"], True):
        _save(key, state=CANCELLED, note=f"{t['underlying']} already at the target ({u:,.2f})")
        return
    _buy(t, u)


def _buy(t: dict, u: float) -> None:
    """Pick the contract, size it and send the buy. Anything that rules the trade out skips it, with the reason."""
    key = t["key"]
    c = fno.otm_contract(t["underlying"], t["opt"], u, config.OPTIONS_OTM_STEPS, config.OPTIONS_MIN_DAYS)
    if c is None:
        _save(key, state=SKIPPED, note=f"no {t['opt']} {config.OPTIONS_OTM_STEPS} strikes out of the money with "
              f"{config.OPTIONS_MIN_DAYS}+ days to expiry")
        return
    tsym, lot, tick = c["tsym"], int(c["lot"]), float(c["tick"])
    q = fno.option_quote(tsym)
    bid, ask = q.get("bid"), q.get("ask")
    why = None
    if not bid or not ask:
        why = f"{tsym} has no two-sided quote (bid {bid}, ask {ask})"
    elif (ask - bid) / ((ask + bid) / 2) * 100 > config.OPTIONS_MAX_SPREAD_PCT:
        why = (f"{tsym} spread too wide: bid ₹{bid:,.2f} / ask ₹{ask:,.2f} "
               f"(over OPTIONS_MAX_SPREAD_PCT {config.OPTIONS_MAX_SPREAD_PCT:g}%)")
    if why is None:
        px = protect(float(ask), "BUY", config.OPTION_PROTECTION_PCT, tick)
        lots = min(int(config.OPTIONS_MAX_PREMIUM // (px * lot)), config.OPTIONS_MAX_LOTS)
        used = open_premium()
        if lots < 1:
            why = (f"one lot of {tsym} ({lot} × ₹{px:,.2f} = ₹{px * lot:,.0f}) costs more than OPTIONS_MAX_PREMIUM "
                   f"₹{config.OPTIONS_MAX_PREMIUM:,.0f}")
        elif used + px * lot * lots > config.OPTIONS_MAX_OPEN_PREMIUM:
            lots = int((config.OPTIONS_MAX_OPEN_PREMIUM - used) // (px * lot))
            if lots < 1:
                why = (f"open premium limit: ₹{used:,.0f} already in options, OPTIONS_MAX_OPEN_PREMIUM "
                       f"₹{config.OPTIONS_MAX_OPEN_PREMIUM:,.0f}")
    contract = dict(tsym=tsym, expiry=str(c["expiry"]), strike=float(c["strike"]), lot=lot)
    if why:
        _save(key, **contract, state=SKIPPED, note=why)
        log.info("No option trade for %s: %s", key, why)
        return
    qty = lots * lot
    oid = _place(t, tsym, "B", qty, px)
    _save(key, **contract, lots=lots, qty=qty, buy_order=oid, buy_price=px, buy_at=_now(), state=BUYING, note=None)
    _notify(f"🛒 <b>BUY {tsym}</b> × {lots} lot{'s' if lots > 1 else ''} ({qty}) · limit ₹{px:,.2f} · "
            f"{t['underlying']} {u:,.2f} reached the {t['side']} signal's entry ₹{t['u_entry']:,.2f} · "
            f"exit at {t['underlying']} stop {t['u_stop']:,.2f} / target {t['u_target']:,.2f} · order {oid}")


def _place(t: dict, tsym: str, side: str, qty: int, px: float) -> str:
    res = broker.call("place_order", exchange_segment=fno.SEGMENT, product=t["product"], validity="DAY", amo="NO",
                      order_type="L", price=f"{px:.2f}", trigger_price="0", quantity=str(qty),
                      trading_symbol=tsym, transaction_type=side)
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    oid = res.get("nOrdNo") or data.get("nOrdNo")
    if not oid:
        raise broker.BrokerError(f"place_order: no order number in {str(res)[:200]}")
    return str(oid)


def _step_buying(t: dict, book: dict, enabled: bool) -> None:
    key, o = t["key"], book.get(t["buy_order"])
    os_ = order_state(o)
    if os_ == "OPEN" and (not enabled or _age(t["buy_at"]) > REPRICE_SEC):
        orders._cancel(t["buy_order"])   # not filled at that price: back to waiting, bought at a fresh price
        o = orders._book().get(t["buy_order"])
        os_ = order_state(o)
        if os_ == "OPEN":
            return                       # cancel not through yet: next loop
    qty, avg = _filled(o)
    if os_ == "FILLED" or qty:
        if os_ == "OPEN":
            return                       # partly filled and still working
        _opened(t, qty or t["qty"], avg or t["buy_price"])
        return
    if os_ == "REJECTED":
        _save(key, state=REJECTED, note=_reason(o))
        _notify(f"❌ <b>{t['tsym']}</b> buy rejected by Kotak: {_reason(o)}")
        return
    if os_ in ("CANCELLED", "GONE"):
        _save(key, state=WAIT if enabled else CANCELLED, buy_order=None, buy_price=None, buy_at=None,
              note="buy not filled; waiting to buy again at a fresh price" if enabled else "auto-trade switched off")


def _opened(t: dict, qty: int, fill: float) -> None:
    _save(t["key"], state=OPEN, qty=qty, lots=qty // (t["lot"] or 1), fill_price=fill, opened_at=_now(), note=None)
    _notify(f"✅ <b>{t['tsym']}</b> bought × {qty} @ ₹{fill:,.2f} (₹{fill * qty:,.0f} premium)")


def _exit_reason(t: dict, sig_status: str, u: float | None) -> str | None:
    m = _mkt()
    late = m.now().time() >= orders._hhmm(config.INTRADAY_SQUARE_OFF)
    if late and intraday(t):
        return "intraday square-off"
    if late and t["expiry"] == m.now().date().isoformat():
        return "expiry day"
    if u is not None and _past(t["u_stop"], u, t["side"], False):
        return f"stop-loss ({t['underlying']} {u:,.2f})"
    if u is not None and t["u_target"] is not None and _past(t["u_target"], u, t["side"], True):
        return f"target ({t['underlying']} {u:,.2f})"
    if sig_status not in ("PENDING", "ACTIVE"):
        return f"signal {sig_status.lower().replace('_', ' ')}"
    return None


def _step_open(t: dict, sig_status: str) -> None:
    if not _mkt().is_open():
        return
    why = _exit_reason(t, sig_status, _spot(t["underlying"]))
    if why:
        _sell(t, why)


def _sell_price(t: dict) -> float:
    q = fno.option_quote(t["tsym"])
    ref = q.get("bid") or q.get("ltp")
    if not ref:
        raise broker.BrokerError(f"no live price for {t['tsym']}, so the sell can't be priced")
    c = fno.contract(t["tsym"]) or {}
    return protect(float(ref), "SELL", config.OPTION_PROTECTION_PCT, float(c.get("tick") or 0.05))


def _sell(t: dict, why: str, qty: int | None = None) -> None:
    qty = qty or t["qty"]
    px = _sell_price(t)
    oid = _place(t, t["tsym"], "S", qty, px)
    _save(t["key"], state=SELLING, qty=qty, sell_order=oid, sell_at=_now(), note=why)
    _notify(f"📤 <b>{t['tsym']}</b> selling × {qty} ({why}) · limit ₹{px:,.2f}")


def _step_selling(t: dict, book: dict) -> None:
    o = book.get(t["sell_order"])
    os_ = order_state(o)
    if os_ == "OPEN":
        if not (_mkt().is_open() and _age(t["sell_at"]) > REPRICE_SEC):
            return
        orders._cancel(t["sell_order"])   # price moved away from the limit: sell again at a fresh price
        o = orders._book().get(t["sell_order"])
        os_ = order_state(o)
        if os_ == "OPEN":
            return
    if os_ == "FILLED":
        _closed(t, _filled(o)[1])
        return
    if os_ in ("REJECTED", "CANCELLED") or (os_ == "GONE" and _mkt().is_open()):
        if os_ == "REJECTED":
            _notify(f"⚠️ <b>{t['tsym']}</b> sell rejected ({_reason(o)}); trying again")
        sold = _filled(o)[0]
        if sold:   # partly filled: only the rest is sold again
            _save(t["key"], qty=t["qty"] - sold)
        _sell(_get(t["key"]), t["note"] or "exit")


def _closed(t: dict, price: float | None) -> None:
    _save(t["key"], state=CLOSED, exit_price=price, closed_at=_now())
    pnl = (price - t["fill_price"]) * t["qty"] if price and t["fill_price"] else None
    _notify(f"🏁 <b>{t['tsym']}</b> closed ({t['note'] or 'exit'}) @ ₹{price or 0:,.2f}"
            + (f" · P&L ₹{pnl:,.0f} before charges" if pnl is not None else ""))


CLOSE_NOTE = "closed by you"


def close_trade(key: str) -> dict:
    """Close an option trade by hand: a waiting one is cancelled, a buy in progress cancelled (anything bought is
    sold), a position sold at the live bid. Market hours only for sells (an option AMO would be priced blind)."""
    with _lock:
        t = _get(key)
        if t is None:
            raise ValueError("Unknown option trade")
        if t["state"] == SELLING:
            raise ValueError(f"{t['tsym']} is already being sold")
        if t["state"] not in (WAIT, BUYING, OPEN):
            raise ValueError(f"{_name(t)} is not open ({t['state'].lower()})")
        if t["state"] == BUYING:
            o = orders._book().get(t["buy_order"])
            if order_state(o) == "OPEN":
                orders._cancel(t["buy_order"])
                o = orders._book().get(t["buy_order"])
            qty, avg = _filled(o)
            if qty:
                _opened(t, qty, avg or t["buy_price"])
                t = _get(key)
        if t["state"] != OPEN:
            _save(key, state=CANCELLED, note=CLOSE_NOTE)
            _notify(f"✖️ <b>{_name(t)}</b> option trade cancelled ({CLOSE_NOTE})")
            return _get(key)
        if not _mkt().is_open():
            raise ValueError("Options are sold only while NSE is open (the price would be stale)")
        _sell(t, CLOSE_NOTE)
        return _get(key)
