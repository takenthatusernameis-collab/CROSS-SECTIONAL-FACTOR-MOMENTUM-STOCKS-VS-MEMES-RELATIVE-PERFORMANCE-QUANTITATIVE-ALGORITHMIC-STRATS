#!/usr/bin/env python3
"""Run Bitget Stocks-vs-Crypto daily paper ranking and accounting."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from decimal import Decimal, Decimal as D
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bitget_stocks_vs_crypto.market import MarketDataError, decimal_value, get_json, build_ranking_report
from bitget_stocks_vs_crypto.portfolio import (PaperError, fee_rate_for, parse_book, rebalance_gate, run_snapshot, size_for_target_notional, trade_report)
from bitget_stocks_vs_crypto.monitor import monitor_open_positions
from bitget_stocks_vs_crypto.risk import expected_minute_starts
from bitget_stocks_vs_crypto.performance import (ensure_equity_start, record_equity_point, performance_summary, export_performance_artifacts)


def fetch_depth(symbol: str) -> dict[str, Any]:
    payload, _ = get_json("/api/v2/mix/market/merge-depth", {
        "symbol": symbol, "productType": "USDT-FUTURES",
    }, stage=f"depth_{symbol}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PaperError(f"{symbol}: malformed Bitget order-book response")
    return data


def read_only_execution_preflight(report: dict[str, Any], initial_nav: Decimal,
                                  fee_override: Decimal | None) -> dict[str, Any]:
    """Check the selected pair's exact entry size against current public max-depth books.

    This is a read-only diagnostic: it does not initialize or modify SQLite state.
    """
    markets = {row.get("symbol"): row for row in report.get("market_results", [])
               if isinstance(row, dict)}
    leaders = report.get("leaders", {})
    selections = (
        ("LONG", leaders.get("long_stock_top_gainer"), "asks", "BUY"),
        ("SHORT", leaders.get("short_crypto_bottom_loser"), "bids", "SELL"),
    )
    if any(not isinstance(item, dict) for _, item, _, _ in selections):
        raise PaperError("Both selected market leaders are required for depth preflight")
    fees = []
    for _, item, _, _ in selections:
        market = markets.get(item.get("symbol"))
        if not isinstance(market, dict):
            raise PaperError(f"{item.get('symbol')}: selected market metadata is unavailable")
        fees.append(fee_rate_for(market, fee_override))
    target = initial_nav / (Decimal("1") + fees[0] + fees[1])
    legs = {}
    for (leg, leader, side, action), fee in zip(selections, fees):
        symbol = leader["symbol"]
        market = markets[symbol]
        raw_book = fetch_depth(symbol)
        book = parse_book(raw_book)
        age_ms = abs(int(report["as_of_server_time_ms"]) - int(book["timestamp_ms"]))
        if age_ms > 900_000:
            raise PaperError(f"{symbol}: order book is stale by {age_ms} ms")
        qty, price, notional = size_for_target_notional(target, book[side], market)
        legs[leg] = {
            "symbol": symbol, "action": action, "requested_target_notional_usdt": str(target),
            "sized_quantity": str(qty), "estimated_entry_vwap": str(price),
            "estimated_entry_notional_usdt": str(notional),
            "estimated_entry_fee_usdt": str(notional * fee), "fee_rate": str(fee),
            "book_age_ms": age_ms, "bid_levels": len(book["bids"]),
            "ask_levels": len(book["asks"]), "depth_status": "PASS",
        }
    return {
        "status": "PASS",
        "scope": "read-only entry-depth and lot-size preflight; no simulated fills or positions",
        "initial_nav_usdt": str(initial_nav),
        "gross_target_usdt": str(target * 2),
        "legs": legs,
    }


def fetch_1m_candles(symbol: str, start_ms: int, end_ms: int, candle_type: str) -> list[dict[str, Any]]:
    """Fetch complete one-minute Bitget contract candles using public GET only.

    The v3 endpoint returns up to 1,000 bars per request. Requests are split on
    minute boundaries; duplicate or malformed rows fail closed for analytics.
    """
    if candle_type not in {"market", "mark"}:
        raise PaperError(f"{symbol}: unsupported candle type {candle_type!r}")
    expected = expected_minute_starts(start_ms, end_ms)
    if not expected:
        return []
    found: dict[int, dict[str, Any]] = {}
    page_size = 1000
    for offset in range(0, len(expected), page_size):
        chunk = expected[offset:offset + page_size]
        chunk_first, chunk_last = chunk[0], chunk[-1]
        payload, _ = get_json("/api/v3/market/candles", {
            "category": "USDT-FUTURES", "symbol": symbol, "interval": "1m",
            "type": candle_type, "startTime": str(chunk_first),
            "endTime": str(chunk_last + 59_999), "limit": str(page_size),
        }, stage=f"candles_{candle_type}_{symbol}")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise PaperError(f"{symbol}: Bitget {candle_type} candles did not return an array")
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 5:
                raise PaperError(f"{symbol}: malformed Bitget {candle_type} candle row")
            try:
                stamp = decimal_value(row[0], "candle timestamp", positive=True)
                if stamp != stamp.to_integral_value():
                    raise PaperError(f"{symbol}: Bitget candle timestamp is not an integer")
                timestamp = int(stamp)
            except (ValueError, TypeError) as exc:
                raise PaperError(f"{symbol}: invalid Bitget candle timestamp") from exc
            if timestamp % 60_000:
                raise PaperError(f"{symbol}: Bitget returned a non-minute-aligned candle")
            if timestamp < chunk_first or timestamp > chunk_last:
                continue
            candle = {
                "timestamp_ms": timestamp,
                "open": decimal_value(row[1], "candle open", positive=True),
                "high": decimal_value(row[2], "candle high", positive=True),
                "low": decimal_value(row[3], "candle low", positive=True),
                "close": decimal_value(row[4], "candle close", positive=True),
            }
            if candle["high"] < max(candle["open"], candle["close"], candle["low"]):
                raise PaperError(f"{symbol}: inconsistent candle high")
            if candle["low"] > min(candle["open"], candle["close"], candle["high"]):
                raise PaperError(f"{symbol}: inconsistent candle low")
            prior = found.get(timestamp)
            if prior is not None and prior != candle:
                raise PaperError(f"{symbol}: conflicting duplicate candle at {timestamp}")
            found[timestamp] = candle
    return [found[ts] for ts in sorted(found)]


def fetch_monitor_tickers(symbols: list[str], now_ms: int) -> dict[str, dict[str, Any]]:
    """Fetch one public ticker snapshot and return validated marks for held symbols."""
    payload, _ = get_json("/api/v3/market/tickers", {"category": "USDT-FUTURES"}, stage="monitor_tickers")
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise PaperError("Bitget monitoring ticker response is not an array")
    wanted = set(symbols)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("symbol") not in wanted:
            continue
        symbol = row["symbol"]
        if symbol in result:
            raise PaperError(f"{symbol}: duplicate monitoring ticker")
        stamp = decimal_value(row.get("ts"), f"{symbol} ticker timestamp", positive=True)
        if stamp != stamp.to_integral_value():
            raise PaperError(f"{symbol}: ticker timestamp is not an integer")
        if abs(int(now_ms) - int(stamp)) > 900_000:
            raise PaperError(f"{symbol}: monitoring ticker is stale")
        result[symbol] = {
            "timestamp_ms": int(stamp),
            "mark_price": str(decimal_value(row.get("markPrice"), f"{symbol} markPrice", positive=True)),
            "last_price": str(decimal_value(row.get("lastPrice"), f"{symbol} lastPrice", positive=True)),
        }
    missing = wanted - set(result)
    if missing:
        raise PaperError(f"Missing Bitget monitoring tickers for {sorted(missing)}")
    return result


def fetch_server_time_ms() -> int:
    payload, _ = get_json("/api/v3/market/time", stage="monitor_server_time")
    data = payload.get("data")
    raw = data.get("serverTime", data.get("ts")) if isinstance(data, dict) else data
    if raw is None:
        raw = payload.get("requestTime")
    stamp = decimal_value(raw, "Bitget server time", positive=True)
    if stamp != stamp.to_integral_value():
        raise PaperError("Bitget server timestamp is not an integer")
    return int(stamp)


def fetch_funding_events(symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    found: dict[int, dict[str, Any]] = {}
    for page in range(1, 6):
        payload, _ = get_json("/api/v2/mix/market/history-fund-rate", {
            "symbol": symbol, "productType": "USDT-FUTURES",
            "pageSize": "100", "pageNo": str(page),
        }, stage=f"funding_{symbol}")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise PaperError(f"{symbol}: Bitget funding history did not return a list")
        if not rows:
            break
        oldest = None
        for item in rows:
            if not isinstance(item, dict) or item.get("symbol") != symbol:
                raise PaperError(f"{symbol}: malformed Bitget funding record")
            event_ms = int(decimal_value(item.get("fundingTime"), "fundingTime", positive=True))
            oldest = event_ms if oldest is None else min(oldest, event_ms)
            if start_ms < event_ms <= end_ms:
                found[event_ms] = item
        if oldest is not None and oldest <= start_ms:
            break
        if len(rows) < 100:
            break
    else:
        raise PaperError(f"{symbol}: funding history exceeded the safe pagination limit")
    return [found[k] for k in sorted(found)]


def fetch_funding_mark(symbol: str, funding_ms: int) -> Decimal:
    # Bitget returns minute mark-price OHLC, not a separate exact settlement mark.
    # Use the 1m candle open when funding aligns with the candle; otherwise use
    # the last fully completed minute's close. The approximation is disclosed.
    minute = funding_ms // 60_000 * 60_000
    payload, _ = get_json("/api/v2/mix/market/history-mark-candles", {
        "symbol": symbol, "productType": "USDT-FUTURES", "granularity": "1m",
        "startTime": str(minute - 120_000), "endTime": str(minute + 120_000), "limit": "10",
    }, stage=f"funding_mark_{symbol}")
    candles = payload.get("data")
    if not isinstance(candles, list):
        raise PaperError(f"{symbol}: malformed historical mark-candle response")
    decoded = []
    for row in candles:
        if not isinstance(row, list) or len(row) < 5:
            raise PaperError(f"{symbol}: malformed historical mark-candle row")
        decoded.append((int(row[0]), decimal_value(row[1], "mark candle open", positive=True),
                        decimal_value(row[4], "mark candle close", positive=True)))
    exact = next((row for row in decoded if row[0] == funding_ms), None)
    if exact:
        return exact[1]
    prior = [row for row in decoded if row[0] + 60_000 <= funding_ms]
    if prior:
        return max(prior, key=lambda x: x[0])[2]
    raise PaperError(f"{symbol}: no historical one-minute mark candle supports funding event {funding_ms}")


def write_outputs(report: dict[str, Any], db_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "bitget_paper_report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    lines = [
        "# Bitget Stocks vs Crypto — Five-Minute Paper Monitor", "",
        f"- Status: **{report.get('status')}**",
        f"- Run UTC: {report.get('run_at_utc')}",
        f"- Ranking server timestamp: {report.get('as_of_server_time_ms')}",
        "- Market data: Bitget public unauthenticated GET only",
        "- Execution: simulated public-book VWAP; no orders submitted", "",
        "## Frozen selection rules", "",
        "- Long the highest 24-hour gainer among active USDT stock perpetuals.",
        "- Short the lowest 24-hour performer among active USDT crypto perpetuals.",
        "- One long and one short; target notional is 1× NAV per leg (2× gross).",
        "- Rebalance only after the persistent entry-time cooldown plus the required completed one-minute candle; a same-date idempotency check remains as a secondary safeguard.",
        "- Ranking uses Bitget's reported price24hPcnt; the raw decimal is multiplied by 100 for display.", "",
        "## Current ranking", "",
    ]
    for key, label in (("long_stock_top_gainer", "LONG stock"), ("short_crypto_bottom_loser", "SHORT crypto")):
        item = report.get("leaders", {}).get(key)
        if item:
            lines.append(f"- {label}: **{item.get('symbol')}** — {item.get('return_24h_pct')}% (source price24hPcnt={item.get('price24hPcnt')})")
    preflight = report.get("execution_preflight")
    if preflight:
        lines.extend(["", "## Read-only execution preflight", "",
            f"- Depth/lot-size gate: **{preflight.get('status')}**",
            f"- Scope: {preflight.get('scope', 'selected pair public depth check')}",
        ])
        for leg, item in preflight.get("legs", {}).items():
            lines.append(f"- {leg}: {item.get('symbol')} | target {item.get('estimated_entry_notional_usdt')} USDT | VWAP {item.get('estimated_entry_vwap')} | entry fee {item.get('estimated_entry_fee_usdt')} USDT | levels bid/ask {item.get('bid_levels')}/{item.get('ask_levels')}")
        failure = preflight.get("failure")
        if failure:
            lines.append(f"- Block: {failure.get('stage')} — {failure.get('message')}")
    lines.extend(["", "## Paper portfolio", ""])
    paper = report.get("paper")
    if paper:
        lines.append(f"- Paper accounting status: **{paper.get('status')}**")
        snap = paper.get("snapshot", {})
        if snap:
            lines.extend([
                f"- NAV: **{snap.get('nav_usdt')} USDT**",
                f"- Daily return: {snap.get('daily_return_pct')}%",
                f"- Cumulative portfolio ROI: **{snap.get('total_return_pct')}%**",
                f"- Realized gross P&L cumulative: {snap.get('realized_gross_pnl_cumulative_usdt')} USDT",
                f"- Unrealized P&L: {snap.get('unrealized_pnl_usdt')} USDT",
                f"- Fees cumulative: {snap.get('fees_cumulative_usdt')} USDT",
                f"- Funding cumulative: {snap.get('funding_cumulative_usdt')} USDT",
                f"- Gross exposure: {snap.get('gross_exposure_usdt')} USDT",
                f"- Hours since previous snapshot: {snap.get('hours_since_previous_snapshot')}",
            ])
            for leg, pos in snap.get("open_positions", {}).items():
                lines.append(f"- Open {leg}: {pos.get('symbol')} | trade ROI to date {pos.get('trade_roi_to_date_pct')}% | unrealized {pos.get('unrealized_pnl_usdt')} USDT")
            for trade in snap.get("closed_trades_today", []):
                lines.append(f"- Closed trade #{trade.get('trade_id')} {trade.get('leg')} {trade.get('symbol')}: net P&L {trade.get('net_pnl')} USDT; ROI {trade.get('trade_roi_pct')}%")
        if paper.get("message"):
            lines.append(f"- Note: {paper['message']}")
        closed_period = paper.get("closed_period_performance")
        if closed_period:
            lines.extend(["", "## Closed workflow-to-workflow performance period", "",
                f"- Actual period start (workflow ms): {closed_period.get('period_start_ms')}",
                f"- Actual period end (first post-reset workflow ms): {closed_period.get('period_end_ms')}",
                f"- Observed period duration: {closed_period.get('duration_ms')} ms",
                f"- Period ROI: {closed_period.get('period_roi_pct')}%",
                f"- Period MDD: {closed_period.get('period_mdd_pct')}%",
                f"- Minute-data coverage: {closed_period.get('coverage_pct')}%",
                f"- Measurement status: **{closed_period.get('status')}**",
                f"- Rebalance delay after measurement boundary: {closed_period.get('rebalance_delay_ms')} ms",
            ])
    elif report.get("failure"):
        lines.append("- No portfolio state transition was committed.")
    if report.get("portfolio"):
        lines.extend(["", "## ROI ledger status", "",
            f"- Portfolio status: {report['portfolio'].get('status')}",
            f"- Latest cumulative ROI: {report['portfolio'].get('portfolio_total_roi_pct')}%",
            f"- Closed trade records included: {len(report['portfolio'].get('closed_trades', []))}",
        ])
        risk = report["portfolio"].get("risk", {})
        strategy_risk = risk.get("strategy") or {}
        latest_risk = risk.get("latest_interval") or {}
        if strategy_risk:
            lines.extend([
                "", "## Intraday spread and drawdown", "",
                f"- Strategy-level historical MDD: **{strategy_risk.get('max_drawdown_pct')}%**",
                f"- MDD data quality: **{risk.get('quality_status')}**",
                f"- Latest holding-interval status: **{latest_risk.get('status')}**",
                f"- One-minute paired-bar coverage: {latest_risk.get('matched_minutes')}/{latest_risk.get('expected_minutes')} ({latest_risk.get('coverage_pct')}%)",
                f"- Synthetic spread ratio open / high / low / close: {latest_risk.get('spread_open_ratio')} / {latest_risk.get('spread_high_ratio')} / {latest_risk.get('spread_low_ratio')} / {latest_risk.get('spread_close_ratio')}",
                f"- Synthetic spread drawdown (low versus high): **{latest_risk.get('spread_drawdown_pct')}%**",
                f"- Intraday NAV low: {latest_risk.get('nav_min_intraday')} USDT",
                f"- Daily portfolio MDD: **{latest_risk.get('daily_mdd_pct')}%**",
                "- Spread uses synchronized one-minute market-price OHLC. Portfolio MDD uses one-minute mark-price OHLC and actual paper quantities, cash and accrued funding.",
                "- OHLC high/low envelopes are conservative intraminute estimates; candle data cannot reveal exact tick ordering. Missing minutes are flagged and are not used to update historical strategy MDD.",
            ])
    monitoring = report.get("monitoring")
    gate = report.get("rebalance_gate")
    if gate:
        lines.extend(["", "## Persistent rebalance safeguard", "",
            f"- Binary cooldown value: **{gate.get('cooldown')}**",
            f"- Gate: **{gate.get('reason')}**",
            f"- Activated at (UTC ms): {gate.get('activated_at_ms')}",
            f"- Scheduled reset after the +24h minute closes (UTC ms): {gate.get('eligible_after_ms')}",
            f"- First post-reset workflow boundary (UTC ms): {gate.get('last_reset_run_started_ms')}",
            f"- Seconds remaining at workflow start: {gate.get('seconds_remaining')}",
            "- A zero flag permits the first run that starts after reset to build the next pair from that run's refreshed feed.",
        ])
    if monitoring:
        lines.extend(["", "## Five-minute open-position monitoring", "",
            f"- Monitoring status: **{monitoring.get('status')}**",
            f"- Marked NAV: {monitoring.get('nav_usdt')} USDT | cumulative ROI: {monitoring.get('portfolio_roi_pct')}% | current-cycle ROI: {monitoring.get('cycle_roi_pct')}%",
            f"- Performance window start (workflow ms): {monitoring.get('cycle_start_ms')} | boundary (workflow ms): {monitoring.get('period_end_ms')}",
            f"- Pending estimated funding (not yet posted to accounting ledger): {monitoring.get('funding_pending_usdt')} USDT",
            f"- Synthetic spread mark: {monitoring.get('spread_ratio_mark')} | observed high/low: {monitoring.get('spread_high_ratio')} / {monitoring.get('spread_low_ratio')}",
            f"- Spread drawdown: {monitoring.get('spread_drawdown_pct')}% | holding-cycle MDD: {monitoring.get('daily_mdd_pct')}% | strategy MDD: {monitoring.get('strategy_mdd_pct')}%",
            f"- Minute coverage: {monitoring.get('matched_minutes')}/{monitoring.get('expected_minutes')} ({monitoring.get('coverage_pct')}%)",
            f"- Seconds to rebalance eligibility: {monitoring.get('seconds_to_rebalance')}",
        ])
        for leg, position in monitoring.get("open_positions", {}).items():
            lines.append(f"- {leg}: {position.get('symbol')} | mark {position.get('mark_price')} | unrealized {position.get('unrealized_pnl_usdt')} USDT | estimated net leg ROI {position.get('net_trade_roi_estimate_pct')}%")
        if monitoring.get("last_error"):
            lines.append(f"- Monitoring data warning: {monitoring.get('last_error')}")
    if report.get("failure"):
        failure = report["failure"]
        lines.extend(["", "## Failure / block", "",
            f"- Stage: {failure.get('stage')}", f"- HTTP status: {failure.get('http_status')}",
            f"- Detail: {failure.get('message')}",
            "- Existing SQLite state is preserved when market-data or paper accounting validation fails.",
        ])
    lines.extend(["", "## Model caveats", "",
        "- Fees use Bitget's public instrument takerFeeRate unless BITGET_TAKER_FEE_RATE overrides it; this is not a user-account fee quote.",
        "- Funding is calculated from public funding history and one-minute historical mark candles; settlement mark is minute-resolution, not an exact account funding record.",
        "- Book VWAP is a visible-depth simulation, not an actual execution. The model does not simulate liquidation, margin-tier changes, ADL or product eligibility.",
        "- Net ROI is calculated after modeled trading fees and funding; no slippage is deducted twice because it is embedded in VWAP.",
        "- No credentials, account endpoints, live/demo orders, or order submission are used.", "",
    ])
    (output_dir / "bitget_paper_report.md").write_text("\n".join(lines), encoding="utf-8")

    if db_path.exists():
        db = sqlite3.connect(str(db_path))
        try:
            with (output_dir / "closed_trades.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["trade_id","leg","symbol","entry_date","exit_date","entry_notional_usdt","gross_pnl_usdt","entry_fee_usdt","exit_fee_usdt","funding_pnl_usdt","net_pnl_usdt","trade_roi_pct"])
                for row in db.execute("""SELECT trade_id,leg,symbol,entry_date,exit_date,entry_notional,
                    gross_pnl,entry_fee,exit_fee,funding_pnl,net_pnl,trade_roi_pct
                    FROM trades WHERE status='CLOSED' ORDER BY trade_id"""):
                    writer.writerow(row)
            with (output_dir / "daily_nav.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["snapshot_date","nav_usdt","daily_return_pct","total_return_pct","realized_gross_pnl_cumulative_usdt","unrealized_pnl_usdt","fees_cumulative_usdt","funding_cumulative_usdt","gross_exposure_usdt","long_symbol","short_symbol"])
                for row in db.execute("""SELECT snapshot_date,nav,daily_return_pct,total_return_pct,
                    realized_gross_pnl_cumulative,unrealized_pnl,fees_cumulative,funding_cumulative,
                    gross_exposure,long_symbol,short_symbol FROM daily_snapshots ORDER BY snapshot_date"""):
                    writer.writerow(row)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "risk_intervals" in tables:
                with (output_dir / "daily_risk.csv").open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["snapshot_date","period_start_ms","period_end_ms","period_duration_ms",
                        "period_roi_pct","status","long_symbol","short_symbol","expected_minutes",
                        "matched_minutes","coverage_pct","spread_open_ratio","spread_high_ratio",
                        "spread_low_ratio","spread_close_ratio","spread_drawdown_pct","nav_min_intraday",
                        "period_mdd_pct","strategy_mdd_pct","nav_after_rebalance","error"])
                    for row in db.execute("""SELECT snapshot_date,period_start_ms,period_end_ms,
                        CASE WHEN period_start_ms IS NOT NULL THEN period_end_ms-period_start_ms ELSE NULL END,
                        period_roi_pct,status,long_symbol,short_symbol,expected_minutes,matched_minutes,
                        coverage_pct,spread_open_ratio,spread_high_ratio,spread_low_ratio,spread_close_ratio,
                        spread_drawdown_pct,nav_min_intraday,daily_mdd_pct,strategy_mdd_pct,nav_after_rebalance,error
                        FROM risk_intervals ORDER BY period_end_ms"""):
                        writer.writerow(row)
            if "monitor_observations" in tables:
                with (output_dir / "intraday_monitor.csv").open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["observed_at_ms","cycle_start_ms","eligible_after_ms","status","nav_usdt","portfolio_roi_pct","cycle_roi_pct","funding_pending_usdt","long_symbol","long_mark_price","long_unrealized_pnl_usdt","long_trade_roi_pct","short_symbol","short_mark_price","short_unrealized_pnl_usdt","short_trade_roi_pct","spread_ratio_mark","spread_high_ratio","spread_low_ratio","spread_drawdown_pct","daily_mdd_pct","strategy_mdd_pct","expected_minutes","matched_minutes","coverage_pct","seconds_to_rebalance","last_error"])
                    for row in db.execute("""SELECT observed_at_ms,cycle_start_ms,eligible_after_ms,status,nav_usdt,portfolio_roi_pct,cycle_roi_pct,funding_pending_usdt,long_symbol,long_mark_price,long_unrealized_pnl_usdt,long_trade_roi_pct,short_symbol,short_mark_price,short_unrealized_pnl_usdt,short_trade_roi_pct,spread_ratio_mark,spread_high_ratio,spread_low_ratio,spread_drawdown_pct,daily_mdd_pct,strategy_mdd_pct,expected_minutes,matched_minutes,coverage_pct,seconds_to_rebalance,last_error FROM monitor_observations ORDER BY observed_at_ms"""):
                        writer.writerow(row)
            if "monitor_minute_metrics" in tables:
                with (output_dir / "latest_minute_risk.csv").open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["timestamp_ms","long_symbol","short_symbol","spread_ratio_close",
                        "spread_ratio_high","spread_ratio_low","nav_close","nav_high","nav_low"])
                    for row in db.execute("""SELECT timestamp_ms,long_symbol,short_symbol,spread_ratio_close,
                        spread_ratio_high,spread_ratio_low,nav_close,nav_high,nav_low
                        FROM monitor_minute_metrics
                        WHERE cycle_start_ms=(SELECT MAX(cycle_start_ms) FROM monitor_minute_metrics)
                        ORDER BY timestamp_ms"""):
                        writer.writerow(row)
        finally:
            db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="state/bitget_paper.sqlite3")
    parser.add_argument("--output-dir", default="artifacts/bitget-paper-trading")
    parser.add_argument("--initial-nav-usdt", default=os.getenv("PAPER_INITIAL_NAV_USDT", "10"))
    parser.add_argument("--fee-rate", default=os.getenv("BITGET_TAKER_FEE_RATE"))
    parser.add_argument("--diagnostic-only", action="store_true", help="Rank and report only; do not modify paper state")
    args = parser.parse_args(argv)
    db_path = Path(args.db_path)
    out = Path(args.output_dir)
    # The workflow captures this before checkout/setup/tests. Local runs fall
    # back to this function's first timestamp before any market-data request.
    raw_started_ms = os.getenv("PAPER_WORKFLOW_STARTED_MS")
    try:
        run_started_ms = int(raw_started_ms) if raw_started_ms else int(datetime.now(timezone.utc).timestamp() * 1000)
    except (TypeError, ValueError) as exc:
        raise PaperError("PAPER_WORKFLOW_STARTED_MS must be an integer Unix-millisecond timestamp") from exc
    if run_started_ms <= 0:
        raise PaperError("Workflow start timestamp must be positive")
    report = build_ranking_report()
    report["paper"] = None
    report["monitoring"] = None
    report["portfolio"] = trade_report(db_path)
    override = D(args.fee_rate) if args.fee_rate not in (None, "") else None
    ranking_pass = report.get("gates", {}).get("ranking") == "PASS"

    if args.diagnostic_only:
        if ranking_pass:
            try:
                report["execution_preflight"] = read_only_execution_preflight(
                    report, D(args.initial_nav_usdt), override
                )
                report["status"] = "DIAGNOSTIC_ONLY_DEPTH_PASS"
            except Exception as exc:
                report["execution_preflight"] = {
                    "status": "BLOCKED",
                    "failure": {"stage": getattr(exc, "stage", "execution_preflight"),
                                "http_status": getattr(exc, "http_status", None),
                                "exception_type": type(exc).__name__, "message": str(exc)[:1600]},
                    "portfolio_state_changed": False,
                }
                report["status"] = "DIAGNOSTIC_ONLY_DEPTH_BLOCKED"
        else:
            report["status"] = "RANKING_BLOCKED"
    else:
        # Read the durable cooldown before monitoring. The first job start after
        # reset fixes the performance-window end, even if fresh ranking/depth
        # validation fails and the portfolio construction must be retried later.
        open_trades = report.get("portfolio", {}).get("open_trades", [])
        if open_trades:
            gate = rebalance_gate(db_path, run_started_ms)
            fixed_end = gate.get("last_reset_run_started_ms")
            performance_end_ms = (
                int(fixed_end) if int(gate.get("cooldown", 1)) == 0 and fixed_end is not None
                else run_started_ms
            )
            try:
                server_ms = report.get("as_of_server_time_ms") or report.get("api", {}).get("server_time_ms")
                if not isinstance(server_ms, int) or server_ms <= 0:
                    server_ms = fetch_server_time_ms()
                report.setdefault("api", {})["server_time_ms"] = server_ms
                report["as_of_server_time_ms"] = server_ms
                monitored = monitor_open_positions(
                    db_path,
                    now_ms=server_ms,
                    initial_nav=D(args.initial_nav_usdt),
                    ticker_fetch=fetch_monitor_tickers,
                    metrics_end_ms=performance_end_ms,
                    funding_fetch=fetch_funding_events,
                    funding_mark_fetch=fetch_funding_mark,
                    candle_fetch=fetch_1m_candles,
                )
                report["monitoring"] = monitored
            except Exception as exc:
                report["monitoring"] = {
                    "status": "MONITORING_BLOCKED",
                    "analytics_updated": False,
                    "portfolio_state_changed": False,
                    "last_error": f"{type(exc).__name__}: {str(exc)[:1200]}",
                }

            report["portfolio"] = trade_report(db_path)
            gate["exchange_time_ms"] = report.get("as_of_server_time_ms") or report.get("api", {}).get("server_time_ms")
            report["rebalance_gate"] = gate

            if gate.get("can_rebalance") and ranking_pass:
                try:
                    paper = run_snapshot(
                        report, db_path, depth_fetch=fetch_depth,
                        funding_fetch=fetch_funding_events,
                        funding_mark_fetch=fetch_funding_mark,
                        candle_fetch=fetch_1m_candles,
                        initial_nav=D(args.initial_nav_usdt),
                        fee_override=override,
                        run_started_ms=run_started_ms,
                    )
                    report["paper"] = paper
                    report["status"] = "PAPER_UPDATED" if paper.get("updated") else paper.get("status", "PAPER_SKIPPED")
                    report["portfolio"] = trade_report(db_path)
                except Exception as exc:
                    report["status"] = "REBALANCE_BLOCKED"
                    report["failure"] = {
                        "stage": getattr(exc, "stage", "paper_accounting"),
                        "http_status": getattr(exc, "http_status", None),
                        "exception_type": type(exc).__name__,
                        "message": str(exc)[:1600],
                        "portfolio_state_changed": False,
                    }
                    report["portfolio"] = trade_report(db_path)
            elif not gate.get("can_rebalance"):
                report["status"] = "MONITORING_ONLY" if report["monitoring"].get("analytics_updated") else "MONITORING_WARNING"
                if not ranking_pass:
                    report["ranking_warning"] = report.get("failure")
            else:
                report["status"] = "REBALANCE_DUE_RANKING_BLOCKED"
                report["ranking_warning"] = report.get("failure")
        elif ranking_pass:
            # First scheduled paper run bootstraps the portfolio and starts its lock.
            try:
                paper = run_snapshot(
                    report, db_path, depth_fetch=fetch_depth,
                    funding_fetch=fetch_funding_events,
                    funding_mark_fetch=fetch_funding_mark,
                    candle_fetch=fetch_1m_candles,
                    initial_nav=D(args.initial_nav_usdt),
                    fee_override=override,
                    run_started_ms=run_started_ms,
                )
                report["paper"] = paper
                report["status"] = "PAPER_UPDATED" if paper.get("updated") else paper.get("status", "PAPER_SKIPPED")
                report["portfolio"] = trade_report(db_path)
            except Exception as exc:
                report["status"] = "PAPER_BLOCKED"
                report["failure"] = {
                    "stage": getattr(exc, "stage", "paper_accounting"),
                    "http_status": getattr(exc, "http_status", None),
                    "exception_type": type(exc).__name__,
                    "message": str(exc)[:1600],
                    "portfolio_state_changed": False,
                }
        else:
            report["status"] = "RANKING_BLOCKED"

    report["run_started_at_utc"] = datetime.fromtimestamp(
        run_started_ms / 1000, timezone.utc
    ).isoformat().replace("+00:00", "Z")
    report["run_started_ms"] = run_started_ms
    report["run_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Lifetime equity starts at the fixed initial NAV and appends each successful mark/rebalance.
    if db_path.exists():
        try:
            ensure_equity_start(db_path, run_started_ms)
            paper_snapshot = (report.get("paper") or {}).get("snapshot") or {}
            monitored = report.get("monitoring") or {}
            if (report.get("paper") or {}).get("updated") and paper_snapshot.get("nav_usdt") is not None:
                point, source = paper_snapshot, "REBALANCE"
                pos = point.get("open_positions") or {}
            elif monitored.get("nav_usdt") is not None:
                point, source = monitored, "MONITOR"
                pos = point.get("open_positions") or {}
            else:
                point, source, pos = None, None, {}
            if point is not None:
                latest_monitor = (report.get("portfolio") or {}).get("latest_monitor") or {}
                record_equity_point(
                    db_path, workflow_started_ms=run_started_ms,
                    valuation_time_ms=report.get("as_of_server_time_ms"),
                    nav_usdt=point.get("nav_usdt"), source=source,
                    cash_usdt=point.get("cash_usdt", latest_monitor.get("cash_usdt")),
                    unrealized_pnl_usdt=point.get("unrealized_pnl_usdt", latest_monitor.get("unrealized_pnl_usdt")),
                    gross_exposure_usdt=point.get("gross_exposure_usdt", latest_monitor.get("gross_exposure_usdt")),
                    long_symbol=(pos.get("LONG") or {}).get("symbol"),
                    short_symbol=(pos.get("SHORT") or {}).get("symbol"),
                )
            perf = performance_summary(db_path)
            report["performance"] = {k:v for k,v in perf.items()
                                     if k not in {"equity_points","daily_closes","daily_returns","trades"}}
            export_performance_artifacts(db_path, out)
        except Exception as exc:
            # The reporting layer never undoes committed paper accounting/monitoring.
            report["performance_warning"] = f"{type(exc).__name__}: {str(exc)[:800]}"

    write_outputs(report, db_path, out)
    print(json.dumps({
        "status": report.get("status"), "gates": report.get("gates"),
        "leaders": report.get("leaders"), "monitoring": report.get("monitoring"),
        "rebalance_gate": report.get("rebalance_gate"), "paper": report.get("paper"),
        "portfolio_roi_pct": report.get("portfolio", {}).get("portfolio_total_roi_pct"),
        "strategy_mdd_pct": (report.get("portfolio", {}).get("risk", {}).get("strategy") or {}).get("max_drawdown_pct"),
        "risk_quality": report.get("portfolio", {}).get("risk", {}).get("quality_status"),
        "failure": report.get("failure"), "orders": "NONE",
    }, indent=2, sort_keys=True, default=str))
    if report["status"] in {
        "PAPER_BLOCKED", "RANKING_BLOCKED", "DIAGNOSTIC_ONLY_DEPTH_BLOCKED",
    }:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
