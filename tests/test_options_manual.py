"""fno (option contracts and chains) and manual (your own option orders). Kotak is faked throughout."""
import inspect
from datetime import date, timedelta

import pytest

from algotrade import broker, config, fno, kotak, manual, state, telegram_bot, web
from conftest import Recorder

E1980 = fno.EPOCH_1980


def epoch(d: date) -> int:
    """Scrip master expiry for a date (seconds since 1980-01-01, as Kotak sends them, 20:00 IST)."""
    from datetime import datetime, timezone
    return int(datetime(d.year, d.month, d.day, 14, 30, tzinfo=timezone.utc).timestamp()) - E1980


EXP1 = date.today() + timedelta(days=4)
EXP2 = date.today() + timedelta(days=11)
OLD = date.today() - timedelta(days=3)
HEAD = ("pSymbol,pGroup,pExchSeg,pInstType,pSymbolName,pTrdSymbol,pOptionType,dTickSize ,lLotSize,"
        "lExpiryDate ,dStrikePrice;,pExpiryDate")


def row(tok, inst, und, tsym, opt, strike, exp, lot=65):
    return f"{tok},XX,nse_fo,{inst},{und},{tsym},{opt},5,{lot},{epoch(exp)},{int(strike * 100)},{epoch(exp)}"


def master_csv():
    rows = [HEAD]
    tok = 1000
    for k in (22900, 22950, 23000, 23050, 23100):
        for opt in ("CE", "PE"):
            tok += 1
            rows.append(row(tok, "OPTIDX", "NIFTY", f"NIFTY26O19{k}{opt}", opt, k, EXP1))
    rows.append(row(2001, "OPTIDX", "NIFTY", "NIFTY26OCT23000CE", "CE", 23000, EXP2))
    rows.append(row(2002, "OPTIDX", "NIFTYFPI", "NIFTYFPI26OCT100CE", "CE", 100, EXP2))   # not offered
    rows.append(row(3001, "OPTSTK", "RELIANCE", "RELIANCE26OCT1200CE", "CE", 1200, EXP2, lot=500))
    rows.append(row(3002, "OPTSTK", "RELIANCE", "RELIANCE26OCT1200PE", "PE", 1200, EXP2, lot=500))
    rows.append(row(3003, "OPTSTK", "ACC", "ACC26OCT1800CE", "CE", 1800, OLD, lot=300))      # expired
    rows.append("9001,XX,nse_fo,FUTIDX,NIFTY,NIFTY26OCTFUT,XX,5,65,%d,-1,%d" % (epoch(EXP2), epoch(EXP2)))
    return "\n".join(rows)


class FakeNeo:
    def __init__(self):
        self.quote_calls = []

    def quotes(self, instrument_tokens=None, quote_type=None):
        self.quote_calls.append([t["instrument_token"] for t in instrument_tokens])
        out = []
        for t in instrument_tokens:
            tok = t["instrument_token"]
            if tok == "Nifty 50":
                out.append({"exchange_token": tok, "ltp": "22990.0", "ohlc": {"close": "22800.0"}})
            elif tok == "RELTOK":
                out.append({"exchange_token": tok, "ltp": "1215.0", "ohlc": {"close": "1200.0"}})
            else:
                n = int(tok)
                out.append({"exchange_token": tok, "ltp": f"{n % 100 + 10}.5", "open_int": str(n * 10),
                            "last_volume": "5000", "change": "1.5", "per_change": "4.2", "lstup_time": "1792059000",
                            "depth": {"buy": [{"price": "10.40", "quantity": "130"}],
                                      "sell": [{"price": "10.60", "quantity": "65"}]}})
        return out


@pytest.fixture
def fo(monkeypatch):
    neo = FakeNeo()
    monkeypatch.setattr(kotak, "_client", neo)
    monkeypatch.setattr(config, "KOTAK_CONSUMER_KEY", "ck")
    monkeypatch.setattr(fno, "_master", {"day": None, "df": None})
    monkeypatch.setattr(fno, "_chains", {})
    (config.DATA_DIR / f"kotak_nse_fo_{date.today()}.csv").write_text(master_csv(), encoding="utf-8")
    monkeypatch.setattr(kotak, "tokens", lambda: {"RELIANCE": "RELTOK"})
    monkeypatch.setattr(fno.time, "sleep", lambda s: None)
    return neo


# ---- contracts --------------------------------------------------------------------------------

def test_parse_master_dates_strikes_and_filters(fo):
    df = fno.contracts()
    assert set(df["underlying"]) == {"NIFTY", "RELIANCE"}            # no NIFTYFPI, futures or expired ACC
    r = df[df["tsym"] == "NIFTY26O1923000CE"].iloc[0]
    assert (r["expiry"], r["strike"], r["opt"], r["lot"], r["tick"], r["kind"]) == (EXP1, 23000.0, "CE", 65, 0.05, "index")


def test_expiry_epoch_matches_kotak_sample():
    # real row: pExpiryDate 1476887400 is NIFTY19OCT26 (scrip ref key)
    df = fno.parse_master(HEAD + "\n" + "1,XX,nse_fo,OPTIDX,NIFTY,NIFTY26O1919300PE,PE,5,65,1476887400,1930000,1476887400")
    assert str(df.iloc[0]["expiry"]) == "2026-10-19"


def test_underlyings_and_expiries(fo):
    assert fno.underlyings() == [{"name": "NIFTY", "kind": "index", "lot": 65},
                                 {"name": "RELIANCE", "kind": "stock", "lot": 500}]
    assert fno.expiries("NIFTY") == [str(EXP1), str(EXP2)]
    assert fno.contract("RELIANCE26OCT1200PE")["lot"] == 500 and fno.contract("NOPE") is None


# ---- chain ------------------------------------------------------------------------------------

def test_chain_around_the_money(fo):
    c = fno.chain("NIFTY", str(EXP1), width=1)
    assert c["spot"]["ltp"] == 22990.0 and round(c["spot"]["change"], 2) == 190.0
    assert c["atm"] == 23000.0 and [r["strike"] for r in c["rows"]] == [22950.0, 23000.0, 23050.0]
    ce = c["rows"][1]["ce"]
    assert ce["tsym"] == "NIFTY26O1923000CE" and (ce["bid"], ce["ask"], ce["volume"]) == (10.4, 10.6, 5000.0)
    assert c["lot"] == 65 and c["days_to_expiry"] == 4 and c["pcr"] is not None
    assert len(fo.quote_calls[-1]) == 6                               # 3 strikes x CE/PE in one call


def test_chain_cached_briefly(fo, monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(fno.time, "monotonic", lambda: clock[0])
    fno.chain("NIFTY", str(EXP1), 1)
    n = len(fo.quote_calls)
    fno.chain("NIFTY", str(EXP1), 1)
    assert len(fo.quote_calls) == n
    clock[0] += fno.CHAIN_CACHE_SEC + 0.1
    fno.chain("NIFTY", str(EXP1), 1)
    assert len(fo.quote_calls) > n


def test_stock_chain_uses_stock_quote(fo):
    c = fno.chain("RELIANCE", str(EXP2), 5)
    assert c["spot"]["ltp"] == 1215.0 and c["kind"] == "stock" and c["lot"] == 500


def test_chain_unknown_expiry(fo):
    with pytest.raises(ValueError, match="No NIFTY options"):
        fno.chain("NIFTY", "2099-01-01")


def test_fno_is_read_only():
    src = inspect.getsource(fno)
    for forbidden in ("place_order", "modify_order", "cancel_order", "totp_login", "broker"):
        assert forbidden not in src


# ---- manual orders ------------------------------------------------------------------------------

class Placed(list):
    positions: dict
    msgs: Recorder


@pytest.fixture
def orders_fake(fo, monkeypatch):
    placed = Placed()
    positions = {"data": []}

    def call(method, **kw):
        if method == "positions":
            return positions
        assert method == "place_order"
        placed.append(kw)
        return {"nOrdNo": f"O{len(placed)}", "stat": "Ok"}
    monkeypatch.setattr(broker, "call", call)
    monkeypatch.setattr(manual.markets.get("IN").__class__, "is_open", lambda self, now=None: True)
    msgs = Recorder(True)
    monkeypatch.setattr(telegram_bot, "send_message", msgs)
    placed.positions, placed.msgs = positions, msgs
    return placed


def test_buy_limit_order(orders_fake):
    o = manual.place_option_order("NIFTY26O1923000CE", "buy", 2, "limit", 91.87, "nrml")
    p = orders_fake[0]
    assert {k: p[k] for k in ("exchange_segment", "product", "order_type", "price", "quantity", "transaction_type",
                               "trading_symbol", "amo", "validity")} == \
        {"exchange_segment": "nse_fo", "product": "NRML", "order_type": "L", "price": "91.85", "quantity": "130",
         "transaction_type": "B", "trading_symbol": "NIFTY26O1923000CE", "amo": "NO", "validity": "DAY"}
    assert o["order_id"] == "O1" and o["qty"] == 130
    assert manual.recent()[0]["order_id"] == "O1"
    assert "Manual BUY NIFTY26O1923000CE" in orders_fake.msgs.calls[0][0][0]


def test_market_order_and_amo(orders_fake, monkeypatch):
    monkeypatch.setattr(manual.markets.get("IN").__class__, "is_open", lambda self, now=None: False)
    o = manual.place_option_order("RELIANCE26OCT1200PE", "BUY", 1, "MARKET", None, "MIS")
    p = orders_fake[0]
    # no market orders via the API: a limit 5% above the best ask (10.60), on the tick grid
    assert (p["order_type"], p["price"], p["amo"], p["product"], p["quantity"]) == ("L", "11.15", "YES", "MIS", "500")
    assert o["order_type"] == "MARKET" and "market (limit ₹11.15)" in orders_fake.msgs.calls[0][0][0]


def test_market_sell_prices_off_the_bid(orders_fake):
    orders_fake.positions["data"] = [{"trdSym": "RELIANCE26OCT1200PE", "flBuyQty": "500", "flSellQty": "0",
                                      "buyAmt": "5000", "sellAmt": "0"}]
    manual.place_option_order("RELIANCE26OCT1200PE", "SELL", 1, "MARKET")
    assert (orders_fake[0]["order_type"], orders_fake[0]["price"]) == ("L", "9.85")   # 5% below the bid 10.40


def test_protect_rounds_away_from_the_price():
    assert manual.protect(100.0, "BUY", 1) == 101.0 and manual.protect(100.0, "SELL", 1) == 99.0
    assert manual.protect(10.6, "BUY", 5, 0.05) == 11.15 and manual.protect(0.1, "SELL", 50, 0.05) == 0.05


@pytest.mark.parametrize("args,msg", [
    (("NOPE", "BUY", 1), "Unknown option contract"),
    (("NIFTY26O1923000CE", "HOLD", 1), "Side must be"),
    (("NIFTY26O1923000CE", "BUY", 0), "Lots must be"),
    (("NIFTY26O1923000CE", "BUY", 11), "Lots must be"),
    (("NIFTY26O1923000CE", "BUY", 1.5), "Lots must be"),
    (("NIFTY26O1923000CE", "BUY", True), "Lots must be"),
    (("NIFTY26O1923000CE", "BUY", 1, "STOP"), "Order type"),
    (("NIFTY26O1923000CE", "BUY", 1, "LIMIT", None), "needs a price"),
    (("NIFTY26O1923000CE", "BUY", 1, "LIMIT", 10, "CNC"), "Product must be"),
])
def test_bad_requests(orders_fake, args, msg):
    with pytest.raises(ValueError, match=msg):
        manual.place_option_order(*args)
    assert orders_fake == []


def test_sell_only_to_close_what_you_hold(orders_fake):
    with pytest.raises(ValueError, match="you hold 0"):
        manual.place_option_order("NIFTY26O1923000CE", "SELL", 1, "LIMIT", 90)
    orders_fake.positions["data"] = [{"trdSym": "NIFTY26O1923000CE", "flBuyQty": "130", "flSellQty": "0",
                                      "buyAmt": "11000", "sellAmt": "0"}]
    with pytest.raises(ValueError, match="you hold 130 .* order is for 195"):
        manual.place_option_order("NIFTY26O1923000CE", "SELL", 3, "LIMIT", 90)
    manual.place_option_order("NIFTY26O1923000CE", "SELL", 2, "LIMIT", 90)
    assert orders_fake[0]["transaction_type"] == "S" and orders_fake[0]["quantity"] == "130"


def test_kotak_refusal_is_recorded_and_raised(orders_fake, monkeypatch):
    def refuse(method, **kw):
        raise broker.BrokerError("place_order failed: F&O segment not active")
    monkeypatch.setattr(broker, "call", refuse)
    with pytest.raises(broker.BrokerError):
        manual.place_option_order("NIFTY26O1923000CE", "BUY", 1, "LIMIT", 90)
    assert "segment not active" in manual.recent()[0]["error"]


# ---- API --------------------------------------------------------------------------------------

def test_api_chain_and_underlyings(fo, monkeypatch):
    c = web.app.test_client()
    assert c.get("/api/options/underlyings").get_json()["max_lots"] == 10
    assert c.get("/api/options/expiries?u=RELIANCE").get_json() == {"expiries": [str(EXP2)]}
    j = c.get(f"/api/options/chain?u=NIFTY&expiry={EXP1}&width=1").get_json()
    assert j["atm"] == 23000.0 and j["held"] == {} and j["recent"] == []
    assert c.get("/api/options/chain?u=NIFTY&expiry=2099-01-01").status_code == 400


def test_api_order_local_only_and_errors(orders_fake):
    c = web.app.test_client()
    body = {"tsym": "NIFTY26O1923000CE", "side": "BUY", "lots": 1, "order_type": "LIMIT", "price": 90, "product": "NRML"}
    r = c.post("/api/options/order", json=body, environ_base={"REMOTE_ADDR": "192.168.1.9"})
    assert r.status_code == 401 and orders_fake == []
    assert c.post("/api/options/order", json={**body, "lots": 50}).status_code == 400
    r = c.post("/api/options/order", json=body)
    assert r.status_code == 200 and r.get_json()["order_id"] == "O1"
