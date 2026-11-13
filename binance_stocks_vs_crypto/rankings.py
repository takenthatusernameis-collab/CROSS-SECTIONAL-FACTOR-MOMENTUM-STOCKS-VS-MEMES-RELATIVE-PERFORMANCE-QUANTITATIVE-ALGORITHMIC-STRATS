"""Universe classification and return-quality rules for Binance perpetuals."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
import math

# Exact Binance exchangeInfo underlyingType values. Commodity/FX/unknown classes
# are deliberately excluded; ticker/title text is never used to infer a class.
LONG_UNDERLYING_TYPES = frozenset({
    "EQUITY", "CN_EQUITY", "KR_EQUITY", "HK_EQUITY", "PREMARKET", "INDEX",
})
SHORT_UNDERLYING_TYPES = frozenset({"COIN"})
LONG_CONTRACT_TYPE = "TRADIFI_PERPETUAL"
SHORT_CONTRACT_TYPE = "PERPETUAL"
QUOTE_ASSET = "USDT"
MAX_PRICE_AGE_SECONDS = 900
REFERENCE_TOLERANCE_SECONDS = 900
LOOKBACK_MILLISECONDS = 24 * 60 * 60 * 1000
ONE_MINUTE_MILLISECONDS = 60_000


def classify_binance_universe(symbols: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Classify currently trading USDT contracts using Binance metadata only."""
    seen: set[str] = set()
    long_contracts: list[dict[str, Any]] = []
    short_contracts: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for item in symbols:
        if not isinstance(item, dict):
            raise ValueError("Every exchangeInfo symbol must be a JSON object")
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("ExchangeInfo item is missing a valid symbol")
        if symbol in seen:
            raise ValueError(f"Duplicate Binance symbol: {symbol}")
        seen.add(symbol)

        status = item.get("status")
        contract_type = item.get("contractType")
        underlying_type = item.get("underlyingType")
        quote_asset = item.get("quoteAsset")
        margin_asset = item.get("marginAsset")
        if status != "TRADING":
            reason = "not_trading"
        elif quote_asset != QUOTE_ASSET or margin_asset != QUOTE_ASSET:
            reason = "not_usdt_quoted_and_margined"
        elif contract_type == LONG_CONTRACT_TYPE and underlying_type in LONG_UNDERLYING_TYPES:
            long_contracts.append(item)
            continue
        elif contract_type == SHORT_CONTRACT_TYPE and underlying_type in SHORT_UNDERLYING_TYPES:
            short_contracts.append(item)
            continue
        else:
            reason = "contract_or_underlying_type_not_in_approved_universe"
        excluded.append({"symbol": symbol, "contractType": contract_type,
                         "underlyingType": underlying_type, "reason": reason})

    return {"long": long_contracts, "short": short_contracts, "excluded": excluded}


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


def valid_candle_closes(candles: list[list[Any]]) -> list[tuple[int, Decimal]]:
    """Return (close-time-ms, close) for completed, traded 1m kline rows only."""
    rows: dict[int, Decimal] = {}
    for candle in candles:
        if not isinstance(candle, (list, tuple)) or len(candle) < 9:
            continue
        close_time = candle[6]
        trade_count = candle[8]
        if isinstance(close_time, bool) or not isinstance(close_time, (int, float)):
            continue
        if isinstance(close_time, float) and not math.isfinite(close_time):
            continue
        if isinstance(trade_count, bool):
            continue
        try:
            trades = int(trade_count)
        except (ValueError, TypeError, OverflowError):
            continue
        if trades <= 0:
            continue
        price = _valid_price(candle[4])
        if price is None:
            continue
        rows[int(close_time)] = price
    return sorted(rows.items())


def calculate_trailing_return(
    candles: list[list[Any]],
    as_of_ms: int,
    max_price_age_seconds: int = MAX_PRICE_AGE_SECONDS,
    reference_tolerance_seconds: int = REFERENCE_TOLERANCE_SECONDS,
) -> dict[str, Any]:
    """Calculate a near-24h return from completed, non-empty 1m contract klines."""
    all_valid = valid_candle_closes(candles)
    # Latest fully completed one-minute candle, not the still-forming minute.
    completed_cutoff_ms = (as_of_ms // ONE_MINUTE_MILLISECONDS) * ONE_MINUTE_MILLISECONDS - 1
    valid = [(ts, px) for ts, px in all_valid if ts <= completed_cutoff_ms]
    if not valid:
        return {"valid": False, "reason": "no_completed_current_close",
                "valid_close_count": 0}
    if len(valid) < 2:
        return {"valid": False, "reason": "insufficient_valid_traded_closes",
                "valid_close_count": len(valid)}
    current_ts, current_price = valid[-1]
    current_age_ms = completed_cutoff_ms - current_ts
    if current_age_ms < 0 or current_age_ms > max_price_age_seconds * 1000:
        return {"valid": False, "reason": "stale_current_price",
                "valid_close_count": len(valid), "current_price_ts_ms": current_ts,
                "current_price_age_seconds": round(current_age_ms / 1000, 3)}

    reference_cutoff_ms = current_ts - LOOKBACK_MILLISECONDS
    reference_candidates = [(ts, px) for ts, px in valid if ts <= reference_cutoff_ms]
    if not reference_candidates:
        return {"valid": False, "reason": "missing_24h_reference_close",
                "valid_close_count": len(valid), "current_price_ts_ms": current_ts,
                "current_price_age_seconds": round(current_age_ms / 1000, 3)}
    reference_ts, reference_price = reference_candidates[-1]
    reference_gap_ms = reference_cutoff_ms - reference_ts
    if reference_gap_ms < 0 or reference_gap_ms > reference_tolerance_seconds * 1000:
        return {"valid": False, "reason": "stale_24h_reference_price",
                "valid_close_count": len(valid), "current_price_ts_ms": current_ts,
                "current_price_age_seconds": round(current_age_ms / 1000, 3),
                "reference_price_ts_ms": reference_ts,
                "reference_gap_seconds": round(reference_gap_ms / 1000, 3)}

    elapsed_ms = current_ts - reference_ts
    if elapsed_ms < LOOKBACK_MILLISECONDS:
        return {"valid": False, "reason": "reference_window_not_at_least_24h",
                "valid_close_count": len(valid)}

    pct = (current_price / reference_price - Decimal("1")) * Decimal("100")
    return {
        "valid": True, "reason": None,
        "return_24h_pct": format(pct, ".8f"),
        "current_price": format(current_price, "f"),
        "current_price_ts_ms": current_ts,
        "current_price_age_seconds": round(current_age_ms / 1000, 3),
        "reference_price": format(reference_price, "f"),
        "reference_price_ts_ms": reference_ts,
        "reference_gap_seconds": round(reference_gap_ms / 1000, 3),
        "observation_interval_seconds": round(elapsed_ms / 1000, 3),
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
    return {"provisional_long_leader": long_leader,
            "provisional_short_leader": short_leader,
            "long_valid_count": len(valid_long),
            "short_valid_count": len(valid_short)}
