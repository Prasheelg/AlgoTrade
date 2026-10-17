# Seams and dependencies (STEP 2: Map)

Status: STEP 2 (Map) complete. Next: `brownfield-3-break`. The expected result is that step 3 needs no source edits.
Input: `plan/plan.md`. Every claim below was checked against the code on 2026-10-14. Several were also checked
with a throwaway probe interpreter that had dotenv disabled, the Telegram environment variables blanked,
`sqlite3.connect` blocked and sockets blocked. Nothing under `algotrade/`, `main.py` or `data/` was changed.

Library versions in `.venv`: pandas 3.0.6, numpy 2.5.4, Flask 3.1.3, yfinance 1.7.0 (it uses **curl_cffi 0.16.3**),
requests 2.34.2, python-dotenv 1.2.4, lxml 6.1.3, beautifulsoup4 4.15.0. pytest is **not** installed.

---

## 1. Patch points: plan section 3 checked

Rule: `from X import name` copies the name into the importing module, so you must patch it where it is
used. `from . import mod` followed by `mod.attr(...)` looks the attribute up on every call, so patching
`mod.attr` affects every caller.

| Plan claim | Verdict | Evidence |
|---|---|---|
| `scanner.fetch_ohlcv` | **Confirmed** | import `scanner.py:8`. Used by `scan` at `scanner.py:181` **and by `fetch_index` at `scanner.py:160`**, so one fake covers both. The fake must return the index ticker too (`"^NSEI"` / `"^GSPC"`, `markets.py:40,43`). Otherwise filters see NaN, which is the "missing index" path. |
| `scanner.get_universe` | **Confirmed** | import `scanner.py:11`, used at `scanner.py:171` only when `symbols` is falsy. |
| `tracker.fetch_ohlcv` | **Confirmed** | import `tracker.py:22`, used at `tracker.py:125` with `period=TRACK_PERIOD.get(tf,"1mo")`, `market=mk`. |
| `backtest.fetch_ohlcv` | **Confirmed** | import `backtest.py:11`, used at `backtest.py:86`. |
| `backtest.fetch_index` | **Confirmed, with a note** | import `backtest.py:13` (`from .scanner import evaluate, fetch_index, strategy_votes`), used at `backtest.py:87`. `backtest.fetch_index` is the *scanner* function and still calls **`scanner.fetch_ohlcv`**. Patching only `backtest.fetch_ohlcv` therefore leaves the index fetch live. Patch `backtest.fetch_index` (or `scanner.fetch_ohlcv`) as well. |
| `web.scan_and_alert` | **Confirmed** | import `web.py:10`, used only in `_scan_timeframes` at `web.py:115`. |
| `main.scan`, `main.scan_and_alert` | **Confirmed** | import `main.py:19`, used in `run_scan` at `main.py:35,38`. |
| `main.run_scan` | **Confirmed** | defined `main.py:33`. `run_loop` (`main.py:58,64`) and `main()` (`main.py:119`) call it as a module global, so `monkeypatch.setattr(main, "run_scan", fake)` works. |
| `config.STATE_DB` read when each connection opens | **Confirmed** | `state._conn` calls `sqlite3.connect(config.STATE_DB)` at `state.py:21` on every call. No connection is opened at import. |
| `telegram_bot.send_signals` / `send_message` (P4) | **Confirmed** | `scanner.scan_and_alert` does a *lazy* `from . import state, telegram_bot` (`scanner.py:201`) and then calls `telegram_bot.send_signals` (`scanner.py:205`). This is an attribute lookup, so a monkeypatch on the module works. `send_signals` calls `send_message` as a module global (`telegram_bot.py:89`), so one patch covers it. `tracker.py:148` and `main.py:47,63,107` also use `telegram_bot.send_message` as an attribute. |
| `tracker.update_all` (P7, P8) | **Confirmed** | `web.refresh` calls `tracker.update_all(notify=True)` (`web.py:43`) and `main.run_loop` calls `tracker.update_all(market=code)` (`main.py:57`). Both are attribute lookups, so patch `tracker.update_all`. |

**Corrections and additions to the plan**

1. **`main.get_universe` is missing from the plan's list.** It is imported by name at `main.py:20` and used by
   `run_backtest` at `main.py:73`. CLI `--backtest` tests must patch `main.get_universe`. Patching
   `universe.get_universe` would have no effect there.
2. `main.run_backtest` imports `backtest` lazily (`main.py:72`, `from algotrade import backtest`). Patch
   `backtest.run` (or `backtest.fetch_ohlcv` + `backtest.fetch_index`) on the module object. Both routes work.
3. **`main.time`, `telegram_bot.time` and `web.time` are the same global `time` module.** The probe confirmed
   `main.time is telegram_bot.time is web.time`. `monkeypatch.setattr(main.time, "sleep", ...)` therefore
   patches `time.sleep` **process-wide** for the whole test. monkeypatch restores it afterwards, but while the
   test runs it also changes Telegram and Flask timing. The cleaner seam is to replace the module reference in
   one module only: `monkeypatch.setattr(main, "time", SimpleNamespace(sleep=fake_sleep))`. Do the same for
   `telegram_bot` (`time.sleep` at `telegram_bot.py:30,38,91`) and `web` (`time.strftime` at `web.py:44`).
   `main.time` is used only at `main.py:68`, and `web.time` only at `web.py:44,52`.
4. **The socket guard alone will NOT stop yfinance.** yfinance 1.7 downloads through `curl_cffi` (libcurl in
   C), which never touches `socket.socket.connect`. The kill-switch must patch `yfinance.download` (that
   is what `data.py:27` calls through `data.yf`). As a belt-and-braces step it should also patch
   `curl_cffi.requests.Session.request` to raise. The socket guard still covers `requests` (Telegram, NSE,
   Wikipedia).
5. **The `.env` contains only `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`**; key names were checked, values
   were not printed. So the plan's worry about `.env` overriding STRATEGIES, CAPITAL and similar settings does
   not apply today. The developer's **shell environment** could still set them, and a future `.env` might.
   Because `markets.MARKETS` captures values at import (section 2.2), changing `config.CAPITAL` with
   monkeypatch is too late. Neutralise these settings **before import** (section 6.1), so `MARKETS` is built
   from the code defaults.
6. Plan P1 says "evaluate(..., votes=...)". When `votes` is given, `evaluate` still loops over `selected`
   (`scanner.py:107-108`) and does `raw[name]`. If `strategies` is omitted, `selected` = `config.STRATEGIES or
   DEFAULT_STRATEGIES` (11 names), and a smaller votes dict raises **KeyError** (the probe confirmed
   `KeyError: 'EMA 9/21 Cross'`). P1 tests must pass `strategies=[...]` that match the votes keys, or supply
   all names. The registry has 13 strategies, of which 11 are default.
7. Plan success criterion 3 ("`data/signals.db` byte-identical, sha256 check") **cannot be met as written**.
   The live dashboard rewrites the database every 5 min (see section 5). `data/signals.db` already differs
   from `plan/data_sha256_before.txt` (its mtime was 12:24 today). Verify isolation with a connection guard
   instead (section 6.3).

Worked anchors from plan P1, confirmed by probe (IN, BUY, Close 100, High 101, Low 99, ATR 2, TURNOVER20 1e9):
limit gives `99.0 / 96.0 / [103.5, 108.0] / qty 333`. Stop gives `101.2 / 98.2 / [105.7, 110.2] / qty 333`.
US gives qty 33 with key `US|X|BUY|<date>`.

---

## 2. Import side effects and global state

### 2.1 Import chain and what runs at import
`main` imports `config, markets, telegram_bot, tracker` and `scanner` (which pulls in `data`, `indicators`, `zones`,
`strategies`, `universe`). `web` is imported lazily by `main` (`main.py:110`). Tests import it directly. A full
import took about 1.0 s in the probe.

| Module:line | Side effect | Test impact |
|---|---|---|
| `config.py:8` `load_dotenv(ROOT / ".env")` | Loads the **real Telegram token and chat id** into `os.environ`. `override=False`, so variables that already exist win. | Neutralise **before** the first `import algotrade...` (section 6.1). |
| `config.py:16-29,39-40` | Reads environment variables **once** into module constants (`TELEGRAM_*`, `UNIVERSE`, `SCAN_INTERVAL_MIN`, `TIMEFRAME`, `MIN_SCORE`, `CAPITAL`, `RISK_PER_TRADE_PCT`, `ALLOW_SHORTS`, `STRATEGIES`, `FILTERS`, `US_UNIVERSE`, `US_CAPITAL`). | Later env changes are ignored. Change behaviour with `monkeypatch.setattr(config, ...)`. |
| `config.py:44-46` | `DATA_DIR = ROOT/"data"`, `DATA_DIR.mkdir(exist_ok=True)` (a no-op because the folder exists), `STATE_DB = DATA_DIR/"signals.db"` (a path only; no file is opened). | Harmless. Redirect `STATE_DB`/`DATA_DIR` per test. |
| `markets.py:37-44` `MARKETS` | Two frozen `Market` instances that **copy** `config.UNIVERSE / CAPITAL / MIN_PRICE / MIN_AVG_TURNOVER` and `config.US_UNIVERSE / US_CAPITAL / US_MIN_PRICE / US_MIN_AVG_TURNOVER` at import. | See section 2.2. |
| `main.py:24-25` | `logging.basicConfig(INFO)` and silences the yfinance logger. | Harmless. `caplog` still works. |
| `web.py:17-20` | `app = Flask(...)`, `_lock`, `_scan_locks = {code: Lock() for code in markets.MARKETS}`, `_last_refresh = {"at": None}`. | Process-global. Reset them per test (section 2.3). |
| `web.py:14` | `STATIC = .../algotrade/static` (`index.html` exists, and `GET /` returned 200 in the probe). | Harmless. |
| `strategies.py:193-218`, `scanner.py:46-60` | Constant registries (`STRATEGIES`, `FAMILIES`, `OPT_IN`, `DEFAULT_STRATEGIES`, `BARS_PER_DAY`, `PULLBACK_STRATEGIES`, `FILTERS`). | Read-only. Mutation tests patch them with monkeypatch. |
| (none) | No import performs network or DB I/O. | Confirmed by the probe with sockets and sqlite blocked. |

### 2.2 Which value lives where (config vs markets)
The probe confirmed that `Market` is `frozen=True` with no `__slots__`, so instances have a `__dict__`.

| Value | Read live from `config` at call time | Frozen copy in `markets.MARKETS[code]` |
|---|---|---|
| capital (qty sizing) | no | `mkt.capital` (`scanner.py:144-145`, `telegram_bot.py:74`, `web.py:71`) |
| min price / min turnover | no (`config.MIN_PRICE` is never read by `evaluate`) | `mkt.min_price`, `mkt.min_turnover` (`scanner.py:100-101`) |
| universe spec | **yes**: `get_universe` (`universe.py:68`), `main.universe_of` (`main.py:30`) | `mkt.universe` (dashboard payload only, `web.py:71`) |
| MIN_SCORE, FILTERS, STRATEGIES, ALLOW_SHORTS, RISK_PER_TRADE_PCT, ATR_SL_MULT, TARGET_R_MULTIPLES, ENTRY_VALID_BARS, TIMEFRAME, SCAN_INTERVAL_MIN, TELEGRAM_* , STATE_DB, DATA_DIR | **yes** (module attribute read at call time) | no |

To change a frozen value in a test, use
`monkeypatch.setitem(markets.MARKETS, "IN", dataclasses.replace(markets.MARKETS["IN"], capital=...))`.
`markets.get` reads the dict on every call (`markets.py:52`), so every caller sees the replacement.
Do not change `config.CAPITAL` and expect qty to change.

### 2.3 Mutable global state that tests touch
- `config.*` constants: tests should change them only through `monkeypatch.setattr`.
- `config.UNIVERSE` / `config.US_UNIVERSE`: **mutated by production code** in `main()` (`main.py:102,104`) when
  `--symbols` is given. Tests must call `monkeypatch.setattr(config, "UNIVERSE", config.UNIVERSE)` (same for
  US) *before* calling `main()`, so monkeypatch restores the value afterwards.
- `web._last_refresh` (dict): reset with `monkeypatch.setitem(web._last_refresh, "at", None)`.
- `web._scan_locks` (dict of `threading.Lock`): released in `finally` (`web.py:148-149`). For the 409 test,
  acquire `web._scan_locks["IN"]` in the test and release it in a `finally`. As a safety net, an autouse web
  fixture can assert that no lock is held after each test (`not lock.locked()`).
- `web._lock`: used by `refresh()`. Never left held, because it is a `with` block.
- `markets.MARKETS`: change it only through `monkeypatch.setitem`.
- `sys.argv`: change it through `monkeypatch.setattr(sys, "argv", [...])`.

---

## 3. Dependency graph (P1-P9)

### 3.1 Call graph
```
main.main ──argparse──┬─ --test-telegram ─> telegram_bot.send_message ─> requests.post [NET]
                      ├─ --ui ───────────> web.serve ─> Thread(_background) + app.run   [NEVER CALL]
                      ├─ --backtest ─────> run_backtest ─> main.get_universe ─> universe.* [NET]
                      │                                  ─> backtest.run ─> backtest.fetch_ohlcv [NET]
                      │                                                  ─> backtest.fetch_index(=scanner.fetch_index) ─> scanner.fetch_ohlcv [NET]
                      │                                                  ─> backtest_symbol ─> add_all, evaluate, fill_price
                      │                                  ─> summarize ; trades.to_csv(config.DATA_DIR/...) [FS]
                      ├─ --once/--dry-run ─> run_scan ─> main.scan | main.scan_and_alert
                      └─ (default) ─────> run_loop ─> markets.get(code).now() [CLOCK]
                                                     ─> Market.is_open / is_after_close
                                                     ─> tracker.update_all, run_scan, telegram_bot.send_message
                                                     ─> time.sleep(SCAN_INTERVAL_MIN*60)  [SLEEP, while True]

scanner.scan_and_alert ─> scan ─> scanner.get_universe [NET] ; scanner.fetch_ohlcv [NET] ; fetch_index
                       │          ─> indicators.add_all ─> zones.annotate ─> find_zones, find_pivots
                       │          ─> evaluate ─> strategy_votes ─> strategies.STRATEGIES[n](df,i) ─> zones.levels_at
                       │                     ─> passes_filters, entry_type_for, markets.get
                       ├─> state.filter_new [DB]
                       ├─> telegram_bot.send_signals ─> format_signal, send_message [NET], time.sleep(1.1)
                       └─> state.record [DB, CLOCK datetime.now]

tracker.update_all ─> state.open_signals [DB]
                   ─> tracker.fetch_ohlcv [NET]
                   ─> _bars_after ─> _walk ─> fill_price
                   ─> telegram_bot.send_message [NET]  (only if notify and sig["telegram"])
                   ─> state.update_signal [DB, CLOCK]

web (Flask test_client)
  GET  /             ─> send_from_directory(STATIC)
  GET  /api/signals  ─> _market ─> markets.get ; _signals_payload ─> state.all_signals [DB] ─> _enrich
  POST /api/refresh  ─> refresh ─> tracker.update_all [NET via tracker] ; time.strftime [CLOCK]
  GET  /api/meta     ─> strategies.describe, scanner.FILTERS, markets.MARKETS
  POST /api/scan     ─> _scan_locks ; _scan_timeframes ─> web.scan_and_alert ; _signals_payload

data.fetch_ohlcv ─> markets.get(market).tz ; universe.to_yahoo ; yf.download [NET via curl_cffi]
universe.get_universe ─> _fetch_index / _fetch_us_index ─> requests.get [NET], pd.read_csv / pd.read_html(lxml)
```

### 3.2 Effect map for the money path (`evaluate` -> `record` -> `_walk` -> `update_all`)
```
config.MIN_SCORE/FILTERS/STRATEGIES/ALLOW_SHORTS/ATR_SL_MULT/TARGET_R_MULTIPLES/RISK_PER_TRADE_PCT
markets.MARKETS[code].capital/min_price/min_turnover
scanner.PULLBACK_ATR/BREAKOUT_BUFFER_ATR/MIN_STOP_ATR/MAX_STOP_ATR/BARS_PER_DAY/PULLBACK_STRATEGIES
        │
        ▼
evaluate ──> Signal(entry, stop_loss, targets, qty, entry_type, ref_price, bar_time, key)
        │                       (seen by: format_signal text, state rows, backtest trades, dashboard)
        ▼
state.record ──> row(status PENDING|ACTIVE, ltp=ref_price or entry, telegram flag, entry_type)
        ▼
tracker._walk (config.ENTRY_VALID_BARS, MAX_HOLD_BARS) ──> status/exit_price/closed_at/fill_price/filled_at
        ▼
update_all ──> state.update_signal (row) + Telegram text ──> web._enrich (pnl_pct, pnl_r, to_entry_pct)
backtest_symbol reuses evaluate + tracker.fill_price (so a change to fill_price shows up in both)
```
Detection points: the P1 unit asserts on the `Signal` fields; the P3 assertions on the DB row; the P2 table
tests on the `_walk` dict; the P2 `update_all` asserts on the row and the message text; the P7 `_enrich` maths;
and the P5/P6 goldens (which catch drift anywhere upstream).

### 3.3 External dependencies per target

| Target | Network | DB | FS | Clock | Sleep | Threads | Pure? |
|---|---|---|---|---|---|---|---|
| P1 `scanner.evaluate`, `passes_filters`, `entry_type_for`, `Signal.key` | - | - | - | - | - | - | **pure** (reads config + MARKETS) |
| P2 `tracker.fill_price`, `_walk`, `_bars_after` | - | - | - | - | - | - | **pure** |
| P2 `tracker.update_all` | `tracker.fetch_ohlcv`, `telegram_bot.send_message` | `state.*` | - | via `state.update_signal` | - | - | no |
| P3 `state.*` | - | sqlite3 at `config.STATE_DB` | DB file | `state.datetime.now()` (`state.py:45,75`) | - | - | no |
| P4 `scanner.scan` / `scan_and_alert` | `scanner.fetch_ohlcv`, `scanner.get_universe`, `telegram_bot.send_signals` | `state.*` | - | `state` | `send_signals` -> 1.1 s/signal | - | no |
| P5 strategies / indicators / zones | - | - | - | - | - | - | **pure** (numpy/pandas only) |
| P6 `backtest_symbol`, `vote_table`, `summarize` | - | - | - | - | - | - | **pure** |
| P6 `backtest.run` | `backtest.fetch_ohlcv`, `backtest.fetch_index` | - | - | - | - | - | no |
| P7 `web` routes | via `web.scan_and_alert`, `tracker.update_all` | `state.all_signals` | `static/index.html` | `web.time.strftime` | - | `threading.Lock` | no |
| P8 `Market.is_open`/`is_after_close`/`get` | - | - | - | `Market.now` only when `now` is None | - | - | pure with an injected `now` |
| P8 `main.run_loop` | `tracker`, `run_scan`, `telegram_bot` | - | - | `m.now()` -> `datetime.now(ZoneInfo)` | `time.sleep`, `while True` | - | no |
| P8 `main.main` | all of the above | - | `config.DATA_DIR` CSV | - | - | `web.serve` | no |
| P9 `telegram_bot.format_signal` | - | - | - | - | - | - | **pure** |
| P9 `telegram_bot.send_message` / `send_signals` | `requests.post` | - | - | - | `time.sleep` (2 s, retry_after, 1.1 s) | - | no |
| P9 `universe.get_universe` / `to_yahoo` | `requests.get` (+ `pd.read_html` needs lxml, which is installed) | - | - | - | - | - | `to_yahoo` pure |
| P9 `data.fetch_ohlcv` | `yf.download` (curl_cffi) | - | - | - | - | - | no |

---

## 4. Seams per class/module

```
scanner.evaluate(symbol, df, i, min_score, strategies, timeframe, market, filters, votes)
├─ Parameter seams: every input is a parameter; votes= bypasses the strategies entirely
├─ Global reads: config.{MIN_SCORE,FILTERS,STRATEGIES,ALLOW_SHORTS,ATR_SL_MULT,TARGET_R_MULTIPLES,RISK_PER_TRADE_PCT}
│                markets.MARKETS[market]; module constants PULLBACK_ATR etc. (monkeypatch-able)
└─ Obstacle: votes must cover every selected name (pass strategies= explicitly)

tracker
├─ fill_price(side, entry_type, entry, bar)    pure; bar = dict or Series with Open/High/Low
├─ _walk(sig: dict, bars: DataFrame, intraday) pure; sig keys used: side, entry_type, entry, stop_loss,
│                                               target1, target2, bar_time, bar_date
├─ _bars_after(sig, df)                        pure; keys: timeframe, bar_date, bar_time
└─ update_all  seams: tracker.fetch_ohlcv, telegram_bot.send_message, config.STATE_DB (via state)

state
├─ config.STATE_DB (read per _conn call)      -> tmp_path
├─ state.datetime (module global class)       -> subclass with fixed now() if exact timestamps needed
└─ sqlite3 (state.sqlite3 is the global module) -> connection guard (section 6.3)

markets.Market (frozen dataclass)
├─ is_open(now) / is_after_close(now): `now` parameter seam (datetimes are always truthy, so `now or ...` is safe)
└─ now(): class-level seam  monkeypatch.setattr(markets.Market, "now", lambda self: script[self.code]())
          instance-level seam object.__setattr__(mkt, "now", fake). This works because there is no
          __slots__, but monkeypatch cannot undo it. monkeypatch.setattr(mkt, "now", ...) raises
          FrozenInstanceError. Prefer the class-level patch.
          module-level seam  monkeypatch.setattr(markets, "datetime", FakeDatetime) (FakeDatetime is a datetime
          subclass whose classmethod now(tz) returns scripted values). Also works, because now() resolves
          `datetime` from markets' globals.

main
├─ run_scan seams: main.scan, main.scan_and_alert, telegram_bot.format_signal
├─ run_loop seams: Market.now (class), main.time -> SimpleNamespace(sleep=raise_after_n), main.run_scan,
│                  tracker.update_all, telegram_bot.send_message
├─ run_backtest seams: main.get_universe, backtest.run (or backtest.fetch_ohlcv + backtest.fetch_index),
│                      config.DATA_DIR -> tmp_path
└─ main(): sys.argv; web.serve (patch it if --ui is ever tested); p.error -> SystemExit(2)

web
├─ app.test_client()
├─ web.scan_and_alert, tracker.update_all, web.time (strftime), web._last_refresh, web._scan_locks
└─ never call web.serve / web._background

telegram_bot
├─ config.TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (read per call)
├─ telegram_bot.requests -> fake namespace with .post and .RequestException (or patch requests.post globally)
│   NB: `except requests.RequestException` (telegram_bot.py:36) resolves through the same name, so a fake
│   namespace must provide RequestException. Simpler: monkeypatch.setattr(requests, "post", fake).
└─ telegram_bot.time -> SimpleNamespace(sleep=recorder)

universe: requests.get (the RequestException note does not apply here; get_universe catches Exception), config.UNIVERSE
data:     data.yf.download (= yfinance.download), markets.MARKETS tz
```

---

## 5. Environment fact: live dashboard running

A live `python main.py --ui` server is running against `data/signals.db`. Its `_background` thread calls
`tracker.update_all(notify=True)` every 300 s (`web.py:49-52`, `REFRESH_SEC=300`), so the file's bytes and
mtime change while the tests run. `data/signals.db` already differs from `plan/data_sha256_before.txt`. Consequences:

- "`data/` unchanged" **cannot** be proven with file hashes for `signals.db`. Prove it **behaviourally**:
  no test may ever open a sqlite connection on the real path (guard in section 6.3), and `config.STATE_DB` /
  `config.DATA_DIR` must always point under a temp directory.
- A hash check is still valid for `backtest_trades.csv`, `backtest_trades_US.csv` and
  `signals_backup_2026-10-13.db`, which the server does not write. Keep it as a secondary check for those three.
- The tests run in a separate process, so they cannot disturb the server, as long as they never touch the real
  DB. sqlite locking could otherwise make a test hang or fail intermittently, which is another reason for the
  guard.
- Never start a server in tests. Port 8050 is in use.

---

## 6. Testing obstacles and how to handle them

| # | Obstacle | Location | Test-side solution | Source edit? |
|---|---|---|---|---|
| O1 | Real Telegram token loaded at import | `config.py:8,16-17` | In `conftest.py`, *before* any `algotrade` import: `import dotenv; dotenv.load_dotenv = lambda *a, **k: False`. This works because config does `from dotenv import load_dotenv` at its own import time, so it picks up the patched attribute. Also `os.environ["TELEGRAM_BOT_TOKEN"] = ""`, `os.environ["TELEGRAM_CHAT_ID"] = ""`, and pop every other config key (section 6.1). Then assert `config.TELEGRAM_BOT_TOKEN == ""` right after import. The probe confirmed the token is `''`. | No |
| O2 | `MARKETS` captures config at import | `markets.py:37-44` | Neutralising the env before import gives code defaults (IN 100000/50/5e7, US 10000/5/2e7). An autouse fixture asserts these values, and tests use `dataclasses.replace` + `setitem` to vary them. | No |
| O3 | sqlite on the real path | `state.py:21` | Session level: set `config.STATE_DB` to a session temp file right after import in conftest. Per test (autouse): `monkeypatch.setattr(config, "STATE_DB", tmp_path / "signals.db")`. Guard: wrap `sqlite3.connect` so it raises if `Path(arg).resolve() == REAL_DB`. | No |
| O4 | `state.record` / `update_signal` use the wall clock | `state.py:45,75` | Assert the format with a regex `\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d`. When ordering matters (`all_signals` ORDER BY sent_at), patch `state.datetime` with a subclass whose `now()` returns scripted values. Otherwise rows recorded in the same second tie. | No |
| O5 | `tracker.update_all` sends Telegram | `tracker.py:148` | `monkeypatch.setattr(telegram_bot, "send_message", recorder)`. Also, a global autouse fake makes `send_message` fail loudly unless a test overrides it. | No |
| O6 | `run_loop` is an infinite loop with sleep | `main.py:51-68` | `monkeypatch.setattr(main, "time", SimpleNamespace(sleep=fake))`, where `fake` raises `StopLoop(Exception)` after N calls. Sleep is outside the `try` (`main.py:68` vs `55-67`), so a plain Exception escapes. Exceptions raised *inside* `run_scan`/`update_all` are caught by `main.py:66`, which is how the "one market fails" case is tested. | No |
| O7 | `Market.now()` on a frozen dataclass | `markets.py:25-26` | `monkeypatch.setattr(markets.Market, "now", lambda self: next(clock[self.code]))` patches the **class**. Frozen only blocks *instance* setattr, and the probe confirmed it works. `object.__setattr__(instance, "now", f)` also works (there is no `__slots__`) but monkeypatch cannot undo it, so it is not recommended. Patching `markets.datetime` works too. Use aware datetimes `datetime(..., tzinfo=ZoneInfo(m.tz))` to mirror production; naive ones also work because only `.weekday()`, `.time()`, `.date()`, `.strftime()` are used. | No |
| O8 | `web.serve` starts a daemon thread + server | `web.py:153-156` | Never call it. `main()` tests skip `--ui`, or patch `web.serve` to a recorder. | No |
| O9 | `data.fetch_ohlcv` -> `yf.download` (curl_cffi, bypasses the socket guard) | `data.py:27` | Autouse: `monkeypatch.setattr(yfinance, "download", raise_network)`. P9 data tests override it with a fake that returns MultiIndex or flat frames. Optional: also patch `curl_cffi.requests.Session.request` to raise. | No |
| O10 | `requests.post` / `requests.get` | `telegram_bot.py:27`, `universe.py:50,60` | Autouse socket guard (`socket.socket.connect` raises) plus `requests.Session.request` raising. Tests inject fakes with `monkeypatch.setattr(requests, "post"/"get", fake)`. Fake responses need `.status_code`, `.ok`, `.json()`, `.text`, `.raise_for_status()`. | No |
| O11 | `time.sleep` in Telegram retries and send pacing | `telegram_bot.py:30,38,91` | `monkeypatch.setattr(telegram_bot, "time", SimpleNamespace(sleep=recorder))`. The recorder also lets tests assert `retry_after` and the 2 s backoff. | No |
| O12 | `scan_and_alert` with `send=True` sleeps 1.1 s per signal | `telegram_bot.py:91` | Patch `telegram_bot.send_signals` directly (P4), or patch `telegram_bot.time` as in O11. | No |
| O13 | `--backtest` writes CSV to `data/` | `main.py:79-80` | `monkeypatch.setattr(config, "DATA_DIR", tmp_path)` and patch `main.get_universe` + `backtest.run`. | No |
| O14 | `--symbols` mutates `config.UNIVERSE` | `main.py:102,104` | Pre-register with monkeypatch (section 2.3). | No |
| O15 | `evaluate` needs `i >= 200`, and the strategies need `df.attrs` | `scanner.py:95`, `zones.py:123`, `strategies.py:124` | P1: hand frame with 201+ rows plus `votes=` (no attrs needed). P5: build through `add_all` (which calls `annotate`), or call `zones.annotate` on a hand frame that has an ATR column. | No |
| O16 | Random-walk synthetic data produces **zero zones** | `zones.find_zones` | Seeded random walks did not satisfy the explosive/boring geometry (0 zones in the probe). The P5 golden builder must **inject** explicit base patterns (explosive leg-in, 1-4 small-body bars, explosive leg-out of at least 1.1x ATR, then a retest that holds), so the zone strategies and structural stops actually run. Also add a few repeated swing levels for S/R. | No |
| O17 | Web `/api/refresh` -> `refresh()` swallows exceptions; `_last_refresh` uses the real clock | `web.py:40-46` | Patch `tracker.update_all` (and `web.time` if an exact value is asserted). Reset `_last_refresh`. | No |
| O18 | Cross-test leakage through the logging config / the yfinance logger | `main.py:24-25` | Accept it. It is harmless. | No |

**Conclusion: no step-3 source edit is required.** Every seam can be reached from the test side. The
minimal edits one *could* make, such as a `clock` parameter on `run_loop` or a `sleep` injection, are not
needed because module-attribute patching covers them. That keeps success criterion 6 (zero source diffs) achievable.

### 6.1 Env neutralisation (top of `tests/conftest.py`, before any `algotrade` import)
```python
import os, dotenv
dotenv.load_dotenv = lambda *a, **k: False          # .env is never read
for k in ("UNIVERSE","SCAN_INTERVAL_MIN","TIMEFRAME","MIN_SCORE","CAPITAL","RISK_PER_TRADE_PCT",
          "ALLOW_SHORTS","STRATEGIES","FILTERS","US_UNIVERSE","US_CAPITAL"):
    os.environ.pop(k, None)
os.environ["TELEGRAM_BOT_TOKEN"] = ""; os.environ["TELEGRAM_CHAT_ID"] = ""
from algotrade import config                        # first import happens here
assert config.TELEGRAM_BOT_TOKEN == "" and config.CAPITAL == 100000.0
REAL_DB = config.STATE_DB.resolve(); REAL_DATA = config.DATA_DIR.resolve()
config.STATE_DB = <session tmp>/signals.db           # until per-test fixtures take over
```
`pytest.ini` `pythonpath = .` is needed so that `import main` / `import algotrade` resolve. Do **not** add
`algotrade` imports to any `tests/__init__.py` or plugin that loads before conftest.

### 6.2 Network kill-switch (autouse, function scope)
- `socket.socket.connect` / `connect_ex` raise `RuntimeError("network blocked in tests")`. Allow AF_UNIX
  if pytest needs it (it does not on Windows).
- `yfinance.download` raises. Optionally `curl_cffi.requests.Session.request` raises as well.
- `requests.Session.request` raises. This covers `requests.get` and `requests.post` because they route
  through Session.
- `telegram_bot.send_message` is replaced by a function that **fails the test** unless the test installs its
  own fake (for example `pytest.fail("unexpected Telegram send")`). P9 tests that exercise the real
  `send_message` restore it with `monkeypatch.setattr(telegram_bot, "send_message", ORIGINAL_SEND)` and fake
  `requests.post`.

### 6.3 DB isolation guard
- Autouse: `monkeypatch.setattr(config, "STATE_DB", tmp_path / "signals.db")` and
  `monkeypatch.setattr(config, "DATA_DIR", tmp_path)`.
- Session-scope wrapper around `sqlite3.connect`: if the first argument resolves to `REAL_DB`, or to anything
  under `REAL_DATA`, raise and record a violation. A `pytest_sessionfinish` hook (or a session fixture
  teardown) fails the run if any violation was recorded. This is the primary "data/ untouched" check.
- Secondary check: sha256 before/after for the three files the live server does not write (section 5).

### 6.4 Config pinning (autouse)
Use `monkeypatch.setattr(config, ...)` to set MIN_SCORE=2, ALLOW_SHORTS=False, STRATEGIES=[], FILTERS=[], TIMEFRAME="1d",
RISK_PER_TRADE_PCT=1.0, SCAN_INTERVAL_MIN=15, TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="", UNIVERSE="NIFTY100",
US_UNIVERSE="SP100". Assert `markets.MARKETS["IN"].capital == 100000.0` and `["US"].capital == 10000.0`.

### 6.5 Data builders and fakes (fixtures/helpers in conftest)
- `make_ohlcv(n=600, seed=..., drift=..., vol=..., start="2023-01-02", freq="B" | "15min", price=500)`.
  It uses `numpy.random.default_rng(seed)`, enforces Low <= min(Open, Close) <= max(Open, Close) <= High,
  and gives float Volume. Intraday frequencies need naive exchange-time timestamps inside session hours.
- `inject_zone(df, at, kind="RBR")` and `inject_levels(...)`, to make zones and S/R appear (O16).
- `eval_frame(rows=201, **overrides)`: the P1 hand frame with columns Close/High/Low/ATR/TURNOVER20/RSI/ADX/
  Volume/VOL_AVG20/IDX_CLOSE/IDX_EMA200/RS, with the last row overridable.
- `bars(rows)`: builds a tz-naive DataFrame from `[(ts, O, H, L, C), ...]` for `_walk` tables.
- `sig_row(**kw)`: a dict shaped like a `state.all_signals` row (side, entry_type, entry, stop_loss, target1,
  target2, bar_date, bar_time, timeframe, market, symbol, key, status, telegram).
- `fake_fetch(frames: dict)`: returns `lambda symbols, tf="1d", period=None, market="IN", **kw:
  {s: frames[s] for s in symbols if s in frames}` and records its calls. Install it on `scanner`, `tracker`
  and `backtest` as needed.
- `telegram_recorder`: a fake `send_message` / `send_signals` that records the text and returns a scripted
  bool. For `send_signals` it returns a scripted subset.
- `FakeResponse(status_code, json=None, text="")` for requests.
- `clock(times)`: an iterator for `Market.now`. `StopLoop` exception plus `sleep_after(n)`.
- `make_signal(**kw)`: a `scanner.Signal` with sensible defaults, for `format_signal` / `state.record`.

Performance (from the probe): `add_all` on 600 bars took 0.07 s, `vote_table` (13 strategies) 0.57 s,
`backtest_symbol` 0.48 s uncached and 0.04 s cached, and the cached and uncached results were equal. A golden set
of about 6 series x 600 bars costs under 5 s, so the 60 s budget is comfortable. Use `scope="module"` fixtures for
`add_all` frames that several tests reuse.

---

## 7. Testing strategy: where to start

1. **Harness first** (`pytest.ini`, `requirements-dev.txt`, `conftest.py` sections 6.1-6.5), plus a smoke test file
   `tests/test_harness.py`. It asserts that the token is empty, a socket connect raises, `yfinance.download`
   raises, a sqlite connect to `REAL_DB` raises, `config.STATE_DB` is under tmp, and `MARKETS` holds the
   defaults. Nothing else is written until this passes.
2. **P1 `evaluate`** is pure, has the highest money risk, and has fully parameter-injectable seams. Start with
   the confirmed anchors.
3. **P2 `fill_price` / `_walk` / `_bars_after`**: pure, table driven.
4. **P3 `state`** on a temp DB, including the hand-built legacy table for the migration.
5. **P2 `update_all`**: combines 2-4 with fake fetch and the Telegram recorder.
6. **P4 scan / scan_and_alert** with fake fetch and universe.
7. **P5 strategies/indicators/zones** plus the golden master (it needs the O16 builders).
8. **P6 backtest**, then **P7 web**, then **P8 markets/main**, then **P9 telegram/universe/data**.

Reasoning: start where the code is pure and the money risk is highest. Each later layer then reuses earlier
fixtures, and the network/DB-touching layers are tested only once the safety harness has proven itself.

---

## 8. Next step

Run **`brownfield-3-break`**. Mapping found **no seam that needs a source edit**. Step 3 should record
"no dependency breaking required", list the test-side seam techniques above (especially the corrections in
section 1: `main.get_universe`, `backtest.fetch_index` -> `scanner.fetch_ohlcv`, the shared `time` module,
yfinance via curl_cffi, and evaluate `votes`/`strategies` coupling), and amend plan success criterion 3 to
use the DB connection guard instead of a `signals.db` hash.
