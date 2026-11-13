# Binance Stock/Index Perpetuals vs Crypto Perpetuals — frozen research directive

## Mission and isolation
Replace the former Kalshi adapter with Binance USDⓈ-M Futures TradFi perpetual discovery and contract-price candles. Keep this package isolated from the legacy stocks_vs_memes/ Bybit + CoinMarketCap implementation. Do not modify the Pionex Grid Restarter.

## Dynamic eligible universe
Refresh GET /fapi/v1/exchangeInfo on every diagnostic/rebalance cycle. Do not hardcode symbols.
- LONG: status=TRADING, contractType=TRADIFI_PERPETUAL, quoteAsset=USDT, marginAsset=USDT, and exact underlyingType in EQUITY, CN_EQUITY, KR_EQUITY, HK_EQUITY, PREMARKET, INDEX.
- SHORT: status=TRADING, contractType=PERPETUAL, quoteAsset=USDT, marginAsset=USDT, and exact underlyingType=COIN.
- Exclude non-trading, non-USDT-quoted/margined, commodity, FX and unknown underlying types. Never infer a class from ticker/title.
- Newly listed qualifying contracts enter automatically after the next exchangeInfo refresh; inactive or delisted contracts exit automatically.

ETFs and index contracts are eligible only when Binance metadata places them in an approved equity/index underlyingType. The implementation must report unknown classifications rather than guess.

## Frozen strategy parameters
- Long exactly the eligible stock/index perpetual with the highest trailing 24-hour percentage return.
- Short exactly the eligible crypto perpetual with the lowest trailing 24-hour percentage return.
- Long target notional = 1× NAV; short target notional = 1× NAV; gross exposure = 2× NAV.
- Exactly one position per side.
- At most one pair-rebalance attempt per UTC calendar day after the daily ranking snapshot.
- No intraday reranking, averaging, pyramiding, discretionary replacement, or additional signal filters.

## Data and 24/7 semantics
Binance TradFi perpetuals are tradable 24/7. Remove all Kalshi schedule.is_open gates and all underlying-equity-session-open requirements. Do not skip ranking when the underlying cash equity market is closed.
Rank each perpetual using Binance's own one-minute continuous-contract klines from GET /fapi/v1/continuousKlines:
- TRADIFI_PERPETUAL for the long stock/index universe.
- PERPETUAL for the short crypto universe.
- Use contract-traded close prices, not underlying index, mark, spot, or another venue's candle.
- Ignore zero-trade candles, null/invalid/non-positive closes, and the still-forming minute.
- Require current traded close no older than 15 minutes and historical reference close no more than 15 minutes before the 24h cutoff. Report timestamps, actual observation interval, trade counts and gaps.
- Missing/stale prices make that contract unavailable. Never substitute zero or another instrument's price. Ranking is incomplete unless every accepted contract passes.

Off-hours underlying index references and mark-price smoothing can differ from the perpetual's own traded price. Report this distinction; do not confuse trade price with mark or index price.

## Provider/API and safety boundaries
Binance USDⓈ-M Futures is the only market-data provider for both legs. Public unauthenticated GET endpoints only in current stages: exchangeInfo, time, continuousKlines, depth, premiumIndex and tradingSchedule. Trading schedules are informational only; they must not gate the 24/7 contract ranking.
No Kalshi, CoinMarketCap, Bybit, secondary exchange, external price/taxonomy provider, credentials, private account endpoints, account state, orders, live or demo execution. Do not add order-submission code to the paper engine.
Product availability is jurisdiction/account dependent. Public market-data access does not prove TradFi Perps are enabled for a specific user/account; verify restrictions before any later execution design.

## Later execution/accounting gates
Before paper fills are implemented, derive actual per-contract tick size, quantity step, min notional/quantity, contract multiplier, price bands, order-book depth, fees, funding interval/rate/caps, leverage brackets, margin rules, maintenance margin, and corporate-action treatment from Binance data/docs. Long entries consume asks and exits bids; short entries consume bids and covers asks. Compute VWAP and fees without double-counting spread/slippage. Distinguish contract traded price, index, mark and liquidation reference.
Use a deterministic UTC daily boundary; never synchronize transitions to the underlying stock session. Transition both legs atomically or preserve the previous pair. Track holding durations, skipped transitions, funding, fees, margin and liquidation risk. Report relative price-ratio performance separately from paper account P&L.

## Stage gates
1. Read-only public API preflight.
2. Full-universe read-only 24-hour return/data-quality diagnostic.
3. Review universe classification, coverage, freshness and provisional leaders.
4. Validate contract economics, execution costs, funding, margin and off-hours pricing.
5. Implement atomic two-leg daily paper transitions and durable state.
6. Run extended paper validation and report performance/risk.

Current implementation is limited to stages 1 and 2. It must not create simulated positions, fills, NAV, portfolio state or exchange orders. Do not advance without explicit PASS/FAIL evidence and review of blockers.