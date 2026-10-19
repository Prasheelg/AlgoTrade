"""Test harness: isolation guards + synthetic data builders.

Order matters. Everything above the first `algotrade` import neutralises the environment so that
the real .env (which holds a REAL Telegram token) is never read and markets.MARKETS is built from the
code defaults. See plan/seams-and-dependencies.md sections 6.1-6.5.
"""
# --------------------------------------------------------------------------------------------
# 6.1 Env neutralisation -- MUST run before any `algotrade` import
# --------------------------------------------------------------------------------------------
import os

import dotenv

dotenv.load_dotenv = lambda *a, **k: False  # .env is never read
for _k in ("UNIVERSE", "SCAN_INTERVAL_MIN", "SCAN_TIMEFRAMES", "TIMEFRAME", "MIN_SCORE", "CAPITAL", "RISK_PER_TRADE_PCT",
           "ALLOW_SHORTS", "STRATEGIES", "FILTERS", "US_UNIVERSE", "US_CAPITAL",
           "STRATEGIES_IN", "MIN_SCORE_IN", "FILTERS_IN", "STRATEGIES_US", "MIN_SCORE_US", "FILTERS_US",
           "BROKERAGE", "DP_CHARGE", "SLIPPAGE_BPS", "US_BROKERAGE", "US_SLIPPAGE_BPS",
           "MAX_ORDER_VALUE"):
    os.environ.pop(_k, None)
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["KOTAK_CONSUMER_KEY"] = ""  # real-time quotes off: tests never reach Kotak
for _k in ("KOTAK_MOBILE", "KOTAK_UCC", "KOTAK_MPIN", "KOTAK_TOTP_SECRET"):  # no trading login either
    os.environ[_k] = ""

import hashlib  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import sqlite3  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import pytest  # noqa: E402
import requests  # noqa: E402
import yfinance  # noqa: E402

from algotrade import config  # noqa: E402  (first algotrade import)

assert config.TELEGRAM_BOT_TOKEN == "", "Telegram token leaked into config"
assert config.TELEGRAM_CHAT_ID == ""
assert config.KOTAK_CONSUMER_KEY == "", "Kotak key leaked into config"
assert config.CAPITAL == 100000.0 and config.US_CAPITAL == 10000.0

REAL_DB = Path(config.STATE_DB).resolve()
REAL_DATA = Path(config.DATA_DIR).resolve()
_SESSION_TMP = Path(tempfile.mkdtemp(prefix="algotrade-tests-"))
config.STATE_DB = _SESSION_TMP / "signals.db"   # until the per-test fixture takes over
config.DATA_DIR = _SESSION_TMP

from algotrade import markets, scanner, state, telegram_bot, tracker  # noqa: E402

assert markets.MARKETS["IN"].capital == 100000.0 and markets.MARKETS["US"].capital == 10000.0

ORIGINAL_SEND = telegram_bot.send_message
ORIGINAL_SEND_SIGNALS = telegram_bot.send_signals

# --------------------------------------------------------------------------------------------
# 6.3 DB isolation guard (process-wide, installed at import)
# --------------------------------------------------------------------------------------------
DB_VIOLATIONS: list[str] = []
_real_connect = sqlite3.connect


def _is_real_data_path(database) -> bool:
    try:
        raw = os.fspath(database)
    except TypeError:
        return False
    if isinstance(raw, bytes):
        raw = raw.decode(errors="ignore")
    if raw in ("", ":memory:") or raw.startswith("file:"):
        return raw.startswith("file:") and str(REAL_DATA).lower() in raw.lower()
    try:
        p = Path(raw).resolve()
    except (OSError, ValueError):
        return False
    return p == REAL_DB or p == REAL_DATA or REAL_DATA in p.parents


def _guarded_connect(database, *args, **kwargs):
    if _is_real_data_path(database):
        DB_VIOLATIONS.append(str(database))
        raise RuntimeError(f"sqlite connect to the REAL data folder blocked in tests: {database}")
    return _real_connect(database, *args, **kwargs)


sqlite3.connect = _guarded_connect

# Files the live dashboard never writes: their bytes must not change during the run.
HASHED_FILES = ["backtest_trades.csv", "backtest_trades_US.csv", "signals_backup_2026-10-13.db"]


def _sha256(p: Path):
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_SESSION_TMP, ignore_errors=True)  # session temp dir from mkdtemp above
    if DB_VIOLATIONS:
        print(f"\nREAL DB GUARD VIOLATIONS: {DB_VIOLATIONS}")
        session.exitstatus = 1


@pytest.fixture(scope="session", autouse=True)
def _data_files_untouched():
    before = {n: _sha256(REAL_DATA / n) for n in HASHED_FILES}
    yield
    after = {n: _sha256(REAL_DATA / n) for n in HASHED_FILES}
    assert before == after, "files under data/ changed during the test run"


# --------------------------------------------------------------------------------------------
# 6.2 Network kill-switch + 6.3 per-test DB redirect + 6.4 config pinning (autouse)
# --------------------------------------------------------------------------------------------
class NetworkBlocked(RuntimeError):
    pass


def _no_network(*a, **k):
    raise NetworkBlocked("network blocked in tests")


def _unexpected_telegram(*a, **k):
    pytest.fail(f"unexpected Telegram send: {a!r} {k!r}")


@pytest.fixture(autouse=True)
def _isolation(monkeypatch, tmp_path):
    # network
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    monkeypatch.setattr(yfinance, "download", _no_network)
    monkeypatch.setattr(requests.Session, "request", _no_network)
    try:
        import curl_cffi.requests as _cc
        monkeypatch.setattr(_cc.Session, "request", _no_network)
    except Exception:  # pragma: no cover - optional dependency layout
        pass
    monkeypatch.setattr(telegram_bot, "send_message", _unexpected_telegram)
    # DB / FS
    monkeypatch.setattr(config, "STATE_DB", tmp_path / "signals.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    # config pinning
    for k, v in dict(MIN_SCORE=2, ALLOW_SHORTS=False, STRATEGIES=[], FILTERS=[], TIMEFRAME="1d",
                     RISK_PER_TRADE_PCT=1.0, SCAN_INTERVAL_MIN=15, SCAN_TIMEFRAMES=["1d"], TELEGRAM_BOT_TOKEN="",
                     TELEGRAM_CHAT_ID="", KOTAK_CONSUMER_KEY="", UNIVERSE="NIFTY100", US_UNIVERSE="SP100",
                     MARKET_SCAN={"IN": {}, "US": {}}, KOTAK_MOBILE="", KOTAK_UCC="", KOTAK_MPIN="",
                     KOTAK_TOTP_SECRET="").items():
        monkeypatch.setattr(config, k, v)
    from algotrade import broker
    monkeypatch.setattr(broker, "_session", {"client": None, "day": None, "name": None})
    assert markets.MARKETS["IN"].capital == 100000.0
    assert markets.MARKETS["US"].capital == 10000.0
    yield


# --------------------------------------------------------------------------------------------
# 6.5 Builders and fakes
# --------------------------------------------------------------------------------------------
EVAL_DEFAULTS = dict(Open=100.0, Close=100.0, High=101.0, Low=99.0, ATR=2.0, TURNOVER20=1e9, RSI=55.0,
                     ADX=25.0, Volume=1000.0, VOL_AVG20=500.0, IDX_CLOSE=float("nan"),
                     IDX_EMA200=float("nan"), RS=float("nan"))


def eval_frame(rows: int = 201, start="2024-01-01", freq="B", **last) -> pd.DataFrame:
    """P1 hand frame: constant rows, the last row overridable via keyword args."""
    idx = pd.date_range(start, periods=rows, freq=freq)
    df = pd.DataFrame({k: [v] * rows for k, v in EVAL_DEFAULTS.items()}, index=idx, dtype=float)
    for k, v in last.items():
        df.loc[df.index[-1], k] = v
    return df


def bars(rows) -> pd.DataFrame:
    """[(ts, O, H, L, C), ...] -> tz-naive OHLC frame."""
    idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows])
    return pd.DataFrame([r[1:] for r in rows], index=idx, columns=["Open", "High", "Low", "Close"], dtype=float)


def sig_row(**kw) -> dict:
    row = dict(key="X|BUY|2024-01-01", symbol="X", side="BUY", entry_type="limit", entry=99.0,
               stop_loss=96.0, target1=103.5, target2=108.0, bar_date="2024-01-01",
               bar_time="2024-01-01T00:00:00", timeframe="1d", market="IN", status="PENDING", telegram=1)
    row.update(kw)
    return row


def make_signal(**kw) -> scanner.Signal:
    d = dict(symbol="RELIANCE", side="BUY", entry=99.0, stop_loss=96.0, targets=[103.5, 108.0], qty=333,
             score=2, reasons=["EMA 9/21 Cross", "RSI Pullback"], rsi=55.0, adx=25.0, vol_ratio=2.0,
             bar_time=pd.Timestamp("2024-01-01"), timeframe="1d", market="IN", entry_type="limit",
             ref_price=100.0)
    d.update(kw)
    return scanner.Signal(**d)


class FakeFetch:
    """Stand-in for data.fetch_ohlcv; records calls."""

    def __init__(self, frames: dict):
        self.frames = frames
        self.calls = []

    def __call__(self, symbols, timeframe="1d", period=None, market="IN", **kw):
        self.calls.append(dict(symbols=list(symbols), timeframe=timeframe, period=period, market=market))
        return {s: self.frames[s] for s in symbols if s in self.frames}


class Recorder:
    """Callable that records arguments and returns a scripted value (or calls a function)."""

    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def __call__(self, *a, **k):
        self.calls.append((a, k))
        return self.result(*a, **k) if callable(self.result) else self.result


class FakeResponse:
    def __init__(self, status_code=200, json=None, text=""):
        self.status_code = status_code
        self._json = json or {}
        self.text = text
        self.ok = status_code < 400

    def json(self):
        return self._json

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"{self.status_code}")


class StopLoop(Exception):
    pass


def sleep_after(n: int, log: list | None = None):
    """A fake sleep that raises StopLoop on its n-th call."""
    calls = log if log is not None else []

    def _sleep(sec):
        calls.append(sec)
        if len(calls) >= n:
            raise StopLoop()
    return _sleep


# ---- synthetic OHLCV ------------------------------------------------------------------------

def make_ohlcv(n=600, seed=0, drift=0.0, vol=0.012, start="2022-01-03", freq="B", price=500.0,
               volume=1e6) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    close = price * np.exp(np.cumsum(r))
    open_ = np.concatenate([[price], close[:-1]])
    wick = np.abs(rng.normal(0, vol / 2, (2, n)))
    high = np.maximum(open_, close) * (1 + wick[0])
    low = np.minimum(open_, close) * (1 - wick[1])
    v = volume * np.exp(rng.normal(0, 0.3, n))
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": v}, index=idx)


# Demand-zone template (O, H, L, C) in ATR units relative to the base level B.
_LEG_IN_UP = (-2.5, 0.1, -2.6, 0.0)
_LEG_IN_DOWN = (2.5, 2.6, -0.1, 0.0)
_DEMAND_TAIL = [
    (0.0, 0.4, -0.4, 0.1), (0.1, 0.4, -0.4, 0.0),          # base: 2 boring bars
    (0.0, 3.1, -0.1, 3.0),                                  # explosive leg-out
    (3.0, 3.4, 2.9, 3.2), (3.2, 3.5, 2.6, 2.7), (2.7, 2.8, 1.9, 2.0),
    (2.0, 2.1, 1.2, 1.3), (1.3, 1.4, 0.6, 0.8),             # drift back toward the zone
    (0.8, 1.0, 0.0, 0.9),                                   # first retest: holds, closes upper half
    (0.9, 1.5, 0.8, 1.4), (1.4, 2.0, 1.3, 1.9), (1.9, 2.5, 1.8, 2.4),
]
ZONE_PATTERN_LEN = 1 + len(_DEMAND_TAIL)
ZONE_RETEST_OFFSET = 9   # retest bar = at + 9


def inject_zone(df: pd.DataFrame, at: int, kind: str = "RBR") -> pd.DataFrame:
    """Overwrite bars at..at+13 with a base pattern + held first retest, rebasing later bars."""
    df = df.copy()
    demand = kind in ("RBR", "DBR")
    leg_in_up = kind in ("RBR", "RBD")
    # Supply patterns are the mirror image of the demand pattern with the opposite leg-in.
    leg = (_LEG_IN_UP if leg_in_up else _LEG_IN_DOWN) if demand else (_LEG_IN_DOWN if leg_in_up else _LEG_IN_UP)
    tmpl = [leg] + _DEMAND_TAIL
    if not demand:
        tmpl = [(-o, -l, -h, -c) for o, h, l, c in tmpl]
    prev_close = float(df["Close"].iloc[at - 1])
    a = float((df["High"] - df["Low"]).iloc[max(0, at - 20):at].mean())
    base = prev_close - tmpl[0][0] * a
    cols = ["Open", "High", "Low", "Close"]
    old_last = float(df["Close"].iloc[at + len(tmpl) - 1])
    for k, (o, h, l, c) in enumerate(tmpl):
        df.iloc[at + k, [df.columns.get_loc(x) for x in cols]] = [base + o * a, base + h * a, base + l * a, base + c * a]
    ratio = (base + tmpl[-1][3] * a) / old_last
    rest = slice(at + len(tmpl), len(df))
    df.iloc[rest, [df.columns.get_loc(x) for x in cols]] *= ratio
    # keep the next bar's open continuous
    return df


def make_range(n=600, seed=0, mid=500.0, amp=0.04, period=24, vol=0.004, start="2022-01-03", freq="B"):
    """Range-bound series (sine + noise) that produces repeated swing levels for S/R."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    close = mid * (1 + amp * np.sin(2 * np.pi * t / period) + rng.normal(0, vol, n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    wick = np.abs(rng.normal(0, vol, (2, n)))
    high = np.maximum(open_, close) * (1 + wick[0])
    low = np.minimum(open_, close) * (1 - wick[1])
    v = 1e6 * np.exp(rng.normal(0, 0.3, n))
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close, "Volume": v}, index=idx)


def intraday_index(days, bars_per_day=25, freq="15min", first="09:15"):
    """Naive exchange-time timestamps inside session hours for consecutive business days."""
    out = []
    for d in pd.bdate_range(days[0], periods=days[1]):
        out += list(pd.date_range(f"{d.date()} {first}", periods=bars_per_day, freq=freq))
    return pd.DatetimeIndex(out)


@pytest.fixture
def tmp_db(tmp_path):
    return config.STATE_DB


# ---- golden-master series (session cached; shared by P5 and P6) -------------------------------

GOLDEN_DIR = Path(__file__).parent / "golden"
UPDATE_GOLDEN = os.environ.get("ALGOTRADE_UPDATE_GOLDEN") == "1"


def golden_specs():
    """name -> (market, raw OHLCV, index close). Deterministic; zones/levels deliberately injected."""
    up = make_ohlcv(450, seed=101, drift=0.0015, vol=0.012)
    for at, kind in ((240, "RBR"), (300, "DBR"), (360, "RBD"), (410, "RBR")):
        up = inject_zone(up, at, kind)
    down = make_ohlcv(450, seed=202, drift=-0.0012, vol=0.012, price=900.0)
    for at, kind in ((240, "DBD"), (300, "RBD"), (360, "DBR"), (410, "DBD")):
        down = inject_zone(down, at, kind)
    rng = make_range(450, seed=303, mid=150.0)
    for at, kind in ((260, "DBR"), (330, "RBD"), (400, "RBR")):
        rng = inject_zone(rng, at, kind)
    rng["Volume"] *= 0.5
    idx_in = make_ohlcv(450, seed=7, drift=0.0004, vol=0.008, price=20000.0)["Close"]
    idx_us = make_ohlcv(450, seed=8, drift=-0.0003, vol=0.008, price=5000.0)["Close"]
    return {"up_IN": ("IN", up, idx_in), "down_IN": ("IN", down, idx_in), "range_US": ("US", rng, idx_us)}


@pytest.fixture(scope="session")
def golden_series():
    from algotrade.backtest import vote_table
    from algotrade.indicators import add_all
    from algotrade.strategies import STRATEGIES
    out = {}
    for name, (mkt, raw, idx) in golden_specs().items():
        df = add_all(raw, idx)
        out[name] = dict(market=mkt, raw=raw, index=idx, df=df, votes=vote_table(df, list(STRATEGIES)))
    return out


def check_golden(name: str, payload):
    """Compare payload with tests/golden/<name>.json (write it when ALGOTRADE_UPDATE_GOLDEN=1)."""
    import json
    import platform
    path = GOLDEN_DIR / f"{name}.json"
    payload = json.loads(json.dumps(payload))  # normalise tuples/keys exactly as stored
    if UPDATE_GOLDEN:
        GOLDEN_DIR.mkdir(exist_ok=True)
        header = {"generated_with": {"python": platform.python_version(), "pandas": pd.__version__,
                                     "numpy": np.__version__}}
        path.write_text(json.dumps({"_meta": header, "data": payload}, indent=1, sort_keys=True), encoding="utf-8")
    assert path.exists(), f"missing golden {path}; run with ALGOTRADE_UPDATE_GOLDEN=1"
    stored = json.loads(path.read_text(encoding="utf-8"))["data"]
    assert payload == stored
