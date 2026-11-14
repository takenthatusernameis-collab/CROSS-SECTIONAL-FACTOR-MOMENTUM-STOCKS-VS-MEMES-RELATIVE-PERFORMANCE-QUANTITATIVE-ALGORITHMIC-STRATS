# Bitget Stocks vs Crypto — Five-Minute Monitoring Paper Strategy

This repository's active paper strategy is a rolling 24-hour cross-sectional pair on Bitget USDT-margined perpetuals. It is paper-only: the workflow uses public market-data GET endpoints, estimates fills from public order-book depth, and never accesses accounts or submits orders.

## Frozen selection and holding rules

- **Long:** the active Bitget USDT perpetual with the highest exchange-reported trailing-24-hour price change among instruments with `symbolType=stock`.
- **Short:** the active Bitget USDT perpetual with the lowest exchange-reported trailing-24-hour price change among instruments with `symbolType=crypto`.
- Eligible instruments must have `category=USDT-FUTURES`, `quoteCoin=USDT`, `status=online`, and `type=perpetual`.
- Category is taken only from Bitget's native `symbolType` field. The strategy does not infer stock or crypto from symbol/base-coin names.
- One long and one short are held. Each target leg is approximately 1× portfolio NAV, for 2× gross notional.
- Market ranking and open-position monitoring run every five minutes.
- The durable SQLite `cooldown_control.cooldown` variable is an integer constrained to `0` or `1`.
- At `0`, the first scheduled workflow refreshes Bitget's current feed and attempts to construct the next pair. It switches to `1` only in the same successful, atomic portfolio transaction.
- At activation, the workflow-start timestamp is stored. The timed reset is the close of the minute exactly 24 hours after the activation minute: `floor(activated_at_ms / 60,000) * 60,000 + 86,400,000 + 60,000`.
- The first scheduled run initiated after that reset timestamp changes the flag back to `0`. It does not wait for an exact cron/expiry match, and the reset does not slide when GitHub delays a run.
- The next scheduled run beginning with `0` builds from that run's refreshed data and arms the next `1` state after successful accounting. On data/depth/accounting failure it leaves the flag at `0` and retries.
- The first post-reset workflow-start timestamp is persisted as `last_reset_run_started_ms`. If the data/depth checks fail, subsequent retries preserve that original ROI/MDD end boundary rather than extending the measurement period; any delay before the next successful portfolio transition is reported separately.
- ROI and holding-period MDD boundaries are the actual workflow-start timestamps at the successful `0 -> 1` run and the first eligible run after reset. Report the observed duration; it may exceed 24 hours. UTC date, exchange response time and daily-chart assumptions are not cooldown or performance boundaries.
- Ranking reads Bitget's `price24hPcnt` directly. It is a decimal fraction; reports multiply it by 100 to display percentages. The return is not locally reconstructed from candles.

## Paper execution and ROI

At each successful cooldown-eligible transition the engine:

1. Applies all funding settlements observed while the previous legs were open. Funding rates come from Bitget's public funding history; settlement mark is estimated from the matching/preceding one-minute historical mark candle.
2. Closes the prior long into public bids and prior short into public asks, walking visible depth to calculate VWAP.
3. Calculates linear USDT P&L, exit fees and net per-trade ROI. Each closed leg remains in the `trades` table and `closed_trades.csv`.
4. Opens the new long at ask-side VWAP and short at bid-side VWAP, each sized near 1× pre-entry NAV. Quantity precision, step, minimum quantity, minimum notional and maximum market-order quantity are checked against Bitget instrument metadata.
5. Marks the new positions using Bitget's mark price and records NAV, cash, unrealized P&L, cumulative realized P&L, fees, funding, gross exposure, daily ROI and cumulative portfolio ROI.

The `trades` table records each leg's entry/exit, size, gross P&L, entry/exit fees, funding and net ROI as a percentage of entry notional. Portfolio ROI is `(NAV / initial NAV - 1) × 100`. Both are reported separately so a per-leg return is not confused with account-level performance.

A selected pair is not replaced by a runner-up if either selected contract has insufficient book depth or invalid contract metadata. The whole rebalance is rolled back and the previously persisted state remains unchanged. No partial fill is invented.

## Minute spread and drawdown reconstruction

During every five-minute holding-period run, the workflow incrementally reconstructs newly completed one-minute observations from Bitget's public contract candles. On the first eligible scheduled run after the binary cooldown reset, it finalizes the prior interval at that workflow's recorded start timestamp before rebuilding the portfolio. The prior interval can exceed 24 hours if GitHub starts the eligible workflow late. It uses no TradingView API.

- **Synthetic spread:** long market-price ratio divided by short market-price ratio. For each synchronized minute, the conservative OHLC envelope uses `long high / short low` for the spread high and `long low / short high` for the spread low. Interval spread drawdown is `(spread low / spread high - 1) × 100`.
- **Strategy equity and MDD:** mark-price candles are used with actual paper quantities, entry prices, starting cash and funding cashflows. For each minute, the NAV high uses the long mark high and short mark low; NAV low uses the long mark low and short mark high. The strategy-level peak-to-trough drawdown is calculated over the chained NAV observations and retained across daily pair changes.
- **Intraminute limitation:** a one-minute OHLC bar does not reveal the exact order in which its high and low occurred, nor whether the legs' extreme prices printed at the same instant. The envelope is intentionally conservative and is an estimate, not tick-perfect execution or a liquidation model.
- **Coverage policy:** both symbols must have matching market and mark candles for every complete minute in the interval before that interval can update historical intraday strategy MDD. Partial observations are reported with a coverage percentage but do not update the historical intraday MDD. Missing candle data never changes the frozen ranking and does not, by itself, block a valid paper rebalance.
- The initial paper run has no prior holding interval; it opens the pair, stores the workflow-start boundary, and atomically sets `cooldown=1`. Five-minute monitoring runs measure the held pair. At the first scheduled run after reset, the prior interval ends at that run's start timestamp; the workflow then builds the next pair and re-arms `cooldown=1`.

The SQLite database includes `strategy_risk`, `risk_intervals`, `minute_metrics`, `monitor_state`, `monitor_minute_metrics` and `monitor_observations`. Reports include `daily_risk.csv`, `latest_minute_risk.csv` and `intraday_monitor.csv`; JSON/Markdown summaries show live mark-to-market ROI, both leg directions, spread drawdown, holding-cycle drawdown, historical strategy MDD, cooldown countdown and data coverage.

**During the lock, monitoring is analytics-only.** It does not change account cash, positions, trades, the funding/transaction ledger or daily portfolio snapshots. Funding seen between rebalances is displayed as a pending estimate and is posted to the accounting ledger exactly once only when a permitted rebalance occurs. Incomplete minute history is retried from the last contiguous checkpoint and never updates strategy MDD.

## Persistence and workflow

- GitHub Actions schedule: **every five minutes (`*/5 * * * *`)** on `ubuntu-latest`.
- Workflow concurrency serializes the SQLite restore/monitor/rebalance/persist cycle. GitHub scheduling may be delayed; the cooldown uses actual run-start time and does not require exact expiry-minute alignment.
- The SQLite database is stored on the dedicated `bitget-paper-portfolio-state` branch and backed up with each workflow artifact. Per-run artifacts expire after one day; the state branch is the long-lived ledger and monitoring store.
- Human-readable live view: [STATUS.md](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/blob/bitget-paper-portfolio-state/STATUS.md) is regenerated from the same database after every successful scheduled or manual paper run. It summarizes open positions, NAV/P&L/ROI, drawdown, cooldown, latest update and data quality.
- The database includes `account`, `positions`, `trades`, `ledger` and `daily_snapshots` tables.
- Reports include JSON, Markdown, `closed_trades.csv`, `daily_nav.csv`, `daily_risk.csv` and `latest_minute_risk.csv`.
- First successful paper run initializes the account at **10,000 USDT** by default. `PAPER_INITIAL_NAV_USDT` can be set before the first run; initial NAV is immutable after the database exists.
- The engine uses Bitget's public instrument `takerFeeRate` as a fee estimate. `BITGET_TAKER_FEE_RATE` can override it. This is not an account-specific fee quote; discounts/tier-specific rates may differ.

A push to main or a manual `mode=diagnostic` dispatch runs ranking diagnostics only. Scheduled paper runs capture the job-start timestamp before checkout/setup and use that timestamp for cooldown transitions and performance-window boundaries. Exchange time is used for market-data freshness validation, not to redefine the binary cooldown or ROI/MDD interval.

## Current model limits

- Order-book VWAP estimates are not actual exchange fills and only reflect visible depth at the snapshot time.
- Funding cashflow uses public historical funding rates and a one-minute historical mark candle; it is a minute-resolution estimate, not an account funding record.
- The model does not simulate maintenance margin, liquidation, ADL, insurance fund, position tiers, dividend/corporate-action adjustments, or account/jurisdiction product eligibility.
- Net ROI includes estimated fees and funding. Slippage is embedded in VWAP and is not deducted a second time.
- No private account endpoint, API key, balance, live/testnet order endpoint or order-submission code is used.

## Run locally

```bash
python -m unittest discover -s tests -p 'test_bitget_*.py' -v
python -m compileall -q bitget_stocks_vs_crypto scripts tests
python scripts/run_bitget_paper.py --diagnostic-only
```

The scheduled paper runner is:

```bash
python scripts/run_bitget_paper.py --db-path state/bitget_paper.sqlite3
```