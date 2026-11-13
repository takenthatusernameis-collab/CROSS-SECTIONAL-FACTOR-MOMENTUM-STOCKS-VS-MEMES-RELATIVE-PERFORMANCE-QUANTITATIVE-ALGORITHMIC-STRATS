"""Pure ranking and timestamp-quality rules for Kalshi ranking diagnostics."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
import math

LONG_ASSET_CLASSES = frozenset({"Equities", "Indices"})
SHORT_ASSET_CLASSES = frozenset({"Crypto"})
MAX_PRICE_AGE_SECONDS = 900
REFERENCE_TOLERANCE_SECONDS = 900
LOOKBACK_SECONDS = 24 * 60 * 60


def split_active_universe(markets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Classify by exact Kalshi asset_class only; Equities and Indices are one long pool."""
    seen: set[str] = set()
    long_markets: list[dict[str, Any]] = []
    short_markets: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for market in markets:
        if not isinstance(market, dict):
            raise ValueError("Every market item must be a JSON object")
        ticker = market.get("ticker")
        asset_class = market.get("asset_class")
        if not isinstance(ticker, str) or not ticker.strip():
            raise ValueError("Active market is missing a valid ticker")
        if ticker in seen:
            raise ValueError(f"Duplicate ticker in active market list: {ticker}")
        seen.add(ticker)
        if asset_class in LONG_ASSET_CLASSES:
            long_markets.append(market)
        elif asset_class in SHORT_ASSET_CLASSES:
            short_markets.append(market)
        else:
            excluded.append(market)
    return {"long": long_markets, "short": short_markets, "excluded": excluded}


def _valid_price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not price.is_finite() or price <= 0:
        return None
    return price


def valid_candle_closes(candles: list[dict[str, Any]]) -> list[tuple[int, Decimal]]:
    """Return sorted timestamp/close observations, omitting null/invalid close values."""
    rows: list[tuple[int, Decimal]] = []
    for candle in candles:
        if not isinstance(candle, dict):
            continue
        timestamp = candle.get("end_period_ts")
        price = candle.get("price")
        close = _valid_price(price.get("close")) if isinstance(price, dict) else None
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            continue
        if isinstance(timestamp, float) and not math.isfinite(timestamp):
            continue
        ts = int(timestamp)
        if close is not None:
            rows.append((ts, close))
    rows.sort(key=lambda row: row[0])
    return rows


def calculate_trailing_return(
    candles: list[dict[str, Any]],
    as_of_ts: int,
    max_price_age_seconds: int = MAX_PRICE_AGE_SECONDS,
    reference_tolerance_seconds: int = REFERENCE_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    """Calculate a near-24h return from completed trade-price closes with bounded gaps."""
    valid = valid_candle_closes(candles)
    if len(valid) < 2:
        return {"valid": False, "reason": "insufficient_valid_trade_closes",
                "valid_close_count": len(valid)}
    completed_cutoff = as_of_ts - (as_of_ts % 60)
    current_candidates = [(ts, px) for ts, px in valid if ts <= completed_cutoff]
    if not current_candidates:
        return {"valid": False, "reason": "no_completed_current_close",
                "valid_close_count": len(valid)}
    current_ts, current_price = current_candidates[-1]
    current_age = completed_cutoff - current_ts
    if current_age < 0 or current_age > max_price_age_seconds:
        return {"valid": False, "reason": "stale_current_price",
                "valid_close_count": len(valid), "current_price_ts": current_ts,
                "current_price_age_seconds": current_age}

    reference_cutoff = current_ts - LOOKBACK_SECONDS
    reference_candidates = [(ts, px) for ts, px in valid if ts <= reference_cutoff]
    if not reference_candidates:
        return {"valid": False, "reason": "missing_24h_reference_close",
                "valid_close_count": len(valid), "current_price_ts": current_ts,
                "current_price_age_seconds": current_age}
    reference_ts, reference_price = reference_candidates[-1]
    reference_gap = reference_cutoff - reference_ts
    if reference_gap < 0 or reference_gap > reference_tolerance_seconds:
        return {"valid": False, "reason": "stale_24h_reference_price",
                "valid_close_count": len(valid), "current_price_ts": current_ts,
                "current_price_age_seconds": current_age, "reference_price_ts": reference_ts,
                "reference_gap_seconds": reference_gap}

    elapsed = current_ts - reference_ts
    if elapsed < LOOKBACK_SECONDS:
        return {"valid": False, "reason": "reference_window_not_at_least_24h",
                "valid_close_count": len(valid)}
    pct = (current_price / reference_price - Decimal("1")) * Decimal("100")
    return {
        "valid": True, "reason": None,
        "return_24h_pct": format(pct, ".8f"),
        "current_price": format(current_price, "f"),
        "current_price_ts": current_ts,
        "current_price_age_seconds": current_age,
        "reference_price": format(reference_price, "f"),
        "reference_price_ts": reference_ts,
        "reference_gap_seconds": reference_gap,
        "observation_interval_seconds": elapsed,
        "valid_close_count": len(valid),
    }


def build_provisional_leaders(
    long_records: list[dict[str, Any]], short_records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return diagnostic leaders; this function never authorizes a position."""
    valid_long = [r for r in long_records if r.get("valid")]
    valid_short = [r for r in short_records if r.get("valid")]
    long_leader = max(valid_long, key=lambda r: Decimal(r["return_24h_pct"])) if valid_long else None
    short_leader = min(valid_short, key=lambda r: Decimal(r["return_24h_pct"])) if valid_short else None
    return {
        "provisional_long_leader": long_leader,
        "provisional_short_leader": short_leader,
        "long_valid_count": len(valid_long),
        "short_valid_count": len(valid_short),
    }
