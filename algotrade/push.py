"""Phone notifications from the installed app itself (Web Push), alongside Telegram.

A phone turns them on in Settings; its subscription (an address at Google's / Apple's push service plus keys)
is stored here. Important events are pushed: price alerts, order fills and rejections, Auto-trade events, health
problems and the daily summary. The server signs pushes with its own VAPID key pair, made once and kept in
data/.vapid_private.pem. iPhone: works only in the installed app (Add to Home Screen), iOS 16.4 or later."""
import base64
import json
import logging
from datetime import datetime

from . import config, state

log = logging.getLogger(__name__)
CONTACT = "mailto:algotrade@localhost"   # VAPID contact claim (required by the push services)
TTL_SEC = 3600


def _conn():
    con = state._conn()
    con.execute("""CREATE TABLE IF NOT EXISTS push_subs (endpoint TEXT PRIMARY KEY, sub TEXT, device TEXT,
        created_at TEXT)""")
    return con


def _key_path():
    return config.DATA_DIR / ".vapid_private.pem"


def _vapid():
    from py_vapid import Vapid01
    path = _key_path()
    if not path.exists():
        v = Vapid01()
        v.generate_keys()
        v.save_key(str(path))
    return Vapid01.from_file(str(path))


def public_key() -> str:
    """Application server key for pushManager.subscribe (uncompressed P-256 point, base64url)."""
    from cryptography.hazmat.primitives import serialization
    raw = _vapid().public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def subscribe(sub: dict, device: str) -> None:
    if not isinstance(sub, dict) or not str(sub.get("endpoint", "")).startswith("https://") \
            or not isinstance(sub.get("keys"), dict) or not {"p256dh", "auth"} <= set(sub["keys"]):
        raise ValueError("Not a push subscription")
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO push_subs (endpoint, sub, device, created_at) VALUES (?, ?, ?, ?)",
                    (sub["endpoint"], json.dumps(sub), device[:80], datetime.now().isoformat(timespec="seconds")))


def unsubscribe(endpoint: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))


def devices() -> list[dict]:
    with _conn() as con:
        return [{"endpoint": r["endpoint"], "device": r["device"], "created_at": r["created_at"]}
                for r in con.execute("SELECT endpoint, device, created_at FROM push_subs ORDER BY created_at")]


def send(title: str, body: str, url: str = "/") -> int:
    """Push to every subscribed device; drops subscriptions the push service says are gone. Returns how many
    devices it reached. Never raises."""
    try:
        with _conn() as con:
            subs = [json.loads(r[0]) for r in con.execute("SELECT sub FROM push_subs")]
    except Exception:
        log.exception("Push subscriptions unreadable")
        return 0
    if not subs:
        return 0
    from pywebpush import WebPushException, webpush
    payload = json.dumps({"title": title[:80], "body": body[:240], "url": url})
    sent = 0
    for sub in subs:
        try:
            webpush(sub, payload, vapid_private_key=str(_key_path()), vapid_claims={"sub": CONTACT}, ttl=TTL_SEC,
                    timeout=10)
            sent += 1
        except WebPushException as exc:
            code = getattr(exc.response, "status_code", None)
            if code in (404, 410):
                unsubscribe(sub["endpoint"])
                log.info("Push subscription gone (%s); removed", code)
            else:
                log.warning("Push failed: %s", exc)
        except Exception as exc:
            log.warning("Push failed: %s", exc)
    return sent
