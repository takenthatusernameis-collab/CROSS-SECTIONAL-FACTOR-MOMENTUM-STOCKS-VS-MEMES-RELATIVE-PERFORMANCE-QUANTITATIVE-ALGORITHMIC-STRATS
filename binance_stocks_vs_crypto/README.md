# Binance Stock/Index Perpetuals vs Crypto Perpetuals — isolated research increment

This isolated Binance research namespace replaces the former Kalshi experiment. It does not alter the legacy stocks_vs_memes/ Bybit + CoinMarketCap implementation, its SQLite state, reports, dependencies or daily workflow.

## Dynamic universe
Every run refreshes Binance USDⓈ-M Futures exchangeInfo. Long: active USDT-quoted/margined TRADIFI_PERPETUAL contracts whose exact underlyingType is EQUITY, CN_EQUITY, KR_EQUITY, HK_EQUITY, PREMARKET or INDEX. Short: active USDT-quoted/margined PERPETUAL contracts with underlyingType=COIN. Other and unknown types are excluded. No static symbol list or ticker inference is used; new eligible contracts enter on the next refresh.

## Stage 1: public API preflight
Run: python -m binance_stocks_vs_crypto.preflight --output-dir artifacts/binance-preflight
This checks dynamic exchange metadata, server time, trading schedules (informational only), and sample stock/index and crypto contract klines, books and premium-index/funding fields. Public unauthenticated GET only.

## Stage 2: full-universe ranking diagnostic
Run: python -m binance_stocks_vs_crypto.ranking_preflight --output-dir artifacts/binance-ranking-preflight --workers 4
The daily GitHub Actions workflow runs at 00:05 UTC and can also be dispatched manually. It ranks every active accepted perpetual by its own one-minute traded-contract close: highest trailing 24h return on the long side, lowest on the short side. Zero-trade and forming candles are excluded; stale/missing contract observations are reported and make coverage incomplete.

## Frozen parameters for later paper stage
- Long 1× NAV; short 1× NAV; gross exposure 2× NAV.
- Exactly one long and one short.
- At most one pair rebalance per UTC calendar day.
- No averaging, pyramiding, intraday reranking or discretionary substitutions.
- Paper only; no live or demo orders.

Stages 1 and 2 do not create paper positions, fills, NAV or portfolio state. Contract economics, fees, funding, margin, off-hours pricing and regional product eligibility require validation before later paper trading.

## 24/7 and access caveat
Binance TradFi perpetual contracts can trade 24/7, including when the underlying stock exchange is closed. The underlying index reference and mark-price smoothing can behave differently off-hours; the ranking intentionally uses the perpetual contract's own trades. A public market-data API response does not establish that Binance TradFi Perps are enabled for a specific account or jurisdiction; verify current product eligibility.

## Boundaries
Binance USDⓈ-M Futures is the sole provider in this package. No Kalshi, CoinMarketCap, Bybit, external price source, API credentials, private account endpoints or order endpoints. The historical Bybit/CMC predecessor and Pionex Grid Restarter remain untouched.