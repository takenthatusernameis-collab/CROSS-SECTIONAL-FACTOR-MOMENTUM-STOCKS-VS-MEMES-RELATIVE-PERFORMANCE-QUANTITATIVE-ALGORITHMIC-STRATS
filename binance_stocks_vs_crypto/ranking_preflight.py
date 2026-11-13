"""Read-only, full-universe Binance 24-hour ticker ranking diagnostic."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .preflight import BASE_URL, request_json
from .rankings import (
    MAX_TICKER_WINDOW_AGE_SECONDS, build_provisional_leaders,
    classify_binance_universe, parse_binance_24h_tickers,
)


def run_ranking_preflight() -> dict[str, Any]:
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
            "methods": ["GET"],
            "authentication": "none",
            "market_data_provider": "Binance USD-M Futures only",
            "ranking_endpoint": "/fapi/v1/ticker/24hr",
            "ranking_request_count": 1,
            "ranking_metric": "Binance priceChangePercent from its rolling 24-hour ticker report",
            "local_return_recalculation": False,
        },
        "strategy_scope": {
            "long_contract_type": "TRADIFI_PERPETUAL",
            "long_underlying_types": ["EQUITY", "CN_EQUITY", "KR_EQUITY", "HK_EQUITY", "PREMARKET", "INDEX"],
            "short_contract_type": "PERPETUAL",
            "short_underlying_types": ["COIN"],
            "quote_and_margin_asset": "USDT",
            "ranking_price": "Binance 24-hour ticker priceChangePercent; latest ticker lastPrice is reported for context",
            "long_notional": "1x NAV",
            "short_notional": "1x NAV",
            "gross_exposure": "2x NAV",
            "paper_positions_created": False,
        },
        "exchange_info": {},
        "ticker_report": {},
        "coverage": {},
        "provisional_leaders": {},
        "market_results": [],
        "gates": {},
        "safety_assertions": {
            "public_get_only": True,
            "no_credentials": True,
            "no_private_account_endpoints": True,
            "no_order_endpoints": True,
            "no_positions_or_portfolio_state": True,
            "no_external_price_or_taxonomy_provider": True,
        },
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

    ticker_payload, ticker_meta = request_json("ticker/24hr")
    ticker_records = parse_binance_24h_tickers(ticker_payload, as_of_ms=as_of_ms)

    counts: dict[str, int] = {}
    for item in symbols:
        key = "{}|{}|{}|{}".format(
            item.get("contractType"), item.get("underlyingType"),
            item.get("status"), item.get("quoteAsset")
        )
        counts[key] = counts.get(key, 0) + 1
    excluded_counts: dict[str, int] = {}
    for item in universe["excluded"]:
        key = item["reason"]
        excluded_counts[key] = excluded_counts.get(key, 0) + 1
    targets = universe["long"] + universe["short"]

    report["exchange_info"] = {
        "http_status": exchange_meta["http_status"],
        "server_time_http_status": server_meta["http_status"],
        "as_of_server_time_ms": as_of_ms,
        "exchange_symbol_count": len(symbols),
        "long_stock_and_index_perp_count": len(universe["long"]),
        "short_crypto_perp_count": len(universe["short"]),
        "excluded_count": len(universe["excluded"]),
        "contract_underlying_status_quote_counts": dict(sorted(counts.items())),
        "excluded_reason_counts": dict(sorted(excluded_counts.items())),
        "all_exchange_symbols_unique": len(symbols) == len({m.get("symbol") for m in symbols}),
    }
    report["ticker_report"] = {
        "http_status": ticker_meta["http_status"],
        "request_seconds": ticker_meta["elapsed_seconds"],
        "response_bytes": ticker_meta["response_bytes"],
        "response_symbol_count": len(ticker_records),
        "eligible_universe_count": len(targets),
        "eligible_symbols_found": sum(1 for market in targets if market["symbol"] in ticker_records),
        "symbols_not_in_ticker_report": [
            market["symbol"] for market in targets if market["symbol"] not in ticker_records
        ],
        "max_ticker_window_age_seconds": MAX_TICKER_WINDOW_AGE_SECONDS,
        "last_trade_timestamp_included_by_endpoint": False,
    }

    results: list[dict[str, Any]] = []
    for market in targets:
        symbol = market["symbol"]
        ticker = ticker_records.get(symbol)
        if ticker is None:
            result = {
                "symbol": symbol,
                "valid": False,
                "reason": "missing_from_24h_ticker_report",
                "return_24h_pct": None,
                "last_price": None,
                "trade_count": None,
                "ticker_window_age_seconds": None,
            }
        else:
            result = dict(ticker)
        result.update({
            "contract_type": market.get("contractType"),
            "underlying_type": market.get("underlyingType"),
            "underlying_sub_type": market.get("underlyingSubType"),
            "quote_asset": market.get("quoteAsset"),
            "margin_asset": market.get("marginAsset"),
        })
        results.append(result)
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
    matched_count = sum(1 for item in results if item.get("reason") != "missing_from_24h_ticker_report")
    complete = (
        len(results) == total
        and valid_count == total
        and leaders["long_valid_count"] == len(universe["long"])
        and leaders["short_valid_count"] == len(universe["short"])
    )
    report["coverage"] = {
        "eligible_contract_count": total,
        "ticker_rows_matched_to_eligible_contracts": matched_count,
        "valid_24h_ticker_count": valid_count,
        "missing_or_invalid_ticker_count": total - valid_count,
        "coverage_pct": round(100 * valid_count / total, 2) if total else 0.0,
        "long_valid_count": leaders["long_valid_count"],
        "long_total_count": len(universe["long"]),
        "short_valid_count": leaders["short_valid_count"],
        "short_total_count": len(universe["short"]),
        "ticker_report_requests": 1,
    }
    leaders_available = bool(
        leaders["provisional_long_leader"] and leaders["provisional_short_leader"]
    )
    unique = report["exchange_info"]["all_exchange_symbols_unique"]
    report["gates"] = {
        "exchange_info_discovery": "PASS" if unique else "FAIL",
        "accepted_stock_and_crypto_universes": (
            "PASS" if universe["long"] and universe["short"] else "FAIL"
        ),
        "binance_24h_ticker_report_retrieval": (
            "PASS" if ticker_meta["http_status"] == 200 and ticker_records else "FAIL"
        ),
        "complete_valid_24h_ticker_coverage": "PASS" if complete else "FAIL",
        "provisional_leaders_available": "PASS" if leaders_available else "FAIL",
        "ranking_diagnostic": "PASS" if complete and leaders_available else "INCOMPLETE",
        "paper_trading": "NOT_IMPLEMENTED",
        "order_submission": "NOT_PRESENT",
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    info = report.get("exchange_info", {})
    ticker = report.get("ticker_report", {})
    coverage = report.get("coverage", {})
    lines = [
        "# Binance Stock/Index Perpetuals vs Crypto Perpetuals — 24-hour Ranking",
        "",
        "- Run: " + report["run_at_utc"],
        "- Binance server time (ms): " + str(info.get("as_of_server_time_ms")),
        "- Public unauthenticated GET only; no paper positions or orders.",
        "- Ranking source: Binance /fapi/v1/ticker/24hr priceChangePercent.",
        "- Returns are taken directly from Binance; this workflow does not recalculate them.",
        "",
        "## Universe",
        "",
        "- Long: active TRADIFI_PERPETUAL contracts with approved equity/index underlyingType values.",
        "- Short: active PERPETUAL contracts with underlyingType=COIN.",
        "- Both sides require USDT quote and margin assets; other/unknown classes excluded.",
        "- No ticker inference. No underlying-equity session gate: stock perps trade 24/7.",
        "",
        "## Ticker report and coverage",
        "",
        "| Measure | Value |",
        "|---|---:|",
        "| Ticker requests | " + str(coverage.get("ticker_report_requests")) + " |",
        "| Symbols returned by Binance | " + str(ticker.get("response_symbol_count")) + " |",
        "| Eligible contracts | " + str(coverage.get("eligible_contract_count")) + " |",
        "| Matched ticker rows | " + str(coverage.get("ticker_rows_matched_to_eligible_contracts")) + " |",
        "| Valid 24h ticker rows | " + str(coverage.get("valid_24h_ticker_count")) + " |",
        "| Missing/invalid rows | " + str(coverage.get("missing_or_invalid_ticker_count")) + " |",
        "| Coverage % | " + str(coverage.get("coverage_pct")) + " |",
        "",
        "## Gates",
        "",
        "| Gate | Status |",
        "|---|---|",
    ]
    lines.extend("| " + key + " | " + value + " |" for key, value in report.get("gates", {}).items())
    lines.extend(["", "## Provisional leaders", ""])
    for label, key in [
        ("Long highest return", "provisional_long_leader"),
        ("Short lowest return", "provisional_short_leader"),
    ]:
        item = report.get("provisional_leaders", {}).get(key)
        if item:
            lines.append(
                "- " + label + ": " + item["symbol"] + " (" +
                str(item["underlying_type"]) + ") — " + item["return_24h_pct"] + "%"
            )
            lines.append(
                "  - Binance lastPrice " + str(item["last_price"]) +
                "; trade count " + str(item["trade_count"]) +
                "; ticker-window age " + str(item["ticker_window_age_seconds"]) + " seconds."
            )
        else:
            lines.append("- " + label + ": unavailable")
    lines.extend([
        "",
        "## Per-contract ticker data",
        "",
        "| Symbol | Contract | Underlying | Binance 24h % | Valid | Trades | Quote volume | Window age s | Reason |",
        "|---|---|---|---:|---|---:|---:|---:|---|",
    ])
    for item in report.get("market_results", []):
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            item.get("symbol"), item.get("contract_type"), item.get("underlying_type"),
            item.get("return_24h_pct") or "", item.get("valid"), item.get("trade_count"),
            item.get("quote_volume") or "", item.get("ticker_window_age_seconds"),
            item.get("reason") or "",
        ))
    lines.extend([
        "",
        "The 24-hour percentage is Binance's reported priceChangePercent, not a locally recomputed return.",
        "The ticker response includes rolling-window timestamps and a 24-hour trade count, but does not include an exact last-trade timestamp. Window freshness therefore does not prove that the last trade itself occurred within the last 15 minutes.",
        "A missing ticker row, invalid price/change, zero trade count, or stale ticker window makes coverage incomplete.",
        "This diagnostic does not create portfolio state, paper fills, NAV, or exchange orders.",
        "",
    ])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="artifacts/binance-ranking-preflight")
    args = parser.parse_args(argv)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = run_ranking_preflight()
    (out / "binance_ranking_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    (out / "binance_ranking_report.md").write_text(
        render_markdown(report), encoding="utf-8"
    )
    summary = {
        "gates": report["gates"],
        "coverage": report["coverage"],
        "provisional_leaders": {
            key: (value.get("symbol"), value.get("return_24h_pct")) if value else None
            for key, value in report["provisional_leaders"].items()
            if key.startswith("provisional_")
        },
    }
    print(json.dumps(summary, indent=2))
    return 0 if report["gates"]["ranking_diagnostic"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
