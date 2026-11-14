#!/usr/bin/env python3
"""Render a read-only, human-friendly STATUS.md from the Bitget paper SQLite ledger."""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from bitget_stocks_vs_crypto.performance import performance_summary, export_performance_artifacts


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def stamp(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "—"


def fmt(value: Any, places: int = 2) -> str:
    try:
        n = Decimal(str(value))
        if not n.is_finite():
            return "—"
        return format(n.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP), ",f")
    except (InvalidOperation, TypeError, ValueError):
        return "—"


def pct(value: Any) -> str:
    result = fmt(value)
    return f"{result}%" if result != "—" else "Not available"


def cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_status(db_path: str | Path, *, workflow_started_ms: int | None = None,
                  generated_at_ms: int | None = None, run_id: str | None = None,
                  event_name: str | None = None, mode: str | None = None,
                  repository: str | None = None, report_json: str | Path | None = None) -> str:
    """Render STATUS.md without modifying the SQLite database."""
    run: dict[str, Any] = {}
    report_warning = None
    if report_json:
        try:
            run = json.loads(Path(report_json).read_text(encoding="utf-8"))
            if not isinstance(run, dict):
                run, report_warning = {}, "Latest runner report is not a JSON object."
        except (OSError, json.JSONDecodeError) as exc:
            report_warning = f"Latest runner report unavailable: {type(exc).__name__}"

    path = Path(db_path)
    if not path.is_file() or not path.stat().st_size:
        raise RuntimeError(f"Missing or empty paper database: {path}")
    db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"account", "positions", "trades", "daily_snapshots", "cooldown_control",
                    "monitor_state", "monitor_observations", "monitor_minute_metrics",
                    "strategy_risk", "risk_intervals"}
        missing = required - tables
        if missing:
            raise RuntimeError(f"Missing SQLite tables: {', '.join(sorted(missing))}")
        one = lambda sql, args=(): db.execute(sql, args).fetchone()
        account = one("SELECT * FROM account WHERE id=1")
        if account is None:
            raise RuntimeError("Paper account is not initialized")
        snapshot = one("SELECT * FROM daily_snapshots ORDER BY as_of_ms DESC LIMIT 1")
        control = one("SELECT * FROM cooldown_control WHERE id=1")
        cycle = one("SELECT * FROM monitor_state WHERE id=1")
        risk = one("SELECT * FROM strategy_risk WHERE id=1")
        interval = one("SELECT * FROM risk_intervals ORDER BY period_end_ms DESC LIMIT 1")
        positions = db.execute("""SELECT p.*,t.entry_time_ms,t.entry_notional AS trade_entry_notional
            FROM positions p JOIN trades t ON t.trade_id=p.trade_id ORDER BY p.leg""").fetchall()

        observation = None
        if cycle is not None:
            observation = one("""SELECT * FROM monitor_observations WHERE cycle_start_ms=?
                               ORDER BY observed_at_ms DESC LIMIT 1""", (int(cycle["cycle_start_ms"]),))
        symbols = {p["leg"]: p["symbol"] for p in positions}
        if observation and (observation["long_symbol"] != symbols.get("LONG") or
                            observation["short_symbol"] != symbols.get("SHORT")):
            observation = None

        snap_positions = {}
        if snapshot:
            for leg, key in (("LONG", "long_position_json"), ("SHORT", "short_position_json")):
                try:
                    snap_positions[leg] = json.loads(snapshot[key])
                except (KeyError, TypeError, json.JSONDecodeError):
                    snap_positions[leg] = {}

        if observation:
            nav, unrealized = observation["nav_usdt"], observation["unrealized_pnl_usdt"]
            roi, cycle_roi = observation["portfolio_roi_pct"], observation["cycle_roi_pct"]
            exposure, pending = observation["gross_exposure_usdt"], observation["funding_pending_usdt"]
            asof, source = observation["observed_at_ms"], "live mark-to-market observation"
        elif snapshot:
            nav, unrealized = snapshot["nav"], snapshot["unrealized_pnl"]
            roi, cycle_roi = snapshot["total_return_pct"], None
            exposure, pending = snapshot["gross_exposure"], "0"
            asof, source = snapshot["as_of_ms"], "entry-time portfolio snapshot (awaiting first monitor mark)"
        else:
            nav, unrealized, roi, cycle_roi = account["cash"], "0", None, None
            exposure, pending, asof, source = "0", "0", None, "cash only; no portfolio snapshot"

        started = generated_at_ms or int(datetime.now(timezone.utc).timestamp() * 1000)
        initial = Decimal(str(account["initial_nav"]))
        nav_change = Decimal(str(nav)) - initial
        expected = int(cycle["expected_minutes"]) if cycle else 0
        matched = int(cycle["matched_minutes"]) if cycle else 0
        stored_minutes = int(one("SELECT COUNT(*) FROM monitor_minute_metrics WHERE cycle_start_ms=?",
                                 (int(cycle["cycle_start_ms"]),))[0]) if cycle else 0
        coverage = Decimal(matched) / Decimal(expected) * 100 if expected else Decimal(0)
        monitor_status = str(cycle["status"]) if cycle else "NO_MONITOR_STATE"
        quality = ("NO_MINUTE_HISTORY" if not expected else
                   "COMPLETE_CURRENT_CYCLE" if monitor_status == "MONITORING" and matched == expected and stored_minutes == matched
                   else "PARTIAL_CURRENT_CYCLE")
        complete = int(risk["complete_intervals"]) if risk else 0
        incomplete = int(risk["incomplete_intervals"]) if risk else 0
        strategy_mdd = risk["max_drawdown_pct"] if risk and (complete or stored_minutes) else None
        cycle_mdd = cycle["daily_mdd_pct"] if cycle and stored_minutes else None
        monitor_report = run.get("monitoring") if isinstance(run.get("monitoring"), dict) else {}
        failure = run.get("failure") if isinstance(run.get("failure"), dict) else {}
        warning = (monitor_report.get("last_error") or failure.get("message") or
                   (observation["last_error"] if observation else None) or (cycle["last_error"] if cycle else None))
        cooldown = ("UNKNOWN" if control is None else
                    "ACTIVE (1)" if int(control["cooldown"]) == 1 else "READY (0)")
        age = max(0, (started - int(asof)) // 60_000) if asof is not None else None
        closed_count = int(one("SELECT COUNT(*) FROM trades WHERE status='CLOSED'")[0])
        performance = performance_summary(path)
        lifetime_pnl = _dec(nav) - initial
        equity_points = performance.get("equity_points", [])
        trade_rows = performance.get("trades", [])

        position_lines = ["No open positions."]
        if positions:
            position_lines = ["| Leg | Contract | Quantity | Entry | Latest mark | Unrealized P&L | Trade ROI |",
                              "|---|---|---:|---:|---:|---:|---:|"]
            for p in positions:
                leg = p["leg"]
                saved = snap_positions.get(leg, {})
                if observation:
                    mark = observation["long_mark_price"] if leg == "LONG" else observation["short_mark_price"]
                    pnl = observation["long_unrealized_pnl_usdt"] if leg == "LONG" else observation["short_unrealized_pnl_usdt"]
                    leg_roi = observation["long_trade_roi_pct"] if leg == "LONG" else observation["short_trade_roi_pct"]
                else:
                    mark = saved.get("mark_price", p["entry_price"])
                    pnl, leg_roi = saved.get("unrealized_pnl_usdt", "0"), saved.get("trade_roi_to_date_pct")
                position_lines.append(
                    f"| {leg} | {cell(p['symbol'])} | {fmt(p['qty'], 6)} | {fmt(p['entry_price'], 8)} | "
                    f"{fmt(mark, 8)} | {fmt(pnl)} USDT | {pct(leg_roi)} |"
                )

        trigger = (event_name or "unknown") + (f" / {mode}" if mode else "")
        run_link = (f"[{cell(run_id)}](https://github.com/{repository}/actions/runs/{run_id})"
                    if run_id and repository else cell(run_id or "—"))
        lines = [
            "# Bitget Stocks vs Crypto — Paper Portfolio Status", "",
            "> **Paper-only simulation.** No exchange orders are placed. Market data comes from Bitget public endpoints.", "",
            "## At a glance", "",
            "| Metric | Latest value |", "|---|---:|",
            f"| Current NAV | **{fmt(nav)} USDT** |", f"| Starting capital | {fmt(initial)} USDT |",
            f"| NAV change vs starting capital | **{fmt(nav_change)} USDT** |",
            f"| Portfolio ROI vs starting capital | **{pct(roi)}** |", f"| Current-cycle ROI | {pct(cycle_roi)} |",
            f"| Unrealized P&L (gross) | {fmt(unrealized)} USDT |",
            f"| Cumulative realized gross P&L | {fmt(account['realized_gross_pnl'])} USDT |",
            f"| Fees paid to date | {fmt(account['fees_cumulative'])} USDT |",
            f"| Funding posted to ledger | {fmt(account['funding_cumulative'])} USDT |",
            f"| Estimated funding pending | {fmt(pending)} USDT |", f"| Gross exposure | {fmt(exposure)} USDT |",
            f"| Closed trades | {closed_count} |", "",
            f"_NAV source: {source}. Values as of {stamp(asof)}._", "",
            "## Latest workflow result", "",
            f"- **Paper runner status:** **{cell(run.get('status', 'not supplied'))}**",
            f"- **Ranking data gate:** {cell((run.get('gates') or {}).get('ranking', 'not supplied'))}",
            f"- **Monitoring operation:** {cell(monitor_report.get('status', 'not supplied'))}",
            f"- **Age of latest saved mark/snapshot:** {age} minutes" if age is not None else "- **Age of latest saved mark/snapshot:** not available",
        ]
        if report_warning:
            lines.append(f"- **Runner report warning:** {cell(report_warning)}")
        sortino_value = performance.get("sortino")
        sortino_display = f"{float(sortino_value):.2f}" if sortino_value is not None else "Not yet statistically available"
        trade_history_lines = [
            "| Trade | Entry date | Leg | Contract | Status | Entry price | Exit price | Net P&L | Trade ROI |",
            "|---:|---|---|---|---|---:|---:|---:|---:|",
        ]
        if trade_rows:
            for trade in trade_rows:
                trade_history_lines.append(
                    f"| {trade.get('trade_id')} | {cell(trade.get('entry_date') or '—')} | "
                    f"{cell(trade.get('leg') or '—')} | {cell(trade.get('symbol') or '—')} | "
                    f"{cell(trade.get('status') or '—')} | {fmt(trade.get('entry_price'), 8)} | "
                    f"{fmt(trade.get('exit_price'), 8)} | "
                    f"{fmt(trade.get('net_pnl')) if trade.get('net_pnl') is not None else '—'} USDT | "
                    f"{pct(trade.get('trade_roi_pct'))} |"
                )
        else:
            trade_history_lines.append("| — | — | — | — | No trades yet | — | — | — | — |")
        lines.extend(["", "## Lifetime strategy performance", "",
            f"- **Initial capital:** {fmt(initial)} USDT",
            f"- **Current equity P&L vs initial capital:** **{fmt(lifetime_pnl)} USDT**",
            f"- **Lifetime portfolio ROI:** **{pct(roi)}**",
            f"- **Equity-curve observations:** {len(equity_points)}",
            f"- **Closed trades:** {performance.get('closed_trade_count', 0)} | **Open trades:** {performance.get('open_trade_count', 0)}",
            f"- **Realized net P&L on closed trades:** {fmt(performance.get('closed_net_pnl_usdt'))} USDT",
            f"- **Closed-trade win rate:** {pct(performance.get('win_rate_pct'))}",
            f"- **Profit factor:** {performance.get('profit_factor') if performance.get('profit_factor') is not None else 'Not available'}",
            f"- **Annualized Sortino ratio:** **{sortino_display}**",
            f"- **Sortino status:** {cell(performance.get('sortino_status', 'No equity history'))}",
            f"- **Sortino method:** consecutive UTC daily equity closes, 0% minimum acceptable return, annualized by √365; requires at least {performance.get('sortino_min_daily_returns', 20)} valid daily returns and {performance.get('sortino_min_downside_days', 5)} negative days.",
            f"- **Sampled equity-curve MDD:** {pct(performance.get('equity_curve_mdd_pct'))}",
            "", "![Portfolio equity curve](equity_curve.svg)",
            "[Complete equity curve CSV](equity_curve.csv) · [Daily equity and returns](daily_equity.csv) · [Complete trade history](trade_history.csv)",
            "", "### Full trade history", "", *trade_history_lines,
            "", "Sortino remains unavailable until enough daily observations exist; no ratio is inferred from a short initial sample.", ""
        ])
        lines.extend(["", "## Open positions", "", *position_lines, "", "## Risk", "",
            f"- **Strategy maximum drawdown:** {pct(strategy_mdd)}" + (" (observed minute history)" if strategy_mdd is not None else " (not established from minute history yet)"),
            f"- **Current holding-cycle MDD:** {pct(cycle_mdd)}" + (" (observed bars; cycle may be partial)" if cycle_mdd is not None and quality != "COMPLETE_CURRENT_CYCLE" else ("" if cycle_mdd is not None else " (not established yet)")),
            f"- **Minute-data coverage:** {matched}/{expected} ({fmt(coverage)}%)" if expected else "- **Minute-data coverage:** not started; awaiting first completed one-minute interval",
            f"- **Risk-data quality:** **{quality}**", f"- **Historical risk intervals:** {complete} complete / {incomplete} incomplete",
            f"- **Latest completed risk interval:** {interval['status'] if interval else 'none'}"])
        if warning:
            lines.append(f"- **Latest monitoring warning:** {cell(warning)}")
        lines.extend(["", "## Cooldown safeguard", "",
            f"- **Cooldown flag:** **{cooldown}**",
            f"- Activated: {stamp(control['activated_at_ms']) if control else '—'}",
            f"- Scheduled reset: {stamp(control['reset_at_ms']) if control else '—'}",
            f"- Current cycle start: {stamp(cycle['cycle_start_ms']) if cycle else '—'}",
            "- A new pair is built only when the binary flag is 0 and the ranking/depth/accounting gates pass.", "",
            "## Timestamps and provenance", "",
            f"- **STATUS.md generated:** {stamp(started)}", f"- **Paper workflow started:** {stamp(workflow_started_ms)}",
            f"- **Latest saved market observation:** {stamp(asof)}", f"- **Workflow trigger:** **{cell(trigger)}**",
            f"- **GitHub Actions run:** {run_link}", "", "## Interpretation notes", "",
            "- NAV includes cash and mark-to-market unrealized P&L. Open-leg unrealized P&L is gross; an estimated exit fee has not yet been charged.",
            "- Pending funding is an estimate and is not posted to the accounting ledger until the next successful rebalance.",
            "- Without a current-pair monitor observation, marks fall back to the entry-time snapshot and are not live quotes.",
            "- MDD is identified as unavailable until supported by minute-level observations; incomplete coverage is shown explicitly.",
            "- This page is derived from the durable SQLite ledger after a successful paper run. The database remains the accounting source of truth.", ""])
        return "\n".join(lines)
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--workflow-start-ms", type=int)
    parser.add_argument("--run-id")
    parser.add_argument("--event-name")
    parser.add_argument("--mode")
    parser.add_argument("--repository")
    parser.add_argument("--report-json")
    args = parser.parse_args()
    export_performance_artifacts(args.db_path, Path(args.output).parent)
    content = render_status(args.db_path, workflow_started_ms=args.workflow_start_ms,
                            run_id=args.run_id, event_name=args.event_name, mode=args.mode,
                            repository=args.repository, report_json=args.report_json)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    print(f"Rendered human-readable status page: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
