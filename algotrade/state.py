"""SQLite store of sent signals, used to avoid duplicate alerts, keep a journal and track outcomes."""
import json
import sqlite3
from datetime import datetime

from . import config

# Status lifecycle: PENDING -> (ACTIVE | CANCELLED) ; ACTIVE -> T1_HIT -> (T2_HIT | BREAKEVEN) ;
# ACTIVE -> SL_HIT ; any filled open -> EXPIRED. Signals from before pending entries start ACTIVE.
OPEN_STATUSES = ("PENDING", "ACTIVE", "T1_HIT")

_EXTRA_COLUMNS = {
    "bar_date": "TEXT", "status": "TEXT DEFAULT 'ACTIVE'", "ltp": "REAL",
    "exit_price": "REAL", "closed_at": "TEXT", "updated_at": "TEXT",
    "timeframe": "TEXT DEFAULT '1d'", "bar_time": "TEXT", "market": "TEXT DEFAULT 'IN'",
    "telegram": "INTEGER DEFAULT 1",
    "entry_type": "TEXT DEFAULT 'market'", "ref_price": "REAL", "fill_price": "REAL", "filled_at": "TEXT",  # 1 = posted to Telegram, so status updates are posted too
}


_wal: set[str] = set()   # databases already switched to WAL by this process


def _conn():
    # The scanner and the dashboard are separate processes sharing this file: wait up to 10 s for the other's
    # write instead of failing with "database is locked", and use WAL so reads never block a write.
    con = sqlite3.connect(config.STATE_DB, timeout=10)
    con.row_factory = sqlite3.Row
    if str(config.STATE_DB) not in _wal:
        con.execute("PRAGMA journal_mode=WAL")   # persistent: stored in the file
        _wal.add(str(config.STATE_DB))
    con.execute("""CREATE TABLE IF NOT EXISTS signals (
        key TEXT PRIMARY KEY, symbol TEXT, side TEXT, entry REAL, stop_loss REAL,
        target1 REAL, target2 REAL, score INTEGER, reasons TEXT, sent_at TEXT)""")
    cols = {r["name"] for r in con.execute("PRAGMA table_info(signals)")}
    for name, typ in _EXTRA_COLUMNS.items():
        if name not in cols:
            con.execute(f"ALTER TABLE signals ADD COLUMN {name} {typ}")
    con.execute("UPDATE signals SET bar_date = substr(key, -10) WHERE bar_date IS NULL")
    con.execute("UPDATE signals SET status = 'ACTIVE' WHERE status IS NULL")
    con.execute("UPDATE signals SET timeframe = '1d' WHERE timeframe IS NULL")
    con.execute("UPDATE signals SET market = 'IN' WHERE market IS NULL")
    con.execute("UPDATE signals SET bar_time = bar_date || 'T00:00:00' WHERE bar_time IS NULL")
    con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    return con


def get_setting(key: str, default=None):
    """A setting shared by the dashboard and the scanner (separate processes), stored as JSON."""
    with _conn() as con:
        row = con.execute("SELECT value, updated_at FROM settings WHERE key = ?", (key,)).fetchone()
    return (json.loads(row["value"]), row["updated_at"]) if row else (default, None)


def set_setting(key: str, value) -> str:
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as con:
        con.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                    (key, json.dumps(value), now))
    return now


def filter_new(signals) -> list:
    with _conn() as con:
        seen = {row[0] for row in con.execute("SELECT key FROM signals")}
    return [s for s in signals if s.key not in seen]


def record(signals, telegram: bool = True) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as con:
        con.executemany(
            """INSERT OR IGNORE INTO signals (key, symbol, side, entry, stop_loss, target1, target2,
               score, reasons, sent_at, bar_date, status, ltp, updated_at, timeframe, bar_time, market, telegram,
               entry_type, ref_price)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(s.key, s.symbol, s.side, s.entry, s.stop_loss, s.targets[0], s.targets[-1],
              s.score, ", ".join(s.reasons), now, str(s.bar_time.date()),
              "ACTIVE" if s.entry_type == "market" else "PENDING", s.ref_price or s.entry, now,
              s.timeframe, s.bar_time.isoformat(), s.market, int(telegram), s.entry_type, s.ref_price or None)
             for s in signals],
        )


def all_signals(market: str | None = None) -> list[dict]:
    with _conn() as con:
        if market:
            q = "SELECT * FROM signals WHERE market = ? ORDER BY sent_at DESC"
            return [dict(r) for r in con.execute(q, (market,))]
        return [dict(r) for r in con.execute("SELECT * FROM signals ORDER BY sent_at DESC")]


def open_signals() -> list[dict]:
    with _conn() as con:
        q = f"SELECT * FROM signals WHERE status IN ({','.join('?' * len(OPEN_STATUSES))})"
        return [dict(r) for r in con.execute(q, OPEN_STATUSES)]


def delete_signals(keys: list[str], market: str) -> int:
    """Delete these signals of one market (a later scan may find them again). Returns how many went."""
    if not keys:
        return 0
    with _conn() as con:
        q = f"DELETE FROM signals WHERE market = ? AND key IN ({','.join('?' * len(keys))})"
        return con.execute(q, (market, *keys)).rowcount


def update_signal(key: str, **fields) -> None:
    fields["updated_at"] = datetime.now().isoformat(timespec="seconds")
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as con:
        con.execute(f"UPDATE signals SET {sets} WHERE key = ?", (*fields.values(), key))
