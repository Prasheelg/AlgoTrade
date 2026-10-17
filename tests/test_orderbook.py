"""Your orders in the order book (orderbook.py, /api/orders/*): sources, cancel, modify, fill notices. Kotak is faked."""
from datetime import date, timedelta

import pytest

from algotrade import broker, config, manual, orderbook, orders, portfolio, telegram_bot, web
from conftest import Recorder


def row(oid, st="open", **kw):
    return {"nOrdNo": oid, "trdSym": "TCS-EQ", "exSeg": "nse_cm", "trnsTp": "B", "prcTp": "L", "prod": "CNC",
            "qty": "4", "prc": "3000", "trgPrc": "0", "ordSt": st, "fldQty": "0", "avgPrc": "0", "vldt": "DAY", **kw}


class Kotak:
    def __init__(self):
        self.book, self.calls = [], []


@pytest.fixture
def kotak(monkeypatch):
    k = Kotak()

    def call(method, **kw):
        if method == "order_report":
            return {"data": k.book}
        assert method in ("cancel_order", "modify_order"), method
        k.calls.append((method, kw))
        return {"stat": "Ok"}
    monkeypatch.setattr(broker, "call", call)
    monkeypatch.setattr(orderbook.markets.get("IN").__class__, "is_open", lambda self, now=None: True)
    k.msgs = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", k.msgs)
    return k


def manual_order(oid, side="BUY", lots=None, placed=None):
    manual._record(placed_at=placed or date.today().isoformat() + "T10:00:00", tsym="TCS-EQ", underlying="TCS",
                   side=side, lots=lots, qty=4, order_type="LIMIT", price=3000, product="CNC", amo="NO", order_id=oid)


def auto_trade(entry="A1", stop="A2"):
    orders._save("k1", symbol="TCS", state=orders.OPEN, qty=4, entry_order=entry, stop_order=stop,
                 created_day=date.today().isoformat())


def test_sources():
    manual_order("M1")
    manual_order("M2", side="SELL")
    manual_order("M3", lots=1)
    auto_trade()
    src = orderbook.sources()
    assert [src[k]["label"] for k in ("M1", "M2", "M3")] == ["Manual buy", "Holdings sell", "Options"]
    assert src["A2"] == {"kind": "auto", "label": "Auto-trade · stop-loss", "trade": "k1"}
    assert orderbook.source_of("X9", src)["kind"] == "external"


def test_portfolio_orders_carry_source(kotak, monkeypatch):
    manual_order("M1")
    monkeypatch.setattr(portfolio, "_cache", {"at": None, "data": None})
    o = portfolio._orders([row("M1"), row("Z1")])
    assert [(x["source"]["kind"], x["state"]) for x in o] == [("manual", "PENDING"), ("external", "PENDING")]


def test_cancel(kotak):
    kotak.book = [row("M1")]
    manual_order("M1")
    assert orderbook.cancel("M1")["symbol"] == "TCS-EQ"
    assert kotak.calls == [("cancel_order", {"order_id": "M1", "amo": "NO"})]


@pytest.mark.parametrize("book, msg", [([], "isn't in today's order book"),
                                       ([row("A1")], "Auto-trade"),
                                       ([row("M1", "complete")], "no longer pending")])
def test_cancel_refused(kotak, book, msg):
    auto_trade()
    kotak.book = book
    oid = "A1" if msg == "Auto-trade" else "M1"
    with pytest.raises(ValueError, match=msg):
        orderbook.cancel(oid)
    assert kotak.calls == []


def test_modify_price_and_lower_qty(kotak):
    kotak.book = [row("M1")]
    out = orderbook.modify("M1", price=3010.03, qty=3)
    assert (out["price"], out["qty"]) == (3010.05, 3)
    assert kotak.calls == [("modify_order", {"order_id": "M1", "price": "3010.05", "order_type": "L", "quantity": "3",
                                             "validity": "DAY", "trigger_price": "0", "amo": "NO"})]


def test_modify_stop_trigger(kotak):
    kotak.book = [row("M1", prcTp="SL-M", prc="0", trgPrc="2900", trnsTp="S")]
    orderbook.modify("M1", trigger=2950)
    kw = kotak.calls[0][1]
    assert (kw["price"], kw["trigger_price"], kw["order_type"]) == ("0", "2950.00", "SL-M")


@pytest.mark.parametrize("kw, msg", [({"qty": 5}, "only be lowered"), ({"qty": 2}, "already filled"),
                                     ({"trigger": 10}, "Only stop orders"), ({"price": 0}, "above 0"),
                                     ({"price": 99999}, "STOCK_MAX_ORDER_VALUE")])
def test_modify_refused(kotak, kw, msg):
    kotak.book = [row("M1", fldQty="2")]
    with pytest.raises(ValueError, match=msg):
        orderbook.modify("M1", **kw)
    assert kotak.calls == []


def test_market_order_has_no_price(kotak):
    kotak.book = [row("M1", prcTp="MKT", prc="0")]
    with pytest.raises(ValueError, match="market order"):
        orderbook.modify("M1", price=3000)


def test_api_local_only(kotak, monkeypatch):
    kotak.book = [row("M1")]
    monkeypatch.setattr(portfolio, "_cache", {"at": 1.0, "data": {"x": 1}})
    c = web.app.test_client()
    r = c.post("/api/orders/cancel", json={"order_id": "M1"}, environ_base={"REMOTE_ADDR": "192.168.1.4"})
    assert r.status_code == 401 and kotak.calls == []
    assert c.post("/api/orders/modify", json={"order_id": "M1", "qty": 9}).status_code == 409
    assert c.post("/api/orders/cancel", json={}).status_code == 400
    r = c.post("/api/orders/cancel", json={"order_id": "M1"})
    assert r.status_code == 200 and portfolio._cache["data"] is None


def test_fill_notices_sent_once(kotak):
    manual_order("M1")
    manual_order("M2", side="SELL")
    manual_order("M3")
    manual_order("M4", placed=(date.today() - timedelta(days=1)).isoformat() + "T20:00:00")
    kotak.book = [row("M1", "complete", fldQty="4", avgPrc="2999.5"), row("M2", "rejected", rejRsn="RMS: no holdings"),
                  row("M3")]
    assert orderbook.check_manual() == 2
    text = "\n".join(map(str, kotak.msgs.calls))
    assert "Filled" in text and "2,999.50" in text and "Rejected" in text and "RMS: no holdings" in text
    assert orderbook.check_manual() == 0                 # M1 / M2 reported already; M3 still pending
    kotak.book[2] = row("M3", "cancelled", fldQty="1", avgPrc="3000")
    assert orderbook.check_manual() == 1
    assert "1 of 4 filled" in str(kotak.msgs.calls[-1])
    assert orderbook._unresolved() == []               # M4: yesterday's, not in today's book -> expired


def test_pending_orders_refresh_fast(kotak, monkeypatch):
    kotak.book = [row("M1", "complete")]
    data = {"orders": [{"state": "PENDING", "order_id": "M1"}], "raw": {"orders": []}}
    monkeypatch.setattr(portfolio, "_cache", {"at": 100.0, "data": data, "orders_at": 100.0})
    portfolio._refresh_pending(102.0)
    assert portfolio._cache["data"] is data              # within PENDING_SEC: no new read
    portfolio._refresh_pending(106.0)
    assert portfolio._cache["data"]["orders"][0]["state"] == "COMPLETED"
    portfolio._refresh_pending(200.0)                    # nothing pending any more: no read
    assert portfolio._cache["orders_at"] == 106.0
