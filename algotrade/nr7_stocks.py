"""Research: does the NR7 breakout (nr7.py) hold up on F&O stocks, as it does on the indices? Backtest only.

The same rules and conservative fills as nr7.backtest_index() on each stock's daily history (Yahoo), costs
COST_PCT of the price per round trip (stock futures: STT, charges and wider slippage than an index), also at
double. Results days are looked at separately: Yahoo has each stock's results dates from about 2014, so trades
from 2015 are split into those whose NR7 day to exit included a results date and the rest.

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


def _results_inside(t: dict, days: list[str]) -> bool:
    """A results date from the NR7 day to the exit day (announced after the NR7 close, it moves the trigger day)."""
    lo, hi = t["nr7_date"], t["exit_at"][:10]
    return any(lo <= d <= hi for d in days)


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


def run(period: str = "max") -> dict:
    from .data import fetch_ohlcv
    symbols = fno_stocks()
    log.info("NR7 stock backtest: %d F&O stocks", len(symbols))
    data = fetch_ohlcv(symbols, "1d", period=period, market="IN")
    res_days = results_dates(list(data))
    rows = []
    for sym, df in data.items():
        df = df[(df.index.dayofweek < 5) & (df["High"] > df["Low"]) & (df["Close"] >= MIN_PRICE)]
        for t in nr7.backtest_index(df, sym, cost_pct=COST_PCT):
            extra = COST_PCT / 100 * t["entry"] / t["risk"]   # the same cost again: costs doubled
            rows.append({"sym": sym, "nr7_date": t["nr7_date"], "entry_at": t["entry_at"][:10],
                         "exit_at": t["exit_at"][:10], "side": t["side"], "status": t["status"], "r": t["r"],
                         "r_cost2": round(t["r"] - extra, 3), "cost_r": round(extra, 3),
                         "results": _results_inside(t, res_days.get(sym, []))})
    tr = pd.DataFrame(rows)
    if tr.empty:
        return {"trades": 0}
    tr["year"] = tr["entry_at"].str[:4]
    recent = tr[tr["entry_at"] >= RESULTS_FROM]
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
        "since_2015_no_results": _stats(recent.loc[~recent["results"], "r"]),
        "since_2015_with_results": _stats(recent.loc[recent["results"], "r"]),
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
