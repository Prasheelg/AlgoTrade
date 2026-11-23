"""Activity feed: one row per thing worth a sound on the phone, read by the dashboard and the Android app.

The scanner and the dashboard are separate processes, so events go through signals.db (table activity). The page
polls /api/activity while it is open and plays a tone per kind; the Android app's background service polls the same
endpoint while the app is closed and shows a notification on that kind's channel (each with its own sound).

Kinds (each has its own sound):
  signal     a new signal was found
  entry      a signal's entry price was reached (the signal is live)
  placed     an order was sent to the broker (a buy, a sell, a roll)
  filled     a trade opened: its entry order filled
  profit     a trade or signal closed in profit (target hit)
  loss       a trade or signal closed at a loss (stop hit)
  closed     closed at about breakeven, or the result isn't known (time exit, manual close)
  cancelled  an order or a signal was cancelled before it filled
  problem    needs you: a rejection, a missing stop-loss, an error, a health problem
  alert      a watchlist price alert rang
  info       quiet news: protective orders placed, daily summary, condor mode changes and skips
"""
import logging
import re
from datetime import datetime, timedelta

from . import state

log = logging.getLogger(__name__)
KINDS = ("signal", "entry", "placed", "filled", "profit", "loss", "closed", "cancelled", "problem", "alert", "info")
TITLES = {"signal": "New signal", "entry": "Signal live", "placed": "Order placed", "filled": "Trade opened",
          "profit": "Closed in profit", "loss": "Closed at a loss", "closed": "Trade closed", "cancelled": "Cancelled",
          "problem": "Needs attention", "alert": "Price alert", "info": "AlgoTrade"}
KEEP_DAYS = 7
_TAGS = re.compile(r"<[^>]+>")


def _conn():
    con = state._conn()
    con.execute("""CREATE TABLE IF NOT EXISTS activity (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, kind TEXT,
        title TEXT, body TEXT, url TEXT)""")
    return con


def record(kind: str, title: str, body: str = "", url: str = "/") -> None:
    """Add one event (title "" = the kind's own title). Never raises: a missed sound must not stop a trade."""
    if kind not in KINDS:
        kind = "info"
    title = title or TITLES[kind]
    try:
        with _conn() as con:
            cur = con.execute("INSERT INTO activity (at, kind, title, body, url) VALUES (?, ?, ?, ?, ?)",
                              (datetime.now().isoformat(timespec="seconds"), kind, _TAGS.sub("", title)[:120],
                               _TAGS.sub("", body)[:300], url))
            if cur.lastrowid % 200 == 0:   # now and then, drop what no one will poll for any more
                cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
                con.execute("DELETE FROM activity WHERE at < ?", (cutoff,))
    except Exception:
        log.exception("Activity not recorded: %s %s", kind, title)


def since(after: int, limit: int = 50) -> dict:
    """Events after id `after`, oldest first, and the newest id. after < 0 returns no events, only the newest id,
    so a client that just started doesn't ring for the past."""
    with _conn() as con:
        last = con.execute("SELECT COALESCE(MAX(id), 0) FROM activity").fetchone()[0]
        if after < 0:
            return {"last": last, "events": []}
        rows = con.execute("SELECT * FROM activity WHERE id > ? ORDER BY id LIMIT ?", (after, limit)).fetchall()
    return {"last": rows[-1]["id"] if len(rows) == limit else last, "events": [dict(r) for r in rows]}


# ---- Kinds for the existing notices ---------------------------------------------------------------------

# Auto-trade notices (orders._notify) start with an icon that says what happened
_ICONS = {"🛒": "placed", "📤": "placed", "✅": "filled", "❌": "problem", "⛔": "problem", "🚨": "problem",
          "⚠️": "problem", "✖️": "cancelled", "🛡️": "info", "🎯": "info"}


def kind_of_notice(text: str) -> str:
    t = text.lstrip()
    if t.startswith("🎟️"):   # options notices carry a ticket in front
        t = t[len("🎟️"):].lstrip()
    for icon, kind in _ICONS.items():
        if t.startswith(icon):
            return kind
    return "info"


def kind_of_pnl(pnl: float | None, flat: float = 1.0) -> str:
    """profit / loss / closed for a trade's P&L (None when unknown)."""
    if pnl is None or abs(pnl) < flat:
        return "closed"
    return "profit" if pnl > 0 else "loss"


# A tracked signal's new status
SIGNAL_STATUS = {"ACTIVE": ("entry", "Signal live"), "CANCELLED": ("cancelled", "Signal cancelled"),
                 "T1_HIT": ("profit", "Signal target hit"), "T2_HIT": ("profit", "Signal target 2 hit"),
                 "SL_HIT": ("loss", "Signal stop hit"), "EXPIRED": ("closed", "Signal time exit"),
                 "BREAKEVEN": ("closed", "Signal back to entry")}

# The 0DTE condor's log kinds
CONDOR = {"entry": "filled", "roll": "placed", "error": "problem", "cap": "problem", "mode": "info", "skip": "info",
          "check": "info"}
