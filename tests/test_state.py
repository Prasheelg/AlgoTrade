"""P3: state (SQLite journal) on a temp DB, including migration of a legacy table."""
import re
import sqlite3
from datetime import datetime

import pandas as pd
import pytest

from algotrade import config, state
from conftest import make_signal

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
LEGACY_COLS = ["key", "symbol", "side", "entry", "stop_loss", "target1", "target2", "score", "reasons", "sent_at"]


def cols():
    con = sqlite3.connect(config.STATE_DB)
    try:
        return [(r[1], r[2], r[4]) for r in con.execute("PRAGMA table_info(signals)")]
    finally:
        con.close()


def test_fresh_schema():
    assert state.all_signals() == []
    names = [c[0] for c in cols()]
    assert names == LEGACY_COLS + list(state._EXTRA_COLUMNS)
    defaults = {n: d for n, _, d in cols()}
    assert defaults["status"] == "'ACTIVE'" and defaults["timeframe"] == "'1d'"
    assert defaults["market"] == "'IN'" and defaults["telegram"] == "1" and defaults["entry_type"] == "'market'"


def test_migration_from_legacy_table():
    """Quirk 3: pre-pending-entry rows migrate to entry_type 'market', telegram 1, status ACTIVE."""
    con = sqlite3.connect(config.STATE_DB)
    con.execute("""CREATE TABLE signals (key TEXT PRIMARY KEY, symbol TEXT, side TEXT, entry REAL,
                   stop_loss REAL, target1 REAL, target2 REAL, score INTEGER, reasons TEXT, sent_at TEXT)""")
    con.executemany("INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?)", [
        ("TCS|BUY|2024-05-06", "TCS", "BUY", 3500.0, 3450.0, 3575.0, 3650.0, 2, "A, B", "2024-05-06T15:40:00"),
        ("INFY|SELL|2024-05-07", "INFY", "SELL", 1400.0, 1420.0, 1370.0, 1340.0, 3, "C", "2024-05-07T15:40:00"),
    ])
    con.commit()
    con.close()

    rows = {r["key"]: r for r in state.all_signals()}
    assert set(rows) == {"TCS|BUY|2024-05-06", "INFY|SELL|2024-05-07"}
    r = rows["TCS|BUY|2024-05-06"]
    assert r["bar_date"] == "2024-05-06"
    assert r["bar_time"] == "2024-05-06T00:00:00"
    assert r["status"] == "ACTIVE" and r["timeframe"] == "1d" and r["market"] == "IN"
    assert r["entry_type"] == "market" and r["telegram"] == 1
    for k in ("ltp", "exit_price", "closed_at", "updated_at", "ref_price", "fill_price", "filled_at"):
        assert r[k] is None
    assert [c[0] for c in cols()] == LEGACY_COLS + list(state._EXTRA_COLUMNS)
    # ordering: sent_at DESC
    assert [r["key"] for r in state.all_signals()] == ["INFY|SELL|2024-05-07", "TCS|BUY|2024-05-06"]
    # migrated rows are open
    assert len(state.open_signals()) == 2


def test_migration_backfills_null_columns_on_every_connect():
    state.all_signals()
    con = sqlite3.connect(config.STATE_DB)
    con.execute("INSERT INTO signals (key, symbol, side, status, timeframe, market) "
                "VALUES ('US|X|BUY|2024-01-02', 'X', 'BUY', NULL, NULL, NULL)")
    con.commit()
    con.close()
    r = state.all_signals()[0]
    # bar_date = last 10 chars of key, status/timeframe/market reset to defaults (market 'IN', even for a US key)
    assert (r["bar_date"], r["status"], r["timeframe"], r["market"], r["bar_time"]) == \
        ("2024-01-02", "ACTIVE", "1d", "IN", "2024-01-02T00:00:00")


def test_record_limit_and_market_rows():
    lim = make_signal(symbol="A", ref_price=100.0)
    mkt = make_signal(symbol="B", entry_type="market", ref_price=0.0, entry=55.5)
    stp = make_signal(symbol="C", entry_type="stop", market="US", timeframe="1h",
                      bar_time=pd.Timestamp("2024-01-01 10:30"))
    state.record([lim, mkt, stp], telegram=True)
    rows = {r["symbol"]: r for r in state.all_signals()}
    a = rows["A"]
    assert a["key"] == "A|BUY|2024-01-01" and a["status"] == "PENDING" and a["ltp"] == 100.0
    assert (a["entry"], a["stop_loss"], a["target1"], a["target2"]) == (99.0, 96.0, 103.5, 108.0)
    assert a["reasons"] == "EMA 9/21 Cross, RSI Pullback" and a["score"] == 2
    assert a["bar_date"] == "2024-01-01" and a["bar_time"] == "2024-01-01T00:00:00"
    assert a["entry_type"] == "limit" and a["ref_price"] == 100.0 and a["telegram"] == 1
    assert TS.match(a["sent_at"]) and a["sent_at"] == a["updated_at"]
    assert a["fill_price"] is None and a["filled_at"] is None
    b = rows["B"]
    assert b["status"] == "ACTIVE" and b["ltp"] == 55.5 and b["ref_price"] is None  # ref_price 0 -> NULL
    c = rows["C"]
    assert c["status"] == "PENDING" and c["key"] == "US|C|BUY|1h|2024-01-01"
    assert c["bar_time"] == "2024-01-01T10:30:00" and c["market"] == "US" and c["timeframe"] == "1h"


def test_record_single_target_uses_it_for_both():
    state.record([make_signal(targets=[110.0])])
    r = state.all_signals()[0]
    assert r["target1"] == r["target2"] == 110.0


def test_record_insert_or_ignore_keeps_first_and_stores_telegram_flag():
    state.record([make_signal(entry=99.0)], telegram=False)
    state.record([make_signal(entry=50.0)], telegram=True)
    rows = state.all_signals()
    assert len(rows) == 1 and rows[0]["entry"] == 99.0 and rows[0]["telegram"] == 0


def test_record_empty_list_is_noop():
    state.record([])
    assert state.all_signals() == []


def test_filter_new_drops_stored_keys():
    a, b = make_signal(symbol="A"), make_signal(symbol="B")
    state.record([a])
    assert [s.symbol for s in state.filter_new([a, b])] == ["B"]
    # same symbol different side / day is new
    assert len(state.filter_new([make_signal(symbol="A", side="SELL"),
                                 make_signal(symbol="A", bar_time=pd.Timestamp("2024-01-02"))])) == 2


def test_all_signals_market_filter_and_order(monkeypatch):
    times = iter([datetime(2024, 1, 1, 9), datetime(2024, 1, 1, 11), datetime(2024, 1, 1, 10)])

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(times)

    monkeypatch.setattr(state, "datetime", FakeDT)
    state.record([make_signal(symbol="A")])
    state.record([make_signal(symbol="B", market="US")])
    state.record([make_signal(symbol="C")])
    assert [r["symbol"] for r in state.all_signals()] == ["B", "C", "A"]
    assert [r["symbol"] for r in state.all_signals("IN")] == ["C", "A"]
    assert [r["symbol"] for r in state.all_signals("US")] == ["B"]
    assert state.all_signals("XX") == []
    assert state.all_signals("")[0]["symbol"] == "B"   # falsy market -> all
    assert state.all_signals()[0]["sent_at"] == "2024-01-01T11:00:00"


def test_open_signals_only_open_statuses():
    assert state.OPEN_STATUSES == ("PENDING", "ACTIVE", "T1_HIT")
    syms = ["P", "A", "T1", "T2", "SL", "BE", "EX", "CA"]
    state.record([make_signal(symbol=s) for s in syms])
    for s, st in zip(syms[1:], ["ACTIVE", "T1_HIT", "T2_HIT", "SL_HIT", "BREAKEVEN", "EXPIRED", "CANCELLED"]):
        state.update_signal(f"{s}|BUY|2024-01-01", status=st)
    assert sorted(r["symbol"] for r in state.open_signals()) == ["A", "P", "T1"]


def test_update_signal_sets_fields_and_updated_at(monkeypatch):
    state.record([make_signal()])

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2030, 2, 3, 4, 5, 6, 789)

    monkeypatch.setattr(state, "datetime", FakeDT)
    state.update_signal("RELIANCE|BUY|2024-01-01", ltp=101.5, status="ACTIVE", fill_price=99.0)
    r = state.all_signals()[0]
    assert (r["ltp"], r["status"], r["fill_price"], r["updated_at"]) == (101.5, "ACTIVE", 99.0, "2030-02-03T04:05:06")
    state.update_signal("nope", ltp=1.0)  # unknown key: silent no-op
    assert state.all_signals()[0]["ltp"] == 101.5


def test_update_signal_unknown_column_raises():
    state.record([make_signal()])
    with pytest.raises(sqlite3.OperationalError):
        state.update_signal("RELIANCE|BUY|2024-01-01", bogus=1)
