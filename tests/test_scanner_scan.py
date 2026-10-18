"""P4: scanner.scan / fetch_index / scan_and_alert with faked data, universe and Telegram."""
import logging
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from algotrade import config, markets, scanner, state, telegram_bot
from algotrade.indicators import add_all
from conftest import FakeFetch, Recorder, make_ohlcv, make_signal


def _fail(*a, **k):
    pytest.fail("should not be called")


@pytest.fixture
def frames():
    return {"AAA": make_ohlcv(300, seed=1), "BBB": make_ohlcv(300, seed=2),
            "^NSEI": make_ohlcv(300, seed=3, price=20000), "^GSPC": make_ohlcv(300, seed=4, price=5000)}


# ---- validation ------------------------------------------------------------------------------

@pytest.mark.parametrize("kw,msg", [
    (dict(strategies=["Bogus", "EMA 9/21 Cross"]), "Unknown strategies: Bogus"),
    (dict(timeframe="2h"), "Unsupported timeframe 2h; use one of 1d, 1h, 30m, 15m, 5m"),
    (dict(filters=["trend", "foo", "bar"]), "Unknown filters: bar, foo"),
])
def test_scan_validation_errors(monkeypatch, kw, msg):
    monkeypatch.setattr(scanner, "fetch_ohlcv", _fail)
    with pytest.raises(ValueError) as e:
        scanner.scan(symbols=["AAA"], **kw)
    assert str(e.value) == msg


def test_scan_validates_config_strategies(monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", ["Nope"])
    monkeypatch.setattr(scanner, "fetch_ohlcv", _fail)
    with pytest.raises(ValueError, match="Unknown strategies: Nope"):
        scanner.scan(symbols=["AAA"])


def test_scan_unknown_market_raises():
    with pytest.raises(ValueError, match="Unknown market XX"):
        scanner.scan(symbols=["AAA"], market="xx")


def test_scan_fetches_universe_before_validation(monkeypatch):
    uni = Recorder(["AAA"])
    monkeypatch.setattr(scanner, "get_universe", uni)
    with pytest.raises(ValueError):
        scanner.scan(timeframe="2h", market="us")
    assert uni.calls == [((), {"market": "US"})]


# ---- orchestration (evaluate faked) ---------------------------------------------------------

def test_scan_orchestration_sort_and_errors(monkeypatch, frames, caplog):
    fetch = FakeFetch(frames)
    monkeypatch.setattr(scanner, "fetch_ohlcv", fetch)
    monkeypatch.setattr(scanner, "get_universe", Recorder(["AAA", "BBB", "CCC", "DDD"]))
    frames["CCC"] = frames["AAA"]
    frames["DDD"] = frames["AAA"]
    seen = {}

    def fake_eval(sym, df, **kw):
        seen[sym] = (kw, df)
        if sym == "CCC":
            raise RuntimeError("boom")
        return {"AAA": make_signal(symbol="AAA", score=2, adx=10.0),
                "BBB": make_signal(symbol="BBB", score=2, adx=30.0),
                "DDD": make_signal(symbol="DDD", score=3, adx=5.0)}[sym]

    monkeypatch.setattr(scanner, "evaluate", fake_eval)
    with caplog.at_level(logging.WARNING, logger="algotrade.scanner"):
        out = scanner.scan(min_score=1, filters=["trend"])
    assert [s.symbol for s in out] == ["DDD", "BBB", "AAA"]
    assert "Error evaluating CCC: boom" in caplog.text
    assert fetch.calls == [
        dict(symbols=["AAA", "BBB", "CCC", "DDD"], timeframe="1d", period=None, market="IN"),
        dict(symbols=["^NSEI"], timeframe="1d", period=None, market="IN"),
    ]
    kw, df = seen["AAA"]
    assert kw == dict(min_score=1, strategies=None, timeframe="1d", market="IN", filters=["trend"])
    assert {"EMA200", "ATR", "IDX_CLOSE", "RS"} <= set(df.columns) and df["IDX_CLOSE"].notna().any()


def test_scan_missing_index_means_nan_index_columns(monkeypatch, frames, caplog):
    del frames["^NSEI"]
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch(frames))
    got = {}
    monkeypatch.setattr(scanner, "evaluate", lambda sym, df, **kw: got.setdefault(sym, df) and None)
    with caplog.at_level(logging.WARNING):
        assert scanner.scan(symbols=["AAA"]) == []
    assert "No ^NSEI index data; market filters are skipped" in caplog.text
    assert got["AAA"]["IDX_CLOSE"].isna().all() and got["AAA"]["RS"].isna().all()


def test_fetch_index(monkeypatch, frames):
    fetch = FakeFetch(frames)
    monkeypatch.setattr(scanner, "fetch_ohlcv", fetch)
    s = scanner.fetch_index("1h", "US", period="60d")
    assert s.equals(frames["^GSPC"]["Close"])
    assert fetch.calls == [dict(symbols=["^GSPC"], timeframe="1h", period="60d", market="US")]
    assert scanner.fetch_index("1d", "IN") is not None
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch({}))
    assert scanner.fetch_index("1d", "IN") is None


def test_scan_with_real_evaluate_on_completed_last_bar_matches_direct_pipeline(monkeypatch, frames):
    """Seeds 11/12/19 were picked because their last bar produces a signal (min_score=1, all strategies).
    Quirk 1 fixed: the last bar is evaluated only once it is complete (here the clock is after that day's close)."""
    data = {"S11": make_ohlcv(300, seed=11), "S12": make_ohlcv(300, seed=12), "S19": make_ohlcv(300, seed=19),
            "^NSEI": frames["^NSEI"]}
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch(data))
    set_exchange_clock(monkeypatch, data["S11"].index[-1].replace(hour=15, minute=31))
    monkeypatch.setattr(config, "ALLOW_SHORTS", True)
    names = list(scanner.STRATEGIES)
    got = scanner.scan(symbols=["S11", "S12", "S19"], min_score=1, strategies=names)
    idx = data["^NSEI"]["Close"]
    expected = [scanner.evaluate(s, add_all(data[s], idx), min_score=1, strategies=names)
                for s in ("S11", "S12", "S19")]
    expected = sorted(expected, key=lambda s: (s.score, s.adx), reverse=True)
    assert got == expected
    assert [(s.symbol, s.side, s.score) for s in got] == [("S11", "SELL", 3), ("S19", "BUY", 2), ("S12", "BUY", 1)]
    assert got[1].reasons == ["Support-Resistance", "Demand-Supply", "Drop-Base-Rally"]
    assert {s.bar_time for s in got} == {data["S11"].index[-1]}


def test_scan_with_real_evaluate_skips_forming_daily_bar(monkeypatch, frames):
    """Quirk 1 fixed: during the session the still-forming daily bar is dropped; scan evaluates the bar before it."""
    data = {"S11": make_ohlcv(300, seed=11), "S12": make_ohlcv(300, seed=12), "S19": make_ohlcv(300, seed=19),
            "^NSEI": frames["^NSEI"]}
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch(data))
    monkeypatch.setattr(config, "ALLOW_SHORTS", True)
    set_exchange_clock(monkeypatch, data["S11"].index[-1].replace(hour=11))
    names = list(scanner.STRATEGIES)
    got = scanner.scan(symbols=["S11", "S12", "S19"], min_score=1, strategies=names)
    idx = data["^NSEI"]["Close"]
    expected = [scanner.evaluate(s, add_all(data[s].iloc[:-1], idx), min_score=1, strategies=names)
                for s in ("S11", "S12", "S19")]
    expected = sorted([e for e in expected if e], key=lambda s: (s.score, s.adx), reverse=True)
    assert got == expected
    assert {(s.symbol, s.side, s.score) for s in got} == {("S11", "SELL", 2), ("S12", "BUY", 1), ("S19", "BUY", 1)}
    assert all(s.bar_time == data["S11"].index[-2] for s in got)
    # none of the forming-bar signals (S11 SELL 3, S19 BUY 2, S12 BUY 1) is produced
    assert not {s.bar_time for s in got} & {data["S11"].index[-1]}


# ---- completed_bars (quirk 1 fix) ---------------------------------------------------------------

def set_exchange_clock(monkeypatch, naive_ts):
    """Patch Market.now so every market reports `naive_ts` as its own exchange-local time."""
    def now(self):
        return pd.Timestamp(naive_ts).to_pydatetime().replace(tzinfo=ZoneInfo(self.tz))
    monkeypatch.setattr(markets.Market, "now", now)


def daily(last="2024-01-03", n=5):
    return pd.DataFrame({"Close": range(n)}, index=pd.bdate_range(end=last, periods=n), dtype=float)


def intraday(last, freq="15min", n=4):
    return pd.DataFrame({"Close": range(n)}, index=pd.date_range(end=last, periods=n, freq=freq), dtype=float)


T = pd.Timestamp


@pytest.mark.parametrize("market,now,dropped", [
    ("IN", "2024-01-03 08:00", True),      # before the open, a bar dated today exists: still forming
    ("IN", "2024-01-03 11:00", True),      # during the session
    ("IN", "2024-01-03 15:30:00", True),   # exactly at the close: not *after* the close yet
    ("IN", "2024-01-03 15:30:01", False),  # after the close
    ("IN", "2024-01-03 23:00", False),
    ("IN", "2024-01-04 08:00", False),     # next morning, before the open
    ("US", "2024-01-03 15:59", True),      # US uses its own session close (16:00)
    ("US", "2024-01-03 16:00", True),
    ("US", "2024-01-03 16:01", False),
])
def test_completed_bars_daily(market, now, dropped):
    df = daily("2024-01-03")
    out = scanner.completed_bars(df, "1d", market, now=T(now))
    assert out.index[-1] == (T("2024-01-02") if dropped else T("2024-01-03"))
    assert len(out) == len(df) - dropped


@pytest.mark.parametrize("now", ["2024-01-06 10:00", "2024-01-07 23:00", "2024-01-08 08:00"])
def test_completed_bars_daily_weekend_keeps_friday_bar(now):
    df = daily("2024-01-05")  # Friday
    assert scanner.completed_bars(df, "1d", "IN", now=T(now)) is df


@pytest.mark.parametrize("tf,last,now,dropped", [
    ("15m", "2024-01-03 10:00", "2024-01-03 10:14:59", True),   # in progress
    ("15m", "2024-01-03 10:00", "2024-01-03 10:15", False),     # just completed
    ("15m", "2024-01-03 10:00", "2024-01-03 10:20", False),
    ("5m", "2024-01-03 10:00", "2024-01-03 10:04", True),
    ("5m", "2024-01-03 10:00", "2024-01-03 10:05", False),
    ("30m", "2024-01-03 10:15", "2024-01-03 10:44", True),
    ("30m", "2024-01-03 10:15", "2024-01-03 10:45", False),
    ("1h", "2024-01-03 14:15", "2024-01-03 15:00", True),       # NSE 1h bars start at :15 (bar's own start)
    ("1h", "2024-01-03 14:15", "2024-01-03 15:14", True),
    ("1h", "2024-01-03 14:15", "2024-01-03 15:15", False),
    ("1h", "2024-01-03 15:15", "2024-01-03 15:29", True),       # the last NSE 1h bar is cut short by the close...
    ("1h", "2024-01-03 15:15", "2024-01-03 15:30", False),      # ...so it is complete at 15:30, not at 16:15
    ("30m", "2024-01-03 15:15", "2024-01-03 15:31", False),
    ("5m", "2024-01-03 15:40", "2024-01-03 15:42", True),       # a bar starting after the close is not capped
    ("5m", "2024-01-03 15:40", "2024-01-03 15:45", False),
])
def test_completed_bars_intraday(tf, last, now, dropped):
    df = intraday(last, freq={"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}[tf])
    out = scanner.completed_bars(df, tf, "IN", now=T(now))
    assert len(out) == len(df) - dropped and out.index[-1] == (df.index[-2] if dropped else df.index[-1])


def test_completed_bars_us_intraday_close_is_16_00():
    df = intraday("2024-01-03 15:30", freq="1h")
    assert len(scanner.completed_bars(df, "1h", "US", now=T("2024-01-03 15:59"))) == len(df) - 1
    assert len(scanner.completed_bars(df, "1h", "US", now=T("2024-01-03 16:00"))) == len(df)


def test_completed_bars_tz_aware_now_is_converted_to_exchange_time():
    df = daily("2024-01-03")
    utc_1030 = datetime(2024, 1, 3, 10, 30, tzinfo=timezone.utc)   # 16:00 IST, 05:30 ET
    assert scanner.completed_bars(df, "1d", "IN", now=utc_1030) is df
    assert len(scanner.completed_bars(df, "1d", "US", now=utc_1030)) == len(df) - 1


def test_completed_bars_defaults_to_market_clock(monkeypatch):
    df = daily("2024-01-03")
    set_exchange_clock(monkeypatch, "2024-01-03 12:00")
    assert len(scanner.completed_bars(df, "1d", "IN")) == len(df) - 1
    set_exchange_clock(monkeypatch, "2024-01-03 16:00")
    assert scanner.completed_bars(df, "1d", "IN") is df


def test_completed_bars_empty_and_input_not_mutated():
    empty = daily().iloc[:0]
    assert scanner.completed_bars(empty, "1d", "IN", now=T("2024-01-03 12:00")).empty
    df = daily("2024-01-03")
    before = df.copy()
    scanner.completed_bars(df, "1d", "IN", now=T("2024-01-03 12:00"))
    assert df.equals(before)


# ---- scan() integration of completed_bars ---------------------------------------------------------

def _spy_eval(monkeypatch):
    seen = {}
    real = scanner.evaluate

    def spy(sym, df, **kw):
        res = real(sym, df, **kw)
        seen[sym] = (df, res)
        return res
    monkeypatch.setattr(scanner, "evaluate", spy)
    return seen


@pytest.mark.parametrize("clock,expected_last", [("11:00", -2), ("16:00", -1)])
def test_scan_daily_trims_only_while_last_bar_forming(monkeypatch, frames, clock, expected_last):
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch(frames))
    seen = _spy_eval(monkeypatch)
    set_exchange_clock(monkeypatch, f"{frames['AAA'].index[-1].date()} {clock}")
    scanner.scan(symbols=["AAA", "BBB"])
    for sym in ("AAA", "BBB"):
        assert seen[sym][0].index[-1] == frames[sym].index[expected_last]


def test_scan_trim_keeps_index_columns_aligned(monkeypatch, frames):
    """The forming index bar needs no separate trim: reindexing onto the trimmed stock frame drops it, and
    IDX_EMA200 is causal, so the index columns equal those built from a trimmed index series."""
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch(frames))
    seen = _spy_eval(monkeypatch)
    set_exchange_clock(monkeypatch, f"{frames['AAA'].index[-1].date()} 11:00")
    scanner.scan(symbols=["AAA"])
    got = seen["AAA"][0]
    ref = add_all(frames["AAA"].iloc[:-1], frames["^NSEI"]["Close"].iloc[:-1])
    cols = ["IDX_CLOSE", "IDX_EMA200", "RS"]
    pd.testing.assert_frame_equal(got[cols], ref[cols])
    assert got["IDX_CLOSE"].iloc[-1] == frames["^NSEI"]["Close"].iloc[-2]


def test_scan_intraday_trims_bar_in_progress(monkeypatch):
    idx = pd.date_range("2024-01-03 09:30", periods=300, freq="15min")
    raw = {"AAA": make_ohlcv(300, seed=1).set_axis(idx), "^GSPC": make_ohlcv(300, seed=4, price=5000).set_axis(idx)}
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch(raw))
    seen = _spy_eval(monkeypatch)
    set_exchange_clock(monkeypatch, idx[-1] + pd.Timedelta("14min"))
    scanner.scan(symbols=["AAA"], timeframe="15m", market="US")
    assert seen["AAA"][0].index[-1] == idx[-2]
    set_exchange_clock(monkeypatch, idx[-1] + pd.Timedelta("15min"))
    scanner.scan(symbols=["AAA"], timeframe="15m", market="US")
    assert seen["AAA"][0].index[-1] == idx[-1]


def test_scan_trim_below_200_bars_evaluates_to_none(monkeypatch, frames):
    """201 bars with the last one forming leave 200 bars: evaluate needs i >= 200, so it returns None."""
    raw = {"AAA": make_ohlcv(201, seed=11), "^NSEI": frames["^NSEI"]}
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch(raw))
    seen = _spy_eval(monkeypatch)
    set_exchange_clock(monkeypatch, f"{raw['AAA'].index[-1].date()} 11:00")
    assert scanner.scan(symbols=["AAA"], min_score=1, strategies=list(scanner.STRATEGIES)) == []
    assert len(seen["AAA"][0]) == 200 and seen["AAA"][1] is None


def test_scan_reads_exchange_clock_of_its_market(monkeypatch, frames):
    """11:00 in New York is after the India close: the IN scan keeps the bar, the US scan drops it."""
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch(frames))
    seen = _spy_eval(monkeypatch)
    d = frames["AAA"].index[-1]
    ny = datetime(d.year, d.month, d.day, 11, 0, tzinfo=ZoneInfo("America/New_York"))
    monkeypatch.setattr(markets.Market, "now", lambda self: ny.astimezone(ZoneInfo(self.tz)))
    scanner.scan(symbols=["AAA"], market="IN")
    assert seen["AAA"][0].index[-1] == d
    scanner.scan(symbols=["AAA"], market="US")
    assert seen["AAA"][0].index[-1] == frames["AAA"].index[-2]


def test_backtest_and_tracker_never_trim_or_read_the_clock(monkeypatch, golden_series):
    """Only the live scan trims the forming bar; backtest (historical) and tracker (needs the live bar) do not."""
    from algotrade import backtest, tracker
    monkeypatch.setattr(markets.Market, "now", lambda self: pytest.fail("clock read"))
    monkeypatch.setattr(scanner, "completed_bars", lambda *a, **k: pytest.fail("completed_bars called"))
    g = golden_series["up_IN"]
    backtest.backtest_symbol("UP", g["raw"], index_close=g["index"])
    state.record([make_signal(symbol="X", bar_time=pd.Timestamp("2024-01-01"))])
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"X": make_ohlcv(5, start="2024-01-02")}))
    tracker.update_all(notify=False)


# ---- scan_and_alert ---------------------------------------------------------------------------

def sigs(*names):
    return [make_signal(symbol=n) for n in names]


def test_scan_and_alert_send_false_records_all_with_telegram0(monkeypatch):
    monkeypatch.setattr(scanner, "scan", Recorder(lambda **kw: sigs("A", "B")))
    monkeypatch.setattr(telegram_bot, "send_signals", _fail)
    out = scanner.scan_and_alert(send=False, market="IN", timeframe="1d")
    assert out == {"found": 2, "new": 2, "sent": 0, "symbols": ["BUY A", "BUY B"]}
    rows = state.all_signals()
    assert sorted(r["symbol"] for r in rows) == ["A", "B"] and {r["telegram"] for r in rows} == {0}
    assert scanner.scan.calls[0][1] == dict(market="IN", timeframe="1d")
    # dedupe across calls
    out = scanner.scan_and_alert(send=False)
    assert out == {"found": 2, "new": 0, "sent": 0, "symbols": []}


def test_scan_and_alert_send_true_records_only_delivered(monkeypatch):
    """Quirk 9: failed sends are not recorded, so they re-alert on the next scan."""
    monkeypatch.setattr(scanner, "scan", lambda **kw: sigs("A", "B", "C"))
    sender = Recorder(lambda new: [s for s in new if s.symbol != "B"])
    monkeypatch.setattr(telegram_bot, "send_signals", sender)
    out = scanner.scan_and_alert()
    assert out == {"found": 3, "new": 3, "sent": 2, "symbols": ["BUY A", "BUY B", "BUY C"]}
    rows = state.all_signals()
    assert sorted(r["symbol"] for r in rows) == ["A", "C"] and {r["telegram"] for r in rows} == {1}
    out = scanner.scan_and_alert()
    assert out == {"found": 3, "new": 1, "sent": 0, "symbols": ["BUY B"]}
    assert [s.symbol for s in sender.calls[1][0][0]] == ["B"]
    sender.result = lambda new: new  # Telegram recovers: B is finally delivered and recorded
    assert scanner.scan_and_alert() == {"found": 3, "new": 1, "sent": 1, "symbols": ["BUY B"]}
    assert len(state.all_signals()) == 3


def test_scan_and_alert_nothing_found(monkeypatch):
    monkeypatch.setattr(scanner, "scan", lambda **kw: [])
    sender = Recorder([])
    monkeypatch.setattr(telegram_bot, "send_signals", sender)
    assert scanner.scan_and_alert() == {"found": 0, "new": 0, "sent": 0, "symbols": []}
    assert sender.calls == [((([]),), {})]


# ---- fill_missing_days: Yahoo a session behind, Kotak fills it ------------------------------------

def _daily(end="2026-10-14", n=80, close=100.0):
    idx = pd.bdate_range(end=end, periods=n)
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close, "Volume": 1000.0}, index=idx)


@pytest.fixture
def kotak_gap(monkeypatch):
    from datetime import datetime
    from algotrade import kotak
    asked = []
    monkeypatch.setattr(kotak, "enabled", lambda: True)
    monkeypatch.setattr(kotak, "index_quote", lambda name: {"ltp": 22997.35, "prev_close": 22622.7,
                                                            "time": datetime(2026, 10, 15, 15, 29)})

    def quotes(syms):
        asked.append(list(syms))
        return {s: {"ltp": 110.0, "open": 101.0, "high": 112.0, "low": 100.5, "volume": 5000.0,
                    "time": datetime(2026, 10, 15, 15, 29)} for s in syms}
    monkeypatch.setattr(kotak, "quotes", quotes)
    return asked


def test_fill_missing_session_from_kotak(kotak_gap):
    data = {"A": _daily(), "B": _daily()}
    idx = _daily()["Close"] * 200
    data, idx = scanner.fill_missing_days(data, idx, "1d", "IN")
    for df in data.values():
        assert df.index[-1] == pd.Timestamp("2026-10-15")
        assert df.iloc[-1].to_dict() == {"Open": 101.0, "High": 112.0, "Low": 100.5, "Close": 110.0, "Volume": 5000.0}
    assert idx.index[-1] == pd.Timestamp("2026-10-15") and idx.iloc[-1] == 22997.35
    assert kotak_gap == [["A", "B"]]


def test_no_gap_no_quotes(kotak_gap, monkeypatch):
    data = {"A": _daily(end="2026-10-15"), "B": _daily(end="2026-10-15")}
    idx = _daily(end="2026-10-15")["Close"]
    out, idx2 = scanner.fill_missing_days(dict(data), idx, "1d", "IN")
    assert kotak_gap == [] and all(out[s] is data[s] for s in data) and idx2 is idx


def test_only_symbols_behind_are_filled(kotak_gap):
    data = {"A": _daily(end="2026-10-15"), "B": _daily(end="2026-10-14")}
    out, _ = scanner.fill_missing_days(data, None, "1d", "IN")
    assert kotak_gap == [["B"]] and out["B"].index[-1] == pd.Timestamp("2026-10-15")


@pytest.mark.parametrize("tf,mkt", [("15m", "IN"), ("1d", "US")])
def test_fill_only_india_daily(kotak_gap, tf, mkt):
    data = {"A": _daily()}
    out, _ = scanner.fill_missing_days(data, None, tf, mkt)
    assert out["A"].index[-1] == pd.Timestamp("2026-10-14") and kotak_gap == []


def test_fill_failure_does_not_stop_scan(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("kotak down")
    monkeypatch.setattr(scanner, "fill_missing_days", boom)
    monkeypatch.setattr(scanner, "fetch_ohlcv", lambda *a, **k: {})
    monkeypatch.setattr(scanner, "fetch_index", lambda *a, **k: None)
    assert scanner.scan(["A"], "1d", market="IN") == []


# ---- stop requests ---------------------------------------------------------------------------

def test_scan_stops_mid_loop_and_flag_is_per_market(monkeypatch, frames):
    """A stop request raised during the symbol loop ends the scan; other markets are unaffected."""
    monkeypatch.setattr(scanner, "fetch_ohlcv", FakeFetch(frames))
    seen = []

    def evaluate(sym, df, **kw):
        seen.append(sym)
        scanner.request_stop("IN")   # pressed Stop while the first symbol was being evaluated
        return None
    monkeypatch.setattr(scanner, "evaluate", evaluate)
    try:
        with pytest.raises(scanner.ScanStopped):
            scanner.scan(symbols=["AAA", "BBB"], market="IN")
        assert seen == ["AAA"]
        scanner.check_stop("US")      # the US flag is untouched
        scanner.clear_stop("IN")
        scanner.check_stop("IN")
    finally:
        scanner.clear_stop("IN")


def test_scan_stopped_before_download(monkeypatch):
    monkeypatch.setattr(scanner, "fetch_ohlcv", _fail)
    scanner.request_stop("US")
    try:
        with pytest.raises(scanner.ScanStopped):
            scanner.scan(symbols=["AAA"], market="US")
    finally:
        scanner.clear_stop("US")


def test_fetch_ohlcv_calls_stop_before_each_batch(monkeypatch):
    from algotrade import data
    downloads = []
    monkeypatch.setattr(data.yf, "download", lambda tickers, **kw: downloads.append(tickers) or pd.DataFrame())

    def stop():
        if len(downloads) == 2:
            raise scanner.ScanStopped("IN scan stopped")
    with pytest.raises(scanner.ScanStopped):
        data.fetch_ohlcv([f"S{i}" for i in range(5)], batch_size=2, stop=stop)
    assert len(downloads) == 2
