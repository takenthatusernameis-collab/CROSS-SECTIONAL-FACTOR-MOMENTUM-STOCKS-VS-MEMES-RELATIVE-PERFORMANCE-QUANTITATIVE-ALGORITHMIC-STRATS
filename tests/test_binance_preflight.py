import unittest

from binance_stocks_vs_crypto.preflight import (
    ALLOWED_ROUTES,
    request_json,
    summarise_klines,
    summarise_orderbook,
    summarise_premium_index,
)


class PublicEndpointGuardTests(unittest.TestCase):
    def test_only_public_market_data_routes_are_allowlisted(self):
        self.assertIn("exchangeInfo", ALLOWED_ROUTES)
        self.assertIn("continuousKlines", ALLOWED_ROUTES)
        self.assertNotIn("order", ALLOWED_ROUTES)
        self.assertNotIn("account", ALLOWED_ROUTES)

    def test_request_rejects_any_non_allowlisted_route_before_network(self):
        with self.assertRaises(ValueError):
            request_json("order", {"symbol": "BTCUSDT"})
        with self.assertRaises(ValueError):
            request_json("../order")


class ResponseSchemaTests(unittest.TestCase):
    def test_kline_summary_validates_binance_12_column_rows(self):
        row = [1000, "10", "11", "9", "10.5", "12", 59999, "125", 4, "5", "52", "0"]
        result = summarise_klines([row])
        self.assertTrue(result["schema_valid"])
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["sample_close"], "10.5")
        self.assertEqual(result["sample_trade_count"], 4)

    def test_kline_summary_rejects_non_array_or_malformed_rows(self):
        self.assertFalse(summarise_klines({})["schema_valid"])
        self.assertFalse(summarise_klines([[1, 2, 3]])["schema_valid"])

    def test_orderbook_summary_reads_bids_and_asks(self):
        result = summarise_orderbook({"lastUpdateId": 10, "bids": [["10", "2"]], "asks": [["11", "3"]]})
        self.assertTrue(result["schema_valid"])
        self.assertEqual(result["bid_level_count"], 1)
        self.assertEqual(result["ask_level_count"], 1)

    def test_orderbook_summary_rejects_bad_shape(self):
        self.assertFalse(summarise_orderbook({"bids": [], "asks": None})["schema_valid"])

    def test_premium_index_summary_extracts_mark_index_and_funding(self):
        payload = {"symbol": "BTCUSDT", "markPrice": "100", "indexPrice": "99",
                   "lastFundingRate": "0.0001", "nextFundingTime": 123456}
        result = summarise_premium_index(payload)
        self.assertTrue(result["schema_valid"])
        self.assertEqual(result["symbol"], "BTCUSDT")
        self.assertEqual(result["last_funding_rate"], "0.0001")

    def test_premium_index_summary_rejects_missing_fields(self):
        self.assertFalse(summarise_premium_index({"symbol": "BTCUSDT"})["schema_valid"])


if __name__ == "__main__":
    unittest.main()
