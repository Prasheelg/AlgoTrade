"""P8: markets.Market sessions with injected datetimes, and markets.get."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from algotrade import markets

IN, US = markets.MARKETS["IN"], markets.MARKETS["US"]


def at(m, *a):
    return datetime(*a, tzinfo=ZoneInfo(m.tz))


@pytest.mark.parametrize("args,open_,after", [
    ((2024, 1, 1, 9, 14, 59), False, False),   # Monday, before open
    ((2024, 1, 1, 9, 15), True, False),
    ((2024, 1, 1, 12, 0), True, False),
    ((2024, 1, 1, 15, 30), True, False),       # quirk 15: bounds inclusive
    ((2024, 1, 1, 15, 30, 0, 1), False, True),
    ((2024, 1, 1, 23, 59), False, True),
    ((2024, 1, 5, 15, 0), True, False),        # Friday
    ((2024, 1, 6, 12, 0), False, False),       # Saturday
    ((2024, 1, 7, 16, 0), False, False),       # Sunday
    ((2024, 1, 26, 11, 0), True, False),       # quirk 2: Republic Day holiday still counts as open
])
def test_india_session(args, open_, after):
    now = at(IN, *args)
    assert IN.is_open(now) is open_
    assert IN.is_after_close(now) is after


@pytest.mark.parametrize("args,open_,after", [
    ((2024, 7, 1, 9, 29), False, False),
    ((2024, 7, 1, 9, 30), True, False),
    ((2024, 7, 1, 16, 0), True, False),
    ((2024, 7, 1, 16, 1), False, True),
    ((2024, 7, 4, 12, 0), True, False),        # quirk 2: Independence Day holiday still "open"
    ((2024, 7, 6, 12, 0), False, False),
])
def test_us_session(args, open_, after):
    now = at(US, *args)
    assert US.is_open(now) is open_
    assert US.is_after_close(now) is after


def test_naive_datetimes_work_too():
    assert IN.is_open(datetime(2024, 1, 1, 10, 0)) is True


def test_now_used_when_not_given(monkeypatch):
    monkeypatch.setattr(markets.Market, "now", lambda self: at(self, 2024, 1, 1, 10, 0))
    assert IN.is_open() is True and US.is_open() is True and IN.is_after_close() is False


def test_real_now_is_aware_in_exchange_tz():
    assert IN.now().tzinfo.key == "Asia/Kolkata" and US.now().tzinfo.key == "America/New_York"


def test_get():
    assert markets.get(None) is IN and markets.get("") is IN
    assert markets.get("us") is US and markets.get("In") is IN
    with pytest.raises(ValueError) as e:
        markets.get("jp")
    assert str(e.value) == "Unknown market JP; use one of IN, US, MCX"


def test_market_fields():
    assert (IN.code, IN.name, IN.currency, IN.locale, IN.tz, IN.tv_prefix, IN.index_symbol) == \
        ("IN", "India", "₹", "en-IN", "Asia/Kolkata", "NSE:", "^NSEI")
    assert (US.code, US.currency, US.tz, US.tv_prefix, US.index_symbol) == \
        ("US", "$", "America/New_York", "", "^GSPC")
    assert markets.DEFAULT == "IN" and list(markets.MARKETS) == ["IN", "US", "MCX"]


def test_market_is_frozen():
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        IN.capital = 1


# ---- holidays (quirk 2 fixed for the years listed) ----

def test_nse_holiday_is_closed_all_day():
    now = at(IN, 2026, 11, 24, 11, 0)   # Guru Nanak Jayanti, a Tuesday
    assert IN.holiday(now.date()) == "Guru Nanak Jayanti"
    assert not IN.is_open(now) and not IN.in_scan_window(now) and not IN.is_after_close(at(IN, 2026, 11, 24, 16, 0))
    assert IN.is_open(at(IN, 2026, 11, 25, 11, 0))


def test_next_trading_day_skips_holidays_and_weekends():
    from datetime import date
    assert IN.next_trading_day(date(2026, 11, 23)) == date(2026, 11, 25)
    assert IN.next_trading_day(date(2026, 12, 24)) == date(2026, 12, 28)   # Christmas Fri, then the weekend
    assert not IN.trading_day(date(2026, 11, 28)) and IN.trading_day(date(2026, 11, 27))


def test_us_holiday_and_early_close():
    assert not US.is_open(at(US, 2026, 11, 26, 12, 0))                  # Thanksgiving
    assert US.is_open(at(US, 2026, 11, 27, 12, 59))
    assert not US.is_open(at(US, 2026, 11, 27, 13, 1))                  # 1:00 p.m. close
    assert US.is_after_close(at(US, 2026, 11, 27, 13, 1)) and not US.in_scan_window(at(US, 2026, 11, 27, 14, 0))


def test_mcx_has_no_holiday_list():
    assert markets.MARKETS["MCX"].holidays == {}


def test_holidays_are_weekdays():
    from datetime import date
    for m in (IN, US):
        assert all(date.fromisoformat(d).weekday() < 5 for d in m.holidays), m.code
