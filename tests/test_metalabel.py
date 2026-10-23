"""Meta-labeling: metalabel.features / walk_forward / report and the backtest's features hook."""
import numpy as np
import pandas as pd
import pytest

from algotrade import backtest, metalabel
from algotrade.indicators import add_all
from algotrade.strategies import STRATEGIES
from conftest import make_signal
from test_backtest import NAMES, hand


def walk(n=320, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close,
                       "Volume": rng.integers(1000, 5000, n).astype(float)}, index=idx)
    index_close = pd.Series(close[::-1].copy(), index=idx)
    return add_all(df, index_close)


def test_features_flip_sign_with_side_and_flag_voting_strategies():
    df = walk()
    buy = metalabel.features(df, 300, make_signal(side="BUY", entry=99.0, stop_loss=96.0))
    sell = metalabel.features(df, 300, make_signal(side="SELL", entry=99.0, stop_loss=102.0))
    for k in ("f_rsi", "f_ema200_dist", "f_ret20", "f_rs", "f_idx_trend"):
        assert buy[k] == pytest.approx(-sell[k])
    assert buy["f_side"] == 1 and sell["f_side"] == -1
    assert buy["f_atr_pct"] == sell["f_atr_pct"] and buy["f_risk_atr"] == sell["f_risk_atr"]
    assert {k for k in buy if k.startswith("s_")} == {f"s_{n}" for n in STRATEGIES}
    assert buy["s_RSI Pullback"] == 1 and buy["s_EMA 9/21 Cross"] == 1 and buy["s_20-bar Breakout"] == 0


def test_backtest_records_features_of_the_signal_bar(monkeypatch):
    seen = []
    monkeypatch.setattr(metalabel, "features", lambda df, i, sig: seen.append(i) or {"f_x": 1.0})
    df, v = hand(bars={201: dict(Low=98.5), 202: dict(High=104.0)})
    trades = backtest.backtest_symbol("X", df, strategies=NAMES, votes=v, min_score=1, filters=[], features=True)
    assert seen == [200]
    assert trades[0]["signal_time"] == df.index[200] and trades[0]["f_x"] == 1.0
    assert "f_x" not in backtest.backtest_symbol("X", df, strategies=NAMES, votes=v, min_score=1, filters=[])[0]


def trades_frame(per_year=200, years=(2020, 2021, 2022), seed=0):
    """Synthetic trades where f_good > 0 wins: a model that learns anything should find it."""
    rng = np.random.default_rng(seed)
    rows = []
    for y in years:
        for d in pd.date_range(f"{y}-01-05", f"{y}-12-10", periods=per_year):
            good = rng.normal()
            rows.append({"signal_time": d, "exit_time": d + pd.Timedelta(days=10), "f_good": good,
                         "f_noise": rng.normal(), "net_R": (1.5 if good > 0 else -1.0) + rng.normal(0, 0.1)})
    return pd.DataFrame(rows)


def test_walk_forward_trains_only_on_trades_closed_before_the_year(monkeypatch):
    t = trades_frame(per_year=200, years=(2020, 2021))
    t.loc[len(t) - 1, "exit_time"] = pd.Timestamp("2022-01-05")   # a 2021 trade that closes in 2022
    t.loc[len(t)] = t.iloc[0].to_dict() | {"signal_time": pd.Timestamp("2022-03-01"),
                                           "exit_time": pd.Timestamp("2022-03-09")}
    sizes = []
    real = metalabel.model

    class Spy:
        def fit(self, X, y):
            sizes.append(len(X))
            self.m = real().fit(X, y)
            return self

        def predict_proba(self, X):
            return self.m.predict_proba(X)

    monkeypatch.setattr(metalabel, "model", Spy)
    out = metalabel.walk_forward(t, min_train=150)
    # 2020: nothing closed before it -> unscored; 2021 trains on 2020; 2022 on 2020 + 2021 minus the late one
    assert out.loc[out.signal_time.dt.year == 2020, "p_win"].isna().all()
    assert sizes == [200, 399]
    assert out.loc[out.signal_time.dt.year > 2020, "p_win"].notna().all()


def test_walk_forward_learns_a_real_edge_and_report_shows_it():
    scored = metalabel.walk_forward(trades_frame())
    s = scored[scored.p_win.notna()]
    kept = s[s.p_win >= s.threshold]
    assert kept.net_R.mean() > s.net_R.mean() + 0.5
    text = metalabel.report(scored)
    assert "Kept by the model" in text and "Q5 (highest)" in text and "2022" in text


def test_report_without_scored_years():
    assert metalabel.report(metalabel.walk_forward(trades_frame(per_year=50, years=(2020,)))) \
        == "Not enough history to score any year."
