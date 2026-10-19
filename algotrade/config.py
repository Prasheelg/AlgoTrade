"""Central configuration, loaded from environment / .env file."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _get(name: str, default: str) -> str:
    # Strip inline comments like "NIFTY100   # comment"
    return os.getenv(name, default).split("#")[0].strip()


TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID", "")
# Kotak Neo Trade API consumer key: real-time NSE quotes (read-only, no login). Empty = Yahoo only.
KOTAK_CONSUMER_KEY = _get("KOTAK_CONSUMER_KEY", "")
# Kotak trading login (broker.py): unattended TOTP + MPIN. Anyone with these can trade the account.
KOTAK_MOBILE = _get("KOTAK_MOBILE", "")             # registered mobile with country code, e.g. +919876543210
KOTAK_UCC = _get("KOTAK_UCC", "")                   # client code (Neo app > Profile)
KOTAK_MPIN = _get("KOTAK_MPIN", "")
KOTAK_TOTP_SECRET = _get("KOTAK_TOTP_SECRET", "")   # base32 secret behind the TOTP QR code
# Dashboard login for other devices (phone app); this PC needs none. Empty = only this PC can open it.
DASHBOARD_PIN = _get("DASHBOARD_PIN", "")
# Hard cap on automatic orders (India). Trading itself is switched on / off on the dashboard (off by default).
MAX_ORDER_VALUE = float(_get("MAX_ORDER_VALUE", "10000"))   # ₹ per order
# Intraday Auto-trade: BUY signals on 5m-1h become intraday (MIS) trades, no new entries after INTRADAY_LAST_ENTRY,
# all squared off at INTRADAY_SQUARE_OFF (IST, before Kotak's own square-off).
INTRADAY_AUTO_TRADE = _get("INTRADAY_AUTO_TRADE", "false").lower() in ("1", "true", "yes")
INTRADAY_LAST_ENTRY = _get("INTRADAY_LAST_ENTRY", "14:30")
INTRADAY_SQUARE_OFF = _get("INTRADAY_SQUARE_OFF", "15:00")
# Manual option orders (dashboard Strategy Builder tab): fat-finger cap on lots per order
OPTIONS_MAX_LOTS = int(_get("OPTIONS_MAX_LOTS", "10"))
# Manual stock buys (dashboard Portfolio): fat-finger cap on the value of one order, ₹
STOCK_MAX_ORDER_VALUE = float(_get("STOCK_MAX_ORDER_VALUE", "100000"))
# SEBI: no market orders via the API for retail clients. A "market" order is sent as a limit this far past the
# live price (buy above, sell below), so it fills like a market order but can't run away. Stocks / options, in %.
MARKET_PROTECTION_PCT = float(_get("MARKET_PROTECTION_PCT", "1.0"))
OPTION_PROTECTION_PCT = float(_get("OPTION_PROTECTION_PCT", "5.0"))
# Portfolio risk: Auto-trade won't open a trade that takes the total risk at stops (all open app trades and
# pending entries) above this % of CAPITAL; the dashboard's tickets warn about it too
MAX_OPEN_RISK_PCT = float(_get("MAX_OPEN_RISK_PCT", "6.0"))
# Daily summary on Telegram after the NSE close, and commands to the Telegram bot (/positions, /stop ...)
DAILY_SUMMARY = _get("DAILY_SUMMARY", "true").lower() in ("1", "true", "yes")
LIVE_FEED = _get("LIVE_FEED", "true").lower() in ("1", "true", "yes")   # Kotak WebSocket prices
DB_BACKUP = _get("DB_BACKUP", "false").lower() in ("1", "true", "yes")
TELEGRAM_COMMANDS = _get("TELEGRAM_COMMANDS", "true").lower() in ("1", "true", "yes")
# "events" scan filter: skip stocks with results or an ex-dividend date within this many days
EVENT_WINDOW_DAYS = int(_get("EVENT_WINDOW_DAYS", "3"))

UNIVERSE = _get("UNIVERSE", "NIFTY100")
SCAN_INTERVAL_MIN = int(_get("SCAN_INTERVAL_MIN", "10"))
# Timeframes the continuous scanner runs every loop, one after another
SCAN_TIMEFRAMES = [s.strip() for s in _get("SCAN_TIMEFRAMES", "5m,15m,30m,1h,1d").split(",") if s.strip()]
TIMEFRAME = _get("TIMEFRAME", "1d")
MIN_SCORE = int(_get("MIN_SCORE", "4"))
CAPITAL = float(_get("CAPITAL", "100000"))
RISK_PER_TRADE_PCT = float(_get("RISK_PER_TRADE_PCT", "1.0"))
ALLOW_SHORTS = _get("ALLOW_SHORTS", "false").lower() in ("1", "true", "yes")
# Strategies used by the continuous scanner / CLI (empty = all), comma-separated
STRATEGIES = [s.strip() for s in _get("STRATEGIES", "").split(",") if s.strip()]
# Signal filters: "trend" (market above its 200 EMA) and/or "rs" (stock beating the index), comma-separated
FILTERS = [s.strip().lower() for s in _get("FILTERS", "").split(",") if s.strip()]


def _list(name: str, lower: bool = False) -> list[str] | None:
    raw = _get(name, "")
    items = [s.strip().lower() if lower else s.strip() for s in raw.split(",") if s.strip()]
    return items or None


# Per-market overrides for the continuous scanner / CLI (e.g. STRATEGIES_IN, MIN_SCORE_IN, FILTERS_IN);
# a market without them uses STRATEGIES / MIN_SCORE / FILTERS above
def market_scan(code: str) -> dict:
    """The scan() keyword overrides set in the environment for one market."""
    kw = {"strategies": _list(f"STRATEGIES_{code}"), "min_score": int(_get(f"MIN_SCORE_{code}", "") or 0) or None,
          "filters": _list(f"FILTERS_{code}", lower=True)}
    return {k: v for k, v in kw.items() if v is not None}


MARKET_SCAN = {code: market_scan(code) for code in ("IN", "US")}

# Risk model
ATR_SL_MULT = 1.5          # stop-loss = entry -/+ 1.5 * ATR
ENTRY_VALID_BARS = 3       # a pending entry not reached within this many bars is cancelled
TARGET_R_MULTIPLES = (1.5, 3.0)  # targets at 1.5R and 3R
MIN_PRICE = 50.0           # skip penny stocks
MIN_AVG_TURNOVER = 5e7     # skip illiquid stocks (avg 20d turnover < ₹5 Cr)

# Backtest costs (see costs.py); brokerage and DP charge in the market's currency, slippage in basis points
BROKERAGE = float(_get("BROKERAGE", "20"))        # ₹ per order
DP_CHARGE = float(_get("DP_CHARGE", "20"))        # ₹ per delivery sale, incl. GST
SLIPPAGE_BPS = float(_get("SLIPPAGE_BPS", "5"))
US_BROKERAGE = float(_get("US_BROKERAGE", "0"))   # $ per order
US_SLIPPAGE_BPS = float(_get("US_SLIPPAGE_BPS", "2"))

# US market (dashboard US tab)
US_UNIVERSE = _get("US_UNIVERSE", "SP100")
US_CAPITAL = float(_get("US_CAPITAL", "10000"))
US_MIN_PRICE = 5.0         # dollars
US_MIN_AVG_TURNOVER = 2e7  # avg 20d turnover < $20M

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_DB = DATA_DIR / "signals.db"
