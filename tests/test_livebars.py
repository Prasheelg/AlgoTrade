"""livebars: India intraday bars = Yahoo history up to yesterday + today's bars built from Kotak's 5-minute ones."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from algotrade import charts, data, kotak, livebars

DAY = datetime(2026, 11, 23).date()   # a Monday
TOKENS = {"AAA": "1", "BBB": "2", "CCC": "3", "DDD": "4", "EEE": "5", "FFF": "6"}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(livebars, "_history", {})
    monkeypatch.setattr(livebars, "_today", {})
    monkeypatch.setattr(livebars, "_skip_until", [None])
    monkeypatch.setattr(kotak, "enabled", lambda: True)
    monkeypatch.setattr(kotak, "tokens", lambda: TOKENS)


def bars5(start="09:15", end="12:00", day=DAY, price=100.0, vol=1000.0):
    """5-minute bars from start to end (bar starts), each +1 in price."""
    idx = pd.date_range(f"{day} {start}", f"{day} {end}", freq="5min")
    c = price + pd.Series(range(len(idx)), index=idx, dtype=float)
    return pd.DataFrame({"Open": c - .5, "High": c + 1, "Low": c - 1, "Close": c, "Volume": vol}, index=idx)


def history(days=3, freq="15min"):
    """Yahoo-style bars on the days before DAY, plus a bar of DAY that must be thrown away."""
    idx = pd.DatetimeIndex([])
    for d in range(days, 0, -1):
        day = DAY - timedelta(days=d)
        idx = idx.append(pd.date_range(f"{day} 09:15", f"{day} 15:15", freq=freq))
    idx = idx.append(pd.DatetimeIndex([f"{DAY} 09:15"]))
    return pd.DataFrame({"Open": 1.0, "High": 2.0, "Low": .5, "Close": 1.5, "Volume": 10}, index=idx)


class Yahoo:
    def __init__(self, frames):
        self.frames, self.calls = frames, []

    def __call__(self, syms, tf, stop=None):
        self.calls.append((list(syms), tf))
        return {s: self.frames[s] for s in syms if s in self.frames}


class KotakHist:
    """Stand-in for charts.kotak_history: per neosymbol a frame, an exception, or nothing."""
    def __init__(self, frames):
        self.frames, self.calls = frames, []

    def __call__(self, name, interval, days, today):
        self.calls.append(name)
        v = self.frames.get(name)
        if isinstance(v, Exception):
            raise v
        return v if v is not None else pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])


# ---- build ----

def test_build_15m_aligned_to_open_and_drops_the_unfinished_bar():
    out = livebars.build(bars5("09:15", "10:05"), "15m", DAY)   # 10:00-10:15 has only 10:00 and 10:05
    assert list(out.index.strftime("%H:%M")) == ["09:15", "09:30", "09:45"]
    first = out.iloc[0]   # 09:15, 09:20, 09:25 -> closes 100, 101, 102
    assert (first.Open, first.High, first.Low, first.Close, first.Volume) == (99.5, 103.0, 99.0, 102.0, 3000.0)


def test_build_keeps_a_bar_once_its_last_piece_is_in():
    assert livebars.build(bars5("09:15", "10:10"), "15m", DAY).index[-1] == pd.Timestamp(f"{DAY} 10:00")


@pytest.mark.parametrize("tf,starts", [("30m", ["09:15", "09:45", "10:15"]), ("1h", ["09:15"])])
def test_build_30m_and_1h_start_at_the_open(tf, starts):
    assert list(livebars.build(bars5("09:15", "10:40"), tf, DAY).index.strftime("%H:%M")) == starts


def test_build_last_hour_ends_at_the_close():
    """15:15-15:30 is the session's last 1h bar: complete once the 15:25 bar is in."""
    out = livebars.build(bars5("09:15", "15:25"), "1h", DAY)
    assert out.index[-1] == pd.Timestamp(f"{DAY} 15:15") and out.iloc[-1].Volume == 3000.0


def test_build_5m_is_unchanged():
    df = bars5()
    assert livebars.build(df, "5m", DAY) is df


# ---- active / neosymbol ----

@pytest.mark.parametrize("tf,market,now,on", [
    ("15m", "IN", datetime(2026, 11, 23, 10, 0), True),
    ("15m", "IN", datetime(2026, 11, 23, 9, 14), False),   # before the open
    ("15m", "IN", datetime(2026, 11, 22, 10, 0), False),   # Sunday
    ("1d", "IN", datetime(2026, 11, 23, 10, 0), False),    # daily: Yahoo
    ("15m", "US", datetime(2026, 11, 23, 10, 0), False),
])
def test_active(tf, market, now, on):
    assert livebars.active(tf, market, now) is on


def test_inactive_without_kotak_or_while_skipping(monkeypatch):
    now = datetime(2026, 11, 23, 10, 0)
    livebars._skip_until[0] = now + timedelta(minutes=1)
    assert not livebars.active("5m", "IN", now)
    assert livebars.active("5m", "IN", now + timedelta(minutes=1))
    monkeypatch.setattr(kotak, "enabled", lambda: False)
    assert not livebars.active("5m", "IN", now + timedelta(minutes=1))


@pytest.mark.parametrize("sym,name", [("AAA", "nse_cm|1"), ("NIFTY", "nse_cm|Nifty 50"), ("^NSEI", "nse_cm|Nifty 50"),
                                      ("BANKNIFTY", "nse_cm|Nifty Bank"), ("ZZZ", None)])
def test_neosymbol(sym, name):
    assert livebars.neosymbol(sym) == name


# ---- fetch ----

def test_fetch_joins_history_and_today(monkeypatch):
    kh = KotakHist({"nse_cm|1": bars5("09:15", "10:10")})
    monkeypatch.setattr(charts, "kotak_history", kh)
    y = Yahoo({"AAA": history()})
    out = livebars.fetch(["AAA"], "15m", datetime(2026, 11, 23, 10, 16), y)["AAA"]
    old, today = out[out.index.date < DAY], out[out.index.date == DAY]
    assert len(old) == 3 * 25 and (old.Close == 1.5).all()           # Yahoo's DAY bar was thrown away
    assert list(today.index.strftime("%H:%M")) == ["09:15", "09:30", "09:45", "10:00"]
    assert y.calls == [(["AAA"], "15m")]                              # history only, no fallback


def test_history_is_downloaded_once_a_day(monkeypatch):
    monkeypatch.setattr(charts, "kotak_history", KotakHist({"nse_cm|1": bars5()}))
    y = Yahoo({"AAA": history()})
    for minute in (5, 20):
        livebars.fetch(["AAA"], "15m", datetime(2026, 11, 23, 12, minute), y)
    assert y.calls == [(["AAA"], "15m")]


def test_kotak_bars_reused_until_the_next_5_minute_boundary(monkeypatch):
    kh = KotakHist({"nse_cm|1": bars5()})
    monkeypatch.setattr(charts, "kotak_history", kh)
    y = Yahoo({"AAA": history()})
    for tf, minute in (("5m", 6), ("15m", 7), ("1h", 9)):   # one scan round, 12:06-12:09: one request
        livebars.fetch(["AAA"], tf, datetime(2026, 11, 23, 12, minute), y)
    assert len(kh.calls) == 1
    livebars.fetch(["AAA"], "15m", datetime(2026, 11, 23, 12, 10), y)   # a new 5-minute bar has ended
    assert len(kh.calls) == 2


@pytest.mark.parametrize("bad", [None, RuntimeError("Kotak down"), "negative"])
def test_symbol_kotak_cant_serve_gets_yahoo(monkeypatch, bad):
    neg = bars5()
    neg.iloc[-1, neg.columns.get_loc("Volume")] = -5e6   # Kotak's closing-volume glitch
    kh = KotakHist({"nse_cm|1": bars5(), "nse_cm|2": neg if bad == "negative" else bad})
    monkeypatch.setattr(charts, "kotak_history", kh)
    full = history()
    y = Yahoo({"AAA": history(), "BBB": full})
    out = livebars.fetch(["AAA", "BBB"], "15m", datetime(2026, 11, 23, 12, 6), y)
    assert y.calls == [(["AAA", "BBB"], "15m"), (["BBB"], "15m")]
    assert out["BBB"] is full and (out["AAA"].index.date == DAY).sum() > 1


def test_symbol_without_history_or_token_goes_to_yahoo(monkeypatch):
    monkeypatch.setattr(charts, "kotak_history", KotakHist({"nse_cm|1": bars5()}))
    y = Yahoo({"AAA": history(), "ZZZ": history()})
    out = livebars.fetch(["AAA", "ZZZ", "NOPE"], "15m", datetime(2026, 11, 23, 12, 6), y)
    assert y.calls[1] == (["ZZZ", "NOPE"], "15m") and set(out) == {"AAA", "ZZZ"}


def test_first_symbols_all_empty_means_no_kotak_today(monkeypatch):
    """A holiday or an outage: after PROBE failures in a row from the start, the rest skip Kotak, and so does every
    scan for SKIP_MIN minutes."""
    kh = KotakHist({})
    monkeypatch.setattr(charts, "kotak_history", kh)
    syms = list(TOKENS)
    y = Yahoo({s: history() for s in syms})
    now = datetime(2026, 11, 23, 10, 0)
    out = livebars.fetch(syms, "15m", now, y)
    assert len(kh.calls) == livebars.PROBE and set(out) == set(syms)
    assert y.calls[-1] == (syms, "15m")
    assert livebars._skip_until[0] == now + timedelta(minutes=livebars.SKIP_MIN)
    assert not livebars.active("15m", "IN", now + timedelta(minutes=5))


def test_a_later_failure_doesnt_switch_kotak_off(monkeypatch):
    kh = KotakHist({f"nse_cm|{t}": bars5() for t in "12345"})   # FFF (6) has no bars
    monkeypatch.setattr(charts, "kotak_history", kh)
    y = Yahoo({s: history() for s in TOKENS})
    livebars.fetch(list(TOKENS), "15m", datetime(2026, 11, 23, 12, 6), y)
    assert livebars._skip_until[0] is None and y.calls[-1] == (["FFF"], "15m")


def test_stop_is_checked_per_symbol(monkeypatch):
    monkeypatch.setattr(charts, "kotak_history", KotakHist({"nse_cm|1": bars5()}))
    seen = []

    def stop():
        seen.append(1)
        if len(seen) > 1:
            raise RuntimeError("stopped")
    with pytest.raises(RuntimeError, match="stopped"):
        livebars.fetch(["AAA", "BBB"], "15m", datetime(2026, 11, 23, 12, 6), lambda s, tf, stop=None: {}, stop=stop)


# ---- data.fetch_ohlcv routing ----

def test_fetch_ohlcv_live_routes_india_intraday_through_livebars(monkeypatch):
    seen = []
    monkeypatch.setattr(livebars, "active", lambda tf, m, now: True)
    monkeypatch.setattr(livebars, "fetch", lambda syms, tf, now, yahoo, stop=None: seen.append((syms, tf)) or {"X": 1})
    assert data.fetch_ohlcv(["X"], "15m", market="IN", live=True) == {"X": 1}
    assert seen == [(["X"], "15m")]


@pytest.mark.parametrize("kw", [{}, {"live": True, "period": "5d"}])
def test_fetch_ohlcv_without_live_or_with_a_period_stays_on_yahoo(monkeypatch, kw):
    monkeypatch.setattr(livebars, "active", lambda *a: True)
    monkeypatch.setattr(livebars, "fetch", lambda *a, **k: pytest.fail("livebars used"))
    monkeypatch.setattr(data, "_yahoo", lambda *a: {"Y": 1})
    assert data.fetch_ohlcv(["Y"], "15m", market="IN", **kw) == {"Y": 1}
