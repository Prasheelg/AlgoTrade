"""options_auto: signals bought as OTM options, exited on the underlying. Kotak and the option chain are faked."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from algotrade import broker, config, fno, options_auto as oa, orders, scanner, state
from algotrade.universe import to_yahoo
from conftest import eval_frame, make_signal
from test_options_manual import fo  # noqa: F401  (fixture)
from test_orders import FakeKotak

IST = ZoneInfo("Asia/Kolkata")


class FakeMarket:
    code = "IN"

    def __init__(self):
        self.at, self.open = datetime(2026, 10, 21, 11, 0, tzinfo=IST), True

    def now(self):
        return self.at

    def is_open(self, now=None):
        return self.open

    def is_after_close(self, now=None):
        return not self.open and self.at.hour >= 15


@pytest.fixture
def opt(monkeypatch):
    fake = FakeKotak()
    monkeypatch.setattr(broker, "call", fake)
    mkt = FakeMarket()
    monkeypatch.setattr(oa, "_mkt", lambda: mkt)
    msgs = []
    monkeypatch.setattr(orders, "_notify", lambda text: msgs.append(text))
    for k, v in dict(OPTIONS_AUTO_TRADE=True, OPTIONS_OTM_STEPS=2, OPTIONS_MIN_DAYS=5, OPTIONS_MAX_PREMIUM=10000.0,
                     OPTIONS_MAX_OPEN_PREMIUM=30000.0, OPTIONS_MAX_SPREAD_PCT=5.0, OPTION_PROTECTION_PCT=5.0,
                     OPTIONS_MAX_LOTS=10, INTRADAY_AUTO_TRADE=False).items():
        monkeypatch.setattr(config, k, v)
    state.set_setting(broker.TRADING_KEY, {"enabled": True})
    fake.spot = {"RELIANCE": 100.0, "NIFTY": 25000.0}
    fake.quote = {"bid": 9.9, "ask": 10.0, "ltp": 9.95}
    fake.picked = []
    monkeypatch.setattr(fno, "underlyings", lambda: [{"name": "NIFTY"}, {"name": "RELIANCE"}])
    monkeypatch.setattr(oa, "_spot", lambda u: fake.spot.get(u))

    def otm(u, o, spot_px, steps, min_days):
        fake.picked.append((u, o, spot_px, steps, min_days))
        return {"tsym": f"{u}TEST{o}", "expiry": "2026-10-28", "strike": 110.0, "lot": 250, "tick": 0.05}
    monkeypatch.setattr(fno, "otm_contract", otm)
    monkeypatch.setattr(fno, "option_quote", lambda tsym: dict(fake.quote))
    monkeypatch.setattr(fno, "contract", lambda tsym: {"tick": 0.05})
    fake.mkt, fake.msgs = mkt, msgs
    return fake


def signal(symbol="RELIANCE", **kw):
    """A limit BUY at 99, stop 96, target 103.5 (make_signal) recorded as a scanner signal; returns its key."""
    s = make_signal(symbol=symbol, **kw)
    state.record([s])
    return s.key


def set_status(key, status):
    with state._conn() as con:
        con.execute("UPDATE signals SET status = ? WHERE key = ?", (status, key))


# ---- contract choice ------------------------------------------------------------------------------

def test_otm_contract_skips_the_nearest_strike_and_near_expiries(fo):  # noqa: F811
    now = datetime.now(IST)
    # NIFTY at 22990: calls above it are 23000, 23050, 23100 on the 4-day expiry
    assert fno.otm_contract("NIFTY", "CE", 22990, steps=2, min_days=0, now=now)["strike"] == 23050
    assert fno.otm_contract("NIFTY", "PE", 22990, steps=2, min_days=0, now=now)["strike"] == 22900
    assert fno.otm_contract("NIFTY", "CE", 22990, steps=1, min_days=0, now=now)["strike"] == 23000
    # 5+ days: only the 11-day expiry counts, which has one call strike (23000)
    c = fno.otm_contract("NIFTY", "CE", 22990, steps=1, min_days=5, now=now)
    assert (c["strike"], c["tsym"]) == (23000, "NIFTY26OCT23000CE")
    assert fno.otm_contract("NIFTY", "CE", 22990, steps=2, min_days=5, now=now) is None


# ---- new signals ----------------------------------------------------------------------------------

def test_new_signals_wait_for_the_entry(opt):
    keys = [signal(), signal("NIFTY", side="SELL", entry=25100.0, stop_loss=25300.0, targets=[24800.0, 24500.0],
                             entry_type="stop"), signal("NOFNO")]
    made = oa.trade_new(keys)
    assert [(t["underlying"], t["opt"], t["state"], t["product"]) for t in made] == \
        [("RELIANCE", "CE", "WAIT", "NRML"), ("NIFTY", "PE", "WAIT", "NRML")]   # NOFNO has no options
    assert opt.placed == []


def test_nothing_when_options_or_auto_trade_off(opt, monkeypatch):
    monkeypatch.setattr(config, "OPTIONS_AUTO_TRADE", False)
    assert oa.trade_new([signal()]) == []
    monkeypatch.setattr(config, "OPTIONS_AUTO_TRADE", True)
    state.set_setting(broker.TRADING_KEY, {"enabled": False})
    assert oa.trade_new([signal()]) == [] and oa.all_trades() == []


def test_intraday_needs_the_intraday_switch(opt):
    t = oa.trade_new([signal(timeframe="15m")])[0]
    assert t["state"] == "SKIPPED" and "intraday" in t["note"]


def test_one_trade_per_underlying(opt):
    oa.trade_new([signal()])
    t = oa.trade_new([signal(timeframe="1d", side="BUY", bar_time=__import__("pandas").Timestamp("2024-01-02"))])[0]
    assert t["state"] == "SKIPPED" and "already trading RELIANCE" in t["note"]


# ---- entry ------------------------------------------------------------------------------------------

def test_buys_the_call_when_the_underlying_reaches_the_entry(opt):
    key = signal()
    oa.trade_new([key])
    oa.step_all()                       # 100 > 99: a limit BUY entry isn't reached yet
    assert opt.placed == []
    opt.spot["RELIANCE"] = 98.9
    oa.step_all()
    t = oa._get(key)
    p = opt.placed[0]
    # ask 10 + 5% = 10.50; ₹10,000 / (10.50 × 250) = 3 lots
    assert (t["state"], t["tsym"], t["lots"], t["qty"], t["buy_price"]) == ("BUYING", "RELIANCETESTCE", 3, 750, 10.5)
    assert {k: p[k] for k in ("exchange_segment", "product", "order_type", "price", "quantity", "transaction_type",
                               "trading_symbol", "amo")} == \
        {"exchange_segment": "nse_fo", "product": "NRML", "order_type": "L", "price": "10.50", "quantity": "750",
         "transaction_type": "B", "trading_symbol": "RELIANCETESTCE", "amo": "NO"}
    assert opt.picked == [("RELIANCE", "CE", 98.9, 2, 5)]
    assert "BUY RELIANCETESTCE" in opt.msgs[0]


def test_sell_signal_buys_a_put_on_a_breakdown(opt):
    key = signal("NIFTY", side="SELL", entry=25100.0, stop_loss=25300.0, targets=[24800.0, 24500.0], entry_type="stop")
    oa.trade_new([key])
    opt.spot["NIFTY"] = 25150.0
    oa.step_all()
    assert opt.placed == []             # a SELL stop entry needs price at or below 25100
    opt.spot["NIFTY"] = 25090.0
    oa.step_all()
    assert oa._get(key)["tsym"] == "NIFTYTESTPE" and opt.placed[0]["transaction_type"] == "B"


def test_no_entry_while_market_shut(opt):
    key = signal()
    oa.trade_new([key])
    opt.mkt.open = False
    opt.spot["RELIANCE"] = 98.0
    oa.step_all()
    assert opt.placed == [] and oa._get(key)["state"] == "WAIT"


def test_skips_wide_spreads_and_costly_lots(opt, monkeypatch):
    k1 = signal()
    oa.trade_new([k1])
    opt.spot["RELIANCE"] = 98.0
    opt.quote = {"bid": 8.0, "ask": 10.0, "ltp": 9.0}
    oa.step_all()
    assert oa._get(k1)["state"] == "SKIPPED" and "spread too wide" in oa._get(k1)["note"]
    k2 = signal("NIFTY")
    oa.trade_new([k2])
    opt.spot["NIFTY"] = 98.0
    opt.quote = {"bid": 99.0, "ask": 100.0}
    oa.step_all()   # one lot = 250 × 105 > ₹10,000
    assert oa._get(k2)["state"] == "SKIPPED" and "costs more than OPTIONS_MAX_PREMIUM" in oa._get(k2)["note"]
    assert opt.placed == []


def test_open_premium_cap_cuts_lots(opt, monkeypatch):
    monkeypatch.setattr(config, "OPTIONS_MAX_OPEN_PREMIUM", 6000.0)
    key = signal()
    oa.trade_new([key])
    opt.spot["RELIANCE"] = 98.0
    oa.step_all()
    assert oa._get(key)["lots"] == 2   # 2 × 2,625 fits under ₹6,000


def test_cancelled_when_signal_ends_or_price_past_stop(opt):
    k1 = signal()
    oa.trade_new([k1])
    set_status(k1, "CANCELLED")
    oa.step_all()
    assert oa._get(k1)["state"] == "CANCELLED"
    k2 = signal("NIFTY")
    oa.trade_new([k2])
    opt.spot["NIFTY"] = 95.0          # below the 96 stop
    oa.step_all()
    assert oa._get(k2)["state"] == "CANCELLED" and "past the stop" in oa._get(k2)["note"]


def test_unfilled_buy_goes_back_to_waiting(opt, monkeypatch):
    key = signal()
    oa.trade_new([key])
    opt.spot["RELIANCE"] = 98.0
    oa.step_all()
    oa._save(key, buy_at=(datetime.now() - timedelta(seconds=oa.REPRICE_SEC + 5)).isoformat(timespec="seconds"))
    opt.spot["RELIANCE"] = 100.0      # moved away: no new buy until the entry is reached again
    oa.step_all()
    t = oa._get(key)
    assert opt.cancelled == ["101"] and t["state"] == "WAIT" and t["buy_order"] is None and len(opt.placed) == 1


def test_rejected_buy(opt):
    key = signal()
    oa.trade_new([key])
    opt.spot["RELIANCE"] = 98.0
    oa.step_all()
    opt.book["101"].update(ordSt="rejected", rejRsn="F&O segment not active")
    oa.step_all()
    assert oa._get(key)["state"] == "REJECTED" and "F&O segment" in opt.msgs[-1]


# ---- exits ------------------------------------------------------------------------------------------

def _open(opt, key):
    oa.trade_new([key])
    opt.spot["RELIANCE"] = 98.0
    oa.step_all()
    opt.fill("101", 750, 10.4)
    oa.step_all()
    t = oa._get(key)
    assert (t["state"], t["qty"], t["fill_price"]) == ("OPEN", 750, 10.4)
    return t


def test_sold_when_the_underlying_hits_the_stop(opt):
    key = signal()
    _open(opt, key)
    opt.spot["RELIANCE"] = 96.5
    oa.step_all()
    assert oa._get(key)["state"] == "OPEN" and len(opt.placed) == 1
    opt.spot["RELIANCE"] = 95.9
    opt.quote = {"bid": 6.0, "ask": 6.1}
    oa.step_all()
    t, p = oa._get(key), opt.placed[-1]
    assert t["state"] == "SELLING" and t["note"].startswith("stop-loss")
    assert (p["transaction_type"], p["price"], p["quantity"]) == ("S", "5.70", "750")   # bid 6 - 5%
    opt.fill(p["oid"], 750, 6.0)
    oa.step_all()
    t = oa._get(key)
    assert (t["state"], t["exit_price"]) == ("CLOSED", 6.0) and "P&L ₹-3,300" in opt.msgs[-1]


def test_sold_at_the_target_and_on_time_exit(opt):
    key = signal()
    _open(opt, key)
    opt.spot["RELIANCE"] = 103.6
    oa.step_all()
    assert oa._get(key)["note"].startswith("target")
    k2 = signal("NIFTY")
    oa.trade_new([k2])
    opt.spot["NIFTY"] = 98.0
    oa.step_all()
    opt.fill(oa._get(k2)["buy_order"], 250, 10.0)
    oa.step_all()
    set_status(k2, "EXPIRED")
    oa.step_all()
    assert oa._get(k2)["state"] == "SELLING" and oa._get(k2)["note"] == "signal expired"


def test_sold_on_expiry_day_afternoon(opt):
    key = signal()
    _open(opt, key)
    opt.mkt.at = datetime(2026, 10, 28, 15, 1, tzinfo=IST)
    oa.step_all()
    assert oa._get(key)["note"] == "expiry day"


def test_unfilled_sell_is_repriced(opt):
    key = signal()
    _open(opt, key)
    opt.spot["RELIANCE"] = 95.0
    oa.step_all()
    oid = oa._get(key)["sell_order"]
    oa._save(key, sell_at=(datetime.now() - timedelta(seconds=oa.REPRICE_SEC + 5)).isoformat(timespec="seconds"))
    oa.step_all()
    assert oid in opt.cancelled and oa._get(key)["sell_order"] != oid and oa._get(key)["state"] == "SELLING"


def test_close_by_hand(opt):
    k1 = signal()
    _open(opt, k1)
    t = oa.close_trade(k1)
    assert t["state"] == "SELLING" and t["note"] == "closed by you"
    k2 = signal("NIFTY")
    oa.trade_new([k2])
    assert oa.close_trade(k2)["state"] == "CANCELLED"
    with pytest.raises(ValueError):
        oa.close_trade(k2)


def test_option_orders_show_as_auto_trade(opt):
    from algotrade import orderbook
    key = signal()
    _open(opt, key)
    assert orderbook.sources()["101"]["label"] == "Auto-trade · option buy"


# ---- scanner and the stock trader -----------------------------------------------------------------

def test_index_symbols_map_to_yahoo():
    assert to_yahoo("NIFTY") == "^NSEI" and to_yahoo("BANKNIFTY") == "^NSEBANK" and to_yahoo("SBIN") == "SBIN.NS"


def test_index_signal_ignores_volume_and_sell_needs_options(monkeypatch):
    names = ["EMA 9/21 Cross", "RSI Pullback"]
    df = eval_frame(TURNOVER20=0.0, Volume=0.0, VOL_AVG20=0.0, RS=1.0)
    votes = {n: "SELL" for n in names}
    kw = dict(strategies=names, min_score=2, filters=["rs"], votes=votes)
    assert scanner.evaluate("NIFTY", df, **kw) is None              # SELLs off without ALLOW_SHORTS / options
    monkeypatch.setattr(config, "OPTIONS_AUTO_TRADE", True)
    s = scanner.evaluate("NIFTY", df, **kw)
    assert s is not None and s.side == "SELL"
    assert scanner.evaluate("SBIN", df, **kw) is None              # a stock still needs turnover


def test_stock_trader_skips_indices():
    sig = {"market": "IN", "side": "BUY", "status": "PENDING", "timeframe": "1d", "symbol": "NIFTY"}
    assert "index" in orders._eligible(sig)


def test_trade_splits_stocks_and_options(opt, monkeypatch):
    import main
    got = {}
    monkeypatch.setattr(orders, "trade_new", lambda keys, manage=True: got.setdefault("stocks", keys))
    buy, sell, idx = signal(), signal("SBIN", side="SELL"), signal("NIFTY")
    main.trade([buy, sell, idx])
    assert got["stocks"] == [buy]                          # stocks: BUYs on shares only
    assert {t["key"] for t in oa.all_trades()} == {buy, idx}   # options: F&O underlyings, both sides (SBIN has none here)
