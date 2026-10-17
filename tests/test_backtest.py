"""P6: backtest.vote_table / backtest_symbol / summarize / run."""
import pandas as pd
import pytest

from algotrade import backtest, config, scanner
from algotrade.strategies import DEFAULT_STRATEGIES, STRATEGIES
from conftest import FakeFetch, Recorder, check_golden, eval_frame

NAMES = ["RSI Pullback", "Support-Resistance"]   # 2 pullback families -> limit entry 99 / SL 96 / T 103.5, 108
QUIET = dict(Open=100.0, High=101.0, Low=99.5, Close=100.0)


def hand(n=215, bars=None, vote_at=(200,), side="BUY", **row_kw):
    """eval_frame with explicit OHLC rows after bar 200 and BUY votes on chosen bars."""
    df = eval_frame(rows=n, **row_kw)
    for k, v in QUIET.items():
        df.loc[df.index[201]:, k] = v
    for i, row in (bars or {}).items():
        for k, v in row.items():
            df.iloc[i, df.columns.get_loc(k)] = v
    votes = {n_: [side if i in vote_at else None for i in range(n)] for n_ in NAMES}
    return df, votes


def bt(df, votes, **kw):
    return backtest.backtest_symbol("X", df, strategies=NAMES, votes=votes, **kw)


def test_no_fill_within_entry_valid_bars_means_no_trade():
    df, v = hand(bars={204: dict(Low=98.0)})   # reachable only on the 4th bar
    assert bt(df, v) == []


def test_fill_bar_stop_exits_at_stop():
    df, v = hand(bars={201: dict(Low=95.0)})
    t = bt(df, v)
    assert t == [{"symbol": "X", "side": "BUY", "entry_type": "limit", "entry_time": df.index[201],
                  "exit_time": df.index[201], "entry": 99.0, "exit": 96.0, "R": -1.0, "score": 2}]


def test_gap_through_fill_and_stop_exits_at_fill_with_zero_r():
    df, v = hand(bars={201: dict(Open=95.0, High=95.5, Low=94.0, Close=95.0)})
    t = bt(df, v)[0]
    assert (t["entry"], t["exit"], t["R"]) == (95.0, 95.0, 0.0)


def test_target_hit_uses_target_idx():
    df, v = hand(bars={201: dict(Low=98.5), 202: dict(High=104.0)})
    t = bt(df, v)[0]
    assert (t["exit"], t["R"], t["exit_time"]) == (103.5, 1.5, df.index[202])
    df, v = hand(bars={201: dict(Low=98.5), 202: dict(High=104.0), 205: dict(High=108.0)})
    t = bt(df, v, target_idx=1)[0]
    assert (t["exit"], t["R"], t["exit_time"]) == (108.0, 3.0, df.index[205])


def test_sl_before_target_same_bar():
    df, v = hand(bars={201: dict(Low=98.5), 202: dict(High=110.0, Low=95.0)})
    assert bt(df, v)[0]["exit"] == 96.0


def test_no_t1_breakeven_stage_in_backtest():
    """Quirk 6: single target, no breakeven stage (tracker would close at BREAKEVEN 99 here)."""
    df, v = hand(bars={201: dict(Low=98.5), 202: dict(High=104.0), 203: dict(Low=95.0)})
    t = bt(df, v, target_idx=1)[0]
    assert t["exit"] == 96.0 and t["R"] == -1.0


def test_max_hold_exit_at_close():
    df, v = hand(bars={201: dict(Low=98.5), 204: dict(Close=100.5)})
    t = bt(df, v, max_hold=3)[0]
    assert (t["exit_time"], t["exit"], t["R"]) == (df.index[204], 100.5, 0.5)


def test_one_position_at_a_time():
    df, v = hand(vote_at=(200, 202, 205), bars={201: dict(Low=98.5), 206: dict(Low=98.5)})
    t = bt(df, v, max_hold=3)
    assert [x["entry_time"] for x in t] == [df.index[201], df.index[206]]  # 202's vote ignored


def test_trade_runs_to_end_of_data():
    df, v = hand(n=204, bars={201: dict(Low=98.5), 203: dict(Close=100.25)})
    t = bt(df, v)[0]
    assert (t["exit_time"], t["exit"]) == (df.index[203], 100.25)


def test_last_bar_signal_is_never_evaluated():
    df, v = hand(n=202, vote_at=(201,))
    assert bt(df, v) == []


def test_sell_trades_when_shorts_allowed(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_SHORTS", True)
    df, v = hand(side="SELL", bars={201: dict(High=101.5), 202: dict(Low=96.0)})
    t = bt(df, v)[0]
    assert (t["side"], t["entry"], t["exit"], t["R"]) == ("SELL", 101.0, 96.5, 1.5)


def test_backtest_ignores_timeframe_uses_daily_turnover():
    """Quirk 6: evaluate is called without timeframe, so intraday turnover thresholds are not applied."""
    df, v = hand(bars={201: dict(Low=98.5)})
    df["TURNOVER20"] = 5e7 / 25   # enough for a 15m bar, too little for a daily bar
    assert scanner.evaluate("X", df, 200, strategies=NAMES, timeframe="15m",
                            votes={n: v[n][200] for n in NAMES}) is not None
    assert bt(df, v) == []


def test_vote_table_shape():
    df, _ = hand(n=203)
    calls = []

    def fake_votes(d, i, names):
        calls.append(i)
        return {n: f"v{i}" for n in names}

    import algotrade.backtest as b
    orig = b.strategy_votes
    b.strategy_votes = fake_votes
    try:
        vt = backtest.vote_table(df, ["a", "b"])
    finally:
        b.strategy_votes = orig
    assert calls == [200, 201, 202]
    assert vt == {"a": [None] * 200 + ["v200", "v201", "v202"], "b": [None] * 200 + ["v200", "v201", "v202"]}


# ---- golden + cache equivalence ---------------------------------------------------------------

def _trades_json(trades):
    return [{**t, "entry_time": str(t["entry_time"]), "exit_time": str(t["exit_time"]),
             "entry": round(float(t["entry"]), 4), "exit": round(float(t["exit"]), 4)} for t in trades]


def test_golden_backtest_trades(golden_series, monkeypatch):
    out = {}
    for name, g in golden_series.items():
        for variant, kw in {"default": {}, "all_min1": dict(min_score=1, strategies=list(STRATEGIES)),
                            "t2_filters": dict(target_idx=1, filters=["trend"], max_hold=10)}.items():
            out[f"{name}/{variant}"] = _trades_json(backtest.backtest_symbol(
                "SYM", g["df"], market=g["market"], votes=g["votes"], **kw))
    assert sum(len(v) for v in out.values()) > 20
    check_golden("backtest_trades", out)


def test_cached_votes_equal_uncached(golden_series):
    g = golden_series["up_IN"]
    kw = dict(market=g["market"], min_score=1, strategies=list(STRATEGIES))
    cached = backtest.backtest_symbol("SYM", g["df"], votes=g["votes"], **kw)
    uncached = backtest.backtest_symbol("SYM", g["raw"], index_close=g["index"], **kw)
    assert cached == uncached and len(cached) > 5


# ---- summarize ---------------------------------------------------------------------------------

def test_summarize_empty():
    assert backtest.summarize(pd.DataFrame()) == "No trades generated."


def test_summarize_layout():
    t = pd.DataFrame({"side": ["BUY", "BUY", "SELL", "BUY"], "R": [1.5, -1.0, 3.0, 0.0]})
    assert backtest.summarize(t) == (
        "Trades: 4\nWin rate: 50.0%\nAvg R/trade: 0.88\nTotal R: 3.5\nProfit factor: 4.50\n\n"
        "By side:\n      count  mean\nside             \nBUY       3  0.17\nSELL      1  3.00")


@pytest.mark.parametrize("rs", [[1.0, 2.0], [1.0, 0.0]])
def test_summarize_profit_factor_inf(rs):
    t = pd.DataFrame({"side": ["BUY"] * len(rs), "R": rs})
    assert "Profit factor: inf" in backtest.summarize(t)


# ---- run --------------------------------------------------------------------------------------

def test_run_with_fake_fetchers(monkeypatch, golden_series):
    g = golden_series["range_US"]
    fetch = FakeFetch({"AAA": g["raw"]})
    idx = Recorder(g["index"])
    monkeypatch.setattr(backtest, "fetch_ohlcv", fetch)
    monkeypatch.setattr(backtest, "fetch_index", idx)
    calls = []
    real = backtest.backtest_symbol

    def spy(sym, df, **kw):
        calls.append(kw)
        return real(sym, df, **kw)

    monkeypatch.setattr(backtest, "backtest_symbol", spy)
    out = backtest.run(["AAA", "MISSING"], "1d", period="1y", market="US", max_hold=5)
    assert fetch.calls == [dict(symbols=["AAA", "MISSING"], timeframe="1d", period="1y", market="US")]
    assert idx.calls == [(("1d", "US"), {"period": "1y"})]
    assert calls[0]["market"] == "US" and calls[0]["max_hold"] == 5 and calls[0]["index_close"] is g["index"]
    assert list(out.columns) == ["symbol", "side", "entry_type", "entry_time", "exit_time", "entry", "exit", "R",
                                 "score"]
    assert set(out["symbol"]) == {"AAA"}


def test_run_no_data_gives_empty_frame(monkeypatch):
    monkeypatch.setattr(backtest, "fetch_ohlcv", FakeFetch({}))
    monkeypatch.setattr(backtest, "fetch_index", Recorder(None))
    out = backtest.run(["A"])
    assert isinstance(out, pd.DataFrame) and out.empty


def test_fetch_index_is_scanners_function():
    assert backtest.fetch_index is scanner.fetch_index
    assert backtest.fill_price.__module__ == "algotrade.tracker"
    assert DEFAULT_STRATEGIES  # imported for the default name list
