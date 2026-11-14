import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from bitget_stocks_vs_crypto.market import classify_instruments
from bitget_stocks_vs_crypto.portfolio import (
    PaperError, parse_book, rebalance_gate, run_snapshot, size_for_target_notional, trade_report,
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
            self.assertEqual(repeated["status"], "COOLDOWN_ACTIVE")
            self.assertEqual(len(trade_report(db_path)["open_trades"]), 2)

    def test_clean_ten_usdt_initialization_has_binary_cooldown_zero_and_empty_ledger(self):
        import sqlite3
        from bitget_stocks_vs_crypto.portfolio import init_db
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            db = init_db(db_path, D("10"))
            db.close()
            with sqlite3.connect(db_path) as db:
                account = db.execute("SELECT initial_nav,cash FROM account WHERE id=1").fetchone()
                cooldown = db.execute("SELECT cooldown,activated_at_ms,reset_at_ms FROM cooldown_control WHERE id=1").fetchone()
                positions = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
                trades = db.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            self.assertEqual(account, ("10", "10"))
            self.assertEqual(cooldown, (0, None, None))
            self.assertEqual(positions, 0)
            self.assertEqual(trades, 0)

    def test_ten_usdt_starting_capital_can_open_both_minimum_notional_legs(self):
        as_of = 1_800_000_000_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            book_map = books(as_of)
            result = run_snapshot(ranking(as_of), db_path,
                depth_fetch=lambda symbol: book_map[symbol],
                funding_fetch=lambda symbol, start, end: [],
                funding_mark_fetch=lambda symbol, timestamp: D("100"),
                initial_nav=D("10"), run_started_ms=as_of)
            self.assertEqual(result["status"], "UPDATED")
            self.assertLess(D(result["snapshot"]["nav_usdt"]), D("10"))
            self.assertEqual(set(result["snapshot"]["open_positions"]), {"LONG", "SHORT"})
            self.assertTrue(all(D(pos["entry_notional_usdt"]) >= D("5")
                                for pos in result["snapshot"]["open_positions"].values()))
            with __import__("sqlite3").connect(db_path) as db:
                self.assertEqual(db.execute("SELECT initial_nav FROM account WHERE id=1").fetchone()[0], "10")
                self.assertEqual(db.execute("SELECT cooldown FROM cooldown_control WHERE id=1").fetchone()[0], 1)

    def test_cooldown_is_binary_and_resets_on_first_run_after_scheduled_minute(self):
        run_started = 1_800_000_000_123
        as_of = run_started + 20_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            book_map = books(as_of)
            result = run_snapshot(ranking(as_of), db_path,
                depth_fetch=lambda symbol: book_map[symbol],
                funding_fetch=lambda symbol, start, end: [],
                funding_mark_fetch=lambda symbol, timestamp: D("100"),
                initial_nav=D("10000"), run_started_ms=run_started)
            self.assertEqual(result["status"], "UPDATED")
            expected_reset = (run_started // 60_000) * 60_000 + 86_400_000 + 60_000
            with __import__("sqlite3").connect(db_path) as db:
                value, activated, reset = db.execute(
                    "SELECT cooldown,activated_at_ms,reset_at_ms FROM cooldown_control WHERE id=1"
                ).fetchone()
            self.assertEqual(value, 1)
            self.assertEqual(activated, run_started)
            self.assertEqual(reset, expected_reset)

            before = rebalance_gate(db_path, expected_reset - 1)
            self.assertEqual(before["cooldown"], 1)
            self.assertFalse(before["can_rebalance"])
            after_start = expected_reset + 5 * 60_000
            after = rebalance_gate(db_path, after_start)
            self.assertEqual(after["cooldown"], 0)
            self.assertTrue(after["can_rebalance"])
            self.assertEqual(after["last_reset_run_started_ms"], after_start)

            # Simulate a failed ranking/depth attempt on the first eligible run.
            # A later retry must not move the already-fixed measurement boundary.
            retry_start = after_start + 5 * 60_000
            retry_gate = rebalance_gate(db_path, retry_start)
            self.assertEqual(retry_gate["cooldown"], 0)
            self.assertEqual(retry_gate["last_reset_run_started_ms"], after_start)

            with __import__("sqlite3").connect(db_path) as db:
                self.assertEqual(db.execute(
                    "SELECT cooldown FROM cooldown_control WHERE id=1"
                ).fetchone()[0], 0)

            next_books = books(retry_start)
            next_result = run_snapshot(ranking(retry_start), db_path,
                depth_fetch=lambda symbol: next_books[symbol],
                funding_fetch=lambda symbol, start, end: [],
                funding_mark_fetch=lambda symbol, timestamp: D("100"),
                initial_nav=D("10000"), run_started_ms=retry_start)
            self.assertEqual(next_result["status"], "UPDATED")
            self.assertEqual(next_result["cooldown"]["value"], 1)
            self.assertEqual(next_result["cooldown"]["activated_at_ms"], retry_start)
            self.assertEqual(next_result["closed_period_performance"]["period_end_ms"], after_start)
            self.assertEqual(next_result["closed_period_performance"]["rebalance_delay_ms"], 5 * 60_000)

    def test_second_snapshot_closes_trades_and_tracks_fee_funding_adjusted_roi(self):
        day1 = 1_800_000_000_000
        day2 = day1 + 86_460_000
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

    def test_second_snapshot_persists_minute_spread_and_strategy_mdd(self):
        day1 = 1_800_000_000_000
        day2 = day1 + 86_460_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            first_books = books(day1)
            run_snapshot(ranking(day1), db_path,
                depth_fetch=lambda symbol: first_books[symbol],
                funding_fetch=lambda symbol, start, end: [],
                funding_mark_fetch=lambda symbol, timestamp: D("100"),
                initial_nav=D("10000"))

            def candle_fetch(symbol, start_ms, end_ms, candle_type):
                rows = []
                for minute in range(1441):
                    ts = start_ms + minute * 60_000
                    if symbol == "STOCKUSDT":
                        if candle_type == "mark" and minute == 600:
                            values = ("100", "101", "80", "90")
                        elif candle_type == "market" and minute == 600:
                            values = ("100", "102", "98", "100")
                        else:
                            values = ("100", "101", "99", "100")
                    else:
                        if candle_type == "mark" and minute == 600:
                            values = ("10", "15", "9", "12")
                        elif candle_type == "market" and minute == 600:
                            values = ("10", "10.5", "9.5", "10")
                        else:
                            values = ("10", "10.1", "9.9", "10")
                    rows.append({"timestamp_ms": ts, "open": values[0], "high": values[1],
                                 "low": values[2], "close": values[3]})
                return rows

            second_books = books(day2, stock_bid="102", stock_ask="102.1",
                                 crypto_bid="9.4", crypto_ask="9.41")
            result = run_snapshot(ranking(day2, stock_price="102.05", crypto_price="9.405"),
                db_path, depth_fetch=lambda symbol: second_books[symbol],
                funding_fetch=lambda symbol, start, end: [],
                funding_mark_fetch=lambda symbol, timestamp: D("100"),
                candle_fetch=candle_fetch, initial_nav=D("10000"))
            self.assertEqual(result["status"], "UPDATED")
            view = trade_report(db_path)
            risk = view["risk"]
            self.assertEqual(risk["latest_interval"]["status"], "COMPLETE")
            self.assertEqual(risk["latest_interval"]["expected_minutes"], 1441)
            self.assertEqual(risk["latest_interval"]["matched_minutes"], 1441)
            self.assertLess(D(risk["latest_interval"]["spread_drawdown_pct"]), D("0"))
            self.assertLess(D(risk["strategy"]["max_drawdown_pct"]), D("0"))
            self.assertEqual(risk["strategy"]["complete_intervals"], 1)
            self.assertEqual(risk["strategy"]["incomplete_intervals"], 0)

    def test_insufficient_depth_does_not_mutate_existing_state(self):
        day1 = 1_800_000_000_000
        day2 = day1 + 86_460_000
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
