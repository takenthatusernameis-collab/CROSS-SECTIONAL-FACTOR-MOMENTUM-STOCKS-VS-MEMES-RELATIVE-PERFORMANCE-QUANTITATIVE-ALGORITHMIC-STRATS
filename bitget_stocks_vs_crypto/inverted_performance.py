"""Retrospective opposite-side equity curve for the existing Bitget paper campaign.

The counterfactual keeps the original selected contracts, quantities and timestamps,
reverses each leg's direction, retains recorded transaction fees, and reverses funding.
"""
from __future__ import annotations

import csv
import html
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

D = Decimal


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        result = D(str(value if value is not None else default))
        return result if result.is_finite() else D(default)
    except (InvalidOperation, ValueError, TypeError):
        return D(default)


def _utc(timestamp_ms: Any) -> str:
    return datetime.fromtimestamp(int(timestamp_ms) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def build_inverted_equity(db_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Rebuild an opposite-side curve from the existing paper ledger; never mutates SQLite."""
    path = Path(db_path)
    db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"account", "trades", "equity_curve", "monitor_observations", "daily_snapshots"}
        missing = required - tables
        if missing:
            raise RuntimeError(f"Cannot build inverted curve; missing tables: {sorted(missing)}")
        account = db.execute("SELECT initial_nav FROM account WHERE id=1").fetchone()
        if account is None:
            raise RuntimeError("Cannot build inverted curve; paper account is not initialized")
        initial_nav = _dec(account["initial_nav"])
        trades = [dict(row) for row in db.execute("SELECT * FROM trades ORDER BY entry_time_ms,trade_id")]
        points = [dict(row) for row in db.execute("SELECT * FROM equity_curve ORDER BY timestamp_ms")]
        observations = {
            int(row["observed_at_ms"]): dict(row)
            for row in db.execute("SELECT * FROM monitor_observations")
        }
        snapshots = {
            int(row["as_of_ms"]): dict(row)
            for row in db.execute("SELECT * FROM daily_snapshots")
        }
    finally:
        db.close()

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    curve: list[dict[str, Any]] = []

    for point in points:
        valuation_ms = int(point.get("valuation_time_ms") or point["timestamp_ms"])
        observation = observations.get(valuation_ms)
        snapshot = snapshots.get(valuation_ms)
        snapshot_positions: dict[str, dict[str, Any]] = {}
        if snapshot:
            for leg, key in (("LONG", "long_position_json"), ("SHORT", "short_position_json")):
                try:
                    snapshot_positions[leg] = json.loads(snapshot.get(key) or "{}")
                except (TypeError, ValueError):
                    snapshot_positions[leg] = {}

        realized_net = D("0")
        open_entry_fees = D("0")
        unrealized = D("0")
        gross_exposure = D("0")
        open_legs = 0

        for trade in trades:
            entry_ms = int(trade["entry_time_ms"])
            if entry_ms > valuation_ms:
                continue

            entry_fee = _dec(trade.get("entry_fee"))
            exit_ms = trade.get("exit_time_ms")
            if exit_ms is not None and int(exit_ms) <= valuation_ms:
                # Reverse directional P&L and funding; transaction fees remain costs.
                realized_net += (
                    -_dec(trade.get("gross_pnl"))
                    -_dec(trade.get("funding_pnl"))
                    -entry_fee
                    -_dec(trade.get("exit_fee"))
                )
                continue

            # An open position: its entry fee is already incurred.
            open_entry_fees += entry_fee
            open_legs += 1
            leg = str(trade["leg"])
            symbol = str(trade["symbol"])
            quantity = _dec(trade["entry_qty"])
            entry_price = _dec(trade["entry_price"])

            if observation and observation.get("long_symbol" if leg == "LONG" else "short_symbol") == symbol:
                mark = _dec(
                    observation.get("long_mark_price" if leg == "LONG" else "short_mark_price"),
                    str(entry_price),
                )
            elif snapshot_positions.get(leg, {}).get("symbol") == symbol:
                mark = _dec(snapshot_positions[leg].get("mark_price"), str(entry_price))
            else:
                # Rebalance/initial points use entry price when no contemporaneous mark was saved.
                mark = entry_price

            original_direction = D("1") if leg == "LONG" else D("-1")
            original_gross = quantity * (mark - entry_price) * original_direction
            unrealized -= original_gross
            gross_exposure += quantity * mark

        # Pending funding is not yet in the original ledger; reverse its direction here.
        reversed_pending_funding = -_dec(observation.get("funding_pending_usdt")) if observation else D("0")
        cash = initial_nav + realized_net - open_entry_fees + reversed_pending_funding
        nav = cash + unrealized

        curve.append({
            "workflow_start_ms": int(point["timestamp_ms"]),
            "workflow_start_utc": _utc(point["timestamp_ms"]),
            "valuation_ms": valuation_ms,
            "valuation_utc": _utc(valuation_ms),
            "event_type": point["event_type"],
            "nav_usdt": nav,
            "cash_usdt": cash,
            "unrealized_pnl_usdt": unrealized,
            "gross_exposure_usdt": gross_exposure,
            "portfolio_roi_pct": (nav / initial_nav - D("1")) * D("100"),
            "open_trade_legs": open_legs,
            # Labels describe the reversed positions, not a changed ranking.
            "long_symbol": point.get("short_symbol"),
            "short_symbol": point.get("long_symbol"),
            "funding_pending_reversed_usdt": reversed_pending_funding,
        })

    # Keep one deterministic observation per original workflow-start timestamp.
    by_timestamp = {row["workflow_start_ms"]: row for row in curve}
    curve = [by_timestamp[key] for key in sorted(by_timestamp)]

    peak: Decimal | None = None
    max_drawdown = D("0")
    for row in curve:
        peak = row["nav_usdt"] if peak is None else max(peak, row["nav_usdt"])
        if peak > 0:
            max_drawdown = min(max_drawdown, (row["nav_usdt"] / peak - D("1")) * D("100"))

    daily_closes: dict[str, dict[str, Any]] = {}
    for row in curve:
        day = datetime.fromtimestamp(row["workflow_start_ms"] / 1000, timezone.utc).date().isoformat()
        daily_closes[day] = row
    daily_rows = []
    previous_nav: Decimal | None = None
    for day, row in sorted(daily_closes.items()):
        daily_return = (
            (row["nav_usdt"] / previous_nav - D("1")) * D("100")
            if previous_nav is not None and previous_nav > 0 else None
        )
        daily_rows.append((day, row["workflow_start_utc"], row["nav_usdt"], daily_return))
        previous_nav = row["nav_usdt"]

    latest_nav = curve[-1]["nav_usdt"] if curve else initial_nav
    metrics = {
        "status": "OK",
        "method": "opposite-side counterfactual on the same selected contracts, timestamps and original quantities",
        "initial_nav_usdt": str(initial_nav),
        "latest_nav_usdt": str(latest_nav),
        "latest_pnl_usdt": str(latest_nav - initial_nav),
        "latest_roi_pct": str((latest_nav / initial_nav - D("1")) * D("100")),
        "max_drawdown_pct": str(max_drawdown),
        "observation_count": len(curve),
        "closed_trade_count": sum(1 for trade in trades if trade.get("status") == "CLOSED"),
        "open_trade_count": sum(1 for trade in trades if trade.get("status") == "OPEN"),
        "costs": "recorded entry/exit fees retained; posted and pending funding cashflows sign-reversed",
        "execution_limit": (
            "Original paper fill prices and quantities are reused for both directions. Historical "
            "opposite-side order-book VWAP was not stored, so incremental bid/ask spread impact "
            "for the inverted fills cannot be reconstructed."
        ),
    }

    with (output / "inverted_equity_curve.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "workflow_start_utc", "workflow_start_ms", "valuation_utc", "event_type",
            "nav_usdt", "cash_usdt", "unrealized_pnl_usdt", "gross_exposure_usdt",
            "portfolio_roi_pct", "open_trade_legs", "long_symbol", "short_symbol",
            "funding_pending_reversed_usdt",
        ])
        for row in curve:
            writer.writerow([
                row["workflow_start_utc"], row["workflow_start_ms"], row["valuation_utc"],
                row["event_type"], row["nav_usdt"], row["cash_usdt"], row["unrealized_pnl_usdt"],
                row["gross_exposure_usdt"], row["portfolio_roi_pct"], row["open_trade_legs"],
                row["long_symbol"], row["short_symbol"], row["funding_pending_reversed_usdt"],
            ])

    with (output / "inverted_daily_equity.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["utc_date", "close_timestamp_utc", "nav_usdt", "close_to_close_return_pct"])
        writer.writerows(daily_rows)

    (output / "inverted_performance_metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_svg(curve, output / "inverted_equity_curve.svg")
    return metrics


def _write_svg(curve: list[dict[str, Any]], path: Path) -> None:
    width, height = 1000, 360
    left, right, top, bottom = 84, 28, 42, 58
    plot_width, plot_height = width - left - right, height - top - bottom
    values = [row["nav_usdt"] for row in curve]
    if not values:
        body = '<text x="500" y="180" text-anchor="middle">No equity observations</text>'
    else:
        low, high = min(values), max(values)
        padding = (high - low) * D("0.08") if high != low else max(abs(high) * D("0.02"), D("0.1"))
        low, high = low - padding, high + padding
        coordinates = [
            f"{left + plot_width * i / max(1, len(values) - 1):.2f},"
            f"{top + plot_height * float(high - value) / float(high - low):.2f}"
            for i, value in enumerate(values)
        ]
        body = (
            f'<line x1="{left}" y1="{top + plot_height}" x2="{width-right}" y2="{top + plot_height}" stroke="#777"/>'
            f'<polyline points="{" ".join(coordinates)}" fill="none" stroke="#d97706" stroke-width="3" '
            'stroke-linejoin="round"/>'
            f'<text x="{left}" y="{top-12}" font-size="14">Start: {html.escape(format(values[0], ".4f"))} USDT</text>'
            f'<text x="{width-right}" y="{top-12}" text-anchor="end" font-size="14">'
            f'Latest: {html.escape(format(values[-1], ".4f"))} USDT</text>'
            f'<text x="{left}" y="{height-18}" font-size="12">{_utc(curve[0]["workflow_start_ms"])}</text>'
            f'<text x="{width-right}" y="{height-18}" text-anchor="end" font-size="12">'
            f'{_utc(curve[-1]["workflow_start_ms"])}</text>'
        )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        '<rect width="100%" height="100%" fill="#fff"/>'
        '<text x="500" y="24" text-anchor="middle" font-size="18" font-family="Arial">'
        'Inverted Positions — Counterfactual Equity Curve</text>'
        f'<g font-family="Arial" fill="#222">{body}</g></svg>\n'
    )
    path.write_text(svg, encoding="utf-8")
