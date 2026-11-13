# Stocks vs Memes — Daily Perpetual Paper Trader

Credential-free, local, paper-only simulation of a daily market-neutral pair using **Bybit public market data + CoinMarketCap's keyless public category taxonomy**.

- Long the highest 24-hour gainer among Bybit's native stock perpetuals.
- Short the lowest 24-hour performer among Bybit USDT linear perpetuals uniquely mapped into CoinMarketCap's exact **Memes** category.
- Equal USDT notional legs. Default gross leverage is 2.0x total: 1x NAV long and 1x NAV short.
- Hold each pair for one UTC daily candle. No intraday re-ranking, rebalancing, performance computation, or discretionary position change.

## Important: live source check

Run `svm-paper preflight` on a network with working DNS and HTTPS. It independently checks Bybit and CoinMarketCap, then verifies the stock and meme contracts map to current Bybit tickers. A PASS proves the data sources are ready but does **not** authorize or execute a trade. Only the scheduled UTC daily-open handler can rank and open a simulated session.

**Latest recorded live attempt (2026-11-13 05:24:58 UTC): FAIL / inconclusive.** DNS resolution failed independently for `api.bybit.com` and `pro-api.coinmarketcap.com`; no live category data was received. This is not evidence either provider lacks the categories. `feeds_ready=false`, `paper_trading_authorized=false`, `strategy_ranking_performed=false`. No positions were opened. See [LIVE-PREFLIGHT-ATTEMPT.md](LIVE-PREFLIGHT-ATTEMPT.md).

## Exact daily timing and ratio metrics

At UTC 00:00 only, the workflow:
1. Finalizes the prior day's paper session.
2. Builds the completed ratio candle from synchronized one-minute Bybit mark candles for the pair that was held during that prior UTC day.
3. Calculates prior-day ratio ROI from the ratio candle's **close/open**, and daily adverse excursion/drawdown from its **low/open**.
4. Calculates cumulative ratio index and historical maximum drawdown using the daily low against the running high-water mark.
5. Fetches and validates fresh Bybit/CMC universes, ranks the two categories, and opens the next paper session if all source and execution conditions pass.

The ratio is `long_mark / short_mark`. Ratio daily ROI is `(ratio_close / ratio_open - 1) * 100`; daily ratio drawdown is `min(0, ratio_low / ratio_open - 1) * 100`. Ratio statistics are not leveraged account ROI. A separate portfolio ROI is calculated from both position legs' entry/exit VWAPs, fees and funding.

Ratio high/low use conservative one-minute bounds: long-high / short-low, and long-low / short-high. They are **not exact tick-synchronous spread extrema** and can overstate intraminute range. A completed daily candle requires exactly 1,440 synchronized minutes with its first and final minute; the next day's midnight bar is excluded. Cumulative MDD assumes daily high precedes the low because OHLC does not expose tick ordering.

The continuous runner sleeps until the next UTC daily open. A manual preflight is diagnostic only. A one-shot run outside the entry grace window returns without market-data calls. Incomplete data, ambiguous CoinMarketCap mappings, missing categories or insufficient execution depth fail closed.

## Sources

- Bybit V5 [linear instrument metadata](https://bybit-exchange.github.io/docs/v5/market/instrument) and [tickers](https://bybit-exchange.github.io/docs/v5/market/tickers).
- Bybit V5 [order book](https://bybit-exchange.github.io/docs/v5/market/orderbook), [mark-price kline](https://bybit-exchange.github.io/docs/v5/market/mark-kline), and [funding history](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate).
- CoinMarketCap [public API documentation](https://coinmarketcap.com/api/documentation/v1/), keyless public prefix `/public-api`, [Categories](https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/categories), [Category details](https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/category), and [ID map](https://pro-api.coinmarketcap.com/public-api/v1/cryptocurrency/map).

Bybit `symbolType=stock` defines the stock universe. CoinMarketCap supplies the exact Memes category only. All rankings, executable market prices and fills come from Bybit. CMC symbols are matched to Bybit `baseCoin` only when the active CMC ID map gives exactly one ID and that ID belongs to the Memes category. Ambiguous symbols are excluded.

## Install

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -e .
svm-paper preflight
svm-paper run --loop
```

No API keys, secrets, private account endpoints, testnet, paid feeds, or real orders.

Commands: `svm-paper preflight`, `svm-paper universe`, `svm-paper run`, `svm-paper run --loop`, `svm-paper status`, `svm-paper report --date YYYY-MM-DD`.

SQLite state is saved locally under `data/`; completed reports are exported to JSON, CSV, Markdown and SVG under `reports/`.

## Simulation costs and risk

Entry/exit are simulated by sweeping public order-book depth for VWAP. Incomplete depth is rejected. Taker fees default to 0.055% per side as a configurable standard-rate assumption, not an account-specific fee quote. Funding is applied at every observed settlement with correct long/short cashflow signs. Slippage is measured versus mid-price for diagnostics and is already embedded in VWAP; it is never deducted twice. Market-data completeness and exit delay are included in each report.

## Tests

`python -m unittest discover -s tests -v` currently passes **29 deterministic unit/integration tests**. `python -m compileall -q stocks_vs_memes tests` passes. Tests use fake market responses and prove code behavior only; they are not live source confirmation or evidence of strategy profitability.
