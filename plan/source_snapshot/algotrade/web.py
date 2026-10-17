"""Local dashboard for viewing signals: http://127.0.0.1:8050"""
import logging
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import config, markets, state, tracker
from .scanner import FILTERS, scan_and_alert
from .strategies import DEFAULT_STRATEGIES, describe, family_of

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
REFRESH_SEC = 300

app = Flask(__name__)
_lock = threading.Lock()
_scan_locks = {code: threading.Lock() for code in markets.MARKETS}  # one scan per market at a time
_last_refresh = {"at": None}


def _enrich(s: dict) -> dict:
    sign = 1 if s["side"] == "BUY" else -1
    px = s["exit_price"] if s["exit_price"] is not None else (s["ltp"] or s["entry"])
    s["current"] = px
    s["open"] = s["status"] in state.OPEN_STATUSES
    # How far price is from a pending entry, in % (positive = still has to move that much)
    s["to_entry_pct"] = round(abs(s["entry"] - px) / px * 100, 2) if px else None
    if s["status"] in ("PENDING", "CANCELLED"):  # no position, so no P&L
        s["pnl_pct"] = s["pnl_r"] = 0.0
        return s
    fill = s["fill_price"] or s["entry"]
    risk = abs(fill - s["stop_loss"]) or 1
    s["pnl_pct"] = round(sign * (px - fill) / fill * 100, 2)
    s["pnl_r"] = round(sign * (px - fill) / risk, 2)
    return s


def refresh() -> None:
    with _lock:
        try:
            tracker.update_all(notify=True)
            _last_refresh["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            log.exception("Refresh failed")


def _background():
    while True:
        refresh()
        time.sleep(REFRESH_SEC)


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


def _market():
    """Market from ?market= or the JSON body; raises ValueError if unknown."""
    body = request.get_json(silent=True) or {}
    return markets.get(request.args.get("market") or body.get("market"))


def _signals_payload(mkt: markets.Market) -> dict:
    return {
        "market": mkt.code,
        "signals": [_enrich(s) for s in state.all_signals(mkt.code)],
        "last_refresh": _last_refresh["at"],
        "config": {"universe": mkt.universe, "timeframe": config.TIMEFRAME, "capital": mkt.capital,
                   "risk_pct": config.RISK_PER_TRADE_PCT, "currency": mkt.currency, "locale": mkt.locale,
                   "tv_prefix": mkt.tv_prefix},
    }


@app.errorhandler(ValueError)
def _bad_request(exc):
    return jsonify({"error": str(exc)}), 400


@app.get("/api/signals")
def api_signals():
    return jsonify(_signals_payload(_market()))


@app.post("/api/refresh")
def api_refresh():
    mkt = _market()
    refresh()
    return jsonify(_signals_payload(mkt))


TIMEFRAMES = [("5m", "5 min"), ("15m", "15 min"), ("30m", "30 min"), ("1h", "1 hour"), ("1d", "Daily")]
ALL_TF = "all"  # scans every timeframe above, one after another


@app.get("/api/meta")
def api_meta():
    return jsonify({
        "strategies": describe(),
        "filters": [{"name": k, "description": v} for k, v in FILTERS.items()],
        "timeframes": [{"value": v, "label": l} for v, l in TIMEFRAMES] + [{"value": ALL_TF, "label": "All"}],
        "markets": [{"code": m.code, "name": m.name, "tz": m.tz,
                     "session": [t.strftime("%H:%M") for t in m.session]} for m in markets.MARKETS.values()],
        "defaults": {"timeframe": config.TIMEFRAME, "min_score": config.MIN_SCORE},
    })


def _scan_timeframes(timeframes: list[str], **kw) -> dict:
    """Run scan_and_alert on each timeframe and combine the counts.
    With more than one timeframe, symbols are labelled with their timeframe."""
    total = {"found": 0, "new": 0, "sent": 0, "symbols": []}
    for tf in timeframes:
        r = scan_and_alert(timeframe=tf, **kw)
        for k in ("found", "new", "sent"):
            total[k] += r[k]
        total["symbols"] += [f"{s} ({tf})" for s in r["symbols"]] if len(timeframes) > 1 else r["symbols"]
    return total


@app.post("/api/scan")
def api_scan():
    body = request.get_json(silent=True) or {}
    mkt = _market()
    strategies = body.get("strategies") or list(DEFAULT_STRATEGIES)
    filters = body.get("filters") or []
    timeframe = body.get("timeframe") or config.TIMEFRAME
    min_score = int(body.get("min_score") or config.MIN_SCORE)
    if timeframe != ALL_TF and timeframe not in dict(TIMEFRAMES):
        return jsonify({"error": f"Unsupported timeframe: {timeframe}"}), 400
    votes = len({family_of(s) for s in strategies})
    if not 1 <= min_score <= votes:
        return jsonify({"error": f"Minimum agreeing votes must be between 1 and {votes}"}), 400
    lock = _scan_locks[mkt.code]
    if not lock.acquire(blocking=False):
        return jsonify({"error": f"A {mkt.name} scan is already running"}), 409
    try:
        result = _scan_timeframes([v for v, _ in TIMEFRAMES] if timeframe == ALL_TF else [timeframe],
                                  send=bool(body.get("send", False)), strategies=strategies,
                                  min_score=min_score, market=mkt.code, filters=filters)
        result.update(timeframe=timeframe, strategies=strategies, min_score=min_score, filters=filters)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("Scan failed")
        return jsonify({"error": str(exc)}), 500
    finally:
        lock.release()
    return jsonify({**_signals_payload(mkt), "scan": result})


def serve(host: str = "127.0.0.1", port: int = 8050) -> None:
    threading.Thread(target=_background, daemon=True).start()
    log.info("Dashboard running at http://%s:%d", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)
