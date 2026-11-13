# Binance Stock/Index Perpetuals vs Crypto Perpetuals — frozen research directive

## Mission

Maintain a read-only research workflow that dynamically discovers Binance USD-M Futures stock/index and crypto perpetuals, consumes Binance's own 24-hour ticker report, and reports the strongest stock/index and weakest crypto contracts. Keep this package self-contained.

## Dynamic eligible universe

Refresh GET /fapi/v1/exchangeInfo on every diagnostic/rebalance cycle. Do not hardcode symbols.
- LONG: status=TRADING, contractType=TRADIFI_PERPETUAL, quoteAsset=USDT, marginAsset=USDT, and exact underlyingType in EQUITY, CN_EQUITY, KR_EQUITY, HK_EQUITY, PREMARKET, INDEX.
- SHORT: status=TRADING, contractType=PERPETUAL, quoteAsset=USDT, marginAsset=USDT, and exact underlyingType=COIN.
- Exclude non-trading, non-USDT-quoted/margined, commodity, FX and unknown underlying types. Never infer a class from ticker/title.
- Newly listed qualifying contracts enter automatically after the next exchangeInfo refresh; inactive or delisted contracts exit automatically.

ETFs and index contracts are eligible only when Binance metadata places them in an approved equity/index underlyingType. Report unknown classifications rather than guessing.

## Frozen strategy parameters

- Long exactly the eligible stock/index perpetual with the highest Binance-reported rolling 24-hour percentage change.
- Short exactly the eligible crypto perpetual with the lowest Binance-reported rolling 24-hour percentage change.
- Long target notional = 1× NAV; short target notional = 1× NAV; gross exposure = 2× NAV.
- Exactly one position per side.
- At most one pair-rebalance attempt per UTC calendar day after the daily ranking snapshot.
- No intraday reranking, averaging, pyramiding, discretionary replacement, or additional signal filters.

## Ranking data source

Use the single unfiltered GET /fapi/v1/ticker/24hr response as the sole 24-hour ranking source.
- Select Binance's priceChangePercent field directly; do not recompute it from candles, openPrice/lastPrice, spot data or another provider.
- Match returned ticker rows to the dynamically discovered eligible symbols by exact symbol.
- Require a valid numeric percentage, positive openPrice and lastPrice, positive count, valid openTime/closeTime, and a ticker-window closeTime within 15 minutes of Binance serverTime.
- Reject duplicate ticker symbols, missing eligible symbols and invalid/stale ticker rows. Coverage is incomplete unless every accepted eligible contract has one valid ticker row.
- Record priceChangePercent, lastPrice, openPrice, count, quoteVolume, openTime, closeTime and ticker-window age for each eligible contract.
- The ticker endpoint does not provide the exact timestamp of the last individual trade. Do not claim that window freshness proves last-trade recency; preserve this limitation in reports.
- Do not issue per-contract continuousKlines requests for the ranking.

Binance TradFi perpetuals are tradable 24/7. Trading schedules are informational only and must not gate the ranking.

## Provider/API and safety boundaries

Binance USD-M Futures is the only market-data provider for both legs. Current stages use public unauthenticated GET requests only: exchangeInfo, time, tradingSchedule, ticker/24hr, depth and premiumIndex.
No API credentials, private account endpoints, account state, orders, live or demo execution, or order-submission code. Do not add order-submission code to the later paper engine.

## Later execution/accounting gates

Before paper fills are implemented, derive actual per-contract tick size, quantity step, min notional/quantity, contract multiplier, price bands, order-book depth, fees, funding interval/rate/caps, leverage brackets, margin rules, maintenance margin and corporate-action treatment from Binance data/docs. Long entries consume asks and exits bids; short entries consume bids and covers asks. Compute VWAP and fees without double-counting spread/slippage. Distinguish contract traded price, index, mark and liquidation reference.

Use a deterministic UTC daily boundary. Transition both legs atomically or preserve the previous pair. Track holding durations, skipped transitions, funding, fees, margin and liquidation risk. Report relative price-ratio performance separately from paper account P&L.

## Stage gates

1. Read-only public API preflight.
2. Full-universe read-only 24-hour ticker coverage and ranking diagnostic.
3. Review universe classification, data coverage, timestamps and provisional leaders.
4. Validate contract economics, execution costs, funding, margin and off-hours pricing.
5. Implement atomic two-leg daily paper transitions and durable state.
6. Run extended paper validation and report performance/risk.

Current implementation is limited to stages 1 and 2. It must not create simulated positions, fills, NAV, portfolio state or exchange orders. Do not advance without explicit PASS/FAIL evidence and review of blockers.
