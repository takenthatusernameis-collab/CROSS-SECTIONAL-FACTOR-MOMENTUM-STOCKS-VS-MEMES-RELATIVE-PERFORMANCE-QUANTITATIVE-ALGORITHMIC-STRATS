# Kalshi Stocks vs Crypto — isolated research increment

This directory is a separate Kalshi research namespace inside the existing repository. It does not alter the legacy stocks_vs_memes/ Bybit + CoinMarketCap implementation, its SQLite state, reports, dependencies, or daily workflow.

## Current scope: Stage 1 only

The initial increment is a **read-only public API preflight**. It performs unauthenticated HTTP GET requests against the Kalshi Perps production REST API and writes JSON and Markdown diagnostic reports. It does not rank markets, create simulated positions, persist portfolio state, or contain any order-submission code.

The only initial executable is:

    python -m kalshi_stocks_vs_crypto.preflight --output-dir artifacts/kalshi-preflight

It checks:

- GET /markets?status=active
- GET /markets/{ticker} for at most three representative tickers
- GET /markets/{ticker}/candlesticks for a 26-hour, one-minute sample
- GET /markets/{ticker}/orderbook
- GET /funding_rates/estimate?ticker=...
- GET /funding_rates/historical?ticker=...
- GET /markets/schedules
- GET /corporate_actions

The requests are based on Kalshi's official Perps REST documentation. They contain no authentication headers and do not access private account, order, or trading endpoints.

## Important interpretation limits

- The active market list exposes raw asset_class values and product_metadata; the program intentionally does not infer individual stocks, ETFs, indices, or crypto membership from ticker/title strings.
- Classification is a hard blocker until actual API metadata has been reviewed and shown to separate individual common stocks from ETFs and equity indices.
- The market-schedules endpoint reports holiday closures only. Regular daily/weekend sessions must use each market's schedule data.
- One-minute candles are sampled for endpoint/schema availability and data quality; this preflight does not calculate ranking returns.
- A successful endpoint call does not prove the full universe is classifiable, tradable, or liquid enough for the strategy.
- Perps production API access is documented as a member-by-member rollout. A GitHub runner result is evidence only for that environment and time.

## Strategy blueprint (not implemented in this increment)

- Long: highest trailing 24-hour return among eligible individual-stock perpetuals.
- Short: lowest trailing 24-hour return among all eligible crypto perpetuals.
- Long notional: 1× NAV; short notional: 1× NAV; gross exposure: 2× NAV.
- Exactly one pair and at most one rebalance attempt per eligible stock trading session.
- No averaging, intraday reranking, ETFs, indices, metals, energy, or prediction markets.
- Paper-only simulation; no real or demo orders.

The first implementation stops after preflight evidence is produced. Market ranking and any paper execution engine require separate review of the API results and classification evidence.
