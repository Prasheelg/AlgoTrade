"""Live trading results from the signal journal (signals.db), before and after costs.

A closed trade is a signal that was filled and then exited (T2 hit, SL hit, breakeven or time exit);
cancelled entries never traded and are left out. R uses the planned risk, as on the dashboard, and costs
use the same model as the backtest (costs.py), with the position size the signal was sent with."""
from datetime import date, timedelta

import pandas as pd

from . import config, costs, markets, state

CLOSED = ("T2_HIT", "SL_HIT", "BREAKEVEN", "EXPIRED")
EXIT_TEXT = {"T2_HIT": "Target 2", "SL_HIT": "Stop loss", "BREAKEVEN": "Breakeven", "EXPIRED": "Time exit"}
TF_TEXT = {"5m": "5 min", "15m": "15 min", "30m": "30 min", "1h": "1 hour", "1d": "Daily"}
MIN_TRADES = 30  # fewer closed trades than this are too few to judge an edge


def _qty(mkt: markets.Market, entry: float, risk: float) -> int:
    """Shares the signal was sized for (same rule as the scanner)."""
    qty = int((mkt.capital * config.RISK_PER_TRADE_PCT / 100) // risk) if risk else 0
    return max(min(qty, int(mkt.capital // entry) if entry else 0), 0)


def closed_trades(market: str, since: date | None = None) -> list[dict]:
    """Closed trades of a market, oldest exit first, with gross R and R after costs."""
    mkt = markets.get(market)
    out = []
    for s in state.all_signals(market):
        if s["status"] not in CLOSED or s["exit_price"] is None or not s["closed_at"]:
            continue
        closed = date.fromisoformat(s["closed_at"][:10])
        if since and closed < since:
            continue
        sign = 1 if s["side"] == "BUY" else -1
        fill = s["fill_price"] or s["entry"]
        risk = abs(s["entry"] - s["stop_loss"])
        gross = sign * (s["exit_price"] - fill) / risk if risk else 0.0
        model = costs.model_for(market, s["timeframe"] or "1d")
        # Slippage as in the backtest: stop entries / gap fills, and every exit except a target
        slip_entry = s["entry_type"] != "limit" or fill != s["entry"]
        net = model.net_r(s["side"], fill, s["exit_price"], risk, _qty(mkt, s["entry"], risk),
                          slip_entry, s["status"] != "T2_HIT")
        opened = (s["filled_at"] or s["bar_date"] or s["closed_at"])[:10]
        out.append({"key": s["key"], "symbol": s["symbol"], "side": s["side"], "timeframe": s["timeframe"],
                    "entry_type": s["entry_type"], "status": s["status"], "closed_at": str(closed),
                    "hold_days": (closed - date.fromisoformat(opened)).days,
                    "strategies": [r for r in (s["reasons"] or "").split(", ") if r],
                    "r": round(gross, 2), "net_r": round(net, 2)})
    return sorted(out, key=lambda t: (t["closed_at"], t["key"]))


def _stats(rs: list[float]) -> dict:
    won = [r for r in rs if r > 0]
    lost = -sum(r for r in rs if r < 0)
    return {"avg_r": round(sum(rs) / len(rs), 3) if rs else None, "total_r": round(sum(rs), 2),
            "win_rate": round(len(won) / len(rs), 3) if rs else None,
            "profit_factor": round(sum(won) / lost, 2) if lost else None}


def _group(trades: list[dict], key) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        for g in key(t):
            groups.setdefault(g, []).append(t)
    rows = [{"group": g, "trades": len(ts), "avg_gross_r": _stats([t["r"] for t in ts])["avg_r"],
             **_stats([t["net_r"] for t in ts])} for g, ts in groups.items()]
    return sorted(rows, key=lambda r: (-r["trades"], r["group"]))


def _max_drawdown(curve: list[float]) -> float:
    peak, dd = 0.0, 0.0
    for v in curve:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return round(dd, 2)


def backtest_reference(market: str) -> dict | None:
    """Average R before / after costs from the last backtest of this market, if it was run with costs."""
    path = config.DATA_DIR / ("backtest_trades.csv" if market == "IN" else f"backtest_trades_{market}.csv")
    try:
        bt = pd.read_csv(path)
    except (OSError, ValueError):
        return None
    if bt.empty or not {"R", "net_R"} <= set(bt.columns):
        return None
    return {"trades": len(bt), "avg_gross_r": round(float(bt.R.mean()), 3),
            "avg_r": round(float(bt.net_R.mean()), 3)}


# ---- strategy scorecard ---------------------------------------------------------------------------

OFF_KEY = "scanner_strategies_off"   # {market: [strategy names the continuous scanner leaves out]}
SCORE_MIN_LIVE = 10                  # closed live trades before a strategy's own results decide its verdict
SCORE_MIN_BACKTEST = 30


def strategies_off(market: str) -> list[str]:
    return list((state.get_setting(OFF_KEY, {})[0] or {}).get(market, []))


def set_strategy_off(market: str, name: str, off: bool) -> list[str]:
    from .strategies import STRATEGIES
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy {name}")
    allv = state.get_setting(OFF_KEY, {})[0] or {}
    cur = set(allv.get(market, []))
    cur = cur | {name} if off else cur - {name}
    allv[market] = sorted(cur)
    state.set_setting(OFF_KEY, allv)
    return allv[market]


def _verdict(avg_r: float | None) -> str:
    if avg_r is None:
        return "No data"
    return "Keep" if avg_r >= 0.1 else "Watch" if avg_r >= -0.05 else "Drop"


def scorecard(market: str) -> dict:
    """Per strategy: live results after costs, the last dashboard backtest, and a verdict (Keep / Watch / Drop)
    from the live results once there are SCORE_MIN_LIVE trades, else from the backtest."""
    from . import backtest_job
    from .strategies import describe
    live = {r["group"]: r for r in _group(closed_trades(market), lambda t: t["strategies"] or ["(none)"])}
    bt = backtest_job.last(market)
    bts = {r["group"]: r for r in (bt or {}).get("by_strategy", [])}
    off = set(strategies_off(market))
    rows = []
    for s in describe():
        n = s["name"]
        lv, b = live.get(n), bts.get(n)
        if lv and lv["trades"] >= SCORE_MIN_LIVE:
            verdict, basis = _verdict(lv["avg_r"]), "live"
        elif b and b["trades"] >= SCORE_MIN_BACKTEST:
            verdict, basis = _verdict(b["avg_r"]), "backtest"
        else:
            verdict, basis = "Too few trades", None
        rows.append({"name": n, "description": s["description"], "default": s["default"], "off": n in off,
                     "live": lv, "backtest": b, "verdict": verdict, "basis": basis})
    order = {"Drop": 0, "Watch": 1, "Keep": 2, "Too few trades": 3, "No data": 4}
    rows.sort(key=lambda r: (order[r["verdict"]], -((r["live"] or {}).get("trades") or 0), r["name"]))
    return {"market": market, "rows": rows, "min_live": SCORE_MIN_LIVE, "min_backtest": SCORE_MIN_BACKTEST,
            "backtest": {k: bt[k] for k in ("finished", "params", "universe")} if bt else None}


def report(market: str, days: int | None = None) -> dict:
    since = date.today() - timedelta(days=days) if days else None
    trades = closed_trades(market, since)
    gross, net, curve = 0.0, 0.0, []
    for t in trades:
        gross, net = gross + t["r"], net + t["net_r"]
        curve.append({"date": t["closed_at"], "symbol": t["symbol"], "side": t["side"], "timeframe": t["timeframe"],
                      "exit": EXIT_TEXT[t["status"]], "r": t["r"],
                      "net_r": t["net_r"], "gross": round(gross, 2), "net": round(net, 2)})
    open_count = sum(s["status"] in state.OPEN_STATUSES for s in state.all_signals(market))
    from . import journal
    tags = {k: e["tags"] for k, e in journal.all_entries().items() if e["tags"]}
    mkt = markets.get(market)
    return {
        "market": market, "days": days, "trades": len(trades), "open": open_count, "min_trades": MIN_TRADES,
        "currency": mkt.currency, "locale": mkt.locale, "r_value": mkt.capital * config.RISK_PER_TRADE_PCT / 100,
        "gross": _stats([t["r"] for t in trades]),
        "net": {**_stats([t["net_r"] for t in trades]), "max_drawdown_r": _max_drawdown([c["net"] for c in curve])},
        "avg_hold_days": round(sum(t["hold_days"] for t in trades) / len(trades), 1) if trades else None,
        "equity": curve,
        "breakdowns": [
            {"name": "Strategy", "note": "a trade counts once for each strategy that voted for it",
             "rows": _group(trades, lambda t: t["strategies"] or ["(none)"])},
            {"name": "Timeframe", "rows": _group(trades, lambda t: [TF_TEXT.get(t["timeframe"], t["timeframe"])])},
            {"name": "Exit", "rows": _group(trades, lambda t: [EXIT_TEXT[t["status"]]])},
            {"name": "Entry order", "rows": _group(trades, lambda t: [(t["entry_type"] or "market").capitalize()])},
            {"name": "Side", "rows": _group(trades, lambda t: [t["side"]])},
            {"name": "Tag", "note": "your trade journal tags; a trade counts once for each tag",
             "rows": _group(trades, lambda t: tags.get(t["key"]) or ["(not journaled)"])},
        ],
        "backtest": backtest_reference(market),
    }
