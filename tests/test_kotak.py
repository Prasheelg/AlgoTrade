"""Kotak Neo real-time quotes (read-only) and their use in the tracker. The SDK client and the
scrip master download are faked; nothing reaches Kotak."""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from algotrade import config, kotak, state, telegram_bot, tracker
from conftest import FakeFetch, Recorder, make_signal

SCRIP_CSV = """pSymbol,pGroup,pExchSeg,pSymbolName,pTrdSymbol
2885,EQ,nse_cm,RELIANCE,RELIANCE-EQ
2031,EQ,nse_cm,M&M,M&M-EQ
9999,BE,nse_cm,ODDCO,ODDCO-BE
11536,EQ,nse_cm,TCS,TCS-EQ
"""


def quote(token, ltp, o=None, h=None, l=None, t=1791968506, vol="8704918"):
    return {"exchange_token": str(token), "display_symbol": "X-EQ", "exchange": "nse_cm", "lstup_time": str(t),
            "ltp": f"{ltp:.4f}", "last_volume": vol,
            "ohlc": {"open": f"{o or ltp:.4f}", "high": f"{h or ltp:.4f}", "low": f"{l or ltp:.4f}", "close": "1.0"}}


class FakeNeo:
    """Stand-in for neo_api_client.NeoAPI exposing only the two read-only calls kotak.py uses."""

    def __init__(self, quotes_by_token=None, fail_batches=()):
        self.quotes_by_token = quotes_by_token or {}
        self.fail_batches = set(fail_batches)
        self.calls = []

    def scrip_master(self, exchange_segment=None):
        self.calls.append(("scrip_master", exchange_segment))
        return "https://example.invalid/nse_cm.csv"

    def quotes(self, instrument_tokens=None, quote_type=None):
        n = sum(1 for c in self.calls if c[0] == "quotes")
        self.calls.append(("quotes", [t["instrument_token"] for t in instrument_tokens], quote_type))
        if n in self.fail_batches:
            return {"code": 429, "error": "Rate limit exceeded"}
        return [self.quotes_by_token[t["instrument_token"]] for t in instrument_tokens
                if t["instrument_token"] in self.quotes_by_token]


@pytest.fixture
def neo(monkeypatch):
    """Kotak enabled with a fake client; module caches reset; no real sleeping."""
    fake = FakeNeo()
    monkeypatch.setattr(config, "KOTAK_CONSUMER_KEY", "test-key")
    monkeypatch.setattr(kotak, "_client", fake)
    monkeypatch.setattr(kotak, "_tokens", {})
    monkeypatch.setattr(kotak, "_tokens_day", None)
    monkeypatch.setattr(kotak, "_download_csv", lambda url: SCRIP_CSV)
    sleeps = []
    clock = [0.0]
    monkeypatch.setattr(kotak, "time", SimpleNamespace(sleep=sleeps.append, monotonic=lambda: clock[0]))
    monkeypatch.setattr(kotak, "_failed_at", None)
    fake.clock = clock
    fake.sleeps = sleeps
    return fake


# ---- parsing ---------------------------------------------------------------------------------------

def test_tick_sizes_from_scrip_master():
    csv = """pSymbol,pGroup,pExchSeg,pSymbolName,pTrdSymbol,dTickSize 
3518,EQ,nse_cm,TORNTPHARM,TORNTPHARM-EQ,10
2885,EQ,nse_cm,RELIANCE,RELIANCE-EQ,5
9999,BE,nse_cm,ODDCO,ODDCO-BE,1
"""
    assert kotak.parse_tick_sizes(csv) == {"TORNTPHARM": 0.10, "RELIANCE": 0.05}
    assert kotak.parse_tick_sizes(SCRIP_CSV) == {}           # no tick column: callers fall back to 0.05


def test_scrip_master_maps_eq_series_only():
    assert kotak.parse_scrip_master(SCRIP_CSV) == {"RELIANCE": "2885", "M&M": "2031", "TCS": "11536"}


def test_parse_quote_real_shape():
    q = kotak.parse_quote(quote(2885, 1185.0, o=1167, h=1188, l=1162.7))
    assert q == {"ltp": 1185.0, "open": 1167.0, "high": 1188.0, "low": 1162.7, "volume": 8704918.0,
                 "time": datetime(2026, 10, 14, 14, 31, 46), "prev_close": 1.0}  # epoch -> naive IST


# ---- quotes() --------------------------------------------------------------------------------------

def test_disabled_without_key_makes_no_calls(monkeypatch):
    monkeypatch.setattr(kotak, "_client", FakeNeo())
    assert config.KOTAK_CONSUMER_KEY == "" and not kotak.enabled()
    assert kotak.quotes(["RELIANCE"]) == {}
    assert kotak._client.calls == []


def test_quotes_maps_tokens_back_to_symbols_and_skips_unknown(neo):
    neo.quotes_by_token = {"2885": quote(2885, 1185.0), "2031": quote(2031, 3100.5)}
    out = kotak.quotes(["RELIANCE", "M&M", "NOTLISTED"])
    assert {s: q["ltp"] for s, q in out.items()} == {"RELIANCE": 1185.0, "M&M": 3100.5}
    assert neo.calls == [("scrip_master", "nse_cm"), ("quotes", ["2885", "2031"], "all")]


def test_quotes_batches_of_49_with_pause_and_failed_batch_skipped(neo, monkeypatch):
    # Kotak rejects 50 instruments in one call ("Please set the Neo symbol max value to 50"), so 49 per batch
    assert kotak.BATCH == 49
    syms = [f"S{i}" for i in range(120)]
    monkeypatch.setattr(kotak, "tokens", lambda: {s: str(i) for i, s in enumerate(syms)})
    neo.quotes_by_token = {str(i): quote(i, 100 + i) for i in range(120)}
    neo.fail_batches = {1}  # second batch is rate-limited
    out = kotak.quotes(syms)
    assert [len(c[1]) for c in neo.calls if c[0] == "quotes"] == [49, 49, 22]
    assert neo.sleeps == [kotak.PAUSE_SEC, kotak.PAUSE_SEC]
    assert sorted(out, key=lambda s: int(s[1:])) == syms[:49] + syms[98:]


def test_scrip_master_failure_returns_empty(neo, monkeypatch):
    monkeypatch.setattr(neo, "scrip_master", lambda exchange_segment=None: {"error": "bad key"})
    assert kotak.quotes(["RELIANCE"]) == {}


def test_scrip_master_cached_per_day(neo):
    kotak.quotes(["RELIANCE"])
    kotak.quotes(["TCS"])
    assert [c[0] for c in neo.calls].count("scrip_master") == 1


def test_only_read_only_sdk_calls_are_used():
    import inspect
    src = inspect.getsource(kotak)
    for forbidden in ("place_order", "modify_order", "cancel_order", "totp_login", "totp_validate", "positions"):
        assert forbidden not in src


# ---- apply_live_bar ----------------------------------------------------------------------------------

def frame(index, closes):
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes,
                         "Volume": [1.0] * len(closes)}, index=pd.DatetimeIndex(index))


LIVE = {"ltp": 105.0, "open": 101.0, "high": 106.0, "low": 99.0, "volume": 5000.0,
        "time": datetime(2024, 1, 3, 11, 0)}


def test_daily_live_bar_replaces_delayed_today_bar():
    df = kotak.apply_live_bar(frame(["2024-01-02", "2024-01-03"], [100.0, 102.0]), LIVE, "1d")
    assert list(df.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]
    assert df.iloc[-1].tolist() == [101.0, 106.0, 99.0, 105.0, 5000.0]


def test_daily_live_bar_appended_when_yahoo_lacks_today():
    df = kotak.apply_live_bar(frame(["2024-01-02"], [100.0]), LIVE, "1d")
    assert list(df.index.strftime("%Y-%m-%d")) == ["2024-01-02", "2024-01-03"]


def test_intraday_live_bar_appended_only_if_newer():
    df = frame(["2024-01-03 10:30", "2024-01-03 10:45"], [100.0, 101.0])
    out = kotak.apply_live_bar(df, LIVE, "15m")
    assert out.index[-1] == pd.Timestamp("2024-01-03 11:00") and out.iloc[-1]["Close"] == 105.0
    old = dict(LIVE, time=datetime(2024, 1, 3, 10, 40))
    assert kotak.apply_live_bar(df, old, "15m").equals(df)


def test_live_bar_ignored_without_time_or_data():
    df = frame(["2024-01-02"], [100.0])
    assert kotak.apply_live_bar(df, dict(LIVE, time=None), "1d").equals(df)
    assert kotak.apply_live_bar(df.iloc[:0], LIVE, "1d").empty


# ---- tracker integration -------------------------------------------------------------------------

@pytest.fixture
def tg(monkeypatch):
    rec = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", rec)
    return rec


def _row(key):
    return next(r for r in state.all_signals() if r["key"] == key)


def test_tracker_uses_live_quote_to_fill_and_set_ltp(neo, monkeypatch, tg):
    sig = make_signal(symbol="RELIANCE", bar_time=pd.Timestamp("2024-01-02"))  # BUY limit 99, SL 96
    state.record([sig], telegram=True)
    # Delayed Yahoo bars: never down to 99 yet
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"RELIANCE": frame(["2024-01-02"], [100.0])}))
    monkeypatch.setattr(kotak, "quotes", lambda syms: {"RELIANCE": dict(LIVE, ltp=100.5, low=98.8, high=101.0)})
    assert tracker.update_all() == 1
    r = _row(sig.key)
    assert (r["status"], r["fill_price"], r["ltp"]) == ("ACTIVE", 99.0, 100.5)
    assert "Entry filled" in tg.calls[0][0][0]


def test_tracker_live_quote_hits_stop(neo, monkeypatch, tg):
    sig = make_signal(symbol="RELIANCE", bar_time=pd.Timestamp("2024-01-02"))
    state.record([sig], telegram=False)
    state.update_signal(sig.key, status="ACTIVE", fill_price=99.0)
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"RELIANCE": frame(["2024-01-02"], [100.0])}))
    monkeypatch.setattr(kotak, "quotes", lambda syms: {"RELIANCE": dict(LIVE, ltp=95.5, low=95.0, high=99.5)})
    tracker.update_all()
    assert (_row(sig.key)["status"], _row(sig.key)["exit_price"]) == ("SL_HIT", 96.0)
    assert tg.calls == []  # dashboard-only signal


def test_tracker_without_kotak_unchanged(monkeypatch, tg):
    sig = make_signal(symbol="RELIANCE", bar_time=pd.Timestamp("2024-01-02"))
    state.record([sig], telegram=False)
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"RELIANCE": frame(["2024-01-02", "2024-01-03"], [100.0, 100.4])}))
    monkeypatch.setattr(kotak, "quotes", lambda syms: pytest.fail("Kotak called while disabled"))
    tracker.update_all()
    assert (_row(sig.key)["status"], _row(sig.key)["ltp"]) == ("PENDING", 100.4)


def test_tracker_us_signals_never_use_kotak(neo, monkeypatch, tg):
    sig = make_signal(symbol="AAPL", market="US", bar_time=pd.Timestamp("2024-01-02"))
    state.record([sig], telegram=False)
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"AAPL": frame(["2024-01-02"], [100.0])}))
    monkeypatch.setattr(kotak, "quotes", lambda syms: pytest.fail("Kotak called for US"))
    tracker.update_all()


def test_status_never_moves_back(neo, monkeypatch, tg):
    """A fill seen on a live quote stays filled while the delayed bars don't show that price yet."""
    sig = make_signal(symbol="RELIANCE", bar_time=pd.Timestamp("2024-01-02"))
    state.record([sig], telegram=True)
    state.update_signal(sig.key, status="ACTIVE", fill_price=99.0, filled_at="2024-01-03T11:00:00")
    monkeypatch.setattr(tracker, "fetch_ohlcv", FakeFetch({"RELIANCE": frame(["2024-01-02"], [100.0])}))
    monkeypatch.setattr(kotak, "quotes", lambda syms: {"RELIANCE": dict(LIVE, ltp=100.2, low=100.0, high=100.5)})
    assert tracker.update_all() == 0
    r = _row(sig.key)
    assert (r["status"], r["fill_price"], r["ltp"]) == ("ACTIVE", 99.0, 100.2)
    assert tg.calls == []


@pytest.mark.parametrize("old,new,back", [("ACTIVE", "PENDING", True), ("T1_HIT", "ACTIVE", True),
                                          ("PENDING", "ACTIVE", False), ("ACTIVE", "SL_HIT", False),
                                          ("T1_HIT", "BREAKEVEN", False), ("PENDING", "CANCELLED", False)])
def test_regressed(old, new, back):
    assert tracker._regressed(old, new) is back


# ---- scrip master robustness ---------------------------------------------------------------------

def test_scrip_master_retried_then_cached_on_disk(neo, monkeypatch):
    replies = iter([{"Error": "Exchange Segment is not available"}, "https://example.invalid/nse_cm.csv"])
    monkeypatch.setattr(neo, "scrip_master", lambda exchange_segment=None: next(replies))
    assert kotak.tokens()["RELIANCE"] == "2885"
    assert neo.sleeps == [2]
    cached = list(config.DATA_DIR.glob("kotak_nse_cm_*.csv"))
    assert len(cached) == 1
    # A restart (fresh module state) reads the day's file instead of calling Kotak
    monkeypatch.setattr(kotak, "_tokens_day", None)
    monkeypatch.setattr(neo, "scrip_master", lambda exchange_segment=None: pytest.fail("refetched"))
    assert kotak.tokens()["TCS"] == "11536"


def test_old_scrip_master_files_removed(neo):
    (config.DATA_DIR / "kotak_nse_cm_2000-01-01.csv").write_text("old")
    kotak.tokens()
    assert [p.name.startswith("kotak_nse_cm_2000") for p in config.DATA_DIR.glob("kotak_nse_cm_*.csv")] == [False]


def test_scrip_master_failure_backs_off(neo, monkeypatch):
    calls = []
    monkeypatch.setattr(neo, "scrip_master", lambda exchange_segment=None: calls.append(1) or {"Error": "x"})
    assert kotak.quotes(["RELIANCE"]) == {}
    assert len(calls) == kotak.RETRIES
    neo.clock[0] = kotak.RETRY_AFTER_SEC - 1   # still backing off: no new attempt
    assert kotak.quotes(["RELIANCE"]) == {} and len(calls) == kotak.RETRIES
    neo.clock[0] = kotak.RETRY_AFTER_SEC + 1   # back-off over: tries again
    kotak.quotes(["RELIANCE"])
    assert len(calls) == 2 * kotak.RETRIES


def test_index_quote(monkeypatch):
    calls = []

    class Neo:
        def quotes(self, instrument_tokens=None, quote_type=None):
            calls.append((instrument_tokens, quote_type))
            return [{"exchange_token": "Nifty 50", "ltp": "22997.3500", "lstup_time": "1792059000",
                     "ohlc": {"open": "22650", "high": "23010", "low": "22600", "close": "22622.7000"}}]
    monkeypatch.setattr(config, "KOTAK_CONSUMER_KEY", "k")
    monkeypatch.setattr(kotak, "_client", Neo())
    q = kotak.index_quote("Nifty 50")
    assert (q["ltp"], q["prev_close"]) == (22997.35, 22622.7) and q["time"] is not None
    assert calls == [([{"instrument_token": "Nifty 50", "exchange_segment": "nse_cm"}], "all")]


def test_index_quote_off_or_failing(monkeypatch):
    assert kotak.index_quote("Nifty 50") is None                     # no consumer key in tests

    class Bad:
        def quotes(self, **kw):
            return {"fault": {"message": "Invalid neosymbol values"}}
    monkeypatch.setattr(config, "KOTAK_CONSUMER_KEY", "k")
    monkeypatch.setattr(kotak, "_client", Bad())
    assert kotak.index_quote("NIFTY 50") is None


def test_never_traded_contract_has_no_time():
    # Kotak sends a text date instead of epoch seconds for contracts with no trades
    assert kotak.quote_time({"lstup_time": "Mon Jan 01 1900 05:21:10 GMT+0521 (India Standard Time)"}) is None
    assert kotak.quote_time({"lstup_time": "0"}) is None and kotak.quote_time({}) is None
    assert kotak.quote_time({"lstup_time": "1792146583"}) == datetime(2026, 10, 16, 15, 59, 43)
