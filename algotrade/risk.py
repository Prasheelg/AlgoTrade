"""Portfolio risk (dashboard Portfolio > Risk): what you'd lose if every stop hit, against MAX_OPEN_RISK_PCT, and
where the money sits by sector.

- App trades (Auto-trade) have stops: risk = (fill or entry - stop) x qty, and "now" from the live price.
- Holdings the app doesn't manage have no stop: their whole value is listed as unprotected.
Read-only: Kotak holdings come from portfolio.report() (cached), prices from Kotak quotes."""
import logging

from . import config, kotak, markets, orders, universe

log = logging.getLogger(__name__)


def report(holdings: list[dict] | None = None) -> dict:
    """holdings: normalised Kotak holdings (portfolio.report()["holdings"]); read here when not given."""
    if holdings is None:
        try:
            from . import portfolio
            holdings = portfolio.report().get("holdings") or []
        except Exception:
            log.exception("Holdings unavailable for the risk view")
            holdings = []
    capital = markets.get("IN").capital
    live = [t for t in orders.all_trades() if t["state"] in orders.ACTIVE_STATES and t["qty"]]
    try:
        ltp = {s: q["ltp"] for s, q in kotak.quotes(sorted({t["symbol"] for t in live})).items()}
    except Exception:
        ltp = {}
    sect = universe.sectors()
    rows = []
    for t in live:
        px = ltp.get(t["symbol"])
        base = t["fill_price"] or t["entry"]
        rows.append({"symbol": t["symbol"], "kind": "pending entry" if t["state"] in (orders.NEW, orders.ENTRY) else "position",
                     "qty": t["qty"], "price": base, "stop": t["stop"], "ltp": px,
                     "value": round((px or base) * t["qty"], 2), "risk": round(orders.trade_risk(t), 2),
                     "risk_now": round(max((px or base) - t["stop"], 0) * t["qty"], 2) if t["stop"] else None,
                     "sector": sect.get(t["symbol"], "Other")})
    managed = {t["symbol"] for t in live}
    for h in holdings:
        if h["symbol"] in managed or not h.get("qty"):
            continue
        val = h.get("value") or (h.get("ltp") or h.get("avg") or 0) * h["qty"]
        rows.append({"symbol": h["symbol"], "kind": "holding (no stop)", "qty": h["qty"], "price": h.get("avg"),
                     "stop": None, "ltp": h.get("ltp"), "value": round(val, 2), "risk": None, "risk_now": None,
                     "sector": sect.get(h["symbol"], "Other")})
    total_value = sum(r["value"] for r in rows) or 0.0
    by_sector: dict[str, float] = {}
    for r in rows:
        by_sector[r["sector"]] = by_sector.get(r["sector"], 0.0) + r["value"]
    sectors = sorted(({"sector": k, "value": round(v, 2), "pct": round(v / total_value * 100, 1) if total_value else 0}
                      for k, v in by_sector.items()), key=lambda x: -x["value"])
    at_risk = round(sum(r["risk"] or 0 for r in rows), 2)
    limit = round(capital * config.MAX_OPEN_RISK_PCT / 100, 2)
    return {"capital": capital, "at_risk": at_risk, "at_risk_pct": round(at_risk / capital * 100, 2) if capital else None,
            "at_risk_now": round(sum(r["risk_now"] or 0 for r in rows), 2),
            "limit": limit, "limit_pct": config.MAX_OPEN_RISK_PCT, "room": round(max(limit - at_risk, 0), 2),
            "unprotected": round(sum(r["value"] for r in rows if r["stop"] is None), 2),
            "risk_per_trade": round(capital * config.RISK_PER_TRADE_PCT / 100, 2),
            "positions": sorted(rows, key=lambda r: -(r["risk"] or 0)), "sectors": sectors,
            "largest_sector": sectors[0] if sectors else None}
