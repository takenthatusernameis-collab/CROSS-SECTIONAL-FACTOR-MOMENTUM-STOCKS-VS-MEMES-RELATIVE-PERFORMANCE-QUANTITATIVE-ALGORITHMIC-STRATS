# Stocks vs Memes — Daily Perpetual Paper Trader

A local, credential-free, **paper-only** simulation of a daily market-neutral pair:

- At each UTC day open, long the highest trailing-24h gainer among Gate.io USDT perpetuals classified as Stocks.
- Short the lowest trailing-24h performer among Gate.io USDT perpetuals classified as Memes.
- Hold both legs until the UTC day close.
- Default sizing is 2.0x total gross exposure: 1x NAV long plus 1x NAV short, equal USDT notional.

## Download and run

Download `stocks_vs_memes_workflow.zip` from this repository, extract it, then:

```bash
python -m venv .venv
# macOS/Linux: source .venv/bin/activate
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
pip install -e .
svm-paper universe
svm-paper run --loop
```

Use `svm-paper status` and `svm-paper report --date YYYY-MM-DD` for the local results.

## Category verification is mandatory

The Gate public contract endpoint exposes a `contract_type` field and documents `stocks` as an example. Gate also currently operates an official Futures Meme Zone, but the public API docs do not enumerate the precise `contract_type` string used by every Meme Zone contract. Run `svm-paper universe` to inspect actual values. The software uses exact category aliases from `config/strategy.json`, never guesses from ticker names, and skips the session if either category cannot be established.

Official Gate references:
- [Futures contract metadata](https://www.gate.com/docs/developers/apiv4/en/futures/)
- [Ticker statistics and 24h change percentage](https://www.gate.com/docs/developers/apiv4/en/futures/)
- [Order-book depth](https://www.gate.com/docs/developers/apiv4/en/futures/)
- [Historical funding rates](https://www.gate.com/docs/developers/apiv4/en/futures/)
- [Gate Futures Meme Zone announcement, 9 Nov 2026](https://www.gate.com/announcements/article/102676)

## Simulation details

Uses only public GET market-data endpoints. No API keys, secrets, private account endpoints, testnet, or real orders. It simulates book-depth VWAP fills, exchange taker fees, mark-price funding settlements, spread/market impact, minute mark-to-market equity, normalized ratio drift, ROI, and maximum drawdown. Execution costs are embedded in fill prices and are not double deducted.

SQLite state is local. Daily reports are exported to JSON, CSV, Markdown, and SVG. Drawdown is sampled from one-minute mark candles plus entry/exit points; it does not capture intraminute extremes. The full README, frozen strategy prompt, Python source, configuration, and tests are included in the ZIP.

## Validation

The current local build passes 17 unit/integration tests and Python compile checks. Tests include a complete simulated day using fake market data; those fixture results are not live exchange performance. Live Gate connectivity and Meme Zone classification must be validated on the machine running the workflow.
