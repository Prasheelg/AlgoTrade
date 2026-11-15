"""NR7 Breakout on Nifty, Bank Nifty and Sensex (Toby Crabel), run on its own, apart from the vote strategies.
Alerts only: nothing here places an order.

- Evening scan (scan(), once a day after SCAN_AFTER IST): when a session's high-low range is the narrowest of the
  last NR_DAYS, the index gets a setup for the next session: buy above the NR7 high, sell below its low.
- Next session (check(), every few seconds from the dashboard, on Yahoo's 1-minute index bars): the first level
  traded is the trade and the other is cancelled. Stop at the NR7 bar's other end; 1R is entry to stop (the NR7
  range, more after a gap past the level), target TARGET_R from the entry, closed at EXIT_AT on the HOLD_DAYS-th
  session (entry day = 1). Not triggered in that session: expired. One trade per index (an NR7 on the day a trade
  closed is skipped too); a stopped trade is not reversed. Muhurat and short weekend sessions don't count.
- The rules follow NR7_Breakout_Playbook.html; its backtest's trades are matched trade for trade where the data
  agree (Yahoo here, Upstox there).
- backtest() runs the same rules on daily candles the playbook's conservative way: a bar that trades both levels
  is a stop-out, the target is never reached on the entry day, a gap past a level or the stop fills at the open."""
import html
import json
import logging
import math
from datetime import date, datetime, time
from time import monotonic

import pandas as pd

from . import config, markets, state, telegram_bot

log = logging.getLogger(__name__)
# index -> (name, Yahoo ticker, futures lot size when Kotak's contract list isn't available)
INDICES = {"NIFTY": ("Nifty 50", "^NSEI", 65), "BANKNIFTY": ("Bank Nifty", "^NSEBANK", 30),
           "SENSEX": ("Sensex", "^BSESN", 20)}
COST_PCT = {"NIFTY": 0.017, "BANKNIFTY": 0.022, "SENSEX": 0.015}   # round trip, % of the index (backtest)
NR_DAYS = 7
TARGET_R = 3.0
HOLD_DAYS = 5
EXIT_AT = time(15, 15)       # time exit on the last day
SESSION_END = time(15, 30)
SCAN_AFTER = time(16, 0)     # the day's candle is final by then
SCAN_GIVE_UP = time(21, 0)   # no candle for today by then: a holiday
POLL_SEC = 30                # minute bars are fetched this often in market hours
IDLE_POLL_SEC = 900          # and this often outside them (catching up after the PC was off)
SETTINGS_KEY = "nr7_settings"
LAST_SCAN_KEY = "nr7_last_scan"
BACKTEST_FILE = config.DATA_DIR / "nr7_backtest.json"
OPEN, WAIT = "OPEN", "WAIT"
ACTIVE = (WAIT, OPEN)
CLOSED = ("STOP", "TARGET", "TIME")

_COLUMNS = ("key TEXT PRIMARY KEY, idx TEXT, nr7_date TEXT, trigger_day TEXT, high REAL, low REAL, risk REAL, "
            "lots INTEGER, lot INTEGER, status TEXT, side TEXT, entry REAL, entry_at TEXT, stop REAL, target REAL, "
            "day_no INTEGER, exit REAL, exit_at TEXT, r REAL, note TEXT, last_bar TEXT, created_at TEXT, "
            "updated_at TEXT")
_fetched: dict[str, float] = {}


def _conn():
    con = state._conn()
    con.execute(f"CREATE TABLE IF NOT EXISTS nr7_setups ({_COLUMNS})")
    return con


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- settings ---------------------------------------------------------------------------------------

def settings() -> dict:
    """{on, send, trade, skip_days}: on = scan and watch; send = Telegram; trade = real orders (nr7_auto.py, with
    the dashboard's Auto-trade switch on too); skip_days = event days (YYYY-MM-DD) with no new trade (Budget day
    and the day before, election results...)."""
    saved = state.get_setting(SETTINGS_KEY, {})[0] or {}
    return {"on": bool(saved.get("on", True)), "send": bool(saved.get("send", False)),
            "trade": bool(saved.get("trade", False)), "skip_days": list(saved.get("skip_days", []))}


def save_settings(body: dict) -> dict:
    s = settings()
    for k in ("on", "send", "trade"):
        if k in body:
            if not isinstance(body[k], bool):
                raise ValueError(f"{k}: true or false")
            s[k] = body[k]
    if "skip_days" in body:
        days = body["skip_days"]
        if isinstance(days, str):
            days = [d for d in days.replace(",", " ").split() if d]
        try:
            s["skip_days"] = sorted({date.fromisoformat(str(d)).isoformat() for d in days})
        except ValueError:
            raise ValueError("Skip days: dates as YYYY-MM-DD")
    state.set_setting(SETTINGS_KEY, s)
    return s


# ---- the pattern and the trade rules (pure) -----------------------------------------------------------

def is_nr7(df: pd.DataFrame) -> bool:
    """The last bar's range is the narrowest of the last NR_DAYS (ties count)."""
    if len(df) < NR_DAYS:
        return False
    rng = (df["High"] - df["Low"]).iloc[-NR_DAYS:]
    return bool(rng.iloc[-1] > 0 and rng.iloc[-1] <= rng.min())


def new_setup(idx: str, bar_date: date, high: float, low: float) -> dict:
    return {"key": f"{idx}|{bar_date}", "idx": idx, "nr7_date": str(bar_date), "trigger_day": None,
            "high": round(float(high), 2), "low": round(float(low), 2), "risk": round(float(high - low), 2),
            "status": WAIT, "side": None, "entry": None, "entry_at": None, "stop": None, "target": None,
            "day_no": None, "exit": None, "exit_at": None, "r": None, "note": None, "last_bar": None}


def trade_risk(t: dict) -> float:
    """1R of an entered trade: entry to stop. The NR7 range, unless the entry gapped past the level (the playbook
    measures R and the target from the actual fill, so a gap entry risks more points but still about 1R)."""
    return abs(t["entry"] - t["stop"])


def _close(t: dict, px: float, how: str, ts) -> None:
    sign = 1 if t["side"] == "LONG" else -1
    t.update(status=how, exit=round(float(px), 2), exit_at=ts.isoformat(),
             r=round(sign * (px - t["entry"]) / trade_risk(t), 2))


def advance(t: dict, bars: pd.DataFrame, daily: bool = False, force_side: str | None = None) -> dict:
    """Walk a WAIT or OPEN setup through bars (Open/High/Low/Close, naive IST index, in order) and return it
    updated. Daily bars: the time exit is the close of day HOLD_DAYS; minute bars: the first bar from EXIT_AT.
    force_side ("LONG" / "SHORT", research): only that level can trigger the trade."""
    t = dict(t)
    for ts, o, h, l, c in zip(bars.index, bars["Open"], bars["High"], bars["Low"], bars["Close"]):
        d = ts.date()
        if t["status"] not in ACTIVE:
            break
        if str(d) <= t["nr7_date"]:
            continue
        prev_day = (t["last_bar"] or "")[:10]
        t["last_bar"] = ts.isoformat()
        if t["status"] == WAIT:
            if t["trigger_day"] is None:
                t["trigger_day"] = str(d)            # the next session, whatever the calendar says
            elif str(d) > t["trigger_day"]:
                t.update(status="EXPIRED", note="Neither level traded")
                break
            up, down = h > t["high"], l < t["low"]
            if force_side:
                up, down = (up, False) if force_side == "LONG" else (False, down)
            if not (up or down):
                continue
            if up and down and t["low"] <= o <= t["high"]:   # both in one bar: assume the nearer one came first
                up = t["high"] - o <= o - t["low"]
            long_ = up
            entry = max(o, t["high"]) if long_ else min(o, t["low"])   # a gap past the level fills at the open
            sign, stop = (1, t["low"]) if long_ else (-1, t["high"])
            t.update(status=OPEN, side="LONG" if long_ else "SHORT", entry=round(float(entry), 2),
                     entry_at=ts.isoformat(), stop=stop,
                     target=round(float(entry + sign * TARGET_R * abs(entry - stop)), 2), day_no=1)
            if (l <= t["stop"]) if long_ else (h >= t["stop"]):   # the stop traded in the entry bar too
                _close(t, t["stop"], "STOP", ts)
            continue
        # OPEN (the entry bar never reaches the target: the order of moves inside a bar is unknown)
        if str(d) != prev_day:
            t["day_no"] += 1
        long_ = t["side"] == "LONG"
        if (o <= t["stop"]) if long_ else (o >= t["stop"]):
            _close(t, o, "STOP", ts)
        elif (o >= t["target"]) if long_ else (o <= t["target"]):
            _close(t, o, "TARGET", ts)
        elif (l <= t["stop"]) if long_ else (h >= t["stop"]):
            _close(t, t["stop"], "STOP", ts)
        elif (h >= t["target"]) if long_ else (l <= t["target"]):
            _close(t, t["target"], "TARGET", ts)
        elif t["day_no"] >= HOLD_DAYS:
            if daily:
                _close(t, c, "TIME", ts)
            elif ts.time() >= EXIT_AT:
                _close(t, o, "TIME", ts)
    return t


def lots_for(idx: str, risk: float) -> tuple[int, int]:
    """(lots, lot size): futures lots that risk at most RISK_PER_TRADE_PCT of CAPITAL at the NR7 range."""
    lot = _lot(idx)
    budget = config.CAPITAL * config.RISK_PER_TRADE_PCT / 100
    return (math.floor(budget / (risk * lot)) if risk > 0 else 0), lot


def _lot(idx: str) -> int:
    """Lot size from Kotak's F&O contract list (NSE indices), else INDICES' value."""
    try:
        from . import fno, kotak
        if kotak.enabled():
            lots = {u["name"]: u["lot"] for u in fno.underlyings() if u["kind"] == "index"}
            if lots.get(idx):
                return int(lots[idx])
    except Exception as exc:
        log.debug("NR7: lot size for %s from Kotak failed (%s)", idx, exc)
    return INDICES[idx][2]


# ---- storage ----------------------------------------------------------------------------------------

def setups(limit: int = 200) -> list[dict]:
    with _conn() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM nr7_setups ORDER BY nr7_date DESC, idx LIMIT ?", (limit,))]


def _insert(t: dict) -> None:
    now = _now()
    row = {**t, "created_at": now, "updated_at": now}
    cols = [c.split()[0] for c in _COLUMNS.split(", ")]
    with _conn() as con:
        con.execute(f"INSERT OR IGNORE INTO nr7_setups ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                    [row.get(c) for c in cols])


def _update(t: dict, changed: dict) -> None:
    changed = {**changed, "updated_at": _now()}
    with _conn() as con:
        con.execute(f"UPDATE nr7_setups SET {', '.join(f'{k} = ?' for k in changed)} WHERE key = ?",
                    (*changed.values(), t["key"]))


# ---- alerts -----------------------------------------------------------------------------------------

def _pts(x) -> str:
    return f"{x:,.2f}"


def _plans_text(t: dict) -> str:
    """The futures / option lines for a setup (or an open trade's side); empty when they can't be had."""
    from . import nr7_instruments
    try:
        return nr7_instruments.text(nr7_instruments.plans(t), t["side"] if t["status"] == OPEN else None)
    except Exception:
        log.exception("NR7 plan text for %s failed", t["key"])
        return ""


def _notify(text: str, t: dict | None = None) -> None:
    """Post to Telegram when Send is on; with a WAIT or OPEN setup t, its futures / option lines are added."""
    if not settings()["send"]:
        return
    if t is not None and t["status"] in ACTIVE:
        extra = _plans_text(t)
        text += f"\n{extra}" if extra else ""
    try:
        telegram_bot.send_message(text)
    except Exception:
        log.exception("NR7 Telegram alert failed")


def _setup_text(t: dict) -> str:
    name = INDICES[t["idx"]][0]
    if t["status"] == "SKIPPED":
        return f"📐 <b>NR7 {name}</b> ({t['nr7_date']}) · skipped: {html.escape(t['note'] or '')}"
    r = t["risk"]
    return (f"📐 <b>NR7 {name}</b> ({t['nr7_date']}) · next session only\n"
            f"Buy above {_pts(t['high'])} · stop {_pts(t['low'])} · target {_pts(t['high'] + TARGET_R * r)}\n"
            f"Sell below {_pts(t['low'])} · stop {_pts(t['high'])} · target {_pts(t['low'] - TARGET_R * r)}\n"
            f"Risk {_pts(r)} pts · {t['lots']} lot{'' if t['lots'] == 1 else 's'} of {t['lot']}"
            + ("" if t["lots"] else f" (one lot risks ₹{r * t['lot']:,.0f}, over your risk per trade)"))


def _event_text(t: dict) -> str | None:
    name = INDICES[t["idx"]][0]
    if t["status"] == OPEN:
        return (f"📐 <b>NR7 {name} {t['side']}</b> at {_pts(t['entry'])} · stop {_pts(t['stop'])} · "
                f"target {_pts(t['target'])} · close by {EXIT_AT:%H:%M} on day {HOLD_DAYS}")
    if t["status"] in CLOSED:
        how = {"STOP": "stopped out", "TARGET": "target hit", "TIME": f"closed on day {HOLD_DAYS}"}[t["status"]]
        return f"📐 <b>NR7 {name} {t['side']}</b> {how} at {_pts(t['exit'])} · {t['r']:+.2f}R"
    if t["status"] == "EXPIRED":
        return f"📐 <b>NR7 {name}</b> ({t['nr7_date']}) expired: {html.escape(t['note'] or '')}"
    return None


# ---- evening scan and the live watch ------------------------------------------------------------------

# Diwali Muhurat sessions: an hour of trading, so a fake narrow day (the playbook leaves them out). Yahoo carries
# the weekday ones; weekend ones are listed in case it ever does.
MUHURAT = {"2000-10-26", "2001-11-14", "2002-11-04", "2003-10-25", "2004-11-12", "2005-11-01", "2006-10-21",
           "2007-11-09", "2008-10-28", "2009-10-17", "2010-11-05", "2011-10-26", "2012-11-13", "2013-11-03",
           "2014-10-23", "2015-11-11", "2016-10-30", "2017-10-19", "2018-11-07", "2019-10-27", "2020-11-14",
           "2021-11-04", "2022-10-24", "2023-11-12", "2024-11-01", "2025-10-21", "2026-11-08"}
# Weekend sessions that were full trading days (Union Budget); any other weekend bar is a short special or test
# session. Add a future full weekend session here.
FULL_WEEKEND_SESSIONS = {"2015-02-28", "2020-02-01", "2025-02-01", "2026-02-01"}


def is_session_day(d: date) -> bool:
    """A real full trading day: a weekday or a listed full weekend session, and not Muhurat."""
    return str(d) not in MUHURAT and (d.weekday() < 5 or str(d) in FULL_WEEKEND_SESSIONS)


def _real_days(df: pd.DataFrame):
    day = df.index.strftime("%Y-%m-%d")
    return ~day.isin(MUHURAT) & ((df.index.dayofweek < 5) | day.isin(FULL_WEEKEND_SESSIONS))


def sessions(df: pd.DataFrame) -> pd.DataFrame:
    """Daily candles of real full sessions only, as in the playbook: no Muhurat, no short weekend specials, no
    close-only records."""
    return df[_real_days(df) & (df["High"] > df["Low"])]


def _daily(period: str = "1y") -> dict[str, pd.DataFrame]:
    from .data import fetch_ohlcv
    tickers = [y for _, y, _ in INDICES.values()]
    data = fetch_ohlcv(tickers, "1d", period=period, market="IN")
    return {idx: sessions(data[y]) for idx, (_, y, _) in INDICES.items() if data.get(y) is not None}


def scan(now: datetime | None = None, force: bool = False) -> list[dict]:
    """New setups from the latest completed session; once a day after SCAN_AFTER unless forced (the dashboard's
    Scan now). On a trading day whose previous session was never scanned (the PC was off that evening) it catches
    up at once, so the setup is there for this session. Returns the setups created."""
    s = settings()
    now = now or markets.get("IN").now()
    today = now.date()
    prev = str((pd.Timestamp(today) - pd.offsets.BDay(1)).date())
    last = state.get_setting(LAST_SCAN_KEY, None)[0]
    catch_up = False
    if not force:
        if not s["on"] or not is_session_day(today):
            return []
        catch_up = now.time() < SCAN_AFTER and (last is None or last < prev)
        if not catch_up and (now.time() < SCAN_AFTER or last == str(today)):
            return []
    data = _daily()
    if not data:
        log.warning("NR7 scan: no index data")
        return []
    if (not force and not catch_up and all(df.index[-1].date() < today for df in data.values())
            and now.time() < SCAN_GIVE_UP):
        return []   # today's candles aren't in yet: try again on the next loop
    rows = setups()
    active = {t["idx"] for t in rows if t["status"] in ACTIVE}
    made = []
    for idx, df in data.items():
        if catch_up or (is_session_day(today) and now.time() <= SESSION_END):
            df = df[df.index.date < today]         # today's session isn't complete
        if not is_nr7(df):
            continue
        bar = df.iloc[-1]
        t = new_setup(idx, df.index[-1].date(), bar["High"], bar["Low"])
        t["lots"], t["lot"] = lots_for(idx, t["risk"])
        nxt = pd.Timestamp(df.index[-1]) + pd.offsets.BDay(1)
        # one trade per index: as in the playbook's backtest, a trade that closed on the NR7 day itself still counts
        closed_that_day = any(x["idx"] == idx and x["status"] in CLOSED and (x["exit_at"] or "")[:10] >= t["nr7_date"]
                              for x in rows)
        # and the price history: a trade the playbook would hold although the app missed it (the PC was off)
        replay = next((x for x in _walk(df, idx, skip_days=set(s["skip_days"])) if x["nr7_date"] == t["nr7_date"]), None)
        if idx in active:
            t.update(status="SKIPPED", note="already a setup or trade on this index")
        elif closed_that_day:
            t.update(status="SKIPPED", note="a trade on this index closed that day")
        elif str(nxt.date()) in s["skip_days"]:
            t.update(status="SKIPPED", note=f"{nxt.date()} is a skip day")
        elif replay and replay["status"] == "SKIPPED":
            t.update(status="SKIPPED", note=f"by the price history, {replay['note']}")
        with _conn() as con:
            if con.execute("SELECT 1 FROM nr7_setups WHERE key = ?", (t["key"],)).fetchone():
                continue
        _insert(t)
        made.append(t)
        log.info("NR7 %s on %s: %s", idx, t["nr7_date"], t["status"])
        _notify(_setup_text(t), t)
    if catch_up:
        state.set_setting(LAST_SCAN_KEY, prev)
    elif not force:
        state.set_setting(LAST_SCAN_KEY, str(today))
    return made


def _minute_bars(idx: str) -> pd.DataFrame | None:
    from .data import fetch_ohlcv
    y = INDICES[idx][1]
    df = fetch_ohlcv([y], "1m", period="7d", market="IN").get(y)
    return None if df is None else df[_real_days(df)]   # a Muhurat hour is no trading day for a setup or a trade


def check(now: datetime | None = None, bars_for=None) -> int:
    """Move the WAIT / OPEN setups on through the minute bars since they were last looked at: entries, stops,
    targets, time exits and expiries, each posted once. Fetches at most every POLL_SEC in market hours
    (IDLE_POLL_SEC outside them). Returns how many setups changed."""
    if not settings()["on"]:
        return 0
    india = markets.get("IN")
    now = now or india.now()
    live = [t for t in setups() if t["status"] in ACTIVE]
    if not live:
        return 0
    wait = POLL_SEC if india.is_open(now) or (now.weekday() < 5 and now.time() <= SCAN_AFTER) else IDLE_POLL_SEC
    bars_for = bars_for or _minute_bars
    changed = 0
    for idx in sorted({t["idx"] for t in live}):
        bars = None
        if bars_for is not _minute_bars or monotonic() - _fetched.get(idx, -1e9) >= wait:
            _fetched[idx] = monotonic()
            try:
                bars = bars_for(idx)
            except Exception:
                log.exception("NR7: minute bars for %s failed", idx)
        if bars is not None and len(bars):
            naive = now.replace(tzinfo=None)
            bars = bars[bars.index + pd.Timedelta(minutes=1) <= naive]   # the current minute isn't finished
        for t in (x for x in live if x["idx"] == idx):
            new = bars[bars.index > pd.Timestamp(t["last_bar"])] if bars is not None and t["last_bar"] else bars
            u = advance(t, new) if new is not None and len(new) else dict(t)
            if u["status"] == WAIT and u["trigger_day"] and (
                    u["trigger_day"] < str(now.date()) or (u["trigger_day"] == str(now.date()) and now.time() > SESSION_END)):
                u.update(status="EXPIRED", note="Neither level traded")
            diff = {k: v for k, v in u.items() if t.get(k) != v}
            if not diff:
                continue
            _update(t, diff)
            changed += 1
            if "status" in diff:
                if diff["status"] in CLOSED and t["status"] == WAIT:   # in and out between two looks
                    _notify(_event_text({**u, "status": OPEN}))
                text = _event_text(u)
                if text:
                    _notify(text, u)
    return changed


def view() -> dict:
    s = settings()
    bt = None
    if BACKTEST_FILE.exists():
        try:
            bt = json.loads(BACKTEST_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.exception("NR7 backtest file unreadable")
    from . import nr7_instruments
    rows = setups()
    for t in rows:   # what to trade: futures and options for setups waiting or trades open
        if t["status"] in ACTIVE:
            t["plans"] = nr7_instruments.plans(t)
    return {**s, "setups": rows, "indices": {k: v[0] for k, v in INDICES.items()},
            "budget": round(nr7_instruments.budget()),
            "last_scan": state.get_setting(LAST_SCAN_KEY, None)[0], "backtest": bt,
            "rules": {"nr_days": NR_DAYS, "target_r": TARGET_R, "hold_days": HOLD_DAYS,
                      "exit_at": EXIT_AT.strftime("%H:%M"), "scan_after": SCAN_AFTER.strftime("%H:%M")}}


# ---- backtest ---------------------------------------------------------------------------------------

def _walk(df: pd.DataFrame, idx: str, cost_pct: float | None = None, only=None, side_for=None, skip_days=()):
    """Every NR7 setup on daily candles (all complete sessions), oldest first, under the live rules: one trade
    per index at a time (an NR7 while a trade is open, or on the day it closed, is SKIPPED, as in the playbook),
    an NR7 whose next session is in skip_days is SKIPPED, the rest walked through the following sessions. A closed trade's R is
    after costs (cost_pct of the price per round trip, COST_PCT[idx] by default), with its net points and days
    held. The last setups can still be OPEN, or WAIT on the final NR7. Research hooks: only(nr7 day) -> bool keeps
    just those setups; side_for() -> side forces the side (see advance)."""
    cost_pct = COST_PCT[idx] if cost_pct is None else cost_pct
    rng = df["High"] - df["Low"]
    nr = (rng > 0) & (rng <= rng.rolling(NR_DAYS).min())
    last_day = str(df.index[-1].date()) if len(df) else None
    busy_until = None   # the exit day of the last trade on this index (an NR7 on it is skipped too)
    for i in range(NR_DAYS - 1, len(df)):
        d = df.index[i]
        if not nr.iloc[i] or (only and not only(d)):
            continue
        t = new_setup(idx, d.date(), df["High"].iloc[i], df["Low"].iloc[i])
        if t["risk"] <= 0:   # a range under a paisa (old split-adjusted prices) rounds to nothing
            continue
        nxt = (d + pd.offsets.BDay(1)).date()
        if busy_until is not None and d <= busy_until:
            t.update(status="SKIPPED", note="already a trade on this index")
        elif str(nxt) in skip_days:
            t.update(status="SKIPPED", note=f"{nxt} is a skip day")
        else:
            t = advance(t, df.iloc[i + 1:i + 2 + HOLD_DAYS + 1], daily=True,
                        force_side=side_for() if side_for else None)
            if t["status"] == WAIT and t["trigger_day"] and t["trigger_day"] <= last_day:
                t.update(status="EXPIRED", note="Neither level traded")   # its session is complete
            if t["status"] in CLOSED:
                cost = cost_pct / 100 * t["entry"]
                t["r"] = round(t["r"] - cost / trade_risk(t), 3)
                t["net_pts"] = round((1 if t["side"] == "LONG" else -1) * (t["exit"] - t["entry"]) - cost, 2)
                t["days"] = t["day_no"]
                busy_until = pd.Timestamp(t["exit_at"])
            elif t["status"] == OPEN:
                busy_until = pd.Timestamp.max
        yield t


def backtest_index(df: pd.DataFrame, idx: str, cost_pct: float | None = None, only=None, side_for=None) -> list[dict]:
    """Every closed NR7 trade on daily candles, one at a time, after costs (see _walk)."""
    return [t for t in _walk(df, idx, cost_pct, only, side_for) if t["status"] in CLOSED]


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    r = pd.Series([t["r"] for t in trades])
    wins, losses = r[r > 0], r[r <= 0]
    eq = r.cumsum()
    years = pd.Series(r.values, index=[t["entry_at"][:4] for t in trades]).groupby(level=0).sum()
    avg_win, avg_loss = (wins.mean() if len(wins) else 0.0), (-losses.mean() if len(losses) else 0.0)
    streak = run = 0
    for x in r:
        run = run + 1 if x <= 0 else 0
        streak = max(streak, run)
    return {"trades": len(r), "from": trades[0]["entry_at"][:10], "to": trades[-1]["exit_at"][:10],
            "wins": len(wins), "losses": len(losses),
            "win_pct": round(len(wins) / len(r) * 100, 1), "avg_r": round(r.mean(), 3), "total_r": round(r.sum(), 1),
            "best_r": round(r.max(), 2), "worst_r": round(r.min(), 2), "max_losing_streak": streak,
            "profit_factor": round(wins.sum() / -losses.sum(), 2) if losses.sum() < 0 else None,
            "avg_win_r": round(avg_win, 2), "avg_loss_r": round(avg_loss, 2),
            "breakeven_win_pct": round(avg_loss / (avg_win + avg_loss) * 100, 1) if avg_win + avg_loss else None,
            "max_dd_r": round((eq.cummax().clip(lower=0) - eq).max(), 1),
            "exits": {k: int(v) for k, v in pd.Series([t["status"] for t in trades]).value_counts().items()},
            "years": {y: round(v, 1) for y, v in years.items()}}


def backtest(period: str = "max") -> dict:
    """Backtest every index on Yahoo's daily history and save the summary for the dashboard."""
    data = _daily(period)
    out = {"run_at": _now(), "costs_pct": COST_PCT, "indices": {}}
    for idx, df in data.items():
        out["indices"][idx] = summarize(backtest_index(df, idx))
    BACKTEST_FILE.write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


# ---- recent history: the last few months, replayed ----------------------------------------------------

HISTORY_TTL_SEC = 600
_history_cache: dict[int, tuple[float, dict]] = {}


def history(months: int = 3, now: datetime | None = None, data: dict[str, pd.DataFrame] | None = None) -> dict:
    """Every NR7 setup on the three indices whose NR7 day falls in the last `months` months (1-24), replayed on
    daily candles under the live rules (see _walk; the saved skip days apply, and the walk starts well before so
    a trade open at the start still blocks), with each index's and the combined results: R after costs, and
    rupees for one futures lot at today's lot sizes and at the lots your risk per trade allows. Cached for
    HISTORY_TTL_SEC unless data is given."""
    months = max(1, min(24, int(months)))
    if data is None and months in _history_cache and monotonic() - _history_cache[months][0] < HISTORY_TTL_SEC:
        return _history_cache[months][1]
    now = now or markets.get("IN").now()
    today = now.date()
    start = str((pd.Timestamp(today) - pd.DateOffset(months=months)).date())
    raw = data if data is not None else _daily("5y" if months > 12 else "2y")
    skip = set(settings()["skip_days"])
    budget = config.CAPITAL * config.RISK_PER_TRADE_PCT / 100
    rows, closed_all, out, last = [], [], {}, None
    for idx, df in raw.items():
        if now.weekday() < 5 and now.time() <= SESSION_END:
            df = df[df.index.date < today]   # today's session isn't complete
        if not len(df):
            continue
        last = max(last or "", str(df.index[-1].date()))
        lot, last_close = _lot(idx), float(df["Close"].iloc[-1])
        mine = [t for t in _walk(df, idx, skip_days=skip) if t["nr7_date"] >= start]
        for t in mine:
            t["lot"] = lot
            t["risk_per_lot"] = round((trade_risk(t) if t["entry"] else t["risk"]) * lot)
            t["lots"] = math.floor(budget / t["risk_per_lot"]) if t["risk_per_lot"] else 0
            if t["status"] in CLOSED:
                t["rs_per_lot"] = round(t["net_pts"] * lot)
                t["rs_sized"] = t["rs_per_lot"] * t["lots"]
            elif t["status"] == OPEN:   # marked at the last close
                sign = 1 if t["side"] == "LONG" else -1
                t["mark"] = round(last_close, 2)
                t["open_r"] = round(sign * (last_close - t["entry"]) / trade_risk(t), 2)
                t["rs_open_per_lot"] = round(sign * (last_close - t["entry"]) * lot)
        closed = [t for t in mine if t["status"] in CLOSED]
        status = [t["status"] for t in mine]
        worst_risk = max((t["risk_per_lot"] for t in mine), default=0)
        out[idx] = {**summarize(closed), "setups": len(mine), "lot": lot,
                    "rs_per_lot": sum(t["rs_per_lot"] for t in closed),
                    "rs_sized": sum(t["rs_sized"] for t in closed),
                    "expired": status.count("EXPIRED"), "skipped": status.count("SKIPPED"),
                    "open": status.count(OPEN), "waiting": status.count(WAIT),
                    # capital at which your risk per trade covers one lot on the widest setup of the period
                    "capital_for_1_lot": round(worst_risk / (config.RISK_PER_TRADE_PCT / 100))
                    if config.RISK_PER_TRADE_PCT > 0 else None}
        rows += mine
        closed_all += closed
    closed_all.sort(key=lambda t: t["exit_at"])
    total = {**summarize(closed_all), "setups": len(rows),
             "rs_per_lot": sum(t["rs_per_lot"] for t in closed_all),
             "rs_sized": sum(t["rs_sized"] for t in closed_all),
             **{k: sum(v[k] for v in out.values()) for k in ("expired", "skipped", "open", "waiting")}}
    by_month: dict[str, dict] = {}
    for t in closed_all:
        m = by_month.setdefault(t["entry_at"][:7], {"trades": 0, "wins": 0, "r": 0.0, "rs_per_lot": 0})
        m["trades"] += 1
        m["wins"] += int(t["r"] > 0)
        m["r"] = round(m["r"] + t["r"], 2)
        m["rs_per_lot"] += t["rs_per_lot"]
    rows.sort(key=lambda t: (t["nr7_date"], t["idx"]), reverse=True)
    res = {"months": months, "from": start, "to": last, "run_at": _now(),
           "capital": config.CAPITAL, "risk_pct": config.RISK_PER_TRADE_PCT, "risk_budget": round(budget),
           "costs_pct": COST_PCT, "indices": out, "total": total,
           "by_month": dict(sorted(by_month.items())), "trades": rows}
    if data is None:
        _history_cache[months] = (monotonic(), res)
    return res
