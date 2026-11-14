# Authorized paper-ledger reset

This marker authorizes exactly one clean bootstrap of the Bitget paper SQLite database with an initial NAV of 10 USDT.

- Reset executed: 2026-11-14T02:21:47.080Z
- New starting capital: 10 USDT
- Initial cooldown state: `cooldown=0` (ready to open the first simulated pair).
- Previous active state database: intentionally removed
- Previous trade/position history: intentionally cleared from this state branch
- Paper execution remains unauthenticated public-market-data simulation only.

The first scheduled or manual `mode=paper` workflow must create the SQLite account with 10 USDT cash, explicitly set `cooldown=0`, verify there are no old positions/trades, and seed the 10 USDT equity baseline before it runs ranking and depth checks. After a successful opening rebalance the normal strategy logic will activate `cooldown=1` for its 24-hour holding period. The workflow must delete this marker from the state branch in the same commit that first persists the replacement SQLite database, STATUS.md, equity curve and complete trade-history exports. If this marker is absent and the database is missing, fail closed rather than silently resetting again.
