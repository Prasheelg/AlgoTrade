"""OI change by strike over a window (oiflow): minute history, window maths, full day from the previous close."""
from datetime import datetime

import pytest

from algotrade import config, fno, oiflow


def chain(spot, legs, atm=100.0, spot_chg=10.0):
    """A minimal fno.chain result: legs = {strike: (ce_oi, pe_oi, ce_day_chg, pe_day_chg)}."""
    rows = [{"strike": k, "ce": {"tsym": f"X{k:g}CE", "oi": v[0], "oi_chg": v[2], "oi_prev_day": "2026-11-18"},
             "pe": {"tsym": f"X{k:g}PE", "oi": v[1], "oi_chg": v[3], "oi_prev_day": "2026-11-18"}} for k, v in legs.items()]
    return {"underlying": "X", "spot": {"ltp": spot, "change": spot_chg}, "atm": atm, "rows": rows}


@pytest.fixture
def flow(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(oiflow, "_hist", {"day": None, "times": [], "oi": {}, "spot": {}})
    monkeypatch.setattr(oiflow, "_track", {})
    monkeypatch.setattr(oiflow, "_state", {"saved": 0.0, "dirty": False, "thread": None})
    monkeypatch.setattr(fno, "expiries", lambda u: ["2026-11-23"])
    monkeypatch.setattr(oiflow, "DEFAULT_TRACK", ("X",))
    cur = {}
    monkeypatch.setattr(fno, "chain", lambda u, e, w, fresh=False: cur[e])
    return cur


def at(h, m):
    return datetime(2026, 11, 19, h, m, tzinfo=oiflow._nse().now().tzinfo)


def test_full_day_uses_change_since_previous_close(flow):
    flow["2026-11-23"] = chain(105.0, {100.0: (500, 800, 50, -20), 110.0: (300, 100, None, 10)})
    d = oiflow.change("X", ["2026-11-23"], 10, None, now=at(12, 0))
    r100, r110 = d["rows"]
    assert (r100["ce_chg"], r100["pe_chg"]) == (50, -20)
    assert r110["ce_known"] is False and r110["pe_chg"] == 10            # unknown change is flagged, not guessed
    assert d["spot_from"] == 95.0 and d["prev_day"] == "2026-11-18" and d["pcr"] == round(900 / 800, 2)


def test_window_against_the_minute_history(flow):
    oiflow._ensure_day("2026-11-19")
    oiflow.record([chain(100.0, {100.0: (400, 700, 0, 0)})], "11:50")
    oiflow.record([chain(102.0, {100.0: (450, 690, 0, 0)})], "11:55")
    flow["2026-11-23"] = chain(105.0, {100.0: (500, 800, 0, 0)})
    d = oiflow.change("X", ["2026-11-23"], 10, 5, now=at(12, 0))        # 12:00 - 5 min = 11:55
    assert d["since"] == "11:55" and d["rows"][0]["ce_chg"] == 50 and d["rows"][0]["pe_chg"] == 110
    assert d["spot_from"] == 102.0 and d["note"] is None
    d = oiflow.change("X", ["2026-11-23"], 10, 60, now=at(12, 0))       # before recording began: from its start
    assert d["since"] == "11:50" and d["rows"][0]["ce_chg"] == 100 and "Recording began at 11:50" in d["note"]


def test_after_the_close_the_window_ends_at_the_last_minute(flow):
    oiflow._ensure_day("2026-11-19")
    for t, oi in (("15:20", 100), ("15:25", 130), ("15:30", 160)):
        oiflow.record([chain(100.0, {100.0: (oi, oi, 0, 0)})], t)
    flow["2026-11-23"] = chain(100.0, {100.0: (160, 160, 0, 0)})
    d = oiflow.change("X", ["2026-11-23"], 10, 5, now=at(18, 30))
    assert d["since"] == "15:25" and d["rows"][0]["ce_chg"] == 30 and d["is_open"] is False


def test_expiries_are_summed_per_strike(flow):
    flow["2026-11-23"] = chain(100.0, {100.0: (500, 800, 50, -20)})
    flow["2026-12-01"] = chain(100.0, {100.0: (200, 100, 5, 5), 105.0: (10, 20, 1, 2)})
    d = oiflow.change("X", ["2026-11-23", "2026-12-01"], 10, None, now=at(12, 0))
    assert [r["strike"] for r in d["rows"]] == [100.0, 105.0]
    assert (d["rows"][0]["ce_oi"], d["rows"][0]["ce_chg"]) == (700, 55) and d["ce_chg"] == 56 and d["pe_chg"] == -13


def test_snapshot_only_in_market_hours_and_saved(flow):
    flow["2026-11-23"] = chain(100.0, {100.0: (500, 800, 0, 0)})
    assert oiflow.snapshot(at(8, 0)) == 0
    assert oiflow.snapshot(at(10, 1)) == 1 and oiflow._hist["oi"]["X100CE"] == {"10:01": 500}
    oiflow._save(force=True)
    oiflow._hist.update(day=None, times=[], oi={}, spot={})
    oiflow._ensure_latest()                                             # e.g. Saturday: Thursday's file
    assert oiflow._hist["day"] == "2026-11-19" and oiflow._hist["spot"]["X"] == {"10:01": 100.0}


def test_api_oichange(flow, monkeypatch):
    from algotrade import web
    flow["2026-11-23"] = chain(100.0, {100.0: (500, 800, 50, -20)})
    c = web.app.test_client()
    j = c.get("/api/options/oichange?u=x&expiries=2026-11-23&width=10").get_json()
    assert j["underlying"] == "X" and j["ce_chg"] == 50
    assert c.get("/api/options/oichange?u=X&expiries=2026-11-23&window=7").status_code == 400
    assert c.get("/api/options/oichange?u=X").status_code == 400
