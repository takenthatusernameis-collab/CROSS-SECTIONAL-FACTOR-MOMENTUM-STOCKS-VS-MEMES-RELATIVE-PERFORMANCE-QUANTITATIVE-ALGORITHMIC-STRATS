# Bitget Stocks vs Crypto — Paper Portfolio Status

> **Paper-only simulation.** No exchange orders are placed. Market data comes from Bitget public endpoints.

## At a glance

| Metric | Latest value |
|---|---:|
| Current NAV | **10.03 USDT** |
| Starting capital | 10.00 USDT |
| NAV change vs starting capital | **0.03 USDT** |
| Portfolio ROI vs starting capital | **0.25%** |
| Current-cycle ROI | 0.49% |
| Unrealized P&L (gross) | 0.04 USDT |
| Cumulative realized gross P&L | 0.00 USDT |
| Fees paid to date | 0.01 USDT |
| Funding posted to ledger | 0.00 USDT |
| Estimated funding pending | 0.00 USDT |
| Gross exposure | 17.26 USDT |
| Closed trades | 0 |

_NAV source: live mark-to-market observation. Values as of 2026-11-14 02:38:17 UTC._

## Latest workflow result

- **Paper runner status:** **MONITORING_ONLY**
- **Ranking data gate:** PASS
- **Monitoring operation:** MONITORING
- **Age of latest saved mark/snapshot:** 0 minutes

## Lifetime strategy performance

- **Initial capital:** 10.00 USDT
- **Current equity P&L vs initial capital:** **0.03 USDT**
- **Lifetime portfolio ROI:** **0.25%**
- **Equity-curve observations:** 3
- **Closed trades:** 0 | **Open trades:** 2
- **Realized net P&L on closed trades:** 0.00 USDT
- **Closed-trade win rate:** Not available
- **Profit factor:** Not available
- **Annualized Sortino ratio:** **Not yet statistically available**
- **Sortino status:** INSUFFICIENT_HISTORY: 0/20 daily returns
- **Sortino method:** consecutive UTC daily equity closes, 0% minimum acceptable return, annualized by √365; requires at least 20 valid daily returns and 5 negative days.
- **Sampled equity-curve MDD:** -0.24%

![Portfolio equity curve](equity_curve.svg)
[Complete equity curve CSV](equity_curve.csv) · [Daily equity and returns](daily_equity.csv) · [Complete trade history](trade_history.csv)

### Full trade history

| Trade | Entry date | Leg | Contract | Status | Entry price | Exit price | Net P&L | Trade ROI |
|---:|---|---|---|---|---:|---:|---:|---:|
| 1 | 2026-11-14 | LONG | SOXSUSDT | OPEN | 52.82500000 | — | — USDT | Not available |
| 2 | 2026-11-14 | SHORT | BANANAS31USDT | OPEN | 0.00391800 | — | — USDT | Not available |

Sortino remains unavailable until enough daily observations exist; no ratio is inferred from a short initial sample.


## Open positions

| Leg | Contract | Quantity | Entry | Latest mark | Unrealized P&L | Trade ROI |
|---|---|---:|---:|---:|---:|---:|
| LONG | SOXSUSDT | 0.180000 | 52.82500000 | 52.68000000 | -0.03 USDT | -0.39% |
| SHORT | BANANAS31USDT | 2,000.000000 | 0.00391800 | 0.00388700 | 0.06 USDT | 0.67% |

## Risk

- **Strategy maximum drawdown:** -1.60% (observed minute history)
- **Current holding-cycle MDD:** -1.60%
- **Minute-data coverage:** 10/10 (100.00%)
- **Risk-data quality:** **COMPLETE_CURRENT_CYCLE**
- **Historical risk intervals:** 0 complete / 0 incomplete
- **Latest completed risk interval:** NO_PRIOR_POSITIONS

## Cooldown safeguard

- **Cooldown flag:** **ACTIVE (1)**
- Activated: 2026-11-14 02:27:58 UTC
- Scheduled reset: 2026-11-15 02:28:00 UTC
- Current cycle start: 2026-11-14 02:27:58 UTC
- A new pair is built only when the binary flag is 0 and the ranking/depth/accounting gates pass.

## Timestamps and provenance

- **STATUS.md generated:** 2026-11-14 02:38:19 UTC
- **Paper workflow started:** 2026-11-14 02:38:12 UTC
- **Latest saved market observation:** 2026-11-14 02:38:17 UTC
- **Workflow trigger:** **workflow_dispatch / paper**
- **GitHub Actions run:** [43547394129](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/actions/runs/43547394129)

## Interpretation notes

- NAV includes cash and mark-to-market unrealized P&L. Open-leg unrealized P&L is gross; an estimated exit fee has not yet been charged.
- Pending funding is an estimate and is not posted to the accounting ledger until the next successful rebalance.
- Without a current-pair monitor observation, marks fall back to the entry-time snapshot and are not live quotes.
- MDD is identified as unavailable until supported by minute-level observations; incomplete coverage is shown explicitly.
- This page is derived from the durable SQLite ledger after a successful paper run. The database remains the accounting source of truth.
