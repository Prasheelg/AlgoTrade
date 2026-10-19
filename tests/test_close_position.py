"""Closing today's positions from Portfolio > Positions (manual.close_position, /api/positions/close). Kotak is faked."""
import pytest

from algotrade import broker, kotak as kotak_api, manual, orders, telegram_bot
from conftest import Recorder

CANBK = {"trdSym": "CANBK-EQ", "exSeg": "nse_cm", "prod": "CNC", "flBuyQty": "82", "flSellQty": "0",
         "buyAmt": "9990.06", "sellAmt": "0"}


class Placed(list):
    positions: list


@pytest.fixture
def kotak(monkeypatch):
    placed = Placed()
    placed.positions = [CANBK]

    def call(method, **kw):
        if method == "positions":
            return {"data": placed.positions}
        assert method == "place_order", method
        placed.append(kw)
        return {"nOrdNo": f"C{len(placed)}", "stat": "Ok"}
    monkeypatch.setattr(broker, "call", call)
    monkeypatch.setattr(manual.markets.get("IN").__class__, "is_open", lambda self, now=None: True)
    ltp = {"CANBK": 122.0, "TORNTPHARM": 4600.0}
    monkeypatch.setattr(kotak_api, "quotes", lambda syms: {s: {"ltp": ltp[s]} for s in syms if s in ltp})
    monkeypatch.setattr(kotak_api, "tick_size", lambda sym: {"CANBK": 0.01, "TORNTPHARM": 0.10}.get(sym))
    monkeypatch.setattr(telegram_bot, "send_message", Recorder(True))
    return placed


def test_long_position_is_sold_in_full_at_a_marketable_limit(kotak):
    o = manual.close_position("canbk-eq", "cnc")
    p = kotak[0]
    assert (p["transaction_type"], p["quantity"], p["product"], p["order_type"]) == ("S", "82", "CNC", "L")
    assert p["price"] == "120.78"                              # 1% below 122, on CANBK's 0.01 tick
    assert o["order_id"] == "C1"


def test_short_position_is_bought_back(kotak):
    kotak.positions = [{"trdSym": "TORNTPHARM-EQ", "exSeg": "nse_cm", "prod": "MIS", "flBuyQty": "0",
                        "flSellQty": "2", "buyAmt": "0", "sellAmt": "9200"}]
    manual.close_position("TORNTPHARM-EQ", "MIS")
    p = kotak[0]
    assert (p["transaction_type"], p["quantity"], p["product"], p["price"]) == ("B", "2", "MIS", "4646.00")


def test_flat_or_unknown_position_refused(kotak):
    kotak.positions = [{**CANBK, "flSellQty": "82", "sellAmt": "10000"}]
    with pytest.raises(ValueError, match="No open CNC position"):
        manual.close_position("CANBK-EQ", "CNC")
    with pytest.raises(ValueError, match="No open MIS position"):
        manual.close_position("CANBK-EQ", "MIS")
    assert kotak == []


def test_auto_trade_position_goes_through_its_app_trade(kotak):
    orders._save("CANBK|BUY|5m|x", symbol="CANBK", state="OPEN", qty=82, created_day="2026-10-19")
    with pytest.raises(ValueError, match="managed by Auto-trade"):
        manual.close_position("CANBK-EQ", "CNC")
    assert kotak == []


def test_api_needs_trade_access(kotak, monkeypatch):
    from algotrade import access, web
    monkeypatch.setattr(access, "can_trade", lambda: False)
    r = web.app.test_client().post("/api/positions/close", json={"tsym": "CANBK-EQ", "product": "CNC"})
    assert r.status_code == 403 and kotak == []
