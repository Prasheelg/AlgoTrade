"""Technical indicators implemented in pure pandas."""
import numpy as np
import pandas as pd

from .zones import annotate


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["Close"].shift()
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - prev_close).abs(),
        (df["Low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["High"].diff()
    down = -df["Low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    a = atr(df, n)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.Series:
    """Returns direction series: +1 uptrend, -1 downtrend."""
    a = atr(df, n)
    hl2 = (df["High"] + df["Low"]) / 2
    upper = (hl2 + mult * a).values
    lower = (hl2 - mult * a).values
    close = df["Close"].values
    direction = np.ones(len(df))
    fu, fl = upper.copy(), lower.copy()
    for i in range(1, len(df)):
        fu[i] = upper[i] if upper[i] < fu[i - 1] or close[i - 1] > fu[i - 1] else fu[i - 1]
        fl[i] = lower[i] if lower[i] > fl[i - 1] or close[i - 1] < fl[i - 1] else fl[i - 1]
        if close[i] > fu[i - 1]:
            direction[i] = 1
        elif close[i] < fl[i - 1]:
            direction[i] = -1
        else:
            direction[i] = direction[i - 1]
    return pd.Series(direction, index=df.index)


RS_BARS = 63  # relative-strength lookback (about 3 months of daily bars)
SQUEEZE_KC_MULT = 1.5  # Keltner channel = EMA20 +/- 1.5 ATR


def add_all(df: pd.DataFrame, index_close: pd.Series | None = None) -> pd.DataFrame:
    """Add indicator columns. index_close (the market index on the same timeframe) enables
    the market-trend and relative-strength columns; without it they are NaN."""
    df = df.copy()
    c = df["Close"]
    df["EMA9"] = ema(c, 9)
    df["EMA21"] = ema(c, 21)
    df["EMA50"] = ema(c, 50)
    df["EMA200"] = ema(c, 200)
    df["RSI"] = rsi(c)
    df["MACD"], df["MACD_SIG"], df["MACD_HIST"] = macd(c)
    df["ATR"] = atr(df)
    df["ADX"] = adx(df)
    df["ST_DIR"] = supertrend(df)
    df["VOL_AVG20"] = sma(df["Volume"], 20)
    df["TURNOVER20"] = sma(c * df["Volume"], 20)
    df["HH20"] = df["High"].rolling(20).max().shift(1)
    df["LL20"] = df["Low"].rolling(20).min().shift(1)
    mid = sma(c, 20)
    sd = c.rolling(20).std()
    df["BB_UP"], df["BB_LO"] = mid + 2 * sd, mid - 2 * sd
    df["RSI2"] = rsi(c, 2)
    ema20 = ema(c, 20)
    df["KC_UP"], df["KC_LO"] = ema20 + SQUEEZE_KC_MULT * df["ATR"], ema20 - SQUEEZE_KC_MULT * df["ATR"]
    df["SQUEEZE"] = (df["BB_UP"] < df["KC_UP"]) & (df["BB_LO"] > df["KC_LO"])  # volatility contraction
    if index_close is not None and len(index_close):
        idx = index_close.reindex(df.index, method="ffill")
        df["IDX_CLOSE"], df["IDX_EMA200"] = idx, ema(index_close, 200).reindex(df.index, method="ffill")
        # > 1: the stock has outperformed the index over RS_BARS bars
        df["RS"] = (c / c.shift(RS_BARS)) / (idx / idx.shift(RS_BARS))
    else:
        df["IDX_CLOSE"] = df["IDX_EMA200"] = df["RS"] = float("nan")
    return annotate(df)
