"""The NR7 F&O stock backtest (nr7_stocks.py): results-day tagging, the statistics and a run on fake data."""
import json

import pandas as pd
import pytest

from algotrade import nr7_stocks


@pytest.mark.parametrize("nr7_day,results,window,expected", [
    ("2024-05-02", ["2024-05-02"], 5, True),          # results on the NR7 day itself
    ("2024-05-02", ["2024-05-08"], 5, True),          # Thu + 4 trading days = next Wed
    ("2024-05-02", ["2024-05-09"], 5, False),
    ("2024-05-02", ["2024-05-01"], 5, False),         # already out
    ("2024-05-02", ["2024-05-06"], 3, True),
    ("2024-05-02", ["2024-05-07"], 3, False),
    ("2014-05-02", ["2014-05-02"], 5, False),         # before RESULTS_FROM
])
def test_results_soon(nr7_day, results, window, expected):
    assert nr7_stocks.results_soon(results, window)(pd.Timestamp(nr7_day)) is expected


def test_stats():
    s = nr7_stocks._stats(pd.Series([3.0, -1.0, -1.0, 3.0, -1.0]))
    assert (s["trades"], s["win_pct"], s["avg_r"], s["total_r"]) == (5, 40.0, 0.6, 3.0)
    assert (s["avg_win_r"], s["avg_loss_r"], s["breakeven_win_pct"], s["worst_r"]) == (3.0, 1.0, 25.0, -1.0)
    assert 0 <= s["p_no_edge"] < 0.5
    assert nr7_stocks._stats(pd.Series([], dtype=float)) == {"trades": 0}


@pytest.fixture
def fake_stocks(monkeypatch):
    """Two stocks with the same bars: an NR7 on 2016-01-12, long at 101 the next day, target 107 the day after.
    AAA has results due on 2016-01-13, BBB none."""
    rows = [(100, 105, 95, 100)] * 6 + [(100, 101, 99, 100), (100, 102, 99.5, 101.5), (101.5, 108, 101, 107)]
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=pd.bdate_range("2016-01-04", periods=len(rows)))
    monkeypatch.setattr(nr7_stocks, "fno_stocks", lambda: ["AAA", "BBB"])
    import algotrade.data
    monkeypatch.setattr(algotrade.data, "fetch_ohlcv", lambda syms, tf, period=None, market="IN": {s: df for s in syms})
    monkeypatch.setattr(nr7_stocks, "_results_for", lambda sym: ["2016-01-13"] if sym == "AAA" else [])
    for old in nr7_stocks.config.DATA_DIR.glob("nr7_*"):
        old.unlink()
    return df


def test_run_on_fake_stocks(fake_stocks):
    out = nr7_stocks.run()
    assert out["stocks"] == 2 and out["all"]["trades"] == 2 and out["exits"] == {"TARGET": 2}
    assert out["costs_doubled"]["avg_r"] < out["all"]["avg_r"]
    assert nr7_stocks.OUT_FILE.exists()


def test_results_variant_on_fake_stocks(fake_stocks, monkeypatch):
    monkeypatch.setattr(nr7_stocks, "COIN_FLIPS", 4)
    out = nr7_stocks.run_results()
    assert out["stocks"] == 1 and out["variant"]["trades"] == 1             # only AAA had results due
    assert out["no_results_control"]["trades"] == 1                           # BBB
    assert out["variant"]["avg_r"] > 2.9 and out["exits"] == {"TARGET": 1}
    assert json.loads(nr7_stocks.RESULTS_FILE.read_text()) == {"AAA": ["2016-01-13"], "BBB": []}
    assert 0 <= out["beats_coin_flip_pct"] <= 100 and nr7_stocks.VARIANT_FILE.exists()


def test_forced_side_only_takes_that_level():
    from algotrade import nr7
    t = nr7.new_setup("X", pd.Timestamp("2016-01-12").date(), 101, 99)
    bars = pd.DataFrame([(100, 102, 99.5, 101.5)], columns=["Open", "High", "Low", "Close"],
                        index=pd.bdate_range("2016-01-13", periods=1))
    assert nr7.advance(t, bars, daily=True, force_side="SHORT")["status"] == "WAIT"
    assert nr7.advance(t, bars, daily=True, force_side="LONG")["side"] == "LONG"
