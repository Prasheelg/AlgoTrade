"""Nifty 0DTE iron condor (condor.py): the rulebook's maths, its worked example, and a paper expiry day."""
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from algotrade import condor, strategy

IST = ZoneInfo("Asia/Kolkata")
EXP = date(2026, 11, 24)   # a Tuesday


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


def test_option_charges():
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
    monkeypatch.setattr(condor, "snapshot", lambda exp, now: m.snap(exp, now))
    monkeypatch.setattr(condor, "vix_quote", lambda: dict(VIX))
    monkeypatch.setattr(condor, "history", lambda now: HIST)
    monkeypatch.setattr(condor, "today_expiry", lambda now: EXP.isoformat() if now.date() == EXP else None)
    monkeypatch.setattr(condor, "next_expiry", lambda now: EXP.isoformat())
    monkeypatch.setattr(condor, "_quote", m.quote)
    monkeypatch.setattr(condor.fno, "contract", lambda t: {"opt": t[-2:], "strike": float(t[5:-2]), "tick": 0.05})
    sent = []
    monkeypatch.setattr(condor.telegram_bot, "send_message", lambda text, *a, **k: sent.append(text))
    m.sent = sent
    condor._mem.update(plan=None, pnl=None, marks={}, retarget_at=0.0, entry_try=0.0, exit_warned=None)
    condor.set_mode("paper", "test")
    return m


def run(m, h, mi, d=EXP):
    m.now = at(h, mi, d)
    condor._mem["retarget_at"] = 0.0
    condor._mem["entry_try"] = 0.0
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

    # Within the 10-minute cooldown nothing rolls again
    mkt.spot += 150
    run(mkt, 10, 45)
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
    condor._mem["retarget_at"] = 1e18                     # loss check only, no re-target
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


def test_live_mode_needs_the_trading_switch(mkt, monkeypatch):
    condor.set_mode("live", "test")
    monkeypatch.setattr(condor.broker, "trading_enabled", lambda: False)
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
