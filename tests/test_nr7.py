"""The NR7 index breakout (algotrade/nr7.py): the pattern, the trade rules on daily and minute bars, the evening
scan, the live check, the backtest and the dashboard API."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from algotrade import nr7, state, telegram_bot, web

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
    # as in the playbook, 1R after a gap entry is entry to stop (15), and the target is 3 of those from the entry
    assert (t["side"], t["entry"], t["target"], nr7.trade_risk(t)) == ("LONG", 115, 160, 15)
    assert (t["status"], t["exit"], t["r"]) == ("STOP", 95, -1.33)


def test_matches_the_playbooks_winner_example():
    """NR7_Breakout_Playbook.html's Nifty winner: NR7 2025-03-19, gap entry at the 03-20 open, target gapped
    through at the 03-25 open; net 3.11R after costs."""
    nr7_bar = [(22874.95, 22940.7, 22807.95, 22907.6)]
    after = [(23036.6, 23216.7, 22973.95, 23190.65), (23168.25, 23402.7, 23132.8, 23350.4),
             (23515.4, 23708.75, 23433.5, 23658.35), (23751.5, 23869.6, 23601.4, 23668.65)]
    wide = [(23000, 23400, 22600, 23000)] * 6
    df = bars(wide + nr7_bar + after, "2025-03-11")
    df.index = list(df.index[:7]) + list(pd.to_datetime(["2025-03-20", "2025-03-21", "2025-03-24", "2025-03-25"]))
    t = nr7.backtest_index(df, "NIFTY")[0]
    assert (t["entry"], t["stop"], t["target"], t["exit"], t["status"]) == (23036.6, 22807.95, 23722.55, 23751.5, "TARGET")
    assert t["r"] == pytest.approx(3.11, abs=0.005)


def test_an_nr7_on_the_day_a_trade_closed_is_skipped():
    rows = [(100, 105, 95, 100)] * 6 + [(100, 101, 99, 100)]       # NR7: 101 / 99 (range 2)
    rows += [(100, 102, 99.5, 101.5)]                                # long at 101
    rows += [(99.8, 99.9, 98.5, 98.8)]                               # stopped at 99, and itself an NR7 (range 1.4)
    got = [(t["nr7_date"], t["status"], t["note"]) for t in nr7._walk(bars(rows, "2026-01-01"), "NIFTY")]
    assert got == [("2026-01-09", "STOP", None), ("2026-01-13", "SKIPPED", "already a trade on this index")]


def test_muhurat_and_short_weekend_sessions_are_dropped():
    idx = pd.to_datetime(["2024-10-31", "2024-11-01", "2024-11-02", "2025-01-31", "2025-02-01", "2025-02-03"])
    df = pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": 1.0, "Close": 1.5}, index=idx)
    assert [str(d.date()) for d in nr7.sessions(df).index] == ["2024-10-31", "2025-01-31", "2025-02-01", "2025-02-03"]


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


def test_scan_skips_an_index_whose_trade_closed_that_day(monkeypatch, sent):
    monkeypatch.setattr(nr7, "_lot", lambda idx: 65)
    nr7._insert({**setup(day="2026-10-20"), "status": "TIME", "side": "LONG", "entry": 110,
                 "exit": 112, "exit_at": "2026-10-27T15:15:00", "r": 0.2})
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": nr7_data())
    t = nr7.scan(at(16, 5))[0]
    assert (t["status"], t["note"]) == ("SKIPPED", "a trade on this index closed that day")


def test_scan_runs_after_a_full_weekend_session_but_not_after_muhurat(monkeypatch, sent):
    monkeypatch.setattr(nr7, "_lot", lambda idx: 65)
    data = nr7_data(day="2026-02-02")
    for df in data.values():                                 # the last session is the Budget Sunday, 2026-02-01
        df.index = list(df.index[:-1]) + [pd.Timestamp("2026-02-01")]
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": data)
    assert nr7.scan(at(16, 5, "2026-11-08")) == []          # Muhurat Sunday: no scan
    assert nr7.scan(at(16, 5, "2026-02-01"))[0]["nr7_date"] == "2026-02-01"   # Budget Sunday, a full session


def test_a_missed_evening_scan_is_caught_up_before_the_next_session(monkeypatch, sent):
    monkeypatch.setattr(nr7, "_lot", lambda idx: 65)
    friday = nr7_data(day="2026-11-13")
    monday = {k: pd.concat([df, df.iloc[[-1]].set_axis([pd.Timestamp("2026-11-16")])]) for k, df in friday.items()}
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": monday)   # Monday's bar so far is ignored
    made = nr7.scan(at(9, 0, "2026-11-16"))
    assert [t["key"] for t in made] == ["NIFTY|2026-11-13"] and made[0]["status"] == "WAIT"
    assert nr7.scan(at(9, 5, "2026-11-16")) == []                    # once
    assert state.get_setting(nr7.LAST_SCAN_KEY, None)[0] == "2026-11-13"


def test_scan_skips_a_setup_the_price_history_says_is_busy(monkeypatch, sent):
    monkeypatch.setattr(nr7, "_lot", lambda idx: 65)
    rows = [(100, 105, 95, 100)] * 6 + [(100, 101, 99, 100)]       # NR7 on 2026-10-26 (bar 6)
    rows += [(100, 102, 99.5, 101.5)]                                # long at 101 on the 27th, still open
    rows += [(101.5, 101.6, 101.4, 101.5)]                           # 28th: another NR7 while the trade is open
    data = {"NIFTY": bars(rows, "2026-10-16"), "BANKNIFTY": daily([5, 9, 8, 7, 6, 5, 9], "2026-10-20"),
            "SENSEX": daily([5, 9, 8, 7, 6, 5, 9], "2026-10-20")}
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": data)
    t = nr7.scan(at(16, 5, "2026-10-28"))[0]
    assert (t["key"], t["status"]) == ("NIFTY|2026-10-28", "SKIPPED") and "price history" in t["note"]


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


# ---- past trades (history) --------------------------------------------------------------------------

def _history_bars():
    rows = [(100, 105, 95, 100)] * 6 + [(100, 101, 99, 100)]            # bar 6: NR7, 101 / 99
    rows += [(100, 102, 99.5, 101.5), (101.5, 101.6, 101.4, 101.5)]     # long at 101; bar 8: NR7 while busy
    rows += [(101.5, 108, 101, 107)]                                     # target 107
    rows += [(107, 107.1, 107, 107.05), (107.05, 107.08, 107.01, 107.05)]   # NR7 not traded next day; NR7 again
    return bars(rows, "2026-01-01")                                      # last bar Fri 2026-01-16


def test_walk_marks_busy_setups_skipped_and_untraded_ones_expired():
    got = [(t["nr7_date"], t["status"]) for t in nr7._walk(_history_bars(), "NIFTY")]
    assert got == [("2026-01-09", "TARGET"), ("2026-01-13", "SKIPPED"), ("2026-01-15", "EXPIRED"),
                   ("2026-01-16", "WAIT")]
    skip = [t["status"] for t in nr7._walk(_history_bars(), "NIFTY", skip_days={"2026-01-12"})]
    assert skip[0] == "SKIPPED"


def test_history_counts_wins_losses_and_rupees_per_lot():
    h = nr7.history(3, now=datetime(2026, 1, 17, 10, 0, tzinfo=IST), data={"NIFTY": _history_bars()})
    n = h["indices"]["NIFTY"]
    assert (n["trades"], n["wins"], n["losses"], n["setups"]) == (1, 1, 0, 4)
    assert (n["expired"], n["skipped"], n["waiting"], n["open"]) == (1, 1, 1, 0)
    win = next(t for t in h["trades"] if t["status"] == "TARGET")
    assert win["net_pts"] == pytest.approx(6 - 0.017 / 100 * 101, abs=0.01) and win["days"] == 3
    assert win["rs_per_lot"] == round(win["net_pts"] * 65) and n["rs_per_lot"] == win["rs_per_lot"]
    assert win["lots"] == 1000 // 130 and win["rs_sized"] == win["rs_per_lot"] * win["lots"]
    assert h["total"]["trades"] == 1 and h["by_month"] == {"2026-01": {"trades": 1, "wins": 1, "r": round(win["r"], 2),
                                                                      "rs_per_lot": win["rs_per_lot"]}}
    assert [t["nr7_date"] for t in h["trades"]] == sorted((t["nr7_date"] for t in h["trades"]), reverse=True)
    h = nr7.history(1, now=datetime(2026, 3, 20, 10, 0, tzinfo=IST), data={"NIFTY": _history_bars()})
    assert h["trades"] == [] and h["total"]["trades"] == 0


def test_api_history(monkeypatch):
    monkeypatch.setattr(nr7, "_daily", lambda period="1y": {"NIFTY": _history_bars()})
    nr7._history_cache.clear()
    c = web.app.test_client()
    assert c.get("/api/nr7/history?months=three").status_code == 400
    j = c.get("/api/nr7/history?months=3").get_json()
    assert j["months"] == 3 and "NIFTY" in j["indices"] and "total" in j
    nr7._history_cache.clear()
