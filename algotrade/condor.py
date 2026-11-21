"""Nifty 0DTE short iron condor, run by the rulebook (README > 0DTE iron condor).

On NIFTY's weekly expiry day only, one lot: buy two wings 50 points outside two short strikes, sell the shorts,
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
  and the roll brings in a net credit; at least 10 minutes between rolls, none after 14:30 (a threatened side
  is closed instead). A roll that can't meet POP > 75% closes that side.
- Daily loss cap: cash from every fill + open legs at mid - charges <= -CONDOR_LOSS_CAP closes everything and
  stops for the day (checked every few seconds). From 15:00 everything is squared off: shorts first, then wings.
Orders: bought legs always before sold ones (wings first in, shorts first out), marketable limits off the best
bid / ask, re-priced up to 4 times. Modes: "paper" (default: fills at the bid / ask, no orders), "live" (real
MIS orders; also needs the Auto-trade switch ON to open or roll, never to close), "off" (nothing new; an open
position is still managed to its exit). Every decision goes to the condor_log table and Telegram."""
import json
import logging
import math
import threading
import time as _time
from datetime import date, datetime, time, timedelta

from . import broker, config, fno, markets, state, strategy, telegram_bot

log = logging.getLogger(__name__)

UNDERLYING = "NIFTY"
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
COOLDOWN_MIN = 10
VIX_BAND = (11.0, 20.0)      # check
VIX_RISE_MAX = 5.0           # % vs previous close, hard
IVP_BAND = (30.0, 85.0)      # hard
AB_TOLERANCE = 0.10
STRADDLE_FACTOR = 1.25       # SD = straddle / sqrt(2/pi)
UNWIND_PCT = 2.0             # a wall's OI this far below its level at the open counts as unwinding
CHAIN_WIDTH = 25             # strikes either side of the money read for walls and deltas
TICK_SEC = 3
RETARGET_SEC = 60
ENTRY_RETRY_SEC = 30
FILL_WAIT_SEC = 10
FILL_TRIES = 4
PRODUCT = "MIS"
MODES = ("off", "paper", "live")
MODE_KEY = "condor_mode"

WAITING, OPEN, SKIPPED, CLOSED, STOPPED, FAILED = "WAITING", "OPEN", "SKIPPED", "CLOSED", "STOPPED", "FAILED"
FINAL = (SKIPPED, CLOSED, STOPPED, FAILED)

_lock = threading.RLock()
_mem = {"plan": None, "pnl": None, "marks": {}, "pnl_at": None, "retarget_at": 0.0, "entry_try": 0.0,
        "exit_warned": None, "thread": None}


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
    """Trading minutes from now until 15:30 on expiry day (weekdays; NSE holidays not removed)."""
    today = now.date()
    if expiry < today:
        return 0.0
    if expiry == today:
        return minutes_left(now)
    left = minutes_left(now) if today.weekday() < 5 else 0.0
    d = today + timedelta(days=1)
    while d <= expiry:
        if d.weekday() < 5:
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
                 call_wall: float | None, put_wall: float | None) -> dict:
    """The farthest of the three candidates per side, rounded outward, and which candidate won."""
    calls = {"delta": d_call, "1SD": spot + sd, "OI wall": call_wall}
    puts = {"delta": d_put, "1SD": spot - sd, "OI wall": put_wall}
    calls = {k: v for k, v in calls.items() if v is not None}
    puts = {k: v for k, v in puts.items() if v is not None}
    c_by = max(calls, key=lambda k: calls[k])
    p_by = min(puts, key=lambda k: puts[k])
    sc, sp = round_up(calls[c_by]), round_down(puts[p_by])
    return {"short_call": sc, "long_call": sc + WING, "short_put": sp, "long_put": sp - WING,
            "call_by": c_by, "put_by": p_by, "call_level": spot + sd, "put_level": spot - sd}


def pop(short_call: float | None, short_put: float | None, credit: float, spot: float, sd: float) -> float:
    """Probability of profit at expiry: N(z_up) + N(z_dn) - 1, a missing side counting as certain."""
    up = ncdf((short_call + credit - spot) / sd) if short_call is not None else 1.0
    dn = ncdf((spot - (short_put - credit)) / sd) if short_put is not None else 1.0
    return up + dn - 1


def roll_signal(opt: str, short: float, spot: float, sd: float) -> str | None:
    """"out" when the unrounded 1SD level is OUT_BUFFER+ past the short strike, "in" when the short strike is
    IN_BUFFER+ beyond it, else None."""
    level = spot + sd if opt == "CE" else spot - sd
    past = level - short if opt == "CE" else short - level
    if past >= OUT_BUFFER:
        return "out"
    if -past >= IN_BUFFER:
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


def _check(rule: str, ok: bool | None, value: str, limit: str, hard: bool, data: bool = False) -> dict:
    """ok None = no data. data=True marks a hard check that fails only for missing data (retried, not a skip)."""
    return {"rule": rule, "ok": ok, "value": value, "limit": limit, "hard": hard, "data": data}


def evaluate(snap: dict, now: datetime, vix: dict | None = None, hist: dict | None = None,
             skip_dates: list[str] | None = None, filters: bool = True) -> dict:
    """The whole rulebook on one market snapshot (pure). snap: {"spot", "expiry" (iso), "lot", "rows":
    [{"strike", "ce": {tsym, bid, ask, ltp, oi, oi_open}, "pe": {...}}]}; vix: {"now", "prev"};
    hist: {"vix": [daily closes], "nifty": [daily closes]}. filters=False skips the day / VIX rules (rolls)."""
    spot, rows = snap["spot"], sorted(snap["rows"], key=lambda r: r["strike"])
    expiry = date.fromisoformat(snap["expiry"])
    mins = trading_minutes_to(expiry, now)
    t = years(mins)
    checks: list[dict] = []
    out = {"as_of": now.isoformat(timespec="seconds"), "expiry": snap["expiry"], "spot": spot,
           "zero_dte": expiry == now.date(), "minutes_left": round(mins, 1), "lot": snap.get("lot"),
           "checks": checks, "ok": False, "blockers": [], "legs": [], "sd": None}

    if filters:
        day = now.date().isoformat()
        checks.append(_check("Expiry day (0DTE)", out["zero_dte"], snap["expiry"], "today", True))
        skip = day in (skip_dates or [])
        checks.append(_check("Not an event day", not skip, "in skip list" if skip else "no", "skip list", True))
        v_now, v_prev = (vix or {}).get("now"), (vix or {}).get("prev")
        chg = (v_now - v_prev) / v_prev * 100 if v_now and v_prev else None
        out["vix"] = {"now": v_now, "prev": v_prev, "change_pct": chg}
        checks.append(_check("India VIX 11-20", (VIX_BAND[0] <= v_now <= VIX_BAND[1]) if v_now else None,
                             f"{v_now:.2f}" if v_now else "no data", "11-20", False))
        checks.append(_check("VIX rise ≤ 5%", (chg <= VIX_RISE_MAX) if chg is not None else None,
                             f"{chg:+.2f}%" if chg is not None else "no data", "≤ +5%", True, data=chg is None))
        p = ivp(v_now, (hist or {}).get("vix") or [])
        out["ivp"] = p
        checks.append(_check("IV percentile 30-85", (IVP_BAND[0] <= p <= IVP_BAND[1]) if p is not None else None,
                             f"{p:.0f}" if p is not None else "no data", "30-85", True, data=p is None))
        hv = hv10((hist or {}).get("nifty") or [])
        out["hv10"] = hv
        checks.append(_check("VIX above HV10", (v_now > hv) if v_now and hv else None,
                             f"{v_now:.2f} vs {hv:.2f}" if v_now and hv else "no data", "VIX > HV10", False))

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
                             ("no OI from the open yet" if no_open else "holding"), "OI ≥ open − 2%", True))
    pk = pick_strikes(spot, one, d_call, d_put, (walls.get("call") or {}).get("strike"),
                      (walls.get("put") or {}).get("strike"))
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
               max_loss=round((WING - credit) * qty, 2),
               breakevens=[round(pk["short_put"] - credit, 2), round(pk["short_call"] + credit, 2)])
    checks.append(_check("POP > 75%", p > POP_MIN, f"{p * 100:.1f}%", "> 75%", True))
    return _finish(out)


def _finish(out: dict) -> dict:
    failed = [c for c in out["checks"] if c["hard"] and c["ok"] is not True
              and not (c["ok"] is None and not c["data"])]
    out["blockers"] = [c["rule"] for c in failed]
    out["data_only"] = bool(failed) and all(c["data"] for c in failed)
    out["ok"] = not failed and bool(out["legs"])
    return out


# ---- market data ------------------------------------------------------------------------------------------

def _nse():
    return markets.get("IN")


def today_expiry(now: datetime) -> str | None:
    day = now.date().isoformat()
    try:
        return day if day in fno.expiries(UNDERLYING) else None
    except Exception as exc:
        log.warning("NIFTY expiries unavailable: %s", exc)
        return None


def next_expiry(now: datetime) -> str | None:
    try:
        exps = [e for e in fno.expiries(UNDERLYING) if e >= now.date().isoformat()]
    except Exception:
        return None
    return exps[0] if exps else None


def snapshot(expiry: str, now: datetime) -> dict:
    """NIFTY's chain for `expiry` with each leg's OI at today's open (oiflow), in evaluate()'s shape."""
    from . import oiflow
    oiflow.track(UNDERLYING, expiry)
    ch = fno.chain(UNDERLYING, expiry, CHAIN_WIDTH, fresh=True)
    spot = ch["spot"].get("ltp")
    if not spot:
        raise RuntimeError("No live NIFTY price")
    day = now.date().isoformat()
    rows = []
    for r in ch["rows"]:
        row = {"strike": float(r["strike"])}
        for k in ("ce", "pe"):
            leg = r.get(k)
            row[k] = None if not leg else {**{f: leg.get(f) for f in ("tsym", "bid", "ask", "ltp", "oi")},
                                           "oi_open": oiflow.oi_at_open(leg["tsym"], day)}
        rows.append(row)
    return {"spot": spot, "expiry": expiry, "lot": ch["lot"], "rows": rows}


def vix_quote() -> dict:
    """India VIX now and its previous close (Kotak)."""
    from . import kotak
    q = kotak.index_quotes(["India VIX"]).get("India VIX") or {}
    return {"now": q.get("ltp"), "prev": q.get("prev_close")}


def history(now: datetime) -> dict:
    """Daily closes before today: India VIX (about 2 years, for the IVP) and Nifty (for HV10), from Yahoo,
    fetched once a day and kept in DATA_DIR/condor_history.json."""
    path = config.DATA_DIR / "condor_history.json"
    day = now.date().isoformat()
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if cached.get("day") == day:
            return cached
    except (OSError, ValueError):
        pass
    import yfinance as yf
    out = {"day": day}
    for key, sym, period in (("vix", "^INDIAVIX", "2y"), ("nifty", "^NSEI", "3mo")):
        df = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=False)
        out[key] = [float(c) for d, c in zip(df.index, df["Close"]) if d.date().isoformat() < day and c == c]
    if len(out["vix"]) >= 200 and len(out["nifty"]) >= 11:
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


def plan_now(now: datetime | None = None, expiry: str | None = None) -> dict:
    """The rulebook evaluated on live data now, for the dashboard (any day: the nearest expiry by default)."""
    now = now or _nse().now()
    exp = expiry or today_expiry(now) or next_expiry(now)
    if not exp:
        raise RuntimeError("No NIFTY expiry found")
    vix, hist = {}, {}
    try:
        vix = vix_quote()
    except Exception as exc:
        log.warning("India VIX unavailable: %s", exc)
    try:
        hist = history(now)
    except Exception as exc:
        log.warning("VIX / Nifty history unavailable: %s", exc)
    return evaluate(snapshot(exp, now), now, vix, hist, skip_dates())


# ---- storage ----------------------------------------------------------------------------------------------

def _conn():
    con = state._conn()
    con.execute("""CREATE TABLE IF NOT EXISTS condor_days (day TEXT PRIMARY KEY, mode TEXT, status TEXT, reason TEXT,
        expiry TEXT, entry_at TEXT, exit_at TEXT, last_roll_at TEXT, rolls INTEGER DEFAULT 0, entry TEXT, pnl REAL,
        charges REAL, updated_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS condor_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, at TEXT,
        mode TEXT, tsym TEXT, opt TEXT, strike REAL, action TEXT, qty INTEGER, price REAL, charges REAL,
        order_id TEXT, why TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS condor_log (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, at TEXT,
        kind TEXT, text TEXT, data TEXT)""")
    return con


def _stamp() -> str:
    return _nse().now().isoformat(timespec="seconds")


def get_day(day: str) -> dict | None:
    with _conn() as con:
        r = con.execute("SELECT * FROM condor_days WHERE day = ?", (day,)).fetchone()
    return dict(r) if r else None


def _save_day(day: str, **fields) -> None:
    fields["updated_at"] = _stamp()
    with _conn() as con:
        con.execute("INSERT OR IGNORE INTO condor_days (day) VALUES (?)", (day,))
        con.execute(f"UPDATE condor_days SET {', '.join(f'{k} = ?' for k in fields)} WHERE day = ?",
                    (*fields.values(), day))


def _log(day: str, kind: str, text: str, data: dict | None = None, notify: bool = False, mode: str = "") -> None:
    log.info("Condor %s: %s", kind, text)
    with _conn() as con:
        con.execute("INSERT INTO condor_log (day, at, kind, text, data) VALUES (?, ?, ?, ?, ?)",
                    (day, _stamp(), kind, text, json.dumps(data, default=str) if data else None))
    if notify:
        try:
            tag = " (PAPER)" if mode == "paper" else ""
            telegram_bot.send_message(f"🦅 <b>0DTE condor{tag}</b> · {text}")
        except Exception:
            log.exception("Telegram notice for the condor failed")


def day_log(day: str, limit: int = 200) -> list[dict]:
    with _conn() as con:
        rows = con.execute("SELECT at, kind, text, data FROM condor_log WHERE day = ? ORDER BY id DESC LIMIT ?",
                           (day, limit)).fetchall()
    return [{**dict(r), "data": json.loads(r["data"]) if r["data"] else None} for r in rows]


def fills(day: str) -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute("SELECT * FROM condor_fills WHERE day = ? ORDER BY id", (day,))]


def position(day: str) -> dict:
    """Open legs: tsym -> {"qty" (signed: + long, - short), "opt", "strike"}."""
    pos: dict = {}
    for f in fills(day):
        p = pos.setdefault(f["tsym"], {"qty": 0, "opt": f["opt"], "strike": f["strike"]})
        p["qty"] += f["qty"] if f["action"] == "BUY" else -f["qty"]
    return {k: v for k, v in pos.items() if v["qty"]}


def pnl(day: str, marks: dict) -> dict:
    """Realised cash of every fill + open legs at `marks` (tsym -> price) - charges."""
    fl = fills(day)
    cash = sum(f["price"] * f["qty"] * (1 if f["action"] == "SELL" else -1) for f in fl)
    charges = sum(f["charges"] or 0 for f in fl)
    pos = position(day)
    unmarked = [t for t in pos if marks.get(t) is None]
    open_val = sum(p["qty"] * marks[t] for t, p in pos.items() if marks.get(t) is not None)
    return {"cash": round(cash, 2), "open_value": round(open_val, 2), "charges": round(charges, 2),
            "total": round(cash + open_val - charges, 2), "unmarked": unmarked}


def get_mode() -> str:
    st, _ = state.get_setting(MODE_KEY, {"mode": "paper"})
    m = str(st.get("mode", "paper"))
    return m if m in MODES else "paper"


def set_mode(mode: str, by: str) -> str:
    mode = str(mode).lower()
    if mode not in MODES:
        raise ValueError(f"Mode must be one of {', '.join(MODES)}")
    state.set_setting(MODE_KEY, {"mode": mode, "by": by})
    day = _nse().now().date().isoformat()
    _log(day, "mode", f"Mode set to {mode.upper()} by {by}", notify=True, mode=mode)
    return mode


# ---- orders -----------------------------------------------------------------------------------------------

def _quote(tsym: str) -> dict:
    return fno.option_quote(tsym)


def _fill(day: str, mode: str, tsym: str, action: str, qty: int, why: str) -> float:
    """Buy or sell `qty` of `tsym`: paper at the bid / ask, live with marketable limits re-priced up to
    FILL_TRIES times. Records the fills; raises ExecError if it can't be filled completely."""
    c = fno.contract(tsym) or {}
    opt, strike, tick = c.get("opt"), float(c.get("strike") or 0), float(c.get("tick") or 0.05)
    if mode != "live":
        q = _quote(tsym)
        px = (q.get("ask") if action == "BUY" else q.get("bid")) or q.get("ltp")
        if not px:
            raise ExecError(f"No price for {tsym}")
        _record_fill(day, mode, tsym, opt, strike, action, qty, float(px), None, why)
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
        res = broker.call("place_order", exchange_segment=fno.SEGMENT, product=PRODUCT, validity="DAY", amo="NO",
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
            _record_fill(day, mode, tsym, opt, strike, action, n, float(avg or px), oid, why)
            got, value, left = got + n, value + n * float(avg or px), left - n
        if left <= 0:
            return value / got
        if order_state(o) == "REJECTED":
            raise ExecError(f"{action} {tsym} rejected: {str((o or {}).get('rejRsn') or '')[:150]}")
    raise ExecError(f"{action} {left} of {tsym} not filled after {FILL_TRIES} tries")


def _record_fill(day, mode, tsym, opt, strike, action, qty, px, oid, why) -> None:
    with _conn() as con:
        con.execute("""INSERT INTO condor_fills (day, at, mode, tsym, opt, strike, action, qty, price, charges,
            order_id, why) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (day, _stamp(), mode, tsym, opt, strike, action, qty, px,
                     round(option_charges(action, px * qty), 2), oid, why))


def _trade_to(day: str, mode: str, target: dict, why: str) -> None:
    """Move the position to `target` (tsym -> signed qty): every buy first, then the sells."""
    cur = {t: p["qty"] for t, p in position(day).items()}
    diff = {t: target.get(t, 0) - cur.get(t, 0) for t in {*cur, *target}}
    for t in sorted(t for t, d in diff.items() if d > 0):
        _fill(day, mode, t, "BUY", diff[t], why)
    for t in sorted(t for t, d in diff.items() if d < 0):
        _fill(day, mode, t, "SELL", -diff[t], why)


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
    with _lock:
        now = now or _nse().now()
        day = now.date().isoformat()
        rec = get_day(day)
        pos = position(day)
        if pos:
            _manage(day, rec or {}, pos, now)
            return
        if rec and rec["status"] == OPEN:        # flat again (closed by hand or the last side exited)
            _close_day(day, CLOSED, rec.get("reason") or "All legs closed", now)
            return
        if not _nse().is_open(now) or (rec and rec["status"] in FINAL):
            return
        mode = get_mode()
        exp = today_expiry(now)
        if mode == "off" or exp is None:
            return
        if rec is None:
            _save_day(day, mode=mode, status=WAITING, expiry=exp, reason="Waiting for 09:45")
            _precheck(day, exp, mode, now)
            return
        if now.time() < ENTRY_FROM:
            return
        if now.time() >= ENTRY_TO:
            _save_day(day, status=SKIPPED, reason="No entry between 09:45 and 10:15")
            _log(day, "skip", "No entry between 09:45 and 10:15: " + (rec.get("reason") or ""), notify=True, mode=mode)
            return
        if _time.monotonic() - _mem["entry_try"] < ENTRY_RETRY_SEC:
            return
        _mem["entry_try"] = _time.monotonic()
        _try_entry(day, exp, mode, now)


def _precheck(day: str, exp: str, mode: str, now: datetime) -> None:
    """Before 09:45: the day filters, posted once; a failed day filter skips the day."""
    try:
        p = plan_now(now, exp)
    except Exception as exc:
        _log(day, "check", f"Pre-check: market data unavailable ({exc}); will try at 09:45")
        return
    _mem["plan"] = p
    day_rules = ("Expiry day (0DTE)", "Not an event day", "VIX rise ≤ 5%", "IV percentile 30-85")
    bad = [c for c in p["checks"] if c["rule"] in day_rules and c["ok"] is False]
    text = " · ".join(f"{'✅' if c['ok'] else ('⚠️' if not c['hard'] else '❌') if c['ok'] is False else '…'} "
                      f"{c['rule']}: {c['value']}" for c in p["checks"] if c["rule"] in day_rules
                      or c["rule"] in ("India VIX 11-20", "VIX above HV10"))
    if bad:
        _save_day(day, status=SKIPPED, reason="; ".join(f"{c['rule']} ({c['value']})" for c in bad))
        _log(day, "skip", "No trade today: " + "; ".join(f"{c['rule']} ({c['value']})" for c in bad),
             {"checks": p["checks"]}, notify=True, mode=mode)
    else:
        _log(day, "check", "Expiry day pre-check: " + text, {"checks": p["checks"]}, notify=True, mode=mode)


def _try_entry(day: str, exp: str, mode: str, now: datetime) -> None:
    try:
        p = plan_now(now, exp)
    except Exception as exc:
        _save_day(day, reason=f"Market data unavailable: {exc}")
        _log(day, "check", f"Entry: market data unavailable ({exc}); retrying")
        return
    _mem["plan"] = p
    if not p["ok"]:
        if p.get("data_only"):
            _save_day(day, reason="Waiting for data: " + ", ".join(p["blockers"]))
            _log(day, "check", "Entry: waiting for data (" + ", ".join(p["blockers"]) + ")", {"checks": p["checks"]})
            return
        bad = [c for c in p["checks"] if c["rule"] in p["blockers"]]
        why = "; ".join(f"{c['rule']} ({c['value']})" for c in bad)
        _save_day(day, status=SKIPPED, reason=why, entry=json.dumps(_summary(p)))
        _log(day, "skip", f"No trade today: {why}", _summary(p), notify=True, mode=mode)
        return
    if mode == "live" and not broker.trading_enabled():
        _save_day(day, status=SKIPPED, reason="Live mode but the Auto-trade switch is OFF")
        _log(day, "skip", "Live mode but the Auto-trade switch is OFF: no trade", notify=True, mode=mode)
        return
    s = _summary(p)
    try:
        _trade_to(day, mode, _target_of(p, p["qty"]), "entry")
    except Exception as exc:
        _log(day, "error", f"Entry failed ({exc}); closing whatever filled", s, notify=True, mode=mode)
        _flatten(day, mode, "entry failed")
        _save_day(day, status=FAILED, reason=f"Entry failed: {exc}")
        return
    _save_day(day, status=OPEN, mode=mode, entry_at=now.isoformat(timespec="seconds"), reason="Open",
              entry=json.dumps(s))
    t = p["targets"]
    _log(day, "entry",
         f"Entered: buy {t['long_put']:.0f} PE, sell {t['short_put']:.0f} PE, sell {t['short_call']:.0f} CE, "
         f"buy {t['long_call']:.0f} CE · credit {p['credit']:.2f} (₹{p['max_profit']:,.0f}) · max loss "
         f"₹{p['max_loss']:,.0f} · POP {p['pop'] * 100:.1f}% · 1SD {p['sd']['sd']:.0f} ({p['sd']['used']}) · "
         f"call by {t['call_by']}, put by {t['put_by']}", s, notify=True, mode=mode)


def _summary(p: dict) -> dict:
    """What gets logged for a decision: checks, 1SD A/B, candidates, legs, credit, POP."""
    return {k: p.get(k) for k in ("as_of", "spot", "expiry", "minutes_left", "vix", "ivp", "hv10", "sd", "candidates",
                                  "walls", "targets", "legs", "credit", "pop", "max_profit", "max_loss", "breakevens",
                                  "checks", "blockers")}


def _marks(pos: dict) -> dict:
    """Mid prices (or last) of the open legs."""
    out = {}
    for t in pos:
        try:
            q = _quote(t)
        except Exception as exc:
            log.debug("Quote for %s failed: %s", t, exc)
            continue
        out[t] = (q["bid"] + q["ask"]) / 2 if q.get("bid") and q.get("ask") else q.get("ltp")
    return out


def _manage(day: str, rec: dict, pos: dict, now: datetime) -> None:
    mode = rec.get("mode") or get_mode()
    marks = _marks(pos)
    pl = pnl(day, marks)
    _mem.update(pnl=pl, marks=marks, pnl_at=now.isoformat(timespec="seconds"))
    cap = config.CONDOR_LOSS_CAP
    if not pl["unmarked"] and pl["total"] <= -cap:
        _log(day, "cap", f"Daily loss cap hit: P&L ₹{pl['total']:,.0f} ≤ −₹{cap:,.0f}; closing everything",
             pl, notify=True, mode=mode)
        _exit_all(day, mode, STOPPED, f"Loss cap (₹{pl['total']:,.0f})", now)
        return
    if now.time() >= EXIT_FROM:
        _exit_all(day, mode, CLOSED, "Time exit (15:00)", now)
        return
    if not _nse().is_open(now) or _time.monotonic() - _mem["retarget_at"] < RETARGET_SEC:
        return
    _mem["retarget_at"] = _time.monotonic()
    try:
        _retarget(day, rec, pos, mode, now)
    except Exception as exc:
        log.exception("Condor re-target failed")
        _log(day, "error", f"Re-target failed: {exc}")


def _retarget(day: str, rec: dict, pos: dict, mode: str, now: datetime) -> None:
    exp = rec.get("expiry") or today_expiry(now)
    p = evaluate(snapshot(exp, now), now, filters=False)
    _mem["plan"] = p
    if not p.get("sd") or not p["sd"].get("sd") or not p.get("targets"):
        return
    spot, one, tg = p["spot"], p["sd"]["sd"], p["targets"]
    walls = p.get("walls") or {}
    for opt, side in (("CE", "call"), ("PE", "put")):
        legs = _side_legs(pos, opt)
        short = next((v["strike"] for v in legs.values() if v["qty"] < 0), None)
        if short is None:
            continue
        sig = roll_signal(opt, short, spot, one)
        why = None
        if sig == "out":
            lvl = spot + one if opt == "CE" else spot - one
            why = f"1SD level {lvl:.0f} is {abs(lvl - short):.0f} pts past the {short:.0f} {opt}"
        wall = (walls.get(side) or {}).get("strike")
        if sig is None and wall is not None and (wall > short if opt == "CE" else wall < short):
            sig, why = "out", f"{side} OI wall moved to {wall:.0f}, past the {short:.0f} {opt}"
        if sig is None:
            continue
        new_short = tg["short_call" if opt == "CE" else "short_put"]
        if now.time() >= ROLL_UNTIL:
            if sig == "out":
                _exit_side(day, mode, opt, f"after 14:30, {why}")
            continue
        if new_short == short:
            continue
        last = rec.get("last_roll_at")
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=COOLDOWN_MIN):
            _log(day, "roll", f"{opt} side wants to roll {sig} ({why or 'short beyond its 1SD level'}) but the "
                              f"10-minute cooldown is running")
            continue
        _roll(day, rec, pos, mode, p, opt, sig, why or f"short {short:.0f} is {IN_BUFFER}+ pts beyond its 1SD level",
              now)
        return   # one roll per re-target; the position changed


def _roll(day: str, rec: dict, pos: dict, mode: str, p: dict, opt: str, sig: str, why: str, now: datetime) -> None:
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
        _log(day, "roll", f"{opt} inward roll skipped: it would not bring a net credit (₹{roll_cash:,.0f})", info)
        return
    if pp <= POP_MIN:
        if sig == "out":
            _log(day, "roll", f"{opt} roll out can't meet POP > 75% ({pp * 100:.1f}%): closing that side",
                 info, notify=True, mode=mode)
            _exit_side(day, mode, opt, why)
        else:
            _log(day, "roll", f"{opt} inward roll skipped: POP {pp * 100:.1f}% ≤ 75%", info)
        return
    if mode == "live" and not broker.trading_enabled():
        _log(day, "roll", f"{opt} roll {sig} needed but the Auto-trade switch is OFF: closing that side instead",
             info, notify=True, mode=mode)
        _exit_side(day, mode, opt, why)
        return
    try:
        _trade_to(day, mode, target, f"roll {sig} {opt}")
    except Exception as exc:
        _log(day, "error", f"Roll failed ({exc}); closing everything", info, notify=True, mode=mode)
        _exit_all(day, mode, FAILED, f"Roll failed: {exc}", now)
        return
    _save_day(day, last_roll_at=now.isoformat(timespec="seconds"), rolls=int(rec.get("rolls") or 0) + 1)
    _log(day, "roll", f"Rolled the {opt} side {sig} to {'/'.join(f'{k:.0f}' for k in sorted(info['to']))} "
                      f"({why}) · cash ₹{roll_cash:,.0f} · POP {pp * 100:.1f}%", info, notify=True, mode=mode)


def _exit_side(day: str, mode: str, opt: str, why: str) -> None:
    pos = position(day)
    target = {t: v["qty"] for t, v in pos.items() if v["opt"] != opt}
    try:
        _trade_to(day, mode, target, f"exit {opt}")
    except Exception as exc:
        _log(day, "error", f"Closing the {opt} side failed ({exc}); retrying", notify=True, mode=mode)
        return
    _log(day, "exit", f"Closed the {opt} side: {why}", notify=True, mode=mode)


def _flatten(day: str, mode: str, why: str) -> bool:
    try:
        _trade_to(day, mode, {}, why)
        return True
    except Exception as exc:
        _log(day, "error", f"Closing everything failed ({exc}); retrying in {TICK_SEC} s", notify=True, mode=mode)
        return False


def _exit_all(day: str, mode: str, status: str, why: str, now: datetime) -> None:
    if _flatten(day, mode, why):
        _close_day(day, status, why, now)
    elif now.time() >= EXIT_BY and _mem["exit_warned"] != day:
        _mem["exit_warned"] = day
        _log(day, "error", "⚠️ Legs still open after 15:15: close them in the Kotak app", notify=True, mode=mode)


def _close_day(day: str, status: str, why: str, now: datetime) -> None:
    pl = pnl(day, {})
    rec = get_day(day) or {}
    _save_day(day, status=status, reason=why, exit_at=now.isoformat(timespec="seconds"), pnl=pl["total"],
              charges=pl["charges"])
    _mem.update(pnl=pl)
    _log(day, "exit", f"Day over ({why}): net P&L ₹{pl['total']:,.0f} after ₹{pl['charges']:,.0f} charges · "
                      f"{int(rec.get('rolls') or 0)} roll(s)", pl, notify=True, mode=rec.get("mode") or "")


def exit_now(by: str) -> dict:
    """Close every leg now (dashboard button)."""
    with _lock:
        now = _nse().now()
        day = now.date().isoformat()
        rec = get_day(day) or {}
        if not position(day):
            raise ValueError("No open condor legs today")
        _log(day, "exit", f"Close all requested by {by}", notify=True, mode=rec.get("mode") or "")
        _exit_all(day, rec.get("mode") or get_mode(), CLOSED, f"Closed by hand ({by})", now)
        return status()


# ---- dashboard --------------------------------------------------------------------------------------------

def status() -> dict:
    now = _nse().now()
    day = now.date().isoformat()
    rec = get_day(day)
    pos = position(day)
    marks = _mem["marks"] if pos else {}
    legs = [{"tsym": t, **v, "mark": marks.get(t)} for t, v in sorted(pos.items(), key=lambda kv: kv[1]["strike"])]
    with _conn() as con:
        hist = [dict(r) for r in con.execute(
            "SELECT day, mode, status, reason, rolls, pnl, charges FROM condor_days ORDER BY day DESC LIMIT 40")]
    return {"mode": get_mode(), "trading_enabled": broker.trading_enabled(), "now": now.isoformat(timespec="seconds"),
            "today": rec, "today_expiry": today_expiry(now), "next_expiry": next_expiry(now),
            "legs": legs, "pnl": pnl(day, marks) if (pos or fills(day)) else None,
            "fills": fills(day), "log": day_log(day), "plan": _mem["plan"], "history": hist,
            "skip_dates": skip_dates(),
            "rules": {"loss_cap": config.CONDOR_LOSS_CAP, "lots": LOTS, "wing": WING, "entry": "09:45-10:15",
                      "roll_until": "14:30", "exit": "15:00-15:15", "pop_min": POP_MIN * 100,
                      "out_buffer": OUT_BUFFER, "in_buffer": IN_BUFFER, "cooldown_min": COOLDOWN_MIN,
                      "vix_band": VIX_BAND, "vix_rise_max": VIX_RISE_MAX, "ivp_band": IVP_BAND, "delta": DELTA}}
