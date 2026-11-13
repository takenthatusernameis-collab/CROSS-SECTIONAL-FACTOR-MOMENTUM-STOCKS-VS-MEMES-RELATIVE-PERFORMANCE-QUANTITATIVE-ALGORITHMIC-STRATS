import unittest

from binance_stocks_vs_crypto.preflight import (
    ALLOWED_ROUTES,
    request_json,
    summarise_24h_ticker,
    summarise_orderbook,
    summarise_premium_index,
)


class PublicEndpointGuardTests(unittest.TestCase):
    def test_only_public_market_data_routes_are_allowlisted(self):
        self.assertIn("exchangeInfo", ALLOWED_ROUTES)
        self.assertIn("ticker/24hr", ALLOWED_ROUTES)
        self.assertIn("fundingRate", ALLOWED_ROUTES)
        self.assertNotIn("continuousKlines", ALLOWED_ROUTES)
        self.assertNotIn("order", ALLOWED_ROUTES)
        self.assertNotIn("account", ALLOWED_ROUTES)

    def test_request_rejects_non_allowlisted_route_before_network(self):
        with self.assertRaises(ValueError):
            request_json("order", {"symbol": "BTCUSDT"})
        with self.assertRaises(ValueError):
            request_json("../order")


class ResponseSchemaTests(unittest.TestCase):
    def sample_ticker(self):
        now = 2_000_000_000_000
        return {
            "symbol": "1000PEPEUSDT",
            "priceChange": "10",
            "priceChangePercent": "10",
            "lastPrice": "110",
            "openPrice": "100",
            "openTime": now - 86_400_000,
            "closeTime": now,
            "count": 250,
        }

    def test_ticker_summary_reads_binance_percent_and_trade_count(self):
        row = self.sample_ticker()
        result = summarise_24h_ticker(row, as_of_ms=row["closeTime"])
        self.assertTrue(result["schema_valid"])
        self.assertTrue(result["data_valid"])
        self.assertEqual(result["price_change_percent"], "10")
        self.assertEqual(result["trade_count"], 250)

    def test_ticker_summary_rejects_missing_required_fields(self):
        result = summarise_24h_ticker({"symbol": "1000PEPEUSDT"})
        self.assertFalse(result["schema_valid"])

    def test_orderbook_summary_reads_bids_and_asks(self):
        result = summarise_orderbook({
            "lastUpdateId": 10, "bids": [["10", "2"]], "asks": [["11", "3"]]
        })
        self.assertTrue(result["schema_valid"])
        self.assertEqual(result["bid_level_count"], 1)
        self.assertEqual(result["ask_level_count"], 1)

    def test_orderbook_summary_rejects_bad_shape(self):
        self.assertFalse(summarise_orderbook({"bids": [], "asks": None})["schema_valid"])

    def test_premium_index_summary_extracts_mark_index_and_funding(self):
        payload = {
            "symbol": "1000PEPEUSDT", "markPrice": "100", "indexPrice": "99",
            "lastFundingRate": "0.0001", "nextFundingTime": 123456,
        }
        result = summarise_premium_index(payload)
        self.assertTrue(result["schema_valid"])
        self.assertEqual(result["symbol"], "1000PEPEUSDT")
        self.assertEqual(result["last_funding_rate"], "0.0001")

    def test_premium_index_summary_rejects_missing_fields(self):
        self.assertFalse(summarise_premium_index({"symbol": "1000PEPEUSDT"}))


if __name__ == "__main__":
    unittest.main()
