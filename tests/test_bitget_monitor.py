import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from bitget_stocks_vs_crypto.monitor import monitor_open_positions
from bitget_stocks_vs_crypto.portfolio import (
    cooldown_close_ms, rebalance_gate, run_snapshot, trade_report,
)
from bitget_stocks_vs_crypto.risk import expected_minute_starts


def instrument(symbol, symbol_type, step):
    return {
        "symbol": symbol, "category": "USDT-FUTURES", "quoteCoin": "USDT",
        "baseCoin": symbol.removesuffix("USDT"), "status": "online", "type": "perpetual",
        "symbolType": symbol_type, "minOrderQty": step, "quantityMultiplier": step,
        "quantityPrecision": len(step.split(".")[-1]) if "." in step else 0,
        "maxMarketOrderQty": "0", "minOrderAmount": "5", "takerFeeRate": "0.0006",
    }


def ranking(as_of):
    long = instrument("STOCKUSDT", "stock", "0.01")
    short = instrument("CRYPTOUSDT", "crypto", "0.1")
    long.update({"price24hPcnt":"0.05","return_24h_pct":"5","markPrice":"100","lastPrice":"100"})
    short.update({"price24hPcnt":"-0.10","return_24h_pct":"-10","markPrice":"10","lastPrice":"10"})
    return {
        "run_at_utc":"2026-11-14T00:00:00Z", "as_of_server_time_ms":as_of,
        "gates":{"ranking":"PASS"},
        "leaders":{"long_stock_top_gainer":{"symbol":"STOCKUSDT"},
                   "short_crypto_bottom_loser":{"symbol":"CRYPTOUSDT"}},
        "market_results":[long,short],
    }


def books(as_of):
    return {
        "STOCKUSDT":{"ts":str(as_of),"bids":[["99.9","1000"]],"asks":[["100.1","1000"]]},
        "CRYPTOUSDT":{"ts":str(as_of),"bids":[["9.99","100000"]],"asks":[["10.01","100000"]]},
    }


class CooldownTests(unittest.TestCase):
    def test_cooldown_requires_first_full_minute_candle_after_24h_expiry(self):
        entry = 1_800_000_000_000  # exactly minute-aligned
        self.assertEqual(cooldown_close_ms(entry), entry + 86_400_000 + 60_000)
        off_minute = entry + 17_000
        expiry = off_minute + 86_400_000
        expected = ((expiry + 59_999) // 60_000) * 60_000 + 60_000
        self.assertEqual(cooldown_close_ms(off_minute), expected)

    def test_run_start_not_expiry_minute_controls_rebalance_gate(self):
        entry = 1_800_000_000_000
        due = cooldown_close_ms(entry)
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            b = books(entry)
            run_snapshot(ranking(entry), db_path, depth_fetch=lambda s:b[s],
                funding_fetch=lambda s,a,z:[], funding_mark_fetch=lambda s,t:D("100"),
                initial_nav=D("10000"))
            self.assertFalse(rebalance_gate(db_path, due - 1)["can_rebalance"])
            self.assertTrue(rebalance_gate(db_path, due)["can_rebalance"])
            # A later five-minute run also qualifies; no exact-minute coincidence is needed.
            self.assertTrue(rebalance_gate(db_path, due + 5 * 60_000)["can_rebalance"])

    def test_snapshot_cannot_rebalance_at_24h_before_required_candle_close(self):
        entry = 1_800_000_000_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            b1 = books(entry)
            run_snapshot(ranking(entry), db_path, depth_fetch=lambda s:b1[s],
                funding_fetch=lambda s,a,z:[], funding_mark_fetch=lambda s,t:D("100"),
                initial_nav=D("10000"))
            before = trade_report(db_path)
            too_early = entry + 86_400_000
            b2 = books(too_early)
            result = run_snapshot(ranking(too_early), db_path, depth_fetch=lambda s:b2[s],
                funding_fetch=lambda s,a,z:[], funding_mark_fetch=lambda s,t:D("100"),
                initial_nav=D("10000"), run_started_ms=too_early)
            after = trade_report(db_path)
            self.assertEqual(result["status"], "COOLDOWN_ACTIVE")
            self.assertEqual(len(after["open_trades"]), 2)
            self.assertEqual(len(after["closed_trades"]), len(before["closed_trades"]))
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
                                 len(before["open_trades"]))


class MonitoringIsolationTests(unittest.TestCase):
    def test_five_minute_monitor_tracks_both_legs_without_mutating_portfolio(self):
        entry = 1_800_000_000_000
        now = entry + 5 * 60_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            b = books(entry)
            run_snapshot(ranking(entry), db_path, depth_fetch=lambda s:b[s],
                funding_fetch=lambda s,a,z:[], funding_mark_fetch=lambda s,t:D("100"),
                initial_nav=D("10000"))

            def portfolio_state():
                with sqlite3.connect(db_path) as db:
                    return {
                        "cash":db.execute("SELECT cash FROM account WHERE id=1").fetchone()[0],
                        "positions":db.execute("SELECT leg,trade_id,symbol,qty,entry_price FROM positions ORDER BY leg").fetchall(),
                        "trades":db.execute("SELECT COUNT(*) FROM trades").fetchone()[0],
                        "ledger":db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],
                        "snapshots":db.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0],
                    }
            before = portfolio_state()

            def ticker_fetch(symbols, timestamp):
                return {
                    "STOCKUSDT":{"timestamp_ms":timestamp,"mark_price":"101","last_price":"101"},
                    "CRYPTOUSDT":{"timestamp_ms":timestamp,"mark_price":"9.8","last_price":"9.8"},
                }

            def candle_fetch(symbol, start_ms, end_ms, candle_type):
                base = D("100") if symbol == "STOCKUSDT" else D("10")
                rows = []
                for ts in expected_minute_starts(start_ms, end_ms):
                    if symbol == "STOCKUSDT":
                        o,h,l,cl = ("100","101","99","100")
                    else:
                        o,h,l,cl = ("10","10.2","9.8","10")
                    rows.append({"timestamp_ms":ts,"open":o,"high":h,"low":l,"close":cl})
                return rows

            result = monitor_open_positions(
                db_path, now_ms=now, initial_nav=D("10000"),
                ticker_fetch=ticker_fetch,
                funding_fetch=lambda symbol,start,end:[],
                funding_mark_fetch=lambda symbol,ts:D("100"),
                candle_fetch=candle_fetch,
            )
            after = portfolio_state()
            self.assertEqual(result["status"], "MONITORING")
            self.assertTrue(result["analytics_updated"])
            self.assertFalse(result["portfolio_state_changed"])
            self.assertEqual(set(result["open_positions"]), {"LONG","SHORT"})
            self.assertEqual(before, after, "monitoring must not mutate account/positions/trades/ledger/snapshots")
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM monitor_observations").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM monitor_minute_metrics").fetchone()[0], 5)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0], 1)
            self.assertFalse(rebalance_gate(db_path, entry + 86_400_000)["can_rebalance"])

    def test_missing_minute_is_not_committed_and_later_run_recovers_gap(self):
        entry = 1_800_000_000_000
        first_now = entry + 5 * 60_000
        second_now = entry + 10 * 60_000
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "paper.sqlite3"
            b = books(entry)
            run_snapshot(ranking(entry), db_path, depth_fetch=lambda s:b[s],
                funding_fetch=lambda s,a,z:[], funding_mark_fetch=lambda s,t:D("100"),
                initial_nav=D("10000"))

            tickers = lambda symbols, timestamp: {
                "STOCKUSDT":{"timestamp_ms":timestamp,"mark_price":"100","last_price":"100"},
                "CRYPTOUSDT":{"timestamp_ms":timestamp,"mark_price":"10","last_price":"10"},
            }
            def candles_with_gap(symbol, start_ms, end_ms, candle_type):
                rows=[]
                for ts in expected_minute_starts(start_ms,end_ms):
                    if symbol == "CRYPTOUSDT" and candle_type == "mark" and ts == entry + 2*60_000:
                        continue
                    values=("100","101","99","100") if symbol == "STOCKUSDT" else ("10","10.2","9.8","10")
                    rows.append({"timestamp_ms":ts,"open":values[0],"high":values[1],
                                 "low":values[2],"close":values[3]})
                return rows
            def complete_candles(symbol, start_ms, end_ms, candle_type):
                rows=[]
                for ts in expected_minute_starts(start_ms,end_ms):
                    values=("100","101","99","100") if symbol == "STOCKUSDT" else ("10","10.2","9.8","10")
                    rows.append({"timestamp_ms":ts,"open":values[0],"high":values[1],
                                 "low":values[2],"close":values[3]})
                return rows

            with sqlite3.connect(db_path) as db:
                mdd_before_monitor = db.execute("SELECT max_drawdown_pct FROM strategy_risk WHERE id=1").fetchone()[0]

            incomplete = monitor_open_positions(
                db_path, now_ms=first_now, initial_nav=D("10000"), ticker_fetch=tickers,
                funding_fetch=lambda symbol,start,end:[],
                funding_mark_fetch=lambda symbol,ts:D("100"), candle_fetch=candles_with_gap)
            self.assertEqual(incomplete["status"], "MONITORING_INCOMPLETE")
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM monitor_minute_metrics").fetchone()[0], 0)
                self.assertIsNone(db.execute("SELECT last_processed_minute_ms FROM monitor_state WHERE id=1").fetchone()[0])
                mdd_after_gap = db.execute("SELECT max_drawdown_pct FROM strategy_risk WHERE id=1").fetchone()[0]
            self.assertEqual(mdd_after_gap, mdd_before_monitor, "an incomplete minute segment must not update strategy MDD")

            recovered = monitor_open_positions(
                db_path, now_ms=second_now, initial_nav=D("10000"), ticker_fetch=tickers,
                funding_fetch=lambda symbol,start,end:[],
                funding_mark_fetch=lambda symbol,ts:D("100"), candle_fetch=complete_candles)
            self.assertEqual(recovered["status"], "MONITORING")
            with sqlite3.connect(db_path) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM monitor_minute_metrics").fetchone()[0], 10)
                self.assertEqual(db.execute("SELECT matched_minutes FROM monitor_state WHERE id=1").fetchone()[0], 10)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM monitor_observations").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
