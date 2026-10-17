"""Your watchlist (NSE stocks with live prices) and price alerts ("tell me when TCS crosses ₹2,500").

Alerts are checked by the dashboard every few seconds while NSE is open, against Kotak's live quotes; each fires
once, posts to Telegram and stays in the list as triggered until you delete it. Read-only towards Kotak."""
import html
import logging
from datetime import datetime

from . import kotak, markets, state, telegram_bot

log = logging.getLogger(__name__)
MAX_ITEMS = 50
MAX_ALERTS = 100
OPS = ("above", "below")


def _conn():
    con = state._conn()
    con.execute("CREATE TABLE IF NOT EXISTS watchlist (symbol TEXT PRIMARY KEY, added_at TEXT)")
    con.execute("""CREATE TABLE IF NOT EXISTS price_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT,
        op TEXT, price REAL, note TEXT, created_at TEXT, triggered_at TEXT, triggered_price REAL)""")
    return con


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _symbol(symbol: str) -> str:
    s = str(symbol or "").strip().upper()
    if s not in kotak.tokens():
        raise ValueError(f"{s or 'That'} is not an NSE equity")
    return s


def symbols() -> list[str]:
    with _conn() as con:
        return [r[0] for r in con.execute("SELECT symbol FROM watchlist ORDER BY added_at")]


def add(symbol: str) -> str:
    s = _symbol(symbol)
    with _conn() as con:
        if con.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] >= MAX_ITEMS:
            raise ValueError(f"The watchlist holds at most {MAX_ITEMS} stocks")
        con.execute("INSERT OR IGNORE INTO watchlist (symbol, added_at) VALUES (?, ?)", (s, _now()))
    return s


def remove(symbol: str) -> None:
    with _conn() as con:
        con.execute("DELETE FROM watchlist WHERE symbol = ?", (str(symbol).strip().upper(),))


def alerts() -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM price_alerts ORDER BY triggered_at IS NOT NULL, created_at DESC")]


def add_alert(symbol: str, op: str, price, note: str = "") -> dict:
    s, op = _symbol(symbol), str(op or "").lower()
    if op not in OPS:
        raise ValueError("Choose above or below")
    try:
        price = round(float(price), 2)
    except (TypeError, ValueError):
        raise ValueError("The price must be a number")
    if price <= 0:
        raise ValueError("The price must be above 0")
    with _conn() as con:
        if con.execute("SELECT COUNT(*) FROM price_alerts WHERE triggered_at IS NULL").fetchone()[0] >= MAX_ALERTS:
            raise ValueError(f"At most {MAX_ALERTS} active alerts")
        cur = con.execute("INSERT INTO price_alerts (symbol, op, price, note, created_at) VALUES (?, ?, ?, ?, ?)",
                          (s, op, price, str(note or "")[:120], _now()))
        rid = cur.lastrowid
    return next(a for a in alerts() if a["id"] == rid)


def delete_alert(alert_id: int) -> None:
    with _conn() as con:
        con.execute("DELETE FROM price_alerts WHERE id = ?", (int(alert_id),))


def view() -> dict:
    """Watchlist with live prices and day change, plus every alert."""
    syms = symbols()
    alert_rows = alerts()
    q = kotak.quotes(sorted(set(syms) | {a["symbol"] for a in alert_rows if not a["triggered_at"]}))
    items = []
    for s in syms:
        x = q.get(s) or {}
        ltp, prev = x.get("ltp"), x.get("prev_close")
        items.append({"symbol": s, "ltp": ltp, "prev_close": prev, "high": x.get("high"), "low": x.get("low"),
                      "change": round(ltp - prev, 2) if ltp and prev else None,
                      "change_pct": round((ltp - prev) / prev * 100, 2) if ltp and prev else None,
                      "alerts": sum(1 for a in alert_rows if a["symbol"] == s and not a["triggered_at"])})
    for a in alert_rows:
        a["ltp"] = (q.get(a["symbol"]) or {}).get("ltp")
    return {"items": items, "alerts": alert_rows, "live": bool(q),
            "as_of": datetime.now().isoformat(timespec="seconds")}


def check(now: datetime | None = None) -> int:
    """Fire every active alert whose price was crossed. NSE hours only. Returns how many fired."""
    if not markets.get("IN").is_open(now):
        return 0
    active = [a for a in alerts() if not a["triggered_at"]]
    if not active:
        return 0
    q = kotak.quotes(sorted({a["symbol"] for a in active}))
    fired = 0
    for a in active:
        ltp = (q.get(a["symbol"]) or {}).get("ltp")
        if ltp is None or not (ltp >= a["price"] if a["op"] == "above" else ltp <= a["price"]):
            continue
        with _conn() as con:
            con.execute("UPDATE price_alerts SET triggered_at = ?, triggered_price = ? WHERE id = ?",
                        (_now(), ltp, a["id"]))
        try:
            telegram_bot.send_message(f"🔔 <b>{a['symbol']}</b> is {a['op']} ₹{a['price']:,.2f} · now ₹{ltp:,.2f}"
                                      + (f"\n{html.escape(a['note'])}" if a["note"] else ""))
        except Exception:
            log.exception("Telegram price alert failed")
        from . import push
        push.send(f"🔔 {a['symbol']} {a['op']} ₹{a['price']:,.2f}", f"Now ₹{ltp:,.2f}" + (f" · {a['note']}" if a["note"] else ""),
                  "/#IN/watchlist")
        fired += 1
    return fired
