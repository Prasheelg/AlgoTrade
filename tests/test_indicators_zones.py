"""P5: indicators and zones (known values + structural checks on injected patterns)."""
import math

import numpy as np
import pandas as pd
import pytest

from algotrade import indicators as ind
from algotrade import zones
from conftest import inject_zone, make_ohlcv, ZONE_RETEST_OFFSET


def s(*v):
    return pd.Series(v, dtype=float)


def ohlc(rows, start="2024-01-01"):
    idx = pd.bdate_range(start, periods=len(rows))
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close"], dtype=float)


# ---- indicators ---------------------------------------------------------------------------------

def test_ema_and_sma_known_values():
    e = ind.ema(s(1, 2, 3), 2)  # alpha = 2/3, adjust=False
    assert e.tolist() == pytest.approx([1.0, 5 / 3, 23 / 9])
    m = ind.sma(s(1, 2, 3, 4), 3)
    assert math.isnan(m[1]) and m[2:].tolist() == [2.0, 3.0]


def test_rsi_edges_fillna_100():
    assert ind.rsi(s(*range(1, 30))).tolist() == [100.0] * 29          # no losses -> 100
    down = ind.rsi(s(*range(30, 1, -1)))
    assert down.iloc[0] == 100.0                                          # first bar (NaN delta) -> 100
    assert (down.iloc[1:] == 0.0).all()                                   # no gains -> 0
    flat = ind.rsi(s(*[5.0] * 10))
    assert flat.tolist() == [100.0] * 10                                  # 0/0 -> NaN -> 100


def test_rsi_known_value():
    r = ind.rsi(s(1, 2, 1), n=1)  # alpha 1: gain/loss are just the last delta
    assert r.tolist() == [100.0, 100.0, 0.0]


def test_macd_returns_line_signal_hist():
    c = s(*np.linspace(100, 120, 60))
    line, sig, hist = ind.macd(c)
    assert (hist == line - sig).all()
    assert line.iloc[-1] > 0 and sig.iloc[-1] > 0


def test_atr_constant_range_and_gap():
    df = ohlc([(10, 11, 9, 10)] * 5)
    assert ind.atr(df).tolist() == [2.0] * 5
    df = ohlc([(10, 11, 9, 10), (15, 16, 14, 15)])  # gap: TR = 16 - 10 = 6
    assert ind.atr(df, n=1).tolist() == [2.0, 6.0]


def test_supertrend_direction():
    up = ohlc([(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(40)])
    assert ind.supertrend(up).iloc[-1] == 1
    down = ohlc([(200 - 3 * i, 201 - 3 * i, 199 - 3 * i, 199.5 - 3 * i) for i in range(40)])
    d = ind.supertrend(down)
    assert d.iloc[0] == 1 and d.iloc[-1] == -1
    assert set(d.unique()) <= {1.0, -1.0}


def test_adx_trending_vs_flat():
    up = ohlc([(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(60)])
    assert ind.adx(up).iloc[-1] == pytest.approx(100.0)
    flat = ohlc([(100, 101, 99, 100)] * 30)
    assert ind.adx(flat).isna().all()  # +DI = -DI = 0 -> 0/NaN


ALL_COLS = ["Open", "High", "Low", "Close", "Volume", "EMA9", "EMA21", "EMA50", "EMA200", "RSI", "MACD",
            "MACD_SIG", "MACD_HIST", "ATR", "ADX", "ST_DIR", "VOL_AVG20", "TURNOVER20", "HH20", "LL20", "BB_UP",
            "BB_LO", "RSI2", "KC_UP", "KC_LO", "SQUEEZE", "IDX_CLOSE", "IDX_EMA200", "RS"]


def test_add_all_columns_attrs_and_no_index():
    raw = make_ohlcv(260, seed=1)
    df = ind.add_all(raw)
    assert list(df.columns) == ALL_COLS
    assert list(raw.columns) == ["Open", "High", "Low", "Close", "Volume"]  # input untouched
    assert df[["IDX_CLOSE", "IDX_EMA200", "RS"]].isna().all().all()
    assert set(df.attrs) == {"zones", "zones_by_touch", "pivots"}
    assert df["SQUEEZE"].dtype == bool
    assert df["HH20"].iloc[25] == raw["High"].iloc[5:25].max()  # previous 20 bars, shifted
    assert df["TURNOVER20"].iloc[19] == pytest.approx((raw["Close"] * raw["Volume"]).iloc[:20].mean())
    # an empty index series is treated as "no index"
    assert ind.add_all(raw, pd.Series(dtype=float))["RS"].isna().all()


def test_add_all_with_index_rs_and_ffill():
    raw = make_ohlcv(150, seed=1)
    idx = make_ohlcv(150, seed=2, price=20000)["Close"].iloc[::2]  # every other bar -> ffill
    df = ind.add_all(raw, idx)
    full = idx.reindex(raw.index, method="ffill")
    assert df["IDX_CLOSE"].equals(full)
    i = 100
    exp = (raw["Close"].iloc[i] / raw["Close"].iloc[i - 63]) / (full.iloc[i] / full.iloc[i - 63])
    assert df["RS"].iloc[i] == pytest.approx(exp)
    assert df["RS"].iloc[:63].isna().all()
    # EMA200 of the index is computed on the index's own bars, then forward-filled
    assert df["IDX_EMA200"].iloc[1] == df["IDX_EMA200"].iloc[0]


# ---- zones ----------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,side", [("RBR", "BUY"), ("DBR", "BUY"), ("RBD", "SELL"), ("DBD", "SELL")])
def test_injected_zone_is_found_and_retest_held(kind, side):
    raw = inject_zone(make_ohlcv(400, seed=5), 300, kind)
    df = ind.add_all(raw)
    z = [z for z in df.attrs["zones"] if z.formed == 303]
    assert len(z) == 1
    z = z[0]
    assert (z.kind, z.side, z.touch, z.held) == (kind, side, 300 + ZONE_RETEST_OFFSET, True)
    assert (z.proximal > z.distal) if side == "BUY" else (z.proximal < z.distal)
    assert z in df.attrs["zones_by_touch"][z.touch]
    base = raw.iloc[301:303]
    if side == "BUY":
        assert z.distal == base["Low"].min() and z.proximal == base[["Open", "Close"]].max(axis=1).max()
    else:
        assert z.distal == base["High"].max() and z.proximal == base[["Open", "Close"]].min(axis=1).min()


def test_zone_not_held_is_excluded_from_by_touch():
    raw = inject_zone(make_ohlcv(400, seed=5), 300, "RBR")
    t = 300 + ZONE_RETEST_OFFSET
    lo = raw["Low"].iloc[t]
    raw.iloc[t, raw.columns.get_loc("Close")] = lo + 0.01  # closes in the lower half -> not held
    raw.iloc[t, raw.columns.get_loc("Open")] = raw["High"].iloc[t] - 0.01
    df = ind.add_all(raw)
    z = next(z for z in df.attrs["zones"] if z.formed == 303)
    assert z.touch == t and not z.held
    assert z not in df.attrs["zones_by_touch"].get(t, [])


def test_zone_never_touched_within_max_age():
    raw = make_ohlcv(300, seed=5)
    raw = inject_zone(raw, 250, "RBR")
    # lift all bars after the leg-out far above the zone -> never retested
    raw.iloc[254:, :4] = raw.iloc[254:, :4] * 1.5
    df = ind.add_all(raw)
    z = next(z for z in df.attrs["zones"] if z.formed == 253)
    assert z.touch == -1 and z.held is False


def test_zone_constants():
    assert (zones.BORING_BODY, zones.EXPLOSIVE_BODY, zones.EXPLOSIVE_RANGE_ATR, zones.MAX_BASE,
            zones.MAX_ZONE_HEIGHT_ATR, zones.MAX_ZONE_AGE) == (0.5, 0.6, 1.1, 4, 2.0, 120)
    assert (zones.PIVOT_K, zones.SR_WINDOW, zones.LEVEL_TOL, zones.MIN_TOUCHES) == (5, 250, 0.3, 2)


def test_find_pivots_single_peak_and_trough():
    h = [10.0] * 21
    l = [9.0] * 21
    h[10] = 15.0
    l[12] = 5.0
    df = pd.DataFrame({"High": h, "Low": l})
    idx, price = zones.find_pivots(df)
    # flat lows at 5 and 6 equal their window minimum (the 5.0 trough is outside it) -> also pivots
    assert list(zip(idx.tolist(), price.tolist())) == [(5, 9.0), (6, 9.0), (10, 15.0), (12, 5.0)]


def test_find_pivots_flat_series_every_full_window_is_pivot():
    """Quirk-ish: equal highs all count as swing highs (== rolling max)."""
    df = pd.DataFrame({"High": [10.0] * 12, "Low": [9.0] * 12})
    idx, price = zones.find_pivots(df)
    assert idx.tolist() == [5, 5, 6, 6]


def test_levels_at():
    df = pd.DataFrame({"ATR": [1.0] * 40})
    df.attrs["pivots"] = (np.array([5, 10, 15, 30]), np.array([100.0, 100.25, 110.0, 100.1]))
    p, t = zones.levels_at(df, 20)          # pivot 30 not known yet (needs i - 5)
    assert p.tolist() == [100.0, 100.25] and t.tolist() == [2, 2]
    p, t = zones.levels_at(df, 35)
    assert p.tolist() == [100.0, 100.25, 100.1] and t.tolist() == [3, 3, 3]
    p, t = zones.levels_at(df, 12)          # only one known pivot
    assert p.tolist() == [] and t.tolist() == []
    df.attrs["pivots"] = (np.array([5, 10]), np.array([100.0, 101.0]))
    assert zones.levels_at(df, 20)[0].tolist() == []   # too far apart: no level


def test_annotate_requires_atr():
    with pytest.raises(KeyError):
        zones.annotate(pd.DataFrame({"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0]}))
