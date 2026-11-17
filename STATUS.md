# Bitget Stocks vs Crypto — Paper Portfolio Status

> **Paper-only simulation.** No exchange orders are placed. Market data comes from Bitget public endpoints.

## At a glance

| Metric | Latest value |
|---|---:|
| Current NAV | **9.51 USDT** |
| Starting capital | 10.00 USDT |
| NAV change vs starting capital | **-0.49 USDT** |
| Portfolio ROI vs starting capital | **-4.92%** |
| Current-cycle ROI | 15.31% |
| Unrealized P&L (gross) | 1.23 USDT |
| Cumulative realized gross P&L | -1.25 USDT |
| Fees paid to date | 0.08 USDT |
| Funding posted to ledger | -0.40 USDT |
| Estimated funding pending | 0.00 USDT |
| Gross exposure | 16.93 USDT |
| Closed trades | 6 |

_NAV source: live mark-to-market observation. Values as of 2026-11-17 19:38:18 UTC._

## Latest workflow result

- **Paper runner status:** **MONITORING_ONLY**
- **Ranking data gate:** PASS
- **Monitoring operation:** MONITORING
- **Age of latest saved mark/snapshot:** 0 minutes

## Lifetime strategy performance

- **Initial capital:** 10.00 USDT
- **Current equity P&L vs initial capital:** **-0.49 USDT**
- **Lifetime portfolio ROI:** **-4.92%**
- **Equity-curve observations:** 47
- **Closed trades:** 6 | **Open trades:** 2
- **Realized net P&L on closed trades:** -1.72 USDT
- **Closed-trade win rate:** 33.33%
- **Profit factor:** 0.1800930733974301186090714112
- **Annualized Sortino ratio:** **Not yet statistically available**
- **Sortino status:** INSUFFICIENT_HISTORY: 3/20 daily returns
- **Sortino method:** consecutive UTC daily equity closes, 0% minimum acceptable return, annualized by √365; requires at least 20 valid daily returns and 5 negative days.
- **Sampled equity-curve MDD:** -25.15%

![Portfolio equity curve](equity_curve.svg)
[Complete equity curve CSV](equity_curve.csv) · [Daily equity and returns](daily_equity.csv) · [Complete trade history](trade_history.csv)

### Full trade history

| Trade | Entry date | Leg | Contract | Status | Entry price | Exit price | Net P&L | Trade ROI |
|---:|---|---|---|---|---:|---:|---:|---:|
| 1 | 2026-11-14 | LONG | SOXSUSDT | CLOSED | 52.82500000 | 53.49900000 | 0.11 USDT | 1.14% |
| 2 | 2026-11-14 | SHORT | BANANAS31USDT | CLOSED | 0.00391800 | 0.00434400 | -0.86 USDT | -10.97% |
| 3 | 2026-11-15 | LONG | SKDDUSDT | CLOSED | 10.64400000 | 10.94700000 | 0.27 USDT | 2.94% |
| 4 | 2026-11-15 | SHORT | KAVAUSDT | CLOSED | 0.02938000 | 0.02945000 | -0.31 USDT | -3.41% |
| 5 | 2026-11-16 | LONG | DXYZUSDT | CLOSED | 27.72300000 | 26.60642424 | -0.38 USDT | -4.16% |
| 6 | 2026-11-16 | SHORT | SOPHUSDT | CLOSED | 0.00090900 | 0.00094800 | -0.54 USDT | -5.90% |
| 7 | 2026-11-17 | LONG | UVXYUSDT | OPEN | 20.38900000 | — | — USDT | Not available |
| 8 | 2026-11-17 | SHORT | JCTUSDT | OPEN | 0.00214100 | — | — USDT | Not available |

Sortino remains unavailable until enough daily observations exist; no ratio is inferred from a short initial sample.


## Open positions

| Leg | Contract | Quantity | Entry | Latest mark | Unrealized P&L | Trade ROI |
|---|---|---:|---:|---:|---:|---:|
| LONG | UVXYUSDT | 0.400000 | 20.38900000 | 22.55500000 | 0.87 USDT | 10.50% |
| SHORT | JCTUSDT | 3,863.000000 | 0.00214100 | 0.00204600 | 0.37 USDT | 4.36% |

## Risk

- **Strategy maximum drawdown:** -38.45% (observed minute history)
- **Current holding-cycle MDD:** -11.85%
- **Minute-data coverage:** 504/504 (100.00%)
- **Risk-data quality:** **COMPLETE_CURRENT_CYCLE**
- **Historical risk intervals:** 3 complete / 0 incomplete
- **Latest completed risk interval:** COMPLETE

## Cooldown safeguard

- **Cooldown flag:** **ACTIVE (1)**
- Activated: 2026-11-17 11:12:25 UTC
- Scheduled reset: 2026-11-18 11:13:00 UTC
- Current cycle start: 2026-11-17 11:12:25 UTC
- A new pair is built only when the binary flag is 0 and the ranking/depth/accounting gates pass.

## Timestamps and provenance

- **STATUS.md generated:** 2026-11-17 19:38:22 UTC
- **Paper workflow started:** 2026-11-17 19:37:16 UTC
- **Latest saved market observation:** 2026-11-17 19:38:18 UTC
- **Workflow trigger:** **workflow_dispatch / paper**
- **GitHub Actions run:** [44160346425](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/actions/runs/44160346425)

## Interpretation notes

- NAV includes cash and mark-to-market unrealized P&L. Open-leg unrealized P&L is gross; an estimated exit fee has not yet been charged.
- Pending funding is an estimate and is not posted to the accounting ledger until the next successful rebalance.
- Without a current-pair monitor observation, marks fall back to the entry-time snapshot and are not live quotes.
- MDD is identified as unavailable until supported by minute-level observations; incomplete coverage is shown explicitly.
- This page is derived from the durable SQLite ledger after a successful paper run. The database remains the accounting source of truth.
