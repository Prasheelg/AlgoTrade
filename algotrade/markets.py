"""Per-market settings: India (NSE), US (NYSE / Nasdaq) and MCX (Indian commodity futures: the minis, traded as
options by Auto-trade)."""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from . import config


@dataclass(frozen=True)
class Market:
    code: str            # "IN" / "US", stored with each signal
    name: str
    flag: str
    currency: str        # symbol shown in alerts and on the dashboard
    locale: str          # number formatting on the dashboard
    tz: str              # exchange timezone; bar timestamps are stored in it
    universe: str        # default universe spec
    capital: float
    min_price: float     # skip penny stocks
    min_turnover: float  # skip illiquid stocks (20-day average daily turnover)
    tv_prefix: str       # TradingView exchange prefix
    session: tuple[time, time]  # regular trading hours, exchange time
    index_symbol: str           # Yahoo ticker of the benchmark index (market filters); "" = none
    scan_from: time | None = None  # the continuous scanner starts this early (None = at the open)
    holidays: dict = field(default_factory=dict)      # "YYYY-MM-DD" -> name: weekdays the exchange is shut
    early_close: dict = field(default_factory=dict)   # "YYYY-MM-DD" -> closing time on a short day

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.tz))

    def holiday(self, day: date) -> str | None:
        """The holiday's name if the exchange is shut on this weekday, else None."""
        return self.holidays.get(day.isoformat())

    def trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day.isoformat() not in self.holidays

    def close_on(self, day: date) -> time:
        return self.early_close.get(day.isoformat(), self.session[1])

    def next_trading_day(self, day: date) -> date:
        """The first trading day after day."""
        day += timedelta(days=1)
        while not self.trading_day(day):
            day += timedelta(days=1)
        return day

    def is_open(self, now: datetime | None = None) -> bool:
        now = now or self.now()
        return self.trading_day(now.date()) and self.session[0] <= now.time() <= self.close_on(now.date())

    @property
    def scan_window(self) -> tuple[time, time]:
        """When the continuous scanner scans, exchange time (weekdays)."""
        return self.scan_from or self.session[0], self.session[1]

    def in_scan_window(self, now: datetime | None = None) -> bool:
        now = now or self.now()
        start, end = self.scan_window
        return self.trading_day(now.date()) and start <= now.time() <= min(end, self.close_on(now.date()))

    def is_after_close(self, now: datetime | None = None) -> bool:
        now = now or self.now()
        return self.trading_day(now.date()) and now.time() > self.close_on(now.date())


# NSE trading holidays (weekdays), from NSE's circular; 2026 checked against the days NIFTY has no daily bar.
# NSE publishes the next year's list in December: add it here then (until it is, those holidays aren't known).
NSE_HOLIDAYS = {
    "2026-01-15": "Municipal elections (Maharashtra)",
    "2026-01-26": "Republic Day",
    "2026-03-03": "Holi",
    "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Guru Nanak Jayanti",
    "2026-12-25": "Christmas",
}
# NYSE holidays and 1:00 p.m. early closes, as published at nyse.com/markets/hours-calendars
NYSE_HOLIDAYS = {
    "2026-01-01": "New Year's Day", "2026-01-19": "Martin Luther King, Jr. Day", "2026-02-16": "Washington's Birthday",
    "2026-04-03": "Good Friday", "2026-05-25": "Memorial Day", "2026-06-19": "Juneteenth",
    "2026-07-03": "Independence Day (observed)", "2026-09-07": "Labor Day", "2026-11-26": "Thanksgiving Day",
    "2026-12-25": "Christmas Day",
    "2027-01-01": "New Year's Day", "2027-01-18": "Martin Luther King, Jr. Day", "2027-02-15": "Washington's Birthday",
    "2027-03-26": "Good Friday", "2027-05-31": "Memorial Day", "2027-06-18": "Juneteenth (observed)",
    "2027-07-05": "Independence Day (observed)", "2027-09-06": "Labor Day", "2027-11-25": "Thanksgiving Day",
    "2027-12-24": "Christmas Day (observed)",
}
NYSE_EARLY_CLOSE = {"2026-11-27": time(13, 0), "2026-12-24": time(13, 0), "2027-11-26": time(13, 0)}

MARKETS = {
    "IN": Market("IN", "India", "🇮🇳", "₹", "en-IN", "Asia/Kolkata", config.UNIVERSE,
                 config.CAPITAL, config.MIN_PRICE, config.MIN_AVG_TURNOVER, "NSE:",
                 (time(9, 15), time(15, 30)), "^NSEI", scan_from=time(9, 0), holidays=NSE_HOLIDAYS),
    "US": Market("US", "US", "🇺🇸", "$", "en-US", "America/New_York", config.US_UNIVERSE,
                 config.US_CAPITAL, config.US_MIN_PRICE, config.US_MIN_AVG_TURNOVER, "",
                 (time(9, 30), time(16, 0)), "^GSPC",
                 holidays=NYSE_HOLIDAYS, early_close=NYSE_EARLY_CLOSE),
    # Futures: no price / turnover floors (a handful of liquid contracts) and no index (trend / RS filters off).
    # No holidays: on most NSE holidays MCX still trades its evening session, which a whole-day list can't say.
    "MCX": Market("MCX", "MCX", "🛢", "₹", "en-IN", "Asia/Kolkata", "MCX", config.MCX_CAPITAL, 0.0, 0.0, "MCX:",
                  (time(9, 0), time(23, 55)), ""),
}
DEFAULT = "IN"


def get(code: str | None) -> Market:
    code = (code or DEFAULT).upper()
    if code not in MARKETS:
        raise ValueError(f"Unknown market {code}; use one of {', '.join(MARKETS)}")
    return MARKETS[code]
