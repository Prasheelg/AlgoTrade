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
    ("limit", "BUY"): "Buy limit · on the pullback",
    ("limit", "SELL"): "Sell limit · on the bounce",
    ("stop", "BUY"): "Buy stop · on a break above",
    ("stop", "SELL"): "Sell stop · on a break below",
}
TF_TEXT = {"5m": "5 min", "15m": "15 min", "30m": "30 min", "1h": "1 hour", "1d": "Daily"}
FOOTER = "<i>Not investment advice. Verify before trading.</i>"


def group_digits(v: float, decimals: int = 2, indian: bool = True) -> str:
    """1234567.5 -> "12,34,567.50" (Indian grouping) or "1,234,567.50"."""
    s = f"{abs(v):,.{decimals}f}"
    if indian:
        whole, _, frac = s.replace(",", "").partition(".")
        if len(whole) > 3:
            head, tail = whole[:-3], whole[-3:]
            head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
            whole = f"{head},{tail}"
        s = whole + (f".{frac}" if frac else "")
    return ("-" if v < 0 else "") + s


def money(v: float, mkt, decimals: int = 2) -> str:
    return mkt.currency + group_digits(v, decimals, indian=mkt.code == "IN")


def _levels(rows: list[tuple[str, str, str]]) -> str:
    """Aligned label / price / note lines in a monospaced block (lines up on phones too)."""
    lw = max(len(r[0]) for r in rows)
    pw = max(len(r[1]) for r in rows)
    lines = [f"{lab:<{lw}}  {px:>{pw}}" + (f"  {note}" if note else "") for lab, px, note in rows]
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def _pct(a: float, b: float) -> str:
    return f"{(a - b) / b * 100:+.2f}%" if b else ""


def chart_link(mkt, sym: str) -> str:
    return f"<a href=\"https://www.tradingview.com/chart/?symbol={mkt.tv_prefix}{html.escape(sym)}\">📈 Chart</a>"


def format_signal(sig) -> str:
    mkt = markets.get(sig.market)
    flag = "" if mkt.code == "IN" else f"{mkt.flag} "
    sym = html.escape(sig.symbol)
    risk = abs(sig.entry - sig.stop_loss)
    tf = TF_TEXT.get(sig.timeframe, sig.timeframe)
    order = ORDER_TEXT.get((sig.entry_type, sig.side), "At market")
    lines = [f"{flag}{'🟢' if sig.side == 'BUY' else '🔴'} <b>{sig.side}  {sym}</b>   {'⭐' * sig.score}",
             f"<i>{tf} · {order}</i>", ""]
    # One target: the trade (and Auto-trade's order) closes there
    lines.append(_levels([
        ("Entry", money(sig.entry, mkt), ""),
        ("Stop", money(sig.stop_loss, mkt), _pct(sig.stop_loss, sig.entry)),
        ("Target", money(sig.targets[0], mkt),
         f"{_pct(sig.targets[0], sig.entry)} · {config.TARGET_R_MULTIPLES[0]:g}R"),
    ]))
    lines.append(f"Qty <b>{sig.qty:,}</b> · risk {money(sig.qty * risk, mkt, 0)} "
                 f"<i>({config.RISK_PER_TRADE_PCT:g}% of {money(mkt.capital, mkt, 0)})</i>")
    if sig.entry_type != "market":
        bars = config.ENTRY_VALID_BARS
        unit = "sessions" if sig.timeframe == "1d" else f"× {sig.timeframe} bars, today only"
        lines.append(f"⏳ Now {money(sig.ref_price, mkt)} · order valid {bars} {unit}")
    lines += ["",
              f"🧭 <b>{sig.score} vote{'s' if sig.score != 1 else ''}</b> · "
              + " · ".join(html.escape(r) for r in sig.reasons),
              f"RSI {sig.rsi:g} · ADX {sig.adx:g} · Vol {sig.vol_ratio:g}× avg"]
    if sig.side == "SELL" and mkt.code == "IN":
        lines.append("⚠️ <i>Short: intraday cash or F&amp;O only</i>")
    lines += ["", chart_link(mkt, sig.symbol), FOOTER]
    return "\n".join(lines)


UPDATE_HEAD = {
    "ACTIVE": "✅ <b>Entry filled</b>",
    "CANCELLED": "❌ <b>Entry not reached</b> · order cancelled",
    "T1_HIT": "🎯 <b>Target hit</b> · trade closed",
    "SL_HIT": "🛑 <b>Stop loss hit</b> · trade closed",
    "EXPIRED": "⌛ <b>Time exit</b> · closed at market",
    "T2_HIT": "🏆 <b>Target 2 hit</b> · trade closed",
    "BREAKEVEN": "➖ <b>Back to entry</b> · closed at breakeven",
}


def format_update(sig: dict, res: dict, ltp: float) -> str:
    """A tracked signal's status change: what happened, the stock, prices and the result."""
    mkt = markets.get(sig["market"])
    flag = "" if mkt.code == "IN" else f"{mkt.flag} "
    side, status = sig["side"], res["status"]
    tf = TF_TEXT.get(sig["timeframe"], sig["timeframe"])
    lines = [UPDATE_HEAD.get(status, html.escape(status)),
             f"{flag}{'🟢' if side == 'BUY' else '🔴'} {side}  <b>{html.escape(sig['symbol'])}</b> · {tf}", ""]
    if status == "CANCELLED":
        lines.append(_levels([("Entry", money(sig["entry"], mkt), ""), ("Last", money(ltp, mkt), "")]))
    elif status == "ACTIVE":
        fill = res["fill_price"] or sig["entry"]
        lines.append(_levels([("Filled", money(fill, mkt), ""),
                              ("Stop", money(sig["stop_loss"], mkt), _pct(sig["stop_loss"], fill)),
                              ("Target", money(sig["target1"], mkt), _pct(sig["target1"], fill))]))
    else:
        sign = 1 if side == "BUY" else -1
        fill = res["fill_price"] or sig["entry"]
        px = res["exit_price"] if res["exit_price"] is not None else ltp
        risk = abs(sig["entry"] - sig["stop_loss"])
        pct = sign * (px - fill) / fill * 100
        lines.append(_levels([("Entry", money(fill, mkt), ""), ("Exit", money(px, mkt), "")]))
        lines.append(f"{'🟩' if pct > 0 else '🟥' if pct < 0 else '⬜'} <b>{pct:+.2f}%</b>"
                     + (f" · {sign * (px - fill) / risk:+.2f}R" if risk else ""))
    return "\n".join(lines)


def send_signals(signals) -> list:
    """Send each signal; returns the ones that were delivered."""
    sent = []
    for sig in signals:
        if send_message(format_signal(sig)):
            sent.append(sig)
        time.sleep(1.1)  # stay under Telegram group rate limit (~20 msg/min)
    return sent
