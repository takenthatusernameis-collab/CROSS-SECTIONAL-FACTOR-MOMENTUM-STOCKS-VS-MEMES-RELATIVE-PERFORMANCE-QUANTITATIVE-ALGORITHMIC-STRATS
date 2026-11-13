"""Read-only, full-universe trailing-24h Binance perpetual ranking diagnostic."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .preflight import BASE_URL, request_json
from .rankings import (
    LOOKBACK_MILLISECONDS, ONE_MINUTE_MILLISECONDS, MAX_PRICE_AGE_SECONDS,
    REFERENCE_TOLERANCE_SECONDS, build_provisional_leaders,
    calculate_trailing_return, classify_binance_universe,
)

KLINE_LIMIT = 1500
KLINE_WINDOW_MILLISECONDS = LOOKBACK_MILLISECONDS + (32 * ONE_MINUTE_MILLISECONDS)


def fetch_market_candles(market: dict[str, Any], as_of_ms: int) -> dict[str, Any]:
    symbol = str(market["symbol"])
    pair = str(market.get("pair") or symbol)
    contract_type = str(market["contractType"])
    try:
        candles, meta = request_json("continuousKlines", {
            "pair": pair, "contractType": contract_type, "interval": "1m",
            "startTime": str(as_of_ms - KLINE_WINDOW_MILLISECONDS),
            "endTime": str(as_of_ms), "limit": str(KLINE_LIMIT),
        }, timeout=22.0, attempts=3)
        if not isinstance(candles, list):
            raise ValueError("continuousKlines did not return an array")
        observation = calculate_trailing_return(candles, as_of_ms)
        return {"symbol": symbol, "pair": pair, "contract_type": contract_type,
                "underlying_type": market.get("underlyingType"),
                "underlying_sub_type": market.get("underlyingSubType"),
                "quote_asset": market.get("quoteAsset"), "candle_count": len(candles),
                **observation, "http_status": meta["http_status"],
                "request_seconds": meta["elapsed_seconds"]}
    except Exception as exc:
        return {"symbol": symbol, "pair": pair, "contract_type": contract_type,
                "underlying_type": market.get("underlyingType"), "valid": False,
                "reason": "request_or_schema_error:" + type(exc).__name__ + ":" + str(exc),
                "http_status": getattr(exc, "status", None)}


def run_ranking_preflight(max_workers: int = 4) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "report_version": 1, "project": "binance_stocks_vs_crypto",
        "run_at_utc": started.isoformat().replace("+00:00", "Z"),
        "runner": {"os": platform.platform(), "python": platform.python_version(),
            "github_runner_os": os.getenv("RUNNER_OS"), "github_run_id": os.getenv("GITHUB_RUN_ID"),
            "github_sha": os.getenv("GITHUB_SHA"), "github_ref": os.getenv("GITHUB_REF")},
        "api": {"base_url": BASE_URL, "methods": ["GET"], "authentication": "none",
            "market_data_provider": "Binance USD-M Futures only",
            "candle_endpoint": "/fapi/v1/continuousKlines", "interval": "1m"},
        "strategy_scope": {
            "long_contract_type": "TRADIFI_PERPETUAL",
            "long_underlying_types": ["EQUITY", "CN_EQUITY", "KR_EQUITY", "HK_EQUITY", "PREMARKET", "INDEX"],
            "short_contract_type": "PERPETUAL", "short_underlying_types": ["COIN"],
            "quote_and_margin_asset": "USDT",
            "ranking_price": "last traded contract close from completed one-minute continuous klines; not mark/index price",
            "long_notional": "1x NAV", "short_notional": "1x NAV", "gross_exposure": "2x NAV",
            "paper_positions_created": False},
        "exchange_info": {}, "coverage": {}, "provisional_leaders": {},
        "market_results": [], "gates": {},
        "safety_assertions": {"public_get_only": True, "no_credentials": True,
            "no_private_account_endpoints": True, "no_order_endpoints": True,
            "no_positions_or_portfolio_state": True, "no_external_price_or_taxonomy_provider": True},
    }

    exchange_payload, exchange_meta = request_json("exchangeInfo")
    symbols = exchange_payload.get("symbols") if isinstance(exchange_payload, dict) else None
    if not isinstance(symbols, list) or any(not isinstance(item, dict) for item in symbols):
        raise ValueError("GET /fapi/v1/exchangeInfo did not return a symbols array")
    universe = classify_binance_universe(symbols)
    server_payload, server_meta = request_json("time")
    as_of_ms = server_payload.get("serverTime") if isinstance(server_payload, dict) else None
    if isinstance(as_of_ms, bool) or not isinstance(as_of_ms, int):
        raise ValueError("GET /fapi/v1/time did not return integer serverTime")

    counts: dict[str, int] = {}
    for item in symbols:
        key = "{}|{}|{}|{}".format(item.get("contractType"), item.get("underlyingType"),
                                    item.get("status"), item.get("quoteAsset"))
        counts[key] = counts.get(key, 0) + 1
    excluded_counts: dict[str, int] = {}
    for item in universe["excluded"]:
        key = item["reason"]
        excluded_counts[key] = excluded_counts.get(key, 0) + 1
    targets = universe["long"] + universe["short"]
    report["exchange_info"] = {
        "http_status": exchange_meta["http_status"], "server_time_http_status": server_meta["http_status"],
        "as_of_server_time_ms": as_of_ms, "exchange_symbol_count": len(symbols),
        "long_stock_and_index_perp_count": len(universe["long"]),
        "short_crypto_perp_count": len(universe["short"]), "excluded_count": len(universe["excluded"]),
        "contract_underlying_status_quote_counts": dict(sorted(counts.items())),
        "excluded_reason_counts": dict(sorted(excluded_counts.items())),
        "ranking_request_count": len(targets),
        "all_exchange_symbols_unique": len(symbols) == len({m.get("symbol") for m in symbols}),
    }

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_market_candles, market, as_of_ms): market for market in targets}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["symbol"])
    report["market_results"] = results

    long_symbols = {m["symbol"] for m in universe["long"]}
    short_symbols = {m["symbol"] for m in universe["short"]}
    long_results = [r for r in results if r["symbol"] in long_symbols]
    short_results = [r for r in results if r["symbol"] in short_symbols]
    leaders = build_provisional_leaders(long_results, short_results)
    report["provisional_leaders"] = leaders

    total = len(targets)
    valid_count = sum(1 for item in results if item.get("valid"))
    complete = (len(results) == total and valid_count == total
                and leaders["long_valid_count"] == len(universe["long"])
                and leaders["short_valid_count"] == len(universe["short"]))
    report["coverage"] = {
        "requested_contract_count": total, "received_contract_count": len(results),
        "valid_24h_return_count": valid_count, "invalid_or_stale_count": total - valid_count,
        "coverage_pct": round(100 * valid_count / total, 2) if total else 0.0,
        "long_valid_count": leaders["long_valid_count"], "long_total_count": len(universe["long"]),
        "short_valid_count": leaders["short_valid_count"], "short_total_count": len(universe["short"]),
        "max_current_price_age_seconds": MAX_PRICE_AGE_SECONDS,
        "max_24h_reference_gap_seconds": REFERENCE_TOLERANCE_SECONDS,
        "requested_kline_window_minutes": KLINE_WINDOW_MILLISECONDS // 60_000,
        "kline_limit": KLINE_LIMIT,
    }
    leaders_available = bool(leaders["provisional_long_leader"] and leaders["provisional_short_leader"])
    unique = report["exchange_info"]["all_exchange_symbols_unique"]
    report["gates"] = {
        "exchange_info_discovery": "PASS" if unique else "FAIL",
        "accepted_stock_and_crypto_universes": "PASS" if universe["long"] and universe["short"] else "FAIL",
        "one_minute_contract_kline_retrieval": "PASS" if len(results) == total and all(r.get("http_status") == 200 for r in results) else "FAIL",
        "complete_fresh_24h_return_coverage": "PASS" if complete else "FAIL",
        "provisional_leaders_available": "PASS" if leaders_available else "FAIL",
        "ranking_diagnostic": "PASS" if complete and leaders_available else "INCOMPLETE",
        "paper_trading": "NOT_IMPLEMENTED", "order_submission": "NOT_PRESENT",
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    info, coverage = report.get("exchange_info", {}), report.get("coverage", {})
    lines = ["# Binance Stock/Index Perpetuals vs Crypto Perpetuals — 24-hour Ranking", "",
        "- Run: " + report["run_at_utc"], "- Binance server time (ms): " + str(info.get("as_of_server_time_ms")),
        "- Public unauthenticated GET only; no paper positions or orders.", "",
        "## Universe", "",
        "- Long: active TRADIFI_PERPETUAL contracts with approved equity/index underlyingType values.",
        "- Short: active PERPETUAL contracts with underlyingType=COIN.",
        "- Both sides require USDT quote and margin assets; other/unknown classes excluded.",
        "- No ticker inference. No underlying-stock session gate: stock perps trade 24/7.",
        "- Rankings use the contract's own traded close, not index or mark-price candles.", "",
        "| Measure | Count |", "|---|---:|"]
    for label, value in [
        ("Exchange symbols", info.get("exchange_symbol_count")),
        ("Long stock/index perps", info.get("long_stock_and_index_perp_count")),
        ("Short crypto perps", info.get("short_crypto_perp_count")),
        ("Excluded", info.get("excluded_count")),
        ("Kline requests", coverage.get("requested_contract_count")),
        ("Valid 24h returns", coverage.get("valid_24h_return_count")),
        ("Invalid/stale", coverage.get("invalid_or_stale_count")),
        ("Coverage %", coverage.get("coverage_pct")),
    ]:
        lines.append("| " + label + " | " + str(value) + " |")
    lines.extend(["", "## Gates", "", "| Gate | Status |", "|---|---|"])
    lines.extend("| " + k + " | " + v + " |" for k, v in report.get("gates", {}).items())
    lines.extend(["", "## Provisional leaders", ""])
    for label, key in [("Long highest return", "provisional_long_leader"),
                       ("Short lowest return", "provisional_short_leader")]:
        item = report.get("provisional_leaders", {}).get(key)
        if item:
            lines.append("- " + label + ": " + item["symbol"] + " (" + str(item["underlying_type"]) +
                         ") — " + item["return_24h_pct"] + "%")
            lines.append("  - Close " + item["current_price"] + " at " + str(item["current_price_ts_ms"]) +
                         " ms; reference " + item["reference_price"] + " at " +
                         str(item["reference_price_ts_ms"]) + " ms.")
        else:
            lines.append("- " + label + ": unavailable")
    lines.extend(["", "## Per-contract data quality", "",
        "| Symbol | Contract | Underlying | Return % | Valid | Reason | Current age s | Reference gap s |",
        "|---|---|---|---:|---|---|---:|---:|"])
    for item in report.get("market_results", []):
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            item.get("symbol"), item.get("contract_type"), item.get("underlying_type"),
            item.get("return_24h_pct", ""), item.get("valid"), item.get("reason") or "",
            item.get("current_price_age_seconds", ""), item.get("reference_gap_seconds", "")))
    lines.extend(["", "A provisional leader is not a complete ranking unless all accepted contracts pass freshness checks.",
        "Stock perps remain tradable 24/7, but index and mark references can behave differently while cash markets are closed.",
        "This diagnostic does not create portfolio state, paper fills, NAV, or exchange orders.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="artifacts/binance-ranking-preflight")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8:
        parser.error("--workers must be between 1 and 8")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = run_ranking_preflight(max_workers=args.workers)
    (out / "binance_ranking_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "binance_ranking_report.md").write_text(render_markdown(report), encoding="utf-8")
    summary = {"gates": report["gates"], "coverage": report["coverage"],
        "provisional_leaders": {k: (v.get("symbol"), v.get("return_24h_pct")) if v else None
            for k, v in report["provisional_leaders"].items() if k.startswith("provisional_")}}
    print(json.dumps(summary, indent=2))
    return 0 if report["gates"]["ranking_diagnostic"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
