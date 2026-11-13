# Kalshi Equities/Indices vs Crypto — isolated research increment

This is a separate Kalshi research namespace inside the existing repository. It does not alter the legacy stocks_vs_memes/ Bybit + CoinMarketCap implementation, its SQLite state, reports, dependencies, or daily workflow.

## User-approved universe rule

The long pool contains all active Kalshi markets whose exact raw asset_class is Equities or Indices. These classes are combined into one cross-sectional ranking. The short pool contains all active Kalshi markets whose exact raw asset_class is Crypto.

This deliberately accepts index contracts and ETFs if Kalshi places them in the accepted classes. Other classes, including Metals and Energy, are excluded. The implementation uses exact raw asset_class values and never infers category from ticker or title.

## Stage 1: API preflight

Run:

    python -m kalshi_stocks_vs_crypto.preflight --output-dir artifacts/kalshi-preflight

Completed successfully on GitHub-hosted Ubuntu in workflow run #3. The API returned 86 active markets: 58 Equities, 2 Indices, 19 Crypto, 6 Metals, and 1 Energy. Sample market detail, order books, one-minute candles, funding estimate/history, holiday schedules, and corporate actions returned HTTP 200. The nine Stage 1 tests and compilation passed.

## Stage 2: full-universe ranking diagnostic

Run:

    python -m kalshi_stocks_vs_crypto.ranking_preflight --output-dir artifacts/kalshi-ranking-preflight --workers 2

This stage remains read-only. It requests one-minute trade-price candles for every active market in the accepted long and short universes (79 markets based on the last preflight), calculates trailing 24-hour returns, and reports all data-quality failures plus provisional leaders.

The current completed-minute price must be no more than 15 minutes old. The reference close must be the latest valid close at or before 24 hours before the current close, and no more than 15 minutes before that reference cutoff. Actual observation timestamps, elapsed duration, and freshness gaps are reported per market.

A provisional leader is only the best/worst result among markets with valid observations. It is not a complete cross-sectional ranking unless every market in both universes passes the freshness and reference-data checks.

## Frozen portfolio parameters for later stages

- Long target notional: 1× NAV.
- Short target notional: 1× NAV.
- Total gross exposure: 2× NAV.
- Exactly one long and one short.
- No averaging or intraday reranking.
- At most one rebalance attempt per eligible Equities trading session, using Kalshi market schedules.
- Paper-only, no live or demo orders.

No paper positions, fills, portfolio state, or order-submission code are implemented in Stages 1 and 2. Later contract economics, execution, funding, margin, and schedule logic require their own validation.

## Source and safety boundaries

Kalshi is the only market-data provider. No CoinMarketCap, Bybit, another exchange, or external price/taxonomy provider is used by this project. Public unauthenticated GET requests only. No credentials, private-account endpoints, order endpoints, or real/demo execution code.

The existing Bybit/CMC implementation and Pionex Grid Restarter remain untouched.
