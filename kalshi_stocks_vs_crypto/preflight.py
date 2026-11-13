"""Read-only diagnostic for the Kalshi Perps public market-data API.

This module has no credentials, private-account, WebSocket, order-submission,
ranking, portfolio, or paper-position functionality.
"""
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_URL = "https://external-api.kalshi.com/trade-api/v2/margin/"
USER_AGENT = "kalshi-stocks-vs-crypto-read-only-preflight/0.1"
MAX_BYTES = 16 * 1024 * 1024
MAX_SAMPLES = 3
RETRYABLE = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def request_json(path: str, params: dict[str, str] | None = None,
                 timeout: float = 18.0, attempts: int = 3) -> tuple[dict[str, Any], dict[str, Any]]:
    """Make an unauthenticated GET to a relative Kalshi Perps API route."""
    allowed_prefixes = ("markets", "funding_rates", "corporate_actions")
    if path.startswith("/") or ".." in path.split("/") or not path.startswith(allowed_prefixes):
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
                if not isinstance(data, dict):
                    raise ApiError("Expected a JSON object response", int(response.status))
                headers = {k.lower(): v for k, v in response.headers.items()}
                return data, {
                    "url": url, "http_status": int(response.status),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "response_bytes": len(body), "content_type": headers.get("content-type"),
                    "date_header": headers.get("date"), "retry_after": headers.get("retry-after"),
                    "rate_limit_headers": {k: v for k, v in headers.items()
                                           if "rate" in k or "limit" in k or "request-id" in k},
                }
        except urllib.error.HTTPError as exc:
            detail = exc.read(800).decode("utf-8", errors="replace")
            err = ApiError(f"HTTP {exc.code}: {detail[:800]}", int(exc.code))
            if exc.code not in RETRYABLE or attempt + 1 >= attempts:
                raise err from exc
            retry = exc.headers.get("Retry-After") if exc.headers else None
            try:
                delay = min(5.0, max(0.5, float(retry))) if retry else .75 * (attempt + 1)
            except ValueError:
                delay = .75 * (attempt + 1)
            time.sleep(delay)
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            if attempt + 1 >= attempts:
                raise ApiError(f"Network error: {type(exc).__name__}: {exc}") from exc
            time.sleep(.5 * (attempt + 1))
    raise ApiError("Request failed after retries")


def call(name: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    start = time.monotonic()
    try:
        payload, meta = request_json(path, params)
        return {"name": name, "path": path, "status": "PASS", "error": None,
                "meta": meta, "payload": payload,
                "duration_seconds": round(time.monotonic() - start, 3)}
    except Exception as exc:  # capture diagnostics rather than hiding endpoint failures
        return {"name": name, "path": path, "status": "FAIL",
                "error": f"{type(exc).__name__}: {exc}",
                "http_status": getattr(exc, "status", None),
                "meta": {"duration_seconds": round(time.monotonic() - start, 3)}, "payload": None}


def keys(value: Any) -> list[str]:
    return sorted(str(k) for k in value) if isinstance(value, dict) else []


def metadata_shapes(value: Any, prefix: str = "product_metadata", depth: int = 0) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if depth > 5:
        return out
    if isinstance(value, dict):
        for key, child in value.items():
            p = f"{prefix}.{key}"
            kind = "object" if isinstance(child, dict) else "array" if isinstance(child, list) else type(child).__name__
            entry: dict[str, Any] = {"type": kind}
            if isinstance(child, str):
                entry["sample_value"] = child[:120]
            elif isinstance(child, (int, float, bool)) or child is None:
                entry["sample_value"] = child
            out[p] = entry
            if isinstance(child, (dict, list)):
                out.update(metadata_shapes(child, p, depth + 1))
    elif isinstance(value, list) and value:
        out.update(metadata_shapes(value[0], prefix + "[]", depth + 1))
    return out


def analyse_market_list(payload: dict[str, Any]) -> dict[str, Any]:
    markets = payload.get("markets")
    if not isinstance(markets, list) or any(not isinstance(m, dict) for m in markets):
        raise ValueError("Market-list response must contain an array of market objects")
    counts: dict[str, int] = {}
    fields: set[str] = set()
    shapes: dict[str, Any] = {}
    missing = 0
    for market in markets:
        fields.update(str(k) for k in market)
        raw = "<missing>" if market.get("asset_class") is None else str(market["asset_class"])
        counts[raw] = counts.get(raw, 0) + 1
        missing += not bool(market.get("ticker"))
        for p, desc in metadata_shapes(market.get("product_metadata")).items():
            shapes.setdefault(p, desc)
            if "sample_value" not in shapes[p] and "sample_value" in desc:
                shapes[p]["sample_value"] = desc["sample_value"]
    return {"market_count": len(markets), "top_level_response_keys": keys(payload),
            "response_has_cursor": "cursor" in payload, "market_field_union": sorted(fields),
            "asset_class_counts": dict(sorted(counts.items())),
            "product_metadata_field_shapes": dict(sorted(shapes.items())),
            "markets_missing_ticker": missing, "markets": markets}


def choose_sample_markets(markets: list[dict[str, Any]], limit: int = MAX_SAMPLES) -> list[dict[str, Any]]:
    """Choose samples across raw asset_class values; never infer an instrument type."""
    ordered = sorted((m for m in markets if isinstance(m.get("ticker"), str) and m["ticker"]),
                     key=lambda m: m["ticker"])
    chosen: list[dict[str, Any]] = []
    classes: set[str] = set()
    for market in ordered:
        raw = "<missing>" if market.get("asset_class") is None else str(market["asset_class"])
        if raw not in classes:
            chosen.append(market)
            classes.add(raw)
            if len(chosen) >= limit:
                return chosen
    for market in ordered:
        if market not in chosen:
            chosen.append(market)
            if len(chosen) >= limit:
                break
    return chosen


def summarise_candlesticks(payload: dict[str, Any]) -> dict[str, Any]:
    candles = payload.get("candlesticks")
    if not isinstance(candles, list):
        return {"schema_valid": False, "reason": "missing candlesticks array"}
    valid = nulls = 0
    timestamps: list[int] = []
    price_fields: set[str] = set()
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        price = candle.get("price")
        if isinstance(price, dict):
            price_fields.update(str(k) for k in price)
            if price.get("close") is None:
                nulls += 1
            else:
                valid += 1
        ts = candle.get("end_period_ts")
        if isinstance(ts, (int, float)):
            timestamps.append(int(ts))
    return {"schema_valid": True, "ticker": payload.get("ticker"),
            "candlestick_count": len(candles), "price_field_names": sorted(price_fields),
            "valid_trade_price_closes": valid, "null_trade_price_closes": nulls,
            "first_end_period_ts": min(timestamps) if timestamps else None,
            "last_end_period_ts": max(timestamps) if timestamps else None}


def summarise_orderbook(payload: dict[str, Any]) -> dict[str, Any]:
    book = payload.get("orderbook")
    if not isinstance(book, dict):
        return {"schema_valid": False, "reason": "missing orderbook object"}
    bids, asks = book.get("bids"), book.get("asks")
    return {"schema_valid": isinstance(bids, list) and isinstance(asks, list),
            "orderbook_keys": keys(book), "bid_level_count": len(bids) if isinstance(bids, list) else None,
            "ask_level_count": len(asks) if isinstance(asks, list) else None,
            "sample_bid_levels": bids[:2] if isinstance(bids, list) else None,
            "sample_ask_levels": asks[:2] if isinstance(asks, list) else None}


def market_object(payload: dict[str, Any]) -> dict[str, Any] | None:
    item = payload.get("market")
    if isinstance(item, dict):
        return item
    return payload if payload.get("ticker") else None


def run_preflight() -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    now = int(started.timestamp())
    report: dict[str, Any] = {
        "report_version": 1, "project": "kalshi_stocks_vs_crypto",
        "run_at_utc": started.isoformat().replace("+00:00", "Z"),
        "runner": {"os": platform.platform(), "python": platform.python_version(),
                   "github_runner_os": os.getenv("RUNNER_OS"), "github_run_id": os.getenv("GITHUB_RUN_ID"),
                   "github_sha": os.getenv("GITHUB_SHA"), "github_ref": os.getenv("GITHUB_REF")},
        "api": {"base_url": BASE_URL, "authentication": "none; public unauthenticated GET only",
                "methods_used": ["GET"], "no_credentials_or_private_account_endpoints": True},
        "endpoint_results": [], "market_universe": {}, "sample_market_checks": [], "gates": {},
        "classification_assessment": {"status": "UNRESOLVED_REQUIRES_EVIDENCE_REVIEW",
          "rule": "Raw asset_class/product_metadata only; no ticker or title inference.",
          "individual_stock_vs_etf_vs_index_confirmed": False, "crypto_universe_confirmed": False}}

    market_result = call("active_markets", "markets", {"status": "active"})
    payload = market_result.pop("payload")
    market_analysis: dict[str, Any] = {}
    sample: list[dict[str, Any]] = []
    if market_result["status"] == "PASS":
        try:
            market_analysis = analyse_market_list(payload)
            sample = choose_sample_markets(market_analysis.pop("markets"))
            report["market_universe"] = market_analysis
        except Exception as exc:
            market_result["status"] = "FAIL_SCHEMA"
            market_result["error"] = f"{type(exc).__name__}: {exc}"
    report["endpoint_results"].append(market_result)

    for market in sample:
        ticker = str(market["ticker"])
        encoded = urllib.parse.quote(ticker, safe="")
        detail = call(f"market_detail:{ticker}", f"markets/{encoded}")
        detail_data = detail.pop("payload")
        obj = market_object(detail_data) if isinstance(detail_data, dict) else None
        detail_summary = {"top_level_keys": keys(detail_data), "market_keys": keys(obj),
                          "asset_class": obj.get("asset_class") if obj else None,
                          "product_metadata_field_shapes": metadata_shapes(obj.get("product_metadata")) if obj else {},
                          "ticker_matches_requested": bool(obj and obj.get("ticker") == ticker)}
        if not obj or obj.get("ticker") != ticker or "asset_class" not in obj:
            detail["status"] = "FAIL_SCHEMA"
            detail["error"] = "Market-detail response did not validate requested ticker and asset_class"
        detail["payload_summary"] = detail_summary
        report["endpoint_results"].append(detail)

        book = call(f"orderbook:{ticker}", f"markets/{encoded}/orderbook")
        book_data = book.pop("payload")
        book_summary = summarise_orderbook(book_data) if isinstance(book_data, dict) else {"schema_valid": False}
        if not book_summary["schema_valid"]:
            book["status"] = "FAIL_SCHEMA"
            book["error"] = book_summary.get("reason", "Invalid orderbook schema")
        book["payload_summary"] = book_summary
        report["endpoint_results"].append(book)

        candles = call(f"candlesticks_1m_26h:{ticker}", f"markets/{encoded}/candlesticks",
                       {"start_ts": str(now - 26 * 3600), "end_ts": str(now),
                        "period_interval": "1", "include_latest_before_start": "true"})
        candle_data = candles.pop("payload")
        candle_summary = summarise_candlesticks(candle_data) if isinstance(candle_data, dict) else {"schema_valid": False}
        if not candle_summary["schema_valid"]:
            candles["status"] = "FAIL_SCHEMA"
            candles["error"] = candle_summary.get("reason", "Invalid candlestick schema")
        candles["payload_summary"] = candle_summary
        report["endpoint_results"].append(candles)

        funding = call(f"funding_estimate:{ticker}", "funding_rates/estimate", {"ticker": ticker})
        funding_data = funding.pop("payload")
        funding_summary = {"top_level_keys": keys(funding_data),
                           "market_ticker": funding_data.get("market_ticker") if isinstance(funding_data, dict) else None,
                           "has_funding_rate": isinstance(funding_data, dict) and "funding_rate" in funding_data,
                           "next_funding_time": funding_data.get("next_funding_time") if isinstance(funding_data, dict) else None}
        if not funding_summary["has_funding_rate"]:
            funding["status"] = "FAIL_SCHEMA"
            funding["error"] = "Funding estimate did not contain funding_rate"
        funding["payload_summary"] = funding_summary
        report["endpoint_results"].append(funding)
        report["sample_market_checks"].append({"ticker": ticker, "raw_asset_class": market.get("asset_class"),
             "market_detail": detail_summary, "orderbook": book_summary,
             "candlesticks": candle_summary, "funding_estimate": funding_summary})
        time.sleep(.35)

    if sample:
        ticker = str(sample[0]["ticker"])
        hist = call(f"historical_funding_rates:{ticker}", "funding_rates/historical",
                    {"ticker": ticker, "start_ts": str(now - 7 * 86400), "end_ts": str(now)})
        hist_data = hist.pop("payload")
        rates = hist_data.get("funding_rates") if isinstance(hist_data, dict) else None
        hist_summary = {"top_level_keys": keys(hist_data), "funding_rate_count": len(rates) if isinstance(rates, list) else None,
                        "sample_fields": keys(rates[0]) if isinstance(rates, list) and rates and isinstance(rates[0], dict) else []}
        if not isinstance(rates, list):
            hist["status"] = "FAIL_SCHEMA"
            hist["error"] = "Historical funding response did not contain funding_rates array"
        hist["payload_summary"] = hist_summary
        report["endpoint_results"].append(hist)
    else:
        report["endpoint_results"].append({"name": "historical_funding_rates", "path": "funding_rates/historical",
                                            "status": "SKIPPED", "reason": "No sample ticker available"})

    for name, path in (("market_holiday_schedules", "markets/schedules"), ("corporate_actions", "corporate_actions")):
        result = call(name, path)
        data = result.pop("payload")
        result["payload_summary"] = {"top_level_keys": keys(data),
          "top_level_item_count": sum(len(v) for v in data.values() if isinstance(v, (list, dict))) if isinstance(data, dict) else None,
          "note": ("Holiday closures only; regular daily/weekend sessions are supplied by per-market schedule metadata."
                   if name == "market_holiday_schedules" else "Public corporate-action notices for margin markets.")}
        if name == "corporate_actions" and not (isinstance(data, dict) and isinstance(data.get("corporate_actions"), list)):
            result["status"] = "FAIL_SCHEMA"
            result["error"] = "Corporate-actions response did not contain corporate_actions array"
        report["endpoint_results"].append(result)

    statuses = [e["status"] for e in report["endpoint_results"] if e.get("status") != "SKIPPED"]
    market_ok = report["endpoint_results"][0]["status"] == "PASS" and bool(market_analysis.get("market_count"))
    all_ok = bool(statuses) and all(s == "PASS" for s in statuses)
    report["gates"] = {
        "production_api_market_discovery": "PASS" if market_ok else "FAIL",
        "sample_public_market_data_endpoints": "PASS" if all_ok else "FAIL",
        "classification_gate": "BLOCKED_REQUIRES_MANUAL_SCHEMA_REVIEW",
        "ranking_gate": "NOT_RUN", "execution_accounting_gate": "NOT_RUN",
        "paper_trading_gate": "NOT_AUTHORIZED_AND_NOT_IMPLEMENTED", "eligible_universe_confirmed": False,
        "overall_stage_1_data_preflight": "PASS" if market_ok and bool(sample) and all_ok else "FAIL",
        "strategy_implementation_authorized": False}
    report["safety_assertions"] = {"only_public_get_requests": True, "no_auth_headers_or_api_secrets": True,
      "no_private_account_endpoints": True, "no_order_or_trade_endpoints": True,
      "no_market_ranking_or_portfolio_state_changes": True, "no_coinmarketcap_or_other_market_data_provider": True}
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = ["# Kalshi Perps Read-only Preflight Report", "",
             f"- Run time: {report.get('run_at_utc')}", f"- Base URL: {report.get('api', {}).get('base_url')}",
             "- Mode: public unauthenticated GET only; no trading or private account endpoints", "",
             "## Stage gates", "", "| Gate | Result |", "|---|---|"]
    lines.extend(f"| {k} | **{v}** |" for k, v in report.get("gates", {}).items())
    lines.extend(["", "## Endpoint results", "", "| Endpoint | Status | HTTP | Duration (s) | Error |", "|---|---:|---:|---:|---|"])
    for item in report.get("endpoint_results", []):
        meta = item.get("meta", {})
        status = item.get("status", "UNKNOWN")
        http = meta.get("http_status", item.get("http_status", ""))
        duration = meta.get("elapsed_seconds", meta.get("duration_seconds", item.get("duration_seconds", "")))
        err = (item.get("error") or item.get("reason") or "").replace("|", "/")
        lines.append(f"| {item.get('name')} | {status} | {http or ''} | {duration or ''} | {err} |")
    u = report.get("market_universe", {})
    lines.extend(["", "## Market-universe schema", "", f"- Active markets returned: {u.get('market_count', 'not available')}",
      f"- Raw asset_class counts: {json.dumps(u.get('asset_class_counts', {}), sort_keys=True)}",
      f"- Market fields: {', '.join(u.get('market_field_union', []))}",
      f"- Product metadata paths: {', '.join(u.get('product_metadata_field_shapes', {}).keys())}",
      "", "**Classification remains unresolved.** No instrument was classified from a ticker or title. Verify individual stocks versus ETFs and indices from authoritative metadata before implementing ranking.", "", "## Sample checks", ""])
    for s in report.get("sample_market_checks", []):
        lines.append(f"### {s['ticker']} (raw asset_class: {s.get('raw_asset_class')})")
        for k in ("market_detail", "orderbook", "candlesticks", "funding_estimate"):
            lines.append(f"- {k}: {json.dumps(s.get(k, {}), sort_keys=True)}")
        lines.append("")
    lines.extend(["## Interpretation", "",
      "A successful API preflight does not prove the full universe is classifiable, liquid, or eligible. It does not authorize ranking, position creation, or subsequent implementation. A failure describes this runner and time only.",
      "", "The market-schedules REST endpoint covers holiday closures; regular daily/weekend sessions come from each market's schedule metadata.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="artifacts/kalshi-preflight", help="Directory for JSON and Markdown reports")
    args = parser.parse_args(argv)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    report = run_preflight()
    json_path, md_path = out / "kalshi_preflight_report.json", out / "kalshi_preflight_report.md"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"gates": report["gates"], "json_report": str(json_path), "markdown_report": str(md_path)}, indent=2))
    return 0 if report["gates"]["overall_stage_1_data_preflight"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
