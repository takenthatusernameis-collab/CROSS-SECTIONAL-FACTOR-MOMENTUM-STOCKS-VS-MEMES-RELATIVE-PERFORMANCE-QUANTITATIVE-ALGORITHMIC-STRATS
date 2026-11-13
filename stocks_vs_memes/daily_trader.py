"""Boundary-triggered Bybit + CoinMarketCap daily paper workflow.

No intraday signal polling or position changes: all new selections and simulated
position transitions occur at UTC midnight. Performance for the completed UTC
ratio candle is finalized at that same boundary.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from .bybit import BybitClient
from .coinmarketcap import CoinMarketCapClient
from .core import (
    BookFill, CategoryUnavailableError, Position, Selection, StrategyError,
    adverse_slippage_usdt, book_mid_price, event_mark_price, funding_cashflow,
    position_price_pnl, walk_order_book,
)
from .spread import build_ratio_candle, build_universes, cumulative_ratio_metrics, select_pair
from .store import PaperStore


class DailyBoundaryError(StrategyError):
    pass


class BybitDailyTrader:
    def __init__(self, config: dict[str, Any], bybit_client: BybitClient | None = None,
                 cmc_client: CoinMarketCapClient | None = None, store: PaperStore | None = None):
        self.config = config
        self.bybit = bybit_client or BybitClient(
            config.get("bybit_base_url", "https://api.bybit.com"),
            int(config.get("request_timeout_seconds", 20)), int(config.get("max_retries", 2)))
        self.cmc = cmc_client or CoinMarketCapClient(
            config.get("cmc_base_url", "https://pro-api.coinmarketcap.com/public-api"),
            int(config.get("request_timeout_seconds", 20)), int(config.get("max_retries", 2)))
        data_dir = Path(config.get("data_dir", "data"))
        self.store = store or PaperStore(data_dir / "stocks_vs_memes_bybit.sqlite3")
        self.reports_dir = Path(config.get("reports_dir", "reports"))
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        self.category_name = str(config.get("cmc_meme_category_name", "Memes"))
        self.gross_leverage = float(config.get("gross_leverage", 2.0))
        self.fee_rate = float(config.get("taker_fee_rate", 0.00055))
        self.orderbook_levels = int(config.get("orderbook_levels", 100))
        self.entry_grace = int(config.get("entry_grace_seconds", 45))
        self.min_coverage = float(config.get("minimum_mark_coverage", 1.0))
        self.last_snapshot: dict[str, Any] | None = None

    def live_preflight(self) -> dict[str, Any]:
        """Independently verify both public feeds; never authorize positions on partial data."""
        checked = datetime.now(timezone.utc)
        result: dict[str, Any] = {
            "checked_at_utc": checked.isoformat(),
            "exchange": "Bybit",
            "classification_provider": "CoinMarketCap keyless public Categories API",
            "bybit_endpoints": [
                f"{self.bybit.base_url}/v5/market/instruments-info?category=linear",
                f"{self.bybit.base_url}/v5/market/tickers?category=linear",
            ],
            "cmc_endpoints": [
                f"{self.cmc.base_url}/v1/cryptocurrency/categories",
                f"{self.cmc.base_url}/v1/cryptocurrency/category?id=<discovered-category-id>",
                f"{self.cmc.base_url}/v1/cryptocurrency/map?symbol=<Bybit-baseCoin-list>",
            ],
            "requested_cmc_category_exact_name": self.category_name,
            "passed": False,
            "paper_trading_authorized": False,
            "feeds_ready": False,
            "strategy_ranking_performed": False,
            "source_checks": {
                "bybit_market_feed": {"status": "NOT_CHECKED"},
                "coinmarketcap_taxonomy_feed": {"status": "NOT_CHECKED"},
            },
        }
        instruments: list[dict[str, Any]] | None = None
        tickers: list[dict[str, Any]] | None = None
        category: dict[str, Any] | None = None
        members: list[dict[str, Any]] | None = None
        category_meta: dict[str, Any] = {}
        coin_map: list[dict[str, Any]] | None = None
        errors: list[str] = []

        try:
            instruments = self.bybit.instruments()
            tickers = self.bybit.tickers()
            result["source_checks"]["bybit_market_feed"] = {
                "status": "PASS", "instruments_count": len(instruments), "tickers_count": len(tickers),
            }
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            errors.append("Bybit market feed: " + message)
            result["source_checks"]["bybit_market_feed"] = {"status": "FAIL", "error": message}

        try:
            category = self.cmc.find_exact_category(self.category_name)
            category_id = str(category.get("id") or "")
            if not category_id:
                raise CategoryUnavailableError("CoinMarketCap exact Memes category has no ID")
            members, category_meta = self.cmc.category_members(category_id)
            result["source_checks"]["coinmarketcap_taxonomy_feed"] = {
                "status": "PASS", "category_id": category_id,
                "category_name": category.get("name"), "category_title": category.get("title"),
                "member_count": len(members), "category_last_updated": category_meta.get("last_updated"),
            }
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            errors.append("CoinMarketCap taxonomy feed: " + message)
            result["source_checks"]["coinmarketcap_taxonomy_feed"] = {"status": "FAIL", "error": message}

        diagnostics: dict[str, Any] | None = None
        stocks: list[Selection] = []
        memes: list[Selection] = []
        if instruments is not None and tickers is not None and members is not None and category is not None:
            try:
                coin_map = self.cmc.cryptocurrency_map_for_symbols(
                    [str(x.get("baseCoin") or "") for x in instruments]
                )
                stocks, memes, diagnostics = build_universes(instruments, tickers, members, coin_map)
                diagnostics["cmc_category"] = {
                    "id": str(category.get("id")), "name": category.get("name"),
                    "title": category.get("title"), "member_count": len(members),
                    "category_last_updated": category_meta.get("last_updated"),
                }
                diagnostics["source_counts"] = {
                    "bybit_instruments": len(instruments), "bybit_tickers": len(tickers),
                    "cmc_category_members": len(members),
                    "cmc_id_map_rows_for_exchange_symbols": len(coin_map),
                }
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                errors.append("Bybit/CMC category-to-contract mapping: " + message)
                result["source_checks"]["category_to_contract_mapping"] = {"status": "FAIL", "error": message}

        if errors or diagnostics is None:
            if diagnostics is not None:
                result.update(diagnostics)
            elif instruments is not None:
                result["observed_bybit_symbol_types"] = self._observed_symbol_type_counts(instruments)
            result.update({
                "status": "FAIL", "passed": False, "paper_trading_authorized": False,
                "error": "; ".join(errors) if errors else "Both live category universes could not be established",
                "message": "Live preflight failed. No new paper exposure is authorized; no symbols or categories were inferred.",
            })
            self.last_snapshot = None
        else:
            result.update(diagnostics)
            result.update({
                "status": "PASS", "passed": True, "feeds_ready": True,
                "paper_trading_authorized": False,
                "stock_contract_count": len(stocks), "meme_contract_count": len(memes),
                "source_checks": {
                    "bybit_market_feed": {"status": "PASS", "instruments_count": len(instruments), "tickers_count": len(tickers)},
                    "coinmarketcap_taxonomy_feed": {
                        "status": "PASS", "category_id": str(category.get("id")),
                        "category_name": category.get("name"), "category_title": category.get("title"),
                        "member_count": len(members), "category_last_updated": category_meta.get("last_updated"),
                    },
                    "category_to_contract_mapping": {"status": "PASS", "stock_contracts": len(stocks), "meme_contracts": len(memes)},
                },
                "strategy_ranking_performed": False,
                "message": "Live Bybit stock perpetuals and CMC Memes members both map to active Bybit USDT linear perpetual tickers. This diagnostic did not rank or open positions.",
            })
            self.last_snapshot = {"instruments": instruments, "tickers": tickers,
                                  "stocks": stocks, "memes": memes, "diagnostics": diagnostics,
                                  "category": category}

        data_dir = Path(self.config.get("data_dir", "data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "live_preflight_latest.json").write_text(
            json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
        )
        return result

    @staticmethod
    def _observed_symbol_type_counts(instruments: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for instrument in instruments:
            value = str(instrument.get("symbolType") or "<empty>").casefold()
            counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items()))

    @staticmethod
    def _floor_to_step(quantity: float, step: float) -> float:
        if quantity <= 0 or step <= 0:
            raise StrategyError("Position quantity and Bybit qtyStep must be positive")
        q = Decimal(str(quantity))
        s = Decimal(str(step))
        return float((q / s).to_integral_value(rounding=ROUND_DOWN) * s)

    def _instrument_map(self, instruments: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {str(item.get("symbol")): item for item in instruments if item.get("symbol")}

    def _open_position(self, selection: Selection, instrument: dict[str, Any], nav: float, direction: int) -> tuple[Position, BookFill, float]:
        lot = instrument.get("lotSizeFilter") or {}
        minimum_qty = float(lot.get("minOrderQty") or 0)
        qty_step = float(lot.get("qtyStep") or 0)
        min_notional = float(lot.get("minNotionalValue") or 0)
        max_market_qty = float(lot.get("maxMktOrderQty") or 0)
        book = self.bybit.order_book(selection.contract, self.orderbook_levels)
        mid = book_mid_price(book)
        leg_notional = nav * self.gross_leverage / 2.0
        qty = self._floor_to_step(leg_notional / mid, qty_step)
        if qty < minimum_qty or qty * mid < min_notional:
            raise StrategyError(f"{selection.contract} order is below Bybit minimum quantity/notional")
        if max_market_qty and qty > max_market_qty:
            raise StrategyError(f"{selection.contract} quantity {qty} exceeds Bybit max market quantity {max_market_qty}")
        side = "buy" if direction > 0 else "sell"
        fill = walk_order_book(book, qty, side)
        if max_market_qty and fill.quantity > max_market_qty:
            raise StrategyError(f"{selection.contract} visible fill exceeds Bybit max market quantity")
        fee = fill.notional * self.fee_rate
        pos = Position(
            contract=selection.contract, category=selection.category, direction=direction,
            quantity=fill.quantity, multiplier=1.0, entry_price=fill.vwap,
            entry_mid=fill.mid_price, entry_fee=fee, taker_fee_rate=self.fee_rate,
            selection_change_pct=selection.change_pct, selection_volume_quote=selection.volume_quote,
        )
        slip = adverse_slippage_usdt(pos, fill, True)
        return pos, fill, slip

    def _close_previous(self, day: dict[str, Any], boundary: datetime) -> dict[str, Any]:
        payload = day["payload"]
        long_pos = Position(**payload["positions"]["long"])
        short_pos = Position(**payload["positions"]["short"])
        start_ts, end_ts = int(day["entry_ts"]), int(day["exit_ts"])

        long_marks_raw = self.bybit.mark_candles(long_pos.contract, start_ts, end_ts)
        short_marks_raw = self.bybit.mark_candles(short_pos.contract, start_ts, end_ts)
        long_marks = [x for x in long_marks_raw if start_ts <= int(x["t"]) < end_ts]
        short_marks = [x for x in short_marks_raw if start_ts <= int(x["t"]) < end_ts]
        ratio = build_ratio_candle(
            long_marks, short_marks, 1440, self.min_coverage,
            expected_start_ts=start_ts, expected_end_ts=end_ts,
        )

        long_book = self.bybit.order_book(long_pos.contract, self.orderbook_levels)
        short_book = self.bybit.order_book(short_pos.contract, self.orderbook_levels)
        long_exit = walk_order_book(long_book, long_pos.quantity, "sell")
        short_exit = walk_order_book(short_book, short_pos.quantity, "buy")
        long_exit_fee = long_exit.notional * self.fee_rate
        short_exit_fee = short_exit.notional * self.fee_rate

        long_rates = self.bybit.funding_rates(long_pos.contract, start_ts, end_ts)
        short_rates = self.bybit.funding_rates(short_pos.contract, start_ts, end_ts)
        funding_rows: list[dict[str, Any]] = []
        for pos, rates, candles in ((long_pos, long_rates, long_marks), (short_pos, short_rates, short_marks)):
            for rate_row in rates:
                ts = int(rate_row["t"])
                if not (start_ts < ts <= end_ts):
                    continue
                mark = event_mark_price(candles, ts)
                cash = funding_cashflow(pos.direction, pos.quantity, pos.multiplier, mark, float(rate_row["r"]))
                funding_rows.append({"symbol": pos.contract, "timestamp_utc": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                                     "funding_rate": float(rate_row["r"]), "mark_price": mark, "cashflow_usdt": cash})
                self.store.add_funding(day["utc_date"], pos.contract, ts, float(rate_row["r"]), mark, cash)

        long_pnl = position_price_pnl(long_pos, long_exit.vwap)
        short_pnl = position_price_pnl(short_pos, short_exit.vwap)
        gross_pnl = long_pnl + short_pnl
        funding = sum(x["cashflow_usdt"] for x in funding_rows)
        entry_fees = long_pos.entry_fee + short_pos.entry_fee
        exit_fees = long_exit_fee + short_exit_fee
        net_pnl = gross_pnl + funding - entry_fees - exit_fees
        start_nav = float(payload["starting_equity_usdt"])
        end_nav = start_nav + net_pnl
        if start_nav <= 0 or end_nav <= 0:
            raise StrategyError("Paper equity reached zero or below; refusing to compound")

        previous = self.store.list_days(5000)
        previous_reports = [x["report"] for x in previous
                            if x.get("report") and x["utc_date"] < day["utc_date"]]
        cumulative = cumulative_ratio_metrics(ratio, previous_reports)

        long_slip = adverse_slippage_usdt(long_pos, long_exit, False)
        short_slip = adverse_slippage_usdt(short_pos, short_exit, False)
        report = {
            "utc_date": day["utc_date"], "exchange": "Bybit", "classification_provider": "CoinMarketCap",
            "strategy": "stocks-vs-memes-daily-market-neutral", "paper_only": True,
            "daily_open_utc": datetime.fromtimestamp(start_ts, timezone.utc).isoformat(),
            "daily_close_utc": datetime.fromtimestamp(end_ts, timezone.utc).isoformat(),
            "performance_computed_at_utc": boundary.isoformat(),
            "long_symbol": long_pos.contract, "short_symbol": short_pos.contract,
            "long_category": "stocks", "short_category": "memes",
            "long_selection_change_24h_pct": long_pos.selection_change_pct,
            "short_selection_change_24h_pct": short_pos.selection_change_pct,
            "starting_equity_usdt": start_nav, "ending_equity_usdt": end_nav,
            "gross_leverage": self.gross_leverage,
            "long_notional_entry_usdt": long_pos.entry_notional,
            "short_notional_entry_usdt": short_pos.entry_notional,
            "ratio_candle": {"open": ratio.open, "high": ratio.high, "low": ratio.low, "close": ratio.close,
                             "samples_1m": ratio.samples, "high_basis": ratio.high_basis,
                             "low_basis": ratio.low_basis},
            "ratio_roi_pct": ratio.roi * 100.0,
            "daily_ratio_drawdown_pct": ratio.drawdown * 100.0,
            **cumulative,
            "long_exit_vwap": long_exit.vwap, "short_exit_vwap": short_exit.vwap,
            "long_gross_pnl_usdt": long_pnl, "short_gross_pnl_usdt": short_pnl,
            "gross_pnl_usdt": gross_pnl, "funding_cashflow_usdt": funding,
            "funding_events": funding_rows, "entry_fees_usdt": entry_fees,
            "exit_fees_usdt": exit_fees, "total_fees_usdt": entry_fees + exit_fees,
            "entry_slippage_usdt": float(payload["entry_slippage_usdt"]["long"]) + float(payload["entry_slippage_usdt"]["short"]),
            "exit_slippage_usdt": long_slip + short_slip,
            "net_pnl_usdt": net_pnl,
            "net_portfolio_roi_pct": net_pnl / start_nav * 100.0,
            "ratio_roi_is_not_account_roi": True,
            "mark_coverage_pct": ratio.samples / 1440.0 * 100.0,
            "warnings": ["Ratio candle high/low use conservative synchronized one-minute OHLC bounds (long high / short low; long low / short high), not exact tick-synchronous extrema.",
                         "Cumulative maximum drawdown assumes each daily candle high precedes its low; OHLC bars do not expose exact tick ordering.",
                         "Ratio ROI/drawdown are relative-spread metrics, not leveraged account ROI. Net portfolio ROI separately includes fills, fees and funding."],
        }
        self.store.save_report(day["utc_date"], report)
        self._write_report(report)
        return report

    def _write_report(self, report: dict[str, Any]) -> None:
        day_dir = self.reports_dir / report["utc_date"]
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        ratio = report["ratio_candle"]
        import csv
        with (day_dir / "ratio_candle.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["utc_date", "open", "high", "low", "close", "samples_1m", "high_basis", "low_basis",
                "ratio_roi_pct", "daily_ratio_drawdown_pct", "cumulative_ratio_roi_pct", "maximum_ratio_drawdown_pct"])
            writer.writeheader()
            writer.writerow({"utc_date": report["utc_date"], **ratio,
                "ratio_roi_pct": report["ratio_roi_pct"],
                "daily_ratio_drawdown_pct": report["daily_ratio_drawdown_pct"],
                "cumulative_ratio_roi_pct": report["cumulative_ratio_roi_pct"],
                "maximum_ratio_drawdown_pct": report["maximum_ratio_drawdown_pct"]})
        o, h, l, c = (float(ratio[k]) for k in ("open", "high", "low", "close"))
        span = max(h - l, abs(h) * 1e-9)
        y = lambda value: 40 + (h - value) / span * 220
        body_top, body_bottom = sorted((y(o), y(c)))
        candle_svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="320" viewBox="0 0 640 320">'
            '<rect width="100%" height="100%" fill="white"/>'
            '<text x="320" y="24" text-anchor="middle" font-family="Arial" font-size="16">Completed UTC Ratio Candle</text>'
            f'<line x1="320" y1="{y(h):.2f}" x2="320" y2="{y(l):.2f}" stroke="#374151" stroke-width="2"/>'
            f'<rect x="280" y="{body_top:.2f}" width="80" height="{max(2.0, body_bottom-body_top):.2f}" fill="#2563eb" stroke="#1e3a8a"/>'
            f'<text x="380" y="{y(h):.2f}" font-family="Arial" font-size="12">H {h:.8g}</text>'
            f'<text x="380" y="{y(o):.2f}" font-family="Arial" font-size="12">O {o:.8g}</text>'
            f'<text x="380" y="{y(c):.2f}" font-family="Arial" font-size="12">C {c:.8g}</text>'
            f'<text x="380" y="{y(l):.2f}" font-family="Arial" font-size="12">L {l:.8g}</text>'
            '</svg>'
        )
        (day_dir / "ratio_candle.svg").write_text(candle_svg, encoding="utf-8")
        lines = [
            f"# Stocks vs Memes — {report['utc_date']} (paper only)", "",
            f"Long **{report['long_symbol']}** / Short **{report['short_symbol']}**", "",
            f"Ratio candle O/H/L/C: {ratio['open']:.10g} / {ratio['high']:.10g} / {ratio['low']:.10g} / {ratio['close']:.10g}",
            f"Ratio ROI (close/open): **{report['ratio_roi_pct']:+.4f}%**",
            f"Daily ratio drawdown (low/open): **{report['daily_ratio_drawdown_pct']:.4f}%**",
            f"Cumulative ratio ROI: **{report['cumulative_ratio_roi_pct']:+.4f}%**",
            f"Maximum ratio drawdown: **{report['maximum_ratio_drawdown_pct']:.4f}%**", "",
            f"Net portfolio P&L after fees/funding: {report['net_pnl_usdt']:+.4f} USDT",
            f"Net portfolio ROI: {report['net_portfolio_roi_pct']:+.4f}%", "",
            "Ratio ROI and ratio drawdown are relative-spread metrics, not leveraged account returns.",
            "Ratio candle high/low use conservative synchronized 1-minute mark-OHLC bounds; they are not exact tick-synchronous extrema.",
            "Cumulative maximum drawdown assumes the daily high precedes the low because OHLC bars do not reveal intrabar tick ordering.",
        ]
        (day_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _open_new_day(self, day_start: datetime, day_end: datetime, now: datetime,
                      snapshot: dict[str, Any]) -> None:
        stock, meme = select_pair(snapshot["stocks"], snapshot["memes"])
        instruments = self._instrument_map(snapshot["instruments"])
        nav = self.store.latest_ending_equity(float(self.config.get("initial_equity_usdt", 10000.0)))
        if self.gross_leverage <= 0 or self.fee_rate < 0:
            raise StrategyError("gross leverage must be positive and taker fee must be non-negative")
        long_pos, long_fill, long_slip = self._open_position(stock, instruments[stock.contract], nav, 1)
        short_pos, short_fill, short_slip = self._open_position(meme, instruments[meme.contract], nav, -1)
        day_key = day_start.date().isoformat()
        payload = {
            "strategy": "stocks-vs-memes-daily-market-neutral", "exchange": "Bybit",
            "classification_provider": "CoinMarketCap", "daily_timezone": "UTC",
            "daily_candle_start": day_start.isoformat(), "scheduled_exit": day_end.isoformat(),
            "actual_entry": now.isoformat(),
            "entry_delay_seconds_after_utc_open": round(now.timestamp() - day_start.timestamp(), 3),
            "starting_equity_usdt": nav, "gross_leverage": self.gross_leverage,
            "long_leg_notional_target_usdt": nav * self.gross_leverage / 2.0,
            "short_leg_notional_target_usdt": nav * self.gross_leverage / 2.0,
            "selections": {"long_stock": asdict(stock), "short_meme": asdict(meme)},
            "positions": {"long": asdict(long_pos), "short": asdict(short_pos)},
            "entry_fills": {"long": asdict(long_fill), "short": asdict(short_fill)},
            "entry_slippage_usdt": {"long": long_slip, "short": short_slip},
            "preflight": snapshot["diagnostics"], "paper_only": True,
            "entry_authorization": "fresh source preflight passed at UTC daily-open trigger",
            "intra_day_actions": "none; next strategy trigger is next UTC daily open",
        }
        self.store.create_day(day_key, int(day_start.timestamp()), int(day_end.timestamp()), payload)

    def process_daily_open(self, now: datetime | None = None) -> str:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(timezone.utc)
        day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        day_end = day_start + timedelta(days=1)
        offset = now.timestamp() - day_start.timestamp()
        if offset < 0 or offset > self.entry_grace:
            return f"waiting for next UTC daily open; no strategy actions taken at {now.isoformat()}"
        day_key = day_start.date().isoformat()
        if self.store.get_day(day_key):
            return f"already processed UTC open {day_key}; no intraday action"

        previous = self.store.get_open_day()
        if previous:
            if int(previous["exit_ts"]) != int(day_start.timestamp()):
                raise DailyBoundaryError(
                    f"Persisted paper session {previous['utc_date']} is not due at this UTC boundary; "
                    "refusing an off-schedule position change"
                )
            try:
                self._close_previous(previous, day_start)
            except Exception as exc:
                self.store.update_status(previous["utc_date"], "CLOSE_PENDING", f"{type(exc).__name__}: {exc}")
                raise

        preflight = self.live_preflight()
        if not preflight.get("passed") or self.last_snapshot is None:
            payload = {"strategy": "stocks-vs-memes-daily-market-neutral", "paper_only": True,
                       "skip_reason": preflight.get("error", "live preflight failed"),
                       "preflight": preflight, "new_exposure_opened": False}
            self.store.create_day(day_key, int(day_start.timestamp()), int(day_end.timestamp()), payload)
            self.store.update_status(day_key, "SKIPPED", str(payload["skip_reason"]))
            return f"skipped {day_key}: live Bybit/CMC preflight failed; no positions opened"

        try:
            self._open_new_day(day_start, day_end, now, self.last_snapshot)
            return f"opened paper session {day_key} from verified Bybit + CoinMarketCap universe"
        except Exception as exc:
            payload = {"strategy": "stocks-vs-memes-daily-market-neutral", "paper_only": True,
                       "skip_reason": f"{type(exc).__name__}: {exc}", "new_exposure_opened": False,
                       "preflight": preflight}
            self.store.create_day(day_key, int(day_start.timestamp()), int(day_end.timestamp()), payload)
            self.store.update_status(day_key, "SKIPPED", payload["skip_reason"])
            return f"skipped {day_key}: {exc}"

    def process_once(self, now: datetime | None = None) -> str:
        return self.process_daily_open(now)

    def run_loop(self) -> None:
        print("Starting daily-boundary scheduler. No ranking, position changes, or performance calculations occur intraday.")
        while True:
            now = datetime.now(timezone.utc)
            day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            day_end = day_start + timedelta(days=1)
            offset = now.timestamp() - day_start.timestamp()
            if offset <= self.entry_grace:
                try:
                    print(self.process_daily_open(now))
                except Exception as exc:
                    print(f"DAILY BOUNDARY BLOCKED: {type(exc).__name__}: {exc}")
                target = day_end
            else:
                target = day_end
            sleep_seconds = max(1.0, target.timestamp() - time.time())
            print(f"Sleeping until next UTC daily open: {target.isoformat()} ({sleep_seconds:.0f}s)")
            time.sleep(sleep_seconds)

    def status(self) -> list[dict[str, Any]]:
        return self.store.list_days(30)
