"""0DTE short iron condor on NIFTY, BANKNIFTY and SENSEX, run by the Nifty rulebook (README > 0DTE iron condor).

Each index is its own book (mode, Auto-trade switch, rules, position, log), traded on its own expiry days only
(NIFTY weekly, SENSEX weekly on BSE, BANKNIFTY monthly). The numbers below are NIFTY's; BANKNIFTY and SENSEX
scale the strike step, wing, roll buffers and loss cap to their price (SPECS); India VIX filters all three.
One lot: buy two wings 50 points outside two short strikes, sell the shorts,
manage the position all day, square off from 15:00. The rules (locked unless marked "check", which is logged
but never blocks):
- Day filters: expiry day, not an event day (skip list), VIX up no more than 5% on the day, India VIX's IV
  percentile (252 days) between 30 and 85; checks: VIX 11-20, VIX above HV10 (Nifty's 10-day realised vol).
- Entry 09:45-10:15. 1SD to expiry two ways: A = spot x IV_avg x sqrt(minutes left / 375 / 252) with IV_avg the
  mean of the ATM call and put IV, and B = ATM straddle x 1.25; A if within 10% of B, else B. The IVs here are
  solved on that same trading-time clock (not calendar days), so A and B normally agree.
- Short call = max(0.16-delta call, spot + 1SD, call OI wall) rounded UP to 50; short put = min(-0.16-delta put,
  spot - 1SD, put OI wall) rounded DOWN to 50; wings +/-50. Walls: most call OI above spot / put OI below spot
  within +/-2SD; no entry if a wall's OI is falling since the open (unwinding). POP > 75% (normal model on the
  same 1SD, with the credit at the prices we would trade at: sell at the bid, buy at the ask).
- Every minute the targets are worked out again. A side rolls out when its 1SD level is 25+ points past the
  short strike (or its OI wall has moved past it), and in when the short strike is 75+ points beyond the level
  and the roll brings in a net credit; at least 2 minutes between rolls, none after 14:30 (a threatened side
  is closed instead). A roll that can't meet POP > 75% closes that side.
- Daily loss cap: cash from every fill + open legs at mid - charges <= -CONDOR_LOSS_CAP closes everything and
  stops for the day (checked every few seconds). From 15:00 everything is squared off: shorts first, then wings.
Orders: bought legs always before sold ones (wings first in, shorts first out), marketable limits off the best
bid / ask, re-priced up to 4 times. Modes: "paper" (default: fills at the bid / ask, no orders), "live" (real
MIS orders; also needs the condor's own Auto-trade switch ON to open or roll, never to close; the dashboard's
global Auto-trade switch doesn't affect it), "off" (nothing new; an open position is still managed to its exit). Every decision goes to the condor_log table and Telegram."""
import json
import logging
import math
import threading
import time as _time
from datetime import date, datetime, time, timedelta

from . import broker, config, fno, markets, state, strategy, telegram_bot

log = logging.getLogger(__name__)

UNDERLYING = "NIFTY"         # the default book (and the rulebook's numbers below)
STEP = 50                    # strike rounding
WING = 50                    # long strike = short strike +/- WING, never changed
LOTS = 1
ENTRY_FROM, ENTRY_TO = time(9, 45), time(10, 15)
ROLL_UNTIL = time(14, 30)
EXIT_FROM, EXIT_BY = time(15, 0), time(15, 15)
SESSION_END = time(15, 30)
SESSION_MIN, YEAR_DAYS = 375, 252
DELTA = 0.16
POP_MIN = 0.75
OUT_BUFFER, IN_BUFFER = 25, 75
CHAIN_WIDTH = 25             # strikes either side of the money read for walls and deltas
# Per index: strike step, wing, roll buffers (scaled to its price, about 2.3x Nifty for Bank Nifty and 3.2x for
# Sensex), Yahoo symbol of its daily closes (HV10), the config name of its daily loss cap and how many strikes
# either side of the money are read (CHAIN_WIDTH for NIFTY; wider where strikes are farther apart in % terms).
SPECS = {
    "NIFTY": {"name": "Nifty", "step": 50, "wing": 50, "out": 25, "in": 75, "yahoo": "^NSEI",
              "cap": "CONDOR_LOSS_CAP", "exchange": "NSE", "expiry": "weekly", "width": CHAIN_WIDTH},
    "BANKNIFTY": {"name": "Bank Nifty", "step": 100, "wing": 100, "out": 50, "in": 150, "yahoo": "^NSEBANK",
                  "cap": "CONDOR_LOSS_CAP_BANKNIFTY", "exchange": "NSE", "expiry": "monthly", "width": 35},
    "SENSEX": {"name": "Sensex", "step": 100, "wing": 200, "out": 75, "in": 250, "yahoo": "^BSESN",
               "cap": "CONDOR_LOSS_CAP_SENSEX", "exchange": "BSE", "expiry": "weekly", "width": 40},
}
UNDERLYINGS = tuple(SPECS)
COOLDOWN_MIN = 2
VIX_BAND = (11.0, 20.0)      # check
VIX_RISE_MAX = 5.0           # % vs previous close, hard
IVP_BAND = (30.0, 85.0)      # hard
AB_TOLERANCE = 0.10
STRADDLE_FACTOR = 1.25       # SD = straddle / sqrt(2/pi)
UNWIND_PCT = 2.0             # a wall's OI this far below its level at the open counts as unwinding
TICK_SEC = 3
RETARGET_SEC = 60
ENTRY_RETRY_SEC = 30
FILL_WAIT_SEC = 10
FILL_TRIES = 4
PRODUCT = "MIS"
MODES = ("off", "paper", "live")
MODE_KEY = "condor_mode"
TRADING_KEY = "condor_trading"   # the condor's own Auto-trade switch (not broker.TRADING_KEY)

# Rules that can be switched off on the dashboard (all on by default): key -> (group, name, what it does)
RULES = {
    "event": ("Entry filters", "Event days", "No trade on a date in the event-day list"),
    "vix_rise": ("Entry filters", "VIX rise ≤ 5%", "Skip the day if India VIX is up more than 5% on the day"),
    "ivp": ("Entry filters", "IV percentile 30–85", "Trade only when India VIX's 252-day percentile is 30 to 85"),
    "oi_unwind": ("Entry filters", "OI walls not unwinding", "No entry if a wall's OI fell 2%+ since the open"),
    "pop": ("Entry filters", "POP > 75%", "Entry and every roll need a probability of profit above 75%"),
    "vix_band": ("Entry filters", "VIX 11–20 (check)", "Logged and alerted only; never blocks"),
    "hv10": ("Entry filters", "VIX above HV10 (check)", "Logged only; never blocks"),
    "cand_delta": ("Strike candidates", "0.16 delta", "The call / put nearest 0.16 delta"),
    "cand_sd": ("Strike candidates", "1SD level", "Spot ± 1SD"),
    "cand_wall": ("Strike candidates", "OI wall", "Most call OI above / put OI below spot within ±2SD"),
    "roll_out": ("Management", "Roll out", "Roll a side out when its 1SD level is 25+ pts past the short"),
    "roll_in": ("Management", "Roll in", "Roll a side in when its short is 75+ pts beyond its 1SD level, for a net credit"),
    "roll_wall": ("Management", "Roll when a wall moves", "Roll a side out when its OI wall moves past the short"),
    "late_exit": ("Management", "Close a threatened side after 14:30", "No rolls after 14:30: a threatened side is closed instead"),
    "loss_cap": ("Management", "Daily loss cap", "Close everything when the day's P&L reaches −CONDOR_LOSS_CAP"),
}
CANDIDATES = ("cand_delta", "cand_sd", "cand_wall")
LOCKED = ("Expiry day only (0DTE)", "1 lot, fixed wings", "Entry 09:45–10:15", "1SD: A vs B cross-check",
          "Strikes in the chain with two-sided quotes", "Wings bought first, shorts closed first",
          "Square-off from 15:00, all closed by 15:15")
RULES_KEY = "condor_rules"

WAITING, OPEN, SKIPPED, CLOSED, STOPPED, FAILED = "WAITING", "OPEN", "SKIPPED", "CLOSED", "STOPPED", "FAILED"
FINAL = (SKIPPED, CLOSED, STOPPED, FAILED)

_lock = threading.RLock()
_mem: dict = {"thread": None, "books": {}}


def _book(u: str) -> dict:
    """The engine's memory of one index's book (last plan, marks, timers)."""
    return _mem["books"].setdefault(u, {"plan": None, "pnl": None, "marks": {}, "quotes": {}, "pnl_at": None,
                                        "retarget_at": 0.0, "entry_try": 0.0, "exit_warned": None})


def _und(u: str | None) -> str:
    u = str(u or UNDERLYING).upper()
    if u not in SPECS:
        raise ValueError(f"Underlying must be one of {', '.join(UNDERLYINGS)}")
    return u


def loss_cap(u: str = UNDERLYING) -> float:
    """The daily loss cap of an index's book (₹): CONDOR_LOSS_CAP for NIFTY, CONDOR_LOSS_CAP_<INDEX> for the others."""
    return float(getattr(config, SPECS[u]["cap"]))


def _key(base: str, u: str) -> str:
    """Settings key of one book: NIFTY keeps the original names."""
    return base if u == UNDERLYING else f"{base}_{u}"


class ExecError(RuntimeError):
    """An order could not be filled."""


# ---- maths (pure) -----------------------------------------------------------------------------------------

def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def minutes_left(now: datetime) -> float:
    """Minutes until 15:30 today (0 after the close, at most a full session)."""
    end = datetime.combine(now.date(), SESSION_END, tzinfo=now.tzinfo)
    return min(max((end - now).total_seconds() / 60, 0.0), float(SESSION_MIN))


def trading_minutes_to(expiry: date, now: datetime) -> float:
    """Trading minutes from now until 15:30 on expiry day (NSE trading days only)."""
    today = now.date()
    if expiry < today:
        return 0.0
    if expiry == today:
        return minutes_left(now)
    left = minutes_left(now) if markets.get("IN").trading_day(today) else 0.0
    d = today + timedelta(days=1)
    while d <= expiry:
        if markets.get("IN").trading_day(d):
            left += SESSION_MIN
        d += timedelta(days=1)
    return left


def years(minutes: float) -> float:
    """Time on the rulebook's clock: (minutes / 375) trading days over 252."""
    return max(minutes, 1.0) / SESSION_MIN / YEAR_DAYS


def one_sd(spot: float, iv_avg: float | None, minutes: float, atm_ce: float | None, atm_pe: float | None) -> dict:
    """1SD to expiry: A (formula) vs B (straddle x 1.25); A when within 10% of B, else B."""
    a = spot * iv_avg * math.sqrt(years(minutes)) if spot and iv_avg else None
    b = (atm_ce + atm_pe) * STRADDLE_FACTOR if atm_ce and atm_pe else None
    if a and b:
        used = "A" if abs(a - b) <= AB_TOLERANCE * b else "B"
    else:
        used = "B" if b else ("A" if a else None)
    sd = a if used == "A" else b if used == "B" else None
    return {"a": a, "b": b, "used": used, "sd": sd,
            "gap_pct": abs(a - b) / b * 100 if a and b else None}


def ivp(vix_now: float, closes: list[float]) -> float | None:
    """Share of the last 252 daily VIX closes below today's VIX, in %. None with under 200 closes."""
    last = [c for c in closes if c is not None][-YEAR_DAYS:]
    if vix_now is None or len(last) < 200:
        return None
    return sum(1 for c in last if c < vix_now) / len(last) * 100


def hv10(closes: list[float]) -> float | None:
    """Nifty's realised volatility: stdev of the last 10 daily log returns x sqrt(252) x 100."""
    c = [x for x in closes if x][-11:]
    if len(c) < 11:
        return None
    r = [math.log(c[i] / c[i - 1]) for i in range(1, len(c))]
    m = sum(r) / len(r)
    var = sum((x - m) ** 2 for x in r) / (len(r) - 1)
    return math.sqrt(var) * math.sqrt(YEAR_DAYS) * 100


def delta(opt: str, s: float, k: float, t: float, iv: float | None) -> float | None:
    """Black-Scholes delta on the rulebook's clock (no rate: minutes to expiry)."""
    if not iv or iv <= 0 or t <= 0:
        return None
    d1 = (math.log(s / k) + iv * iv / 2 * t) / (iv * math.sqrt(t))
    return ncdf(d1) if opt == "CE" else ncdf(d1) - 1


def round_up(x: float, step: float = STEP) -> float:
    return math.ceil(round(x / step, 9)) * step


def round_down(x: float, step: float = STEP) -> float:
    return math.floor(round(x / step, 9)) * step


def pick_strikes(spot: float, sd: float, d_call: float | None, d_put: float | None,
                 call_wall: float | None, put_wall: float | None, use_sd: bool = True, step: float = STEP,
                 wing: float = WING) -> dict:
    """The farthest of the candidates per side, rounded outward, and which candidate won. A candidate passed as
    None (or the 1SD level with use_sd=False) is left out; with none left the 1SD level is used."""
    calls = {"delta": d_call, "1SD": spot + sd if use_sd else None, "OI wall": call_wall}
    puts = {"delta": d_put, "1SD": spot - sd if use_sd else None, "OI wall": put_wall}
    calls = {k: v for k, v in calls.items() if v is not None} or {"1SD": spot + sd}
    puts = {k: v for k, v in puts.items() if v is not None} or {"1SD": spot - sd}
    c_by = max(calls, key=lambda k: calls[k])
    p_by = min(puts, key=lambda k: puts[k])
    sc, sp = round_up(calls[c_by], step), round_down(puts[p_by], step)
    return {"short_call": sc, "long_call": sc + wing, "short_put": sp, "long_put": sp - wing,
            "call_by": c_by, "put_by": p_by, "call_level": spot + sd, "put_level": spot - sd}


def pop(short_call: float | None, short_put: float | None, credit: float, spot: float, sd: float) -> float:
    """Probability of profit at expiry: N(z_up) + N(z_dn) - 1, a missing side counting as certain."""
    up = ncdf((short_call + credit - spot) / sd) if short_call is not None else 1.0
    dn = ncdf((spot - (short_put - credit)) / sd) if short_put is not None else 1.0
    return up + dn - 1


def roll_signal(opt: str, short: float, spot: float, sd: float, out_buf: float = OUT_BUFFER,
                in_buf: float = IN_BUFFER) -> str | None:
    """"out" when the unrounded 1SD level is OUT_BUFFER+ past the short strike, "in" when the short strike is
    IN_BUFFER+ beyond it, else None."""
    level = spot + sd if opt == "CE" else spot - sd
    past = level - short if opt == "CE" else short - level
    if past >= out_buf:
        return "out"
    if -past >= in_buf:
        return "in"
    return None


def oi_walls(rows: list[dict], spot: float, sd: float | None) -> dict:
    """Call wall: strike above spot with the most call OI; put wall: below spot with the most put OI; only strikes
    within +/-2SD of spot. Each {"strike", "oi", "oi_open", "unwinding"} or None."""
    lo, hi = (spot - 2 * sd, spot + 2 * sd) if sd else (-math.inf, math.inf)
    out = {}
    for side, opt, keep in (("call", "ce", lambda k: spot < k <= hi), ("put", "pe", lambda k: lo <= k < spot)):
        best = None
        for r in rows:
            leg = r.get(opt) or {}
            if keep(r["strike"]) and leg.get("oi") and (best is None or leg["oi"] > best[1]["oi"]):
                best = (r["strike"], leg)
        if best is None:
            out[side] = None
            continue
        oi, oi_open = best[1]["oi"], best[1].get("oi_open")
        out[side] = {"strike": best[0], "oi": oi, "oi_open": oi_open,
                     "unwinding": (oi < oi_open * (1 - UNWIND_PCT / 100)) if oi_open else None}
    return out


def option_charges(action: str, value: float) -> float:
    """Brokerage and statutory charges of one option order of this premium value (₹): STT 0.1% on sells, NSE
    transaction 0.03503%, SEBI 0.0001%, stamp duty 0.003% on buys, GST 18% on brokerage + exchange + SEBI."""
    fees = config.BROKERAGE + value * (0.0003503 + 0.000001)
    return fees * 1.18 + (value * 0.001 if action == "SELL" else value * 0.00003)


def _mid(leg: dict | None) -> float | None:
    if not leg:
        return None
    if leg.get("bid") and leg.get("ask"):
        return (leg["bid"] + leg["ask"]) / 2
    return leg.get("ltp")


def _check(rule: str, ok: bool | None, value: str, limit: str, hard: bool, data: bool = False,
           key: str | None = None) -> dict:
    """ok None = no data. data=True marks a hard check that fails only for missing data (retried, not a skip).
    key: the RULES entry that can switch it off (None = locked)."""
    return {"rule": rule, "ok": ok, "value": value, "limit": limit, "hard": hard, "data": data, "key": key,
            "on": True}


def evaluate(snap: dict, now: datetime, vix: dict | None = None, hist: dict | None = None,
             skip_dates: list[str] | None = None, filters: bool = True, rules: dict | None = None) -> dict:
    """The whole rulebook on one market snapshot (pure). snap: {"spot", "expiry" (iso), "lot", "rows":
    [{"strike", "ce": {tsym, bid, ask, ltp, oi, oi_open}, "pe": {...}}], "underlying" (NIFTY when absent)};
    vix: {"now", "prev"}; hist: {"vix": [daily closes], "index": [the underlying's daily closes]}. filters=False skips the day / VIX rules (rolls).
    rules: {RULES key: bool}; a rule that is off is still shown, but never blocks (all on when omitted)."""
    on = {k: True for k in RULES} | (rules or {})
    u = snap.get("underlying") or UNDERLYING
    spec = SPECS[u]
    spot, rows = snap["spot"], sorted(snap["rows"], key=lambda r: r["strike"])
    expiry = date.fromisoformat(snap["expiry"])
    mins = trading_minutes_to(expiry, now)
    t = years(mins)
    checks: list[dict] = []
    out = {"underlying": u, "as_of": now.isoformat(timespec="seconds"), "expiry": snap["expiry"], "spot": spot,
           "zero_dte": expiry == now.date(), "minutes_left": round(mins, 1), "lot": snap.get("lot"),
           "checks": checks, "ok": False, "blockers": [], "legs": [], "sd": None, "rules": on}

    if filters:
        day = now.date().isoformat()
        checks.append(_check("Expiry day (0DTE)", out["zero_dte"], snap["expiry"], "today", True))
        skip = day in (skip_dates or [])
        checks.append(_check("Not an event day", not skip, "in skip list" if skip else "no", "skip list", True, key="event"))
        v_now, v_prev = (vix or {}).get("now"), (vix or {}).get("prev")
        chg = (v_now - v_prev) / v_prev * 100 if v_now and v_prev else None
        out["vix"] = {"now": v_now, "prev": v_prev, "change_pct": chg}
        checks.append(_check("India VIX 11-20", (VIX_BAND[0] <= v_now <= VIX_BAND[1]) if v_now else None,
                             f"{v_now:.2f}" if v_now else "no data", "11-20", False, key="vix_band"))
        checks.append(_check("VIX rise ≤ 5%", (chg <= VIX_RISE_MAX) if chg is not None else None,
                             f"{chg:+.2f}%" if chg is not None else "no data", "≤ +5%", True, data=chg is None, key="vix_rise"))
        p = ivp(v_now, (hist or {}).get("vix") or [])
        out["ivp"] = p
        checks.append(_check("IV percentile 30-85", (IVP_BAND[0] <= p <= IVP_BAND[1]) if p is not None else None,
                             f"{p:.0f}" if p is not None else "no data", "30-85", True, data=p is None, key="ivp"))
        hv = hv10((hist or {}).get("index") or (hist or {}).get(u.lower()) or [])
        out["hv10"] = hv
        checks.append(_check("VIX above HV10", (v_now > hv) if v_now and hv else None,
                             f"{v_now:.2f} vs {hv:.2f}" if v_now and hv else "no data", "VIX > HV10", False, key="hv10"))

    # 1SD: ATM IVs on the rulebook's clock, A vs B
    atm = min(rows, key=lambda r: abs(r["strike"] - spot)) if rows else None
    ce_px, pe_px = (_mid(atm.get("ce")), _mid(atm.get("pe"))) if atm else (None, None)
    iv_ce = strategy.implied_vol("CE", ce_px, spot, atm["strike"], t, 0.0) if ce_px else None
    iv_pe = strategy.implied_vol("PE", pe_px, spot, atm["strike"], t, 0.0) if pe_px else None
    iv_avg = (iv_ce + iv_pe) / 2 if iv_ce and iv_pe else None
    sd = one_sd(spot, iv_avg, mins, ce_px, pe_px)
    out["sd"] = {**sd, "atm": atm["strike"] if atm else None, "atm_ce": ce_px, "atm_pe": pe_px,
                 "iv_ce": iv_ce, "iv_pe": iv_pe, "iv_avg": iv_avg}
    checks.append(_check("1SD (A vs B)", sd["sd"] is not None,
                         f"{sd['sd']:.0f} pts ({sd['used']})" if sd["sd"] else "no ATM prices", "A within 10% of B",
                         True, data=True))
    if not sd["sd"]:
        return _finish(out)
    one = sd["sd"]

    # Candidates: 0.16 delta (each strike's own IV), 1SD, OI walls
    d_call = d_put = None
    best_c = best_p = None
    for r in rows:
        k = r["strike"]
        for opt, key in (("CE", "ce"), ("PE", "pe")):
            px = _mid(r.get(key))
            if not px:
                continue
            dl = delta(opt, spot, k, t, strategy.implied_vol(opt, px, spot, k, t, 0.0))
            if dl is None:
                continue
            if opt == "CE" and k > spot and (best_c is None or abs(dl - DELTA) < best_c):
                best_c, d_call = abs(dl - DELTA), k
            if opt == "PE" and k < spot and (best_p is None or abs(dl + DELTA) < best_p):
                best_p, d_put = abs(dl + DELTA), k
    walls = oi_walls(rows, spot, one)
    out["walls"] = walls
    unwinding = [s for s in ("call", "put") if walls.get(s) and walls[s]["unwinding"]]
    no_open = [s for s in ("call", "put") if walls.get(s) and walls[s]["oi_open"] is None]
    if filters:
        checks.append(_check("OI walls not unwinding", not unwinding,
                             ", ".join(f"{s} wall falling" for s in unwinding) or
                             ("no OI from the open yet" if no_open else "holding"), "OI ≥ open − 2%", True,
                             key="oi_unwind"))
    pk = pick_strikes(spot, one, d_call if on["cand_delta"] else None, d_put if on["cand_delta"] else None,
                      (walls.get("call") or {}).get("strike") if on["cand_wall"] else None,
                      (walls.get("put") or {}).get("strike") if on["cand_wall"] else None, use_sd=on["cand_sd"],
                      step=spec["step"], wing=spec["wing"])
    out["candidates"] = {"call": {"delta": d_call, "sd": spot + one, "wall": (walls.get("call") or {}).get("strike"),
                                  "by": pk["call_by"], "strike": pk["short_call"]},
                         "put": {"delta": d_put, "sd": spot - one, "wall": (walls.get("put") or {}).get("strike"),
                                 "by": pk["put_by"], "strike": pk["short_put"]}}
    out["targets"] = pk

    # Legs at the prices we would trade at
    by_k = {r["strike"]: r for r in rows}
    legs = []
    for role, opt, k, action in (("long put", "PE", pk["long_put"], "BUY"), ("short put", "PE", pk["short_put"], "SELL"),
                                 ("short call", "CE", pk["short_call"], "SELL"), ("long call", "CE", pk["long_call"], "BUY")):
        leg = (by_k.get(k) or {}).get(opt.lower())
        px = (leg.get("ask") if action == "BUY" else leg.get("bid")) if leg else None
        legs.append({"role": role, "opt": opt, "strike": k, "action": action, "tsym": (leg or {}).get("tsym"),
                     "bid": (leg or {}).get("bid"), "ask": (leg or {}).get("ask"), "ltp": (leg or {}).get("ltp"),
                     "oi": (leg or {}).get("oi"), "price": px})
    out["legs"] = legs
    missing = [g["role"] for g in legs if not g["tsym"]]
    one_sided = [g["role"] for g in legs if g["tsym"] and not g["price"]]
    checks.append(_check("Strikes in the chain", not missing, ", ".join(missing) or "all four", "all four", True,
                         data=True))
    checks.append(_check("Two-sided quotes", not one_sided and not missing, ", ".join(one_sided) or "yes",
                         "bid and ask", True, data=True))
    if missing or one_sided:
        return _finish(out)
    credit = sum(g["price"] * (1 if g["action"] == "SELL" else -1) for g in legs)
    lot = int(snap.get("lot") or 0)
    qty = lot * LOTS
    p = pop(pk["short_call"], pk["short_put"], credit, spot, one)
    out.update(credit=round(credit, 2), qty=qty, pop=p, max_profit=round(credit * qty, 2),
               max_loss=round((spec["wing"] - credit) * qty, 2),
               breakevens=[round(pk["short_put"] - credit, 2), round(pk["short_call"] + credit, 2)])
    checks.append(_check("POP > 75%", p > POP_MIN, f"{p * 100:.1f}%", "> 75%", True, key="pop"))
    return _finish(out)


def _finish(out: dict, rules: dict | None = None) -> dict:
    for c in out["checks"]:
        c["on"] = not c["key"] or out["rules"].get(c["key"], True)
    failed = [c for c in out["checks"] if c["on"] and c["hard"] and c["ok"] is not True
              and not (c["ok"] is None and not c["data"])]
    out["blockers"] = [c["rule"] for c in failed]
    out["data_only"] = bool(failed) and all(c["data"] for c in failed)
    out["ok"] = not failed and bool(out["legs"])
    return out


# ---- market data ------------------------------------------------------------------------------------------

def _nse():
    return markets.get("IN")


def today_expiry(now: datetime, u: str = UNDERLYING) -> str | None:
    day = now.date().isoformat()
    try:
        return day if day in fno.expiries(u) else None
    except Exception as exc:
        log.warning("%s expiries unavailable: %s", u, exc)
        return None


def next_expiry(now: datetime, u: str = UNDERLYING) -> str | None:
    try:
        exps = [e for e in fno.expiries(u) if e >= now.date().isoformat()]
    except Exception:
        return None
    return exps[0] if exps else None


def snapshot(expiry: str, now: datetime, u: str = UNDERLYING) -> dict:
    """An index's chain for `expiry` with each leg's OI at today's open (oiflow), in evaluate()'s shape."""
    from . import oiflow
    oiflow.track(u, expiry)
    ch = fno.chain(u, expiry, SPECS[u]["width"], fresh=True)
    spot = ch["spot"].get("ltp")
    if not spot:
        raise RuntimeError(f"No live {u} price")
    day = now.date().isoformat()
    rows = []
    for r in ch["rows"]:
        row = {"strike": float(r["strike"])}
        for k in ("ce", "pe"):
            leg = r.get(k)
            row[k] = None if not leg else {**{f: leg.get(f) for f in ("tsym", "bid", "ask", "ltp", "oi")},
                                           "oi_open": oiflow.oi_at_open(leg["tsym"], day)}
        rows.append(row)
    return {"underlying": u, "spot": spot, "expiry": expiry, "lot": ch["lot"], "rows": rows}


def vix_quote() -> dict:
    """India VIX now and its previous close (Kotak)."""
    from . import kotak
    q = kotak.index_quotes(["INDIA VIX"]).get("INDIA VIX") or {}
    return {"now": q.get("ltp"), "prev": q.get("prev_close")}


def history(now: datetime) -> dict:
    """Daily closes before today: India VIX (about 2 years, for the IVP) and each index (for HV10: keys "nifty",
    "banknifty", "sensex"), from Yahoo, fetched once a day and kept in DATA_DIR/condor_history.json."""
    path = config.DATA_DIR / "condor_history.json"
    day = now.date().isoformat()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("day") == day and all(u.lower() in cached for u in SPECS):
            return cached
    except (OSError, ValueError):
        pass
    import yfinance as yf
    out = {"day": day}
    for key, sym, period in (("vix", "^INDIAVIX", "2y"), *((u.lower(), sp["yahoo"], "3mo") for u, sp in SPECS.items())):
        df = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=False)
        out[key] = [float(c) for d, c in zip(df.index, df["Close"]) if d.date().isoformat() < day and c == c]
    if len(out["vix"]) >= 200 and all(len(out[u.lower()]) >= 11 for u in SPECS):
        path.write_text(json.dumps(out), encoding="utf-8")
    return out


def skip_dates() -> list[str]:
    """Event days (no trade): DATA_DIR/condor_skip.json, {"skipDates": ["YYYY-MM-DD", ...]}."""
    try:
        d = json.loads((config.DATA_DIR / "condor_skip.json").read_text(encoding="utf-8"))
        return sorted({str(x) for x in d.get("skipDates", [])})
    except (OSError, ValueError):
        return []


def set_skip_dates(dates: list[str]) -> list[str]:
    clean = sorted({date.fromisoformat(str(d).strip()).isoformat() for d in dates if str(d).strip()})
    (config.DATA_DIR / "condor_skip.json").write_text(json.dumps({"skipDates": clean}, indent=1), encoding="utf-8")
    return clean


def plan_now(now: datetime | None = None, expiry: str | None = None, u: str = UNDERLYING) -> dict:
    """The rulebook evaluated on live data now, for the dashboard (any day: the nearest expiry by default)."""
    now = now or _nse().now()
    exp = expiry or today_expiry(now, u) or next_expiry(now, u)
    if not exp:
        raise RuntimeError(f"No {u} expiry found")
    vix, hist = {}, {}
    try:
        vix = vix_quote()
    except Exception as exc:
        log.warning("India VIX unavailable: %s", exc)
    try:
        hist = history(now)
    except Exception as exc:
        log.warning("VIX / index history unavailable: %s", exc)
    hist = {"vix": hist.get("vix") or [], "index": hist.get(u.lower()) or []}
    return evaluate(snapshot(exp, now, u), now, vix, hist, skip_dates(), rules=get_rules(u))


# ---- storage ----------------------------------------------------------------------------------------------

_DAY_COLS = ("day", "mode", "status", "reason", "expiry", "entry_at", "exit_at", "last_roll_at", "rolls", "entry",
             "pnl", "charges", "updated_at")


def _conn():
    con = state._conn()
    old = [r[1] for r in con.execute("PRAGMA table_info(condor_days)")]
    if old and "und" not in old:                 # NIFTY-only table (day was the key): one row per (day, index) now
        con.execute("ALTER TABLE condor_days RENAME TO condor_days_nifty_only")
    con.execute("""CREATE TABLE IF NOT EXISTS condor_days (day TEXT, und TEXT NOT NULL DEFAULT 'NIFTY', mode TEXT,
        status TEXT, reason TEXT, expiry TEXT, entry_at TEXT, exit_at TEXT, last_roll_at TEXT, rolls INTEGER DEFAULT 0,
        entry TEXT, pnl REAL, charges REAL, updated_at TEXT, PRIMARY KEY (day, und))""")
    if old and "und" not in old:
        cols = ", ".join(_DAY_COLS)
        con.execute(f"INSERT INTO condor_days ({cols}, und) SELECT {cols}, 'NIFTY' FROM condor_days_nifty_only")
        con.execute("DROP TABLE condor_days_nifty_only")
    con.execute("""CREATE TABLE IF NOT EXISTS condor_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, at TEXT,
        mode TEXT, tsym TEXT, opt TEXT, strike REAL, action TEXT, qty INTEGER, price REAL, charges REAL,
        order_id TEXT, why TEXT, und TEXT NOT NULL DEFAULT 'NIFTY')""")
    con.execute("""CREATE TABLE IF NOT EXISTS condor_log (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, at TEXT,
        kind TEXT, text TEXT, data TEXT, und TEXT NOT NULL DEFAULT 'NIFTY')""")
    for t in ("condor_fills", "condor_log"):
        if "und" not in [r[1] for r in con.execute(f"PRAGMA table_info({t})")]:
            con.execute(f"ALTER TABLE {t} ADD COLUMN und TEXT NOT NULL DEFAULT 'NIFTY'")
    con.commit()
    return con


def _stamp() -> str:
    return _nse().now().isoformat(timespec="seconds")


def get_day(day: str, u: str = UNDERLYING) -> dict | None:
    with _conn() as con:
        r = con.execute("SELECT * FROM condor_days WHERE day = ? AND und = ?", (day, u)).fetchone()
    return dict(r) if r else None


def _save_day(day: str, u: str, **fields) -> None:
    fields["updated_at"] = _stamp()
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO condor_days (day, und) VALUES (?, ?)", (day, u))
        con.execute(f"UPDATE condor_days SET {', '.join(f'{k} = ?' for k in fields)} WHERE day = ? AND und = ?",
                    (*fields.values(), day, u))


def _log(day: str, u: str, kind: str, text: str, data: dict | None = None, notify: bool = False,
         mode: str = "") -> None:
    log.info("Condor %s %s: %s", u, kind, text)
    with _conn() as con:
        con.execute("INSERT INTO condor_log (day, und, at, kind, text, data) VALUES (?, ?, ?, ?, ?, ?)",
                    (day, u, _stamp(), kind, text, json.dumps(data, default=str) if data else None))
    if notify:
        from . import activity
        total = data.get("total") if isinstance(data, dict) else None
        sound = activity.kind_of_pnl(total) if kind == "exit" and isinstance(total, (int, float)) else             "closed" if kind == "exit" else activity.CONDOR.get(kind, "info")
        activity.record(sound, f"{u} 0DTE condor{' (paper)' if mode == 'paper' else ''}", text, "/#IN/options")
        try:
            tag = " (PAPER)" if mode == "paper" else ""
            telegram_bot.send_message(f"🦅 <b>{u} 0DTE condor{tag}</b> · {text}")
        except Exception:
            log.exception("Telegram notice for the condor failed")


def day_log(day: str, u: str = UNDERLYING, limit: int = 200) -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT at, kind, text, data FROM condor_log WHERE day = ? AND und = ? ORDER BY id DESC "
                           "LIMIT ?", (day, u, limit)).fetchall()
    return [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None} for r in rows]


def fills(day: str, u: str = UNDERLYING) -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM condor_fills WHERE day = ? AND und = ? ORDER BY id",
                                             (day, u))]


def entry_prices(day: str, u: str = UNDERLYING) -> dict:
    """Average price of each open leg's current quantity (fills that added to it since it was last flat)."""
    run: dict = {}
    for f in fills(day, u):
        q, avg = run.get(f["tsym"], (0, 0.0))
        d = f["qty"] if f["action"] == "BUY" else -f["qty"]
        if q == 0 or (q > 0) == (d > 0):            # opening or adding: weighted average
            n = q + d
            avg = (abs(q) * avg + abs(d) * f["price"]) / abs(n) if n else 0.0
            q = n
        else:                                        # reducing: the average stays; crossing zero restarts it
            n = q + d
            avg, q = (avg, n) if n == 0 or (n > 0) == (q > 0) else (f["price"], n)
        run[f["tsym"]] = (q, avg)
    return {t: round(a, 2) for t, (q, a) in run.items() if q}


def position(day: str, u: str = UNDERLYING) -> dict:
    """Open legs: tsym -> {"qty" (signed: + long, - short), "opt", "strike"}."""
    pos: dict = {}
    for f in fills(day, u):
        p = pos.setdefault(f["tsym"], {"qty": 0, "opt": f["opt"], "strike": f["strike"]})
        p["qty"] += f["qty"] if f["action"] == "BUY" else -f["qty"]
    return {k: v for k, v in pos.items() if v["qty"]}


def closed_legs(day: str, u: str = UNDERLYING) -> list[dict]:
    """Legs taken and closed again today (by a roll, a side close or the exit), oldest close first: each round trip
    of a strike from flat back to flat, with its average entry and exit, gross P&L and charges."""
    out, run = [], {}
    for f in fills(day, u):
        d = f["qty"] if f["action"] == "BUY" else -f["qty"]
        seg = run.get(f["tsym"])
        parts = [(d, f.get("charges") or 0.0)]
        if seg and (seg["q"] > 0) != (d > 0) and abs(d) > abs(seg["q"]):   # crosses flat: close, then open the rest
            close = -seg["q"]
            parts = [(close, (f.get("charges") or 0.0) * abs(close) / abs(d)),
                     (d - close, (f.get("charges") or 0.0) * abs(d - close) / abs(d))]
        for dq, ch in parts:
            seg = run.get(f["tsym"])
            if not seg:
                seg = run[f["tsym"]] = {"tsym": f["tsym"], "opt": f["opt"], "strike": f["strike"], "q": 0, "size": 0,
                                        "buy": [0, 0.0], "sell": [0, 0.0], "charges": 0.0, "opened_at": f["at"]}
            side = seg["buy"] if dq > 0 else seg["sell"]
            side[0] += abs(dq); side[1] += abs(dq) * f["price"]
            seg["charges"] += ch
            seg["q"] += dq
            if abs(seg["q"]) > abs(seg["size"]):
                seg["size"] = seg["q"]
            if seg["q"] == 0:
                long = seg["size"] > 0
                into, outof = (seg["buy"], seg["sell"]) if long else (seg["sell"], seg["buy"])
                out.append({"tsym": seg["tsym"], "opt": seg["opt"], "strike": seg["strike"], "qty": seg["size"],
                            "avg": round(into[1] / into[0], 2) if into[0] else None,
                            "exit": round(outof[1] / outof[0], 2) if outof[0] else None,
                            "pnl": round(seg["sell"][1] - seg["buy"][1], 2), "charges": round(seg["charges"], 2),
                            "opened_at": seg["opened_at"], "closed_at": f["at"]})
                del run[f["tsym"]]
    return out


def pnl(day: str, marks: dict, u: str = UNDERLYING) -> dict:
    """Realised cash of every fill + open legs at `marks` (tsym -> price) - charges."""
    fl = fills(day, u)
    cash = sum(f["price"] * f["qty"] * (1 if f["action"] == "SELL" else -1) for f in fl)
    charges = sum(f["charges"] or 0 for f in fl)
    pos = position(day, u)
    unmarked = [t for t in pos if marks.get(t) is None]
    open_val = sum(p["qty"] * marks[t] for t, p in pos.items() if marks.get(t) is not None)
    return {"cash": round(cash, 2), "open_value": round(open_val, 2), "charges": round(charges, 2),
            "total": round(cash + open_val - charges, 2), "unmarked": unmarked}


def get_rules(u: str = UNDERLYING) -> dict:
    """{RULES key: on}; every rule is on unless switched off on the dashboard."""
    st, _ = state.get_setting(_key(RULES_KEY, u), {})
    return {k: bool(st.get(k, True)) for k in RULES}


def set_rules(changes: dict, by: str, u: str = UNDERLYING) -> dict:
    """Switch rules on / off. At least one strike candidate must stay on."""
    bad = [k for k in changes if k not in RULES]
    if bad:
        raise ValueError(f"Unknown rule: {', '.join(bad)}")
    if any(not isinstance(v, bool) for v in changes.values()):
        raise ValueError("Each rule is true (on) or false (off)")
    old = get_rules(u)
    new = old | changes
    if not any(new[k] for k in CANDIDATES):
        raise ValueError("Keep at least one strike candidate (0.16 delta, 1SD level or OI wall) on")
    state.set_setting(_key(RULES_KEY, u), new)
    diff = [f"{RULES[k][1]} {'ON' if v else 'OFF'}" for k, v in changes.items() if old[k] != v]
    if diff:
        _log(_nse().now().date().isoformat(), u, "mode", f"Rules changed by {by}: {', '.join(diff)}", notify=True,
             mode=get_mode(u))
    return new


def get_mode(u: str = UNDERLYING) -> str:
    st, _ = state.get_setting(_key(MODE_KEY, u), {"mode": "paper"})
    m = str(st.get("mode", "paper"))
    return m if m in MODES else "paper"


def set_mode(mode: str, by: str, u: str = UNDERLYING) -> str:
    mode = str(mode).lower()
    if mode not in MODES:
        raise ValueError(f"Mode must be one of {', '.join(MODES)}")
    state.set_setting(_key(MODE_KEY, u), {"mode": mode, "by": by})
    day = _nse().now().date().isoformat()
    _log(day, u, "mode", f"Mode set to {mode.upper()} by {by}", notify=True, mode=mode)
    return mode


def trading_enabled(u: str = UNDERLYING) -> bool:
    """An index's own Auto-trade switch: Live mode opens and rolls only while it is ON (off until switched on)."""
    st, _ = state.get_setting(_key(TRADING_KEY, u), {"enabled": False})
    return bool(st.get("enabled"))


def set_trading(enabled: bool, by: str, u: str = UNDERLYING) -> bool:
    """Switch an index's condor auto-trading. ON first proves the Kotak login works (raises BrokerError if not)."""
    if enabled:
        broker.login(force=True)
    state.set_setting(_key(TRADING_KEY, u), {"enabled": bool(enabled), "by": by})
    day = _nse().now().date().isoformat()
    _log(day, u, "mode", f"Condor Auto-trade {'ON' if enabled else 'OFF'} by {by}", notify=True, mode=get_mode(u))
    return bool(enabled)


# ---- orders -----------------------------------------------------------------------------------------------

def _quote(tsym: str) -> dict:
    return fno.option_quote(tsym)


def _fill(day: str, u: str, mode: str, tsym: str, action: str, qty: int, why: str) -> float:
    """Buy or sell `qty` of `tsym`: paper at the bid / ask, live with marketable limits re-priced up to
    FILL_TRIES times. Records the fills; raises ExecError if it can't be filled completely."""
    c = fno.contract(tsym) or {}
    opt, strike, tick = c.get("opt"), float(c.get("strike") or 0), float(c.get("tick") or 0.05)
    if mode != "live":
        q = _quote(tsym)
        px = (q.get("ask") if action == "BUY" else q.get("bid")) or q.get("ltp")
        if not px:
            raise ExecError(f"No price for {tsym}")
        _record_fill(day, u, mode, tsym, opt, strike, action, qty, float(px), None, why)
        return float(px)
    from .manual import protect
    from .orders import _filled, order_state
    left, got, value = qty, 0, 0.0
    for attempt in range(FILL_TRIES):
        q = _quote(tsym)
        ref = (q.get("ask") if action == "BUY" else q.get("bid")) or q.get("ltp")
        if not ref:
            raise ExecError(f"No price for {tsym}")
        px = protect(float(ref), action, min(config.OPTION_PROTECTION_PCT * (attempt + 1), 25.0), tick)
        res = broker.call("place_order", exchange_segment=fno.segment_of(c), product=PRODUCT, validity="DAY", amo="NO",
                          order_type="L", price=f"{px:.2f}", trigger_price="0", quantity=str(left),
                          trading_symbol=tsym, transaction_type=action[0])
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        oid = str(res.get("nOrdNo") or data.get("nOrdNo") or "")
        if not oid:
            raise ExecError(f"No order number for {tsym}: {str(res)[:200]}")
        deadline = _time.monotonic() + FILL_WAIT_SEC
        o = None
        while True:
            book = {str(x.get("nOrdNo")): x for x in (broker.call("order_report").get("data") or [])}
            o = book.get(oid)
            st = order_state(o)
            if st in ("FILLED", "REJECTED", "CANCELLED") or _time.monotonic() >= deadline:
                break
            _time.sleep(1)
        if order_state(o) == "OPEN":
            try:
                broker.call("cancel_order", order_id=oid)
                _time.sleep(1)
                book = {str(x.get("nOrdNo")): x for x in (broker.call("order_report").get("data") or [])}
                o = book.get(oid, o)
            except broker.BrokerError as exc:
                log.warning("Cancelling condor order %s failed: %s", oid, exc)
        n, avg = _filled(o)
        if n:
            _record_fill(day, u, mode, tsym, opt, strike, action, n, float(avg or px), oid, why)
            got, value, left = got + n, value + n * float(avg or px), left - n
        if left <= 0:
            return value / got
        if order_state(o) == "REJECTED":
            raise ExecError(f"{action} {tsym} rejected: {str((o or {}).get('rejRsn') or '')[:150]}")
    raise ExecError(f"{action} {left} of {tsym} not filled after {FILL_TRIES} tries")


def _record_fill(day, u, mode, tsym, opt, strike, action, qty, px, oid, why) -> None:
    with _conn() as con:
        con.execute("""INSERT INTO condor_fills (day, und, at, mode, tsym, opt, strike, action, qty, price, charges,
            order_id, why) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (day, u, _stamp(), mode, tsym, opt, strike, action, qty, px,
                     round(option_charges(action, px * qty), 2), oid, why))


def _trade_to(day: str, u: str, mode: str, target: dict, why: str) -> None:
    """Move the position to `target` (tsym -> signed qty): every buy first, then the sells."""
    cur = {t: p["qty"] for t, p in position(day, u).items()}
    diff = {t: target.get(t, 0) - cur.get(t, 0) for t in {*cur, *target}}
    for t in sorted(t for t, d in diff.items() if d > 0):
        _fill(day, u, mode, t, "BUY", diff[t], why)
    for t in sorted(t for t, d in diff.items() if d < 0):
        _fill(day, u, mode, t, "SELL", -diff[t], why)


def _target_of(plan: dict, qty: int, sides=("call", "put")) -> dict:
    out = {}
    for g in plan["legs"]:
        if ("call" if g["opt"] == "CE" else "put") in sides:
            out[g["tsym"]] = qty if g["action"] == "BUY" else -qty
    return out


def _side_legs(pos: dict, opt: str) -> dict:
    return {t: p for t, p in pos.items() if p["opt"] == opt}


# ---- the daily loop ---------------------------------------------------------------------------------------

def run_forever() -> None:
    while True:
        try:
            tick()
        except Exception:
            log.exception("Condor tick failed")
        _time.sleep(TICK_SEC)


def start() -> None:
    if _mem["thread"] is None:
        _mem["thread"] = threading.Thread(target=run_forever, name="condor", daemon=True)
        _mem["thread"].start()


def tick(now: datetime | None = None) -> None:
    """One pass of every index's book (a failure in one never stops the others)."""
    now = now or _nse().now()
    for u in UNDERLYINGS:
        try:
            tick_book(u, now)
        except Exception:
            log.exception("Condor tick failed for %s", u)


def tick_book(u: str, now: datetime) -> None:
    with _lock:
        mem = _book(u)
        day = now.date().isoformat()
        rec = get_day(day, u)
        pos = position(day, u)
        if pos:
            _manage(day, u, rec or {}, pos, now)
            return
        if rec and rec["status"] == OPEN:        # flat again (closed by hand or the last side exited)
            _close_day(day, u, CLOSED, rec.get("reason") or "All legs closed", now)
            return
        if not _nse().is_open(now) or (rec and rec["status"] in FINAL):
            return
        mode = get_mode(u)
        if mode == "off":
            return
        exp = today_expiry(now, u)
        if exp is None:
            return
        if rec is None:
            _save_day(day, u, mode=mode, status=WAITING, expiry=exp, reason="Waiting for 09:45")
            _precheck(day, u, exp, mode, now)
            return
        if now.time() < ENTRY_FROM:
            return
        if now.time() >= ENTRY_TO:
            _save_day(day, u, status=SKIPPED, reason="No entry between 09:45 and 10:15")
            _log(day, u, "skip", "No entry between 09:45 and 10:15: " + (rec.get("reason") or ""), notify=True,
                 mode=mode)
            return
        if _time.monotonic() - mem["entry_try"] < ENTRY_RETRY_SEC:
            return
        mem["entry_try"] = _time.monotonic()
        _try_entry(day, u, exp, mode, now)


def _precheck(day: str, u: str, exp: str, mode: str, now: datetime) -> None:
    """Before 09:45: the day filters, posted once; a failed day filter skips the day."""
    try:
        p = plan_now(now, exp, u)
    except Exception as exc:
        _log(day, u, "check", f"Pre-check: market data unavailable ({exc}); will try at 09:45")
        return
    _book(u)["plan"] = p
    day_rules = ("Expiry day (0DTE)", "Not an event day", "VIX rise ≤ 5%", "IV percentile 30-85")
    bad = [c for c in p["checks"] if c["rule"] in day_rules and c["ok"] is False and c["on"]]
    text = " · ".join(f"{'✅' if c['ok'] else ('⚠️' if not c['hard'] else '❌') if c['ok'] is False else '…'} "
                      f"{c['rule']}: {c['value']}" for c in p["checks"] if c["rule"] in day_rules
                      or c["rule"] in ("India VIX 11-20", "VIX above HV10"))
    if bad:
        _save_day(day, u, status=SKIPPED, reason="; ".join(f"{c['rule']} ({c['value']})" for c in bad))
        _log(day, u, "skip", "No trade today: " + "; ".join(f"{c['rule']} ({c['value']})" for c in bad),
             {"checks": p["checks"]}, notify=True, mode=mode)
    else:
        _log(day, u, "check", "Expiry day pre-check: " + text, {"checks": p["checks"]}, notify=True, mode=mode)


def _try_entry(day: str, u: str, exp: str, mode: str, now: datetime) -> None:
    try:
        p = plan_now(now, exp, u)
    except Exception as exc:
        _save_day(day, u, reason=f"Market data unavailable: {exc}")
        _log(day, u, "check", f"Entry: market data unavailable ({exc}); retrying")
        return
    _book(u)["plan"] = p
    if not p["ok"]:
        if p.get("data_only"):
            _save_day(day, u, reason="Waiting for data: " + ", ".join(p["blockers"]))
            _log(day, u, "check", "Entry: waiting for data (" + ", ".join(p["blockers"]) + ")", {"checks": p["checks"]})
            return
        bad = [c for c in p["checks"] if c["rule"] in p["blockers"]]
        why = "; ".join(f"{c['rule']} ({c['value']})" for c in bad)
        _save_day(day, u, status=SKIPPED, reason=why, entry=json.dumps(_summary(p)))
        _log(day, u, "skip", f"No trade today: {why}", _summary(p), notify=True, mode=mode)
        return
    if mode == "live" and not trading_enabled(u):
        _save_day(day, u, status=SKIPPED, reason="Live mode but this index's Auto-trade switch is OFF")
        _log(day, u, "skip", "Live mode but the condor's Auto-trade switch is OFF: no trade", notify=True, mode=mode)
        return
    s = _summary(p)
    try:
        _trade_to(day, u, mode, _target_of(p, p["qty"]), "entry")
    except Exception as exc:
        _log(day, u, "error", f"Entry failed ({exc}); closing whatever filled", s, notify=True, mode=mode)
        _flatten(day, u, mode, "entry failed")
        _save_day(day, u, status=FAILED, reason=f"Entry failed: {exc}")
        return
    _save_day(day, u, status=OPEN, mode=mode, entry_at=now.isoformat(timespec="seconds"), reason="Open",
              entry=json.dumps(s))
    t = p["targets"]
    _log(day, u, "entry",
         f"Entered: buy {t['long_put']:.0f} PE, sell {t['short_put']:.0f} PE, sell {t['short_call']:.0f} CE, "
         f"buy {t['long_call']:.0f} CE · credit {p['credit']:.2f} (₹{p['max_profit']:,.0f}) · max loss "
         f"₹{p['max_loss']:,.0f} · POP {p['pop'] * 100:.1f}% · 1SD {p['sd']['sd']:.0f} ({p['sd']['used']}) · "
         f"call by {t['call_by']}, put by {t['put_by']}", s, notify=True, mode=mode)


def _summary(p: dict) -> dict:
    """What gets logged for a decision: checks, 1SD A/B, candidates, legs, credit, POP."""
    return {k: p.get(k) for k in ("underlying", "as_of", "spot", "expiry", "minutes_left", "vix", "ivp", "hv10", "sd",
                                  "candidates", "walls", "targets", "legs", "credit", "pop", "max_profit", "max_loss",
                                  "breakevens", "checks", "blockers")}


def _marks(u: str, pos: dict) -> dict:
    """Mid prices (or last) of the open legs; their full quotes are kept for the dashboard."""
    out = {}
    for t in pos:
        try:
            q = _quote(t)
        except Exception as exc:
            log.debug("Quote for %s failed: %s", t, exc)
            continue
        _book(u)["quotes"][t] = {k: q.get(k) for k in ("bid", "ask", "ltp")}
        out[t] = (q["bid"] + q["ask"]) / 2 if q.get("bid") and q.get("ask") else q.get("ltp")
    return out


def _manage(day: str, u: str, rec: dict, pos: dict, now: datetime) -> None:
    mem = _book(u)
    mode = rec.get("mode") or get_mode(u)
    marks = _marks(u, pos)
    pl = pnl(day, marks, u)
    mem.update(pnl=pl, marks=marks, pnl_at=now.isoformat(timespec="seconds"))
    cap = loss_cap(u)
    if get_rules(u)["loss_cap"] and not pl["unmarked"] and pl["total"] <= -cap:
        _log(day, u, "cap", f"Daily loss cap hit: P&L ₹{pl['total']:,.0f} ≤ −₹{cap:,.0f}; closing everything",
             pl, notify=True, mode=mode)
        _exit_all(day, u, mode, STOPPED, f"Loss cap (₹{pl['total']:,.0f})", now)
        return
    if now.time() >= EXIT_FROM:
        _exit_all(day, u, mode, CLOSED, "Time exit (15:00)", now)
        return
    if not _nse().is_open(now) or _time.monotonic() - mem["retarget_at"] < RETARGET_SEC:
        return
    mem["retarget_at"] = _time.monotonic()
    try:
        _retarget(day, u, rec, pos, mode, now)
    except Exception as exc:
        log.exception("Condor re-target failed")
        _log(day, u, "error", f"Re-target failed: {exc}")


def _retarget(day: str, u: str, rec: dict, pos: dict, mode: str, now: datetime) -> None:
    spec = SPECS[u]
    exp = rec.get("expiry") or today_expiry(now, u)
    rules = get_rules(u)
    p = evaluate(snapshot(exp, now, u), now, filters=False, rules=rules)
    _book(u)["plan"] = p
    if not p.get("sd") or not p["sd"].get("sd") or not p.get("targets"):
        return
    spot, one, tg = p["spot"], p["sd"]["sd"], p["targets"]
    walls = p.get("walls") or {}
    wants = []   # (opt, sig, why); a threatened side (out) goes before an inward roll
    for opt, side in (("CE", "call"), ("PE", "put")):
        legs = _side_legs(pos, opt)
        short = next((v["strike"] for v in legs.values() if v["qty"] < 0), None)
        if short is None:
            continue
        level = roll_signal(opt, short, spot, one, spec["out"], spec["in"])
        lvl = spot + one if opt == "CE" else spot - one
        wall = (walls.get(side) or {}).get("strike")
        wall_past = wall is not None and (wall > short if opt == "CE" else wall < short)
        threat = (f"1SD level {lvl:.0f} is {abs(lvl - short):.0f} pts past the {short:.0f} {opt}" if level == "out"
                  else f"{side} OI wall moved to {wall:.0f}, past the {short:.0f} {opt}" if wall_past and rules["roll_wall"]
                  else None)
        if now.time() >= ROLL_UNTIL:
            if threat and rules["late_exit"]:
                _exit_side(day, u, mode, opt, f"after 14:30, {threat}")
            continue
        sig, why = None, None
        if level == "out" and rules["roll_out"]:
            sig, why = "out", threat
        elif wall_past and rules["roll_wall"]:
            sig, why = "out", f"{side} OI wall moved to {wall:.0f}, past the {short:.0f} {opt}"
        elif level == "in" and rules["roll_in"]:
            sig = "in"
        if sig is None:
            continue
        new_short = tg["short_call" if opt == "CE" else "short_put"]
        if new_short == short:
            continue
        last = rec.get("last_roll_at")
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=COOLDOWN_MIN):
            _log(day, u, "roll", f"{opt} side wants to roll {sig} ({why or 'short beyond its 1SD level'}) but the "
                                 f"{COOLDOWN_MIN}-minute cooldown is running")
            continue
        wants.append((opt, sig, why or f"short {short:.0f} is {spec['in']}+ pts beyond its 1SD level"))
    # Out before in: a skipped inward roll on one side must never keep the threatened side from rolling
    for opt, sig, why in sorted(wants, key=lambda w: w[1] != "out"):
        if _roll(day, u, rec, pos, mode, p, opt, sig, why, now):
            return   # one change per re-target; the position changed


def _roll(day: str, u: str, rec: dict, pos: dict, mode: str, p: dict, opt: str, sig: str, why: str,
          now: datetime) -> bool:
    """Roll one side to the plan's strikes; True when the position changed (rolled, or the side was closed)."""
    side = "call" if opt == "CE" else "put"
    qty = max(abs(v["qty"]) for v in pos.values())
    keep = {t: v["qty"] for t, v in pos.items() if v["opt"] != opt}
    new_side = _target_of(p, qty, (side,))
    target = {**keep, **new_side}
    legs = {g["tsym"]: g for g in p["legs"]}
    # Resulting condor at the prices we'd trade at, for POP; the roll's own cash for the inward rule
    quotes = {t: _quote(t) for t in {*pos, *target}}
    def px(t, action):
        q = quotes[t]
        return (q.get("ask") if action == "BUY" else q.get("bid")) or q.get("ltp") or 0.0
    credit = sum(px(t, "SELL" if q < 0 else "BUY") * (1 if q < 0 else -1) for t, q in target.items()) / qty
    shorts = {v["opt"]: v["strike"] for v in pos.values() if v["qty"] < 0 and v["opt"] != opt}
    new_short = next(g["strike"] for g in p["legs"] if g["opt"] == opt and g["action"] == "SELL")
    sc = new_short if opt == "CE" else shorts.get("CE")
    sp = new_short if opt == "PE" else shorts.get("PE")
    pp = pop(sc, sp, credit, p["spot"], p["sd"]["sd"])
    cur = {t: v["qty"] for t, v in pos.items()}
    diff = {t: target.get(t, 0) - cur.get(t, 0) for t in {*cur, *target}}
    roll_cash = sum(-d * px(t, "BUY") if d > 0 else -d * px(t, "SELL") for t, d in diff.items() if d)
    info = {"side": opt, "direction": sig, "why": why, "from": [v["strike"] for v in pos.values() if v["opt"] == opt],
            "to": [legs[t]["strike"] for t in new_side if t in legs], "pop": pp, "roll_cash": round(roll_cash, 2),
            "spot": p["spot"], "sd": p["sd"]["sd"]}
    if sig == "in" and roll_cash <= 0:
        _log(day, u, "roll", f"{opt} inward roll skipped: it would not bring a net credit (₹{roll_cash:,.0f})", info)
        return False
    if pp <= POP_MIN and get_rules(u)["pop"]:
        if sig == "out":
            _log(day, u, "roll", f"{opt} roll out can't meet POP > 75% ({pp * 100:.1f}%): closing that side",
                 info, notify=True, mode=mode)
            _exit_side(day, u, mode, opt, why)
            return True
        _log(day, u, "roll", f"{opt} inward roll skipped: POP {pp * 100:.1f}% ≤ 75%", info)
        return False
    if mode == "live" and not trading_enabled(u):
        _log(day, u, "roll", f"{opt} roll {sig} needed but the condor's Auto-trade switch is OFF: closing that side "
                             f"instead", info, notify=True, mode=mode)
        _exit_side(day, u, mode, opt, why)
        return True
    try:
        _trade_to(day, u, mode, target, f"roll {sig} {opt}")
    except Exception as exc:
        _log(day, u, "error", f"Roll failed ({exc}); closing everything", info, notify=True, mode=mode)
        _exit_all(day, u, mode, FAILED, f"Roll failed: {exc}", now)
        return True
    _save_day(day, u, last_roll_at=now.isoformat(timespec="seconds"), rolls=int(rec.get("rolls") or 0) + 1)
    _log(day, u, "roll", f"Rolled the {opt} side {sig} to {'/'.join(f'{k:.0f}' for k in sorted(info['to']))} "
                         f"({why}) · cash ₹{roll_cash:,.0f} · POP {pp * 100:.1f}%", info, notify=True, mode=mode)
    return True


def _exit_side(day: str, u: str, mode: str, opt: str, why: str) -> bool:
    pos = position(day, u)
    target = {t: v["qty"] for t, v in pos.items() if v["opt"] != opt}
    try:
        _trade_to(day, u, mode, target, f"exit {opt}")
    except Exception as exc:
        _log(day, u, "error", f"Closing the {opt} side failed ({exc}); retrying", notify=True, mode=mode)
        return False
    _log(day, u, "exit", f"Closed the {opt} side: {why}", notify=True, mode=mode)
    return True


def _flatten(day: str, u: str, mode: str, why: str) -> bool:
    try:
        _trade_to(day, u, mode, {}, why)
        return True
    except Exception as exc:
        _log(day, u, "error", f"Closing everything failed ({exc}); retrying in {TICK_SEC} s", notify=True, mode=mode)
        return False


def _exit_all(day: str, u: str, mode: str, status: str, why: str, now: datetime) -> None:
    mem = _book(u)
    if _flatten(day, u, mode, why):
        _close_day(day, u, status, why, now)
    elif now.time() >= EXIT_BY and mem["exit_warned"] != day:
        mem["exit_warned"] = day
        _log(day, u, "error", "⚠️ Legs still open after 15:15: close them in the Kotak app", notify=True, mode=mode)


def _close_day(day: str, u: str, status: str, why: str, now: datetime) -> None:
    pl = pnl(day, {}, u)
    rec = get_day(day, u) or {}
    _save_day(day, u, status=status, reason=why, exit_at=now.isoformat(timespec="seconds"), pnl=pl["total"],
              charges=pl["charges"])
    _book(u).update(pnl=pl)
    _log(day, u, "exit", f"Day over ({why}): net P&L ₹{pl['total']:,.0f} after ₹{pl['charges']:,.0f} charges · "
                         f"{int(rec.get('rolls') or 0)} roll(s)", pl, notify=True, mode=rec.get("mode") or "")


def exit_now(by: str, u: str = UNDERLYING) -> dict:
    """Close every leg of one index's condor now (dashboard button)."""
    u = _und(u)
    with _lock:
        now = _nse().now()
        day = now.date().isoformat()
        rec = get_day(day, u) or {}
        if not position(day, u):
            raise ValueError(f"No open {u} condor legs today")
        _log(day, u, "exit", f"Close all requested by {by}", notify=True, mode=rec.get("mode") or "")
        _exit_all(day, u, rec.get("mode") or get_mode(u), CLOSED, f"Closed by hand ({by})", now)
        return status(u)


def close_side(opt: str, by: str, u: str = UNDERLYING) -> dict:
    """Close one side (its short bought back first, then its wing sold) now (dashboard button)."""
    u = _und(u)
    opt = str(opt).upper()
    if opt not in ("CE", "PE"):
        raise ValueError("Side must be CE (calls) or PE (puts)")
    with _lock:
        now = _nse().now()
        day = now.date().isoformat()
        rec = get_day(day, u) or {}
        if not _side_legs(position(day, u), opt):
            raise ValueError(f"No open {u} {opt} legs")
        mode = rec.get("mode") or get_mode(u)
        if not _exit_side(day, u, mode, opt, f"closed by hand ({by})"):
            raise RuntimeError(f"Closing the {opt} side failed; see Today's decisions")
        if not position(day, u):
            _close_day(day, u, CLOSED, f"Closed by hand ({by})", now)
        return status(u)


def close_leg(tsym: str, by: str, u: str = UNDERLYING) -> dict:
    """Buy back one short leg now. A wing can't be closed on its own: its short would be left uncovered."""
    u = _und(u)
    with _lock:
        now = _nse().now()
        day = now.date().isoformat()
        rec = get_day(day, u) or {}
        pos = position(day, u)
        leg = pos.get(tsym)
        if not leg:
            raise ValueError(f"{tsym} isn't an open {u} condor leg")
        if leg["qty"] > 0:
            raise ValueError("A wing can't be closed on its own: its short would be left uncovered. "
                             "Close the short first, or the whole side.")
        mode = rec.get("mode") or get_mode(u)
        target = {t: v["qty"] for t, v in pos.items() if t != tsym}
        try:
            _trade_to(day, u, mode, target, "close leg")
        except Exception as exc:
            _log(day, u, "error", f"Buying back {tsym} failed ({exc})", notify=True, mode=mode)
            raise RuntimeError(f"Buying back {tsym} failed: {exc}") from exc
        _log(day, u, "exit", f"Bought back {tsym} by hand ({by})", notify=True, mode=mode)
        if not position(day, u):
            _close_day(day, u, CLOSED, f"Closed by hand ({by})", now)
        return status(u)


# ---- dashboard --------------------------------------------------------------------------------------------

def overview(now: datetime | None = None) -> list[dict]:
    """One line per index for the dashboard's index picker: mode, switch, today's state, open legs, P&L."""
    now = now or _nse().now()
    day = now.date().isoformat()
    out = []
    for u in UNDERLYINGS:
        rec, pos = get_day(day, u), position(day, u)
        pl = _book(u)["pnl"] if pos else (rec or {}).get("pnl")
        out.append({"underlying": u, "name": SPECS[u]["name"], "mode": get_mode(u), "trading_enabled": trading_enabled(u),
                    "status": (rec or {}).get("status"), "legs": len(pos), "today_expiry": today_expiry(now, u),
                    "next_expiry": next_expiry(now, u),
                    "pnl": pl.get("total") if isinstance(pl, dict) else pl})
    return out


def status(u: str = UNDERLYING) -> dict:
    u = _und(u)
    mem, spec = _book(u), SPECS[u]
    now = _nse().now()
    day = now.date().isoformat()
    rec = get_day(day, u)
    pos = position(day, u)
    marks = mem["marks"] if pos else {}
    if pos and not marks:                       # outside the engine's loop (e.g. market closed): read them now
        marks = _marks(u, pos)
    avg = entry_prices(day, u)
    legs = []
    for t, v in sorted(pos.items(), key=lambda kv: (kv[1]["opt"] != "PE", kv[1]["strike"])):
        q, m, a = mem["quotes"].get(t) or {}, marks.get(t), avg.get(t)
        legs.append({"tsym": t, **v, "avg": a, "mark": m, "bid": q.get("bid"), "ask": q.get("ask"),
                     "ltp": q.get("ltp"), "pnl": round((m - a) * v["qty"], 2) if m is not None and a else None})
    with _conn() as con:
        hist = [dict(r) for r in con.execute(
            "SELECT day, mode, status, reason, rolls, pnl, charges FROM condor_days WHERE und = ? "
            "ORDER BY day DESC LIMIT 40", (u,))]
    plan = mem["plan"]
    if plan and plan.get("underlying", UNDERLYING) != u:
        plan = None
    cap = loss_cap(u)
    return {"underlying": u, "name": spec["name"], "books": overview(now),
            "mode": get_mode(u), "trading_enabled": trading_enabled(u), "now": now.isoformat(timespec="seconds"),
            "today": rec, "today_expiry": today_expiry(now, u), "next_expiry": next_expiry(now, u),
            "legs": legs, "pnl": pnl(day, marks, u) if (pos or fills(day, u)) else None,
            "fills": fills(day, u), "closed_legs": closed_legs(day, u), "log": day_log(day, u), "plan": plan, "history": hist,
            "skip_dates": skip_dates(),
            "rule_settings": [{"key": k, "group": g, "name": n,
                               "desc": d.replace("CONDOR_LOSS_CAP", f"₹{cap:,.0f}")
                                        .replace("25+ pts", f"{spec['out']}+ pts").replace("75+ pts", f"{spec['in']}+ pts"),
                               "on": v} for (k, (g, n, d)), v in zip(RULES.items(), get_rules(u).values())],
            "locked_rules": [r.replace("1 lot, fixed wings", f"1 lot, {spec['wing']}-point wings") for r in LOCKED],
            "rules": {"loss_cap": cap, "lots": LOTS, "wing": spec["wing"], "step": spec["step"],
                      "entry": "09:45-10:15", "roll_until": "14:30", "exit": "15:00-15:15", "pop_min": POP_MIN * 100,
                      "out_buffer": spec["out"], "in_buffer": spec["in"], "cooldown_min": COOLDOWN_MIN,
                      "vix_band": VIX_BAND, "vix_rise_max": VIX_RISE_MAX, "ivp_band": IVP_BAND, "delta": DELTA,
                      "exchange": spec["exchange"], "expiry": spec["expiry"]}}
