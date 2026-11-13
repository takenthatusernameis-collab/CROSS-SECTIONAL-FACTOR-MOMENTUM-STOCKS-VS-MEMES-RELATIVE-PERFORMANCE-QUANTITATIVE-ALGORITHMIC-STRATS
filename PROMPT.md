# Frozen Strategy and Engineering Directive

Build and maintain a credential-free, locally persisted, paper-only simulator for the Stocks-vs-Memes daily market-neutral Gate.io USDT perpetual strategy.

## Frozen strategy logic

At each UTC daily candle open, use Gate.io USDT perpetual contract metadata and trailing-24-hour ticker percentage changes to select exactly:
1. The highest percentage-change active Stocks-category perpetual.
2. The lowest percentage-change active Memes-category perpetual.

Go long the first and short the second with equal USDT notionals. Default to 2.0x total gross leverage: 1x NAV long plus 1x NAV short. Hold through the UTC day and close at the next UTC midnight. Do not optimize, add filters, substitute contracts, or change the model logic.

## Mandatory live-feed preflight gate

Before any new paper session starts, make fresh unauthenticated GET requests to:
- `/futures/usdt/contracts`
- `/futures/usdt/tickers`

Confirm from the actual responses that both categories exist in the exchange contract metadata, contain at least one active direct USDT contract, and each have a matching ticker with a valid parseable 24-hour percentage change. Record the UTC check time, endpoint URLs, response counts, all observed raw `contract_type` values, eligible counts, and the current stock leader and meme laggard.

Expose the check as `svm-paper preflight`. Return exit code 0 only on PASS, otherwise exit nonzero. Persist the result locally. Both one-shot and continuous runner entry points must run the preflight before opening any new paper exposure; independently revalidate both categories immediately at each UTC entry. A mocked test, old report, announcement, or ticker-name heuristic must never authorize trading. If DNS, network, API, payload, category, or matching-ticker validation fails, block new paper sessions and explain the reason. Do not record a network failure as proof that a category does not exist. A previously persisted open paper session may resume solely to monitor or close existing exposure after restart; it may not use this exception to open new exposure.

Use only exact category labels confirmed by live exchange responses or authoritative Gate documentation. Default to conservative `stock`/`stocks` and `meme`/`memes`; do not add speculative variants such as `meme_zone` without evidence.

## Execution, accounting, and persistence

Use public GET endpoints only. No API keys, secrets, private account endpoints, testnet, live orders, or paid services. Estimate execution by sweeping public order-book levels for VWAP and reject insufficient depth. Account for exchange taker fees and every funding event using correct long/short signs. Use mark-price one-minute candles for mark-to-market accounting. Keep execution slippage embedded in fill prices, report it separately, and never deduct it twice. Report normalized long/short ratio drift separately from net portfolio ROI. Calculate maximum drawdown from the net equity path and label its one-minute sampling limitations.

Persist strategy state, positions, minute marks, funding events, and reports in local SQLite. Recover open or close-pending paper sessions after restart. Export JSON, CSV, Markdown, and a ratio chart.

## Required tests

Include deterministic tests for category recognition and fail-closed behavior, ranking, sizing, order-book fills, fees, funding signs, ratio drift, ROI, drawdown, UTC boundaries, persistence, restart recovery, complete daily lifecycle, and the mandatory preflight gate. Verify both a mocked PASS and mocked missing-category/network-error FAIL. Verify that both one-shot and continuous runner startup remain blocked on a failed live preflight. Never report mock data as actual exchange confirmation.

Keep this directive and strategy logic frozen. Document exchange API and classification limitations honestly. No transition to live trading.
