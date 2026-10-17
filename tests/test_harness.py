"""Proves the isolation guards in conftest.py work before anything else is trusted."""
import os
import socket
import sqlite3

import pytest
import requests
import yfinance

import conftest
from algotrade import config, data, markets, telegram_bot


def test_telegram_token_blank_and_dotenv_disabled():
    assert config.TELEGRAM_BOT_TOKEN == ""
    assert config.TELEGRAM_CHAT_ID == ""
    assert os.environ["TELEGRAM_BOT_TOKEN"] == ""
    import dotenv
    assert dotenv.load_dotenv() is False  # no-op


def test_markets_hold_code_defaults():
    assert markets.MARKETS["IN"].capital == 100000.0
    assert markets.MARKETS["US"].capital == 10000.0
    assert markets.MARKETS["IN"].min_price == 50.0 and markets.MARKETS["IN"].min_turnover == 5e7
    assert markets.MARKETS["US"].min_price == 5.0 and markets.MARKETS["US"].min_turnover == 2e7


def test_socket_connect_blocked():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(conftest.NetworkBlocked):
            s.connect(("127.0.0.1", 8050))
    finally:
        s.close()
    with pytest.raises(conftest.NetworkBlocked):
        socket.create_connection(("example.com", 443))


def test_requests_blocked():
    with pytest.raises(conftest.NetworkBlocked):
        requests.get("https://archives.nseindia.com/")
    with pytest.raises(conftest.NetworkBlocked):
        requests.post("https://api.telegram.org/x")


def test_yfinance_download_patched():
    with pytest.raises(conftest.NetworkBlocked):
        yfinance.download(["RELIANCE.NS"])
    assert data.yf.download is yfinance.download
    # data.fetch_ohlcv swallows batch errors -> empty result, no network
    assert data.fetch_ohlcv(["RELIANCE"]) == {}


def test_curl_cffi_blocked():
    cc = pytest.importorskip("curl_cffi.requests")
    with pytest.raises(conftest.NetworkBlocked):
        cc.Session().request("GET", "https://query1.finance.yahoo.com/")


def test_telegram_send_fails_test_unless_faked():
    with pytest.raises(pytest.fail.Exception):
        telegram_bot.send_message("hi")


def test_real_db_guard_trips():
    n = len(conftest.DB_VIOLATIONS)
    with pytest.raises(RuntimeError, match="REAL data folder"):
        sqlite3.connect(conftest.REAL_DB)
    with pytest.raises(RuntimeError):
        sqlite3.connect(str(conftest.REAL_DATA / "other.db"))
    # this test's own expected violations are removed so the session does not fail
    assert len(conftest.DB_VIOLATIONS) == n + 2
    del conftest.DB_VIOLATIONS[n:]


def test_state_db_and_data_dir_under_tmp(tmp_path):
    assert config.STATE_DB == tmp_path / "signals.db"
    assert config.DATA_DIR == tmp_path
    assert conftest.REAL_DATA not in config.STATE_DB.resolve().parents
    con = sqlite3.connect(config.STATE_DB)  # allowed
    con.close()


def test_config_pinned():
    assert config.MIN_SCORE == 2 and config.ALLOW_SHORTS is False
    assert config.STRATEGIES == [] and config.FILTERS == [] and config.TIMEFRAME == "1d"
