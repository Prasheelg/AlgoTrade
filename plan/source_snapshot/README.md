# AlgoTrade — NSE Scanner with Telegram Alerts

Scans NSE stocks (NIFTY 50/100/200/500 or a custom list), scores them with 5 technical
strategies, and sends trade suggestions (entry, stop loss, targets, position size) to Telegram.

## Setup
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```
Credentials and settings go in `.env` (see `.env.example`).

## Usage
| Command | What it does |
|---|---|
| `run.bat --test-telegram` | Send a test message to the group |
| `run.bat --dry-run` | Scan now and print signals without sending them |
| `run.bat --once` | Scan now and send new signals |
| `run.bat` | Run all the time: scan India 09:15–15:30 IST and US 09:30–16:00 ET every N min, plus one end-of-day scan per market |
| `run.bat --ui` | Signals dashboard at http://127.0.0.1:8050 (tracks SL/target hits, refreshes every 5 min) |
| `run.bat --backtest --market IN --symbols NIFTY100` | Backtest the strategy (trades saved to `data/backtest_trades.csv`, US to `backtest_trades_US.csv`) |

Add `--market IN` or `--market US` to any command to limit it to one market; by default both are scanned.
`--symbols` needs `--market`.

## Strategy
A signal is sent when at least `MIN_SCORE` of these agree on a direction:
EMA 9/21 cross (EMA50 filter) · 20-bar breakout on 1.5x volume with ADX > 20 · Supertrend (10, 3) flip ·
MACD cross aligned with EMA200 · RSI pullback in trend · support/resistance bounce · supply/demand zone retests
(the five zone patterns share one vote).

Opt-in strategies (unticked on the dashboard; add them to `STRATEGIES` in `.env` for the scanner):
**Squeeze Breakout** (Bollinger Bands inside Keltner Channels for 5+ bars, then a close outside the bands) and
**RSI(2) Reversion** (RSI(2) < 10 while price > EMA200).

Optional filters veto signals using the market index (NIFTY 50 / S&P 500), set on the dashboard or `FILTERS` in `.env`:
**Market trend** (BUY only while the index is above its 200-bar EMA) and **Relative strength**
(BUY only stocks that beat the index over the last 63 bars).

**Entry is a pending order, not the current price**, so there is time to place it:
- bounce setups (Support-Resistance, supply/demand zones, RSI pullback) → **limit** order 0.5 × ATR below the close (BUY);
- momentum setups (20-bar breakout, EMA / MACD cross, Supertrend) → **stop** order 0.1 × ATR above the signal bar's high (BUY);
- mixed votes go to whichever kind has more votes (ties → limit). SELL setups mirror this.

The order is valid for `ENTRY_VALID_BARS` (3) bars; intraday orders also lapse at that day's close.
Stop loss = 1.5 × ATR from the entry (or a zone / level stop); targets at 1.5R and 3R; quantity sized so one loss costs `RISK_PER_TRADE_PCT` of `CAPITAL`.
Filters: price ≥ ₹50 and 20-day average turnover ≥ ₹5 Cr. Alerts are de-duplicated per symbol, side and day (`data/signals.db`).

## Dashboard scans
From the dashboard you can run a scan with your own settings for that run: timeframe (5 min, 15 min, 30 min,
1 hour, daily), which strategies to use, and how many must agree. Your last choices are remembered in the browser.
The continuous scanner (`run.bat`) still uses the settings in `.env`.

## Markets
The dashboard has two markets, **India** and **US**, switched at the top of the page. Each has its own
signals, scan settings and scan button, and both can scan at the same time.
US scans use `US_UNIVERSE` (`SP100` by default, or `SP500`, or comma-separated tickers) and `US_CAPITAL`
(in dollars), with filters of price ≥ $5 and 20-day average turnover ≥ $20M. US alerts go to the same
Telegram group, marked 🇺🇸 and priced in $. The continuous scanner (`run.bat`) runs both markets with the same
settings (`TIMEFRAME`, `MIN_SCORE`, `SCAN_INTERVAL_MIN`, all strategies), each during its own trading hours
(US hours follow New York time, so daylight saving is handled).

## Signal tracking
Each signal is followed on bars of its own timeframe: **Pending** until the entry is reached
(**Cancelled** if it isn't in time; filled at the open if price gaps through it) **→ Active → T1 hit** (stop moves to entry) **→ T2 hit** or **Breakeven**;
**SL hit** closes it; daily signals still open after 20 sessions are **Expired**; intraday signals are closed at the end of their trading day. Status changes (including fills and cancellations)
are posted to Telegram for signals that were sent there.

## Notes
- Data comes from Yahoo Finance (free). Intraday data is about 15 minutes delayed. For real-time data,
  swap `algotrade/data.py` for a broker API (Zerodha Kite, Upstox, Angel SmartAPI).
- NSE and US market holidays are not handled; on a holiday the scanner runs but finds no new bars.
- Backtest (5y, daily, long-only, score ≥ 2, pending entries, before costs and slippage):
  NIFTY100: 2,654 trades, 44% win rate, profit factor 1.15, +0.08R per trade;
  S&P 100: 2,689 trades, 45% win rate, profit factor 1.22, +0.12R per trade.
  About 20% of setups never reach their entry. Shorts were unprofitable, so `ALLOW_SHORTS=false` by default.
- Best single strategy in the same test: **RSI(2) Reversion on its own** (min votes 1): +0.16R (India) / +0.18R (US)
  per trade, profit factor 1.31 / 1.33; with both filters +0.24R / +0.19R and about half the drawdown.
  Inside the main vote it adds little, because it rarely agrees with the trend strategies.
  Squeeze Breakout was roughly break-even on its own. Supertrend Flip (India) and 20-bar Breakout were the weakest.
- Educational tool, not investment advice.
