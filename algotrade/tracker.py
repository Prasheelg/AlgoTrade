"""Tracks open signals against market data: fills pending entries, updates LTP and marks SL / target hits.

Each signal is replayed on bars of its own timeframe after the signal bar:
- Pending entry (limit / stop order) not reached within ENTRY_VALID_BARS bars -> CANCELLED (closed, no trade)
    intraday signals are also cancelled at the end of the signal's trading day
- Entry reached                           -> ACTIVE (filled at the entry, or at the open if price gapped
                                             through it); on the fill bar only the SL is checked
- SL touched first                        -> SL_HIT (closed)
- Target 1 touched                        -> T1_HIT (closed at target 1, as Auto-trade sells the whole position
                                             there and the backtest scores it; target 2 isn't traded)
  (T2_HIT and BREAKEVEN are from before, when the stop moved to entry at T1 and the trade ran on to T2)
- Time exit                               -> EXPIRED (closed at market)
    daily signals: MAX_HOLD_BARS sessions after the fill
    intraday signals: squared off at the last bar of the signal's trading day
Signals from before pending entries (entry_type "market") start ACTIVE at the signal-bar close.
If SL and a target are touched in the same bar, SL is assumed (conservative)."""
import logging

import pandas as pd

from . import config, kotak, state, telegram_bot
from .data import fetch_ohlcv

log = logging.getLogger(__name__)
MAX_HOLD_BARS = 20
# History needed to cover a signal's whole life on each timeframe
TRACK_PERIOD = {"1d": "3mo", "1h": "1mo", "30m": "1mo", "15m": "5d", "5m": "5d"}

def fill_price(side: str, entry_type: str, entry: float, bar) -> float | None:
    """Price a pending limit / stop order fills at on this bar, or None if it isn't reached.
    If the bar opens beyond the entry (a gap), the order fills at the open."""
    sign = 1 if side == "BUY" else -1
    o, hi, lo = float(bar["Open"]), float(bar["High"]), float(bar["Low"])
    if entry_type == "limit":  # BUY below the market: fills when price comes down to it
        reached = lo <= entry if sign > 0 else hi >= entry
        gapped = sign * (entry - o) > 0
    else:                      # BUY stop above the market: fills when price breaks up through it
        reached = hi >= entry if sign > 0 else lo <= entry
        gapped = sign * (o - entry) > 0
    if not reached:
        return None
    return o if gapped else entry


def _walk(sig: dict, bars: pd.DataFrame, intraday: bool = False) -> dict:
    """Replay bars after the signal; return status, exit_price, closed_at, fill_price, filled_at."""
    sign = 1 if sig["side"] == "BUY" else -1
    entry_type = sig.get("entry_type") or "market"
    pending = entry_type != "market"
    out = {"status": "PENDING" if pending else "ACTIVE", "exit_price": None, "closed_at": None,
           "fill_price": None if pending else sig["entry"], "filled_at": None}
    stop = sig["stop_loss"]
    entry_day = pd.Timestamp(sig["bar_time"] or sig["bar_date"]).date()
    prev, waited, held = None, 0, 0

    def close(status, px, ts):
        return {**out, "status": status, "exit_price": px, "closed_at": str(ts.date())}

    for ts, b in bars.iterrows():
        if intraday and ts.date() > entry_day:
            last = prev[0] if prev is not None else ts
            if out["status"] == "PENDING":
                return close("CANCELLED", None, last)
            # Intraday position is squared off at the close of the signal day
            px = float(prev[1]["Close"]) if prev is not None else float(b["Open"])
            return close("EXPIRED", px, last)
        hi, lo = b["High"], b["Low"]
        hit = lambda level, against: (lo <= level if sign > 0 else hi >= level) if against \
            else (hi >= level if sign > 0 else lo <= level)

        if out["status"] == "PENDING":
            waited += 1
            px = fill_price(sig["side"], entry_type, sig["entry"], b)
            if px is None:
                if waited >= config.ENTRY_VALID_BARS:
                    return close("CANCELLED", None, ts)
                prev = (ts, b)
                continue
            out.update(status="ACTIVE", fill_price=round(px, 2), filled_at=ts.isoformat())
            # Intrabar order is unknown on the fill bar, so only the stop is checked (conservative)
            if hit(stop, True):
                return close("SL_HIT", stop if sign * (px - stop) > 0 else px, ts)
            prev = (ts, b)
            continue

        held += 1
        if hit(stop, True):
            return close("SL_HIT", stop, ts)
        if hit(sig["target1"], False):
            return close("T1_HIT", sig["target1"], ts)
        if not intraday and held >= MAX_HOLD_BARS:
            return close("EXPIRED", float(b["Close"]), ts)
        prev = (ts, b)
    return out


def _bars_after(sig: dict, df: pd.DataFrame) -> pd.DataFrame:
    if sig["timeframe"] == "1d":
        return df[df.index.date > pd.Timestamp(sig["bar_date"]).date()]
    return df[df.index > pd.Timestamp(sig["bar_time"])]


# Lifecycle order of open statuses; a refresh never moves a signal back (e.g. a fill seen on a live
# quote stays filled while the delayed Yahoo bars catch up)
OPEN_RANK = {"PENDING": 0, "ACTIVE": 1}


def _regressed(old: str, new: str) -> bool:
    return old in OPEN_RANK and new in OPEN_RANK and OPEN_RANK[new] < OPEN_RANK[old]


def _traded(key: str) -> bool:
    """Auto-trade took this signal (shares or options): its own fill / close notices cover it, so the signal's
    status changes don't ring a second time."""
    try:
        with state._conn() as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "live_trades" in tables and con.execute(
                    "SELECT 1 FROM live_trades WHERE key = ? AND entry_order IS NOT NULL", (key,)).fetchone():
                return True
            return "option_trades" in tables and bool(con.execute(
                "SELECT 1 FROM option_trades WHERE key = ? AND state NOT IN ('SKIPPED', 'CANCELLED', 'REJECTED')",
                (key,)).fetchone())
    except Exception:
        return False


def _activity(sig: dict, res: dict, ltp: float) -> None:
    """A signal's status change in the activity feed (the phone's sound)."""
    from . import activity
    kind, title = activity.SIGNAL_STATUS.get(res["status"], ("info", f"Signal {res['status'].lower()}"))
    if _traded(sig["key"]):
        return
    exit_px, entry = res.get("exit_price"), res.get("fill_price") or sig.get("fill_price") or sig["entry"]
    if kind in ("loss", "closed") and exit_px and entry:   # a stop above entry (or a time exit) can still be a gain
        move = (exit_px - entry) if sig["side"] == "BUY" else (entry - exit_px)
        kind = activity.kind_of_pnl(move, flat=entry * 0.0005)
    cur = "$" if sig.get("market") == "US" else "₹"
    body = f"{sig['side']} {sig['symbol']} · {sig.get('timeframe') or '1d'}" + (
        f" · exit {cur}{exit_px:,.2f}" if exit_px else f" · now {cur}{ltp:,.2f}")
    activity.record(kind, f"{title} · {sig['symbol']}", body, f"/#{sig.get('market') or 'IN'}/{'active' if kind == 'entry' else 'closed'}")


def update_all(notify: bool = True, market: str | None = None) -> int:
    """Refresh open signals (all markets, or one). Returns number of status changes."""
    open_sigs = [s for s in state.open_signals() if market is None or s["market"] == market]
    if not open_sigs:
        return 0
    changes = 0
    # Real-time quotes for Indian stocks when Kotak is configured (one call batch for all timeframes)
    live = {}
    if kotak.enabled() and any(s["market"] == "IN" for s in open_sigs):
        live = kotak.quotes(sorted({s["symbol"] for s in open_sigs if s["market"] == "IN"}))
    for mk, tf in sorted({(s["market"], s["timeframe"]) for s in open_sigs}):
        group = [s for s in open_sigs if s["market"] == mk and s["timeframe"] == tf]
        data = fetch_ohlcv(sorted({s["symbol"] for s in group}), tf, period=TRACK_PERIOD.get(tf, "1mo"),
                           market=mk)
        for sig in group:
            df = data.get(sig["symbol"])
            if df is None or df.empty:
                continue
            q = live.get(sig["symbol"]) if mk == "IN" else None
            if q:
                df = kotak.apply_live_bar(df, q, tf)
            ltp = round(float(df["Close"].iloc[-1]), 2)
            res = _walk(sig, _bars_after(sig, df), intraday=tf != "1d")
            fields = {"ltp": ltp}
            # (a T1_HIT left open from before, when T1 moved the stop, closes now at target 1)
            moved = res["status"] != sig["status"] or (sig["exit_price"] is None and res["exit_price"] is not None)
            if moved and not _regressed(sig["status"], res["status"]):
                fields.update(res)
                changes += 1
                if notify and sig["telegram"]:  # only signals that were posted to Telegram
                    telegram_bot.send_message(telegram_bot.format_update(sig, res, ltp))
                if notify:
                    _activity(sig, res, ltp)
            state.update_signal(sig["key"], **fields)
    log.info("Tracked %d open signals, %d status changes", len(open_sigs), changes)
    return changes
