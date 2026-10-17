"""Watchlist and price alerts (watchlist.py, /api/watchlist, /api/alerts), order history (orderbook.save_history,
/api/orders/history) and signal charts (/api/chart). Kotak and Yahoo are faked."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from algotrade import broker, kotak, orderbook, portfolio, telegram_bot, watchlist, web
from conftest import Recorder

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def quotes(monkeypatch):
    px = {"TCS": {"ltp": 2490.0, "prev_close": 2450.0, "high": 2500.0, "low": 2440.0},
          "INFY": {"ltp": 1500.0, "prev_close": None, "high": 1510.0, "low": 1490.0}}
    monkeypatch.setattr(kotak, "tokens", lambda: {"TCS": "1", "INFY": "2", "RELIANCE": "3"})
    monkeypatch.setattr(kotak, "quotes", lambda syms: {s: px[s] for s in syms if s in px})
    monkeypatch.setattr(type(watchlist.markets.get("IN")), "is_open", lambda self, now=None: True)
    rec = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", rec)
    px_ = px
    px_["msgs"] = rec
    return px


def test_watchlist_add_view_remove(quotes):
    c = web.app.test_client()
    assert c.post("/api/watchlist", json={"symbol": "tcs"}).get_json() == {"symbol": "TCS"}
    c.post("/api/watchlist", json={"symbol": "INFY"})
    c.post("/api/watchlist", json={"symbol": "TCS"})                 # no duplicates
    j = c.get("/api/watchlist").get_json()
    assert [i["symbol"] for i in j["items"]] == ["TCS", "INFY"]
    assert (j["items"][0]["change"], j["items"][0]["change_pct"]) == (40.0, 1.63)
    assert j["items"][1]["change"] is None                           # no previous close: no change shown
    r = c.post("/api/watchlist", json={"symbol": "NOPE"})
    assert r.status_code == 400 and "not an NSE equity" in r.get_json()["error"]
    c.post("/api/watchlist/remove", json={"symbol": "TCS"})
    assert watchlist.symbols() == ["INFY"]


def test_alert_fires_once(quotes):
    c = web.app.test_client()
    a = c.post("/api/alerts", json={"symbol": "TCS", "op": "above", "price": 2500, "note": "breakout <go>"}).get_json()
    c.post("/api/alerts", json={"symbol": "TCS", "op": "below", "price": 2400})
    assert watchlist.check() == 0
    quotes["TCS"]["ltp"] = 2503.4
    assert watchlist.check() == 1 and watchlist.check() == 0
    text = quotes["msgs"].calls[0][0][0]
    assert "TCS</b> is above ₹2,500.00 · now ₹2,503.40" in text and "breakout &lt;go&gt;" in text
    rows = {x["id"]: x for x in watchlist.alerts()}
    assert rows[a["id"]]["triggered_price"] == 2503.4
    assert c.post("/api/alerts/delete", json={"id": a["id"]}).status_code == 200
    assert len(watchlist.alerts()) == 1


def test_alerts_only_in_market_hours(quotes, monkeypatch):
    watchlist.add_alert("TCS", "below", 2600)
    monkeypatch.setattr(type(watchlist.markets.get("IN")), "is_open", lambda self, now=None: False)
    assert watchlist.check() == 0


@pytest.mark.parametrize("body,msg", [({"symbol": "TCS", "op": "near", "price": 1}, "above or below"),
                                      ({"symbol": "TCS", "op": "above", "price": 0}, "above 0"),
                                      ({"symbol": "TCS", "op": "above", "price": "x"}, "a number"),
                                      ({"symbol": "ZZZ", "op": "above", "price": 1}, "not an NSE equity")])
def test_bad_alerts(quotes, body, msg):
    r = web.app.test_client().post("/api/alerts", json=body)
    assert r.status_code == 400 and msg in r.get_json()["error"]


def test_watchlist_needs_login_from_other_devices(quotes):
    r = web.app.test_client().post("/api/watchlist", json={"symbol": "TCS"}, environ_base={"REMOTE_ADDR": "192.168.1.4"})
    assert r.status_code == 401 and watchlist.symbols() == []


# ---- order history --------------------------------------------------------------------------------

def book_row(oid, st="complete", t=None):
    t = t or date.today().strftime("%d-%b-%Y") + " 10:04:16"
    return {"nOrdNo": oid, "trdSym": "TCS-EQ", "trnsTp": "B", "prcTp": "L", "prod": "CNC", "qty": "4", "prc": "3000",
            "ordSt": st, "fldQty": "4" if st == "complete" else "0", "avgPrc": "2999", "ordDtTm": t}


def test_history_kept_and_updated(monkeypatch):
    orderbook.save_history(portfolio._orders([book_row("A1", "open")]))
    orderbook.save_history(portfolio._orders([book_row("A1", "complete")]))   # later read: same order, new state
    yesterday = date.fromordinal(date.today().toordinal() - 1).strftime("%d-%b-%Y") + " 09:30:00"
    orderbook.save_history(portfolio._orders([book_row("A0", "rejected", yesterday)]))
    h = orderbook.history(30)
    assert [(o["order_id"], o["state"]) for o in h] == [("A1", "COMPLETED"), ("A0", "REJECTED")]   # newest first
    assert {o["order_id"]: o["source"] for o in h}["A1"] == "Kotak app / web"
    j = web.app.test_client().get("/api/orders/history?days=30").get_json()
    assert len(j["orders"]) == 2 and j["keep_days"] == orderbook.HISTORY_DAYS


def test_old_history_dropped():
    orderbook.save_history([{"order_id": "OLD", "time": "01-Jan-2020 10:00:00", "state": "COMPLETED"}])
    orderbook.save_history([{"order_id": "NEW", "time": "", "state": "PENDING"}])     # no time: today
    assert [o["order_id"] for o in orderbook.history(90)] == ["NEW"]


def test_snapshot_saves_the_whole_book(monkeypatch):
    monkeypatch.setattr(broker, "call", lambda method, **kw: {"data": [book_row("B1"), book_row("B2", "open")]})
    assert orderbook.snapshot() == 2 and len(orderbook.history()) == 2


# ---- signal chart ---------------------------------------------------------------------------------

def test_chart_bars_cached(monkeypatch):
    calls = []
    idx = pd.bdate_range("2026-01-01", periods=200)
    df = pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 10.0}, index=idx)

    def fetch(symbols, tf, period=None, market="IN"):
        calls.append((symbols, tf, period, market))
        return {"TCS": df}
    monkeypatch.setattr("algotrade.data.fetch_ohlcv", fetch)
    monkeypatch.setattr(kotak, "enabled", lambda: False)
    monkeypatch.setattr(web, "_charts", {})
    c = web.app.test_client()
    j = c.get("/api/chart?symbol=tcs&market=IN&tf=1d").get_json()
    assert len(j["bars"]) == web.CHART_BARS and j["bars"][-1] == {"t": idx[-1].isoformat(), "o": 1.0, "h": 2.0,
                                                                   "l": 0.5, "c": 1.5}
    c.get("/api/chart?symbol=TCS&market=IN&tf=1d")
    assert calls == [(["TCS"], "1d", "1y", "IN")]
    assert c.get("/api/chart?symbol=TCS&tf=7m").status_code == 400
