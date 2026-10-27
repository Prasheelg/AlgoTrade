"""The dashboard's Scanner switch: runner.start/stop, the /api/scanner endpoint, the scanner noticing the off
switch, and a stopped or vanished scanner no longer counting as running."""
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import main
from algotrade import health, markets, runner, state, telegram_bot, tracker, web
from algotrade.scanner import get_status, stop_requested as scan_stop_requested


@pytest.fixture(autouse=True)
def _fresh_runner(monkeypatch):
    from algotrade.scanner import clear_stop
    monkeypatch.setattr(runner, "_proc", None)
    yield
    for code in markets.MARKETS:   # the off switch stops scans in this process; later tests must not see it
        clear_stop(code)


class FakeProc:
    def __init__(self, code=None):
        self.pid, self.returncode = 4242, code

    def poll(self):
        return self.returncode


@pytest.fixture
def popen(monkeypatch):
    calls = []

    def fake(args, **kw):
        calls.append((args, kw))
        return FakeProc()
    monkeypatch.setattr(subprocess, "Popen", fake)
    return calls


@pytest.fixture
def client():
    return web.app.test_client()


# ---- heartbeat: stopped / vanished scanners -------------------------------------------------------

def test_heartbeat_of_this_process_counts_until_it_says_goodbye():
    health.beat()
    assert health.scanner_running() is True
    health.gone()
    assert health.scanner_running() is False


def test_heartbeat_of_a_dead_process_does_not_count():
    p = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    state.set_setting(health.HEARTBEAT, {"pid": int(p.stdout)})
    assert health.scanner_running() is False


# ---- runner ---------------------------------------------------------------------------------------

def test_start_launches_main_once(popen):
    state.set_setting(runner.STOP_KEY, True)               # left from an earlier stop
    st = runner.start()
    assert len(popen) == 1 and popen[0][0][-1].endswith("main.py")
    assert st == {"running": False, "starting": True, "stopping": False, "error": None}
    assert runner.stop_requested() is False
    runner.start()                                         # still starting: no second scanner
    assert len(popen) == 1
    health.gone()                                          # it came up and was switched off, still exiting
    assert runner.status()["starting"] is False


def test_start_does_nothing_while_a_scanner_runs(popen):
    health.beat()                                          # e.g. one started with run.bat
    assert runner.start()["running"] is True and popen == []


def test_stop_asks_the_scanner_to_quit():
    health.beat()
    assert runner.stop() == {"running": True, "starting": False, "stopping": True, "error": None}
    assert runner.stop_requested() is True


def test_a_crashed_start_is_reported(monkeypatch):
    monkeypatch.setattr(runner, "_proc", FakeProc(code=1))
    assert "exit code 1" in runner.status()["error"]


# ---- /api/scanner ---------------------------------------------------------------------------------

def test_api_switches_on_and_off(client, popen):
    assert client.post("/api/scanner", json={"on": True}).get_json()["starting"] is True
    assert client.post("/api/scanner", json={"on": False}).status_code == 200
    assert runner.stop_requested() is True
    assert client.get("/api/scan/status").get_json()["runner"]["starting"] is True


def test_api_needs_trade_access_and_a_bool(client, popen):
    r = client.post("/api/scanner", json={"on": True}, environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert r.status_code in (302, 401, 403) and popen == []
    assert client.post("/api/scanner", json={"on": "yes"}).status_code == 400


# ---- per-market switches --------------------------------------------------------------------------

def test_markets_are_on_until_switched_off(popen):
    assert runner.markets_on() == {code: True for code in markets.MARKETS}
    health.beat()                                          # a scanner is running
    st = runner.switch_market("US", False)
    assert runner.markets_on()["US"] is False and runner.market_on("IN") is True
    assert st["stopping"] is False                         # India is still on: the scanner keeps going
    assert runner.switch_market("IN", False)["stopping"] is True   # the last one off stops it
    runner.switch_market("IN", True)                       # back on before it noticed: the stop is withdrawn
    assert runner.stop_requested() is False and runner.markets_on() == {"IN": True, "US": False}


def test_api_switches_one_market(client, popen):
    j = client.post("/api/scanner", json={"on": False, "market": "US"}).get_json()
    assert j["markets"] == {"IN": True, "US": False} and popen == []   # nothing running, nothing to start
    j = client.post("/api/scanner?market=US", json={"on": True}).get_json()
    assert j["markets"]["US"] is True and j["starting"] is True and len(popen) == 1
    status = client.get("/api/scan/status").get_json()["markets"]
    assert status["IN"]["on"] is True and status["US"]["on"] is True
    assert client.post("/api/scanner", json={"on": True, "market": "XX"}).status_code == 400


def test_loop_skips_a_market_that_is_off(monkeypatch):
    monkeypatch.setattr(telegram_bot, "send_message", lambda text: True)
    monkeypatch.setattr(tracker, "update_all", lambda market=None, **k: None)
    monkeypatch.setattr(main, "housekeeping", health.beat)
    monkeypatch.setattr(main, "trade", lambda keys: None)
    monkeypatch.setattr(markets.Market, "now", lambda self: datetime(2024, 1, 1, 10, 0, tzinfo=ZoneInfo(self.tz)))
    monkeypatch.setattr(markets.Market, "in_scan_window", lambda self, now=None: True)
    monkeypatch.setattr(runner, "STOP_POLL_SEC", 0.01)
    scanned = []
    monkeypatch.setattr(main, "run_scan", lambda code, dry_run=False: scanned.append(code) or runner.stop())
    runner.set_markets({"IN": False})
    main.run_loop(["IN", "US"])
    assert scanned == ["US"] and get_status("IN")["next_at"] is None


def test_a_start_with_every_market_off_turns_them_on(monkeypatch):
    monkeypatch.setattr(telegram_bot, "send_message", lambda text: True)
    monkeypatch.setattr(main, "_loop", lambda codes: None)
    runner.set_markets({"IN": False, "US": False})
    main.run_loop(["IN"])                                  # e.g. run.bat after both were switched off
    assert runner.markets_on() == {"IN": True, "US": False}


# ---- the scanner loop sees the off switch ---------------------------------------------------------

def test_loop_exits_on_the_off_switch(monkeypatch):
    msgs = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda text: msgs.append(text) or True)
    monkeypatch.setattr(tracker, "update_all", lambda market=None, **k: None)
    monkeypatch.setattr(main, "housekeeping", health.beat)
    monkeypatch.setattr(markets.Market, "now", lambda self: datetime(2024, 1, 1, 10, 0, tzinfo=ZoneInfo(self.tz)))
    monkeypatch.setattr(runner, "STOP_POLL_SEC", 0.01)
    monkeypatch.setattr(main, "run_scan", lambda code, dry_run=False: runner.stop())   # switched off mid-loop
    state.set_setting(runner.STOP_KEY, True)               # a stale request from before the start is ignored
    main.run_loop(["IN"])                                  # returns instead of sleeping SCAN_INTERVAL_MIN
    assert msgs[-1] == "🛑 <b>Scanner stopped</b> from the dashboard"
    assert health.scanner_running() is False and runner.stop_requested() is False
    assert scan_stop_requested("IN") and get_status("IN")["next_at"] is None


def test_a_stopped_scan_skips_auto_trade(monkeypatch):
    from algotrade.scanner import ScanStopped
    monkeypatch.setattr(main, "scan_and_alert", lambda **kw: (_ for _ in ()).throw(ScanStopped("IN scan stopped")))
    monkeypatch.setattr(main, "trade", lambda keys: pytest.fail("a stopped scan must not trade"))
    main.run_scan("IN")
    st = get_status("IN")
    assert st["running"] is False and st["error"] is None
