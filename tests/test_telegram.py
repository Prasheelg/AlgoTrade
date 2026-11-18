"""P9: telegram_bot.format_signal snapshots, send_message retry logic, send_signals."""
from types import SimpleNamespace

import pytest
import requests

from algotrade import config, telegram_bot
from conftest import ORIGINAL_SEND, FakeResponse, Recorder, make_signal

FOOTER = "\n\n<a href=\"https://www.tradingview.com/chart/?symbol={sym}\">📈 Chart</a>  ·  " \
         "<i>Not investment advice</i>"


def test_format_in_buy_limit():
    assert telegram_bot.format_signal(make_signal()) == (
        "🟢 <b>BUY · RELIANCE</b>  ⭐⭐\n"
        "<i>Daily · Limit order, buy the pullback</i>\n"
        "\n"
        "<blockquote>▶️ Entry  <b>₹99.00</b>\n"
        "🛑 Stop  <b>₹96.00</b>  <i>-3.03%</i>\n"
        "🎯 Target  <b>₹103.50</b>  <i>+4.55%</i></blockquote>\n"
        "\n"
        "📦 Qty <b>333</b>  ·  Risk <b>₹999</b>  ·  Reward <b>1.5R</b>\n"
        "⏳ Valid 3 sessions  ·  Now ₹100.00\n"
        "\n"
        "💡 <b>Why</b>  EMA 9/21 Cross, RSI Pullback\n"
        "<i>RSI 55  ·  ADX 25  ·  Volume 2× avg</i>" + FOOTER.format(sym="NSE:RELIANCE"))


def test_format_us_stop_intraday():
    s = make_signal(symbol="BRK.B", market="US", entry_type="stop", timeframe="15m", entry=1234.5,
                    stop_loss=1200.0, targets=[1286.25, 1338.0], qty=2, score=3, ref_price=1230.0,
                    reasons=["EMA 9/21 Cross", "20-bar Breakout", "MACD Trend Cross"])
    assert telegram_bot.format_signal(s) == (
        "🇺🇸 🟢 <b>BUY · BRK.B</b>  ⭐⭐⭐\n"
        "<i>15 min · Stop order, buy the breakout</i>\n"
        "\n"
        "<blockquote>▶️ Entry  <b>$1,234.50</b>\n"
        "🛑 Stop  <b>$1,200.00</b>  <i>-2.79%</i>\n"
        "🎯 Target  <b>$1,286.25</b>  <i>+4.19%</i></blockquote>\n"
        "\n"
        "📦 Qty <b>2</b>  ·  Risk <b>$69</b>  ·  Reward <b>1.5R</b>\n"
        "⏳ Valid 3 bars, today only  ·  Now $1,230.00\n"
        "\n"
        "💡 <b>Why</b>  EMA 9/21 Cross, 20-bar Breakout, MACD Trend Cross\n"
        "<i>RSI 55  ·  ADX 25  ·  Volume 2× avg</i>" + FOOTER.format(sym="BRK.B"))


def test_format_in_sell_has_short_note():
    s = make_signal(side="SELL", entry=101.0, stop_loss=104.0, targets=[96.5, 92.0])
    text = telegram_bot.format_signal(s)
    assert text.startswith("🔴 <b>SELL · RELIANCE</b>")
    assert "<i>Daily · Limit order, sell the bounce</i>" in text
    assert "Now ₹100.00\n⚠️ Short: intraday cash or F&amp;O only\n\n💡" in text
    us = telegram_bot.format_signal(make_signal(side="SELL", market="US", entry_type="stop"))
    assert "Short:" not in us and "Stop order, sell the breakdown" in us


def test_format_market_entry_has_no_order_line():
    text = telegram_bot.format_signal(make_signal(entry_type="market"))
    assert "⏳" not in text and "<i>Daily · Market order</i>" in text


def test_format_escapes_html():
    text = telegram_bot.format_signal(make_signal(symbol="M&M", reasons=["A<B"], score=1))
    assert "<b>BUY · M&amp;M</b>  ⭐\n" in text
    assert "symbol=NSE:M&amp;M\"" in text
    assert "💡 <b>Why</b>  A&lt;B" in text


def test_format_single_target_and_live_config(monkeypatch):
    monkeypatch.setattr(config, "TARGET_R_MULTIPLES", [2.0, 3.0])
    monkeypatch.setattr(config, "ENTRY_VALID_BARS", 5)
    text = telegram_bot.format_signal(make_signal(targets=[103.5]))
    assert "Target 2" not in text and "Reward <b>2R</b>" in text and "Valid 5 sessions" in text


@pytest.mark.parametrize("v,indian,out", [(100000, True, "1,00,000.00"), (12345678.9, True, "1,23,45,678.90"),
                                          (999, True, "999.00"), (100000, False, "100,000.00"),
                                          (-1500.5, True, "-1,500.50")])
def test_group_digits(v, indian, out):
    assert telegram_bot.group_digits(v, 2, indian) == out


def test_format_update_filled_and_time_exit():
    sig = {"market": "IN", "side": "SELL", "symbol": "TCS", "timeframe": "15m", "entry": 3000.0,
           "stop_loss": 3030.0, "target1": 2955.0}
    filled = telegram_bot.format_update(sig, {"status": "ACTIVE", "fill_price": 3000.0, "exit_price": None}, 2999.0)
    assert filled == ("✅ <b>Entry filled · TCS</b>\n<i>SELL · 15 min · trade is live</i>\n\n"
                      "<blockquote>▶️ Filled  <b>₹3,000.00</b>\n🛑 Stop  <b>₹3,030.00</b>  <i>+1.00%</i>\n"
                      "🎯 Target  <b>₹2,955.00</b>  <i>-1.50%</i></blockquote>")
    out = telegram_bot.format_update(sig, {"status": "EXPIRED", "fill_price": 3000.0, "exit_price": 2985.0}, 2985.0)
    assert out.startswith("⌛ <b>Time exit · TCS</b>") and out.endswith("🟩 Result  <b>+0.50%</b>  ·  +0.50R")


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
