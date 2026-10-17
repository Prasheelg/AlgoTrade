"""Option strategy maths: payoff at expiry and today (Black-Scholes), implied volatility, Greeks,
max profit / loss, breakevens, probability of profit, and the defined-risk rule for placing a strategy.

Pure functions, no I/O. A leg is a dict: opt "CE"/"PE", strike, side "BUY"/"SELL", qty (units = lots x lot
size), price (premium per unit paid / received), and optionally iv (annualised, e.g. 0.14).
NSE index and stock options are European, so Black-Scholes applies."""
import math

RISK_FREE = 0.065        # annual risk-free rate used for pricing (roughly the Indian T-bill yield)
MIN_T = 0.5 / 365        # treat the last half-day before expiry as the floor for time value


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def bs_price(opt: str, s: float, k: float, t: float, iv: float, r: float = RISK_FREE) -> float:
    """Black-Scholes value of a European option (t in years)."""
    if t <= 0 or iv <= 0:
        return max(s - k, 0.0) if opt == "CE" else max(k - s, 0.0)
    d1 = (math.log(s / k) + (r + iv * iv / 2) * t) / (iv * math.sqrt(t))
    d2 = d1 - iv * math.sqrt(t)
    if opt == "CE":
        return s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)
    return k * math.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1)


def implied_vol(opt: str, price: float, s: float, k: float, t: float, r: float = RISK_FREE) -> float | None:
    """Volatility that reproduces the market price (bisection), or None if the price is outside the model range."""
    if not price or price <= 0 or t <= 0:
        return None
    lo, hi = 1e-4, 5.0
    if price < bs_price(opt, s, k, t, lo, r) - 1e-9 or price > bs_price(opt, s, k, t, hi, r):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2
        if bs_price(opt, s, k, t, mid, r) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def greeks(opt: str, s: float, k: float, t: float, iv: float, r: float = RISK_FREE) -> dict:
    """Per unit: delta, gamma, theta per calendar day, vega per 1 volatility point."""
    t = max(t, MIN_T)
    if not iv or iv <= 0:
        itm = (s > k) if opt == "CE" else (s < k)
        return {"delta": (1.0 if itm else 0.0) * (1 if opt == "CE" else -1), "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sq = math.sqrt(t)
    d1 = (math.log(s / k) + (r + iv * iv / 2) * t) / (iv * sq)
    d2 = d1 - iv * sq
    gamma = _npdf(d1) / (s * iv * sq)
    vega = s * _npdf(d1) * sq / 100
    if opt == "CE":
        delta = _ncdf(d1)
        theta = -s * _npdf(d1) * iv / (2 * sq) - r * k * math.exp(-r * t) * _ncdf(d2)
    else:
        delta = _ncdf(d1) - 1
        theta = -s * _npdf(d1) * iv / (2 * sq) + r * k * math.exp(-r * t) * _ncdf(-d2)
    return {"delta": delta, "gamma": gamma, "theta": theta / 365, "vega": vega}


def _sign(leg: dict) -> int:
    return 1 if leg["side"] == "BUY" else -1


def payoff_expiry(legs: list[dict], s: float) -> float:
    """P&L in rupees at expiry if the underlying settles at s."""
    total = 0.0
    for g in legs:
        intrinsic = max(s - g["strike"], 0.0) if g["opt"] == "CE" else max(g["strike"] - s, 0.0)
        total += _sign(g) * g["qty"] * (intrinsic - g["price"])
    return total


def payoff_at(legs: list[dict], s: float, t: float) -> float:
    """P&L in rupees with t years left, each leg valued at its own implied volatility."""
    total = 0.0
    for g in legs:
        v = bs_price(g["opt"], s, g["strike"], t, g.get("iv") or 0.0)
        total += _sign(g) * g["qty"] * (v - g["price"])
    return total


def tail_slopes(legs: list[dict]) -> tuple[float, float]:
    """d(P&L)/d(price) far below all strikes and far above them, at expiry."""
    low = sum(-_sign(g) * g["qty"] for g in legs if g["opt"] == "PE")
    high = sum(_sign(g) * g["qty"] for g in legs if g["opt"] == "CE")
    return low, high


def defined_risk(legs: list[dict]) -> bool:
    """True if no option is sold uncovered: per type (CE / PE) the units bought are at least the units sold.
    Then the loss is capped on both sides (spreads, condors, butterflies); naked shorts and ratio spreads fail."""
    for opt in ("CE", "PE"):
        net = sum(_sign(g) * g["qty"] for g in legs if g["opt"] == opt)
        if net < 0:
            return False
    return True


def analyze(legs: list[dict], spot: float, days: float, span: float = 0.12, points: int = 241) -> dict:
    """Full picture of a single-expiry strategy: payoff curves, extremes, breakevens, Greeks, net premium."""
    if not legs:
        raise ValueError("Add at least one leg")
    t = max(days, 0) / 365
    strikes = [g["strike"] for g in legs]
    lo = min(spot * (1 - span), min(strikes) * 0.985)
    hi = max(spot * (1 + span), max(strikes) * 1.015)
    xs = [lo + (hi - lo) * i / (points - 1) for i in range(points)]
    exp_curve = [payoff_expiry(legs, x) for x in xs]
    now_curve = [payoff_at(legs, x, max(t, MIN_T)) for x in xs]

    # Extremes: the expiry payoff is piecewise linear, so its max / min over [0, inf) sit at 0, a strike,
    # or run off to infinity on the upside when the tail slope is non-zero.
    low_slope, high_slope = tail_slopes(legs)
    kinks = sorted({0.0, *strikes})
    vals = [payoff_expiry(legs, k) for k in kinks]
    max_profit = None if high_slope > 0 else max(vals)        # None = unlimited
    max_loss = None if high_slope < 0 else min(vals)

    # Breakevens: sign changes of the expiry payoff between kinks and beyond the last strike
    pts = kinks + [max(strikes) * 3]
    bes = []
    for a, b in zip(pts, pts[1:]):
        pa, pb = payoff_expiry(legs, a), payoff_expiry(legs, b)
        if pa == 0 and a > 0:
            bes.append(a)
        elif pa * pb < 0:
            bes.append(a + (b - a) * pa / (pa - pb))
    bes = sorted({round(x, 2) for x in bes})

    g = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        gl = greeks(leg["opt"], spot, leg["strike"], t, leg.get("iv") or 0.0)
        for k in g:
            g[k] += _sign(leg) * leg["qty"] * gl[k]

    net = sum(-_sign(leg) * leg["qty"] * leg["price"] for leg in legs)   # > 0 = credit received
    ivs = [leg["iv"] for leg in legs if leg.get("iv")]
    pop = probability_of_profit(legs, spot, t, sum(ivs) / len(ivs)) if ivs and t > 0 else None
    if net < 0:
        funds = -net                                     # debit: the premium paid
    else:
        funds = -max_loss if max_loss is not None and max_loss < 0 else None   # credit spread: about its max loss
    return {"x": [round(x, 2) for x in xs], "pnl_expiry": [round(v, 2) for v in exp_curve],
            "pnl_today": [round(v, 2) for v in now_curve], "max_profit": max_profit, "max_loss": max_loss,
            "breakevens": bes, "net_premium": round(net, 2), "greeks": {k: round(v, 4) for k, v in g.items()},
            "pop": pop, "defined_risk": defined_risk(legs), "funds_estimate": funds,
            "payoff_now_at_spot": round(payoff_at(legs, spot, max(t, MIN_T)), 2)}


def probability_of_profit(legs: list[dict], spot: float, t: float, iv: float, n: int = 400) -> float:
    """Chance the expiry payoff is positive if the underlying is lognormal (risk-neutral drift) at volatility iv."""
    sd = iv * math.sqrt(t)
    mu = math.log(spot) + (RISK_FREE - iv * iv / 2) * t
    total = 0.0
    for i in range(n):           # midpoints of n equal-probability buckets
        z = _inv_ncdf((i + 0.5) / n)
        if payoff_expiry(legs, math.exp(mu + sd * z)) > 0:
            total += 1
    return round(total / n, 3)


def _inv_ncdf(p: float) -> float:
    """Inverse standard normal CDF (Acklam's rational approximation, |error| < 1.2e-9)."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        return -_inv_ncdf(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
