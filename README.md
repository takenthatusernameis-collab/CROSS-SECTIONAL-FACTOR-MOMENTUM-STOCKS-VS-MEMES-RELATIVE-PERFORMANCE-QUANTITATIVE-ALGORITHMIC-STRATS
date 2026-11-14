# Bitget Stocks vs Crypto — Daily Perpetual Paper Strategy

This repository's active paper strategy is a daily cross-sectional pair on Bitget USDT-margined perpetuals. It is paper-only: the workflow uses public market-data GET endpoints, estimates fills from public order-book depth, and never accesses accounts or submits orders.

## Frozen daily rules

- **Long:** the active Bitget USDT perpetual with the highest exchange-reported trailing-24-hour price change among instruments with `symbolType=stock`.
- **Short:** the active Bitget USDT perpetual with the lowest exchange-reported trailing-24-hour price change among instruments with `symbolType=crypto`.
- Eligible instruments must have `category=USDT-FUTURES`, `quoteCoin=USDT`, `status=online`, and `type=perpetual`.
- Category is taken only from Bitget's native `symbolType` field. The strategy does not infer stock or crypto from symbol/base-coin names.
- One long and one short are held. Each target leg is approximately 1× portfolio NAV, for 2× gross notional.
- One rebalance per UTC date at 00:05 UTC. A same-date rerun is idempotent and creates no extra trades.
- Ranking reads Bitget's `price24hPcnt` directly. It is a decimal fraction; reports multiply it by 100 to display percentages. The return is not locally reconstructed from candles.

## Paper execution and ROI

At each successful daily transition the engine:

1. Applies all funding settlements observed while the previous legs were open. Funding rates come from Bitget's public funding history; settlement mark is estimated from the matching/preceding one-minute historical mark candle.
2. Closes the prior long into public bids and prior short into public asks, walking visible depth to calculate VWAP.
3. Calculates linear USDT P&L, exit fees and net per-trade ROI. Each closed leg remains in the `trades` table and `closed_trades.csv`.
4. Opens the new long at ask-side VWAP and short at bid-side VWAP, each sized near 1× pre-entry NAV. Quantity precision, step, minimum quantity, minimum notional and maximum market-order quantity are checked against Bitget instrument metadata.
5. Marks the new positions using Bitget's mark price and records NAV, cash, unrealized P&L, cumulative realized P&L, fees, funding, gross exposure, daily ROI and cumulative portfolio ROI.

The `trades` table records each leg's entry/exit, size, gross P&L, entry/exit fees, funding and net ROI as a percentage of entry notional. Portfolio ROI is `(NAV / initial NAV - 1) × 100`. Both are reported separately so a per-leg return is not confused with account-level performance.

A selected pair is not replaced by a runner-up if either selected contract has insufficient book depth or invalid contract metadata. The whole rebalance is rolled back and the previously persisted state remains unchanged. No partial fill is invented.

## Persistence and workflow

- GitHub Actions schedule: **00:05 UTC daily** on `ubuntu-latest`.
- The SQLite database is stored on the dedicated `bitget-paper-portfolio-state` branch and backed up with each workflow artifact.
- The database includes `account`, `positions`, `trades`, `ledger` and `daily_snapshots` tables.
- Reports include JSON, Markdown, `closed_trades.csv` and `daily_nav.csv`.
- First successful paper run initializes the account at **10,000 USDT** by default. `PAPER_INITIAL_NAV_USDT` can be set before the first run; initial NAV is immutable after the database exists.
- The engine uses Bitget's public instrument `takerFeeRate` as a fee estimate. `BITGET_TAKER_FEE_RATE` can override it. This is not an account-specific fee quote; discounts/tier-specific rates may differ.

A push to main or a manual `mode=diagnostic` dispatch runs ranking diagnostics only. A scheduled run performs paper accounting. A manual `mode=paper` dispatch explicitly requests one paper transition and is subject to the once-per-UTC-date idempotency guard.

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