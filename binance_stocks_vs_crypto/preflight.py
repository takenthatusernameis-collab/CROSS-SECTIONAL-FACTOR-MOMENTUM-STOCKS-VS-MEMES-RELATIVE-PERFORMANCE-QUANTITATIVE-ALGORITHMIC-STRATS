"""Read-only diagnostic for Binance USD-M stock and crypto perpetuals."""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .rankings import classify_binance_universe, parse_binance_24h_tickers

BASE_URL = "https://fapi.binance.com/fapi/v1/"
USER_AGENT = "binance-stocks-vs-crypto-read-only-preflight/0.2"
MAX_BYTES = 24 * 1024 * 1024
RETRYABLE = {429, 500, 502, 503, 504}
ALLOWED_ROUTES = frozenset({
    "exchangeInfo", "time", "tradingSchedule", "ticker/24hr", "depth", "premiumIndex"
})


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def request_json(path: str, params: dict[str, str] | None = None,
                 timeout: float = 20.0, attempts: int = 3) -> tuple[Any, dict[str, Any]]:
    """Make an unauthenticated GET to an explicitly allowlisted public route."""
    if path not in ALLOWED_ROUTES:
        raise ValueError("Request path is outside the fixed public market-data route allowlist")
    query = urllib.parse.urlencode(params or {})
    url = BASE_URL + path + ("?" + query if query else "")
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": USER_AGENT,
    }, method="GET")
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise ApiError(f"Response exceeded {MAX_BYTES} bytes", int(response.status))
                if not 200 <= int(response.status) < 300:
                    raise ApiError(f"Unexpected HTTP status {response.status}", int(response.status))
                try:
                    data = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ApiError(f"Response was not valid UTF-8 JSON: {exc}", int(response.status)) from exc
                headers = {k.lower(): v for k, v in response.headers.items()}
                return data, {
                    "url": url,
                    "http_status": int(response.status),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "response_bytes": len(body),
                    "content_type": headers.get("content-type"),
                    "date_header": headers.get("date"),
                    "retry_after": headers.get("retry-after"),
                    "rate_limit_headers": {
                        k: v for k, v in headers.items()
                        if "rate" in k or "limit" in k or "request-id" in k
                    },
                }
        except urllib.error.HTTPError as exc:
            detail = exc.read(800).decode("utf-8", errors="replace")
            err = ApiError(f"HTTP {exc.code}: {detail[:800]}", int(exc.code))
            if exc.code not in RETRYABLE or attempt + 1 >= attempts:
                raise err from exc
            retry = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = min(8.0, max(0.5, float(retry))) if retry else .75 * (attempt + 1)
            except ValueError:
                delay = .75 * (attempt + 1)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            if attempt + 1 >= attempts:
                raise ApiError(f"Network error: {type(exc).__name__}: {exc}") from exc
            time.sleep(.5 * (attempt + 1))
    raise ApiError("Request failed after retries")


def call(name: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.monotonic()
    try:
        payload, meta = request_json(path, params)
        return {"name": name, "path": path, "status": "PASS", "error": None,
                "meta": meta, "payload": payload,
                "duration_seconds": round(time.monotonic() - started, 3)}
    except Exception as exc:
        return {"name": name, "path": path, "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
                "http_status": getattr(exc, "status", None),
                "duration_seconds": round(time.monotonic() - started, 3), "payload": None}


def summarise_24h_ticker(payload: Any, as_of_ms: int | None = None) -> dict[str, Any]:
    """Summarise one symbol-scoped ticker response for preflight validation."""
    if not isinstance(payload, dict):
        return {"schema_valid": False, "data_valid": False,
                "reason": "expected a single 24-hour ticker object"}
    required = ("symbol", "priceChangePercent", "lastPrice", "openPrice",
                "openTime", "closeTime", "count")
    if any(key not in payload for key in required):
        return {"schema_valid": False, "data_valid": False,
                "reason": "ticker response is missing required fields"}
    try:
        parsed = parse_binance_24h_tickers([payload], as_of_ms=as_of_ms)
    except ValueError as exc:
        return {"schema_valid": False, "data_valid": False, "reason": str(exc)}
    item = parsed[payload["symbol"]]
    return {
        "schema_valid": True,
        "data_valid": item["valid"],
        "reason": item["reason"],
        "symbol": item["symbol"],
        "price_change_percent": item["price_change_percent"],
        "last_price": item["last_price"],
        "open_price": item["open_price"],
        "trade_count": item["trade_count"],
        "ticker_window_open_time_ms": item["ticker_window_open_time_ms"],
        "ticker_window_close_time_ms": item["ticker_window_close_time_ms"],
        "ticker_window_age_seconds": item["ticker_window_age_seconds"],
    }


def summarise_orderbook(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"schema_valid": False, "reason": "expected an order-book object"}
    bids, asks = payload.get("bids"), payload.get("asks")
    return {"schema_valid": isinstance(bids, list) and isinstance(asks, list),
            "last_update_id": payload.get("lastUpdateId"),
            "bid_level_count": len(bids) if isinstance(bids, list) else None,
            "ask_level_count": len(asks) if isinstance(asks, list) else None,
            "sample_bid": bids[0] if isinstance(bids, list) and bids else None,
            "sample_ask": asks[0] if isinstance(asks, list) and asks else None}


def summarise_premium_index(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"schema_valid": False, "reason": "expected a premium-index object"}
    fields = ("symbol", "markPrice", "indexPrice", "lastFundingRate", "nextFundingTime")
    return {"schema_valid": all(key in payload for key in fields),
            "fields_present": [key for key in fields if key in payload],
            "symbol": payload.get("symbol"), "mark_price": payload.get("markPrice"),
            "index_price": payload.get("indexPrice"),
            "last_funding_rate": payload.get("lastFundingRate"),
            "next_funding_time_ms": payload.get("nextFundingTime")}


def run_preflight() -> dict[str, Any]:
    from datetime import datetime, timezone

    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "report_version": 2,
        "project": "binance_stocks_vs_crypto",
        "run_at_utc": started.isoformat().replace("+00:00", "Z"),
        "runner": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "github_runner_os": os.getenv("RUNNER_OS"),
            "github_run_id": os.getenv("GITHUB_RUN_ID"),
            "github_sha": os.getenv("GITHUB_SHA"),
            "github_ref": os.getenv("GITHUB_REF"),
        },
        "api": {
            "base_url": BASE_URL,
            "authentication": "none; public unauthenticated GET only",
            "methods_used": ["GET"],
            "provider": "Binance USD-M Futures only",
        },
        "market_universe": {},
        "endpoint_results": [],
        "sample_checks": [],
        "gates": {},
        "safety_assertions": {
            "public_get_only": True,
            "no_credentials": True,
            "no_private_account_endpoints": True,
            "no_order_or_trade_endpoints": True,
            "no_positions_or_portfolio_state": True,
            "no_external_price_or_taxonomy_provider": True,
        },
    }

    exchange = call("exchange_info", "exchangeInfo")
    exchange_payload = exchange.pop("payload")
    symbols = exchange_payload.get("symbols") if isinstance(exchange_payload, dict) else None
    if exchange["status"] == "PASS" and isinstance(symbols, list):
        try:
            universe = classify_binance_universe(symbols)
            long_types: dict[str, int] = {}
            short_types: dict[str, int] = {}
            for item in universe["long"]:
                key = str(item.get("underlyingType"))
                long_types[key] = long_types.get(key, 0) + 1
            for item in universe["short"]:
                key = str(item.get("underlyingType"))
                short_types[key] = short_types.get(key, 0) + 1
            excluded_by_reason: dict[str, int] = {}
            for item in universe["excluded"]:
                key = item["reason"]
                excluded_by_reason[key] = excluded_by_reason.get(key, 0) + 1
            report["market_universe"] = {
                "exchange_symbol_count": len(symbols),
                "long_stock_and_index_perps_count": len(universe["long"]),
                "short_crypto_perps_count": len(universe["short"]),
                "excluded_count": len(universe["excluded"]),
                "long_underlying_type_counts": dict(sorted(long_types.items())),
                "short_underlying_type_counts": dict(sorted(short_types.items())),
                "excluded_reason_counts": dict(sorted(excluded_by_reason.items())),
                "sample_long_symbols": [m["symbol"] for m in universe["long"][:5]],
                "sample_short_symbols": [m["symbol"] for m in universe["short"][:5]],
            }
        except Exception as exc:
            exchange["status"] = "FAIL_SCHEMA"
            exchange["error"] = f"{type(exc).__name__}: {exc}"
            universe = {"long": [], "short": [], "excluded": []}
    else:
        exchange["status"] = "FAIL_SCHEMA" if exchange["status"] == "PASS" else exchange["status"]
        if exchange.get("status") == "FAIL_SCHEMA":
            exchange["error"] = "Exchange information did not contain a symbols array"
        universe = {"long": [], "short": [], "excluded": []}
    exchange["payload_summary"] = {
        "response_keys": sorted(exchange_payload) if isinstance(exchange_payload, dict) else [],
        "symbol_count": len(symbols) if isinstance(symbols, list) else None,
    }
    report["endpoint_results"].append(exchange)

    server_time = call("server_time", "time")
    time_payload = server_time.pop("payload")
    server_time_ms = time_payload.get("serverTime") if isinstance(time_payload, dict) else None
    server_time_ok = isinstance(server_time_ms, int) and not isinstance(server_time_ms, bool)
    server_time["payload_summary"] = {
        "server_time_ms": server_time_ms if server_time_ok else None,
        "schema_valid": server_time_ok,
    }
    if server_time["status"] == "PASS" and not server_time_ok:
        server_time["status"] = "FAIL_SCHEMA"
        server_time["error"] = "Server-time response missing integer serverTime"
    report["endpoint_results"].append(server_time)

    schedule = call("underlying_trading_schedule", "tradingSchedule")
    schedule_payload = schedule.pop("payload")
    schedule_ok = (
        isinstance(schedule_payload, dict)
        and isinstance(schedule_payload.get("marketSchedules"), dict)
    )
    schedule["payload_summary"] = {
        "schema_valid": schedule_ok,
        "market_schedule_groups": sorted(schedule_payload.get("marketSchedules", {})) if schedule_ok else [],
        "note": "Underlying-market hours are informational only; TradFi perpetual contracts remain continuously tradable.",
    }
    if schedule["status"] == "PASS" and not schedule_ok:
        schedule["status"] = "FAIL_SCHEMA"
        schedule["error"] = "Trading-schedule response missing marketSchedules object"
    report["endpoint_results"].append(schedule)

    samples = []
    for side in ("long", "short"):
        if universe[side]:
            samples.append((side, universe[side][0]))
    for side, market in samples:
        symbol = str(market["symbol"])
        ticker = call(
            f"sample_24h_ticker:{symbol}", "ticker/24hr", {"symbol": symbol}
        )
        ticker_payload = ticker.pop("payload")
        ticker_summary = summarise_24h_ticker(
            ticker_payload, as_of_ms=server_time_ms if server_time_ok else None
        )
        if ticker["status"] == "PASS" and not ticker_summary["schema_valid"]:
            ticker["status"] = "FAIL_SCHEMA"
            ticker["error"] = ticker_summary.get("reason", "Invalid 24-hour ticker schema")
        ticker["payload_summary"] = ticker_summary
        report["endpoint_results"].append(ticker)

        depth = call(f"sample_order_book:{symbol}", "depth", {"symbol": symbol, "limit": "5"})
        depth_payload = depth.pop("payload")
        depth_summary = summarise_orderbook(depth_payload)
        if depth["status"] == "PASS" and not depth_summary["schema_valid"]:
            depth["status"] = "FAIL_SCHEMA"
            depth["error"] = depth_summary.get("reason", "Invalid order-book schema")
        depth["payload_summary"] = depth_summary
        report["endpoint_results"].append(depth)

        premium = call(f"sample_mark_index_funding:{symbol}", "premiumIndex", {"symbol": symbol})
        premium_payload = premium.pop("payload")
        premium_summary = summarise_premium_index(premium_payload)
        if premium["status"] == "PASS" and not premium_summary["schema_valid"]:
            premium["status"] = "FAIL_SCHEMA"
            premium["error"] = premium_summary.get("reason", "Invalid premium-index schema")
        premium["payload_summary"] = premium_summary
        report["endpoint_results"].append(premium)
        report["sample_checks"].append({
            "side": side,
            "symbol": symbol,
            "contract_type": market.get("contractType"),
            "underlying_type": market.get("underlyingType"),
            "ticker_24h": ticker_summary,
            "order_book": depth_summary,
            "mark_index_funding": premium_summary,
        })

    statuses = [item["status"] for item in report["endpoint_results"]]
    endpoint_ok = bool(statuses) and all(status == "PASS" for status in statuses)
    long_ok = bool(universe["long"])
    short_ok = bool(universe["short"])
    ticker_checks = [
        item for item in report["endpoint_results"]
        if item["name"].startswith("sample_24h_ticker:")
    ]
    ticker_schema_ok = len(ticker_checks) == len(samples) and all(
        item["payload_summary"]["schema_valid"] for item in ticker_checks
    )
    report["gates"] = {
        "public_exchange_info": "PASS" if report["market_universe"] else "FAIL",
        "stock_and_index_perp_universe": "PASS" if long_ok else "FAIL",
        "crypto_perp_universe": "PASS" if short_ok else "FAIL",
        "sample_24h_ticker_orderbook_and_funding_endpoints": "PASS" if endpoint_ok else "FAIL",
        "ticker_endpoint_supports_sampled_contract_types": "PASS" if ticker_schema_ok else "FAIL",
        "overall_read_only_preflight": (
            "PASS" if endpoint_ok and long_ok and short_ok and ticker_schema_ok else "FAIL"
        ),
        "ranking": "NOT_RUN",
        "paper_trading": "NOT_IMPLEMENTED",
        "order_submission": "NOT_PRESENT",
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    universe = report.get("market_universe", {})
    lines = [
        "# Binance Stock/Index Perpetuals vs Crypto Perpetuals — API Preflight",
        "",
        f"- Run: {report['run_at_utc']}",
        f"- Active exchange symbols: {universe.get('exchange_symbol_count')}",
        "- Provider: Binance USD-M Futures only",
        "- Authentication: none; public GET only",
        "",
        "## Approved universe",
        "",
        f"- Long stock/index perpetuals: {universe.get('long_stock_and_index_perps_count')}",
        f"- Short crypto perpetuals: {universe.get('short_crypto_perps_count')}",
        f"- Excluded: {universe.get('excluded_count')}",
        "- Classification uses exact Binance contractType, underlyingType, status, and USDT settlement fields.",
        "- Commodity, FX, and unknown underlying types are excluded; no ticker/title inference.",
        "- Trading-session schedules are informational only; no stock-market-open gate is used.",
        "",
        "## Sample contracts",
        "",
        "| Side | Symbol | Contract type | Underlying type | Ticker 24h % | Ticker data valid |",
        "|---|---|---|---|---:|---|",
    ]
    for sample in report.get("sample_checks", []):
        ticker = sample["ticker_24h"]
        lines.append(
            f"| {sample['side']} | {sample['symbol']} | {sample['contract_type']} | "
            f"{sample['underlying_type']} | {ticker.get('price_change_percent')} | "
            f"{ticker.get('data_valid')} |"
        )
    lines.extend(["", "## Endpoint checks", "", "| Endpoint | Status | Detail |", "|---|---|---|"])
    for item in report.get("endpoint_results", []):
        detail = item.get("error") or str(item.get("payload_summary", {}))[:220]
        lines.append(f"| {item['name']} | {item['status']} | {detail.replace('|', '/')} |")
    lines.extend(["", "## Gates", "", "| Gate | Status |", "|---|---|"])
    lines.extend(f"| {name} | **{status}** |" for name, status in report.get("gates", {}).items())
    lines.extend([
        "",
        "No account state, positions, portfolio state, simulated fills, or exchange orders are read or written.",
        "The 24-hour ticker endpoint is checked for one sampled stock/index contract and one sampled crypto contract.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="artifacts/binance-preflight")
    args = parser.parse_args(argv)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = run_preflight()
    (out / "binance_preflight_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (out / "binance_preflight_report.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    print(json.dumps(report["gates"], indent=2))
    return 0 if report["gates"]["overall_read_only_preflight"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
