import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.render_bitget_status import render_status


class StatusRendererTests(unittest.TestCase):
    def make_db(self, *, with_observation=True, expected=10, matched=10, cooldown=1):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "paper.sqlite3"
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE account(id INTEGER PRIMARY KEY, initial_nav TEXT, cash TEXT,
                realized_gross_pnl TEXT, fees_cumulative TEXT, funding_cumulative TEXT);
            CREATE TABLE trades(trade_id INTEGER PRIMARY KEY, leg TEXT, symbol TEXT,
                entry_time_ms INTEGER, entry_date TEXT, entry_notional TEXT, entry_fee TEXT,
                funding_pnl TEXT, status TEXT);
            CREATE TABLE positions(leg TEXT, trade_id INTEGER, symbol TEXT, qty TEXT,
                entry_price TEXT, entry_notional TEXT, entry_fee_rate TEXT, entry_fee TEXT,
                last_funding_ms INTEGER);
            CREATE TABLE daily_snapshots(snapshot_date TEXT, as_of_ms INTEGER,
                long_symbol TEXT, short_symbol TEXT, nav TEXT, cash TEXT,
                unrealized_pnl TEXT, realized_gross_pnl_cumulative TEXT,
                fees_cumulative TEXT, funding_cumulative TEXT, gross_exposure TEXT,
                total_return_pct TEXT, long_position_json TEXT, short_position_json TEXT);
            CREATE TABLE cooldown_control(id INTEGER PRIMARY KEY, cooldown INTEGER,
                activated_at_ms INTEGER, reset_at_ms INTEGER, last_reset_run_started_ms INTEGER);
            CREATE TABLE monitor_state(id INTEGER PRIMARY KEY, cycle_start_ms INTEGER,
                eligible_after_ms INTEGER, long_trade_id INTEGER, short_trade_id INTEGER,
                nav_start TEXT, nav_last TEXT, daily_mdd_pct TEXT, expected_minutes INTEGER,
                matched_minutes INTEGER, status TEXT, last_error TEXT);
            CREATE TABLE monitor_minute_metrics(cycle_start_ms INTEGER, timestamp_ms INTEGER);
            CREATE TABLE monitor_observations(observed_at_ms INTEGER, cycle_start_ms INTEGER,
                status TEXT, nav_usdt TEXT, unrealized_pnl_usdt TEXT,
                funding_pending_usdt TEXT, gross_exposure_usdt TEXT,
                portfolio_roi_pct TEXT, cycle_roi_pct TEXT, long_symbol TEXT,
                long_mark_price TEXT, long_unrealized_pnl_usdt TEXT, long_trade_roi_pct TEXT,
                short_symbol TEXT, short_mark_price TEXT, short_unrealized_pnl_usdt TEXT,
                short_trade_roi_pct TEXT, strategy_mdd_pct TEXT, expected_minutes INTEGER,
                matched_minutes INTEGER, coverage_pct TEXT, last_error TEXT);
            CREATE TABLE strategy_risk(id INTEGER PRIMARY KEY, max_drawdown_pct TEXT,
                complete_intervals INTEGER, incomplete_intervals INTEGER);
            CREATE TABLE risk_intervals(snapshot_date TEXT, period_end_ms INTEGER, status TEXT);
        """)
        start = 1_800_000_000_000
        db.execute("INSERT INTO account VALUES(1,'10000','9920','5','20','2')")
        db.execute("INSERT INTO trades VALUES(1,'LONG','SOXSUSDT',?,?,?,?,?,'OPEN')",
                   (start, '2026-11-14', '10000', '6', '0'))
        db.execute("INSERT INTO trades VALUES(2,'SHORT','BTWUSDT',?,?,?,?,?,'OPEN')",
                   (start, '2026-11-14', '10000', '6', '0'))
        db.execute("INSERT INTO positions VALUES('LONG',1,'SOXSUSDT','100','50','5000','0.0006','6',?)", (start,))
        db.execute("INSERT INTO positions VALUES('SHORT',2,'BTWUSDT','10000','1','10000','0.0006','6',?)", (start,))
        long_json = json.dumps({"symbol":"SOXSUSDT","quantity":"100","entry_price":"50",
                                "mark_price":"49.9","unrealized_pnl_usdt":"-10",
                                "trade_roi_to_date_pct":"-0.22"})
        short_json = json.dumps({"symbol":"BTWUSDT","quantity":"10000","entry_price":"1",
                                 "mark_price":"1.001","unrealized_pnl_usdt":"-10",
                                 "trade_roi_to_date_pct":"-0.16"})
        db.execute("INSERT INTO daily_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ('2026-11-14', start, 'SOXSUSDT', 'BTWUSDT', '9900', '9920', '-20',
                    '5', '20', '2', '20000', '-1', long_json, short_json))
        db.execute("INSERT INTO cooldown_control VALUES(1,?,?,?,NULL)",
                   (cooldown, start, start + 86_460_000))
        db.execute("INSERT INTO monitor_state VALUES(1,?,?,?,?,?,?,?,?,?,?,?)",
                   (start, start + 86_460_000, 1, 2, '9900', '10050', '-0.4',
                    expected, matched, 'MONITORING' if expected and matched == expected else 'WAITING', None))
        db.execute("INSERT INTO strategy_risk VALUES(1,'-2.6',0,0)")
        for minute in range(matched):
            db.execute("INSERT INTO monitor_minute_metrics VALUES(?,?)", (start, start + (minute + 1) * 60_000))
        if with_observation:
            db.execute("""INSERT INTO monitor_observations VALUES(
                ?,?,'MONITORING','10050','-15','0.5','20100','0.5','0.3',
                'SOXSUSDT','50.1','10','0.12','BTWUSDT','1.002','-25','-0.24',
                '-2.6',?,?,?,NULL)""",
                (start + 600_000, start, expected, matched, '100' if expected else '0'))
        db.commit()
        db.close()
        return tmp, path

    def test_live_observation_drives_nav_positions_roi_mdd_and_coverage(self):
        tmp, path = self.make_db()
        try:
            status = render_status(path, workflow_started_ms=1_800_000_001_000,
                                   generated_at_ms=1_800_000_700_000,
                                   run_id='12345', repository='owner/repo',
                                   event_name='schedule', mode='paper')
            self.assertIn('10,050.00 USDT', status)
            self.assertIn('50.10000000', status)
            self.assertIn('1.00200000', status)
            self.assertIn('0.50%', status)
            self.assertIn('-2.60%', status)
            self.assertIn('10/10 (100.00%)', status)
            self.assertIn('ACTIVE (1)', status)
            self.assertIn('COMPLETE_CURRENT_CYCLE', status)
            self.assertIn('https://github.com/owner/repo/actions/runs/12345', status)
        finally:
            tmp.cleanup()

    def test_fresh_cycle_uses_snapshot_marks_and_discloses_missing_minute_history(self):
        tmp, path = self.make_db(with_observation=False, expected=0, matched=0)
        try:
            status = render_status(path, generated_at_ms=1_800_000_700_000)
            self.assertIn('9,900.00 USDT', status)
            self.assertIn('49.90000000', status)
            self.assertIn('NO_MINUTE_HISTORY', status)
            self.assertIn('awaiting first completed one-minute interval', status)
            self.assertIn('not established from minute history yet', status)
        finally:
            tmp.cleanup()

    def test_cooldown_zero_is_shown_as_ready(self):
        tmp, path = self.make_db(with_observation=False, expected=0, matched=0, cooldown=0)
        try:
            status = render_status(path)
            self.assertIn('READY (0)', status)
        finally:
            tmp.cleanup()

    def test_latest_runner_status_and_warning_are_included(self):
        tmp, path = self.make_db()
        try:
            report = Path(tmp.name) / "report.json"
            report.write_text(json.dumps({
                "status": "MONITORING_WARNING",
                "gates": {"ranking": "PASS"},
                "monitoring": {"status": "MONITORING_BLOCKED", "last_error": "Ticker endpoint timed out"},
            }), encoding="utf-8")
            status = render_status(path, generated_at_ms=1_800_001_000_000,
                                   report_json=report)
            self.assertIn("MONITORING_WARNING", status)
            self.assertIn("MONITORING_BLOCKED", status)
            self.assertIn("Ticker endpoint timed out", status)
            self.assertIn("Age of latest saved mark/snapshot", status)
        finally:
            tmp.cleanup()

    def test_renderer_is_read_only(self):
        tmp, path = self.make_db()
        try:
            before = path.read_bytes()
            render_status(path)
            self.assertEqual(path.read_bytes(), before)
        finally:
            tmp.cleanup()


if __name__ == '__main__':
    unittest.main()
