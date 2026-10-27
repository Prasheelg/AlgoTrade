"""Research: does the NR7 breakout (nr7.py) hold up on F&O stocks, as it does on the indices? Backtest only.

The same rules and conservative fills as nr7.backtest_index() on each stock's daily history (Yahoo), costs
COST_PCT of the price per round trip (stock futures: STT, charges and wider slippage than an index), also at
double. run_results() tests the one variant: an NR7 taken only when results are due within a few trading days
(Yahoo has each stock's results dates from about 2014, so from 2015).

Caveat: the stocks are today's F&O list, so stocks that dropped out of F&O (often after falling) are missing."""
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from . import config, nr7

log = logging.getLogger(__name__)
COST_PCT = 0.10
RESULTS_FROM = "2015-01-01"       # results dates are complete from about here
RESULTS_FILE = config.DATA_DIR / "nr7_results_dates.json"
OUT_FILE = config.DATA_DIR / "nr7_backtest_stocks.json"
MIN_TRADES_PER_STOCK = 10
MIN_PRICE = 10.0   # older split-adjusted prices below this are too coarse at two decimals
BOOTSTRAP = 2000


def fno_stocks() -> list[str]:
    from . import fno
    return [u["name"] for u in fno.underlyings() if u["kind"] == "stock"]


def _results_for(sym: str) -> list[str]:
    import yfinance as yf
    from .universe import to_yahoo
    try:
        d = yf.Ticker(to_yahoo(sym)).get_earnings_dates(limit=60)
    except Exception as exc:
        log.debug("Results dates for %s failed (%s)", sym, exc)
        return []
    if d is None or d.empty:
        return []
    return sorted({str(ts.tz_convert("Asia/Kolkata").date()) for ts in d.index})


def results_dates(symbols: list[str]) -> dict[str, list[str]]:
    """Past results dates per stock, cached in RESULTS_FILE (fetched once; delete the file to refresh)."""
    cached = json.loads(RESULTS_FILE.read_text(encoding="utf-8")) if RESULTS_FILE.exists() else {}
    todo = [s for s in symbols if s not in cached]
    if todo:
        log.info("Fetching results dates for %d stocks", len(todo))
        with ThreadPoolExecutor(8) as ex:
            for sym, days in zip(todo, ex.map(_results_for, todo)):
                cached[sym] = days
        RESULTS_FILE.write_text(json.dumps(cached), encoding="utf-8")
    return {s: cached.get(s, []) for s in symbols}


def _stats(r: pd.Series) -> dict:
    if not len(r):
        return {"trades": 0}
    wins, losses = r[r > 0], r[r <= 0]
    avg_win = wins.mean() if len(wins) else 0.0
    avg_loss = -losses.mean() if len(losses) else 0.0
    rng = np.random.default_rng(7)
    means = np.concatenate([rng.choice(r.values, size=(100, len(r)), replace=True).mean(axis=1)   # in chunks:
                            for _ in range(BOOTSTRAP // 100)])                                      # memory
    return {"trades": int(len(r)), "win_pct": round(len(wins) / len(r) * 100, 1), "avg_r": round(float(r.mean()), 3),
            "total_r": round(float(r.sum()), 1), "avg_win_r": round(float(avg_win), 2),
            "avg_loss_r": round(float(avg_loss), 2),
            "breakeven_win_pct": round(avg_loss / (avg_win + avg_loss) * 100, 1) if avg_win + avg_loss else None,
            "worst_r": round(float(r.min()), 2), "p_no_edge": round(float((means <= 0).mean()), 3)}


def _stock_data(period: str = "max") -> dict[str, pd.DataFrame]:
    """Daily history of every F&O stock, cleaned (weekdays, a real range, MIN_PRICE); cached on disk for the day."""
    from .data import fetch_ohlcv
    cache = config.DATA_DIR / f"nr7_stock_daily_{pd.Timestamp.today().date()}_{period}.pkl"
    if cache.exists():
        return pd.read_pickle(cache)
    symbols = fno_stocks()
    log.info("NR7 stock backtest: %d F&O stocks", len(symbols))
    data = {s: df[(df.index.dayofweek < 5) & (df["High"] > df["Low"]) & (df["Close"] >= MIN_PRICE)]
            for s, df in fetch_ohlcv(symbols, "1d", period=period, market="IN").items()}
    for old in config.DATA_DIR.glob("nr7_stock_daily_*.pkl"):
        old.unlink(missing_ok=True)
    pd.to_pickle(data, cache)
    return data


def run(period: str = "max") -> dict:
    data = _stock_data(period)
    rows = []
    for sym, df in data.items():
        for t in nr7.backtest_index(df, sym, cost_pct=COST_PCT):
            extra = COST_PCT / 100 * t["entry"] / t["risk"]   # the same cost again: costs doubled
            rows.append({"sym": sym, "nr7_date": t["nr7_date"], "entry_at": t["entry_at"][:10],
                         "exit_at": t["exit_at"][:10], "side": t["side"], "status": t["status"], "r": t["r"],
                         "r_cost2": round(t["r"] - extra, 3), "cost_r": round(extra, 3)})
    tr = pd.DataFrame(rows)
    if tr.empty:
        return {"trades": 0}
    tr["year"] = tr["entry_at"].str[:4]
    by_stock = tr.groupby("sym")["r"].agg(["count", "mean"])
    by_stock = by_stock[by_stock["count"] >= MIN_TRADES_PER_STOCK]
    years = tr.groupby("year")["r"].sum()
    out = {
        "run_at": pd.Timestamp.now().isoformat(timespec="seconds"), "stocks": int(tr["sym"].nunique()),
        "from": tr["entry_at"].min(), "to": tr["exit_at"].max(), "cost_pct": COST_PCT,
        "all": _stats(tr["r"]),
        "costs_doubled": _stats(tr["r_cost2"]),
        "until_2012": _stats(tr.loc[tr["entry_at"] < "2013-01-01", "r"]),
        "from_2013": _stats(tr.loc[tr["entry_at"] >= "2013-01-01", "r"]),
        "long": _stats(tr.loc[tr["side"] == "LONG", "r"]),
        "short": _stats(tr.loc[tr["side"] == "SHORT", "r"]),
        "median_cost_r": round(float(tr["cost_r"].median()), 3),
        "stocks_positive_pct": round(float((by_stock["mean"] > 0).mean() * 100), 1) if len(by_stock) else None,
        "stocks_counted": int(len(by_stock)),
        "best_stocks": {k: round(v, 2) for k, v in by_stock["mean"].nlargest(8).items()},
        "worst_stocks": {k: round(v, 2) for k, v in by_stock["mean"].nsmallest(8).items()},
        "years_positive_pct": round(float((years > 0).mean() * 100), 1),
        "years": {y: round(float(v), 1) for y, v in years.items()},
        "exits": {k: int(v) for k, v in tr["status"].value_counts().items()},
    }
    OUT_FILE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    tr.to_csv(config.DATA_DIR / "nr7_stock_trades.csv", index=False)
    return out


# ---- the results variant: NR7 only when results are due within RESULTS_WINDOW trading days -----------------------
# Fixed before it was run (the earlier "results inside the trade" slice used the exit day, i.e. the outcome).

RESULTS_WINDOW = 5         # results on the NR7 day or the 4 trading days after it
COIN_FLIPS = 100
VARIANT_FILE = config.DATA_DIR / "nr7_backtest_results_variant.json"


def results_soon(days: list[str], window: int = RESULTS_WINDOW):
    """only() for backtest_index: results due from the NR7 day to window-1 trading days after it (from 2015)."""
    stamps = sorted(pd.Timestamp(d) for d in days)

    def only(d: pd.Timestamp) -> bool:
        if d < pd.Timestamp(RESULTS_FROM):
            return False
        end = d + pd.offsets.BDay(window - 1)
        return any(d <= s <= end for s in stamps)
    return only


def _trades(data, res_days, only_for, side_for=None) -> pd.DataFrame:
    rows = []
    for sym, df in data.items():
        only = only_for(res_days.get(sym, []))
        for t in nr7.backtest_index(df, sym, cost_pct=COST_PCT, only=only, side_for=side_for):
            rows.append({"sym": sym, "nr7_date": t["nr7_date"], "entry_at": t["entry_at"][:10], "side": t["side"],
                         "status": t["status"], "r": t["r"], "r_cost2": round(t["r"] - COST_PCT / 100 * t["entry"] / t["risk"], 3)})
    return pd.DataFrame(rows)


def run_results(period: str = "max") -> dict:
    data = {s: df[df.index >= "2014-12-01"] for s, df in _stock_data(period).items()}   # all it needs: faster flips
    res_days = results_dates(list(data))
    main = _trades(data, res_days, results_soon)
    if main.empty:
        return {"trades": 0}

    def control(days):   # the same period, NR7 days without results due
        soon = results_soon(days)
        return lambda d: d >= pd.Timestamp(RESULTS_FROM) and not soon(d)
    other = _trades(data, res_days, control)
    rng = np.random.default_rng(11)
    flips = []
    for _ in range(COIN_FLIPS):
        f = _trades(data, res_days, results_soon, side_for=lambda: "LONG" if rng.random() < 0.5 else "SHORT")
        flips.append(float(f["r"].mean()) if len(f) else 0.0)
    flips = np.array(flips)
    by_year = main.assign(year=main["entry_at"].str[:4]).groupby("year")["r"].agg(["count", "mean"])
    by_stock = main.groupby("sym")["r"].mean()
    out = {
        "run_at": pd.Timestamp.now().isoformat(timespec="seconds"), "window": RESULTS_WINDOW, "cost_pct": COST_PCT,
        "stocks": int(main["sym"].nunique()), "from": main["entry_at"].min(), "to": main["entry_at"].max(),
        "variant": _stats(main["r"]),
        "costs_doubled": _stats(main["r_cost2"]),
        "2015_2020": _stats(main.loc[main["entry_at"] < "2021-01-01", "r"]),
        "2021_on": _stats(main.loc[main["entry_at"] >= "2021-01-01", "r"]),
        "long": _stats(main.loc[main["side"] == "LONG", "r"]),
        "short": _stats(main.loc[main["side"] == "SHORT", "r"]),
        "no_results_control": _stats(other["r"]),
        "window_3": _stats(_trades(data, res_days, lambda days: results_soon(days, 3))["r"]),
        "window_10": _stats(_trades(data, res_days, lambda days: results_soon(days, 10))["r"]),
        "coin_flip_avg_r": round(float(flips.mean()), 3),
        "beats_coin_flip_pct": round(float((flips < main["r"].mean()).mean() * 100), 1),
        "worse_than_3r_pct": round(float((main["r"] < -3).mean() * 100), 2),
        "stocks_positive_pct": round(float((by_stock > 0).mean() * 100), 1),
        "years": {y: {"trades": int(v["count"]), "avg_r": round(float(v["mean"]), 2)} for y, v in by_year.iterrows()},
        "exits": {k: int(v) for k, v in main["status"].value_counts().items()},
    }
    VARIANT_FILE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    main.to_csv(config.DATA_DIR / "nr7_results_variant_trades.csv", index=False)
    return out
