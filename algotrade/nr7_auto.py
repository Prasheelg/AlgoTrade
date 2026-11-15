"""NR7 auto-trade: real Kotak orders for the NR7 index breakout (nr7.py), bought as in-the-money options.

The rules are NR7_Breakout_Playbook.html's, with the choices in config (NR7_AUTO_*):
- Indices NR7_AUTO_INDICES (Nifty, Bank Nifty); one trade per index; NR7_AUTO_LOTS lots (1) whatever it risks.
- Entry, on the setup's next session only: the first NR7 level the index trades past on Kotak's live index quote
  (its day high / low, so a spike between two looks still counts) buys an in-the-money call (buy level) or put
  (sell level): the monthly expiry at least NR7_OPTION_MIN_DAYS days away, delta nearest 0.7
  (nr7_instruments.option_plan). Both levels already traded when the bot first looks (it was off): no trade, the
  playbook counts that day as a stop-out. The index already back at the stop or at the target: no trade.
- Levels, on the index: entry = the level, or the day's open when the session opened past it; stop = the NR7
  day's other end; target = entry + TARGET_R x (entry - stop).
- Exit (the option is sold): the index at the stop or the target on the live quote; 15:15 on the 5th session
  (entry day = 1; sessions as counted by the NR7 tracker); or the tracker closing the same trade (a backup when
  the bot missed a move). Nothing rests at Kotak, so the dashboard must be running.
- Orders: limits at the ask + OPTION_PROTECTION_PCT for a buy, the bid - it for a sell (SEBI: no market orders via
  the API), product NRML; an unfilled order is re-priced after REPRICE_SEC. Before a sell, Kotak's positions are
  read so a lot already closed by hand in the Kotak app is not sold again (that would open a short option).
- Runs only with the dashboard's Auto-trade switch AND the NR7 view's Trade switch on. Switching either off
  cancels trades still waiting to buy; options already bought are still sold by the rules.
- One process runs it: the dashboard holding OWNER_KEY (renewed every loop), so two dashboards can't double up.
Every step is posted to Telegram."""
import json
import logging
import os
import threading
from datetime import date, datetime

import numpy as np

from . import broker, config, fno, kotak, markets, nr7, nr7_instruments, orders, state
from .manual import protect
from .orders import _filled, _reason, order_state

log = logging.getLogger(__name__)
REPRICE_SEC = 60            # an unfilled buy / sell is cancelled after this long and placed again at a fresh price
IDLE_SEC = 30               # loop interval outside market hours
OWNER_KEY = "nr7_auto_owner"
OWNER_STALE_SEC = 120
_lock = threading.RLock()

WAIT, BUYING, OPEN, SELLING = "WAIT", "BUYING", "OPEN", "SELLING"
CLOSED, CANCELLED, REJECTED, SKIPPED = "CLOSED", "CANCELLED", "REJECTED", "SKIPPED"
ACTIVE_STATES = (WAIT, BUYING, OPEN, SELLING)

_COLUMNS = """key TEXT PRIMARY KEY, idx TEXT, nr7_date TEXT, high REAL, low REAL, side TEXT, opt TEXT, state TEXT,
    u_entry REAL, u_stop REAL, u_target REAL, tsym TEXT, token TEXT, segment TEXT, expiry TEXT, strike REAL,
    lot INTEGER, tick REAL, qty INTEGER, buy_order TEXT, buy_price REAL, buy_at TEXT, fill_price REAL,
    sell_order TEXT, sell_at TEXT, exit_price REAL, trade_day TEXT, opened_at TEXT, closed_at TEXT, pos_seen INTEGER,
    exit_why TEXT, note TEXT, created_at TEXT, updated_at TEXT"""


# ---- storage ----------------------------------------------------------------------------------------

def _conn():
    con = state._conn()
    con.execute(f"CREATE TABLE IF NOT EXISTS nr7_trades ({_COLUMNS})")
    return con


def all_trades() -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM nr7_trades ORDER BY created_at DESC, key")]


def _get(key: str) -> dict | None:
    with _conn() as con:
        row = con.execute("SELECT * FROM nr7_trades WHERE key = ?", (key,)).fetchone()
    return dict(row) if row else None


def _save(key: str, **fields) -> None:
    fields["updated_at"] = _now()
    with _conn() as con:
        if con.execute("SELECT 1 FROM nr7_trades WHERE key = ?", (key,)).fetchone():
            con.execute(f"UPDATE nr7_trades SET {', '.join(f'{k} = ?' for k in fields)} WHERE key = ?",
                        (*fields.values(), key))
        else:
            fields["created_at"] = fields["updated_at"]
            con.execute(f"INSERT INTO nr7_trades (key, {', '.join(fields)}) VALUES (?, {', '.join('?' * len(fields))})",
                        (key, *fields.values()))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _age(ts: str | None) -> float:
    return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() if ts else 1e9


def _mkt():
    return markets.get("IN")


def _notify(text: str) -> None:
    orders._notify(f"📐 NR7 · {text}")


def _name(t: dict) -> str:
    return nr7.INDICES[t["idx"]][0]


def enabled() -> bool:
    """Real orders allowed: the dashboard's Auto-trade switch and the NR7 view's Watch and Trade switches."""
    s = nr7.settings()
    return bool(s["on"] and s["trade"] and broker.trading_enabled())


# ---- live prices ------------------------------------------------------------------------------------

def _index_quote(idx: str) -> dict | None:
    """Kotak's live index quote: ltp and the day's open / high / low. None if unavailable or not from today."""
    _, cash, name = nr7_instruments.QUOTE[idx]
    try:
        res = fno._quotes([name], cash)
        q = kotak.parse_quote(res[0]) if res else None
    except Exception as exc:
        log.warning("NR7 auto: %s quote failed (%s)", idx, exc)
        return None
    if q is None or (q["time"] is not None and q["time"].date() != _mkt().now().date()):
        return None
    return q


def _option_quote(t: dict) -> dict:
    res = fno._quotes([t["token"]], t["segment"])
    if not res:
        raise broker.BrokerError(f"no quote for {t['tsym']}")
    return fno.parse_option_quote(res[0])


def _held(t: dict) -> int | None:
    """Net quantity of the option in Kotak's positions; None if they don't list it. Raises BrokerError if they
    can't be read (the step is tried again next loop)."""
    from . import portfolio
    res = broker.call("positions")
    try:
        (config.DATA_DIR / "kotak_positions_last.json").write_text(json.dumps(res, indent=1, default=str),
                                                                   encoding="utf-8")
    except OSError:
        pass
    for row in portfolio._rows(res):
        p = portfolio.norm_position(row)
        if p["tsym"] == (t["tsym"] or "").upper():
            return int(p["net_qty"] or 0)
    return None


def _past(level: float, px: float, side: str, toward_target: bool) -> bool:
    """The index has reached a level: the target side (up for a LONG) or the stop side."""
    return (px >= level) if (side == "LONG") == toward_target else (px <= level)


# ---- the loop ---------------------------------------------------------------------------------------

def _claim() -> bool:
    """This process runs the bot unless another live one holds the lock."""
    me = str(os.getpid())
    cur = state.get_setting(OWNER_KEY, None)[0] or {}
    if cur.get("pid") not in (None, me) and _age(cur.get("at")) < OWNER_STALE_SEC:
        return False
    state.set_setting(OWNER_KEY, {"pid": me, "at": _now()})
    return True


def watch(done: threading.Event) -> None:
    """The dashboard's thread: a step every orders.WATCH_SEC in market hours, IDLE_SEC otherwise."""
    while not done.is_set():
        try:
            if _claim():
                step_all()
        except Exception:
            log.exception("NR7 auto-trade loop failed")
        done.wait(orders.WATCH_SEC if _mkt().is_open() else IDLE_SEC)


def step_all() -> bool:
    """Create trades for new setups, then move every live one on. True if there were live trades."""
    with _lock:
        on = enabled()
        setups = {s["key"]: s for s in nr7.setups()}
        if on:
            _create(setups)
        live = [t for t in all_trades() if t["state"] in ACTIVE_STATES]
        if not live:
            return False
        book = orders._book() if any(t["state"] in (BUYING, SELLING) for t in live) else {}
        for t in live:
            try:
                _step(t, book, setups.get(t["key"]), on)
            except broker.BrokerError as exc:
                log.warning("NR7 trade %s: %s", t["key"], exc)
                _save(t["key"], note=str(exc)[:300])
            except Exception:
                log.exception("NR7 trade %s crashed", t["key"])
        return True


def _create(setups: dict[str, dict]) -> None:
    today = _mkt().now().date().isoformat()
    for key, s in setups.items():
        if s["idx"] not in config.NR7_AUTO_INDICES or _get(key):
            continue
        # a setup still waiting, or one the tracker entered today before the bot first looked
        if s["status"] == nr7.WAIT or (s["status"] == nr7.OPEN and s["trigger_day"] == today):
            _save(key, idx=s["idx"], nr7_date=s["nr7_date"], high=s["high"], low=s["low"], state=WAIT)
            log.info("NR7 auto: watching %s", key)


def _step(t: dict, book: dict, setup: dict | None, on: bool) -> None:
    st = t["state"]
    if st == OPEN and t["expiry"] and date.fromisoformat(t["expiry"]) <= _mkt().now().date():
        _notify(f"⚠️ <b>{t['tsym']}</b> reaches its expiry while the bot holds it; selling it")
        _sell(t, "expiry day")
        return
    if st == WAIT:
        _step_wait(t, setup, on)
    elif st == BUYING:
        _step_buying(t, book, on)
    elif st == OPEN:
        _step_open(t, setup)
    elif st == SELLING:
        _step_selling(t, book)


# ---- entry ------------------------------------------------------------------------------------------

def _cancel(t: dict, why: str, state_: str = CANCELLED, tell: bool = False) -> None:
    _save(t["key"], state=state_, note=why)
    log.info("NR7 auto %s: %s (%s)", t["key"], state_.lower(), why)
    if tell:
        _notify(f"✖️ <b>{_name(t)}</b> ({t['nr7_date']}): no trade, {why}")


def _step_wait(t: dict, setup: dict | None, on: bool) -> None:
    m = _mkt()
    today = m.now().date().isoformat()
    if not on:
        _cancel(t, "auto-trade switched off")
        return
    if setup is None or setup["status"] in ("EXPIRED", "SKIPPED"):
        _cancel(t, f"the setup is {(setup or {}).get('status', 'gone').lower()}")
        return
    if (setup["trigger_day"] and setup["trigger_day"] < today) or (t["trade_day"] and t["trade_day"] < today):
        _cancel(t, "its entry day is over", tell=bool(t["side"]))
        return
    if not m.is_open():
        return
    q = _index_quote(t["idx"])
    if q is None:
        return
    if not t["side"]:
        up, down = q["high"] > t["high"], q["low"] < t["low"]
        if up and down:
            _cancel(t, "both levels had traded before the bot saw either (the playbook counts it as a stop-out)",
                    SKIPPED, tell=True)
            return
        if not (up or down):
            return
        side = "LONG" if up else "SHORT"
        entry = max(q["open"], t["high"]) if up else min(q["open"], t["low"])   # a gap past the level: the open
        stop = t["low"] if up else t["high"]
        target = entry + (1 if up else -1) * nr7.TARGET_R * abs(entry - stop)
        _save(t["key"], side=side, opt="CE" if up else "PE", u_entry=round(entry, 2), u_stop=stop,
              u_target=round(target, 2), trade_day=today)
        t = _get(t["key"])
        _notify(f"🔔 <b>{_name(t)} {side}</b>: {'above' if up else 'below'} {t['high'] if up else t['low']:,.2f} "
                f"(now {q['ltp']:,.2f}) · stop {stop:,.2f} · target {t['u_target']:,.2f} · buying a "
                f"{'call' if up else 'put'}")
    if _past(t["u_stop"], q["ltp"], t["side"], False):
        _cancel(t, f"{_name(t)} back at the stop ({q['ltp']:,.2f}) before the option was bought", tell=True)
        return
    if _past(t["u_target"], q["ltp"], t["side"], True):
        _cancel(t, f"{_name(t)} already at the target ({q['ltp']:,.2f}) before the option was bought", tell=True)
        return
    _buy(t, q["ltp"])


def _buy(t: dict, spot: float) -> None:
    """Pick the contract and send the buy. A missing contract or a bad quote waits for the next loop."""
    key = t["key"]
    if not t["tsym"]:
        setup = {"key": key, "idx": t["idx"], "high": t["high"], "low": t["low"], "risk": t["high"] - t["low"]}
        plan = nr7_instruments.option_plan(setup, t["opt"], spot, datetime.now(nr7_instruments.IST))
        if plan is None:
            _save(key, note=f"no {t['opt']} contract found {config.NR7_OPTION_MIN_DAYS}+ days out; retrying")
            return
        _save(key, tsym=plan["tsym"], token=plan["token"], segment=plan["segment"], expiry=plan["expiry"],
              strike=plan["strike"], lot=plan["lot"], tick=plan["tick"])
        t = _get(key)
    q = _option_quote(t)
    bid, ask = q.get("bid"), q.get("ask")
    if not bid or not ask:
        _save(key, note=f"{t['tsym']} has no two-sided quote (bid {bid}, ask {ask}); retrying")
        return
    if (ask - bid) / ((ask + bid) / 2) * 100 > config.OPTIONS_MAX_SPREAD_PCT:
        _save(key, note=f"{t['tsym']} spread too wide (₹{bid:,.2f} / ₹{ask:,.2f}); retrying")
        return
    px = protect(float(ask), "BUY", config.OPTION_PROTECTION_PCT, float(t["tick"] or 0.05))
    qty = max(1, config.NR7_AUTO_LOTS) * int(t["lot"])
    oid = _place(t, "B", qty, px)
    _save(key, state=BUYING, qty=qty, buy_order=oid, buy_price=px, buy_at=_now(), note=None)
    _notify(f"🛒 <b>BUY {t['tsym']}</b> × {qty} ({qty // int(t['lot'])} lot) · limit ₹{px:,.2f} "
            f"(≈₹{px * qty:,.0f}) · {_name(t)} {spot:,.2f} · exit at {t['u_stop']:,.2f} / {t['u_target']:,.2f} "
            f"or 15:15 on day {nr7.HOLD_DAYS} · order {oid}")


def _place(t: dict, side: str, qty: int, px: float) -> str:
    res = broker.call("place_order", exchange_segment=t["segment"], product="NRML", validity="DAY", amo="NO",
                      order_type="L", price=f"{px:.2f}", trigger_price="0", quantity=str(qty),
                      trading_symbol=t["tsym"], transaction_type=side)
    data = res.get("data") if isinstance(res.get("data"), dict) else {}
    oid = res.get("nOrdNo") or data.get("nOrdNo")
    if not oid:
        raise broker.BrokerError(f"place_order: no order number in {str(res)[:200]}")
    return str(oid)


def _step_buying(t: dict, book: dict, on: bool) -> None:
    key, o = t["key"], book.get(t["buy_order"])
    os_ = order_state(o)
    if os_ == "OPEN" and (not on or _age(t["buy_at"]) > REPRICE_SEC or not _mkt().is_open()):
        orders._cancel(t["buy_order"])   # not filled at that price: back to waiting, bought again at a fresh one
        o = orders._book().get(t["buy_order"])
        os_ = order_state(o)
        if os_ == "OPEN":
            return
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
        _save(key, state=WAIT if on else CANCELLED, buy_order=None, buy_price=None, buy_at=None,
              note="buy not filled; buying again at a fresh price" if on else "auto-trade switched off")


def _opened(t: dict, qty: int, fill: float) -> None:
    try:
        held = _held(t)
    except broker.BrokerError as exc:
        log.warning("NR7 auto: positions unavailable after the fill (%s)", exc)
        held = None
    _save(t["key"], state=OPEN, qty=qty, fill_price=fill, opened_at=_now(), pos_seen=int(bool(held)), note=None)
    _notify(f"✅ <b>{t['tsym']}</b> bought × {qty} @ ₹{fill:,.2f} (₹{fill * qty:,.0f} premium) · {_name(t)} "
            f"{t['side']} from {t['u_entry']:,.2f}, stop {t['u_stop']:,.2f}, target {t['u_target']:,.2f}"
            + ("" if held else " · ⚠️ not yet seen in Kotak positions"))


# ---- exit -------------------------------------------------------------------------------------------

def _day_no(t: dict, setup: dict | None, today: date) -> int:
    """Session of the trade (entry day = 1): the tracker's count for the same trade, else weekdays."""
    if setup and setup.get("side") == t["side"] and setup.get("day_no") and (setup.get("entry_at") or "")[:10] == t["trade_day"]:
        return int(setup["day_no"])
    return int(np.busday_count(date.fromisoformat(t["trade_day"]), today)) + 1


def exit_reason(t: dict, setup: dict | None, px: float | None, now: datetime) -> str | None:
    if px is not None and _past(t["u_stop"], px, t["side"], False):
        return f"stop-loss ({_name(t)} {px:,.2f})"
    if px is not None and _past(t["u_target"], px, t["side"], True):
        return f"target ({_name(t)} {px:,.2f})"
    day = _day_no(t, setup, now.date())
    if day > nr7.HOLD_DAYS or (day == nr7.HOLD_DAYS and now.time() >= nr7.EXIT_AT):
        return f"day {nr7.HOLD_DAYS} exit"
    if setup and setup["status"] in nr7.CLOSED and setup.get("side") == t["side"] \
            and (setup.get("entry_at") or "")[:10] == t["trade_day"]:
        return f"the NR7 tracker closed it ({setup['status'].lower()})"
    return None


def _step_open(t: dict, setup: dict | None) -> None:
    m = _mkt()
    if not m.is_open():
        return
    q = _index_quote(t["idx"])
    why = exit_reason(t, setup, q["ltp"] if q else None, m.now())
    if why:
        _sell(t, why)


def _sell(t: dict, why: str) -> None:
    held = _held(t)
    if (held is not None and held <= 0) or (held is None and t["pos_seen"]):
        _save(t["key"], state=CLOSED, closed_at=_now(), exit_why=why,
              note="no open position at Kotak (closed outside the app?); nothing sold")
        _notify(f"⚠️ <b>{t['tsym']}</b>: time to exit ({why}) but Kotak shows no open position, so nothing was "
                "sold. Check the Kotak app.")
        return
    qty = min(held, t["qty"]) if held else t["qty"]
    q = _option_quote(t)
    ref = q.get("bid") or q.get("ltp")
    if not ref:
        raise broker.BrokerError(f"no live price for {t['tsym']}, so the sell can't be priced")
    px = protect(float(ref), "SELL", config.OPTION_PROTECTION_PCT, float(t["tick"] or 0.05))
    oid = _place(t, "S", qty, px)
    _save(t["key"], state=SELLING, qty=qty, sell_order=oid, sell_at=_now(), exit_why=why, note=None)
    _notify(f"📤 <b>{t['tsym']}</b> selling × {qty} ({why}) · limit ₹{px:,.2f}")


def _step_selling(t: dict, book: dict) -> None:
    o = book.get(t["sell_order"])
    os_ = order_state(o)
    if os_ == "OPEN":
        if not (_mkt().is_open() and _age(t["sell_at"]) > REPRICE_SEC):
            return
        orders._cancel(t["sell_order"])   # the price moved away: sell again at a fresh one
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
        if sold:
            _save(t["key"], qty=t["qty"] - sold)
        _sell(_get(t["key"]), t["exit_why"] or "exit")


def _closed(t: dict, price: float | None) -> None:
    _save(t["key"], state=CLOSED, exit_price=price, closed_at=_now())
    pnl = (price - t["fill_price"]) * t["qty"] if price and t["fill_price"] else None
    _notify(f"🏁 <b>{t['tsym']}</b> sold ({t['exit_why'] or 'exit'}) @ ₹{price or 0:,.2f}"
            + (f" · P&L ₹{pnl:,.0f} before charges" if pnl is not None else ""))


CLOSE_NOTE = "closed by you"


def close_trade(key: str) -> dict:
    """From the dashboard: a waiting trade is cancelled, a buy in progress cancelled (anything bought is sold), an
    open one sold now. Sells only while NSE is open."""
    with _lock:
        t = _get(key)
        if t is None:
            raise ValueError("Unknown NR7 trade")
        if t["state"] == SELLING:
            raise ValueError(f"{t['tsym']} is already being sold")
        if t["state"] not in (WAIT, BUYING, OPEN):
            raise ValueError(f"This NR7 trade is not open ({t['state'].lower()})")
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
            _notify(f"✖️ <b>{_name(t)}</b> NR7 trade cancelled ({CLOSE_NOTE})")
            return _get(key)
        if not _mkt().is_open():
            raise ValueError("Options are sold only while NSE is open (the price would be stale)")
        _sell(t, CLOSE_NOTE)
        return _get(key)


def view() -> dict:
    s = nr7.settings()
    return {"trade": s["trade"], "auto_trade": broker.trading_enabled(), "active": enabled(),
            "credentials": not broker.missing_credentials(), "indices": config.NR7_AUTO_INDICES,
            "lots": config.NR7_AUTO_LOTS, "min_days": config.NR7_OPTION_MIN_DAYS, "trades": all_trades()[:50]}
