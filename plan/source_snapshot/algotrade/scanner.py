"""Scans the universe, combines strategy votes and builds trade suggestions."""
import logging
from dataclasses import dataclass, field

import pandas as pd

from . import config, markets
from .data import fetch_ohlcv
from .indicators import add_all
from .strategies import DEFAULT_STRATEGIES, FAMILIES, STRATEGIES, family_of
from .universe import get_universe

log = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    side: str                 # BUY / SELL
    entry: float
    stop_loss: float
    targets: list[float]
    qty: int
    score: int
    reasons: list[str]
    rsi: float
    adx: float
    vol_ratio: float
    bar_time: pd.Timestamp
    timeframe: str = "1d"
    market: str = "IN"
    entry_type: str = "market"  # "limit" (pullback) / "stop" (breakout) / "market" (older signals)
    ref_price: float = 0.0      # close of the signal bar, i.e. the price when the signal was found
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Identity used to avoid re-alerting the same setup: one per market/symbol/side/timeframe/day.
        India daily keys keep the original format (no market or timeframe) for older rows."""
        mk = "" if self.market == "IN" else f"{self.market}|"
        tf = "" if self.timeframe == "1d" else f"{self.timeframe}|"
        return f"{mk}{self.symbol}|{self.side}|{tf}{self.bar_time.date()}"


# Approximate bars per session (NSE 6h15m, US 6h30m), used to scale the daily turnover filter to intraday bars
BARS_PER_DAY = {"1d": 1, "1h": 7, "30m": 13, "15m": 25, "5m": 75}
# Structural stops (zone / level) are used only if they sit 0.3-3 ATR from entry
MIN_STOP_ATR, MAX_STOP_ATR = 0.3, 3.0
# Entries are pending orders away from the signal-bar close, so there is time to place them:
# bounce setups wait for a pullback (limit order), momentum setups for a break of the bar (stop order).
PULLBACK_STRATEGIES = {"RSI Pullback", "RSI(2) Reversion", "Support-Resistance", *FAMILIES}
PULLBACK_ATR = 0.5         # limit entry this far below (BUY) / above (SELL) the close
BREAKOUT_BUFFER_ATR = 0.1  # stop entry this far beyond the signal bar's high (BUY) / low (SELL)


# Filters veto signals instead of voting. Both use the market index (see Market.index_symbol).
FILTERS = {
    "trend": "Market trend: BUY only while the index is above its 200-bar EMA (SELL only below)",
    "rs": "Relative strength: BUY only stocks beating the index over the last 63 bars (SELL only laggards)",
}


def passes_filters(r: pd.Series, side: str, filters) -> bool:
    """Missing index data (NaN) never blocks a signal."""
    up = side == "BUY"
    if "trend" in filters and pd.notna(r["IDX_EMA200"]) and (r["IDX_CLOSE"] > r["IDX_EMA200"]) != up:
        return False
    if "rs" in filters and pd.notna(r["RS"]) and (r["RS"] > 1) != up:
        return False
    return True


def strategy_votes(df: pd.DataFrame, i: int, names) -> dict:
    """Raw vote of each named strategy on bar i: "BUY", "SELL", None or (side, stop)."""
    return {n: STRATEGIES[n](df, i) for n in names}


def entry_type_for(reasons: list[str]) -> str:
    """'limit' if bounce-type families are at least as many as momentum families, else 'stop'."""
    families = {family_of(n) for n in reasons}
    pullback = {family_of(n) for n in reasons if n in PULLBACK_STRATEGIES}
    return "limit" if len(pullback) >= len(families) - len(pullback) else "stop"


def evaluate(symbol: str, df: pd.DataFrame, i: int = -1, min_score: int | None = None,
             strategies: list[str] | None = None, timeframe: str = "1d",
             market: str = "IN", filters=None, votes: dict | None = None) -> Signal | None:
    """Evaluate the selected strategies (default: config.STRATEGIES) on bar i of an indicator DataFrame.
    votes: precomputed strategy_votes() for bar i (the backtester caches them)."""
    min_score = config.MIN_SCORE if min_score is None else min_score
    filters = config.FILTERS if filters is None else filters
    mkt = markets.get(market)
    selected = strategies or config.STRATEGIES or DEFAULT_STRATEGIES
    i = i if i >= 0 else len(df) + i
    if i < 200:
        return None
    r = df.iloc[i]

    # Liquidity filters
    min_turnover = mkt.min_turnover / BARS_PER_DAY.get(timeframe, 1)
    if r["Close"] < mkt.min_price or r["TURNOVER20"] < min_turnover:
        return None

    raw = votes if votes is not None else strategy_votes(df, i, selected)
    votes: dict[str, list[str]] = {"BUY": [], "SELL": []}
    stops: dict[str, list[float]] = {"BUY": [], "SELL": []}
    for name in selected:
        vote = raw[name]
        if isinstance(vote, tuple):
            vote, stop = vote
            stops[vote].append(float(stop))
        if vote:
            votes[vote].append(name)

    # Score = number of distinct strategy families agreeing (zone patterns count once)
    score = {s: len({family_of(n) for n in votes[s]}) for s in votes}
    side = max(score, key=score.get)
    reasons = votes[side]
    if score[side] < min_score or score["SELL" if side == "BUY" else "BUY"] >= score[side]:
        return None
    if side == "SELL" and not config.ALLOW_SHORTS:
        return None
    if filters and not passes_filters(r, side, filters):
        return None

    close = float(r["Close"])
    atr_val = float(r["ATR"])
    sign = 1 if side == "BUY" else -1
    entry_type = entry_type_for(reasons)
    if entry_type == "limit":
        entry = close - sign * PULLBACK_ATR * atr_val
    else:
        entry = float(r["High"] if side == "BUY" else r["Low"]) + sign * BREAKOUT_BUFFER_ATR * atr_val
    risk = config.ATR_SL_MULT * atr_val
    # Prefer a structural stop (zone / level) if it is on the right side and sensibly sized
    structural = [sign * (entry - s) for s in stops[side]]
    structural = [d for d in structural if MIN_STOP_ATR * atr_val <= d <= MAX_STOP_ATR * atr_val]
    if structural:
        risk = max(structural)  # widest valid structural stop is the safest
    if risk <= 0:
        return None
    sl = entry - sign * risk
    targets = [round(entry + sign * risk * m, 2) for m in config.TARGET_R_MULTIPLES]
    qty = int((mkt.capital * config.RISK_PER_TRADE_PCT / 100) // risk)
    qty = min(qty, int(mkt.capital // entry))  # never exceed capital

    return Signal(
        symbol=symbol, side=side, entry=round(entry, 2), stop_loss=round(sl, 2),
        targets=targets, qty=max(qty, 0), score=score[side], reasons=reasons,
        rsi=round(float(r["RSI"]), 1), adx=round(float(r["ADX"]), 1),
        vol_ratio=round(float(r["Volume"] / r["VOL_AVG20"]), 2) if r["VOL_AVG20"] else 0.0,
        bar_time=df.index[i], timeframe=timeframe, market=mkt.code,
        entry_type=entry_type, ref_price=round(close, 2),
    )


def fetch_index(timeframe: str, market: str, period: str | None = None) -> pd.Series | None:
    """Close of the market's index on this timeframe, or None if it can't be downloaded."""
    sym = markets.get(market).index_symbol
    df = fetch_ohlcv([sym], timeframe, period=period, market=market).get(sym)
    if df is None:
        log.warning("No %s index data; market filters are skipped", sym)
        return None
    return df["Close"]


def scan(symbols: list[str] | None = None, timeframe: str | None = None,
         strategies: list[str] | None = None, min_score: int | None = None,
         market: str = "IN", filters=None) -> list[Signal]:
    market = markets.get(market).code
    symbols = symbols or get_universe(market=market)
    timeframe = timeframe or config.TIMEFRAME
    unknown = set(strategies or config.STRATEGIES) - set(STRATEGIES)
    if unknown:
        raise ValueError(f"Unknown strategies: {', '.join(sorted(unknown))}")
    if timeframe not in BARS_PER_DAY:
        raise ValueError(f"Unsupported timeframe {timeframe}; use one of {', '.join(BARS_PER_DAY)}")
    unknown = set(filters or []) - set(FILTERS)
    if unknown:
        raise ValueError(f"Unknown filters: {', '.join(sorted(unknown))}")
    data = fetch_ohlcv(symbols, timeframe, market=market)
    index_close = fetch_index(timeframe, market)
    signals: list[Signal] = []
    for sym, df in data.items():
        try:
            sig = evaluate(sym, add_all(df, index_close), min_score=min_score, strategies=strategies,
                           timeframe=timeframe, market=market, filters=filters)
            if sig:
                signals.append(sig)
        except Exception as exc:
            log.warning("Error evaluating %s: %s", sym, exc)
    signals.sort(key=lambda s: (s.score, s.adx), reverse=True)
    log.info("%s scan complete (%s): %d signals", market, timeframe, len(signals))
    return signals


def scan_and_alert(send: bool = True, **scan_kw) -> dict:
    """Scan, keep only signals not seen before, send them to Telegram and record them.
    With send=False new signals are recorded (visible on the dashboard) without alerting.
    scan_kw is passed to scan() (timeframe, strategies, min_score, symbols, market, filters)."""
    from . import state, telegram_bot
    signals = scan(**scan_kw)
    new = state.filter_new(signals)
    log.info("%d signals, %d new", len(signals), len(new))
    delivered = telegram_bot.send_signals(new) if send else new
    state.record(delivered, telegram=send)
    return {"found": len(signals), "new": len(new), "sent": len(delivered) if send else 0,
            "symbols": [f"{s.side} {s.symbol}" for s in new]}
