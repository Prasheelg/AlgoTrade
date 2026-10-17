"""Daily backup of signals.db (signals, app trades, manual orders, order history, watchlist, settings).

Once a day after BACKUP_AFTER IST a consistent copy is made with SQLite's backup API (safe while the scanner and
the dashboard are writing), kept in data/backups for KEEP_DAYS days and sent to the Telegram group, so the
history survives a dead disk. Off unless DB_BACKUP=true. Whichever process (scanner or dashboard) gets there first does it; the other sees
the day recorded in the shared settings and skips it."""
import logging
import sqlite3
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, markets, state, telegram_bot

log = logging.getLogger(__name__)
BACKUP_AFTER = time(16, 0)   # IST, after the close and the end-of-day scan
KEEP_DAYS = 14
SETTING = "backup_last"


def backup_dir():
    d = config.DATA_DIR / "backups"
    d.mkdir(exist_ok=True)
    return d


def make(day: str) -> Path:
    """Copy the database to data/backups/signals_<day>.db and drop copies older than KEEP_DAYS."""
    out = backup_dir() / f"signals_{day}.db"
    src = state._conn()
    src.commit()   # _conn's schema upkeep leaves a write transaction open, which the backup would wait on forever
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    old = sorted(backup_dir().glob("signals_*.db"))[:-KEEP_DAYS]
    for p in old:
        p.unlink(missing_ok=True)
    return out


def status() -> dict:
    last, at = state.get_setting(SETTING, {})
    return {**last, "at": at}


def daily(now: datetime | None = None) -> bool:
    """Make and send today's backup if it's due and not done yet. Returns True if one was made now."""
    if not config.DB_BACKUP:
        return False
    now = now or datetime.now(ZoneInfo(markets.get("IN").tz))
    day = now.date().isoformat()
    if now.time() < BACKUP_AFTER or status().get("day") == day:
        return False
    try:
        path = make(day)
    except Exception as exc:
        log.exception("Database backup failed")
        state.set_setting(SETTING, {**status(), "error": str(exc)[:200]})
        return False
    kb = path.stat().st_size / 1024
    sent = telegram_bot.send_document(path, f"🗄️ AlgoTrade daily backup · {day} · {kb:,.0f} KB")
    state.set_setting(SETTING, {"day": day, "file": path.name, "sent": bool(sent), "kb": round(kb)})
    log.info("Database backup %s (%.0f KB)%s", path.name, kb, " sent to Telegram" if sent else "")
    return True
