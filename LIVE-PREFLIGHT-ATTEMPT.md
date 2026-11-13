# Bybit + CoinMarketCap live preflight attempt

- Attempt: 2026-11-13 05:24:58 UTC
- Method: public, unauthenticated HTTPS GET; no credentials, no testnet, no orders
- Bybit: `https://api.bybit.com/v5/market/instruments-info?category=linear`
- CoinMarketCap: `https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/categories`

## Result

**FAIL — inconclusive because both hostnames failed DNS resolution in this execution environment.**

- Bybit market feed: FAIL; `Temporary failure in name resolution` for `api.bybit.com`.
- CoinMarketCap taxonomy feed: FAIL; `Temporary failure in name resolution` for `pro-api.coinmarketcap.com`.
- Contract/category mapping: not established; prerequisite live responses were unavailable.
- Live Stocks contracts confirmed: none.
- Live Memes contracts mapped to Bybit: none.
- `feeds_ready=false`; `paper_trading_authorized=false`; `strategy_ranking_performed=false`.
- Exit code: 1.
- No paper positions were opened.

These DNS failures do not establish that either service lacks the required categories. They establish only that this terminal could not reach either host. A fresh successful `svm-paper preflight` on a machine with working DNS and HTTPS is required before the next scheduled UTC-open handler can open a new paper session. Manual preflight is diagnostic only and does not authorize an entry.

## Daily logic implemented

At UTC 00:00 only, the workflow finalizes the preceding session and constructs its completed ratio candle. Daily ratio ROI uses candle close/open; daily ratio drawdown uses low/open. It then calculates cumulative ratio drawdown against the historical high-water mark, records actual account ROI separately after VWAP/fees/funding, and only then fetches a fresh universe and considers the next daily pair.

The active-day scheduler sleeps until the next UTC boundary and does not poll rankings, compute performance or adjust simulated positions. Manual invocations outside the UTC-open grace window do not call market endpoints or alter state.

## Local validation

- 29/29 deterministic unit/integration tests passed.
- Python compile checks passed.
- Editable package install and CLI invocation passed.
- Intraday invocation returned "waiting for next UTC daily open" without a market call or position action.
- Local paper status: no sessions recorded.

Mocked test fixtures validate program logic, not live category support or profitability.
