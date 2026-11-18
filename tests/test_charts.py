"""Chart view candles (charts.py, /api/candles). Kotak and Yahoo are faked."""
from datetime import date

import pandas as pd
import pytest

from algotrade import broker, charts, fno, kotak, web

CONTRACTS = pd.DataFrame([
    {"underlying": "NIFTY", "kind": "index", "expiry": date(2099, 1, 8), "strike": 25350.0, "opt": "CE", "lot": 65,
     "token": "111", "tsym": "NIFTY99JAN25350CE", "tick": 0.05},
    {"underlying": "NIFTY", "kind": "index", "expiry": date(2099, 1, 1), "strike": 25350.0, "opt": "CE", "lot": 65,
     "token": "222", "tsym": "NIFTY99JAN0125350CE", "tick": 0.05},
    {"underlying": "NIFTY", "kind": "index", "expiry": date(2000, 1, 1), "strike": 25350.0, "opt": "CE", "lot": 65,
     "token": "333", "tsym": "NIFTY00JAN25350CE", "tick": 0.05},
])


@pytest.fixture
def fakes(monkeypatch):
    calls = []
    candles = {"data": {"candles": [["2026-11-18T09:15:00+0530", 100, 105, 99, 104, 1000],
                                    ["2026-11-18T09:16:00+0530", 0, 0, 0, 0, 0],   # no trade that minute
                                    ["2026-11-18T09:20:00+0530", 104, 106, 103, 105, 500]]}}

    def call(method, **kw):
        calls.append((method, kw))
        return candles
    monkeypatch.setattr(broker, "call", call)
    monkeypatch.setattr(kotak, "enabled", lambda: True)
    monkeypatch.setattr(kotak, "tokens", lambda: {"RELIANCE": "2885"})
    monkeypatch.setattr(fno, "contracts", lambda: CONTRACTS)
    monkeypatch.setattr(charts, "_cache", {})
    monkeypatch.setattr(charts, "KOTAK_GAP_SEC", 0)
    monkeypatch.setattr(charts, "_known_index", {})
    monkeypatch.setattr(kotak, "index_quote", lambda name: {"ltp": 1.0} if name == "Nifty IT" else None)
    return calls, candles


def test_resolve_stock_index_and_option(fakes):
    assert charts.resolve("reliance")["kotak"] == "nse_cm|2885"
    assert charts.resolve("nifty")["kotak"] == "nse_cm|Nifty 50"
    assert charts.resolve("Nifty Bank")["symbol"] == "BANKNIFTY"
    opt = charts.resolve("NIFTY99JAN25350CE")
    assert opt["kind"] == "option" and opt["kotak"] == "nse_fo|111" and opt["lot"] == 65
    # "NIFTY 25350 CE" -> the nearest expiry not yet past
    assert charts.resolve("NIFTY 25350 CE")["symbol"] == "NIFTY99JAN0125350CE"
    with pytest.raises(ValueError):
        charts.resolve("NOSUCH")
    with pytest.raises(ValueError):
        charts.resolve("  ")


def test_intraday_comes_from_kotak_within_its_range_limit(fakes):
    calls, _ = fakes
    j = charts.candles("RELIANCE", "5m")
    assert j["source"] == "Kotak Neo, live" and j["kind"] == "stock"
    assert [b["t"] for b in j["bars"]] == ["2026-11-18T09:15:00", "2026-11-18T09:20:00"]   # zero bar dropped
    assert j["bars"][0] == {"t": "2026-11-18T09:15:00", "o": 100.0, "h": 105.0, "l": 99.0, "c": 104.0, "v": 1000.0}
    method, kw = calls[0]
    assert method == "historical_data" and kw["neosymbol"] == "nse_cm|2885" and kw["interval"] == "5min"
    days = (date.fromisoformat(kw["to_date"]) - date.fromisoformat(kw["from_date"])).days
    assert days == charts.KOTAK_TF["5m"][1]
    charts.candles("RELIANCE", "5m")
    assert len(calls) == 1   # cached


def test_empty_kotak_history_falls_back_to_yahoo_for_a_stock(fakes, monkeypatch):
    _, candles = fakes
    candles["data"]["candles"] = []
    idx = pd.date_range("2026-11-17 09:15", periods=3, freq="5min")
    yahoo = pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 10.0}, index=idx)
    seen = []
    monkeypatch.setattr(charts, "_yahoo_history", lambda t, tf, tz: seen.append((t, tf)) or yahoo)
    j = charts.candles("RELIANCE", "5m")
    assert seen == [("RELIANCE.NS", "5m")] and j["source"].startswith("Yahoo") and len(j["bars"]) == 3


def test_option_daily_is_built_from_kotak_30_min_bars(fakes):
    calls, _ = fakes
    j = charts.candles("NIFTY99JAN25350CE", "1d")
    assert calls[0][1]["interval"] == "30min"
    assert j["bars"] == [{"t": "2026-11-18", "o": 100.0, "h": 106.0, "l": 99.0, "c": 105.0, "v": 1500.0}]


def test_option_without_kotak_history_says_so(fakes):
    _, candles = fakes
    candles["data"]["candles"] = []
    with pytest.raises(RuntimeError, match="no 5m history"):
        charts.candles("NIFTY99JAN25350CE", "5m")


def test_api_candles(fakes):
    c = web.app.test_client()
    j = c.get("/api/candles?s=RELIANCE&tf=15m&market=IN").get_json()
    assert j["symbol"] == "RELIANCE" and len(j["bars"]) == 2
    assert c.get("/api/candles?s=RELIANCE&tf=7m&market=IN").status_code == 400
    assert c.get("/api/candles?s=NOSUCH&tf=5m&market=IN").status_code == 400


class TickFeed:
    """Scripted feed: each wait() moves the charted instrument to the next price (None: no change)."""

    def __init__(self, prices):
        self.prices, self.n, self.changed, self.touched, self.q = list(prices), 0, 0, [], {"ltp": 100.0, "last_volume": 10}

    def connected(self):
        return bool(self.prices)

    def seq(self):
        return self.n

    def touch(self, keys):
        self.touched.append(list(keys))

    def lookup(self, seg, toks):
        assert seg == "nse_fo" and toks == ["111"]
        return {"111": self.q}

    def wait(self, timeout):
        px = self.prices.pop(0)
        if px is not None:
            self.n += 1
            self.changed = self.n
            self.q = {**self.q, "ltp": px, "last_volume": self.q["last_volume"] + 5}

    def changed_since(self, since, keys):
        return self.n, keys if self.changed > since else []


def test_chart_ticks_streams_the_instruments_price(monkeypatch):
    from datetime import datetime
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    monkeypatch.setattr(type(web.markets.get("IN")), "now", lambda self: datetime(2026, 11, 18, 10, 7, 30))
    out = list(web.chart_ticks("nse_fo|111", feed=TickFeed([101.5, None, 102.0])))
    data = [line for line in out if line.startswith("data: {")]
    assert data == ['data: {"ltp": 100.0, "vol": 10.0, "t": "2026-11-18T10:07:30"}\n\n',   # current price first
                    'data: {"ltp": 101.5, "vol": 15.0, "t": "2026-11-18T10:07:30"}\n\n',
                    'data: {"ltp": 102.0, "vol": 20.0, "t": "2026-11-18T10:07:30"}\n\n']
    assert out[-1] == "event: down\ndata: 0\n\n"


def test_api_candles_live_needs_the_stream(fakes, monkeypatch):
    from algotrade import livefeed
    monkeypatch.setattr(livefeed, "connected", lambda: False)
    c = web.app.test_client()
    assert c.get("/api/candles/live?s=RELIANCE").status_code == 503
    monkeypatch.setattr(livefeed, "connected", lambda: True)
    assert c.get("/api/candles/live?s=NOSUCH").status_code == 400


def test_any_kotak_index_by_name_and_index_daily_from_kotak(fakes):
    calls, _ = fakes
    inst = charts.resolve("Nifty IT")
    assert inst == {"kind": "index", "symbol": "Nifty IT", "label": "Nifty IT", "kotak": "nse_cm|Nifty IT"}
    j = charts.candles("Nifty IT", "1d")
    assert calls[-1][1]["interval"] == "D" and calls[-1][1]["neosymbol"] == "nse_cm|Nifty IT"
    assert j["source"] == "Kotak Neo, live" and j["bars"][0]["t"] == "2026-11-18"
