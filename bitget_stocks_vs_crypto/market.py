"""Bitget public-market-data discovery and daily Stocks-vs-Crypto ranking."""
from __future__ import annotations

import json
import os
import platform
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

BASE_URL = "https://api.bitget.com"
CATEGORY = "USDT-FUTURES"
MAX_DATA_AGE_MS = 900_000
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
ALLOWED_PATHS = frozenset({
    "/api/v3/market/time",
    "/api/v3/market/instruments",
    "/api/v3/market/tickers",
    "/api/v2/mix/market/merge-depth",
    "/api/v2/mix/market/history-fund-rate",
    "/api/v2/mix/market/history-mark-candles",
})


class MarketDataError(RuntimeError):
    def __init__(self, stage: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.stage = stage
        self.http_status = http_status


def decimal_value(value: Any, name: str = "value", *, positive: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise MarketDataError("validation", f"{name} is missing")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MarketDataError("validation", f"{name} is not numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise MarketDataError("validation", f"{name} is invalid")
    return result


def get_json(path: str, params: dict[str, str] | None = None, *, stage: str = "api") -> tuple[dict[str, Any], dict[str, Any]]:
    """Unauthenticated GET to a fixed allowlist of public Bitget endpoints."""
    if path not in ALLOWED_PATHS:
        raise MarketDataError(stage, f"Endpoint {path!r} is outside the fixed public GET allowlist")
    query = urllib.parse.urlencode(params or {})
    url = BASE_URL + path + ("?" + query if query else "")
    last_error: Exception | None = None
    for attempt in range(3):
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "Accept-Encoding": "identity",
                     "User-Agent": "bitget-stocks-vs-crypto-paper/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise MarketDataError(stage, "Response exceeded the 24 MB safety limit")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise MarketDataError(stage, "API response root is not a JSON object")
                if response.status != 200:
                    raise MarketDataError(stage, f"Unexpected HTTP status {response.status}", int(response.status))
                if payload.get("code") != "00000":
                    raise MarketDataError(stage, f"Bitget API error {payload.get('code')}: {payload.get('msg')}")
                return payload, {
                    "http_status": int(response.status),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "response_bytes": len(raw),
                    "request_time_ms": payload.get("requestTime"),
                    "url": url,
                }
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            last_error = MarketDataError(stage, f"HTTP {exc.code}: {detail[:800]}", int(exc.code))
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise last_error from exc
            time.sleep(0.75 * (attempt + 1))
        except MarketDataError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = MarketDataError(stage, f"{type(exc).__name__}: {exc}")
            if attempt == 2:
                raise last_error from exc
            time.sleep(0.5 * (attempt + 1))
    raise last_error or MarketDataError(stage, "Request failed without a response")


def _data_rows(payload: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows or any(not isinstance(x, dict) for x in rows):
        raise MarketDataError(stage, "Expected a non-empty data array of objects")
    return rows


def classify_instruments(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Use Bitget's native symbolType taxonomy; never infer category from names."""
    seen: set[str] = set()
    stocks: list[dict[str, Any]] = []
    crypto: list[dict[str, Any]] = []
    excluded: dict[str, int] = {}
    for item in rows:
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise MarketDataError("instrument_validation", "Instrument is missing a valid symbol")
        if symbol in seen:
            raise MarketDataError("instrument_validation", f"Duplicate instrument symbol {symbol}")
        seen.add(symbol)
        category = str(item.get("category", "")).upper()
        quote = str(item.get("quoteCoin", "")).upper()
        status = str(item.get("status", "")).casefold()
        contract_type = str(item.get("type", "")).casefold()
        symbol_type = str(item.get("symbolType", "")).casefold()
        if category != CATEGORY or quote != "USDT":
            reason = "not_usdt_futures"
        elif status != "online":
            reason = "not_online_or_open_restricted"
        elif contract_type != "perpetual":
            reason = "not_perpetual"
        elif symbol_type == "stock":
            stocks.append(item)
            continue
        elif symbol_type == "crypto":
            crypto.append(item)
            continue
        else:
            reason = "outside_stock_crypto_taxonomy"
        excluded[reason] = excluded.get(reason, 0) + 1
    if not stocks:
        raise MarketDataError("instrument_validation", "No active USDT stock perpetuals matched the exact Bitget taxonomy")
    if not crypto:
        raise MarketDataError("instrument_validation", "No active USDT crypto perpetuals matched the exact Bitget taxonomy")
    return stocks, crypto, excluded


def build_ranking_report() -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "report_version": 1,
        "strategy": "Bitget Stocks vs Crypto — daily cross-sectional paper strategy",
        "run_at_utc": started.isoformat().replace("+00:00", "Z"),
        "runner": {"os": platform.platform(), "python": platform.python_version(),
                   "github_run_id": os.getenv("GITHUB_RUN_ID"), "github_sha": os.getenv("GITHUB_SHA"),
                   "github_ref": os.getenv("GITHUB_REF")},
        "api": {"base_url": BASE_URL, "authentication": "none", "methods": ["GET"],
                "category": CATEGORY, "ranking_field": "price24hPcnt",
                "price24hPcnt_interpretation": "decimal fraction; multiplied by 100 for percentage display",
                "ranking_return_recalculated": False},
        "universe": {}, "ticker_coverage": {}, "market_results": [], "leaders": {},
        "gates": {"public_api": "NOT_EVALUATED", "universe_discovery": "NOT_EVALUATED",
                  "ticker_coverage": "NOT_EVALUATED", "ranking": "NOT_EVALUATED"},
        "safety": {"public_get_only": True, "no_credentials": True,
                   "no_private_account_endpoints": True, "no_order_endpoints": True,
                   "no_orders_submitted": True, "no_portfolio_state_changed_by_ranking": True,
                   "no_symbol_name_category_inference": True},
    }
    stage = "server_time"
    try:
        time_payload, time_meta = get_json("/api/v3/market/time", stage=stage)
        time_data = time_payload.get("data")
        if isinstance(time_data, dict):
            raw_time = time_data.get("serverTime", time_data.get("ts"))
        else:
            raw_time = time_data
        if raw_time is None:
            raw_time = time_payload.get("requestTime")
        server_ms_dec = decimal_value(raw_time, "Bitget server time", positive=True)
        if server_ms_dec != server_ms_dec.to_integral_value():
            raise MarketDataError(stage, "Bitget server timestamp is not an integer")
        server_ms = int(server_ms_dec)
        report["api"]["server_time_http_status"] = time_meta["http_status"]
        report["api"]["server_time_ms"] = server_ms

        stage = "instrument_discovery"
        instruments_payload, instruments_meta = get_json(
            "/api/v3/market/instruments", {"category": CATEGORY}, stage=stage
        )
        instruments = _data_rows(instruments_payload, stage)
        stocks, crypto, excluded = classify_instruments(instruments)
        candidates = stocks + crypto
        report["api"]["instrument_http_status"] = instruments_meta["http_status"]
        report["api"]["instrument_request_seconds"] = instruments_meta["elapsed_seconds"]
        report["universe"] = {
            "instrument_count": len(instruments), "stock_perpetual_count": len(stocks),
            "crypto_perpetual_count": len(crypto), "eligible_total": len(candidates),
            "excluded_reason_counts": excluded,
            "stock_definition": "category=USDT-FUTURES, quoteCoin=USDT, status=online, type=perpetual, symbolType=stock",
            "crypto_definition": "category=USDT-FUTURES, quoteCoin=USDT, status=online, type=perpetual, symbolType=crypto",
        }
        report["gates"]["public_api"] = "PASS"
        report["gates"]["universe_discovery"] = "PASS"

        stage = "ticker_discovery"
        ticker_payload, ticker_meta = get_json(
            "/api/v3/market/tickers", {"category": CATEGORY}, stage=stage
        )
        tickers = _data_rows(ticker_payload, stage)
        ticker_by_symbol: dict[str, dict[str, Any]] = {}
        for ticker in tickers:
            symbol = ticker.get("symbol")
            if not isinstance(symbol, str) or not symbol:
                raise MarketDataError(stage, "Ticker row is missing symbol")
            if symbol in ticker_by_symbol:
                raise MarketDataError(stage, f"Duplicate ticker row for {symbol}")
            ticker_by_symbol[symbol] = ticker

        stage = "ticker_validation"
        markets: list[dict[str, Any]] = []
        missing: list[str] = []
        invalid: list[dict[str, str]] = []
        for instrument in candidates:
            symbol = instrument["symbol"]
            ticker = ticker_by_symbol.get(symbol)
            if ticker is None:
                missing.append(symbol)
                continue
            reasons: list[str] = []
            if str(ticker.get("category", "")).upper() != CATEGORY:
                reasons.append("ticker_category_mismatch")
            change = decimal_value(ticker.get("price24hPcnt"), "price24hPcnt") if ticker.get("price24hPcnt") is not None else None
            last = decimal_value(ticker.get("lastPrice"), "lastPrice", positive=True) if ticker.get("lastPrice") is not None else None
            bid = decimal_value(ticker.get("bid1Price"), "bid1Price", positive=True) if ticker.get("bid1Price") is not None else None
            ask = decimal_value(ticker.get("ask1Price"), "ask1Price", positive=True) if ticker.get("ask1Price") is not None else None
            mark = decimal_value(ticker.get("markPrice"), "markPrice", positive=True) if ticker.get("markPrice") is not None else None
            stamp = decimal_value(ticker.get("ts"), "ticker ts", positive=True) if ticker.get("ts") is not None else None
            if change is None: reasons.append("invalid_price24hPcnt")
            if last is None: reasons.append("invalid_lastPrice")
            if bid is None: reasons.append("invalid_bid1Price")
            if ask is None or (bid is not None and ask < bid): reasons.append("invalid_ask1Price_or_crossed_quote")
            if mark is None: reasons.append("invalid_markPrice")
            if stamp is None or stamp != stamp.to_integral_value():
                reasons.append("invalid_ticker_timestamp")
            elif abs(server_ms - int(stamp)) > MAX_DATA_AGE_MS:
                reasons.append("stale_ticker_timestamp")
            if reasons:
                invalid.append({"symbol": symbol, "reason": ";".join(reasons)})
                continue
            row = dict(instrument)
            row.update({
                "price24hPcnt": str(change), "return_24h_pct": str(change * Decimal("100")),
                "lastPrice": str(last), "bid1Price": str(bid), "ask1Price": str(ask),
                "markPrice": str(mark), "turnover24h": ticker.get("turnover24h"),
                "volume24h": ticker.get("volume24h"), "ticker_ts": int(stamp),
                "ticker_age_seconds": round((server_ms - int(stamp)) / 1000, 3),
            })
            markets.append(row)
        if missing or invalid or len(markets) != len(candidates):
            raise MarketDataError(stage,
                f"Ticker coverage incomplete: missing={len(missing)}, invalid={len(invalid)}; "
                f"missing_examples={missing[:8]}, invalid_examples={invalid[:8]}")
        report["api"]["ticker_http_status"] = ticker_meta["http_status"]
        report["api"]["ticker_request_seconds"] = ticker_meta["elapsed_seconds"]
        report["ticker_coverage"] = {"ticker_rows": len(tickers), "eligible_instruments": len(candidates),
            "validated_rows": len(markets), "coverage_pct": "100.0",
            "max_ticker_age_seconds": MAX_DATA_AGE_MS / 1000}
        report["gates"]["ticker_coverage"] = "PASS"

        stocks_by_symbol = {x["symbol"] for x in stocks}
        crypto_by_symbol = {x["symbol"] for x in crypto}
        stock_markets = [x for x in markets if x["symbol"] in stocks_by_symbol]
        crypto_markets = [x for x in markets if x["symbol"] in crypto_by_symbol]
        stock_ranked = sorted(stock_markets, key=lambda x: (Decimal(x["price24hPcnt"]).__neg__(), x["symbol"]))
        crypto_ranked = sorted(crypto_markets, key=lambda x: (Decimal(x["price24hPcnt"]), x["symbol"]))
        long_leader, short_leader = stock_ranked[0], crypto_ranked[0]
        report["as_of_server_time_ms"] = server_ms
        report["market_results"] = markets
        report["leaders"] = {
            "long_stock_top_gainer": {k: long_leader.get(k) for k in
                ("symbol", "baseCoin", "symbolType", "price24hPcnt", "return_24h_pct", "lastPrice", "markPrice", "ticker_ts")},
            "short_crypto_bottom_loser": {k: short_leader.get(k) for k in
                ("symbol", "baseCoin", "symbolType", "price24hPcnt", "return_24h_pct", "lastPrice", "markPrice", "ticker_ts")},
        }
        report["ranking_samples"] = {
            "top_10_stocks": [{"symbol": x["symbol"], "return_24h_pct": x["return_24h_pct"]} for x in stock_ranked[:10]],
            "bottom_10_crypto": [{"symbol": x["symbol"], "return_24h_pct": x["return_24h_pct"]} for x in crypto_ranked[:10]],
        }
        report["gates"]["ranking"] = "PASS"
        report["status"] = "RANKING_PASS"
    except Exception as exc:
        status = getattr(exc, "http_status", None)
        report["status"] = "FAILED"
        report["failure"] = {"stage": getattr(exc, "stage", stage), "http_status": status,
                             "exception_type": type(exc).__name__, "message": str(exc)[:1600]}
        gate = {"server_time": "public_api", "instrument_discovery": "universe_discovery",
                "ticker_discovery": "ticker_coverage", "ticker_validation": "ticker_coverage"}.get(getattr(exc, "stage", stage))
        if gate:
            report["gates"][gate] = "FAIL"
    return report
