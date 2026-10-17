"""P2: tracker.fill_price / _walk / _bars_after / update_all (characterization)."""
import pandas as pd
import pytest

from algotrade import config, state, telegram_bot, tracker
from algotrade.tracker import _bars_after, _walk, fill_price
from conftest import FakeFetch, Recorder, bars, make_signal, sig_row


def B(o, h, l):
    return {"Open": o, "High": h, "Low": l}


# ---- fill_price ----------------------------------------------------------------------------

@pytest.mark.parametrize("side,etype,bar,expected", [
    # limit BUY at 100: fills when price comes down to it; gap down fills at the open
    ("BUY", "limit", B(102, 103, 101), None),
    ("BUY", "limit", B(102, 103, 100), 100),
    ("BUY", "limit", B(102, 103, 99.5), 100),
    ("BUY", "limit", B(98, 99, 97), 98),
    # limit SELL at 100
    ("SELL", "limit", B(98, 99, 97), None),
    ("SELL", "limit", B(98, 100.5, 97), 100),
    ("SELL", "limit", B(102, 103, 101), 102),
    # stop BUY at 100
    ("BUY", "stop", B(98, 99, 97), None),
    ("BUY", "stop", B(98, 101, 97), 100),
    ("BUY", "stop", B(102, 103, 101), 102),
    # stop SELL at 100
    ("SELL", "stop", B(102, 103, 101), None),
    ("SELL", "stop", B(102, 103, 99), 100),
    ("SELL", "stop", B(98, 99, 97), 98),
    # anything that is not "limit" is treated as a stop order
    ("BUY", "market", B(102, 103, 101), 102),
    ("BUY", "market", B(98, 99, 97), None),
])
def test_fill_price(side, etype, bar, expected):
    assert fill_price(side, etype, 100.0, bar) == expected


def test_fill_price_accepts_series():
    assert fill_price("BUY", "limit", 100.0, pd.Series(B(98, 99, 97))) == 98.0


# ---- _walk: daily ---------------------------------------------------------------------------
# sig_row defaults: BUY limit entry 99, SL 96, T1 103.5, T2 108, signal bar 2024-01-01

QUIET = (100, 101, 99.5, 100)  # never reaches entry 99, SL, or targets


def days(*rows, start="2024-01-02"):
    idx = pd.bdate_range(start, periods=len(rows))
    return bars([(ts, *r) for ts, r in zip(idx, rows)])


def test_pending_cancelled_after_entry_valid_bars():
    out = _walk(sig_row(), days(QUIET, QUIET, QUIET, QUIET))
    assert out == {"status": "CANCELLED", "exit_price": None, "closed_at": "2024-01-04",
                   "fill_price": None, "filled_at": None}


def test_pending_stays_pending_before_limit():
    out = _walk(sig_row(), days(QUIET, QUIET))
    assert out == {"status": "PENDING", "exit_price": None, "closed_at": None, "fill_price": None,
                   "filled_at": None}


def test_no_bars_returns_initial_state():
    assert _walk(sig_row(), days())["status"] == "PENDING"
    assert _walk(sig_row(entry_type="market"), days())["fill_price"] == 99.0


def test_entry_valid_bars_read_from_config(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_VALID_BARS", 1)
    assert _walk(sig_row(), days(QUIET))["closed_at"] == "2024-01-02"


def test_fill_on_third_bar_then_active():
    out = _walk(sig_row(), days(QUIET, QUIET, (100, 101, 98.5, 100)))
    assert out == {"status": "ACTIVE", "exit_price": None, "closed_at": None, "fill_price": 99.0,
                   "filled_at": "2024-01-04T00:00:00"}


def test_fill_bar_checks_only_stop_not_targets():
    out = _walk(sig_row(), days((100, 120, 98.5, 110)))
    assert out["status"] == "ACTIVE" and out["fill_price"] == 99.0


def test_fill_bar_stop_hit_exits_at_stop():
    out = _walk(sig_row(), days((100, 100, 95, 95.5)))
    assert out == {"status": "SL_HIT", "exit_price": 96.0, "closed_at": "2024-01-02", "fill_price": 99.0,
                   "filled_at": "2024-01-02T00:00:00"}


def test_fill_bar_gap_through_stop_exits_at_fill():
    out = _walk(sig_row(), days((95, 95.5, 94, 95)))
    assert out["status"] == "SL_HIT" and out["fill_price"] == 95.0 and out["exit_price"] == 95.0


def test_gap_fill_rounds_fill_price():
    out = _walk(sig_row(), days((98.456, 99, 98, 98.6)))
    assert out["fill_price"] == 98.46


def test_sl_before_target_in_same_bar():
    out = _walk(sig_row(), days((100, 101, 98.5, 100), (100, 110, 95, 100)))
    assert out["status"] == "SL_HIT" and out["exit_price"] == 96.0 and out["closed_at"] == "2024-01-03"


def test_t2_checked_before_t1():
    out = _walk(sig_row(), days((100, 101, 98.5, 100), (100, 108, 99.5, 107)))
    assert out["status"] == "T2_HIT" and out["exit_price"] == 108.0


def test_t1_then_breakeven_at_fill_price():
    fill = (100, 101, 98.5, 100)
    t1 = (100, 104, 99.5, 103)
    back = (101, 101.5, 98.8, 99)
    out = _walk(sig_row(), days(fill, t1))
    assert out["status"] == "T1_HIT" and out["exit_price"] is None
    out = _walk(sig_row(), days(fill, t1, back))
    assert out == {"status": "BREAKEVEN", "exit_price": 99.0, "closed_at": "2024-01-04", "fill_price": 99.0,
                   "filled_at": "2024-01-02T00:00:00"}


def test_breakeven_uses_gap_fill_price():
    out = _walk(sig_row(), days((98.5, 99, 98.2, 98.8), (99, 104, 98.9, 103), (101, 101, 98.4, 98.6)))
    assert out["status"] == "BREAKEVEN" and out["exit_price"] == 98.5


def test_t1_then_t2():
    out = _walk(sig_row(), days((100, 101, 98.5, 100), (100, 104, 99.5, 103), (103, 109, 102, 108)))
    assert out["status"] == "T2_HIT" and out["exit_price"] == 108.0


def test_daily_expiry_after_20_held_bars_fill_bar_not_counted():
    fill = (100, 101, 98.5, 100)
    rows = [fill] + [QUIET] * 19
    assert _walk(sig_row(), days(*rows))["status"] == "ACTIVE"
    rows = [fill] + [QUIET] * 19 + [(100, 101, 99.5, 100.7)]
    out = _walk(sig_row(), days(*rows))
    assert out["status"] == "EXPIRED" and out["exit_price"] == 100.7
    assert out["closed_at"] == str(days(*rows).index[-1].date())


def test_expired_after_t1_keeps_running():
    rows = [(100, 101, 98.5, 100), (100, 104, 99.5, 103)] + [(103, 103.2, 102, 103)] * 19
    out = _walk(sig_row(), days(*rows))
    assert out["status"] == "EXPIRED" and out["exit_price"] == 103.0


def test_market_rows_start_active_and_count_every_bar():
    """Quirk 3 rows: entry_type 'market' starts ACTIVE with fill = entry."""
    s = sig_row(entry_type="market", entry=100.0)
    out = _walk(s, days(*[QUIET] * 19))
    assert out == {"status": "ACTIVE", "exit_price": None, "closed_at": None, "fill_price": 100.0,
                   "filled_at": None}
    out = _walk(s, days(*[QUIET] * 20))
    assert out["status"] == "EXPIRED"
    assert _walk(sig_row(entry_type=None, entry=100.0), days(QUIET))["fill_price"] == 100.0


def test_sell_mirror(monkeypatch):
    s = sig_row(side="SELL", entry=101.0, stop_loss=104.0, target1=96.5, target2=92.0)
    fill = (100, 101.5, 99.5, 100)
    assert _walk(s, days(fill))["fill_price"] == 101.0
    out = _walk(s, days(fill, (100, 100.5, 96, 97), (98, 101.2, 97, 101)))
    assert out["status"] == "BREAKEVEN" and out["exit_price"] == 101.0
    out = _walk(s, days(fill, (100, 105, 91, 100)))
    assert out["status"] == "SL_HIT" and out["exit_price"] == 104.0
    out = _walk(s, days((103, 103.5, 102, 103)))
    assert out["status"] == "ACTIVE" and out["fill_price"] == 103.0  # gap fill at the open
    out = _walk(s, days((105, 106, 104.5, 105)))
    assert out["status"] == "SL_HIT" and out["exit_price"] == 105.0  # gapped through the stop


def test_bar_time_preferred_over_bar_date_for_entry_day():
    # entry_day only matters intraday; bar_time None falls back to bar_date
    s = sig_row(bar_time=None, bar_date="2024-01-01", timeframe="15m")
    b = bars([("2024-01-02 09:15", 100, 101, 99.5, 100)])
    out = _walk(s, b, intraday=True)
    assert out["status"] == "CANCELLED"


# ---- _walk: intraday ------------------------------------------------------------------------

ISIG = dict(timeframe="15m", bar_time="2024-01-01T10:00:00", bar_date="2024-01-01")


def test_intraday_pending_cancelled_on_next_day_bar():
    b = bars([("2024-01-01 10:15", *QUIET), ("2024-01-01 10:30", *QUIET), ("2024-01-02 09:15", *QUIET)])
    out = _walk(sig_row(**ISIG), b, intraday=True)
    assert out == {"status": "CANCELLED", "exit_price": None, "closed_at": "2024-01-01", "fill_price": None,
                   "filled_at": None}


def test_intraday_pending_cancelled_by_entry_valid_bars_same_day():
    b = bars([(f"2024-01-01 10:{m}", *QUIET) for m in ("15", "30", "45")])
    out = _walk(sig_row(**ISIG), b, intraday=True)
    assert out["status"] == "CANCELLED" and out["closed_at"] == "2024-01-01"


def test_intraday_active_expires_at_previous_close_on_next_day():
    """Quirk 13: closed_at is a date only while filled_at is a full timestamp."""
    b = bars([("2024-01-01 10:15", 100, 101, 98.5, 100), ("2024-01-01 15:15", 100, 101, 99.5, 100.4),
              ("2024-01-02 09:15", 102, 103, 101, 102)])
    out = _walk(sig_row(**ISIG), b, intraday=True)
    assert out == {"status": "EXPIRED", "exit_price": 100.4, "closed_at": "2024-01-01", "fill_price": 99.0,
                   "filled_at": "2024-01-01T10:15:00"}


def test_intraday_no_hold_limit_and_stays_open_until_next_day_bar():
    """Quirk 10: after the close a position stays open until a next-day bar arrives."""
    ts = pd.date_range("2024-01-01 10:15", periods=30, freq="5min")
    b = bars([(ts[0], 100, 101, 98.5, 100)] + [(t, *QUIET) for t in ts[1:]])
    out = _walk(sig_row(**ISIG), b, intraday=True)
    assert out["status"] == "ACTIVE"


def test_intraday_first_bar_next_day_without_prev():
    b = bars([("2024-01-02 09:15", 102, 103, 101, 102)])
    out = _walk(sig_row(**ISIG), b, intraday=True)
    assert out["status"] == "CANCELLED" and out["closed_at"] == "2024-01-02"
    out = _walk(sig_row(**ISIG, entry_type="market", entry=100.0), b, intraday=True)
    assert out["status"] == "EXPIRED" and out["exit_price"] == 102.0 and out["closed_at"] == "2024-01-02"


# ---- _bars_after ---------------------------------------------------------------------------

def test_bars_after_daily_filters_by_date():
    df = bars([(f"2024-01-0{d}", *QUIET) for d in (1, 2, 3)])
    out = _bars_after(sig_row(bar_date="2024-01-02"), df)
    assert list(out.index.strftime("%Y-%m-%d")) == ["2024-01-03"]


def test_bars_after_intraday_filters_by_timestamp():
    df = bars([("2024-01-01 10:00", *QUIET), ("2024-01-01 10:15", *QUIET), ("2024-01-02 09:15", *QUIET)])
    out = _bars_after(sig_row(**ISIG), df)
    assert list(out.index.strftime("%m-%d %H:%M")) == ["01-01 10:15", "01-02 09:15"]


# ---- update_all ----------------------------------------------------------------------------

def daily_frame(rows, start="2024-01-02"):
    df = days(*rows, start=start)
    df["Volume"] = 1000.0
    return df


@pytest.fixture
def tg(monkeypatch):
    rec = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", rec)
    return rec


def _row(key):
    return next(r for r in state.all_signals() if r["key"] == key)


def test_update_all_no_open_signals_does_not_fetch(monkeypatch, tg):
    fetch = FakeFetch({})
    monkeypatch.setattr(tracker, "fetch_ohlcv", fetch)
    assert tracker.update_all() == 0
    assert fetch.calls == []


def test_update_all_status_change_writes_fields_and_notifies(monkeypatch, tg):
    sig = make_signal(symbol="X", bar_time=pd.Timestamp("2024-01-01"))
    state.record([sig], telegram=True)
    frame = daily_frame([(100, 101, 98.5, 100), (100, 104, 99.5, 103.25)])
    fetch = FakeFetch({"X": frame})
    monkeypatch.setattr(tracker, "fetch_ohlcv", fetch)

    assert tracker.update_all() == 1
    assert fetch.calls == [dict(symbols=["X"], timeframe="1d", period="3mo", market="IN")]
    r = _row(sig.key)
    assert (r["status"], r["fill_price"], r["filled_at"], r["exit_price"], r["closed_at"], r["ltp"]) == \
        ("T1_HIT", 99.0, "2024-01-02T00:00:00", None, None, 103.25)
    assert len(tg.calls) == 1
    assert tg.calls[0][0][0] == ("<b>BUY X</b> (1d) update\n🎯 Target 1 hit — move stop to entry\n"
                                 "Entry ₹99.00 → ₹103.25 (+4.29%)")

    # replay is idempotent: no change, no message, ltp still updated
    assert tracker.update_all() == 0
    assert len(tg.calls) == 1


def test_update_all_unchanged_status_updates_only_ltp(monkeypatch, tg):
    sig = make_signal(symbol="X", bar_time=pd.Timestamp("2024-01-01"))
    state.record([sig])
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"X": daily_frame([QUIET, (100, 101, 99.5, 100.55)])}))
    before = _row(sig.key)
    assert tracker.update_all() == 0
    after = _row(sig.key)
    assert after["ltp"] == 100.55 and after["status"] == "PENDING"
    changed = {k for k in after if after[k] != before[k]}
    assert changed <= {"ltp", "updated_at"}
    assert tg.calls == []


def test_update_all_cancelled_message_and_us_flag(monkeypatch, tg):
    sig = make_signal(symbol="AAPL", market="US", bar_time=pd.Timestamp("2024-01-01"))
    state.record([sig])
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"AAPL": daily_frame([QUIET] * 3)}))
    assert tracker.update_all() == 1
    assert _row(sig.key)["status"] == "CANCELLED"
    assert tg.calls[0][0][0] == ("🇺🇸 <b>BUY AAPL</b> (1d) update\n❌ Entry not reached in time — order cancelled\n"
                                 "Entry $99.00 not reached · LTP $100.00")


def test_update_all_exit_price_used_in_pnl(monkeypatch, tg):
    sig = make_signal(symbol="X", bar_time=pd.Timestamp("2024-01-01"))
    state.record([sig])
    monkeypatch.setattr(tracker, "fetch_ohlcv",
                        FakeFetch({"X": daily_frame([(100, 101, 98.5, 100), (100, 101, 95, 95.5), QUIET])}))
    assert tracker.update_all() == 1
    r = _row(sig.key)
    assert (r["status"], r["exit_price"], r["closed_at"], r["ltp"]) == ("SL_HIT", 96.0, "2024-01-03", 100.0)
    assert tg.calls[0][0][0].endswith("Entry ₹99.00 → ₹96.00 (-3.03%)")


def test_update_all_telegram0_rows_never_notified(monkeypatch, tg):
    sig = make_signal(symbol="X", bar_time=pd.Timestamp("2024-01-01"))
    state.record([sig], telegram=False)
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"X": daily_frame([QUIET] * 3)}))
    assert tracker.update_all(notify=True) == 1
    assert _row(sig.key)["status"] == "CANCELLED"
    assert tg.calls == []


def test_update_all_notify_false(monkeypatch, tg):
    state.record([make_signal(symbol="X", bar_time=pd.Timestamp("2024-01-01"))], telegram=True)
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"X": daily_frame([QUIET] * 3)}))
    assert tracker.update_all(notify=False) == 1
    assert tg.calls == []


def test_update_all_market_filter_and_grouping(monkeypatch, tg):
    a = make_signal(symbol="X", bar_time=pd.Timestamp("2024-01-01"))
    b = make_signal(symbol="AAPL", market="US", bar_time=pd.Timestamp("2024-01-01"))
    c = make_signal(symbol="Y", timeframe="15m", bar_time=pd.Timestamp("2024-01-01 10:00"))
    state.record([a, b, c], telegram=False)
    fetch = FakeFetch({"X": daily_frame([QUIET] * 3), "AAPL": daily_frame([QUIET] * 3)})
    monkeypatch.setattr(tracker, "fetch_ohlcv", fetch)
    assert tracker.update_all(market="US") == 1
    assert [c["market"] for c in fetch.calls] == ["US"]
    assert _row(a.key)["status"] == "PENDING" and _row(b.key)["status"] == "CANCELLED"

    fetch.calls.clear()
    assert tracker.update_all() == 1  # X cancelled; Y (15m) has no data and is skipped
    assert [(c["market"], c["timeframe"], c["period"]) for c in fetch.calls] == \
        [("IN", "15m", "5d"), ("IN", "1d", "3mo")]   # sorted by (market, timeframe)
    assert _row(c.key)["status"] == "PENDING"


def test_update_all_missing_or_empty_data_skips_row(monkeypatch, tg):
    sig = make_signal(symbol="X", bar_time=pd.Timestamp("2024-01-01"))
    state.record([sig])
    before = _row(sig.key)
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"X": daily_frame([QUIET]).iloc[:0]}))
    assert tracker.update_all() == 0
    assert _row(sig.key) == before


def test_track_period_constants():
    assert tracker.TRACK_PERIOD == {"1d": "3mo", "1h": "1mo", "30m": "1mo", "15m": "5d", "5m": "5d"}
    assert tracker.MAX_HOLD_BARS == 20 and config.ENTRY_VALID_BARS == 3


def test_update_all_sees_forming_daily_bar_during_session(monkeypatch, tg):
    """Quirk 1 fix is scan-only: during the session the tracker still uses today's forming bar (fill + LTP)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from algotrade import markets
    monkeypatch.setattr(markets.Market, "now", lambda self: datetime(2024, 1, 3, 11, 0, tzinfo=ZoneInfo(self.tz)))
    sig = make_signal(symbol="X", bar_time=pd.Timestamp("2024-01-01"))
    state.record([sig])
    # 2024-01-03 is "today" and still forming: it reaches the limit entry 99
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"X": daily_frame([QUIET, (100, 100.5, 98.8, 99.4)])}))
    assert tracker.update_all(notify=False) == 1
    r = _row(sig.key)
    assert (r["status"], r["filled_at"], r["ltp"]) == ("ACTIVE", "2024-01-03T00:00:00", 99.4)
