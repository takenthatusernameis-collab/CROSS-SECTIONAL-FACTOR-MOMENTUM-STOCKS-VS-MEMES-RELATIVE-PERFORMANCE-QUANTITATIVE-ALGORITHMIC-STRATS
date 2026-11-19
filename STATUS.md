# Bitget Stocks vs Crypto — Paper Portfolio Status

> **Automatically synchronized main-branch snapshot.** The authoritative live dashboard and SQLite ledger continue to update on [the durable state branch](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/tree/bitget-paper-portfolio-state). Open the [latest live dashboard](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/blob/bitget-paper-portfolio-state/STATUS.md) for the newest marks.

> **Paper-only simulation.** No exchange orders are placed. Market data comes from Bitget public endpoints.

## At a glance

| Metric | Latest value |
|---|---:|
| Current NAV | **8.99 USDT** |
| Starting capital | 10.00 USDT |
| NAV change vs starting capital | **-1.01 USDT** |
| Portfolio ROI vs starting capital | **-10.10%** |
| Current-cycle ROI | Not available |
| Unrealized P&L (gross) | -0.03 USDT |
| Cumulative realized gross P&L | -0.25 USDT |
| Fees paid to date | 0.12 USDT |
| Funding posted to ledger | -0.62 USDT |
| Estimated funding pending | 0.00 USDT |
| Gross exposure | 18.03 USDT |
| Closed trades | 10 |

_NAV source: entry-time portfolio snapshot (awaiting first monitor mark). Values as of 2026-11-19 17:42:19 UTC._

## Latest workflow result

- **Paper runner status:** **PAPER_UPDATED**
- **Ranking data gate:** PASS
- **Monitoring operation:** MONITORING
- **Age of latest saved mark/snapshot:** 0 minutes

## Lifetime strategy performance

- **Initial capital:** 10.00 USDT
- **Current equity P&L vs initial capital:** **-1.01 USDT**
- **Lifetime portfolio ROI:** **-10.10%**
- **Equity-curve observations:** 78
- **Closed trades:** 10 | **Open trades:** 2
- **Realized net P&L on closed trades:** -0.97 USDT
- **Closed-trade win rate:** 50.00%
- **Profit factor:** 0.6880868429643478300464051078
- **Annualized Sortino ratio:** **Not yet statistically available**
- **Sortino status:** INSUFFICIENT_HISTORY: 5/20 daily returns
- **Sortino method:** consecutive UTC daily equity closes, 0% minimum acceptable return, annualized by √365; requires at least 20 valid daily returns and 5 negative days.
- **Sampled equity-curve MDD:** -25.15%

![Portfolio equity curve](https://raw.githubusercontent.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/bitget-paper-portfolio-state/equity_curve.svg)
[Complete equity curve CSV](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/blob/bitget-paper-portfolio-state/equity_curve.csv) · [Daily equity and returns](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/blob/bitget-paper-portfolio-state/daily_equity.csv) · [Complete trade history](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/blob/bitget-paper-portfolio-state/trade_history.csv)

### Full trade history

| Trade | Entry date | Leg | Contract | Status | Entry price | Exit price | Net P&L | Trade ROI |
|---:|---|---|---|---|---:|---:|---:|---:|
| 1 | 2026-11-14 | LONG | SOXSUSDT | CLOSED | 52.82500000 | 53.49900000 | 0.11 USDT | 1.14% |
| 2 | 2026-11-14 | SHORT | BANANAS31USDT | CLOSED | 0.00391800 | 0.00434400 | -0.86 USDT | -10.97% |
| 3 | 2026-11-15 | LONG | SKDDUSDT | CLOSED | 10.64400000 | 10.94700000 | 0.27 USDT | 2.94% |
| 4 | 2026-11-15 | SHORT | KAVAUSDT | CLOSED | 0.02938000 | 0.02945000 | -0.31 USDT | -3.41% |
| 5 | 2026-11-16 | LONG | DXYZUSDT | CLOSED | 27.72300000 | 26.60642424 | -0.38 USDT | -4.16% |
| 6 | 2026-11-16 | SHORT | SOPHUSDT | CLOSED | 0.00090900 | 0.00094800 | -0.54 USDT | -5.90% |
| 7 | 2026-11-17 | LONG | UVXYUSDT | CLOSED | 20.38900000 | 21.66700000 | 0.54 USDT | 6.64% |
| 8 | 2026-11-17 | SHORT | JCTUSDT | CLOSED | 0.00214100 | 0.00203200 | 0.42 USDT | 5.10% |
| 9 | 2026-11-18 | LONG | SHAZUSDT | CLOSED | 24.25000000 | 21.62000000 | -1.02 USDT | -11.11% |
| 10 | 2026-11-18 | SHORT | BREVUSDT | CLOSED | 0.08598000 | 0.07592000 | 0.81 USDT | 8.76% |
| 11 | 2026-11-19 | LONG | BITOUSDT | OPEN | 16.65200000 | — | — USDT | Not available |
| 12 | 2026-11-19 | SHORT | SKYUSDT | OPEN | 0.04017000 | — | — USDT | Not available |

Sortino remains unavailable until enough daily observations exist; no ratio is inferred from a short initial sample.


## Open positions

| Leg | Contract | Quantity | Entry | Latest mark | Unrealized P&L | Trade ROI |
|---|---|---:|---:|---:|---:|---:|
| LONG | BITOUSDT | 0.541000 | 16.65200000 | 16.64500000 | -0.00 USDT | -0.10% |
| SHORT | SKYUSDT | 224.000000 | 0.04017000 | 0.04027000 | -0.02 USDT | -0.31% |

## Risk

- **Strategy maximum drawdown:** -38.45% (observed minute history)
- **Current holding-cycle MDD:** Not available (not established yet)
- **Minute-data coverage:** not started; awaiting first completed one-minute interval
- **Risk-data quality:** **NO_MINUTE_HISTORY**
- **Historical risk intervals:** 5 complete / 0 incomplete
- **Latest completed risk interval:** COMPLETE

## Cooldown safeguard

- **Cooldown flag:** **ACTIVE (1)**
- Activated: 2026-11-19 17:41:04 UTC
- Scheduled reset: 2026-11-20 17:42:00 UTC
- Current cycle start: 2026-11-19 17:41:04 UTC
- A new pair is built only when the binary flag is 0 and the ranking/depth/accounting gates pass.

## Timestamps and provenance

- **STATUS.md generated:** 2026-11-19 17:42:39 UTC
- **Paper workflow started:** 2026-11-19 17:41:04 UTC
- **Latest saved market observation:** 2026-11-19 17:42:19 UTC
- **Workflow trigger:** **schedule / paper**
- **GitHub Actions run:** [44579782876](https://github.com/takenthatusernameis-collab/CROSS-SECTIONAL-FACTOR-MOMENTUM-STOCKS-VS-MEMES-RELATIVE-PERFORMANCE-QUANTITATIVE-ALGORITHMIC-STRATS/actions/runs/44579782876)

## Interpretation notes

- NAV includes cash and mark-to-market unrealized P&L. Open-leg unrealized P&L is gross; an estimated exit fee has not yet been charged.
- Pending funding is an estimate and is not posted to the accounting ledger until the next successful rebalance.
- Without a current-pair monitor observation, marks fall back to the entry-time snapshot and are not live quotes.
- MDD is identified as unavailable until supported by minute-level observations; incomplete coverage is shown explicitly.
- This page is derived from the durable SQLite ledger after a successful paper run. The database remains the accounting source of truth.
