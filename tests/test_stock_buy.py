"""Buying stocks from the Portfolio (manual.buy_stock, /api/stocks/*). Kotak is faked."""
from datetime import datetime

import pytest

from algotrade import broker, config, kotak, manual, portfolio, telegram_bot, web
from conftest import Recorder


class Placed(list):
    msgs: Recorder


@pytest.fixture
def kotak_fake(monkeypatch):
    placed = Placed()

    def call(method, **kw):
        assert method == "place_order", method
        placed.append(kw)
        return {"nOrdNo": f"B{len(placed)}", "stat": "Ok"}
    monkeypatch.setattr(broker, "call", call)
    monkeypatch.setattr(kotak, "tokens", lambda: {"RELIANCE": "2885", "TCS": "11536"})
    monkeypatch.setattr(kotak, "quotes", lambda syms: {s: {"ltp": 1215.0, "open": 1200.0, "high": 1220.0, "low": 1195.0,
                                                           "volume": 1.0, "time": datetime(2026, 10, 15, 15, 29)}
                                                       for s in syms if s == "RELIANCE"})
    monkeypatch.setattr(manual.markets.get("IN").__class__, "is_open", lambda self, now=None: True)
    monkeypatch.setattr(config, "STOCK_MAX_ORDER_VALUE", 100000.0)
    placed.msgs = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", placed.msgs)
    return placed


def test_limit_buy_delivery(kotak_fake):
    o = manual.buy_stock("reliance", 10, "LIMIT", 1214.97)
    p = kotak_fake[0]
    assert {k: p[k] for k in ("exchange_segment", "product", "order_type", "price", "quantity", "transaction_type",
                               "trading_symbol", "amo")} == \
        {"exchange_segment": "nse_cm", "product": "CNC", "order_type": "L", "price": "1214.95", "quantity": "10",
         "transaction_type": "B", "trading_symbol": "RELIANCE-EQ", "amo": "NO"}
    assert o["order_id"] == "B1" and o["value"] == pytest.approx(12149.5)
    assert "Manual BUY RELIANCE" in kotak_fake.msgs.calls[0][0][0] and manual.recent()[0]["side"] == "BUY"


def test_market_intraday_after_hours(kotak_fake, monkeypatch):
    monkeypatch.setattr(manual.markets.get("IN").__class__, "is_open", lambda self, now=None: False)
    o = manual.buy_stock("RELIANCE", 5, "MARKET", None, "MIS")
    p = kotak_fake[0]
    # SEBI: no market orders via the API -> a limit 1% above the live 1215, which also caps the value
    assert (p["order_type"], p["price"], p["product"], p["amo"]) == ("L", "1227.15", "MIS", "YES")
    assert o["value"] == pytest.approx(6135.75) and manual.recent()[0]["order_type"] == "MARKET"


def test_market_buy_needs_a_live_price(kotak_fake):
    with pytest.raises(ValueError, match="No live price for TCS"):
        manual.buy_stock("TCS", 1, "MARKET")
    assert kotak_fake == []


@pytest.mark.parametrize("args,msg", [
    (("NOTASTOCK", 1, "LIMIT", 10), "not an NSE equity"),
    (("RELIANCE", 0, "LIMIT", 10), "whole number"),
    (("RELIANCE", 1.5, "LIMIT", 10), "whole number"),
    (("RELIANCE", 1, "LIMIT", None), "needs a price"),
    (("RELIANCE", 1, "STOP", 10), "Order type"),
    (("RELIANCE", 1, "LIMIT", 10, "NRML"), "Product must be"),
    (("RELIANCE", 100, "LIMIT", 1215), "above STOCK_MAX_ORDER_VALUE"),   # ₹1,21,500 > ₹1,00,000
    (("RELIANCE", 100, "MARKET", None), "above STOCK_MAX_ORDER_VALUE"),  # checked at the live price
    (("TCS", 1, "MARKET", None), "No live price"),                       # market order needs a price to check
])
def test_refusals(kotak_fake, args, msg):
    with pytest.raises(ValueError, match=msg):
        manual.buy_stock(*args)
    assert kotak_fake == []


def test_kotak_refusal_recorded(kotak_fake, monkeypatch):
    def refuse(method, **kw):
        raise broker.BrokerError("place_order failed: RMS: insufficient funds")
    monkeypatch.setattr(broker, "call", refuse)
    with pytest.raises(broker.BrokerError):
        manual.buy_stock("RELIANCE", 1, "LIMIT", 1200)
    assert "insufficient funds" in manual.recent()[0]["error"]


def test_stock_quote(kotak_fake):
    q = manual.stock_quote("reliance")
    assert q["symbol"] == "RELIANCE" and q["ltp"] == 1215.0 and q["time"] == "2026-10-15T15:29:00"
    with pytest.raises(ValueError, match="not an NSE equity"):
        manual.stock_quote("XYZ")
    with pytest.raises(ValueError, match="No live price"):
        manual.stock_quote("TCS")


def test_api(kotak_fake, monkeypatch):
    monkeypatch.setattr(portfolio, "_cache", {"at": 1.0, "data": {"x": 1}})
    c = web.app.test_client()
    j = c.get("/api/stocks/symbols").get_json()
    assert j == {"symbols": ["RELIANCE", "TCS"], "max_value": 100000.0}
    assert c.get("/api/stocks/quote?s=RELIANCE").get_json()["ltp"] == 1215.0
    assert c.get("/api/stocks/quote?s=XYZ").status_code == 400
    body = {"symbol": "RELIANCE", "qty": 2, "order_type": "LIMIT", "price": 1210, "product": "CNC"}
    r = c.post("/api/stocks/buy", json=body, environ_base={"REMOTE_ADDR": "192.168.1.3"})
    assert r.status_code == 401 and kotak_fake == []
    assert c.post("/api/stocks/buy", json={**body, "qty": 1000}).status_code == 400
    r = c.post("/api/stocks/buy", json=body)
    assert r.status_code == 200 and r.get_json()["order_id"] == "B1" and portfolio._cache["data"] is None
