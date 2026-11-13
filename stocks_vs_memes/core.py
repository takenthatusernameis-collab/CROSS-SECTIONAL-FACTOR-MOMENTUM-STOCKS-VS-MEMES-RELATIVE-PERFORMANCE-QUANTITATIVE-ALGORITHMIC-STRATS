"""Pure strategy, execution, and accounting functions (no network or database)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from typing import Any, Iterable


class StrategyError(RuntimeError):
    """Base error for invalid strategy inputs."""


class CategoryUnavailableError(StrategyError):
    """Raised when the exchange feed cannot establish both requested categories."""


class UnexecutableMarketError(StrategyError):
    """Raised when the selected contract cannot be filled using visible depth."""


@dataclass(frozen=True)
class Selection:
    contract: str
    category: str
    change_pct: float
    volume_quote: float
    contract_type: str


@dataclass(frozen=True)
class BookFill:
    quantity: float
    vwap: float
    mid_price: float
    best_price: float
    notional: float


@dataclass(frozen=True)
class Position:
    contract: str
    category: str
    direction: int
    quantity: float
    multiplier: float
    entry_price: float
    entry_mid: float
    entry_fee: float
    taker_fee_rate: float
    selection_change_pct: float
    selection_volume_quote: float

    @property
    def entry_notional(self) -> float:
        return self.quantity * self.multiplier * self.entry_price


def utc_day_bounds(now: datetime) -> tuple[datetime, datetime]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = now.astimezone(timezone.utc)
    start = datetime(current.year, current.month, current.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _as_float(value: Any, field: str, contract: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyError(f"Invalid {field} for {contract}: {value!r}") from exc
    if result != result or result in (float("inf"), float("-inf")):
        raise StrategyError(f"Non-finite {field} for {contract}: {value!r}")
    return result


def round_contract_quantity(quantity: float, enable_decimal: bool, minimum: float) -> float:
    if quantity <= 0 or minimum <= 0:
        raise UnexecutableMarketError("Position quantity and minimum quantity must be positive")
    quant = Decimal("0.000001") if enable_decimal else Decimal("1")
    rounded = float(Decimal(str(quantity)).quantize(quant, rounding=ROUND_DOWN))
    if rounded < minimum:
        raise UnexecutableMarketError(f"Rounded size {rounded:g} is below minimum order size {minimum:g}")
    return rounded


def size_leg(nav: float, gross_leverage: float, mark_price: float, multiplier: float,
             enable_decimal: bool, minimum_quantity: float) -> tuple[float, float]:
    if nav <= 0 or gross_leverage <= 0 or mark_price <= 0 or multiplier <= 0:
        raise UnexecutableMarketError("NAV, leverage, mark price and contract multiplier must be positive")
    leg_notional = nav * gross_leverage / 2.0
    raw_quantity = leg_notional / (mark_price * multiplier)
    quantity = round_contract_quantity(raw_quantity, enable_decimal, minimum_quantity)
    return leg_notional, quantity


def book_mid_price(order_book: dict[str, Any]) -> float:
    bids = order_book.get("bids") or []
    asks = order_book.get("asks") or []
    if not bids or not asks:
        raise UnexecutableMarketError("Order book has no two-sided top of book")
    bid = _as_float(bids[0].get("p", bids[0].get("price")), "best bid", "order book")
    ask = _as_float(asks[0].get("p", asks[0].get("price")), "best ask", "order book")
    if bid <= 0 or ask <= 0 or ask < bid:
        raise UnexecutableMarketError("Invalid or crossed order book")
    return (bid + ask) / 2.0


def walk_order_book(order_book: dict[str, Any], quantity: float, side: str) -> BookFill:
    if side not in {"buy", "sell"}:
        raise ValueError("side must be 'buy' or 'sell'")
    if quantity <= 0:
        raise UnexecutableMarketError("Requested book quantity must be positive")
    levels = order_book.get("asks" if side == "buy" else "bids") or []
    if not levels:
        raise UnexecutableMarketError(f"Order book has no levels for {side}")
    remaining = quantity
    total_value = 0.0
    filled = 0.0
    for level in levels:
        price = _as_float(level.get("p", level.get("price")), "book price", "order book")
        available = _as_float(level.get("s", level.get("size")), "book size", "order book")
        if price <= 0 or available <= 0:
            continue
        take = min(remaining, available)
        total_value += take * price
        filled += take
        remaining -= take
        if remaining <= max(1e-12, quantity * 1e-10):
            break
    if remaining > max(1e-12, quantity * 1e-10):
        raise UnexecutableMarketError(f"Insufficient visible {side}-side depth: requested {quantity:g}, filled {filled:g}")
    best = _as_float(levels[0].get("p", levels[0].get("price")), "best price", "order book")
    mid = book_mid_price(order_book)
    vwap = total_value / filled
    return BookFill(filled, vwap, mid, best, total_value)


def adverse_slippage_usdt(position: Position, fill: BookFill, is_entry: bool) -> float:
    if is_entry:
        signed_difference = position.direction * (fill.vwap - fill.mid_price)
    else:
        signed_difference = position.direction * (fill.mid_price - fill.vwap)
    return max(0.0, signed_difference * position.quantity * position.multiplier)


def funding_cashflow(direction: int, quantity: float, multiplier: float,
                     mark_price: float, funding_rate: float) -> float:
    if direction not in (-1, 1):
        raise ValueError("direction must be +1 (long) or -1 (short)")
    return -direction * quantity * multiplier * mark_price * funding_rate


def relative_ratio_drift(long_mark: float, long_entry_mid: float,
                         short_mark: float, short_entry_mid: float) -> float:
    if min(long_mark, long_entry_mid, short_mark, short_entry_mid) <= 0:
        raise ValueError("All prices must be positive")
    return (long_mark / long_entry_mid) / (short_mark / short_entry_mid) - 1.0


def position_price_pnl(position: Position, mark_price: float) -> float:
    return position.direction * position.quantity * position.multiplier * (mark_price - position.entry_price)


def maximum_drawdown(equity_points: Iterable[tuple[int, float]]) -> tuple[float, float]:
    peak: float | None = None
    worst_pct = 0.0
    worst_usdt = 0.0
    for _, equity in sorted(equity_points, key=lambda x: x[0]):
        if peak is None or equity > peak:
            peak = equity
        if peak and peak > 0:
            drawdown = equity / peak - 1.0
            if drawdown < worst_pct:
                worst_pct = drawdown
                worst_usdt = equity - peak
    return worst_pct, worst_usdt


def event_mark_price(candles: list[dict[str, Any]], event_ts: int) -> float:
    prior = [c for c in candles if int(c["t"]) <= event_ts]
    if not prior:
        raise StrategyError(f"No mark candle at or before funding timestamp {event_ts}")
    candle = max(prior, key=lambda c: int(c["t"]))
    if int(candle["t"]) == event_ts:
        price = _as_float(candle.get("o"), "funding-event mark open", "mark candle")
    else:
        if event_ts - int(candle["t"]) > 60:
            raise StrategyError(f"Mark-price data is too far from funding timestamp {event_ts}")
        price = _as_float(candle.get("c"), "funding-event mark close", "mark candle")
    if price <= 0:
        raise StrategyError("Funding-event mark price must be positive")
    return price
