# Kalshi Stocks vs Crypto Perpetuals — frozen research directive

## Mission

Build an isolated Kalshi-only Stocks-vs-Crypto perpetuals paper research system within this repository. This project must remain separate from the existing legacy Bybit + CoinMarketCap strategy implementation.

## Strategy specification

- LONG exactly the eligible individual-stock perpetual with the highest trailing 24-hour percentage return.
- SHORT exactly the eligible crypto perpetual with the lowest trailing 24-hour percentage return across the complete eligible crypto universe.
- Long target notional = 1× NAV.
- Short target notional = 1× NAV.
- Gross exposure = 2× NAV.
- Exactly one long and one short position.
- At most one rebalance attempt per eligible stock trading session.
- No intraday reranking, averaging, pyramiding, discretionary substitutions, or additional signal filters.
- Explicitly exclude ETFs/funds, equity indices, metals, energy, and prediction/event contracts.

## Provider boundary

Use Kalshi Perps market data only. Do not use CoinMarketCap, Bybit, another exchange, or another market-data provider as a fallback. The existing Bybit + CMC implementation remains present as a historical predecessor and is not part of this new project's source dependencies.

## Classification gate

Use exact, authoritative Kalshi metadata. Do not infer instrument type from ticker symbols or titles. The system must distinguish individual common stocks from ETFs and equity indices before any stock ranking is permitted. If the production response cannot support that distinction, stop and report the missing metadata rather than relaxing exclusions.

## Price/ranking gate

Calculate trailing 24-hour return from timestamped Kalshi one-minute trade-price candles. Require valid, sufficiently fresh prices at the current observation and 24-hour reference time. Reject missing, null, stale, or invalid observations. Do not substitute volume for return. Do not label trade-price OHLC as mark-price history.

## Contract and execution gate

Use actual per-market contract size, underlying multiplier, quantity increment, minimum size, fractional-trading flag, book depth, fee schedule, funding rate, and margin/risk parameters. Simulated long entries consume asks and long exits consume bids; short entries consume bids and covers consume asks. Compute VWAP and fees without double-counting spread/slippage. Do not reuse the predecessor's Bybit fee assumptions.

## Session gate

Use each stock market's session schedule and Kalshi holiday closure data. No fixed UTC-midnight assumption. Define one deterministic session boundary, and transition the pair only when both legs can be transitioned safely. A failure on either leg leaves the prior pair unchanged. Record skipped sessions and actual holding duration.

## Accounting gate

Report relative price-ratio performance separately from paper portfolio P&L. Account P&L must include both legs' simulated fills, fees, funding, contract multipliers, and documented corporate-action adjustments. Track market-specific margin and liquidation risk. Keep trade, reference, settlement-mark, and liquidation-mark prices distinct.

## Safety and project separation

- Paper only; no live or demo order submission.
- Public GET market-data endpoints only in the initial preflight.
- Never access private account endpoints or request/store credentials.
- No order-submission code path should be added to the paper engine.
- Keep all Kalshi code, config, tests, workflow concurrency, reports and persistence separate from the legacy Bybit/CMC project.
- Do not modify the Pionex Grid Restarter.

## Stage-gated implementation

1. Read-only production API preflight on GitHub-hosted Ubuntu.
2. Validate exact Stocks/ETF/Index/Crypto classification.
3. Validate timestamped 24-hour ranking data.
4. Implement contract economics, fills, costs, funding, and margin modelling.
5. Implement session-aware atomic two-leg paper state transitions and durable persistence.
6. Run extended paper validation and report performance/risk.

Do not advance to the next stage merely because the previous workflow completed. Record explicit PASS/FAIL evidence for each gate and stop on unresolved blockers.

## Current authorized increment

Stage 1 only: add and run the read-only preflight. After reporting endpoint results, raw schema, classification evidence, data quality, rate-limit observations, and blockers, stop for review. Ranking and paper trading are not yet implemented or authorized by this stage.
