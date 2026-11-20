"""MCX commodity futures: contracts, recorded bars, the world-market proxy, lot sizing and the alert."""
from datetime import date, datetime

import pandas as pd
import pytest

from algotrade import config, data, livefeed, markets, mcx, scanner, telegram_bot, universe
from conftest import eval_frame, make_signal

IST = mcx.IST

MASTER = """pSymbol,pInstType,pSymbolName,pTrdSymbol,lLotSize,pExpiryDate
576264,FUTCOM,CRUDEOIL,CRUDEOIL18DEC26FUT,100,1
576265,FUTCOM,CRUDEOIL,CRUDEOIL19JAN27FUT,100,1
495213,FUTCOM,GOLD,GOLD04DEC26FUT,1,1
574319,FUTCOM,NATURALGAS,NATURALGAS24NOV26FUT,1250,1
574320,FUTCOM,NATURALGAS,NATURALGAS28DEC26FUT,1250,1
600001,OPTFUT,CRUDEOIL,CRUDEOIL18DEC267000CE,100,1
600002,FUTCOM,CARDAMOM,CARDAMOM15DEC26FUT,100,1
"""


@pytest.fixture
def master(monkeypatch):
    df = mcx.parse_contracts(MASTER)
    monkeypatch.setattr(mcx, "contracts", lambda: df)
    return df


# ---- the market ---------------------------------------------------------------------------------

def test_mcx_is_a_market_with_its_own_session_and_no_index():
    m = markets.get("mcx")
    assert (m.code, m.currency, m.tz, m.index_symbol) == ("MCX", "₹", "Asia/Kolkata", "")
    assert m.session[0].strftime("%H:%M") == "09:00" and m.session[1].strftime("%H:%M") == "23:55"
    assert m.min_price == 0 and m.min_turnover == 0
    assert m.in_scan_window(datetime(2026, 11, 20, 21, 30, tzinfo=IST))         # evening session
    assert not m.in_scan_window(datetime(2026, 11, 21, 12, 0, tzinfo=IST))      # Saturday


def test_universe_is_the_liquid_commodities():
    assert universe.get_universe(market="MCX") == ["CRUDEOIL", "NATURALGAS", "GOLD", "SILVER", "COPPER"]
    assert universe.get_universe("gold, cotton", market="MCX") == ["GOLD"]


def test_fetch_ohlcv_goes_to_mcx_not_yahoo(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcx, "fetch_ohlcv", lambda s, tf, stop=None: seen.setdefault("args", (s, tf)) and {})
    data.fetch_ohlcv(["GOLD"], "15m", market="MCX")
    assert seen["args"] == (["GOLD"], "15m")


def test_no_index_means_no_index_download(monkeypatch):
    monkeypatch.setattr(scanner, "fetch_ohlcv", lambda *a, **k: pytest.fail("no index to fetch"))
    assert scanner.fetch_index("15m", "MCX") is None


# ---- contracts ------------------------------------------------------------------------------------

def test_contracts_keep_only_futures_of_the_commodities(master):
    assert sorted(master["tsym"]) == ["CRUDEOIL18DEC26FUT", "CRUDEOIL19JAN27FUT", "GOLD04DEC26FUT",
                                      "NATURALGAS24NOV26FUT", "NATURALGAS28DEC26FUT"]
    assert master.set_index("tsym").loc["CRUDEOIL18DEC26FUT", "expiry"] == date(2026, 12, 18)


@pytest.mark.parametrize("today, expected", [
    (date(2026, 11, 20), "NATURALGAS24NOV26FUT"),     # 4 days to expiry: still the front month
    (date(2026, 11, 21), "NATURALGAS28DEC26FUT"),     # 3 days: rolled to the next
    (date(2026, 12, 27), "NATURALGAS28DEC26FUT"),     # nothing later listed: the nearest left
    (date(2026, 12, 29), None),                       # all expired
])
def test_front_rolls_before_expiry(master, today, expected):
    c = mcx.front("NATURALGAS", today)
    assert (c["tsym"] if c else None) == expected


def test_gold_moves_100_rupees_per_lot_per_rupee(master):
    assert mcx.lot_size("CRUDEOIL") == 100 and mcx.point_value("CRUDEOIL") == 100
    assert mcx.lot_size("GOLD") == 1 and mcx.point_value("GOLD") == 100      # 1 kg, quoted per 10 g
    assert mcx.point_value("NATURALGAS") == 1250


# ---- recorded bars --------------------------------------------------------------------------------

def tick(rec, hh, mm, ss, price, cum_vol, key="mcx_fo|576264"):
    rec.on_tick(key, {"ltp": price, "last_volume": cum_vol}, now=datetime(2026, 11, 20, hh, mm, ss, tzinfo=IST))


def test_recorder_builds_5_minute_bars_from_ticks():
    rec = mcx.Recorder()
    rec.set_contracts({"CRUDEOIL": {"token": "576264", "tsym": "CRUDEOIL18DEC26FUT"}})
    tick(rec, 10, 0, 5, 7600, 1000)       # first tick: no volume yet (no earlier day total to compare)
    tick(rec, 10, 2, 0, 7620, 1030)
    tick(rec, 10, 4, 59, 7590, 1050)
    tick(rec, 10, 5, 1, 7605, 1060)       # next bar starts: the 10:00 bar is saved
    tick(rec, 10, 6, 0, 7608, 1060, key="mcx_fo|999")   # not a watched contract: ignored
    rec.flush_due(datetime(2026, 11, 20, 10, 11, tzinfo=IST))
    df = mcx.recorded("CRUDEOIL")
    assert list(df.index.strftime("%H:%M")) == ["10:00", "10:05"]
    assert df.iloc[0].to_dict() == {"Open": 7600, "High": 7620, "Low": 7590, "Close": 7590, "Volume": 50}
    assert df.iloc[1]["Close"] == 7605 and df.iloc[1]["Volume"] == 10


def test_recorder_ignores_ticks_outside_the_session():
    rec = mcx.Recorder()
    rec.set_contracts({"GOLD": {"token": "495213", "tsym": "GOLD04DEC26FUT"}})
    rec.on_tick("mcx_fo|495213", {"ltp": 122000}, now=datetime(2026, 11, 20, 8, 59, tzinfo=IST))
    rec.on_tick("mcx_fo|495213", {"ltp": 122000}, now=datetime(2026, 11, 21, 10, 0, tzinfo=IST))   # Saturday
    assert rec.bars == {}


def test_live_feed_passes_every_update_to_listeners(monkeypatch):
    got = []
    monkeypatch.setattr(livefeed, "_listeners", [])
    livefeed.add_listener(lambda k, q: got.append((k, q["ltp"])))

    class Msg:
        type, exchange_segment, instrument_token = "scrip", "mcx_fo", "576264"
        last_traded_price, open_price, high_price, low_price, close_price = 7600.0, 1, 1, 1, 1
        volume_traded_today = open_interest = net_change = net_change_percent = 0
        last_trade_time = last_update_time = None
        buy = sell = []
    livefeed.on_message(Msg())
    assert got == [("mcx_fo|576264", 7600.0)]


def test_resample_cuts_bars_into_timeframes():
    idx = pd.date_range("2026-11-20 09:00", periods=12, freq="5min")
    df = pd.DataFrame({"Open": range(12), "High": range(1, 13), "Low": range(12), "Close": range(12),
                       "Volume": [1] * 12}, index=idx, dtype=float)
    h = mcx.resample(df, "1h")
    assert len(h) == 1 and h.iloc[0].to_dict() == {"Open": 0, "High": 12, "Low": 0, "Close": 11, "Volume": 12}
    assert len(mcx.resample(df, "15m")) == 4


# ---- the world-market proxy -----------------------------------------------------------------------

def proxy_frame(n, start="2026-11-01 09:00", price=80.0, freq="15min", volume=100.0):
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"Open": price, "High": price + 1, "Low": price - 1, "Close": price, "Volume": volume},
                        index=idx, dtype=float)


def test_proxy_only_is_scaled_to_todays_mcx_price():
    out, src = mcx.combine(pd.DataFrame(), proxy_frame(100, price=80.0), anchor=7600.0)
    assert src == "proxy" and out["Close"].iloc[-1] == pytest.approx(7600.0) and len(out) == 100


def test_recorded_bars_take_over_where_recording_began():
    prox = proxy_frame(100, price=80.0, volume=1000.0)
    real = proxy_frame(10, start=str(prox.index[90]), price=7200.0, volume=5.0)
    out, src = mcx.combine(real, prox)
    assert src == "blend" and len(out) == 100                 # 90 proxy bars before the first recorded one
    assert out["Close"].iloc[0] == pytest.approx(7200.0)      # scaled at the seam
    assert out["Volume"].iloc[0] == pytest.approx(5.0)        # proxy volume scaled to MCX's
    assert (out.index[90:] == real.index).all()


def test_enough_recorded_bars_drop_the_proxy():
    real = proxy_frame(mcx.MIN_BARS, price=7200.0)
    out, src = mcx.combine(real, proxy_frame(500))
    assert src == "mcx" and out is real


def test_proxy_keeps_only_mcx_hours_on_weekdays():
    df = proxy_frame(96, start="2026-11-20 00:00")             # Friday, round the clock
    df = pd.concat([df, proxy_frame(4, start="2026-11-21 10:00")])   # Saturday
    kept = mcx.to_mcx_hours(df, "15m")
    assert kept.index.min().strftime("%H:%M") == "09:00" and kept.index.max().strftime("%H:%M") == "23:45"
    assert (kept.index.dayofweek < 5).all()


# ---- signals --------------------------------------------------------------------------------------

def evaluate_mcx(symbol, side="SELL"):
    names = ["EMA 9/21 Cross", "RSI Pullback"]
    return scanner.evaluate(symbol, eval_frame(), min_score=2, strategies=names, timeframe="15m", market="MCX",
                            filters=[], votes={n: side for n in names})


@pytest.mark.parametrize("symbol, lots, lot_risk", [
    ("CRUDEOIL", 3, 300.0),     # stop 3 (1.5 x ATR 2) x 100 bbl = Rs 300 a lot; 1% of 1 lakh = Rs 1,000 -> 3 lots
    ("GOLD", 3, 300.0),         # 1 kg lot quoted per 10 g: x100
    ("NATURALGAS", 0, 3750.0),  # one lot risks more than the 1%: no lots, the alert says so
])
def test_signals_are_sized_in_whole_lots_and_can_be_sells(master, monkeypatch, symbol, lots, lot_risk):
    monkeypatch.setattr(config, "ALLOW_SHORTS", False)       # stocks can't short, futures can
    sig = evaluate_mcx(symbol)
    assert sig.side == "SELL" and sig.market == "MCX" and sig.key.startswith(f"MCX|{symbol}|SELL|15m|")
    assert sig.qty == lots and sig.extra["lot_risk"] == lot_risk


def test_alert_shows_lots_contract_and_estimated_prices(master, monkeypatch):
    monkeypatch.setattr(telegram_bot, "_mcx_contract", lambda s: "CRUDEOIL18DEC26FUT")
    sig = make_signal(symbol="CRUDEOIL", market="MCX", side="SELL", entry=7600, stop_loss=7630,
                      targets=[7555, 7510], qty=3, timeframe="15m", entry_type="stop",
                      extra={"lot": 100, "point_value": 100.0, "lot_risk": 3000.0, "data": "proxy"})
    text = telegram_bot.format_signal(sig)
    assert "CRUDEOIL18DEC26FUT" in text and "Lots" in text and "₹9,000" in text
    assert "world-market prices scaled to MCX" in text and "alert only, no orders" in text
    assert "Short: intraday cash" not in text


def test_alert_warns_when_one_lot_is_too_big(master, monkeypatch):
    monkeypatch.setattr(telegram_bot, "_mcx_contract", lambda s: "")
    sig = make_signal(symbol="NATURALGAS", market="MCX", qty=0, timeframe="15m",
                      extra={"lot": 1250, "point_value": 1250.0, "lot_risk": 3750.0, "data": "mcx"})
    text = telegram_bot.format_signal(sig)
    assert "One lot risks ₹3,750 (3.8% of capital)" in text and "Prices: MCX prices" in text


def test_mcx_never_auto_trades():
    import main
    assert "MCX" in markets.MARKETS
    # main.run_scan hands new signals to Auto-trade only for India
    src = open(main.__file__, encoding="utf-8").read()
    assert 'if code == "IN" and (new := [' in src
