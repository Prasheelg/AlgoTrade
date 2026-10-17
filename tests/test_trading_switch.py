"""The auto-trade switch: settings store, broker.set_trading and /api/trading."""
import pytest

from algotrade import broker, config, state, telegram_bot, web
from conftest import Recorder


def test_settings_roundtrip():
    assert state.get_setting("x", 5) == (5, None)
    at = state.set_setting("x", {"a": [1, 2]})
    assert state.get_setting("x") == ({"a": [1, 2]}, at)


def test_trading_off_by_default_with_limits():
    st = broker.trading_status()
    assert st == {"enabled": False, "changed_at": None, "changed_by": None, "account": None, "credentials": False,
                  "limits": {"max_orders_per_day": 2, "max_order_value": 10000.0}}
    assert broker.trading_enabled() is False


@pytest.fixture
def logged_in(monkeypatch):
    monkeypatch.setattr(config, "KOTAK_UCC", "AB123")
    calls = []

    def fake_login(force=False):
        calls.append(force)
        broker._session["name"] = "TEST USER"
    monkeypatch.setattr(broker, "login", fake_login)
    return calls


def test_enable_logs_in_first_and_disable_does_not(logged_in):
    st = broker.set_trading(True, by="test")
    assert logged_in == [True] and st["enabled"] and st["account"] == "TEST USER (AB123)" and st["changed_by"] == "test"
    assert broker.trading_enabled()
    st = broker.set_trading(False, by="test")
    assert logged_in == [True] and not st["enabled"] and not broker.trading_enabled()


def test_enable_with_failed_login_stays_off(monkeypatch):
    def boom(force=False):
        raise broker.BrokerError("TOTP login failed: Invalid TOTP")
    monkeypatch.setattr(broker, "login", boom)
    with pytest.raises(broker.BrokerError):
        broker.set_trading(True, by="test")
    assert not broker.trading_enabled()


# ---- /api/trading ------------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    msgs = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", msgs)
    c = web.app.test_client()
    c.msgs = msgs
    return c


def post(client, body, addr="127.0.0.1"):
    return client.post("/api/trading", json=body, environ_base={"REMOTE_ADDR": addr})


def test_api_get(client):
    assert client.get("/api/trading").get_json()["enabled"] is False


def test_api_enable_from_this_pc_notifies(client, logged_in):
    r = post(client, {"enabled": True})
    assert r.status_code == 200 and r.get_json()["enabled"] is True
    assert "Automatic trading ON" in client.msgs.calls[0][0][0] and "max 2 orders/day" in client.msgs.calls[0][0][0]
    assert r.get_json()["changed_by"] == "dashboard (this PC)"


def test_api_enable_from_another_device_refused(client, logged_in):
    r = post(client, {"enabled": True}, addr="192.168.1.20")
    assert r.status_code == 401 and not broker.trading_enabled() and logged_in == []


def test_api_disable_from_anywhere(client, logged_in):
    broker.set_trading(True, by="test")
    r = post(client, {"enabled": False}, addr="192.168.1.20")
    assert r.status_code == 200 and not broker.trading_enabled()
    assert "Automatic trading OFF" in client.msgs.calls[0][0][0]


def test_api_bad_body_and_failed_login(client, monkeypatch):
    assert post(client, {"enabled": "yes"}).status_code == 400
    assert post(client, {}).status_code == 400
    r = post(client, {"enabled": True})          # no credentials -> login fails, stays off
    assert r.status_code == 502 and "trading stays off" in r.get_json()["error"]
    assert not broker.trading_enabled() and client.msgs.calls == []


def test_api_telegram_failure_does_not_block_switch(client, logged_in, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("telegram down")
    monkeypatch.setattr(telegram_bot, "send_message", boom)
    assert post(client, {"enabled": True}).status_code == 200 and broker.trading_enabled()
