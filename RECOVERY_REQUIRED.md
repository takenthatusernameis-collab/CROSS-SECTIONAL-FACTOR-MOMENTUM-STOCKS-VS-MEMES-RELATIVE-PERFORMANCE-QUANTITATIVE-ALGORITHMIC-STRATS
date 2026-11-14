# Paper portfolio recovery required

The clean-reset bootstrap must not run again. A manual paper run successfully initialized the $10 portfolio and opened two simulated positions, but the durable-state push failed during STATUS.md rendering.

Exact recovery source:
- GitHub Actions run: 43546451119
- Artifact ID: 14094526343
- Artifact name: bitget-stocks-vs-crypto-43546451119
- Database path in artifact: state/bitget_paper.sqlite3
- Expected initial NAV: 10 USDT
- Expected open trades: 2 (LONG SOXSUSDT, SHORT BANANAS31USDT)
- Expected cooldown: 1, scheduled reset 2026-11-15T02:28:00Z

Restore this exact database to this branch, verify SQLite integrity and expected account/positions/cooldown, render current status, and remove both this marker and RESET_PENDING.md only in the same successful persistence commit.

Until this marker is removed, scheduled and manual mode=paper cycles must fail closed before reading or modifying portfolio state. Do not create a fresh database or execute another paper cycle.
