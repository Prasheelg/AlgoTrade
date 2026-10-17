"""strategy maths, live analysis (fno.analyze_strategy), and strategy placement (manual.place_strategy)."""
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from algotrade import broker, fno, manual, strategy as st, web
from test_options_manual import EXP1, fo, orders_fake  # noqa: F401  (fixtures)


def L(opt, k, side, price, qty=65, iv=0.14):
    return {"opt": opt, "strike": k, "side": side, "qty": qty, "price": price, "iv": iv}


# ---- pricing ----------------------------------------------------------------------------------

def test_put_call_parity_and_iv_roundtrip():
    s, k, t, iv = 23000, 23200, 10 / 365, 0.15
    c, p = st.bs_price("CE", s, k, t, iv), st.bs_price("PE", s, k, t, iv)
    assert c - p == pytest.approx(s - k * math.exp(-st.RISK_FREE * t), abs=1e-6)
    assert st.implied_vol("CE", c, s, k, t) == pytest.approx(iv, abs=1e-6)
    assert st.implied_vol("PE", p, s, k, t) == pytest.approx(iv, abs=1e-6)


def test_iv_outside_model_range_is_none():
    assert st.implied_vol("CE", 0, 23000, 23000, 0.02) is None
    assert st.implied_vol("CE", 1.0, 23000, 22000, 0.02) is None        # below intrinsic
    assert st.implied_vol("CE", 50.0, 23000, 23000, 0) is None


def test_expired_option_is_intrinsic():
    assert st.bs_price("CE", 23100, 23000, 0, 0.2) == 100 and st.bs_price("PE", 23100, 23000, 0, 0.2) == 0


def test_greeks_signs():
    c = st.greeks("CE", 23000, 23000, 7 / 365, 0.14)
    p = st.greeks("PE", 23000, 23000, 7 / 365, 0.14)
    assert 0.45 < c["delta"] < 0.6 and -0.55 < p["delta"] < -0.4
    assert c["gamma"] > 0 and c["theta"] < 0 and c["vega"] > 0
    assert c["delta"] - p["delta"] == pytest.approx(1.0, abs=1e-9)


# ---- payoff and analysis ------------------------------------------------------------------------

def test_long_call():
    a = st.analyze([L("CE", 23000, "BUY", 100)], 23000, 7)
    assert (a["max_profit"], a["max_loss"], a["breakevens"], a["net_premium"]) == (None, -6500.0, [23100.0], -6500.0)
    assert a["defined_risk"] and a["funds_estimate"] == 6500.0


def test_bull_call_spread():
    a = st.analyze([L("CE", 23000, "BUY", 100), L("CE", 23200, "SELL", 40)], 23000, 7)
    assert (a["max_profit"], a["max_loss"], a["breakevens"]) == (9100.0, -3900.0, [23060.0])
    assert a["defined_risk"] and a["funds_estimate"] == 3900.0


def test_iron_condor():
    legs = [L("PE", 22600, "BUY", 15), L("PE", 22800, "SELL", 35), L("CE", 23200, "SELL", 40), L("CE", 23400, "BUY", 15)]
    a = st.analyze(legs, 23000, 7)
    assert (a["max_profit"], a["max_loss"], a["breakevens"]) == (2925.0, -10075.0, [22755.0, 23245.0])
    assert a["net_premium"] == 2925.0 and a["defined_risk"] and a["funds_estimate"] == 10075.0
    assert a["greeks"]["theta"] > 0 and a["greeks"]["vega"] < 0


def test_short_straddle_is_unlimited_and_not_defined_risk():
    a = st.analyze([L("CE", 23000, "SELL", 100), L("PE", 23000, "SELL", 95)], 23000, 7)
    assert a["max_loss"] is None and a["max_profit"] == 12675.0 and not a["defined_risk"]
    assert a["breakevens"] == [22805.0, 23195.0] and a["funds_estimate"] is None


@pytest.mark.parametrize("legs,ok", [
    ([("CE", "BUY", 65)], True),
    ([("CE", "BUY", 65), ("CE", "SELL", 65)], True),                       # vertical spread
    ([("CE", "BUY", 65), ("CE", "SELL", 130), ("CE", "BUY", 65)], True),   # butterfly
    ([("CE", "BUY", 65), ("CE", "SELL", 130)], False),                     # ratio spread: one call naked
    ([("PE", "SELL", 65)], False),                                         # naked put
    ([("PE", "SELL", 65), ("CE", "BUY", 65)], False),                      # a call doesn't cover a put
])
def test_defined_risk_rule(legs, ok):
    assert st.defined_risk([{"opt": o, "side": s, "qty": q} for o, s, q in legs]) is ok


def test_today_curve_converges_to_expiry_payoff():
    legs = [L("CE", 23000, "BUY", 100)]
    assert st.payoff_at(legs, 23300, 1e-9) == pytest.approx(st.payoff_expiry(legs, 23300))
    assert st.payoff_at(legs, 23000, 7 / 365) > st.payoff_expiry(legs, 23000)   # time value left


def test_probability_of_profit_reasonable():
    deep_itm = st.probability_of_profit([L("CE", 20000, "BUY", 10)], 23000, 7 / 365, 0.14)
    far_otm = st.probability_of_profit([L("CE", 26000, "BUY", 10)], 23000, 7 / 365, 0.14)
    assert deep_itm > 0.99 and far_otm < 0.01


def test_analyze_needs_legs():
    with pytest.raises(ValueError):
        st.analyze([], 23000, 5)


def test_days_to_expiry_until_1530_ist():
    ist = ZoneInfo("Asia/Kolkata")
    exp = datetime(2026, 10, 19).date()
    assert fno.days_to_expiry(exp, datetime(2026, 10, 18, 15, 30, tzinfo=ist)) == pytest.approx(1.0)
    assert fno.days_to_expiry(exp, datetime(2026, 10, 19, 16, 0, tzinfo=ist)) == 0.0


# ---- live analysis (fake quotes from test_options_manual.FakeNeo) ------------------------------

def test_analyze_strategy_live(fo):
    a = fno.analyze_strategy([{"tsym": "NIFTY26O1923000CE", "side": "BUY", "lots": 1},
                              {"tsym": "NIFTY26O1923100CE", "side": "SELL", "lots": 1, "price": 9.0}])
    assert a["underlying"] == "NIFTY" and a["expiry"] == str(EXP1) and a["lot"] == 65
    assert a["legs"][0]["price"] == 10.5 and a["legs"][0]["mid"] == 10.5   # bid 10.4 / ask 10.6
    assert a["legs"][1]["price"] == 9.0 and a["legs"][0]["qty"] == 65
    assert a["net_premium"] == pytest.approx(-(10.5 - 9.0) * 65)
    assert len(a["x"]) == len(a["pnl_expiry"]) == len(a["pnl_today"])


def test_analyze_strategy_rejects_mixed_expiries_and_unknown(fo):
    with pytest.raises(ValueError, match="same underlying and expiry"):
        fno.analyze_strategy([{"tsym": "NIFTY26O1923000CE", "side": "BUY"}, {"tsym": "NIFTY26OCT23000CE", "side": "BUY"}])
    with pytest.raises(ValueError, match="Unknown option contract"):
        fno.analyze_strategy([{"tsym": "NOPE", "side": "BUY"}])


def test_api_analyze(fo):
    c = web.app.test_client()
    j = c.post("/api/options/analyze", json={"legs": [{"tsym": "NIFTY26O1923000CE", "side": "BUY", "lots": 1}]}).get_json()
    assert j["max_loss"] is not None and j["defined_risk"]
    assert c.post("/api/options/analyze", json={}).status_code == 400
    assert c.post("/api/options/analyze", json={"legs": [{"tsym": "NOPE"}]}).status_code == 400


# ---- placing a strategy ---------------------------------------------------------------------------

SPREAD = [{"tsym": "NIFTY26O1923100CE", "side": "SELL", "lots": 1, "price": 9},
          {"tsym": "NIFTY26O1923000CE", "side": "BUY", "lots": 1, "price": 11}]


def book_with(orders_fake, monkeypatch, status="complete"):
    """order_report shows every placed order with the given status."""
    real = broker.call

    def call(method, **kw):
        if method == "order_report":
            return {"data": [{"nOrdNo": f"O{i + 1}", "ordSt": status} for i in range(len(orders_fake))]}
        return real(method, **kw)
    monkeypatch.setattr(broker, "call", call)
    monkeypatch.setattr(manual.time, "sleep", lambda s: None)


def test_buys_first_then_sells_after_fill(orders_fake, monkeypatch):
    book_with(orders_fake, monkeypatch)
    out = manual.place_strategy(SPREAD)
    assert [p["transaction_type"] for p in orders_fake] == ["B", "S"]
    assert out["sells_placed"] and "All 2 legs placed" in out["message"]


def test_sells_not_placed_when_buys_do_not_fill(orders_fake, monkeypatch):
    book_with(orders_fake, monkeypatch, status="open")
    out = manual.place_strategy(SPREAD, wait=0)
    assert [p["transaction_type"] for p in orders_fake] == ["B"]
    assert not out["sells_placed"] and "NOT placed" in out["message"]


def test_rejected_buy_stops_the_sells(orders_fake, monkeypatch):
    book_with(orders_fake, monkeypatch, status="rejected")
    out = manual.place_strategy(SPREAD, wait=60)
    assert [p["transaction_type"] for p in orders_fake] == ["B"] and not out["sells_placed"]


def test_uncovered_strategy_refused_before_anything_is_sent(orders_fake):
    naked = [{"tsym": "NIFTY26O1923000CE", "side": "SELL", "lots": 1, "price": 10},
             {"tsym": "NIFTY26O1923000PE", "side": "SELL", "lots": 1, "price": 10}]
    with pytest.raises(ValueError, match="without buying cover"):
        manual.place_strategy(naked)
    ratio = [{"tsym": "NIFTY26O1923000CE", "side": "BUY", "lots": 1, "price": 10},
             {"tsym": "NIFTY26O1923100CE", "side": "SELL", "lots": 2, "price": 5}]
    with pytest.raises(ValueError, match="without buying cover"):
        manual.place_strategy(ratio)
    assert orders_fake == []


def test_sold_legs_need_market_hours_buy_only_can_be_amo(orders_fake, monkeypatch):
    monkeypatch.setattr(manual.markets.get("IN").__class__, "is_open", lambda self, now=None: False)
    with pytest.raises(ValueError, match="while the market is open"):
        manual.place_strategy(SPREAD)
    out = manual.place_strategy([{"tsym": "NIFTY26O1923000CE", "side": "BUY", "lots": 1, "price": 11},
                                 {"tsym": "NIFTY26O1923000PE", "side": "BUY", "lots": 1, "price": 11}])
    assert [p["amo"] for p in orders_fake] == ["YES", "YES"] and "2 buy orders placed" in out["message"]


def test_strategy_validation(orders_fake):
    with pytest.raises(ValueError, match="no legs"):
        manual.place_strategy([])
    with pytest.raises(ValueError, match="same underlying and expiry"):
        manual.place_strategy([{"tsym": "NIFTY26O1923000CE", "side": "BUY", "lots": 1, "price": 1},
                               {"tsym": "NIFTY26OCT23000CE", "side": "BUY", "lots": 1, "price": 1}])
    with pytest.raises(ValueError, match="Lots must be"):
        manual.place_strategy([{"tsym": "NIFTY26O1923000CE", "side": "BUY", "lots": 99, "price": 1}])


def test_api_strategy_local_only(orders_fake, monkeypatch):
    book_with(orders_fake, monkeypatch)
    c = web.app.test_client()
    r = c.post("/api/options/strategy", json={"legs": SPREAD}, environ_base={"REMOTE_ADDR": "10.0.0.7"})
    assert r.status_code == 401 and orders_fake == []
    assert c.post("/api/options/strategy", json={}).status_code == 400
    r = c.post("/api/options/strategy", json={"legs": SPREAD, "order_type": "LIMIT", "product": "NRML"})
    assert r.status_code == 200 and r.get_json()["sells_placed"]
