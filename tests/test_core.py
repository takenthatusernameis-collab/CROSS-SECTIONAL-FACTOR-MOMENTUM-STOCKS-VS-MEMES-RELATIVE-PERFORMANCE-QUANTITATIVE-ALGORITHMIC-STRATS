from __future__ import annotations

import unittest
from datetime import datetime, timezone

from stocks_vs_memes.core import (
    UnexecutableMarketError, event_mark_price, funding_cashflow, maximum_drawdown,
    relative_ratio_drift, size_leg, utc_day_bounds, walk_order_book,
)


class CoreTests(unittest.TestCase):
    def test_size_equal_dollar_legs_at_two_times_gross(self):
        notional, qty = size_leg(10000, 2.0, 100, 1, False, 1)
        self.assertEqual(notional, 10000)
        self.assertEqual(qty, 100)

    def test_book_walk_calculates_vwap_for_both_sides(self):
        book = {"bids": [{"p": "99", "s": "4"}, {"p": "98", "s": "10"}],
                "asks": [{"p": "101", "s": "2"}, {"p": "102", "s": "10"}]}
        buy = walk_order_book(book, 4, "buy")
        self.assertAlmostEqual(buy.vwap, (2 * 101 + 2 * 102) / 4)
        sell = walk_order_book(book, 4, "sell")
        self.assertAlmostEqual(sell.vwap, 99)
        self.assertEqual(buy.mid_price, 100)

    def test_book_walk_rejects_partial_visible_depth(self):
        book = {"bids": [{"p": "99", "s": "1"}], "asks": [{"p": "101", "s": "1"}]}
        with self.assertRaises(UnexecutableMarketError):
            walk_order_book(book, 2, "buy")

    def test_funding_cashflow_sign_is_correct(self):
        self.assertAlmostEqual(funding_cashflow(1, 10, 1, 100, 0.001), -1.0)
        self.assertAlmostEqual(funding_cashflow(-1, 10, 1, 100, 0.001), 1.0)
        self.assertAlmostEqual(funding_cashflow(1, 10, 1, 100, -0.001), 1.0)

    def test_ratio_drift_is_distinct_from_portfolio_return(self):
        drift = relative_ratio_drift(110, 100, 90, 100)
        self.assertAlmostEqual(drift, 110 / 90 - 1)
        self.assertNotAlmostEqual(drift, 0.10 - (-0.10))

    def test_maximum_drawdown_uses_running_peak(self):
        pct, usdt = maximum_drawdown([(1, 1000), (2, 1100), (3, 990), (4, 1050), (5, 1200)])
        self.assertAlmostEqual(pct, -0.10)
        self.assertAlmostEqual(usdt, -110)

    def test_funding_event_mark_uses_exact_minute_open(self):
        candles = [{"t": 100, "o": "10", "c": "11"}, {"t": 160, "o": "12", "c": "13"}]
        self.assertEqual(event_mark_price(candles, 160), 12.0)
        self.assertEqual(event_mark_price(candles, 180), 13.0)

    def test_utc_day_boundaries_are_timezone_independent(self):
        dt = datetime(2026, 11, 13, 5, 0, tzinfo=timezone.utc)
        start, end = utc_day_bounds(dt)
        self.assertEqual(start.isoformat(), "2026-11-13T00:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-11-14T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
