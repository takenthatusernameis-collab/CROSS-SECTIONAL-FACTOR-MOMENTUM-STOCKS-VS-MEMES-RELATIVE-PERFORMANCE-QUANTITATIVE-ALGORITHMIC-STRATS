# Binance Stock/Index Perpetuals vs Crypto Perpetuals

This package discovers eligible Binance USD-M Futures contracts and produces a daily read-only cross-sectional ranking.

## Dynamic universe

Every run refreshes Binance USD-M Futures exchangeInfo. Long: active USDT-quoted/margined TRADIFI_PERPETUAL contracts whose exact underlyingType is EQUITY, CN_EQUITY, KR_EQUITY, HK_EQUITY, PREMARKET or INDEX. Short: active USDT-quoted/margined PERPETUAL contracts with underlyingType=COIN. Other and unknown types are excluded. There is no static symbol list or ticker-name inference; newly eligible listings enter after the next metadata refresh.

## Stage 1: public API preflight

Run:

    python -m binance_stocks_vs_crypto.preflight --output-dir artifacts/binance-preflight

This checks live exchange metadata, server time, the trading schedule for information only, and sample 24-hour ticker, order-book and premium-index/funding responses for one eligible contract on each side. The 24-hour ticker check tests both stock/index and crypto contract categories. Requests are unauthenticated public GETs only.

## Stage 2: daily full-universe ranking diagnostic

Run:

    python -m binance_stocks_vs_crypto.ranking_preflight --output-dir artifacts/binance-ranking-preflight

The GitHub Actions workflow runs daily at 00:05 UTC and can also be dispatched manually. Each run makes one unfiltered GET request to /fapi/v1/ticker/24hr, then matches its rows to the live eligible universe from exchangeInfo.

The ranking uses Binance's priceChangePercent field directly:
- LONG: highest reported 24-hour percentage change among eligible stock/index perpetuals.
- SHORT: lowest reported 24-hour percentage change among eligible crypto perpetuals.

The workflow does not reconstruct the percentage from candle history or from lastPrice/openPrice. It validates required fields, positive open and last prices, a positive 24-hour trade count, unique symbols and ticker-window timestamp freshness. A missing or invalid row makes the universe coverage incomplete. Extra ticker rows outside the approved universe are ignored.

The response includes ticker-window openTime/closeTime and count, but does not expose an exact timestamp for the latest individual trade. Consequently, ticker-window freshness is not proof that the last trade itself occurred within the previous 15 minutes; this limitation is recorded in the report.

## Frozen strategy parameters for a later paper stage

- Exactly one long and one short.
- Long target notional = 1× NAV; short target notional = 1× NAV; gross exposure = 2× NAV.
- At most one pair-rebalance attempt per UTC calendar day after the ranking snapshot.
- No intraday reranking, averaging, pyramiding, discretionary substitutions or additional signal filters.
- No underlying-equity session gate: stock/index perpetuals remain eligible around the clock.

Stages 1 and 2 do not create simulated positions, fills, NAV or portfolio state. They submit no orders. Contract economics, fees, funding, margin, off-hours pricing and account/jurisdiction product eligibility must be validated before any later paper-trading implementation.
