import unittest

from kalshi_stocks_vs_crypto.preflight import (
    analyse_market_list,
    choose_sample_markets,
    summarise_candlesticks,
    summarise_orderbook,
    schedule_query_params,
)


class MarketSchemaTests(unittest.TestCase):
    def test_market_list_reports_raw_classes_without_classifying(self):
        payload = {
            "markets": [
                {"ticker": "STOCK-A", "asset_class": "Equities", "product_metadata": {"security_type": "common_stock"}},
                {"ticker": "CRYPTO-B", "asset_class": "Crypto", "product_metadata": {"underlying": "BTC"}},
                {"ticker": "INDEX-C", "asset_class": "Equities", "product_metadata": {}},
            ]
        }
        result = analyse_market_list(payload)
        self.assertEqual(result["market_count"], 3)
        self.assertEqual(result["asset_class_counts"], {"Crypto": 1, "Equities": 2})
        self.assertIn("product_metadata.security_type", result["product_metadata_field_shapes"])
        self.assertIn("ticker", result["market_field_union"])
        self.assertNotIn("classification", result)

    def test_market_list_rejects_missing_array(self):
        with self.assertRaises(ValueError):
            analyse_market_list({"data": []})

    def test_market_sample_has_one_per_raw_class_then_fills_up_to_limit(self):
        markets = [
            {"ticker": "Z", "asset_class": "Equities"},
            {"ticker": "B", "asset_class": "Crypto"},
            {"ticker": "A", "asset_class": "Equities"},
            {"ticker": "C", "asset_class": "Metals"},
        ]
        result = choose_sample_markets(markets, limit=3)
        self.assertEqual([m["ticker"] for m in result], ["A", "B", "C"])
        self.assertEqual(len({m["asset_class"] for m in result}), 3)

    def test_sample_fills_second_ticker_when_only_one_class(self):
        markets = [{"ticker": "A", "asset_class": "Crypto"}, {"ticker": "B", "asset_class": "Crypto"}]
        self.assertEqual([m["ticker"] for m in choose_sample_markets(markets, limit=3)], ["A", "B"])


class ResponseSummaryTests(unittest.TestCase):
    def test_schedule_query_requires_and_joins_tickers(self):
        self.assertEqual(
            schedule_query_params([{"ticker": "ABC"}, {"ticker": "XYZ"}]),
            {"tickers": "ABC,XYZ"},
        )
        self.assertIsNone(schedule_query_params([]))

    def test_candlestick_summary_counts_null_and_valid_trade_closes(self):
        payload = {
            "ticker": "ABC",
            "candlesticks": [
                {"end_period_ts": 100, "price": {"open": None, "close": None}},
                {"end_period_ts": 160, "price": {"open": "12.0", "close": "13.0"}},
            ],
        }
        result = summarise_candlesticks(payload)
        self.assertTrue(result["schema_valid"])
        self.assertEqual(result["candlestick_count"], 2)
        self.assertEqual(result["null_trade_price_closes"], 1)
        self.assertEqual(result["valid_trade_price_closes"], 1)
        self.assertEqual(result["last_end_period_ts"], 160)

    def test_candlestick_summary_fails_closed_without_array(self):
        self.assertFalse(summarise_candlesticks({})["schema_valid"])

    def test_orderbook_summary_reads_bids_and_asks(self):
        result = summarise_orderbook({"orderbook": {"bids": [["1", "2"]], "asks": [["3", "4"], ["5", "6"]]}})
        self.assertTrue(result["schema_valid"])
        self.assertEqual(result["bid_level_count"], 1)
        self.assertEqual(result["ask_level_count"], 2)

    def test_orderbook_summary_fails_closed_without_orderbook(self):
        self.assertFalse(summarise_orderbook({})["schema_valid"])


if __name__ == "__main__":
    unittest.main()
