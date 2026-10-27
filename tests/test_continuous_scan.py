"""Continuous scanner: India scan window, scan panel settings stored on the PC, every timeframe per loop,
scan status for the dashboard, and the /api/scan/settings + /api/scan/status endpoints."""
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import main
from algotrade import config, markets, orders, settings, state, telegram_bot, tracker, web
from algotrade.scanner import get_status, set_status
from algotrade.strategies import DEFAULT_STRATEGIES
from conftest import Recorder, StopLoop, make_signal, sleep_after

IN, US = markets.MARKETS["IN"], markets.MARKETS["US"]


def at(m, *args):
    return datetime(*args, tzinfo=ZoneInfo(m.tz))


# ---- scan window ----------------------------------------------------------------------------------

@pytest.mark.parametrize("args,inside", [
    ((2024, 1, 1, 8, 59), False),
    ((2024, 1, 1, 9, 0), True),       # India scans from 09:00, before the 09:15 open
    ((2024, 1, 1, 15, 30), True),
    ((2024, 1, 1, 15, 31), False),
    ((2024, 1, 6, 10, 0), False),     # Saturday
])
def test_india_scan_window(args, inside):
    assert IN.in_scan_window(at(IN, *args)) is inside
    assert IN.scan_window == (IN.scan_from, IN.session[1])


def test_us_scan_window_is_the_session():
    assert US.scan_window == US.session
    assert US.in_scan_window(at(US, 2024, 7, 1, 9, 29)) is False and US.in_scan_window(at(US, 2024, 7, 1, 9, 30))


# ---- stored scan settings -------------------------------------------------------------------------

def test_scan_settings_fall_back_to_env(monkeypatch):
    assert settings.scan_settings("IN") == {"strategies": list(DEFAULT_STRATEGIES), "min_score": 2, "filters": [],
                                            "send": False, "saved": None}
    monkeypatch.setattr(config, "MARKET_SCAN", {"IN": {"strategies": ["RSI Pullback"], "min_score": 3,
                                                       "filters": ["trend"]}, "US": {}})
    s = settings.scan_settings("IN")
    assert s["strategies"] == ["RSI Pullback"] and s["min_score"] == 1 and s["filters"] == ["trend"]  # 3 > 1 vote


def test_save_scan_settings_round_trip_per_market():
    body = {"strategies": ["RSI Pullback", "EMA 9/21 Cross"], "min_score": 2, "filters": ["rs"], "send": False}
    saved = settings.save_scan_settings("in", body)
    assert {k: saved[k] for k in body} == body and saved["saved"]
    assert settings.scan_settings("IN")["send"] is False
    assert settings.scan_settings("US")["saved"] is None          # other market untouched


@pytest.mark.parametrize("body,msg", [
    ({"strategies": "RSI Pullback", "min_score": 1, "filters": [], "send": True}, "strategies"),
    ({"strategies": ["Bogus"], "min_score": 1, "filters": [], "send": True}, "Unknown strategies: Bogus"),
    ({"strategies": ["RSI Pullback"], "min_score": 1, "filters": ["moon"], "send": True}, "filters"),
    ({"strategies": ["RSI Pullback"], "min_score": 2, "filters": [], "send": True}, "between 1 and 1"),
    ({"strategies": ["RSI Pullback"], "min_score": "x", "filters": [], "send": True}, "min_score"),
    ({"strategies": ["RSI Pullback"], "min_score": 1, "filters": [], "send": "yes"}, "send"),
])
def test_save_scan_settings_validation(body, msg):
    with pytest.raises(ValueError) as e:
        settings.save_scan_settings("IN", body)
    assert msg in str(e.value)
    assert settings.scan_settings("IN")["saved"] is None


def test_no_strategies_can_be_saved():
    s = settings.save_scan_settings("US", {"strategies": [], "min_score": 1, "filters": [], "send": True})
    assert s["strategies"] == []


# ---- run_scan: every timeframe, panel settings, status, Auto-trade --------------------------------

@pytest.fixture
def scans(monkeypatch):
    """scan_and_alert fake recording calls; per-timeframe signals to record can be scripted."""
    monkeypatch.setattr(config, "SCAN_TIMEFRAMES", ["15m", "1d"])
    script, calls = {}, []

    def fake(**kw):
        calls.append(kw)
        if isinstance(script.get(kw["timeframe"]), Exception):
            raise script[kw["timeframe"]]
        sigs = script.get(kw["timeframe"], [])
        state.record(sigs, telegram=False)
        return {"found": len(sigs), "new": len(sigs), "sent": 0, "symbols": [f"{s.side} {s.symbol}" for s in sigs]}
    monkeypatch.setattr(main, "scan_and_alert", fake)
    traded = []
    monkeypatch.setattr(main, "trade", lambda keys: traded.append(keys))
    return script, calls, traded


def test_run_scan_uses_saved_panel_on_every_timeframe(scans):
    script, calls, traded = scans
    settings.save_scan_settings("IN", {"strategies": ["RSI Pullback"], "min_score": 1, "filters": ["trend"],
                                       "send": False})
    day, fast, short = make_signal(symbol="DAY"), make_signal(symbol="FAST", timeframe="15m"), \
        make_signal(symbol="DOWN", side="SELL")
    script.update({"15m": [fast], "1d": [day, short]})
    main.run_scan("IN")
    kw = {"market": "IN", "send": False, "strategies": ["RSI Pullback"], "min_score": 1, "filters": ["trend"]}
    assert calls == [{**kw, "timeframe": "15m"}, {**kw, "timeframe": "1d"}]
    # every new signal, any timeframe: trade() gives stocks the BUYs, options both sides
    assert [sorted(t) for t in traded] == [sorted([fast.key, day.key, short.key])]
    st = get_status("IN")
    assert st["running"] is False and st["timeframe"] is None and st["error"] is None
    assert st["result"] == {"found": 3, "new": 3, "sent": 0,
                            "symbols": ["BUY FAST (15m)", "BUY DAY (1d)", "SELL DOWN (1d)"]}
    assert st["started_at"] <= st["finished_at"]


def test_run_scan_picks_up_a_change_on_the_next_scan(scans):
    _, calls, _ = scans
    main.run_scan("US")
    assert calls[-1]["strategies"] == list(DEFAULT_STRATEGIES)
    settings.save_scan_settings("US", {"strategies": ["MACD Trend Cross"], "min_score": 1, "filters": [], "send": True})
    main.run_scan("US")
    assert calls[-1]["strategies"] == ["MACD Trend Cross"]


def test_run_scan_one_timeframe_failing_does_not_stop_the_others(scans):
    script, calls, traded = scans
    day = make_signal(symbol="DAY")
    script.update({"15m": RuntimeError("Yahoo down"), "1d": [day]})
    with pytest.raises(RuntimeError, match="IN scan failed on 15m"):
        main.run_scan("IN")
    assert [c["timeframe"] for c in calls] == ["15m", "1d"]
    assert traded == [[day.key]]
    st = get_status("IN")
    assert st["error"] == "15m: Yahoo down" and st["result"]["new"] == 1 and st["running"] is False


def test_run_scan_with_nothing_ticked_records_why(scans):
    _, calls, traded = scans
    settings.save_scan_settings("IN", {"strategies": [], "min_score": 1, "filters": [], "send": True})
    main.run_scan("IN")
    assert calls == [] and traded == []
    assert "No strategies are ticked" in get_status("IN")["error"]


def test_status_merges_fields():
    set_status("US", running=True, timeframe="5m")
    set_status("US", timeframe="1h")
    assert get_status("US") == {"running": True, "timeframe": "1h"}
    assert get_status("IN") == {}


# ---- run_loop: window, interval from the start of a loop, next scan time ---------------------------

def test_loop_scans_from_nine_and_keeps_the_interval(monkeypatch):
    events, sleeps = [], []
    monkeypatch.setattr(telegram_bot, "send_message", lambda text: True)
    monkeypatch.setattr(tracker, "update_all", lambda market=None, **k: events.append(("update", market)))
    monkeypatch.setattr(main, "housekeeping", lambda: None)
    clock = iter([at(IN, 2024, 1, 1, 8, 55), at(IN, 2024, 1, 1, 9, 5)])
    monkeypatch.setattr(markets.Market, "now", lambda self: next(clock))
    ticks = iter([0, 0, 100, 300])   # second loop: the scan took 200 s
    monkeypatch.setattr(main, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(main, "run_scan", lambda code, dry_run=False: events.append(("scan", code)))
    monkeypatch.setattr(main, "_pause", sleep_after(2, sleeps))
    with pytest.raises(StopLoop):
        main.run_loop(["IN"])
    assert events == [("update", "IN"), ("scan", "IN")]   # 08:55 nothing, 09:05 scans
    assert sleeps == [900, 700]                            # SCAN_INTERVAL_MIN (15 in tests) from each loop's start
    assert get_status("IN")["next_at"]


# ---- API ------------------------------------------------------------------------------------------

@pytest.fixture
def client():
    return web.app.test_client()


def test_api_scan_settings_get_and_save(client):
    assert client.get("/api/scan/settings?market=US").get_json()["saved"] is None
    body = {"strategies": ["RSI Pullback"], "min_score": 1, "filters": [], "send": False}
    r = client.post("/api/scan/settings?market=US", json=body)
    assert r.status_code == 200 and r.get_json()["send"] is False
    assert client.get("/api/scan/settings?market=US").get_json()["strategies"] == ["RSI Pullback"]
    r = client.post("/api/scan/settings?market=US", json={**body, "min_score": 5})
    assert r.status_code == 400 and "between 1 and 1" in r.get_json()["error"]


def test_api_scan_settings_save_needs_trade_access(client):
    r = client.post("/api/scan/settings?market=IN", json={"strategies": [], "min_score": 1, "filters": [], "send": True},
                    environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert r.status_code in (302, 401, 403)
    assert settings.scan_settings("IN")["saved"] is None


def test_api_scan_status(client, monkeypatch):
    from algotrade import health
    monkeypatch.setattr(health, "scanner_running", lambda now=None: True)
    monkeypatch.setattr(health, "scanner_age_min", lambda now=None: 1.5)
    monkeypatch.setattr(markets.Market, "in_scan_window", lambda self, now=None: self.code == "IN")
    set_status("IN", running=True, timeframe="30m")
    j = client.get("/api/scan/status").get_json()
    assert j == {"alive": True, "last_seen_min": 1.5,
                 "runner": {"running": True, "starting": False, "stopping": False, "error": None},
                 "markets": {"IN": {"running": True, "timeframe": "30m", "in_window": True, "on": True},
                             "US": {"in_window": False, "on": True}}}
