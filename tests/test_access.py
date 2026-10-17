"""Phone app access (access.py): PIN login for other devices, trading only from this PC or over Tailscale."""
import pytest

from algotrade import access, broker, config, manual, telegram_bot, web
from conftest import Recorder

LAN, TAILNET = "192.168.1.20", "100.101.7.3"
TS = {"Tailscale-User-Login": "me@example.com", "X-Forwarded-For": TAILNET}   # as `tailscale serve` proxies


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PIN", "246810")
    monkeypatch.setattr(access, "_fails", {"n": 0, "until": 0.0})
    msgs = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", msgs)
    client = web.app.test_client()
    client.msgs = msgs
    return client


def get(c, path, addr=LAN, headers=None):
    return c.get(path, environ_base={"REMOTE_ADDR": addr}, headers=headers or {})


def post(c, path, body, addr=LAN, headers=None):
    return c.post(path, json=body, environ_base={"REMOTE_ADDR": addr}, headers=headers or {})


def login(c, pin="246810", addr=LAN, headers=None):
    return post(c, "/api/login", {"pin": pin}, addr, headers)


def test_this_pc_needs_no_login(c):
    assert get(c, "/api/trading", addr="127.0.0.1").status_code == 200
    assert get(c, "/api/access", addr="127.0.0.1").get_json()["can_trade"] is True


def test_other_devices_must_log_in(c):
    assert get(c, "/api/trading").status_code == 401
    r = get(c, "/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    for path in ("/login", "/manifest.webmanifest", "/sw.js", "/offline", "/icons/icon-192.png"):
        assert get(c, path).status_code == 200, path


def test_proxied_requests_are_not_local(c):
    """tailscale serve connects from 127.0.0.1: it must not count as this PC."""
    assert get(c, "/api/trading", addr="127.0.0.1", headers=TS).status_code == 401
    assert get(c, "/api/trading", addr="127.0.0.1", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 401


def test_login_and_logout(c):
    assert login(c).status_code == 200
    assert get(c, "/api/trading").status_code == 200
    assert "Dashboard login" in c.msgs.calls[-1][0][0]
    assert post(c, "/api/logout", {}).status_code == 200
    assert get(c, "/api/trading").status_code == 401


def test_wrong_pins_lock_logins(c):
    for _ in range(access.MAX_FAILS - 1):
        assert login(c, "000000").get_json()["error"] == "Wrong PIN"
    r = login(c, "000000")
    assert "locked" in r.get_json()["error"] and "Dashboard locked" in c.msgs.calls[-1][0][0]
    r = login(c)                                   # even the right PIN waits out the lock
    assert r.status_code == 401 and "Try again" in r.get_json()["error"]


@pytest.mark.parametrize("pin", ["", "1234"])
def test_no_or_short_pin_means_this_pc_only(c, monkeypatch, pin):
    monkeypatch.setattr(config, "DASHBOARD_PIN", pin)
    r = login(c, pin)
    assert r.status_code == 401 and "DASHBOARD_PIN" in r.get_json()["error"]


def test_changing_the_pin_logs_devices_out(c, monkeypatch):
    login(c)
    monkeypatch.setattr(config, "DASHBOARD_PIN", "135799")
    assert get(c, "/api/trading").status_code == 401


def test_logged_in_lan_device_can_look_but_not_trade(c):
    login(c)
    assert get(c, "/api/access").get_json() == {"local": False, "logged_in": True, "can_trade": False,
                                                "pin_set": True, "device": LAN}
    r = post(c, "/api/orders/cancel", {"order_id": "1"})
    assert r.status_code == 403 and "Tailscale" in r.get_json()["error"]
    assert post(c, "/api/trading", {"enabled": True}).status_code == 403


@pytest.mark.parametrize("addr, headers", [("127.0.0.1", TS), (TAILNET, {})])
def test_logged_in_tailscale_device_can_trade(c, monkeypatch, addr, headers):
    placed = []
    monkeypatch.setattr(manual, "buy_stock", lambda *a: placed.append(a) or {"order_id": "X1"})
    login(c, addr=addr, headers=headers)
    assert get(c, "/api/access", addr, headers).get_json()["can_trade"] is True
    r = post(c, "/api/stocks/buy", {"symbol": "TCS", "qty": 1, "price": 3000}, addr, headers)
    assert r.status_code == 200 and placed


def test_tailscale_without_login_cannot_trade(c):
    assert post(c, "/api/stocks/buy", {"symbol": "TCS", "qty": 1}, "127.0.0.1", TS).status_code == 401


def test_kill_switch_needs_no_login(c, monkeypatch):
    monkeypatch.setattr(broker, "login", lambda force=False: None)
    broker.set_trading(True, by="test")
    assert post(c, "/api/trading", {"enabled": False}).status_code == 200 and not broker.trading_enabled()
    assert post(c, "/api/trading", {"enabled": True}).status_code == 401


def test_cookie_secret_kept_in_data_dir():
    first = access._secret()
    assert access._secret() == first and (config.DATA_DIR / ".web_secret").exists()
