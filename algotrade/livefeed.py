"""Live prices streamed from Kotak Neo's market feed (WebSocket), shared with every quote lookup (read-only).

One background thread in the dashboard keeps a WebSocket open (it needs the logged-in session from broker.py)
and subscribes on demand: whatever kotak.quotes / index_quote(s) and fno's option quotes are asked for is
subscribed, and the stream keeps it current, so those lookups are answered from memory instead of a REST call.
Instruments nobody has asked about for IDLE_SEC are unsubscribed again (MAX_TOKENS at most).

Lookups only use the stream while it is connected and the instrument has had its first update (Kotak sends a
snapshot on subscribe); anything else falls back to the REST quote as before, so nothing depends on it. Nothing
here places or changes orders: the feed only carries prices."""
import asyncio
import logging
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import config

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
IDLE_SEC = 600          # unsubscribe what hasn't been asked for in this long
MAX_TOKENS = 1200       # subscriptions at most (Kotak allows 3000); the OI history alone keeps ~300
SYNC_SEC = 0.5          # subscribe newly requested instruments this often
RETRY_SEC = (5, 15, 30, 60, 120)   # waits between reconnects after failures
STALL_SEC = 60          # no message this long while the NSE is open and an index is subscribed (indices tick
                        # every second) = a frozen socket (it stays "open" but sends nothing): reconnect

_ticks: dict[str, dict] = {}        # "segment|token" -> quote in the REST shape (see _scrip / _index)
_listeners: list = []               # fn(key, quote) called on every stored update (e.g. the MCX bar recorder)
_want: dict[str, float] = {}        # "segment|token" -> monotonic time it was last asked for
_subscribed: set[str] = set()
_lock = threading.Lock()
_seq = 0                            # counts stored updates; _changed[key] = the count at its last update
_changed: dict[str, int] = {}
_tick = threading.Condition()       # notified on every update (dashboard push streams wait on it)
_thread: threading.Thread | None = None
_state = {"connected": False, "since": None, "error": None, "messages": 0, "last_at": None, "reconnects": 0,
          "down_since": None, "last_mono": None}   # down_since: monotonic time the stream went down (or the thread started);
                                  # last_mono: monotonic time of the last message


def key(segment: str, token: str) -> str:
    return f"{segment}|{token}"


def is_index(token: str) -> bool:
    """Indices are subscribed by name ("Nifty 50"), instruments by numeric token."""
    return not str(token).isdigit()


def running() -> bool:
    return _thread is not None and _thread.is_alive()


def connected() -> bool:
    return running() and _state["connected"]


def lookup(segment: str, tokens: list[str]) -> dict[str, dict]:
    """{token: quote} for the tokens the stream already has; the rest are queued for subscribing.
    Quotes have the same shape as Kotak's REST quotes, so the callers parse them the same way."""
    if not running():
        return {}
    now, out = time.monotonic(), {}
    with _lock:
        for t in tokens:
            k = key(segment, str(t))
            _want[k] = now
            if _state["connected"] and k in _subscribed and k in _ticks:
                out[str(t)] = _ticks[k]
    return out


def touch(keys) -> None:
    """Keep these "segment|token" keys subscribed (a page is showing them) without reading them."""
    if running():
        now = time.monotonic()
        with _lock:
            for k in keys:
                _want[k] = now


def seq() -> int:
    return _seq


def changed_since(since: int, keys) -> tuple[int, list[str]]:
    """(the current count, those of `keys` updated after the count `since`)."""
    now = _seq
    return now, [k for k in keys if _changed.get(k, 0) > since]


def wait(timeout: float) -> None:
    """Block until the next update (or the timeout)."""
    with _tick:
        _tick.wait(timeout)


def down_minutes() -> float | None:
    """How long the stream has been down while it should be up; None if it's up, off or not started here."""
    if not running() or not config.LIVE_FEED or _state["connected"] or _state["down_since"] is None:
        return None
    return (time.monotonic() - _state["down_since"]) / 60


def status() -> dict:
    return {"enabled": config.LIVE_FEED, "running": running(), "connected": connected(),
            "subscribed": len(_subscribed), "messages": _state["messages"], "since": _state["since"],
            "last_at": _state["last_at"], "error": _state["error"], "reconnects": _state["reconnects"]}


# ---- messages -> REST-shaped quotes ---------------------------------------------------------------

def _scrip(m) -> dict:
    """SFeedScrip (touch line: price, OHLC with the previous close, volume, OI, best bid / ask)."""
    def lvl(rows):
        return [{"price": r.price, "quantity": r.quantity, "orders": r.orders} for r in rows]
    return {"exchange_token": m.instrument_token, "ltp": m.last_traded_price,
            "ohlc": {"open": m.open_price, "high": m.high_price, "low": m.low_price, "close": m.close_price},
            "last_volume": m.volume_traded_today, "open_int": m.open_interest,
            "change": m.net_change, "per_change": m.net_change_percent,
            "lstup_time": m.last_trade_time or m.last_update_time or None,
            "depth": {"buy": lvl(m.buy), "sell": lvl(m.sell)}, "source": "stream"}


def _index(m) -> dict:
    return {"exchange_token": m.name, "ltp": m.last_traded_price,
            "ohlc": {"open": m.open_price, "high": m.high_price, "low": m.low_price, "close": m.close_price},
            "change": m.change, "per_change": m.net_change_percent,
            "lstup_time": m.last_trade_time or None, "source": "stream"}


def on_message(m) -> None:
    """Store a decoded feed message (other message types, e.g. closing-auction updates, are ignored)."""
    kind = getattr(m, "type", None)
    if kind == "scrip":
        q, k = _scrip(m), key(m.exchange_segment, m.instrument_token)
    elif kind == "index":
        q, k = _index(m), key(m.exchange_segment, m.name)
    else:
        return
    if not q["ltp"]:
        return
    global _seq
    _ticks[k] = q
    _seq += 1
    _changed[k] = _seq
    with _tick:
        _tick.notify_all()
    _state["messages"] += 1
    _state["last_mono"] = time.monotonic()
    _state["last_at"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    for fn in list(_listeners):
        try:
            fn(k, q)
        except Exception:
            log.exception("Live feed listener failed")


def add_listener(fn) -> None:
    """Call fn(key, quote) on every price update the stream stores (once per function)."""
    if fn not in _listeners:
        _listeners.append(fn)


def stalled(now: float | None = None, market_open: bool | None = None) -> bool:
    """True when the connected stream has gone silent for STALL_SEC while the NSE is open with something
    subscribed. Kotak's socket can freeze without closing, so this is the only sign it's dead. Only counted
    with an index subscribed: a quiet stock or far option can legitimately go a minute without a trade."""
    if not any(is_index(k.split("|", 1)[1]) for k in _subscribed):
        return False
    if market_open is None:
        from . import markets
        market_open = markets.get("IN").is_open()
    if not market_open:
        return False
    now = time.monotonic() if now is None else now
    return now - (_state["last_mono"] or 0) > STALL_SEC


# ---- the connection ---------------------------------------------------------------------------------

def _desired() -> set[str]:
    now = time.monotonic()
    with _lock:
        for k in [k for k, at in _want.items() if now - at > IDLE_SEC]:
            del _want[k]
        recent = sorted(_want, key=_want.get, reverse=True)[:MAX_TOKENS]
    return set(recent)


async def _sync(ws) -> None:
    """Subscribe what's newly wanted and unsubscribe what has gone idle."""
    from neo_api_client.websocket.feed import WsToken
    want = _desired()
    add, drop = want - _subscribed, _subscribed - want

    def toks(keys, index):
        return [WsToken(*k.split("|", 1)) for k in sorted(keys) if is_index(k.split("|", 1)[1]) == index]
    for index, sub, unsub in ((False, ws.subscribe_scrips, ws.unsubscribe_scrips),
                              (True, ws.subscribe_index, ws.unsubscribe_index)):
        if t := toks(drop, index):
            await unsub(t)
        if t := toks(add, index):
            await sub(t)
    with _lock:
        _subscribed.difference_update(drop)
        _subscribed.update(add)
        for k in drop:
            _ticks.pop(k, None)


async def _session(force_login: bool = False) -> None:
    """One connection: log in, connect, keep subscriptions in sync, and read messages until it ends or the
    trading day changes (the login session is per day)."""
    from . import broker
    client = broker.login(force=force_login)
    # Reconnecting is done here (_run), with a fresh login, rather than by the SDK
    ws = client.create_websocket(max_reconnect_attempts=0, max_connect_retries=1, ping_interval=20)
    await ws.connect()
    day = datetime.now(IST).date()
    with _lock:
        _subscribed.clear()
        _ticks.clear()
    _state.update(connected=True, since=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"), error=None, down_since=None,
                  last_mono=time.monotonic())
    log.info("Kotak live feed connected")

    async def reader():
        async for m in ws:
            on_message(m)
    task = asyncio.create_task(reader())
    try:
        while not task.done() and ws.is_connected:
            await _sync(ws)
            if datetime.now(IST).date() != day or not config.LIVE_FEED:
                break
            if stalled():
                raise ConnectionError(f"no prices for {STALL_SEC} s while the market is open")
            await asyncio.sleep(SYNC_SEC)
    finally:
        _state.update(connected=False, down_since=time.monotonic())
        task.cancel()
        try:
            await ws.close()
        except Exception:
            pass
        with _lock:
            _subscribed.clear()
            _ticks.clear()


def _run() -> None:
    fails = 0
    while True:
        if not config.LIVE_FEED:
            time.sleep(5)
            continue
        started = time.monotonic()
        try:
            asyncio.run(_session(force_login=fails > 0))
            _state["error"] = None
        except Exception as exc:
            _state["error"] = str(exc)[:200]
            log.warning("Kotak live feed stopped (%s); quotes use REST until it reconnects", exc)
        _state["connected"] = False
        _state["reconnects"] += 1
        fails = 0 if time.monotonic() - started > 300 else fails + 1
        time.sleep(RETRY_SEC[min(fails, len(RETRY_SEC) - 1)])


def start() -> bool:
    """Start the feed thread (once) if it's switched on and Kotak login details are in .env."""
    global _thread
    from . import broker
    if running() or not config.LIVE_FEED or broker.missing_credentials():
        return running()
    _state["down_since"] = time.monotonic()
    _thread = threading.Thread(target=_run, name="kotak-live-feed", daemon=True)
    _thread.start()
    return True
