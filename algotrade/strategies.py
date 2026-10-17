"""Trading strategies. Each one looks at bar `i` of an indicator-enriched DataFrame
and returns "BUY", "SELL", None, or (side, stop) when it has a structural stop-loss level.
Crossover strategies fire if the cross happened within the last `lookback` bars, so
confluence between strategies is possible.

Strategies in the same family (e.g. the supply/demand zone patterns) describe the same
market event, so they count as one vote when scoring."""
from typing import Callable, Optional, Union

import numpy as np
import pandas as pd

from .zones import levels_at

Vote = Union[str, tuple[str, float], None]
Strategy = Callable[[pd.DataFrame, int], Vote]
LOOKBACK = 3
STOP_BUFFER_ATR = 0.2   # structural stops sit this far beyond the zone / level


def _crossed_up(a: pd.Series, b: pd.Series, i: int, lookback: int = LOOKBACK) -> bool:
    for k in range(i - lookback + 1, i + 1):
        if k >= 1 and a.iloc[k - 1] <= b.iloc[k - 1] and a.iloc[k] > b.iloc[k]:
            return a.iloc[i] > b.iloc[i]  # still above now
    return False


def _crossed_down(a: pd.Series, b: pd.Series, i: int, lookback: int = LOOKBACK) -> bool:
    return _crossed_up(b, a, i, lookback)


def ema_crossover(df: pd.DataFrame, i: int) -> Optional[str]:
    """EMA 9/21 crossover in direction of EMA50."""
    c = df["Close"].iloc[i]
    if _crossed_up(df["EMA9"], df["EMA21"], i) and c > df["EMA50"].iloc[i]:
        return "BUY"
    if _crossed_down(df["EMA9"], df["EMA21"], i) and c < df["EMA50"].iloc[i]:
        return "SELL"
    return None


def breakout(df: pd.DataFrame, i: int) -> Optional[str]:
    """20-bar high/low breakout on 1.5x volume with trending ADX."""
    r = df.iloc[i]
    vol_ok = r["Volume"] > 1.5 * r["VOL_AVG20"]
    if r["ADX"] < 20 or not vol_ok:
        return None
    if r["Close"] > r["HH20"]:
        return "BUY"
    if r["Close"] < r["LL20"]:
        return "SELL"
    return None


def supertrend_flip(df: pd.DataFrame, i: int) -> Optional[str]:
    """Supertrend (10, 3) direction change within lookback."""
    d = df["ST_DIR"]
    for k in range(max(1, i - LOOKBACK + 1), i + 1):
        if d.iloc[k - 1] != d.iloc[k] and d.iloc[i] == d.iloc[k]:
            return "BUY" if d.iloc[i] > 0 else "SELL"
    return None


def macd_trend(df: pd.DataFrame, i: int) -> Optional[str]:
    """MACD/signal cross aligned with EMA200 trend."""
    c, e200 = df["Close"].iloc[i], df["EMA200"].iloc[i]
    if _crossed_up(df["MACD"], df["MACD_SIG"], i) and c > e200:
        return "BUY"
    if _crossed_down(df["MACD"], df["MACD_SIG"], i) and c < e200:
        return "SELL"
    return None


def rsi_pullback(df: pd.DataFrame, i: int) -> Optional[str]:
    """Buy the dip in an uptrend: RSI recovers above 40 while price > EMA200 (mirror for shorts)."""
    rsi = df["RSI"]
    c, e200 = df["Close"].iloc[i], df["EMA200"].iloc[i]
    forty = pd.Series(40.0, index=df.index)
    sixty = pd.Series(60.0, index=df.index)
    if c > e200 and _crossed_up(rsi, forty, i):
        return "BUY"
    if c < e200 and _crossed_down(rsi, sixty, i):
        return "SELL"
    return None


SQUEEZE_MIN_BARS = 5  # bars of Bollinger-inside-Keltner needed before a squeeze breakout counts


def squeeze_breakout(df: pd.DataFrame, i: int) -> Optional[str]:
    """Volatility squeeze release: Bollinger Bands inside Keltner Channels for 5+ bars, then a close outside the bands."""
    sq = df["SQUEEZE"].to_numpy()
    # A squeeze run of SQUEEZE_MIN_BARS bars ending within the last LOOKBACK bars (before this one)
    for end in range(i - 1, i - 1 - LOOKBACK, -1):
        start = end - SQUEEZE_MIN_BARS + 1
        if start >= 0 and sq[start:end + 1].all():
            break
    else:
        return None
    r = df.iloc[i]
    if r["Close"] > r["BB_UP"]:
        return "BUY"
    if r["Close"] < r["BB_LO"]:
        return "SELL"
    return None


def rsi2_reversion(df: pd.DataFrame, i: int) -> Optional[str]:
    """Short-term dip in a long-term uptrend: RSI(2) below 10 while price > EMA200 (mirror for shorts)."""
    r = df.iloc[i]
    if r["Close"] > r["EMA200"] and r["RSI2"] < 10:
        return "BUY"
    if r["Close"] < r["EMA200"] and r["RSI2"] > 90:
        return "SELL"
    return None


# ---- Supply / demand zones -------------------------------------------------

def _zone_retest(df: pd.DataFrame, i: int, kinds: set[str]) -> Vote:
    """Most recent fresh zone of the given kinds whose first retest happened in the last
    LOOKBACK bars and has held since. Stop goes beyond the zone's distal line."""
    c = df["Close"].to_numpy()
    by_touch = df.attrs.get("zones_by_touch", {})
    recent = [z for t in range(i, i - LOOKBACK, -1) for z in reversed(by_touch.get(t, []))]
    for z in recent:
        if z.kind not in kinds:
            continue
        since = c[z.touch:i + 1]
        if z.side == "BUY" and (since > z.distal).all():
            return "BUY", z.distal - STOP_BUFFER_ATR * df["ATR"].iloc[i]
        if z.side == "SELL" and (since < z.distal).all():
            return "SELL", z.distal + STOP_BUFFER_ATR * df["ATR"].iloc[i]
    return None


def demand_supply(df: pd.DataFrame, i: int) -> Vote:
    """First retest of a fresh demand zone (BUY) or supply zone (SELL) from any base pattern."""
    return _zone_retest(df, i, {"RBR", "DBR", "RBD", "DBD"})


def rally_base_rally(df: pd.DataFrame, i: int) -> Vote:
    """Rally → base → rally demand zone (trend continuation); BUY on first retest."""
    return _zone_retest(df, i, {"RBR"})


def drop_base_rally(df: pd.DataFrame, i: int) -> Vote:
    """Drop → base → rally demand zone (bullish reversal); BUY on first retest."""
    return _zone_retest(df, i, {"DBR"})


def drop_base_drop(df: pd.DataFrame, i: int) -> Vote:
    """Drop → base → drop supply zone (trend continuation); SELL on first retest."""
    return _zone_retest(df, i, {"DBD"})


def rally_base_drop(df: pd.DataFrame, i: int) -> Vote:
    """Rally → base → drop supply zone (bearish reversal); SELL on first retest."""
    return _zone_retest(df, i, {"RBD"})


# ---- Support / resistance ---------------------------------------------------

def support_resistance(df: pd.DataFrame, i: int) -> Vote:
    """Bounce off a support level (BUY) or rejection at resistance (SELL); levels need 2+ swing touches."""
    levels, touches = levels_at(df, i)
    if not len(levels):
        return None
    r = df.iloc[i]
    a = r["ATR"]
    o, h, l, c = r["Open"], r["High"], r["Low"], r["Close"]
    # Support: low dips to the level (not far through it) and the bar closes up, above the level
    sup = (l <= levels + 0.2 * a) & (l >= levels - 0.5 * a) & (c > levels + 0.2 * a) & (c > o)
    # Resistance: high reaches the level and the bar closes down, below the level
    res = (h >= levels - 0.2 * a) & (h <= levels + 0.5 * a) & (c < levels - 0.2 * a) & (c < o)
    if sup.any():
        lvl = levels[sup][np.argmax(touches[sup])]
        return "BUY", min(l, lvl) - STOP_BUFFER_ATR * a
    if res.any():
        lvl = levels[res][np.argmax(touches[res])]
        return "SELL", max(h, lvl) + STOP_BUFFER_ATR * a
    return None


def describe() -> list[dict]:
    price_action = set(FAMILIES) | {"Support-Resistance"}
    return [{"name": n, "description": (f.__doc__ or "").strip(), "family": family_of(n),
             "group": "Price action" if n in price_action else "Indicators", "default": n in DEFAULT_STRATEGIES}
            for n, f in STRATEGIES.items()]


ZONE_FAMILY = "Supply/Demand zone"
FAMILIES = {n: ZONE_FAMILY for n in
            ("Demand-Supply", "Rally-Base-Rally", "Drop-Base-Rally", "Rally-Base-Drop", "Drop-Base-Drop")}


def family_of(name: str) -> str:
    return FAMILIES.get(name, name)


STRATEGIES: dict[str, Strategy] = {
    "EMA 9/21 Cross": ema_crossover,
    "20-bar Breakout": breakout,
    "Supertrend Flip": supertrend_flip,
    "MACD Trend Cross": macd_trend,
    "RSI Pullback": rsi_pullback,
    "Support-Resistance": support_resistance,
    "Demand-Supply": demand_supply,
    "Rally-Base-Rally": rally_base_rally,
    "Drop-Base-Rally": drop_base_rally,
    "Rally-Base-Drop": rally_base_drop,
    "Drop-Base-Drop": drop_base_drop,
    "Squeeze Breakout": squeeze_breakout,
    "RSI(2) Reversion": rsi2_reversion,
}
# Opt-in strategies are off unless selected (dashboard) or listed in STRATEGIES in .env
OPT_IN = {"Squeeze Breakout", "RSI(2) Reversion"}
DEFAULT_STRATEGIES = [n for n in STRATEGIES if n not in OPT_IN]
