"""P7: Flask API through app.test_client(); scan_and_alert and tracker.update_all are faked."""
from types import SimpleNamespace

import pandas as pd
import pytest

from algotrade import config, markets, state, tracker, web
from algotrade.strategies import DEFAULT_STRATEGIES
from conftest import Recorder, make_signal


@pytest.fixture(autouse=True)
def _web_state(monkeypatch):
    monkeypatch.setitem(web._last_refresh, "at", None)
    monkeypatch.setattr(tracker, "update_all", lambda *a, **k: pytest.fail("unexpected update_all"))
    monkeypatch.setattr(web, "scan_and_alert", lambda *a, **k: pytest.fail("unexpected scan_and_alert"))
    yield
    assert not any(l.locked() for l in web._scan_locks.values()), "scan lock left held"
    assert not web._lock.locked()


@pytest.fixture
def client():
    return web.app.test_client()


def scan_result(**kw):
    return {"found": 1, "new": 1, "sent": 0, "symbols": ["BUY A"], **kw}


# ---- /api/signals + _enrich ----------------------------------------------------------------------

def test_signals_payload_shape(client):
    r = client.get("/api/signals")
    assert r.status_code == 200
    assert r.get_json() == {
        "market": "IN", "signals": [], "last_refresh": None,
        "config": {"universe": "NIFTY100", "timeframe": "1d", "capital": 100000.0, "risk_pct": 1.0,
                   "currency": "₹", "locale": "en-IN", "tv_prefix": "NSE:", "live": None}}
    us = client.get("/api/signals?market=us").get_json()
    assert us["market"] == "US" and us["config"]["currency"] == "$" and us["config"]["capital"] == 10000.0
    assert us["config"]["tv_prefix"] == "" and us["config"]["universe"] == "SP100"


def test_signals_live_source_only_for_india_with_kotak_key(client, monkeypatch):
    monkeypatch.setattr(config, "KOTAK_CONSUMER_KEY", "k")
    assert client.get("/api/signals?market=IN").get_json()["config"]["live"] == "Kotak Neo"
    assert client.get("/api/signals?market=US").get_json()["config"]["live"] is None


def test_signals_unknown_market_400(client):
    r = client.get("/api/signals?market=XX")
    assert r.status_code == 400
    assert r.get_json() == {"error": "Unknown market XX; use one of IN, US"}


def test_signals_market_filter_and_enrich(client):
    state.record([make_signal(symbol="A"), make_signal(symbol="B", market="US")])
    sigs = client.get("/api/signals").get_json()["signals"]
    assert [s["symbol"] for s in sigs] == ["A"]
    s = sigs[0]
    # PENDING: no P&L; current = ltp (= ref_price 100); to_entry_pct = |99 - 100| / 100
    assert (s["current"], s["open"], s["to_entry_pct"], s["pnl_pct"], s["pnl_r"]) == (100.0, True, 1.0, 0.0, 0.0)


def E(**kw):
    base = dict(side="BUY", entry=100.0, stop_loss=95.0, exit_price=None, ltp=110.0, fill_price=None,
                status="ACTIVE")
    base.update(kw)
    return web._enrich(base)


def test_enrich_math():
    s = E()
    assert (s["current"], s["pnl_pct"], s["pnl_r"], s["to_entry_pct"], s["open"]) == (110.0, 10.0, 2.0, 9.09, True)
    # Filled better than planned (98 vs 100), stopped at 95: lost 3 of the planned 5 risk
    s = E(fill_price=98.0, exit_price=95.0, status="SL_HIT")
    assert (s["current"], s["pnl_pct"], s["pnl_r"], s["open"]) == (95.0, -3.06, -0.6, False)
    # A gap fill right next to the stop must not inflate R (was ~+150R with fill-based risk)
    s = E(fill_price=95.1, exit_price=110.0, status="T2_HIT")
    assert s["pnl_r"] == 2.98
    s = E(side="SELL", stop_loss=105.0, ltp=90.0, status="T1_HIT")
    assert (s["pnl_pct"], s["pnl_r"], s["open"]) == (10.0, 2.0, True)
    s = E(stop_loss=100.0)                       # zero risk -> risk falls back to 1
    assert s["pnl_r"] == 10.0
    s = E(ltp=None)                              # no ltp -> entry
    assert (s["current"], s["pnl_pct"], s["to_entry_pct"]) == (100.0, 0.0, 0.0)
    s = E(ltp=0.0, entry=0.0, status="CANCELLED")  # px 0 -> to_entry_pct None
    assert s["to_entry_pct"] is None and s["pnl_pct"] == 0.0
    s = E(status="CANCELLED", exit_price=None, ltp=90.0)
    assert (s["pnl_pct"], s["pnl_r"], s["open"]) == (0.0, 0.0, False)


# ---- /api/meta ----------------------------------------------------------------------------------

def test_meta_shape(client):
    m = client.get("/api/meta").get_json()
    assert set(m) == {"strategies", "filters", "timeframes", "markets", "defaults"}
    assert len(m["strategies"]) == 13
    assert [f["name"] for f in m["filters"]] == ["trend", "rs", "events"]
    assert [t["value"] for t in m["timeframes"]] == ["5m", "15m", "30m", "1h", "1d", "all"]
    assert m["timeframes"][-1] == {"value": "all", "label": "All"}
    assert m["markets"] == [
        {"code": "IN", "name": "India", "tz": "Asia/Kolkata", "session": ["09:15", "15:30"]},
        {"code": "US", "name": "US", "tz": "America/New_York", "session": ["09:30", "16:00"]}]
    assert m["defaults"] == {"timeframe": "1d", "min_score": 2}


# ---- /api/refresh ---------------------------------------------------------------------------------

def test_refresh_calls_update_all_and_sets_time(client, monkeypatch):
    upd = Recorder(0)
    monkeypatch.setattr(tracker, "update_all", upd)
    monkeypatch.setattr(web, "time", SimpleNamespace(strftime=lambda fmt: "2030-01-02 03:04:05"))
    r = client.post("/api/refresh", json={"market": "US"})
    assert r.status_code == 200
    assert upd.calls == [((), {"notify": True})]
    j = r.get_json()
    assert j["market"] == "US" and j["last_refresh"] == "2030-01-02 03:04:05"


def test_refresh_swallows_update_errors(client, monkeypatch):
    def boom(**k):
        raise RuntimeError("down")
    monkeypatch.setattr(tracker, "update_all", boom)
    r = client.post("/api/refresh")
    assert r.status_code == 200 and r.get_json()["last_refresh"] is None


# ---- /api/scan ------------------------------------------------------------------------------------

def test_scan_defaults_ignore_env_strategies_and_filters(client, monkeypatch):
    """Quirk 5: /api/scan defaults to DEFAULT_STRATEGIES and filters=[] even if config says otherwise."""
    monkeypatch.setattr(config, "STRATEGIES", ["EMA 9/21 Cross"])
    monkeypatch.setattr(config, "FILTERS", ["trend"])
    rec = Recorder(scan_result())
    monkeypatch.setattr(web, "scan_and_alert", rec)
    r = client.post("/api/scan")
    assert r.status_code == 200
    assert rec.calls == [((), dict(timeframe="1d", send=False, strategies=list(DEFAULT_STRATEGIES), min_score=2,
                                   market="IN", filters=[]))]
    j = r.get_json()
    assert j["scan"] == {**scan_result(), "timeframe": "1d", "strategies": list(DEFAULT_STRATEGIES),
                         "min_score": 2, "filters": []}
    assert j["market"] == "IN" and "signals" in j and "config" in j


def test_scan_body_values_and_send(client, monkeypatch):
    rec = Recorder(scan_result(sent=1))
    monkeypatch.setattr(web, "scan_and_alert", rec)
    body = {"market": "US", "strategies": ["EMA 9/21 Cross", "RSI Pullback"], "filters": ["rs"],
            "timeframe": "15m", "min_score": "2", "send": 1}
    r = client.post("/api/scan", json=body)
    assert r.status_code == 200
    assert rec.calls[0][1] == dict(timeframe="15m", send=True, strategies=["EMA 9/21 Cross", "RSI Pullback"],
                                   min_score=2, market="US", filters=["rs"])


def test_scan_query_market_wins_over_body(client, monkeypatch):
    rec = Recorder(scan_result())
    monkeypatch.setattr(web, "scan_and_alert", rec)
    client.post("/api/scan?market=US", json={"market": "IN"})
    assert rec.calls[0][1]["market"] == "US"


def test_scan_all_timeframes_labels_symbols(client, monkeypatch):
    rec = Recorder(lambda **kw: {"found": 2, "new": 1, "sent": 0, "symbols": ["BUY A"]})
    monkeypatch.setattr(web, "scan_and_alert", rec)
    j = client.post("/api/scan", json={"timeframe": "all"}).get_json()
    assert [c[1]["timeframe"] for c in rec.calls] == ["5m", "15m", "30m", "1h", "1d"]
    assert j["scan"]["found"] == 10 and j["scan"]["new"] == 5 and j["scan"]["sent"] == 0
    assert j["scan"]["symbols"] == ["BUY A (5m)", "BUY A (15m)", "BUY A (30m)", "BUY A (1h)", "BUY A (1d)"]
    assert j["scan"]["timeframe"] == "all"


@pytest.mark.parametrize("body,msg", [
    ({"timeframe": "2h"}, "Unsupported timeframe: 2h"),
    ({"min_score": 10}, "Minimum agreeing votes must be between 1 and 7"),           # 11 defaults = 7 families
    ({"min_score": -1}, "Minimum agreeing votes must be between 1 and 7"),
    ({"strategies": ["Demand-Supply", "Rally-Base-Rally"], "min_score": 2},
     "Minimum agreeing votes must be between 1 and 1"),
    ({"strategies": ["Bogus"], "min_score": 2}, "Minimum agreeing votes must be between 1 and 1"),
])
def test_scan_400_validation(client, body, msg):
    r = client.post("/api/scan", json=body)
    assert r.status_code == 400 and r.get_json() == {"error": msg}


def test_scan_min_score_zero_falls_back_to_config(client, monkeypatch):
    """`int(body.get("min_score") or config.MIN_SCORE)`: 0 is falsy -> config default."""
    rec = Recorder(scan_result())
    monkeypatch.setattr(web, "scan_and_alert", rec)
    assert client.post("/api/scan", json={"min_score": 0}).status_code == 200
    assert rec.calls[0][1]["min_score"] == 2


def test_scan_unknown_market_400(client):
    assert client.post("/api/scan", json={"market": "JP"}).status_code == 400


def test_scan_lock_held_409(client, monkeypatch):
    monkeypatch.setattr(web, "scan_and_alert", Recorder(scan_result()))
    lock = web._scan_locks["IN"]
    lock.acquire()
    try:
        r = client.post("/api/scan")
        assert r.status_code == 409 and r.get_json() == {"error": "A India scan is already running"}
        assert client.post("/api/scan?market=US").status_code == 200   # locks are per market
    finally:
        lock.release()


def test_scan_value_error_400_other_500(client, monkeypatch):
    def bad(**kw):
        raise ValueError("Unknown strategies: X")
    monkeypatch.setattr(web, "scan_and_alert", bad)
    r = client.post("/api/scan")
    assert r.status_code == 400 and r.get_json() == {"error": "Unknown strategies: X"}

    def boom(**kw):
        raise RuntimeError("kaput")
    monkeypatch.setattr(web, "scan_and_alert", boom)
    r = client.post("/api/scan")
    assert r.status_code == 500 and r.get_json() == {"error": "kaput"}
    assert not web._scan_locks["IN"].locked()


def test_scan_payload_includes_recorded_signals(client, monkeypatch):
    def fake(**kw):
        state.record([make_signal(symbol="NEW")], telegram=False)
        return scan_result(symbols=["BUY NEW"])
    monkeypatch.setattr(web, "scan_and_alert", fake)
    j = client.post("/api/scan").get_json()
    assert [s["symbol"] for s in j["signals"]] == ["NEW"]


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and b"<html" in r.data.lower()
    r.close()


def test_constants():
    assert web.REFRESH_SEC == 300 and web.ALL_TF == "all"
    assert set(web._scan_locks) == set(markets.MARKETS)


# ---- /api/index (status-bar index quote) -----------------------------------------------------------

@pytest.fixture
def index_feed(monkeypatch):
    """Fake web.fetch_index; records calls; empty quote cache for each test."""
    monkeypatch.setattr(web, "_index_cache", {})
    calls = []

    def fake(timeframe, market, period=None):
        calls.append((timeframe, market, period))
        return feed.get(market)

    feed = {}
    monkeypatch.setattr(web, "fetch_index", fake)
    return SimpleNamespace(calls=calls, feed=feed)


def closes(*vals, end="2026-10-14"):
    return pd.Series(vals, index=pd.date_range(end=end, periods=len(vals), freq="D"))


def test_index_quote_last_change_and_name(client, index_feed):
    index_feed.feed["IN"] = closes(22800.0, 22708.6)
    j = client.get("/api/index?market=IN").get_json()
    assert j == {"market": "IN", "name": "NIFTY 50", "last": 22708.6, "change": -91.4,
                 "change_pct": -0.4, "as_of": "2026-10-14", "source": "Yahoo Finance, delayed"}
    assert index_feed.calls == [("1d", "IN", "6mo")]
    us = client.get("/api/index?market=US")
    assert us.get_json()["name"] == "S&P 500" and us.get_json()["last"] is None  # no data for US in the feed


def test_index_quote_is_cached_per_market(client, index_feed, monkeypatch):
    index_feed.feed["IN"] = closes(100.0, 101.0)
    client.get("/api/index?market=IN")
    client.get("/api/index?market=IN")
    assert len(index_feed.calls) == 1
    t = web.time.monotonic()
    # replace web's own time reference (time is a shared module)
    monkeypatch.setattr(web, "time", SimpleNamespace(monotonic=lambda: t + web.INDEX_TTL_SEC + 1))  # cache expired
    client.get("/api/index?market=IN")
    assert len(index_feed.calls) == 2


def test_index_quote_failures_not_cached(client, index_feed, monkeypatch):
    def boom(*a, **k):
        index_feed.calls.append("boom")
        raise RuntimeError("yahoo down")
    monkeypatch.setattr(web, "fetch_index", boom)
    for _ in range(2):
        j = client.get("/api/index?market=US").get_json()
        assert (j["last"], j["change"], j["change_pct"], j["as_of"]) == (None, None, None, None)
    assert index_feed.calls == ["boom", "boom"] and web._index_cache == {}


def test_index_quote_needs_two_closes(client, index_feed):
    index_feed.feed["IN"] = closes(100.0)
    assert client.get("/api/index?market=IN").get_json()["last"] is None
    assert web._index_cache == {}


def test_index_quote_unknown_market_400(client, index_feed):
    r = client.get("/api/index?market=XX")
    assert r.status_code == 400 and index_feed.calls == []


# ---- India index from Kotak (live), Yahoo as fallback -------------------------------------------

def test_india_index_prefers_kotak_live(client, index_feed, monkeypatch):
    from datetime import datetime
    from algotrade import kotak
    asked = []
    monkeypatch.setattr(kotak, "index_quote", lambda name: asked.append(name) or
                        {"ltp": 22997.35, "prev_close": 22622.7, "time": datetime(2026, 10, 15, 15, 30)})
    index_feed.feed["IN"] = closes(22721.95, 22622.7)            # Yahoo a day behind: must not be used
    j = client.get("/api/index?market=IN").get_json()
    assert (j["last"], j["change"], j["change_pct"], j["as_of"], j["source"]) == \
        (22997.35, 374.65, 1.66, "2026-10-15 15:30", "Kotak Neo, live")
    assert asked == ["Nifty 50"] and index_feed.calls == []
    client.get("/api/index?market=IN")                             # served from the 30 s cache
    assert asked == ["Nifty 50"]


def test_india_index_falls_back_to_yahoo(client, index_feed, monkeypatch):
    from algotrade import kotak
    monkeypatch.setattr(kotak, "index_quote", lambda name: None)
    index_feed.feed["IN"] = closes(22800.0, 22708.6)
    j = client.get("/api/index?market=IN").get_json()
    assert j["last"] == 22708.6 and j["source"] == "Yahoo Finance, delayed"


def test_us_index_stays_on_yahoo(client, index_feed, monkeypatch):
    from algotrade import kotak
    monkeypatch.setattr(kotak, "index_quote", lambda name: pytest.fail("no Kotak index for the US"))
    index_feed.feed["US"] = closes(5000.0, 5050.0)
    assert client.get("/api/index?market=US").get_json()["last"] == 5050.0


# ---- /api/scan/stop -------------------------------------------------------------------------------

def test_scan_stop_without_a_running_scan_409(client):
    r = client.post("/api/scan/stop?market=US")
    assert r.status_code == 409 and r.get_json() == {"error": "No US scan is running"}


def test_scan_stop_sets_flag_and_scan_reports_stopped(client, monkeypatch):
    from algotrade import scanner

    def fake_scan(**kw):
        # Stop pressed while the first timeframe runs: the next timeframe never starts
        assert client.post("/api/scan/stop?market=IN").get_json() == {"ok": True}
        return scan_result()
    rec = Recorder(fake_scan)
    monkeypatch.setattr(web, "scan_and_alert", rec)
    scanner.request_stop("IN")   # a leftover request from before is cleared when a scan starts
    try:
        r = client.post("/api/scan", json={"timeframe": "all"})
        assert r.status_code == 200
        j = r.get_json()
        assert j["scan"] == {"stopped": True} and "signals" in j
        assert [c[1]["timeframe"] for c in rec.calls] == ["5m"]
    finally:
        scanner.clear_stop("IN")
