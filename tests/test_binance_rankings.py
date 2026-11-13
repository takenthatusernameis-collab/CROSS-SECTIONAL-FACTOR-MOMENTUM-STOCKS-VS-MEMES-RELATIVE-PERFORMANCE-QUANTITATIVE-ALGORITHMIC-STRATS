import unittest
from decimal import Decimal

from binance_stocks_vs_crypto.rankings import (
    build_provisional_leaders,
    calculate_trailing_return,
    classify_binance_universe,
    valid_candle_closes,
)


def contract(symbol, contract_type, underlying_type, *, status="TRADING", quote="USDT", margin="USDT"):
    return {"symbol": symbol, "pair": symbol, "contractType": contract_type,
            "underlyingType": underlying_type, "status": status,
            "quoteAsset": quote, "marginAsset": margin}


def candle(close_time_ms, close, trades=1):
    return [close_time_ms - 59_999, str(close), str(close), str(close), str(close),
            "1", close_time_ms, "1", trades, "0.5", "0.5", "0"]


class UniverseTests(unittest.TestCase):
    def test_exact_metadata_separates_stock_index_and_crypto(self):
        symbols = [
            contract("AAPLUSDT", "TRADIFI_PERPETUAL", "EQUITY"),
            contract("SPYUSDT", "TRADIFI_PERPETUAL", "INDEX"),
            contract("BTCUSDT", "PERPETUAL", "COIN"),
            contract("XAUUSDT", "TRADIFI_PERPETUAL", "COMMODITY"),
        ]
        result = classify_binance_universe(symbols)
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
            classify_binance_universe([contract("DUP", "PERPETUAL", "COIN"),
                                       contract("DUP", "PERPETUAL", "COIN")])
        with self.assertRaises(ValueError):
            classify_binance_universe([{"contractType": "PERPETUAL"}])


class ReturnTests(unittest.TestCase):
    def setUp(self):
        self.as_of = 200_099_000
        self.current = (self.as_of // 60_000) * 60_000 - 1
        self.reference = self.current - 86_400_000 - 60_000

    def test_calculates_return_from_completed_traded_closes(self):
        result = calculate_trailing_return([
            candle(self.reference, "100"),
            candle(self.current, "110"),
        ], self.as_of)
        self.assertTrue(result["valid"])
        self.assertEqual(Decimal(result["return_24h_pct"]), Decimal("10.00000000"))

    def test_zero_trade_and_forming_candles_are_ignored(self):
        rows = [candle(self.reference, "100"), candle(self.current, "110"),
                candle(self.current + 60_000, "999"), candle(self.current - 60_000, "888", 0)]
        result = calculate_trailing_return(rows, self.as_of)
        self.assertTrue(result["valid"])
        self.assertEqual(result["current_price"], "110")
        self.assertEqual(result["valid_close_count"], 2)

    def test_stale_current_price_is_rejected(self):
        result = calculate_trailing_return([
            candle(self.reference, "100"), candle(self.current - 1_200_000, "110")
        ], self.as_of)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "stale_current_price")

    def test_missing_24h_reference_is_rejected(self):
        result = calculate_trailing_return([candle(self.current, "110")], self.as_of)
        self.assertFalse(result["valid"])

    def test_valid_parser_ignores_empty_and_invalid_rows(self):
        result = valid_candle_closes([
            candle(self.current, "110"), candle(self.current - 60_000, "0", 1),
            candle(self.current - 120_000, "100", 0), ["bad"],
        ])
        self.assertEqual(result, [(self.current, Decimal("110"))])

    def test_provisional_leaders_select_extremes_from_valid_rows(self):
        leaders = build_provisional_leaders(
            [{"symbol": "AAA", "valid": True, "return_24h_pct": "5"},
             {"symbol": "BBB", "valid": True, "return_24h_pct": "8"}],
            [{"symbol": "BTC", "valid": True, "return_24h_pct": "-4"},
             {"symbol": "ETH", "valid": True, "return_24h_pct": "-8"},
             {"symbol": "BAD", "valid": False, "return_24h_pct": "-99"}],
        )
        self.assertEqual(leaders["provisional_long_leader"]["symbol"], "BBB")
        self.assertEqual(leaders["provisional_short_leader"]["symbol"], "ETH")


if __name__ == "__main__":
    unittest.main()
