"""Price-action structures: supply/demand zones (base patterns) and support/resistance levels.

Zones
-----
A *base* is 1-4 "boring" candles (body <= 50% of range) between two "explosive" candles
(body >= 60% of range and range >= 1.1x ATR). The legs give the pattern name:

    leg-in  leg-out   pattern   zone
    rally   rally     RBR       demand (continuation)
    drop    rally     DBR       demand (reversal)
    drop    drop      DBD       supply (continuation)
    rally   drop      RBD       supply (reversal)

Demand zone: distal = lowest low of the base, proximal = highest candle body in the base.
Supply zone: distal = highest high of the base, proximal = lowest candle body in the base.
A zone is only traded on its first return (fresh zone), and only if it is still within
MAX_ZONE_AGE bars of forming.

Support / resistance
--------------------
Swing highs/lows are bars that are the extreme of the PIVOT_K bars on each side (only known
PIVOT_K bars later). A level is a pivot price with at least MIN_TOUCHES pivots within
LEVEL_TOL x ATR of it, over the last SR_WINDOW bars.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

BORING_BODY = 0.5
EXPLOSIVE_BODY = 0.6
EXPLOSIVE_RANGE_ATR = 1.1
MAX_BASE = 4
MAX_ZONE_HEIGHT_ATR = 2.0
MAX_ZONE_AGE = 120

PIVOT_K = 5
SR_WINDOW = 250
LEVEL_TOL = 0.3
MIN_TOUCHES = 2


@dataclass
class Zone:
    kind: str          # RBR / DBR / DBD / RBD
    side: str          # BUY (demand) / SELL (supply)
    formed: int        # index of leg-out candle (zone known at its close)
    proximal: float
    distal: float
    touch: int = -1    # first bar that returned into the zone, -1 if never
    held: bool = False  # touch bar closed on the right side of the distal line with rejection


def find_zones(df: pd.DataFrame) -> list[Zone]:
    o, h, l, c = (df[k].to_numpy(float) for k in ("Open", "High", "Low", "Close"))
    atr = df["ATR"].shift(1).bfill().to_numpy(float)
    rng = h - l
    body = np.abs(c - o)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(rng > 0, body / rng, 0.0)
    boring = (ratio <= BORING_BODY) & (rng > 0)
    explosive = (ratio >= EXPLOSIVE_BODY) & (rng >= EXPLOSIVE_RANGE_ATR * atr)
    up = c > o
    n = len(df)

    zones: list[Zone] = []
    for out in range(2, n):
        if not explosive[out]:
            continue
        # walk back over the base
        j = out - 1
        while j >= 0 and boring[j] and out - j <= MAX_BASE:
            j -= 1
        base_len = out - 1 - j
        if base_len < 1 or j < 0 or not explosive[j] or boring[j]:
            continue
        base = slice(j + 1, out)
        leg_in_up, leg_out_up = up[j], up[out]
        if leg_out_up:
            distal, proximal = l[base].min(), np.maximum(o[base], c[base]).max()
            if c[out] <= h[base].max():          # leg-out must leave the base
                continue
            kind, side = ("RBR" if leg_in_up else "DBR"), "BUY"
        else:
            distal, proximal = h[base].max(), np.minimum(o[base], c[base]).min()
            if c[out] >= l[base].min():
                continue
            kind, side = ("RBD" if leg_in_up else "DBD"), "SELL"
        height = abs(proximal - distal)
        if height <= 0 or height > MAX_ZONE_HEIGHT_ATR * atr[out]:
            continue
        z = Zone(kind, side, out, float(proximal), float(distal))

        # first return into the zone
        end = min(n, out + 1 + MAX_ZONE_AGE)
        seg = slice(out + 1, end)
        entered = (l[seg] <= proximal) if side == "BUY" else (h[seg] >= proximal)
        if entered.any():
            t = out + 1 + int(np.argmax(entered))
            mid = (h[t] + l[t]) / 2
            z.touch = t
            z.held = (c[t] > distal and c[t] >= mid) if side == "BUY" else (c[t] < distal and c[t] <= mid)
        zones.append(z)
    return zones


def find_pivots(df: pd.DataFrame, k: int = PIVOT_K) -> tuple[np.ndarray, np.ndarray]:
    """Return (pivot_index, pivot_price) for all swing highs and lows, sorted by index."""
    h, l = df["High"].to_numpy(float), df["Low"].to_numpy(float)
    win = 2 * k + 1
    hmax = pd.Series(h).rolling(win, center=True).max().to_numpy()
    lmin = pd.Series(l).rolling(win, center=True).min().to_numpy()
    hi_idx = np.flatnonzero(h == hmax)
    lo_idx = np.flatnonzero(l == lmin)
    idx = np.concatenate([hi_idx, lo_idx])
    price = np.concatenate([h[hi_idx], l[lo_idx]])
    order = np.argsort(idx, kind="stable")
    return idx[order], price[order]


def levels_at(df: pd.DataFrame, i: int) -> tuple[np.ndarray, np.ndarray]:
    """S/R levels known at bar i: (price, touches) for pivots touched >= MIN_TOUCHES times."""
    idx, price = df.attrs["pivots"]
    known = (idx <= i - PIVOT_K) & (idx >= i - SR_WINDOW)
    p = price[known]
    if len(p) < MIN_TOUCHES:
        return np.array([]), np.array([])
    tol = LEVEL_TOL * df["ATR"].iloc[i]
    touches = (np.abs(p[:, None] - p[None, :]) <= tol).sum(axis=1)
    strong = touches >= MIN_TOUCHES
    return p[strong], touches[strong]


def annotate(df: pd.DataFrame) -> pd.DataFrame:
    """Attach zones and pivots to df.attrs (requires ATR column)."""
    zones = find_zones(df)
    by_touch: dict[int, list[Zone]] = {}
    for z in zones:  # index retested zones by their first-touch bar for fast lookup
        if z.touch >= 0 and z.held:
            by_touch.setdefault(z.touch, []).append(z)
    df.attrs["zones"] = zones
    df.attrs["zones_by_touch"] = by_touch
    df.attrs["pivots"] = find_pivots(df)
    return df
