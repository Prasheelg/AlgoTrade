"""portfolio: normalisers, totals, caching, error paths, /api/portfolio, and the read-only guarantee.
broker.call is faked; nothing here reaches Kotak."""
import inspect

import pytest

from algotrade import broker, config, kotak, portfolio, web

LIMITS = {"Category": "CLIENT_MTF", "Net": "25000.50", "MarginUsed": "1200", "CollateralValue": "0",
          "RmsPayInAmt": "5000", "stat": "Ok", "stCode": 200}
HOLD = {"displaySymbol": "TCS", "quantity": "10", "averagePrice": "3000", "closingPrice": "3100", "ltp": "3150"}
POS = {"trdSym": "INFY-EQ", "prod": "CNC", "flBuyQty": "5", "flSellQty": "2", "buyAmt": "7500", "sellAmt": "3100"}
ORD = {"nOrdNo": "2410150001", "trdSym": "INFY-EQ", "trnsTp": "B", "prcTp": "L", "prod": "CNC", "qty": "5",
       "prc": "1500", "trgPrc": "0", "ordSt": "complete", "fldQty": "5", "avgPrc": "1499.5", "rejRsn": "",
       "ordDtTm": "15-Oct-2026 10:01:02"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(portfolio, "_cache", {"at": None, "data": None})


class Calls(list):
    data: dict


@pytest.fixture
def kotak_fake(monkeypatch):
    for k in ("KOTAK_CONSUMER_KEY", "KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN", "KOTAK_TOTP_SECRET"):
        monkeypatch.setattr(config, k, "x")
    data = {"limits": LIMITS, "holdings": {"data": [HOLD]}, "positions": {"data": [POS]},
            "order_report": {"data": [ORD]}}
    calls = Calls()

    def call(method, **kw):
        calls.append(method)
        assert method in data, f"unexpected broker call {method}"
        return data[method]
    monkeypatch.setattr(broker, "call", call)
    monkeypatch.setitem(broker._session, "name", "TEST USER")
    # the fake consumer key would switch live quotes on: keep them offline
    monkeypatch.setattr(kotak, "quotes", lambda syms: {s: {"ltp": 1520.0} for s in syms if s == "INFY"})
    calls.data = data
    return calls


# ---- normalisers -------------------------------------------------------------------------------

@pytest.mark.parametrize("row", [
    {"displaySymbol": "TCS", "quantity": "10", "averagePrice": "3000", "ltp": "3150", "closingPrice": "3100"},
    {"symbol": "TCS-EQ", "qty": 10, "avgPrice": 3000, "lastPrice": 3150, "prevClose": 3100},
    {"trdSym": "tcs-eq", "sellableQuantity": "10", "avgPrc": "3000", "mktValue": "31500", "closingPrice": "3100"},
])
def test_holding_field_variants(row):
    h = portfolio._finish_holding(portfolio.norm_holding(row))
    assert (h["symbol"], h["qty"], h["avg"], h["ltp"], h["invested"], h["value"]) == ("TCS", 10, 3000, 3150, 30000, 31500)
    assert h["pnl"] == 1500 and h["pnl_pct"] == pytest.approx(5.0)
    assert h["day_pnl"] == 500 and h["day_pct"] == pytest.approx(50 / 3100 * 100)


def test_holding_cost_and_missing_prices():
    h = portfolio._finish_holding(portfolio.norm_holding({"symbol": "X", "quantity": "4", "holdingCost": "400"}))
    assert (h["invested"], h["ltp"], h["value"], h["pnl"], h["day_pnl"]) == (400, None, None, None, None)
    h = portfolio._finish_holding(portfolio.norm_holding({"symbol": "X", "quantity": "4", "averagePrice": "100",
                                                          "closingPrice": "110"}))
    assert h["value"] == 440 and h["pnl"] == 40 and h["day_pnl"] is None   # no live price: last close


def test_position_and_order():
    p = portfolio._finish_position(portfolio.norm_position({**POS, "ltp": "1520"}))
    assert (p["symbol"], p["net_qty"], p["buy_avg"], p["sell_avg"]) == ("INFY", 3, 1500, 1550)
    assert p["pnl"] == pytest.approx(3100 - 7500 + 3 * 1520)
    assert portfolio._finish_position(portfolio.norm_position(POS))["pnl"] is None   # no price yet
    o = portfolio.norm_order(ORD)
    assert (o["symbol"], o["side"], o["qty"], o["filled_qty"], o["avg_price"], o["status"]) == \
        ("INFY", "BUY", 5, 5, 1499.5, "complete")
    assert o["trigger"] is None                                        # "0" = no trigger
    o = portfolio.norm_order({**ORD, "trnsTp": "S", "prcTp": "SL-M", "prc": "0", "trgPrc": "1462.3",
                              "ordSt": "trigger pending", "fldQty": "0", "avgPrc": "0"})
    assert (o["side"], o["price"], o["trigger"], o["avg_price"], o["filled_qty"]) == ("SELL", None, 1462.3, None, 0)


@pytest.mark.parametrize("status, state", [("complete", "COMPLETED"), ("trigger pending", "PENDING"), ("open", "PENDING"),
                                           ("cancelled", "CANCELLED"), ("rejected", "REJECTED")])
def test_order_state(status, state):
    assert portfolio.norm_order({**ORD, "ordSt": status})["state"] == state


def test_funds_and_totals():
    f = portfolio.funds(LIMITS)
    assert (f["available"], f["margin_used"], f["payin"], f["category"]) == (25000.5, 1200, 5000, "CLIENT_MTF")
    hs = [portfolio._finish_holding(portfolio.norm_holding(HOLD)),
          portfolio._finish_holding(portfolio.norm_holding({"symbol": "A", "quantity": "2", "averagePrice": "50",
                                                            "ltp": "40"}))]
    t = portfolio.totals(hs, 100.0)
    assert (t["invested"], t["value"], t["pnl"], t["day_pnl"], t["cash"], t["count"]) == (30100, 31580, 1480, 500, 100.0, 2)
    assert portfolio.totals([], None) == {"invested": 0, "value": 0, "pnl": 0, "pnl_pct": None, "day_pnl": None,
                                          "cash": None, "count": 0}


def test_missing_ltp_filled_from_kotak_quotes(monkeypatch):
    monkeypatch.setattr(kotak, "enabled", lambda: True)
    monkeypatch.setattr(kotak, "quotes", lambda syms: {"X": {"ltp": 12.0}})
    items = [{"symbol": "X", "ltp": None}, {"symbol": "Y", "ltp": None}, {"symbol": "Z", "ltp": 5.0}]
    portfolio._fill_ltp(items)
    assert [i["ltp"] for i in items] == [12.0, None, 5.0]


# ---- report / caching / errors ------------------------------------------------------------------

def test_report_shape_and_cache(kotak_fake, monkeypatch):
    r = portfolio.report()
    assert r["error"] is None and r["credentials"] and not r["cached"] and r["account"] == "TEST USER"
    assert r["holdings"][0]["symbol"] == "TCS" and r["orders"][0]["order_id"] == "2410150001"
    assert r["raw"]["holdings"] == [HOLD] and r["totals"]["cash"] == 25000.5 and r["trades"] == []
    assert r["positions"][0]["ltp"] == 1520.0 and r["positions"][0]["pnl"] == pytest.approx(3100 - 7500 + 3 * 1520)
    n = len(kotak_fake)
    assert portfolio.report()["cached"] and len(kotak_fake) == n            # served from the cache
    assert not portfolio.report(refresh=True)["cached"] and len(kotak_fake) == 2 * n
    clock = [portfolio._cache["at"] + portfolio.CACHE_SEC + 1]
    monkeypatch.setattr(portfolio.time, "monotonic", lambda: clock[0])
    assert not portfolio.report()["cached"]                                 # expired


def test_empty_account(kotak_fake):
    kotak_fake.data.update(holdings={"data": []}, positions={"data": []}, order_report={"data": []})
    r = portfolio.report()
    assert r["holdings"] == r["positions"] == r["orders"] == [] and r["totals"]["count"] == 0


def test_missing_credentials():
    r = portfolio.report()
    assert not r["credentials"] and "missing in .env" in r["error"]
    assert r["holdings"] == [] and r["funds"] is None and r["trades"] == []


def test_broker_error_is_reported_not_raised(kotak_fake, monkeypatch):
    def boom(method, **kw):
        raise broker.BrokerError("TOTP login failed: Invalid TOTP")
    monkeypatch.setattr(broker, "call", boom)
    r = portfolio.report()
    assert "Invalid TOTP" in r["error"] and r["holdings"] == [] and portfolio._cache["data"] is None


def test_app_trades_included(kotak_fake, monkeypatch):
    from algotrade import orders
    monkeypatch.setattr(orders, "all_trades", lambda: [{"key": "K", "symbol": "TCS", "state": "OPEN"}])
    assert portfolio.report()["trades"] == [{"key": "K", "symbol": "TCS", "state": "OPEN"}]


def test_api_portfolio(kotak_fake):
    c = web.app.test_client()
    r = c.get("/api/portfolio")
    assert r.status_code == 200 and r.get_json()["holdings"][0]["symbol"] == "TCS"
    assert c.get("/api/portfolio").get_json()["cached"] is True
    assert c.get("/api/portfolio?refresh=1").get_json()["cached"] is False


def test_api_portfolio_without_credentials():
    r = web.app.test_client().get("/api/portfolio")
    assert r.status_code == 200 and r.get_json()["error"]


def test_portfolio_is_read_only():
    src = inspect.getsource(portfolio)
    for forbidden in ("place_order", "modify_order", "cancel_order", "set_trading", "enter(", "reconcile("):
        assert forbidden not in src


# ---- live prices (?live=1) ----------------------------------------------------------------------

@pytest.fixture
def live(kotak_fake, monkeypatch):
    monkeypatch.setattr(portfolio, "_live", {"at": None, "symbols": None, "quotes": {}})
    monkeypatch.setattr(kotak, "enabled", lambda: True)
    asked = []
    from datetime import datetime as dt

    def quotes(syms):
        asked.append(list(syms))
        return {s: {"ltp": px, "time": dt(2026, 10, 15, 10, 0, k)} for k, (s, px) in
                enumerate({"TCS": 3200.0, "INFY": 1530.0, "SBIN": 812.0}.items()) if s in syms}
    monkeypatch.setattr(kotak, "quotes", quotes)
    return asked


def test_live_overlays_quotes_and_recomputes(live):
    r = portfolio.report(live=True)
    h = r["holdings"][0]
    assert (h["ltp"], h["value"], h["pnl"], h["day_pnl"]) == (3200.0, 32000.0, 2000.0, 1000.0)
    assert r["positions"][0]["pnl"] == pytest.approx(3100 - 7500 + 3 * 1530)
    assert r["totals"]["value"] == 32000.0 and r["totals"]["day_pnl"] == 1000.0
    assert r["live"] == {"source": "Kotak Neo", "count": 2, "as_of": "2026-10-15T10:00:01"}
    # the cached account data keeps Kotak's own price
    assert portfolio._cache["data"]["holdings"][0]["ltp"] == 3150.0
    assert portfolio.report()["holdings"][0]["ltp"] == 3150.0 and portfolio.report()["live"] is None


def test_live_unrealized_pnl_for_open_app_trades(live, monkeypatch):
    trades = [{"key": "a", "symbol": "SBIN", "state": "OPEN", "qty": 10, "fill_price": 800.0},
              {"key": "b", "symbol": "TCS", "state": "ENTRY", "qty": 3, "fill_price": None},
              {"key": "c", "symbol": "INFY", "state": "CLOSED", "qty": 5, "fill_price": 1500.0}]
    monkeypatch.setattr(portfolio, "_app_trades", lambda: [dict(t) for t in trades])
    r = portfolio.report(live=True)
    by = {t["key"]: t for t in r["trades"]}
    assert (by["a"]["ltp"], by["a"]["unrealized"]) == (812.0, 120.0)
    assert "unrealized" not in by["b"] and "unrealized" not in by["c"]
    assert any("SBIN" in call for call in live)


def test_live_quotes_shared_for_a_few_seconds(live, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(portfolio.time, "monotonic", lambda: clock[0])
    portfolio.report(live=True)          # account fetch (fills missing prices) + one live call
    n = len(live)
    portfolio.report(live=True)
    assert len(live) == n                # within LIVE_SEC: shared, and the account data is cached
    clock[0] += portfolio.LIVE_SEC + 0.1
    portfolio.report(live=True)
    assert len(live) == n + 1


def test_live_without_quotes_keeps_account_prices(kotak_fake, monkeypatch):
    monkeypatch.setattr(portfolio, "_live", {"at": None, "symbols": None, "quotes": {}})
    monkeypatch.setattr(kotak, "enabled", lambda: False)
    r = portfolio.report(live=True)
    assert r["live"] is None and r["holdings"][0]["ltp"] == 3150.0


def test_live_quote_failure_is_not_an_error(kotak_fake, monkeypatch):
    monkeypatch.setattr(portfolio, "_live", {"at": None, "symbols": None, "quotes": {}})
    monkeypatch.setattr(kotak, "enabled", lambda: True)

    def boom(syms):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(kotak, "quotes", boom)
    r = portfolio.report(live=True)
    assert r["error"] is None and r["live"] is None


def test_api_portfolio_live_flag(live):
    j = web.app.test_client().get("/api/portfolio?live=1").get_json()
    assert j["live"]["count"] == 2 and j["holdings"][0]["ltp"] == 3200.0
