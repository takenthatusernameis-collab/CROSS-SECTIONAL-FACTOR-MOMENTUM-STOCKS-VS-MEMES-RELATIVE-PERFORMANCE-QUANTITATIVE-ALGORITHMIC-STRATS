"""Five-minute mark-to-market monitoring for the locked Bitget paper pair.

Monitoring writes analytics only. It never changes account cash, positions, trades,
funding ledger, or daily portfolio snapshots. Those are changed only by run_snapshot
after the 24h holding lock and final one-minute candle close have both passed.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .portfolio import (
    PaperError, cooldown_close_ms, cooldown_reset_ms, dec, fmt, init_db, utc_iso,
)
from .risk import analyse_intraday_risk, expected_minute_starts

D = Decimal
MINUTE_MS = 60_000
MAX_MARKET_AGE_MS = 900_000


def _event_payment(leg: str, qty: Decimal, rate: Decimal, mark: Decimal) -> Decimal:
    direction = D("1") if leg == "LONG" else D("-1")
    return -direction * qty * mark * rate


def _ensure_cycle(db: sqlite3.Connection, positions: dict[str, dict[str, Any]],
                  initial_nav: Decimal) -> sqlite3.Row:
    long_pos, short_pos = positions["LONG"], positions["SHORT"]
    state = db.execute("SELECT * FROM monitor_state WHERE id=1").fetchone()
    valid = bool(
        state is not None
        and int(state["long_trade_id"]) == int(long_pos["trade_id"])
        and int(state["short_trade_id"]) == int(short_pos["trade_id"])
    )
    if valid:
        return state

    control = db.execute("SELECT activated_at_ms,reset_at_ms FROM cooldown_control WHERE id=1").fetchone()
    entries = [int(long_pos["entry_time_ms"]), int(short_pos["entry_time_ms"])]
    cycle_start = int(control["activated_at_ms"]) if control and control["activated_at_ms"] is not None else max(entries)
    eligible_after = int(control["reset_at_ms"]) if control and control["reset_at_ms"] is not None else cooldown_reset_ms(cycle_start)
    snapshot = db.execute(
        "SELECT as_of_ms,nav FROM daily_snapshots ORDER BY as_of_ms DESC LIMIT 1"
    ).fetchone()
    nav_start = dec(snapshot["nav"], "cycle opening NAV", positive=True) if snapshot else initial_nav
    spread_open = dec(long_pos["entry_price"], "long entry price", positive=True) / dec(
        short_pos["entry_price"], "short entry price", positive=True
    )
    db.execute("""INSERT OR REPLACE INTO monitor_state(
        id,cycle_start_ms,eligible_after_ms,long_trade_id,short_trade_id,
        last_processed_minute_ms,last_observed_at_ms,nav_start,nav_last,funding_accrued,
        spread_open_ratio,spread_high_ratio,spread_low_ratio,spread_close_ratio,
        nav_low_intraday,nav_low_time_ms,daily_peak_nav,daily_mdd_pct,
        expected_minutes,matched_minutes,status,last_error)
        VALUES(1,?,?,?,?,NULL,NULL,?,?,?, ?,?,?,?,NULL,NULL,?,'0',0,0,'WAITING',NULL)""",
        (cycle_start, eligible_after, int(long_pos["trade_id"]), int(short_pos["trade_id"]),
         fmt(nav_start), fmt(nav_start), "0", fmt(spread_open), fmt(spread_open),
         fmt(spread_open), fmt(spread_open), fmt(nav_start)))
    state = db.execute("SELECT * FROM monitor_state WHERE id=1").fetchone()
    if state is None:
        raise PaperError("Unable to initialize monitor cycle state")
    return state


def monitor_open_positions(
    db_path: str | Path,
    *,
    now_ms: int,
    initial_nav: Decimal,
    ticker_fetch: Callable[[list[str], int], dict[str, dict[str, Any]]],
    funding_fetch: Callable[[str, int, int], list[dict[str, Any]]],
    funding_mark_fetch: Callable[[str, int], Decimal],
    candle_fetch: Callable[[str, int, int, str], list[dict[str, Any]]],
    metrics_end_ms: int | None = None,
) -> dict[str, Any]:
    """Mark open legs and process bars only through the workflow-start boundary."""
    initial_nav = dec(initial_nav, "initial NAV", positive=True)
    metrics_end = int(metrics_end_ms) if metrics_end_ms is not None else int(now_ms)
    if metrics_end > int(now_ms):
        raise PaperError("Risk-window end cannot be later than the live monitoring timestamp")
    db = init_db(db_path, initial_nav)
    try:
        positions_rows = db.execute("""SELECT p.*,t.entry_time_ms,t.entry_notional,
            t.entry_fee,t.funding_pnl FROM positions p JOIN trades t ON t.trade_id=p.trade_id""").fetchall()
        positions = {row["leg"]: dict(row) for row in positions_rows}
        if not positions:
            return {"status": "NO_OPEN_POSITIONS", "updated": False}
        if set(positions) != {"LONG", "SHORT"}:
            raise PaperError("Monitoring requires exactly one open LONG and one open SHORT position")

        account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
        if account is None:
            raise PaperError("Paper account row is missing")
        state = _ensure_cycle(db, positions, initial_nav)
        state_before = dict(state)
        account_cash = dec(account["cash"], "account cash")
        initial_nav_stored = dec(account["initial_nav"], "stored initial NAV", positive=True)

        symbols = [positions["LONG"]["symbol"], positions["SHORT"]["symbol"]]
        tickers = ticker_fetch(symbols, int(now_ms))
        for symbol in symbols:
            ticker = tickers.get(symbol)
            if not isinstance(ticker, dict):
                raise PaperError(f"{symbol}: monitoring ticker is missing")
            stamp = int(dec(ticker.get("timestamp_ms"), f"{symbol} ticker timestamp", positive=True))
            if abs(int(now_ms) - stamp) > MAX_MARKET_AGE_MS:
                raise PaperError(f"{symbol}: monitoring ticker is stale")
            dec(ticker.get("mark_price"), f"{symbol} mark price", positive=True)
            dec(ticker.get("last_price"), f"{symbol} last price", positive=True)

        # Re-read pending funding from each position's last committed settlement
        # cursor. This is deliberately not committed into account/trade/ledger here.
        events_by_leg: dict[str, list[dict[str, Any]]] = {}
        funding_by_leg: dict[str, Decimal] = {"LONG": D("0"), "SHORT": D("0")}
        all_flows: list[dict[str, Any]] = []
        for leg, position in positions.items():
            events = funding_fetch(position["symbol"], int(position["last_funding_ms"]), int(now_ms))
            if not isinstance(events, list):
                raise PaperError(f"{position['symbol']}: funding response is not a list")
            checked: list[dict[str, Any]] = []
            qty = dec(position["qty"], f"{leg} quantity", positive=True)
            for event in events:
                if not isinstance(event, dict) or event.get("symbol") != position["symbol"]:
                    raise PaperError(f"{position['symbol']}: malformed funding record")
                event_ms = int(dec(event.get("fundingTime"), "fundingTime", positive=True))
                if not int(position["last_funding_ms"]) < event_ms <= int(now_ms):
                    continue
                rate = dec(event.get("fundingRate"), "fundingRate")
                mark = dec(funding_mark_fetch(position["symbol"], event_ms), "funding mark", positive=True)
                payment = _event_payment(leg, qty, rate, mark)
                checked.append({"time_ms": event_ms, "payment": fmt(payment),
                                "rate": rate, "mark": mark})
                funding_by_leg[leg] += payment
                all_flows.append({"time_ms": event_ms, "payment": fmt(payment)})
            events_by_leg[leg] = sorted(checked, key=lambda item: item["time_ms"])

        funding_total = funding_by_leg["LONG"] + funding_by_leg["SHORT"]
        mark_long = dec(tickers[positions["LONG"]["symbol"]]["mark_price"], "long mark", positive=True)
        mark_short = dec(tickers[positions["SHORT"]["symbol"]]["mark_price"], "short mark", positive=True)
        last_long = dec(tickers[positions["LONG"]["symbol"]]["last_price"], "long last", positive=True)
        last_short = dec(tickers[positions["SHORT"]["symbol"]]["last_price"], "short last", positive=True)

        unrealized_by_leg: dict[str, Decimal] = {}
        roi_by_leg: dict[str, Decimal] = {}
        gross_exposure = D("0")
        unrealized_total = D("0")
        for leg, mark in (("LONG", mark_long), ("SHORT", mark_short)):
            position = positions[leg]
            qty = dec(position["qty"], f"{leg} quantity", positive=True)
            entry = dec(position["entry_price"], f"{leg} entry price", positive=True)
            entry_fee = dec(position["entry_fee"], f"{leg} entry fee")
            entry_notional = dec(position["entry_notional"], f"{leg} entry notional", positive=True)
            fee_rate = dec(position["entry_fee_rate"], f"{leg} fee rate")
            sign = D("1") if leg == "LONG" else D("-1")
            unrealized = (mark - entry) * qty * sign
            estimated_exit_fee = mark * qty * fee_rate
            net_open_pnl = unrealized - entry_fee - estimated_exit_fee + funding_by_leg[leg]
            unrealized_by_leg[leg] = unrealized
            roi_by_leg[leg] = net_open_pnl / entry_notional * D("100")
            unrealized_total += unrealized
            gross_exposure += mark * qty

        cash_now = account_cash + funding_total
        nav_now = cash_now + unrealized_total
        if nav_now <= 0:
            raise PaperError("Monitored paper NAV is non-positive")
        portfolio_roi = (nav_now / initial_nav_stored - D("1")) * D("100")
        spread_mark = last_long / last_short

        cycle_start = int(state_before["cycle_start_ms"])
        previous_processed = state_before["last_processed_minute_ms"]
        segment_start = cycle_start if previous_processed is None else int(previous_processed) + MINUTE_MS
        segment_end = (metrics_end // MINUTE_MS) * MINUTE_MS
        analysis: dict[str, Any] | None = None
        risk_error: str | None = None
        if segment_end > segment_start:
            try:
                market_bars: dict[str, list[dict[str, Any]]] = {}
                mark_bars: dict[str, list[dict[str, Any]]] = {}
                for symbol in sorted(symbols):
                    market_bars[symbol] = candle_fetch(symbol, segment_start, segment_end, "market")
                    mark_bars[symbol] = candle_fetch(symbol, segment_start, segment_end, "mark")

                # The function uses a strict start bound for funding events. Move
                # the bound back 1ms so a settlement exactly at the first bar's
                # timestamp is included in that bar's NAV.
                analysis_start = max(0, segment_start - 1)
                pending_before = sum(
                    (D(item["payment"]) for item in all_flows if item["time_ms"] <= analysis_start),
                    D("0"),
                )
                interval_flows = [
                    {"time_ms": item["time_ms"], "payment": item["payment"]}
                    for item in all_flows if analysis_start < item["time_ms"] <= segment_end
                ]
                risk_state = db.execute("SELECT * FROM strategy_risk WHERE id=1").fetchone()
                if risk_state is None:
                    seed = max(initial_nav_stored, dec(state_before["nav_start"], "cycle NAV start", positive=True))
                    db.execute("INSERT INTO strategy_risk(id,peak_nav,peak_time_ms,max_drawdown_pct) VALUES(1,?,?,'0')",
                               (fmt(seed), cycle_start))
                    risk_state = db.execute("SELECT * FROM strategy_risk WHERE id=1").fetchone()
                analysis = analyse_intraday_risk(
                    start_ms=analysis_start, end_ms=segment_end,
                    long_position=positions["LONG"], short_position=positions["SHORT"],
                    starting_cash=account_cash + pending_before,
                    previous_nav=dec(state_before["nav_last"], "previous monitor NAV", positive=True),
                    funding_cashflows=interval_flows,
                    market_candles=market_bars, mark_candles=mark_bars,
                    prior_peak_nav=dec(risk_state["peak_nav"], "strategy peak NAV", positive=True),
                    prior_peak_time_ms=risk_state["peak_time_ms"],
                    prior_mdd_pct=dec(risk_state["max_drawdown_pct"], "strategy MDD"),
                    prior_mdd_peak_nav=dec(risk_state["mdd_peak_nav"], "MDD peak") if risk_state["mdd_peak_nav"] is not None else None,
                    prior_mdd_trough_nav=dec(risk_state["mdd_trough_nav"], "MDD trough") if risk_state["mdd_trough_nav"] is not None else None,
                    prior_mdd_peak_time_ms=risk_state["mdd_peak_time_ms"],
                    prior_mdd_trough_time_ms=risk_state["mdd_trough_time_ms"],
                    prior_daily_peak_nav=dec(state_before["daily_peak_nav"], "holding-cycle NAV peak", positive=True),
                    prior_daily_mdd_pct=dec(state_before["daily_mdd_pct"], "holding-cycle MDD"),
                )
            except Exception as exc:
                risk_error = f"{type(exc).__name__}: {str(exc)[:800]}"
                analysis = None

        db.execute("BEGIN IMMEDIATE")
        # Guard against a concurrent paper transition while the public-data
        # requests were in flight. Never apply observations to a different pair.
        current_ids = {r["leg"]: int(r["trade_id"]) for r in db.execute("SELECT leg,trade_id FROM positions")}
        expected_ids = {"LONG": int(positions["LONG"]["trade_id"]),
                        "SHORT": int(positions["SHORT"]["trade_id"])}
        if current_ids != expected_ids:
            db.execute("ROLLBACK")
            return {"status": "STALE_MONITOR_CYCLE", "updated": False,
                    "message": "Positions changed during monitoring; this observation was discarded."}

        latest_state = db.execute("SELECT * FROM monitor_state WHERE id=1").fetchone()
        if latest_state is None or int(latest_state["long_trade_id"]) != expected_ids["LONG"] or int(latest_state["short_trade_id"]) != expected_ids["SHORT"]:
            db.execute("ROLLBACK")
            return {"status": "STALE_MONITOR_CYCLE", "updated": False}
        # A concurrent monitor may have advanced the checkpoint. Do not replay or
        # double-count its bars.
        if latest_state["last_processed_minute_ms"] != state_before["last_processed_minute_ms"]:
            db.execute("ROLLBACK")
            return {"status": "STALE_MONITOR_CHECKPOINT", "updated": False}

        last_processed = latest_state["last_processed_minute_ms"]
        spread_high = dec(latest_state["spread_high_ratio"], "spread high", positive=True)
        spread_low = dec(latest_state["spread_low_ratio"], "spread low", positive=True)
        spread_close = dec(latest_state["spread_close_ratio"], "spread close", positive=True) if latest_state["spread_close_ratio"] else spread_mark
        nav_low = dec(latest_state["nav_low_intraday"], "intraday NAV low", positive=True) if latest_state["nav_low_intraday"] else None
        nav_low_time = latest_state["nav_low_time_ms"]
        daily_peak = dec(latest_state["daily_peak_nav"], "daily NAV peak", positive=True)
        daily_mdd = dec(latest_state["daily_mdd_pct"], "daily MDD")
        status = "WAITING_FOR_CLOSED_MINUTE"
        complete_segment = bool(analysis and analysis.get("status") == "COMPLETE")
        if analysis and complete_segment:
            spread_high = max(spread_high, dec(analysis["spread_high_ratio"], "segment spread high", positive=True))
            spread_low = min(spread_low, dec(analysis["spread_low_ratio"], "segment spread low", positive=True))
            spread_close = dec(analysis["spread_close_ratio"], "segment spread close", positive=True)
            segment_low = dec(analysis["nav_low"], "segment NAV low", positive=True)
            if nav_low is None or segment_low < nav_low:
                nav_low = segment_low
                nav_low_time = analysis.get("nav_low_time_ms")
            daily_peak = max(daily_peak, dec(analysis["daily_peak_nav"], "daily peak NAV", positive=True))
            daily_mdd = min(daily_mdd, dec(analysis["daily_mdd_pct"], "daily MDD"))
            last_processed = max((int(row["timestamp_ms"]) for row in analysis.get("minute_rows", [])), default=last_processed)
            for point in analysis.get("minute_rows", []):
                db.execute("""INSERT OR REPLACE INTO monitor_minute_metrics(
                    cycle_start_ms,timestamp_ms,long_trade_id,short_trade_id,long_symbol,short_symbol,
                    spread_ratio_close,spread_ratio_high,spread_ratio_low,nav_close,nav_high,nav_low)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cycle_start,int(point["timestamp_ms"]),expected_ids["LONG"],expected_ids["SHORT"],
                     positions["LONG"]["symbol"],positions["SHORT"]["symbol"],
                     point["spread_ratio_close"],point["spread_ratio_high"],point["spread_ratio_low"],
                     point["nav_close"],point["nav_high"],point["nav_low"]))
            status = "MONITORING"
            risk_state = db.execute("SELECT * FROM strategy_risk WHERE id=1").fetchone()
            if risk_state is not None and analysis.get("expected_minutes", 0) > 0:
                db.execute("""UPDATE strategy_risk SET peak_nav=?,peak_time_ms=?,max_drawdown_pct=?,
                    mdd_peak_nav=?,mdd_trough_nav=?,mdd_peak_time_ms=?,mdd_trough_time_ms=?
                    WHERE id=1""",
                    (analysis["strategy_peak_nav"],analysis.get("strategy_peak_time_ms"),
                     analysis["strategy_mdd_pct"],analysis.get("strategy_mdd_peak_nav"),
                     analysis.get("strategy_mdd_trough_nav"),analysis.get("strategy_mdd_peak_time_ms"),
                     analysis.get("strategy_mdd_trough_time_ms")))
        elif risk_error or (analysis and analysis.get("status") == "INCOMPLETE"):
            status = "MONITORING_INCOMPLETE"

        expected_total = len(expected_minute_starts(cycle_start, segment_end)) if segment_end > cycle_start else 0
        matched_total = int(db.execute(
            "SELECT COUNT(*) FROM monitor_minute_metrics WHERE cycle_start_ms=?", (cycle_start,)
        ).fetchone()[0])
        coverage = (D(matched_total) / D(expected_total) * D("100")) if expected_total else D("0")
        if expected_total and matched_total == expected_total:
            status = "MONITORING"
        elif expected_total and status != "MONITORING_INCOMPLETE":
            status = "MONITORING_INCOMPLETE"

        spread_drawdown = (spread_low / spread_high - D("1")) * D("100")
        seconds_to_rebalance = max(0, int((int(latest_state["eligible_after_ms"]) - int(now_ms) + 999) // 1000))
        checkpoint_nav = dec(state_before["nav_last"], "last contiguous minute NAV", positive=True)
        if complete_segment and analysis and analysis.get("nav_close") is not None:
            checkpoint_nav = dec(analysis["nav_close"], "contiguous segment closing NAV", positive=True)
        cycle_nav_start = dec(state_before["nav_start"], "workflow-cycle starting NAV", positive=True)
        cycle_roi_pct = (nav_now / cycle_nav_start - D("1")) * D("100")
        db.execute("""UPDATE monitor_state SET
            last_processed_minute_ms=?,last_observed_at_ms=?,nav_last=?,funding_accrued=?,
            spread_high_ratio=?,spread_low_ratio=?,spread_close_ratio=?,nav_low_intraday=?,
            nav_low_time_ms=?,daily_peak_nav=?,daily_mdd_pct=?,expected_minutes=?,
            matched_minutes=?,status=?,last_error=? WHERE id=1""",
            (last_processed,int(now_ms),fmt(checkpoint_nav),fmt(funding_total),fmt(spread_high),fmt(spread_low),
             fmt(spread_mark if analysis is None else spread_close),
             fmt(nav_low) if nav_low is not None else None,nav_low_time,fmt(daily_peak),fmt(daily_mdd),
             expected_total,matched_total,status,risk_error))

        long_pos, short_pos = positions["LONG"], positions["SHORT"]
        db.execute("""INSERT OR REPLACE INTO monitor_observations(
            observed_at_ms,cycle_start_ms,eligible_after_ms,status,nav_usdt,cash_usdt,
            unrealized_pnl_usdt,funding_pending_usdt,gross_exposure_usdt,portfolio_roi_pct,cycle_roi_pct,
            long_symbol,long_mark_price,long_unrealized_pnl_usdt,long_trade_roi_pct,
            short_symbol,short_mark_price,short_unrealized_pnl_usdt,short_trade_roi_pct,
            spread_ratio_mark,spread_open_ratio,spread_high_ratio,spread_low_ratio,
            spread_drawdown_pct,daily_mdd_pct,strategy_mdd_pct,expected_minutes,
            matched_minutes,coverage_pct,seconds_to_rebalance,last_error)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(now_ms),cycle_start,int(latest_state["eligible_after_ms"]),status,fmt(nav_now),
             fmt(cash_now),fmt(unrealized_total),fmt(funding_total),fmt(gross_exposure),fmt(portfolio_roi),
             fmt(cycle_roi_pct),long_pos["symbol"],fmt(mark_long),fmt(unrealized_by_leg["LONG"]),fmt(roi_by_leg["LONG"]),
             short_pos["symbol"],fmt(mark_short),fmt(unrealized_by_leg["SHORT"]),fmt(roi_by_leg["SHORT"]),
             fmt(spread_mark),fmt(dec(latest_state["spread_open_ratio"],"spread open",positive=True)),
             fmt(spread_high),fmt(spread_low),fmt(spread_drawdown),fmt(daily_mdd),
             (db.execute("SELECT max_drawdown_pct FROM strategy_risk WHERE id=1").fetchone() or ["0"])[0],
             expected_total,matched_total,fmt(coverage),seconds_to_rebalance,risk_error))
        db.execute("COMMIT")

        return {
            "status": status, "updated": False, "analytics_updated": True,
            "cycle_start_ms": cycle_start, "period_end_ms": metrics_end,
            "eligible_after_ms": int(latest_state["eligible_after_ms"]),
            "seconds_to_rebalance": seconds_to_rebalance,
            "nav_usdt": fmt(nav_now), "portfolio_roi_pct": fmt(portfolio_roi),
            "cycle_roi_pct": fmt(cycle_roi_pct),
            "funding_pending_usdt": fmt(funding_total), "gross_exposure_usdt": fmt(gross_exposure),
            "open_positions": {
                "LONG": {"symbol":long_pos["symbol"],"mark_price":fmt(mark_long),
                         "unrealized_pnl_usdt":fmt(unrealized_by_leg["LONG"]),
                         "net_trade_roi_estimate_pct":fmt(roi_by_leg["LONG"])},
                "SHORT": {"symbol":short_pos["symbol"],"mark_price":fmt(mark_short),
                          "unrealized_pnl_usdt":fmt(unrealized_by_leg["SHORT"]),
                          "net_trade_roi_estimate_pct":fmt(roi_by_leg["SHORT"])},
            },
            "spread_ratio_mark":fmt(spread_mark),"spread_high_ratio":fmt(spread_high),
            "spread_low_ratio":fmt(spread_low),"spread_drawdown_pct":fmt(spread_drawdown),
            "daily_mdd_pct":fmt(daily_mdd),
            "strategy_mdd_pct":(db.execute("SELECT max_drawdown_pct FROM strategy_risk WHERE id=1").fetchone() or ["0"])[0],
            "expected_minutes":expected_total,"matched_minutes":matched_total,
            "coverage_pct":fmt(coverage),"last_error":risk_error,
            "portfolio_state_changed":False,
        }
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
