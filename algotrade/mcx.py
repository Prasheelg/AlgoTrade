"""MCX commodity futures (market "MCX"): the liquid contracts, scanned like a market, alerts only (no orders).

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
# symbol: (Yahoo world future used as the proxy, what it is)
COMMODITIES = {
    "CRUDEOIL": ("CL=F", "Crude oil (NYMEX WTI)"),
    "NATURALGAS": ("NG=F", "Natural gas (NYMEX)"),
    "GOLD": ("GC=F", "Gold (COMEX)"),
    "SILVER": ("SI=F", "Silver (COMEX)"),
    "COPPER": ("HG=F", "Copper (COMEX)"),
}
# Rupees a 1-rupee price move is worth per traded unit: Gold is quoted per 10 g but traded in kg (lot 1 = 1 kg)
MULTIPLIER = {"GOLD": 100}
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


def parse_contracts(csv_text: str) -> pd.DataFrame:
    """Commodity futures of the COMMODITIES from Kotak's mcx_fo scrip master: symbol, expiry, token, tsym, lot."""
    m = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False)
    m.columns = [c.strip().rstrip(";") for c in m.columns]
    m = m[(m["pInstType"].str.strip() == "FUTCOM") & m["pSymbolName"].str.strip().isin(COMMODITIES)]
    tsym = m["pTrdSymbol"].str.strip()
    # The trading symbol carries the expiry (CRUDEOIL18DEC26FUT); the numeric field's epoch differs by segment
    expiry = pd.to_datetime(tsym.str.extract(r"(\d{2}[A-Z]{3}\d{2})FUT$")[0], format="%d%b%y", errors="coerce")
    return pd.DataFrame({
        "symbol": m["pSymbolName"].str.strip(), "expiry": expiry.dt.date, "token": m["pSymbol"].str.strip(),
        "tsym": tsym, "lot": pd.to_numeric(m["lLotSize"], errors="coerce").fillna(0).astype(int),
    }).dropna(subset=["expiry"]).reset_index(drop=True)


def contracts() -> pd.DataFrame:
    """Today's MCX futures of the COMMODITIES (downloaded once a day, cached on disk)."""
    today = date.today()
    if _contracts["day"] != today:
        from . import fno
        _contracts.update(day=today, df=parse_contracts(fno._master_text(SEGMENT)))
    return _contracts["df"]


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
    return float(lot * MULTIPLIER.get(symbol, 1))


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
    df = _yahoo(COMMODITIES[symbol][0], timeframe)
    if df.empty:
        return df
    fx = _yahoo(FX_TICKER, "1d" if timeframe == "1d" else "1h")["Close"]
    if fx.empty:
        return pd.DataFrame()
    rate = fx.reindex(df.index.union(fx.index)).ffill().bfill().reindex(df.index)
    for col in ("Open", "High", "Low", "Close"):
        df[col] = df[col] * rate
    return to_mcx_hours(df, timeframe)


def live_price(symbol: str) -> float | None:
    """The front contract's last price (Kotak REST quote), or None if it can't be had. Kotak answers quotes with a
    plain list, which broker.call's dict check rejects, so the client is called directly."""
    from . import broker
    c = front(symbol)
    if not c:
        return None
    try:
        res = broker.login().quotes(
            instrument_tokens=[{"instrument_token": c["token"], "exchange_segment": SEGMENT}], quote_type="ltp")
    except Exception as exc:
        log.warning("MCX %s live price failed: %s", symbol, exc)
        return None
    rows = res if isinstance(res, list) else (res or {}).get("data") or []
    for r in rows:
        if str(r.get("exchange_token")) == str(c["token"]) and r.get("ltp"):
            return float(r["ltp"])
    return None


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
        if sym not in COMMODITIES:
            continue
        try:
            real = resample(recorded(sym), timeframe)
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
