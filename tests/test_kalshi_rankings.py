import unittest
from decimal import Decimal

from kalshi_stocks_vs_crypto.rankings import (
    build_provisional_leaders,
    calculate_trailing_return,
    split_active_universe,
)


class UniverseScopeTests(unittest.TestCase):
    def test_equities_and_indices_long_crypto_short_other_classes_excluded(self):
        markets = [
            {"ticker": "STOCK", "asset_class": "Equities"},
            {"ticker": "ETF", "asset_class": "Equities"},
            {"ticker": "INDEX", "asset_class": "Indices"},
            {"ticker": "BTC", "asset_class": "Crypto"},
            {"ticker": "GOLD", "asset_class": "Metals"},
            {"ticker": "OIL", "asset_class": "Energy"},
        ]
        result = split_active_universe(markets)
        self.assertEqual([m["ticker"] for m in result["long"]], ["STOCK", "ETF", "INDEX"])
        self.assertEqual([m["ticker"] for m in result["short"]], ["BTC"])
        self.assertEqual([m["ticker"] for m in result["excluded"]], ["GOLD", "OIL"])

    def test_unknown_classes_are_excluded_not_inferred_from_ticker(self):
        result = split_active_universe([
            {"ticker": "AAPL", "asset_class": "Unknown", "title": "Apple Inc."},
            {"ticker": "BTC", "asset_class": "Crypto"},
        ])
        self.assertEqual(result["long"], [])
        self.assertEqual([m["ticker"] for m in result["excluded"]], ["AAPL"])

    def test_duplicate_or_missing_tickers_fail_closed(self):
        with self.assertRaises(ValueError):
            split_active_universe([
                {"ticker": "DUP", "asset_class": "Equities"},
                {"ticker": "DUP", "asset_class": "Crypto"},
            ])
        with self.assertRaises(ValueError):
            split_active_universe([{"asset_class": "Crypto"}])


class ReturnCalculationTests(unittest.TestCase):
    def setUp(self):
        self.as_of = 200_099
        self.current_ts = 200_040
        self.reference_ts = self.current_ts - 86_400 - 60

    def test_calculates_near_24h_return_from_completed_trade_closes(self):
        candles = [
            {"end_period_ts": self.reference_ts, "price": {"close": "100.00"}},
            {"end_period_ts": self.current_ts, "price": {"close": "110.00"}},
        ]
        result = calculate_trailing_return(candles, self.as_of)
        self.assertTrue(result["valid"])
        self.assertEqual(Decimal(result["return_24h_pct"]), Decimal("10.00000000"))
        self.assertEqual(result["observation_interval_seconds"], 86_460)
        self.assertEqual(result["reference_gap_seconds"], 60)

    def test_ignores_null_invalid_and_future_closes(self):
        candles = [
            {"end_period_ts": self.reference_ts, "price": {"close": None}},
            {"end_period_ts": self.reference_ts - 60, "price": {"close": "100"}},
            {"end_period_ts": self.current_ts, "price": {"close": "110"}},
            {"end_period_ts": self.current_ts + 60, "price": {"close": "999"}},
        ]
        result = calculate_trailing_return(candles, self.as_of)
        self.assertTrue(result["valid"])
        self.assertEqual(result["current_price"], "110")
        self.assertEqual(result["reference_price"], "100")

    def test_rejects_stale_current_price(self):
        candles = [
            {"end_period_ts": self.reference_ts, "price": {"close": "100"}},
            {"end_period_ts": self.current_ts - 1_200, "price": {"close": "110"}},
        ]
        result = calculate_trailing_return(candles, self.as_of)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "stale_current_price")

    def test_rejects_stale_reference_price(self):
        candles = [
            {"end_period_ts": self.reference_ts - 2_000, "price": {"close": "100"}},
            {"end_period_ts": self.current_ts, "price": {"close": "110"}},
        ]
        result = calculate_trailing_return(candles, self.as_of)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "stale_24h_reference_price")

    def test_rejects_nonpositive_or_nonfinite_prices(self):
        candles = [
            {"end_period_ts": self.reference_ts, "price": {"close": "0"}},
            {"end_period_ts": self.reference_ts + 60, "price": {"close": "NaN"}},
        ]
        result = calculate_trailing_return(candles, self.as_of)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "insufficient_valid_trade_closes")

    def test_provisional_leaders_are_selected_by_return(self):
        long = [
            {"ticker": "AAA", "valid": True, "return_24h_pct": "5.0"},
            {"ticker": "ZZZ", "valid": True, "return_24h_pct": "8.0"},
        ]
        short = [
            {"ticker": "BTC", "valid": True, "return_24h_pct": "-4.0"},
            {"ticker": "ETH", "valid": True, "return_24h_pct": "-8.0"},
        ]
        result = build_provisional_leaders(long, short)
        self.assertEqual(result["provisional_long_leader"]["ticker"], "ZZZ")
        self.assertEqual(result["provisional_short_leader"]["ticker"], "ETH")


if __name__ == "__main__":
    unittest.main()
