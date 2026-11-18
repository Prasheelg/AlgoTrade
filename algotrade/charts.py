"""Candles for the Chart view (/api/candles).

India, intraday: Kotak's own history, in real time, for NSE stocks, indices (NIFTY, BANKNIFTY, ...) and option
contracts (e.g. NIFTY26NOV25350CE). Kotak returns nothing at all when the date range is longer than it allows,
so each timeframe asks for no more than KOTAK_TF's days. Daily bars, the US, and stocks or indices Kotak can't
serve come from Yahoo (~15 min delayed intraday); an Indian stock's daily bar for today is Kotak's live quote.
An option has no Yahoo history: its daily bars are built from Kotak's 30-minute ones."""
import logging
import re
import threading
import time
from datetime import date, timedelta

import pandas as pd

from . import broker, fno, kotak, markets
from .universe import INDEX_YAHOO

log = logging.getLogger(__name__)

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "1d"]
# timeframe -> (Kotak interval, most days of history Kotak returns in one request)
KOTAK_TF = {"1m": ("1min", 7), "5m": ("5min", 20), "15m": ("15min", 40), "30m": ("30min", 60), "1h": ("60min", 60)}
KOTAK_INDEX_DAILY = ("D", 170)   # Kotak has daily bars for indices only (none for stocks)
YAHOO_PERIOD = {"1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d", "1h": "730d", "1d": "2y"}
INDEX_YAHOO_ALL = {**INDEX_YAHOO, "NIFTYNXT50": "^NSMIDCP"}
MAX_BARS = 1500
TTL_SEC = {"intraday": 5, "1d": 60}   # one Kotak / Yahoo round per chart for this long, however many tabs ask
KOTAK_GAP_SEC = 1.1                   # between Kotak history requests (it rate-limits back-to-back calls)

_cache: dict = {}
_lock = threading.Lock()
_last_call = [0.0]


def resolve(name: str, market: str = "IN") -> dict:
    """What a name in the chart box is: {"kind": "stock" | "index" | "option", "symbol", "label", ...}."""
    sym = name.strip().upper()
    if not sym:
        raise ValueError("Type a stock, an index (NIFTY, BANKNIFTY) or an option contract")
    if market == "US":
        return {"kind": "stock", "symbol": sym, "label": sym}
    alias = {v.upper(): k for k, v in fno.INDEX_QUOTE.items()}   # "NIFTY 50" -> NIFTY
    sym = alias.get(sym, sym)
    if sym in fno.INDEX_QUOTE:
        return {"kind": "index", "symbol": sym, "label": fno.INDEX_QUOTE[sym], "kotak": f"nse_cm|{fno.INDEX_QUOTE[sym]}"}
    if sym in kotak.tokens():
        return {"kind": "stock", "symbol": sym, "label": sym, "kotak": f"nse_cm|{kotak.tokens()[sym]}"}
    c = fno.contract(sym) or _nearest_contract(sym)
    if c:
        exp = pd.Timestamp(c["expiry"]).strftime("%d %b")
        sym = c["tsym"]
        return {"kind": "option", "symbol": sym, "label": f"{c['underlying']} {c['strike']:g} {c['opt']} · exp {exp}",
                "kotak": f"{fno.SEGMENT}|{c['token']}", "lot": int(c["lot"])}
    name = name.strip()
    if _kotak_index(name):   # any other index Kotak quotes, by its Kotak name ("Nifty IT", "INDIA VIX")
        return {"kind": "index", "symbol": name, "label": name, "kotak": f"nse_cm|{name}"}
    raise ValueError(f"{sym} isn't an NSE stock, index or option contract")


_known_index: dict[str, bool] = {}


def _kotak_index(name: str) -> bool:
    if name not in _known_index:
        if not kotak.enabled():
            return False
        _known_index[name] = kotak.index_quote(name) is not None
    return _known_index[name]


def _nearest_contract(text: str) -> dict | None:
    """"NIFTY 25350 CE" -> that strike's contract with the nearest expiry not yet past."""
    m = re.fullmatch(r"([A-Z0-9&-]+)\s+(\d+(?:\.\d+)?)\s*(CE|PE)", text)
    if not m:
        return None
    df = fno.contracts()
    hit = df[(df["underlying"] == m[1]) & (df["strike"] == float(m[2])) & (df["opt"] == m[3])
             & (pd.to_datetime(df["expiry"]) >= pd.Timestamp(date.today()))]
    return None if hit.empty else hit.sort_values("expiry").iloc[0].to_dict()


def _kotak_history(neosymbol: str, interval: str, days: int, today: date) -> pd.DataFrame:
    with _lock:
        wait = KOTAK_GAP_SEC - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        try:
            res = broker.call("historical_data", neosymbol=neosymbol, interval=interval,
                              from_date=str(today - timedelta(days=days)), to_date=str(today))
        finally:
            _last_call[0] = time.monotonic()
    rows = ((res or {}).get("data") or {}).get("candles") or []
    df = pd.DataFrame([r[:6] for r in rows], columns=["t", "Open", "High", "Low", "Close", "Volume"])
    if df.empty:
        return df
    df.index = pd.to_datetime(df.pop("t").str.slice(0, 19))   # naive IST
    df = df.astype(float)
    return df[df["High"] > 0].sort_index()    # a minute with no trade comes back as zeros


def _yahoo_history(ticker: str, tf: str, tz: str) -> pd.DataFrame:
    import yfinance as yf
    df = yf.download(ticker, period=YAHOO_PERIOD[tf], interval=tf, auto_adjust=True, progress=False)
    if df is None or df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if df.index.tz is not None:
        df.index = df.index.tz_convert(tz).tz_localize(None)
    return df


def _daily_from(df: pd.DataFrame) -> pd.DataFrame:
    return df.resample("1D").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last",
                                  "Volume": "sum"}).dropna()


def _fetch(inst: dict, tf: str, market: str) -> tuple[pd.DataFrame, str]:
    mkt = markets.get(market)
    today = mkt.now().date()
    use_kotak = market == "IN" and kotak.enabled() and "kotak" in inst
    kotak_tf = KOTAK_TF.get(tf) or (KOTAK_INDEX_DAILY if tf == "1d" and inst["kind"] == "index" else None)
    if use_kotak and kotak_tf:
        try:
            interval, days = kotak_tf
            df = _kotak_history(inst["kotak"], interval, days, today)
            if not df.empty:
                return df, "Kotak Neo, live"
        except Exception as exc:
            log.warning("Kotak history for %s %s failed (%s)", inst["symbol"], tf, exc)
    if inst["kind"] == "option":
        if not use_kotak:
            raise RuntimeError("Option charts need the Kotak login")
        if tf == "1d":
            df = _kotak_history(inst["kotak"], "30min", 60, today)
            if not df.empty:
                return _daily_from(df), "Kotak Neo, live (daily from 30 min bars)"
        raise RuntimeError(f"Kotak has no {tf} history for {inst['symbol']} (no trades yet?)")
    ticker = INDEX_YAHOO_ALL.get(inst["symbol"]) if inst["kind"] == "index" else None
    if inst["kind"] == "index" and not ticker:
        raise RuntimeError(f"No daily history for {inst['label']}")
    from .universe import to_yahoo
    df = _yahoo_history(ticker or to_yahoo(inst["symbol"], market), tf, mkt.tz)
    if df.empty:
        raise RuntimeError(f"No price history for {inst['symbol']}")
    source = "Yahoo" + ("" if tf == "1d" else ", ~15 min delayed")
    if tf == "1d" and use_kotak and inst["kind"] == "stock":
        q = kotak.quotes([inst["symbol"]]).get(inst["symbol"])
        if q:
            df = kotak.apply_live_bar(df, q, "1d")
            source = "Yahoo, today from Kotak live"
    return df, source


def candles(name: str, tf: str, market: str = "IN") -> dict:
    """{symbol, label, kind, tf, source, bars: [{t, o, h, l, c, v}]}, oldest first, at most MAX_BARS."""
    if tf not in TIMEFRAMES:
        raise ValueError(f"Timeframe must be one of {', '.join(TIMEFRAMES)}")
    market = markets.get(market).code
    inst = resolve(name, market)
    key = (market, inst["symbol"], tf)
    hit = _cache.get(key)
    ttl = TTL_SEC["1d" if tf == "1d" else "intraday"]
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    df, source = _fetch(inst, tf, market)
    df = df.tail(MAX_BARS)
    fmt = "%Y-%m-%d" if tf == "1d" else "%Y-%m-%dT%H:%M:%S"
    out = {"symbol": inst["symbol"], "label": inst["label"], "kind": inst["kind"], "tf": tf, "market": market,
           "source": source, "lot": inst.get("lot"),
           "bars": [{"t": ts.strftime(fmt), "o": round(float(r.Open), 2), "h": round(float(r.High), 2),
                     "l": round(float(r.Low), 2), "c": round(float(r.Close), 2), "v": float(r.Volume)}
                    for ts, r in df.iterrows()]}
    _cache[key] = (time.monotonic(), out)
    return out
