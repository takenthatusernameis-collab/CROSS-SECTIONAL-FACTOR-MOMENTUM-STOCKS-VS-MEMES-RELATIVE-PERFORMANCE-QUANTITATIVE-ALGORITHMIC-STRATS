# Strategy Directive — Bitget Stocks vs Crypto Daily Paper Portfolio

## Mission

Maintain a public-data-only, credential-free, paper-only cross-sectional pair on Bitget USDT-margined perpetuals. The strategy is deliberately simple and frozen. Do not add discretionary filters, change ranking fields, substitute runners-up, or submit exchange orders.

## Universe

Refresh `GET /api/v3/market/instruments?category=USDT-FUTURES` on each five-minute scheduled run. Eligible instruments must satisfy all of:
- `category == USDT-FUTURES`
- `quoteCoin == USDT`
- `status == online`
- `type == perpetual`

Classify only using Bitget's explicit `symbolType`:
- LONG universe: `symbolType == stock`
- SHORT universe: `symbolType == crypto`

Do not infer class from ticker, `baseCoin`, contract name, project name, token description or external taxonomy. Exclude metals, commodities, deliveries, inactive/restricted contracts, non-USDT contracts and all unknown categories.

## Frozen ranking and sizing

- Fetch the complete `GET /api/v3/market/tickers?category=USDT-FUTURES` response.
- Rank using the exact `price24hPcnt` value from Bitget, with no local return reconstruction.
- `price24hPcnt` is a decimal fraction; multiply by 100 for percentage display only.
- Require one unique, valid, fresh ticker for every eligible contract. Require positive last/bid/ask/mark prices and a ticker timestamp within 15 minutes of Bitget server time. Fail closed if coverage is incomplete.
- Long exactly the eligible stock with the highest 24-hour percentage return.
- Short exactly the eligible crypto contract with the lowest 24-hour percentage return.
- One position per leg. Long notional target = 1× NAV; short notional target = 1× NAV; total gross exposure target = 2× NAV.
- Refresh the market ranking every five-minute workflow run. Ranking refreshes never independently authorize a portfolio transition.
- Persist a dedicated SQLite `cooldown_control.cooldown` integer constrained to exactly `0` or `1`. Never use inferred entry age, UTC date, daily candles, or exchange time as the cooldown state.
- When `cooldown == 0`, the first scheduled workflow run refreshes current Bitget instruments/tickers, validates the latest eligible pair and execution data, and constructs the paper portfolio from that refreshed feed. Only after the entire accounting transaction succeeds does that same workflow atomically set `cooldown = 1`.
- Capture the workflow/job start timestamp before checkout, tests, ranking or API work. Store that exact timestamp as `activated_at_ms` and the portfolio-performance window start. The minute containing this timestamp is the activation minute.
- Schedule the reset at the close of the minute exactly 24 hours after the activation minute: `reset_at_ms = floor(activated_at_ms / 60,000) * 60,000 + 86,400,000 + 60,000`. At the first workflow run initiated at/after that timestamp, automatically change the persisted flag from `1` to `0`. Cron delay, late API responses, missed workflow slots, and portfolio construction time do not extend the reset.
- The first scheduled workflow run initiated after the flag has become `0` uses its run-start timestamp as the prior performance-window end, refreshes the latest feed, and builds the next portfolio if all checks pass. It then switches the flag back to `1` and starts a new window. If validation fails, leave the flag at `0` and retry on a later scheduled run; do not fabricate a fill or re-arm the lock.
- Persist that first post-reset workflow-start timestamp in `last_reset_run_started_ms`. If ranking, book depth or accounting validation fails on that run, later retries must keep this original performance-window end; they may continue live mark reporting, but must not slide the ROI/MDD boundary forward.
- ROI and holding-period MDD use the interval between actual workflow-start boundaries: the run that successfully changed `0 -> 1`, and the first eligible scheduled run that starts after the timed reset to `0`. A delayed workflow may make the measurement period longer than 24 hours; report the actual timestamps and duration. Do not substitute the Bitget response timestamp, UTC midnight, a daily snapshot, an idealized fill, or an assumed cron time for either boundary.
- Process minute OHLC history only through the workflow-start end boundary. Require contiguous paired history; gaps remain explicitly incomplete and cannot silently update MDD. Record each completed interval's ROI, MDD, coverage, actual start and end times.
- No runner-up, liquidity-ranked substitute, symbol-based exclusion, scaling, averaging, pyramiding or intraday reranking. If the selected pair cannot be safely paper-filled, skip the entire transition and preserve prior positions/state.

## Paper accounting

- Use unauthenticated GET only: instruments, server time, tickers, merge-depth, historical funding rates and historical mark candles.
- Simulate opening a long at ask depth VWAP and closing it at bid depth VWAP. Simulate opening a short at bid depth VWAP and covering it at ask depth VWAP.
- Use maximum public depth available from the order-book endpoint. Reject a transition if any full close/open quantity lacks sufficient visible depth.
- Size quantities using exchange `quantityMultiplier` / `quantityPrecision`, `minOrderQty`, `maxMarketOrderQty` and `minOrderAmount`. Do not create partial synthetic fills.
- Use Bitget instrument `takerFeeRate` by default; an explicit `BITGET_TAKER_FEE_RATE` override is allowed. State clearly that these rates are estimates, not a user-account fee quote.
- Apply funding at each observed settlement. The rate comes from Bitget public funding history. Estimate the settlement mark using the matching one-minute historical mark candle, or the preceding completed minute's close if the event is not aligned to a minute. Disclose minute-resolution uncertainty.
- For linear USDT perpetuals, funding cashflow is negative for a long when the funding rate is positive and positive for a short, with the sign reversed for negative rates.
- Track each leg's entry/exit price, quantity, notional, gross P&L, entry/exit fees, funding, net P&L and net trade ROI (`net P&L / entry notional`).
- Track portfolio cash, realized and unrealized P&L, fees, funding, gross exposure, NAV, daily return and cumulative portfolio ROI (`NAV / initial NAV - 1`). Preserve every closed and open trade in the lifetime ledger. Append a workflow-start-timestamped portfolio-equity point after each successful monitor/rebalance. Export the complete trade history, equity curve and daily equity returns. Recompute annualized Sortino from consecutive UTC daily equity closes with a 0% minimum acceptable return and √365 annualization; show it as unavailable until at least 20 daily returns and 5 negative days have been observed.
- Treat funding, closes, opens, ledger rows, trade updates and daily snapshot as one atomic SQLite transition. Any market-data/accounting failure rolls back the full transition.

## Minute spread and risk analytics (accounting only; do not change selection)

- After a pair has been opened, every five-minute run monitors the existing holdings while the 24-hour lock is active. Fetch live Bitget tickers for current mark/last prices and incrementally fetch all newly closed one-minute candles from the public `GET /api/v3/market/candles` endpoint. Use `category=USDT-FUTURES`, `interval=1m`, and `type=market` for the synthetic spread, plus `type=mark` for NAV/MDD.
- The synthetic spread ratio is `long market price / short market price`. Within each minute, spread high envelope = `long high / short low`; spread low envelope = `long low / short high`; interval spread DD = `(minimum spread low / maximum spread high - 1) × 100`.
- Reconstruct minute paper NAV from the previous snapshot's cash, observed funding settlements, actual leg quantities and entry prices. For a conservative mark-price NAV envelope, NAV high uses long mark high and short mark low; NAV low uses long mark low and short mark high. Update the historical strategy peak and MDD only from complete paired minute intervals, plus the final daily rebalance NAV.
- Synchronized OHLC bars do not prove exact intraminute price ordering or simultaneous cross-leg extrema. Label these values as conservative OHLC-envelope estimates, never tick-exact values.
- Require every expected complete minute to exist for both symbols and both candle types before updating historical intraday MDD. Record expected/matched counts and coverage. Partial/missing data is explicitly marked and must not contaminate the historical minute MDD. Candle-data failure must not alter the ranking or independently block a valid daily paper rebalance.
- Persist interval summaries and minute observations to SQLite. During the cooldown, monitoring may update only the dedicated `monitor_state`, `monitor_minute_metrics`, and `monitor_observations` tables plus strategy peak/MDD risk state after a complete paired segment. It must not update account cash, open positions, trades, the transaction/funding ledger, or `daily_snapshots`.
- Show current NAV/ROI estimates, each leg's direction, mark, unrealized P&L and estimated net leg ROI, pending funding estimate, spread mark/high/low/drawdown, daily MDD, historical strategy MDD, paired-minute coverage, and the countdown to eligibility.
- Pending funding in monitoring is informational only: do not post it to account cash or trade/ledger records until the next successful rebalance atomically applies each observed settlement exactly once.
- The monitor checkpoint advances only through a fully complete contiguous minute segment. If any expected paired market/mark candle is missing, do not update historical strategy MDD or advance past the gap; retry the missing segment on a later run. Never silently skip missing minutes.
- The first portfolio snapshot has no prior held interval; record `NO_PRIOR_POSITIONS` rather than inventing a spread or minute MDD. Do not call TradingView or use its API.

## Persistence and operation

- SQLite path: `state/bitget_paper.sqlite3`.
- Durable state branch: `bitget-paper-portfolio-state`.
- Keep append-only transaction events and closed trade history. Persist daily NAV snapshots. Upload per-run JSON/Markdown plus closed-trade and NAV CSVs.
- Initial NAV is fixed at 10 USDT for the reset campaign and is immutable after database initialization.
- Manual diagnostic runs must never change paper positions or monitoring state. Manual paper dispatch is opt-in and obeys the same persisted entry-time cooldown, candle-close requirement and first-run-after-expiry guard as the scheduled workflow.
- Scheduled workflow runs every five minutes (`*/5 * * * *`). Each scheduled run performs market ranking and, when an open pair exists, analytics-only monitoring. Portfolio transitions remain cooldown-gated as specified above.
- GitHub Actions concurrency must serialize runs so two jobs cannot monitor or mutate the same SQLite file concurrently. Do not cancel a job while it is persisting state.
- Every scheduled or manually dispatched paper run restores and persists the same SQLite state branch. Push-triggered and manual diagnostic runs remain read-only and must never update monitor or portfolio state.
- Do not override the cooldown in manual paper mode, on retry, after a data gap, or after workflow delay.

## Safety boundary and limitations

No API credentials, private account endpoints, account balances, live/demo order endpoints or order submission. Public market-data access does not prove that the user's account is eligible to trade a product. Paper VWAP is not an actual fill record. Liquidation, maintenance margin, ADL, insurance fund, product eligibility and corporate-action effects are not modeled. Never label paper ROI as live account ROI.

## Lifetime performance record

Track every open and closed trade from the $10 USDT reset onward. Persist a workflow-start-timestamped NAV equity curve after each successful mark/rebalance, export the full trade ledger and daily equity series, and recompute annualized Sortino from consecutive UTC daily equity closes using a 0% minimum acceptable return and a 365-day annualizer. Do not report Sortino until at least 20 daily return observations and 5 downside days exist. Preserve the current paper-only boundary.
