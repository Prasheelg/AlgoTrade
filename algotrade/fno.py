"""Option chains for NSE index and stock options from Kotak Neo (read-only: scrip master and quotes).

Contracts come from Kotak's nse_fo scrip master (downloaded once a day, cached in DATA_DIR); prices, volume,
open interest and best bid / ask from quotes on the consumer key alone, like kotak.py. Nothing here can place
an order; manual.py does that."""
import io
import logging
import math
import time
from datetime import date, datetime

import pandas as pd

from . import config, kotak, livefeed

log = logging.getLogger(__name__)
SEGMENT = "nse_fo"
EPOCH_1980 = 315532800      # scrip master expiry dates count seconds from 1980-01-01
QUOTE_BATCH = kotak.BATCH   # Kotak takes at most 49 instruments per quote call
CHAIN_CACHE_SEC = 2         # one quote round per chain for this long, however many tabs poll
LIVE_CHAIN_CACHE_SEC = 0.2  # while the live stream is connected (prices come from memory)
# Index option underlyings -> Kotak quote name of the index (nse_cm)
INDEX_QUOTE = {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank", "FINNIFTY": "Nifty Fin Service",
               "MIDCPNIFTY": "NIFTY MID SELECT", "NIFTYNXT50": "Nifty Next 50"}

_master = {"day": None, "df": None}
_chains: dict = {}


def parse_master(csv_text: str) -> pd.DataFrame:
    """Index and stock options from the nse_fo scrip master: one row per contract."""
    m = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False)
    m.columns = [c.strip().rstrip(";") for c in m.columns]
    m = m[m["pInstType"].str.strip().isin(["OPTIDX", "OPTSTK"])]
    exp = pd.to_numeric(m["pExpiryDate"], errors="coerce") + EPOCH_1980
    df = pd.DataFrame({
        "underlying": m["pSymbolName"].str.strip(),
        "kind": m["pInstType"].str.strip().map({"OPTIDX": "index", "OPTSTK": "stock"}),
        "expiry": pd.to_datetime(exp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date,
        "strike": pd.to_numeric(m["dStrikePrice"], errors="coerce") / 100,
        "opt": m["pOptionType"].str.strip(),
        "lot": pd.to_numeric(m["lLotSize"], errors="coerce").fillna(0).astype(int),
        "token": m["pSymbol"].str.strip(),
        "tsym": m["pTrdSymbol"].str.strip(),
        "tick": pd.to_numeric(m["dTickSize"], errors="coerce").fillna(5) / 100,
    })
    df = df[df["underlying"].isin(INDEX_QUOTE) | (df["kind"] == "stock")]
    return df.dropna(subset=["strike", "expiry"]).reset_index(drop=True)


def _master_text(segment: str) -> str:
    """A segment's scrip master CSV, downloaded once a day and cached on disk across restarts."""
    today = date.today()
    cache = config.DATA_DIR / f"kotak_{segment}_{today}.csv"
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    url = kotak._neo().scrip_master(exchange_segment=segment)
    if not (isinstance(url, str) and url.startswith("http")):
        raise RuntimeError(f"Unexpected F&O scrip master response: {str(url)[:200]}")
    text = kotak._download_csv(url)
    cache.write_text(text, encoding="utf-8")
    for old in config.DATA_DIR.glob(f"kotak_{segment}_*.csv"):
        if old != cache:
            old.unlink(missing_ok=True)
    return text


def contracts() -> pd.DataFrame:
    """All option contracts, refreshed once a day (cached on disk across restarts)."""
    today = date.today()
    if _master["day"] != today:
        df = parse_master(_master_text(SEGMENT))
        _master.update(day=today, df=df[df["expiry"] >= today].reset_index(drop=True))
        log.info("Kotak F&O scrip master: %d option contracts", len(_master["df"]))
    return _master["df"]


# Index futures and options (the NR7 plans): NSE sends expiries in seconds since 1980, BSE in Unix seconds
INDEX_SEGMENTS = {"nse_fo": ({"FUTIDX": "FUT", "OPTIDX": "OPT"}, EPOCH_1980), "bse_fo": ({"IF": "FUT", "IO": "OPT"}, 0)}
_index_master: dict = {}


def parse_index_master(csv_text: str, segment: str) -> pd.DataFrame:
    """Index futures (opt "FUT", no strike) and options (CE / PE) from a segment's scrip master."""
    kinds, epoch0 = INDEX_SEGMENTS[segment]
    m = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False)
    m.columns = [c.strip().rstrip(";") for c in m.columns]
    inst = m["pInstType"].str.strip().map(kinds)
    m, inst = m[inst.notna()], inst[inst.notna()]
    exp = pd.to_numeric(m["pExpiryDate"], errors="coerce") + epoch0
    strike = pd.to_numeric(m["dStrikePrice"], errors="coerce") / 100
    df = pd.DataFrame({
        "underlying": m["pSymbolName"].str.strip(),
        "opt": m["pOptionType"].str.strip().where(inst == "OPT", "FUT"),
        "expiry": pd.to_datetime(exp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date,
        "strike": strike.where(inst == "OPT"),
        "lot": pd.to_numeric(m["lLotSize"], errors="coerce").fillna(0).astype(int),
        "token": m["pSymbol"].str.strip(),
        "tsym": m["pTrdSymbol"].str.strip(),
        "tick": pd.to_numeric(m["dTickSize"], errors="coerce").fillna(5) / 100,
    })
    return df.dropna(subset=["expiry"]).reset_index(drop=True)


def index_contracts(underlying: str, segment: str = SEGMENT) -> pd.DataFrame:
    """An index's live futures and options (e.g. NIFTY on nse_fo, SENSEX on bse_fo), refreshed once a day."""
    today = date.today()
    hit = _index_master.get(segment)
    if not hit or hit[0] != today:
        df = parse_index_master(_master_text(segment), segment)
        hit = _index_master[segment] = (today, df[df["expiry"] >= today].reset_index(drop=True))
    df = hit[1]
    return df[df["underlying"] == underlying].reset_index(drop=True)


def underlyings() -> list[dict]:
    """Index underlyings first (in INDEX_QUOTE order), then stocks A-Z, with their lot size."""
    df = contracts()
    lots = df.groupby("underlying")["lot"].max().to_dict()
    idx = [{"name": u, "kind": "index", "lot": lots[u]} for u in INDEX_QUOTE if u in lots]
    stocks = sorted(set(df.loc[df["kind"] == "stock", "underlying"]))
    return idx + [{"name": u, "kind": "stock", "lot": lots[u]} for u in stocks]


def expiries(underlying: str) -> list[str]:
    df = contracts()
    return sorted({str(e) for e in df.loc[df["underlying"] == underlying, "expiry"]})


def contract(tsym: str) -> dict | None:
    df = contracts()
    hit = df[df["tsym"] == tsym]
    return None if hit.empty else hit.iloc[0].to_dict()


def _quotes(tokens: list[str], segment: str = SEGMENT) -> list[dict]:
    """Raw quotes for instrument tokens: from the live stream where it has them, the rest in batches (the API
    takes 50 at a time)."""
    live = livefeed.lookup(segment, tokens)
    out = list(live.values())
    tokens = [t for t in tokens if str(t) not in live]
    for n in range(0, len(tokens), QUOTE_BATCH):
        if n:
            time.sleep(kotak.PAUSE_SEC)
        batch = [{"instrument_token": t, "exchange_segment": segment} for t in tokens[n:n + QUOTE_BATCH]]
        res = kotak._neo().quotes(instrument_tokens=batch, quote_type="all")
        if not isinstance(res, list):
            raise RuntimeError(f"Kotak quotes failed: {str(res)[:200]}")
        out += res
    return out


def _f(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def parse_option_quote(q: dict) -> dict:
    depth = q.get("depth") or {}
    bid = (depth.get("buy") or [{}])[0]
    ask = (depth.get("sell") or [{}])[0]
    return {"ltp": _f(q.get("ltp")), "change": _f(q.get("change")), "pct": _f(q.get("per_change")),
            "volume": _f(q.get("last_volume")), "oi": _f(q.get("open_int")),
            "bid": _f(bid.get("price")) or None, "ask": _f(ask.get("price")) or None,
            "bid_qty": _f(bid.get("quantity")), "ask_qty": _f(ask.get("quantity")),
            "time": t.isoformat(timespec="seconds") if (t := kotak.quote_time(q)) else None}


def option_quote(tsym: str) -> dict:
    """Live quote of one option contract: ltp, best bid / ask (see parse_option_quote)."""
    c = contract(tsym)
    if c is None:
        raise ValueError(f"Unknown option contract {tsym}")
    res = _quotes([c["token"]])
    if not res:
        raise RuntimeError(f"No quote for {tsym}")
    return parse_option_quote(res[0])


def spot(underlying: str) -> dict:
    """Underlying price: the index quote, or the stock's NSE quote."""
    token = INDEX_QUOTE.get(underlying) or kotak.tokens().get(underlying)
    q = {}
    if token:
        try:
            res = _quotes([token], "nse_cm")
        except RuntimeError:
            res = []
        q = res[0] if res else {}
    ltp, prev = _f(q.get("ltp")), _f((q.get("ohlc") or {}).get("close"))
    return {"ltp": ltp, "change": ltp - prev if ltp is not None and prev else None,
            "pct": (ltp - prev) / prev * 100 if ltp is not None and prev else None}


def chain(underlying: str, expiry: str, width: int = 10) -> dict:
    """Option chain around the money: `width` strikes either side of the strike nearest the spot.
    Cached CHAIN_CACHE_SEC per (underlying, expiry, width)."""
    ck = (underlying, expiry, width)
    hit = _chains.get(ck)
    if hit and time.monotonic() - hit[0] < (LIVE_CHAIN_CACHE_SEC if livefeed.connected() else CHAIN_CACHE_SEC):
        return hit[1]
    df = contracts()
    rows = df[(df["underlying"] == underlying) & (df["expiry"].astype(str) == expiry)]
    if rows.empty:
        raise ValueError(f"No {underlying} options expiring {expiry}")
    strikes = sorted(rows["strike"].unique())
    sp = spot(underlying)
    ref = sp["ltp"] if sp["ltp"] is not None else strikes[len(strikes) // 2]
    atm_i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - ref))
    window = strikes[max(0, atm_i - width): atm_i + width + 1]
    sel = rows[rows["strike"].isin(window)]
    quotes = {str(q.get("exchange_token")): parse_option_quote(q) for q in _quotes(list(sel["token"]))}
    out_rows = []
    for k in window:
        row = {"strike": float(k)}
        for opt in ("CE", "PE"):
            c = sel[(sel["strike"] == k) & (sel["opt"] == opt)]
            if c.empty:
                row[opt.lower()] = None
                continue
            c = c.iloc[0]
            row[opt.lower()] = {"tsym": c["tsym"], **quotes.get(c["token"], {"ltp": None})}
        out_rows.append(row)
    ce_oi = sum((r["ce"] or {}).get("oi") or 0 for r in out_rows)
    pe_oi = sum((r["pe"] or {}).get("oi") or 0 for r in out_rows)
    exp_d = date.fromisoformat(expiry)
    data = {"underlying": underlying, "kind": rows.iloc[0]["kind"], "expiry": expiry,
            "days_to_expiry": (exp_d - date.today()).days, "lot": int(rows["lot"].max()),
            "tick": float(rows["tick"].min()), "spot": sp, "atm": float(strikes[atm_i]), "rows": out_rows,
            "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None,
            "as_of": datetime.now().isoformat(timespec="seconds")}
    _chains[ck] = (time.monotonic(), data)
    return data


def days_to_expiry(expiry: date, now: datetime | None = None) -> float:
    """Calendar days (fractional) until 15:30 IST on expiry day."""
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    now = now or datetime.now(ist)
    end = datetime(expiry.year, expiry.month, expiry.day, 15, 30, tzinfo=ist)
    return max((end - now).total_seconds() / 86400, 0.0)


def otm_contract(underlying: str, opt: str, spot_px: float, steps: int = 2, min_days: float = 5,
                 now: datetime | None = None) -> dict | None:
    """The contract options Auto-trade buys: on the nearest expiry at least `min_days` away, the `steps`-th strike
    out of the money (steps=1 is the first strike past the spot: above it for a call, below it for a put).
    None if there is no such expiry or strike."""
    df = contracts()
    rows = df[(df["underlying"] == underlying) & (df["opt"] == opt)]
    exps = sorted(e for e in rows["expiry"].unique() if days_to_expiry(e, now) >= min_days)
    if not exps:
        return None
    rows = rows[rows["expiry"] == exps[0]]
    strikes = sorted(rows["strike"].unique())
    otm = [k for k in strikes if k > spot_px] if opt == "CE" else [k for k in reversed(strikes) if k < spot_px]
    if len(otm) < max(steps, 1):
        return None
    return rows[rows["strike"] == otm[max(steps, 1) - 1]].iloc[0].to_dict()


def analyze_strategy(legs_in: list[dict]) -> dict:
    """Analyse a strategy (legs: tsym, side, lots, optional price) on live quotes: payoff curves, extremes,
    breakevens, Greeks, probability of profit. Each leg's IV comes from its live mid price."""
    from . import strategy
    if not legs_in:
        raise ValueError("Add at least one leg")
    cs = []
    for g in legs_in:
        c = contract(str(g.get("tsym", "")))
        if c is None:
            raise ValueError(f"Unknown option contract {g.get('tsym')}")
        cs.append(c)
    if len({(c["underlying"], str(c["expiry"])) for c in cs}) > 1:
        raise ValueError("All legs must be on the same underlying and expiry")
    quotes = {str(q.get("exchange_token")): parse_option_quote(q) for q in _quotes(sorted({c["token"] for c in cs}))}
    u, exp = cs[0]["underlying"], cs[0]["expiry"]
    sp = spot(u)
    s = sp["ltp"]
    if s is None:
        raise ValueError(f"No live price for {u}")
    days = days_to_expiry(exp)
    t = max(days / 365, strategy.MIN_T)
    legs = []
    for g, c in zip(legs_in, cs):
        q = quotes.get(c["token"], {})
        mid = (q["bid"] + q["ask"]) / 2 if q.get("bid") and q.get("ask") else q.get("ltp")
        lots = int(g.get("lots") or 1)
        price = float(g["price"]) if g.get("price") not in (None, "") else mid
        if price is None:
            raise ValueError(f"No price for {c['tsym']}")
        legs.append({"tsym": c["tsym"], "opt": c["opt"], "strike": float(c["strike"]),
                     "side": str(g.get("side", "BUY")).upper(), "lots": lots, "qty": lots * int(c["lot"]),
                     "price": price, "mid": mid, "ltp": q.get("ltp"), "bid": q.get("bid"), "ask": q.get("ask"),
                     "iv": strategy.implied_vol(c["opt"], mid or price, s, float(c["strike"]), t)})
    # Chart range: about 3 standard deviations of the expected move (from the legs' IV and time left)
    ivs = [g["iv"] for g in legs if g["iv"]]
    span = min(max(3 * (sum(ivs) / len(ivs) if ivs else 0.2) * math.sqrt(t), 0.02), 0.25)
    a = strategy.analyze(legs, s, days, span=span)
    return {**a, "legs": legs, "underlying": u, "expiry": str(exp), "days": round(days, 2), "spot": sp,
            "lot": int(cs[0]["lot"])}
