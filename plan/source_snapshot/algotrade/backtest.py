"""Simple bar-by-bar backtester for the combined signal logic.

Entry is the signal's pending order (pullback limit / breakout stop), filled if reached within
ENTRY_VALID_BARS bars (at the open on a gap); exit at stop-loss, target, or after `max_hold` bars.
If both SL and target are touched in the same bar, SL is assumed (conservative)."""
import logging

import pandas as pd

from . import config
from .data import fetch_ohlcv
from .indicators import add_all
from .scanner import evaluate, fetch_index, strategy_votes
from .strategies import DEFAULT_STRATEGIES
from .tracker import fill_price

log = logging.getLogger(__name__)


def vote_table(df: pd.DataFrame, names) -> dict[str, list]:
    """Every strategy's raw vote on every bar of an add_all() frame, so several strategy / filter
    combinations can be backtested from one pass over the data."""
    rows = [strategy_votes(df, i, names) if i >= 200 else {} for i in range(len(df))]
    return {n: [r.get(n) for r in rows] for n in names}


def backtest_symbol(symbol: str, df: pd.DataFrame, target_idx: int = 0, max_hold: int = 20,
                    min_score: int | None = None, strategies: list[str] | None = None,
                    market: str = "IN", filters=None, index_close: pd.Series | None = None,
                    votes: dict[str, list] | None = None) -> list[dict]:
    """df is raw OHLCV, or an add_all() frame when votes (from vote_table) are given."""
    if votes is None:
        df = add_all(df, index_close)
    names = strategies or config.STRATEGIES or DEFAULT_STRATEGIES
    trades, i = [], 200
    while i < len(df) - 1:
        bar_votes = {n: votes[n][i] for n in names} if votes is not None else None
        sig = evaluate(symbol, df, i, min_score, names, market=market, filters=filters, votes=bar_votes)
        if not sig:
            i += 1
            continue
        sign = 1 if sig.side == "BUY" else -1
        # Pending entry: must be reached within ENTRY_VALID_BARS bars, otherwise no trade
        f, fill = i, None
        for f in range(i + 1, min(i + 1 + config.ENTRY_VALID_BARS, len(df))):
            fill = fill_price(sig.side, sig.entry_type, sig.entry, df.iloc[f])
            if fill is not None:
                break
        if fill is None:
            i += 1
            continue
        risk = abs(fill - sig.stop_loss)
        target = sig.targets[target_idx]
        # Fill bar: only the stop is checked (intrabar order unknown)
        f_lo, f_hi = df["Low"].iloc[f], df["High"].iloc[f]
        if (f_lo <= sig.stop_loss if sign > 0 else f_hi >= sig.stop_loss):
            exit_px = sig.stop_loss if sign * (fill - sig.stop_loss) > 0 else fill
            trades.append({"symbol": symbol, "side": sig.side, "entry_type": sig.entry_type,
                           "entry_time": df.index[f], "exit_time": df.index[f], "entry": fill, "exit": exit_px,
                           "R": round(sign * (exit_px - fill) / risk, 2) if risk else 0.0, "score": sig.score})
            i = f + 1
            continue
        exit_px, j = None, f
        for j in range(f + 1, min(f + 1 + max_hold, len(df))):
            hi, lo = df["High"].iloc[j], df["Low"].iloc[j]
            hit_sl = lo <= sig.stop_loss if sign > 0 else hi >= sig.stop_loss
            hit_tg = hi >= target if sign > 0 else lo <= target
            if hit_sl:
                exit_px = sig.stop_loss
                break
            if hit_tg:
                exit_px = target
                break
        if exit_px is None:
            exit_px = float(df["Close"].iloc[j])
        r_mult = sign * (exit_px - fill) / risk if risk else 0.0
        trades.append({"symbol": symbol, "side": sig.side, "entry_type": sig.entry_type,
                       "entry_time": df.index[f], "exit_time": df.index[j], "entry": fill, "exit": exit_px,
                       "R": round(r_mult, 2), "score": sig.score})
        i = j + 1  # one position at a time per symbol
    return trades


def run(symbols: list[str], timeframe: str = "1d", period: str = "5y", market: str = "IN",
        **kw) -> pd.DataFrame:
    data = fetch_ohlcv(symbols, timeframe, period=period, market=market)
    index_close = fetch_index(timeframe, market, period=period)
    all_trades = []
    for sym, df in data.items():
        all_trades += backtest_symbol(sym, df, market=market, index_close=index_close, **kw)
    return pd.DataFrame(all_trades)


def summarize(trades: pd.DataFrame) -> str:
    if trades.empty:
        return "No trades generated."
    wins = trades[trades.R > 0]
    losses = trades[trades.R <= 0]
    pf = wins.R.sum() / abs(losses.R.sum()) if len(losses) and losses.R.sum() else float("inf")
    by_side = trades.groupby("side").R.agg(["count", "mean"]).round(2)
    return (
        f"Trades: {len(trades)}\n"
        f"Win rate: {len(wins) / len(trades):.1%}\n"
        f"Avg R/trade: {trades.R.mean():.2f}\n"
        f"Total R: {trades.R.sum():.1f}\n"
        f"Profit factor: {pf:.2f}\n\n"
        f"By side:\n{by_side.to_string()}"
    )
