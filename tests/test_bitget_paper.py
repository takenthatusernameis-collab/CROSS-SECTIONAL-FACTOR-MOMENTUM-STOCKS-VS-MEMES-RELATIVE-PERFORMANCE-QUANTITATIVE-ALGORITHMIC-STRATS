import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from bitget_stocks_vs_crypto.market import classify_instruments
from bitget_stocks_vs_crypto.portfolio import (
    PaperError, parse_book, run_snapshot, size_for_target_notional, trade_report,
)


def instrument(symbol, symbol_type, *, price="100", step="0.01"):
    return {
        "symbol": symbol, "category": "USDT-FUTURES", "quoteCoin": "USDT",
        "baseCoin": symbol.removesuffix("USDT"), "status": "online", "type": "perpetual",
        "symbolType": symbol_type, "minOrderQty": step, "quantityMultiplier": step,
        "quantityPrecision": len(step.split(".")[-1]) if "." in step else 0,
        "maxMarketOrderQty": "0", "minOrderAmount": "5", "takerFeeRate": "0.0006",
        "price": price,
    }


def ranking(as_of, stock_price="100", crypto_price="10"):
    long = instrument("STOCKUSDT", "stock", price=stock_price)
    short = instrument("CRYPTOUSDT", "crypto", price=crypto_price, step="0.1")
    long.update({"price24hPcnt": "0.05", "return_24h_pct": "5.0", "markPrice": stock_price,
                 "lastPrice": stock_price, "status": "online"})
    short.update({"price24hPcnt": "-0.10", "return_24h_pct": "-10.0", "markPrice": crypto_price,
                  "lastPrice": crypto_price, "status": "online"})
    return {
        "run_at_utc": "2026-11-14T00:00:00Z", "as_of_server_time_ms": as_of,
        "gates": {"ranking": "PASS"},
        "leaders": {"long_stock_top_gainer": {"symbol": "STOCKUSDT"},
                    "short_crypto_bottom_loser": {"symbol": "CRYPTOUSDT"}},
        "market_results": [long, short],
    }


def books(as_of, stock_bid="99.9", stock_ask="100.1", crypto_bid="9.99", crypto_ask="10.01"):
    return {
        "STOCKUSDT": {"ts": str(as_of), "bids": [[stock_bid, "1000"]], "asks": [[stock_ask, "1000"]]},
        "CRYPTOUSDT": {"ts": str(as_of), "bids": [[crypto_bid, "100000"]], "asks": [[crypto_ask, "100000"]]},
    }


class UniverseTests(unittest.TestCase):
    def test_uses_native_symbol_type_and_excludes_other_classes(self):
        stocks, crypto, excluded = classify_instruments([
            instrument("AAPLUSDT", "stock"),
            instrument("BTCUSDT", "crypto"),
            {**instrument("XAUUSDT", "metal"), "symbolType": "metal"},
            {**instrument("OFFUSDT", "stock"), "status": "offline"},
            {**instrument("DELIVERYUSDT", "crypto"), "type": "delivery"},
        ])
        self.assertEqual([x["symbol"] for x in stocks], ["AAPLUSDT"])
        self.assertEqual([x["symbol"] for x in crypto], ["BTCUSDT"])
        self.assertEqual(sum(excluded.values()), 3)

    def test_duplicate_symbol_fails_closed(self):
        with self.assertRaises(Exception):
            classify_instruments([instrument("DUPUSDT", "stock"), instrument("DUPUSDT", "crypto")])


class ExecutionTests(unittest.TestCase):
    def test_order_book_rejects_missing_timestamp_and_crossed_quotes(self):
        with self.assertRaises(PaperError):
            parse_book({"bids": [["10", "1"]], "asks": [["11", "1"]]})
        with self.assertRaises(PaperError):
            parse_book({"ts": "1000", "bids": [["11", "1"]], "asks": [["10", "1"]]})

    def test_size_obeys_quantity_step_and_min_notional(self):
        market = instrument("SAMPLEUSDT", "stock", step="0.1")
        qty, px, notional = size_for_target_notional(D("1000"), [(D("100"), D("1000"))], market)
        self.assertEqual(qty % D("0.1"), D("0"))
        self.assertLessEqual(notional, D("1000"))


class PaperLedgerTests(unittest.TestCase):
    def test_first_snapshot_opens_paper_pair_and_records_portfolio_roi(self):
        as_of = 1_800_000_000_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            book_map = books(as_of)
            result = run_snapshot(ranking(as_of), db_path,
                depth_fetch=lambda symbol: book_map[symbol],
                funding_fetch=lambda symbol, start, end: [],
                funding_mark_fetch=lambda symbol, timestamp: D("100"),
                initial_nav=D("10000"))
            self.assertEqual(result["status"], "UPDATED")
            self.assertIsNone(result["snapshot"]["daily_return_pct"])
            self.assertLess(D(result["snapshot"]["nav_usdt"]), D("10000"))
            self.assertEqual(set(result["snapshot"]["open_positions"]), {"LONG", "SHORT"})
            view = trade_report(db_path)
            self.assertEqual(len(view["open_trades"]), 2)
            self.assertEqual(view["portfolio_total_roi_pct"], result["snapshot"]["total_return_pct"])
            repeated = run_snapshot(ranking(as_of), db_path,
                depth_fetch=lambda symbol: book_map[symbol],
                funding_fetch=lambda symbol, start, end: [],
                funding_mark_fetch=lambda symbol, timestamp: D("100"),
                initial_nav=D("10000"))
            self.assertEqual(repeated["status"], "ALREADY_PROCESSED")
            self.assertEqual(len(trade_report(db_path)["open_trades"]), 2)

    def test_second_snapshot_closes_trades_and_tracks_fee_funding_adjusted_roi(self):
        day1 = 1_800_000_000_000
        day2 = day1 + 86_400_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            first_books = books(day1)
            run_snapshot(ranking(day1), db_path,
                depth_fetch=lambda symbol: first_books[symbol],
                funding_fetch=lambda symbol, start, end: [],
                funding_mark_fetch=lambda symbol, timestamp: D("100"),
                initial_nav=D("10000"))

            second_books = books(day2, stock_bid="102", stock_ask="102.1", crypto_bid="9.4", crypto_ask="9.41")
            def funding(symbol, start, end):
                return [{"symbol": symbol, "fundingTime": str(day1 + 8*60*60*1000), "fundingRate": "0.0001"}]
            def mark(symbol, timestamp):
                return D("100") if symbol == "STOCKUSDT" else D("10")
            result = run_snapshot(ranking(day2, stock_price="102.05", crypto_price="9.405"), db_path,
                depth_fetch=lambda symbol: second_books[symbol], funding_fetch=funding,
                funding_mark_fetch=mark, initial_nav=D("10000"))
            self.assertEqual(result["status"], "UPDATED")
            self.assertEqual(len(result["snapshot"]["closed_trades_today"]), 2)
            self.assertIsNotNone(result["snapshot"]["daily_return_pct"])
            view = trade_report(db_path)
            self.assertEqual(len(view["closed_trades"]), 2)
            self.assertEqual(len(view["open_trades"]), 2)
            closed = view["closed_trades"]
            self.assertTrue(all(row["net_pnl"] is not None and row["trade_roi_pct"] is not None for row in closed))
            self.assertTrue(any(D(row["funding_pnl"]) != 0 for row in closed))
            self.assertIsNotNone(view["portfolio_total_roi_pct"])

    def test_insufficient_depth_does_not_mutate_existing_state(self):
        day1 = 1_800_000_000_000
        day2 = day1 + 86_400_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            b1 = books(day1)
            run_snapshot(ranking(day1), db_path, depth_fetch=lambda s: b1[s],
                funding_fetch=lambda s, a, b: [], funding_mark_fetch=lambda s, t: D("100"))
            before = trade_report(db_path)
            bad = books(day2)
            bad["CRYPTOUSDT"]["bids"] = [["10", "0.01"]]
            with self.assertRaises(PaperError):
                run_snapshot(ranking(day2), db_path, depth_fetch=lambda s: bad[s],
                    funding_fetch=lambda s, a, b: [], funding_mark_fetch=lambda s, t: D("100"))
            after = trade_report(db_path)
            self.assertEqual(before["latest_snapshot"]["snapshot_date"], after["latest_snapshot"]["snapshot_date"])
            self.assertEqual(len(before["open_trades"]), len(after["open_trades"]))


if __name__ == "__main__":
    unittest.main()
