"""Activity feed (the phone's sounds): what is recorded, with which kind, and how clients read it."""
import pytest

from algotrade import activity, condor, orders, push, scanner, state, telegram_bot, tracker, web
from conftest import make_signal, sig_row


@pytest.fixture
def quiet(monkeypatch):
    """Telegram and web push on, but going nowhere."""
    sent = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda text, *a, **k: sent.append(text))
    monkeypatch.setattr(push, "send", lambda *a, **k: 0)
    return sent


def kinds():
    return [e["kind"] for e in activity.since(0)["events"]]


def test_since_starts_at_the_newest_and_then_returns_what_came_after():
    assert activity.since(-1) == {"last": 0, "events": []}
    activity.record("signal", "New signal · BUY X", "body", "/#IN/all")
    activity.record("bogus", "", "unknown kinds count as info")
    first = activity.since(-1)
    assert first == {"last": 2, "events": []}        # a client that just started doesn't ring for the past
    activity.record("profit", "", "<b>X</b> closed")
    j = activity.since(first["last"])
    assert j["last"] == 3 and [(e["kind"], e["title"], e["body"]) for e in j["events"]] == \
        [("profit", "Closed in profit", "X closed")]  # the kind's own title; no HTML
    assert [e["kind"] for e in activity.since(0)["events"]] == ["signal", "info", "profit"]


def test_since_pages_through_a_long_backlog():
    for i in range(5):
        activity.record("info", f"n{i}")
    page = activity.since(0, limit=2)
    assert [e["title"] for e in page["events"]] == ["n0", "n1"] and page["last"] == 2
    assert [e["title"] for e in activity.since(page["last"], limit=2)["events"]] == ["n2", "n3"]


@pytest.mark.parametrize("text, kind", [
    ("🛒 <b>BUY X</b> × 5", "placed"), ("📤 <b>X</b> selling × 5", "placed"), ("✅ <b>X</b> bought × 5", "filled"),
    ("❌ <b>X</b> entry rejected", "problem"), ("🚨 <b>X</b> has NO stop-loss", "problem"),
    ("⚠️ <b>X</b> exit rejected", "problem"), ("✖️ <b>X</b> entry cancelled", "cancelled"),
    ("🛡️ <b>X</b> stop-loss placed", "info"), ("🎯 <b>X</b> target placed", "info"),
    ("🎟️ ✅ <b>NIFTY 25000 CE</b> bought", "filled"), ("something else", "info")])
def test_kind_of_notice(text, kind):
    assert activity.kind_of_notice(text) == kind


def test_kind_of_pnl():
    assert [activity.kind_of_pnl(v) for v in (None, 0.4, -0.4, 250.0, -250.0)] == \
        ["closed", "closed", "closed", "profit", "loss"]


def test_auto_trade_notices_are_recorded(quiet):
    orders._notify("🛒 <b>BUY X</b> × 5")
    orders._notify("🏁 <b>X</b> closed (target)", kind="profit")
    assert kinds() == ["placed", "profit"] and len(quiet) == 2


def test_new_signals_are_recorded(monkeypatch):
    monkeypatch.setattr(scanner, "scan", lambda **kw: [make_signal(symbol="TCS"), make_signal(symbol="GOLD", market="MCX")])
    scanner.scan_and_alert(send=False)
    evs = activity.since(0)["events"]
    assert [(e["kind"], e["title"], e["url"]) for e in evs] == [
        ("signal", "New signal · BUY TCS", "/#IN/all"), ("signal", "New signal · BUY GOLD", "/#MCX/all")]
    assert "entry ₹99.00 · stop ₹96.00 · target ₹103.50" in evs[0]["body"]
    scanner.scan_and_alert(send=False)        # seen before: no second sound
    assert len(activity.since(0)["events"]) == 2


def test_signal_status_changes_ring_unless_auto_trade_took_the_signal():
    sig = sig_row(key="X|BUY|1", entry=100.0)
    tracker._activity(sig, {"status": "ACTIVE", "exit_price": None}, 101.0)
    tracker._activity(sig, {"status": "SL_HIT", "exit_price": 98.0}, 98.0)
    tracker._activity(sig, {"status": "SL_HIT", "exit_price": 102.0}, 102.0)   # a trailed stop above entry
    tracker._activity(sig, {"status": "T1_HIT", "exit_price": 104.0}, 104.0)
    assert kinds() == ["entry", "loss", "profit", "profit"]
    assert activity.since(0)["events"][0]["url"] == "/#IN/active"
    with state._conn() as con:
        con.execute("CREATE TABLE live_trades (key TEXT PRIMARY KEY, entry_order TEXT)")
        con.execute("INSERT INTO live_trades VALUES ('X|BUY|1', 'O1')")
    tracker._activity(sig, {"status": "T1_HIT", "exit_price": 104.0}, 104.0)
    assert len(kinds()) == 4                  # the trade's own fill / close notices ring instead


def test_condor_notices_are_recorded(quiet):
    condor._log("2026-11-23", "NIFTY", "entry", "Sold the condor", notify=True)
    condor._log("2026-11-23", "NIFTY", "exit", "Day over", {"total": -1500.0}, notify=True)
    condor._log("2026-11-23", "NIFTY", "skip", "No trade today", notify=True)
    condor._log("2026-11-23", "NIFTY", "roll", "Rolled", notify=False)
    assert kinds() == ["filled", "loss", "info"]


def test_api_activity():
    c = web.app.test_client()
    assert c.get("/api/activity").get_json() == {"last": 0, "events": []}
    activity.record("alert", "🔔 X above ₹100.00", "Now ₹101.00", "/#IN/watchlist")
    assert [e["kind"] for e in c.get("/api/activity?after=0").get_json()["events"]] == ["alert"]
    assert c.get("/api/activity?after=x").status_code == 400
    assert c.get("/sounds/signal.wav").status_code == 200
