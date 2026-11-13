"""Universe classification and Binance-reported 24-hour ticker ranking rules."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

LONG_UNDERLYING_TYPES = frozenset({
    "EQUITY", "CN_EQUITY", "KR_EQUITY", "HK_EQUITY", "PREMARKET", "INDEX",
})
MEME_UNDERLYING_SUBTYPE = "Meme"
LONG_CONTRACT_TYPE = "TRADIFI_PERPETUAL"
MEME_CONTRACT_TYPE = "PERPETUAL"
QUOTE_ASSET = "USDT"
MAX_TICKER_WINDOW_AGE_SECONDS = 900


def _underlying_subtypes(item: dict[str, Any]) -> tuple[str, ...]:
    """Return only string tags from Binance's underlyingSubType array."""
    raw = item.get("underlyingSubType")
    if not isinstance(raw, list):
        return ()
    return tuple(value.strip() for value in raw if isinstance(value, str) and value.strip())


def is_meme_perpetual(item: dict[str, Any]) -> bool:
    """Use Binance's explicit subtype tag; never infer meme status from a symbol."""
    return (
        item.get("contractType") == MEME_CONTRACT_TYPE
        and item.get("underlyingType") == "COIN"
        and any(tag.casefold() == MEME_UNDERLYING_SUBTYPE.casefold()
                for tag in _underlying_subtypes(item))
    )


def classify_binance_universe(symbols: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Classify active USDT stock/index and explicitly tagged meme perps."""
    seen: set[str] = set()
    long_contracts: list[dict[str, Any]] = []
    meme_contracts: list[dict[str, Any]] = []
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
        elif is_meme_perpetual(item):
            meme_contracts.append(item)
            continue
        elif contract_type == MEME_CONTRACT_TYPE and underlying_type == "COIN":
            reason = "not_meme_underlying_subtype"
        else:
            reason = "contract_or_underlying_type_not_in_approved_universe"
        excluded.append({
            "symbol": symbol,
            "contractType": contract_type,
            "underlyingType": underlying_type,
            "underlyingSubType": _underlying_subtypes(item),
            "reason": reason,
        })

    return {"long": long_contracts, "meme": meme_contracts, "excluded": excluded}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() else None


def _positive_decimal(value: Any) -> Decimal | None:
    number = _decimal(value)
    return number if number is not None and number > 0 else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if isinstance(value, float) and value != number:
        return None
    if isinstance(value, str) and str(number) != value.strip():
        return None
    return number


def parse_binance_24h_tickers(
    payload: Any,
    as_of_ms: int | None = None,
    max_window_age_seconds: int = MAX_TICKER_WINDOW_AGE_SECONDS,
) -> dict[str, dict[str, Any]]:
    """Validate and index Binance's 24-hour ticker report without recomputing returns.

    The returned return_24h_pct is Binance's priceChangePercent field verbatim
    after numeric validation. It is not calculated from openPrice and lastPrice.
    """
    if not isinstance(payload, list):
        raise ValueError("GET /fapi/v1/ticker/24hr without a symbol must return an array")

    records: dict[str, dict[str, Any]] = {}
    for ticker in payload:
        if not isinstance(ticker, dict):
            raise ValueError("Every 24-hour ticker row must be a JSON object")
        symbol = ticker.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("24-hour ticker row is missing a valid symbol")
        if symbol in records:
            raise ValueError(f"Duplicate symbol in Binance 24-hour ticker report: {symbol}")

        percent = _decimal(ticker.get("priceChangePercent"))
        last_price = _positive_decimal(ticker.get("lastPrice"))
        open_price = _positive_decimal(ticker.get("openPrice"))
        trade_count = _integer(ticker.get("count"))
        open_time = _integer(ticker.get("openTime"))
        close_time = _integer(ticker.get("closeTime"))
        reasons: list[str] = []

        if percent is None:
            reasons.append("invalid_price_change_percent")
        if last_price is None:
            reasons.append("invalid_last_price")
        if open_price is None:
            reasons.append("invalid_open_price")
        if trade_count is None or trade_count <= 0:
            reasons.append("no_valid_trades_in_24h_window")
        if open_time is None or close_time is None or open_time <= 0 or close_time <= open_time:
            reasons.append("invalid_ticker_window_timestamps")

        window_age_seconds: float | None = None
        if close_time is not None and as_of_ms is not None:
            window_age_ms = as_of_ms - close_time
            window_age_seconds = round(window_age_ms / 1000, 3)
            if abs(window_age_ms) > max_window_age_seconds * 1000:
                reasons.append("stale_ticker_window")

        quote_volume = _decimal(ticker.get("quoteVolume"))
        volume = _decimal(ticker.get("volume"))
        price_change = _decimal(ticker.get("priceChange"))
        record = {
            "symbol": symbol,
            "valid": not reasons,
            "reason": ";".join(reasons) if reasons else None,
            "return_24h_pct": format(percent, "f") if percent is not None else None,
            "price_change_percent": format(percent, "f") if percent is not None else None,
            "last_price": format(last_price, "f") if last_price is not None else None,
            "open_price": format(open_price, "f") if open_price is not None else None,
            "price_change": format(price_change, "f") if price_change is not None else None,
            "quote_volume": format(quote_volume, "f") if quote_volume is not None else None,
            "volume": format(volume, "f") if volume is not None else None,
            "trade_count": trade_count,
            "ticker_window_open_time_ms": open_time,
            "ticker_window_close_time_ms": close_time,
            "ticker_window_age_seconds": window_age_seconds,
            "last_trade_timestamp_available": False,
        }
        records[symbol] = record

    return records


def build_provisional_leaders(
    long_records: list[dict[str, Any]], meme_records: list[dict[str, Any]]
) -> dict[str, Any]:
    """Return diagnostic leaders; this function never authorizes a position."""
    valid_long = [r for r in long_records if r.get("valid")]
    valid_meme = [r for r in meme_records if r.get("valid")]
    long_leader = max(valid_long, key=lambda r: Decimal(r["return_24h_pct"])) if valid_long else None
    meme_leader = min(valid_meme, key=lambda r: Decimal(r["return_24h_pct"])) if valid_meme else None
    return {
        "provisional_long_leader": long_leader,
        "provisional_meme_short_leader": meme_leader,
        "long_valid_count": len(valid_long),
        "meme_valid_count": len(valid_meme),
    }
