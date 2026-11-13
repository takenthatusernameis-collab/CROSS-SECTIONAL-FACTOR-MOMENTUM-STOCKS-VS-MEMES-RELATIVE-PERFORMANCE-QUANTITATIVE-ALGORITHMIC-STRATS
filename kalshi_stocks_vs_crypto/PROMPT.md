# Kalshi Equities/Indices vs Crypto Perpetuals — frozen research directive

## Mission and project isolation

Build an isolated Kalshi-only paper research system in this directory. Preserve the existing Bybit + CoinMarketCap implementation as a historical predecessor. Do not modify the Pionex Grid Restarter.

## User-approved strategy universe

The user explicitly accepts the broader rule Equities/Indices versus Crypto:

- LONG universe: all active Kalshi markets with exact raw asset_class equal to Equities or Indices, combined into one long-ranking pool.
- SHORT universe: all active Kalshi markets with exact raw asset_class equal to Crypto.
- Metals, Energy, and all other raw asset classes are excluded.

The combined long pool intentionally permits index contracts and ETFs if Kalshi places them in the selected raw classes. Do not reintroduce an individual-common-stock-only gate. Do not assume an index can never outperform an individual stock; rank every included contract using the same return formula.

Use exact asset_class values only. Never infer asset class from ticker, title, symbol, or market naming patterns.

## Immutable strategy parameters

- Long exactly the eligible long-universe market with the highest trailing 24-hour percentage return.
- Short exactly the eligible Crypto market with the lowest trailing 24-hour percentage return.
- Long target notional = 1× NAV.
- Short target notional = 1× NAV.
- Gross exposure = 2× NAV.
- Exactly one position per side.
- At most one pair-rebalance attempt per eligible Equities trading session.
- No intraday reranking, averaging, pyramiding, discretionary replacement, or additional signal filters.

## Provider boundary

Kalshi Perps is the only market-data provider. No Bybit, CoinMarketCap, external taxonomy service, secondary exchange, external price feed, or fallback provider.

## Ranking data integrity

Calculate:

    R_24h = (P_now / P_24h_ago - 1) × 100

Use Kalshi one-minute trade-price candlesticks. Use completed candles only; ignore null or invalid closes. Require a current observation no older than 15 minutes and a historical reference observation no more than 15 minutes before the 24-hour cutoff. Report exact timestamps, the actual elapsed window, and freshness gaps.

A market with missing or stale observations is unavailable for the diagnostic ranking. Report every unavailable market. Show provisional leaders only among valid observations and mark the ranking incomplete unless every market in both accepted universes has a valid return. Never replace missing values with zero or infer prices from another market.

Do not confuse volume with price return. Do not label trade-price OHLC as mark-price OHLC.

## Contract and execution gate

Use actual per-market contract size, underlying multiplier, quantity increment, minimum size, fractional-trading flag, book depth, fee schedule, funding rate, and margin/risk parameters. Simulated long entries consume asks and long exits consume bids; short entries consume bids and covers consume asks. Compute VWAP and fees without double-counting spread/slippage. Do not reuse the predecessor's Bybit fee assumptions.

## Session and accounting gates

Use actual stock/Equities market schedules and Kalshi holiday closure data. No fixed UTC-midnight assumption. Define one deterministic daily boundary. Transition the pair only when both legs can be transitioned safely; otherwise leave the previous pair unchanged. Record skipped sessions and actual holding duration.

Report relative price-ratio performance separately from paper portfolio P&L. Account P&L must include both legs' simulated fills, fees, funding, contract multipliers, and documented corporate-action adjustments. Track market-specific margin and liquidation risk. Keep trade, reference, settlement-mark, and liquidation-mark prices distinct.

## Safety and project separation

- Paper only; no live or demo order submission.
- No private account endpoints or API credentials.
- Public GET market-data endpoints only in the current research stages.
- No order-submission code path should be added to the paper engine.
- Keep Kalshi code, config, tests, workflow concurrency, reports, and persistence separate from the legacy Bybit/CMC project.
- Do not modify the Pionex Grid Restarter.

## Stage-gated implementation

1. Read-only production API preflight (completed and passed on GitHub-hosted Ubuntu).
2. Full-universe, read-only trailing-24-hour return and data-quality diagnostic (current stage).
3. Review data coverage, timestamp freshness, and provisional leaders.
4. Implement contract economics, fills, costs, funding, and margin modelling.
5. Implement session-aware atomic two-leg paper transitions and durable persistence.
6. Run extended paper validation and report performance/risk.

Do not advance merely because the previous workflow completed. Record explicit PASS/FAIL evidence and stop on unresolved blockers.

## Current stage boundary

The current increment may retrieve all accepted markets' public one-minute candles and calculate provisional 24-hour returns. It may not create simulated positions, execute simulated fills, update NAV, or persist portfolio state. Stop after publishing the diagnostic report for review.
