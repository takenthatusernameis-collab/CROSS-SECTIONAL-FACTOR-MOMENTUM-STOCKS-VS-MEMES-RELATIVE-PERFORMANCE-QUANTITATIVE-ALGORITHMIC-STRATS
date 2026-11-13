# Stocks vs Memes — Daily Perpetual Paper Trader

A local, credential-free, **paper-only** simulation of a daily market-neutral Gate.io USDT perpetual pair:

- At each UTC day open, long the highest trailing-24h gainer among contracts whose exchange metadata classifies them as Stocks.
- Short the lowest trailing-24h performer among contracts whose metadata classifies them as Memes.
- Hold both legs until the UTC daily candle closes.
- Default sizing is 2.0x total gross exposure: 1x NAV long plus 1x NAV short, equal USDT notional.

## Mandatory live preflight — do this first

Run:

```bash
svm-paper preflight
```

Exit code 0 is required. The command fetches current public Gate contract metadata and ticker data, confirms at least one active direct USDT contract in both categories with a matching valid 24-hour ticker, records all observed `contract_type` values, saves `data/live_preflight_latest.json`, and returns exit code 1 on any failure.

Both `svm-paper run` and `svm-paper run --loop` enforce this gate before opening any new paper exposure. The daily entry path independently revalidates the category universe. Mock tests and documentation do not authorize trading. An already-open persisted paper session can resume to monitor or close after restart, but this exception cannot open new positions.

## Current live test result (13 November 2026)

**FAIL — inconclusive due to local DNS/network failure.** The terminal could not resolve `api.gateio.ws` while requesting the public contract endpoint. No live response body or category values were received, so neither Stocks nor Memes support was confirmed. The one-shot `svm-paper run` was also executed and exited with code 1 before opening a session. Paper trading is therefore blocked. See [LIVE-PREFLIGHT-ATTEMPT.md](LIVE-PREFLIGHT-ATTEMPT.md).

This failure does not mean that Gate lacks the category; it means this execution environment could not reach the feed. Retry `svm-paper preflight` from a machine with working DNS and HTTPS access. Only a fresh PASS permits new paper sessions.

## Category rules

Use native `contract_type` metadata only. No symbol-name guessing or alternative-category substitution. Only conservative exact aliases `stock`/`stocks` and `meme`/`memes` are enabled by default in `config/strategy.json`. Do not add aliases unless a live exchange response or authoritative Gate documentation confirms the exact value.

Gate's current API documentation defines `contract_type` as a contract classification and gives examples including `stocks`, metals, indices, forex, and commodities, but does not enumerate Meme values. Gate's official Futures Meme Zone announcement proves that the product category exists, not which `contract_type` string the public API returns.

## Simulation details

Uses public GET market-data endpoints only. No API keys, secrets, private account endpoints, testnet, paid services, or real order placement. Simulates book-depth VWAP fills, taker fees, funding settlements, spread/market-impact slippage, minute mark-to-market equity, normalized ratio drift, ROI, and maximum drawdown. Execution costs are embedded in fill prices and are not deducted twice.

SQLite state is local. Daily reports are exported to JSON, CSV, Markdown, and SVG. Drawdown uses one-minute mark candles plus entry/exit points and does not capture intraminute extremes.

## Install and run

Download and extract the workflow ZIP supplied with this conversation, then:

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -e .
svm-paper preflight
svm-paper run --loop
```

Other commands: `svm-paper universe` (same strict live check), `svm-paper status`, and `svm-paper report --date YYYY-MM-DD`.

## Validation

The current local build passes 20 unit/integration tests and Python compile checks. Tests include mocked positive and negative feed cases, explicit runner blocking when either category is missing, network-error blocking, and a complete fixture-based daily session. Mock fixture performance is not live exchange performance and does not satisfy the live preflight.

## Official Gate references

- [Gate API v4 access URLs](https://www.gate.com/docs/developers/apiv4/en/)
- [USDT Futures contract endpoint and `contract_type` schema](https://www.gate.com/docs/developers/apiv4/en/futures/)
- [Gate Futures Meme Zone announcement, 9 November 2026](https://www.gate.com/announcements/article/102676)
