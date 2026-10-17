# Plan: Put the trading core under an automated safety net

Status: **COMPLETE for this run (2026-10-14).** Steps 1-4 and 7 are done. Steps 5-6 (implement / refactor)
are N/A because this run adds no feature. Final result: 377 tests pass in about 11.3 s with 99% line coverage
and zero source diffs (see section 8). Pinned quirks and suspected bugs are in `plan/quirks.md`.
Section 7 amends sections 2-5; where they conflict, section 7 wins.
Scope: coverage only (characterization / regression tests). Workflow steps 5-6 (implement / refactor
features) are OUT OF SCOPE for this run; the next feature request will be built on top of this net.

---

## 1. Feature description

AlgoTrade has zero automated tests (1,499 lines in `algotrade/` plus `main.py`, Python 3.12.7, pandas 3.0.6,
numpy 2.5.4, Flask 3.1.3; pytest is not installed). We will add a pytest suite that is:

- **Characterization**: it pins down what the code does *today*, including the odd parts. It does not
  check what the code "should" do. Any assertion that fails against the current code is a test bug, not a
  code bug.
- **Offline**: no Yahoo Finance, NSE, Wikipedia or Telegram traffic. No Telegram message is ever sent.
- **Deterministic**: synthetic, seeded OHLCV data; no wall-clock dependence (times are injected or patched).
- **Isolated**: never reads or writes `data/signals.db`, `data/signals_backup_*.db`, `data/backtest_trades*.csv`
  or anything else under `data/`.
- **Fast**: the whole suite runs in under ~60 s on the dev machine.

Deliverables (all new files; no edits to existing source):
- `requirements-dev.txt`: `-r requirements.txt` + `pytest>=8` (`requirements.txt` stays as it is).
- `pytest.ini`: `testpaths = tests`, `pythonpath = .` (so `import main` works).
- `tests/conftest.py`: safety fixtures (see section 4.1) and synthetic data builders.
- `tests/test_*.py`: one file per module, in the order shown in section 2.
- `tests/fixtures/golden_*.json`: golden-master snapshots, generated once from the current code.

## 2. Change points (what gets tested, in priority order)

Priority is set by risk to money: wrong entry, stop, quantity or status means a wrong trade.

### P1: `scanner.evaluate` and the trade math (`tests/test_scanner_evaluate.py`)
Seam: `evaluate(..., votes=...)` takes precomputed votes, so tests can build a small frame by hand (201+ rows,
with columns Close/High/Low/ATR/TURNOVER20/RSI/ADX/Volume/VOL_AVG20/IDX_CLOSE/IDX_EMA200/RS) and choose the
votes exactly. Pin:
- `i < 200` returns None, and negative `i` is normalised.
- Liquidity: `Close < min_price` or `TURNOVER20 < min_turnover / BARS_PER_DAY[tf]` returns None (IN: 50 / 5e7; US: 5 / 2e7).
- Scoring counts distinct families, so 5 zone strategies count as one vote. A side wins only if its score is
  >= min_score and strictly greater than the other side's. Ties return None.
- `SELL` returns None while `ALLOW_SHORTS=False`, but SELL votes still veto BUY when the scores tie.
- `entry_type_for`: "limit" when pullback families >= momentum families (ties go to limit), otherwise "stop".
- Entry: limit = close - sign*0.5*ATR. Stop = High (BUY) / Low (SELL) + sign*0.1*ATR.
- Risk: default 1.5*ATR. A structural stop measured from **entry** (not close) is used only if it lies in
  [0.3, 3.0]*ATR, and the widest valid one wins. Invalid structural stops fall back to ATR.
- SL/targets: SL = entry - sign*risk. Targets = entry + sign*risk*{1.5, 3.0}, each rounded to 2 dp.
- Qty: `int(capital*1%//risk)`, capped at `int(capital//entry)`, floored at 0. A signal with qty 0 is still returned.
- Worked anchors (IN, BUY, Close=100, High=101, Low=99, ATR=2):
  limit gives entry 99.00, SL 96.00, T 103.50/108.00, qty 333. Stop gives entry 101.20, SL 98.20,
  T 105.70/110.20, qty 333. US capital 10000 gives qty 33.
- `passes_filters`: trend and rs veto by side, and NaN index data never blocks a signal.
- `Signal.key`: IN daily is `SYM|SIDE|YYYY-MM-DD` (legacy format). Other markets get a `US|` prefix,
  and non-1d timeframes get a `tf|` segment.
- Output fields: `ref_price` = close, plus rsi/adx/vol_ratio rounding.

### P2: `tracker` (`tests/test_tracker.py`)
- `fill_price`: limit/stop x BUY/SELL x {not reached, reached, gapped at the open}. Gaps fill at the open.
- `_walk` state machine, with hand-written bar tables:
  PENDING->CANCELLED after `ENTRY_VALID_BARS`(3) bars. On the fill bar only the SL is checked (SL exit = stop,
  or = fill if the bar gapped through the stop). SL before target when both are hit in the same bar.
  ACTIVE->T1_HIT moves the stop to `fill_price`, and a later stop hit closes as BREAKEVEN at the fill price.
  T2 is checked before T1. Daily EXPIRED after 20 held bars (the fill bar is not counted), exiting at that
  bar's close. "market" rows start ACTIVE with fill=entry.
  Intraday: the first bar of the next day cancels a PENDING order, or EXPIREs at the previous bar's close,
  with closed_at = the signal day. Intraday has no hold-bar limit. closed_at is a date string and filled_at
  is a full ISO timestamp.
- `_bars_after`: 1d filters by date > bar_date, intraday by timestamp > bar_time.
- `update_all` (with a fake `tracker.fetch_ohlcv`, a fake `telegram_bot.send_message` and a temp DB):
  if the status is unchanged only ltp is updated. A status change writes all `_walk` fields and counts 1.
  Telegram is called only when `notify and telegram=1`, and the message text (CANCELLED / P&L body,
  non-IN flag) is pinned. The market filter works. Missing or empty data skips the row. Re-running gives
  0 changes (the replay is idempotent).

### P3: `state` (`tests/test_state.py`)
- Fresh DB schema. **Migration**: a hand-built legacy table (original 10 columns, plus rows) gets
  every `_EXTRA_COLUMNS` column added, and the backfills apply: bar_date = last 10 chars of key,
  status ACTIVE, timeframe 1d, market IN, bar_time `date T00:00:00`, entry_type 'market', telegram 1.
- `record`: status PENDING for limit/stop and ACTIVE for market. ltp = ref_price or entry. `INSERT OR IGNORE`
  keeps the first row. The telegram flag is stored.
  `filter_new` drops keys that are already stored. `all_signals` filters by market and orders by sent_at
  DESC. `open_signals` returns OPEN_STATUSES only. `update_signal` sets updated_at.
- `sent_at` / `updated_at` come from `datetime.now()`. Assert their format, or patch `state.datetime`.

### P4: `scanner.scan` / `scan_and_alert` (`tests/test_scanner_scan.py`)
Fakes: `scanner.fetch_ohlcv`, `scanner.get_universe`, `telegram_bot.send_signals` / `send_message`.
- Validation errors: unknown strategies, timeframe or filters raise ValueError.
- Errors inside evaluate for one symbol are logged and that symbol is skipped. Results are sorted by
  (score, adx) descending. A missing index means filters are skipped.
- `scan_and_alert`: send=False records all new signals with telegram=0 and sent=0. send=True records only
  the delivered ones (failed sends are not recorded, so they re-alert next scan). Dedupe across calls.
  The return dict has a fixed shape.

### P5: `strategies`, `indicators`, `zones` (`tests/test_strategies.py`, `tests/test_indicators_zones.py`)
- Each of the 13 strategies, on small crafted frames where possible (crossovers inside LOOKBACK=3, breakout
  ADX/volume gates, squeeze run >= 5 bars, RSI2 thresholds, zone retest returns `(side, stop)`, S/R bounce).
- Registry: `DEFAULT_STRATEGIES` = all except `OPT_IN` {Squeeze Breakout, RSI(2) Reversion}. `FAMILIES`,
  `family_of`, and the `describe()` shape.
- Indicators: known-value checks (ema/sma/rsi edge `fillna(100)`, atr, supertrend direction). `add_all`
  adds the column set and NaN index columns when no index is given. attrs zones/pivots are populated.
- **Golden master**: `add_all` + `strategy_votes` + `evaluate` over every bar of 2-3 seeded synthetic series
  (uptrend, downtrend, range; ~600 bars, IN and US) is snapshotted to `tests/fixtures/golden_signals.json`.
  This catches any drift in the indicator, zone or strategy pipeline.

### P6: `backtest` (`tests/test_backtest.py`)
- `backtest_symbol` on a seeded series is snapshotted to a golden trade list. Results with `votes=vote_table(...)`
  must equal results without it (cache equivalence). Hand-built cases: no fill within 3 bars means no trade,
  fill-bar stop, gap-through fill, max_hold exit at close, one position at a time.
- `summarize`: empty input, the profit-factor inf branch, and the text layout. `run` with fake fetchers.

### P7: `web` Flask API (`tests/test_web.py`, using `app.test_client()`)
Fakes: `web.scan_and_alert`, `tracker.update_all`. Use a temp DB. Reset `_last_refresh` and the `_scan_locks` between tests.
- `/api/signals?market=IN|US|XX` (400 on an unknown market) returns the payload, config and `_enrich` maths
  (pnl 0 for PENDING/CANCELLED, risk fallback `or 1`, to_entry_pct).
- `/api/meta` shape. `/api/refresh` calls update_all(notify=True).
- `/api/scan`: default strategies = `DEFAULT_STRATEGIES` (not config.STRATEGIES). Filters default `[]`.
  Unsupported timeframe gives 400. min_score outside 1..families gives 400. A held lock gives 409. ValueError
  gives 400 and other exceptions give 500. `all` iterates the 5 timeframes and labels symbols `"X (tf)"`.
  send defaults to False.
- `/` serves index.html (smoke test only; the dashboard JS is out of scope).

### P8: `markets` + `main` scheduling (`tests/test_markets.py`, `tests/test_main.py`)
- `Market.is_open` / `is_after_close` with injected datetimes: weekday gate, inclusive session bounds
  (15:30 IN counts as open), weekends, `markets.get` (None gives IN, case-insensitive, unknown raises ValueError).
- `run_loop`: patch `main.time.sleep` to raise a sentinel after N iterations, patch `Market.now` to return
  scripted times, and patch `main.run_scan`, `tracker.update_all` and `telegram_bot.send_message`.
  Pin: the startup message, update+scan while open, nothing before the open, exactly one EOD scan per
  market per date (header message first), and that one market's exception does not stop the other.
- CLI `main()` via `sys.argv`: --dry-run prints `format_signal`, --test-telegram, the --symbols/--market
  rule (`p.error` exits with code 2), and --backtest writes its CSV to the (patched) `config.DATA_DIR`.

### P9: `telegram_bot`, `universe`, `data` (`tests/test_telegram.py`, `tests/test_universe_data.py`)
- `format_signal`: snapshot text for IN BUY limit, US stop, IN SELL (short note), market entry (no "Now" line),
  and HTML escaping of the symbol ("M&M").
- `send_message`: with no token it returns False without posting. With a fake `requests.post` it covers ok,
  error status, the 429 retry_after branch and the RequestException retries (sleep patched). `send_signals`
  keeps only delivered signals.
- `get_universe`: comma list, NSE/Wikipedia fakes, fallbacks on exceptions. `to_yahoo` mappings (`.NS`,
  `BRK.B->BRK-B`, `^` passthrough).
- `fetch_ohlcv` with a fake `yf.download`: MultiIndex and flat frames, tz conversion to naive exchange time,
  dropping frames with < 60 rows, batch errors, and missing tickers.

## 3. Impact analysis

- **Production code: no changes expected.** Every seam needed can be reached with `monkeypatch` on module
  attributes. These names are imported directly and must be patched where they are used:
  `scanner.fetch_ohlcv`, `scanner.get_universe`, `tracker.fetch_ohlcv`, `backtest.fetch_ohlcv`,
  `backtest.fetch_index`, `web.scan_and_alert`, `main.scan`, `main.scan_and_alert`, `main.run_scan`.
  `config.STATE_DB` is read when each connection opens, so patching it works.
  Step 3 (break dependencies) should only be needed if mapping finds a seam that cannot be reached. Any such
  edit must be minimal, behaviour-preserving and listed in the step-3 notes.
- Things that happen at import time and that tests must accept:
  - `config` runs `load_dotenv(.env)`, which loads the **real Telegram token and chat id**.
  - `config` runs `DATA_DIR.mkdir`, which does nothing because the folder exists.
  - `main` runs `logging.basicConfig`.
  - `web` creates the Flask app and the locks.
  No import triggers network traffic.
- Global mutable state touched by tests: `config.*` constants, `web._last_refresh`, `web._scan_locks`, and
  `main()` --symbols mutating `config.UNIVERSE` / `config.US_UNIVERSE`. Tests should change these only
  through `monkeypatch` so they are restored afterwards.
- New files only: `tests/`, `pytest.ini`, `requirements-dev.txt`. `.gitignore` already excludes `data/`.

## 4. Risk assessment

### 4.1 Safety risks (mitigated in `tests/conftest.py`, autouse)
| Risk | Mitigation |
|---|---|
| A real Telegram message is sent (the `.env` token is real) | Set `os.environ["TELEGRAM_BOT_TOKEN"]=""` / `TELEGRAM_CHAT_ID` **before** importing `algotrade` (load_dotenv does not override existing variables), and also `monkeypatch config.TELEGRAM_BOT_TOKEN=""`. Patch `requests.post`/`requests.get`/`requests.Session.request` to raise. |
| Network calls to Yahoo, NSE or Wikipedia | Patch `yfinance.download` to raise, plus a socket guard (`socket.socket.connect` raises). A test that needs data must inject a fake explicitly. |
| The live `data/signals.db` is read or migrated | At conftest import, point `config.STATE_DB` at a session temp file. Per test, an autouse fixture sets `tmp_path/"signals.db"`. A session-end check confirms the real DB's sha256 and mtime are unchanged. |
| `data/backtest_trades*.csv` is overwritten | Patch `config.DATA_DIR` to `tmp_path` in main/backtest tests. |
| `.env` overrides (STRATEGIES, FILTERS, ALLOW_SHORTS, MIN_SCORE, capital) change test results | An autouse fixture pins config to known defaults: MIN_SCORE=2, ALLOW_SHORTS=False, STRATEGIES=[], FILTERS=[], CAPITAL=100000, US_CAPITAL=10000, RISK 1.0, TIMEFRAME 1d. `markets.MARKETS` captured capital at import, so tests build expectations from `mkt.capital`. |
| Slow or hanging tests (`time.sleep` in send_signals and run_loop, the infinite `while True`) | Patch `telegram_bot.time.sleep` and `main.time.sleep`, and use a sentinel exception to leave the loop. |

### 4.2 Correctness risks
- **Tests that assert the "intended" behaviour instead of the current behaviour.** Mitigation: for each
  assertion, run it against the unmodified code first. Golden files are generated from the current code
  and never edited by hand.
- **Golden-master brittleness**: pandas/numpy upgrades can shift floats. Values are already rounded to 2 dp.
  Compare with `pytest.approx(abs=1e-6)` where needed, and record the library versions in the golden file
  header.
- **Wall-clock dependence**: `datetime.now()` in state, and `Market.now()`. Inject or patch them, and never
  assert actual dates.
- **pandas `df.attrs`** (zones/pivots) must survive in hand-built frames. Always build frames through
  `add_all`, or call `zones.annotate` explicitly.

### 4.3 Quirks to PIN, not fix (document each one in the test name or docstring)
1. Daily scans during market hours evaluate the **still-forming** daily bar (data includes today's partial candle).
2. **Market holidays are not handled**: `is_open` / `is_after_close` only check weekday and session time.
3. Pre-pending-entry DB rows migrate to `entry_type='market'`, `telegram=1`, `status='ACTIVE'`.
4. **Zone SELL strategies (RBD/DBD) never trade while ALLOW_SHORTS=false**, but SELL votes still veto a tied BUY.
5. Dashboard `/api/scan` defaults to `DEFAULT_STRATEGIES` and `filters=[]`, so it **ignores** `.env`
   STRATEGIES/FILTERS. The CLI and loop use config.
6. `backtest_symbol` does not pass `timeframe` to `evaluate`. Intraday backtests therefore use the **daily**
   turnover threshold and a 1d key. The backtest uses a single target (`target_idx`) with no T1/breakeven
   stage and no intraday day-end exits, so its results differ from the tracker's.
7. Structural stop distance is measured from the pending **entry**, not the close.
8. Score ties (including 0-0) never signal. A signal can have `qty == 0`.
9. With `send=True`, signals whose Telegram send failed are **not recorded**, so they re-alert next scan.
10. Intraday expiry/cancel only triggers when a bar from the **next day** arrives. After the close a
    position stays open until then.
11. `main.run_loop`: tracking runs only while the market is open. The EOD scan runs at the first loop after
    close (including on startup after close). The EOD "done" flag is in memory, so a restart re-sends the
    EOD header. The EOD scan uses `config.TIMEFRAME` even if it is intraday. One shared sleep covers both markets.
12. `vol_ratio` becomes NaN (not 0.0) when VOL_AVG20 is NaN, because NaN is truthy.
13. `closed_at` is a date only, while `filled_at` is a full timestamp.
14. `update_all` fetches only `TRACK_PERIOD` of history (1d: 3mo), so very old open rows replay on truncated data.
15. Session bounds are inclusive (`<=`): 15:30:00 IN / 16:00:00 US count as open.

### 4.4 How we know something broke
After this step, any behaviour change in the covered functions fails a named test or a golden diff.
Remaining blind spots: `static/index.html` JS, `web.serve`/`_background` thread, real yfinance response
formats, and real Telegram API behaviour. These stay manual smoke checks.

## 5. Success criteria

1. `.venv\Scripts\python.exe -m pip install -r requirements-dev.txt`, then `.venv\Scripts\python.exe -m pytest`, passes green.
2. It passes with networking disabled (socket guard on). Zero Telegram, Yahoo, NSE or Wikipedia calls.
3. `data/signals.db` (and everything under `data/`) is byte-identical before and after the run (sha256 check).
4. The full suite takes under ~60 s, and repeated runs give identical results (no flakiness, no clock dependence).
5. Every function listed in section 2 (P1-P9) has at least one test. P1-P3 money logic covers every branch
   listed above, and every quirk in 4.3 has a pinning test.
6. **Zero diffs** to `algotrade/*.py`, `main.py`, `requirements.txt` and `static/` (step 3 edits, if any, are
   listed and shown to be behaviour-preserving).
7. Mutation sanity check (manual, then reverted): flipping one of `PULLBACK_ATR`, `ATR_SL_MULT`, the
   `>=` in the tie rule, or `ENTRY_VALID_BARS` makes at least one test fail.

## 6. Next step

Run **`brownfield-4-cover`**. Build the harness first (`pytest.ini`, `requirements-dev.txt`, `tests/conftest.py`,
`tests/test_harness.py`) as described in `plan/seams-and-dependencies.md` sections 6.1-6.5, then follow P1-P9 in order.

## 7. Step 2/3 amendments (2026-10-14)

Details and evidence: `plan/seams-and-dependencies.md`.

**Patch points (amends section 3)**
- Add `main.get_universe` to the patch list (`main.py:20,73`). `main.run_backtest` imports `backtest` lazily, so
  patch `backtest.run` on the module object (`monkeypatch.setattr(backtest, "run", fake)`).
- `backtest.fetch_index` is the scanner's function and still calls `scanner.fetch_ohlcv`. Patch
  `backtest.fetch_index` or `scanner.fetch_ohlcv` as well as `backtest.fetch_ohlcv`.
- `time` is one shared module (`main.time is telegram_bot.time is web.time`). Do not patch `time.sleep` globally.
  Replace the module reference in the one module under test instead:
  `monkeypatch.setattr(main, "time", SimpleNamespace(sleep=...))`. Do the same for `telegram_bot` (sleep) and `web`
  (strftime).
- `Market` is a frozen dataclass, so patch `Market.now` on the **class**:
  `monkeypatch.setattr(markets.Market, "now", ...)`. A frozen instance cannot be patched.

**Safety harness (amends section 4.1)**
- yfinance 1.7 downloads through curl_cffi, which bypasses the socket guard. conftest must patch
  `yfinance.download` (and may also patch `curl_cffi.requests.Session.request`). The socket guard plus
  `requests.Session.request` still covers `requests` (Telegram, NSE, Wikipedia).
- Before any `algotrade` import, conftest:
  - makes `dotenv.load_dotenv` a no-op, so `.env` is never read;
  - pops the config-related environment variables (UNIVERSE, SCAN_INTERVAL_MIN, TIMEFRAME, MIN_SCORE, CAPITAL,
    RISK_PER_TRADE_PCT, ALLOW_SHORTS, STRATEGIES, FILTERS, US_UNIVERSE, US_CAPITAL);
  - blanks `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- `markets.MARKETS` freezes capital, min price and min turnover at import, so patching `config.CAPITAL` has no
  effect. Tests that need other market values use
  `monkeypatch.setitem(markets.MARKETS, code, dataclasses.replace(markets.MARKETS[code], ...))`.

**Test construction**
- P1: tests that pass `votes=` must also pass a matching `strategies=` list. Otherwise `evaluate` looks up every
  default strategy name in the votes dict and raises KeyError.
- P5: synthetic data builders must deliberately inject base patterns (explosive leg-in, 1-4 small-body bars,
  explosive leg-out of at least 1.1x ATR, then a retest) and repeated swing levels. Seeded random walks
  produced zero zones.

**Success criterion 3 (replaces section 5 item 3)**
A live dashboard server rewrites `data/signals.db` every 300 s, so a sha256 check cannot prove isolation for
that file. Isolation is proven by:
- an autouse fixture that points `config.STATE_DB` / `config.DATA_DIR` at `tmp_path` for every test;
- a conftest guard that fails the run if `sqlite3.connect` is called on any path under the real `data/` folder.

sha256 before/after checks apply only to `data/backtest_trades.csv`, `data/backtest_trades_US.csv` and
`data/signals_backup_2026-10-13.db`, which the server does not write. Baseline hashes are in
`plan/data_sha256_before.txt`.

**Step 3 outcome: no dependency breaking needed. Zero source edits.**
- Re-checked independently: `algotrade/*.py` and `main.py` are identical to `plan/source_snapshot/`.
- A guarded probe (dotenv off, sockets blocked, sqlite on `data/` blocked) confirmed:
  - the Telegram token is empty after import, and `MARKETS` holds the code defaults (100000 / 10000);
  - the class-level `Market.now` patch works;
  - `run_loop` exits through a sentinel raised by `main.time = SimpleNamespace(sleep=...)`, and the global
    `time.sleep` stays untouched;
  - `state` works on a temp DB;
  - `evaluate(votes=...)` without `strategies=` raises KeyError;
  - `curl_cffi.requests.Session.request` exists, so it can be patched.
- Success criterion 6 (zero source diffs) still holds.

## 8. Step 4 + 7 outcome (2026-10-14)

**Delivered (new files only):** `requirements-dev.txt` (adds pytest and pytest-cov), `pytest.ini`,
`tests/conftest.py`, 13 test modules, `tests/golden/*.json` (4 golden files; the plan said `tests/fixtures/`),
`plan/quirks.md`, and a "Tests" section in `README.md`.

**Final verification (step 7):**
| Criterion | Result |
|---|---|
| 1. Suite green | 377 passed, 0 skipped/xfail. Two consecutive runs: 11.38 s and 11.28 s, with identical results. |
| 2. Offline | Socket, `requests.Session.request`, `yfinance.download` and `curl_cffi` guards are autouse. `.env` is never read and the Telegram token is blank. Each guard is proven by `tests/test_harness.py`. |
| 3 (amended). Isolation | Per-test `config.STATE_DB` / `DATA_DIR` point at `tmp_path`, and a `sqlite3.connect` guard blocks the real `data/` folder (`test_harness.py::test_real_db_guard_trips`). The sha256 of `backtest_trades.csv`, `backtest_trades_US.csv` and `signals_backup_2026-10-13.db` matches `plan/data_sha256_before.txt` after all runs. |
| 4. Speed / determinism | About 11.3 s, far under the 60 s budget. Seeded data and patched clocks. |
| 5. Coverage | 99% line coverage of `algotrade` + `main` (1050 statements, 8 missed). The misses are `web._background`/`web.serve`, `main`'s `__main__` guard and one `zones` height-guard `continue`. Every section 4.3 quirk has a pinning test (`plan/quirks.md`). |
| 6. Zero source diffs | `diff -r` of `algotrade/`, `main.py`, `run.bat` and `requirements.txt` against `plan/source_snapshot/`: identical. |
| 7. Mutation sanity | Done in memory through a throwaway pytest plugin, with no source edit. `PULLBACK_ATR` 0.5→0.6: 24 failures. `ATR_SL_MULT` 1.5→1.6: 23 failures. `ENTRY_VALID_BARS` 3→4: 11 failures. The `>=` tie rule cannot be mutated without editing source. It is covered by `test_score_tie_returns_none` and `test_sell_votes_veto_tied_buy_even_with_shorts_disabled`, which assert `None` on exact ties. |

**Known harness nit:** `conftest.py` creates a session temp dir (`%TEMP%\algotrade-tests-*`, empty) on every run
and never removes it.

**Next:** pick fixes from `plan/quirks.md` as separate change requests. Each one is a deliberate behaviour change that
must update its pinning test.

## Change 2: fix three suspected bugs (2026-10-14)

Steps 1-4 for this change are covered by the existing suite (377 tests) and the maps in
`plan/seams-and-dependencies.md`. Each fix is TDD: rename and flip the pinning test, add edge-case tests, then change the code.

| Fix | Quirk | Change point | New behaviour |
|---|---|---|---|
| A | B3 | `algotrade/data.py::fetch_ohlcv` | A flat (non-MultiIndex) frame is accepted only for a 1-ticker batch. For a multi-ticker batch, `log.error` (naming the first symbols) and skip the whole batch. |
| B | 1 | `algotrade/scanner.py::scan`, plus a sprouted `completed_bars(df, timeframe, market, now=None)` | Live scans drop a last bar that is still forming. For 1d bars, that means the bar's date is the exchange's today and the time is not after the close. For intraday bars, it means bar start + interval > now (naive exchange time). Tracker and backtest are not changed. The index series needs no trim, because it is reindexed onto the stock frame. |
| C | B9 | `main.py::run_loop` | The EOD header is sent at most once per market per exchange date. A failed EOD scan is retried on later loops without re-sending the header. Once the scan succeeds, no further EOD scan runs that date. |

**Success criteria**
1. The pinning tests are renamed and assert the new behaviour. New tests cover the edge cases: flat frame with 1 vs many tickers; daily bars before the open, during the session, at the close, after the close and on a weekend with a Friday bar; intraday bars just completed vs in progress; EOD fail-then-succeed, fail twice and header count.
2. No other existing assertion changes. The golden files stay byte-identical.
3. The full suite passes on two runs. `plan/quirks.md` marks 1, B3 and B9 as FIXED. The README notes that scans ignore the still-forming bar.

**Change 2 status: COMPLETE (2026-10-14, step 7).**

| Criterion | Result |
|---|---|
| 1. Pinning tests renamed and edge cases added | Done (step 5). Suite grew from 377 to 424 tests. |
| 2. No other assertion changes; golden files unchanged | The 4 files in `tests/golden/` predate the change (mtime 12:49:59, source edits from 13:03) and their golden tests pass. sha256: `backtest_trades.json` 80005b83..., `pipeline_down_IN.json` 84400316..., `pipeline_range_US.json` e6cf2d21..., `pipeline_up_IN.json` f0235518... |
| 3. Suite green twice | 424 passed in 14.11 s and 13.73 s. No skips or xfails. Coverage: 99% (1074 statements, 8 missed, the same uncovered lines as before; `data.py`, `scanner.py` 100%, `main.py` 99%). |
| Source diff | `diff -r` against `plan/source_snapshot/`: only `algotrade/data.py`, `algotrade/scanner.py` and `main.py` differ (plus `README.md`, which is docs). Line endings are preserved (CRLF, CRLF, LF). |
| Data files | sha256 of `backtest_trades.csv`, `backtest_trades_US.csv` and `signals_backup_2026-10-13.db` matches `plan/data_sha256_before.txt`. |
| Offline smoke | Real `scanner.scan(market="IN", timeframe="1d")` on a 300-bar fake frame whose last bar is dated 2026-10-14, with the network blocked and a temp `STATE_DB`. At 11:00 IST it evaluated 299 bars ending 2026-10-13 (with `min_score=1`, one BUY signal, `bar_time` 2026-10-13, key `SMOKE\|BUY\|2026-10-13`). At 16:00 IST it evaluated all 300 bars ending 2026-10-14. |

Follow-ups found during verification (recommendations only, not fixed) are listed in `plan/quirks.md` section C.
