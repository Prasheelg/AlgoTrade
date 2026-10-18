"""orders: automatic Kotak entries, stops and exits against a fake order book (never the real API)."""
import pytest

import main
from algotrade import broker, config, kotak as kotak_quotes, orders, state
from conftest import Recorder, make_signal


class FakeKotak:
    """Order book stand-in behind broker.call: place / cancel / order_report."""

    def __init__(self):
        self.book, self.placed, self.cancelled, self.n = {}, [], [], 100
        self.fail_place = []      # BrokerError messages for the next place_order calls
        self.reject_types = set() # order types Kotak rejects on placement (status 'rejected')

    def __call__(self, method, **kw):
        if method == "order_report":
            return {"data": list(self.book.values())}
        if method == "place_order":
            if self.fail_place:
                raise broker.BrokerError(f"place_order failed: {self.fail_place.pop(0)}")
            self.n += 1
            oid = str(self.n)
            status = "rejected" if kw["order_type"] in self.reject_types else "open"
            self.book[oid] = {"nOrdNo": oid, "ordSt": status, "trdSym": kw["trading_symbol"], "trnsTp": kw["transaction_type"],
                              "qty": kw["quantity"], "fldQty": "0", "avgPrc": "0",
                              "rejRsn": "SL-M not allowed" if status == "rejected" else ""}
            self.placed.append({**kw, "oid": oid})
            return {"nOrdNo": oid, "stat": "Ok", "stCode": 200}
        if method == "cancel_order":
            self.cancelled.append(kw["order_id"])
            self.book[kw["order_id"]]["ordSt"] = "cancelled"
            return {"stat": "Ok"}
        raise AssertionError(f"unexpected call {method}")

    def fill(self, oid, qty, price, status="complete"):
        self.book[oid].update(ordSt=status, fldQty=str(qty), avgPrc=str(price))


@pytest.fixture
def kotak(monkeypatch):
    fake = FakeKotak()
    monkeypatch.setattr(broker, "call", fake)
    monkeypatch.setattr(orders, "_market_open", lambda: True)
    monkeypatch.setattr(config, "MAX_ORDERS_PER_DAY", 2)
    monkeypatch.setattr(config, "MAX_ORDER_VALUE", 10000.0)
    msgs = Recorder(True)
    monkeypatch.setattr(orders.telegram_bot, "send_message", msgs)
    fake.msgs = msgs
    fake.ltp = {"RELIANCE": 100.0, "A": 100.0, "B": 100.0}   # live prices (kotak.quotes)
    monkeypatch.setattr(kotak_quotes, "quotes", lambda syms: {s: {"ltp": fake.ltp[s]} for s in syms if s in fake.ltp})
    state.set_setting(broker.TRADING_KEY, {"enabled": True})
    return fake


def signal(symbol="RELIANCE", **kw):
    """Record a scanner signal (limit BUY 99, SL 96, T 103.5 / 108) and return its key."""
    s = make_signal(symbol=symbol, **kw)
    state.record([s])
    return s.key


def trade(key):
    return orders._get(key)


def texts(fake):
    return [c[0][0] for c in fake.msgs.calls]


# ---- entries ----------------------------------------------------------------------------------

def test_nothing_when_switch_off(kotak):
    state.set_setting(broker.TRADING_KEY, {"enabled": False})
    assert orders.enter([signal()]) == [] and kotak.placed == [] and orders.all_trades() == []


def test_limit_entry_capped_by_order_value(kotak):
    t = orders.enter([signal()])[0]
    # qty by risk 333 (₹1000 / 3), capped to 101 by ₹10,000 / 99
    assert (t["state"], t["qty"], t["entry_order"]) == ("ENTRY", 101, "101")
    p = kotak.placed[0]
    assert {k: p[k] for k in ("order_type", "price", "trigger_price", "quantity", "transaction_type", "trading_symbol",
                               "product", "validity", "amo", "exchange_segment")} == \
        {"order_type": "L", "price": "99.00", "trigger_price": "0", "quantity": "101", "transaction_type": "B",
         "trading_symbol": "RELIANCE-EQ", "product": "CNC", "validity": "DAY", "amo": "NO", "exchange_segment": "nse_cm"}
    assert "BUY RELIANCE" in texts(kotak)[0] and "× 101" in texts(kotak)[0]


def test_stop_entry_and_amo_when_closed(kotak, monkeypatch):
    monkeypatch.setattr(orders, "_market_open", lambda: False)
    orders.enter([signal(entry_type="stop", entry=99.02)])
    p = kotak.placed[0]
    assert (p["order_type"], p["trigger_price"], p["price"], p["amo"]) == ("SL", "99.05", "99.55", "YES")
    assert "(AMO)" in texts(kotak)[0]


@pytest.mark.parametrize("kw,why", [
    (dict(side="SELL"), "only BUY"),
    (dict(timeframe="15m"), "intraday signals aren't traded"),   # INTRADAY_AUTO_TRADE off
    (dict(market="US"), None),                      # not an India signal: not looked up at all
    (dict(entry=12000.0, stop_loss=11900.0), "costs more than MAX_ORDER_VALUE"),
])
def test_skipped_signals(kotak, kw, why):
    key = signal(**kw)
    orders.enter([key])
    t = trade(key)
    if why is None:
        assert t is None
    else:
        assert t["state"] == "SKIPPED" and why in t["note"] and t["qty"] == 0
    assert kotak.placed == []


def test_daily_cap_counts_placed_entries(kotak):
    keys = [signal(symbol=s) for s in ("A", "B", "C")]
    orders.enter(keys)
    assert [trade(k)["state"] for k in keys] == ["ENTRY", "ENTRY", "SKIPPED"]
    assert "daily limit of 2" in trade(keys[2])["note"]


def test_same_signal_never_entered_twice(kotak):
    key = signal()
    orders.enter([key])
    orders.enter([key])
    assert len(kotak.placed) == 1


def test_open_buy_in_book_blocks_entry(kotak):
    kotak.book["9"] = {"nOrdNo": "9", "ordSt": "open", "trdSym": "RELIANCE-EQ", "trnsTp": "B"}
    t = orders.enter([signal()])[0]
    assert t["state"] == "NEW" and "already exists" in t["note"] and kotak.placed == []


def test_place_error_alerts_once_and_retries(kotak):
    kotak.fail_place = ["Insufficient funds", "Insufficient funds"]
    key = signal()
    orders.enter([key])
    assert trade(key)["state"] == "NEW" and "Insufficient funds" in trade(key)["note"]
    orders.reconcile()
    assert len([m for m in texts(kotak) if "entry order failed" in m]) == 1
    orders.reconcile()                         # third try succeeds
    assert trade(key)["state"] == "ENTRY" and trade(key)["note"] is None


# ---- fills and stops --------------------------------------------------------------------------

def test_fill_places_stop_limit(kotak):
    key = signal()
    orders.enter([key])
    kotak.fill("101", 101, 98.9)
    orders.reconcile()
    t = trade(key)
    assert (t["state"], t["qty"], t["fill_price"], t["stop_order"]) == ("OPEN", 101, 98.9, "102")
    s = kotak.placed[1]
    assert (s["order_type"], s["trigger_price"], s["price"], s["transaction_type"], s["quantity"]) == \
        ("SL", "96.00", "95.00", "S", "101")     # no SL-M via the API: limit 1% below the trigger
    assert any("bought × 101" in m for m in texts(kotak)) and any("stop-loss @ ₹96.00" in m for m in texts(kotak))


def test_partial_fill_then_expiry_opens_with_filled_qty(kotak):
    key = signal()
    orders.enter([key])
    kotak.fill("101", 40, 99.0, status="open")     # partly filled, still working: wait
    orders.reconcile()
    assert trade(key)["state"] == "ENTRY"
    kotak.fill("101", 40, 99.0, status="cancelled")
    orders.reconcile()
    assert (trade(key)["state"], trade(key)["qty"]) == ("OPEN", 40)


def test_entry_rejected(kotak):
    key = signal()
    orders.enter([key])
    kotak.book["101"].update(ordSt="rejected", rejRsn="RMS:Margin Exceeds")
    orders.reconcile()
    assert trade(key)["state"] == "REJECTED" and trade(key)["note"] == "RMS:Margin Exceeds"
    assert any("entry rejected by Kotak: RMS:Margin Exceeds" in m for m in texts(kotak))


def test_switch_off_cancels_unfilled_entries(kotak):
    key = signal()
    orders.enter([key])
    state.set_setting(broker.TRADING_KEY, {"enabled": False})
    orders.reconcile()
    assert kotak.cancelled == ["101"] and trade(key)["state"] == "CANCELLED"
    assert trade(key)["note"] == "auto-trade switched off"


def test_tracker_cancel_cancels_entry(kotak):
    key = signal()
    orders.enter([key])
    state.update_signal(key, status="CANCELLED")
    orders.reconcile()
    assert kotak.cancelled == ["101"] and trade(key)["note"] == "signal cancelled"


def test_expired_entry_placed_again_while_signal_pending(kotak, monkeypatch):
    key = signal()
    orders.enter([key])
    kotak.book["101"]["ordSt"] = "cancelled"        # day order lapsed at the close
    orders.reconcile()
    assert trade(key)["entry_order"] == "102"
    del kotak.book["102"]                           # next day, book empty after midnight: wait for the open
    monkeypatch.setattr(orders, "_market_open", lambda: False)
    orders.reconcile()
    assert trade(key)["entry_order"] == "102" and len(kotak.placed) == 2
    monkeypatch.setattr(orders, "_market_open", lambda: True)
    orders.reconcile()
    assert trade(key)["entry_order"] == "103"
    assert len([m for m in texts(kotak) if "BUY RELIANCE" in m]) == 1   # re-placements aren't re-announced


def opened(kotak):
    key = signal()
    orders.enter([key])
    kotak.fill("101", 101, 99.0)
    orders.reconcile()                              # stop = order 102
    return key


def test_stop_filled_closes_trade(kotak):
    key = opened(kotak)
    kotak.fill("102", 101, 95.9)
    orders.reconcile()
    t = trade(key)
    assert (t["state"], t["exit_price"], t["note"]) == ("CLOSED", 95.9, "stop-loss")
    assert any("P&L ₹-313 before charges" in m for m in texts(kotak))


def test_expired_stop_replaced_but_not_overnight_when_missing(kotak, monkeypatch):
    key = opened(kotak)
    kotak.book["102"]["ordSt"] = "cancelled"         # lapsed at the close: re-place (AMO after hours)
    monkeypatch.setattr(orders, "_market_open", lambda: False)
    orders.reconcile()
    assert trade(key)["stop_order"] == "103" and kotak.placed[-1]["amo"] == "YES"
    del kotak.book["103"]                           # after midnight: not in today's book -> wait
    orders.reconcile()
    assert trade(key)["stop_order"] == "103"


def test_stop_rejections_retry_then_give_up_loudly(kotak):
    kotak.reject_types = {"SL"}
    key = signal()
    orders.enter([key])
    kotak.fill("101", 101, 99.0)
    for _ in range(5):
        orders.reconcile()
    assert len([p for p in kotak.placed if p["order_type"] == "SL"]) == orders.STOP_MAX_FAILS
    t = trade(key)
    assert t["alerted"] == "stop_gave_up" and t["note"].startswith("NO STOP") and t["state"] == "OPEN"
    assert len([m for m in texts(kotak) if "stop-loss rejected" in m]) == orders.STOP_MAX_FAILS - 1
    assert len([m for m in texts(kotak) if "has stopped retrying" in m]) == 1


def test_stop_cancelled_in_market_hours_is_replaced_with_a_notice(kotak):
    key = opened(kotak)
    kotak.book["102"]["ordSt"] = "cancelled"         # someone cancelled it in the Kotak app
    orders.reconcile()
    assert trade(key)["stop_order"] == "103"
    assert any("was cancelled (in the Kotak app?)" in m for m in texts(kotak))


def test_price_gapping_through_the_stop_sells(kotak):
    key = opened(kotak)
    kotak.ltp["RELIANCE"] = 94.0                    # below the stop's limit (95.00): it can't fill
    orders.reconcile()
    t = trade(key)
    assert t["state"] == "EXITING" and "gapped" in t["note"] and kotak.cancelled == ["102"]
    p = kotak.placed[-1]
    assert (p["order_type"], p["price"], p["transaction_type"]) == ("L", "93.05", "S")   # 1% below 94


def test_unfilled_exit_is_repriced(kotak):
    key = opened(kotak)
    state.update_signal(key, status="T2_HIT")
    orders.reconcile()                              # sell 103 @ 99.00
    orders.reconcile()                              # just placed: left alone
    assert len(kotak.placed) == 3
    orders._save(key, exit_at="2000-01-01T00:00:00")
    kotak.ltp["RELIANCE"] = 97.0
    kotak.book["103"]["fldQty"] = "1"               # one share sold before the cancel
    orders.reconcile()
    t = trade(key)
    assert kotak.cancelled[-1] == "103" and t["exit_order"] == "104" and t["qty"] == 100
    assert (kotak.placed[-1]["price"], kotak.placed[-1]["quantity"]) == ("96.00", "100")


def test_no_live_price_no_blind_sell(kotak):
    key = opened(kotak)
    kotak.ltp.clear()
    with pytest.raises(broker.BrokerError, match="no live price"):
        orders.close_trade(key)
    assert orders._get(key)["state"] == "OPEN" and kotak.cancelled == []


def test_no_stop_alert_once(kotak):
    key = signal()
    orders.enter([key])
    kotak.fill("101", 101, 99.0)
    kotak.fail_place = ["down", "down", "down"]
    orders.reconcile()
    orders.reconcile()
    assert "NO STOP" in trade(key)["note"]
    assert len([m for m in texts(kotak) if "NO stop-loss" in m]) == 1


def test_target1_moves_stop_to_fill(kotak):
    key = opened(kotak)
    state.update_signal(key, status="T1_HIT")
    orders.reconcile()
    t = trade(key)
    assert kotak.cancelled == ["102"] and t["stop"] == 99.0 and t["be_moved"] == 1 and t["stop_order"] == "103"
    assert kotak.placed[-1]["trigger_price"] == "99.00"
    orders.reconcile()                              # only once
    assert len(kotak.placed) == 3


@pytest.mark.parametrize("status,why", [("T2_HIT", "target 2"), ("EXPIRED", "time exit")])
def test_target2_or_time_exit_sells_with_a_limit(kotak, status, why):
    key = opened(kotak)
    state.update_signal(key, status=status)
    orders.reconcile()
    t = trade(key)
    assert kotak.cancelled == ["102"] and t["state"] == "EXITING" and t["exit_order"] == "103"
    assert (kotak.placed[-1]["order_type"], kotak.placed[-1]["price"], kotak.placed[-1]["transaction_type"]) == (
        "L", "99.00", "S")                          # live 100, 1% below: no market orders via the API
    kotak.fill("103", 101, 108.2)
    orders.reconcile()
    assert (trade(key)["state"], trade(key)["exit_price"], trade(key)["note"]) == ("CLOSED", 108.2, why)


def test_open_positions_keep_stops_when_switch_off(kotak):
    key = opened(kotak)
    state.set_setting(broker.TRADING_KEY, {"enabled": False})
    kotak.book["102"]["ordSt"] = "cancelled"
    orders.reconcile()
    assert trade(key)["state"] == "OPEN" and trade(key)["stop_order"] == "103"


def test_one_failing_trade_does_not_stop_others(kotak, monkeypatch):
    a, b = signal(symbol="A"), signal(symbol="B")
    orders.enter([a, b])
    kotak.book["101"]["ordSt"] = "rejected"
    real = orders._step_entry

    def flaky(t, *args):
        if t["symbol"] == "A":
            raise RuntimeError("boom")
        return real(t, *args)
    monkeypatch.setattr(orders, "_step_entry", flaky)
    kotak.fill("102", 101, 99.0)
    orders.reconcile()
    assert trade(b)["state"] == "OPEN" and trade(a)["state"] == "ENTRY"


# ---- scanner hook ------------------------------------------------------------------------------

def test_run_scan_enters_new_india_signals_only(monkeypatch):
    old = signal(symbol="OLD")
    calls = []
    monkeypatch.setattr(main, "scan_and_alert", lambda **kw: state.record([make_signal(symbol="NEW")]))
    monkeypatch.setattr(orders, "enter", lambda keys: calls.append(("enter", keys)))
    monkeypatch.setattr(orders, "reconcile", lambda: calls.append(("reconcile",)))
    main.run_scan("IN")
    assert calls == [("enter", [make_signal(symbol="NEW").key]), ("reconcile",)] and old not in calls[0][1]
    calls.clear()
    monkeypatch.setattr(main, "scan_and_alert", lambda **kw: None)
    main.run_scan("US")
    assert calls == []


def test_trade_errors_never_stop_the_scanner(monkeypatch):
    def boom(keys):
        raise RuntimeError("kotak down")
    monkeypatch.setattr(orders, "enter", boom)
    main.trade(["x"])                               # logged, not raised


# ---- closing by hand (Portfolio > App trades > Close) -------------------------------------------

def test_close_open_trade_sells_first_then_cancels_stop(kotak):
    key = opened(kotak)
    state.set_setting(broker.TRADING_KEY, {"enabled": False})   # works with Auto-trade off too
    t = orders.close_trade(key)
    assert (t["state"], t["exit_order"], t["note"]) == ("EXITING", "103", "closed by you")
    assert (kotak.placed[-1]["order_type"], kotak.placed[-1]["transaction_type"], kotak.placed[-1]["quantity"]) == \
        ("L", "S", "101")
    assert kotak.cancelled == ["102"]
    kotak.fill("103", 101, 100.5)
    orders.reconcile()
    assert (orders._get(key)["state"], orders._get(key)["note"]) == ("CLOSED", "closed by you")


def test_close_after_hours_is_amo(kotak, monkeypatch):
    key = opened(kotak)
    monkeypatch.setattr(orders, "_market_open", lambda: False)
    orders.close_trade(key)
    assert kotak.placed[-1]["amo"] == "YES" and any("executes at the next open" in m for m in texts(kotak))


def test_close_unfilled_entry_cancels_it(kotak):
    key = signal()
    orders.enter([key])
    t = orders.close_trade(key)
    assert t["state"] == "CANCELLED" and kotak.cancelled == ["101"] and len(kotak.placed) == 1


def test_close_partly_filled_entry_sells_the_filled_shares(kotak):
    key = signal()
    orders.enter([key])
    kotak.fill("101", 30, 99.0, status="open")
    t = orders.close_trade(key)
    assert kotak.cancelled == ["101"] and t["state"] == "EXITING" and t["qty"] == 30
    assert (kotak.placed[-1]["order_type"], kotak.placed[-1]["quantity"]) == ("L", "30")


@pytest.mark.parametrize("setup,msg", [("exiting", "already being sold"), ("closed", "is not open"),
                                       ("unknown", "Unknown trade")])
def test_close_refusals(kotak, setup, msg):
    key = "nope"
    if setup != "unknown":
        key = opened(kotak)
        orders._save(key, state="EXITING" if setup == "exiting" else "CLOSED")
    with pytest.raises(ValueError, match=msg):
        orders.close_trade(key)


def test_close_api_local_only(kotak):
    from algotrade import web
    key = opened(kotak)
    c = web.app.test_client()
    r = c.post("/api/trades/close", json={"key": key}, environ_base={"REMOTE_ADDR": "192.168.1.5"})
    assert r.status_code == 401 and orders._get(key)["state"] == "OPEN"
    assert c.post("/api/trades/close", json={}).status_code == 400
    r = c.post("/api/trades/close", json={"key": key})
    assert r.status_code == 200 and r.get_json()["state"] == "EXITING"
    r = c.post("/api/trades/close", json={"key": key})
    assert r.status_code == 409 and "already being sold" in r.get_json()["error"]


def test_close_api_kotak_error(kotak):
    from algotrade import web
    key = opened(kotak)
    kotak.fail_place = ["Market closed for AMO"]
    r = web.app.test_client().post("/api/trades/close", json={"key": key})
    assert r.status_code == 502 and "Market closed for AMO" in r.get_json()["error"]
    assert orders._get(key)["state"] == "OPEN" and kotak.cancelled == []   # stop left in place
