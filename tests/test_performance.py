"""performance.closed_trades / report and /api/performance."""
from datetime import date, timedelta

import pandas as pd
import pytest

from algotrade import config, costs, performance, state, web
from conftest import make_signal


def close(symbol, status, exit_price, closed_at="2026-10-01", fill=99.0, market="IN", **kw):
    """Record a signal (limit BUY 99, SL 96, T 103.5 / 108, qty 333) and close it."""
    sig = make_signal(symbol=symbol, market=market, **kw)
    state.record([sig])
    state.update_signal(sig.key, status=status, exit_price=exit_price, closed_at=closed_at, fill_price=fill,
                        filled_at="2026-09-29T00:00:00")
    return sig


@pytest.fixture
def no_costs(monkeypatch):
    monkeypatch.setattr(costs, "model_for", lambda market, tf="1d": costs.CostModel())


def test_closed_trades_skip_open_and_cancelled(no_costs):
    close("A", "T2_HIT", 108.0)
    close("B", "SL_HIT", 96.0, closed_at="2026-09-30")
    close("C", "CANCELLED", None)
    state.record([make_signal(symbol="D")])                 # still pending
    close("E", "T2_HIT", 108.0, market="US")                # other market
    t = performance.closed_trades("IN")
    assert [(x["symbol"], x["r"], x["net_r"]) for x in t] == [("B", -1.0, -1.0), ("A", 3.0, 3.0)]  # oldest exit first
    assert t[0]["hold_days"] == 1 and t[0]["strategies"] == ["EMA 9/21 Cross", "RSI Pullback"]


def test_r_uses_fill_and_planned_risk(no_costs):
    close("A", "EXPIRED", 101.0, fill=98.0)                  # risk 3 (99 - 96), filled at 98
    assert performance.closed_trades("IN")[0]["r"] == 1.0


def test_net_r_matches_cost_model():
    close("A", "SL_HIT", 96.0)
    t = performance.closed_trades("IN")[0]
    m = costs.model_for("IN", "1d")
    assert t["net_r"] == round(m.net_r("BUY", 99.0, 96.0, 3.0, 333, False, True), 2) and t["net_r"] < t["r"]


def test_target_exit_has_no_exit_slippage(monkeypatch):
    monkeypatch.setattr(costs, "model_for", lambda market, tf="1d": costs.CostModel(slippage_bps=100))
    close("A", "T2_HIT", 108.0)
    close("B", "SL_HIT", 96.0)
    by = {t["symbol"]: t for t in performance.closed_trades("IN")}
    assert by["A"]["net_r"] == 3.0
    assert by["B"]["net_r"] == round((96 * 0.99 - 99) / 3, 2)


def test_qty_capped_by_capital():
    mkt = performance.markets.get("IN")
    assert performance._qty(mkt, 99.0, 3.0) == 333
    assert performance._qty(mkt, 5000.0, 0.5) == 20           # 2000 by risk, 20 by capital
    assert performance._qty(mkt, 99.0, 0.0) == 0


def test_report_totals_drawdown_and_breakdowns(no_costs):
    close("A", "T2_HIT", 108.0, closed_at="2026-09-01")      # +3
    close("B", "SL_HIT", 96.0, closed_at="2026-09-02")       # -1
    close("C", "SL_HIT", 96.0, closed_at="2026-09-03", reasons=["RSI Pullback"])  # -1
    close("D", "EXPIRED", 100.5, closed_at="2026-09-04", entry_type="stop")       # +0.5
    r = performance.report("IN")
    assert (r["trades"], r["open"], r["currency"], r["r_value"]) == (4, 0, "₹", 1000.0)
    assert r["net"] == {"avg_r": 0.375, "total_r": 1.5, "win_rate": 0.5, "profit_factor": 1.75, "max_drawdown_r": 2.0}
    assert [c["net"] for c in r["equity"]] == [3.0, 2.0, 1.0, 1.5]
    assert r["equity"][0]["exit"] == "Target 2"
    strat = {g["group"]: g for g in r["breakdowns"][0]["rows"]}
    assert strat["RSI Pullback"]["trades"] == 4 and strat["EMA 9/21 Cross"]["trades"] == 3
    exits = {g["group"]: g["trades"] for g in r["breakdowns"][2]["rows"]}
    assert exits == {"Stop loss": 2, "Target 2": 1, "Time exit": 1}
    assert [g["group"] for g in r["breakdowns"][3]["rows"]] == ["Limit", "Stop"]


def test_report_days_filter_and_empty(no_costs):
    close("A", "T2_HIT", 108.0, closed_at=str(date.today() - timedelta(days=40)))
    close("B", "SL_HIT", 96.0, closed_at=str(date.today()))
    assert performance.report("IN", days=30)["trades"] == 1
    empty = performance.report("US")
    assert empty["trades"] == 0 and empty["net"]["avg_r"] is None and empty["equity"] == []
    assert empty["net"]["max_drawdown_r"] == 0.0 and empty["avg_hold_days"] is None


def test_backtest_reference(tmp_path):
    assert performance.backtest_reference("IN") is None                       # no file
    pd.DataFrame({"R": [1.0, -1.0]}).to_csv(tmp_path / "backtest_trades.csv", index=False)
    assert performance.backtest_reference("IN") is None                       # run without costs
    pd.DataFrame({"R": [1.0, -1.0], "net_R": [0.9, -1.1]}).to_csv(tmp_path / "backtest_trades_US.csv", index=False)
    assert performance.backtest_reference("US") == {"trades": 2, "avg_gross_r": 0.0, "avg_r": -0.1}
    assert config.DATA_DIR == tmp_path


def test_api_performance(no_costs):
    close("A", "T2_HIT", 108.0, closed_at=str(date.today()))
    c = web.app.test_client()
    r = c.get("/api/performance?market=IN").get_json()
    assert r["trades"] == 1 and r["days"] is None
    assert c.get("/api/performance?market=IN&days=7").get_json()["days"] == 7
    assert c.get("/api/performance?market=IN&days=0").get_json()["days"] is None
    assert c.get("/api/performance?market=US").get_json()["trades"] == 0
    assert c.get("/api/performance?market=XX").status_code == 400


def test_report_all_markets_together(no_costs):
    close("A", "T2_HIT", 108.0, closed_at="2026-09-01")                 # IN +3R
    close("G", "SL_HIT", 96.0, closed_at="2026-09-02", market="MCX")    # MCX -1R
    close("U", "T2_HIT", 108.0, closed_at="2026-09-03", market="US")    # not asked for
    r = performance.report("ALL", codes=["IN", "MCX"])
    assert r["market"] == "ALL" and r["trades"] == 2 and r["backtest"] is None
    assert [(c["symbol"], c["market"], c["net"]) for c in r["equity"]] == [("A", "IN", 3.0), ("G", "MCX", 2.0)]
    rv = r["r_values"]
    assert set(rv) == {"IN", "MCX"} and r["r_value"] == rv["IN"]
    assert r["net_money"] == pytest.approx(3 * rv["IN"] - rv["MCX"])   # each trade at its own exchange's 1R
    assert r["max_drawdown_money"] == pytest.approx(rv["MCX"])
    ex = {g["group"]: g["trades"] for g in next(b for b in r["breakdowns"] if b["name"] == "Exchange")["rows"]}
    assert ex == {"NSE": 1, "MCX": 1}
    assert "Exchange" not in [b["name"] for b in performance.report("IN")["breakdowns"]]   # one market: no such tab


def test_api_performance_all(no_costs, monkeypatch):
    monkeypatch.setattr(config, "HIDDEN_MARKETS", {"US"})
    close("A", "T2_HIT", 108.0, closed_at=str(date.today()))
    close("G", "SL_HIT", 96.0, closed_at=str(date.today()), market="MCX")
    close("U", "T2_HIT", 108.0, closed_at=str(date.today()), market="US")   # hidden market: left out
    r = web.app.test_client().get("/api/performance?market=all").get_json()
    assert r["market"] == "ALL" and r["trades"] == 2 and set(r["r_values"]) == {"IN", "MCX"}


# ---- health: the Signals page's verdict on the live results after costs ----

def _trades(*rs):
    return [{"net_r": r} for r in rs]


@pytest.mark.parametrize("rs,bt,level", [
    ([1.0] * 29, None, "early"),                          # under MIN_TRADES
    ([-0.5] * 30, None, "bad"),                           # losing after costs
    ([1.0] * 20 + [-0.5] * 20, None, "warn"),             # +ve overall, last 20 losing
    ([0.3] * 40, {"avg_r": 0.5}, "warn"),                 # lags the backtest by 0.2R
    ([0.3] * 40, {"avg_r": 0.35}, "ok"),                  # within 0.1R of it
    ([0.3] * 40, None, "ok"),
])
def test_health_levels(monkeypatch, rs, bt, level):
    monkeypatch.setattr(performance, "backtest_reference", lambda m: bt)
    h = performance.health("IN", _trades(*rs))
    assert h["level"] == level and h["trades"] == len(rs) and h["min_trades"] == performance.MIN_TRADES
    assert h["recent_n"] == min(len(rs), performance.RECENT_TRADES)
    assert h["backtest_r"] == (bt["avg_r"] if bt else None)


def test_health_without_trades(monkeypatch):
    monkeypatch.setattr(performance, "backtest_reference", lambda m: None)
    assert performance.health("IN", []) == {"level": "early", "trades": 0, "min_trades": performance.MIN_TRADES,
                                            "avg_r": None, "recent_r": None, "recent_n": 0, "backtest_r": None}
