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

UNIVERSE = _get("UNIVERSE", "NIFTY100")
SCAN_INTERVAL_MIN = int(_get("SCAN_INTERVAL_MIN", "15"))
TIMEFRAME = _get("TIMEFRAME", "1d")
MIN_SCORE = int(_get("MIN_SCORE", "2"))
CAPITAL = float(_get("CAPITAL", "100000"))
RISK_PER_TRADE_PCT = float(_get("RISK_PER_TRADE_PCT", "1.0"))
ALLOW_SHORTS = _get("ALLOW_SHORTS", "false").lower() in ("1", "true", "yes")
# Strategies used by the continuous scanner / CLI (empty = all except the opt-in ones), comma-separated
STRATEGIES = [s.strip() for s in _get("STRATEGIES", "").split(",") if s.strip()]
# Signal filters: "trend" (market above its 200 EMA) and/or "rs" (stock beating the index), comma-separated
FILTERS = [s.strip().lower() for s in _get("FILTERS", "").split(",") if s.strip()]

# Risk model
ATR_SL_MULT = 1.5          # stop-loss = entry -/+ 1.5 * ATR
ENTRY_VALID_BARS = 3       # a pending entry not reached within this many bars is cancelled
TARGET_R_MULTIPLES = (1.5, 3.0)  # targets at 1.5R and 3R
MIN_PRICE = 50.0           # skip penny stocks
MIN_AVG_TURNOVER = 5e7     # skip illiquid stocks (avg 20d turnover < ₹5 Cr)

# US market (dashboard US tab)
US_UNIVERSE = _get("US_UNIVERSE", "SP100")
US_CAPITAL = float(_get("US_CAPITAL", "10000"))
US_MIN_PRICE = 5.0         # dollars
US_MIN_AVG_TURNOVER = 2e7  # avg 20d turnover < $20M

DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
STATE_DB = DATA_DIR / "signals.db"
