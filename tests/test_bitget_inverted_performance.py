import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bitget_stocks_vs_crypto.inverted_performance import build_inverted_equity


class InvertedPerformanceTests(unittest.TestCase):
    def test_reverses_direction_and_funding_but_retains_fees(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db_path = root / "paper.sqlite3"
            con = sqlite3.connect(db_path)
            con.executescript("""
                CREATE TABLE account(id INTEGER PRIMARY KEY, initial_nav TEXT);
                INSERT INTO account VALUES(1, '100');
                CREATE TABLE trades(
                    trade_id INTEGER PRIMARY KEY, leg TEXT, symbol TEXT, entry_time_ms INTEGER,
                    entry_qty TEXT, entry_price TEXT, entry_fee TEXT, exit_time_ms INTEGER,
                    exit_fee TEXT, gross_pnl TEXT, funding_pnl TEXT, status TEXT
                );
                INSERT INTO trades VALUES(1,'LONG','STOCKUSDT',1000,'2','10','0.1',2000,'0.1','4','0.2','CLOSED');
                INSERT INTO trades VALUES(2,'SHORT','COINUSDT',2000,'10','5','0.05',NULL,NULL,NULL,'0','OPEN');
                CREATE TABLE equity_curve(
                    timestamp_ms INTEGER PRIMARY KEY, valuation_time_ms INTEGER, event_type TEXT,
                    nav_usdt TEXT, cash_usdt TEXT, unrealized_pnl_usdt TEXT, gross_exposure_usdt TEXT,
                    portfolio_roi_pct TEXT, long_symbol TEXT, short_symbol TEXT
                );
                INSERT INTO equity_curve VALUES(1000,1000,'START','100','100','0','0','0',NULL,NULL);
                INSERT INTO equity_curve VALUES(2000,2000,'REBALANCE','100','100','0','10','0','STOCKUSDT','COINUSDT');
                CREATE TABLE monitor_observations(
                    observed_at_ms INTEGER PRIMARY KEY, funding_pending_usdt TEXT,
                    long_symbol TEXT, long_mark_price TEXT, short_symbol TEXT, short_mark_price TEXT
                );
                INSERT INTO monitor_observations VALUES(2000,'-0.3','STOCKUSDT','12','COINUSDT','4.5');
                CREATE TABLE daily_snapshots(
                    as_of_ms INTEGER PRIMARY KEY, long_position_json TEXT, short_position_json TEXT
                );
            """)
            con.commit()
            con.close()

            metrics = build_inverted_equity(db_path, root / "out")
            self.assertEqual(metrics["observation_count"], 2)
            self.assertEqual(metrics["latest_nav_usdt"], "90.85")
            self.assertEqual(Decimal(metrics["latest_roi_pct"]), Decimal("-9.15"))
            self.assertTrue((root / "out" / "inverted_equity_curve.svg").is_file())
            self.assertTrue((root / "out" / "inverted_equity_curve.csv").is_file())


if __name__ == "__main__":
    unittest.main()
