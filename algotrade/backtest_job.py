"""Backtests started from the dashboard (Performance > Backtest): one at a time, in a background thread.

Daily bars over the market's universe, with the given strategies / minimum votes / filters, after costs.
The result (summary, equity curve, results per strategy) is kept in data/backtest_dashboard_<market>.json,
which the strategy scorecard also reads."""
import json
import logging
import threading
import time
from datetime import datetime

from . import config, costs, markets

log = logging.getLogger(__name__)
PERIODS = ("2y", "5y", "10y")
CURVE_POINTS = 400
_job: dict = {"running": False, "market": None, "started": None, "params": None, "error": None}
_lock = threading.Lock()


def _path(market: str):
    return config.DATA_DIR / f"backtest_dashboard_{market}.json"


def last(market: str) -> dict | None:
    try:
        return json.loads(_path(market).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _stats(rs: list[float]) -> dict:
    won = [r for r in rs if r > 0]
    lost = -sum(r for r in rs if r < 0)
    return {"trades": len(rs), "avg_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 2),
            "win_rate": round(len(won) / len(rs), 3) if rs else None,
            "profit_factor": round(sum(won) / lost, 2) if lost else None}


def summarize(trades: list[dict]) -> dict:
    trades = sorted(trades, key=lambda t: str(t["exit_time"]))
    net = [t.get("net_R", t["R"]) for t in trades]
    curve, total, peak, dd = [], 0.0, 0.0, 0.0
    for t, r in zip(trades, net):
        total += r
        peak, dd = max(peak, total), max(dd, peak - total)
        curve.append({"date": str(t["exit_time"])[:10], "net": round(total, 2)})
    step = max(1, len(curve) // CURVE_POINTS)
    by: dict[str, list[float]] = {}
    for t, r in zip(trades, net):
        for name in [x for x in str(t.get("strategies") or "").split(", ") if x] or ["(none)"]:
            by.setdefault(name, []).append(r)
    return {"gross": _stats([t["R"] for t in trades]), "net": {**_stats(net), "max_drawdown_r": round(dd, 2)},
            "equity": curve[::step] + (curve[-1:] if curve and (len(curve) - 1) % step else []),
            "by_strategy": sorted(({"group": k, **_stats(v)} for k, v in by.items()), key=lambda x: -x["trades"]),
            "by_side": [{"group": side, **_stats([r for t, r in zip(trades, net) if t["side"] == side])}
                        for side in sorted({t["side"] for t in trades})],
            "symbols": len({t["symbol"] for t in trades})}


def _run(market: str, params: dict) -> None:
    from . import backtest
    from .universe import get_universe
    t0 = time.monotonic()
    try:
        syms = get_universe(market=market)
        df = backtest.run(syms, "1d", period=params["period"], market=market, costs=costs.model_for(market, "1d"),
                          strategies=params["strategies"], min_score=params["min_score"],
                          filters=[f for f in params["filters"] if f != "events"], tag_strategies=True)
        trades = df.to_dict("records") if not df.empty else []
        out = {"market": market, "params": params, "universe": len(syms), "finished": datetime.now().isoformat(timespec="seconds"),
               "seconds": round(time.monotonic() - t0), **summarize(trades)}
        _path(market).write_text(json.dumps(out, default=str), encoding="utf-8")
        log.info("Dashboard backtest %s: %d trades in %.0f s", market, len(trades), time.monotonic() - t0)
    except Exception as exc:
        log.exception("Dashboard backtest failed")
        _job["error"] = str(exc)[:300]
    finally:
        _job["running"] = False


def start(market: str, strategies: list[str] | None, min_score: int | None, filters: list[str] | None,
          period: str = "5y") -> dict:
    from .scanner import FILTERS
    from .strategies import STRATEGIES, family_of
    market = markets.get(market).code
    if period not in PERIODS:
        raise ValueError(f"Period must be one of {', '.join(PERIODS)}")
    names = list(strategies or [])
    unknown = set(names) - set(STRATEGIES)
    if not names or unknown:
        raise ValueError("Pick at least one known strategy" + (f" (unknown: {', '.join(sorted(unknown))})" if unknown else ""))
    votes = len({family_of(n) for n in names})
    min_score = int(min_score or 1)
    if not 1 <= min_score <= votes:
        raise ValueError(f"Minimum votes must be between 1 and {votes}")
    filters = [f for f in (filters or []) if f in FILTERS]
    with _lock:
        if _job["running"]:
            raise RuntimeError(f"A {_job['market']} backtest is already running")
        params = {"strategies": names, "min_score": min_score, "filters": filters, "period": period}
        _job.update(running=True, market=market, started=datetime.now().isoformat(timespec="seconds"),
                    params=params, error=None)
    threading.Thread(target=_run, args=(market, params), name="backtest", daemon=True).start()
    return status(market)


def status(market: str) -> dict:
    mine = _job["market"] == market
    return {"running": _job["running"] and mine, "busy_market": _job["market"] if _job["running"] else None,
            "started": _job["started"] if mine else None, "error": _job["error"] if mine else None,
            "params": _job["params"] if mine else None, "periods": PERIODS, "last": last(market)}
