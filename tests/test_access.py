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


# ---- Cloudflare Access (a Cloudflare Tunnel on your own domain) ----
import time as _time

import jwt as _jwt
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

TEAM, AUD, ME = "myteam.cloudflareaccess.com", "aud-tag-123", "me@example.com"
_KEY = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = _rsa.generate_private_key(public_exponent=65537, key_size=2048)


def cf_token(email=ME, aud=AUD, iss=f"https://{TEAM}", exp_in=600, key=_KEY):
    now = int(_time.time())
    return _jwt.encode({"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in},
                       key, algorithm="RS256", headers={"kid": "k1"})


def cf_headers(token=None):
    """As cloudflared proxies a request that passed Cloudflare Access."""
    return {"Cf-Access-Jwt-Assertion": token or cf_token(), "Cf-Connecting-Ip": "203.0.113.9",
            "Cf-Ray": "abc-BOM", "X-Forwarded-For": "203.0.113.9"}


@pytest.fixture
def cf(monkeypatch):
    """Cloudflare Access configured; the team's public key is _KEY's (no network)."""
    monkeypatch.setattr(config, "CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setattr(config, "CF_ACCESS_AUD", [AUD])
    monkeypatch.setattr(config, "CF_ACCESS_EMAILS", [ME])

    class Jwks:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": _KEY.public_key()})()
    monkeypatch.setattr(access, "_jwk_client", lambda: Jwks())


def test_cloudflare_requests_are_never_local(c):
    """cloudflared connects from 127.0.0.1: even with only one Cloudflare header it must not pass as this PC."""
    for h in ({"Cf-Ray": "abc"}, {"Cf-Connecting-Ip": "203.0.113.9"}, {"Cf-Access-Jwt-Assertion": "x"}):
        assert get(c, "/api/trading", addr="127.0.0.1", headers=h).status_code == 401, h


def test_logged_in_cloudflare_device_can_trade(c, cf, monkeypatch):
    placed = []
    monkeypatch.setattr(manual, "buy_stock", lambda *a: placed.append(a) or {"order_id": "X1"})
    h = cf_headers()
    login(c, addr="127.0.0.1", headers=h)
    st = get(c, "/api/access", "127.0.0.1", h).get_json()
    assert st["can_trade"] is True and st["device"] == f"{ME} via Cloudflare"
    r = post(c, "/api/stocks/buy", {"symbol": "TCS", "qty": 1, "price": 3000}, "127.0.0.1", h)
    assert r.status_code == 200 and placed


def test_cloudflare_without_pin_login_cannot_trade(c, cf):
    assert post(c, "/api/stocks/buy", {"symbol": "TCS", "qty": 1}, "127.0.0.1", cf_headers()).status_code == 401


@pytest.mark.parametrize("token", [
    "not-a-jwt",
    cf_token(key=_OTHER_KEY),                       # signed by someone else
    cf_token(aud="another-app"),                    # token for a different Access app
    cf_token(iss="https://evil.cloudflareaccess.com"),
    cf_token(exp_in=-60),                           # expired
    cf_token(email="stranger@example.com"),         # passed Access, but not an allowed email
], ids=["garbage", "wrong-key", "wrong-aud", "wrong-issuer", "expired", "email-not-allowed"])
def test_bad_cloudflare_tokens_can_only_look(c, cf, token):
    h = cf_headers(token)
    login(c, addr="127.0.0.1", headers=h)
    st = get(c, "/api/access", "127.0.0.1", h).get_json()
    assert st["logged_in"] is True and st["can_trade"] is False
    r = post(c, "/api/orders/cancel", {"order_id": "1"}, "127.0.0.1", h)
    assert r.status_code == 403


def test_cloudflare_token_must_come_through_cloudflared_on_this_machine(c, cf):
    """A valid token replayed straight at the dashboard from elsewhere doesn't count."""
    h = cf_headers()
    login(c, addr=LAN, headers=h)
    assert get(c, "/api/access", LAN, h).get_json()["can_trade"] is False


def test_cloudflare_not_configured_means_look_only(c, cf, monkeypatch):
    monkeypatch.setattr(config, "CF_ACCESS_AUD", [])
    h = cf_headers()
    login(c, addr="127.0.0.1", headers=h)
    assert get(c, "/api/access", "127.0.0.1", h).get_json()["can_trade"] is False
