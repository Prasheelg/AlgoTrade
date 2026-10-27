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
| `run.bat` | Run all the time: scan India 09:00–15:30 IST and US 09:30–16:00 ET every `SCAN_INTERVAL_MIN` (10) min on every timeframe, plus one end-of-day scan per market |
| `run.bat --ui` | Signals dashboard at http://127.0.0.1:8050 (tracks SL/target hits, refreshes every 5 min); the **Scan India** / **Scan US** switch in each market's scan panel turns that market's scanning on or off (starting the continuous scanner if needed, stopping it when both are off), output in `logs/scanner.log` |
| `run.bat --backtest --market IN --symbols NIFTY100` | Backtest the strategy, before and after costs (trades saved to `data/backtest_trades.csv`, US to `backtest_trades_US.csv`) |
| `run.bat --ml-test --market IN` | Walk-forward test of the AI signal filter (see Meta-labeling test); 10 years of daily bars, about 20 min per 100 symbols |
| `run.bat --nr7-backtest` | Backtest the NR7 index breakout on Nifty 50, Bank Nifty and Sensex (see NR7 index breakout); shown on the dashboard's NR7 view |

Add `--market IN` or `--market US` to any command to limit it to one market; by default both are scanned.
`--symbols` needs `--market`.

## Strategy
A signal is sent when at least `MIN_SCORE` of these agree on a direction:
EMA 9/21 cross (EMA50 filter) · 20-bar breakout on 1.5x volume with ADX > 20 · Supertrend (10, 3) flip ·
MACD cross aligned with EMA200 · RSI pullback in trend · support/resistance bounce · supply/demand zone retests
(the five zone patterns share one vote) · **Squeeze Breakout** (Bollinger Bands inside Keltner Channels for 5+ bars,
then a close outside the bands) · **RSI(2) Reversion** (RSI(2) < 10 while price > EMA200).
All are on by default; untick them on the dashboard or list a subset in `STRATEGIES` in `.env`.

Optional filters veto signals using the market index (NIFTY 50 / S&P 500), set on the dashboard or `FILTERS` in `.env`:
**Market trend** (BUY only while the index is above its 200-bar EMA) and **Relative strength**
(BUY only stocks that beat the index over the last 63 bars). **Events** (`events`) skips stocks with quarterly
results or an ex-dividend date within `EVENT_WINDOW_DAYS` (3) days, from Yahoo's calendar (good for large caps,
patchy for small ones; live scans only, backtests ignore it).

### Per-market scanner settings
**The scan panel on the dashboard is the scanner's settings.** For each market it holds the strategies, the
minimum votes, the filters and *Send to Telegram*; changes are saved on the PC (in `signals.db`) and the scanner
(`run.bat`) reads them at the start of every loop, so a change applies from the next scan, without a restart.
Changing them needs the same access as placing an order. `--once`, `--dry-run` and `--backtest` use them too.
Strategies switched off in the strategy scorecard are still left out.

Every loop scans each timeframe in `SCAN_TIMEFRAMES` (`5m,15m,30m,1h,1d`) one after another, every
`SCAN_INTERVAL_MIN` (10) minutes counted from the start of the previous scan, within each market's scan window:
**India 09:00–15:30 IST** (from before the 09:15 open), US 09:30–16:00 ET, weekdays. Instead of a Run scan button
the panel shows the scanner's status: *Scanning 15m*, *Next scan 10:20*, *Idle* outside the window, or *Scanner not
running* when `run.bat` isn't up; the tooltip has the last result. When a scan finds something new the signals
list updates by itself.

Until a market's panel is first changed, it starts from `.env`: `STRATEGIES_IN`, `MIN_SCORE_IN` and `FILTERS_IN`
for India, `STRATEGIES_US`, `MIN_SCORE_US` and `FILTERS_US` for the US, else `STRATEGIES`, `MIN_SCORE` and
`FILTERS`. The startup message on Telegram lists what each market scans.

**India's `.env` is RSI Pullback or RSI(2) Reversion (min score 1) with both filters**, the only India setup that
was still positive after costs in the backtest (see Notes):
```
STRATEGIES_IN=RSI Pullback,RSI(2) Reversion
MIN_SCORE_IN=1
FILTERS_IN=trend,rs
```

**Entry is a pending order, not the current price**, so there is time to place it:
- bounce setups (Support-Resistance, supply/demand zones, RSI pullback) → **limit** order 0.5 × ATR below the close (BUY);
- momentum setups (20-bar breakout, EMA / MACD cross, Supertrend) → **stop** order 0.1 × ATR above the signal bar's high (BUY);
- mixed votes go to whichever kind has more votes (ties → limit). SELL setups mirror this.

The order is valid for `ENTRY_VALID_BARS` (3) bars; intraday orders also lapse at that day's close.
Stop loss = 1.5 × ATR from the entry (or a zone / level stop); targets at 1.5R and 3R; quantity sized so one loss costs `RISK_PER_TRADE_PCT` of `CAPITAL`.
Filters: price ≥ ₹50 and 20-day average turnover ≥ ₹5 Cr. Alerts are de-duplicated per symbol, side and day (`data/signals.db`).

## NR7 index breakout
Toby Crabel's NR7 on **Nifty 50, Bank Nifty and Sensex**, run on its own (`algotrade/nr7.py`), apart from the
strategies above: it doesn't vote, and the scan panel's settings don't apply to it. **Alerts only**: it places no orders.
- **Evening scan**, after 16:00 IST: if an index's daily range (high − low) is the narrowest of the last 7 days, it
  gets a setup for the **next session only**: buy above that day's high, sell below its low.
- **Next session**: the first level traded is the trade, the other is cancelled. Stop at the NR7 day's other end
  (1R = its range), target 3R from the entry, closed at 15:15 on the 5th session (entry day = 1). Neither level
  traded: the setup expires. One trade per index at a time; a stopped trade is not reversed.
- It is followed on Yahoo's 1-minute index bars (checked every 30 s in market hours) by the dashboard
  (`run.bat --ui`), so the dashboard must be running; the continuous scanner isn't needed.
- **Lots**: futures lots that risk at most `RISK_PER_TRADE_PCT` of `CAPITAL` at the NR7 range (lot sizes from
  Kotak's contract list, else 65 / 30 / 20). 0 means one lot risks more than that.
- **What to trade** (`algotrade/nr7_instruments.py`, Kotak contracts and quotes), shown under each waiting setup or
  open trade and added to its Telegram alerts:
  - **Futures** (Nifty, Bank Nifty): the current month, the next once fewer than 5 trading days are left, with the
    levels shifted by the future's premium over the index. Sensex futures barely trade, so Sensex is options only.
  - **Options**: the monthly expiry (same roll), a call for the buy level and a put for the sell level, in the money
    there with delta closest to 0.7 (0.6–0.8). Lots keep the premium paid within the risk per trade; the rough loss
    per lot if the index goes back to the stop is shown too.
- The dashboard's **NR7** view has its own **Watch** and **Send to Telegram** switches (Telegram off by default),
  **Skip days** for events (Budget day and the day before, election results), a **Scan now** button and the setups
  and trades with their R.
- `run.bat --nr7-backtest` runs the same rules on daily candles, conservatively: a day that trades both levels is a
  stop-out, no target on the entry day, a gap past a level or the stop fills at the open; costs 0.017% / 0.022% /
  0.015% of the index per round trip. Weekend special sessions (Muhurat) are left out.

**F&O stocks: not added, the research says no.** `run.bat --nr7-stocks` runs the same rules on today's F&O stocks
(daily, 0.10% cost a round trip): 212 stocks, about 98,000 trades since 1991, −0.06R a trade, no better from 2013
or on either side. `run.bat --nr7-results` tests the one variant worth a look, an NR7 taken only when results are due
within 5 trading days (fixed before it was run; Yahoo's results dates, so 2015 on): 3,482 trades, +0.004R a trade
(p ≈ 0.45), −0.06R with costs doubled, no consistent years. Results land in `data/nr7_backtest_stocks.json` and
`data/nr7_backtest_results_variant.json`.

## Dashboard scans
From the dashboard you can run a scan with your own settings for that run: timeframe (5 min, 15 min, 30 min,
1 hour, daily), which strategies to use, and how many must agree. Your last choices are remembered in the browser.
The continuous scanner (`run.bat`) still uses the settings in `.env`.

## Performance view
The **Performance** switch at the top of the dashboard shows how the signals have actually done, per market:
win rate, average R, profit factor, total R and maximum drawdown for closed trades (target 2, stop loss,
breakeven or time exit; cancelled entries and open trades are not counted). Every figure is shown before and
after costs, using the same cost model as the backtest, with an equity curve of cumulative R, a table of the
trades, and breakdowns by strategy, timeframe, exit, order type and side. If a backtest has been run with costs,
its average R per trade is shown alongside for comparison. Pick all time, 90 days or 30 days. Fewer than
30 closed trades are flagged as too few to judge. Link straight to it with `#IN/performance` or `#US/performance`.

Intraday signals have very tight stops (often 0.1–0.5% of the price), so fixed brokerage and slippage can cost
more than 1R per trade; the view makes that visible.

## Markets
The dashboard has two markets, **India** and **US**, switched at the top of the page. Each has its own
signals, scan settings and scanner status.
US scans use `US_UNIVERSE` (`SP100` by default, or `SP500`, or comma-separated tickers) and `US_CAPITAL`
(in dollars), with filters of price ≥ $5 and 20-day average turnover ≥ $20M. US alerts go to the same
Telegram group, marked 🇺🇸 and priced in $. The continuous scanner (`run.bat`) runs both markets with the same
`SCAN_TIMEFRAMES` and `SCAN_INTERVAL_MIN` (strategies, min score and filters differ per market, see
[Per-market scanner settings](#per-market-scanner-settings)), each during its own scan window
(US hours follow New York time, so daylight saving is handled).

## Backtest costs
The backtest reports results twice: before costs (`R`) and after costs (`net_R`, with `cost_R` the difference;
both columns are in the trades CSV). The cost model is in `algotrade/costs.py`:
- **India, daily (delivery):** STT 0.1% on buy and sell, stamp duty 0.015% on the buy, NSE transaction charge
  0.00297% and SEBI fee on each side, 18% GST on brokerage and those charges, `BROKERAGE` per order (₹20) and
  `DP_CHARGE` per sale (₹20).
- **India, intraday:** STT 0.025% on the sell, stamp duty 0.003% on the buy, same exchange charges and brokerage, no DP charge.
- **US:** `US_BROKERAGE` per order ($0) and about 0.003% regulatory fees on the sell.
- **Slippage** (`SLIPPAGE_BPS` 5 for India, `US_SLIPPAGE_BPS` 2 for the US) is charged on market-type fills: stop
  entries, gap fills at the open, stop-loss and time exits. Limit fills at their price (limit entries, target exits) have none.

Fixed charges are spread over the signal's position size (`CAPITAL` × `RISK_PER_TRADE_PCT`), so a larger
account pays less per trade in R. Charges change from time to time; check your broker's schedule and set the
values in `.env`.

## Meta-labeling test (AI signal filter)
`--ml-test` checks whether a machine-learning model can pick out which signals tend to work. The strategies
still decide BUY / SELL; a small gradient-boosted tree model (`algotrade/metalabel.py`, scikit-learn, runs
locally) scores each signal from the market conditions on its bar: ADX, RSI, distance from the 50 / 200 EMA,
ATR %, volume ratio, recent returns, relative strength, the index's trend, stop size and which strategies voted.
It learns from every signal the strategies give (all strategies, min score 1, no filters) on 10 years of daily
bars, and is judged **walk-forward**: each year is scored by a model trained only on trades closed before that
year. A signal is kept when the model rates it above an average one. The report shows, after costs, all signals
against the kept and skipped ones, per year, and by quintile of the model's score, for every signal and for the
scan panel's current setup. Trades are saved to `data/ml_trades_<market>.csv` (`_current` for the current setup).

Result (October 2026, NIFTY 100 and S&P 100, 2018–2026): **no reliable edge, so it is not used for live signals.**
India every signal: −0.10R per trade, kept −0.07R; current setup −0.17R, kept −0.26R (worse). US every signal:
+0.07R, kept +0.08R; current setup +0.18R, kept +0.19R, but the kept trades made less in total than all of them.
Net R did not rise from the lowest to the highest score quintile in either market, and predicting net R
instead of win / loss gave the same picture.

## Signal tracking
Each signal is followed on bars of its own timeframe: **Pending** until the entry is reached
(**Cancelled** if it isn't in time; filled at the open if price gaps through it) **→ Active → T1 hit** (stop moves to entry) **→ T2 hit** or **Breakeven**;
**SL hit** closes it; daily signals still open after 20 sessions are **Expired**; intraday signals are closed at the end of their trading day. Status changes (including fills and cancellations)
are posted to Telegram for signals that were sent there.

### Real-time prices (India, optional)
Set `KOTAK_CONSUMER_KEY` in `.env` (Kotak Neo app > More > Trade API) and Indian signals are tracked on
**live Kotak Neo quotes** instead of delayed Yahoo bars, so fills, stops and targets register as they happen and the
dashboard shows live prices ("Live · Kotak Neo" in the status bar).
- Only the consumer key is used: no login, MPIN or TOTP, so the app has **no trading access**. Never put your
  password, MPIN or TOTP secret in `.env`.
- Read-only: `algotrade/kotak.py` only calls the scrip master and quotes; a test guards against order calls.
- The NSE symbol list is downloaded once a day (cached in `data/`). If Kotak fails, tracking falls back to Yahoo
  and retries Kotak after 5 minutes.
- A fill seen on a live price stays filled; a refresh never moves a signal back (e.g. Active to Pending).
- Scans still use Yahoo history (scans ignore the still-forming bar, so live candles would add little), and US
  stocks stay on Yahoo.

### Automatic orders (Kotak)
`algotrade/broker.py` logs in to Kotak Neo by itself each day (TOTP generated from the stored secret, then MPIN)
so orders can be placed without you. Add to `.env`: `KOTAK_MOBILE` (with +91), `KOTAK_UCC` (Neo app > Profile),
`KOTAK_MPIN` and `KOTAK_TOTP_SECRET`, the base32 key from TOTP registration on the Kotak Neo Trade API page (the
text version of the QR code; if TOTP is already set up and the key wasn't saved, register it again to see it).
Then run `run.bat --kotak-check`: it logs in and shows your funds, holdings and today's orders, and saves the raw
responses to `data/kotak_account_check.json`. It places no orders.

**If orders fail with "unauthorized"** while the login and account reads work, Kotak is refusing order access
for the API: usually the PC's internet IP isn't registered for Trade API orders (shown by `run.bat --kotak-check`;
register it in Kotak's Trade API settings), or, for options, the F&O segment isn't active. Home internet IPs
often change; if Kotak requires a fixed IP, run the app on a machine with a static IP. Failed order calls are
retried only after such an authentication refusal (which happens before anything executes), never after a
timeout or server error, so an order is never placed twice.

**Anyone who can read `.env` can trade your account.** Keep it private, never share or commit it, and switch on
TOTP alerts in the Kotak app. `kotak.py` (live quotes) stays read-only; trading lives in `broker.py` and `orders.py`.

**Auto-trade switch.** The dashboard's top bar has an **Auto-trade** switch, off by default. Switching it on asks
for confirmation, first checks that the Kotak login works (it stays off if not), and turns red while live. The
switch is stored in `signals.db`, so the scanner (`run.bat`) sees a change on its next order, without a restart.
Every change is posted to Telegram. It can only be switched **on** from the PC running the dashboard; switching
**off** works from any device. There is no limit on trades per day; `MAX_ORDER_VALUE`
(₹10,000 per order) and `MAX_OPEN_RISK_PCT` (risk at the stops across all trades) still apply.

**No market orders.** SEBI doesn't allow market orders through the API for retail clients, so the app never
sends one. Wherever you choose *Market* (or the app sells at once), it sends a **limit just past the live
price**: `MARKET_PROTECTION_PCT` (1%) above for buys / below for sells on stocks, `OPTION_PROTECTION_PCT` (5%)
past the best ask / bid on options. It fills straight away like a market order, but never at a runaway price.
If there is no live price the order is refused rather than sent blind. The order still shows as *market* in
the app's records and Telegram, with its limit.

**What gets traded** (`algotrade/orders.py`): new **India BUY** signals on the **daily** timeframe, as delivery
(CNC), found by the scanner (`run.bat` or `--once`), and with **Auto-trade intraday signals** on (Settings, or
`INTRADAY_AUTO_TRADE`) also on **5m–1h**, as intraday (MIS). US and SELL signals never place orders. One app trade
per stock at a time. Each live trade has its **own watcher thread** in the scanner that follows it from entry to
exit: it checks Kotak every 5 seconds in market hours (every minute outside them) and ends when the trade is
done. New trades get their watcher at once (dashboard scans: within 15 seconds).

- **Intraday trades** work like daily ones (entry, stop, target) with these differences: entries only in
  market hours until `INTRADAY_LAST_ENTRY` (14:30), and at
  `INTRADAY_SQUARE_OFF` (15:00) unfilled entries are cancelled and positions sold, before Kotak's own square-off.
  One still open in the app after the close (the scanner was off at 15:00) is marked closed, with a notice.

- **Entry:** the signal's pending order, a limit at the entry price (pullback setups) or a stop-limit triggered
  at it (breakouts; limit 0.5% above). Quantity = the signal's size, cut so the order stays under
  `MAX_ORDER_VALUE`; if one share costs more, the signal is skipped. Outside market hours orders go in as AMO.
  Kotak orders last one day, so an unfilled entry is placed again each session until the tracker cancels the
  signal (after `ENTRY_VALID_BARS` sessions). An entry is never placed while the order book shows an open buy
  order for the same stock, so a position can't be doubled.
- **Stop-loss:** as soon as an entry fills, a stop-limit sell triggered at the signal's stop (limit 1% below),
  placed again every session. If the price gaps below the stop's limit
  (so it can't fill), the position is sold at the live price instead. If Kotak rejects the stop, it is
  retried and you are told; after 3 rejections in a row the app stops retrying and sends a 🚨 alert to place
  a stop yourself or close the trade. A stop cancelled during market hours (e.g. in the Kotak app) is placed
  again, with a notice.
- **Target:** one target, the signal's target 1 (1.5R), placed as a sell limit right after the stop. Kotak has
  no one-cancels-other for the pair, so the watcher does it: when the stop or the target fills, the other is
  cancelled; when one partly fills, the other is cut to the shares still held. If Kotak refuses the target twice
  (e.g. a delivery holding already blocked by the stop) the watcher sells at the target on the live price
  instead, with a notice. Should both ever fill, a 🚨 alert says how many shares are short.
- **Exit:** at the time exit (from the signal tracker) the position is sold (limit just below the live price):
  the target is cancelled first, then the stop once the sell is in. A sell that hasn't filled after 2 minutes is
  re-priced. If the stop or target fills, the trade is closed.
- **Auto-trade OFF** stops new entries and cancels unfilled ones; open positions keep their stop and exits, so
  nothing is left unprotected.
- **Telegram** gets every order event: placed, filled, rejected, stop and target placed, exits, closes with P&L,
  and an alert if a position has no stop (retried every loop).
- **Portfolio** (dashboard view): funds, holdings, today's positions and orders, and the app's trades.
  Prices are **live from Kotak quotes, refreshed every 5 seconds while NSE is open** (every 60 s when closed),
  and P&L, day change and totals are recalculated from them, including open P&L on the app's trades. Funds,
  holdings and orders themselves refresh every 60 s (Reload fetches them at once). Prices are snapshots a few
  seconds old, not a tick-by-tick stream.
- **Buy stocks:** **+ Buy stock** in the Portfolio header (search any NSE stock) and **Buy** on each holding open a
  ticket with the live price: quantity or a ₹ amount (converted to shares), limit or market, CNC (delivery) or
  MIS (intraday). One order's value is capped at `STOCK_MAX_ORDER_VALUE` (₹1,00,000) against fat-finger
  mistakes; market orders are checked at their protective limit. Confirmation, this PC only, AMO when the market is
  closed, Telegram notice. These are your own orders, separate from Auto-trade.
- **From the Signals table:** India signals have **Buy** and **Sell** buttons. Buy opens the same ticket prefilled
  from the signal: the entry as the limit price and the signal's risk-based quantity (`RISK_PER_TRADE_PCT` of
  `CAPITAL` over the entry-to-stop distance), with its stop and targets shown. It is your own order, so no
  stop-loss is placed for it. Sell opens the holdings sell ticket if you hold the stock.
- **Sell holdings:** Portfolio > Holdings has a **Sell** button on every holding: choose the quantity (default
  everything sellable now), limit or market. It places a delivery (CNC) sell on NSE (AMO when the market is
  closed). The quantity is checked against a fresh read of your Kotak holdings (sellable quantity, which
  leaves out unsettled or pledged shares). Stocks managed by Auto-trade are refused here: close them under App
  trades so their stop-loss is cancelled too. Confirmation, this PC only, Telegram notice. Kotak may ask for
  the CDSL TPIN / DDPI authorisation to sell from demat; if it refuses, the reason is shown.
- **Close by hand:** Portfolio > App trades has a **Close** button on open positions (sells everything at
  the live price, as AMO for the next open if the market is closed, then cancels the stop) and **Cancel** on unfilled
  entries (shares from a partial fill are sold). It works with Auto-trade on or off, asks for confirmation,
  posts to Telegram, and only works from the PC running the dashboard.
- Kotak's order book (`order_report`) decides what filled; the latest one is saved to `data/kotak_orders_last.json`.
- **Order history:** Kotak's book only holds today's orders, so the dashboard saves it (every 5 minutes in market
  hours, once after the close, and whenever the Portfolio is open) and keeps 3 years (the tax report needs it).
  See Portfolio > Order history.

Limits of this version: stops and exits are managed by the app (Kotak's API has no GTT / OCO orders), so **the
scanner must be running** during market hours; with the PC off an open position keeps only the stop placed
for that day. The order book holds only today's orders, so an order missing from it is re-placed only during
market hours. Prices are rounded to ₹0.05.

### Options Auto-trade
With **Auto-trade options** on (Settings, or `OPTIONS_AUTO_TRADE=true`) and the Auto-trade switch ON, India signals
on **F&O stocks** and on the **indices** in `OPTIONS_INDICES` (`NIFTY,BANKNIFTY`; also `FINNIFTY`, `MIDCPNIFTY`) are
traded as options (`algotrade/options_auto.py`). Options are only ever **bought**, so the most a trade can lose is
its premium.
- **BUY signal → call, SELL signal → put.** With options on, the scanner also produces India SELL signals (as with
  `ALLOW_SHORTS`) and scans the indices themselves (Yahoo ^NSEI / ^NSEBANK; they have no volume, so the turnover
  check and the Relative strength filter are skipped for them). The stock trader still takes only BUYs on shares;
  index signals are traded through options only.
- **Entry** when the underlying reaches the signal's entry (the same pending order as for shares: at or below the
  limit for pullbacks, past the stop for breakouts; SELLs mirror this), or once the tracker fills the signal. Only
  then is the contract picked: the nearest expiry at least `OPTIONS_MIN_DAYS` (5) away and the
  `OPTIONS_OTM_STEPS`-th (2nd) strike **out of the money**, skipping the one nearest the price. It is bought with a
  limit `OPTION_PROTECTION_PCT` above the best ask. A contract without a bid and ask, or with a spread wider than
  `OPTIONS_MAX_SPREAD_PCT` (5%), is skipped. Market hours only.
- **Size:** as many lots as fit in `OPTIONS_MAX_PREMIUM` (₹10,000), at most `OPTIONS_MAX_LOTS`; the premium in all
  open option trades stays under `OPTIONS_MAX_OPEN_PREMIUM` (₹30,000). A lot dearer than the cap is skipped.
- **Exit** on the **underlying**, not the option's price: sold (limit `OPTION_PROTECTION_PCT` below the best bid)
  when the stock or index reaches the signal's stop or target 1, when the tracker closes the signal (time exit),
  and from `INTRADAY_SQUARE_OFF` (15:00) on the option's expiry day. Intraday signals (with *Auto-trade intraday
  signals* on) are MIS, entered until `INTRADAY_LAST_ENTRY` and sold at `INTRADAY_SQUARE_OFF`; daily ones NRML.
  An unfilled buy is cancelled after 60 s and bought again at a fresh price if the entry still holds; an unfilled
  sell is re-priced after 60 s.
- **No stop order rests at Kotak** (exits follow the underlying, which an option order can't), so exits only happen
  while the scanner (`run.bat`) is running. One option trade per underlying at a time.
- Trades are listed under **Portfolio > Option trades** with Close / Cancel, and every step is posted to Telegram.

### Options (manual)
The dashboard's **Strategy Builder** tab shows live option chains for **index options** (NIFTY, BANKNIFTY, FINNIFTY,
MIDCPNIFTY, NIFTYNXT50) and **stock options** (every NSE stock with options): pick the underlying and expiry, and
see calls and puts around the money with LTP, best bid / ask, change, volume and open interest (in-the-money
strikes shaded, ATM highlighted, OI bars, put-call ratio). Prices refresh every 3 s while NSE is open.

Click a price to open an order ticket and place **your own** order: lots, limit or market, NRML (carry forward)
or MIS (intraday). This is separate from Auto-trade, which trades equity only. Safeguards:
- whole lots only, at most `OPTIONS_MAX_LOTS` (10) per order, and a confirmation showing the premium;
- **Sell is only for closing options you hold** (checked against your Kotak positions), so you can't write
  (short) an option by mistake;
- only from the PC running the dashboard; outside market hours orders go in as AMO;
- every order is posted to Telegram and listed under the chain; status and fills are in Portfolio > Orders today.

**Strategy builder** (same view): pick a template (long call / put, bull call, bear put, bull put and bear
call spreads, long straddle / strangle, iron condor, iron butterfly, long call butterfly, and short straddle /
strangle for analysis only), set the strike width and lots, or build your own legs; with the chain switched to
"Click = add to strategy", clicking prices adds legs. On live quotes it shows the payoff at expiry and today
(Black-Scholes at each leg's implied volatility, from its bid/ask mid), net credit / debit, max profit and max
loss (or "Unlimited"), breakevens, chance of profit, Greeks (delta, theta per day, vega per 1% IV, gamma),
P&L now and an estimate of the funds needed. Leg prices default to the best ask (buys) and bid (sells) and
can be edited; everything refreshes with the chain.

**Place strategy** sends every leg (limit at the leg prices, or market; NRML or MIS), with these rules:
- only **defined-risk** strategies: per option type, at least as many bought as sold (spreads, condors,
  butterflies). Naked shorts and ratio spreads can be analysed but not placed;
- all legs on one underlying and expiry;
- bought legs go first; sold legs are placed only after **every bought leg has filled** (checked in the order
  book for up to 20 s), so you are never left short without cover. If they don't fill in time, the sold legs
  are not placed and you are told. Strategies with sold legs therefore need market hours; buy-only strategies
  can go in as AMO.
"Funds needed" is the premium for debit strategies and about the max loss for credit spreads; Kotak's actual
margin can differ.

Option trading needs the F&O segment activated on the Kotak account; otherwise Kotak rejects the order and the
reason is shown. Contracts come from Kotak's F&O scrip master, downloaded once a day (`data/kotak_nse_fo_*.csv`).

### Watchlist, market overview and price alerts
**Market today** (first tab of Watchlist): NIFTY 50, Bank, Next 50, Midcap, Smallcap and India VIX; sector indices
ranked by today's move; NIFTY 100 breadth (up / down), the average move per industry, the top gainers and losers;
and the previous day's FII / DII cash flows from NSE (₹ crore). Live Kotak quotes, refreshed every 30 s in market
hours.

The **Stocks** tab follows NSE stocks of your choice with live Kotak prices (every 5 s while NSE is open):
price, day change, day range, and **Buy**. **🔔 Alert** sets a price alert ("rises above" / "falls below" a level,
with an optional note): the dashboard checks it every 15 s in market hours and posts to Telegram once when it is
crossed. Triggered alerts stay listed (with the price they hit) until you delete them.

### Signal details and trade journal
Click (or tap) a stock in the Signals list for its detail view: a candle chart of the last 120 bars with the
entry, stop-loss and both targets drawn in and the signal's bar marked, the figures, upcoming results /
ex-dividend dates (warned about when they are within `EVENT_WINDOW_DAYS`), and what each strategy that voted for
it looks for. Ctrl-click still opens TradingView; there is also a link in the view.

**Journal** (same view): tag the trade (Followed the plan, Entered late, Moved stop, FOMO, …), write a note and
attach a chart screenshot. Performance › **By tag** then shows win rate and R per tag, so costly habits stand out.

### Position size calculator
**🧮 Position size** (Watchlist header, **Size** on watchlist rows and in signal details): enter an entry and a
stop and it gives the shares that risk your chosen % of capital, the capital used, the ₹ at risk, targets at
1.5R and 3R, and whether the trade fits under your open-risk limit; **Buy** opens the order ticket prefilled.

### Portfolio risk
Portfolio › **Risk**: what each position loses if its stop hits, the total against `MAX_OPEN_RISK_PCT` (6% of
capital), holdings without a stop (their whole value counts as unprotected), and the largest sector. Auto-trade
won't open a trade that would take the total over the limit (the signal is skipped with the reason), and buy
tickets opened from a signal warn when it would.

### Strategy scorecard and backtests
Performance › **Strategy scorecard**: per strategy, live results after costs and the last dashboard backtest,
with a verdict: *Keep* (avg ≥ +0.1R), *Watch*, or *Drop* (below −0.05R), from live results once a strategy has
10 closed trades, otherwise from the backtest (30+ trades). The **Scanner** switch leaves a strategy out of the
continuous scanner (`run.bat`) from its next loop, without editing `.env`.

Performance › **Backtest** runs the market's current scan settings (strategies, minimum votes, filters) over 2,
5 or 10 years of daily bars for the whole universe, after costs, in the background (a minute or two): trades, win
rate, average and total R, profit factor, max drawdown and the equity curve; results per strategy feed the
scorecard.

### Daily summary and Telegram commands
After 15:45 IST on trading days one Telegram message sums up the day: account value and day P&L, trades closed
today (in R), new and open signals with the distance to their stop and target, Auto-trade positions, today's
orders (with rejection reasons) and anything the health check flagged. `DAILY_SUMMARY=false` turns it off.

The bot answers commands in the group (`TELEGRAM_COMMANDS=true`): `/positions`, `/pnl`, `/signals`, `/summary`,
`/status`, `/stop` (Auto-trade **off**; it can't be switched on from Telegram), `/watch TCS`, `/unwatch TCS`,
`/alert TCS above 2500`, `/alerts`, `/help`. Only messages from the configured group are answered; the dashboard
must be running.

### Phone notifications
Settings › **Phone notifications › Turn on** makes the phone app itself show notifications (like any app) for
price alerts, order fills and rejections, Auto-trade events, health problems and the daily summary, in addition
to Telegram. It needs the app opened at its `https://…ts.net` address (Tailscale); on iPhone the app must be
installed to the home screen first (iOS 16.4+). **Send a test** checks it.

### Settings screen
The ⚙ button in the top bar opens Settings: capital, risk per trade, max open risk, the order caps, the
market-order limits, the events window, and the daily summary / Telegram commands switches. Changes are saved in
the app and override `.env` until reset (each changed value has *reset to .env*); the scanner picks them up on
its next loop, and every change is posted to Telegram. Changing them needs the same access as placing an order
(this PC, or the phone app over Tailscale). Kotak / Telegram credentials and the PIN stay in `.env` only.

### Tax / P&L report
Performance › **Tax · realised P&L**: for a financial year (April to March), every sale matched against earlier
buys (oldest first) and split into short-term gains (held ≤ 12 months), long-term gains, intraday (speculative)
and F&O (business income), with **Download Excel**. It is built from the orders the dashboard saved (order
history, kept 3 years), so sales of shares bought before that are listed with an unknown cost; charges are not
included. Kotak's own tax P&L report is the official one.

### Live price stream (Kotak WebSocket)
While the dashboard runs it keeps one streaming connection to Kotak Neo's market feed (it uses the same login
as the Portfolio). Whatever the app asks prices for (watchlist, alerts, portfolio, market overview, option chains,
order tickets) is subscribed on the stream and then answered from memory, updated within a second, instead of a
quote call each time. Instruments nobody has looked at for 10 minutes are dropped again.

**Instant updates on every screen:** in market hours each open page keeps a push connection (`/api/stream`) and
the dashboard tells it the moment one of the prices on screen changes, so it updates right then (typically
0.05–0.2 s after the tick, at most about 3 times a second) instead of polling: open Signals (their LTP and P&L),
the index in the top bar, Watchlist, Market today, Portfolio (and its Risk tab), the option chain with its order
ticket and strategy builder, and the Buy ticket (only its price line while you're typing in it). Pages show
*Live stream · instant*. Without the push connection they fall back to refreshing every second (every 5 s
without the stream), and price alerts are checked every second while the stream is up. If the stream drops, it reconnects by itself (logging in again if needed) and
prices come from normal quote calls meanwhile; if it stays down for 3 minutes in market hours the health banner
says so. `LIVE_FEED=false` (or Settings › *Live price stream*) turns it off. It only carries prices: stop-losses
are real orders at Kotak and never depended on it. The scanner (`run.bat`) still uses quote calls.

### Health checks and backups
The dashboard checks every minute for problems that would otherwise go unnoticed and shows them in a banner at
the top of every screen (red = serious), posting to Telegram when one starts and when it clears:
- an Auto-trade position without a working stop-loss (after the first 20 minutes of the session);
- the Kotak login failing;
- Auto-trade ON while the scanner (`run.bat`) isn't running (no heartbeat for two loops), since the scanner
  places and watches Auto-trade's orders;
- live prices not updating during market hours, or the live price stream down for 3 minutes;
- no database backup for two days (only when backups are on).

**Backups** (off by default; `DB_BACKUP=true` or Settings › *Daily database backup* turns them on): every day after 16:00 IST a copy of `data/signals.db` (signals, trades, orders, order history,
watchlist, settings; no passwords) is saved to `data/backups/` (14 days kept) and sent to the Telegram group.
To restore, stop the app and copy a backup over `data/signals.db`. The database runs in WAL mode, so the
scanner and the dashboard can write at the same time without "database is locked" errors.

## Phone app (Android and iPhone)
The dashboard installs as an app on your phone: same screens, laid out for a phone (bottom tab bar, tables as
cards), with its own home-screen icon. It talks to the dashboard running on your PC through **Tailscale**, a
free private network between your own devices, so the dashboard is never open to the internet.

One-time setup:
1. Put a PIN in `.env` on the PC, at least 6 digits: `DASHBOARD_PIN=482913`. Restart the dashboard.
2. Install Tailscale (tailscale.com, free Personal plan) on the PC and on the phone, signed in with the same
   account.
3. In the Tailscale admin console (login.tailscale.com > DNS), turn on **MagicDNS** and **HTTPS certificates**.
4. On the PC, once: `tailscale serve --bg 8050`. It prints the app's address, like
   `https://your-pc.tail1234.ts.net`. This keeps working after reboots.
5. On the phone, open that address and log in with the PIN, then install:
   - **Android (Chrome):** menu ⋮ > *Add to Home screen* / *Install app*.
   - **iPhone (Safari):** Share > *Add to Home Screen*.

What the phone can do:
- Everything the PC dashboard shows (signals, performance, portfolio, orders, option chains), with live prices.
- Real orders (buy, sell, cancel / modify, close app trades, Auto-trade ON), but only when it comes in through
  Tailscale and is logged in. A device on the home Wi-Fi that isn't on Tailscale can at most look.
- Auto-trade OFF works from any device, even without logging in, so trading can always be stopped.

Safety: logins last 30 days per device (*Log out* is in the header). Five wrong PINs lock logins for 15 minutes.
Every login and lockout is posted to Telegram. Changing `DASHBOARD_PIN` logs every device out. The PC itself
never needs the PIN.

The PC has to be on with the dashboard running; otherwise the app shows "Can't reach AlgoTrade". Moving the
dashboard to a cloud server later removes that need.

## Tests
```
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
```
The suite (659 tests, about 14 s) pins how the code behaves **today**, including known quirks (listed in
`plan/quirks.md`). It is fully offline: it never calls Yahoo, NSE, Wikipedia or Telegram, never reads `.env`,
and never touches `data/`. Each test uses its own temporary database, and any attempt to open a database under
`data/` fails the run. Add `--cov=algotrade --cov=main` for a coverage report.

Golden files in `tests/golden/` snapshot the indicator, strategy and backtest output. Regenerate them only after
an **intended** behaviour change, and review the diff before keeping it:
```
set ALGOTRADE_UPDATE_GOLDEN=1
.venv\Scripts\python -m pytest tests\test_strategies.py tests\test_backtest.py
set ALGOTRADE_UPDATE_GOLDEN=
```
(In PowerShell, use `$env:ALGOTRADE_UPDATE_GOLDEN="1"` and then `Remove-Item Env:ALGOTRADE_UPDATE_GOLDEN`.)
If a golden test fails when you did not mean to change behaviour, that is a regression. Do not regenerate.

## Notes
- History comes from Yahoo Finance (free), delayed by up to about 15 minutes intraday. Indian prices for tracking
  can be real-time via Kotak Neo (see above).
- Yahoo sometimes publishes a finished India session late (a row with no close, dropped by the loader), so its
  newest daily bar is a day old. India daily scans then fill that session's bars from Kotak quotes (logged as
  "Yahoo was missing the latest session ... filled from Kotak quotes"), NIFTY included, so the end-of-day scan
  evaluates the right day. The India status bar shows NIFTY from Kotak's live quote (Yahoo only as a fallback),
  the same source as the option chain; the US index stays on Yahoo.
- Kotak's quote API takes at most 49 instruments per call (50 is rejected), so quotes go in batches of 49.
- NSE and US market holidays are not handled; on a holiday the scanner runs but finds no new bars.
- Scans (CLI, continuous loop and dashboard) ignore the still-forming last bar. During the session, a daily scan
  evaluates yesterday's completed bar, and an intraday scan evaluates the last bar whose interval has ended
  (exchange clock). Yahoo intraday data is delayed, so the newest "completed" intraday bar can still be missing
  its last minutes. Signal tracking still uses the live bar, so fills and stops during the session register.
- If the end-of-day scan fails, it is retried on the next loop. Its Telegram header is sent only once per market per day.
- If Yahoo returns a single-ticker (flat) frame for a multi-symbol download, that batch is skipped and an error is logged.
  Without this check, every symbol in the batch would get the same prices.
- Backtest (5y, daily, long-only, score ≥ 2, pending entries), before → after costs and slippage:
  NIFTY100: 2,659 trades, profit factor 1.14 → 0.83, **+0.08R → −0.11R per trade** (costs about 0.18R a trade);
  S&P 100: 2,688 trades, profit factor 1.20 → 1.18, +0.11R → +0.10R per trade (costs about 0.01R).
  In India the edge does not survive costs: STT (0.1% on each side of a delivery trade) and the fixed
  brokerage / DP charges each cost about 0.065R, because stops are only about 3% of the price.
  About 20% of setups never reach their entry. Shorts were unprofitable, so `ALLOW_SHORTS=false` by default.
- Strategy comparison (same test, each strategy on its own with min votes 1, avg R per trade after costs;
  "filters" = market trend + relative strength; run on 2026-10-15):

  | Setup | India | US |
  |---|---|---|
  | **RSI Pullback or RSI(2) Reversion, filters** (India scanner) | **+0.05** (1,171 trades, PF 1.09) | – |
  | RSI Pullback, filters | +0.05 (410) | +0.16 (400) |
  | RSI(2) Reversion, filters | +0.05 (931) | +0.17 (899) |
  | RSI(2) Reversion | −0.01 | +0.14 (1,762; best total R) |
  | RSI Pullback | −0.05 | +0.17 (1,119) |
  | Supply/demand zones | +0.02 (159) | +0.19 (228) |
  | EMA 9/21 Cross | −0.05 | +0.11 |
  | MACD Trend Cross | −0.09 | +0.06 |
  | Default vote (min 2) | −0.11 | +0.10 |
  | Support-Resistance | −0.17 | +0.08 |
  | Supertrend Flip | −0.18 | +0.10 |
  | Squeeze Breakout | −0.17 | +0.06 |
  | 20-bar Breakout | −0.15 | 0.00 |

  In India only the two RSI setups with both filters stay positive after costs, and only just: with ₹0 delivery
  brokerage they would gain about 0.05R more, with higher charges they turn negative. Requiring both RSI strategies
  to agree gave a single trade in five years, so the India scanner takes either one. In the US costs are about
  0.01R a trade and the RSI setups clearly beat the default vote. Before costs RSI(2) Reversion with filters showed
  about +0.19R in both markets.
- Educational tool, not investment advice.
