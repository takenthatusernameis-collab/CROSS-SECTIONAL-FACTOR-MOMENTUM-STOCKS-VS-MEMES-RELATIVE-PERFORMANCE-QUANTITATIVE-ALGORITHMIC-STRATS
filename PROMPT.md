# Frozen Strategy Directive — Stocks vs Memes (Bybit + CoinMarketCap)

Implement and maintain a local, credential-free, paper-only daily market-neutral perpetual strategy. The strategy logic is frozen; do not optimize, add filters, introduce discretionary substitutions, or place live orders.

## Data architecture

**Bybit V5 public market-data feed:** Use active USDT-settled linear perpetual instruments only: `contractType=LinearPerpetual`, `status=Trading`, `quoteCoin=USDT`, `settleCoin=USDT`. Native Bybit `symbolType=stock` is the exclusive Stocks category definition. Use Bybit tickers for rolling 24-hour price change and turnover, order-book depth for simulated execution, mark-price one-minute candles for spread accounting, and funding history for settlements.

**CoinMarketCap keyless public API:** Use the public no-key/no-signup route rooted at `https://pro-api.coinmarketcap.com/public-api`. Discover exactly one category named `Memes`, retrieve its full paginated member list using documented `num_tokens` metadata, and resolve candidate Bybit `baseCoin` symbols with the active cryptocurrency ID map. A ticker is a Meme only if the base symbol maps to exactly one active CMC ID and that ID belongs to the exact Memes category. If a symbol maps to multiple IDs, exclude it; never guess. CMC supplies taxonomy only, not price returns or rankings.

## Frozen daily strategy

At UTC 00:00 only:
- Long exactly the active Bybit USDT linear perpetual with the highest trailing-24-hour Bybit `price24hPcnt` among Bybit `symbolType=stock` contracts.
- Short exactly the active Bybit USDT linear perpetual with the lowest trailing-24-hour Bybit `price24hPcnt` among uniquely mapped CMC Memes category members.
- Do not substitute a runner-up if the selected contract is missing, ambiguous or unexecutable. Skip the new session and record the reason.
- Use 2.0x total gross leverage split into equal USDT notionals: 1.0x NAV long and 1.0x NAV short.
- Hold both legs throughout that UTC daily candle and close/replace only at the next UTC daily boundary.

## Strict timing state machine

The strategy may trigger, rank, adapt/replace positions, and compute completed performance only at UTC 00:00 when the prior daily candle has just closed. The continuous runner sleeps until that boundary; no intraday polling, ranking, performance calculation, position adaptation, scaling or re-entry is allowed.

At each boundary:
1. Finalize the prior session from its complete synchronized one-minute mark data and all funding settlements.
2. Construct the completed ratio-spread candle for the pair held during that prior UTC day.
3. Compute daily ratio ROI from close/open and daily adverse excursion/drawdown from low/open.
4. Compute cumulative ratio index and maximum drawdown using the completed daily low against the historical high-water mark.
5. Record separate net account P&L and account ROI after fills, fees and funding.
6. Fetch and validate a fresh Bybit/CMC universe, rank categories, and open the next daily paper position only if every source and execution precondition passes.

Manual `preflight` is diagnostic only: it may establish feed reachability, categories, mappings and eligible counts but must not rank, compute performance or modify positions. A manual `run` outside UTC-open grace must return without market-data calls or state changes. A preflight PASS alone does not authorize or open exposure. Only the daily-boundary handler does so. If a scheduled close cannot be safely reconstructed, leave it visibly pending; never silently enter late.

## Ratio candle and metric definitions

For the previous day's held pair: `ratio(t) = long_mark(t) / short_mark(t)`.

- Open = first synchronized minute's long open / short open.
- Close = last synchronized minute's long close / short close.
- For each minute, conservative high bound = long high / short low; conservative low bound = long low / short high.
- Daily high/low are max/min of those bounds plus open/close. Require all 1,440 synchronized minutes including the first and last; exclude any bar timestamped at the next midnight.

Daily ratio ROI = `(ratio_close / ratio_open - 1) * 100`.
Daily ratio drawdown/adverse excursion = `min(0, ratio_low / ratio_open - 1) * 100`.
Cumulative ratio index compounds close/open ratio returns from 1.0. Maximum ratio drawdown compares cumulative candle low against the historical high-water mark and carries forward the worst drawdown across recoveries. Conservatively assume the day's high preceded its low. Clearly disclose that one-minute separate-leg OHLC bounds are not exact tick-synchronous spread extrema and may overstate intraminute range.

Ratio ROI/drawdown are relative-spread metrics, not leveraged account returns. Account P&L/ROI must be separately derived from the actual simulated two-leg entry/exit VWAPs, funding cashflows and trading fees.

## Execution costs and persistence

Sweep Bybit public order-book levels for entry/exit VWAP and reject insufficient depth. Respect quantity step, minimum quantity/notional and max market quantity. Apply configurable taker fees (default 0.055% standard-rate assumption, not an account-specific quote). Apply all funding settlements with correct long-pays/short-receives sign and contemporaneous mark price. Report slippage versus mid but do not double-deduct it because VWAP already embeds it.

Persist sessions/reports locally in SQLite and recover safely without unscheduled intraday actions. Export JSON, CSV, Markdown and SVG.

## Safety and required validation

Use public GET only. No secrets, API keys, private account endpoints, testnet, paid data feeds or live orders. Fail closed for DNS/API errors, missing categories, ambiguous mappings, stale/incomplete data, missing one-minute bars, or inadequate book depth.

Test keyless GET behavior, Bybit instrument pagination and native stock type, CMC category pagination and ID-map ambiguity, ranking, equal-notional sizing, VWAP, fees, funding, ratio OHLC, ROI from close/open, drawdown from low/open, cumulative MDD, portfolio ROI, UTC boundaries, zero intraday actions, close-then-open order, skip conditions and restart persistence.

Mock tests prove program behavior only. A live preflight must confirm both data sources and matching current tradable contracts; a manual preflight never authorizes a strategy entry. Keep the strategy frozen and paper-only.


## Scheduled GitHub Actions runner

Deploy the strategy as a GitHub-hosted Ubuntu scheduled workflow, not as a permanently running ChatGPT terminal process. Trigger once daily at 23:58 UTC, wait until UTC midnight, and invoke exactly one daily boundary operation. The run may process the current day's open if GitHub starts it up to 15 minutes after midnight; beyond that grace it must close any overdue persisted paper session at its first available public book, record the actual delayed exit, then skip the current day's new entry. Never open a late pair. A scheduled run must never loop, poll markets intraday, or run a second strategy event in the same UTC date.

Manual `workflow_dispatch` is diagnostic-only and calls live preflight without ranking, calculating strategy performance, or changing positions. Restore the latest prior scheduled-run state artifact before running; validate the SQLite database; execute automated tests/compile checks; then, on scheduled events only, run `python -m stocks_vs_memes.cli run --scheduled-once`. Always upload the updated SQLite database and report directory as a uniquely scoped per-run Actions artifact (90-day retention). The GITHUB_TOKEN requires read access to Actions artifacts; no exchange credentials or API secrets are required.

Use concurrency to prevent overlapping daily sessions. If no prior scheduled state artifact can be found, start only from configured initial equity and clearly log that it is a first run. If a previous scheduled run exists but its state artifact cannot be downloaded or fails SQLite integrity, abort safely instead of resetting state. GitHub schedule delays/dropped events must be exposed as missed-boundary skips, never compensated with an intraday entry.
