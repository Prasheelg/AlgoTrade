"""Trade journal: your notes, tags (e.g. "Entered late", "Moved stop") and a chart screenshot on any signal.

Kept in signals.db (journal table; images in data/journal/). Performance > Tag shows which tags win and lose,
so recurring mistakes become visible."""
import hashlib
import json
from datetime import datetime

from . import config, state

TAGS = ["Followed the plan", "Good setup", "Entered late", "Entered early", "Exited early", "Moved stop",
        "Oversized", "FOMO", "Ignored a rule", "News / event"]
MAX_NOTE = 4000
MAX_TAGS = 12
MAX_IMAGE = 6 * 1024 * 1024
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _conn():
    con = state._conn()
    con.execute("CREATE TABLE IF NOT EXISTS journal (key TEXT PRIMARY KEY, note TEXT, tags TEXT, image TEXT, updated_at TEXT)")
    return con


def image_dir():
    d = config.DATA_DIR / "journal"
    d.mkdir(exist_ok=True)
    return d


def _row(r) -> dict:
    return {"key": r["key"], "note": r["note"] or "", "tags": json.loads(r["tags"] or "[]"), "image": r["image"],
            "updated_at": r["updated_at"]}


def get(key: str) -> dict:
    with _conn() as con:
        r = con.execute("SELECT * FROM journal WHERE key = ?", (key,)).fetchone()
    return _row(r) if r else {"key": key, "note": "", "tags": [], "image": None, "updated_at": None}


def all_entries() -> dict[str, dict]:
    with _conn() as con:
        return {r["key"]: _row(r) for r in con.execute("SELECT * FROM journal")}


def _check_key(key: str) -> None:
    if not isinstance(key, str) or not key or not any(s["key"] == key for s in state.all_signals()):
        raise ValueError("Unknown signal")


def save(key: str, note: str | None = None, tags: list | None = None) -> dict:
    _check_key(key)
    cur = get(key)
    if note is not None:
        cur["note"] = str(note)[:MAX_NOTE]
    if tags is not None:
        if not isinstance(tags, list):
            raise ValueError("Tags must be a list")
        cur["tags"] = list(dict.fromkeys(str(t).strip()[:40] for t in tags if str(t).strip()))[:MAX_TAGS]
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO journal (key, note, tags, image, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (key, cur["note"], json.dumps(cur["tags"]), cur["image"], datetime.now().isoformat(timespec="seconds")))
    return get(key)


def save_image(key: str, data: bytes, mimetype: str) -> dict:
    _check_key(key)
    if mimetype not in IMAGE_TYPES:
        raise ValueError("A JPEG, PNG or WebP image, please")
    if not data or len(data) > MAX_IMAGE:
        raise ValueError(f"The image must be under {MAX_IMAGE // 1024 // 1024} MB")
    name = hashlib.sha1(key.encode()).hexdigest()[:16] + IMAGE_TYPES[mimetype]
    for old in image_dir().glob(name.split(".")[0] + ".*"):
        old.unlink(missing_ok=True)
    (image_dir() / name).write_bytes(data)
    cur = get(key)
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO journal (key, note, tags, image, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (key, cur["note"], json.dumps(cur["tags"]), name, datetime.now().isoformat(timespec="seconds")))
    return get(key)


def delete_image(key: str) -> dict:
    cur = get(key)
    if cur["image"]:
        (image_dir() / cur["image"]).unlink(missing_ok=True)
        with _conn() as con:
            con.execute("UPDATE journal SET image = NULL WHERE key = ?", (key,))
    return get(key)
