import csv
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal as D

from bitget_stocks_vs_crypto.portfolio import init_db
from bitget_stocks_vs_crypto.performance import (
    ensure_equity_start, record_equity_point, performance_summary,
    export_performance_artifacts,
)


def utc_ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


class LifetimePerformanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "paper.sqlite3"
        init_db(self.db_path, D("10")).close()
        self.start = utc_ms("2026-01-01T12:00:00Z")

    def tearDown(self):
        self.tmp.cleanup()

    def test_equity_start_is_seeded_once_and_curve_points_are_idempotent(self):
        ensure_equity_start(self.db_path, self.start)
        ensure_equity_start(self.db_path, self.start + 60_000)
        record_equity_point(self.db_path, workflow_started_ms=self.start,
                            valuation_time_ms=self.start + 10_000, nav_usdt="9.98",
                            source="REBALANCE", cash_usdt="9.97",
                            unrealized_pnl_usdt="0.01", gross_exposure_usdt="19.9",
                            long_symbol="SOXSUSDT", short_symbol="BTWUSDT")
        record_equity_point(self.db_path, workflow_started_ms=self.start,
                            valuation_time_ms=self.start + 10_000, nav_usdt="9.99",
                            source="REBALANCE", cash_usdt="9.98",
                            unrealized_pnl_usdt="0.01", gross_exposure_usdt="19.9",
                            long_symbol="SOXSUSDT", short_symbol="BTWUSDT")
        with sqlite3.connect(self.db_path) as db:
            rows = db.execute("SELECT event_type,nav_usdt FROM equity_curve ORDER BY timestamp_ms").fetchall()
        self.assertEqual(rows, [("START", "10"), ("REBALANCE", "9.99")])

    def test_sortino_waits_for_minimum_daily_history_and_downside_days(self):
        ensure_equity_start(self.db_path, self.start)
        early = performance_summary(self.db_path)
        self.assertIsNone(early["sortino"])
        self.assertIn("INSUFFICIENT_HISTORY", early["sortino_status"])

        nav = D("10")
        for day in range(23):
            if day > 0:
                nav *= D("0.99") if day % 3 == 0 else D("1.01")
            timestamp = self.start + day * 86_400_000 + 60_000
            record_equity_point(
                self.db_path, workflow_started_ms=timestamp,
                valuation_time_ms=timestamp + 5_000, nav_usdt=nav,
                source="MONITOR", cash_usdt="10", unrealized_pnl_usdt="0",
                gross_exposure_usdt="20", long_symbol="SOXSUSDT", short_symbol="BTWUSDT"
            )
        summary = performance_summary(self.db_path)
        self.assertGreaterEqual(summary["daily_return_count"], 20)
        self.assertGreaterEqual(summary["downside_days"], 5)
        self.assertEqual(summary["sortino_status"], "AVAILABLE")
        self.assertIsNotNone(summary["sortino"])
        self.assertGreater(len(summary["equity_points"]), 20)
        self.assertIsNotNone(summary["equity_curve_mdd_pct"])

    def test_entire_trade_history_includes_open_and_closed_records(self):
        with sqlite3.connect(self.db_path) as db:
            db.execute("""INSERT INTO trades(
                leg,symbol,entry_time_ms,entry_date,entry_qty,entry_price,entry_notional,
                entry_fee_rate,entry_fee,net_pnl,trade_roi_pct,status)
                VALUES('LONG','SOXSUSDT',?,'2026-01-01','1','10','10','0.0006','0.006','0.50','5.0','CLOSED')""",
                (self.start,))
            db.execute("""INSERT INTO trades(
                leg,symbol,entry_time_ms,entry_date,entry_qty,entry_price,entry_notional,
                entry_fee_rate,entry_fee,status)
                VALUES('SHORT','BTWUSDT',?,'2026-01-02','10','1','10','0.0006','0.006','OPEN')""",
                (self.start + 86_400_000,))
            db.commit()
        summary = performance_summary(self.db_path)
        self.assertEqual(summary["trade_count_total"], 2)
        self.assertEqual(summary["closed_trade_count"], 1)
        self.assertEqual(summary["open_trade_count"], 1)
        self.assertEqual(summary["closed_net_pnl_usdt"], "0.50")
        self.assertEqual(summary["win_rate_pct"], "100")

    def test_exports_full_curve_trade_history_daily_returns_and_svg(self):
        ensure_equity_start(self.db_path, self.start)
        record_equity_point(self.db_path, workflow_started_ms=self.start,
                            valuation_time_ms=self.start, nav_usdt="9.95",
                            source="REBALANCE", cash_usdt="9.95")
        output = Path(self.tmp.name) / "exports"
        export_performance_artifacts(self.db_path, output)
        for name in ("equity_curve.csv","daily_equity.csv","trade_history.csv",
                     "equity_curve.svg","performance_metrics.json"):
            self.assertTrue((output / name).is_file(), name)
            self.assertGreater((output / name).stat().st_size, 0, name)
        with (output / "equity_curve.csv").open(newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 3)  # header plus seed point and one completed valuation

    def test_out_of_order_workflow_observation_fails_closed(self):
        ensure_equity_start(self.db_path, self.start)
        record_equity_point(self.db_path, workflow_started_ms=self.start,
                            valuation_time_ms=self.start, nav_usdt="9.9", source="MONITOR")
        with self.assertRaises(ValueError):
            record_equity_point(self.db_path, workflow_started_ms=self.start - 10,
                                valuation_time_ms=self.start, nav_usdt="9.8", source="MONITOR")


if __name__ == "__main__":
    unittest.main()
