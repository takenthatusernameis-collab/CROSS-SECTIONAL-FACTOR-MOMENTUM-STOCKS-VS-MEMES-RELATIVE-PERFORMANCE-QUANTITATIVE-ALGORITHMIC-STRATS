#!/usr/bin/env python3
"""Read-only Bitget stock-vs-crypto perpetual ranking and depth diagnostic.

Venue-compatibility test only. The short side is broad crypto, not Memes.
Only unauthenticated public GET endpoints are used. No paper state is touched.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

BASE_URL = "https://api.bitget.com"
CATEGORY = "USDT-FUTURES"
LEG_NOTIONAL_USDT = Decimal("10000")
MAX_DATA_AGE_MS = 900_000
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
ALLOWED_ENDPOINTS = {
    "/api/v3/market/time",
    "/api/v3/market/instruments",
    "/api/v3/market/tickers",
    "/api/v2/mix/market/merge-depth",
}


class DiagnosticError(RuntimeError):
    def __init__(self, stage: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.stage = stage
        self.http_status = http_status


def dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def get_json(stage: str, path: str, params: dict[str, str] | None = None):
    if path not in ALLOWED_ENDPOINTS:
        raise DiagnosticError(stage, "Endpoint is outside the fixed public GET allowlist.")
    query = urllib.parse.urlencode(params or {})
    url = BASE_URL + path + ("?" + query if query else "")
    for attempt in range(3):
        started = time.monotonic()
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "Accept-Encoding": "identity",
                     "User-Agent": "bitget-readonly-paper-ranking-diagnostic/1.0"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise DiagnosticError(stage, "Response exceeded the 24 MB safety limit.")
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise DiagnosticError(stage, "API response root was not a JSON object.")
                if response.status != 200:
                    raise DiagnosticError(stage, f"Unexpected HTTP status {response.status}.",
                                          int(response.status))
                if payload.get("code") != "00000":
                    raise DiagnosticError(stage, f"Bitget API error: {payload.get('code')} {payload.get('msg')}")
                return payload, {
                    "http_status": int(response.status),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                    "response_bytes": len(raw),
                    "request_time": payload.get("requestTime"),
                    "url": url,
                }
        except urllib.error.HTTPError as exc:
            detail = exc.read(1000).decode("utf-8", errors="replace")
            error = DiagnosticError(stage, f"HTTP {exc.code}: {detail[:800]}", int(exc.code))
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise error from exc
            time.sleep(0.75 * (attempt + 1))
        except DiagnosticError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            error = DiagnosticError(stage, f"{type(exc).__name__}: {exc}")
            if attempt == 2:
                raise error from exc
            time.sleep(0.5 * (attempt + 1))
    raise DiagnosticError(stage, "Request failed after retries.")


def rows(payload: dict[str, Any], stage: str) -> list[dict[str, Any]]:
    value = payload.get("data")
    if not isinstance(value, list) or not value or any(not isinstance(x, dict) for x in value):
        raise DiagnosticError(stage, "Expected a non-empty data array of objects.")
    return value


def scan_meme_tags(instruments: list[dict[str, Any]]) -> dict[str, Any]:
    keys = sorted({key for row in instruments for key in row})
    fields = [key for key in keys if "meme" in key.casefold()]
    taxonomy_tokens = (
        "category", "symboltype", "instrumenttype", "underlyingtype",
        "underlyingsubtype", "tag", "subtype", "classification", "sector", "theme",
    )
    taxonomy_fields = [
        key for key in keys
        if any(token in key.casefold() for token in taxonomy_tokens)
    ]
    matches = []
    non_taxonomy_collisions = []
    for row in instruments:
        hits = {}
        collisions = {}
        for key, value in row.items():
            values = value if isinstance(value, list) else [value]
            exact = any(
                isinstance(x, str) and x.strip().casefold() in {"meme", "memes"}
                for x in values
            )
            if exact and key in taxonomy_fields:
                hits[key] = value
            elif exact:
                collisions[key] = value
        if hits:
            matches.append({"symbol": row.get("symbol"), "fields": hits})
        if collisions:
            non_taxonomy_collisions.append({"symbol": row.get("symbol"), "fields": collisions})
    return {
        "metadata_fields": keys,
        "taxonomy_candidate_fields": taxonomy_fields,
        "explicit_meme_field_names": fields,
        "exact_meme_tag_match_count": len(matches),
        "exact_meme_tag_matches": matches[:50],
        "non_taxonomy_string_collisions": non_taxonomy_collisions[:50],
        "non_taxonomy_string_collision_count": len(non_taxonomy_collisions),
        "symbol_name_inference_used": False,
    }


def classify(instruments: list[dict[str, Any]]):
    seen = set()
    stocks, crypto = [], []
    excluded: dict[str, int] = {}
    for row in instruments:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise DiagnosticError("instrument_validation", "Instrument row is missing symbol.")
        if symbol in seen:
            raise DiagnosticError("instrument_validation", f"Duplicate instrument symbol: {symbol}")
        seen.add(symbol)

        category = str(row.get("category", "")).upper()
        quote = str(row.get("quoteCoin", "")).upper()
        status = str(row.get("status", "")).casefold()
        contract_type = str(row.get("type", "")).casefold()
        symbol_type = str(row.get("symbolType", "")).casefold()
        instrument_type = str(row.get("instrumentType", "")).casefold()

        if category != CATEGORY or quote != "USDT":
            reason = "not_usdt_futures"
        elif status != "online":
            reason = "not_online"
        elif contract_type != "perpetual":
            reason = "not_perpetual"
        elif symbol_type == "stock" and instrument_type == "stock":
            stocks.append(row)
            continue
        elif symbol_type == "crypto" and instrument_type == "crypto":
            crypto.append(row)
            continue
        else:
            reason = "outside_exact_stock_or_crypto_taxonomy"
        excluded[reason] = excluded.get(reason, 0) + 1

    if not stocks:
        raise DiagnosticError("instrument_validation", "No online USDT stock perpetuals matched exact stock metadata.")
    if not crypto:
        raise DiagnosticError("instrument_validation", "No online USDT crypto perpetuals matched exact crypto metadata.")
    return stocks, crypto, excluded


def validate_tickers(tickers: list[dict[str, Any]], candidates: list[dict[str, Any]],
                     server_ms: int) -> dict[str, dict[str, Any]]:
    index = {}
    for row in tickers:
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise DiagnosticError("ticker_validation", "Ticker row is missing symbol.")
        if symbol in index:
            raise DiagnosticError("ticker_validation", f"Duplicate ticker symbol: {symbol}")
        index[symbol] = row

    valid = {}
    missing, invalid = [], []
    for instrument in candidates:
        symbol = instrument["symbol"]
        ticker = index.get(symbol)
        if ticker is None:
            missing.append(symbol)
            continue
        reasons = []
        if str(ticker.get("category", "")).upper() != CATEGORY:
            reasons.append("category_mismatch")
        change = dec(ticker.get("price24hPcnt"))
        last, bid, ask, stamp = (dec(ticker.get(k)) for k in
                                 ("lastPrice", "bid1Price", "ask1Price", "ts"))
        if change is None:
            reasons.append("invalid_price24hPcnt")
        if last is None or last <= 0:
            reasons.append("invalid_lastPrice")
        if bid is None or bid <= 0:
            reasons.append("invalid_bid1Price")
        if ask is None or ask <= 0 or (bid is not None and ask < bid):
            reasons.append("invalid_ask1Price_or_crossed_quote")
        if stamp is None or stamp <= 0 or stamp != stamp.to_integral_value():
            reasons.append("invalid_timestamp")
        elif abs(server_ms - int(stamp)) > MAX_DATA_AGE_MS:
            reasons.append("stale_ticker")
        if reasons:
            invalid.append({"symbol": symbol, "reason": ";".join(reasons)})
            continue
        valid[symbol] = {
            **ticker, "_change": change, "_last": last, "_bid": bid,
            "_ask": ask, "_ts": int(stamp),
        }
    if missing or invalid or len(valid) != len(candidates):
        raise DiagnosticError(
            "ticker_coverage",
            f"Ticker coverage failed: missing={len(missing)}, invalid={len(invalid)}; "
            f"missing_examples={missing[:8]}, invalid_examples={invalid[:8]}",
        )
    return valid


def vwap_for_notional(levels: Any, notional: Decimal) -> dict[str, Any]:
    if not isinstance(levels, list) or not levels:
        raise ValueError("Empty order book side.")
    remaining = notional
    quote_used = Decimal("0")
    quantity = Decimal("0")
    used = 0
    for level in levels:
        if not isinstance(level, list) or len(level) < 2:
            raise ValueError("Malformed order book level.")
        price, amount = dec(level[0]), dec(level[1])
        if price is None or amount is None or price <= 0 or amount <= 0:
            continue
        available = price * amount
        take_quote = min(remaining, available)
        quote_used += take_quote
        quantity += take_quote / price
        remaining -= take_quote
        used += 1
        if remaining <= Decimal("0.00000001"):
            break
    if remaining > Decimal("0.00000001") or quantity <= 0:
        return {
            "sufficient_depth": False,
            "requested_notional_usdt": str(notional),
            "unfilled_notional_usdt": str(max(remaining, Decimal("0"))),
            "levels_used": used,
        }
    return {
        "sufficient_depth": True,
        "requested_notional_usdt": str(notional),
        "filled_quantity_base": str(quantity),
        "vwap": str(quote_used / quantity),
        "quote_notional_usdt": str(quote_used),
        "levels_used": used,
    }


def check_depth(symbol: str, side: str, server_ms: int) -> dict[str, Any]:
    payload, meta = get_json(
        "depth_" + symbol, "/api/v2/mix/market/merge-depth",
        {"symbol": symbol, "productType": CATEGORY, "limit": "50"},
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise DiagnosticError("depth_" + symbol, "Order book data is not an object.")
    stamp = dec(data.get("ts"))
    if stamp is None or stamp <= 0 or stamp != stamp.to_integral_value():
        raise DiagnosticError("depth_" + symbol, "Order book timestamp is invalid.")
    age = abs(server_ms - int(stamp))
    if age > MAX_DATA_AGE_MS:
        raise DiagnosticError("depth_" + symbol, f"Order book is stale by {age} ms.")
    bids, asks = data.get("bids"), data.get("asks")
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        raise DiagnosticError("depth_" + symbol, "Order book is missing one side.")
    best_bid = dec(bids[0][0]) if isinstance(bids[0], list) and bids[0] else None
    best_ask = dec(asks[0][0]) if isinstance(asks[0], list) and asks[0] else None
    if best_bid is None or best_ask is None or best_bid <= 0 or best_ask < best_bid:
        raise DiagnosticError("depth_" + symbol, "Top-of-book is invalid or crossed.")
    mid = (best_bid + best_ask) / Decimal("2")
    levels = asks if side == "buy" else bids
    result = vwap_for_notional(levels, LEG_NOTIONAL_USDT)
    if result.get("sufficient_depth"):
        vwap = Decimal(result["vwap"])
        slip = ((vwap / mid - 1) if side == "buy" else (1 - vwap / mid)) * Decimal("10000")
        result["slippage_vs_mid_bps"] = str(slip)
    result.update({
        "symbol": symbol, "side": side, "book_timestamp_ms": int(stamp),
        "book_age_ms": age, "bid_levels": len(bids), "ask_levels": len(asks),
        "best_bid": str(best_bid), "best_ask": str(best_ask), "mid_price": str(mid),
        "http_status": meta["http_status"], "request_seconds": meta["elapsed_seconds"],
    })
    return result


def run_diagnostic() -> dict[str, Any]:
    report: dict[str, Any] = {
        "report_version": 1,
        "project": "bitget_readonly_perpetual_ranking_diagnostic",
        "run_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "runner": {
            "os": platform.platform(), "python": platform.python_version(),
            "github_runner_os": os.getenv("RUNNER_OS"),
            "github_run_id": os.getenv("GITHUB_RUN_ID"),
            "github_sha": os.getenv("GITHUB_SHA"), "github_ref": os.getenv("GITHUB_REF"),
        },
        "api": {
            "base_url": BASE_URL, "methods": ["GET"], "authentication": "none",
            "category": CATEGORY, "ranking_field": "price24hPcnt",
            "return_recalculation": False, "paper_leg_notional_usdt": str(LEG_NOTIONAL_USDT),
            "endpoints_allowlisted": sorted(ALLOWED_ENDPOINTS),
        },
        "universe": {}, "ticker_coverage": {}, "leaders": {},
        "ranking_samples": {}, "depth_checks": {}, "meme_taxonomy": {},
        "gates": {
            "bitget_public_api": "NOT_EVALUATED",
            "perpetual_instrument_discovery": "NOT_EVALUATED",
            "exact_stock_crypto_classification": "NOT_EVALUATED",
            "complete_fresh_ticker_coverage": "NOT_EVALUATED",
            "stock_crypto_ranking": "NOT_EVALUATED",
            "selected_pair_public_depth": "NOT_EVALUATED",
            "paper_accounting": "NOT_RUN_READ_ONLY_TEST",
            "order_submission": "NOT_PRESENT",
        },
        "safety_assertions": {
            "public_get_only": True, "no_credentials": True,
            "no_private_account_endpoints": True, "no_order_endpoints": True,
            "no_order_submission": True, "no_paper_positions_created": True,
            "no_sqlite_or_portfolio_state_access": True,
            "broad_crypto_short_is_not_meme": True, "symbol_name_inference_used": False,
        },
    }
    stage = "server_time"
    try:
        time_payload, time_meta = get_json(stage, "/api/v3/market/time")
        time_data = time_payload.get("data")
        server_time = dec(time_data.get("serverTime")) if isinstance(time_data, dict) else None
        if server_time is None or server_time <= 0 or server_time != server_time.to_integral_value():
            raise DiagnosticError(stage, "Expected integer data.serverTime from Bitget.")
        server_ms = int(server_time)
        report["api"]["server_time_http_status"] = time_meta["http_status"]
        report["api"]["server_time_ms"] = server_ms

        stage = "instrument_discovery"
        instruments_payload, instruments_meta = get_json(
            stage, "/api/v3/market/instruments", {"category": CATEGORY}
        )
        instruments = rows(instruments_payload, stage)
        report["api"]["instrument_http_status"] = instruments_meta["http_status"]
        report["api"]["instrument_request_seconds"] = instruments_meta["elapsed_seconds"]
        report["meme_taxonomy"] = scan_meme_tags(instruments)

        stage = "instrument_validation"
        stocks, crypto, excluded = classify(instruments)
        candidates = stocks + crypto
        report["universe"] = {
            "instrument_count": len(instruments),
            "eligible_stock_perpetual_count": len(stocks),
            "eligible_crypto_perpetual_count": len(crypto),
            "excluded_reason_counts": excluded,
            "all_symbols_unique": len({row["symbol"] for row in instruments}) == len(instruments),
            "long_universe_definition": "online perpetual USDT-FUTURES; symbolType=stock and instrumentType=stock",
            "short_test_universe_definition": "online perpetual USDT-FUTURES; symbolType=crypto and instrumentType=crypto",
            "short_universe_is_meme_only": False,
        }
        report["gates"]["bitget_public_api"] = "PASS"
        report["gates"]["perpetual_instrument_discovery"] = "PASS"
        report["gates"]["exact_stock_crypto_classification"] = "PASS"

        stage = "ticker_discovery"
        ticker_payload, ticker_meta = get_json(
            stage, "/api/v3/market/tickers", {"category": CATEGORY}
        )
        tickers = rows(ticker_payload, stage)
        report["api"]["ticker_http_status"] = ticker_meta["http_status"]
        report["api"]["ticker_request_seconds"] = ticker_meta["elapsed_seconds"]
        report["ticker_coverage"] = {
            "ticker_rows": len(tickers), "eligible_instrument_count": len(candidates),
        }

        stage = "ticker_validation"
        valid = validate_tickers(tickers, candidates, server_ms)
        report["ticker_coverage"].update({
            "validated_eligible_rows": len(valid), "coverage_percent": "100.0",
            "max_allowed_data_age_ms": MAX_DATA_AGE_MS,
        })
        report["gates"]["complete_fresh_ticker_coverage"] = "PASS"

        stock_ranked = sorted((valid[x["symbol"]] for x in stocks),
                              key=lambda x: (-x["_change"], x["symbol"]))
        crypto_ranked = sorted((valid[x["symbol"]] for x in crypto),
                               key=lambda x: (x["_change"], x["symbol"]))
        top_stock, bottom_crypto = stock_ranked[0], crypto_ranked[0]
        report["leaders"] = {
            "long_test_top_stock_gainer": {
                "symbol": top_stock["symbol"], "price24hPcnt": str(top_stock["_change"]),
                "lastPrice": str(top_stock["_last"]), "ticker_timestamp_ms": top_stock["_ts"],
            },
            "short_test_bottom_crypto_loser": {
                "symbol": bottom_crypto["symbol"], "price24hPcnt": str(bottom_crypto["_change"]),
                "lastPrice": str(bottom_crypto["_last"]), "ticker_timestamp_ms": bottom_crypto["_ts"],
            },
        }
        report["ranking_samples"] = {
            "top_10_stocks_by_source_return": [
                {"symbol": x["symbol"], "price24hPcnt": str(x["_change"])} for x in stock_ranked[:10]
            ],
            "bottom_10_crypto_by_source_return": [
                {"symbol": x["symbol"], "price24hPcnt": str(x["_change"])} for x in crypto_ranked[:10]
            ],
        }
        report["gates"]["stock_crypto_ranking"] = "PASS"

        stage = "selected_pair_depth"
        report["depth_checks"]["long_stock_buy"] = check_depth(top_stock["symbol"], "buy", server_ms)
        report["depth_checks"]["short_crypto_sell"] = check_depth(bottom_crypto["symbol"], "sell", server_ms)
        if all(x.get("sufficient_depth") for x in report["depth_checks"].values()):
            report["gates"]["selected_pair_public_depth"] = "PASS"
            report["status"] = "PASS_READ_ONLY_RANKING_AND_DEPTH"
        else:
            report["gates"]["selected_pair_public_depth"] = "FAIL_INSUFFICIENT_10K_DEPTH"
            report["status"] = "RANKING_PASS_DEPTH_BLOCKED"
    except DiagnosticError as exc:
        report["failure"] = {"stage": exc.stage, "http_status": exc.http_status, "message": str(exc)[:1600]}
        report["status"] = "FAILED"
        gate = {
            "server_time": "bitget_public_api",
            "instrument_discovery": "perpetual_instrument_discovery",
            "instrument_validation": "exact_stock_crypto_classification",
            "ticker_discovery": "complete_fresh_ticker_coverage",
            "ticker_validation": "complete_fresh_ticker_coverage",
            "selected_pair_depth": "selected_pair_public_depth",
        }.get(exc.stage)
        if gate:
            report["gates"][gate] = "FAIL"
    except Exception as exc:
        report["failure"] = {"stage": stage, "http_status": None,
                             "message": f"{type(exc).__name__}: {exc}"[:1600]}
        report["status"] = "FAILED"
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Bitget Read-only Perpetual Ranking Diagnostic", "",
        f"- Status: **{report.get('status')}**",
        f"- Run UTC: {report.get('run_at_utc')}",
        f"- GitHub run: {report.get('runner', {}).get('github_run_id')}",
        f"- Runner: {report.get('runner', {}).get('os')}", "",
        "Unauthenticated public GET only. No account access, orders, paper positions, SQLite, or portfolio state.",
        "", "## Test scope", "",
        "- Long test universe: active USDT perpetuals explicitly classified as stock by both metadata fields.",
        "- Short test universe: active USDT perpetuals explicitly classified as crypto by both metadata fields.",
        "- The short test is broad crypto, not Memes, and is not a silent migration of the frozen Stocks-vs-Memes strategy.",
        "- Ranking uses the exchange-reported price24hPcnt field; returns are not recomputed from prices.",
        f"- Depth test sweeps public bids/asks for {LEG_NOTIONAL_USDT} USDT per leg to estimate VWAP only.",
        "", "## Gates", "",
    ]
    for key, value in report.get("gates", {}).items():
        lines.append(f"- {key}: **{value}**")
    if report.get("universe"):
        u = report["universe"]
        lines.extend(["", "## Universe", "",
            f"- Instruments: {u.get('instrument_count')}",
            f"- Active stock perpetuals: {u.get('eligible_stock_perpetual_count')}",
            f"- Active crypto perpetuals: {u.get('eligible_crypto_perpetual_count')}",
            f"- Validated eligible ticker rows: {report.get('ticker_coverage', {}).get('validated_eligible_rows')}",
        ])
    if report.get("leaders"):
        lines.extend(["", "## Provisional rankings", ""])
        for side, item in report["leaders"].items():
            lines.append(f"- {side}: **{item.get('symbol')}**, price24hPcnt={item.get('price24hPcnt')}, last={item.get('lastPrice')}")
    if report.get("ranking_samples"):
        lines.extend(["", "## Top/bottom 10", "",
            "Top stock gainers: " + json.dumps(report["ranking_samples"].get("top_10_stocks_by_source_return", [])),
            "Bottom crypto performers: " + json.dumps(report["ranking_samples"].get("bottom_10_crypto_by_source_return", [])),
        ])
    if report.get("depth_checks"):
        lines.extend(["", "## Order-book depth", ""])
        for side, item in report["depth_checks"].items():
            lines.append(f"- {side}: {json.dumps(item, sort_keys=True)}")
    tax = report.get("meme_taxonomy", {})
    if tax:
        lines.extend(["", "## Meme taxonomy audit", "",
            f"- Metadata fields containing the word meme: {tax.get('explicit_meme_field_names') or 'none'}",
            f"- Exact Meme/Memes matches in taxonomy fields: {tax.get('exact_meme_tag_match_count')}",
            f"- Exact Meme/Memes strings outside taxonomy fields (not treated as tags): {tax.get('non_taxonomy_string_collision_count')}",
            f"- Non-taxonomy examples: {json.dumps(tax.get('non_taxonomy_string_collisions', []))}",
            "- Base-coin/name strings are not accepted as taxonomy; no Meme-only ranking was attempted.",
        ])
    if report.get("failure"):
        fail = report["failure"]
        lines.extend(["", "## Failure", "", f"- Stage: {fail.get('stage')}",
                      f"- HTTP status: {fail.get('http_status')}", f"- Detail: {fail.get('message')}"])
    lines.extend(["", "## Safety assertions", ""])
    for key, value in report.get("safety_assertions", {}).items():
        lines.append(f"- {key}: {str(value).lower()}")
    return "\n".join(lines) + "\n"


def main() -> int:
    out = Path("artifacts/bitget-readonly-ranking")
    out.mkdir(parents=True, exist_ok=True)
    report = run_diagnostic()
    (out / "bitget_ranking_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "bitget_ranking_report.md").write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({
        "status": report.get("status"), "gates": report.get("gates"),
        "universe": report.get("universe"), "leaders": report.get("leaders"),
        "depth_checks": report.get("depth_checks"), "failure": report.get("failure"),
        "paper_accounting": "NOT RUN", "orders": "NONE",
    }, indent=2, sort_keys=True))
    return 0 if report.get("status") == "PASS_READ_ONLY_RANKING_AND_DEPTH" else 1


if __name__ == "__main__":
    sys.exit(main())
