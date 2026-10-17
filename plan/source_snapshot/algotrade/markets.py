"""Per-market settings: India (NSE) and US (NYSE / Nasdaq)."""
from dataclasses import dataclass
from datetime import datetime, time
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
    index_symbol: str           # Yahoo ticker of the benchmark index (market filters)

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.tz))

    def is_open(self, now: datetime | None = None) -> bool:
        now = now or self.now()
        return now.weekday() < 5 and self.session[0] <= now.time() <= self.session[1]

    def is_after_close(self, now: datetime | None = None) -> bool:
        now = now or self.now()
        return now.weekday() < 5 and now.time() > self.session[1]


MARKETS = {
    "IN": Market("IN", "India", "🇮🇳", "₹", "en-IN", "Asia/Kolkata", config.UNIVERSE,
                 config.CAPITAL, config.MIN_PRICE, config.MIN_AVG_TURNOVER, "NSE:",
                 (time(9, 15), time(15, 30)), "^NSEI"),
    "US": Market("US", "US", "🇺🇸", "$", "en-US", "America/New_York", config.US_UNIVERSE,
                 config.US_CAPITAL, config.US_MIN_PRICE, config.US_MIN_AVG_TURNOVER, "",
                 (time(9, 30), time(16, 0)), "^GSPC"),
}
DEFAULT = "IN"


def get(code: str | None) -> Market:
    code = (code or DEFAULT).upper()
    if code not in MARKETS:
        raise ValueError(f"Unknown market {code}; use one of {', '.join(MARKETS)}")
    return MARKETS[code]
