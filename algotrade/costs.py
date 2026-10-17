"""Trading costs for backtests: brokerage, statutory charges and slippage, turned into R per trade.

Rates approximate the published schedules (Indian equity charges, US regulatory fees) and change from time
to time; brokerage, DP charge and slippage can be overridden in .env. Slippage is charged on market-type
fills only (stop entries, gap fills at the open, stop-loss and time exits), not on limit fills at their price
(limit entries, target exits)."""
from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class CostModel:
    brokerage: float = 0.0     # per order (entry and exit), in the market's currency
    buy_pct: float = 0.0       # fraction of buy value (STT on delivery, stamp duty)
    sell_pct: float = 0.0      # fraction of sell value (STT, US regulatory fees)
    side_pct: float = 0.0      # fraction of each side's value (exchange transaction charge, SEBI fee)
    gst: float = 0.0           # GST rate on brokerage + side_pct charges
    sell_fixed: float = 0.0    # per sell order (depository / DP charge on delivery sales)
    slippage_bps: float = 0.0  # adverse price move on market-type fills, in basis points

    def fees(self, buy_value: float, sell_value: float) -> float:
        """Brokerage and charges of one round trip (one buy, one sell)."""
        taxable = 2 * self.brokerage + self.side_pct * (buy_value + sell_value)
        return (taxable * (1 + self.gst) + self.buy_pct * buy_value + self.sell_pct * sell_value
                + self.sell_fixed)

    def net_r(self, side: str, entry: float, exit_px: float, risk: float, qty: int,
              slip_entry: bool, slip_exit: bool) -> float:
        """R multiple after slippage and fees; risk is the planned risk per share."""
        if not risk:
            return 0.0
        sign = 1 if side == "BUY" else -1
        slip = self.slippage_bps / 1e4
        entry = entry * (1 + sign * slip) if slip_entry else entry    # pay more on a buy, get less on a short
        exit_px = exit_px * (1 - sign * slip) if slip_exit else exit_px
        qty = max(qty, 1)
        buy, sell = (entry, exit_px) if sign > 0 else (exit_px, entry)
        pnl = sign * (exit_px - entry) * qty - self.fees(buy * qty, sell * qty)
        return pnl / (risk * qty)


_NSE_SIDE = 0.0000297 + 0.000001   # NSE transaction charge 0.00297% + SEBI fee ₹10 / crore


def model_for(market: str, timeframe: str = "1d") -> CostModel:
    """Cost model for a market; daily timeframe = delivery (swing) trades, intraday = MIS charges."""
    if market == "US":
        return CostModel(brokerage=config.US_BROKERAGE, sell_pct=0.00003,  # SEC + FINRA fees, rounded up
                         slippage_bps=config.US_SLIPPAGE_BPS)
    if timeframe == "1d":   # delivery: STT 0.1% each side, stamp duty 0.015% on the buy, DP charge per sale
        return CostModel(brokerage=config.BROKERAGE, buy_pct=0.001 + 0.00015, sell_pct=0.001,
                         side_pct=_NSE_SIDE, gst=0.18, sell_fixed=config.DP_CHARGE,
                         slippage_bps=config.SLIPPAGE_BPS)
    # intraday: STT 0.025% on the sell, stamp duty 0.003% on the buy, no DP charge
    return CostModel(brokerage=config.BROKERAGE, buy_pct=0.00003, sell_pct=0.00025,
                     side_pct=_NSE_SIDE, gst=0.18, slippage_bps=config.SLIPPAGE_BPS)
