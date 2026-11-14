"""Persistent lifetime equity curve, trade ledger exports, and daily Sortino analytics."""
from __future__ import annotations

import csv
import html
import json
import math
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

D = Decimal
MIN_DAILY_RETURNS = 20
MIN_DOWNSIDE_DAYS = 5
ANNUAL_DAYS = 365


def _dec(value: Any, default: str = "0") -> Decimal:
    try:
        result = D(str(value if value is not None else default))
        return result if result.is_finite() else D(default)
    except (InvalidOperation, ValueError, TypeError):
        return D(default)


def _utc(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _create_table(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS equity_curve(
        timestamp_ms INTEGER PRIMARY KEY,
        valuation_time_ms INTEGER,
        event_type TEXT NOT NULL CHECK(event_type IN ('START','MONITOR','REBALANCE')),
        nav_usdt TEXT NOT NULL,
        cash_usdt TEXT,
        unrealized_pnl_usdt TEXT,
        gross_exposure_usdt TEXT,
        portfolio_roi_pct TEXT NOT NULL,
        long_symbol TEXT,
        short_symbol TEXT
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS equity_curve_valuation_idx ON equity_curve(valuation_time_ms)")


def ensure_equity_start(db_path: str | Path, workflow_started_ms: int) -> None:
    """Seed the curve with initial capital exactly once, without touching accounting tables."""
    path = Path(db_path)
    if not path.exists():
        return
    db = sqlite3.connect(str(path), timeout=30)
    try:
        _create_table(db)
        account = db.execute("SELECT initial_nav FROM account WHERE id=1").fetchone()
        if account is None:
            return
        if db.execute("SELECT 1 FROM equity_curve LIMIT 1").fetchone():
            return
        initial = _dec(account[0])
        start = max(1, int(workflow_started_ms) - 1)
        db.execute("""INSERT INTO equity_curve(
            timestamp_ms,valuation_time_ms,event_type,nav_usdt,cash_usdt,
            unrealized_pnl_usdt,gross_exposure_usdt,portfolio_roi_pct)
            VALUES(?,?,'START',?,?, '0','0','0')""",
            (start, start, format(initial, "f"), format(initial, "f")))
        db.commit()
    finally:
        db.close()


def record_equity_point(db_path: str | Path, *, workflow_started_ms: int,
                        valuation_time_ms: int | None, nav_usdt: Any,
                        source: str, cash_usdt: Any = None,
                        unrealized_pnl_usdt: Any = None,
                        gross_exposure_usdt: Any = None,
                        long_symbol: str | None = None,
                        short_symbol: str | None = None) -> None:
    """Append one NAV mark per completed paper workflow, keyed by workflow-start time."""
    if source not in {"MONITOR", "REBALANCE"}:
        raise ValueError("source must be MONITOR or REBALANCE")
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(path)
    started = int(workflow_started_ms)
    nav = _dec(nav_usdt)
    if nav <= 0:
        raise ValueError("NAV must be positive")
    db = sqlite3.connect(str(path), timeout=30)
    try:
        _create_table(db)
        account = db.execute("SELECT initial_nav FROM account WHERE id=1").fetchone()
        if account is None:
            raise ValueError("Paper account has not been initialized")
        initial = _dec(account[0])
        if initial <= 0:
            raise ValueError("Initial NAV must be positive")
        if db.execute("SELECT 1 FROM equity_curve LIMIT 1").fetchone() is None:
            seed = max(1, started - 1)
            db.execute("""INSERT INTO equity_curve(
                timestamp_ms,valuation_time_ms,event_type,nav_usdt,cash_usdt,
                unrealized_pnl_usdt,gross_exposure_usdt,portfolio_roi_pct)
                VALUES(?,?,'START',?,?, '0','0','0')""",
                (seed, seed, format(initial, "f"), format(initial, "f")))
        latest = db.execute("SELECT MAX(timestamp_ms) FROM equity_curve").fetchone()[0]
        if latest is not None and started < int(latest):
            raise ValueError(f"Out-of-order equity point: {started} precedes {latest}")
        roi = (nav / initial - D("1")) * D("100")
        db.execute("""INSERT INTO equity_curve(
            timestamp_ms,valuation_time_ms,event_type,nav_usdt,cash_usdt,
            unrealized_pnl_usdt,gross_exposure_usdt,portfolio_roi_pct,long_symbol,short_symbol)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(timestamp_ms) DO UPDATE SET
              valuation_time_ms=excluded.valuation_time_ms,
              event_type=excluded.event_type,nav_usdt=excluded.nav_usdt,
              cash_usdt=excluded.cash_usdt,unrealized_pnl_usdt=excluded.unrealized_pnl_usdt,
              gross_exposure_usdt=excluded.gross_exposure_usdt,
              portfolio_roi_pct=excluded.portfolio_roi_pct,
              long_symbol=excluded.long_symbol,short_symbol=excluded.short_symbol""",
            (started, int(valuation_time_ms) if valuation_time_ms else None, source,
             format(nav, "f"), None if cash_usdt is None else str(cash_usdt),
             None if unrealized_pnl_usdt is None else str(unrealized_pnl_usdt),
             None if gross_exposure_usdt is None else str(gross_exposure_usdt),
             format(roi, "f"), long_symbol, short_symbol))
        db.commit()
    finally:
        db.close()


def _daily_closes(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closes: dict[str, dict[str, Any]] = {}
    for point in points:
        day = datetime.fromtimestamp(int(point["timestamp_ms"]) / 1000, timezone.utc).date().isoformat()
        closes[day] = {**point, "date": day}
    return [closes[d] for d in sorted(closes)]


def performance_summary(db_path: str | Path) -> dict[str, Any]:
    """Calculate lifetime trade statistics, daily close returns, and annualized Sortino."""
    path = Path(db_path)
    if not path.exists():
        return {"status": "NO_DATABASE", "sortino": None, "sortino_status": "NO_DATABASE",
                "equity_points": [], "daily_closes": [], "trades": []}
    db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "equity_curve" not in tables:
            points: list[dict[str, Any]] = []
        else:
            points = [dict(r) for r in db.execute("SELECT * FROM equity_curve ORDER BY timestamp_ms")]
        trades = [dict(r) for r in db.execute("SELECT * FROM trades ORDER BY trade_id")] if "trades" in tables else []
        closes = _daily_closes(points)
        daily_returns: list[dict[str, Any]] = []
        for previous, current in zip(closes, closes[1:]):
            prior_day = datetime.fromisoformat(previous["date"]).date()
            current_day = datetime.fromisoformat(current["date"]).date()
            if (current_day - prior_day).days != 1:
                continue
            prior_nav, current_nav = _dec(previous["nav_usdt"]), _dec(current["nav_usdt"])
            if prior_nav <= 0:
                continue
            ret = (current_nav / prior_nav - D("1")) * D("100")
            daily_returns.append({"date": current["date"], "previous_date": previous["date"],
                                  "return_pct": format(ret, "f"),
                                  "nav_usdt": current["nav_usdt"]})
        downside = [ _dec(x["return_pct"]) for x in daily_returns if _dec(x["return_pct"]) < 0 ]
        mean_return = (sum((_dec(x["return_pct"]) for x in daily_returns), D("0")) /
                       D(len(daily_returns))) if daily_returns else D("0")
        downside_dev = (sum((min(_dec(x["return_pct"]), D("0")) ** 2 for x in daily_returns), D("0")) /
                        D(len(daily_returns))).sqrt() if daily_returns else D("0")
        sortino = None
        if len(daily_returns) < MIN_DAILY_RETURNS:
            sortino_status = f"INSUFFICIENT_HISTORY: {len(daily_returns)}/{MIN_DAILY_RETURNS} daily returns"
        elif len(downside) < MIN_DOWNSIDE_DAYS:
            sortino_status = f"INSUFFICIENT_DOWNSIDE: {len(downside)}/{MIN_DOWNSIDE_DAYS} negative days"
        elif downside_dev == 0:
            sortino_status = "NO_DOWNSIDE_DEVIATION"
        else:
            sortino = math.sqrt(ANNUAL_DAYS) * float(mean_return / downside_dev)
            sortino_status = "AVAILABLE"
        closed = [t for t in trades if t.get("status") == "CLOSED" and t.get("net_pnl") is not None]
        wins = [t for t in closed if _dec(t.get("net_pnl")) > 0]
        losses = [t for t in closed if _dec(t.get("net_pnl")) < 0]
        net_realized = sum((_dec(t.get("net_pnl")) for t in closed), D("0"))
        win_rate = D(len(wins)) / D(len(closed)) * D("100") if closed else None
        total_wins = sum((_dec(t.get("net_pnl")) for t in wins), D("0"))
        total_losses = -sum((_dec(t.get("net_pnl")) for t in losses), D("0"))
        profit_factor = (total_wins / total_losses) if total_losses > 0 else (None if not wins else "UNBOUNDED")
        equity_mdd: Decimal | None = D("0") if len(points) >= 2 else None
        peak: Decimal | None = None
        if equity_mdd is not None:
            for p in points:
                nav = _dec(p["nav_usdt"])
                peak = nav if peak is None else max(peak, nav)
                if peak and nav < peak:
                    equity_mdd = min(equity_mdd, (nav / peak - D("1")) * D("100"))
        return {
            "status": "OK", "equity_points": points, "daily_closes": closes,
            "daily_returns": daily_returns, "daily_return_count": len(daily_returns),
            "downside_days": len(downside), "mean_daily_return_pct": format(mean_return, "f"),
            "downside_deviation_pct": format(downside_dev, "f"),
            "sortino": sortino, "sortino_status": sortino_status,
            "sortino_target_daily_pct": "0", "sortino_annualization_days": ANNUAL_DAYS,
            "sortino_min_daily_returns": MIN_DAILY_RETURNS,
            "sortino_min_downside_days": MIN_DOWNSIDE_DAYS,
            "equity_curve_mdd_pct": format(equity_mdd, "f") if equity_mdd is not None else None,
            "trade_count_total": len(trades), "closed_trade_count": len(closed),
            "open_trade_count": sum(1 for t in trades if t.get("status") == "OPEN"),
            "closed_net_pnl_usdt": format(net_realized, "f"),
            "win_rate_pct": format(win_rate, "f") if win_rate is not None else None,
            "profit_factor": format(profit_factor, "f") if isinstance(profit_factor, D) else profit_factor,
            "trades": trades,
        }
    finally:
        db.close()


def _write_equity_svg(points: list[dict[str, Any]], path: Path) -> None:
    width, height = 1000, 360
    left, right, top, bottom = 84, 28, 42, 58
    plot_w, plot_h = width - left - right, height - top - bottom
    values = [_dec(p["nav_usdt"]) for p in points]
    if not values:
        body = '<text x="500" y="180" text-anchor="middle" font-size="20">Waiting for first paper equity observation</text>'
    else:
        low, high = min(values), max(values)
        spread = high - low
        pad = (spread * D("0.08")) if spread else max(abs(high) * D("0.02"), D("0.1"))
        low, high = low - pad, high + pad
        if high == low:
            high = low + D("1")
        coords = []
        for i, (point, value) in enumerate(zip(points, values)):
            x = left + (plot_w * i / max(1, len(points) - 1))
            y = top + plot_h * (float(high - value) / float(high - low))
            coords.append(f"{x:.2f},{y:.2f}")
        polyline = " ".join(coords)
        first, last = values[0], values[-1]
        body = (
            f'<line x1="{left}" y1="{top+plot_h}" x2="{width-right}" y2="{top+plot_h}" stroke="#777" stroke-width="1"/>'
            f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top+plot_h}" stroke="#777" stroke-width="1"/>'
            f'<polyline points="{polyline}" fill="none" stroke="#1976d2" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{left}" cy="{top+plot_h*(float(high-first)/float(high-low)):.2f}" r="4" fill="#1976d2"/>'
            f'<circle cx="{width-right}" cy="{top+plot_h*(float(high-last)/float(high-low)):.2f}" r="4" fill="#1976d2"/>'
            f'<text x="{left}" y="{top-12}" font-size="14">Start: {html.escape(format(first, ".4f"))} USDT</text>'
            f'<text x="{width-right}" y="{top-12}" text-anchor="end" font-size="14">Latest: {html.escape(format(last, ".4f"))} USDT</text>'
            f'<text x="{left}" y="{height-18}" font-size="12">{html.escape(_utc(points[0]["timestamp_ms"]))}</text>'
            f'<text x="{width-right}" y="{height-18}" text-anchor="end" font-size="12">{html.escape(_utc(points[-1]["timestamp_ms"]))}</text>'
        )
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
           '<rect width="100%" height="100%" fill="#ffffff"/>'
           '<text x="500" y="24" text-anchor="middle" font-size="18" font-family="Arial">Portfolio Equity Curve — USDT</text>'
           f'<g font-family="Arial" fill="#222">{body}</g></svg>\n')
    path.write_text(svg, encoding="utf-8")


def export_performance_artifacts(db_path: str | Path, output_dir: str | Path) -> dict[str, Any]:
    """Export the complete lifetime curve, daily series and full trade history."""
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    summary = performance_summary(db_path)
    points = summary.get("equity_points", [])
    trades = summary.get("trades", [])
    with (output / "equity_curve.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["workflow_start_utc","workflow_start_ms","valuation_utc","event_type","nav_usdt",
                         "cash_usdt","unrealized_pnl_usdt","gross_exposure_usdt","portfolio_roi_pct",
                         "long_symbol","short_symbol"])
        for p in points:
            writer.writerow([_utc(p["timestamp_ms"]),p["timestamp_ms"],_utc(p.get("valuation_time_ms")),
                             p["event_type"],p["nav_usdt"],p.get("cash_usdt"),
                             p.get("unrealized_pnl_usdt"),p.get("gross_exposure_usdt"),
                             p["portfolio_roi_pct"],p.get("long_symbol"),p.get("short_symbol")])
    with (output / "daily_equity.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["utc_date","close_timestamp_utc","nav_usdt","close_to_close_return_pct"])
        returns = {r["date"]:r["return_pct"] for r in summary.get("daily_returns", [])}
        for p in summary.get("daily_closes", []):
            writer.writerow([p["date"],_utc(p["timestamp_ms"]),p["nav_usdt"],returns.get(p["date"])])
    with (output / "trade_history.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        fields = ["trade_id","status","leg","symbol","entry_time_ms","entry_date","entry_qty",
                  "entry_price","entry_notional","entry_fee","exit_time_ms","exit_date","exit_price",
                  "exit_notional","exit_fee","gross_pnl","funding_pnl","net_pnl","trade_roi_pct"]
        writer.writerow(fields)
        for t in trades:
            writer.writerow([t.get(k) for k in fields])
    _write_equity_svg(points, output / "equity_curve.svg")
    (output / "performance_metrics.json").write_text(
        json.dumps({k:v for k,v in summary.items() if k not in {"equity_points","daily_closes","daily_returns","trades"}},
                   indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    return summary
