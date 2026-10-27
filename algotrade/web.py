"""Local dashboard for viewing signals: http://127.0.0.1:8050"""
import json
import logging
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, redirect, request, send_from_directory

from . import access, broker, config, kotak, markets, performance, state, telegram_bot, tracker
from .scanner import (FILTERS, ScanStopped, check_stop, clear_stop, fetch_index, request_stop, scan_and_alert,
                      stop_requested)
from .strategies import DEFAULT_STRATEGIES, describe, family_of

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
REFRESH_SEC = 300

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024   # journal screenshots
access.setup(app)
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
    risk = abs(s["entry"] - s["stop_loss"]) or 1  # planned risk: position size is set from the plan
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


# ---- phone app (installable web app) and login ----

@app.get("/login")
def login_page():
    if access.is_local() or access.logged_in():
        return redirect("/")
    return send_from_directory(STATIC, "login.html")


@app.post("/api/login")
def api_login():
    ok, msg = access.login(str((request.get_json(silent=True) or {}).get("pin", "")))
    return (jsonify({"ok": True}), 200) if ok else (jsonify({"error": msg}), 401)


@app.post("/api/logout")
def api_logout():
    access.logout()
    return jsonify({"ok": True})


@app.get("/api/access")
def api_access():
    return jsonify(access.status())


@app.get("/offline")
def offline_page():
    return send_from_directory(STATIC, "offline.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(STATIC, "manifest.webmanifest", mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    r = send_from_directory(STATIC, "sw.js", mimetype="text/javascript")
    r.headers["Cache-Control"] = "no-cache"   # a new version is picked up on the next visit
    return r


@app.get("/icons/<path:name>")
def icons(name):
    return send_from_directory(STATIC / "icons", name)


def _market():
    """Market from ?market= or the JSON body; raises ValueError if unknown."""
    body = request.get_json(silent=True) or {}
    return markets.get(request.args.get("market") or body.get("market"))


def _live_ltp(mkt: markets.Market, sigs: list[dict]) -> None:
    """While the live stream is connected, show open India signals at the streamed price (display only: the
    tracker still decides stops, targets and fills from its own checks)."""
    from . import livefeed
    if mkt.code != "IN" or not livefeed.connected():
        return
    open_ = [s for s in sigs if s["status"] in state.OPEN_STATUSES and s["exit_price"] is None]
    try:
        q = kotak.quotes(sorted({s["symbol"] for s in open_}))
    except Exception:
        log.exception("Live prices for signals failed")
        return
    for s in open_:
        if (q.get(s["symbol"]) or {}).get("ltp"):
            s["ltp"] = q[s["symbol"]]["ltp"]


def _real_trades(code: str) -> dict[str, dict]:
    """Signal key -> the Auto-trade order actually sent to Kotak for it (fill, exit, P&L), India only. The signal's
    own status is the tracker's paper result; this is what happened on the account."""
    if code != "IN":
        return {}
    from . import orders
    out = {}
    for t in orders.all_trades():
        if not t["entry_order"]:
            continue
        pnl = (round((t["exit_price"] - t["fill_price"]) * t["qty"], 2)
               if t["exit_price"] is not None and t["fill_price"] is not None else None)
        out[t["key"]] = {"state": t["state"], "qty": t["qty"], "fill_price": t["fill_price"],
                         "exit_price": t["exit_price"], "pnl": pnl, "note": t["note"]}
    return out


def _signals_payload(mkt: markets.Market) -> dict:
    sigs = state.all_signals(mkt.code)
    _live_ltp(mkt, sigs)
    real = _real_trades(mkt.code)
    return {
        "market": mkt.code,
        "signals": [{**_enrich(s), "trade": real.get(s["key"])} for s in sigs],
        "last_refresh": _last_refresh["at"],
        "config": {"universe": mkt.universe, "timeframe": config.TIMEFRAME, "capital": mkt.capital,
                   "risk_pct": config.RISK_PER_TRADE_PCT, "currency": mkt.currency, "locale": mkt.locale,
                   "tv_prefix": mkt.tv_prefix,
                   # real-time price source for tracking, or None when only the delayed Yahoo feed is used
                   "live": "Kotak Neo" if mkt.code == "IN" and kotak.enabled() else None},
    }


@app.errorhandler(ValueError)
def _bad_request(exc):
    return jsonify({"error": str(exc)}), 400


@app.get("/api/signals")
def api_signals():
    return jsonify(_signals_payload(_market()))


@app.get("/api/performance")
def api_performance():
    """Closed-trade results of a market, before and after costs; ?days=N limits to recent exits."""
    days = request.args.get("days", type=int)
    return jsonify(performance.report(_market().code, days if days and days > 0 else None))


@app.get("/api/portfolio")
def api_portfolio():
    """Kotak demat account (read-only): funds, holdings, today's positions / orders, app-managed trades.
    ?refresh=1 skips the server-side cache. Problems come back as 200 with an "error" field."""
    from . import portfolio
    return jsonify(portfolio.report(refresh=request.args.get("refresh") == "1", live=request.args.get("live") == "1"))




@app.get("/api/options/underlyings")
def api_option_underlyings():
    from . import fno
    try:
        return jsonify({"underlyings": fno.underlyings(), "max_lots": config.OPTIONS_MAX_LOTS})
    except Exception as exc:
        log.exception("Option contracts unavailable")
        return jsonify({"error": f"Option contracts unavailable: {exc}"}), 502


@app.get("/api/options/expiries")
def api_option_expiries():
    from . import fno
    return jsonify({"expiries": fno.expiries(request.args.get("u", ""))})


@app.get("/api/options/chain")
def api_option_chain():
    """Option chain around the money (?u=NIFTY&expiry=YYYY-MM-DD&width=10), plus what you hold of it."""
    from . import fno, manual
    u, expiry = request.args.get("u", ""), request.args.get("expiry", "")
    width = min(max(request.args.get("width", default=10, type=int), 2), 20)
    try:
        ch = fno.chain(u, expiry, width)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("Option chain failed")
        return jsonify({"error": f"Option chain unavailable: {exc}"}), 502
    return jsonify({**ch, "held": _held_options(), "recent": manual.recent(10)})


HELD_SEC = 5   # positions for the chain are shared this long (the chain reloads up to a few times a second)
_held: dict = {"at": -1e9, "data": {}}


def _held_options() -> dict:
    """Long positions {trading symbol: qty}, so the option ticket can offer "Sell to close"."""
    if broker.missing_credentials():
        return {}
    if time.monotonic() - _held["at"] < HELD_SEC:
        return _held["data"]
    held = {}
    try:
        from .portfolio import _rows, norm_position
        for r in _rows(broker.call("positions")):
            p = norm_position(r)
            if p["net_qty"] > 0:
                held[str(r.get("trdSym") or "").strip().upper()] = p["net_qty"]
    except Exception as exc:
        log.warning("Positions for the option chain unavailable: %s", exc)
    _held.update(at=time.monotonic(), data=held)
    return held


@app.after_request
def _orders_changed(resp):
    """After an option order, cancel or modify, the next chain asks Kotak for positions again."""
    if request.method == "POST" and request.path in ("/api/options/order", "/api/options/strategy",
                                                     "/api/orders/cancel", "/api/orders/modify"):
        _held["at"] = -1e9
    return resp


@app.post("/api/options/analyze")
def api_option_analyze():
    """Strategy analysis on live quotes (read-only): {"legs": [{"tsym", "side", "lots", "price"?}]}."""
    from . import fno
    legs = (request.get_json(silent=True) or {}).get("legs")
    if not isinstance(legs, list):
        return jsonify({"error": "Send {\"legs\": [...]}"}), 400
    try:
        return jsonify(fno.analyze_strategy(legs))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("Strategy analysis failed")
        return jsonify({"error": f"Analysis unavailable: {exc}"}), 502


@app.post("/api/options/strategy")
def api_option_strategy():
    """Place a defined-risk strategy (bought legs first, sold legs after they fill). Only from this PC."""
    from . import manual
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    b = request.get_json(silent=True) or {}
    if not isinstance(b.get("legs"), list):
        return jsonify({"error": "Send {\"legs\": [...]}"}), 400
    try:
        return jsonify(manual.place_strategy(b["legs"], str(b.get("order_type", "LIMIT")),
                                             str(b.get("product", "NRML"))))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak refused: {exc} (legs placed before this one stay placed; "
                                 "see Portfolio > Orders today)"}), 502


@app.get("/api/stocks/symbols")
def api_stock_symbols():
    """NSE equity symbols (for the Buy stock search) and the per-order value cap."""
    try:
        return jsonify({"symbols": sorted(kotak.tokens()), "max_value": config.STOCK_MAX_ORDER_VALUE})
    except Exception as exc:
        log.exception("Stock list unavailable")
        return jsonify({"error": f"Stock list unavailable: {exc}"}), 502


@app.get("/api/stocks/quote")
def api_stock_quote():
    from . import manual
    try:
        return jsonify(manual.stock_quote(request.args.get("s", "")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        log.exception("Stock quote failed")
        return jsonify({"error": f"Quote unavailable: {exc}"}), 502


@app.post("/api/stocks/buy")
def api_stock_buy():
    """Buy shares: {"symbol", "qty", "order_type": LIMIT|MARKET, "price", "product": CNC|MIS}. Only from this PC."""
    from . import manual
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    b = request.get_json(silent=True) or {}
    try:
        out = manual.buy_stock(str(b.get("symbol", "")), b.get("qty"), str(b.get("order_type", "LIMIT")),
                               b.get("price"), str(b.get("product", "CNC")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak refused: {exc}"}), 502
    from . import portfolio
    portfolio._cache.update(at=None, data=None)
    return jsonify(out)


@app.post("/api/holdings/sell")
def api_holdings_sell():
    """Sell demat shares: {"symbol", "qty", "order_type": LIMIT|MARKET, "price"}. Real order, so only from this PC."""
    from . import manual
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    b = request.get_json(silent=True) or {}
    try:
        out = manual.sell_holding(str(b.get("symbol", "")), b.get("qty"), str(b.get("order_type", "LIMIT")),
                                  b.get("price"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak refused: {exc}"}), 502
    from . import portfolio
    portfolio._cache.update(at=None, data=None)   # next read shows the order straight away
    return jsonify(out)


@app.post("/api/positions/close")
def api_positions_close():
    """Close an open position at the market: {"tsym", "product"}. Real order, so only from this PC."""
    from . import manual
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    b = request.get_json(silent=True) or {}
    try:
        out = manual.close_position(str(b.get("tsym", "")), str(b.get("product", "")))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak refused: {exc}"}), 502
    from . import portfolio
    portfolio._cache.update(at=None, data=None)   # next read shows the order straight away
    return jsonify(out)


@app.post("/api/options/order")
def api_option_order():
    """Your own option order (not Auto-trade). Real money, so only from this PC."""
    from . import manual
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    b = request.get_json(silent=True) or {}
    try:
        return jsonify(manual.place_option_order(str(b.get("tsym", "")), str(b.get("side", "")), b.get("lots"),
                                                 str(b.get("order_type", "LIMIT")), b.get("price"),
                                                 str(b.get("product", "NRML"))))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak refused: {exc}"}), 502


@app.post("/api/orders/cancel")
def api_order_cancel():
    """Cancel one of your pending orders: {"order_id"}. Real order, so only from this PC."""
    from . import orderbook, portfolio
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    oid = (request.get_json(silent=True) or {}).get("order_id")
    if not isinstance(oid, str) or not oid:
        return jsonify({"error": "Send {\"order_id\": \"...\"}"}), 400
    try:
        out = orderbook.cancel(oid)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak refused: {exc}"}), 502
    portfolio._cache.update(at=None, data=None)
    return jsonify(out)


@app.post("/api/orders/modify")
def api_order_modify():
    """Change a pending order: {"order_id", "price"?, "trigger"?, "qty"?}. Only from this PC."""
    from . import orderbook, portfolio
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    b = request.get_json(silent=True) or {}
    if not isinstance(b.get("order_id"), str) or not b["order_id"]:
        return jsonify({"error": "Send {\"order_id\": \"...\", ...}"}), 400
    try:
        out = orderbook.modify(b["order_id"], b.get("price"), b.get("trigger"), b.get("qty"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak refused: {exc}"}), 502
    portfolio._cache.update(at=None, data=None)
    return jsonify(out)


@app.post("/api/trades/close")
def api_trade_close():
    """Close an app trade by hand (sell at market / cancel the entry). Real orders, so only from this PC."""
    from . import orders
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    key = (request.get_json(silent=True) or {}).get("key")
    if not isinstance(key, str) or not key:
        return jsonify({"error": "Send {\"key\": \"<trade key>\"}"}), 400
    try:
        return jsonify(orders.close_trade(key))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak refused: {exc}"}), 502


@app.post("/api/option-trades/close")
def api_option_trade_close():
    """Close an options Auto-trade trade by hand (sell at the bid / cancel the wait). Real orders, so only from
    this PC."""
    from . import options_auto
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    key = (request.get_json(silent=True) or {}).get("key")
    if not isinstance(key, str) or not key:
        return jsonify({"error": "Send {\"key\": \"<trade key>\"}"}), 400
    try:
        return jsonify(options_auto.close_trade(key))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 409
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak refused: {exc}"}), 502


@app.get("/api/trading")
def api_trading():
    return jsonify(broker.trading_status())


@app.post("/api/trading")
def api_trading_set():
    """Switch automatic trading. Switching ON needs this PC or a logged-in phone over Tailscale (access.py);
    switching OFF works from anywhere, so trading can always be stopped."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body.get("enabled"), bool):
        return jsonify({"error": "Send {\"enabled\": true | false}"}), 400
    enabled = body["enabled"]
    if enabled and not access.can_trade():
        return jsonify({"error": "Automatic trading can only be switched on from this PC or your phone app "
                                 "over Tailscale (logged in)"}), 403
    try:
        st = broker.set_trading(enabled, by=f"dashboard ({access.who()})")
    except broker.BrokerError as exc:
        return jsonify({"error": f"Kotak login failed, trading stays off: {exc}"}), 502
    try:
        lim = st["limits"]
        telegram_bot.send_message(
            f"🟢 <b>Automatic trading ON</b> · {st['account']}\nmax "
            f"₹{lim['max_order_value']:,.0f} per order" if enabled else "🔴 <b>Automatic trading OFF</b>")
    except Exception:
        log.exception("Telegram notice for the trading switch failed")
    return jsonify(st)


@app.post("/api/refresh")
def api_refresh():
    mkt = _market()
    refresh()
    return jsonify(_signals_payload(mkt))


TIMEFRAMES = [("5m", "5 min"), ("15m", "15 min"), ("30m", "30 min"), ("1h", "1 hour"), ("1d", "Daily")]
ALL_TF = "all"  # scans every timeframe above, one after another


INDEX_NAME = {"IN": "NIFTY 50", "US": "S&P 500"}
INDEX_TTL_SEC = 300  # index quote cache, per market
KOTAK_INDEX = {"IN": "Nifty 50"}   # Kotak quote name of the market's index (live), where available
KOTAK_INDEX_TTL_SEC = 30
LIVE_INDEX_TTL_SEC = 0.2   # while the live stream is connected
_index_cache: dict[str, tuple[float, dict]] = {}


@app.get("/api/index")
def api_index():
    """Latest level and daily change of the market's index. India: Kotak's live index quote (the same source as
    the option chain), cached KOTAK_INDEX_TTL_SEC; otherwise, or if Kotak fails, Yahoo daily closes (delayed,
    and sometimes a day behind), cached INDEX_TTL_SEC."""
    from . import livefeed
    mkt = _market()
    hit = _index_cache.get(mkt.code)
    if hit and time.monotonic() - hit[0] < hit[1].get("ttl", INDEX_TTL_SEC):
        return jsonify(hit[1])
    quote = {"market": mkt.code, "name": INDEX_NAME.get(mkt.code, mkt.index_symbol),
             "last": None, "change": None, "change_pct": None, "as_of": None, "source": "Yahoo Finance, delayed"}
    kq = kotak.index_quote(KOTAK_INDEX[mkt.code]) if mkt.code in KOTAK_INDEX else None
    if kq and kq["prev_close"]:
        last, prev = kq["ltp"], kq["prev_close"]
        quote.update(last=round(last, 2), change=round(last - prev, 2), change_pct=round((last - prev) / prev * 100, 2),
                     as_of=kq["time"].isoformat(sep=" ", timespec="minutes") if kq["time"] else None,
                     source="Kotak Neo, live", ttl=LIVE_INDEX_TTL_SEC if livefeed.connected() else KOTAK_INDEX_TTL_SEC)
        _index_cache[mkt.code] = (time.monotonic(), quote)
        return jsonify(quote)
    try:
        close = fetch_index("1d", mkt.code, period="6mo")
    except Exception:
        log.exception("Index quote failed")
        close = None
    if close is not None and len(close) >= 2:
        last, prev = float(close.iloc[-1]), float(close.iloc[-2])
        quote.update(last=round(last, 2), change=round(last - prev, 2),
                     change_pct=round((last - prev) / prev * 100, 2), as_of=str(close.index[-1].date()))
        _index_cache[mkt.code] = (time.monotonic(), quote)  # failures aren't cached, so they retry
    return jsonify(quote)


@app.get("/api/meta")
def api_meta():
    return jsonify({
        "strategies": describe(),
        "filters": [{"name": k, "description": v} for k, v in FILTERS.items()],
        "timeframes": [{"value": v, "label": l} for v, l in TIMEFRAMES] + [{"value": ALL_TF, "label": "All"}],
        "markets": [{"code": m.code, "name": m.name, "tz": m.tz,
                     "session": [t.strftime("%H:%M") for t in m.session],
                     "scan_window": [t.strftime("%H:%M") for t in m.scan_window]} for m in markets.MARKETS.values()],
        "defaults": {"timeframe": config.TIMEFRAME, "min_score": config.MIN_SCORE},
        "scan": {"interval_min": config.SCAN_INTERVAL_MIN, "timeframes": config.SCAN_TIMEFRAMES},
    })


@app.get("/api/scan/settings")
def api_scan_settings():
    """The continuous scanner's settings for ?market= (the scan panel): strategies, min_score, filters, send."""
    from . import settings
    return jsonify(settings.scan_settings(_market().code))


@app.post("/api/scan/settings")
def api_scan_settings_save():
    """Save the scan panel for ?market=; the scanner (run.bat) uses it from its next loop. Changing what gets
    scanned changes what Auto-trade buys, so it needs the same access as placing an order."""
    from . import settings
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    return jsonify(settings.save_scan_settings(_market().code, request.get_json(silent=True) or {}))


@app.get("/api/scan/status")
def api_scan_status():
    """The continuous scanner, per market: running (and on which timeframe), last result, next scan, and whether
    the scanner process is alive at all."""
    from . import health, runner
    from .scanner import get_status
    return jsonify({"alive": health.scanner_running(), "last_seen_min": health.scanner_age_min(),
                    "runner": runner.status(),
                    "markets": {code: {**get_status(code), "in_window": m.in_scan_window(), "on": on[code]}
                                for code, m in markets.MARKETS.items() for on in [runner.markets_on()]}})


@app.post("/api/scanner")
def api_scanner():
    """The Scanner switch. With a market (?market= or "market" in the body), {"on": true / false} turns that
    market's scanning on or off, starting the continuous scanner (main.py) if needed and stopping it when no market
    is left on. Without one, it starts or stops the whole scanner, also one started with run.bat. It places and
    watches Auto-trade orders, so it needs the same access as an order."""
    from . import runner
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    body = request.get_json(silent=True) or {}
    on = body.get("on")
    if not isinstance(on, bool):
        raise ValueError('Send {"on": true} or {"on": false}')
    if request.args.get("market") or body.get("market"):
        mkt = _market()
        log.info("%s scanning switched %s from %s", mkt.name, "on" if on else "off", access.who())
        return jsonify({**runner.switch_market(mkt.code, on), "markets": runner.markets_on()})
    log.info("Scanner switched %s from %s", "on" if on else "off", access.who())
    return jsonify(runner.start() if on else runner.stop())


def _scan_timeframes(timeframes: list[str], **kw) -> dict:
    """Run scan_and_alert on each timeframe and combine the counts.
    With more than one timeframe, symbols are labelled with their timeframe."""
    total = {"found": 0, "new": 0, "sent": 0, "symbols": []}
    for tf in timeframes:
        if tf != timeframes[0]:
            check_stop(kw["market"])   # stop between timeframes too, not only inside one
        r = scan_and_alert(timeframe=tf, **kw)
        for k in ("found", "new", "sent"):
            total[k] += r[k]
        total["symbols"] += [f"{s} ({tf})" for s in r["symbols"]] if len(timeframes) > 1 else r["symbols"]
    return total


def _auto_trade(new_keys: list[str]) -> dict | None:
    """Hand an India dashboard scan's new signals to Auto-trade, like the scanner does. Only while the scanner
    is running: it places the stop-loss after a fill and watches the exits, so without it a filled entry would
    sit unprotected. None when Auto-trade is off or there is nothing new."""
    from . import health, options_auto, orders
    from .universe import is_index
    if not new_keys or not broker.trading_enabled():
        return None
    if not health.scanner_running():
        log.warning("Dashboard scan: %d new signals not traded, the scanner isn't running", len(new_keys))
        return {"placed": [], "skipped": [], "blocked": "the scanner (run.bat) isn't running, and it is what "
                "places the stop-loss after a fill. Start run.bat, then scan again"}
    sigs = {s["key"]: s for s in state.all_signals("IN")}
    made = orders.trade_new([k for k in new_keys if k in sigs and sigs[k]["side"] == "BUY"
                             and not is_index(sigs[k]["symbol"])], manage=False)   # the scanner's watchers take over
    opts = options_auto.trade_new(new_keys)   # waits for the entry; the scanner's option watcher buys
    return {"placed": [f"{t['symbol']} × {t['qty']}" for t in made if t and t["entry_order"]]
                      + [f"{t['underlying']} {t['opt']} (waiting for entry)" for t in opts if t["state"] == "WAIT"],
            "skipped": [f"{t['symbol']}: {t['note']}" for t in made if t and not t["entry_order"]]
                       + [f"{t['underlying']} options: {t['note']}" for t in opts if t["state"] != "WAIT"],
            "blocked": None}


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
        if stop_requested(mkt.code):
            return jsonify({"error": f"The stopped {mkt.name} scan is still finishing a download; "
                                     "try again in a few seconds"}), 409
        return jsonify({"error": f"A {mkt.name} scan is already running"}), 409
    clear_stop(mkt.code)
    # The scan runs in its own thread so Stop answers at once: a Yahoo batch download can't be interrupted
    # and takes several seconds. The thread holds the lock until it really ends, so scans never overlap.
    out, done = {}, threading.Event()

    def work():
        try:
            before = {s["key"] for s in state.all_signals(mkt.code)}
            out["result"] = _scan_timeframes([v for v, _ in TIMEFRAMES] if timeframe == ALL_TF else [timeframe],
                                             send=bool(body.get("send", False)), strategies=strategies,
                                             min_score=min_score, market=mkt.code, filters=filters)
            if mkt.code == "IN":
                # Auto-trade: stocks take the BUYs, options both sides (intraday ones when switched on in Settings)
                at = _auto_trade([s["key"] for s in state.all_signals("IN") if s["key"] not in before])
                if at:
                    out["result"]["auto_trade"] = at
        except ScanStopped:
            log.info("%s scan stopped from the dashboard", mkt.name)
        except Exception as exc:
            out["error"] = exc
            if not isinstance(exc, ValueError):
                log.exception("Scan failed")
        finally:
            lock.release()
            done.set()

    threading.Thread(target=work, name=f"scan-{mkt.code}", daemon=True).start()
    while not done.wait(0.2):
        if stop_requested(mkt.code):
            done.wait(1)   # most steps end within a second; don't wait out a running download
            break
    if "result" not in out:
        exc = out.get("error")
        if exc is None:
            return jsonify({**_signals_payload(mkt), "scan": {"stopped": True}})
        return jsonify({"error": str(exc)}), 400 if isinstance(exc, ValueError) else 500
    result = out["result"]
    result.update(timeframe=timeframe, strategies=strategies, min_score=min_score, filters=filters)
    return jsonify({**_signals_payload(mkt), "scan": result})


@app.post("/api/signals/delete")
def api_signals_delete():
    """Delete signals of ?market= by key ({"keys": [...]}). A signal whose Auto-trade order is still in
    progress is kept, so the app never loses track of a real order; those come back in "kept"."""
    from . import orders
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    mkt = _market()
    keys = (request.get_json(silent=True) or {}).get("keys")
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        raise ValueError('Send {"keys": ["<signal key>", ...]}')
    from . import options_auto
    busy = {t["key"] for t in orders.all_trades() if t["state"] in orders.ACTIVE_STATES} |            {t["key"] for t in options_auto.all_trades() if t["state"] in options_auto.ACTIVE_STATES}
    kept = [k for k in keys if k in busy]
    n = state.delete_signals([k for k in keys if k not in busy], mkt.code)
    log.info("Deleted %d %s signals from the dashboard%s", n, mkt.name, f" ({len(kept)} kept: Auto-trade)" if kept else "")
    return jsonify({**_signals_payload(mkt), "deleted": n, "kept": kept})


@app.post("/api/scan/stop")
def api_scan_stop():
    """Ask the running scan of ?market= to stop; it ends at its next step (signals already saved stay)."""
    mkt = _market()
    if not _scan_locks[mkt.code].locked():
        return jsonify({"error": f"No {mkt.name} scan is running"}), 409
    request_stop(mkt.code)
    return jsonify({"ok": True})


# ---- health, watchlist and price alerts, order history, signal charts ------------------------------

MONITOR_SEC = 15          # price alerts are checked this often; health every HEALTH_EVERY loops
LIVE_ALERT_SEC = 1        # price alerts while the live stream is connected (prices come from memory)
HEALTH_EVERY = 4
_health: dict = {"problems": None, "at": None}


def _monitor() -> None:
    """Dashboard background work: price alerts, health checks (+ Telegram), the daily backup, and saving the
    order book for the history (every SNAPSHOT_SEC in market hours, once more after the close)."""
    from . import backup, health, orderbook, settings, summary, watchlist
    from . import livefeed
    n, snap_at, snapped_after_close = 0, 0.0, None
    while True:
        try:
            watchlist.check()
        except Exception:
            log.exception("Price alert check failed")
        # with the stream live, alerts are checked every LIVE_ALERT_SEC until the next full round
        loop_end = time.monotonic() + MONITOR_SEC
        if n % HEALTH_EVERY == 0:
            try:
                _health.update(problems=health.watch_once(_last_refresh["at"]), at=time.strftime("%Y-%m-%d %H:%M:%S"))
            except Exception:
                log.exception("Health check failed")
            for job in (settings.apply, backup.daily, summary.daily):
                try:
                    job()
                except Exception:
                    log.exception("%s failed", job.__module__ + "." + job.__name__)
        india = markets.get("IN")
        if not broker.missing_credentials():
            try:
                if india.is_open() and time.monotonic() - snap_at > orderbook.SNAPSHOT_SEC:
                    orderbook.snapshot()
                    snap_at = time.monotonic()
                elif india.is_after_close() and snapped_after_close != india.now().date():
                    orderbook.snapshot()
                    snapped_after_close = india.now().date()
            except Exception:
                log.exception("Order history snapshot failed")
        n += 1
        while time.monotonic() < loop_end:
            if livefeed.connected():
                time.sleep(LIVE_ALERT_SEC)
                try:
                    watchlist.check()
                except Exception:
                    log.exception("Price alert check failed")
            else:
                time.sleep(min(LIVE_ALERT_SEC, max(loop_end - time.monotonic(), 0)))


@app.get("/api/feed")
def api_feed():
    from . import livefeed
    return jsonify(livefeed.status())


PUSH_MIN_SEC = 0.25    # a push stream sends at most this often (updates in between are combined)
PUSH_PING_SEC = 15     # keep-alive comment when nothing changes, so a closed page is noticed
PUSH_TOUCH_SEC = 60    # re-mark the page's instruments as wanted, so the feed keeps them subscribed
PUSH_MAX_KEYS = 400


def _arg_list(name: str) -> list[str]:
    return [x.strip() for x in request.args.get(name, "").split(",") if x.strip()]


def stream_keys() -> dict[str, set[str]]:
    """Feed keys ("segment|token") for what the page shows -> the parts of the page they belong to:
    "v" (the screen) from s = NSE stocks, i = index names (Kotak's, e.g. "Nifty 50"), o = option contracts,
    u = option underlyings, m=1 = everything the market overview uses; "h" (the index in the top bar) from h=1;
    "t" (an open order ticket) from t = its stock."""
    from . import kotak
    tok = kotak.tokens()
    out: dict[str, set[str]] = {}
    for k in _view_keys(tok):
        out.setdefault(k, set()).add("v")
    if request.args.get("h") == "1":
        out.setdefault(f"nse_cm|{KOTAK_INDEX['IN']}", set()).add("h")
    for s in _arg_list("t"):
        if s in tok:
            out.setdefault(f"nse_cm|{tok[s]}", set()).add("t")
    return dict(list(out.items())[:PUSH_MAX_KEYS])


def _view_keys(tok: dict) -> list[str]:
    from . import fno, market
    keys = []
    stocks, idx = _arg_list("s"), _arg_list("i")
    for u in _arg_list("u"):
        if u in fno.INDEX_QUOTE:
            idx.append(fno.INDEX_QUOTE[u])
        else:
            stocks.append(u)
    if request.args.get("m") == "1":
        stocks += market._stocks()
        idx += market.MAIN + market.SECTORS
    keys += [f"nse_cm|{tok[s]}" for s in stocks if s in tok]
    keys += [f"nse_cm|{n}" for n in idx]
    for t in _arg_list("o"):
        c = fno.contract(t)
        if c is not None:
            keys.append(f"nse_fo|{c['token']}")
    return list(dict.fromkeys(keys))


def push_events(keys: dict[str, set[str]], feed=None):
    """Server-sent events: "data: ["v", "h"]" (the parts of the page whose prices changed) as soon as any of
    `keys` has a new price (at most every PUSH_MIN_SEC), a ping when quiet, and "event: down" if the live stream
    drops, so the page goes back to polling."""
    from . import livefeed
    feed = feed or livefeed
    yield "retry: 5000\n\n"
    seq, sent, beat, touched = feed.seq(), 0.0, time.monotonic(), float("-inf")
    while feed.connected():
        now = time.monotonic()
        if now - touched > PUSH_TOUCH_SEC:
            feed.touch(keys)
            touched = now
        feed.wait(PUSH_PING_SEC)
        time.sleep(max(0.0, PUSH_MIN_SEC - (time.monotonic() - sent)))
        seq, changed = feed.changed_since(seq, keys)
        if changed:
            sent = beat = time.monotonic()
            yield f"data: {json.dumps(sorted(set().union(*(keys[k] for k in changed))))}\n\n"
        elif time.monotonic() - beat > PUSH_PING_SEC:
            beat = time.monotonic()
            yield ": ping\n\n"
    yield "event: down\ndata: 0\n\n"


@app.get("/api/stream")
def api_stream():
    """Push stream for the page on screen: tells it the moment one of its prices changes (it then reloads)."""
    from . import livefeed
    if not livefeed.connected():
        return jsonify({"error": "The live price stream isn't connected"}), 503
    try:
        keys = stream_keys()
    except Exception as exc:
        log.exception("Push stream keys failed")
        return jsonify({"error": str(exc)}), 502
    if not keys:
        return jsonify({"error": "Nothing to stream"}), 400
    return Response(push_events(keys), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/health")
def api_health():
    """Current problems (see health.py), as last checked by the monitor; checked now on the first call."""
    from . import health
    if _health["problems"] is None:
        _health.update(problems=health.problems(_last_refresh["at"]), at=time.strftime("%Y-%m-%d %H:%M:%S"))
    from . import livefeed
    return jsonify({**_health, "feed": livefeed.status()})


@app.get("/api/watchlist")
def api_watchlist():
    from . import watchlist
    try:
        return jsonify(watchlist.view())
    except Exception as exc:
        log.exception("Watchlist failed")
        return jsonify({"error": f"Watchlist unavailable: {exc}"}), 502


@app.post("/api/watchlist")
def api_watchlist_add():
    """{"symbol"}: add an NSE stock to the watchlist."""
    from . import watchlist
    return jsonify({"symbol": watchlist.add((request.get_json(silent=True) or {}).get("symbol", ""))})


@app.post("/api/watchlist/remove")
def api_watchlist_remove():
    from . import watchlist
    watchlist.remove((request.get_json(silent=True) or {}).get("symbol", ""))
    return jsonify({"ok": True})


@app.post("/api/alerts")
def api_alert_add():
    """{"symbol", "op": above | below, "price", "note"?}: a price alert, posted to Telegram once when crossed."""
    from . import watchlist
    b = request.get_json(silent=True) or {}
    return jsonify(watchlist.add_alert(b.get("symbol", ""), b.get("op", ""), b.get("price"), b.get("note", "")))


@app.post("/api/alerts/delete")
def api_alert_delete():
    from . import watchlist
    aid = (request.get_json(silent=True) or {}).get("id")
    if not isinstance(aid, int) or isinstance(aid, bool):
        raise ValueError('Send {"id": <alert id>}')
    watchlist.delete_alert(aid)
    return jsonify({"ok": True})


@app.get("/api/orders/history")
def api_order_history():
    """Orders saved from Kotak's book over the last ?days= (default 30, up to HISTORY_DAYS)."""
    from . import orderbook
    return jsonify({"orders": orderbook.history(request.args.get("days", 30, type=int)),
                    "keep_days": orderbook.HISTORY_DAYS})


CHART_BARS = 120
CHART_TTL_SEC = 600
_charts: dict = {}


@app.get("/api/chart")
def api_chart():
    """Recent candles for a signal's detail view: ?symbol=&market=&tf= (Yahoo, cached CHART_TTL_SEC; India daily
    bars get today's live Kotak bar)."""
    from .data import PERIOD_FOR, fetch_ohlcv
    mkt = _market()
    sym = request.args.get("symbol", "").strip().upper()
    tf = request.args.get("tf", "1d")
    if not sym or tf not in dict(TIMEFRAMES):
        raise ValueError("Send ?symbol= and a timeframe")
    ck = (mkt.code, sym, tf)
    hit = _charts.get(ck)
    if hit and time.monotonic() - hit[0] < CHART_TTL_SEC:
        return jsonify(hit[1])
    period = {"1d": "1y", "1h": "60d"}.get(tf, PERIOD_FOR.get(tf))
    df = fetch_ohlcv([sym], tf, period=period, market=mkt.code).get(sym)
    if df is None or df.empty:
        return jsonify({"error": f"No price history for {sym}"}), 502
    if mkt.code == "IN" and tf == "1d" and kotak.enabled():
        q = kotak.quotes([sym]).get(sym)
        if q:
            df = kotak.apply_live_bar(df, q, tf)
    df = df.tail(CHART_BARS)
    out = {"symbol": sym, "market": mkt.code, "tf": tf,
           "bars": [{"t": ts.isoformat(), "o": round(float(r.Open), 2), "h": round(float(r.High), 2),
                     "l": round(float(r.Low), 2), "c": round(float(r.Close), 2)} for ts, r in df.iterrows()]}
    _charts[ck] = (time.monotonic(), out)
    return jsonify(out)


# ---- settings, risk, scorecard + backtest, events, market overview, tax report, phone notifications -------

def _trade_guard():
    """Changes that affect trading need the same access as placing an order."""
    if not access.can_trade():
        return jsonify({"error": access.TRADE_REFUSED}), 403
    return None


@app.get("/api/settings")
def api_settings():
    from . import settings
    return jsonify({"settings": settings.current(), "can_change": access.can_trade()})


@app.post("/api/settings")
def api_settings_set():
    """{"changes": {NAME: value | null}}: null goes back to the .env value."""
    from . import settings
    if (refused := _trade_guard()):
        return refused
    changes = (request.get_json(silent=True) or {}).get("changes")
    if not isinstance(changes, dict) or not changes:
        raise ValueError('Send {"changes": {...}}')
    return jsonify({"settings": settings.update(changes, by=f"dashboard ({access.who()})"), "can_change": True})


@app.get("/api/risk")
def api_risk():
    from . import risk
    return jsonify(risk.report())


@app.get("/api/scorecard")
def api_scorecard():
    return jsonify(performance.scorecard(_market().code))


@app.post("/api/scorecard")
def api_scorecard_set():
    """{"market", "name", "off": bool}: leave a strategy out of (or back into) the continuous scanner."""
    if (refused := _trade_guard()):
        return refused
    b = request.get_json(silent=True) or {}
    mkt = _market()
    off = performance.set_strategy_off(mkt.code, str(b.get("name", "")), bool(b.get("off")))
    telegram_bot.send_message(f"⚙️ Scanner ({mkt.name}): <b>{b.get('name')}</b> switched {'off' if b.get('off') else 'on'} "
                              f"from the dashboard ({access.who()})")
    return jsonify(performance.scorecard(mkt.code) | {"off": off})


@app.get("/api/backtest")
def api_backtest():
    from . import backtest_job
    return jsonify(backtest_job.status(_market().code))


@app.post("/api/backtest")
def api_backtest_start():
    """{"market", "strategies", "min_score", "filters", "period": 2y | 5y | 10y}: runs in the background."""
    from . import backtest_job
    b = request.get_json(silent=True) or {}
    try:
        return jsonify(backtest_job.start(_market().code, b.get("strategies"), b.get("min_score"), b.get("filters"),
                                          b.get("period", "5y")))
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409


@app.get("/api/events")
def api_events():
    from . import events
    sym = request.args.get("symbol", "").strip().upper()
    if not sym:
        raise ValueError("Send ?symbol=")
    return jsonify(events.describe(sym, _market().code))


@app.get("/api/market")
def api_market():
    from . import market
    try:
        return jsonify(market.overview())
    except Exception as exc:
        log.exception("Market overview failed")
        return jsonify({"error": f"Market overview unavailable: {exc}"}), 502


@app.get("/api/tax")
def api_tax():
    from . import tax
    return jsonify(tax.report(request.args.get("fy", type=int)))


@app.get("/api/tax.xlsx")
def api_tax_xlsx():
    from . import tax
    fy = request.args.get("fy", type=int) or tax.current_fy()
    return app.response_class(tax.xlsx(fy), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                              headers={"Content-Disposition": f'attachment; filename="algotrade_pnl_FY{fy}-{str(fy + 1)[-2:]}.xlsx"'})


@app.post("/api/summary/send")
def api_summary_send():
    """Send the daily summary to Telegram now (it also goes out by itself after the close)."""
    from . import summary
    return jsonify({"sent": telegram_bot.send_message(summary.build())})


@app.get("/api/push/key")
def api_push_key():
    from . import push
    return jsonify({"key": push.public_key(), "devices": len(push.devices())})


@app.post("/api/push/subscribe")
def api_push_subscribe():
    from . import push
    b = request.get_json(silent=True) or {}
    push.subscribe(b.get("subscription"), str(b.get("device") or access.who()))
    return jsonify({"ok": True, "devices": len(push.devices())})


@app.post("/api/push/unsubscribe")
def api_push_unsubscribe():
    from . import push
    push.unsubscribe(str((request.get_json(silent=True) or {}).get("endpoint", "")))
    return jsonify({"ok": True})


@app.post("/api/push/test")
def api_push_test():
    from . import push
    return jsonify({"sent": push.send("AlgoTrade", "Test notification: phone notifications work 👍")})


@app.get("/api/journal")
def api_journal():
    from . import journal
    key = request.args.get("key", "")
    return jsonify({**journal.get(key), "presets": journal.TAGS})


@app.post("/api/journal")
def api_journal_save():
    """{"key", "note"?, "tags"?}: your journal entry for a signal."""
    from . import journal
    b = request.get_json(silent=True) or {}
    return jsonify(journal.save(b.get("key"), b.get("note"), b.get("tags")))


@app.post("/api/journal/image")
def api_journal_image():
    """multipart: key + image (JPEG / PNG / WebP) — a chart screenshot for the journal entry."""
    from . import journal
    f = request.files.get("image")
    if f is None:
        raise ValueError("Attach an image")
    return jsonify(journal.save_image(request.form.get("key", ""), f.read(journal.MAX_IMAGE + 1), f.mimetype))


@app.post("/api/journal/image/delete")
def api_journal_image_delete():
    from . import journal
    return jsonify(journal.delete_image((request.get_json(silent=True) or {}).get("key", "")))


@app.get("/journal-img/<name>")
def journal_image(name):
    from . import journal
    return send_from_directory(journal.image_dir(), name)


def serve(host: str = "127.0.0.1", port: int = 8050) -> None:
    from . import settings, tgcommands
    settings.apply()
    threading.Thread(target=_background, daemon=True).start()
    threading.Thread(target=_monitor, name="monitor", daemon=True).start()
    threading.Thread(target=tgcommands.run_forever, name="telegram-commands", daemon=True).start()
    from . import livefeed
    livefeed.start()
    from . import orderbook
    orderbook.start_watch()
    log.info("Dashboard running at http://%s:%d", host, port)
    # Flask logs every request; the page polls constantly, so keep only warnings and errors
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host=host, port=port, debug=False, use_reloader=False)
