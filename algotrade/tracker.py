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
            state.update_signal(sig["key"], **fields)
    log.info("Tracked %d open signals, %d status changes", len(open_sigs), changes)
    return changes
