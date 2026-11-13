"""Read-only Bybit V5 public market-data client for linear USDT perpetuals."""
from __future__ import annotations
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from typing import Any

class BybitAPIError(RuntimeError):
    pass

class BybitClient:
    def __init__(self, base_url: str = "https://api.bybit.com", timeout: int = 15, max_retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.timeout = int(timeout)
        self.max_retries = int(max_retries)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = "?" + urlencode({k: v for k, v in (params or {}).items() if v is not None}) if params else ""
        url = f"{self.base_url}{path}{query}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "stocks-vs-memes-paper/0.3"}, method="GET")
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise BybitAPIError(f"Unexpected Bybit payload for {path}: expected object")
                if int(payload.get("retCode", -1)) != 0:
                    raise BybitAPIError(f"Bybit retCode={payload.get('retCode')}: {payload.get('retMsg')} ({path})")
                result = payload.get("result")
                if not isinstance(result, dict):
                    raise BybitAPIError(f"Unexpected Bybit result for {path}")
                return result
            except HTTPError as exc:
                last = exc
                body = exc.read().decode("utf-8", "replace")[:300]
                if exc.code not in (429, 500, 502, 503, 504) or attempt >= self.max_retries:
                    raise BybitAPIError(f"Bybit HTTP {exc.code} on {path}: {body}") from exc
                time.sleep(min(2 ** attempt, 5))
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                if attempt >= self.max_retries:
                    raise BybitAPIError(f"Bybit request failed for {path}: {exc}") from exc
                time.sleep(min(2 ** attempt, 5))
        raise BybitAPIError(f"Bybit request failed: {last}")

    def instruments(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            params = {"category": "linear", "limit": 1000, "cursor": cursor}
            result = self.get("/v5/market/instruments-info", params)
            page = result.get("list") or []
            if not isinstance(page, list):
                raise BybitAPIError("Bybit instruments-info result.list is not an array")
            rows.extend(page)
            next_cursor = result.get("nextPageCursor") or ""
            if not next_cursor:
                break
            if next_cursor in seen:
                raise BybitAPIError("Bybit instruments-info pagination cursor repeated")
            seen.add(next_cursor)
            cursor = next_cursor
        return rows

    def tickers(self) -> list[dict[str, Any]]:
        result = self.get("/v5/market/tickers", {"category": "linear"})
        rows = result.get("list") or []
        if not isinstance(rows, list):
            raise BybitAPIError("Bybit tickers result.list is not an array")
        return rows

    def order_book(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        result = self.get("/v5/market/orderbook", {"category": "linear", "symbol": symbol, "limit": limit})
        bids = result.get("b") or []
        asks = result.get("a") or []
        return {"bids": [{"p": x[0], "s": x[1]} for x in bids],
                "asks": [{"p": x[0], "s": x[1]} for x in asks], "timestamp_ms": result.get("ts")}

    def _klines(self, path: str, symbol: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        rows: dict[int, dict[str, Any]] = {}
        cursor_start = int(start_ts)
        page_span_ms = 999 * 60_000
        while cursor_start < int(end_ts):
            cursor_end = min(int(end_ts), cursor_start + page_span_ms)
            result = self.get(path, {"category": "linear", "symbol": symbol, "interval": "1",
                                     "start": cursor_start, "end": cursor_end, "limit": 1000})
            for raw in result.get("list") or []:
                if not isinstance(raw, list) or len(raw) < 5:
                    continue
                ts = int(raw[0]) // 1000
                rows[ts] = {"t": ts, "o": str(raw[1]), "h": str(raw[2]), "l": str(raw[3]), "c": str(raw[4])}
            if cursor_end >= int(end_ts):
                break
            cursor_start = cursor_end + 60_000
        return [rows[k] for k in sorted(rows)]

    def mark_candles(self, symbol: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        return self._klines("/v5/market/mark-price-kline", symbol, start_ts * 1000, end_ts * 1000)

    def funding_rates(self, symbol: str, start_ts: int, end_ts: int) -> list[dict[str, Any]]:
        result = self.get("/v5/market/funding/history", {"category": "linear", "symbol": symbol,
                                "startTime": int(start_ts) * 1000, "endTime": int(end_ts) * 1000, "limit": 200})
        rows = result.get("list") or []
        return sorted([{"t": int(x["fundingRateTimestamp"]) // 1000, "r": str(x["fundingRate"])}
                       for x in rows if x.get("fundingRateTimestamp") is not None and x.get("fundingRate") is not None],
                      key=lambda x: x["t"])
