# Binance Stock/Index Perpetuals vs Meme Perpetuals

This package discovers eligible Binance USD-M Futures contracts, ranks them using Binance's own rolling 24-hour ticker report, and maintains a persistent paper portfolio with daily NAV snapshots and a transaction ledger.

## Dynamic eligible universe

Every daily run refreshes Binance USD-M Futures exchangeInfo. The two strategy universes are:

- LONG: active USDT-quoted and USDT-margined TRADIFI_PERPETUAL contracts whose exact underlyingType is EQUITY, CN_EQUITY, KR_EQUITY, HK_EQUITY, PREMARKET or INDEX.
- SHORT: active USDT-quoted and USDT-margined PERPETUAL contracts with underlyingType=COIN and an underlyingSubType array containing the Meme tag (case-insensitive).

Other crypto subtypes, commodities, FX, non-trading contracts, non-USDT contracts and unknown classifications are excluded. The subtype must come from Binance metadata; no symbol, project name or ticker inference is permitted. New eligible listings enter automatically after the next exchangeInfo refresh.

## Ranking signal

The daily workflow uses one unfiltered GET /fapi/v1/ticker/24hr response and takes Binance's priceChangePercent field directly:

- LONG: highest reported 24-hour percentage change among eligible stock/index perpetuals.
- SHORT: lowest reported 24-hour percentage change among eligible Meme perpetuals.

It does not reconstruct the percentage from candle history or from lastPrice/openPrice. Every accepted ticker row must have valid prices, a positive 24-hour trade count, valid timestamps and a ticker-window closeTime within 15 minutes of Binance serverTime. A missing or invalid row blocks the portfolio update. The endpoint does not expose the exact timestamp of the latest individual trade; window freshness is not proof of last-trade recency.

## Persistent paper portfolio

The scheduled GitHub Action runs at 00:05 UTC. After a PASS ranking diagnostic, it runs the paper portfolio engine:

1. Accrue every public Binance funding-rate-history event for the previously held positions since their last funding checkpoint. Regular and Special funding events are recorded separately.
2. Close the previous LONG at bid-side depth VWAP and the previous SHORT at ask-side depth VWAP.
3. Deduct estimated taker fees and realize each leg's linear USDT P&L.
4. Open the new LONG at ask-side depth VWAP and new SHORT at bid-side depth VWAP, sizing each leg to approximately 1× pre-entry NAV after estimated entry fees. Quantities are rounded down using exchange lot-size filters.
5. Mark both new positions to the best-bid/best-ask midpoint and save NAV, unrealized P&L, realized P&L, cumulative fees, funding cash flow and gross exposure.

The engine requests up to 100 levels of public depth for each contract being closed or opened. It refuses to commit the day if a selected contract lacks usable lot-size metadata, visible depth is insufficient, or funding data is malformed. Both legs, their ledger entries and the daily snapshot are committed atomically in SQLite.

The strategy closes and reopens both legs on each valid daily snapshot—even when a symbol remains selected—so target weights are reset to 1× NAV per leg and both-side turnover costs are represented. Only one committed snapshot is allowed per UTC date. A repeat run on the same date is idempotent and creates no duplicate trades. Missed days are not backfilled; the database records the elapsed days between successful snapshots and reports the return over that period.

### State persistence

The database is `state/paper_portfolio.sqlite3`. Because GitHub-hosted runners are ephemeral, the workflow restores and commits this SQLite file on a dedicated `paper-portfolio-state` branch. The branch is the durable source of truth; each run also uploads a copy with its JSON and Markdown reports. This repository is public, so the paper ledger is public as well. It contains no API keys or private account data.

### Initial NAV and fee assumptions

The default initial paper NAV is 10,000 USDT. It can be overridden before the first portfolio run with the repository variable `PAPER_INITIAL_NAV_USDT`. Once the database exists, initial NAV is intentionally immutable; changing it requires an explicit state reset rather than silently rewriting performance history.

Default estimated taker fees are 0.0005 (0.05%) per fill for each leg. Override them with repository variables `PAPER_LONG_TAKER_FEE_RATE` and `PAPER_SHORT_TAKER_FEE_RATE`. The long TradFi-perpetual fee schedule and the short USDⓈ-M fee schedule may differ, and actual rates depend on account tier and discounts. Set these variables to the applicable rates before evaluating net performance. The defaults are assumptions, not an account-specific fee quote. Binance states that futures fees vary by VIP tier and publishes separate USDⓈ-M and TradFi fee tables: https://www.binance.com/en/support/faq/detail/360033544231

## What the performance numbers mean

- Entry/exit prices are simulated market-order VWAP estimates over the retrieved public order-book depth, not actual exchange fills.
- Unrealized P&L is marked at the best-bid/best-ask midpoint. Crossing the spread is represented by the entry/exit VWAP and midpoint mark.
- Funding is accrued from Binance's public funding history using the associated mark price and funding rate.
- Taker fees use the configured estimates above.
- Gross exposure, cash balance, NAV, return since the previous snapshot, cumulative return, realized/unrealized P&L, fees and funding are stored per daily snapshot.
- Margin/leverage brackets, maintenance margin, liquidation, ADL, insurance-fund effects, dividend/corporate-action cash adjustments, and account/jurisdiction product eligibility are not modeled. Therefore this is a transparent paper-performance estimate, not an exchange fill record or a fully validated margin-risk simulation.

No API credentials, private account endpoints, account balances, live or demo order endpoints, or order-submission code are used.

## Running locally

Stage 1 public API preflight:

    python -m binance_stocks_vs_crypto.preflight --output-dir artifacts/binance-preflight

Stage 2 full-universe ranking diagnostic:

    python -m binance_stocks_vs_crypto.ranking_preflight --output-dir artifacts/binance-ranking-preflight

Stage 3 paper accounting (requires a PASS ranking report and public Binance API access):

    python -m binance_stocks_vs_crypto.paper_portfolio --ranking-report artifacts/binance-ranking-preflight/binance_ranking_report.json --db-path state/paper_portfolio.sqlite3 --output-dir artifacts/binance-paper-portfolio

The SQLite database can be inspected with any SQLite client. The core tables are `daily_snapshots`, `transaction_ledger`, `open_positions` and `portfolio_account`.
