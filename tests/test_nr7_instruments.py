"""NR7 futures and option plans (nr7_instruments.py) and the index contract lists behind them (fno). Kotak is faked."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from algotrade import config, fno, kotak, nr7, nr7_instruments as ni, strategy

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 10, 27, 20, 0, tzinfo=IST)            # a Tuesday
HEAD = ("pSymbol,pGroup,pExchSeg,pInstType,pSymbolName,pTrdSymbol,pOptionType,dTickSize ,lLotSize,"
        "lExpiryDate ,dStrikePrice;,pExpiryDate")


def unix(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 10, 0, tzinfo=timezone.utc).timestamp())


def row(tok, seg, inst, und, tsym, opt, strike, exp, lot):
    e = unix(exp) - (fno.EPOCH_1980 if seg == "nse_fo" else 0)
    return f"{tok},XX,{seg},{inst},{und},{tsym},{opt},5,{lot},{e},{int(strike * 100) if strike else -1},{e}"


OCT, NOV, NOV_WEEK, DEC = date(2026, 10, 29), date(2026, 11, 24), date(2026, 11, 3), date(2026, 12, 29)


def nse_csv():
    rows = [HEAD, row(1, "nse_fo", "FUTIDX", "NIFTY", "NIFTY26OCTFUT", "XX", None, OCT, 65),
            row(2, "nse_fo", "FUTIDX", "NIFTY", "NIFTY26NOVFUT", "XX", None, NOV, 65)]
    tok = 100
    for exp in (NOV_WEEK, NOV):
        for k in range(22000, 23801, 100):
            for opt in ("CE", "PE"):
                tok += 1
                rows.append(row(tok, "nse_fo", "OPTIDX", "NIFTY", f"NIFTY{exp:%y%b}{k}{opt}".upper(), opt, k, exp, 65))
    return "\n".join(rows)


def bse_csv():
    return "\n".join([HEAD, row(9, "bse_fo", "IO", "SENSEX", "SENSEX26NOV73000CE", "CE", 73000, date(2026, 11, 26), 20),
                      row(10, "bse_fo", "IF", "SENSEX", "SENSEX26NOVFUT", "XX", None, date(2026, 11, 26), 20)])


class FakeNeo:
    """Index spot 22,750; the November future 22,800; options priced by Black-Scholes at 15% volatility."""
    def __init__(self, df):
        self.df = df

    def quotes(self, instrument_tokens=None, quote_type=None):
        out = []
        for t in instrument_tokens:
            tok = t["instrument_token"]
            if tok == "Nifty 50":
                out.append({"exchange_token": tok, "ltp": "22750"})
                continue
            c = self.df[self.df["token"] == tok].iloc[0]
            if c["opt"] == "FUT":
                px = 22800.0
            else:
                years = fno.days_to_expiry(c["expiry"], NOW) / 365
                px = strategy.bs_price(c["opt"], 22750, float(c["strike"]), years, 0.15)
            out.append({"exchange_token": tok, "ltp": f"{px:.2f}",
                        "depth": {"buy": [{"price": f"{px - 0.5:.2f}"}], "sell": [{"price": f"{px + 0.5:.2f}"}]}})
        return out


@pytest.fixture
def kot(monkeypatch):
    monkeypatch.setattr(config, "KOTAK_CONSUMER_KEY", "ck")
    monkeypatch.setattr(fno, "_index_master", {})
    monkeypatch.setattr(ni, "_cache", {})
    df = fno.parse_index_master(nse_csv(), "nse_fo")
    monkeypatch.setattr(fno, "index_contracts", lambda u, seg="nse_fo": df[df["underlying"] == u].reset_index(drop=True))
    monkeypatch.setattr(kotak, "_client", FakeNeo(df))
    monkeypatch.setattr(fno.livefeed, "lookup", lambda seg, toks: {})
    return df


def setup():
    t = nr7.new_setup("NIFTY", date(2026, 10, 27), 22829.75, 22636.85)
    return t


# ---- contract lists -----------------------------------------------------------------------------------

def test_index_master_reads_both_exchanges_epochs():
    nse = fno.parse_index_master(nse_csv(), "nse_fo")
    fut = nse[nse["tsym"] == "NIFTY26NOVFUT"].iloc[0]
    assert (fut["opt"], fut["expiry"], fut["lot"]) == ("FUT", NOV, 65) and fut["strike"] != fut["strike"]   # NaN
    bse = fno.parse_index_master(bse_csv(), "bse_fo")
    assert list(bse["opt"]) == ["CE", "FUT"] and set(bse["expiry"]) == {date(2026, 11, 26)}
    assert bse.iloc[0]["strike"] == 73000 and bse.iloc[0]["lot"] == 20


def test_index_contracts_reads_the_cached_file(monkeypatch):
    monkeypatch.setattr(fno, "_index_master", {})
    today = date.today()
    exp = today + timedelta(days=30)
    (config.DATA_DIR / f"kotak_bse_fo_{today}.csv").write_text(
        "\n".join([HEAD, row(9, "bse_fo", "IO", "SENSEX", "SX", "CE", 73000, exp, 20),
                   row(8, "bse_fo", "IO", "SENSEX", "OLD", "CE", 73000, today - timedelta(days=2), 20),
                   row(7, "bse_fo", "IO", "BANKEX", "BX", "CE", 60000, exp, 30)]), encoding="utf-8")
    assert list(fno.index_contracts("SENSEX", "bse_fo")["tsym"]) == ["SX"]       # expired and other indices dropped


# ---- choosing contracts -------------------------------------------------------------------------------

@pytest.mark.parametrize("expiries,today,monthly,expected", [
    ([OCT, NOV, DEC], date(2026, 10, 27), False, NOV),            # October has 2 trading days left: roll
    ([OCT, NOV, DEC], date(2026, 10, 20), False, OCT),            # 7 left: keep
    ([OCT, NOV_WEEK, NOV, DEC], date(2026, 10, 27), True, NOV),   # monthly: the weekly 3 Nov is skipped
    ([OCT, NOV_WEEK, NOV, DEC], date(2026, 12, 23), True, None),  # 4 trading days to December's: nothing left
])
def test_pick_expiry(expiries, today, monthly, expected):
    assert ni.pick_expiry(expiries, today, monthly) == expected


def test_plans_for_a_waiting_setup(kot):
    p = ni.plans(setup(), NOW)
    f = p["futures"]
    assert (f["tsym"], f["basis"], f["buy_above"], f["sell_below"], f["lot"]) == ("NIFTY26NOVFUT", 50, 22879.75, 22686.85, 65)
    assert f["risk_per_lot"] == round(192.9 * 65) and f["lots"] == 0       # ₹1,000 budget < ₹12,539 a lot
    c, pu = p["call"], p["put"]
    assert c["tsym"].endswith("CE") and c["strike"] <= 22829.75 and c["expiry"] == str(NOV)   # monthly, not weekly
    assert pu["tsym"].endswith("PE") and pu["strike"] >= 22636.85
    for o in (c, pu):
        assert 0.6 <= o["delta"] <= 0.8 and o["cost_per_lot"] == round(o["premium"] * 65)


def test_option_lots_spend_at_most_the_risk_budget(kot, monkeypatch):
    monkeypatch.setattr(config, "CAPITAL", 10_00_000)
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 10.0)                # ₹1,00,000 per trade
    c = ni.plans(setup(), NOW)["call"]
    assert c["lots"] == 100_000 // c["cost_per_lot"] and c["lots"] * c["cost_per_lot"] <= 100_000


def test_an_open_trade_gets_only_its_side_and_text(kot):
    t = {**setup(), "status": "OPEN", "side": "SHORT"}
    p = ni.plans(t, NOW)
    assert "put" in p and "call" not in p
    txt = ni.text(p, "SHORT")
    assert "sell below 22,686.85" in txt and "buy above" not in txt and txt.count("\n") == 1


def test_sensex_has_options_only():
    assert "SENSEX" not in ni.FUTURES and ni.futures_plan({"idx": "SENSEX"}, None, NOW.date()) is None


def test_plans_without_kotak():
    assert "error" in ni.plans(setup(), NOW)


def test_view_carries_plans_for_active_setups_only():
    nr7._insert(setup())
    nr7._insert({**nr7.new_setup("SENSEX", date(2026, 10, 20), 1, 0), "status": "EXPIRED"})
    rows = {t["idx"]: t for t in nr7.view()["setups"]}
    assert "error" in rows["NIFTY"]["plans"] and "plans" not in rows["SENSEX"]
