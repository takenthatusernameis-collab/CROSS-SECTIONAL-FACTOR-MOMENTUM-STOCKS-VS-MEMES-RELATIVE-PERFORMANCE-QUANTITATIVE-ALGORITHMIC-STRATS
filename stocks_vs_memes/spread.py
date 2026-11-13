"""Pure Bybit/CMC universe mapping and completed daily ratio-candle metrics."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .core import CategoryUnavailableError, Selection, StrategyError


@dataclass(frozen=True)
class RatioCandle:
    open: float
    high: float
    low: float
    close: float
    samples: int
    high_basis: str = "maximum synchronized 1-minute bar-range bound: long high / short low"
    low_basis: str = "minimum synchronized 1-minute bar-range bound: long low / short high"

    @property
    def roi(self) -> float:
        return self.close / self.open - 1.0

    @property
    def drawdown(self) -> float:
        return min(0.0, self.low / self.open - 1.0)


def _finite(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise StrategyError(f"Invalid {label}: {value!r}") from exc
    if not (result == result and abs(result) != float("inf")):
        raise StrategyError(f"Non-finite {label}: {value!r}")
    return result


def eligible_linear_usdt(instrument: dict[str, Any]) -> bool:
    return (
        str(instrument.get("contractType", "")).casefold() == "linearperpetual"
        and str(instrument.get("status", "")).casefold() == "trading"
        and str(instrument.get("quoteCoin", "")).upper() == "USDT"
        and str(instrument.get("settleCoin", "")).upper() == "USDT"
        and bool(instrument.get("symbol"))
        and bool(instrument.get("baseCoin"))
    )


def build_universes(
    instruments: list[dict[str, Any]],
    tickers: list[dict[str, Any]],
    meme_members: list[dict[str, Any]],
    coin_map: list[dict[str, Any]],
) -> tuple[list[Selection], list[Selection], dict[str, Any]]:
    ticker_map = {str(row.get("symbol", "")).upper(): row for row in tickers if row.get("symbol")}
    member_ids = {str(coin.get("id")) for coin in meme_members if coin.get("id") is not None}
    id_map_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for coin in coin_map:
        symbol = str(coin.get("symbol") or "").upper()
        if symbol:
            id_map_by_symbol.setdefault(symbol, []).append(coin)
    unique_meme_symbols = {
        symbol for symbol, rows in id_map_by_symbol.items()
        if len(rows) == 1 and str(rows[0].get("id")) in member_ids
    }
    ambiguous_symbols = sorted(symbol for symbol, rows in id_map_by_symbol.items() if len(rows) > 1)
    meme_category_symbols = {str(coin.get("symbol") or "").upper() for coin in meme_members}

    stocks: list[Selection] = []
    memes: list[Selection] = []
    skipped = {"not_linear_usdt_perpetual": 0, "missing_ticker": 0, "bad_ticker": 0,
               "not_stock_or_cmc_meme": 0, "ambiguous_cmc_symbol": 0}
    observed_symbol_types: dict[str, int] = {}
    matched_members: list[str] = []

    for instrument in instruments:
        raw_symbol_type = str(instrument.get("symbolType") or "<empty>").casefold()
        observed_symbol_types[raw_symbol_type] = observed_symbol_types.get(raw_symbol_type, 0) + 1
        if not eligible_linear_usdt(instrument):
            skipped["not_linear_usdt_perpetual"] += 1
            continue
        symbol = str(instrument["symbol"]).upper()
        ticker = ticker_map.get(symbol)
        if ticker is None:
            skipped["missing_ticker"] += 1
            continue
        try:
            change_pct = _finite(ticker.get("price24hPcnt"), f"{symbol} price24hPcnt") * 100.0
            turnover = _finite(ticker.get("turnover24h", 0), f"{symbol} turnover24h")
            if turnover < 0:
                raise ValueError("negative turnover")
        except (StrategyError, ValueError):
            skipped["bad_ticker"] += 1
            continue

        symbol_type = raw_symbol_type
        if symbol_type == "stock":
            stocks.append(Selection(symbol, "stocks", change_pct, turnover, symbol_type))
        else:
            base = str(instrument.get("baseCoin") or "").upper()
            if base in ambiguous_symbols:
                skipped["ambiguous_cmc_symbol"] += 1
                continue
            if base in unique_meme_symbols:
                memes.append(Selection(symbol, "memes", change_pct, turnover, "cmc:Memes"))
                matched_members.append(base)
            else:
                skipped["not_stock_or_cmc_meme"] += 1

    diagnostics = {
        "observed_bybit_symbol_types": dict(sorted(observed_symbol_types.items())),
        "eligible_counts": {"stocks": len(stocks), "memes": len(memes)},
        "cmc_meme_member_count": len(meme_members),
        "cmc_map_symbol_count": len(id_map_by_symbol),
        "cmc_unique_meme_symbol_count": len(unique_meme_symbols),
        "cmc_meme_members_missing_from_active_map": sorted(meme_category_symbols - set(id_map_by_symbol))[:100],
        "cmc_ambiguous_symbols_excluded": ambiguous_symbols,
        "matched_meme_base_coins": sorted(set(matched_members)),
        "skipped": skipped,
    }
    if not stocks or not memes:
        missing = [name for name, values in (("stocks", stocks), ("memes", memes)) if not values]
        raise CategoryUnavailableError(
            "Cannot establish Bybit/CMC perpetual universe; missing " + ", ".join(missing)
            + f". Bybit symbolType counts={diagnostics['observed_bybit_symbol_types']}; "
              f"CMC unique meme symbols={diagnostics['cmc_unique_meme_symbol_count']}"
        )
    return stocks, memes, diagnostics


def select_pair(stocks: list[Selection], memes: list[Selection]) -> tuple[Selection, Selection]:
    stock = sorted(stocks, key=lambda x: (-x.change_pct, -x.volume_quote, x.contract))[0]
    meme = sorted(memes, key=lambda x: (x.change_pct, -x.volume_quote, x.contract))[0]
    if stock.contract == meme.contract:
        raise CategoryUnavailableError("The same Bybit contract resolved to both legs")
    return stock, meme


def build_ratio_candle(
    long_candles: list[dict[str, Any]],
    short_candles: list[dict[str, Any]],
    expected_minutes: int = 1440,
    minimum_coverage: float = 1.0,
    expected_start_ts: int | None = None,
    expected_end_ts: int | None = None,
) -> RatioCandle:
    long_map = {int(x["t"]): x for x in long_candles}
    short_map = {int(x["t"]): x for x in short_candles}
    timestamps = sorted(set(long_map).intersection(short_map))
    if len(timestamps) < max(2, int(expected_minutes * minimum_coverage)):
        raise StrategyError(f"Insufficient synchronized ratio marks: {len(timestamps)}/{expected_minutes} minutes")
    if expected_start_ts is not None and timestamps[0] != int(expected_start_ts):
        raise StrategyError(f"Completed ratio candle is missing its first UTC minute: expected {expected_start_ts}, received {timestamps[0]}")
    if expected_end_ts is not None and timestamps[-1] != int(expected_end_ts) - 60:
        raise StrategyError(f"Completed ratio candle is missing its final UTC minute: expected {int(expected_end_ts)-60}, received {timestamps[-1]}")

    first = timestamps[0]
    first_long_open = _finite(long_map[first].get("o"), "long first open")
    first_short_open = _finite(short_map[first].get("o"), "short first open")
    if first_long_open <= 0 or first_short_open <= 0:
        raise StrategyError("Ratio candle opening marks must be positive")
    ratio_open = first_long_open / first_short_open

    ratio_highs: list[float] = [ratio_open]
    ratio_lows: list[float] = [ratio_open]
    for ts in timestamps:
        long_bar, short_bar = long_map[ts], short_map[ts]
        long_open = _finite(long_bar.get("o"), "long mark open")
        long_high = _finite(long_bar.get("h"), "long mark high")
        long_low = _finite(long_bar.get("l"), "long mark low")
        long_close = _finite(long_bar.get("c"), "long mark close")
        short_open = _finite(short_bar.get("o"), "short mark open")
        short_high = _finite(short_bar.get("h"), "short mark high")
        short_low = _finite(short_bar.get("l"), "short mark low")
        short_close = _finite(short_bar.get("c"), "short mark close")
        if min(long_open, long_high, long_low, long_close, short_open, short_high, short_low, short_close) <= 0:
            raise StrategyError("Ratio candle has non-positive mark price")
        if (long_high < max(long_open, long_close) or long_low > min(long_open, long_close)
                or short_high < max(short_open, short_close) or short_low > min(short_open, short_close)):
            raise StrategyError("Invalid mark-price OHLC range")
        ratio_highs.append(long_high / short_low)
        ratio_lows.append(long_low / short_high)

    last = timestamps[-1]
    ratio_close = _finite(long_map[last].get("c"), "long final close") / _finite(short_map[last].get("c"), "short final close")
    if ratio_close <= 0:
        raise StrategyError("Ratio candle close must be positive")
    ratio_highs.append(ratio_close)
    ratio_lows.append(ratio_close)
    return RatioCandle(open=ratio_open, high=max(ratio_highs), low=min(ratio_lows),
                       close=ratio_close, samples=len(timestamps))


def cumulative_ratio_metrics(ratio: RatioCandle, previous_reports: list[dict[str, Any]]) -> dict[str, float]:
    history = sorted((row for row in previous_reports if row.get("utc_date") is not None),
                     key=lambda row: str(row["utc_date"]))
    if history:
        latest = history[-1]
        prior_close = float(latest.get("cumulative_ratio_index_close", 1.0))
        prior_peak = float(latest.get("cumulative_ratio_index_high_water", max(
            float(row.get("cumulative_ratio_index_high", 1.0)) for row in history
        )))
        prior_mdd = float(latest.get("maximum_ratio_drawdown_pct", min(
            float(row.get("maximum_ratio_drawdown_pct", 0.0)) for row in history
        ))) / 100.0
    else:
        prior_close, prior_peak, prior_mdd = 1.0, 1.0, 0.0
    if ratio.open <= 0:
        raise StrategyError("Ratio candle open must be positive")
    index_open = prior_close
    index_high = prior_close * ratio.high / ratio.open
    index_low = prior_close * ratio.low / ratio.open
    index_close = prior_close * ratio.close / ratio.open
    high_water = max(prior_peak, index_high)
    current_bar_drawdown = index_low / high_water - 1.0
    maximum_drawdown = min(0.0, prior_mdd, current_bar_drawdown)
    return {
        "cumulative_ratio_index_open": index_open,
        "cumulative_ratio_index_high": index_high,
        "cumulative_ratio_index_low": index_low,
        "cumulative_ratio_index_close": index_close,
        "cumulative_ratio_index_high_water": high_water,
        "cumulative_ratio_roi_pct": (index_close - 1.0) * 100.0,
        "maximum_ratio_drawdown_pct": maximum_drawdown * 100.0,
    }
