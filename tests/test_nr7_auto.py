"""nr7_auto: NR7 setups traded as in-the-money options with real (here: fake) Kotak orders."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from algotrade import broker, config, nr7, nr7_auto as na, nr7_instruments, orders, state, web
from test_orders import FakeKotak

IST = ZoneInfo("Asia/Kolkata")


class Kotak(FakeKotak):
    """FakeKotak plus the positions call: net quantity by trading symbol, from the fills."""

    def __init__(self):
        super().__init__()
        self.net = {}            # tsym -> net qty override (a lot closed by hand in the Kotak app)

    def __call__(self, method, **kw):
        if method == "positions":
            net = {}
            for o in self.book.values():
                q = int(o["fldQty"]) * (1 if o["trnsTp"] == "B" else -1)
                net[o["trdSym"]] = net.get(o["trdSym"], 0) + q
            net.update(self.net)
            return {"data": [{"trdSym": k, "netQty": str(v), "flBuyQty": "0", "flSellQty": "0"} for k, v in net.items()]}
        return super().__call__(method, **kw)


class Market:
    def __init__(self):
        self.at, self.open = datetime(2026, 11, 16, 9, 20, tzinfo=IST), True

    def now(self):
        return self.at

    def is_open(self, now=None):
        return self.open


@pytest.fixture
def bot(monkeypatch):
    k = Kotak()
    monkeypatch.setattr(broker, "call", k)
    m = Market()
    monkeypatch.setattr(na, "_mkt", lambda: m)
    msgs = []
    monkeypatch.setattr(orders, "_notify", lambda text: msgs.append(text))
    for key, v in dict(NR7_AUTO_INDICES=["NIFTY", "BANKNIFTY"], NR7_AUTO_LOTS=1, NR7_OPTION_MIN_DAYS=14,
                       OPTIONS_MAX_SPREAD_PCT=5.0, OPTION_PROTECTION_PCT=5.0).items():
        monkeypatch.setattr(config, key, v)
    state.set_setting(broker.TRADING_KEY, {"enabled": True})
    nr7.save_settings({"trade": True})
    k.index = {"BANKNIFTY": {"ltp": 61000.0, "open": 61000.0, "high": 61100.0, "low": 60900.0, "time": None}}
    k.option = {"bid": 1495.0, "ask": 1500.0, "ltp": 1497.0}
    k.plans = []

    def plan(setup, opt, spot, now):
        k.plans.append((setup["idx"], opt, spot))
        return {"tsym": f"BANKNIFTY25DEC{61000 if opt == 'CE' else 61100}{opt}", "token": "111", "segment": "nse_fo",
                "expiry": "2026-12-29", "strike": 61000.0, "lot": 30, "tick": 0.05}
    monkeypatch.setattr(nr7_instruments, "option_plan", plan)
    monkeypatch.setattr(na, "_index_quote", lambda idx: dict(k.index[idx]) if idx in k.index else None)
    monkeypatch.setattr(na, "_option_quote", lambda t: dict(k.option))
    k.mkt, k.msgs = m, msgs
    return k


def bank_setup(day="2026-11-13", high=61300.55, low=60780.55, **kw):
    t = nr7.new_setup("BANKNIFTY", pd.Timestamp(day).date(), high, low)
    t.update(kw)
    nr7._insert(t)
    return t["key"]


def move(k, **kw):
    k.index["BANKNIFTY"].update(kw)


def only(key):
    return na._get(key)


def test_long_trigger_buys_one_itm_call_and_sells_it_at_the_target(bot):
    key = bank_setup()
    na.step_all()
    assert only(key)["state"] == "WAIT" and not bot.placed            # inside the range: nothing yet
    move(bot, ltp=61350.0, high=61360.0)
    na.step_all()
    t = only(key)
    assert (t["state"], t["side"], t["opt"], t["u_entry"], t["u_stop"]) == ("BUYING", "LONG", "CE", 61300.55, 60780.55)
    assert t["u_target"] == pytest.approx(61300.55 + 3 * 520, abs=0.01)
    o = bot.placed[-1]
    assert (o["transaction_type"], o["quantity"], o["product"], o["exchange_segment"], o["order_type"]) == \
        ("B", "30", "NRML", "nse_fo", "L")
    assert o["price"] == "1575.00" and o["trading_symbol"].endswith("CE")   # ask + 5%, a marketable limit
    bot.fill(o["oid"], 30, 1500.0)
    na.step_all()
    assert only(key)["state"] == "OPEN" and only(key)["pos_seen"] == 1
    move(bot, ltp=62870.0, high=62880.0)                               # past 61300.55 + 1560
    na.step_all()
    s = bot.placed[-1]
    assert only(key)["state"] == "SELLING" and (s["transaction_type"], s["quantity"]) == ("S", "30")
    bot.fill(s["oid"], 30, 2900.0)
    na.step_all()
    t = only(key)
    assert t["state"] == "CLOSED" and t["exit_price"] == 2900.0 and t["exit_why"].startswith("target")
    assert "P&L ₹42,000" in bot.msgs[-1]


def test_a_gap_open_past_the_level_enters_at_the_open(bot):
    key = bank_setup()
    move(bot, open=61500.0, high=61550.0, low=61450.0, ltp=61520.0)
    na.step_all()
    t = only(key)
    assert (t["u_entry"], t["u_target"]) == (61500.0, pytest.approx(61500 + 3 * (61500 - 60780.55), abs=0.01))


def test_short_trigger_buys_a_put_and_the_stop_sells_it(bot):
    key = bank_setup()
    move(bot, low=60700.0, ltp=60750.0)
    na.step_all()
    t = only(key)
    assert (t["side"], t["opt"], t["u_stop"]) == ("SHORT", "PE", 61300.55)
    bot.fill(bot.placed[-1]["oid"], 30, 1500.0)
    na.step_all()
    move(bot, ltp=61301.0, high=61301.0)
    na.step_all()
    assert only(key)["state"] == "SELLING" and only(key)["exit_why"].startswith("stop-loss")


def test_both_levels_traded_before_the_first_look_is_no_trade(bot):
    key = bank_setup()
    move(bot, high=61400.0, low=60700.0)
    na.step_all()
    assert only(key)["state"] == "SKIPPED" and not bot.placed


def test_only_the_chosen_indices_and_only_with_both_switches_on(bot):
    nr7._insert(nr7.new_setup("SENSEX", pd.Timestamp("2026-11-13").date(), 81880, 81289))
    na.step_all()
    assert na.all_trades() == []                                      # Sensex isn't traded
    state.set_setting(broker.TRADING_KEY, {"enabled": False})
    key = bank_setup()
    na.step_all()
    assert only(key) is None                                          # Auto-trade off: not even watched
    state.set_setting(broker.TRADING_KEY, {"enabled": True})
    na.step_all()
    nr7.save_settings({"trade": False})
    na.step_all()
    assert only(key)["state"] == "CANCELLED"


def test_an_open_option_is_still_sold_after_trading_is_switched_off(bot):
    key = bank_setup()
    move(bot, ltp=61350.0, high=61360.0)
    na.step_all()
    bot.fill(bot.placed[-1]["oid"], 30, 1500.0)
    na.step_all()
    nr7.save_settings({"trade": False})
    move(bot, ltp=60700.0, low=60700.0)
    na.step_all()
    assert only(key)["state"] == "SELLING"


def test_unfilled_buy_is_repriced_after_a_minute(bot, monkeypatch):
    key = bank_setup()
    move(bot, ltp=61350.0, high=61360.0)
    na.step_all()
    first = bot.placed[-1]["oid"]
    monkeypatch.setattr(na, "_age", lambda ts: 61)
    na.step_all()                                                      # cancelled, back to waiting
    assert first in bot.cancelled and only(key)["state"] == "WAIT" and only(key)["side"] == "LONG"
    bot.option.update(ask=1510.0)
    na.step_all()
    assert only(key)["state"] == "BUYING" and bot.placed[-1]["price"] == "1585.50"


def test_day_five_exit_at_1515(bot):
    key = bank_setup()
    move(bot, ltp=61350.0, high=61360.0)
    na.step_all()
    bot.fill(bot.placed[-1]["oid"], 30, 1500.0)
    na.step_all()
    bot.mkt.at = datetime(2026, 11, 20, 15, 10, tzinfo=IST)            # Friday = day 5
    na.step_all()
    assert only(key)["state"] == "OPEN"
    bot.mkt.at = datetime(2026, 11, 20, 15, 15, tzinfo=IST)
    na.step_all()
    assert only(key)["state"] == "SELLING" and only(key)["exit_why"] == "day 5 exit"


def test_a_lot_closed_by_hand_in_kotak_is_not_sold_again(bot):
    key = bank_setup()
    move(bot, ltp=61350.0, high=61360.0)
    na.step_all()
    bot.fill(bot.placed[-1]["oid"], 30, 1500.0)
    na.step_all()
    bot.net[only(key)["tsym"]] = 0
    n = len(bot.placed)
    move(bot, ltp=60700.0, low=60700.0)
    na.step_all()
    assert only(key)["state"] == "CLOSED" and len(bot.placed) == n and "no open position" in only(key)["note"]


def test_the_entry_day_passing_cancels_a_waiting_trade(bot):
    key = bank_setup()
    na.step_all()
    with nr7._conn() as con:
        con.execute("UPDATE nr7_setups SET trigger_day = '2026-11-16', status = 'EXPIRED' WHERE key = ?", (key,))
    bot.mkt.at = datetime(2026, 11, 17, 9, 20, tzinfo=IST)
    na.step_all()
    assert only(key)["state"] == "CANCELLED" and not bot.placed


def test_only_one_dashboard_runs_the_bot(bot, monkeypatch):
    state.set_setting(na.OWNER_KEY, {"pid": "999999", "at": na._now()})
    assert na._claim() is False
    monkeypatch.setattr(na, "_age", lambda ts: na.OWNER_STALE_SEC + 1)
    assert na._claim() is True


def test_close_from_the_dashboard_sells_now(bot):
    key = bank_setup()
    move(bot, ltp=61350.0, high=61360.0)
    na.step_all()
    bot.fill(bot.placed[-1]["oid"], 30, 1500.0)
    na.step_all()
    c = web.app.test_client()
    j = c.post("/api/nr7/auto/close", json={"key": key}).get_json()
    assert only(key)["state"] == "SELLING" and j["auto"]["trades"][0]["state"] == "SELLING"


def test_trade_switch_needs_kotak_credentials(monkeypatch):
    monkeypatch.setattr(broker, "missing_credentials", lambda: ["KOTAK_MPIN"])
    r = web.app.test_client().post("/api/nr7/settings", json={"trade": True})
    assert r.status_code == 400 and "KOTAK_MPIN" in r.get_json()["error"] and nr7.settings()["trade"] is False
