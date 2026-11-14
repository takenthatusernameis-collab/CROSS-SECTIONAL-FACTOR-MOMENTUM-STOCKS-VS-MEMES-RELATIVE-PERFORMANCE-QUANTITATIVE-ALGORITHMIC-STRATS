"""Minute-resolution synthetic spread and paper-equity risk reconstruction.

Market candles are used for the relative-price spread. Mark-price candles are used
for portfolio NAV reconstruction so the series is consistent with the daily mark.
OHLC envelopes are conservative proxies: intraminute ordering is unknowable.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

D = Decimal
MINUTE_MS = 60_000
MAX_INTERVAL_MINUTES = 10_080  # Seven days; beyond this, skip minute analytics safely.


class RiskDataError(ValueError):
    pass


def _dec(value: Any, name: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise RiskDataError(f"{name} is missing")
    try:
        result = D(str(value))
    except Exception as exc:
        raise RiskDataError(f"{name} is not numeric") from exc
    if not result.is_finite() or result <= 0:
        raise RiskDataError(f"{name} must be finite and positive")
    return result


def expected_minute_starts(start_ms: int, end_ms: int) -> list[int]:
    """Return only complete one-minute bars wholly inside (start_ms, end_ms)."""
    first = ((start_ms + MINUTE_MS - 1) // MINUTE_MS) * MINUTE_MS
    stop = (end_ms // MINUTE_MS) * MINUTE_MS
    if stop <= first:
        return []
    count = (stop - first) // MINUTE_MS
    if count > MAX_INTERVAL_MINUTES:
        raise RiskDataError(
            f"Minute-risk interval is {count} minutes; safety limit is {MAX_INTERVAL_MINUTES}"
        )
    return list(range(first, stop, MINUTE_MS))


def _candle_map(rows: list[dict[str, Any]], label: str) -> dict[int, dict[str, Decimal]]:
    if not isinstance(rows, list):
        raise RiskDataError(f"{label} candles must be a list")
    decoded: dict[int, dict[str, Decimal]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RiskDataError(f"{label} candle row is malformed")
        raw_ts = row.get("timestamp_ms", row.get("ts"))
        if isinstance(raw_ts, bool):
            raise RiskDataError(f"{label} candle timestamp is invalid")
        try:
            ts = int(raw_ts)
        except (ValueError, TypeError) as exc:
            raise RiskDataError(f"{label} candle timestamp is invalid") from exc
        if ts <= 0 or ts % MINUTE_MS:
            raise RiskDataError(f"{label} candle timestamp is not minute-aligned")
        candle = {
            name: _dec(row.get(name), f"{label} {name}")
            for name in ("open", "high", "low", "close")
        }
        if candle["high"] < max(candle["open"], candle["close"], candle["low"]):
            raise RiskDataError(f"{label} candle high is inconsistent")
        if candle["low"] > min(candle["open"], candle["close"], candle["high"]):
            raise RiskDataError(f"{label} candle low is inconsistent")
        prior = decoded.get(ts)
        if prior is not None and prior != candle:
            raise RiskDataError(f"{label} has conflicting duplicate candle at {ts}")
        decoded[ts] = candle
    return decoded


def _pct(value: Decimal) -> str:
    return format(value * D("100"), "f")


def analyse_intraday_risk(
    *,
    start_ms: int,
    end_ms: int,
    long_position: dict[str, Any],
    short_position: dict[str, Any],
    starting_cash: Decimal,
    previous_nav: Decimal,
    funding_cashflows: list[dict[str, Any]],
    market_candles: dict[str, list[dict[str, Any]]],
    mark_candles: dict[str, list[dict[str, Any]]],
    prior_peak_nav: Decimal,
    prior_peak_time_ms: int | None,
    prior_mdd_pct: Decimal,
    prior_mdd_peak_nav: Decimal | None,
    prior_mdd_trough_nav: Decimal | None,
    prior_mdd_peak_time_ms: int | None,
    prior_mdd_trough_time_ms: int | None,
    prior_daily_peak_nav: Decimal | None = None,
    prior_daily_mdd_pct: Decimal = D("0"),
) -> dict[str, Any]:
    """Reconstruct synchronized spread and NAV envelopes for the held pair.

    Strategy MDD is updated only when every expected minute is available for
    both symbols and both candle types. Partial observations remain reportable
    but cannot silently lower/raise the strategy-level MDD.
    """
    expected = expected_minute_starts(start_ms, end_ms)
    base = {
        "status": "INCOMPLETE",
        "period_start_ms": start_ms,
        "period_end_ms": end_ms,
        "expected_minutes": len(expected),
        "matched_minutes": 0,
        "coverage_pct": "0",
        "missing_minutes": len(expected),
        "spread_open_ratio": None,
        "spread_high_ratio": None,
        "spread_low_ratio": None,
        "spread_close_ratio": None,
        "spread_drawdown_pct": None,
        "nav_start": format(previous_nav, "f"),
        "daily_peak_nav": format(previous_nav, "f"),
        "nav_low": None,
        "nav_low_time_ms": None,
        "nav_close": None,
        "daily_mdd_pct": None,
        "strategy_peak_nav": format(prior_peak_nav, "f"),
        "strategy_peak_time_ms": prior_peak_time_ms,
        "strategy_mdd_pct": format(prior_mdd_pct, "f"),
        "strategy_mdd_peak_nav": format(prior_mdd_peak_nav, "f") if prior_mdd_peak_nav is not None else None,
        "strategy_mdd_trough_nav": format(prior_mdd_trough_nav, "f") if prior_mdd_trough_nav is not None else None,
        "strategy_mdd_peak_time_ms": prior_mdd_peak_time_ms,
        "strategy_mdd_trough_time_ms": prior_mdd_trough_time_ms,
        "minute_rows": [],
    }
    if not expected:
        base["status"] = "NO_COMPLETE_MINUTES"
        return base

    long_symbol = str(long_position["symbol"])
    short_symbol = str(short_position["symbol"])
    if long_symbol == short_symbol:
        raise RiskDataError("Long and short symbols must differ")
    long_qty = _dec(long_position["qty"], "long quantity")
    short_qty = _dec(short_position["qty"], "short quantity")
    long_entry = _dec(long_position["entry_price"], "long entry price")
    short_entry = _dec(short_position["entry_price"], "short entry price")
    cash0 = D(str(starting_cash))
    nav0 = D(str(previous_nav))

    long_market = _candle_map(market_candles.get(long_symbol, []), f"{long_symbol} market")
    short_market = _candle_map(market_candles.get(short_symbol, []), f"{short_symbol} market")
    long_mark = _candle_map(mark_candles.get(long_symbol, []), f"{long_symbol} mark")
    short_mark = _candle_map(mark_candles.get(short_symbol, []), f"{short_symbol} mark")
    common = [
        ts for ts in expected
        if ts in long_market and ts in short_market and ts in long_mark and ts in short_mark
    ]
    matched = len(common)
    coverage = D(matched) / D(len(expected)) * D("100")
    base["matched_minutes"] = matched
    base["coverage_pct"] = format(coverage, "f")
    base["missing_minutes"] = len(expected) - matched
    complete = matched == len(expected)
    base["status"] = "COMPLETE" if complete else "INCOMPLETE"
    if not common:
        return base

    open_ratio = long_entry / short_entry
    spread_high = open_ratio
    spread_low = open_ratio
    spread_close = open_ratio
    daily_peak = max(nav0, D(str(prior_daily_peak_nav))) if prior_daily_peak_nav is not None else nav0
    if not daily_peak.is_finite() or daily_peak <= 0:
        raise RiskDataError("Prior daily NAV peak must be finite and positive")
    daily_mdd = D(str(prior_daily_mdd_pct))
    if not daily_mdd.is_finite() or daily_mdd > 0:
        raise RiskDataError("Prior daily MDD must be a finite non-positive percentage")
    nav_low_seen: Decimal | None = None
    nav_low_time: int | None = None

    peak_nav = D(str(prior_peak_nav))
    peak_time = prior_peak_time_ms
    mdd = D(str(prior_mdd_pct))
    mdd_peak_nav = prior_mdd_peak_nav
    mdd_trough_nav = prior_mdd_trough_nav
    mdd_peak_time = prior_mdd_peak_time_ms
    mdd_trough_time = prior_mdd_trough_time_ms

    flows: list[tuple[int, Decimal]] = []
    for event in funding_cashflows:
        try:
            event_ms = int(event["time_ms"])
            payment = D(str(event["payment"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise RiskDataError("Funding cashflow input is malformed") from exc
        if not payment.is_finite():
            raise RiskDataError("Funding cashflow is non-finite")
        if start_ms < event_ms <= end_ms:
            flows.append((event_ms, payment))
    flows.sort(key=lambda x: x[0])
    flow_index = 0
    funding_accrued = D("0")
    rows: list[dict[str, Any]] = []

    for ts in common:
        while flow_index < len(flows) and flows[flow_index][0] <= ts:
            funding_accrued += flows[flow_index][1]
            flow_index += 1
        lm, sm = long_market[ts], short_market[ts]
        lp, sp = long_mark[ts], short_mark[ts]

        ratio_high = lm["high"] / sm["low"]
        ratio_low = lm["low"] / sm["high"]
        ratio_close = lm["close"] / sm["close"]
        spread_high = max(spread_high, ratio_high)
        spread_low = min(spread_low, ratio_low)
        spread_close = ratio_close

        cash = cash0 + funding_accrued
        nav_high = cash + long_qty * (lp["high"] - long_entry) - short_qty * (sp["low"] - short_entry)
        nav_low = cash + long_qty * (lp["low"] - long_entry) - short_qty * (sp["high"] - short_entry)
        nav_close = cash + long_qty * (lp["close"] - long_entry) - short_qty * (sp["close"] - short_entry)
        if nav_low_seen is None or nav_low < nav_low_seen:
            nav_low_seen, nav_low_time = nav_low, ts

        daily_peak = max(daily_peak, nav_high)
        daily_drawdown = (nav_low / daily_peak - D("1")) * D("100")
        daily_mdd = min(daily_mdd, daily_drawdown)

        # Only a complete paired minute history is allowed to update strategy MDD.
        if complete:
            if nav_high > peak_nav:
                peak_nav, peak_time = nav_high, ts
            drawdown = (nav_low / peak_nav - D("1")) * D("100")
            if drawdown < mdd:
                mdd = drawdown
                mdd_peak_nav = peak_nav
                mdd_trough_nav = nav_low
                mdd_peak_time = peak_time
                mdd_trough_time = ts

        rows.append({
            "timestamp_ms": ts,
            "spread_ratio_close": format(ratio_close, "f"),
            "spread_ratio_high": format(ratio_high, "f"),
            "spread_ratio_low": format(ratio_low, "f"),
            "nav_close": format(nav_close, "f"),
            "nav_high": format(nav_high, "f"),
            "nav_low": format(nav_low, "f"),
        })

    base.update({
        "spread_open_ratio": format(open_ratio, "f"),
        "spread_high_ratio": format(spread_high, "f"),
        "spread_low_ratio": format(spread_low, "f"),
        "spread_close_ratio": format(spread_close, "f"),
        "spread_drawdown_pct": _pct(spread_low / spread_high - D("1")),
        "nav_low": format(nav_low_seen, "f") if nav_low_seen is not None else None,
        "nav_low_time_ms": nav_low_time,
        "nav_close": rows[-1]["nav_close"],
        "daily_mdd_pct": format(daily_mdd, "f"),
        "daily_peak_nav": format(daily_peak, "f"),
        "minute_rows": rows,
    })
    if complete:
        base.update({
            "strategy_peak_nav": format(peak_nav, "f"),
            "strategy_peak_time_ms": peak_time,
            "strategy_mdd_pct": format(mdd, "f"),
            "strategy_mdd_peak_nav": format(mdd_peak_nav, "f") if mdd_peak_nav is not None else None,
            "strategy_mdd_trough_nav": format(mdd_trough_nav, "f") if mdd_trough_nav is not None else None,
            "strategy_mdd_peak_time_ms": mdd_peak_time,
            "strategy_mdd_trough_time_ms": mdd_trough_time,
        })
    return base
