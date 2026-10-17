"""costs.CostModel / model_for, and backtest_symbol / summarize with a cost model."""
import pandas as pd
import pytest

from algotrade import backtest, config, costs
from algotrade.costs import CostModel
from test_backtest import bt, hand


def test_zero_cost_model_gives_gross_r():
    assert CostModel().net_r("BUY", 99.0, 103.5, 3.0, 333, True, True) == pytest.approx(1.5)


def test_slippage_is_adverse_on_both_sides():
    m = CostModel(slippage_bps=100)
    assert m.net_r("BUY", 100.0, 90.0, 10.0, 10, True, True) == pytest.approx((89.1 - 101.0) / 10)
    assert m.net_r("SELL", 100.0, 110.0, 10.0, 10, True, True) == pytest.approx(-(111.1 - 99.0) / 10)
    assert m.net_r("BUY", 100.0, 90.0, 10.0, 10, False, False) == pytest.approx(-1.0)


def test_fees_round_trip():
    m = CostModel(brokerage=20, buy_pct=0.001, sell_pct=0.001, side_pct=0.0001, gst=0.18, sell_fixed=20)
    assert m.fees(10000, 11000) == pytest.approx((40 + 2.1) * 1.18 + 10 + 11 + 20)
    # 100 shares bought at 100, sold at 110, risk 5/share
    assert m.net_r("BUY", 100.0, 110.0, 5.0, 100, False, False) == pytest.approx((1000 - m.fees(10000, 11000)) / 500)


def test_zero_qty_and_zero_risk():
    m = CostModel(brokerage=20)
    assert m.net_r("BUY", 100.0, 110.0, 5.0, 0, False, False) == pytest.approx((10 - 40) / 5)  # sized as 1 share
    assert m.net_r("BUY", 100.0, 110.0, 0.0, 10, False, False) == 0.0


def test_model_for_markets_and_timeframes(monkeypatch):
    monkeypatch.setattr(config, "SLIPPAGE_BPS", 7.0)
    monkeypatch.setattr(config, "DP_CHARGE", 15.0)
    delivery, intraday, us = costs.model_for("IN", "1d"), costs.model_for("IN", "15m"), costs.model_for("US", "1d")
    assert (delivery.slippage_bps, delivery.sell_fixed, delivery.buy_pct, delivery.sell_pct) == \
        (7.0, 15.0, pytest.approx(0.00115), 0.001)
    assert intraday.sell_fixed == 0.0 and intraday.sell_pct == 0.00025 and intraday.gst == 0.18
    assert us.gst == 0.0 and us.brokerage == config.US_BROKERAGE and us.slippage_bps == config.US_SLIPPAGE_BPS


# ---- backtest with costs ------------------------------------------------------------------------

def test_target_exit_after_limit_fill_has_no_slippage():
    df, v = hand(bars={201: dict(Low=98.5), 202: dict(High=104.0)})
    t = bt(df, v, costs=CostModel(slippage_bps=100))[0]
    assert (t["R"], t["net_R"], t["cost_R"]) == (1.5, 1.5, 0.0)


def test_stop_exit_pays_slippage():
    df, v = hand(bars={201: dict(Low=98.5), 202: dict(Low=95.0)})
    t = bt(df, v, costs=CostModel(slippage_bps=100))[0]
    assert (t["R"], t["net_R"], t["cost_R"]) == (-1.0, round((96 * 0.99 - 99) / 3, 2), 0.32)


def test_gap_fill_pays_entry_slippage():
    df, v = hand(bars={201: dict(Open=98.0, High=98.5, Low=97.5, Close=98.0), 202: dict(High=104.0)})
    t = bt(df, v, costs=CostModel(slippage_bps=100))[0]
    assert t["entry"] == 98.0 and t["net_R"] == round((103.5 - 98 * 1.01) / 3, 2)


def test_fees_scale_with_signal_qty():
    df, v = hand(bars={201: dict(Low=98.5), 202: dict(High=104.0)})
    t = bt(df, v, costs=CostModel(brokerage=30))[0]   # qty = 1% of 100000 / risk 3 = 333 shares
    assert t["net_R"] == round(1.5 - 60 / (3 * 333), 2)


def test_no_cost_columns_without_model():
    df, v = hand(bars={201: dict(Low=98.5), 202: dict(High=104.0)})
    assert "net_R" not in bt(df, v)[0]


def test_summarize_with_costs():
    t = pd.DataFrame({"side": ["BUY", "BUY"], "R": [1.5, -1.0], "net_R": [1.4, -1.1], "cost_R": [0.1, 0.1]})
    out = backtest.summarize(t)
    assert out.startswith("Trades: 2\nWin rate: 50.0%\nAvg R/trade: 0.25\n")
    assert ("After costs (avg cost 0.10R per trade):\nWin rate: 50.0%\nAvg R/trade: 0.15\nTotal R: 0.3\n"
            "Profit factor: 1.27") in out
    assert "By side, after costs:" in out
