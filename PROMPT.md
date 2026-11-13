# Frozen Strategy and Engineering Directive

Build and maintain a credential-free, locally persisted paper-trading workflow for the Stocks-vs-Memes daily market-neutral perpetual strategy.

At each UTC daily candle open, use Gate.io USDT perpetual contract metadata and its trailing 24-hour ticker percentage change to select exactly (1) the largest gainer among contracts classified by the exchange as Stocks and (2) the lowest percentage-change contract among contracts classified as Memes. Open a long in the first and a short in the second, sized to equal USDT notionals. Default to 2.0x total gross leverage: long notional = 1x NAV and short notional = 1x NAV. Hold through the UTC day and close at the next UTC midnight.

Category membership must come from native exchange metadata or documented authoritative category data. Never infer stock or meme status from ticker names, and never substitute a different contract because the selected one cannot be traded. If either category is missing, ambiguous, stale, or unexecutable, record a reason and skip the session or keep the close pending.

Use public GET endpoints only. No secrets, API credentials, private account endpoints, testnet, live order placement, or paid services. Simulate execution by sweeping public order-book levels for VWAP; reject incomplete depth; account for exchange taker fees and every funding event; use mark-price one-minute candles for mark-to-market accounting. Keep execution slippage embedded in fill prices, report it separately, and never deduct it twice. Report normalized long/short ratio drift separately from net portfolio ROI. Calculate maximum drawdown from the net equity path and label one-minute sampling limitations.

Persist strategy state, positions, minute marks, funding events, and reports locally in SQLite. Recover open or close-pending paper sessions after restart. Export JSON, CSV, Markdown, and a ratio chart. Include deterministic automated tests for category recognition and fail-closed behavior, ranking, sizing, order-book fills, fees, funding signs, ratio drift, ROI, drawdown, UTC boundaries, persistence, and a complete paper session lifecycle.

Keep the model logic frozen. Do not optimize, add filters, alter category definitions without source evidence, change leverage interpretation silently, or make any transition to live trading. Document exchange API or classification limitations honestly.
