"""The NR7 F&O stock backtest (nr7_stocks.py): results-day tagging, the statistics and a run on fake data."""
import json

import pandas as pd
import pytest

from algotrade import nr7_stocks


def test_results_inside_the_trade_window():
    t = {"nr7_date": "2024-05-02", "exit_at": "2024-05-08T00:00:00"}
    assert nr7_stocks._results_inside(t, ["2024-04-30", "2024-05-02"])     # announced after the NR7 close
    assert nr7_stocks._results_inside(t, ["2024-05-08"])
    assert not nr7_stocks._results_inside(t, ["2024-05-01", "2024-05-09"])


def test_stats():
    s = nr7_stocks._stats(pd.Series([3.0, -1.0, -1.0, 3.0, -1.0]))
    assert (s["trades"], s["win_pct"], s["avg_r"], s["total_r"]) == (5, 40.0, 0.6, 3.0)
    assert (s["avg_win_r"], s["avg_loss_r"], s["breakeven_win_pct"], s["worst_r"]) == (3.0, 1.0, 25.0, -1.0)
    assert 0 <= s["p_no_edge"] < 0.5
    assert nr7_stocks._stats(pd.Series([], dtype=float)) == {"trades": 0}


def test_run_on_fake_stocks(monkeypatch):
    rows = [(100, 105, 95, 100)] * 6 + [(100, 101, 99, 100), (100, 102, 99.5, 101.5), (101.5, 108, 101, 107)]
    df = pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=pd.bdate_range("2016-01-04", periods=len(rows)))
    monkeypatch.setattr(nr7_stocks, "fno_stocks", lambda: ["AAA", "BBB"])
    import algotrade.data
    monkeypatch.setattr(algotrade.data, "fetch_ohlcv", lambda syms, tf, period=None, market="IN": {s: df for s in syms})
    monkeypatch.setattr(nr7_stocks, "_results_for", lambda sym: ["2016-01-13"] if sym == "AAA" else [])
    out = nr7_stocks.run()
    assert out["stocks"] == 2 and out["all"]["trades"] == 2 and out["exits"] == {"TARGET": 2}
    assert out["since_2015_with_results"]["trades"] == 1 and out["since_2015_no_results"]["trades"] == 1
    assert out["costs_doubled"]["avg_r"] < out["all"]["avg_r"]
    assert json.loads(nr7_stocks.RESULTS_FILE.read_text()) == {"AAA": ["2016-01-13"], "BBB": []}
    assert nr7_stocks.OUT_FILE.exists()
