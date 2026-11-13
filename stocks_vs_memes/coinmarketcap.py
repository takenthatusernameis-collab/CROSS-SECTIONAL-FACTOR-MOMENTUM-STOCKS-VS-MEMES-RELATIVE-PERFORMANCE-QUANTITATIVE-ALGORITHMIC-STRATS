"""CoinMarketCap keyless public category feed (taxonomy only; no trading prices)."""
from __future__ import annotations
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from typing import Any

class CMCAPIError(RuntimeError):
    pass

class CoinMarketCapClient:
    def __init__(self, base_url: str = "https://pro-api.coinmarketcap.com/public-api", timeout: int = 20, max_retries: int = 2):
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
                if not isinstance(payload, dict) or "data" not in payload:
                    raise CMCAPIError(f"Unexpected CoinMarketCap payload for {path}")
                status = payload.get("status") or {}
                if str(status.get("error_code", 0)) not in ("0", "None"):
                    raise CMCAPIError(f"CoinMarketCap error {status.get('error_code')}: {status.get('error_message')}")
                return payload
            except HTTPError as exc:
                last = exc
                body = exc.read().decode("utf-8", "replace")[:300]
                if exc.code not in (429, 500, 502, 503, 504) or attempt >= self.max_retries:
                    raise CMCAPIError(f"CoinMarketCap HTTP {exc.code} on {path}: {body}") from exc
                time.sleep(min(2 ** attempt, 5))
            except (URLError, TimeoutError, json.JSONDecodeError) as exc:
                last = exc
                if attempt >= self.max_retries:
                    raise CMCAPIError(f"CoinMarketCap request failed for {path}: {exc}") from exc
                time.sleep(min(2 ** attempt, 5))
        raise CMCAPIError(f"CoinMarketCap request failed: {last}")

    def categories(self) -> list[dict[str, Any]]:
        payload = self.get("/v1/cryptocurrency/categories", {"start": 1, "limit": 5000})
        data = payload.get("data")
        if not isinstance(data, list):
            raise CMCAPIError("CoinMarketCap categories endpoint did not return a data array")
        return data

    def find_exact_category(self, exact_name: str) -> dict[str, Any]:
        categories = self.categories()
        matches = [c for c in categories if str(c.get("name", "")).strip().casefold() == exact_name.casefold()
                   or str(c.get("title", "")).strip().casefold() == exact_name.casefold()]
        if len(matches) != 1:
            names = sorted({str(c.get("name") or c.get("title") or "") for c in categories})
            raise CMCAPIError(f"Expected exactly one CoinMarketCap category named {exact_name!r}; found {len(matches)}. "
                              f"Observed possible category names: {[n for n in names if 'meme' in n.casefold()][:20]}")
        return matches[0]

    def category_members(self, category_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        all_coins: list[dict[str, Any]] = []
        start = 1
        category_meta: dict[str, Any] = {}
        while True:
            payload = self.get("/v1/cryptocurrency/category", {"id": category_id, "start": start, "limit": 1000})
            data = payload.get("data")
            if not isinstance(data, dict):
                raise CMCAPIError("CoinMarketCap category endpoint did not return a category object")
            category_meta = {k: data.get(k) for k in ("id", "name", "title", "last_updated", "num_tokens") if k in data}
            coins = data.get("coins") or []
            if not isinstance(coins, list):
                raise CMCAPIError("CoinMarketCap category coins field is not an array")
            all_coins.extend(coins)
            # Category responses expose num_tokens. Do not default to the current
            # page length when status.total_count is absent: that truncates page 1.
            total_raw = data.get("num_tokens")
            if total_raw is None:
                total_raw = (payload.get("status") or {}).get("total_count")
            total = int(total_raw) if total_raw is not None else None
            if len(coins) < 1000 or (total is not None and len(all_coins) >= total):
                break
            start += len(coins)
        if not all_coins:
            raise CMCAPIError(f"CoinMarketCap category {category_id} returned no member coins")
        return all_coins, category_meta

    def cryptocurrency_map_for_symbols(self, symbols: list[str]) -> list[dict[str, Any]]:
        unique = sorted({str(x).strip().upper() for x in symbols if str(x).strip()})
        rows: list[dict[str, Any]] = []
        for offset in range(0, len(unique), 100):
            chunk = unique[offset:offset + 100]
            # CMC documents symbol-filtered ID map overrides other query options.
            # Use bounded batches and preserve all returned IDs for ambiguity checks.
            payload = self.get("/v1/cryptocurrency/map", {"symbol": ",".join(chunk), "aux": "platform",
                                                           "listing_status": "active", "limit": 5000})
            data = payload.get("data")
            if not isinstance(data, list):
                raise CMCAPIError("CoinMarketCap ID map did not return a data array")
            rows.extend(data)
        return rows
