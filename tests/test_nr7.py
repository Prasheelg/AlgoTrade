"""The NR7 index breakout (algotrade/nr7.py): the pattern, the trade rules on daily and minute bars, the evening
scan, the live check, the backtest and the dashboard API."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from algotrade import nr7, telegram_bot, web

IST = ZoneInfo("Asia/Kolkata")


def bars(rows, start="2026-10-20", freq="B"):
    """rows: (open, high, low, close) per bar."""
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)


def daily(ranges, start="2026-10-12"):
    """Daily bars centred on 100 with these high-low ranges."""
    return bars([(100, 100 + r / 2, 100 - r / 2, 100) for r in ranges], start)


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda text: out.append(text) or True)
    nr7.save_settings({"send": True})
    return out


# ---- the pattern ------------------------------------------------------------------------------------

@pytest.mark.parametrize("ranges,expected", [
    ([10, 9, 8, 7, 6, 5, 4], True),
    ([10, 9, 8, 7, 6, 5, 5], True),          # a tie counts
    ([10, 9, 8, 7, 6, 3, 4], False),         # a narrower day inside the window
    ([3, 9, 8, 7, 6, 5, 4], False),          # 6 bars back is still in the window
    ([3, 9, 8, 7, 6, 5, 4, 4][1:], True),    # 7 bars back is out of it
    ([9, 8, 7, 6, 5, 4], False),             # fewer than 7 bars
])
def test_is_nr7(ranges, expected):
    assert nr7.is_nr7(daily(ranges)) is expected


# ---- trade rules ------------------------------------------------------------------------------------

def setup(high=110.0, low=100.0, day="2026-10-19"):
    return nr7.new_setup("NIFTY", pd.Timestamp(day).date(), high, low)


def test_long_hits_the_target_after_the_entry_day():
    t = nr7.advance(setup(), bars([(105, 112, 104, 111), (111, 130, 110, 129), (129, 145, 128, 140)]), daily=True)
    assert (t["status"], t["side"], t["entry"], t["stop"], t["target"]) == ("TARGET", "LONG", 110, 100, 140)
    assert t["r"] == 3.0 and t["exit_at"].startswith("2026-10-22") and t["trigger_day"] == "2026-10-20"


def test_short_is_stopped_at_the_nr7_high():
    t = nr7.advance(setup(), bars([(105, 106, 98, 99), (99, 111, 97, 108)]), daily=True)
    assert (t["status"], t["side"], t["entry"], t["exit"], t["r"]) == ("STOP", "SHORT", 100, 110, -1.0)


def test_the_target_is_not_reached_in_the_entry_bar():
    t = nr7.advance(setup(), bars([(105, 150, 104, 149)]), daily=True)
    assert t["status"] == "OPEN" and t["day_no"] == 1


def test_both_levels_in_the_entry_bar_is_a_stop_out():
    t = nr7.advance(setup(), bars([(108, 112, 98, 105)]), daily=True)   # opened nearer the high
    assert (t["status"], t["side"], t["r"]) == ("STOP", "LONG", -1.0)


def test_a_gap_past_the_level_fills_at_the_open_and_a_gap_past_the_stop_exits_at_the_open():
    t = nr7.advance(setup(), bars([(115, 118, 113, 116), (95, 96, 90, 92)]), daily=True)
    assert (t["side"], t["entry"], t["target"]) == ("LONG", 115, 145)
    assert (t["status"], t["exit"], t["r"]) == ("STOP", 95, -2.0)


def test_time_exit_at_the_close_of_day_five():
    rows = [(105, 112, 104, 111)] + [(111, 115, 108, 112)] * 3 + [(112, 116, 109, 113)]
    t = nr7.advance(setup(), bars(rows), daily=True)
    assert (t["status"], t["day_no"], t["exit"], t["r"]) == ("TIME", 5, 113, 0.3)


def test_expires_when_the_next_session_trades_neither_level():
    t = nr7.advance(setup(), bars([(105, 109, 101, 106), (106, 120, 104, 118)]), daily=True)
    assert (t["status"], t["side"], t["trigger_day"]) == ("EXPIRED", None, "2026-10-20")


def test_minute_bars_count_days_across_calls_and_exit_at_1515():
    t = setup()
    day1 = bars([(105, 111, 104, 111), (111, 112, 110, 111)], "2026-10-20 09:15", "min")
    t = nr7.advance(t, day1)
    assert t["status"] == "OPEN" and t["day_no"] == 1
    for d in ("2026-10-21", "2026-10-22", "2026-10-23"):
        t = nr7.advance(t, bars([(111, 112, 110, 111)], f"{d} 10:00", "min"))
    assert t["day_no"] == 4 and t["status"] == "OPEN"
    t = nr7.advance(t, bars([(111, 112, 110, 111), (112, 113, 111, 112)], "2026-10-26 15:14", "min"))
    assert (t["status"], t["day_no"], t["exit"], t["exit_at"]) == ("TIME", 5, 112, "2026-10-26T15:15:00")


# ---- evening scan -----------------------------------------------------------------------------------

def nr7_data(nifty_last_range=4, day="2026-10-27"):
    """Seven daily bars ending on `day`: Nifty's last range as given, the others not an NR7."""
    start = (pd.Timestamp(day) - pd.offsets.BDay(6)).strftime("%Y-%m-%d")
    return {"NIFTY": daily([10, 9, 8, 7, 6, 5, nifty_last_range], start),
            "BANKNIFTY": daily([5, 9, 8, 7, 6, 5, 9], start), "SENSEX": daily([5, 9, 8, 7, 6, 5, 9], start)}


def at(hh, mm, day="2026-10-27"):
    return datetime.fromisoformat(f"{day}T{hh:02d}:{mm:02d}").replace(tzinfo=IST)


def test_scan_creates_a_setup_once_after_the_close(monkeypatch, sent):
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": nr7_data())
    monkeypatch.setattr(nr7, "_lot", lambda idx: 65)
    assert nr7.scan(at(15, 0)) == []                       # before SCAN_AFTER
    made = nr7.scan(at(16, 5))
    assert [t["key"] for t in made] == ["NIFTY|2026-10-27"]
    t = nr7.setups()[0]
    assert (t["status"], t["high"], t["low"], t["risk"], t["lot"]) == ("WAIT", 102, 98, 4, 65)
    assert t["lots"] == 3                                   # ₹1,00,000 × 1% ÷ (4 pts × 65)
    assert "Buy above 102.00" in sent[-1] and "Sell below 98.00" in sent[-1]
    assert nr7.scan(at(18, 0)) == []                        # once a day


def test_scan_waits_for_todays_candle_and_skips_event_days_and_busy_indices(monkeypatch, sent):
    monkeypatch.setattr(nr7, "_lot", lambda idx: 65)
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": nr7_data(day="2026-10-26"))
    assert nr7.scan(at(16, 5)) == [] and nr7.setups() == []   # only yesterday's candle: try again later
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": nr7_data())
    nr7.save_settings({"skip_days": "2026-10-28"})
    t = nr7.scan(at(16, 6))[0]
    assert (t["status"], t["note"]) == ("SKIPPED", "2026-10-28 is a skip day")
    assert "skipped" in sent[-1]


def test_scan_skips_an_index_that_already_has_a_trade(monkeypatch, sent):
    monkeypatch.setattr(nr7, "_lot", lambda idx: 65)
    nr7._insert({**setup(day="2026-10-23"), "status": "OPEN", "side": "LONG", "entry": 110})
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": nr7_data())
    assert nr7.scan(at(16, 5))[0]["status"] == "SKIPPED"


def test_scan_does_nothing_when_off(monkeypatch):
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": pytest.fail("must not fetch"))
    nr7.save_settings({"on": False})
    assert nr7.scan(at(16, 5)) == []


# ---- the live check ---------------------------------------------------------------------------------

def test_check_enters_then_exits_and_alerts_once(sent):
    nr7._insert({**setup(day="2026-10-26"), "lots": 1, "lot": 65})
    minute = bars([(105, 109, 104, 108), (108, 111, 107, 110.5), (110.5, 112, 110, 111)], "2026-10-27 09:15", "min")
    assert nr7.check(at(9, 20), bars_for=lambda idx: minute) == 1
    t = nr7.setups()[0]
    assert (t["status"], t["side"], t["entry"], t["last_bar"]) == ("OPEN", "LONG", 110, "2026-10-27T09:17:00")
    assert "LONG</b> at 110.00" in sent[-1]
    assert nr7.check(at(9, 21), bars_for=lambda idx: minute) == 0     # nothing new: no second alert
    more = pd.concat([minute, bars([(111, 141, 111, 140)], "2026-10-27 09:21", "min")])
    nr7.check(at(9, 23), bars_for=lambda idx: more)
    t = nr7.setups()[0]
    assert (t["status"], t["exit"], t["r"]) == ("TARGET", 140, 3.0) and "target hit" in sent[-1]


def test_check_ignores_the_unfinished_minute_and_expires_after_the_session(sent):
    nr7._insert(setup(day="2026-10-26"))
    minute = bars([(105, 109, 104, 108), (108, 120, 107, 119)], "2026-10-27 09:15", "min")
    nr7.check(at(9, 16), bars_for=lambda idx: minute)            # the 09:16 bar is still forming
    assert nr7.setups()[0]["status"] == "WAIT"
    quiet = bars([(105, 109, 104, 108)], "2026-10-27 09:15", "min")
    nr7.check(at(15, 45), bars_for=lambda idx: quiet)
    t = nr7.setups()[0]
    assert (t["status"], t["note"]) == ("EXPIRED", "Neither level traded") and "expired" in sent[-1]


def test_alerts_stay_quiet_when_send_is_off(monkeypatch):
    out = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda text: out.append(text) or True)
    nr7._insert(setup(day="2026-10-26"))
    nr7.check(at(9, 20), bars_for=lambda idx: bars([(105, 111, 104, 111)], "2026-10-27 09:15", "min"))
    assert nr7.setups()[0]["status"] == "OPEN" and out == []


# ---- backtest ---------------------------------------------------------------------------------------

def test_backtest_takes_one_trade_at_a_time_and_charges_costs():
    rows = [(100, 105, 95, 100)] * 6 + [(100, 101, 99, 100)]            # NR7 on bar 6 (range 2)
    rows += [(100, 102, 99.5, 101.5), (101.5, 101.6, 101.4, 101.5)]     # long at 101; bar 8 is an NR7 but busy
    rows += [(101.5, 108, 101, 107)]                                     # target 107 (3 × 2 from 101)
    tr = nr7.backtest_index(bars(rows, "2026-01-01"), "NIFTY")
    assert len(tr) == 1 and tr[0]["status"] == "TARGET"
    assert tr[0]["r"] == pytest.approx(3 - 0.017 / 100 * 101 / 2, abs=1e-3)
    s = nr7.summarize(tr)
    assert s["trades"] == 1 and s["win_pct"] == 100 and s["exits"] == {"TARGET": 1}


# ---- API --------------------------------------------------------------------------------------------

def test_api_view_settings_and_access():
    c = web.app.test_client()
    j = c.get("/api/nr7").get_json()
    assert (j["on"], j["send"], j["skip_days"], j["setups"]) == (True, False, [], [])
    assert j["rules"] == {"nr_days": 7, "target_r": 3.0, "hold_days": 5, "exit_at": "15:15", "scan_after": "16:00"}
    j = c.post("/api/nr7/settings", json={"send": True, "skip_days": ["2027-02-01", "2027-01-31"]}).get_json()
    assert j["send"] is True and j["skip_days"] == ["2027-01-31", "2027-02-01"]
    assert c.post("/api/nr7/settings", json={"skip_days": "1st Feb"}).status_code == 400
    assert c.post("/api/nr7/settings", json={"on": "yes"}).status_code == 400
    r = c.post("/api/nr7/settings", json={"on": False}, environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert r.status_code in (302, 401, 403) and nr7.settings()["on"] is True
