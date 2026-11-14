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
from bitget_stocks_vs_crypto.portfolio import (PaperError, fee_rate_for, parse_book, run_snapshot, size_for_target_notional, trade_report)


def fetch_depth(symbol: str) -> dict[str, Any]:
    payload, _ = get_json("/api/v2/mix/market/merge-depth", {
        "symbol": symbol, "productType": "USDT-FUTURES", "limit": "max",
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
        "# Bitget Stocks vs Crypto — Daily Paper Portfolio", "",
        f"- Status: **{report.get('status')}**",
        f"- Run UTC: {report.get('run_at_utc')}",
        f"- Ranking server timestamp: {report.get('as_of_server_time_ms')}",
        "- Market data: Bitget public unauthenticated GET only",
        "- Execution: simulated public-book VWAP; no orders submitted", "",
        "## Frozen selection rules", "",
        "- Long the highest 24-hour gainer among active USDT stock perpetuals.",
        "- Short the lowest 24-hour performer among active USDT crypto perpetuals.",
        "- One long and one short; target notional is 1× NAV per leg (2× gross).",
        "- Rebalance at most once per UTC date. A failed liquidity/data gate does not select a runner-up.",
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
    elif report.get("failure"):
        lines.append("- No portfolio state transition was committed.")
    if report.get("portfolio"):
        lines.extend(["", "## ROI ledger status", "",
            f"- Portfolio status: {report['portfolio'].get('status')}",
            f"- Latest cumulative ROI: {report['portfolio'].get('portfolio_total_roi_pct')}%",
            f"- Closed trade records included: {len(report['portfolio'].get('closed_trades', []))}",
        ])
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
        finally:
            db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="state/bitget_paper.sqlite3")
    parser.add_argument("--output-dir", default="artifacts/bitget-paper-trading")
    parser.add_argument("--initial-nav-usdt", default=os.getenv("PAPER_INITIAL_NAV_USDT", "10000"))
    parser.add_argument("--fee-rate", default=os.getenv("BITGET_TAKER_FEE_RATE"))
    parser.add_argument("--diagnostic-only", action="store_true", help="Rank and report only; do not modify paper state")
    args = parser.parse_args(argv)
    db_path = Path(args.db_path)
    out = Path(args.output_dir)
    report = build_ranking_report()
    report["paper"] = None
    report["portfolio"] = trade_report(db_path)
    override = D(args.fee_rate) if args.fee_rate not in (None, "") else None
    if report.get("gates", {}).get("ranking") == "PASS" and args.diagnostic_only:
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
    elif report.get("gates", {}).get("ranking") == "PASS":
        try:
            paper = run_snapshot(report, db_path, depth_fetch=fetch_depth,
                funding_fetch=fetch_funding_events, funding_mark_fetch=fetch_funding_mark,
                initial_nav=D(args.initial_nav_usdt), fee_override=override)
            report["paper"] = paper
            report["status"] = "PAPER_UPDATED" if paper.get("updated") else paper.get("status", "PAPER_SKIPPED")
            report["portfolio"] = trade_report(db_path)
        except Exception as exc:
            report["status"] = "PAPER_BLOCKED"
            report["failure"] = {"stage": getattr(exc, "stage", "paper_accounting"),
                                 "http_status": getattr(exc, "http_status", None),
                                 "exception_type": type(exc).__name__, "message": str(exc)[:1600],
                                 "portfolio_state_changed": False}
    else:
        report["status"] = "RANKING_BLOCKED"
    report["run_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    write_outputs(report, db_path, out)
    print(json.dumps({"status":report.get("status"),"gates":report.get("gates"),
        "leaders":report.get("leaders"),"paper":report.get("paper"),
        "portfolio_roi_pct":report.get("portfolio",{}).get("portfolio_total_roi_pct"),
        "failure":report.get("failure"),"orders":"NONE"},indent=2,sort_keys=True,default=str))
    if report["status"] in {"PAPER_BLOCKED","RANKING_BLOCKED","DIAGNOSTIC_ONLY_DEPTH_BLOCKED"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
