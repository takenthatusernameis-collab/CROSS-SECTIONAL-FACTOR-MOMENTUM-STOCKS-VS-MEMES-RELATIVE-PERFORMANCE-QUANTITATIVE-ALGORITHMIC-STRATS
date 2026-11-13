# Binance Stock/Index Perpetuals vs Meme Perpetuals — frozen research directive

## Mission

Maintain a daily, public-market-data-only strategy research workflow that dynamically discovers Binance USD-M Futures stock/index and Meme perpetuals, ranks the eligible contracts, and records a persistent two-leg paper portfolio. The paper engine estimates portfolio performance; it does not execute exchange orders.

## Dynamic eligible universe

Refresh GET /fapi/v1/exchangeInfo on every diagnostic/rebalance cycle. Do not hardcode symbols.
- LONG: status=TRADING, contractType=TRADIFI_PERPETUAL, quoteAsset=USDT, marginAsset=USDT, and exact underlyingType in EQUITY, CN_EQUITY, KR_EQUITY, HK_EQUITY, PREMARKET, INDEX.
- SHORT: status=TRADING, contractType=PERPETUAL, quoteAsset=USDT, marginAsset=USDT, underlyingType=COIN, and underlyingSubType is an array containing the exact category tag Meme (case-insensitive).
- Exclude all other crypto subtypes from SHORT, plus non-trading, non-USDT-quoted/margined, commodity, FX and unknown underlying types.
- Never infer a classification from symbol, ticker, token/project name or online descriptions.
- Newly listed qualifying stock or Meme contracts enter automatically after the next exchangeInfo refresh; inactive or delisted contracts exit automatically from the selection universe.

## Frozen strategy parameters

- Long exactly the eligible stock/index perpetual with the highest Binance-reported rolling 24-hour percentage change.
- Short exactly the eligible Meme perpetual with the lowest Binance-reported rolling 24-hour percentage change.
- Long target notional = 1× NAV; short target notional = 1× NAV; gross exposure target = 2× NAV.
- Exactly one position per side.
- One portfolio transition per UTC calendar date, at most.
- The daily transition closes both previous legs and opens both newly selected legs, even if one or both symbols remain unchanged; this resets both leg notionals to the current NAV target and charges the corresponding paper turnover costs.
- No intraday reranking, averaging, pyramiding, discretionary replacement, or additional signal filters.

## Ranking data source

Use the single unfiltered GET /fapi/v1/ticker/24hr response as the sole 24-hour ranking source.
- Select Binance's priceChangePercent field directly; do not recompute it from candles, openPrice/lastPrice, spot data or another provider.
- Match returned ticker rows to the dynamically discovered eligible symbols by exact symbol.
- Require a valid numeric percentage, positive openPrice and lastPrice, positive count, valid openTime/closeTime, and a ticker-window closeTime within 15 minutes of Binance serverTime.
- Reject duplicate ticker symbols, missing eligible symbols and invalid/stale ticker rows. Coverage is incomplete unless every accepted eligible contract has one valid ticker row.
- Record priceChangePercent, lastPrice, openPrice, count, quoteVolume, openTime, closeTime, ticker-window age and exchange lot filters for each eligible contract.
- The ticker endpoint does not provide the exact timestamp of the last individual trade. Do not claim that window freshness proves last-trade recency; preserve this limitation in reports.
- Do not issue per-contract continuousKlines requests for the ranking.

Binance TradFi perpetuals are tradable 24/7. Trading schedules are informational only and must not gate the ranking.

## Paper execution and accounting model

Use only public, unauthenticated Binance USD-M Futures GET endpoints: exchangeInfo, time, tradingSchedule, ticker/24hr, depth and fundingRate.
- Require a PASS full-universe ranking gate before any portfolio state transition.
- Long entry consumes ask depth; long exit consumes bid depth. Short entry consumes bid depth; short cover consumes ask depth.
- Simulate marketable fills by walking up to 100 displayed depth levels and calculate VWAP. Fail closed when displayed depth cannot fill the full quantity.
- Use MARKET_LOT_SIZE when valid; otherwise use LOT_SIZE. Round quantity down to the exchange step and enforce min/max quantity and min notional when exposed by exchange metadata.
- Apply separate configurable taker-fee assumptions for the TradFi long and USDⓈ-M Meme short. Defaults are 0.0005 per fill; these are estimates and must be replaced with applicable account-specific rates before interpreting net performance.
- Accrue every public funding-rate-history event while a position is open, including Regular and Special event types, using quantity × funding mark price × funding rate and the correct long/short cash-flow sign.
- Mark open positions at the top-of-book midpoint. Keep realized P&L, unrealized P&L, fees and funding separate.
- Treat a two-leg transition, all associated ledger rows and its daily NAV snapshot as one atomic SQLite transaction. If market data or accounting validation fails, preserve the previous pair and do not commit a partial day.
- Same-day reruns must be idempotent: a committed UTC date cannot create duplicate ledger entries.
- If a scheduled day is missed, do not invent/backfill fills. Report the actual elapsed days since the previous successful snapshot.

## Persistent state and configuration

- Persist the SQLite database at state/paper_portfolio.sqlite3 on the dedicated paper-portfolio-state Git branch. The GitHub Actions runner is ephemeral; never rely on its local filesystem as the durable store.
- Store daily NAV snapshots, the current open pair, and an append-only event ledger for OPEN, CLOSE and FUNDING transactions.
- Initial NAV defaults to 10,000 USDT and is configurable with PAPER_INITIAL_NAV_USDT before the first run. Initial capital is immutable once database state exists.
- Fee assumptions are configurable with PAPER_LONG_TAKER_FEE_RATE and PAPER_SHORT_TAKER_FEE_RATE. Store the applied rates in each snapshot and transaction.
- Keep the state public and free of credentials or private account information. No secrets belong in the SQLite file or artifacts.

## Provider/API and safety boundaries

Binance USD-M Futures is the only market-data provider for both legs. All current actions use public unauthenticated GET requests only.
No API credentials, private account endpoints, account state, actual orders, live or demo execution, or order-submission code. Do not add order-submission code to the paper engine.
Do not claim the portfolio simulation is an exchange execution record.

## Known model limitations

The current paper model does not calculate margin tiers, isolated/cross margin, maintenance margin, liquidation price, ADL, insurance-fund effects, leverage-bracket changes, dividend/corporate-action cash adjustments, or account/jurisdiction product eligibility. Keep these limitations visible in all performance summaries. Do not describe the paper NAV as fully margin-risk-validated.

## Stage gates

1. Public API preflight.
2. Full-universe read-only 24-hour ticker coverage and ranking diagnostic.
3. Persistent paper accounting with order-book VWAP estimates, funding, configurable fee assumptions, daily snapshots and transaction ledger.
4. Extended paper validation and a separate review of margin/liquidation, corporate-action and jurisdiction-specific eligibility risks.

The paper engine may run only when ranking coverage is complete and each required order-book/funding data request passes validation. It must never submit exchange orders.
