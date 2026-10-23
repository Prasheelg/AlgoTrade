"""Meta-labeling: a model that learns which of the rule-based signals tend to work.

The strategies still decide BUY / SELL; the model only scores each signal by how likely it is to end in
profit after costs, from the market conditions on the signal bar (features()). walk_forward() tests that
honestly: each year is scored by a model trained only on trades that closed before the year began."""
import logging

import numpy as np
import pandas as pd

from .strategies import STRATEGIES

log = logging.getLogger(__name__)

MIN_TRAIN = 300   # trades needed before a year can be scored


def _num(x) -> float:
    return float(x) if pd.notna(x) else np.nan


def features(df: pd.DataFrame, i: int, sig) -> dict:
    """Market conditions on signal bar i of an add_all() frame, as model inputs. Directional values are
    multiplied by the side (+1 BUY, -1 SELL), so "with the trade" is always positive."""
    r = df.iloc[i]
    sign = 1 if sig.side == "BUY" else -1
    c, a = float(r["Close"]), _num(r["ATR"]) or np.nan
    close, idx = df["Close"], df["IDX_CLOSE"]
    ext = (c - r["HH20"]) if sign > 0 else (r["LL20"] - c)   # beyond the 20-bar range on the trade's side
    out = {
        "f_side": sign,
        "f_score": sig.score,
        "f_n_strategies": len(sig.reasons),
        "f_limit_entry": int(sig.entry_type == "limit"),
        "f_adx": _num(r["ADX"]),
        "f_rsi": sign * (_num(r["RSI"]) - 50),
        "f_rsi2": sign * (_num(r["RSI2"]) - 50),
        "f_vol_ratio": _num(r["Volume"] / r["VOL_AVG20"]) if r["VOL_AVG20"] else np.nan,
        "f_atr_pct": a / c * 100,
        "f_ema200_dist": sign * (c - r["EMA200"]) / a,
        "f_ema50_dist": sign * (c - r["EMA50"]) / a,
        "f_ema9_21": sign * (r["EMA9"] - r["EMA21"]) / a,
        "f_supertrend": sign * _num(r["ST_DIR"]),
        "f_macd_hist": sign * r["MACD_HIST"] / a,
        "f_bb_width_pct": (r["BB_UP"] - r["BB_LO"]) / c * 100,
        "f_squeeze": int(bool(r["SQUEEZE"])),
        "f_range_ext": _num(ext) / a,
        "f_ret5": sign * (c / close.iloc[i - 5] - 1) * 100,
        "f_ret20": sign * (c / close.iloc[i - 20] - 1) * 100,
        "f_rs": sign * np.log(r["RS"]) if pd.notna(r["RS"]) and r["RS"] > 0 else np.nan,
        "f_idx_trend": sign * (r["IDX_CLOSE"] / r["IDX_EMA200"] - 1) * 100,
        "f_idx_ret20": sign * (idx.iloc[i] / idx.iloc[i - 20] - 1) * 100,
        "f_risk_atr": abs(sig.entry - sig.stop_loss) / a,
        "f_weekday": df.index[i].weekday(),
    }
    out.update({f"s_{n}": int(n in sig.reasons) for n in STRATEGIES})
    return {k: (round(v, 4) if isinstance(v, float) and np.isfinite(v) else v) for k, v in out.items()}


def feature_columns(trades: pd.DataFrame) -> list[str]:
    return [c for c in trades.columns if c.startswith(("f_", "s_"))]


def model():
    """Small, heavily regularised gradient-boosted trees: a few thousand trades can't support more."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=150, min_samples_leaf=50,
                                          l2_regularization=1.0, random_state=0)


def walk_forward(trades: pd.DataFrame, score_on: pd.DataFrame | None = None, label: str = "net_R",
                 min_train: int = MIN_TRAIN) -> pd.DataFrame:
    """Score each calendar year with a model trained on `trades` that closed before that year started
    (expanding window, so nothing from the scored year or later is seen). Returns score_on (default: trades)
    with p_win (the model's probability that net_R > 0) and threshold (the training set's win rate: a signal
    is kept when p_win >= threshold, i.e. the model rates it above an average signal). Unscored years: NaN."""
    trades = trades.assign(signal_time=pd.to_datetime(trades["signal_time"]),
                           exit_time=pd.to_datetime(trades["exit_time"]))
    out = (trades if score_on is None else score_on).copy()
    out["signal_time"] = pd.to_datetime(out["signal_time"])
    out["p_win"] = out["threshold"] = np.nan
    cols = feature_columns(trades)
    for year in sorted(out["signal_time"].dt.year.unique()):
        start = pd.Timestamp(year, 1, 1)
        train = trades[trades["exit_time"] < start]
        if len(train) < min_train:
            continue
        y = (train[label] > 0).astype(int)
        m = model().fit(train[cols], y)
        test = out["signal_time"].dt.year == year
        if test.any():
            out.loc[test, "p_win"] = m.predict_proba(out.loc[test, cols])[:, 1]
            out.loc[test, "threshold"] = y.mean()
    return out


def _stats(r: pd.Series) -> dict:
    wins, losses = r[r > 0], r[r <= 0]
    return {"trades": len(r), "win_rate": round(len(wins) / len(r), 3) if len(r) else np.nan,
            "avg_net_R": round(r.mean(), 3) if len(r) else np.nan, "total_net_R": round(r.sum(), 1),
            "profit_factor": round(wins.sum() / -losses.sum(), 2) if len(losses) and losses.sum() else np.nan}


def report(scored: pd.DataFrame, label: str = "net_R") -> str:
    """Baseline (every scored signal) against the filter (only p_win >= threshold), overall and per year,
    plus net R by quintile of p_win: if the model has learned anything, it rises from Q1 to Q5."""
    s = scored[scored["p_win"].notna()]
    if s.empty:
        return "Not enough history to score any year."
    kept = s[s["p_win"] >= s["threshold"]]
    rows = {"All signals (rules only)": _stats(s[label]), "Kept by the model": _stats(kept[label]),
            "Skipped by the model": _stats(s.loc[~s.index.isin(kept.index), label])}
    out = [pd.DataFrame(rows).T.to_string()]
    years = []
    for year, g in s.groupby(s["signal_time"].dt.year):
        k = g[g["p_win"] >= g["threshold"]]
        years.append({"year": year, "signals": len(g), "avg_net_R": round(g[label].mean(), 3),
                      "total_net_R": round(g[label].sum(), 1), "kept": len(k),
                      "kept_avg_net_R": round(k[label].mean(), 3) if len(k) else np.nan,
                      "kept_total_net_R": round(k[label].sum(), 1)})
    out.append("Per year:\n" + pd.DataFrame(years).set_index("year").to_string())
    q = pd.qcut(s["p_win"].rank(method="first"), 5, labels=["Q1 (lowest)", "Q2", "Q3", "Q4", "Q5 (highest)"])
    quint = s.groupby(q, observed=True).agg(trades=(label, "size"), p_win=("p_win", "mean"),
                                            win_rate=(label, lambda r: (r > 0).mean()), avg_net_R=(label, "mean"))
    out.append("By model score (quintiles):\n" + quint.round(3).to_string())
    return "\n\n".join(out)
