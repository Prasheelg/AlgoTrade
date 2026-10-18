"""orders: intraday (MIS) Auto-trade for 5m-1h signals: entry window, own daily cap, one trade per stock, the
15:00 square-off, and trades left after the close."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from algotrade import config, markets, orders
from test_orders import kotak, signal, texts, trade  # noqa: F401  (kotak is a fixture)

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clock(kotak, monkeypatch):
    """Set India's clock (Monday 2024-01-01 by default); market hours follow it. Intraday trading on, cap 2."""
    now = {"t": datetime(2024, 1, 1, 10, 0, tzinfo=IST)}
    monkeypatch.setattr(markets.Market, "now", lambda self: now["t"].astimezone(ZoneInfo(self.tz)))
    monkeypatch.setattr(orders, "_market_open", lambda: markets.get("IN").is_open())
    monkeypatch.setattr(config, "INTRADAY_AUTO_TRADE", True)
    monkeypatch.setattr(config, "MAX_INTRADAY_ORDERS_PER_DAY", 2)

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


def test_daily_and_intraday_caps_are_separate(clock, kotak, monkeypatch):
    monkeypatch.setattr(config, "MAX_INTRADAY_ORDERS_PER_DAY", 1)
    monkeypatch.setattr(config, "MAX_ORDERS_PER_DAY", 1)
    got = orders.enter([signal("A"), fast("B"), fast("C", tf="5m"), signal("RELIANCE")])
    assert [t["state"] for t in got] == ["ENTRY", "ENTRY", "SKIPPED", "SKIPPED"]
    assert "daily limit of 1 intraday orders" in got[2]["note"] and "daily limit of 1 orders" in got[3]["note"]


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


def test_guard_tick_only_with_live_intraday_trades(clock, kotak, monkeypatch):
    calls = []
    monkeypatch.setattr(orders, "reconcile", lambda: calls.append(1))
    orders.enter([signal("A")])                              # a daily trade: the scan loop looks after it
    orders.guard_tick()
    assert calls == []
    orders._save(fast("B"), symbol="B", state="ENTRY", timeframe="15m", created_day="2024-01-01", qty=1)
    orders.guard_tick()
    assert calls == [1]
    clock(16, 0)
    orders.guard_tick()
    assert calls == [1]                                      # market closed
