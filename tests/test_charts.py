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
