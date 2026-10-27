"""P5: each strategy on small crafted frames, the registry, and the pipeline golden master."""
import math

import numpy as np
import pandas as pd
import pytest

from algotrade import config, scanner, strategies as st
from algotrade.indicators import add_all
from conftest import ZONE_RETEST_OFFSET, check_golden, inject_zone, make_ohlcv


def frame(n=10, **cols):
    base = {k: [v] * n if not isinstance(v, list) else v for k, v in cols.items()}
    return pd.DataFrame(base, index=pd.bdate_range("2024-01-01", periods=n))


# ---- EMA crossover -----------------------------------------------------------------------------

def ema_df(ema9, ema21, close=100.0, ema50=90.0):
    n = len(ema9)
    return frame(n, EMA9=ema9, EMA21=ema21, Close=close, EMA50=ema50)


@pytest.mark.parametrize("ema9,close,ema50,expected", [
    ([1, 1, 1, 3, 3, 3], 100, 90, "BUY"),     # cross at i-2 (within LOOKBACK=3)
    ([1, 1, 3, 3, 3, 3], 100, 90, None),      # cross at i-3: too old
    ([1, 1, 1, 1, 1, 3], 100, 90, "BUY"),     # cross on the bar itself
    ([1, 1, 1, 3, 3, 1], 100, 90, None),      # crossed up but back below now
    ([1, 1, 1, 3, 3, 3], 80, 90, None),       # close below EMA50
    ([3, 3, 3, 1, 1, 1], 80, 90, "SELL"),
    ([3, 3, 3, 1, 1, 1], 100, 90, None),
    ([1, 1, 1, 2, 3, 3], 100, 90, "BUY"),     # equal (2 == 2) then above counts as a cross
])
def test_ema_crossover(ema9, close, ema50, expected):
    df = ema_df([float(x) for x in ema9], [2.0] * 6, close, ema50)
    assert st.ema_crossover(df, 5) == expected


def test_crossed_up_ignores_k_below_1():
    a, b = pd.Series([3.0, 3.0]), pd.Series([1.0, 1.0])
    assert st._crossed_up(a, b, 1) is False
    got = st._crossed_up(pd.Series([1.0, 3.0]), b, 1)  # k=1 compares with bar 0
    assert got == True and isinstance(got, np.bool_)  # noqa: E712  returns numpy bool, not bool


# ---- Breakout ---------------------------------------------------------------------------------

@pytest.mark.parametrize("adx,vol,close,expected", [
    (25, 1600, 111, "BUY"),
    (25, 1600, 89, "SELL"),
    (25, 1600, 100, None),
    (19.99, 1600, 111, None),
    (20, 1600, 111, "BUY"),          # ADX 20 is enough
    (25, 1500, 111, None),           # needs > 1.5x average volume
    (25, 1501, 110, None),           # close must exceed HH20 strictly
])
def test_breakout(adx, vol, close, expected):
    df = frame(1, ADX=adx, Volume=float(vol), VOL_AVG20=1000.0, Close=float(close), HH20=110.0, LL20=90.0)
    assert st.breakout(df, 0) == expected


# ---- Supertrend flip --------------------------------------------------------------------------

@pytest.mark.parametrize("dirs,expected", [
    ([-1, -1, -1, 1, 1, 1], "BUY"),   # flip at i-2
    ([-1, -1, 1, 1, 1, 1], None),     # flip at i-3
    ([1, 1, 1, 1, 1, -1], "SELL"),
    ([1, 1, 1, -1, 1, 1], "BUY"),     # whipsaw: latest flip direction wins
    ([1, 1, 1, 1, 1, 1], None),
])
def test_supertrend_flip(dirs, expected):
    assert st.supertrend_flip(frame(6, ST_DIR=[float(d) for d in dirs]), 5) == expected


# ---- MACD trend --------------------------------------------------------------------------------

@pytest.mark.parametrize("macd,close,e200,expected", [
    ([-1, -1, -1, 1, 1, 1], 100, 90, "BUY"),
    ([-1, -1, -1, 1, 1, 1], 80, 90, None),
    ([1, 1, 1, -1, -1, -1], 80, 90, "SELL"),
    ([1, 1, 1, -1, -1, -1], 100, 90, None),
])
def test_macd_trend(macd, close, e200, expected):
    df = frame(6, MACD=[float(x) for x in macd], MACD_SIG=0.0, Close=float(close), EMA200=float(e200))
    assert st.macd_trend(df, 5) == expected


# ---- RSI pullback ------------------------------------------------------------------------------

@pytest.mark.parametrize("rsi,close,expected", [
    ([35, 35, 35, 38, 41, 45], 100, "BUY"),    # back above 40 in an uptrend
    ([35, 35, 35, 38, 41, 45], 80, None),
    ([65, 65, 65, 62, 59, 55], 80, "SELL"),    # back below 60 in a downtrend
    ([65, 65, 65, 62, 59, 55], 100, None),
    ([35, 35, 41, 45, 50, 55], 100, None),     # crossed 4 bars ago
])
def test_rsi_pullback(rsi, close, expected):
    df = frame(6, RSI=[float(x) for x in rsi], Close=float(close), EMA200=90.0)
    assert st.rsi_pullback(df, 5) == expected


# ---- Squeeze breakout --------------------------------------------------------------------------

@pytest.mark.parametrize("sq,close,expected", [
    ([0, 0, 1, 1, 1, 1, 1, 0], 111, "BUY"),    # 5-bar run ending at i-1
    ([1, 1, 1, 1, 1, 0, 0, 0], 111, "BUY"),    # run ending at i-3 (still within LOOKBACK)
    ([1, 1, 1, 1, 1, 0, 0, 0], 89, "SELL"),
    ([0, 0, 0, 1, 1, 1, 1, 0], 111, None),     # only 4 bars
    ([1, 1, 1, 1, 1, 0, 0, 0, 0], 111, None),  # run ended 4 bars ago
    ([0, 0, 1, 1, 1, 1, 1, 1], 100, None),     # inside the bands
])
def test_squeeze_breakout(sq, close, expected):
    n = len(sq)
    df = frame(n, SQUEEZE=[bool(x) for x in sq], Close=float(close), BB_UP=110.0, BB_LO=90.0)
    assert st.squeeze_breakout(df, n - 1) == expected


# ---- NR7 breakout ------------------------------------------------------------------------------

def nr7_df(after, ema50=90.0):
    """Six bars of range 10 (95-105), an NR7 bar of range 2 (99-101), then `after`: closes of the bars after it."""
    n = 7 + len(after)
    highs = [105.0] * 6 + [101.0] + [max(c, 101.0) + 1 for c in after]
    lows = [95.0] * 6 + [99.0] + [min(c, 99.0) - 1 for c in after]
    return frame(n, High=highs, Low=lows, Close=[100.0] * 7 + [float(c) for c in after], ATR=5.0, EMA50=ema50)


@pytest.mark.parametrize("after,ema50,expected", [
    ([102], 90, ("BUY", 99 - 0.2 * 5)),        # the next bar closes above the NR7 high; stop below its low
    ([98], 110, ("SELL", 101 + 0.2 * 5)),
    ([102, 103, 104], 90, ("BUY", 99 - 1)),    # broke out 2 bars ago, still above
    ([102, 103, 104, 105], 90, None),          # broke out 3 bars ago: too old
    ([102, 100], 90, None),                    # back inside the NR7 range
    ([100, 102], 90, None),                    # the bar after the NR7 stayed inside: no breakout
    ([102], 110, None),                        # against EMA50
    ([98], 90, None),
])
def test_nr7_breakout(after, ema50, expected):
    df = nr7_df(after, ema50)
    assert st.nr7_breakout(df, len(df) - 1) == expected


def test_nr7_needs_the_narrowest_of_seven_bars():
    df = nr7_df([102])
    df.iloc[2, df.columns.get_loc("High")], df.iloc[2, df.columns.get_loc("Low")] = 100.5, 99.5   # narrower, 4 bars before
    assert st.nr7_breakout(df, len(df) - 1) is None
    assert st.nr7_breakout(nr7_df([102]).iloc[2:], 5) is None   # fewer than 7 bars up to the NR7 bar


# ---- RSI(2) ------------------------------------------------------------------------------------

@pytest.mark.parametrize("rsi2,close,expected", [
    (9.9, 100, "BUY"), (10, 100, None), (9.9, 80, None),
    (90.1, 80, "SELL"), (90, 80, None), (90.1, 100, None),
])
def test_rsi2_reversion(rsi2, close, expected):
    assert st.rsi2_reversion(frame(1, RSI2=float(rsi2), Close=float(close), EMA200=90.0), 0) == expected


# ---- Zones (on injected patterns) --------------------------------------------------------------

ZONE_FN = {"RBR": st.rally_base_rally, "DBR": st.drop_base_rally, "RBD": st.rally_base_drop,
           "DBD": st.drop_base_drop}


@pytest.mark.parametrize("kind", ["RBR", "DBR", "RBD", "DBD"])
def test_zone_retest_votes_for_lookback_bars(kind):
    df = add_all(inject_zone(make_ohlcv(400, seed=5), 300, kind))
    t = 300 + ZONE_RETEST_OFFSET
    z = next(z for z in df.attrs["zones"] if z.formed == 303)
    side = "BUY" if kind in ("RBR", "DBR") else "SELL"
    sign = -1 if side == "BUY" else 1
    for i in (t, t + 1, t + 2):
        vote = ZONE_FN[kind](df, i)
        assert vote[0] == side
        assert vote[1] == pytest.approx(z.distal + sign * 0.2 * df["ATR"].iloc[i])
        assert st.demand_supply(df, i) == vote
    assert ZONE_FN[kind](df, t - 1) is None and ZONE_FN[kind](df, t + 3) is None
    other = [k for k in ZONE_FN if k != kind]
    assert all(ZONE_FN[k](df, t) is None for k in other)


def test_zone_retest_invalidated_by_close_beyond_distal():
    df = add_all(inject_zone(make_ohlcv(400, seed=5), 300, "RBR"))
    t = 300 + ZONE_RETEST_OFFSET
    z = next(z for z in df.attrs["zones"] if z.formed == 303)
    df2 = df.copy()
    df2.attrs = df.attrs
    df2.iloc[t + 1, df2.columns.get_loc("Close")] = z.distal - 1
    assert st.rally_base_rally(df2, t + 1) is None
    assert st.rally_base_rally(df2, t + 2) is None


def test_zone_without_attrs_returns_none():
    df = frame(5, Close=1.0, ATR=1.0)
    assert st.demand_supply(df, 4) is None


# ---- Support / resistance ----------------------------------------------------------------------

def sr_df(o, h, l, c, pivots):
    df = frame(25, Open=o, High=h, Low=l, Close=c, ATR=1.0)
    df.attrs["pivots"] = (np.array([p[0] for p in pivots]), np.array([p[1] for p in pivots], dtype=float))
    return df


def test_support_bounce_buy_with_stop():
    df = sr_df(100.2, 100.8, 100.1, 100.5, [(5, 100.0), (10, 100.1), (12, 120.0)])
    side, stop = st.support_resistance(df, 20)
    assert side == "BUY" and stop == pytest.approx(99.8)   # min(low, level 100.0) - 0.2 ATR


def test_resistance_rejection_sell_with_stop():
    df = sr_df(119.8, 120.1, 119.2, 119.5, [(5, 120.0), (10, 120.2), (12, 80.0)])
    side, stop = st.support_resistance(df, 20)
    # both levels qualify with equal touches -> argmax picks the FIRST (120.0): max(120.1, 120.0) + 0.2
    assert side == "SELL" and stop == pytest.approx(120.3)


def test_sr_needs_up_close_and_close_clear_of_level():
    assert st.support_resistance(sr_df(100.6, 100.8, 100.1, 100.5, [(5, 100.0), (10, 100.1)]), 20) is None
    assert st.support_resistance(sr_df(100.1, 100.8, 100.1, 100.15, [(5, 100.0), (10, 100.1)]), 20) is None
    # close 100.25 clears level 100.0 + 0.2 ATR (but not 100.1 + 0.2): still a BUY off the 100.0 level
    assert st.support_resistance(sr_df(100.1, 100.8, 100.1, 100.25, [(5, 100.0), (10, 100.1)]), 20)[0] == "BUY"


def test_sr_no_levels():
    assert st.support_resistance(sr_df(100.2, 100.8, 100.1, 100.5, [(5, 100.0)]), 20) is None


def test_sr_prefers_most_touched_level():
    pivots = [(2, 100.0), (4, 100.25), (6, 100.5), (8, 100.5), (10, 100.55)]
    df = sr_df(100.6, 101.5, 100.3, 101.2, pivots)
    side, stop = st.support_resistance(df, 20)
    lv, t = st.levels_at(df, 20)
    assert lv.tolist() == [100.0, 100.25, 100.5, 100.5, 100.55] and t.tolist() == [2, 5, 4, 4, 4]
    assert side == "BUY"
    assert stop == pytest.approx(min(100.3, 100.25) - 0.2)  # most-touched level 100.25


# ---- registry ----------------------------------------------------------------------------------

def test_registry():
    assert list(st.STRATEGIES) == [
        "EMA 9/21 Cross", "20-bar Breakout", "Supertrend Flip", "MACD Trend Cross", "RSI Pullback",
        "Support-Resistance", "Demand-Supply", "Rally-Base-Rally", "Drop-Base-Rally", "Rally-Base-Drop",
        "Drop-Base-Drop", "Squeeze Breakout", "RSI(2) Reversion", "NR7 Breakout"]
    assert st.OPT_IN == set()
    assert st.DEFAULT_STRATEGIES == list(st.STRATEGIES)   # every strategy is on by default
    assert set(st.FAMILIES) == {"Demand-Supply", "Rally-Base-Rally", "Drop-Base-Rally", "Rally-Base-Drop",
                                "Drop-Base-Drop"}
    assert set(st.FAMILIES.values()) == {"Supply/Demand zone"}
    assert st.family_of("Rally-Base-Drop") == "Supply/Demand zone"
    assert st.family_of("EMA 9/21 Cross") == "EMA 9/21 Cross"
    assert st.family_of("anything") == "anything"
    assert len({st.family_of(n) for n in st.STRATEGIES}) == 10
    assert (st.LOOKBACK, st.STOP_BUFFER_ATR, st.SQUEEZE_MIN_BARS, st.NR_BARS) == (3, 0.2, 5, 7)


def test_describe_shape():
    d = st.describe()
    assert [x["name"] for x in d] == list(st.STRATEGIES)
    assert all(set(x) == {"name", "description", "family", "group", "default"} for x in d)
    by = {x["name"]: x for x in d}
    assert by["EMA 9/21 Cross"] == {"name": "EMA 9/21 Cross", "description": "EMA 9/21 crossover in direction of EMA50.",
                                    "family": "EMA 9/21 Cross", "group": "Indicators", "default": True}
    assert by["Support-Resistance"]["group"] == "Price action"
    assert by["Drop-Base-Drop"]["group"] == "Price action" and by["Drop-Base-Drop"]["family"] == "Supply/Demand zone"
    assert all(x["default"] for x in d)


def test_strategy_votes():
    df = frame(1, RSI2=5.0, Close=100.0, EMA200=90.0)
    assert scanner.strategy_votes(df, 0, ["RSI(2) Reversion"]) == {"RSI(2) Reversion": "BUY"}


# ---- golden master ------------------------------------------------------------------------------

IND_COLS = ["EMA9", "EMA21", "EMA50", "EMA200", "RSI", "MACD", "MACD_SIG", "ATR", "ADX", "ST_DIR", "VOL_AVG20",
            "TURNOVER20", "HH20", "LL20", "BB_UP", "BB_LO", "RSI2", "KC_UP", "KC_LO", "IDX_CLOSE", "IDX_EMA200", "RS"]


def _num(x, nd=6):
    x = float(x)
    return None if math.isnan(x) else round(x, nd)


def _vote(v):
    return [v[0], _num(v[1], 4)] if isinstance(v, tuple) else v


def _sig(s):
    return None if s is None else dict(
        i=None, side=s.side, entry=s.entry, sl=s.stop_loss, targets=s.targets, qty=s.qty, score=s.score,
        reasons=s.reasons, rsi=_num(s.rsi, 1), adx=_num(s.adx, 1), vol_ratio=_num(s.vol_ratio, 2),
        entry_type=s.entry_type, ref=s.ref_price, key=s.key)


def pipeline_snapshot(g, monkeypatch):
    df, votes, mkt = g["df"], g["votes"], g["market"]
    names = list(st.STRATEGIES)
    snap = {
        "indicators_sum": {c: _num(df[c].sum(), 4) for c in IND_COLS},
        "indicators_last": {c: _num(df[c].iloc[-1]) for c in IND_COLS},
        "squeeze_bars": int(df["SQUEEZE"].sum()),
        "zones": [[z.kind, z.formed, z.touch, bool(z.held), _num(z.proximal, 4), _num(z.distal, 4)]
                  for z in df.attrs["zones"]],
        "pivots": len(df.attrs["pivots"][0]),
        "votes": {n: {str(i): _vote(v) for i, v in enumerate(votes[n]) if v} for n in names},
    }
    variants = {
        "default": dict(),
        "all_min1_shorts": dict(min_score=1, strategies=names, shorts=True),
        "all_min1_filters": dict(min_score=1, strategies=names, filters=["trend", "rs"], shorts=True),
    }
    for vname, kw in variants.items():
        monkeypatch.setattr(config, "ALLOW_SHORTS", kw.pop("shorts", False))
        sel = kw.get("strategies") or st.DEFAULT_STRATEGIES
        out = []
        for i in range(200, len(df)):
            s = scanner.evaluate("SYM", df, i, market=mkt, votes={n: votes[n][i] for n in sel}, **kw)
            if s:
                d = _sig(s)
                d["i"] = i
                out.append(d)
        snap["signals_" + vname] = out
    return snap


@pytest.mark.parametrize("name", ["up_IN", "down_IN", "range_US"])
def test_golden_pipeline(name, golden_series, monkeypatch):
    snap = pipeline_snapshot(golden_series[name], monkeypatch)
    # sanity: the builders really exercise zones, S/R and several strategies
    assert snap["zones"] and snap["votes"]["Support-Resistance"]
    check_golden(f"pipeline_{name}", snap)


def test_golden_series_exercise_every_strategy(golden_series):
    fired = {n: 0 for n in st.STRATEGIES}
    for g in golden_series.values():
        for n, vs in g["votes"].items():
            fired[n] += sum(1 for v in vs if v)
    assert {n for n, c in fired.items() if c == 0} <= {"20-bar Breakout", "Squeeze Breakout"}, fired


def test_cached_votes_equal_live_strategy_votes(golden_series):
    g = golden_series["up_IN"]
    names = list(st.STRATEGIES)
    for i in range(200, len(g["df"]), 37):
        assert scanner.strategy_votes(g["df"], i, names) == {n: g["votes"][n][i] for n in names}
