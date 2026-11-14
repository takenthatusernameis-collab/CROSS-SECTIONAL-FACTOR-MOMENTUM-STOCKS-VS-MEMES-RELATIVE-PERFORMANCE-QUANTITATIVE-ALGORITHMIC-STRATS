# Bitget Stocks vs Crypto — Paper Portfolio Status

> **Paper-only simulation.** No exchange orders are placed. Market data is sourced from Bitget public endpoints.

## Reset baseline

<badge>New campaign</badge>

The previous paper account, open-position records, trade ledger and performance history have been cleared from the active state branch. The new campaign starts with **10 USDT** of starting capital.

| Metric | Reset baseline |
|---|---:|
| Starting capital | **10.00 USDT** |
| Current NAV before first pair | 10.00 USDT |
| Cumulative P&L | 0.00 USDT |
| Portfolio ROI | 0.00% |
| Open positions | 0 |
| Closed trades | 0 |
| Recorded equity points | 1 (initial-capital baseline) |
| Realized trade P&L | 0.00 USDT |
| Fees | 0.00 USDT |
| Funding | 0.00 USDT |

This is the starting-balance baseline, not a live mark or a completed strategy observation. The first successful scheduled or manual paper run will open the new pair and replace this page with live metrics.

## Lifetime strategy performance

- **Annualized Sortino ratio:** Not yet available.
- **Sortino observations:** 0 daily close-to-close returns; requires at least 20 daily returns and 5 negative days.
- **Minimum acceptable return:** 0% per day.
- **Annualization:** √365.
- **Maximum drawdown:** Not yet established.
- **Trade history:** Empty by design; all new open and closed trades will be retained from this reset onward.

![Portfolio equity curve](equity_curve.svg)

[Complete equity curve CSV](equity_curve.csv) · [Daily equity and returns](daily_equity.csv) · [Complete trade history](trade_history.csv)

## Open positions

No open positions yet.

## Cooldown safeguard

- **Cooldown flag:** READY (0).
- The first successful paper run will construct the opening long/short pair using Bitget's latest ranking and public depth checks.
- On successful construction, the 24-hour binary cooldown will activate.

## Reset and provenance

- **Reset timestamp:** 2026-11-14T02:21:47.080Z
- **Starting capital:** 10 USDT.
- **Next action:** the first successful scheduled or manual `mode=paper` run will initialize the SQLite ledger, open positions, and begin equity tracking.
- **Trading boundary:** simulation only; no exchange orders or account endpoints.

The durable database will be created on the first successful paper run. The `RESET_PENDING.md` marker is temporary and is removed when the new database and reports are committed together.
