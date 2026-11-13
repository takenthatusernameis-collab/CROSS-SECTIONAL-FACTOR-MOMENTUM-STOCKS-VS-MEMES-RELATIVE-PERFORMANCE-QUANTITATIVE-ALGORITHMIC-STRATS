# Live Gate category preflight attempt

- Attempt date: 2026-11-13 (UTC)
- Mode: public read-only REST GET; no credentials, testnet, or order placement
- Contract endpoint: `https://api.gateio.ws/api/v4/futures/usdt/contracts`
- Ticker endpoint: `https://api.gateio.ws/api/v4/futures/usdt/tickers`
- Result: **FAIL — inconclusive due to local DNS/network failure**
- Error: `Temporary failure in name resolution` while resolving `api.gateio.ws`
- Category values observed from live exchange data: none (no response body received)
- Eligible Stocks contracts confirmed: not established
- Eligible Memes contracts confirmed: not established
- Paper trading authorized: **NO**

This failure does not show that Gate lacks either category. It means the current execution environment could not retrieve the exchange feed. The one-shot runner was also attempted and exited with code 1 before opening a paper session. The current runner implementation is wired to block all new paper exposure until a later live preflight returns PASS.

Gate's current API documentation describes `contract_type` as a contract classification and gives examples including `stocks`, metals, indices, forex, and commodities. It does not provide a complete enumeration of possible values and does not mention a `meme` value. Gate's Futures Meme Zone announcement confirms that the product category exists, but does not establish the contract metadata string. Therefore, only live contract-feed output can confirm the required pair of categories.

## Reproduction

From the project root on a machine with working DNS and HTTPS access:

```bash
svm-paper preflight
```

Exit code 0 and JSON `"passed": true` are required before starting a new paper session. Exit code 1 keeps paper trading blocked.
