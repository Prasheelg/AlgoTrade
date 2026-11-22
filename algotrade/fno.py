"""Option chains for NSE index and stock options from Kotak Neo (read-only: scrip master and quotes).

Contracts come from Kotak's nse_fo scrip master (downloaded once a day, cached in DATA_DIR); prices, volume,
open interest and best bid / ask from quotes on the consumer key alone, like kotak.py. Nothing here can place
an order; manual.py does that."""
import io
import logging
import math
import time
from datetime import date, datetime, timedelta

import pandas as pd

from . import config, kotak, livefeed, oibook

log = logging.getLogger(__name__)
SEGMENT = "nse_fo"
BSE_SEGMENT = "bse_fo"
EPOCH_1980 = 315532800      # scrip master expiry dates count seconds from 1980-01-01
QUOTE_BATCH = kotak.BATCH   # Kotak takes at most 49 instruments per quote call
CHAIN_CACHE_SEC = 2         # one quote round per chain for this long, however many tabs poll
LIVE_CHAIN_CACHE_SEC = 0.2  # while the live stream is connected (prices come from memory)
# Index option underlyings -> Kotak quote name of the index (nse_cm)
INDEX_QUOTE = {"NIFTY": "Nifty 50", "BANKNIFTY": "Nifty Bank", "FINNIFTY": "Nifty Fin Service",
               "MIDCPNIFTY": "NIFTY MID SELECT", "NIFTYNXT50": "Nifty Next 50"}
# BSE index options (bse_fo), read for the 0DTE condor only: underlying -> Kotak quote name of the index (bse_cm).
# They stay out of underlyings() (the Options tab and its orders are NSE only).
BSE_INDEX_QUOTE = {"SENSEX": "SENSEX"}

_master = {"day": None, "df": None, "fut": None}
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
        "segment": SEGMENT,
    })
    df = df[df["underlying"].isin(INDEX_QUOTE) | (df["kind"] == "stock")]
    return df.dropna(subset=["strike", "expiry"]).reset_index(drop=True)


def parse_bse_master(csv_text: str) -> pd.DataFrame:
    """The BSE index options in BSE_INDEX_QUOTE from the bse_fo scrip master (instrument type "IO"; its expiry
    dates are plain Unix seconds, not counted from 1980 like nse_fo's)."""
    m = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False)
    m.columns = [c.strip().rstrip(";") for c in m.columns]
    m = m[(m["pInstType"].str.strip() == "IO") & m["pSymbolName"].str.strip().isin(BSE_INDEX_QUOTE)]
    exp = pd.to_numeric(m["pExpiryDate"], errors="coerce")
    df = pd.DataFrame({
        "underlying": m["pSymbolName"].str.strip(), "kind": "index",
        "expiry": pd.to_datetime(exp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date,
        "strike": pd.to_numeric(m["dStrikePrice"], errors="coerce") / 100,
        "opt": m["pOptionType"].str.strip(),
        "lot": pd.to_numeric(m["lLotSize"], errors="coerce").fillna(0).astype(int),
        "token": m["pSymbol"].str.strip(), "tsym": m["pTrdSymbol"].str.strip(),
        "tick": pd.to_numeric(m["dTickSize"], errors="coerce").fillna(5) / 100,
        "segment": BSE_SEGMENT,
    })
    return df[df["opt"].isin(["CE", "PE"])].dropna(subset=["strike", "expiry"]).reset_index(drop=True)


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


def parse_futures(csv_text: str) -> pd.DataFrame:
    """Index and stock futures from the nse_fo scrip master: underlying, expiry, token, tsym, lot."""
    m = pd.read_csv(io.StringIO(csv_text), dtype=str, low_memory=False)
    m.columns = [c.strip().rstrip(";") for c in m.columns]
    m = m[m["pInstType"].str.strip().isin(["FUTIDX", "FUTSTK"])]
    exp = pd.to_numeric(m["pExpiryDate"], errors="coerce") + EPOCH_1980
    return pd.DataFrame({
        "underlying": m["pSymbolName"].str.strip(),
        "expiry": pd.to_datetime(exp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.date,
        "token": m["pSymbol"].str.strip(), "tsym": m["pTrdSymbol"].str.strip(),
        "lot": pd.to_numeric(m["lLotSize"], errors="coerce").fillna(0).astype(int),
    }).dropna(subset=["expiry"]).reset_index(drop=True)


def contracts() -> pd.DataFrame:
    """All option contracts, refreshed once a day (cached on disk across restarts)."""
    today = date.today()
    if _master["day"] != today:
        text = _master_text(SEGMENT)
        df = parse_master(text)
        try:
            fut = parse_futures(text)
            fut = fut[fut["expiry"] >= today].reset_index(drop=True)
        except Exception as exc:     # futures are extra information; options must still load
            log.warning("No futures in the F&O scrip master: %s", exc)
            fut = None
        try:
            df = pd.concat([df, parse_bse_master(_master_text(BSE_SEGMENT))], ignore_index=True)
        except Exception as exc:     # BSE (SENSEX) options are extra; NSE's must still load
            log.warning("BSE F&O scrip master unavailable: %s", exc)
        _master.update(day=today, df=df[df["expiry"] >= today].reset_index(drop=True), fut=fut)
        log.info("Kotak F&O scrip master: %d option contracts", len(_master["df"]))
    return _master["df"]


def futures(underlying: str, n: int = 3) -> list[dict]:
    """The nearest `n` futures of an underlying with their live price (empty if none)."""
    contracts()
    fut = _master.get("fut")
    if fut is None:
        return []
    rows = fut[fut["underlying"] == underlying].sort_values("expiry").head(n)
    if rows.empty:
        return []
    try:
        quotes = {str(q.get("exchange_token")): q for q in _quotes(list(rows["token"]))}
    except Exception as exc:
        log.debug("Futures quotes failed: %s", exc)
        quotes = {}
    return [{"tsym": r["tsym"], "expiry": str(r["expiry"]), "ltp": _f(quotes.get(r["token"], {}).get("ltp"))}
            for _, r in rows.iterrows()]


def underlyings() -> list[dict]:
    """Index underlyings first (in INDEX_QUOTE order), then stocks A-Z, with their lot size."""
    df = contracts()
    lots = df.groupby("underlying")["lot"].max().to_dict()
    idx = [{"name": u, "kind": "index", "lot": lots[u]} for u in INDEX_QUOTE if u in lots]
    stocks = sorted(set(df.loc[df["kind"] == "stock", "underlying"]))
    return idx + [{"name": u, "kind": "stock", "lot": lots[u]} for u in stocks]


def segment_of(c: dict | None) -> str:
    """The exchange segment a contract (a contracts() row) trades on."""
    return str((c or {}).get("segment") or SEGMENT)


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
    res = _quotes([c["token"]], segment_of(c))
    if not res:
        raise RuntimeError(f"No quote for {tsym}")
    return parse_option_quote(res[0])


def spot(underlying: str) -> dict:
    """Underlying price: the index quote (NSE, or BSE for SENSEX), or the stock's NSE quote."""
    seg = "bse_cm" if underlying in BSE_INDEX_QUOTE else "nse_cm"
    token = BSE_INDEX_QUOTE.get(underlying) or INDEX_QUOTE.get(underlying) or kotak.tokens().get(underlying)
    q = {}
    if token:
        try:
            res = _quotes([token], seg)
        except RuntimeError:
            res = []
        q = res[0] if res else {}
    ltp, prev = _f(q.get("ltp")), _f((q.get("ohlc") or {}).get("close"))
    return {"ltp": ltp, "change": ltp - prev if ltp is not None and prev else None,
            "pct": (ltp - prev) / prev * 100 if ltp is not None and prev else None}


def chain(underlying: str, expiry: str, width: int = 10, fresh: bool = False) -> dict:
    """Option chain around the money: `width` strikes either side of the strike nearest the spot, each leg with
    its change in OI since the previous session (oibook). Cached CHAIN_CACHE_SEC per (underlying, expiry, width)."""
    ck = (underlying, expiry, width)
    hit = _chains.get(ck)
    if not fresh:
        oibook.viewed(underlying, expiry, width)
    if hit and not fresh and time.monotonic() - hit[0] < (LIVE_CHAIN_CACHE_SEC if livefeed.connected() else CHAIN_CACHE_SEC):
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
    quotes = {str(q.get("exchange_token")): parse_option_quote(q)
              for q in _quotes(list(sel["token"]), segment_of(rows.iloc[0].to_dict()))}
    out_rows = []
    for k in window:
        row = {"strike": float(k)}
        for opt in ("CE", "PE"):
            c = sel[(sel["strike"] == k) & (sel["opt"] == opt)]
            if c.empty:
                row[opt.lower()] = None
                continue
            c = c.iloc[0]
            q = quotes.get(c["token"], {"ltp": None})
            row[opt.lower()] = {"tsym": c["tsym"], **q, **oibook.record(c["token"], q.get("time"), q.get("oi"))}
        out_rows.append(row)
    ce_oi = sum((r["ce"] or {}).get("oi") or 0 for r in out_rows)
    pe_oi = sum((r["pe"] or {}).get("oi") or 0 for r in out_rows)
    chg = {k: [(r[k] or {}).get("oi_chg") for r in out_rows if r[k]] for k in ("ce", "pe")}
    exp_d = date.fromisoformat(expiry)
    data = {"underlying": underlying, "kind": rows.iloc[0]["kind"], "expiry": expiry,
            "days_to_expiry": (exp_d - date.today()).days, "lot": int(rows["lot"].max()),
            "tick": float(rows["tick"].min()), "spot": sp, "atm": float(strikes[atm_i]), "rows": out_rows,
            "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None,
            # total change in OI over the strikes shown, once every leg has a previous-day figure
            "ce_oi_chg": sum(chg["ce"]) if chg["ce"] and None not in chg["ce"] else None,
            "pe_oi_chg": sum(chg["pe"]) if chg["pe"] and None not in chg["pe"] else None,
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


def analyze_strategy(legs_in: list[dict], target_days: float = 0.0, iv_offset: float = 0.0) -> dict:
    """Analyse a strategy (legs: tsym, side, lots, optional price, optional iv in %) on live quotes: payoff curves
    (at expiry and on a target date `target_days` from now), extremes, breakevens, Greeks, probability of
    profit, standard deviations, the expiry's OI by strike and the underlying's futures. Each leg's IV comes from
    its live mid price, unless the leg sets its own; `iv_offset` (volatility points) shifts every leg's IV."""
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
    off = float(iv_offset or 0) / 100
    for g, c in zip(legs_in, cs):
        q = quotes.get(c["token"], {})
        mid = (q["bid"] + q["ask"]) / 2 if q.get("bid") and q.get("ask") else q.get("ltp")
        lots = int(g.get("lots") or 1)
        price = float(g["price"]) if g.get("price") not in (None, "") else mid
        if price is None:
            raise ValueError(f"No price for {c['tsym']}")
        k = float(c["strike"])
        iv_mkt = strategy.implied_vol(c["opt"], mid or price, s, k, t)
        iv_set = float(g["iv"]) / 100 if g.get("iv") not in (None, "") else None
        iv = iv_set if iv_set is not None else iv_mkt
        iv = max(iv + off, 0.001) if iv is not None else None
        intrinsic = max(s - k, 0.0) if c["opt"] == "CE" else max(k - s, 0.0)
        legs.append({"tsym": c["tsym"], "opt": c["opt"], "strike": k,
                     "side": str(g.get("side", "BUY")).upper(), "lots": lots, "qty": lots * int(c["lot"]),
                     "price": price, "mid": mid, "ltp": q.get("ltp"), "bid": q.get("bid"), "ask": q.get("ask"),
                     "iv": iv, "iv_market": iv_mkt, "iv_set": iv_set is not None,
                     "intrinsic": intrinsic, "unit_greeks": strategy.greeks(c["opt"], s, k, t, iv or 0.0)})
    # Chart range: about 3 standard deviations of the expected move (from the legs' IV and time left)
    ivs = [g["iv"] for g in legs if g["iv"]]
    span = min(max(3 * (sum(ivs) / len(ivs) if ivs else 0.2) * math.sqrt(t), 0.02), 0.25)
    target = min(max(float(target_days or 0), 0.0), days)
    a = strategy.analyze(legs, s, days, span=span, target_days=target)

    # The expiry's OI by strike (drawn behind the payoff) and its at-the-money IV (for the standard deviation)
    atm_iv, oi = None, []
    try:
        ch = chain(u, str(exp), 25)
        atm = next((r for r in ch["rows"] if r["strike"] == ch["atm"]), None)
        if atm:
            found = [strategy.implied_vol(o, (atm[o.lower()] or {}).get("ltp"), s, atm["strike"], t) for o in ("CE", "PE")]
            found = [v for v in found if v]
            atm_iv = sum(found) / len(found) if found else None
        lo_x, hi_x = a["x"][0], a["x"][-1]
        oi = [{"strike": r["strike"], "ce": (r["ce"] or {}).get("oi"), "pe": (r["pe"] or {}).get("oi")}
              for r in ch["rows"] if lo_x <= r["strike"] <= hi_x]
    except Exception as exc:
        log.debug("Chain for the strategy's OI / ATM IV failed: %s", exc)
    sd_iv = atm_iv if atm_iv else (sum(ivs) / len(ivs) if ivs else None)
    if sd_iv and atm_iv and off:
        sd_iv = max(sd_iv + off, 0.001)
    sd = None
    if sd_iv:
        pts = s * sd_iv * math.sqrt(max(days, 0) / 365)
        sd = {"iv": round(sd_iv, 4), "points": round(pts, 2), "pct": round(pts / s * 100, 2),
              "one": [round(s - pts, 2), round(s + pts, 2)], "two": [round(s - 2 * pts, 2), round(s + 2 * pts, 2)]}

    # P&L table at the strikes in range (and the spot), on the target date and at expiry
    marks = sorted({*(r["strike"] for r in oi), round(s, 2)})
    t_left = max(days - target, 0) / 365
    at_target = (lambda m: strategy.payoff_at(legs, m, t_left if target else max(t_left, strategy.MIN_T))
                 if t_left > 1e-9 else strategy.payoff_expiry(legs, m))
    table = [{"price": m, "pnl_target": round(at_target(m), 2),
              "pnl_expiry": round(strategy.payoff_expiry(legs, m), 2)} for m in marks]

    sign = lambda g: 1 if g["side"] == "BUY" else -1
    intrinsic = sum(sign(g) * g["qty"] * g["intrinsic"] for g in legs)
    time_value = sum(sign(g) * g["qty"] * ((g["mid"] if g["mid"] is not None else g["price"]) - g["intrinsic"])
                     for g in legs)
    ml, mp = a["max_loss"], a["max_profit"]
    rr = mp / -ml if mp is not None and ml is not None and ml < 0 else None
    from zoneinfo import ZoneInfo
    target_at = datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=target)
    return {**a, "legs": legs, "underlying": u, "expiry": str(exp), "days": round(days, 2), "spot": sp,
            "lot": int(cs[0]["lot"]), "target_days": round(target, 4),
            "target_at": target_at.isoformat(timespec="minutes"), "iv_offset": float(iv_offset or 0),
            "sd": sd, "atm_iv": atm_iv, "oi": oi, "table": table,
            "intrinsic_value": round(intrinsic, 2), "time_value": round(time_value, 2),
            "reward_risk": round(rr, 3) if rr is not None else None, "futures": futures(u),
            "margin": strategy.margin_estimate(legs, s, index=cs[0].get("kind") == "index")}
