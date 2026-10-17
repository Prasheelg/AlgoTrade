"""Selling demat holdings from Portfolio > Holdings (manual.sell_holding, /api/holdings/sell). Kotak is faked."""
import pytest

from algotrade import broker, kotak as kotak_api, manual, orders, portfolio, telegram_bot, web
from conftest import Recorder

HOLD = [{"displaySymbol": "TCS", "quantity": "10", "sellableQuantity": "8", "averagePrice": "3000", "ltp": "3150"},
        {"displaySymbol": "INFY", "quantity": "5", "averagePrice": "1500"}]


class Placed(list):
    msgs: Recorder


@pytest.fixture
def kotak(monkeypatch):
    placed = Placed()

    def call(method, **kw):
        if method == "holdings":
            return {"data": HOLD}
        assert method == "place_order", method
        placed.append(kw)
        return {"nOrdNo": f"S{len(placed)}", "stat": "Ok"}
    monkeypatch.setattr(broker, "call", call)
    monkeypatch.setattr(manual.markets.get("IN").__class__, "is_open", lambda self, now=None: True)
    ltp = {"TCS": 3150.0, "INFY": 1500.0}
    monkeypatch.setattr(kotak_api, "quotes", lambda syms: {s: {"ltp": ltp[s]} for s in syms if s in ltp})
    placed.msgs = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", placed.msgs)
    return placed


def test_sellable_quantity_in_normaliser():
    assert portfolio.norm_holding(HOLD[0])["sellable"] == 8
    assert portfolio.norm_holding(HOLD[1])["sellable"] == 5          # not reported: all of it


def test_limit_sell(kotak):
    o = manual.sell_holding("tcs", 8, "LIMIT", 3151.23)
    p = kotak[0]
    assert {k: p[k] for k in ("exchange_segment", "product", "order_type", "price", "quantity", "transaction_type",
                               "trading_symbol", "amo")} == \
        {"exchange_segment": "nse_cm", "product": "CNC", "order_type": "L", "price": "3151.25", "quantity": "8",
         "transaction_type": "S", "trading_symbol": "TCS-EQ", "amo": "NO"}
    assert o["order_id"] == "S1" and manual.recent()[0]["tsym"] == "TCS-EQ"
    assert "Manual SELL TCS" in kotak.msgs.calls[0][0][0]


def test_market_sell_after_hours_is_amo(kotak, monkeypatch):
    monkeypatch.setattr(manual.markets.get("IN").__class__, "is_open", lambda self, now=None: False)
    manual.sell_holding("INFY", 5, "MARKET")
    assert (kotak[0]["order_type"], kotak[0]["price"], kotak[0]["amo"]) == ("L", "1485.00", "YES")   # 1% under 1500


@pytest.mark.parametrize("args,msg", [
    (("TCS", 9, "LIMIT", 3100), "at most 8 TCS"),           # more than the sellable quantity
    (("WIPRO", 1, "LIMIT", 500), "at most 0 WIPRO"),          # not held
    (("TCS", 0, "LIMIT", 3100), "whole number"),
    (("TCS", 2.5, "LIMIT", 3100), "whole number"),
    (("TCS", 1, "STOP", 3100), "Order type"),
    (("TCS", 1, "LIMIT", None), "needs a price"),
])
def test_refusals(kotak, args, msg):
    with pytest.raises(ValueError, match=msg):
        manual.sell_holding(*args)
    assert kotak == []


def test_auto_trade_stock_refused(kotak):
    orders._save("TCS|BUY|2026-10-14", symbol="TCS", state="OPEN", qty=3, created_day="2026-10-14")
    with pytest.raises(ValueError, match="managed by Auto-trade"):
        manual.sell_holding("TCS", 1, "MARKET")
    orders._save("TCS|BUY|2026-10-14", state="CLOSED")
    manual.sell_holding("TCS", 1, "MARKET")                   # closed app trade: allowed again
    assert len(kotak) == 1


def test_kotak_refusal_recorded(kotak, monkeypatch):
    def refuse(method, **kw):
        if method == "holdings":
            return {"data": HOLD}
        raise broker.BrokerError("place_order failed: DP authorisation (TPIN) required")
    monkeypatch.setattr(broker, "call", refuse)
    with pytest.raises(broker.BrokerError):
        manual.sell_holding("TCS", 1, "MARKET")
    assert "TPIN" in manual.recent()[0]["error"]


def test_api_local_only_and_clears_cache(kotak, monkeypatch):
    monkeypatch.setattr(portfolio, "_cache", {"at": 1.0, "data": {"x": 1}})
    c = web.app.test_client()
    body = {"symbol": "TCS", "qty": 2, "order_type": "LIMIT", "price": 3150}
    r = c.post("/api/holdings/sell", json=body, environ_base={"REMOTE_ADDR": "192.168.1.4"})
    assert r.status_code == 401 and kotak == []
    assert c.post("/api/holdings/sell", json={**body, "qty": 50}).status_code == 400
    r = c.post("/api/holdings/sell", json=body)
    assert r.status_code == 200 and r.get_json()["order_id"] == "S1" and portfolio._cache["data"] is None
