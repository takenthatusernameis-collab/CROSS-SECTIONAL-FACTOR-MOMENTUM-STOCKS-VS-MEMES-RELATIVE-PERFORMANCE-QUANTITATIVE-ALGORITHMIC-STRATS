"""SQLite-backed paper accounting for Bitget USDT perpetuals.

No order endpoints are used. Paper fills are depth-walk VWAP estimates.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable

from .risk import analyse_intraday_risk, expected_minute_starts

D = Decimal


class PaperError(RuntimeError):
    pass


def dec(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PaperError(f"{name} is missing")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PaperError(f"{name} is not numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise PaperError(f"{name} is invalid")
    return result


def fmt(value: Decimal) -> str:
    return format(value, "f")


HOLD_DURATION_MS = 24 * 60 * 60 * 1000
MINUTE_MS = 60 * 1000


def cooldown_close_ms(entry_time_ms: int) -> int:
    """Legacy conversion used only when migrating pre-binary-lock databases."""
    expiry = int(entry_time_ms) + HOLD_DURATION_MS
    first_full_minute_start = ((expiry + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS
    return first_full_minute_start + MINUTE_MS


def cooldown_reset_ms(run_started_ms: int) -> int:
    """Reset after the minute at +24h from the activation minute has passed."""
    activation_minute = (int(run_started_ms) // MINUTE_MS) * MINUTE_MS
    return activation_minute + HOLD_DURATION_MS + MINUTE_MS


def rebalance_gate(db_path: str | Path, run_started_ms: int) -> dict[str, Any]:
    """Read/update the persistent binary cooldown using the actual run-start time."""
    path = Path(db_path)
    started = int(run_started_ms)
    if not path.exists():
        return {"can_rebalance": True, "cooldown": 0, "reason": "COOLDOWN_ZERO_NO_DATABASE",
                "eligible_after_ms": None, "run_started_ms": started}

    db = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS cooldown_control(
            id INTEGER PRIMARY KEY CHECK(id=1),
            cooldown INTEGER NOT NULL DEFAULT 0 CHECK(cooldown IN (0,1)),
            activated_at_ms INTEGER, reset_at_ms INTEGER,
            last_reset_run_started_ms INTEGER)""")
        state = db.execute("SELECT * FROM cooldown_control WHERE id=1").fetchone()
        if state is None:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            legacy = db.execute("SELECT cycle_start_ms,eligible_after_ms FROM monitor_state WHERE id=1").fetchone() if "monitor_state" in tables else None
            open_count = int(db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]) if "positions" in tables else 0
            if legacy is not None and open_count == 2:
                activated = int(legacy["cycle_start_ms"])
                reset_at = int(legacy["eligible_after_ms"] or cooldown_reset_ms(activated))
                db.execute("INSERT INTO cooldown_control(id,cooldown,activated_at_ms,reset_at_ms) VALUES(1,1,?,?)",
                           (activated, reset_at))
            else:
                db.execute("INSERT INTO cooldown_control(id,cooldown) VALUES(1,0)")
            state = db.execute("SELECT * FROM cooldown_control WHERE id=1").fetchone()
        reset_at = int(state["reset_at_ms"]) if state["reset_at_ms"] is not None else None
        if int(state["cooldown"]) == 1 and reset_at is not None and started >= reset_at:
            db.execute("""UPDATE cooldown_control SET cooldown=0,
                last_reset_run_started_ms=? WHERE id=1""", (started,))
            state = db.execute("SELECT * FROM cooldown_control WHERE id=1").fetchone()
        value = int(state["cooldown"])
        db.execute("COMMIT")
        return {
            "can_rebalance": value == 0, "cooldown": value,
            "reason": "COOLDOWN_ZERO_READY" if value == 0 else "COOLDOWN_ONE_ACTIVE",
            "activated_at_ms": state["activated_at_ms"],
            "eligible_after_ms": state["reset_at_ms"],
            "last_reset_run_started_ms": state["last_reset_run_started_ms"],
            "run_started_ms": started,
            "seconds_remaining": max(0, (int(state["reset_at_ms"]) - started) / 1000)
                if state["reset_at_ms"] is not None and value == 1 else 0,
        }
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def utc_date(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).date().isoformat()


def utc_iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def init_db(path: str | Path, initial_nav: Decimal) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS account(
        id INTEGER PRIMARY KEY CHECK(id=1), initial_nav TEXT NOT NULL,
        cash TEXT NOT NULL, realized_gross_pnl TEXT NOT NULL DEFAULT '0',
        fees_cumulative TEXT NOT NULL DEFAULT '0', funding_cumulative TEXT NOT NULL DEFAULT '0');
      CREATE TABLE IF NOT EXISTS trades(
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT, leg TEXT NOT NULL CHECK(leg IN ('LONG','SHORT')),
        symbol TEXT NOT NULL, entry_time_ms INTEGER NOT NULL, entry_date TEXT NOT NULL,
        entry_qty TEXT NOT NULL, entry_price TEXT NOT NULL, entry_notional TEXT NOT NULL,
        entry_fee_rate TEXT NOT NULL, entry_fee TEXT NOT NULL,
        funding_pnl TEXT NOT NULL DEFAULT '0', exit_time_ms INTEGER, exit_date TEXT,
        exit_price TEXT, exit_notional TEXT, exit_fee_rate TEXT, exit_fee TEXT,
        gross_pnl TEXT, net_pnl TEXT, trade_roi_pct TEXT, status TEXT NOT NULL DEFAULT 'OPEN');
      CREATE INDEX IF NOT EXISTS trades_date_idx ON trades(entry_date, trade_id);
      CREATE TABLE IF NOT EXISTS positions(
        leg TEXT PRIMARY KEY CHECK(leg IN ('LONG','SHORT')), trade_id INTEGER NOT NULL UNIQUE,
        symbol TEXT NOT NULL, qty TEXT NOT NULL, entry_price TEXT NOT NULL,
        entry_notional TEXT NOT NULL, entry_fee_rate TEXT NOT NULL, entry_fee TEXT NOT NULL,
        last_funding_ms INTEGER NOT NULL,
        FOREIGN KEY(trade_id) REFERENCES trades(trade_id));
      CREATE TABLE IF NOT EXISTS ledger(
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
        snapshot_date TEXT NOT NULL, event_time_ms INTEGER NOT NULL,
        trade_id INTEGER, leg TEXT NOT NULL, event_type TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL, qty TEXT NOT NULL, price TEXT, notional TEXT NOT NULL,
        fee_rate TEXT, fee TEXT NOT NULL DEFAULT '0', gross_pnl TEXT NOT NULL DEFAULT '0',
        funding_rate TEXT, funding_cashflow TEXT NOT NULL DEFAULT '0', detail_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(trade_id) REFERENCES trades(trade_id));
      CREATE INDEX IF NOT EXISTS ledger_date_idx ON ledger(snapshot_date,id);
      CREATE TABLE IF NOT EXISTS daily_snapshots(
        snapshot_date TEXT PRIMARY KEY, as_of_ms INTEGER NOT NULL,
        long_symbol TEXT NOT NULL, short_symbol TEXT NOT NULL,
        nav TEXT NOT NULL, cash TEXT NOT NULL, unrealized_pnl TEXT NOT NULL,
        realized_gross_pnl_cumulative TEXT NOT NULL, fees_cumulative TEXT NOT NULL,
        funding_cumulative TEXT NOT NULL, gross_exposure TEXT NOT NULL,
        daily_return_pct TEXT, hours_since_previous REAL,
        total_return_pct TEXT NOT NULL, long_position_json TEXT NOT NULL,
        short_position_json TEXT NOT NULL, long_fee_rate TEXT NOT NULL,
        short_fee_rate TEXT NOT NULL, ranking_run_at TEXT NOT NULL,
        closed_trades_count INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE IF NOT EXISTS strategy_risk(
        id INTEGER PRIMARY KEY CHECK(id=1), peak_nav TEXT NOT NULL,
        peak_time_ms INTEGER, max_drawdown_pct TEXT NOT NULL DEFAULT '0',
        mdd_peak_nav TEXT, mdd_trough_nav TEXT,
        mdd_peak_time_ms INTEGER, mdd_trough_time_ms INTEGER,
        complete_intervals INTEGER NOT NULL DEFAULT 0,
        incomplete_intervals INTEGER NOT NULL DEFAULT 0,
        last_interval_status TEXT NOT NULL DEFAULT 'NOT_EVALUATED');
      CREATE TABLE IF NOT EXISTS risk_intervals(
        snapshot_date TEXT PRIMARY KEY, period_start_ms INTEGER, period_end_ms INTEGER NOT NULL,
        long_symbol TEXT, short_symbol TEXT, expected_minutes INTEGER NOT NULL DEFAULT 0,
        matched_minutes INTEGER NOT NULL DEFAULT 0, coverage_pct TEXT NOT NULL DEFAULT '0',
        status TEXT NOT NULL, error TEXT, period_roi_pct TEXT,
        spread_open_ratio TEXT, spread_high_ratio TEXT, spread_low_ratio TEXT,
        spread_close_ratio TEXT, spread_drawdown_pct TEXT,
        nav_start TEXT, nav_min_intraday TEXT, nav_min_time_ms INTEGER,
        nav_close_intraday TEXT, nav_after_rebalance TEXT,
        daily_mdd_pct TEXT, strategy_mdd_pct TEXT, strategy_peak_nav TEXT,
        strategy_mdd_peak_nav TEXT, strategy_mdd_trough_nav TEXT,
        strategy_mdd_peak_time_ms INTEGER, strategy_mdd_trough_time_ms INTEGER);
      CREATE TABLE IF NOT EXISTS minute_metrics(
        snapshot_date TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
        long_symbol TEXT NOT NULL, short_symbol TEXT NOT NULL,
        spread_ratio_close TEXT NOT NULL, spread_ratio_high TEXT NOT NULL,
        spread_ratio_low TEXT NOT NULL, nav_close TEXT NOT NULL,
        nav_high TEXT NOT NULL, nav_low TEXT NOT NULL,
        PRIMARY KEY(snapshot_date,timestamp_ms),
        FOREIGN KEY(snapshot_date) REFERENCES risk_intervals(snapshot_date));
      CREATE INDEX IF NOT EXISTS minute_metrics_time_idx ON minute_metrics(snapshot_date,timestamp_ms);
      CREATE TABLE IF NOT EXISTS monitor_state(
        id INTEGER PRIMARY KEY CHECK(id=1), cycle_start_ms INTEGER NOT NULL,
        eligible_after_ms INTEGER NOT NULL, long_trade_id INTEGER NOT NULL,
        short_trade_id INTEGER NOT NULL, last_processed_minute_ms INTEGER,
        last_observed_at_ms INTEGER, nav_start TEXT NOT NULL, nav_last TEXT NOT NULL,
        funding_accrued TEXT NOT NULL DEFAULT '0', spread_open_ratio TEXT NOT NULL,
        spread_high_ratio TEXT NOT NULL, spread_low_ratio TEXT NOT NULL,
        spread_close_ratio TEXT, nav_low_intraday TEXT, nav_low_time_ms INTEGER,
        daily_peak_nav TEXT NOT NULL, daily_mdd_pct TEXT NOT NULL DEFAULT '0',
        expected_minutes INTEGER NOT NULL DEFAULT 0, matched_minutes INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'WAITING', last_error TEXT);
      CREATE TABLE IF NOT EXISTS monitor_minute_metrics(
        cycle_start_ms INTEGER NOT NULL, timestamp_ms INTEGER NOT NULL,
        long_trade_id INTEGER NOT NULL, short_trade_id INTEGER NOT NULL,
        long_symbol TEXT NOT NULL, short_symbol TEXT NOT NULL,
        spread_ratio_close TEXT NOT NULL, spread_ratio_high TEXT NOT NULL,
        spread_ratio_low TEXT NOT NULL, nav_close TEXT NOT NULL,
        nav_high TEXT NOT NULL, nav_low TEXT NOT NULL,
        PRIMARY KEY(cycle_start_ms,timestamp_ms));
      CREATE INDEX IF NOT EXISTS monitor_minute_time_idx ON monitor_minute_metrics(timestamp_ms);
      CREATE TABLE IF NOT EXISTS monitor_observations(
        observed_at_ms INTEGER PRIMARY KEY, cycle_start_ms INTEGER NOT NULL,
        eligible_after_ms INTEGER NOT NULL, status TEXT NOT NULL,
        nav_usdt TEXT NOT NULL, cash_usdt TEXT NOT NULL,
        unrealized_pnl_usdt TEXT NOT NULL, funding_pending_usdt TEXT NOT NULL,
        gross_exposure_usdt TEXT NOT NULL, portfolio_roi_pct TEXT NOT NULL, cycle_roi_pct TEXT NOT NULL,
        long_symbol TEXT NOT NULL, long_mark_price TEXT NOT NULL,
        long_unrealized_pnl_usdt TEXT NOT NULL, long_trade_roi_pct TEXT NOT NULL,
        short_symbol TEXT NOT NULL, short_mark_price TEXT NOT NULL,
        short_unrealized_pnl_usdt TEXT NOT NULL, short_trade_roi_pct TEXT NOT NULL,
        spread_ratio_mark TEXT NOT NULL, spread_open_ratio TEXT NOT NULL,
        spread_high_ratio TEXT NOT NULL, spread_low_ratio TEXT NOT NULL,
        spread_drawdown_pct TEXT NOT NULL, daily_mdd_pct TEXT NOT NULL,
        strategy_mdd_pct TEXT NOT NULL, expected_minutes INTEGER NOT NULL,
        matched_minutes INTEGER NOT NULL, coverage_pct TEXT NOT NULL,
        seconds_to_rebalance INTEGER NOT NULL, last_error TEXT);
      CREATE INDEX IF NOT EXISTS monitor_observations_cycle_idx ON monitor_observations(cycle_start_ms,observed_at_ms);
      CREATE TABLE IF NOT EXISTS cooldown_control(
        id INTEGER PRIMARY KEY CHECK(id=1),
        cooldown INTEGER NOT NULL DEFAULT 0 CHECK(cooldown IN (0,1)),
        activated_at_ms INTEGER, reset_at_ms INTEGER,
        last_reset_run_started_ms INTEGER);
    """)
    risk_columns = {r[1] for r in db.execute("PRAGMA table_info(risk_intervals)")}
    if "period_roi_pct" not in risk_columns:
        db.execute("ALTER TABLE risk_intervals ADD COLUMN period_roi_pct TEXT")
    monitor_columns = {r[1] for r in db.execute("PRAGMA table_info(monitor_observations)")}
    if "cycle_roi_pct" not in monitor_columns:
        db.execute("ALTER TABLE monitor_observations ADD COLUMN cycle_roi_pct TEXT NOT NULL DEFAULT '0'")
    account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
    if account is None:
        db.execute("INSERT INTO account(id,initial_nav,cash) VALUES(1,?,?)", (fmt(initial_nav), fmt(initial_nav)))
    elif dec(account["initial_nav"], "stored initial NAV", positive=True) != initial_nav:
        db.close()
        raise PaperError("Initial NAV is immutable after the paper database is initialized")

    cooldown_row = db.execute("SELECT * FROM cooldown_control WHERE id=1").fetchone()
    if cooldown_row is None:
        legacy = db.execute("SELECT cycle_start_ms,eligible_after_ms FROM monitor_state WHERE id=1").fetchone()
        open_count = int(db.execute("SELECT COUNT(*) FROM positions").fetchone()[0])
        if legacy is not None and open_count == 2:
            activated = int(legacy["cycle_start_ms"])
            reset_at = int(legacy["eligible_after_ms"] or cooldown_reset_ms(activated))
            db.execute("""INSERT INTO cooldown_control(id,cooldown,activated_at_ms,reset_at_ms)
                          VALUES(1,1,?,?)""", (activated, reset_at))
        else:
            db.execute("INSERT INTO cooldown_control(id,cooldown) VALUES(1,0)")
    return db


def parse_book(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PaperError("Bitget depth response is not an object")
    result: dict[str, Any] = {}
    stamp = dec(payload.get("ts"), "order-book timestamp", positive=True)
    if stamp != stamp.to_integral_value():
        raise PaperError("Order-book timestamp is not an integer")
    result["timestamp_ms"] = int(stamp)
    for side in ("bids", "asks"):
        raw = payload.get(side)
        if not isinstance(raw, list) or not raw:
            raise PaperError(f"Bitget order book has no {side}")
        levels = []
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise PaperError(f"Malformed {side} level")
            levels.append((dec(row[0], "depth price", positive=True), dec(row[1], "depth quantity", positive=True)))
        if side == "bids" and any(a[0] < b[0] for a, b in zip(levels, levels[1:])):
            raise PaperError("Bid depth is not sorted descending")
        if side == "asks" and any(a[0] > b[0] for a, b in zip(levels, levels[1:])):
            raise PaperError("Ask depth is not sorted ascending")
        result[side] = levels
    if result["bids"][0][0] >= result["asks"][0][0]:
        raise PaperError("Order book is crossed or locked")
    return result


def vwap_for_qty(levels: list[tuple[Decimal, Decimal]], qty: Decimal) -> Decimal:
    if qty <= 0:
        raise PaperError("Simulated quantity must be positive")
    remaining, cost = qty, D("0")
    for price, available in levels:
        take = min(remaining, available)
        cost += take * price
        remaining -= take
        if remaining <= D("0.000000000000000001"):
            break
    if remaining > D("0.000000000000000001"):
        raise PaperError("Insufficient visible order-book depth for the full simulated fill")
    return cost / qty


def lot_rules(market: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal | None, Decimal | None]:
    min_qty = dec(market.get("minOrderQty"), f"{market.get('symbol')} minOrderQty", positive=True)
    raw_step = market.get("quantityMultiplier")
    if raw_step in (None, "", "0", 0):
        precision = market.get("quantityPrecision")
        if precision in (None, ""):
            raise PaperError(f"{market.get('symbol')}: quantity step and precision are missing")
        try:
            raw_step = D("1").scaleb(-int(precision))
        except (ValueError, TypeError) as exc:
            raise PaperError(f"{market.get('symbol')}: invalid quantityPrecision") from exc
    step = dec(raw_step, f"{market.get('symbol')} quantity step", positive=True)
    max_raw = market.get("maxMarketOrderQty")
    maximum = dec(max_raw, f"{market.get('symbol')} max order quantity", positive=True) if max_raw not in (None, "", "0", 0) else None
    min_amount_raw = market.get("minOrderAmount")
    min_amount = dec(min_amount_raw, f"{market.get('symbol')} minimum order amount", positive=True) if min_amount_raw not in (None, "", "0", 0) else None
    return min_qty, step, maximum, min_amount


def size_for_target_notional(target: Decimal, levels: list[tuple[Decimal, Decimal]],
                             market: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    minimum, step, maximum, min_amount = lot_rules(market)
    qty = (target / levels[0][0] / step).to_integral_value(rounding=ROUND_DOWN) * step
    for _ in range(80):
        if qty < minimum:
            raise PaperError(f"{market['symbol']}: target notional is below minimum quantity")
        if maximum is not None and qty > maximum:
            raise PaperError(f"{market['symbol']}: target quantity exceeds Bitget max market order quantity")
        price = vwap_for_qty(levels, qty)
        notional = price * qty
        if notional <= target:
            if min_amount is not None and notional < min_amount:
                raise PaperError(f"{market['symbol']}: simulated order is below minimum notional")
            return qty, price, notional
        revised = (target / price / step).to_integral_value(rounding=ROUND_DOWN) * step
        if revised >= qty:
            revised = qty - step
        qty = revised
    raise PaperError(f"{market['symbol']}: unable to size below target notional")


def fee_rate_for(market: dict[str, Any], override: Decimal | None) -> Decimal:
    rate = override if override is not None else dec(market.get("takerFeeRate"), f"{market['symbol']} takerFeeRate", positive=True)
    if rate < 0 or rate >= 1:
        raise PaperError(f"{market['symbol']}: invalid taker fee rate")
    return rate


def _insert_ledger(db: sqlite3.Connection, *, event_key: str, date: str, time_ms: int,
                   trade_id: int | None, leg: str, event_type: str, symbol: str,
                   side: str, qty: Decimal, price: Decimal | None = None,
                   notional: Decimal = D("0"), fee_rate: Decimal | None = None,
                   fee: Decimal = D("0"), gross_pnl: Decimal = D("0"),
                   funding_rate: Decimal | None = None, funding: Decimal = D("0"),
                   detail: dict[str, Any] | None = None) -> None:
    db.execute("""INSERT INTO ledger
      (event_key,snapshot_date,event_time_ms,trade_id,leg,event_type,symbol,side,qty,price,
       notional,fee_rate,fee,gross_pnl,funding_rate,funding_cashflow,detail_json)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (event_key,date,time_ms,trade_id,leg,event_type,symbol,side,fmt(qty),
       fmt(price) if price is not None else None,fmt(notional),
       fmt(fee_rate) if fee_rate is not None else None,fmt(fee),fmt(gross_pnl),
       fmt(funding_rate) if funding_rate is not None else None,fmt(funding),
       json.dumps(detail or {},sort_keys=True)))


def run_snapshot(report: dict[str, Any], db_path: str | Path, *,
                 depth_fetch: Callable[[str], dict[str, Any]],
                 funding_fetch: Callable[[str, int, int], list[dict[str, Any]]],
                 funding_mark_fetch: Callable[[str, int], Decimal],
                 candle_fetch: Callable[[str, int, int, str], list[dict[str, Any]]] | None = None,
                 initial_nav: Decimal = D("10000"),
                 fee_override: Decimal | None = None,
                 run_started_ms: int | None = None) -> dict[str, Any]:
    if report.get("gates", {}).get("ranking") != "PASS":
        raise PaperError("Ranking gate is not PASS; paper state was not changed")
    as_of = report.get("as_of_server_time_ms")
    if isinstance(as_of, bool) or not isinstance(as_of, int) or as_of <= 0:
        raise PaperError("Ranking report has no valid Bitget server timestamp")
    long_lead = report.get("leaders", {}).get("long_stock_top_gainer")
    short_lead = report.get("leaders", {}).get("short_crypto_bottom_loser")
    if not isinstance(long_lead, dict) or not isinstance(short_lead, dict):
        raise PaperError("Both selected leaders are required")
    long_symbol, short_symbol = long_lead.get("symbol"), short_lead.get("symbol")
    if not isinstance(long_symbol, str) or not isinstance(short_symbol, str) or long_symbol == short_symbol:
        raise PaperError("Selected symbols are invalid or duplicated")
    date = utc_date(as_of)
    initial_nav = dec(initial_nav, "initial NAV", positive=True)
    if fee_override is not None:
        fee_override = dec(fee_override, "fee override")
        if fee_override < 0 or fee_override >= 1:
            raise PaperError("Fee override must be in [0,1)")

    db = init_db(db_path, initial_nav)
    started_ms = int(run_started_ms) if run_started_ms is not None else as_of
    try:
        cooldown_row = db.execute("SELECT * FROM cooldown_control WHERE id=1").fetchone()
        cooldown_value = int(cooldown_row["cooldown"]) if cooldown_row else 0
        reset_at = int(cooldown_row["reset_at_ms"]) if cooldown_row and cooldown_row["reset_at_ms"] is not None else None
        if cooldown_value == 1 and reset_at is not None and started_ms >= reset_at:
            db.execute("UPDATE cooldown_control SET cooldown=0,last_reset_run_started_ms=? WHERE id=1",
                       (started_ms,))
            cooldown_value = 0
        if cooldown_value == 1:
            return {"status":"COOLDOWN_ACTIVE","updated":False,"cooldown":1,
                    "eligible_after_ms":reset_at,"run_started_ms":started_ms,
                    "seconds_remaining":max(0,(reset_at-started_ms)/1000) if reset_at else None,
                    "message":"The persisted binary cooldown remains 1 until its scheduled reset minute has passed."}

        old_positions = {r["leg"]: dict(r) for r in db.execute("SELECT * FROM positions")}
        if old_positions and set(old_positions) != {"LONG", "SHORT"}:
            raise PaperError("Existing paper state does not contain exactly one LONG and one SHORT position")
        performance_end_ms = (
            int(cooldown_row["last_reset_run_started_ms"])
            if old_positions and cooldown_row and cooldown_row["last_reset_run_started_ms"] is not None
            else started_ms
        )
        monitor_state_row = db.execute("SELECT * FROM monitor_state WHERE id=1").fetchone()
        monitor_cycle_matches = bool(
            monitor_state_row is not None and set(old_positions) == {"LONG", "SHORT"}
            and int(monitor_state_row["long_trade_id"]) == int(old_positions["LONG"]["trade_id"])
            and int(monitor_state_row["short_trade_id"]) == int(old_positions["SHORT"]["trade_id"])
        )
        monitor_cycle_valid = bool(monitor_cycle_matches and monitor_state_row["last_processed_minute_ms"] is not None)
        prior_snapshot_for_risk = db.execute(
            "SELECT snapshot_date,as_of_ms,nav FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1"
        ).fetchone()
        risk_market_candles: dict[str, list[dict[str, Any]]] = {}
        risk_mark_candles: dict[str, list[dict[str, Any]]] = {}
        risk_fetch_error: str | None = None
        risk_period_start_ms = (
            int(monitor_state_row["cycle_start_ms"]) if monitor_cycle_matches
            else (int(prior_snapshot_for_risk["as_of_ms"]) if prior_snapshot_for_risk else None)
        )
        markets = {m.get("symbol"): m for m in report.get("market_results", []) if isinstance(m, dict)}
        for symbol, expected in ((long_symbol, "stock"), (short_symbol, "crypto")):
            market = markets.get(symbol)
            if not isinstance(market, dict):
                raise PaperError(f"{symbol}: selected contract is not in validated market results")
            if (market.get("symbolType") or "").casefold() != expected or market.get("status") != "online" or market.get("type") != "perpetual":
                raise PaperError(f"{symbol}: selected contract does not match the exact active {expected} perpetual taxonomy")
            if market.get("category") != "USDT-FUTURES" or market.get("quoteCoin") != "USDT":
                raise PaperError(f"{symbol}: selected contract is not USDT-settled")

        symbols_to_fetch = {long_symbol, short_symbol} | {p["symbol"] for p in old_positions.values()}
        books = {symbol: parse_book(depth_fetch(symbol)) for symbol in sorted(symbols_to_fetch)}
        for symbol, book in books.items():
            if abs(as_of - int(book["timestamp_ms"])) > 900_000:
                raise PaperError(f"{symbol}: public order book is stale relative to Bitget server time")

        funding_by_leg: dict[str, list[dict[str, Any]]] = {}
        for leg, position in old_positions.items():
            start_ms = int(position["last_funding_ms"])
            events = funding_fetch(position["symbol"], start_ms, as_of)
            if not isinstance(events, list):
                raise PaperError(f"{position['symbol']}: funding endpoint did not return an array")
            checked = []
            for event in events:
                if not isinstance(event, dict) or event.get("symbol") != position["symbol"]:
                    raise PaperError(f"{position['symbol']}: malformed funding event")
                event_ms = int(dec(event.get("fundingTime"), "fundingTime", positive=True))
                if start_ms < event_ms <= as_of:
                    rate = dec(event.get("fundingRate"), "fundingRate")
                    mark = dec(funding_mark_fetch(position["symbol"], event_ms), "funding mark price", positive=True)
                    checked.append({"symbol": position["symbol"], "fundingTime": event_ms,
                                    "fundingRate": rate, "markPrice": mark})
            funding_by_leg[leg] = sorted(checked, key=lambda x: x["fundingTime"])

        # Risk reconstruction is non-blocking: a candle-data outage must not change
        # ranking or prevent otherwise valid paper accounting.
        if old_positions and set(old_positions) == {"LONG", "SHORT"} and risk_period_start_ms is not None and not monitor_cycle_valid:
            if candle_fetch is None:
                risk_fetch_error = "No one-minute candle fetcher was supplied"
            else:
                try:
                    for symbol in sorted({p["symbol"] for p in old_positions.values()}):
                        risk_market_candles[symbol] = candle_fetch(symbol, risk_period_start_ms, performance_end_ms, "market")
                        risk_mark_candles[symbol] = candle_fetch(symbol, risk_period_start_ms, performance_end_ms, "mark")
                except Exception as exc:
                    risk_fetch_error = f"{type(exc).__name__}: {str(exc)[:800]}"
                    risk_market_candles.clear()
                    risk_mark_candles.clear()
        elif old_positions and risk_period_start_ms is None and not monitor_cycle_valid:
            risk_fetch_error = "Existing positions have no preceding daily snapshot"
        elif old_positions and set(old_positions) != {"LONG", "SHORT"}:
            risk_fetch_error = "Prior portfolio does not contain exactly one LONG and one SHORT position"

        # Validate new leg execution sizing before opening the SQLite transaction.
        long_market, short_market = markets[long_symbol], markets[short_symbol]
        long_fee = fee_rate_for(long_market, fee_override)
        short_fee = fee_rate_for(short_market, fee_override)

        db.execute("BEGIN IMMEDIATE")
        control_tx = db.execute("SELECT cooldown,reset_at_ms FROM cooldown_control WHERE id=1").fetchone()
        if control_tx is None or int(control_tx["cooldown"]) != 0:
            db.execute("ROLLBACK")
            return {"status":"COOLDOWN_ACTIVE","updated":False,"cooldown":1,
                    "eligible_after_ms":control_tx["reset_at_ms"] if control_tx else None,
                    "run_started_ms":started_ms,
                    "message":"The binary cooldown changed before the accounting transaction; no portfolio changes were made."}
        account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
        cash = dec(account["cash"], "account cash")
        realized = dec(account["realized_gross_pnl"], "realized P&L")
        fees = dec(account["fees_cumulative"], "cumulative fees")
        funding_total = dec(account["funding_cumulative"], "cumulative funding")
        starting_cash = cash
        previous = db.execute("SELECT snapshot_date,as_of_ms,nav FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        previous_nav = dec(previous["nav"], "previous NAV", positive=True) if previous else None
        hours_since = ((as_of - int(previous["as_of_ms"])) / 3_600_000) if previous else None
        count_before = db.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'").fetchone()[0]

        risk_state_row = db.execute("SELECT * FROM strategy_risk WHERE id=1").fetchone()
        if risk_state_row is None:
            seed_peak = max(initial_nav, previous_nav) if previous_nav is not None else initial_nav
            seed_time = started_ms
            db.execute("""INSERT INTO strategy_risk(id,peak_nav,peak_time_ms,max_drawdown_pct)
                          VALUES(?,?,?,'0')""", (1, fmt(seed_peak), seed_time))
            risk_state_row = db.execute("SELECT * FROM strategy_risk WHERE id=1").fetchone()

        funding_cashflows: list[dict[str, Any]] = []
        # Accrue every observed funding settlement for the existing positions.
        for leg, position in old_positions.items():
            qty = dec(position["qty"], "position quantity", positive=True)
            trade_id = int(position["trade_id"])
            for event in funding_by_leg[leg]:
                rate, mark = event["fundingRate"], event["markPrice"]
                direction = D("1") if leg == "LONG" else D("-1")
                payment = -direction * qty * mark * rate
                funding_cashflows.append({"time_ms": event["fundingTime"], "payment": fmt(payment)})
                cash += payment
                funding_total += payment
                _insert_ledger(db, event_key=f"{trade_id}:funding:{event['fundingTime']}",
                    date=date, time_ms=event["fundingTime"], trade_id=trade_id, leg=leg,
                    event_type="FUNDING", symbol=position["symbol"], side=leg, qty=qty,
                    price=mark, notional=qty*mark, funding_rate=rate, funding=payment,
                    detail={"funding_mark_source": "Bitget historical 1m mark-price candle"})
                db.execute("UPDATE trades SET funding_pnl=? WHERE trade_id=?",
                    (fmt(dec(db.execute("SELECT funding_pnl FROM trades WHERE trade_id=?", (trade_id,)).fetchone()[0], "trade funding") + payment), trade_id))

        risk_analysis: dict[str, Any] | None = None
        risk_status = "NO_PRIOR_POSITIONS"
        risk_error = risk_fetch_error
        if monitor_cycle_valid:
            monitor_state_tx = db.execute("SELECT * FROM monitor_state WHERE id=1").fetchone()
            monitor_start = int(monitor_state_tx["cycle_start_ms"])
            try:
                # Recompute the full interval expected count at finalization so a
                # failed last five-minute job cannot make stale coverage look complete.
                monitor_expected = len(expected_minute_starts(monitor_start, performance_end_ms))
            except Exception as exc:
                monitor_expected = int(monitor_state_tx["expected_minutes"]) + 1
                risk_error = f"Final interval exceeds minute-risk limit: {type(exc).__name__}: {str(exc)[:300]}"
            monitor_matched = int(db.execute(
                "SELECT COUNT(*) FROM monitor_minute_metrics WHERE cycle_start_ms=?",
                (monitor_start,),
            ).fetchone()[0])
            monitor_coverage = (D(monitor_matched)/D(monitor_expected)*D("100")) if monitor_expected else D("0")
            monitor_status = str(monitor_state_tx["status"])
            risk_status = "COMPLETE" if monitor_status == "MONITORING" and monitor_expected > 0 and monitor_matched == monitor_expected else "INCOMPLETE"
            risk_error = risk_error or monitor_state_tx["last_error"]
            if risk_status != "COMPLETE" and not risk_error:
                risk_error = "One or more expected one-minute intervals were not durably observed before rebalance."
            strategy_minutes = db.execute("""SELECT timestamp_ms,spread_ratio_close,spread_ratio_high,
                spread_ratio_low,nav_close,nav_high,nav_low FROM monitor_minute_metrics
                WHERE cycle_start_ms=? ORDER BY timestamp_ms""",
                (int(monitor_state_tx["cycle_start_ms"]),)).fetchall()
            risk_analysis = {
                "status":risk_status,"period_start_ms":monitor_start,
                "period_end_ms":performance_end_ms,"expected_minutes":monitor_expected,"matched_minutes":monitor_matched,
                "coverage_pct":fmt(monitor_coverage),"spread_open_ratio":monitor_state_tx["spread_open_ratio"],
                "spread_high_ratio":monitor_state_tx["spread_high_ratio"],
                "spread_low_ratio":monitor_state_tx["spread_low_ratio"],
                "spread_close_ratio":monitor_state_tx["spread_close_ratio"],
                "spread_drawdown_pct":fmt((dec(monitor_state_tx["spread_low_ratio"],"spread low",positive=True)/dec(monitor_state_tx["spread_high_ratio"],"spread high",positive=True)-D("1"))*D("100")),
                "nav_start":monitor_state_tx["nav_start"],"nav_low":monitor_state_tx["nav_low_intraday"],
                "nav_low_time_ms":monitor_state_tx["nav_low_time_ms"],"nav_close":monitor_state_tx["nav_last"],
                "daily_peak_nav":monitor_state_tx["daily_peak_nav"],"daily_mdd_pct":monitor_state_tx["daily_mdd_pct"],
                "strategy_peak_nav":risk_state_row["peak_nav"],"strategy_peak_time_ms":risk_state_row["peak_time_ms"],
                "strategy_mdd_pct":risk_state_row["max_drawdown_pct"],
                "strategy_mdd_peak_nav":risk_state_row["mdd_peak_nav"],
                "strategy_mdd_trough_nav":risk_state_row["mdd_trough_nav"],
                "strategy_mdd_peak_time_ms":risk_state_row["mdd_peak_time_ms"],
                "strategy_mdd_trough_time_ms":risk_state_row["mdd_trough_time_ms"],
                "minute_rows":[dict(r) for r in strategy_minutes],
            }
        elif old_positions and set(old_positions) == {"LONG", "SHORT"} and previous is not None and not risk_fetch_error:
            try:
                risk_analysis = analyse_intraday_risk(
                    start_ms=int(risk_period_start_ms), end_ms=performance_end_ms,
                    long_position=old_positions["LONG"], short_position=old_positions["SHORT"],
                    starting_cash=starting_cash, previous_nav=previous_nav,
                    funding_cashflows=funding_cashflows,
                    market_candles=risk_market_candles, mark_candles=risk_mark_candles,
                    prior_peak_nav=dec(risk_state_row["peak_nav"], "strategy peak NAV", positive=True),
                    prior_peak_time_ms=risk_state_row["peak_time_ms"],
                    prior_mdd_pct=dec(risk_state_row["max_drawdown_pct"], "strategy MDD"),
                    prior_mdd_peak_nav=dec(risk_state_row["mdd_peak_nav"], "MDD peak NAV") if risk_state_row["mdd_peak_nav"] is not None else None,
                    prior_mdd_trough_nav=dec(risk_state_row["mdd_trough_nav"], "MDD trough NAV") if risk_state_row["mdd_trough_nav"] is not None else None,
                    prior_mdd_peak_time_ms=risk_state_row["mdd_peak_time_ms"],
                    prior_mdd_trough_time_ms=risk_state_row["mdd_trough_time_ms"],
                )
                risk_status = risk_analysis.get("status", "INCOMPLETE")
            except Exception as exc:
                risk_error = f"{type(exc).__name__}: {str(exc)[:800]}"
                risk_status = "UNAVAILABLE"
        elif old_positions:
            risk_status = "UNAVAILABLE"

        # Close both old legs at the visible opposing side of the public book.
        for leg in ("LONG", "SHORT"):
            position = old_positions.get(leg)
            if not position:
                continue
            qty = dec(position["qty"], "position quantity", positive=True)
            entry = dec(position["entry_price"], "entry price", positive=True)
            trade_id = int(position["trade_id"])
            sign = D("1") if leg == "LONG" else D("-1")
            close_levels = books[position["symbol"]]["bids" if leg == "LONG" else "asks"]
            close_price = vwap_for_qty(close_levels, qty)
            close_notional = close_price * qty
            # Reuse the rate captured at entry unless an explicit override is configured.
            exit_fee_rate = fee_override if fee_override is not None else dec(position["entry_fee_rate"], "stored entry fee rate")
            exit_fee = close_notional * exit_fee_rate
            gross_pnl = (close_price - entry) * qty * sign
            entry_fee = dec(position["entry_fee"], "entry fee")
            trade = db.execute("SELECT funding_pnl,entry_notional FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            trade_funding = dec(trade["funding_pnl"], "trade funding")
            entry_notional = dec(trade["entry_notional"], "entry notional", positive=True)
            net_pnl = gross_pnl - entry_fee - exit_fee + trade_funding
            cash += gross_pnl - exit_fee
            realized += gross_pnl
            fees += exit_fee
            trade_roi = net_pnl / entry_notional * D("100")
            db.execute("""UPDATE trades SET exit_time_ms=?,exit_date=?,exit_price=?,exit_notional=?,
                exit_fee_rate=?,exit_fee=?,gross_pnl=?,net_pnl=?,trade_roi_pct=?,status='CLOSED'
                WHERE trade_id=?""",
                (as_of,date,fmt(close_price),fmt(close_notional),fmt(exit_fee_rate),fmt(exit_fee),
                 fmt(gross_pnl),fmt(net_pnl),fmt(trade_roi),trade_id))
            _insert_ledger(db, event_key=f"{date}:{leg}:close:{trade_id}", date=date,
                time_ms=as_of, trade_id=trade_id, leg=leg, event_type="CLOSE",
                symbol=position["symbol"], side="SELL" if leg == "LONG" else "BUY",
                qty=qty, price=close_price, notional=close_notional,
                fee_rate=exit_fee_rate, fee=exit_fee, gross_pnl=gross_pnl,
                detail={"fill_model": "public order-book depth VWAP"})
            db.execute("DELETE FROM positions WHERE leg=?", (leg,))

        target_notional = cash / (D("1") + long_fee + short_fee)
        new_positions: dict[str, dict[str, Any]] = {}
        for leg, symbol, market, fee_rate in (
            ("LONG", long_symbol, long_market, long_fee),
            ("SHORT", short_symbol, short_market, short_fee),
        ):
            book = books[symbol]
            entry_levels = book["asks"] if leg == "LONG" else book["bids"]
            qty, entry_price, entry_notional = size_for_target_notional(target_notional, entry_levels, market)
            entry_fee = entry_notional * fee_rate
            cash -= entry_fee
            fees += entry_fee
            cursor = db.execute("""INSERT INTO trades
                (leg,symbol,entry_time_ms,entry_date,entry_qty,entry_price,entry_notional,
                 entry_fee_rate,entry_fee,status) VALUES(?,?,?,?,?,?,?,?,?,'OPEN')""",
                (leg,symbol,as_of,date,fmt(qty),fmt(entry_price),fmt(entry_notional),fmt(fee_rate),fmt(entry_fee)))
            trade_id = int(cursor.lastrowid)
            db.execute("INSERT INTO positions(leg,trade_id,symbol,qty,entry_price,entry_notional,entry_fee_rate,entry_fee,last_funding_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                (leg,trade_id,symbol,fmt(qty),fmt(entry_price),fmt(entry_notional),fmt(fee_rate),fmt(entry_fee),as_of))
            _insert_ledger(db,event_key=f"{date}:{leg}:open:{trade_id}",date=date,time_ms=as_of,
                trade_id=trade_id,leg=leg,event_type="OPEN",symbol=symbol,
                side="BUY" if leg=="LONG" else "SELL",qty=qty,price=entry_price,
                notional=entry_notional,fee_rate=fee_rate,fee=entry_fee,
                detail={"fill_model":"public order-book depth VWAP","target_notional_usdt":fmt(target_notional)})
            new_positions[leg] = {"trade_id":trade_id,"symbol":symbol,"qty":qty,
                "entry_price":entry_price,"entry_notional":entry_notional,
                "entry_fee":entry_fee,"fee_rate":fee_rate,
                "mark_price":dec(market["markPrice"],f"{symbol} markPrice",positive=True)}

        unrealized = D("0")
        gross_exposure = D("0")
        position_json: dict[str, str] = {}
        for leg, position in new_positions.items():
            sign = D("1") if leg == "LONG" else D("-1")
            mark = position["mark_price"]
            u_pnl = (mark-position["entry_price"])*position["qty"]*sign
            unrealized += u_pnl
            gross_exposure += mark*position["qty"]
            open_net_pnl = u_pnl-position["entry_fee"]
            trade_roi_to_date = open_net_pnl/position["entry_notional"]*D("100")
            record = {"trade_id":position["trade_id"],"leg":leg,"symbol":position["symbol"],
                "quantity":fmt(position["qty"]),"entry_price":fmt(position["entry_price"]),
                "mark_price":fmt(mark),"entry_notional_usdt":fmt(position["entry_notional"]),
                "unrealized_pnl_usdt":fmt(u_pnl),"entry_fee_usdt":fmt(position["entry_fee"]),
                "funding_pnl_usdt":"0","net_open_pnl_usdt":fmt(open_net_pnl),
                "trade_roi_to_date_pct":fmt(trade_roi_to_date),"entry_time_utc":utc_iso(as_of)}
            position_json[leg]=json.dumps(record,sort_keys=True)

        nav = cash + unrealized
        if nav <= 0:
            raise PaperError("Portfolio NAV is non-positive; refusing to commit snapshot")
        daily_return = (nav/previous_nav-D("1"))*D("100") if previous_nav else None
        total_return = (nav/initial_nav-D("1"))*D("100")

        # Merge complete minute history into the strategy risk state. On an
        # incomplete interval, only the observed rebalance NAV endpoint is used;
        # the report permanently flags that the historical MDD sample has a gap.
        peak_nav = dec(risk_state_row["peak_nav"], "strategy peak NAV", positive=True)
        peak_time_ms = risk_state_row["peak_time_ms"]
        mdd_pct = dec(risk_state_row["max_drawdown_pct"], "strategy MDD")
        mdd_peak_nav = dec(risk_state_row["mdd_peak_nav"], "MDD peak NAV") if risk_state_row["mdd_peak_nav"] is not None else None
        mdd_trough_nav = dec(risk_state_row["mdd_trough_nav"], "MDD trough NAV") if risk_state_row["mdd_trough_nav"] is not None else None
        mdd_peak_time_ms = risk_state_row["mdd_peak_time_ms"]
        mdd_trough_time_ms = risk_state_row["mdd_trough_time_ms"]
        if risk_status == "COMPLETE" and risk_analysis is not None:
            peak_nav = dec(risk_analysis["strategy_peak_nav"], "reconstructed peak NAV", positive=True)
            peak_time_ms = risk_analysis.get("strategy_peak_time_ms")
            mdd_pct = dec(risk_analysis["strategy_mdd_pct"], "reconstructed MDD")
            mdd_peak_nav = dec(risk_analysis["strategy_mdd_peak_nav"], "reconstructed MDD peak") if risk_analysis.get("strategy_mdd_peak_nav") is not None else None
            mdd_trough_nav = dec(risk_analysis["strategy_mdd_trough_nav"], "reconstructed MDD trough") if risk_analysis.get("strategy_mdd_trough_nav") is not None else None
            mdd_peak_time_ms = risk_analysis.get("strategy_mdd_peak_time_ms")
            mdd_trough_time_ms = risk_analysis.get("strategy_mdd_trough_time_ms")
        if nav > peak_nav:
            peak_nav, peak_time_ms = nav, as_of
        endpoint_dd = (nav / peak_nav - D("1")) * D("100")
        if endpoint_dd < mdd_pct:
            mdd_pct = endpoint_dd
            mdd_peak_nav, mdd_trough_nav = peak_nav, nav
            mdd_peak_time_ms, mdd_trough_time_ms = peak_time_ms or as_of, as_of

        daily_peak_nav = (dec(risk_analysis["daily_peak_nav"], "daily peak NAV", positive=True)
                          if risk_analysis and risk_analysis.get("daily_peak_nav") is not None
                          else (previous_nav if previous_nav is not None else initial_nav))
        daily_mdd_pct = (dec(risk_analysis["daily_mdd_pct"], "daily MDD")
                         if risk_analysis and risk_analysis.get("daily_mdd_pct") is not None else D("0"))
        if nav > daily_peak_nav:
            daily_peak_nav = nav
        daily_mdd_pct = min(daily_mdd_pct, (nav / daily_peak_nav - D("1")) * D("100"))

        complete_count = int(risk_state_row["complete_intervals"])
        incomplete_count = int(risk_state_row["incomplete_intervals"])
        if old_positions and risk_status == "COMPLETE":
            complete_count += 1
        elif old_positions:
            incomplete_count += 1
        db.execute("""UPDATE strategy_risk SET peak_nav=?,peak_time_ms=?,max_drawdown_pct=?,
            mdd_peak_nav=?,mdd_trough_nav=?,mdd_peak_time_ms=?,mdd_trough_time_ms=?,
            complete_intervals=?,incomplete_intervals=?,last_interval_status=? WHERE id=1""",
            (fmt(peak_nav), peak_time_ms, fmt(mdd_pct),
             fmt(mdd_peak_nav) if mdd_peak_nav is not None else None,
             fmt(mdd_trough_nav) if mdd_trough_nav is not None else None,
             mdd_peak_time_ms, mdd_trough_time_ms, complete_count, incomplete_count, risk_status))

        held_long = old_positions.get("LONG", {}).get("symbol")
        held_short = old_positions.get("SHORT", {}).get("symbol")
        analysis = risk_analysis or {}
        period_roi_pct = (
            fmt((dec(analysis["nav_close"], "period closing NAV", positive=True) /
                 dec(analysis["nav_start"], "period starting NAV", positive=True) - D("1")) * D("100"))
            if risk_status == "COMPLETE" and analysis.get("nav_close") is not None
            and analysis.get("nav_start") is not None else None
        )
        period_mdd_pct = analysis.get("daily_mdd_pct") if risk_status == "COMPLETE" else None
        db.execute("""INSERT INTO risk_intervals(
            snapshot_date,period_start_ms,period_end_ms,long_symbol,short_symbol,
            expected_minutes,matched_minutes,coverage_pct,status,error,period_roi_pct,
            spread_open_ratio,spread_high_ratio,spread_low_ratio,spread_close_ratio,spread_drawdown_pct,
            nav_start,nav_min_intraday,nav_min_time_ms,nav_close_intraday,nav_after_rebalance,
            daily_mdd_pct,strategy_mdd_pct,strategy_peak_nav,strategy_mdd_peak_nav,
            strategy_mdd_trough_nav,strategy_mdd_peak_time_ms,strategy_mdd_trough_time_ms)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (date, int(risk_period_start_ms) if risk_period_start_ms is not None else None, performance_end_ms,
             held_long, held_short, int(analysis.get("expected_minutes", 0)),
             int(analysis.get("matched_minutes", 0)), str(analysis.get("coverage_pct", "0")),
             risk_status, risk_error, period_roi_pct,
             analysis.get("spread_open_ratio"), analysis.get("spread_high_ratio"),
             analysis.get("spread_low_ratio"), analysis.get("spread_close_ratio"),
             analysis.get("spread_drawdown_pct"),
             analysis.get("nav_start", fmt(previous_nav) if previous_nav is not None else fmt(initial_nav)),
             analysis.get("nav_low"), analysis.get("nav_low_time_ms"), analysis.get("nav_close"),
             fmt(nav), period_mdd_pct, fmt(mdd_pct), fmt(peak_nav),
             fmt(mdd_peak_nav) if mdd_peak_nav is not None else None,
             fmt(mdd_trough_nav) if mdd_trough_nav is not None else None,
             mdd_peak_time_ms, mdd_trough_time_ms))
        if risk_analysis:
            for point in risk_analysis.get("minute_rows", []):
                db.execute("""INSERT INTO minute_metrics(
                    snapshot_date,timestamp_ms,long_symbol,short_symbol,
                    spread_ratio_close,spread_ratio_high,spread_ratio_low,
                    nav_close,nav_high,nav_low) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (date, int(point["timestamp_ms"]), held_long, held_short,
                     point["spread_ratio_close"], point["spread_ratio_high"],
                     point["spread_ratio_low"], point["nav_close"], point["nav_high"],
                     point["nav_low"]))

        closed_count = db.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED' AND exit_date=?", (date,)).fetchone()[0]
        db.execute("""INSERT INTO daily_snapshots(
          snapshot_date,as_of_ms,long_symbol,short_symbol,nav,cash,unrealized_pnl,
          realized_gross_pnl_cumulative,fees_cumulative,funding_cumulative,gross_exposure,
          daily_return_pct,hours_since_previous,total_return_pct,long_position_json,
          short_position_json,long_fee_rate,short_fee_rate,ranking_run_at,closed_trades_count)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (date,as_of,long_symbol,short_symbol,fmt(nav),fmt(cash),fmt(unrealized),fmt(realized),
           fmt(fees),fmt(funding_total),fmt(gross_exposure),fmt(daily_return) if daily_return is not None else None,
           hours_since,fmt(total_return),position_json["LONG"],position_json["SHORT"],
           fmt(long_fee),fmt(short_fee),report.get("run_at_utc",utc_iso(as_of)),closed_count))
        db.execute("UPDATE account SET cash=?,realized_gross_pnl=?,fees_cumulative=?,funding_cumulative=? WHERE id=1",
                   (fmt(cash),fmt(realized),fmt(fees),fmt(funding_total)))

        # Arm the binary lock on successful construction, using this workflow run's
        # start minute as the performance-window and cooldown activation boundary.
        reset_at_ms = cooldown_reset_ms(started_ms)
        new_cycle_spread = new_positions["LONG"]["entry_price"] / new_positions["SHORT"]["entry_price"]
        db.execute("""INSERT OR REPLACE INTO monitor_state(
            id,cycle_start_ms,eligible_after_ms,long_trade_id,short_trade_id,
            last_processed_minute_ms,last_observed_at_ms,nav_start,nav_last,funding_accrued,
            spread_open_ratio,spread_high_ratio,spread_low_ratio,spread_close_ratio,
            nav_low_intraday,nav_low_time_ms,daily_peak_nav,daily_mdd_pct,
            expected_minutes,matched_minutes,status,last_error)
            VALUES(1,?,?,?,?,NULL,?,?,?,?,?,?,?,?,NULL,NULL,?,'0',0,0,'WAITING',NULL)""",
            (started_ms,reset_at_ms,new_positions["LONG"]["trade_id"],
             new_positions["SHORT"]["trade_id"],started_ms,fmt(nav),fmt(nav),"0",
             fmt(new_cycle_spread),fmt(new_cycle_spread),fmt(new_cycle_spread),
             fmt(new_cycle_spread),fmt(nav)))
        db.execute("""UPDATE cooldown_control SET cooldown=1,activated_at_ms=?,
            reset_at_ms=?,last_reset_run_started_ms=NULL WHERE id=1""",
            (started_ms,reset_at_ms))
        db.execute("COMMIT")

        closed_trades = [dict(r) for r in db.execute(
            "SELECT trade_id,leg,symbol,entry_date,exit_date,entry_notional,gross_pnl,entry_fee,exit_fee,funding_pnl,net_pnl,trade_roi_pct FROM trades WHERE status='CLOSED' AND exit_date=? ORDER BY trade_id", (date,))]
        return {"status":"UPDATED","updated":True,"snapshot_date":date,
            "cooldown":{"value":1,"activated_at_ms":started_ms,"reset_at_ms":reset_at_ms},
            "closed_period_performance":{
                "period_start_ms":risk_period_start_ms if old_positions else None,
                "period_end_ms":performance_end_ms if old_positions else None,
                "rebalance_delay_ms":max(0,started_ms-performance_end_ms) if old_positions else None,
                "duration_ms":(performance_end_ms-int(risk_period_start_ms))
                    if old_positions and risk_period_start_ms is not None else None,
                "period_roi_pct":period_roi_pct,
                "period_mdd_pct":period_mdd_pct,
                "duration_ms":(performance_end_ms - int(risk_analysis["period_start_ms"]))
                    if risk_analysis and risk_analysis.get("period_start_ms") is not None else None,
                "coverage_pct":analysis.get("coverage_pct","0"),
                "status":risk_status,
            },
            "snapshot":{"nav_usdt":fmt(nav),"cash_usdt":fmt(cash),"unrealized_pnl_usdt":fmt(unrealized),
                "daily_return_pct":fmt(daily_return) if daily_return is not None else None,
                "hours_since_previous_snapshot":round(hours_since,3) if hours_since is not None else None,
                "total_return_pct":fmt(total_return),"realized_gross_pnl_cumulative_usdt":fmt(realized),
                "fees_cumulative_usdt":fmt(fees),"funding_cumulative_usdt":fmt(funding_total),
                "gross_exposure_usdt":fmt(gross_exposure),"gross_exposure_to_nav":fmt(gross_exposure/nav),
                "daily_mdd_pct":fmt(daily_mdd_pct),"strategy_mdd_pct":fmt(mdd_pct),
                "strategy_mdd_status":("PARTIAL_DATA" if incomplete_count > 0 else
                    ("OBSERVED" if complete_count > 0 else "MINUTE_HISTORY_NOT_STARTED")),
                "intraday_risk_status":risk_status,
                "spread_drawdown_pct":analysis.get("spread_drawdown_pct"),
                "spread_ratio_low":analysis.get("spread_low_ratio"),
                "spread_ratio_high":analysis.get("spread_high_ratio"),
                "minute_coverage_pct":analysis.get("coverage_pct","0"),
                "open_positions":{leg:json.loads(raw) for leg,raw in position_json.items()},
                "closed_trades_today":closed_trades}}
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def trade_report(db_path: str | Path) -> dict[str, Any]:
    """Return inspectable latest snapshot, open trades, and closed-trade ROI history."""
    path = Path(db_path)
    if not path.exists():
        return {"status":"NO_PORTFOLIO_YET","initial_nav_usdt":None,"latest_snapshot":None,
                "open_trades":[],"closed_trades":[]}
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
        snapshot = db.execute("SELECT * FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        open_rows = db.execute("""SELECT p.*,t.entry_notional,t.entry_fee,t.funding_pnl,t.entry_date
                                  FROM positions p JOIN trades t ON t.trade_id=p.trade_id ORDER BY p.leg""").fetchall()
        closed = db.execute("""SELECT trade_id,leg,symbol,entry_date,exit_date,entry_notional,
            gross_pnl,entry_fee,exit_fee,funding_pnl,net_pnl,trade_roi_pct
            FROM trades WHERE status='CLOSED' ORDER BY trade_id DESC LIMIT 100""").fetchall()
        snap_dict = dict(snapshot) if snapshot else None
        if snap_dict:
            snap_dict["long_position"] = json.loads(snap_dict.pop("long_position_json"))
            snap_dict["short_position"] = json.loads(snap_dict.pop("short_position_json"))
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"strategy_risk", "risk_intervals"}.issubset(tables):
            risk_state = db.execute("SELECT * FROM strategy_risk WHERE id=1").fetchone()
            latest_interval = db.execute("SELECT * FROM risk_intervals ORDER BY snapshot_date DESC LIMIT 1").fetchone()
            recent_intervals = db.execute("SELECT * FROM risk_intervals ORDER BY snapshot_date DESC LIMIT 100").fetchall()
        else:
            risk_state = latest_interval = None
            recent_intervals = []
        risk_dict = dict(risk_state) if risk_state else None
        interval_dict = dict(latest_interval) if latest_interval else None
        latest_monitor_row = db.execute("SELECT * FROM monitor_observations ORDER BY observed_at_ms DESC LIMIT 1").fetchone() if "monitor_observations" in tables else None
        monitor_state = db.execute("SELECT * FROM monitor_state WHERE id=1").fetchone() if "monitor_state" in tables else None
        cooldown_state = db.execute("SELECT * FROM cooldown_control WHERE id=1").fetchone() if "cooldown_control" in tables else None
        if monitor_state and int(monitor_state["expected_minutes"]) > 0:
            risk_quality = ("COMPLETE_CURRENT_CYCLE"
                            if monitor_state["status"] == "MONITORING"
                            and int(monitor_state["matched_minutes"]) == int(monitor_state["expected_minutes"])
                            else "PARTIAL_CURRENT_CYCLE")
        else:
            risk_quality = ("NO_MINUTE_HISTORY" if not risk_dict or
                            (risk_dict["complete_intervals"] == 0 and risk_dict["incomplete_intervals"] == 0)
                            else ("COMPLETE" if risk_dict["incomplete_intervals"] == 0 else "PARTIAL_DATA"))
        return {"status":"OK","initial_nav_usdt":account["initial_nav"] if account else None,
            "latest_snapshot":snap_dict,"open_trades":[dict(x) for x in open_rows],
            "closed_trades":[dict(x) for x in closed],
            "portfolio_total_roi_pct":snap_dict.get("total_return_pct") if snap_dict else None,
            "latest_monitor":dict(latest_monitor_row) if latest_monitor_row else None,
            "rebalance_lock":dict(monitor_state) if monitor_state else None,
            "cooldown_control":dict(cooldown_state) if cooldown_state else {"cooldown":0},
            "risk":{"quality_status":risk_quality,"strategy":risk_dict,
                    "latest_interval":interval_dict,"recent_intervals":[dict(x) for x in recent_intervals]}}
    finally:
        db.close()
