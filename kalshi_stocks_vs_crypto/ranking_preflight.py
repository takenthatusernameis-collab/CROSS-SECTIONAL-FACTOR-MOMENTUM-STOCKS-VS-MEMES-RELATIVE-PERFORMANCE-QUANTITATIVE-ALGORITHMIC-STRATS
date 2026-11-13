"""Read-only, full-universe Kalshi trailing-24h ranking data-quality diagnostic."""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .preflight import BASE_URL, request_json
from .rankings import (
    MAX_PRICE_AGE_SECONDS,
    REFERENCE_TOLERANCE_SECONDS,
    build_provisional_leaders,
    calculate_trailing_return,
    split_active_universe,
)


def schedule_summary(market: dict[str, Any]) -> dict[str, Any]:
    schedule = market.get("schedule")
    if not isinstance(schedule, dict):
        return {"schedule_available": False, "is_open": None,
                "next_open_ts": None, "next_close_ts": None}
    return {"schedule_available": True,
            "is_open": schedule.get("is_open"),
            "next_open_ts": schedule.get("next_open_ts"),
            "next_close_ts": schedule.get("next_close_ts")}



def equities_session_status(markets: list[dict[str, Any]]) -> dict[str, Any]:
    """Open only when every active market in the Equities class reports open."""
    equities = [m for m in markets if m.get("asset_class") == "Equities"]
    open_markets = [m for m in equities if schedule_summary(m)["is_open"] is True]
    closed_markets = [m for m in equities if schedule_summary(m)["is_open"] is False]
    unknown_markets = [m for m in equities if schedule_summary(m)["is_open"] not in (True, False)]
    next_open_values = sorted({
        int(s["next_open_ts"])
        for m in equities
        for s in [schedule_summary(m)]
        if isinstance(s["next_open_ts"], (int, float)) and not isinstance(s["next_open_ts"], bool)
    })
    return {
        "session_open": bool(equities) and len(open_markets) == len(equities),
        "rule": "all active markets with asset_class=Equities must report schedule.is_open=true",
        "equities_market_count": len(equities),
        "open_count": len(open_markets),
        "closed_count": len(closed_markets),
        "unknown_schedule_count": len(unknown_markets),
        "next_open_ts_values": next_open_values,
        "next_open_timestamps_utc": [
            datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")
            for ts in next_open_values
        ],
    }


def fetch_market_candles(market: dict[str, Any], as_of_ts: int) -> dict[str, Any]:
    ticker = str(market["ticker"])
    encoded = urllib.parse.quote(ticker, safe="")
    try:
        payload, meta = request_json(
            f"markets/{encoded}/candlesticks",
            {"start_ts": str(as_of_ts - 26 * 3600), "end_ts": str(as_of_ts),
             "period_interval": "1", "include_latest_before_start": "true"},
            timeout=22.0, attempts=3,
        )
        candles = payload.get("candlesticks")
        if not isinstance(candles, list):
            raise ValueError("Candlestick response missing a candlesticks array")
        observation = calculate_trailing_return(candles, as_of_ts)
        return {"ticker": ticker, "asset_class": market.get("asset_class"),
                "title": market.get("title"), "schedule": schedule_summary(market), **observation,
                "http_status": meta["http_status"], "request_seconds": meta["elapsed_seconds"]}
    except Exception as exc:
        return {"ticker": ticker, "asset_class": market.get("asset_class"),
                "title": market.get("title"), "schedule": schedule_summary(market), "valid": False,
                "reason": f"request_or_schema_error:{type(exc).__name__}:{exc}",
                "http_status": getattr(exc, "status", None)}


def run_ranking_preflight(max_workers: int = 2) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    as_of_ts = int(started.timestamp())
    report: dict[str, Any] = {
        "report_version": 1, "project": "kalshi_stocks_vs_crypto",
        "run_at_utc": started.isoformat().replace("+00:00", "Z"),
        "runner": {"os": platform.platform(), "python": platform.python_version(),
                   "github_runner_os": os.getenv("RUNNER_OS"), "github_run_id": os.getenv("GITHUB_RUN_ID"),
                   "github_sha": os.getenv("GITHUB_SHA"), "github_ref": os.getenv("GITHUB_REF")},
        "api": {"base_url": BASE_URL, "methods": ["GET"], "authentication": "none",
                "market_data_provider": "Kalshi only"},
        "strategy_scope": {
            "long_asset_classes": ["Equities", "Indices"],
            "short_asset_classes": ["Crypto"],
            "excluded_asset_classes": "all other raw asset_class values, including Metals and Energy",
            "note": "Equities and Indices are deliberately combined. ETFs remain included if Kalshi classifies them as Equities. No ticker/title inference.",
            "long_notional": "1x NAV", "short_notional": "1x NAV",
            "gross_exposure": "2x NAV", "paper_positions_created": False},
        "market_list": {}, "equities_session": {}, "coverage": {}, "provisional_leaders": {},
        "market_results": [], "gates": {},
        "safety_assertions": {"public_get_only": True, "no_credentials": True,
            "no_private_account_endpoints": True, "no_order_endpoints": True,
            "no_positions_or_portfolio_state": True, "no_external_price_or_taxonomy_provider": True},
    }

    market_payload, market_meta = request_json("markets", {"status": "active"})
    markets = market_payload.get("markets")
    if not isinstance(markets, list) or any(not isinstance(item, dict) for item in markets):
        raise ValueError("GET /markets?status=active did not return a markets array")
    cursor = market_payload.get("cursor")
    if cursor:
        raise RuntimeError("Market-list response contains a non-empty cursor; pagination is required but not implemented")
    if len(markets) != len({m.get("ticker") for m in markets}):
        raise ValueError("Active market response contains duplicate or missing tickers")

    universes = split_active_universe(markets)
    raw_counts: dict[str, int] = {}
    for market in markets:
        raw = str(market.get("asset_class") or "<missing>")
        raw_counts[raw] = raw_counts.get(raw, 0) + 1
    all_targets = universes["long"] + universes["short"]

    excluded_counts: dict[str, int] = {}
    for market in universes["excluded"]:
        key = str(market.get("asset_class") or "<missing>")
        excluded_counts[key] = excluded_counts.get(key, 0) + 1

    schedule_counts: dict[str, dict[str, int]] = {}
    for market in all_targets:
        cls = str(market.get("asset_class"))
        raw_schedule = market.get("schedule")
        state = "unknown"
        if isinstance(raw_schedule, dict):
            state = "open" if raw_schedule.get("is_open") is True else "closed" if raw_schedule.get("is_open") is False else "unknown"
        schedule_counts.setdefault(cls, {"open": 0, "closed": 0, "unknown": 0})
        schedule_counts[cls][state] += 1

    report["market_list"] = {
        "http_status": market_meta["http_status"], "response_keys": sorted(market_payload),
        "cursor_present": "cursor" in market_payload, "cursor_nonempty": bool(cursor),
        "active_market_count": len(markets), "raw_asset_class_counts": dict(sorted(raw_counts.items())),
        "long_equities_and_indices_count": len(universes["long"]),
        "short_crypto_count": len(universes["short"]), "excluded_count": len(universes["excluded"]),
        "excluded_class_counts": dict(sorted(excluded_counts.items())),
        "schedule_open_counts_by_class": dict(sorted(schedule_counts.items())),
        "ranking_request_count": len(all_targets), "all_unique_tickers": True,
    }

    equities_status = equities_session_status(markets)
    report["equities_session"] = equities_status
    if not equities_status["session_open"]:
        report["gates"] = {
            "active_market_discovery": "PASS",
            "equities_session_open": "CLOSED_OR_UNKNOWN_SKIP",
            "one_minute_candle_retrieval": "NOT_RUN_SESSION_CLOSED",
            "complete_fresh_24h_return_coverage": "NOT_RUN",
            "provisional_leaders_available": "NOT_RUN",
            "ranking_diagnostic": "SKIPPED_SESSION_CLOSED",
            "paper_trading": "NOT_IMPLEMENTED",
            "order_submission": "NOT_PRESENT",
        }
        report["skip_reason"] = "Kalshi did not report every active Equities market as open; ranking candles were not requested."
        return report

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_market_candles, market, as_of_ts): market for market in all_targets}
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: item["ticker"])
    report["market_results"] = results

    long_tickers = {m["ticker"] for m in universes["long"]}
    short_tickers = {m["ticker"] for m in universes["short"]}
    long_results = [r for r in results if r["ticker"] in long_tickers]
    short_results = [r for r in results if r["ticker"] in short_tickers]
    leaders = build_provisional_leaders(long_results, short_results)
    report["provisional_leaders"] = leaders

    total = len(all_targets)
    valid_count = sum(1 for item in results if item.get("valid"))
    complete = (len(results) == total and valid_count == total
                and leaders["long_valid_count"] == len(universes["long"])
                and leaders["short_valid_count"] == len(universes["short"]))
    report["coverage"] = {
        "requested_market_count": total, "received_market_count": len(results),
        "valid_24h_return_count": valid_count, "invalid_or_stale_count": total - valid_count,
        "coverage_pct": round(100 * valid_count / total, 2) if total else 0.0,
        "long_valid_count": leaders["long_valid_count"], "long_total_count": len(universes["long"]),
        "short_valid_count": leaders["short_valid_count"], "short_total_count": len(universes["short"]),
        "max_current_price_age_seconds": MAX_PRICE_AGE_SECONDS,
        "max_24h_reference_gap_seconds": REFERENCE_TOLERANCE_SECONDS,
        "effective_window_note": "Reference is the latest valid close at or before 24h before the current close; actual interval and reference gap are reported per market.",
    }
    report["gates"] = {
        "active_market_discovery": "PASS",
        "equities_session_open": "PASS",
        "accepted_equity_and_crypto_universe": "PASS" if universes["long"] and universes["short"] else "FAIL",
        "one_minute_candle_retrieval": "PASS" if all(item.get("http_status") == 200 for item in results) and len(results) == total else "FAIL",
        "complete_fresh_24h_return_coverage": "PASS" if complete else "FAIL",
        "provisional_leaders_available": "PASS" if leaders["provisional_long_leader"] and leaders["provisional_short_leader"] else "FAIL",
        "ranking_diagnostic": "PASS" if complete and leaders["provisional_long_leader"] and leaders["provisional_short_leader"] else "INCOMPLETE",
        "paper_trading": "NOT_IMPLEMENTED",
        "order_submission": "NOT_PRESENT",
    }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Kalshi Equities/Indices vs Crypto — 24-hour Ranking Diagnostic", "",
        f"- Run: {report['run_at_utc']}", f"- Active markets: {report.get('market_list', {}).get('active_market_count')}",
        "- Requests: unauthenticated public GET only", "- No ranking result is used to create a paper position.", "",
        "## Accepted universe", "",
        "- Long: exact raw asset_class Equities or Indices.",
        "- Short: exact raw asset_class Crypto.",
        "- Other classes excluded. ETFs remain included if Kalshi puts them under Equities.",
        "- No ticker/title inference.", "", "## Equities session gate", ""]
    session = report.get("equities_session", {})
    lines.append(f"- Reported open: **{session.get('session_open')}**")
    lines.append(f"- Open/closed/unknown: {session.get('open_count')}/{session.get('closed_count')}/{session.get('unknown_schedule_count')} of {session.get('equities_market_count')}")
    next_open = session.get("next_open_timestamps_utc") or []
    lines.append(f"- Next reported open: {', '.join(next_open) if next_open else 'not supplied'}")
    if report.get("skip_reason"):
        lines.append(f"- **Skipped:** {report['skip_reason']}")
    lines.extend(["", "## Universe and coverage", "",
        "| Measure | Count |", "|---|---:|"])
    ml, cov = report.get("market_list", {}), report.get("coverage", {})
    for label, value in [
        ("Active markets", ml.get("active_market_count")),
        ("Long pool: Equities + Indices", ml.get("long_equities_and_indices_count")),
        ("Short pool: Crypto", ml.get("short_crypto_count")),
        ("Excluded", ml.get("excluded_count")),
        ("Candle requests", cov.get("requested_market_count")),
        ("Valid 24h returns", cov.get("valid_24h_return_count")),
        ("Invalid / stale returns", cov.get("invalid_or_stale_count")),
        ("Return coverage (%)", cov.get("coverage_pct")),
    ]:
        lines.append(f"| {label} | {value} |")
    lines.extend(["", "## Gates", "", "| Gate | Status |", "|---|---|"])
    lines.extend(f"| {name} | **{status}** |" for name, status in report.get("gates", {}).items())
    lines.extend(["", "## Provisional leaders", ""])
    leaders = report.get("provisional_leaders", {})
    for label, key in (("Long candidate (highest return)", "provisional_long_leader"),
                       ("Short candidate (lowest return)", "provisional_short_leader")):
        item = leaders.get(key)
        if item:
            lines.append(f"- **{label}:** {item['ticker']} ({item['asset_class']}) — {item['return_24h_pct']}%")
            lines.append(f"  - Current close: {item['current_price']} at {item['current_price_ts']}; reference: {item['reference_price']} at {item['reference_price_ts']}.")
            lines.append(f"  - Actual observation interval: {item['observation_interval_seconds']} seconds; reference gap: {item['reference_gap_seconds']} seconds.")
        else:
            lines.append(f"- **{label}:** unavailable")
    lines.extend(["", "## Per-market data quality", "",
        "| Ticker | Class | Return % | Valid | Reason | Current age s | Reference gap s | Market open |",
        "|---|---|---:|---:|---|---:|---:|"])
    for item in report.get("market_results", []):
        schedule = item.get('schedule', {})
        is_open = schedule.get('is_open')
        lines.append(f"| {item.get('ticker')} | {item.get('asset_class')} | {item.get('return_24h_pct','')} | {item.get('valid')} | {item.get('reason') or ''} | {item.get('current_price_age_seconds','')} | {item.get('reference_gap_seconds','')} | {is_open} |")
    lines.extend(["", "## Interpretation", "",
      "A provisional leader is only the best among markets with valid data; it is not a complete cross-sectional ranking unless complete-fresh-return-coverage passes. Null closes are ignored, not replaced. Markets failing freshness or reference-window validation are unavailable.",
      "", "This diagnostic creates no portfolio state, simulated fills, paper positions, or exchange orders.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="artifacts/kalshi-ranking-preflight")
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 3:
        parser.error("--workers must be between 1 and 3")
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = run_ranking_preflight(max_workers=args.workers)
    (out / "kalshi_ranking_report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (out / "kalshi_ranking_report.md").write_text(render_markdown(report), encoding="utf-8")
    summary = {"gates": report["gates"], "equities_session": report.get("equities_session"),
        "coverage": report.get("coverage"),
        "provisional_leaders": {key: (value.get("ticker"), value.get("return_24h_pct")) if value else None
            for key, value in report.get("provisional_leaders", {}).items() if key.startswith("provisional_")}}
    print(json.dumps(summary, indent=2))
    return 0 if report["gates"]["ranking_diagnostic"] in {"PASS", "SKIPPED_SESSION_CLOSED"} else 1


if __name__ == "__main__":
    sys.exit(main())
