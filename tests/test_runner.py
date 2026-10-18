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
