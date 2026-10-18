"""Market data via Yahoo Finance (free, ~15 min delayed for intraday)."""
import logging

import pandas as pd
import yfinance as yf

from . import markets
from .universe import to_yahoo

log = logging.getLogger(__name__)

# How much history to pull per timeframe (enough for 200-period indicators)
PERIOD_FOR = {"1d": "2y", "1h": "730d", "30m": "60d", "15m": "60d", "5m": "60d"}


def fetch_ohlcv(symbols: list[str], timeframe: str = "1d", period: str | None = None,
                batch_size: int = 50, market: str = "IN", stop=None) -> dict[str, pd.DataFrame]:
    """Download OHLCV for many symbols. Returns {symbol: DataFrame[Open,High,Low,Close,Volume]}
    with naive timestamps in the market's exchange timezone. stop(), if given, is called before each batch
    and may raise to abandon the download (a batch itself can't be interrupted)."""
    tz = markets.get(market).tz
    period = period or PERIOD_FOR.get(timeframe, "1y")
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), batch_size):
        if stop:
            stop()
        batch = symbols[i:i + batch_size]
        tickers = [to_yahoo(s, market) for s in batch]
        try:
            raw = yf.download(tickers, period=period, interval=timeframe, group_by="ticker",
                              auto_adjust=True, progress=False, threads=True)
        except Exception as exc:
            log.error("Download failed for batch %s: %s", batch[:3], exc)
            continue
        flat = not isinstance(raw.columns, pd.MultiIndex)
        if flat and len(tickers) > 1:  # can't tell which symbol a flat frame belongs to
            log.error("Flat (non-MultiIndex) frame for multi-ticker batch %s; batch skipped", batch[:3])
            continue
        for sym, tkr in zip(batch, tickers):
            try:
                df = raw if flat else raw[tkr]
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
                if df.index.tz is not None:  # normalise to naive exchange-time timestamps
                    df.index = df.index.tz_convert(tz).tz_localize(None)
                if len(df) >= 60:
                    out[sym] = df
            except KeyError:
                log.debug("No data for %s", sym)
    log.info("Fetched data for %d/%d symbols", len(out), len(symbols))
    return out
