"""Health checks (health.py), the daily database backup (backup.py) and the shared DB settings. Kotak is faked."""
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from algotrade import backup, broker, config, health, kotak, orders, state, telegram_bot, web
from conftest import Recorder

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def msgs(monkeypatch):
    rec = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", rec)
    monkeypatch.setattr(health, "_alerted", {})
    return rec


@pytest.fixture
def at(monkeypatch):
    """Pin the India clock: at(hh, mm) on a Friday."""
    def set_(hh, mm=0):
        t = datetime(2026, 10, 16, hh, mm, tzinfo=IST)
        monkeypatch.setattr(type(health.markets.get("IN")), "now", lambda self: t)
        return t
    return set_


def creds(monkeypatch):
    for k in broker.CREDENTIALS:
        monkeypatch.setattr(config, k, "x")


def test_database_uses_wal():
    with state._conn() as con:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_healthy_by_default(at, msgs):
    at(11)
    assert health.problems() == [] and health.watch_once() == [] and msgs.calls == []


def test_kotak_login_failure_is_critical_until_a_login_works(at, msgs, monkeypatch):
    at(20)
    creds(monkeypatch)
    broker._login_result("TOTP login failed: invalid otp")
    p = health.watch_once()
    assert p[0]["id"] == "kotak_login" and "invalid otp" in p[0]["text"]
    health.watch_once()
    assert len(msgs.calls) == 1 and "🚨" in msgs.calls[0][0][0]            # posted once
    broker._login_result(None)
    assert health.watch_once() == [] and "resolved" in msgs.calls[-1][0][0]


def test_scanner_must_run_while_auto_trade_is_on(at, msgs):
    at(20)
    state.set_setting(broker.TRADING_KEY, {"enabled": True})
    assert [p["id"] for p in health.problems()] == ["scanner"]
    health.beat()
    assert health.problems() == []
    later = datetime.now() + timedelta(minutes=60)                # no heartbeat for an hour
    assert "last seen 60 min ago" in health.problems(now=later)[0]["text"]
    state.set_setting(broker.TRADING_KEY, {"enabled": False})
    assert health.problems(now=datetime.now() + timedelta(hours=2)) == []   # not needed with Auto-trade off


def test_open_position_without_a_working_stop(at, msgs, monkeypatch):
    creds(monkeypatch)
    orders._save("k1", symbol="TCS", state=orders.OPEN, qty=3, stop_order="S1", created_day="2026-10-16")
    book = {"data": [{"nOrdNo": "S1", "ordSt": "trigger pending"}]}
    monkeypatch.setattr(broker, "call", lambda method, **kw: book)
    monkeypatch.setattr(kotak, "enabled", lambda: False)
    at(9, 20)                                                     # just opened: the scanner hasn't run yet
    assert health.problems() == []
    at(11)
    assert health.problems() == []                                # stop working
    book["data"][0]["ordSt"] = "cancelled"
    assert "isn't working (cancelled)" in health.problems()[0]["text"]
    at(20)                                                        # after hours the book isn't checked
    assert health.problems() == []
    orders._save("k1", note="NO STOP: Kotak rejected the stop-loss 3 times (RMS)")
    p = health.problems()
    assert p[0]["level"] == "critical" and "TCS: no stop-loss (Kotak rejected" in p[0]["text"]


def test_prices_not_updating_in_market_hours(at, msgs, monkeypatch):
    now = at(11)
    monkeypatch.setattr(kotak, "enabled", lambda: True)
    monkeypatch.setattr(kotak, "index_quote", lambda name: {"ltp": 1.0, "time": now.replace(tzinfo=None)})
    assert health.problems() == []
    monkeypatch.setattr(kotak, "index_quote", lambda name: {"ltp": 1.0, "time": (now - timedelta(minutes=9)).replace(tzinfo=None)})
    assert "9 min old" in health.problems()[0]["text"]
    monkeypatch.setattr(kotak, "index_quote", lambda name: None)
    assert "failing" in health.problems()[0]["text"]
    at(20)
    assert health.problems() == []


def test_health_api(at, msgs, monkeypatch):
    at(20)
    monkeypatch.setattr(web, "_health", {"problems": None, "at": None})
    state.set_setting(broker.TRADING_KEY, {"enabled": True})
    j = web.app.test_client().get("/api/health").get_json()
    assert [p["id"] for p in j["problems"]] == ["scanner"] and j["at"]


# ---- backup -------------------------------------------------------------------------------------

def test_backup_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "DB_BACKUP", False)
    assert backup.daily(datetime(2026, 10, 16, 16, 5, tzinfo=IST)) is False
    assert not (config.DATA_DIR / "backups").exists()


def test_daily_backup_once_after_1600(monkeypatch):
    monkeypatch.setattr(config, "DB_BACKUP", True)
    sent = []
    monkeypatch.setattr(telegram_bot, "send_document", lambda path, caption="": sent.append((path, caption)) or True)
    state.set_setting("x", 1)
    assert backup.daily(datetime(2026, 10, 16, 15, 0, tzinfo=IST)) is False
    assert backup.daily(datetime(2026, 10, 16, 16, 5, tzinfo=IST)) is True
    assert backup.daily(datetime(2026, 10, 16, 18, 0, tzinfo=IST)) is False      # once a day
    path = config.DATA_DIR / "backups" / "signals_2026-10-16.db"
    assert path.exists() and sent[0][0] == path and "2026-10-16" in sent[0][1]
    con = sqlite3.connect(path)
    assert con.execute("SELECT value FROM settings WHERE key = 'x'").fetchone()[0] == "1"
    con.close()
    assert backup.status()["day"] == "2026-10-16" and backup.status()["sent"] is True


def test_backups_pruned(monkeypatch):
    monkeypatch.setattr(telegram_bot, "send_document", lambda *a, **k: True)
    for d in range(1, backup.KEEP_DAYS + 4):
        backup.make(f"2026-09-{d:02d}")
    files = sorted(p.name for p in (config.DATA_DIR / "backups").glob("*.db"))
    assert len(files) == backup.KEEP_DAYS and files[0] == "signals_2026-09-04.db"


def test_stale_backup_is_a_warning(at, msgs, monkeypatch):
    at(11)
    state.set_setting(backup.SETTING, {"day": "2026-10-13"})
    monkeypatch.setattr(config, "DB_BACKUP", False)
    assert health.problems() == []                      # backups off: no warning
    monkeypatch.setattr(config, "DB_BACKUP", True)
    state.set_setting(backup.SETTING, {"day": "2026-10-13"})
    assert [p["id"] for p in health.problems()] == ["backup"]


def test_scanner_running_follows_the_heartbeat(monkeypatch):
    from datetime import datetime, timedelta
    from algotrade import config, health, state
    now = datetime(2026, 10, 19, 10, 0)
    assert health.scanner_running(now) is False and health.scanner_age_min(now) is None   # never seen
    monkeypatch.setattr(state, "get_setting",
                        lambda k, d=None: ({}, (now - timedelta(minutes=2 * config.SCAN_INTERVAL_MIN + 4)).isoformat()))
    assert health.scanner_running(now) is True
    monkeypatch.setattr(state, "get_setting",
                        lambda k, d=None: ({}, (now - timedelta(minutes=2 * config.SCAN_INTERVAL_MIN + 6)).isoformat()))
    assert health.scanner_running(now) is False
