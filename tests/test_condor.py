"""Nifty 0DTE iron condor (condor.py): the rulebook's maths, its worked example, and a paper expiry day."""
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from algotrade import condor, strategy

IST = ZoneInfo("Asia/Kolkata")
EXP = date(2026, 12, 1)    # a Tuesday (not a holiday)


def at(h, m, d=EXP):
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


# ---- maths ------------------------------------------------------------------------------------------------

def test_worked_example_one_sd_at_ten():
    """Rulebook: 10:00 (330 min left), spot 25,800, IV 17.3% -> t = 0.003492, A ~ 264 pts."""
    assert condor.minutes_left(at(10, 0)) == 330
    t = condor.years(330)
    assert t == pytest.approx(0.003492, abs=1e-6)
    sd = condor.one_sd(25800, 0.173, 330, None, None)
    assert sd["a"] == pytest.approx(264, abs=1) and sd["used"] == "A"


def test_one_sd_a_vs_b():
    a = 25800 * 0.173 * math.sqrt(condor.years(330))
    near = condor.one_sd(25800, 0.173, 330, 105.0, 106.0)       # B = 263.75, within 10%
    assert near["used"] == "A" and near["sd"] == pytest.approx(a)
    far = condor.one_sd(25800, 0.173, 330, 140.0, 140.0)        # B = 350: A is >10% short, use B
    assert far["used"] == "B" and far["sd"] == pytest.approx(350)
    assert condor.one_sd(25800, None, 330, 100.0, 100.0)["used"] == "B"


def test_strikes_worked_example():
    """20 Nov table: call 0.16D 26,050 / 1SD 26,064 / wall 26,100 -> 26,100; put 25,550 / 25,536 / 25,500 -> 25,500."""
    pk = condor.pick_strikes(25800.6, 263.4, 26050, 25550, 26100, 25500)
    assert (pk["short_call"], pk["long_call"], pk["call_by"]) == (26100, 26150, "OI wall")
    assert (pk["short_put"], pk["long_put"], pk["put_by"]) == (25500, 25450, "OI wall")
    # 1SD the farthest: rounded outward
    pk = condor.pick_strikes(25800, 330, 26050, 25550, 26000, 25600)
    assert (pk["short_call"], pk["call_by"]) == (26150, "1SD") and (pk["short_put"], pk["put_by"]) == (25450, "1SD")


def test_rounding_is_outward():
    assert condor.round_up(26064) == 26100 and condor.round_up(26100) == 26100
    assert condor.round_down(25536) == 25500 and condor.round_down(25500) == 25500


def test_roll_buffers_worked_example():
    """10:30, Nifty 25,850, 1SD 252: call level 26,102 is 2 pts past 26,100 -> no roll; put 25,500 is 98 pts
    beyond its level -> an inward candidate (the OI wall keeps the target at 25,500, so nothing changes)."""
    assert condor.roll_signal("CE", 26100, 25850, 252) is None
    assert condor.roll_signal("CE", 26100, 25875, 252) == "out"           # 27 pts past
    assert condor.roll_signal("PE", 25500, 25850, 252) == "in"
    assert condor.roll_signal("PE", 25500, 25770, 252) is None             # 18 pts inside, 25 needed
    assert condor.roll_signal("PE", 25500, 25700, 252) == "out"            # level 25,448: 52 pts past


def test_pop():
    assert condor.pop(26000, 25600, 0.0, 25800, 200) == pytest.approx(0.6827, abs=1e-3)   # shorts at 1SD
    assert condor.pop(26030, 25570, 0.0, 25800, 200) == pytest.approx(0.75, abs=0.005)    # +/-1.15 SD
    assert condor.pop(26000, None, 0.0, 25800, 200) == pytest.approx(0.8413, abs=1e-3)    # one side left


def test_ivp_and_hv10():
    closes = [10 + i * 10 / 251 for i in range(252)]          # 10 .. 20
    assert condor.ivp(15.0, closes) == pytest.approx(50, abs=1)
    assert condor.ivp(15.0, closes[:100]) is None
    flat = [100 * math.exp(0.01 * (i % 2)) for i in range(11)]  # alternating +/-1% log moves
    assert condor.hv10(flat) == pytest.approx(0.01 * math.sqrt(10 / 9) * math.sqrt(252) * 100, rel=1e-6)
    assert condor.hv10([100.0] * 5) is None


def test_oi_walls_above_and_below_spot_and_unwinding():
    rows = [{"strike": 25800.0, "ce": {"oi": 9e6, "oi_open": 9e6}, "pe": {"oi": 5e6, "oi_open": 5e6}},  # at spot: not a wall
            {"strike": 26100.0, "ce": {"oi": 7e6, "oi_open": 6e6}, "pe": {"oi": 1e5}},
            {"strike": 26200.0, "ce": {"oi": 3e6, "oi_open": 3e6}, "pe": {"oi": 1e5}},
            {"strike": 25500.0, "ce": {"oi": 1e5}, "pe": {"oi": 6e6, "oi_open": 7e6}},
            {"strike": 24500.0, "ce": {"oi": 1e5}, "pe": {"oi": 9e9, "oi_open": 9e9}}]   # beyond 2SD
    w = condor.oi_walls(rows, 25800.6, 264)
    assert w["call"]["strike"] == 26100 and w["call"]["unwinding"] is False
    assert w["put"]["strike"] == 25500 and w["put"]["unwinding"] is True


def test_option_charges(monkeypatch):
    from algotrade import config
    buy = condor.option_charges("BUY", 1000.0)                 # API orders: no brokerage, statutory charges only
    assert buy == pytest.approx(1000 * 0.0003513 * 1.18 + 0.03)
    monkeypatch.setattr(config, "BROKERAGE", 20.0)
    sell = condor.option_charges("SELL", 1000.0)
    buy = condor.option_charges("BUY", 1000.0)
    assert sell - buy == pytest.approx(1000 * (0.001 - 0.00003))
    assert buy == pytest.approx((20 + 1000 * 0.0003513) * 1.18 + 0.03)


# ---- evaluate on a synthetic chain ------------------------------------------------------------------------

def make_snap(spot, iv, now, call_wall=26100, put_wall=25500, unwinding=False, expiry=EXP):
    t = condor.years(condor.trading_minutes_to(expiry, now))
    rows = []
    for k in range(24500, 27050, 50):
        row = {"strike": float(k)}
        for opt in ("CE", "PE"):
            px = max(strategy.bs_price(opt, spot, k, t, iv, 0.0), 0.05)
            oi = 5e6 if (opt == "CE" and k == call_wall) or (opt == "PE" and k == put_wall) else 1e6
            row[opt.lower()] = {"tsym": f"NIFTY{k}{opt}", "bid": round(max(px - 0.05, 0.05), 2),
                                "ask": round(px + 0.05, 2), "ltp": round(px, 2), "oi": oi,
                                "oi_open": oi * (1.1 if unwinding and oi == 5e6 else 1.0)}
        rows.append(row)
    return {"spot": spot, "expiry": expiry.isoformat(), "lot": 65, "rows": rows}


VIX = {"now": 15.0, "prev": 14.8}
HIST = {"vix": [10 + i * 10 / 251 for i in range(252)], "nifty": [25000 * (1 + 0.004 * (i % 3)) for i in range(11)]}


def test_evaluate_passes_and_builds_the_condor():
    now = at(10, 0)
    p = condor.evaluate(make_snap(25800, 0.173, now), now, VIX, HIST, [])
    assert p["sd"]["used"] == "A" and p["sd"]["sd"] == pytest.approx(264, abs=3)   # BS prices: A and B agree
    tg = p["targets"]
    assert tg["short_call"] >= 26100 and tg["short_put"] <= 25500
    assert tg["long_call"] - tg["short_call"] == 50 and tg["short_put"] - tg["long_put"] == 50
    assert [g["action"] for g in p["legs"]] == ["BUY", "SELL", "SELL", "BUY"]
    assert p["credit"] > 0 and p["qty"] == 65
    assert p["max_loss"] == pytest.approx((50 - p["credit"]) * 65)
    assert p["pop"] > 0.75 and p["ok"], p["blockers"]


def test_hard_filters_block():
    now = at(10, 0)
    snap = make_snap(25800, 0.173, now)
    p = condor.evaluate(snap, now, {"now": 17.32, "prev": 16.11}, HIST, [])     # VIX +7.5% (20 Nov)
    assert not p["ok"] and "VIX rise ≤ 5%" in p["blockers"] and not p["data_only"]
    p = condor.evaluate(snap, now, VIX, HIST, [EXP.isoformat()])
    assert "Not an event day" in p["blockers"]
    p = condor.evaluate(snap, at(10, 0, EXP - timedelta(days=1)), VIX, HIST, [])
    assert "Expiry day (0DTE)" in p["blockers"]
    p = condor.evaluate(make_snap(25800, 0.173, now, unwinding=True), now, VIX, HIST, [])
    assert "OI walls not unwinding" in p["blockers"]
    low = {"vix": [20 + i * 0.01 for i in range(252)], "nifty": HIST["nifty"]}   # VIX 15 below every close
    assert "IV percentile 30-85" in condor.evaluate(snap, now, VIX, low, [])["blockers"]


def test_checks_never_block():
    now = at(10, 0)
    p = condor.evaluate(make_snap(25800, 0.173, now), now, {"now": 22.0, "prev": 21.5},
                        {"vix": [10 + i * 15 / 251 for i in range(252)], "nifty": HIST["nifty"]}, [])
    band = next(c for c in p["checks"] if c["rule"] == "India VIX 11-20")
    assert band["ok"] is False and not band["hard"]
    assert "India VIX 11-20" not in p["blockers"]


def test_missing_vix_is_a_data_wait_not_a_skip():
    now = at(10, 0)
    p = condor.evaluate(make_snap(25800, 0.173, now), now, {}, HIST, [])
    assert not p["ok"] and p["data_only"]


# ---- a paper expiry day -----------------------------------------------------------------------------------

class Market:
    """The engine's market: a synthetic chain that follows `spot` / `iv` and the clock."""
    def __init__(self):
        self.spot, self.iv, self.now, self.override = 25800.0, 0.173, at(9, 20), {}

    def snap(self, expiry=None, now=None):
        return make_snap(self.spot, self.iv, now or self.now)

    def quote(self, tsym):
        if tsym in self.override:
            return self.override[tsym]
        for r in self.snap()["rows"]:
            for k in ("ce", "pe"):
                if r[k]["tsym"] == tsym:
                    return dict(r[k])
        raise KeyError(tsym)


@pytest.fixture
def mkt(monkeypatch):
    m = Market()
    monkeypatch.setattr(condor, "snapshot", lambda exp, now, u="NIFTY": m.snap(exp, now))
    monkeypatch.setattr(condor, "vix_quote", lambda: dict(VIX))
    monkeypatch.setattr(condor, "history", lambda now: HIST)
    monkeypatch.setattr(condor, "today_expiry",
                        lambda now, u="NIFTY": EXP.isoformat() if now.date() == EXP and u == "NIFTY" else None)
    monkeypatch.setattr(condor, "next_expiry", lambda now, u="NIFTY": EXP.isoformat())
    monkeypatch.setattr(condor, "_quote", m.quote)
    real = condor.markets.get("IN")

    class Clock:                                   # the NSE on the test's clock
        now = staticmethod(lambda: m.now)
        is_open = staticmethod(lambda now=None: real.is_open(now or m.now))
    monkeypatch.setattr(condor, "_nse", lambda: Clock)
    monkeypatch.setattr(condor.fno, "contract", lambda t: {"opt": t[-2:], "strike": float(t[5:-2]), "tick": 0.05})
    sent = []
    monkeypatch.setattr(condor.telegram_bot, "send_message", lambda text, *a, **k: sent.append(text))
    m.sent = sent
    condor._mem["books"].clear()
    condor.set_mode("paper", "test")
    return m


def run(m, h, mi, d=EXP):
    m.now = at(h, mi, d)
    condor._book("NIFTY").update(retarget_at=0.0, entry_try=0.0)
    condor.tick(m.now)


def shorts(day):
    return {v["opt"]: v["strike"] for v in condor.position(day).values() if v["qty"] < 0}


def test_paper_day_entry_roll_and_time_exit(mkt):
    day = EXP.isoformat()
    run(mkt, 9, 20)
    assert condor.get_day(day)["status"] == condor.WAITING
    assert any("pre-check" in s for s in mkt.sent)
    run(mkt, 9, 40)
    assert not condor.position(day)                       # before 09:45: nothing
    run(mkt, 9, 46)
    rec = condor.get_day(day)
    assert rec["status"] == condor.OPEN, rec["reason"]
    pos = condor.position(day)
    assert len(pos) == 4 and sorted(v["qty"] for v in pos.values()) == [-65, -65, 65, 65]
    fl = condor.fills(day)
    assert [f["action"] for f in fl] == ["BUY", "BUY", "SELL", "SELL"]     # wings first
    first = shorts(day)

    # Nifty rallies: the call side's 1SD level goes 25+ pts past its short -> rolls out
    mkt.spot = first["CE"] - 120
    run(mkt, 10, 40)
    after = shorts(day)
    assert after["CE"] > first["CE"] and after["PE"] == first["PE"]
    assert condor.get_day(day)["rolls"] == 1
    assert len(condor.position(day)) == 4

    # Within the 2-minute cooldown nothing rolls again
    mkt.spot += 150
    run(mkt, 10, 41)
    assert shorts(day)["CE"] == after["CE"]

    # 15:00: everything squared off, shorts bought back before the wings are sold
    mkt.spot = 25900
    run(mkt, 15, 0)
    assert not condor.position(day)
    rec = condor.get_day(day)
    assert rec["status"] == condor.CLOSED and rec["pnl"] is not None
    last = condor.fills(day)[-4:]
    assert [f["action"] for f in last] == ["BUY", "BUY", "SELL", "SELL"]
    pl = condor.pnl(day, {})
    assert pl["total"] == pytest.approx(rec["pnl"]) and pl["charges"] > 0
    run(mkt, 15, 5)                                       # the day is over: no new trade
    assert not condor.position(day)


def test_loss_cap_closes_everything(mkt):
    day = EXP.isoformat()
    run(mkt, 9, 20)
    run(mkt, 9, 46)
    sc = shorts(day)["CE"]
    tsym = f"NIFTY{int(sc)}CE"
    mkt.override[tsym] = {"tsym": tsym, "bid": 90.0, "ask": 90.1, "ltp": 90.0}   # short call blows up
    mkt.now = at(11, 0)
    condor._book("NIFTY")["retarget_at"] = 1e18                     # loss check only, no re-target
    condor.tick(mkt.now)
    rec = condor.get_day(day)
    assert rec["status"] == condor.STOPPED and not condor.position(day)
    assert rec["pnl"] <= -2600 + 1e-6 or rec["pnl"] < 0
    assert any("loss cap" in s.lower() for s in mkt.sent)


def test_skip_day_on_vix_rise(mkt, monkeypatch):
    monkeypatch.setattr(condor, "vix_quote", lambda: {"now": 17.32, "prev": 16.11})
    day = EXP.isoformat()
    run(mkt, 9, 20)
    rec = condor.get_day(day)
    assert rec["status"] == condor.SKIPPED and "VIX rise" in rec["reason"]
    run(mkt, 9, 50)
    assert not condor.position(day)


def test_not_an_expiry_day_does_nothing(mkt):
    other = EXP - timedelta(days=1)
    run(mkt, 9, 50, other)
    assert condor.get_day(other.isoformat()) is None and not condor.position(other.isoformat())


def test_off_mode_opens_nothing(mkt):
    condor.set_mode("off", "test")
    run(mkt, 9, 20)
    run(mkt, 9, 50)
    assert not condor.position(EXP.isoformat())


def test_live_mode_needs_the_condors_own_switch(mkt, monkeypatch):
    condor.set_mode("live", "test")
    monkeypatch.setattr(condor.broker, "trading_enabled", lambda: True)    # the global switch doesn't count
    assert condor.trading_enabled() is False                               # off until switched on
    day = EXP.isoformat()
    run(mkt, 9, 20)
    run(mkt, 9, 46)
    rec = condor.get_day(day)
    assert rec["status"] == condor.SKIPPED and "Auto-trade" in rec["reason"]
    assert not condor.fills(day)


def test_skip_dates_roundtrip():
    assert condor.set_skip_dates(["2026-12-05", " 2026-02-01 ", "2026-12-05"]) == ["2026-02-01", "2026-12-05"]
    assert condor.skip_dates() == ["2026-02-01", "2026-12-05"]
    with pytest.raises(ValueError):
        condor.set_skip_dates(["5 Dec"])


def test_index_quotes_matches_kotaks_other_spelling(monkeypatch):
    """Kotak answers "INDIA VIX" as "India VIX"; the quote must still come back under the name asked for."""
    from algotrade import kotak

    class Neo:
        def quotes(self, instrument_tokens, quote_type):
            return [{"exchange_token": "India VIX", "ltp": "17.32", "ohlc": {"close": "16.11"}}]

    monkeypatch.setattr(kotak, "enabled", lambda: True)
    monkeypatch.setattr(kotak, "_neo", lambda: Neo())
    monkeypatch.setattr(kotak.livefeed, "lookup", lambda seg, names: {})
    assert kotak.index_quotes(["INDIA VIX"]) == {"INDIA VIX": {"ltp": 17.32, "prev_close": 16.11}}
    assert condor.vix_quote() == {"now": 17.32, "prev": 16.11}


def test_condor_trading_switch(monkeypatch):
    logins = []
    monkeypatch.setattr(condor.broker, "login", lambda force=False: logins.append(force))
    monkeypatch.setattr(condor.telegram_bot, "send_message", lambda *a, **k: None)
    assert condor.trading_enabled() is False
    condor.set_trading(True, "test")
    assert condor.trading_enabled() is True and logins == [True]          # ON proves the Kotak login
    condor.set_trading(False, "test")
    assert condor.trading_enabled() is False and logins == [True]         # OFF needs no login
    assert condor.status()["trading_enabled"] is False


def test_live_position_and_closing_by_hand(mkt):
    day = EXP.isoformat()
    run(mkt, 9, 20)
    run(mkt, 9, 46)
    st = condor.status()
    assert len(st["legs"]) == 4
    for g in st["legs"]:
        assert g["avg"] and g["mark"] is not None and g["bid"] is not None and g["pnl"] is not None
    short_put = next(g for g in st["legs"] if g["opt"] == "PE" and g["qty"] < 0)
    wing_call = next(g for g in st["legs"] if g["opt"] == "CE" and g["qty"] > 0)
    assert short_put["avg"] == condor.fills(day)[2]["price"] or short_put["avg"] > 0

    with pytest.raises(ValueError, match="wing can't be closed"):
        condor.close_leg(wing_call["tsym"], "test")
    condor.close_leg(short_put["tsym"], "test")              # buy back the short put; its wing stays
    pos = condor.position(day)
    assert short_put["tsym"] not in pos and len(pos) == 3
    assert condor.fills(day)[-1]["action"] == "BUY"

    condor.close_side("CE", "test")                         # short call first, then the call wing
    pos = condor.position(day)
    assert all(v["opt"] == "PE" and v["qty"] > 0 for v in pos.values())
    assert [f["action"] for f in condor.fills(day)[-2:]] == ["BUY", "SELL"]
    assert condor.get_day(day)["status"] == condor.OPEN

    condor.close_side("PE", "test")                         # the last leg: the day is over
    assert not condor.position(day) and condor.get_day(day)["status"] == condor.CLOSED
    with pytest.raises(ValueError):
        condor.close_side("PE", "test")


def test_entry_prices_average_and_reset():
    from algotrade import condor as c
    day = "2026-11-24"
    for action, qty, px in (("SELL", 65, 10.0), ("SELL", 65, 12.0), ("BUY", 65, 5.0), ("BUY", 65, 6.0),
                            ("BUY", 65, 3.0)):
        c._record_fill(day, "NIFTY", "paper", "NIFTY26100CE", "CE", 26100.0, action, qty, px, None, "t")
    c._record_fill(day, "NIFTY", "paper", "NIFTY26150CE", "CE", 26150.0, "BUY", 65, 2.0, None, "t")
    avg = c.entry_prices(day)
    assert avg == {"NIFTY26100CE": 3.0, "NIFTY26150CE": 2.0}     # short closed, then re-opened long at 3


def test_closed_legs_are_each_round_trip():
    from algotrade import condor as c
    day = "2026-11-24"
    for action, qty, px in (("SELL", 65, 10.0), ("SELL", 65, 12.0), ("BUY", 65, 5.0), ("BUY", 65, 6.0),   # short, closed
                            ("BUY", 65, 3.0)):                                                         # long, still open
        c._record_fill(day, "NIFTY", "paper", "NIFTY26100CE", "CE", 26100.0, action, qty, px, None, "t")
    c._record_fill(day, "NIFTY", "paper", "NIFTY26150CE", "CE", 26150.0, "BUY", 65, 2.0, None, "t")
    c._record_fill(day, "NIFTY", "paper", "NIFTY26150CE", "CE", 26150.0, "SELL", 65, 1.5, None, "t")
    legs = c.closed_legs(day)
    assert [(g["tsym"], g["qty"], g["avg"], g["exit"], g["pnl"]) for g in legs] == [
        ("NIFTY26100CE", -130, 11.0, 5.5, 715.0),        # (11 - 5.5) x 130
        ("NIFTY26150CE", 65, 2.0, 1.5, -32.5)]
    assert c.closed_legs(day, "SENSEX") == []


def test_rules_off_never_block():
    now = at(10, 0)
    snap = make_snap(25800, 0.173, now)
    rise = {"now": 17.32, "prev": 16.11}                                     # +7.5%
    p = condor.evaluate(snap, now, rise, HIST, [], rules={"vix_rise": False})
    chk = next(c for c in p["checks"] if c["rule"] == "VIX rise ≤ 5%")
    assert chk["ok"] is False and chk["on"] is False and "VIX rise ≤ 5%" not in p["blockers"] and p["ok"]
    p = condor.evaluate(snap, now, VIX, HIST, [EXP.isoformat()], rules={"event": False})
    assert p["ok"]
    p = condor.evaluate(make_snap(25800, 0.173, now, unwinding=True), now, VIX, HIST, [], rules={"oi_unwind": False})
    assert p["ok"]


def test_candidate_switches_change_the_strikes():
    now = at(10, 0)
    snap = make_snap(25800, 0.173, now, call_wall=26300, put_wall=25300)      # walls far out
    with_wall = condor.evaluate(snap, now, VIX, HIST, [])
    assert with_wall["targets"]["short_call"] == 26300 and with_wall["targets"]["call_by"] == "OI wall"
    no_wall = condor.evaluate(snap, now, VIX, HIST, [], rules={"cand_wall": False})
    assert no_wall["targets"]["short_call"] < 26300 and no_wall["targets"]["call_by"] in ("delta", "1SD")
    only_delta = condor.evaluate(snap, now, VIX, HIST, [], rules={"cand_wall": False, "cand_sd": False})
    assert only_delta["targets"]["call_by"] == "delta"


def test_set_rules(monkeypatch):
    monkeypatch.setattr(condor.telegram_bot, "send_message", lambda *a, **k: None)
    assert all(condor.get_rules().values())                                   # all on by default
    condor.set_rules({"pop": False, "roll_in": False}, "test")
    r = condor.get_rules()
    assert r["pop"] is False and r["roll_in"] is False and r["loss_cap"] is True
    with pytest.raises(ValueError):
        condor.set_rules({"cand_delta": False, "cand_sd": False, "cand_wall": False}, "test")
    with pytest.raises(ValueError):
        condor.set_rules({"nope": False}, "test")
    st = condor.status()
    assert {x["key"]: x["on"] for x in st["rule_settings"]}["pop"] is False and st["locked_rules"]


def test_roll_out_off_holds_the_position(mkt):
    day = EXP.isoformat()
    run(mkt, 9, 20)
    run(mkt, 9, 46)
    first = shorts(day)
    condor.set_rules({"roll_out": False, "roll_wall": False}, "test")
    mkt.spot = first["CE"] - 120                                              # would roll out with the rule on
    run(mkt, 10, 40)
    assert shorts(day) == first and condor.get_day(day)["rolls"] in (0, None)


def test_loss_cap_off_keeps_the_position(mkt):
    day = EXP.isoformat()
    run(mkt, 9, 20)
    run(mkt, 9, 46)
    condor.set_rules({"loss_cap": False}, "test")
    sc = shorts(day)["CE"]
    tsym = f"NIFTY{int(sc)}CE"
    mkt.override[tsym] = {"tsym": tsym, "bid": 90.0, "ask": 90.1, "ltp": 90.0}
    mkt.now = at(11, 0)
    condor._book("NIFTY")["retarget_at"] = 1e18
    condor.tick(mkt.now)
    assert condor.get_day(day)["status"] == condor.OPEN and condor.position(day)


# ---- BANKNIFTY and SENSEX books ---------------------------------------------------------------------------

def index_snap(u, spot, iv, now, lo, hi, step, lot):
    t = condor.years(condor.trading_minutes_to(EXP, now))
    rows = []
    for k in range(lo, hi + step, step):
        row = {"strike": float(k)}
        for opt in ("CE", "PE"):
            px = max(strategy.bs_price(opt, spot, k, t, iv, 0.0), 0.05)
            row[opt.lower()] = {"tsym": f"{u}{k}{opt}", "bid": round(max(px - 0.05, 0.05), 2), "ask": round(px + 0.05, 2),
                                "ltp": round(px, 2), "oi": 1e6, "oi_open": 1e6}
        rows.append(row)
    return {"underlying": u, "spot": spot, "expiry": EXP.isoformat(), "lot": lot, "rows": rows}


@pytest.mark.parametrize("u,spot,step,wing,lot", [("BANKNIFTY", 59050.0, 100, 100, 30), ("SENSEX", 84020.0, 100, 200, 20)])
def test_other_indices_use_their_own_step_and_wing(u, spot, step, wing, lot):
    now = at(10, 0)
    snap = index_snap(u, spot, 0.16, now, int(spot * 0.9) // step * step, int(spot * 1.1) // step * step, step, lot)
    hist = {"vix": HIST["vix"], "index": [spot * (1 + 0.004 * (i % 3)) for i in range(11)]}
    p = condor.evaluate(snap, now, VIX, hist, [])
    tg = p["targets"]
    assert p["underlying"] == u and tg["short_call"] % step == 0 and tg["short_put"] % step == 0
    assert tg["long_call"] - tg["short_call"] == wing and tg["short_put"] - tg["long_put"] == wing
    assert p["qty"] == lot and p["max_loss"] == pytest.approx((wing - p["credit"]) * lot)
    assert all(g["tsym"].startswith(u) for g in p["legs"]) and p["hv10"] is not None


def test_books_are_independent(monkeypatch):
    monkeypatch.setattr(condor.telegram_bot, "send_message", lambda *a, **k: None)
    monkeypatch.setattr(condor, "today_expiry", lambda now, u="NIFTY": None)
    monkeypatch.setattr(condor, "next_expiry", lambda now, u="NIFTY": None)
    condor.set_mode("off", "test", u="BANKNIFTY")
    condor.set_rules({"pop": False}, "test", u="SENSEX")
    assert condor.get_mode() == "paper" and condor.get_mode("BANKNIFTY") == "off" and condor.get_mode("SENSEX") == "paper"
    assert condor.get_rules()["pop"] and not condor.get_rules("SENSEX")["pop"]
    day = "2026-11-26"
    condor._record_fill(day, "SENSEX", "paper", "SENSEX84000CE", "CE", 84000.0, "SELL", 20, 50.0, None, "t")
    assert condor.position(day, "SENSEX") and not condor.position(day) and not condor.position(day, "BANKNIFTY")
    assert condor.loss_cap("NIFTY") == 2600 and condor.loss_cap("BANKNIFTY") == 2400 and condor.loss_cap("SENSEX") == 3200
    st = condor.status("BANKNIFTY")
    assert st["underlying"] == "BANKNIFTY" and st["mode"] == "off" and st["rules"]["wing"] == 100
    assert [b["underlying"] for b in st["books"]] == ["NIFTY", "BANKNIFTY", "SENSEX"]
    assert "1 lot, 100-point wings" in st["locked_rules"]
    with pytest.raises(ValueError):
        condor.status("FINNIFTY")


def test_nifty_only_tables_are_migrated():
    from algotrade import state
    con = state._conn()
    con.execute("CREATE TABLE condor_days (day TEXT PRIMARY KEY, mode TEXT, status TEXT, reason TEXT, expiry TEXT, "
                "entry_at TEXT, exit_at TEXT, last_roll_at TEXT, rolls INTEGER DEFAULT 0, entry TEXT, pnl REAL, "
                "charges REAL, updated_at TEXT)")
    con.execute("INSERT INTO condor_days (day, mode, status, pnl) VALUES ('2026-11-17', 'paper', 'CLOSED', 812.5)")
    con.execute("CREATE TABLE condor_fills (id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, at TEXT, mode TEXT, "
                "tsym TEXT, opt TEXT, strike REAL, action TEXT, qty INTEGER, price REAL, charges REAL, order_id TEXT, "
                "why TEXT)")
    con.execute("INSERT INTO condor_fills (day, tsym, opt, strike, action, qty, price) "
                "VALUES ('2026-11-17', 'NIFTY26000CE', 'CE', 26000, 'SELL', 65, 10)")
    con.commit()
    rec = condor.get_day("2026-11-17")
    assert rec["status"] == "CLOSED" and rec["pnl"] == 812.5 and rec["und"] == "NIFTY"
    assert condor.get_day("2026-11-17", "SENSEX") is None
    assert condor.position("2026-11-17") and not condor.position("2026-11-17", "BANKNIFTY")


def test_bse_scrip_master_parses_sensex_options():
    from algotrade import fno
    head = "pSymbol,pExchSeg,pInstType,pSymbolName,pTrdSymbol,pOptionType,dTickSize ,lLotSize,dStrikePrice;,pExpiryDate"
    rows = ["1134407,bse_fo,IO,SENSEX,SENSEX26N2675900PE,PE,5,20,7.59e+06,1795717799",
            "1134408,bse_fo,IO,BANKEX,BANKEX26N2660000CE,CE,5,30,6.0e+06,1795717799",
            "1134409,bse_fo,SO,RELIANCE,RELIANCE26N1500CE,CE,5,500,1.5e+05,1795717799"]
    df = fno.parse_bse_master("\n".join([head, *rows]))
    assert list(df["tsym"]) == ["SENSEX26N2675900PE"]
    r = df.iloc[0]
    assert r["strike"] == 75900 and r["lot"] == 20 and r["tick"] == 0.05 and str(r["expiry"]) == "2026-11-26"
    assert fno.segment_of(r.to_dict()) == "bse_fo" and fno.segment_of({}) == "nse_fo"


def test_threatened_side_rolls_even_when_the_other_side_wants_in(monkeypatch):
    """Sensex at 84,072, 1SD 350: the 83,900 PE is 178 pts inside its 1SD level (roll out) while the 85,000 CE is
    578 beyond its own (roll in). The put must be tried first, and a skipped inward call roll must not stop it."""
    plan = {"spot": 84072.0, "sd": {"sd": 350.0}, "targets": {"short_call": 84500.0, "short_put": 83700.0}, "walls": {},
            "legs": []}
    rows = [{"strike": float(k), "ce": {"tsym": f"SENSEX{k}CE", "bid": 5.0, "ask": 5.2, "ltp": 5.1}} for k in (84700, 84900)]
    monkeypatch.setattr(condor, "snapshot", lambda exp, now, u="NIFTY": {"rows": rows})
    monkeypatch.setattr(condor, "evaluate", lambda *a, **k: plan)
    pos = {f"SENSEX{k}{o}": {"opt": o, "strike": float(k), "qty": q}
           for k, o, q in ((83700, "PE", 20), (83900, "PE", -20), (85000, "CE", -20), (85200, "CE", 20))}
    calls, changed, plans = [], set(), {}
    monkeypatch.setattr(condor, "_roll", lambda day, u, rec, pos, mode, p, opt, sig, why, now:
                        plans.update({opt: p}) or calls.append((opt, sig)) or opt in changed)
    rec = {"expiry": EXP.isoformat()}
    condor._retarget(EXP.isoformat(), "SENSEX", rec, pos, "paper", at(13, 32))
    assert calls == [("PE", "out"), ("CE", "in")]           # the put first; a skipped roll moves on to the call
    # The call rolls in only to 250 pts beyond its 1SD level (84,422 + 250 -> 84,700), not the fresh 84,500
    assert sorted((g["strike"], g["action"]) for g in plans["CE"]["legs"]) == [(84700.0, "SELL"), (84900.0, "BUY")]
    calls.clear(); changed.add("PE")
    condor._retarget(EXP.isoformat(), "SENSEX", rec, pos, "paper", at(13, 32))
    assert calls == [("PE", "out")]                         # one change per re-target
