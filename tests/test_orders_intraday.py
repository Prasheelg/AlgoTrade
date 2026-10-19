"""orders: intraday (MIS) Auto-trade for 5m-1h signals: entry window, one trade per stock, the
15:00 square-off, and trades left after the close."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from algotrade import config, markets, orders
from test_orders import kotak, signal, texts, trade  # noqa: F401  (kotak is a fixture)

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clock(kotak, monkeypatch):
    """Set India's clock (Monday 2024-01-01 by default); market hours follow it. Intraday trading on."""
    now = {"t": datetime(2024, 1, 1, 10, 0, tzinfo=IST)}
    monkeypatch.setattr(markets.Market, "now", lambda self: now["t"].astimezone(ZoneInfo(self.tz)))
    monkeypatch.setattr(orders, "_market_open", lambda: markets.get("IN").is_open())
    monkeypatch.setattr(config, "INTRADAY_AUTO_TRADE", True)

    def set_(h, m, day=1):
        now["t"] = datetime(2024, 1, day, h, m, tzinfo=IST)
    return set_


def fast(symbol="RELIANCE", tf="15m"):
    return signal(symbol, timeframe=tf, bar_time=datetime(2024, 1, 1, 9, 45))


def test_intraday_entry_is_mis_in_market_hours(clock, kotak):
    t = orders.enter([fast()])[0]
    assert (t["state"], t["timeframe"], t["product"]) == ("ENTRY", "15m", "MIS")
    p = kotak.placed[0]
    assert (p["product"], p["amo"], p["order_type"], p["price"]) == ("MIS", "NO", "L", "99.00")
    assert "BUY RELIANCE</b> (15m intraday)" in texts(kotak)[0]


@pytest.mark.parametrize("hm,why", [((14, 30), "too late for an intraday entry"),
                                    ((8, 50), "only traded while the market is open")])
def test_intraday_entry_window(clock, kotak, hm, why):
    clock(*hm)
    t = orders.enter([fast()])[0]
    assert t["state"] == "SKIPPED" and why in t["note"] and kotak.placed == []


def test_intraday_off_in_settings(clock, kotak, monkeypatch):
    monkeypatch.setattr(config, "INTRADAY_AUTO_TRADE", False)
    assert "Auto-trade intraday signals is off" in orders.enter([fast()])[0]["note"]


def test_one_trade_per_stock(clock, kotak):
    orders.enter([signal("A")])
    t = orders.enter([fast("A", tf="5m")])[0]
    assert t["state"] == "SKIPPED" and "already trading A" in t["note"] and len(kotak.placed) == 1


def test_fill_gets_a_mis_stop(clock, kotak):
    key = fast()
    orders.enter([key])
    kotak.fill("101", 101, 98.9)
    orders.reconcile()
    s = kotak.placed[1]
    assert trade(key)["state"] == "OPEN" and (s["order_type"], s["product"], s["transaction_type"]) == ("SL", "MIS", "S")


def test_square_off_at_three(clock, kotak):
    held, waiting, daily = fast("A"), fast("B", tf="5m"), signal("RELIANCE")
    orders.enter([held, waiting, daily])                     # entries 101 (A), 102 (B), 103 (RELIANCE)
    kotak.fill("101", 101, 98.9)
    orders.reconcile()                                       # A's stop: 104
    clock(14, 59)
    orders.reconcile()
    assert trade(held)["state"] == "OPEN"                    # not yet
    clock(15, 0)
    orders.reconcile()
    a, b = trade(held), trade(waiting)
    assert (a["state"], a["note"]) == ("EXITING", orders.SQUARE_OFF_NOTE)
    sell = kotak.placed[-1]
    assert (sell["transaction_type"], sell["product"], sell["order_type"], sell["trading_symbol"]) == ("S", "MIS", "L", "A-EQ")
    assert "104" in kotak.cancelled and "102" in kotak.cancelled   # A's stop after the sell; B's entry
    assert (b["state"], b["note"]) == ("CANCELLED", orders.SQUARE_OFF_NOTE)
    assert trade(daily)["state"] == "ENTRY" and "103" not in kotak.cancelled   # daily trades carry on


def test_intraday_left_after_the_close_is_not_carried_over(clock, kotak):
    held, waiting = fast("A"), fast("B")
    orders.enter([held, waiting])
    kotak.fill("101", 101, 98.9)
    orders.reconcile()
    n = len(kotak.placed)
    clock(16, 0)                                             # the scanner missed the square-off
    orders.reconcile()
    assert trade(held)["state"] == "CLOSED" and "squared off by Kotak" in trade(held)["note"]
    assert trade(waiting)["state"] == "CANCELLED"
    assert len(kotak.placed) == n                            # no AMO stop for a position Kotak already closed
    assert any("still open in the app" in m for m in texts(kotak))


def wait_for(cond, secs=5.0):
    end = time.monotonic() + secs
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_each_live_trade_gets_a_watcher_thread(clock, kotak, monkeypatch):
    monkeypatch.setattr(orders, "WATCH_SEC", 0.01)
    orders.guard_tick()
    assert orders._watchers == {}                            # not the scanner: no threads (dashboard, tests)
    orders.start_watching()
    try:
        a, b = signal("A"), fast("B")                        # a daily trade is watched too
        orders.enter([a, b])
        orders.guard_tick()
        assert {k for k, th in orders._watchers.items() if th.is_alive()} == {a, b}
        orders.guard_tick()
        assert len(orders._watchers) == 2                    # one each
        kotak.fill("101", 101, 98.9)                         # A's entry fills: its watcher places stop + target
        wait_for(lambda: trade(a)["target_order"] is not None)
        assert trade(a)["state"] == "OPEN" and trade(a)["stop_order"] is not None
        kotak.fill(trade(a)["target_order"], 101, 103.5)
        wait_for(lambda: not orders._watchers[a].is_alive())  # done: its thread ends
        assert trade(a)["state"] == "CLOSED" and orders._watchers[b].is_alive()
    finally:
        orders.stop_watching()
    assert not any(th.is_alive() for th in orders._watchers.values())
    orders._watchers.clear()


def test_pre_open_signal_is_entered_at_the_open(clock, kotak):
    clock(9, 5)                                              # pre-open scan
    key = fast()
    assert orders.enter([key])[0]["state"] == "SKIPPED" and kotak.placed == []
    orders.guard_tick()
    assert kotak.placed == []                                # still shut
    clock(9, 16)
    orders.guard_tick()
    assert trade(key)["state"] == "ENTRY" and kotak.placed[0]["product"] == "MIS"
    orders.guard_tick()
    assert len(kotak.placed) == 1


def test_pre_open_signal_from_an_earlier_day_is_not_retried(clock, kotak):
    clock(15, 45)                                            # after the close
    key = fast()
    orders.enter([key])
    clock(9, 16, day=2)
    orders.guard_tick()
    assert trade(key)["state"] == "SKIPPED" and kotak.placed == []
