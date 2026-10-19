"""Kotak Neo trading session: unattended TOTP + MPIN login and account reads.

Credentials come from .env: KOTAK_CONSUMER_KEY, KOTAK_MOBILE (+91...), KOTAK_UCC, KOTAK_MPIN and
KOTAK_TOTP_SECRET (the base32 secret behind the authenticator QR code), so the app can log in by itself.
Anyone who can read .env can trade the account. A session is kept for the day and renewed on failure.
This module never logs or prints a credential."""
import base64
import hashlib
import hmac
import logging
import struct
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config, state

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
CREDENTIALS = ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN", "KOTAK_TOTP_SECRET")

_session = {"client": None, "day": None, "name": None}


class BrokerError(RuntimeError):
    pass


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 time-based one-time password (SHA-1), as an authenticator app shows it."""
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    counter = int((time.time() if at is None else at) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10 ** digits).zfill(digits)


def missing_credentials() -> list[str]:
    return [name for name in CREDENTIALS if not getattr(config, name, "")]


def _is_empty(res: dict) -> bool:
    """Kotak reports an empty list as an error, in two forms:
    {'error': [{'code': 424, 'message': 'No holdings found ...'}]} and {'stCode': 5203, 'errMsg': 'No Data', ...}."""
    if str(res.get("stCode")) == "5203" or str(res.get("errMsg", "")).lower() == "no data":
        return True
    err = res.get("error") or res.get("Error")
    errs = err if isinstance(err, list) else [err]
    return bool(err) and all(isinstance(e, dict) and str(e.get("code")) == "424"
                             and str(e.get("message", "")).lower().startswith("no ") for e in errs)


def _check(res, step: str) -> dict:
    """The SDK returns error dicts instead of raising; turn them into BrokerError (without credentials)."""
    if not isinstance(res, dict):
        raise BrokerError(f"{step}: unexpected response {str(res)[:200]}")
    if _is_empty(res):
        return {"data": []}
    err = res.get("Error") or res.get("error") or res.get("Error Message")
    if err is None and str(res.get("stat", "")).lower() == "not_ok":
        err = res.get("errMsg") or res.get("emsg") or res.get("desc") or res
    if err is not None:
        raise BrokerError(f"{step} failed: {str(err)[:300]}")
    return res


def login(force: bool = False):
    """Logged-in NeoAPI client for today, logging in if needed."""
    today = datetime.now(IST).date()
    if _session["client"] is not None and _session["day"] == today and not force:
        return _session["client"]
    missing = missing_credentials()
    if missing:
        raise BrokerError(f"Missing in .env: {', '.join(missing)}")
    from neo_api_client import NeoAPI
    logging.getLogger("neo_api_client").setLevel(logging.CRITICAL)
    try:
        client = NeoAPI(consumer_key=config.KOTAK_CONSUMER_KEY, environment="prod")
        view = _check(client.totp_login(mobile_number=config.KOTAK_MOBILE, ucc=config.KOTAK_UCC,
                                        totp=totp(config.KOTAK_TOTP_SECRET)), "TOTP login")
        _check(client.totp_validate(mpin=config.KOTAK_MPIN), "MPIN validation")
    except Exception as exc:
        _login_result(str(exc)[:300])
        raise
    data = view.get("data") or {}
    _session.update(client=client, day=today, name=data.get("greetingName"))
    _login_result(None)
    log.info("Kotak login OK (%s)", _session["name"] or config.KOTAK_UCC)
    return client


LOGIN_KEY = "kotak_login"


def _login_result(error: str | None) -> None:
    """Remember the last login attempt (shared with the other process) for the health check."""
    try:
        state.set_setting(LOGIN_KEY, {"ok": error is None, "error": error})
    except Exception:
        log.exception("Could not record the Kotak login result")


def login_status() -> dict:
    st, at = state.get_setting(LOGIN_KEY, {"ok": True, "error": None})
    return {**st, "at": at}


ORDER_METHODS = ("place_order", "modify_order", "cancel_order")


def _auth_error(msg: str) -> bool:
    """A refusal at the login / permission check, before anything is executed."""
    m = msg.lower()
    return any(k in m for k in ("unauthori", "authenticat", "2fa", "invalid session", "session expired",
                                 "token expired", "invalid token"))


def public_ip() -> str | None:
    """This PC's IP as Kotak sees it (the one to register for API orders)."""
    try:
        data = (login().whatsmyip() or {}).get("data") or []
        return data[0].get("ip") if data else None
    except Exception:
        return None


def call(method: str, **kw):
    """Call a logged-in SDK method; on failure log in again once and retry.
    Order calls (place / modify / cancel) are retried only after an authentication refusal, which happens before
    anything executes; after any other failure (timeout, server error) the order may have gone through, so a
    retry could duplicate it."""
    for attempt in (1, 2):
        client = login(force=attempt == 2)
        try:
            return _check(getattr(client, method)(**kw), method)
        except BrokerError as exc:
            auth = _auth_error(str(exc))
            if attempt == 2 or (method in ORDER_METHODS and not auth):
                if method in ORDER_METHODS and auth:
                    ip = public_ip()
                    raise BrokerError(
                        f"{exc}. Kotak accepted the login but refused order access. Usually this means this PC's "
                        f"internet IP{f' ({ip})' if ip else ''} isn't registered for Kotak Trade API orders, or the "
                        "F&O segment isn't active (option orders). Register the IP / activate the segment in Kotak, "
                        "then try again.") from exc
                raise
            log.warning("Kotak %s failed; logging in again", method)


TRADING_KEY = "trading_enabled"


def trading_status() -> dict:
    """The auto-trade switch (shared with the scanner through signals.db; off until switched on) and its limits."""
    st, changed_at = state.get_setting(TRADING_KEY, {"enabled": False})
    return {"enabled": bool(st.get("enabled")), "changed_at": changed_at, "changed_by": st.get("by"),
            "account": st.get("account"), "credentials": not missing_credentials(),
            "limits": {"max_order_value": config.MAX_ORDER_VALUE}}


def trading_enabled() -> bool:
    """Checked before every order: a switch-off takes effect on the next order, even mid-loop."""
    return trading_status()["enabled"]


def set_trading(enabled: bool, by: str) -> dict:
    """Switch automatic trading on or off. Switching on first proves the Kotak login works."""
    account_name = None
    if enabled:
        login(force=True)   # raises BrokerError if credentials are missing or wrong
        account_name = f"{_session['name'] or ''} ({config.KOTAK_UCC})".strip()
    state.set_setting(TRADING_KEY, {"enabled": bool(enabled), "by": by, "account": account_name})
    log.warning("Automatic trading %s by %s", "ENABLED" if enabled else "disabled", by)
    return trading_status()


def account() -> dict:
    """Read-only snapshot: funds, holdings, positions and today's order book (raw API responses)."""
    login()
    return {"name": _session["name"], "limits": call("limits"), "holdings": call("holdings"),
            "positions": call("positions"), "orders": call("order_report")}
