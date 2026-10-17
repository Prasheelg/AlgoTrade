"""Stock universe: NSE / US index constituents (fetched live, with an offline fallback)."""
import io
import logging

import pandas as pd
import requests

from . import config

log = logging.getLogger(__name__)

INDEX_CSV = {
    "NIFTY50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "NIFTY500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}

# Fallback if NSE blocks the request
NIFTY50_FALLBACK = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA",
    "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC", "JIOFIN",
    "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA",
    "TATACONSUM", "TATAMOTORS", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT",
    "ULTRACEMCO", "WIPRO", "HEROMOTOCO",
]


# US indexes come from Wikipedia's constituent tables
US_INDEX_PAGE = {
    "SP100": "https://en.wikipedia.org/wiki/S%26P_100",
    "SP500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
}

SP100_FALLBACK = [
    "AAPL", "ABBV", "ABT", "ACN", "ADBE", "AMD", "AMGN", "AMZN", "AVGO", "AXP", "BA", "BAC",
    "BKNG", "BLK", "BRK.B", "C", "CAT", "CMCSA", "COP", "COST", "CRM", "CSCO", "CVX", "DIS",
    "GE", "GOOGL", "GS", "HD", "HON", "IBM", "INTC", "INTU", "JNJ", "JPM", "KO", "LLY",
    "LOW", "MA", "MCD", "META", "MRK", "MS", "MSFT", "NFLX", "NKE", "NVDA", "ORCL", "PEP",
    "PFE", "PG", "QCOM", "T", "TMO", "TSLA", "TXN", "UNH", "UNP", "V", "VZ", "WFC", "WMT", "XOM",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _fetch_us_index(name: str) -> list[str]:
    resp = requests.get(US_INDEX_PAGE[name], headers=HEADERS, timeout=15)
    resp.raise_for_status()
    for df in pd.read_html(io.StringIO(resp.text)):
        if "Symbol" in df.columns and len(df) >= 25:
            return df["Symbol"].astype(str).str.strip().tolist()
    raise ValueError("constituent table not found")


def _fetch_index(name: str) -> list[str]:
    url = INDEX_CSV[name]
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    return df["Symbol"].str.strip().tolist()


_sectors: dict = {"day": None, "map": {}}


def sectors() -> dict[str, str]:
    """NSE symbol -> industry (from the NIFTY 500 list), refreshed once a day and cached in DATA_DIR.
    {} if NSE can't be reached and there is no cached copy."""
    from datetime import date
    today = date.today()
    if _sectors["day"] == today:
        return _sectors["map"]
    cache = config.DATA_DIR / "nse_sectors.csv"
    df = None
    if cache.exists() and date.fromtimestamp(cache.stat().st_mtime) == today:
        df = pd.read_csv(cache)
    else:
        try:
            resp = requests.get(INDEX_CSV["NIFTY500"], headers=HEADERS, timeout=15)
            resp.raise_for_status()
            df = pd.read_csv(io.StringIO(resp.text))
            df[["Symbol", "Industry"]].to_csv(cache, index=False)
        except Exception as exc:
            log.warning("Could not fetch NSE sectors (%s)", exc)
            if cache.exists():
                df = pd.read_csv(cache)
    out = {} if df is None else {str(s).strip(): str(i).strip() for s, i in zip(df["Symbol"], df["Industry"])}
    _sectors.update(day=today, map=out)
    return out


def get_universe(spec: str | None = None, market: str = "IN") -> list[str]:
    """Return a list of symbols as the exchange writes them (NSE: without .NS suffix)."""
    spec = (spec or (config.US_UNIVERSE if market == "US" else config.UNIVERSE)).strip()
    key = spec.upper().replace(" ", "").replace("&", "")
    if market == "US" and key in US_INDEX_PAGE:
        try:
            symbols = _fetch_us_index(key)
            log.info("Loaded %d symbols for %s", len(symbols), key)
            return symbols
        except Exception as exc:
            log.warning("Could not fetch %s list (%s); using S&P 100 fallback", key, exc)
            return SP100_FALLBACK
    if market != "US" and key in INDEX_CSV:
        try:
            symbols = _fetch_index(key)
            log.info("Loaded %d symbols for %s", len(symbols), key)
            return symbols
        except Exception as exc:  # network / NSE blocking
            log.warning("Could not fetch %s list (%s); using NIFTY50 fallback", key, exc)
            return NIFTY50_FALLBACK
    return [s.strip().upper() for s in spec.split(",") if s.strip()]


def to_yahoo(symbol: str, market: str = "IN") -> str:
    if symbol.startswith("^"):  # an index, already a Yahoo ticker
        return symbol
    if market == "US":
        return symbol.replace(".", "-")  # BRK.B -> BRK-B
    return f"{symbol}.NS"
