"""MCX commodity futures (market "MCX"): the mini contracts, scanned like a market. Auto-trade buys options on
them (options_auto.py, MCX_AUTO_TRADE); the futures themselves are never ordered.

The scan trades the minis (GOLDM, SILVERM, CRUDEOILM, NATGASMINI), one lot a signal: the same prices as the
full-size contracts at a fraction of the size. Copper has no mini on MCX, so it isn't scanned.

Kotak gives live MCX prices but no MCX price history, and Yahoo has none either, so history is built two ways:

- Recorded (the real thing): the dashboard listens to Kotak's live stream for the front-month contract of each
  commodity and stores 5-minute bars in signals.db (table mcx_bars; backed up with it). 5m / 15m / 30m / 1h /
  daily bars are cut from these.
- Proxy (until enough is recorded): the world futures contract on Yahoo (NYMEX / COMEX), turned into rupees
  with Yahoo's USD/INR and scaled so it lines up with MCX (at the first recorded bar, else today's live MCX
  price). Its levels are estimates; it only fills the history the strategies look back over.

fetch_ohlcv() gives each commodity's bars: recorded only once there are MIN_BARS of them on that timeframe,
otherwise the proxy with the recorded bars taking over from where recording began. The DataFrame's
attrs["source"] says which ("mcx", "blend" or "proxy") and the alert shows it.

Contracts: the nearest monthly future with more than ROLL_DAYS to expiry (Kotak's mcx_fo scrip master), so the
scan moves to the next month a few days before expiry, as volume does. Recorded bars keep the contract they came
from; prices are not back-adjusted across a roll.
"""
import io
import logging
import threading
import time
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from . import config

log = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")
SEGMENT = "mcx_fo"
# Scanned: the mini contracts. symbol: (the full-size contract quoted in the same units, what it is)
COMMODITIES = {
    "CRUDEOILM": ("CRUDEOIL", "Crude oil mini (10 bbl)"),
    "NATGASMINI": ("NATURALGAS", "Natural gas mini (250 mmBtu)"),
    "GOLDM": ("GOLD", "Gold mini (100 g)"),
    "SILVERM": ("SILVER", "Silver mini (5 kg)"),
}
PARENT = {sym: full for sym, (full, _) in COMMODITIES.items()}
# The Yahoo world future used as the proxy. The full-size contracts (scanned before the minis) stay known so their
# recorded bars and any signal still open on them keep working.
PROXY = {"CRUDEOIL": "CL=F", "NATURALGAS": "NG=F", "GOLD": "GC=F", "SILVER": "SI=F", "COPPER": "HG=F"}
PROXY.update({sym: PROXY[full] for sym, full in PARENT.items()})
KNOWN = set(PROXY)
# Rupees a 1-rupee price move is worth per unit of Kotak's lot size: Gold is quoted per 10 g but its lot is in
# kg (lot 1 = 1 kg -> x100); Gold mini's lot is in grams (lot 100 = 100 g -> x0.1)
MULTIPLIER = {"GOLD": 100, "GOLDM": 0.1}
FX_TICKER = "INR=X"                   # USD/INR
SESSION = (dtime(9, 0), dtime(23, 55))   # MCX metals & energy (23:30 while the US is on summer time)
ROLL_DAYS = 3                         # move to the next month this many days before expiry
BAR_MIN = 5                           # recorded bar length, minutes (every scanned timeframe is a multiple)
MIN_BARS = 260                        # recorded bars needed on a timeframe before the proxy is dropped (EMA 200 + room)
KEEP_DAYS = 400                       # recorded 5-minute bars older than this are deleted
RULE = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "60min", "1d": "1D"}
PROXY_PERIOD = {"5m": "60d", "15m": "60d", "30m": "60d", "1h": "730d", "1d": "2y"}
EPOCH_1980 = 315532800


# ---------------- contracts ----------------

_contracts = {"day": None, "df": None}
_options = {"day": None, "df": None}
OPTION_TSYM = r"^([A-Z]+?)(\d{2}[A-Z]{3}\d{2})(\d+(?:\.\d+)?)(CE|PE)$"   # GOLDM27NOV26144500CE


def parse_contracts(csv_text: str) -> pd.DataFrame:
    """Commodity futures of the KNOWN commodities from Kotak's mcx_fo scrip master: symbol, expiry, token, tsym, lot."""
    m = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False)
    m.columns = [c.strip().rstrip(";") for c in m.columns]
    m = m[(m["pInstType"].str.strip() == "FUTCOM") & m["pSymbolName"].str.strip().isin(KNOWN)]
    tsym = m["pTrdSymbol"].str.strip()
    # The trading symbol carries the expiry (CRUDEOIL18DEC26FUT); the numeric field's epoch differs by segment
    expiry = pd.to_datetime(tsym.str.extract(r"(\d{2}[A-Z]{3}\d{2})FUT$")[0], format="%d%b%y", errors="coerce")
    return pd.DataFrame({
        "symbol": m["pSymbolName"].str.strip(), "expiry": expiry.dt.date, "token": m["pSymbol"].str.strip(),
        "tsym": tsym, "lot": pd.to_numeric(m["lLotSize"], errors="coerce").fillna(0).astype(int),
    }).dropna(subset=["expiry"]).reset_index(drop=True)


def contracts() -> pd.DataFrame:
    """Today's MCX futures of the KNOWN commodities (downloaded once a day, cached on disk)."""
    today = date.today()
    if _contracts["day"] != today:
        from . import fno
        _contracts.update(day=today, df=parse_contracts(fno._master_text(SEGMENT)))
    return _contracts["df"]


def _num(m: pd.DataFrame, col: str, default: float) -> pd.Series:
    return pd.to_numeric(m[col], errors="coerce").fillna(default) if col in m else pd.Series(default, index=m.index)


def parse_options(csv_text: str) -> pd.DataFrame:
    """Options on the scanned minis' futures from Kotak's mcx_fo scrip master: symbol, opt, strike, expiry, token,
    tsym, lot, tick (rupees) and mult (rupees per rupee of premium per unit of quantity: Gold mini's quantity is in
    grams, its premium per 10 g -> 0.1)."""
    m = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False)
    m.columns = [c.strip().rstrip(";") for c in m.columns]
    m = m[(m["pInstType"].str.strip() == "OPTFUT") & m["pSymbolName"].str.strip().isin(COMMODITIES)]
    parts = m["pTrdSymbol"].str.strip().str.extract(OPTION_TSYM)
    out = pd.DataFrame({
        "symbol": m["pSymbolName"].str.strip(), "opt": parts[3],
        "strike": pd.to_numeric(parts[2], errors="coerce"),
        "expiry": pd.to_datetime(parts[1], format="%d%b%y", errors="coerce").dt.date,
        "token": m["pSymbol"].str.strip(), "tsym": m["pTrdSymbol"].str.strip(),
        "lot": pd.to_numeric(m["lLotSize"], errors="coerce").fillna(0).astype(int),
        "tick": _num(m, "dTickSize", 5) / 100,              # in paise
        "mult": (_num(m, "dGenNum", 1) / _num(m, "dGenDen", 1).replace(0, 1)
                 * _num(m, "dPriceNum", 1) / _num(m, "dPriceDen", 1).replace(0, 1)),
    })
    return out.dropna(subset=["expiry", "strike", "opt"]).reset_index(drop=True)


def options() -> pd.DataFrame:
    """Today's options on the minis (downloaded once a day with the futures list)."""
    today = date.today()
    if _options["day"] != today:
        from . import fno
        _options.update(day=today, df=parse_options(fno._master_text(SEGMENT)))
    return _options["df"]


def has_options(symbol: str) -> bool:
    try:
        return symbol in COMMODITIES and bool((options()["symbol"] == symbol).any())
    except Exception:
        log.exception("MCX option list unavailable")
        return False


def option_contract(tsym: str) -> dict | None:
    df = options()
    hit = df[df["tsym"] == tsym]
    return None if hit.empty else hit.iloc[0].to_dict()


def option_future(symbol: str, expiry: date, df: pd.DataFrame | None = None) -> dict | None:
    """The futures contract an option is written on: the first one expiring on or after the option (MCX options
    expire a few days before their future)."""
    df = contracts() if df is None else df
    rows = df[(df["symbol"] == symbol) & (df["expiry"] >= expiry)].sort_values("expiry")
    return None if rows.empty else rows.iloc[0].to_dict()


def otm_option(symbol: str, opt: str, steps: int = 2, min_days: float = 5, now: datetime | None = None,
               price=None) -> dict | None:
    """The option Auto-trade buys on a mini: on the nearest expiry at least `min_days` away, the `steps`-th strike
    out of the money from the live price of the future that option is on (price: token -> ltp, for tests).
    The row comes back with "future" and "u_price" (that future and its price). None if there is no such option
    or no price."""
    from . import fno
    df = options()
    rows = df[(df["symbol"] == symbol) & (df["opt"] == opt)]
    exps = sorted(e for e in rows["expiry"].unique() if fno.days_to_expiry(e, now) >= min_days)
    if not exps:
        return None
    rows = rows[rows["expiry"] == exps[0]]
    fut = option_future(symbol, exps[0])
    px = (price or ltp)(fut["token"]) if fut else None
    if not px:
        return None
    strikes = sorted(rows["strike"].unique())
    otm = [k for k in strikes if k > px] if opt == "CE" else [k for k in reversed(strikes) if k < px]
    if len(otm) < max(steps, 1):
        return None
    return {**rows[rows["strike"] == otm[max(steps, 1) - 1]].iloc[0].to_dict(), "future": fut["tsym"],
            "u_price": px}


def option_quote(tsym: str) -> dict:
    """Live quote of an MCX option: ltp, best bid / ask (as fno.option_quote)."""
    from . import fno
    c = option_contract(tsym)
    if c is None:
        raise ValueError(f"Unknown MCX option contract {tsym}")
    res = fno._quotes([c["token"]], SEGMENT)
    if not res:
        raise RuntimeError(f"No quote for {tsym}")
    return fno.parse_option_quote(res[0])


def front(symbol: str, today: date | None = None, df: pd.DataFrame | None = None) -> dict | None:
    """The contract to trade / record: the nearest expiry more than ROLL_DAYS away (else the nearest left)."""
    today = today or date.today()
    df = contracts() if df is None else df
    rows = df[(df["symbol"] == symbol) & (df["expiry"] >= today)].sort_values("expiry")
    if rows.empty:
        return None
    later = rows[rows["expiry"] > today + timedelta(days=ROLL_DAYS)]
    return (later if not later.empty else rows).iloc[0].to_dict()


def lot_size(symbol: str) -> int:
    try:
        c = front(symbol)
    except Exception as exc:
        log.warning("No MCX contract list (%s); lot size unknown for %s", exc, symbol)
        return 0
    return int(c["lot"]) if c else 0


def point_value(symbol: str, lot: int | None = None) -> float:
    """Rupees one lot gains or loses per 1-rupee price move (lot x multiplier); 0 if the lot is unknown."""
    lot = lot_size(symbol) if lot is None else lot
    return round(float(lot * MULTIPLIER.get(symbol, 1)), 6)


def in_session(now: datetime | None = None) -> bool:
    now = now or datetime.now(IST)
    return now.weekday() < 5 and SESSION[0] <= now.time() <= SESSION[1]


# ---------------- recorded bars ----------------

def _conn():
    from . import state
    con = state._conn()
    con.execute("""CREATE TABLE IF NOT EXISTS mcx_bars (
        symbol TEXT, ts TEXT, contract TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
        PRIMARY KEY (symbol, ts))""")
    return con


def save_bar(symbol: str, ts: datetime, contract: str, o: float, h: float, low: float, c: float, v: float) -> None:
    with _conn() as con:
        con.execute("""INSERT INTO mcx_bars (symbol, ts, contract, open, high, low, close, volume)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(symbol, ts) DO UPDATE SET high = max(high, excluded.high),
                         low = min(low, excluded.low), close = excluded.close,
                         volume = volume + excluded.volume, contract = excluded.contract""",
                    (symbol, ts.strftime("%Y-%m-%dT%H:%M:%S"), contract, o, h, low, c, v))


def recorded(symbol: str) -> pd.DataFrame:
    """Recorded 5-minute bars of a commodity (naive IST index; columns Open High Low Close Volume)."""
    with _conn() as con:
        rows = con.execute("SELECT ts, open, high, low, close, volume FROM mcx_bars WHERE symbol = ? ORDER BY ts",
                           (symbol,)).fetchall()
    df = pd.DataFrame([tuple(r) for r in rows], columns=["ts", "Open", "High", "Low", "Close", "Volume"])
    if df.empty:
        return df.set_index("ts")
    df.index = pd.to_datetime(df.pop("ts"))
    return df.astype(float)


def history(symbol: str) -> pd.DataFrame:
    """Recorded bars of a mini, led by its full-size contract's bars from before the mini was recorded (same
    commodity, same price units: the history scanned before the switch to minis)."""
    real = recorded(symbol)
    parent = PARENT.get(symbol)
    if not parent:
        return real
    old = recorded(parent)
    if not real.empty:
        old = old[old.index < real.index[0]]
    return pd.concat([old, real]) if not old.empty and not real.empty else (real if old.empty else old)


def prune(today: date | None = None) -> int:
    cutoff = ((today or date.today()) - timedelta(days=KEEP_DAYS)).isoformat()
    with _conn() as con:
        return con.execute("DELETE FROM mcx_bars WHERE ts < ?", (cutoff,)).rowcount


def resample(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Cut 5-minute (or finer) bars into the timeframe's bars, labelled by their start; empty periods dropped."""
    if df.empty or timeframe not in RULE:
        return df
    out = df.resample(RULE[timeframe], label="left", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"})
    return out.dropna(subset=["Open"])


class Recorder:
    """Builds 5-minute bars from live ticks of the front contracts (fed by livefeed's listener)."""

    def __init__(self):
        self.watch: dict[str, tuple[str, str]] = {}   # "mcx_fo|token" -> (symbol, contract)
        self.bars: dict[str, dict] = {}               # symbol -> the bar being built
        self.cum_vol: dict[str, float] = {}           # "segment|token" -> last cumulative day volume seen
        self.lock = threading.Lock()

    def set_contracts(self, fronts: dict[str, dict]) -> None:
        with self.lock:
            self.watch = {f"{SEGMENT}|{c['token']}": (sym, c["tsym"]) for sym, c in fronts.items() if c}

    def on_tick(self, key: str, q: dict, now: datetime | None = None) -> None:
        hit = self.watch.get(key)
        if not hit:
            return
        now = now or datetime.now(IST)
        if not in_session(now):
            return
        price = float(q.get("ltp") or 0)
        if price <= 0:
            return
        sym, contract = hit
        cum = float(q.get("last_volume") or 0)
        with self.lock:
            prev = self.cum_vol.get(key)
            self.cum_vol[key] = cum
            vol = max(cum - prev, 0) if prev is not None and cum >= prev else 0
            start = now.replace(second=0, microsecond=0, tzinfo=None)
            start -= timedelta(minutes=start.minute % BAR_MIN)
            bar = self.bars.get(sym)
            if bar and bar["start"] != start:
                self._flush(sym, bar)
                bar = None
            if not bar:
                self.bars[sym] = {"start": start, "contract": contract, "o": price, "h": price, "l": price,
                                  "c": price, "v": vol}
            else:
                bar.update(h=max(bar["h"], price), l=min(bar["l"], price), c=price, v=bar["v"] + vol)

    def flush_due(self, now: datetime | None = None) -> None:
        """Save bars whose 5 minutes are over even if no newer tick came (a quiet market)."""
        now = (now or datetime.now(IST)).replace(tzinfo=None)
        with self.lock:
            for sym, bar in list(self.bars.items()):
                if bar["start"] + timedelta(minutes=BAR_MIN) <= now:
                    self._flush(sym, bar)
                    del self.bars[sym]

    @staticmethod
    def _flush(sym: str, bar: dict) -> None:
        try:
            save_bar(sym, bar["start"], bar["contract"], bar["o"], bar["h"], bar["l"], bar["c"], bar["v"])
        except Exception:
            log.exception("Could not save the MCX %s bar", sym)


recorder = Recorder()
_thread: threading.Thread | None = None


def _record_forever() -> None:
    """Keep the front contracts subscribed on the live feed, re-pick them daily, save finished bars."""
    from . import livefeed
    livefeed.add_listener(recorder.on_tick)
    day = None
    while True:
        try:
            if day != date.today():
                fronts = {s: front(s) for s in COMMODITIES}
                recorder.set_contracts(fronts)
                log.info("Recording MCX bars: %s", ", ".join(c["tsym"] for c in fronts.values() if c))
                prune()
                day = date.today()
            if in_session():
                livefeed.touch(list(recorder.watch))
            recorder.flush_due()
        except Exception as exc:
            log.warning("MCX recorder: %s", exc)
            time.sleep(60)
        time.sleep(15)


def start_recorder() -> bool:
    """Start recording (once, in the dashboard, where the live feed runs)."""
    global _thread
    from . import livefeed
    if _thread is not None and _thread.is_alive():
        return True
    if not livefeed.start() and not livefeed.running():
        return False
    _thread = threading.Thread(target=_record_forever, name="mcx-recorder", daemon=True)
    _thread.start()
    return True


# ---------------- proxy history ----------------

def _yahoo(ticker: str, timeframe: str) -> pd.DataFrame:
    import yfinance as yf
    interval = "60m" if timeframe == "1h" else timeframe
    raw = yf.download(ticker, period=PROXY_PERIOD[timeframe], interval=interval, auto_adjust=True,
                      progress=False, threads=False)
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()
    if df.index.tz is not None:
        df.index = df.index.tz_convert(IST).tz_localize(None)
    return df


def to_mcx_hours(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Keep only bars inside MCX's session on weekdays (the world contracts trade almost round the clock)."""
    if df.empty or timeframe == "1d":
        return df[df.index.dayofweek < 5] if not df.empty else df
    t = df.index.time
    return df[(df.index.dayofweek < 5) & (t >= SESSION[0]) & (t <= SESSION[1])]


def proxy(symbol: str, timeframe: str) -> pd.DataFrame:
    """The world contract in rupees (unscaled): price x USD/INR at the time. Empty if Yahoo has nothing."""
    df = _yahoo(PROXY[symbol], timeframe)
    if df.empty:
        return df
    fx = _yahoo(FX_TICKER, "1d" if timeframe == "1d" else "1h")["Close"]
    if fx.empty:
        return pd.DataFrame()
    rate = fx.reindex(df.index.union(fx.index)).ffill().bfill().reindex(df.index)
    for col in ("Open", "High", "Low", "Close"):
        df[col] = df[col] * rate
    return to_mcx_hours(df, timeframe)


def ltp(token: str) -> float | None:
    """Last price of an mcx_fo contract (the live stream where it has it, else a Kotak quote), or None."""
    from . import fno
    try:
        rows = fno._quotes([str(token)], SEGMENT)
    except Exception as exc:
        log.warning("MCX live price of %s failed: %s", token, exc)
        return None
    for r in rows:
        if str(r.get("exchange_token")) == str(token) and fno._f(r.get("ltp")):
            return float(r["ltp"])
    return None


def live_price(symbol: str) -> float | None:
    """The front contract's last price (the one the scan's signals are on), or None if it can't be had."""
    c = front(symbol)
    return ltp(c["token"]) if c else None


def combine(real: pd.DataFrame, prox: pd.DataFrame, anchor: float | None = None) -> tuple[pd.DataFrame, str]:
    """Recorded bars, with the scaled proxy in front of them while there are fewer than MIN_BARS.
    Scale: the proxy's close at the first recorded bar -> that bar's open (else the newest proxy close ->
    `anchor`, today's live MCX price). Proxy volume is scaled to the recorded bars' typical volume."""
    if len(real) >= MIN_BARS or prox.empty:
        return real, "mcx"
    if not real.empty:
        at = prox["Close"].asof(real.index[0])
        k = real["Open"].iloc[0] / at if at and at == at else None
    else:
        k = anchor / prox["Close"].iloc[-1] if anchor else None
    if not k:
        return real, "mcx"
    p = prox.copy()
    for col in ("Open", "High", "Low", "Close"):
        p[col] = p[col] * k
    if not real.empty:
        pv, rv = p["Volume"].replace(0, pd.NA).median(), real["Volume"].replace(0, pd.NA).median()
        if pd.notna(pv) and pd.notna(rv) and pv:
            p["Volume"] = p["Volume"] * (rv / pv)
        p = p[p.index < real.index[0]]
    out = pd.concat([p, real]) if not real.empty else p
    return out, ("blend" if not real.empty else "proxy")


def fetch_ohlcv(symbols: list[str], timeframe: str = "1d", stop=None) -> dict[str, pd.DataFrame]:
    """{commodity: bars} for the scanner and the tracker (see the module docstring)."""
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        if stop:
            stop()
        if sym not in KNOWN:
            continue
        try:
            real = resample(history(sym), timeframe)
            prox, anchor = pd.DataFrame(), None
            if len(real) < MIN_BARS:
                prox = proxy(sym, timeframe)
                if real.empty and not prox.empty:
                    anchor = live_price(sym)
            df, source = combine(real, prox, anchor)
        except Exception as exc:
            log.warning("MCX %s %s data failed: %s", sym, timeframe, exc)
            continue
        if len(df) >= 60:
            df.attrs["source"] = source
            out[sym] = df
    log.info("MCX data for %d/%d commodities (%s)", len(out), len(symbols), timeframe)
    return out


SOURCE_TEXT = {
    "mcx": "MCX prices",
    "blend": "MCX prices, older bars from the world market (estimated)",
    "proxy": "world-market prices scaled to MCX (estimated: MCX history is still being recorded)",
}
