"""Who may use the dashboard, and who may trade from it.

- This PC (a direct request to 127.0.0.1) needs no login, as before.
- Any other device (phone app, another PC) logs in with DASHBOARD_PIN from .env. Without a PIN set, only this PC
  can open the dashboard. Five wrong PINs lock logins for 15 minutes; lockouts and logins are posted to Telegram.
- Real orders (buy, sell, cancel, modify, Auto-trade ON) need this PC, or a logged-in device on your Tailscale
  network: reached through `tailscale serve` (which proxies from 127.0.0.1 and adds the Tailscale-User-Login
  header) or directly on a Tailscale address (100.64.0.0/10). A logged-in device on the open LAN can only look.
- Switching Auto-trade OFF works from anywhere, so trading can always be stopped.
Sessions are signed cookies (30 days). Changing DASHBOARD_PIN logs every device out."""
import hashlib
import hmac
import ipaddress
import logging
import secrets
import time
from datetime import timedelta

from flask import jsonify, redirect, request, session

from . import config, telegram_bot

log = logging.getLogger(__name__)
LOCAL_ADDRS = ("127.0.0.1", "::1")
TAILNET = ipaddress.ip_network("100.64.0.0/10")
MAX_FAILS, LOCK_SEC = 5, 15 * 60
MIN_PIN = 6
SESSION_DAYS = 30
# Reachable without a login: the login page itself, and what a phone needs to install the app
OPEN_PATHS = ("/login", "/api/login", "/offline", "/manifest.webmanifest", "/sw.js", "/icons/")
_fails = {"n": 0, "until": 0.0}


def _secret() -> bytes:
    """Cookie signing key, made once and kept in data/ (never in .env, never logged)."""
    f = config.DATA_DIR / ".web_secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32), encoding="utf-8")
    return bytes.fromhex(f.read_text(encoding="utf-8").strip())


def setup(app) -> None:
    app.secret_key = _secret()
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS))
    app.before_request(_gate)


def _pin_tag() -> str:
    return hashlib.sha256(f"algotrade:{config.DASHBOARD_PIN}".encode()).hexdigest()[:16]


def is_local() -> bool:
    """A request typed into this PC's browser: loopback and not forwarded by a proxy (tailscale serve)."""
    h = request.headers
    return (request.remote_addr in LOCAL_ADDRS and "X-Forwarded-For" not in h
            and "Tailscale-User-Login" not in h)


def via_tailscale() -> bool:
    if request.remote_addr in LOCAL_ADDRS and request.headers.get("Tailscale-User-Login"):
        return True
    try:
        return ipaddress.ip_address(request.remote_addr or "") in TAILNET
    except ValueError:
        return False


def pin_ok() -> bool:
    return len(config.DASHBOARD_PIN) >= MIN_PIN


def logged_in() -> bool:
    return pin_ok() and session.get("pin") == _pin_tag()


def can_trade() -> bool:
    return is_local() or (logged_in() and via_tailscale())


TRADE_REFUSED = ("Orders can only be placed from this PC, or from your phone app over Tailscale "
                 "(logged in). See README > Phone app.")


def who() -> str:
    """Short description of the device, for logs and Telegram."""
    if is_local():
        return "this PC"
    user = request.headers.get("Tailscale-User-Login")
    return f"{user} via Tailscale" if user else str(request.remote_addr)


def status() -> dict:
    return {"local": is_local(), "logged_in": logged_in(), "can_trade": can_trade(), "pin_set": pin_ok(),
            "device": who()}


def _kill_switch() -> bool:
    """POST /api/trading {"enabled": false} is allowed without a login: stopping trading must always work."""
    return (request.method == "POST" and request.path == "/api/trading"
            and (request.get_json(silent=True) or {}).get("enabled") is False)


def _gate():
    if is_local() or logged_in() or request.path.startswith(OPEN_PATHS) or _kill_switch():
        return None
    if request.path.startswith("/api/"):
        return jsonify({"error": "Log in first", "login": True}), 401
    return redirect("/login")


def login(pin: str) -> tuple[bool, str]:
    """Check a PIN; on success the session cookie is set. Returns (ok, message)."""
    now = time.time()
    if not pin_ok():
        return False, (f"Set DASHBOARD_PIN in .env on the PC ({MIN_PIN}+ digits) and restart the dashboard; "
                       "until then only the PC can open it.")
    if now < _fails["until"]:
        return False, f"Too many wrong PINs. Try again in {int(_fails['until'] - now) // 60 + 1} min."
    if not hmac.compare_digest(str(pin).encode(), config.DASHBOARD_PIN.encode()):
        _fails["n"] += 1
        log.warning("Wrong dashboard PIN from %s (%d)", who(), _fails["n"])
        if _fails["n"] >= MAX_FAILS:
            _fails.update(n=0, until=now + LOCK_SEC)
            _notify(f"🔒 <b>Dashboard locked</b> for {LOCK_SEC // 60} min after {MAX_FAILS} wrong PINs "
                    f"(last from {who()})")
            return False, f"Too many wrong PINs. Logins are locked for {LOCK_SEC // 60} min."
        return False, "Wrong PIN"
    _fails.update(n=0, until=0.0)
    session.clear()
    session.permanent = True
    session["pin"] = _pin_tag()
    log.info("Dashboard login from %s", who())
    _notify(f"📱 Dashboard login from {who()}")
    return True, "ok"


def logout() -> None:
    session.clear()


def _notify(text: str) -> None:
    try:
        telegram_bot.send_message(text)
    except Exception:
        log.exception("Telegram login notice failed")
