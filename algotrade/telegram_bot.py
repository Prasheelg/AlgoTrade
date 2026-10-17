"""Telegram notifications via the Bot HTTP API."""
import html
import logging
import time
from pathlib import Path

import requests

from . import config, markets

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


def send_message(text: str, chat_id: str | None = None) -> bool:
    if not config.TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set")
        return False
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": chat_id or config.TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 429:  # rate limited
                wait = resp.json().get("parameters", {}).get("retry_after", 5)
                time.sleep(wait)
                continue
            if not resp.ok:
                log.error("Telegram error %s: %s", resp.status_code, resp.text)
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Telegram send failed (attempt %d): %s", attempt + 1, exc)
            time.sleep(2)
    return False


def send_document(path, caption: str = "", chat_id: str | None = None) -> bool:
    """Send a file (e.g. the daily database backup) to the group."""
    if not config.TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN is not set")
        return False
    url = API.format(token=config.TELEGRAM_BOT_TOKEN, method="sendDocument")
    try:
        with open(path, "rb") as f:
            resp = requests.post(url, data={"chat_id": chat_id or config.TELEGRAM_CHAT_ID, "caption": caption,
                                            "parse_mode": "HTML"},
                                 files={"document": (Path(path).name, f)}, timeout=60)
    except (OSError, requests.RequestException) as exc:
        log.warning("Telegram file send failed: %s", exc)
        return False
    if not resp.ok:
        log.error("Telegram error %s: %s", resp.status_code, resp.text[:300])
    return resp.ok


ORDER_TEXT = {
    ("limit", "BUY"): "(buy limit — wait for the pullback)",
    ("limit", "SELL"): "(sell limit — wait for the bounce)",
    ("stop", "BUY"): "(buy stop — on a break above)",
    ("stop", "SELL"): "(sell stop — on a break below)",
}


def format_signal(sig) -> str:
    emoji = "🟢" if sig.side == "BUY" else "🔴"
    mkt = markets.get(sig.market)
    c = mkt.currency
    flag = "" if mkt.code == "IN" else f"{mkt.flag} "
    sym = html.escape(sig.symbol)
    risk = abs(sig.entry - sig.stop_loss)
    stars = "⭐" * sig.score
    lines = [
        f"{flag}{emoji} <b>{sig.side} {sym}</b>  {stars}",
        f"<a href=\"https://www.tradingview.com/chart/?symbol={mkt.tv_prefix}{sym}\">Chart</a> · TF: {sig.timeframe}",
        "",
        f"<b>Entry:</b> {c}{sig.entry:,.2f}  {ORDER_TEXT.get((sig.entry_type, sig.side), '')}",
    ]
    if sig.entry_type != "market":
        bars = config.ENTRY_VALID_BARS
        unit = "sessions" if sig.timeframe == "1d" else f"{sig.timeframe} bars, same day only"
        lines.append(f"<i>Now {c}{sig.ref_price:,.2f} · order valid {bars} {unit}</i>")
    lines += [
        f"<b>Stop Loss:</b> {c}{sig.stop_loss:,.2f}  (risk {c}{risk:,.2f}/sh)",
    ]
    for n, (t, m) in enumerate(zip(sig.targets, config.TARGET_R_MULTIPLES), 1):
        lines.append(f"<b>Target {n}:</b> {c}{t:,.2f}  ({m}R)")
    lines += [
        f"<b>Qty:</b> {sig.qty} (risking {config.RISK_PER_TRADE_PCT}% of {c}{mkt.capital:,.0f})",
        "",
        f"<b>Signals ({sig.score}):</b> " + ", ".join(html.escape(r) for r in sig.reasons),
        f"RSI {sig.rsi} · ADX {sig.adx} · Vol {sig.vol_ratio}x avg",
    ]
    if sig.side == "SELL" and mkt.code == "IN":
        lines.append("<i>Short: intraday cash or F&amp;O only</i>")
    lines.append("\n<i>Not investment advice. Verify before trading.</i>")
    return "\n".join(lines)


def send_signals(signals) -> list:
    """Send each signal; returns the ones that were delivered."""
    sent = []
    for sig in signals:
        if send_message(format_signal(sig)):
            sent.append(sig)
        time.sleep(1.1)  # stay under Telegram group rate limit (~20 msg/min)
    return sent
