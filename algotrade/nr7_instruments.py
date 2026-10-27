"""What to trade for an NR7 setup (nr7.py), from Kotak's contract lists and quotes. Read-only: nothing is ordered.

- Futures (Nifty, Bank Nifty): the current month, or the next once fewer than ROLL_DAYS trading days are left. The
  setup's levels are on the spot index, so they are shown shifted by the future's premium (basis) too. Sensex
  futures barely trade, so Sensex gets options only.
- Options: the monthly expiry (same roll), a call for the buy level and a put for the sell level, in the money at
  that level, with the delta there closest to DELTA_AIM (within DELTA_RANGE when one is). Lots are sized so the
  premium paid is at most the risk per trade (a bought option can lose no more than that)."""
import logging
import math
from datetime import date, datetime
from time import monotonic
from zoneinfo import ZoneInfo

import numpy as np

from . import config, fno, kotak, strategy

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
# index -> (F&O segment, cash segment, the index's quote name there)
QUOTE = {"NIFTY": ("nse_fo", "nse_cm", "Nifty 50"), "BANKNIFTY": ("nse_fo", "nse_cm", "Nifty Bank"),
         "SENSEX": ("bse_fo", "bse_cm", "SENSEX")}
FUTURES = {"NIFTY", "BANKNIFTY"}
ROLL_DAYS = 5
DELTA_RANGE = (0.6, 0.8)
DELTA_AIM = 0.7
ITM_STRIKES = 20     # strikes in the money quoted to find the delta (one quote call holds 49)
CACHE_SEC = 60
_cache: dict[str, tuple[float, dict]] = {}


def budget() -> float:
    """Rupees at risk per trade: RISK_PER_TRADE_PCT of CAPITAL."""
    return config.CAPITAL * config.RISK_PER_TRADE_PCT / 100


def trading_days_left(expiry: date, today: date) -> int:
    """Weekdays from today up to, not including, the expiry day."""
    return int(np.busday_count(today, expiry))


def pick_expiry(expiries, today: date, monthly: bool = True) -> date | None:
    """The nearest expiry (the last one of its month when monthly) at least ROLL_DAYS trading days away."""
    exps = sorted(set(expiries))
    if monthly:
        last = {}
        for e in exps:
            last[(e.year, e.month)] = e
        exps = sorted(last.values())
    return next((e for e in exps if trading_days_left(e, today) >= ROLL_DAYS), None)


def _price(q: dict) -> float | None:
    """Mid of the best bid and ask, else the last price, else the previous close."""
    f = fno._f
    depth = q.get("depth") or {}
    bid, ask = f((depth.get("buy") or [{}])[0].get("price")), f((depth.get("sell") or [{}])[0].get("price"))
    if bid and ask:
        return round((bid + ask) / 2, 2)
    return f(q.get("ltp")) or f((q.get("ohlc") or {}).get("close")) or None


def _quotes(tokens: list[str], segment: str) -> dict[str, dict]:
    return {str(q.get("exchange_token")): q for q in fno._quotes(tokens, segment)}


def spot(idx: str) -> float | None:
    _, cash, name = QUOTE[idx]
    q = _quotes([name], cash).get(name) or {}
    return fno._f(q.get("ltp")) or None


def futures_plan(t: dict, spot_px: float | None, today: date) -> dict | None:
    if t["idx"] not in FUTURES:
        return None
    df = fno.index_contracts(t["idx"], QUOTE[t["idx"]][0])
    futs = df[df["opt"] == "FUT"]
    exp = pick_expiry(futs["expiry"], today, monthly=False)
    if exp is None:
        return None
    c = futs[futs["expiry"] == exp].iloc[0]
    px = _price(_quotes([c["token"]], QUOTE[t["idx"]][0]).get(c["token"]) or {})
    basis = round(px - spot_px, 2) if px and spot_px else None
    lot = int(c["lot"])
    return {"tsym": c["tsym"], "expiry": str(exp), "lot": lot, "price": px, "basis": basis,
            "buy_above": round(t["high"] + basis, 2) if basis is not None else None,
            "sell_below": round(t["low"] + basis, 2) if basis is not None else None,
            "risk_per_lot": round(t["risk"] * lot), "lots": math.floor(budget() / (t["risk"] * lot)) if t["risk"] else 0}


def option_plan(t: dict, opt: str, spot_px: float | None, now: datetime) -> dict | None:
    """The in-the-money monthly call (opt "CE", for the buy level) or put ("PE", the sell level)."""
    level = t["high"] if opt == "CE" else t["low"]
    seg = QUOTE[t["idx"]][0]
    df = fno.index_contracts(t["idx"], seg)
    df = df[df["opt"] == opt]
    exp = pick_expiry(df["expiry"], now.date())
    if exp is None:
        return None
    rows = df[df["expiry"] == exp]
    strikes = sorted(rows["strike"].unique())
    itm = [k for k in reversed(strikes) if k <= level] if opt == "CE" else [k for k in strikes if k >= level]
    rows = rows[rows["strike"].isin(itm[:ITM_STRIKES])]
    if rows.empty:
        return None
    quotes = _quotes(list(rows["token"]), seg)
    years = fno.days_to_expiry(exp, now) / 365
    best = None
    for _, c in rows.iterrows():
        px = _price(quotes.get(c["token"]) or {})
        if not px:
            continue
        iv = strategy.implied_vol(opt, px, spot_px or level, float(c["strike"]), years) if years > 0 else None
        delta = abs(strategy.greeks(opt, level, float(c["strike"]), years, iv or 0)["delta"])
        lo, hi = DELTA_RANGE
        rank = (not lo <= delta <= hi, abs(delta - DELTA_AIM))   # inside the range first, then nearest the aim
        if best is None or rank < best[0]:
            best = (rank, c, px, iv, delta)
    if best is None:
        return None
    _, c, px, iv, delta = best
    lot = int(c["lot"])
    return {"tsym": c["tsym"], "strike": float(c["strike"]), "expiry": str(exp), "premium": px, "lot": lot,
            "delta": round(delta, 2), "iv": round(iv * 100, 1) if iv else None, "cost_per_lot": round(px * lot),
            "lots": math.floor(budget() / (px * lot)),
            "loss_at_stop_per_lot": round(delta * t["risk"] * lot)}   # the option's rough move back to the stop


def plans(t: dict, now: datetime | None = None) -> dict:
    """{futures, call, put, spot} for a WAIT setup; an OPEN trade gets only its side's option. Cached CACHE_SEC.
    {"error": ...} when Kotak is off or a lookup fails."""
    if not kotak.enabled():
        return {"error": "Kotak isn't set up, so no contracts or premiums"}
    now = now or datetime.now(IST)
    hit = _cache.get(t["key"])
    if hit and monotonic() - hit[0] < CACHE_SEC and hit[1].get("status") == t["status"]:
        return hit[1]
    try:
        sp = spot(t["idx"])
        out = {"status": t["status"], "spot": sp, "futures": futures_plan(t, sp, now.date())}
        if t["status"] != "OPEN" or t["side"] == "LONG":
            out["call"] = option_plan(t, "CE", sp, now)
        if t["status"] != "OPEN" or t["side"] == "SHORT":
            out["put"] = option_plan(t, "PE", sp, now)
    except Exception as exc:
        log.warning("NR7 plans for %s failed (%s)", t["key"], exc)
        return {"error": f"Contracts or quotes unavailable: {exc}"}
    _cache[t["key"]] = (monotonic(), out)
    return out


def text(p: dict, side: str | None = None) -> str:
    """Telegram lines for a setup's plans (side LONG / SHORT: only that one)."""
    if not p or p.get("error"):
        return ""
    lines = []
    f = p.get("futures")
    if f:
        if f.get("basis") is None:
            lv = "levels as on the index"
        else:
            lv = " · ".join(([f"buy above {f['buy_above']:,.2f}"] if side != "SHORT" else [])
                            + ([f"sell below {f['sell_below']:,.2f}"] if side != "LONG" else []))
        lines.append(f"Futures {f['tsym']} ({lv}) · {f['lots']} lot{'' if f['lots'] == 1 else 's'} · "
                     f"₹{f['risk_per_lot']:,} risk per lot")
    for key, label in (("call", "Call"), ("put", "Put")):
        o = p.get(key)
        if o:
            lines.append(f"{label} {o['tsym']} ₹{o['premium']:,.2f} · Δ{o['delta']:.2f} · {o['lots']} "
                         f"lot{'' if o['lots'] == 1 else 's'} (₹{o['cost_per_lot']:,} per lot)")
    return "\n".join(lines)
