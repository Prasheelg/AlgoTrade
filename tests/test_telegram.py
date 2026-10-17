"""P9: telegram_bot.format_signal snapshots, send_message retry logic, send_signals."""
from types import SimpleNamespace

import pytest
import requests

from algotrade import config, telegram_bot
from conftest import ORIGINAL_SEND, FakeResponse, Recorder, make_signal

FOOTER = "\n\n<i>Not investment advice. Verify before trading.</i>"


def test_format_in_buy_limit():
    assert telegram_bot.format_signal(make_signal()) == (
        "🟢 <b>BUY RELIANCE</b>  ⭐⭐\n"
        "<a href=\"https://www.tradingview.com/chart/?symbol=NSE:RELIANCE\">Chart</a> · TF: 1d\n"
        "\n"
        "<b>Entry:</b> ₹99.00  (buy limit — wait for the pullback)\n"
        "<i>Now ₹100.00 · order valid 3 sessions</i>\n"
        "<b>Stop Loss:</b> ₹96.00  (risk ₹3.00/sh)\n"
        "<b>Target 1:</b> ₹103.50  (1.5R)\n"
        "<b>Target 2:</b> ₹108.00  (3.0R)\n"
        "<b>Qty:</b> 333 (risking 1.0% of ₹100,000)\n"
        "\n"
        "<b>Signals (2):</b> EMA 9/21 Cross, RSI Pullback\n"
        "RSI 55.0 · ADX 25.0 · Vol 2.0x avg" + FOOTER)


def test_format_us_stop_intraday():
    s = make_signal(symbol="BRK.B", market="US", entry_type="stop", timeframe="15m", entry=1234.5,
                    stop_loss=1200.0, targets=[1286.25, 1338.0], qty=2, score=3, ref_price=1230.0,
                    reasons=["EMA 9/21 Cross", "20-bar Breakout", "MACD Trend Cross"])
    assert telegram_bot.format_signal(s) == (
        "🇺🇸 🟢 <b>BUY BRK.B</b>  ⭐⭐⭐\n"
        "<a href=\"https://www.tradingview.com/chart/?symbol=BRK.B\">Chart</a> · TF: 15m\n"
        "\n"
        "<b>Entry:</b> $1,234.50  (buy stop — on a break above)\n"
        "<i>Now $1,230.00 · order valid 3 15m bars, same day only</i>\n"
        "<b>Stop Loss:</b> $1,200.00  (risk $34.50/sh)\n"
        "<b>Target 1:</b> $1,286.25  (1.5R)\n"
        "<b>Target 2:</b> $1,338.00  (3.0R)\n"
        "<b>Qty:</b> 2 (risking 1.0% of $10,000)\n"
        "\n"
        "<b>Signals (3):</b> EMA 9/21 Cross, 20-bar Breakout, MACD Trend Cross\n"
        "RSI 55.0 · ADX 25.0 · Vol 2.0x avg" + FOOTER)


def test_format_in_sell_has_short_note():
    s = make_signal(side="SELL", entry=101.0, stop_loss=104.0, targets=[96.5, 92.0])
    text = telegram_bot.format_signal(s)
    assert text.startswith("🔴 <b>SELL RELIANCE</b>")
    assert "<b>Entry:</b> ₹101.00  (sell limit — wait for the bounce)\n" in text
    assert text.endswith("Vol 2.0x avg\n<i>Short: intraday cash or F&amp;O only</i>" + FOOTER)
    us = telegram_bot.format_signal(make_signal(side="SELL", market="US", entry_type="stop"))
    assert "Short:" not in us and "(sell stop — on a break below)" in us


def test_format_market_entry_has_no_now_line():
    text = telegram_bot.format_signal(make_signal(entry_type="market"))
    assert "<i>Now" not in text
    assert "\n<b>Entry:</b> ₹99.00  \n<b>Stop Loss:</b>" in text  # no order text, trailing spaces kept


def test_format_escapes_html():
    text = telegram_bot.format_signal(make_signal(symbol="M&M", reasons=["A<B"], score=1))
    assert "<b>BUY M&amp;M</b>  ⭐\n" in text
    assert "symbol=NSE:M&amp;M\"" in text
    assert "<b>Signals (1):</b> A&lt;B" in text


def test_format_single_target_and_live_config(monkeypatch):
    monkeypatch.setattr(config, "RISK_PER_TRADE_PCT", 0.5)
    monkeypatch.setattr(config, "ENTRY_VALID_BARS", 5)
    text = telegram_bot.format_signal(make_signal(targets=[103.5]))
    assert "Target 2" not in text and "(risking 0.5% of ₹100,000)" in text and "valid 5 sessions" in text


# ---- send_message --------------------------------------------------------------------------------

@pytest.fixture
def tg(monkeypatch):
    monkeypatch.setattr(telegram_bot, "send_message", ORIGINAL_SEND)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "TESTTOKEN")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "-100")
    sleeps = []
    monkeypatch.setattr(telegram_bot, "time", SimpleNamespace(sleep=sleeps.append))

    def install(*responses):
        """Scripted requests.post: each call returns (or raises) the next item."""
        seq = list(responses)

        def post(*a, **k):
            r = seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        rec = Recorder(post)
        monkeypatch.setattr(requests, "post", rec)
        return rec
    return install, sleeps


def test_send_message_without_token_does_not_post(monkeypatch):
    monkeypatch.setattr(telegram_bot, "send_message", ORIGINAL_SEND)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("posted"))
    assert telegram_bot.send_message("hi") is False


def test_send_message_ok_payload(tg):
    install, sleeps = tg
    post = install(FakeResponse(200))
    assert telegram_bot.send_message("hello") is True
    assert post.calls == [(("https://api.telegram.org/botTESTTOKEN/sendMessage",),
                           {"json": {"chat_id": "-100", "text": "hello", "parse_mode": "HTML",
                                     "disable_web_page_preview": True}, "timeout": 15})]
    install(FakeResponse(200))
    telegram_bot.send_message("x", chat_id="42")
    assert sleeps == []


def test_send_message_chat_id_override(tg):
    install, _ = tg
    post = install(FakeResponse(200))
    telegram_bot.send_message("x", chat_id="42")
    assert post.calls[0][1]["json"]["chat_id"] == "42"


def test_send_message_error_status(tg):
    install, sleeps = tg
    post = install(FakeResponse(400, text="Bad Request"))
    assert telegram_bot.send_message("x") is False
    assert len(post.calls) == 1 and sleeps == []


def test_send_message_429_retry_after(tg):
    install, sleeps = tg
    post = install(FakeResponse(429, json={"parameters": {"retry_after": 7}}), FakeResponse(429), FakeResponse(200))
    assert telegram_bot.send_message("x") is True
    assert sleeps == [7, 5] and len(post.calls) == 3


def test_send_message_429_three_times_gives_up(tg):
    install, sleeps = tg
    install(*[FakeResponse(429, json={"parameters": {"retry_after": 1}})] * 3)
    assert telegram_bot.send_message("x") is False
    assert sleeps == [1, 1, 1]


def test_send_message_request_exceptions_retry(tg):
    install, sleeps = tg
    install(requests.ConnectionError("a"), requests.Timeout("b"), requests.ConnectionError("c"))
    assert telegram_bot.send_message("x") is False
    assert sleeps == [2, 2, 2]
    install(requests.ConnectionError("a"), FakeResponse(200))
    assert telegram_bot.send_message("x") is True


def test_send_message_other_exceptions_propagate(tg):
    install, _ = tg
    install(ValueError("bug"))
    with pytest.raises(ValueError):
        telegram_bot.send_message("x")


# ---- send_signals --------------------------------------------------------------------------------

def test_send_signals_keeps_only_delivered(monkeypatch):
    sleeps = []
    monkeypatch.setattr(telegram_bot, "time", SimpleNamespace(sleep=sleeps.append))
    sent = Recorder(lambda text: "FAIL" not in text)
    monkeypatch.setattr(telegram_bot, "send_message", sent)
    sigs = [make_signal(symbol="A"), make_signal(symbol="FAIL"), make_signal(symbol="C")]
    out = telegram_bot.send_signals(sigs)
    assert [s.symbol for s in out] == ["A", "C"]
    assert sleeps == [1.1, 1.1, 1.1]
    assert sent.calls[0][0][0] == telegram_bot.format_signal(sigs[0])
    assert telegram_bot.send_signals([]) == []
