"""Kotak live price stream (livefeed.py): feed messages become REST-shaped quotes that kotak / fno lookups use,
subscriptions follow demand, and the health check notices an outage. No WebSocket is opened."""
import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from neo_api_client.websocket.feed import DepthLevel, SFeedIndex, SFeedScrip

from algotrade import broker, config, fno, health, kotak, livefeed
from test_kotak import FakeNeo, neo, quote  # noqa: F401  (fixture)

IST = ZoneInfo("Asia/Kolkata")


def scrip(token, ltp, seg="nse_cm", prev=100.0, bid=None, ask=None, oi=0):
    return SFeedScrip(
        exchange_segment=seg, instrument_token=str(token), level=4, last_traded_price=ltp, open_price=101.0,
        high_price=max(ltp, 102.0), low_price=99.0, close_price=prev, average_trade_price=ltp,
        last_trade_time=1792146583, last_update_time=1792146600, last_trade_qty=1, total_buy_quantity=0,
        total_sell_quantity=0, volume_traded_today=12345, open_interest=oi, net_change=ltp - prev,
        net_change_percent=(ltp - prev) / prev * 100, upper_circuit_limit=0, lower_circuit_limit=0,
        yearly_high=0, yearly_low=0, total_traded_value=0, market_lot=1, precision=2, multiplier=1,
        buy=[DepthLevel(quantity=50, price=bid, orders=1)] if bid else [],
        sell=[DepthLevel(quantity=75, price=ask, orders=1)] if ask else [])


def index(name, ltp, prev):
    return SFeedIndex(exchange_segment="nse_cm", instrument_token="4247863880", name=name, last_traded_price=ltp,
                      open_price=prev, high_price=ltp, low_price=prev, close_price=prev, change=ltp - prev,
                      net_change_percent=0.5, yearly_high=0, yearly_low=0, last_trade_time=1792144733,
                      precision=2, multiplier=0.01)


@pytest.fixture
def feed(monkeypatch):
    """The stream 'running and connected' without a thread or socket; deliver(msg) makes a key subscribed + ticked."""
    for name, val in (("_ticks", {}), ("_want", {}), ("_subscribed", set())):
        monkeypatch.setattr(livefeed, name, val)
    monkeypatch.setattr(livefeed, "_state", {**livefeed._state, "connected": True, "messages": 0, "down_since": None})
    monkeypatch.setattr(livefeed, "running", lambda: True)

    def deliver(m):
        livefeed.on_message(m)
        k = livefeed.key(m.exchange_segment, m.name if m.type == "index" else m.instrument_token)
        livefeed._subscribed.add(k)
    return deliver


def test_lookup_does_nothing_when_the_stream_isnt_running(monkeypatch):
    monkeypatch.setattr(livefeed, "_want", {})
    monkeypatch.setattr(livefeed, "_thread", None)
    assert livefeed.lookup("nse_cm", ["2885"]) == {} and livefeed._want == {}


def test_stock_quotes_come_from_the_stream_and_the_rest_from_rest(neo, feed):  # noqa: F811
    neo.quotes_by_token = {"11536": quote(11536, 2465.0)}
    feed(scrip(2885, 1211.2, prev=1215.0))
    q = kotak.quotes(["RELIANCE", "TCS"])
    assert q["RELIANCE"]["ltp"] == 1211.2 and q["RELIANCE"]["prev_close"] == 1215.0
    assert q["RELIANCE"]["volume"] == 12345 and q["RELIANCE"]["time"] == datetime(2026, 10, 16, 15, 59, 43)
    assert q["TCS"]["ltp"] == 2465.0
    assert [c[1] for c in neo.calls if c[0] == "quotes"] == [["11536"]]      # only TCS went to REST
    assert set(livefeed._want) == {"nse_cm|2885", "nse_cm|11536"}           # TCS is now wanted, so it'll stream


def test_all_streamed_means_no_rest_call(neo, feed):  # noqa: F811
    feed(scrip(2885, 1211.2))
    kotak.quotes(["RELIANCE"])
    assert not [c for c in neo.calls if c[0] == "quotes"]


def test_disconnected_stream_falls_back_to_rest(neo, feed):  # noqa: F811
    neo.quotes_by_token = {"2885": quote(2885, 1200.0)}
    feed(scrip(2885, 1211.2))
    livefeed._state["connected"] = False
    assert kotak.quotes(["RELIANCE"])["RELIANCE"]["ltp"] == 1200.0


def test_index_quotes_from_the_stream(neo, feed):  # noqa: F811
    feed(index("Nifty 50", 23146.15, 22997.35))
    q = kotak.index_quote("Nifty 50")
    assert q["ltp"] == 23146.15 and q["prev_close"] == 22997.35 and q["time"].year == 2026
    assert kotak.index_quotes(["Nifty 50"]) == {"Nifty 50": {"ltp": 23146.15, "prev_close": 22997.35}}
    assert not [c for c in neo.calls if c[0] == "quotes"]


def test_option_quote_from_the_stream_has_bid_and_ask(monkeypatch, feed):
    monkeypatch.setattr(fno, "contract", lambda tsym: {"token": "43210"})
    monkeypatch.setattr(kotak, "_client", FakeNeo())
    feed(scrip(43210, 70.5, seg="nse_fo", prev=65.0, bid=70.4, ask=70.6, oi=1500))
    q = fno.option_quote("NIFTY26O1923050CE")
    assert (q["ltp"], q["bid"], q["ask"], q["bid_qty"], q["ask_qty"], q["oi"]) == (70.5, 70.4, 70.6, 50, 75, 1500)
    assert q["pct"] == pytest.approx(8.4615, abs=1e-3)


def test_no_bid_is_none(monkeypatch, feed):
    monkeypatch.setattr(fno, "contract", lambda tsym: {"token": "43210"})
    feed(scrip(43210, 70.5, seg="nse_fo"))
    assert fno.option_quote("X")["bid"] is None


def run(coro):
    """Run a coroutine that never waits (the fake socket answers at once); asyncio's loop needs a local socket,
    which the test network block refuses on Windows."""
    try:
        coro.send(None)
    except StopIteration:
        return
    raise AssertionError("coroutine waited")


class FakeWs:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def call(tokens):
            self.calls.append((name, sorted(t.inputtoken for t in tokens)))
        return call


def test_sync_subscribes_demand_and_drops_idle(monkeypatch, feed):
    clock = [1000.0]
    monkeypatch.setattr(livefeed.time, "monotonic", lambda: clock[0])
    livefeed.lookup("nse_cm", ["2885", "Nifty 50"])
    livefeed.lookup("nse_fo", ["43210"])
    ws = FakeWs()
    run(livefeed._sync(ws))
    assert ws.calls == [("subscribe_scrips", ["nse_cm|2885", "nse_fo|43210"]),
                        ("subscribe_index", ["nse_cm|Nifty 50"])]
    assert livefeed._subscribed == {"nse_cm|2885", "nse_fo|43210", "nse_cm|Nifty 50"}

    clock[0] += livefeed.IDLE_SEC - 1
    livefeed.lookup("nse_cm", ["2885"])                       # still in use
    clock[0] += 2
    livefeed.on_message(scrip(43210, 70.0, seg="nse_fo"))
    ws.calls.clear()
    run(livefeed._sync(ws))
    assert ws.calls == [("unsubscribe_scrips", ["nse_fo|43210"]), ("unsubscribe_index", ["nse_cm|Nifty 50"])]
    assert livefeed._subscribed == {"nse_cm|2885"} and "nse_fo|43210" not in livefeed._ticks


def test_subscriptions_capped_to_most_recent(monkeypatch, feed):
    monkeypatch.setattr(livefeed, "MAX_TOKENS", 2)
    clock = [0.0]
    monkeypatch.setattr(livefeed.time, "monotonic", lambda: clock[0])
    for tok in ("1", "2", "3"):
        clock[0] += 1
        livefeed.lookup("nse_cm", [tok])
    assert livefeed._desired() == {"nse_cm|2", "nse_cm|3"}


def test_other_messages_ignored(feed):
    livefeed.on_message(type("Cas", (), {"type": "cas_change"})())
    assert livefeed._ticks == {}


def test_down_minutes(monkeypatch, feed):
    monkeypatch.setattr(config, "LIVE_FEED", True)
    assert livefeed.down_minutes() is None                                  # connected
    livefeed._state.update(connected=False, down_since=livefeed.time.monotonic() - 300)
    assert livefeed.down_minutes() == pytest.approx(5, abs=0.1)
    monkeypatch.setattr(config, "LIVE_FEED", False)
    assert livefeed.down_minutes() is None                                  # switched off


def test_health_warns_when_the_stream_is_down_in_market_hours(monkeypatch):
    monkeypatch.setattr(broker, "missing_credentials", lambda: ["KOTAK_MPIN"])
    monkeypatch.setattr(config, "KOTAK_CONSUMER_KEY", "")
    monkeypatch.setattr(livefeed, "status", lambda: {"error": "timeout"})
    for hh, down, ids in ((11, 7.0, ["feed"]), (17, 7.0, []), (11, 1.0, [])):   # closed / just dropped: no warning
        t = datetime(2026, 10, 16, hh, 0, tzinfo=IST)
        monkeypatch.setattr(type(health.markets.get("IN")), "now", lambda self, t=t: t)
        monkeypatch.setattr(livefeed, "down_minutes", lambda d=down: d)
        assert [p["id"] for p in health.problems()] == ids


def test_feed_is_read_only():
    src = inspect.getsource(livefeed)
    for forbidden in ("place_order", "modify_order", "cancel_order", "set_trading", "orders."):
        assert forbidden not in src


def test_kotak_still_never_logs_in():
    src = inspect.getsource(kotak)
    assert "totp_login" not in src and "broker" not in src


# ---- push stream to the page (/api/stream) --------------------------------------------------------------

class FakeFeed:
    """Scripted feed for web.push_events: each wait() applies the next step's updates."""

    def __init__(self, steps):
        self.steps, self.n, self.changed, self.touched = list(steps), 0, {}, []

    def connected(self):
        return bool(self.steps)

    def seq(self):
        return self.n

    def touch(self, keys):
        self.touched.append(list(keys))

    def wait(self, timeout):
        for k in self.steps.pop(0):
            self.n += 1
            self.changed[k] = self.n

    def changed_since(self, since, keys):
        return self.n, [k for k in keys if self.changed.get(k, 0) > since]


def test_push_events_only_for_the_pages_instruments(monkeypatch):
    from algotrade import web
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    clock = [0.0]
    monkeypatch.setattr(web.time, "monotonic", lambda: clock[0])
    feed = FakeFeed([["nse_cm|2885"], ["nse_cm|999"], ["nse_cm|2885", "nse_cm|11536"]])
    keys = {"nse_cm|2885": {"v", "t"}, "nse_cm|11536": {"v"}, "nse_cm|Nifty 50": {"h"}}
    feed.steps.append(["nse_cm|Nifty 50"])
    out = list(web.push_events(keys, feed))
    assert out == ["retry: 5000\n\n", 'data: ["t", "v"]\n\n', 'data: ["t", "v"]\n\n', 'data: ["h"]\n\n',
                   "event: down\ndata: 0\n\n"]
    assert feed.touched == [list(keys)]


def test_push_events_ping_when_quiet(monkeypatch):
    from algotrade import web
    monkeypatch.setattr(web.time, "sleep", lambda s: None)
    clock = [0.0]

    def mono():
        clock[0] += 10
        return clock[0]
    monkeypatch.setattr(web.time, "monotonic", mono)
    out = list(web.push_events({"nse_cm|2885": {"v"}}, FakeFeed([[], [], []])))
    assert ": ping\n\n" in out and not [x for x in out if x.startswith("data")]


def test_stream_keys_and_refusals(monkeypatch, feed):
    from algotrade import market, web
    monkeypatch.setattr(kotak, "tokens", lambda: {"RELIANCE": "2885", "TCS": "11536"})
    monkeypatch.setattr(fno, "contract", lambda t: {"token": "43210"} if t == "NIFTYX" else None)
    monkeypatch.setattr(market, "_stocks", lambda: ["TCS"])
    with web.app.test_request_context("/api/stream?s=RELIANCE,NOPE&i=Nifty 50&o=NIFTYX,BAD&u=NIFTY"):
        assert list(web.stream_keys()) == ["nse_cm|2885", "nse_cm|Nifty 50", "nse_fo|43210"]   # unknowns dropped
    with web.app.test_request_context("/api/stream?s=RELIANCE&u=NIFTY&o=NIFTYX&h=1&t=TCS,RELIANCE"):
        assert web.stream_keys() == {"nse_cm|2885": {"v", "t"}, "nse_cm|Nifty 50": {"v", "h"},
                                     "nse_fo|43210": {"v"}, "nse_cm|11536": {"t"}}
    with web.app.test_request_context("/api/stream?m=1"):
        keys = list(web.stream_keys())
        assert keys[0] == "nse_cm|11536" and "nse_cm|Nifty 50" in keys and "nse_cm|Nifty IT" in keys
    c = web.app.test_client()
    assert c.get("/api/stream").status_code == 400                       # nothing asked for
    livefeed._state["connected"] = False
    assert c.get("/api/stream?s=RELIANCE").status_code == 503            # page keeps polling instead


def test_signals_show_streamed_prices_while_connected(neo, feed):  # noqa: F811
    from algotrade import state, web
    from conftest import make_signal
    state.record([make_signal(symbol="RELIANCE", entry=1200.0, stop_loss=1180.0, targets=[1230.0, 1260.0])], telegram=False)
    feed(scrip(2885, 1230.0))
    c = web.app.test_client()
    s = c.get("/api/signals?market=IN").get_json()["signals"][0]
    assert s["current"] == 1230.0 and s["to_entry_pct"] == round(30 / 1230 * 100, 2)
    livefeed._state["connected"] = False                     # stream down: the tracker's last price, no REST call
    s = c.get("/api/signals?market=IN").get_json()["signals"][0]
    assert s["current"] != 1230.0 and not [x for x in neo.calls if x[0] == "quotes"]
