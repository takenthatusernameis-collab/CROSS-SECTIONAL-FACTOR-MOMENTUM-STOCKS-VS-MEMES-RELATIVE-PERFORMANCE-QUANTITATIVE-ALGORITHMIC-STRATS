import sqlite3
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path

from binance_stocks_vs_crypto.paper_portfolio import (
    PaperError, funding_cash, init_db, parse_book, run_day, size_order, vwap,
)

class PaperPortfolioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "portfolio.sqlite3"
        self.t = 1_800_000_000_000
        self.filters = [
            {"filterType":"MARKET_LOT_SIZE","minQty":"0.01","maxQty":"1000000","stepSize":"0.01"},
            {"filterType":"MIN_NOTIONAL","notional":"5"},
        ]
        self.books = {
            "STOCKUSDT":{"bids":[["99.9","100000"]],"asks":[["100.1","100000"]]},
            "MEMEUSDT":{"bids":[["49.9","100000"]],"asks":[["50.1","100000"]]},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, day=0):
        t = self.t + day * 86_400_000
        return {
            "run_at_utc":"2027-01-15T00:05:00Z",
            "exchange_info":{"as_of_server_time_ms":t},
            "gates":{"ranking_diagnostic":"PASS"},
            "provisional_leaders":{
                "provisional_long_leader":{"symbol":"STOCKUSDT"},
                "provisional_meme_short_leader":{"symbol":"MEMEUSDT"},
            },
            "market_results":[
                {"symbol":"STOCKUSDT","valid":True,"quote_asset":"USDT","margin_asset":"USDT","filters":self.filters},
                {"symbol":"MEMEUSDT","valid":True,"quote_asset":"USDT","margin_asset":"USDT","filters":self.filters},
            ],
        }

    def depth(self, symbol):
        return self.books[symbol]

    def no_funding(self, symbol, start, end):
        return []

    def run_it(self, report=None, funding=None, depth=None):
        return run_day(report or self.report(),self.db,
            depth_fetch=depth or self.depth,funding_fetch=funding or self.no_funding)

    def test_vwap_walks_multiple_levels_and_fails_on_short_depth(self):
        self.assertEqual(vwap([(D("10"),D("2")),(D("11"),D("3"))],D("4")),D("10.5"))
        with self.assertRaises(PaperError):
            vwap([(D("10"),D("1"))],D("2"))

    def test_depth_parser_rejects_crossed_book(self):
        with self.assertRaises(PaperError):
            parse_book({"bids":[["101","1"]],"asks":[["100","1"]]})

    def test_lot_step_rounding(self):
        market={"symbol":"X","filters":self.filters}
        qty,price,notional=size_order(D("99.95"),[(D("10"),D("100"))],market)
        self.assertEqual(qty,D("9.99"))
        self.assertEqual(price,D("10"))
        self.assertEqual(notional,D("99.90"))

    def test_funding_sign_for_long_and_short(self):
        event={"fundingRate":"0.001","markPrice":"100"}
        self.assertEqual(funding_cash("LONG",D("2"),event),D("-0.200"))
        self.assertEqual(funding_cash("SHORT",D("2"),event),D("0.200"))

    def test_first_day_snapshot_and_two_open_ledger_rows(self):
        result=self.run_it()
        self.assertEqual(result["status"],"UPDATED")
        self.assertEqual(result["ledger_rows_added"],2)
        self.assertEqual(result["snapshot"]["long_position"]["symbol"],"STOCKUSDT")
        self.assertEqual(result["snapshot"]["short_position"]["symbol"],"MEMEUSDT")
        db=init_db(self.db,D("10000"))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM positions").fetchone()[0],2)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],2)
        db.close()

    def test_second_day_books_funding_and_close_open(self):
        self.run_it()
        def funding(symbol,start,end):
            return [{"symbol":symbol,"fundingRate":"0.0001","markPrice":"100" if symbol=="STOCKUSDT" else "50",
                     "fundingTime":self.t+43_200_000,"rateType":"Regular"}]
        result=self.run_it(self.report(1),funding=funding)
        self.assertEqual(result["status"],"UPDATED")
        self.assertEqual(result["ledger_rows_added"],6)
        db=sqlite3.connect(self.db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0],2)
        kinds=dict(db.execute("SELECT event_type,COUNT(*) FROM ledger GROUP BY event_type").fetchall())
        self.assertEqual(kinds,{"CLOSE":2,"FUNDING":2,"OPEN":4})
        db.close()

    def test_same_day_rerun_is_idempotent(self):
        self.run_it()
        result=self.run_it()
        self.assertEqual(result["status"],"ALREADY_PROCESSED")
        db=sqlite3.connect(self.db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],2)
        db.close()

    def test_ranking_failure_and_depth_failure_do_not_commit_partial_state(self):
        bad=self.report()
        bad["gates"]["ranking_diagnostic"]="INCOMPLETE"
        with self.assertRaises(PaperError):
            self.run_it(bad)
        def broken(symbol):
            raise PaperError("no order book")
        with self.assertRaises(PaperError):
            self.run_it(depth=broken)
        db=sqlite3.connect(self.db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_snapshots").fetchone()[0],0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],0)
        db.close()

    def test_initial_nav_is_immutable(self):
        db=init_db(self.db,D("10000"))
        db.close()
        with self.assertRaises(PaperError):
            init_db(self.db,D("5000"))

if __name__=="__main__":
    unittest.main()
