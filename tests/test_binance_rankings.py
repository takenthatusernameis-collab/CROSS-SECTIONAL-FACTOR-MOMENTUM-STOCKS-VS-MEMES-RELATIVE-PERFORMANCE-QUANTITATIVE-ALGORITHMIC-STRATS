import unittest
from decimal import Decimal

from binance_stocks_vs_crypto.rankings import (
    build_provisional_leaders,
    classify_binance_universe,
    parse_binance_24h_tickers,
)


def contract(symbol, contract_type, underlying_type, *, status="TRADING", quote="USDT", margin="USDT"):
    return {"symbol": symbol, "pair": symbol, "contractType": contract_type,
            "underlyingType": underlying_type, "status": status,
            "quoteAsset": quote, "marginAsset": margin}


def ticker(symbol, percent, *, as_of=2_000_000_000_000, count=250,
           last="110", open_price="100", close_offset_ms=0):
    return {
        "symbol": symbol,
        "priceChange": "10",
        "priceChangePercent": str(percent),
        "weightedAvgPrice": "105",
        "lastPrice": last,
        "openPrice": open_price,
        "highPrice": "115",
        "lowPrice": "95",
        "volume": "500",
        "quoteVolume": "50000",
        "openTime": as_of - 86_400_000,
        "closeTime": as_of - close_offset_ms,
        "firstId": 100,
        "lastId": 349,
        "count": count,
    }


class UniverseTests(unittest.TestCase):
    def test_exact_metadata_separates_stock_index_and_crypto(self):
        result = classify_binance_universe([
            contract("AAPLUSDT", "TRADIFI_PERPETUAL", "EQUITY"),
            contract("SPYUSDT", "TRADIFI_PERPETUAL", "INDEX"),
            contract("BTCUSDT", "PERPETUAL", "COIN"),
            contract("XAUUSDT", "TRADIFI_PERPETUAL", "COMMODITY"),
        ])
        self.assertEqual([x["symbol"] for x in result["long"]], ["AAPLUSDT", "SPYUSDT"])
        self.assertEqual([x["symbol"] for x in result["short"]], ["BTCUSDT"])
        self.assertEqual([x["symbol"] for x in result["excluded"]], ["XAUUSDT"])

    def test_unknown_and_non_trading_contracts_are_excluded(self):
        result = classify_binance_universe([
            contract("TSLAUSDT", "TRADIFI_PERPETUAL", "UNCLASSIFIED"),
            contract("BTCUSDT", "PERPETUAL", "COIN", status="BREAK"),
            contract("ETHUSDC", "PERPETUAL", "COIN", quote="USDC", margin="USDC"),
        ])
        self.assertEqual(result["long"], [])
        self.assertEqual(result["short"], [])
        self.assertEqual(len(result["excluded"]), 3)

    def test_duplicate_or_missing_symbol_fails_closed(self):
        with self.assertRaises(ValueError):
            classify_binance_universe([
                contract("DUP", "PERPETUAL", "COIN"),
                contract("DUP", "PERPETUAL", "COIN"),
            ])
        with self.assertRaises(ValueError):
            classify_binance_universe([{"contractType": "PERPETUAL"}])


class TickerTests(unittest.TestCase):
    def setUp(self):
        self.as_of = 2_000_000_000_000

    def test_uses_binance_reported_percent_without_recomputing(self):
        # lastPrice/openPrice imply +10%; the official report says +7.25%.
        parsed = parse_binance_24h_tickers(
            [ticker("AAPLUSDT", "7.25", as_of=self.as_of)], as_of_ms=self.as_of
        )
        result = parsed["AAPLUSDT"]
        self.assertTrue(result["valid"])
        self.assertEqual(Decimal(result["return_24h_pct"]), Decimal("7.25"))
        self.assertEqual(result["last_price"], "110")
        self.assertEqual(result["trade_count"], 250)
        self.assertFalse(result["last_trade_timestamp_available"])

    def test_zero_trade_ticker_is_invalid(self):
        result = parse_binance_24h_tickers(
            [ticker("QUIETUSDT", "0", as_of=self.as_of, count=0)],
            as_of_ms=self.as_of,
        )["QUIETUSDT"]
        self.assertFalse(result["valid"])
        self.assertIn("no_valid_trades_in_24h_window", result["reason"])

    def test_stale_ticker_window_is_invalid(self):
        result = parse_binance_24h_tickers(
            [ticker("OLDUSDT", "1", as_of=self.as_of, close_offset_ms=901_000)],
            as_of_ms=self.as_of,
        )["OLDUSDT"]
        self.assertFalse(result["valid"])
        self.assertIn("stale_ticker_window", result["reason"])

    def test_invalid_percent_and_prices_are_rejected(self):
        row = ticker("BADUSDT", "NaN", as_of=self.as_of, last="0", open_price="-1")
        result = parse_binance_24h_tickers([row], as_of_ms=self.as_of)["BADUSDT"]
        self.assertFalse(result["valid"])
        self.assertIn("invalid_price_change_percent", result["reason"])
        self.assertIn("invalid_last_price", result["reason"])
        self.assertIn("invalid_open_price", result["reason"])

    def test_duplicate_symbols_in_ticker_report_fail_closed(self):
        with self.assertRaises(ValueError):
            parse_binance_24h_tickers([
                ticker("DUPUSDT", "1", as_of=self.as_of),
                ticker("DUPUSDT", "2", as_of=self.as_of),
            ], as_of_ms=self.as_of)

    def test_non_array_or_malformed_report_fails_closed(self):
        with self.assertRaises(ValueError):
            parse_binance_24h_tickers({}, as_of_ms=self.as_of)
        with self.assertRaises(ValueError):
            parse_binance_24h_tickers([{"priceChangePercent": "1"}], as_of_ms=self.as_of)

    def test_provisional_leaders_select_binance_report_extremes(self):
        parsed = parse_binance_24h_tickers([
            ticker("STOCK1", "5", as_of=self.as_of),
            ticker("STOCK2", "8", as_of=self.as_of),
            ticker("BTC", "-4", as_of=self.as_of),
            ticker("ETH", "-8", as_of=self.as_of),
            ticker("BAD", "-99", as_of=self.as_of, count=0),
        ], as_of_ms=self.as_of)
        leaders = build_provisional_leaders(
            [
                {**parsed["STOCK1"], "underlying_type": "EQUITY"},
                {**parsed["STOCK2"], "underlying_type": "INDEX"},
            ],
            [
                {**parsed["BTC"], "underlying_type": "COIN"},
                {**parsed["ETH"], "underlying_type": "COIN"},
                {**parsed["BAD"], "underlying_type": "COIN"},
            ],
        )
        self.assertEqual(leaders["provisional_long_leader"]["symbol"], "STOCK2")
        self.assertEqual(leaders["provisional_short_leader"]["symbol"], "ETH")


if __name__ == "__main__":
    unittest.main()
