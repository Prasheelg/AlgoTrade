"""P8: main.run_loop scheduling (scripted clock, sentinel sleep) and the CLI."""
import sys
import time as real_time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import main
from algotrade import backtest, config, costs, markets, telegram_bot, tracker
from conftest import Recorder, StopLoop, make_signal, sleep_after

STARTUP = ("🤖 <b>Scanner started</b>\n🇮🇳 India: NIFTY100 · 09:15–15:30 IST\n   default strategies · min score 2\n"
           "🇺🇸 US: SP100 · 09:30–16:00 ET\n   default strategies · min score 2\nTF: 1d · every 15 min")


@pytest.fixture
def loop(monkeypatch):
    """Install fakes; returns (events, set_clock, run)."""
    events = []
    monkeypatch.setattr(telegram_bot, "send_message", lambda text: events.append(("msg", text)) or True)
    monkeypatch.setattr(tracker, "update_all", lambda market=None, **k: events.append(("update", market)))
    monkeypatch.setattr(main, "run_scan", lambda code, dry_run=False: events.append(("scan", code)))
    monkeypatch.setattr(main, "housekeeping", lambda: None)   # backup / summary run off the real clock
    clocks = {}
    monkeypatch.setattr(markets.Market, "now", lambda self: next(clocks[self.code]))
    sleeps = []

    def set_clock(code, *times):
        tz = ZoneInfo(markets.MARKETS[code].tz)
        clocks[code] = iter([datetime(*t, tzinfo=tz) for t in times])

    def run(codes, iterations):
        monkeypatch.setattr(main, "time", SimpleNamespace(sleep=sleep_after(iterations, sleeps)))
        with pytest.raises(StopLoop):
            main.run_loop(codes)
        return sleeps

    return events, set_clock, run


def test_global_time_module_untouched(loop):
    events, set_clock, run = loop
    set_clock("IN", (2024, 1, 6, 10, 0))
    run(["IN"], 1)
    # only main.time was replaced; the shared `time` module (used by telegram_bot/web) is intact
    assert main.time is not real_time and telegram_bot.time is real_time
    assert type(real_time.sleep).__name__ == "builtin_function_or_method"


def test_startup_message_and_open_market(loop):
    events, set_clock, run = loop
    set_clock("IN", (2024, 1, 1, 10, 0), (2024, 1, 1, 10, 15))
    set_clock("US", (2024, 1, 1, 8, 0), (2024, 1, 1, 8, 15))   # before the US open: nothing
    sleeps = run(["IN", "US"], 2)
    assert events == [("msg", STARTUP), ("update", "IN"), ("scan", "IN"), ("update", "IN"), ("scan", "IN")]
    assert sleeps == [900, 900]   # one shared sleep per loop, SCAN_INTERVAL_MIN * 60


def test_startup_message_single_market_and_config(loop, monkeypatch):
    events, set_clock, run = loop
    monkeypatch.setattr(config, "TIMEFRAME", "15m")
    monkeypatch.setattr(config, "SCAN_INTERVAL_MIN", 5)
    monkeypatch.setattr(config, "US_UNIVERSE", "AAPL,MSFT")
    set_clock("US", (2024, 1, 6, 12, 0))
    sleeps = run(["US"], 1)
    assert events == [("msg", "🤖 <b>Scanner started</b>\n🇺🇸 US: AAPL,MSFT · 09:30–16:00 ET\n"
                              "   default strategies · min score 2\nTF: 15m · every 5 min")]
    assert sleeps == [300]


def test_eod_scan_once_per_market_per_date(loop):
    """Quirk 11: one EOD scan per market per date, header message first; uses run_scan(code) (config TF)."""
    events, set_clock, run = loop
    set_clock("IN", (2024, 1, 1, 15, 45), (2024, 1, 1, 16, 0), (2024, 1, 1, 23, 0), (2024, 1, 2, 15, 31))
    run(["IN"], 4)
    hdr = ("msg", "🇮🇳 📊 <b>India end-of-day scan</b> — setups for next session:")
    start_in = "🤖 <b>Scanner started</b>\n🇮🇳 India: NIFTY100 · 09:15–15:30 IST\n   default strategies · min score 2\nTF: 1d · every 15 min"
    assert events == [("msg", start_in), hdr, ("scan", "IN"), hdr, ("scan", "IN")]


def test_eod_scan_on_startup_after_close_and_restart_resends(loop):
    """Quirk 11: the EOD flag is in memory, so a restart on the same evening re-sends the EOD header."""
    events, set_clock, run = loop
    set_clock("US", (2024, 1, 1, 17, 0))
    run(["US"], 1)
    set_clock("US", (2024, 1, 1, 17, 30))
    run(["US"], 1)
    eod = [e for e in events if e[0] == "msg" and "end-of-day" in e[1]]
    assert len(eod) == 2 and eod[0][1] == "🇺🇸 📊 <b>US end-of-day scan</b> — setups for next session:"
    assert [e for e in events if e[0] == "scan"] == [("scan", "US"), ("scan", "US")]


def test_tracking_only_while_open(loop):
    """Quirk 11: tracker.update_all runs only during the session (never after close or on weekends)."""
    events, set_clock, run = loop
    set_clock("IN", (2024, 1, 6, 10, 0), (2024, 1, 7, 18, 0), (2024, 1, 1, 8, 0))
    run(["IN"], 3)
    assert events == [("msg", events[0][1])]


def test_one_market_failure_does_not_stop_the_other(loop, monkeypatch, caplog):
    events, set_clock, run = loop

    def update(market=None, **k):
        events.append(("update", market))
        if market == "IN":
            raise RuntimeError("IN down")
    monkeypatch.setattr(tracker, "update_all", update)
    set_clock("IN", (2024, 1, 1, 10, 0))
    set_clock("US", (2024, 1, 1, 10, 0))
    run(["IN", "US"], 1)
    assert events[1:] == [("update", "IN"), ("update", "US"), ("scan", "US")]
    assert "India scan failed" in caplog.text


EOD_IN = ("msg", "🇮🇳 📊 <b>India end-of-day scan</b> — setups for next session:")


def flaky_scan(events, failures):
    """run_scan fake that raises on its first `failures` calls and records every attempt."""
    calls = []

    def _scan(code, dry_run=False):
        calls.append(code)
        events.append(("scan", code))
        if len(calls) <= failures:
            raise RuntimeError(f"fail {len(calls)}")
    return _scan


def test_eod_failure_retried_without_resending_header(loop, monkeypatch, caplog):
    """B9 fixed: a failed EOD scan is retried next loop, but the header is sent only once."""
    events, set_clock, run = loop
    monkeypatch.setattr(main, "run_scan", flaky_scan(events, failures=1))
    set_clock("IN", (2024, 1, 1, 16, 0), (2024, 1, 1, 16, 15), (2024, 1, 1, 16, 30))
    run(["IN"], 3)
    assert events[1:] == [EOD_IN, ("scan", "IN"), ("scan", "IN")]  # retried once, then done for the date
    assert "India scan failed" in caplog.text


def test_eod_failing_twice_then_succeeding_sends_one_header(loop, monkeypatch, caplog):
    events, set_clock, run = loop
    monkeypatch.setattr(main, "run_scan", flaky_scan(events, failures=2))
    set_clock("IN", (2024, 1, 1, 16, 0), (2024, 1, 1, 16, 15), (2024, 1, 1, 16, 30), (2024, 1, 1, 16, 45),
              (2024, 1, 1, 23, 0))
    run(["IN"], 5)
    assert events[1:] == [EOD_IN, ("scan", "IN"), ("scan", "IN"), ("scan", "IN")]
    assert caplog.text.count("India scan failed") == 2


def test_eod_persistent_failure_keeps_retrying_with_one_header(loop, monkeypatch):
    events, set_clock, run = loop
    monkeypatch.setattr(main, "run_scan", flaky_scan(events, failures=99))
    set_clock("IN", *[(2024, 1, 1, 16, 15 * k) for k in range(4)])
    run(["IN"], 4)
    assert events[1:] == [EOD_IN] + [("scan", "IN")] * 4


def test_eod_header_resent_on_next_date_after_failure(loop, monkeypatch):
    """The header flag is per exchange date: a new date gets a new header even if the last scan failed."""
    events, set_clock, run = loop
    monkeypatch.setattr(main, "run_scan", flaky_scan(events, failures=2))
    set_clock("IN", (2024, 1, 1, 16, 0), (2024, 1, 2, 9, 0), (2024, 1, 2, 16, 0), (2024, 1, 2, 16, 15))
    run(["IN"], 4)
    assert events[1:] == [EOD_IN, ("scan", "IN"), EOD_IN, ("scan", "IN"), ("scan", "IN")]


def test_eod_header_flags_are_per_market(loop, monkeypatch):
    events, set_clock, run = loop

    def scan(code, dry_run=False):
        events.append(("scan", code))
        if code == "IN":
            raise RuntimeError("IN down")
    monkeypatch.setattr(main, "run_scan", scan)
    set_clock("IN", (2024, 1, 1, 16, 0), (2024, 1, 1, 16, 15))
    set_clock("US", (2024, 1, 1, 17, 0), (2024, 1, 1, 17, 15))
    run(["IN", "US"], 2)
    eod_us = ("msg", "🇺🇸 📊 <b>US end-of-day scan</b> — setups for next session:")
    assert events[1:] == [EOD_IN, ("scan", "IN"), eod_us, ("scan", "US"), ("scan", "IN")]


# ---- run_scan ---------------------------------------------------------------------------------

def test_run_scan_modes(monkeypatch, capsys):
    monkeypatch.setattr(main, "scan", Recorder([make_signal()]))
    saa = Recorder({})
    monkeypatch.setattr(main, "scan_and_alert", saa)
    main.run_scan("IN", dry_run=True)
    out = capsys.readouterr().out
    assert out == telegram_bot.format_signal(make_signal()) + " \n" + "-" * 40 + "\n"
    assert main.scan.calls == [((), {"market": "IN"})]
    main.run_scan("US")
    assert saa.calls == [((), {"market": "US"})]


def test_universe_of(monkeypatch):
    monkeypatch.setattr(config, "UNIVERSE", "NIFTY50")
    assert main.universe_of("IN") == "NIFTY50" and main.universe_of("US") == "SP100"
    assert main.universe_of("XX") == "NIFTY50"


# ---- CLI ------------------------------------------------------------------------------------

@pytest.fixture
def cli(monkeypatch):
    monkeypatch.setattr(config, "UNIVERSE", config.UNIVERSE)       # main() mutates these
    monkeypatch.setattr(config, "US_UNIVERSE", config.US_UNIVERSE)
    monkeypatch.setattr(main, "run_loop", lambda codes: pytest.fail("run_loop not expected"))

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["main.py", *args])
        return main.main()
    return run


def test_cli_dry_run_all_markets(cli, monkeypatch, capsys):
    monkeypatch.setattr(main, "scan", lambda market: [make_signal(market=market, symbol=f"S{market}")])
    cli("--dry-run")
    out = capsys.readouterr().out
    assert out.startswith("\n===== 🇮🇳 India =====\n")
    assert "\n===== 🇺🇸 US =====\n" in out and "SIN" in out and "SUS" in out


def test_cli_dry_run_single_market_no_header(cli, monkeypatch, capsys):
    monkeypatch.setattr(main, "scan", lambda market: [])
    cli("--dry-run", "--market", "us")
    assert capsys.readouterr().out == ""


def test_cli_once_uses_scan_and_alert(cli, monkeypatch):
    saa = Recorder({})
    monkeypatch.setattr(main, "scan_and_alert", saa)
    cli("--once", "--market", "IN")
    assert saa.calls == [((), {"market": "IN"})]


IN_SCAN = {"strategies": ["RSI Pullback", "RSI(2) Reversion"], "min_score": 1, "filters": ["trend", "rs"]}


def test_cli_once_uses_per_market_settings(cli, monkeypatch):
    monkeypatch.setattr(config, "MARKET_SCAN", {"IN": IN_SCAN, "US": {}})
    saa = Recorder({})
    monkeypatch.setattr(main, "scan_and_alert", saa)
    cli("--once")
    assert saa.calls == [((), {"market": "IN", **IN_SCAN}), ((), {"market": "US"})]


def test_cli_dry_run_uses_per_market_settings(cli, monkeypatch):
    monkeypatch.setattr(config, "MARKET_SCAN", {"IN": IN_SCAN, "US": {}})
    calls = []
    monkeypatch.setattr(main, "scan", lambda **kw: calls.append(kw) or [])
    cli("--dry-run", "--market", "IN")
    assert calls == [{"market": "IN", **IN_SCAN}]


def test_scan_settings_line(monkeypatch):
    monkeypatch.setattr(config, "MARKET_SCAN", {"IN": IN_SCAN, "US": {"min_score": 3}})
    assert main.scan_settings("IN") == "RSI Pullback, RSI(2) Reversion · min score 1 · filters trend, rs"
    assert main.scan_settings("US") == "default strategies · min score 3"
    monkeypatch.setattr(config, "FILTERS", ["trend"])
    assert main.scan_settings("US") == "default strategies · min score 3 · filters trend"


def test_market_scan_from_env(monkeypatch):
    monkeypatch.setenv("STRATEGIES_IN", "RSI Pullback, RSI(2) Reversion  # comment")
    monkeypatch.setenv("MIN_SCORE_IN", "1")
    monkeypatch.setenv("FILTERS_IN", "Trend,RS")
    assert config.market_scan("IN") == IN_SCAN
    assert config.market_scan("US") == {}
    for k in ("STRATEGIES_US", "MIN_SCORE_US", "FILTERS_US"):   # empty values, as in .env.example
        monkeypatch.setenv(k, "")
    assert config.market_scan("US") == {}
    assert config.MARKET_SCAN == {"IN": {}, "US": {}}  # tests never see the real .env


@pytest.mark.parametrize("ok,text", [(True, "Sent\n"),
                                     (False, "Failed — check token/chat id and that the bot is in the group\n")])
def test_cli_test_telegram(cli, monkeypatch, capsys, ok, text):
    rec = Recorder(ok)
    monkeypatch.setattr(telegram_bot, "send_message", rec)
    cli("--test-telegram")
    assert rec.calls == [(("✅ AlgoTrade bot is connected.",), {})]
    assert capsys.readouterr().out == text


def test_cli_symbols_needs_single_market(cli, capsys):
    with pytest.raises(SystemExit) as e:
        cli("--symbols", "RELIANCE,TCS", "--once")
    assert e.value.code == 2
    assert "--symbols needs --market IN or --market US" in capsys.readouterr().err


def test_cli_symbols_sets_universe(cli, monkeypatch):
    monkeypatch.setattr(main, "scan_and_alert", Recorder({}))
    cli("--symbols", "AAPL", "--market", "US", "--once")
    assert config.US_UNIVERSE == "AAPL" and config.UNIVERSE == "NIFTY100"
    cli("--symbols", "TCS", "--market", "IN", "--once")
    assert config.UNIVERSE == "TCS"


def test_cli_bad_market_exits(cli):
    with pytest.raises(SystemExit) as e:
        cli("--market", "JP")
    assert e.value.code == 2


def test_cli_default_runs_loop(cli, monkeypatch):
    rec = Recorder(None)
    monkeypatch.setattr(main, "run_loop", rec)
    cli()
    assert rec.calls == [((["IN", "US"],), {})]
    cli("--market", "US")
    assert rec.calls[-1] == ((["US"],), {})


def test_cli_ui_calls_serve(cli, monkeypatch):
    from algotrade import web
    rec = Recorder(None)
    monkeypatch.setattr(web, "serve", rec)
    cli("--ui", "--port", "9999")
    assert rec.calls == [((), {"host": "127.0.0.1", "port": 9999})]  # this PC only by default
    cli("--ui", "--host", "0.0.0.0")
    assert rec.calls[-1] == ((), {"host": "0.0.0.0", "port": 8050})


def test_cli_backtest_writes_csv_to_data_dir(cli, monkeypatch, tmp_path, capsys):
    uni = Recorder(lambda spec, market: [f"{market}1", f"{market}2"])
    monkeypatch.setattr(main, "get_universe", uni)
    trades = pd.DataFrame({"symbol": ["A"], "side": ["BUY"], "R": [1.5]})
    run = Recorder(lambda syms, tf, period, market, costs: trades if market == "IN" else pd.DataFrame())
    monkeypatch.setattr(backtest, "run", run)
    cli("--backtest")
    assert uni.calls == [((None,), {"market": "IN"}), ((None,), {"market": "US"})]
    assert run.calls[0] == ((["IN1", "IN2"], "1d"), {"period": "5y", "market": "IN",
                                                     "costs": costs.model_for("IN", "1d")})
    assert (tmp_path / "backtest_trades.csv").read_text().splitlines() == ["symbol,side,R", "A,BUY,1.5"]
    assert not (tmp_path / "backtest_trades_US.csv").exists()  # empty trades: no file
    out = capsys.readouterr().out
    assert "=== India ===" in out and "No trades generated." in out
    assert f"Trades saved to {tmp_path / 'backtest_trades.csv'}" in out


def test_cli_backtest_intraday_period_and_us_file(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TIMEFRAME", "15m")
    monkeypatch.setattr(main, "get_universe", Recorder(["X"]))
    run = Recorder(pd.DataFrame({"symbol": ["X"], "side": ["SELL"], "R": [0.5]}))
    monkeypatch.setattr(backtest, "run", run)
    cli("--backtest", "--market", "US", "--symbols", "X")
    assert run.calls[0] == ((["X"], "15m"), {"period": "60d", "market": "US",
                                             "costs": costs.model_for("US", "15m")})
    assert main.get_universe.calls == [(("X",), {"market": "US"})]
    assert (tmp_path / "backtest_trades_US.csv").exists()
