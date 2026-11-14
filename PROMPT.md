# Strategy Directive — Bitget Stocks vs Crypto Daily Paper Portfolio

## Mission

Maintain a public-data-only, credential-free, paper-only cross-sectional pair on Bitget USDT-margined perpetuals. The strategy is deliberately simple and frozen. Do not add discretionary filters, change ranking fields, substitute runners-up, or submit exchange orders.

## Universe

Refresh `GET /api/v3/market/instruments?category=USDT-FUTURES` each daily run. Eligible instruments must satisfy all of:
- `category == USDT-FUTURES`
- `quoteCoin == USDT`
- `status == online`
- `type == perpetual`

Classify only using Bitget's explicit `symbolType`:
- LONG universe: `symbolType == stock`
- SHORT universe: `symbolType == crypto`

Do not infer class from ticker, `baseCoin`, contract name, project name, token description or external taxonomy. Exclude metals, commodities, deliveries, inactive/restricted contracts, non-USDT contracts and all unknown categories.

## Frozen ranking and sizing

- Fetch the complete `GET /api/v3/market/tickers?category=USDT-FUTURES` response.
- Rank using the exact `price24hPcnt` value from Bitget, with no local return reconstruction.
- `price24hPcnt` is a decimal fraction; multiply by 100 for percentage display only.
- Require one unique, valid, fresh ticker for every eligible contract. Require positive last/bid/ask/mark prices and a ticker timestamp within 15 minutes of Bitget server time. Fail closed if coverage is incomplete.
- Long exactly the eligible stock with the highest 24-hour percentage return.
- Short exactly the eligible crypto contract with the lowest 24-hour percentage return.
- One position per leg. Long notional target = 1× NAV; short notional target = 1× NAV; total gross exposure target = 2× NAV.
- One portfolio transition per UTC date at 00:05 UTC. Repeated same-day runs must be idempotent.
- No runner-up, liquidity-ranked substitute, symbol-based exclusion, scaling, averaging, pyramiding or intraday reranking. If the selected pair cannot be safely paper-filled, skip the entire transition and preserve prior positions/state.

## Paper accounting

- Use unauthenticated GET only: instruments, server time, tickers, merge-depth, historical funding rates and historical mark candles.
- Simulate opening a long at ask depth VWAP and closing it at bid depth VWAP. Simulate opening a short at bid depth VWAP and covering it at ask depth VWAP.
- Use maximum public depth available from the order-book endpoint. Reject a transition if any full close/open quantity lacks sufficient visible depth.
- Size quantities using exchange `quantityMultiplier` / `quantityPrecision`, `minOrderQty`, `maxMarketOrderQty` and `minOrderAmount`. Do not create partial synthetic fills.
- Use Bitget instrument `takerFeeRate` by default; an explicit `BITGET_TAKER_FEE_RATE` override is allowed. State clearly that these rates are estimates, not a user-account fee quote.
- Apply funding at each observed settlement. The rate comes from Bitget public funding history. Estimate the settlement mark using the matching one-minute historical mark candle, or the preceding completed minute's close if the event is not aligned to a minute. Disclose minute-resolution uncertainty.
- For linear USDT perpetuals, funding cashflow is negative for a long when the funding rate is positive and positive for a short, with the sign reversed for negative rates.
- Track each leg's entry/exit price, quantity, notional, gross P&L, entry/exit fees, funding, net P&L and net trade ROI (`net P&L / entry notional`).
- Track portfolio cash, realized and unrealized P&L, fees, funding, gross exposure, NAV, daily return and cumulative portfolio ROI (`NAV / initial NAV - 1`).
- Treat funding, closes, opens, ledger rows, trade updates and daily snapshot as one atomic SQLite transition. Any market-data/accounting failure rolls back the full transition.

## Persistence and operation

- SQLite path: `state/bitget_paper.sqlite3`.
- Durable state branch: `bitget-paper-portfolio-state`.
- Keep append-only transaction events and closed trade history. Persist daily NAV snapshots. Upload per-run JSON/Markdown plus closed-trade and NAV CSVs.
- Initial NAV defaults to 10,000 USDT and is immutable after database initialization.
- Manual diagnostic runs must never change paper positions. Manual paper dispatch is opt-in and guarded by the same per-UTC-date idempotency rule.
- Scheduled workflow is a single UTC daily event; do not add a loop or intraday monitoring.

## Safety boundary and limitations

No API credentials, private account endpoints, account balances, live/demo order endpoints or order submission. Public market-data access does not prove that the user's account is eligible to trade a product. Paper VWAP is not an actual fill record. Liquidation, maintenance margin, ADL, insurance fund, product eligibility and corporate-action effects are not modeled. Never label paper ROI as live account ROI.