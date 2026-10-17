"""P9: universe.get_universe / to_yahoo and data.fetch_ohlcv with a fake yf.download."""
import logging

import numpy as np
import pandas as pd
import pytest
import requests
import yfinance

from algotrade import config, data, universe
from conftest import FakeResponse, Recorder


# ---- universe --------------------------------------------------------------------------------

def test_comma_list():
    assert universe.get_universe(" reliance, tcs ,,m&m ") == ["RELIANCE", "TCS", "M&M"]
    assert universe.get_universe("aapl,brk.b", market="US") == ["AAPL", "BRK.B"]


def test_default_spec_from_config(monkeypatch):
    monkeypatch.setattr(config, "UNIVERSE", "a,b")
    monkeypatch.setattr(config, "US_UNIVERSE", "x")
    assert universe.get_universe() == ["A", "B"]
    assert universe.get_universe(market="US") == ["X"]


def test_nse_index_csv(monkeypatch):
    csv = "Company Name,Industry,Symbol,Series,ISIN Code\nReliance,Oil, RELIANCE ,EQ,X\nM&M,Auto,M&M,EQ,Y\n"
    get = Recorder(FakeResponse(200, text=csv))
    monkeypatch.setattr(requests, "get", get)
    assert universe.get_universe("nifty 50") == ["RELIANCE", "M&M"]
    assert get.calls == [(("https://archives.nseindia.com/content/indices/ind_nifty50list.csv",),
                          {"headers": universe.HEADERS, "timeout": 15})]


@pytest.mark.parametrize("resp", [FakeResponse(403), requests.ConnectionError("blocked")])
def test_nse_failure_falls_back_to_nifty50(monkeypatch, resp):
    def get(*a, **k):
        if isinstance(resp, Exception):
            raise resp
        return resp
    monkeypatch.setattr(requests, "get", get)
    out = universe.get_universe("NIFTY500")
    assert out is universe.NIFTY50_FALLBACK and len(out) == 50


def test_blocked_network_also_falls_back():
    """With the harness network guard, the default spec silently uses the fallback list."""
    assert universe.get_universe() is universe.NIFTY50_FALLBACK
    assert universe.get_universe(market="US") is universe.SP100_FALLBACK


def _html(n):
    rows = "".join(f"<tr><td> SYM{i} </td><td>Co {i}</td></tr>" for i in range(n))
    return f"<html><body><table><tr><th>Symbol</th><th>Name</th></tr>{rows}</table></body></html>"


def test_us_index_from_wikipedia(monkeypatch):
    get = Recorder(FakeResponse(200, text=_html(30)))
    monkeypatch.setattr(requests, "get", get)
    out = universe.get_universe("sp 500", market="US")
    assert out[:2] == ["SYM0", "SYM1"] and len(out) == 30
    assert get.calls[0][0] == ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",)


def test_us_small_table_falls_back(monkeypatch):
    monkeypatch.setattr(requests, "get", Recorder(FakeResponse(200, text=_html(10))))
    assert universe.get_universe("SP100", market="US") is universe.SP100_FALLBACK


def test_index_names_are_market_specific():
    """An NSE index name on the US market (or vice versa) is treated as a literal symbol list."""
    assert universe.get_universe("NIFTY50", market="US") == ["NIFTY50"]
    assert universe.get_universe("SP100", market="IN") == ["SP100"]


@pytest.mark.parametrize("sym,market,expected", [
    ("RELIANCE", "IN", "RELIANCE.NS"), ("M&M", "IN", "M&M.NS"), ("BRK.B", "US", "BRK-B"),
    ("AAPL", "US", "AAPL"), ("^NSEI", "IN", "^NSEI"), ("^GSPC", "US", "^GSPC"), ("BRK.B", "IN", "BRK.B.NS"),
])
def test_to_yahoo(sym, market, expected):
    assert universe.to_yahoo(sym, market) == expected


def test_fallback_lists():
    assert len(universe.SP100_FALLBACK) == 62 and "BRK.B" in universe.SP100_FALLBACK


# ---- data.fetch_ohlcv -------------------------------------------------------------------------

def ohlcv(n=70, start="2024-01-01 03:45", freq="D", tz="UTC", extra=False):
    idx = pd.date_range(start, periods=n, freq=freq, tz=tz)
    rng = np.random.default_rng(0)
    c = 100 + rng.normal(0, 1, n).cumsum()
    df = pd.DataFrame({"Open": c, "High": c + 1, "Low": c - 1, "Close": c, "Volume": 1000.0}, index=idx)
    if extra:
        df["Adj Close"] = c
    return df


def multi(frames: dict):
    return pd.concat(frames, axis=1)


@pytest.fixture
def yf(monkeypatch):
    def install(fn):
        rec = Recorder(fn)
        monkeypatch.setattr(yfinance, "download", rec)
        return rec
    return install


def test_multiindex_and_tz_conversion(yf):
    raw = multi({"RELIANCE.NS": ohlcv(extra=True), "TCS.NS": ohlcv()})
    rec = yf(lambda *a, **k: raw)
    out = data.fetch_ohlcv(["RELIANCE", "TCS"])
    assert set(out) == {"RELIANCE", "TCS"}
    df = out["RELIANCE"]
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.tz is None and df.index[0] == pd.Timestamp("2024-01-01 09:15")  # UTC 03:45 -> IST
    assert rec.calls == [((["RELIANCE.NS", "TCS.NS"],), dict(period="2y", interval="1d", group_by="ticker",
                                                             auto_adjust=True, progress=False, threads=True))]


def test_us_tickers_tz_and_period(yf):
    raw = multi({"BRK-B": ohlcv(start="2024-01-02 14:30", freq="15min")})
    rec = yf(lambda *a, **k: raw)
    out = data.fetch_ohlcv(["BRK.B"], "15m", market="US")
    assert out["BRK.B"].index[0] == pd.Timestamp("2024-01-02 09:30")
    assert rec.calls[0][0] == (["BRK-B"],) and rec.calls[0][1]["period"] == "60d"
    data.fetch_ohlcv(["BRK.B"], "1h", period="5d", market="US")
    assert rec.calls[1][1]["period"] == "5d" and rec.calls[1][1]["interval"] == "1h"
    data.fetch_ohlcv(["X"], "2h")
    assert rec.calls[2][1]["period"] == "1y"


def test_flat_frame_for_multi_ticker_batch_is_skipped_with_error(yf, caplog):
    """B3 fixed: a flat (non-MultiIndex) frame for a multi-ticker batch is ambiguous, so the batch is skipped."""
    flat = ohlcv(tz=None)
    yf(lambda *a, **k: flat)
    with caplog.at_level(logging.ERROR, logger="algotrade.data"):
        out = data.fetch_ohlcv(["A", "B", "C", "D"])
    assert out == {}
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "['A', 'B', 'C']" in errors[0].getMessage()
    assert "'D'" not in errors[0].getMessage()  # only the first few symbols are named


def test_flat_frame_accepted_for_single_ticker_batch(yf):
    flat = ohlcv(tz=None)
    yf(lambda *a, **k: flat)
    out = data.fetch_ohlcv(["A"])
    assert set(out) == {"A"} and out["A"].equals(flat)
    assert out["A"].index[0] == pd.Timestamp("2024-01-01 03:45")  # naive index left as is


def test_flat_frame_single_ticker_tz_converted_and_short_dropped(yf):
    yf(lambda *a, **k: ohlcv(extra=True))
    df = data.fetch_ohlcv(["A"])["A"]
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert df.index.tz is None and df.index[0] == pd.Timestamp("2024-01-01 09:15")
    yf(lambda *a, **k: ohlcv(59, tz=None))
    assert data.fetch_ohlcv(["A"]) == {}


def test_flat_frame_skips_only_its_batch(yf, caplog):
    """A flat frame for a multi-ticker batch skips that batch; other batches (and a flat 1-ticker batch) still load."""
    def dl(tickers, **k):
        if len(tickers) == 1:
            return ohlcv(tz=None)                       # flat, single ticker: fine
        if tickers == ["A.NS", "B.NS"]:
            return ohlcv(tz=None)                       # flat, two tickers: ambiguous
        return multi({t: ohlcv() for t in tickers})
    yf(dl)
    with caplog.at_level(logging.ERROR, logger="algotrade.data"):
        out = data.fetch_ohlcv(["A", "B", "C", "D", "E"], batch_size=2)
    assert set(out) == {"C", "D", "E"}
    assert sum(r.levelno == logging.ERROR for r in caplog.records) == 1


def test_short_frames_and_nan_rows_dropped(yf):
    nan59 = ohlcv(61)
    nan59.iloc[[3, 7], nan59.columns.get_loc("Close")] = np.nan
    raw = multi({"A.NS": ohlcv(60), "B.NS": ohlcv(59), "C.NS": nan59})
    yf(lambda *a, **k: raw)
    out = data.fetch_ohlcv(["A", "B", "C"])
    assert set(out) == {"A"} and len(out["A"]) == 60


def test_missing_ticker_skipped(yf):
    yf(lambda *a, **k: multi({"A.NS": ohlcv()}))
    assert set(data.fetch_ohlcv(["A", "MISSING"])) == {"A"}


def test_batch_errors_skip_only_that_batch(yf):
    def dl(tickers, **k):
        if tickers == ["A.NS"]:
            raise RuntimeError("yahoo down")
        return multi({t: ohlcv() for t in tickers})
    rec = yf(dl)
    out = data.fetch_ohlcv(["A", "B", "C"], batch_size=1)
    assert set(out) == {"B", "C"} and len(rec.calls) == 3


def test_batching(yf):
    rec = yf(lambda tickers, **k: multi({t: ohlcv() for t in tickers}))
    out = data.fetch_ohlcv([f"S{i}" for i in range(5)], batch_size=2)
    assert [c[0][0] for c in rec.calls] == [["S0.NS", "S1.NS"], ["S2.NS", "S3.NS"], ["S4.NS"]]
    assert len(out) == 5


def test_empty_symbols(yf):
    rec = yf(lambda *a, **k: pytest.fail("no download expected"))
    assert data.fetch_ohlcv([]) == {} and rec.calls == []


def test_period_for():
    assert data.PERIOD_FOR == {"1d": "2y", "1h": "730d", "30m": "60d", "15m": "60d", "5m": "60d"}
