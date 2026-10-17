"""P1: scanner.evaluate and the trade math (characterization).

Anchor frame (conftest.eval_frame): last bar Close 100 / High 101 / Low 99 / ATR 2 / TURNOVER20 1e9.
Votes are injected via votes= together with a matching strategies= list (see plan section 7)."""
import dataclasses
import math

import pandas as pd
import pytest

from algotrade import config, markets, scanner
from algotrade.scanner import Signal, entry_type_for, evaluate, passes_filters
from conftest import eval_frame

LIMIT_BUY = {"RSI Pullback": "BUY", "Support-Resistance": "BUY"}      # 2 pullback families
STOP_BUY = {"EMA 9/21 Cross": "BUY", "20-bar Breakout": "BUY"}        # 2 momentum families
LIMIT_SELL = {"RSI Pullback": "SELL", "Support-Resistance": "SELL"}
STOP_SELL = {"EMA 9/21 Cross": "SELL", "20-bar Breakout": "SELL"}


def ev(votes, df=None, i=-1, **kw):
    df = eval_frame() if df is None else df
    return evaluate("X", df, i, strategies=list(votes), votes=votes, **kw)


# ---- worked anchors ------------------------------------------------------------------------

def test_anchor_in_buy_limit():
    s = ev(LIMIT_BUY)
    assert (s.side, s.entry_type) == ("BUY", "limit")
    assert s.entry == 99.0 and s.stop_loss == 96.0
    assert s.targets == [103.5, 108.0]
    assert s.qty == 333
    assert s.score == 2 and s.reasons == ["RSI Pullback", "Support-Resistance"]


def test_anchor_in_buy_stop():
    s = ev(STOP_BUY)
    assert s.entry_type == "stop"
    assert (s.entry, s.stop_loss, s.targets, s.qty) == (101.2, 98.2, [105.7, 110.2], 333)


def test_anchor_us_qty_and_key():
    s = ev(LIMIT_BUY, market="US")
    assert s.qty == 33  # 10000 * 1% // 3
    assert s.market == "US"
    assert s.key == "US|X|BUY|" + str(eval_frame().index[-1].date())


def test_market_code_is_case_insensitive():
    assert ev(LIMIT_BUY, market="us").market == "US"


def test_sell_limit_and_stop_when_shorts_allowed(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_SHORTS", True)
    s = ev(LIMIT_SELL)
    assert (s.side, s.entry_type, s.entry, s.stop_loss, s.targets, s.qty) == \
        ("SELL", "limit", 101.0, 104.0, [96.5, 92.0], 333)
    s = ev(STOP_SELL)
    assert (s.entry_type, s.entry, s.stop_loss, s.targets) == ("stop", 98.8, 101.8, [94.3, 89.8])


# ---- bar index -----------------------------------------------------------------------------

def test_fewer_than_201_rows_returns_none():
    assert ev(LIMIT_BUY, df=eval_frame(rows=200)) is None


def test_explicit_index_below_200_returns_none():
    assert ev(LIMIT_BUY, df=eval_frame(rows=260), i=199) is None


def test_negative_index_is_normalised():
    df = eval_frame(rows=201)
    a, b = ev(LIMIT_BUY, df=df, i=-1), ev(LIMIT_BUY, df=df, i=200)
    assert a == b and a.bar_time == df.index[200]


# ---- liquidity ------------------------------------------------------------------------------

@pytest.mark.parametrize("market,price_ok,price_bad,turn_ok,turn_bad", [
    ("IN", 50.0, 49.99, 5e7, 5e7 - 1),
    ("US", 5.0, 4.99, 2e7, 2e7 - 1),
])
def test_liquidity_thresholds(market, price_ok, price_bad, turn_ok, turn_bad):
    kw = dict(High=price_ok + 1, Low=price_ok - 0.5, ATR=0.2)
    assert ev(LIMIT_BUY, df=eval_frame(Close=price_ok, **kw), market=market) is not None
    assert ev(LIMIT_BUY, df=eval_frame(Close=price_bad, **kw), market=market) is None
    assert ev(LIMIT_BUY, df=eval_frame(TURNOVER20=turn_ok), market=market) is not None
    assert ev(LIMIT_BUY, df=eval_frame(TURNOVER20=turn_bad), market=market) is None


@pytest.mark.parametrize("tf,div", list(scanner.BARS_PER_DAY.items()))
def test_turnover_threshold_scales_with_bars_per_day(tf, div):
    thr = 5e7 / div
    assert ev(LIMIT_BUY, df=eval_frame(TURNOVER20=thr), timeframe=tf) is not None
    assert ev(LIMIT_BUY, df=eval_frame(TURNOVER20=thr * 0.999), timeframe=tf) is None


def test_unknown_timeframe_uses_daily_turnover_threshold():
    assert ev(LIMIT_BUY, df=eval_frame(TURNOVER20=5e7 - 1), timeframe="2h") is None
    s = ev(LIMIT_BUY, df=eval_frame(TURNOVER20=5e7), timeframe="2h")
    assert s.key.endswith("|2h|" + str(s.bar_time.date()))


# ---- scoring ------------------------------------------------------------------------------

ZONES = ["Demand-Supply", "Rally-Base-Rally", "Drop-Base-Rally", "Rally-Base-Drop", "Drop-Base-Drop"]


def test_five_zone_strategies_count_as_one_family():
    votes = {n: "BUY" for n in ZONES}
    assert ev(votes) is None                     # score 1 < MIN_SCORE 2
    s = ev(votes, min_score=1)
    assert s.score == 1 and s.reasons == ZONES and s.entry_type == "limit"
    s = ev({**votes, "EMA 9/21 Cross": "BUY"})
    assert s.score == 2


def test_min_score_default_comes_from_config(monkeypatch):
    monkeypatch.setattr(config, "MIN_SCORE", 3)
    assert ev(LIMIT_BUY) is None
    assert ev(LIMIT_BUY, min_score=2) is not None


def test_score_tie_returns_none():
    votes = {**LIMIT_BUY, "EMA 9/21 Cross": "SELL", "20-bar Breakout": "SELL"}
    assert ev(votes) is None
    assert ev(votes, min_score=1) is None


def test_zero_zero_tie_never_signals():
    """Quirk 8: 0-0 ties never signal, even with min_score=0."""
    assert ev({"EMA 9/21 Cross": None}, min_score=0) is None


def test_buy_must_strictly_beat_sell():
    votes = {**LIMIT_BUY, "EMA 9/21 Cross": "SELL"}
    s = ev(votes)
    assert s.side == "BUY" and s.score == 2 and s.reasons == ["RSI Pullback", "Support-Resistance"]


def test_sell_never_trades_while_shorts_disabled():
    """Quirk 4: SELL winners return None while ALLOW_SHORTS=False."""
    assert config.ALLOW_SHORTS is False
    assert ev(LIMIT_SELL) is None
    assert ev({n: ("SELL", 110.0) for n in ["Rally-Base-Drop", "Drop-Base-Drop"]} | {"EMA 9/21 Cross": "SELL"}) is None


def test_sell_votes_veto_tied_buy_even_with_shorts_disabled():
    """Quirk 4: a SELL tie still vetoes a BUY."""
    votes = {"Rally-Base-Rally": ("BUY", 95.0), "EMA 9/21 Cross": "BUY",
             "Rally-Base-Drop": ("SELL", 105.0), "20-bar Breakout": "SELL"}
    assert ev(votes) is None


def test_missing_vote_key_raises_keyerror():
    with pytest.raises(KeyError):
        evaluate("X", eval_frame(), votes=LIMIT_BUY)  # strategies defaults to 11 names


def test_strategies_default_from_config(monkeypatch):
    monkeypatch.setattr(config, "STRATEGIES", list(LIMIT_BUY))
    assert evaluate("X", eval_frame(), votes=LIMIT_BUY).entry == 99.0


# ---- entry type ----------------------------------------------------------------------------

@pytest.mark.parametrize("reasons,expected", [
    (["RSI Pullback"], "limit"),
    (["EMA 9/21 Cross"], "stop"),
    (["RSI Pullback", "EMA 9/21 Cross"], "limit"),                       # tie goes to limit
    (["RSI Pullback", "EMA 9/21 Cross", "20-bar Breakout"], "stop"),
    (["Demand-Supply", "Rally-Base-Rally", "EMA 9/21 Cross", "MACD Trend Cross"], "stop"),  # zones = 1 family
    (["Demand-Supply", "Rally-Base-Rally", "EMA 9/21 Cross"], "limit"),
    (["RSI(2) Reversion", "Squeeze Breakout"], "limit"),
    (["Supertrend Flip", "Squeeze Breakout"], "stop"),
    ([], "limit"),
])
def test_entry_type_for(reasons, expected):
    assert entry_type_for(reasons) == expected


def test_pullback_strategy_set():
    assert scanner.PULLBACK_STRATEGIES == {"RSI Pullback", "RSI(2) Reversion", "Support-Resistance", *ZONES}


# ---- structural stops ------------------------------------------------------------------------

def test_valid_structural_stop_replaces_atr_stop():
    s = ev({"Support-Resistance": ("BUY", 95.0), "RSI Pullback": "BUY"})
    assert (s.entry, s.stop_loss, s.targets, s.qty) == (99.0, 95.0, [105.0, 111.0], 250)


def test_widest_valid_structural_stop_wins():
    s = ev({"Support-Resistance": ("BUY", 97.0), "Rally-Base-Rally": ("BUY", 94.0), "RSI Pullback": "BUY"})
    assert s.stop_loss == 94.0 and s.qty == 200  # risk 5


@pytest.mark.parametrize("stop", [98.5, 92.9, 100.0, 120.0])
def test_invalid_structural_stops_fall_back_to_atr(stop):
    """Valid only if 0.6 <= entry - stop <= 6.0 (0.3-3.0 ATR)."""
    s = ev({"Support-Resistance": ("BUY", stop), "RSI Pullback": "BUY"})
    assert s.stop_loss == 96.0


@pytest.mark.parametrize("stop,expected_sl", [
    (98.4, 96.0),   # FLOAT QUIRK: 99.0 - 98.4 = 0.59999999 < 0.6 -> rejected despite being "exactly" 0.3 ATR
    (98.375, 98.38),  # 0.625 below entry: accepted (SL then rounded to 2 dp)
    (93.0, 93.0),   # 6.0 = 3.0 ATR exactly: upper bound inclusive
])
def test_structural_stop_bounds_inclusive(stop, expected_sl):
    s = ev({"Support-Resistance": ("BUY", stop), "RSI Pullback": "BUY"})
    assert s.stop_loss == expected_sl


def test_structural_stop_measured_from_entry_not_close():
    """Quirk 7: 98.6 is 1.4 below the close (valid) but only 0.4 below the 99.0 entry -> rejected."""
    s = ev({"Support-Resistance": ("BUY", 98.6), "RSI Pullback": "BUY"})
    assert s.stop_loss == 96.0


def test_opposite_side_stops_are_ignored():
    votes = {"Support-Resistance": ("BUY", 95.0), "RSI Pullback": "BUY", "Rally-Base-Drop": ("SELL", 97.0)}
    s = ev(votes)
    assert s.side == "BUY" and s.stop_loss == 95.0


def test_sell_structural_stop(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_SHORTS", True)
    s = ev({"Support-Resistance": ("SELL", 105.0), "RSI Pullback": "SELL"})
    assert (s.entry, s.stop_loss, s.targets) == (101.0, 105.0, [95.0, 89.0])


def test_zero_atr_returns_none():
    assert ev(LIMIT_BUY, df=eval_frame(ATR=0.0)) is None


# ---- quantity --------------------------------------------------------------------------------

def test_qty_capped_by_capital():
    s = ev(LIMIT_BUY, df=eval_frame(ATR=0.02))
    assert s.entry == 99.99 and s.qty == 1000  # risk 0.03 -> 33333, capped at 100000 // 99.99


def test_qty_zero_signal_is_still_returned(monkeypatch):
    """Quirk 8: a signal can have qty == 0."""
    monkeypatch.setitem(markets.MARKETS, "IN", dataclasses.replace(markets.MARKETS["IN"], capital=200.0))
    s = ev(LIMIT_BUY)
    assert s is not None and s.qty == 0


def test_config_capital_does_not_change_qty(monkeypatch):
    """markets.MARKETS froze capital at import; config.CAPITAL is not read by evaluate."""
    monkeypatch.setattr(config, "CAPITAL", 1.0)
    assert ev(LIMIT_BUY).qty == 333


def test_risk_pct_is_read_live(monkeypatch):
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 2.0)
    assert ev(LIMIT_BUY).qty == 666


def test_rounding_of_prices():
    s = ev(LIMIT_BUY, df=eval_frame(Close=123.456, High=124.0, Low=122.0, ATR=1.333))
    # entry 122.7895 -> 122.79 ; risk 1.9995 ; sl 120.79 ; targets from unrounded entry
    assert (s.entry, s.stop_loss, s.targets) == (122.79, 120.79, [125.79, 128.79])
    assert s.ref_price == 123.46
    assert s.qty == 500


# ---- filters ---------------------------------------------------------------------------------

def row(**kw):
    return pd.Series({"IDX_CLOSE": float("nan"), "IDX_EMA200": float("nan"), "RS": float("nan"), **kw})


@pytest.mark.parametrize("filters,r,side,expected", [
    (["trend"], row(IDX_CLOSE=110, IDX_EMA200=100), "BUY", True),
    (["trend"], row(IDX_CLOSE=110, IDX_EMA200=100), "SELL", False),
    (["trend"], row(IDX_CLOSE=90, IDX_EMA200=100), "BUY", False),
    (["trend"], row(IDX_CLOSE=90, IDX_EMA200=100), "SELL", True),
    (["trend"], row(IDX_CLOSE=100, IDX_EMA200=100), "BUY", False),   # equal counts as "not above"
    (["trend"], row(), "BUY", True),                                 # NaN never blocks
    (["trend"], row(), "SELL", True),
    (["rs"], row(RS=1.2), "BUY", True),
    (["rs"], row(RS=1.2), "SELL", False),
    (["rs"], row(RS=0.8), "BUY", False),
    (["rs"], row(RS=1.0), "BUY", False),
    (["rs"], row(RS=1.0), "SELL", True),
    (["rs"], row(), "BUY", True),
    (["trend", "rs"], row(IDX_CLOSE=110, IDX_EMA200=100, RS=0.9), "BUY", False),
    ([], row(IDX_CLOSE=90, IDX_EMA200=100, RS=0.1), "BUY", True),
    (["unknown"], row(IDX_CLOSE=90, IDX_EMA200=100), "BUY", True),   # unknown filter names are ignored here
])
def test_passes_filters(filters, r, side, expected):
    assert passes_filters(r, side, filters) is expected


def test_evaluate_applies_filters():
    df = eval_frame(IDX_CLOSE=90.0, IDX_EMA200=100.0)
    assert ev(LIMIT_BUY, df=df, filters=["trend"]) is None
    assert ev(LIMIT_BUY, df=df, filters=[]) is not None
    assert ev(LIMIT_BUY, df=eval_frame(), filters=["trend", "rs"]) is not None  # NaN index data


def test_evaluate_filters_default_from_config(monkeypatch):
    monkeypatch.setattr(config, "FILTERS", ["rs"])
    assert ev(LIMIT_BUY, df=eval_frame(RS=0.5)) is None
    assert ev(LIMIT_BUY, df=eval_frame(RS=0.5), filters=[]) is not None


# ---- key & output fields ---------------------------------------------------------------------

@pytest.mark.parametrize("market,tf,expected", [
    ("IN", "1d", "SYM|BUY|2024-03-05"),
    ("US", "1d", "US|SYM|BUY|2024-03-05"),
    ("IN", "15m", "SYM|BUY|15m|2024-03-05"),
    ("US", "1h", "US|SYM|BUY|1h|2024-03-05"),
])
def test_signal_key(market, tf, expected):
    s = Signal("SYM", "BUY", 1, 1, [1, 1], 1, 1, [], 1, 1, 1, pd.Timestamp("2024-03-05 10:15"), tf, market)
    assert s.key == expected


def test_signal_defaults():
    s = Signal("SYM", "BUY", 1, 1, [1, 1], 1, 1, [], 1, 1, 1, pd.Timestamp("2024-03-05"))
    assert (s.timeframe, s.market, s.entry_type, s.ref_price, s.extra) == ("1d", "IN", "market", 0.0, {})


def test_output_fields():
    df = eval_frame(RSI=55.55, ADX=24.96, Volume=1234.0, VOL_AVG20=1000.0)
    s = ev(LIMIT_BUY, df=df, timeframe="15m")
    assert s.rsi == 55.5  # round(55.55, 1): float repr rounds down
    assert s.adx == 25.0
    assert s.vol_ratio == 1.23
    assert s.ref_price == 100.0
    assert s.bar_time == df.index[-1] and s.timeframe == "15m" and s.market == "IN"
    assert isinstance(s.qty, int)
    assert s.key == f"X|BUY|15m|{df.index[-1].date()}"


def test_vol_ratio_zero_when_avg_is_zero():
    assert ev(LIMIT_BUY, df=eval_frame(VOL_AVG20=0.0)).vol_ratio == 0.0


def test_vol_ratio_nan_when_avg_is_nan():
    """Quirk 12: NaN VOL_AVG20 is truthy, so vol_ratio becomes NaN instead of 0.0."""
    s = ev(LIMIT_BUY, df=eval_frame(VOL_AVG20=float("nan")))
    assert math.isnan(s.vol_ratio)
