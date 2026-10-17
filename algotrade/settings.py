"""Settings changed from the dashboard (Settings screen) instead of .env.

Only the trading and alert numbers below can be changed here; Kotak and Telegram credentials and the PIN stay in
.env only. A changed value is stored in signals.db (shared with the scanner) and overrides .env until it is
reset. Both processes call apply() regularly, so a change reaches the scanner on its next loop."""
import logging
from dataclasses import replace

from . import config, markets, state

log = logging.getLogger(__name__)
KEY = "config_overrides"

# name: (type, min, max, label, help)
EDITABLE = {
    "CAPITAL": (float, 1000, 1e9, "Capital (₹)", "India account size: position sizes and risk % are based on it"),
    "US_CAPITAL": (float, 100, 1e8, "US capital ($)", "Account size for US signal sizing"),
    "RISK_PER_TRADE_PCT": (float, 0.1, 5, "Risk per trade (%)", "Of capital, lost if a trade hits its stop"),
    "MAX_OPEN_RISK_PCT": (float, 0.5, 50, "Max open risk (%)",
                          "Of capital, at stops across all app trades; Auto-trade opens nothing beyond it"),
    "MAX_ORDERS_PER_DAY": (int, 0, 20, "Auto-trade: new trades per day", "Hard cap on automatic entries"),
    "MAX_ORDER_VALUE": (float, 0, 1e7, "Auto-trade: max ₹ per order", "Quantity is cut to fit"),
    "STOCK_MAX_ORDER_VALUE": (float, 0, 1e7, "Manual stock order: max ₹", "Fat-finger cap on your own buys"),
    "OPTIONS_MAX_LOTS": (int, 1, 50, "Options: max lots per order", "Fat-finger cap on option orders"),
    "MARKET_PROTECTION_PCT": (float, 0.1, 5, "Market order limit, stocks (%)",
                              "\"Market\" orders go in as a limit this far past the live price"),
    "OPTION_PROTECTION_PCT": (float, 0.5, 20, "Market order limit, options (%)", "The same for options"),
    "EVENT_WINDOW_DAYS": (int, 0, 15, "Events filter window (days)",
                          "The Events scan filter skips stocks with results / ex-dividend this close"),
    "DAILY_SUMMARY": (bool, None, None, "Daily summary on Telegram", "After the NSE close"),
    "LIVE_FEED": (bool, None, None, "Live price stream", "Kotak WebSocket prices in the dashboard (else quotes every few seconds)"),
    "DB_BACKUP": (bool, None, None, "Daily database backup", "Copy signals.db to data/backups and Telegram after 16:00"),
    "TELEGRAM_COMMANDS": (bool, None, None, "Telegram commands", "/positions, /pnl, /stop … in the group"),
}
_DEFAULTS = {name: getattr(config, name) for name in EDITABLE}   # the .env values, as loaded at start


def _overrides() -> dict:
    return state.get_setting(KEY, {})[0] or {}


def _set(name: str, value) -> None:
    setattr(config, name, value)
    if name == "CAPITAL":
        markets.MARKETS["IN"] = replace(markets.MARKETS["IN"], capital=float(value))
    elif name == "US_CAPITAL":
        markets.MARKETS["US"] = replace(markets.MARKETS["US"], capital=float(value))


def apply() -> None:
    """Bring config in line with the stored overrides (None = back to the .env value)."""
    for name, value in _overrides().items():
        if name not in EDITABLE:
            continue
        want = _DEFAULTS[name] if value is None else value
        if getattr(config, name) != want:
            _set(name, want)


def current() -> list[dict]:
    ov = _overrides()
    return [{"name": n, "value": getattr(config, n), "default": _DEFAULTS[n], "changed": ov.get(n) is not None,
             "type": t.__name__, "min": lo, "max": hi, "label": label, "help": help_}
            for n, (t, lo, hi, label, help_) in EDITABLE.items()]


def _parse(name: str, raw):
    t, lo, hi, label, _ = EDITABLE[name]
    if t is bool:
        if not isinstance(raw, bool):
            raise ValueError(f"{label}: on or off")
        return raw
    try:
        v = t(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label}: a number, please")
    if t is int and float(raw) != v:
        raise ValueError(f"{label}: a whole number, please")
    if not lo <= v <= hi:
        raise ValueError(f"{label}: between {lo:g} and {hi:g}")
    return v


def update(changes: dict, by: str) -> list[dict]:
    """Change settings: {name: value, or None to go back to .env}. Validates all before saving any."""
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise ValueError(f"Can't change {', '.join(sorted(unknown))} here")
    parsed = {n: None if v is None else _parse(n, v) for n, v in changes.items()}
    ov = {**_overrides(), **parsed}
    state.set_setting(KEY, ov)
    apply()
    lines = [f"{EDITABLE[n][3]}: {_DEFAULTS[n] if v is None else v}{' (.env)' if v is None else ''}" for n, v in parsed.items()]
    log.warning("Settings changed by %s: %s", by, "; ".join(lines))
    try:
        from . import telegram_bot
        telegram_bot.send_message("⚙️ <b>Settings changed</b> from the " + by + "\n" + "\n".join(lines))
    except Exception:
        log.exception("Telegram settings notice failed")
    return current()
