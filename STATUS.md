# Bitget Stocks vs Crypto — Paper Portfolio Status

> **Automatically synchronized main-branch snapshot.** The authoritative live dashboard and SQLite ledger continue to update on [the durable state branch](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/tree/bitget-paper-portfolio-state). Open the [latest live dashboard](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/blob/bitget-paper-portfolio-state/STATUS.md) for the newest marks.

> **Paper-only simulation.** No exchange orders are placed. Market data comes from Bitget public endpoints.

## At a glance

| Metric | Latest value |
|---|---:|
| Current NAV | **8.71 USDT** |
| Starting capital | 10.00 USDT |
| NAV change vs starting capital | **-1.29 USDT** |
| Portfolio ROI vs starting capital | **-12.93%** |
| Current-cycle ROI | -4.50% |
| Unrealized P&L (gross) | -0.47 USDT |
| Cumulative realized gross P&L | -0.49 USDT |
| Fees paid to date | 0.05 USDT |
| Funding posted to ledger | -0.26 USDT |
| Estimated funding pending | -0.01 USDT |
| Gross exposure | 18.84 USDT |
| Closed trades | 4 |

_NAV source: live mark-to-market observation. Values as of 2026-11-16 07:18:45 UTC._

## Latest workflow result

- **Paper runner status:** **MONITORING_ONLY**
- **Ranking data gate:** PASS
- **Monitoring operation:** MONITORING
- **Age of latest saved mark/snapshot:** 0 minutes

## Lifetime strategy performance

- **Initial capital:** 10.00 USDT
- **Current equity P&L vs initial capital:** **-1.29 USDT**
- **Lifetime portfolio ROI:** **-12.93%**
- **Equity-curve observations:** 33
- **Closed trades:** 4 | **Open trades:** 2
- **Realized net P&L on closed trades:** -0.80 USDT
- **Closed-trade win rate:** 50.00%
- **Profit factor:** 0.3216205849949802844862514462
- **Annualized Sortino ratio:** **Not yet statistically available**
- **Sortino status:** INSUFFICIENT_HISTORY: 2/20 daily returns
- **Sortino method:** consecutive UTC daily equity closes, 0% minimum acceptable return, annualized by √365; requires at least 20 valid daily returns and 5 negative days.
- **Sampled equity-curve MDD:** -18.25%

![Portfolio equity curve](https://raw.githubusercontent.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/bitget-paper-portfolio-state/equity_curve.svg)
[Complete equity curve CSV](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/blob/bitget-paper-portfolio-state/equity_curve.csv) · [Daily equity and returns](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/blob/bitget-paper-portfolio-state/daily_equity.csv) · [Complete trade history](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/blob/bitget-paper-portfolio-state/trade_history.csv)

### Full trade history

| Trade | Entry date | Leg | Contract | Status | Entry price | Exit price | Net P&L | Trade ROI |
|---:|---|---|---|---|---:|---:|---:|---:|
| 1 | 2026-11-14 | LONG | SOXSUSDT | CLOSED | 52.82500000 | 53.49900000 | 0.11 USDT | 1.14% |
| 2 | 2026-11-14 | SHORT | BANANAS31USDT | CLOSED | 0.00391800 | 0.00434400 | -0.86 USDT | -10.97% |
| 3 | 2026-11-15 | LONG | SKDDUSDT | CLOSED | 10.64400000 | 10.94700000 | 0.27 USDT | 2.94% |
| 4 | 2026-11-15 | SHORT | KAVAUSDT | CLOSED | 0.02938000 | 0.02945000 | -0.31 USDT | -3.41% |
| 5 | 2026-11-16 | LONG | DXYZUSDT | OPEN | 27.72300000 | — | — USDT | Not available |
| 6 | 2026-11-16 | SHORT | SOPHUSDT | OPEN | 0.00090900 | — | — USDT | Not available |

Sortino remains unavailable until enough daily observations exist; no ratio is inferred from a short initial sample.


## Open positions

| Leg | Contract | Quantity | Entry | Latest mark | Unrealized P&L | Trade ROI |
|---|---|---:|---:|---:|---:|---:|
| LONG | DXYZUSDT | 0.330000 | 27.72300000 | 27.75500000 | 0.01 USDT | -0.00% |
| SHORT | SOPHUSDT | 10,112.000000 | 0.00090900 | 0.00095700 | -0.49 USDT | -5.52% |

## Risk

- **Strategy maximum drawdown:** -38.45% (observed minute history)
- **Current holding-cycle MDD:** -16.65%
- **Minute-data coverage:** 194/194 (100.00%)
- **Risk-data quality:** **COMPLETE_CURRENT_CYCLE**
- **Historical risk intervals:** 2 complete / 0 incomplete
- **Latest completed risk interval:** COMPLETE

## Cooldown safeguard

- **Cooldown flag:** **ACTIVE (1)**
- Activated: 2026-11-16 04:02:14 UTC
- Scheduled reset: 2026-11-17 04:03:00 UTC
- Current cycle start: 2026-11-16 04:02:14 UTC
- A new pair is built only when the binary flag is 0 and the ranking/depth/accounting gates pass.

## Timestamps and provenance

- **STATUS.md generated:** 2026-11-16 07:18:48 UTC
- **Paper workflow started:** 2026-11-16 07:17:39 UTC
- **Latest saved market observation:** 2026-11-16 07:18:45 UTC
- **Workflow trigger:** **workflow_dispatch / paper**
- **GitHub Actions run:** [43822666209](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/actions/runs/43822666209)

## Interpretation notes

- NAV includes cash and mark-to-market unrealized P&L. Open-leg unrealized P&L is gross; an estimated exit fee has not yet been charged.
- Pending funding is an estimate and is not posted to the accounting ledger until the next successful rebalance.
- Without a current-pair monitor observation, marks fall back to the entry-time snapshot and are not live quotes.
- MDD is identified as unavailable until supported by minute-level observations; incomplete coverage is shown explicitly.
- This page is derived from the durable SQLite ledger after a successful paper run. The database remains the accounting source of truth.
