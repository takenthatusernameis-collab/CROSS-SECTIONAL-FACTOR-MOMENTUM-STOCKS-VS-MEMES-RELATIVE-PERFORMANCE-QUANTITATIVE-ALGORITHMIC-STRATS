import unittest
from unittest.mock import patch

from bitget_stocks_vs_crypto import market


class BitgetRankingTests(unittest.TestCase):
    def _rows(self):
        instruments = [
            {"symbol":"STOCKUSDT","category":"USDT-FUTURES","quoteCoin":"USDT","status":"online","type":"perpetual","symbolType":"stock","baseCoin":"STOCK","minOrderQty":"0.01","quantityMultiplier":"0.01","quantityPrecision":"2","takerFeeRate":"0.0006"},
            {"symbol":"CRYPTOUSDT","category":"USDT-FUTURES","quoteCoin":"USDT","status":"online","type":"perpetual","symbolType":"crypto","baseCoin":"CRYPTO","minOrderQty":"0.1","quantityMultiplier":"0.1","quantityPrecision":"1","takerFeeRate":"0.0006"},
        ]
        return instruments

    def _mock_get(self, stale=False):
        now = 1_800_000_000_000
        def fake(path, params=None, stage="api"):
            if path.endswith("/time"):
                return {"data":{"serverTime":str(now)}}, {"http_status":200,"elapsed_seconds":0.01}
            if path.endswith("/instruments"):
                return {"data":self._rows()}, {"http_status":200,"elapsed_seconds":0.01}
            if path.endswith("/tickers"):
                age = 1_000_000 if stale else 1000
                return {"data":[
                    {"symbol":"STOCKUSDT","category":"USDT-FUTURES","price24hPcnt":"0.125","lastPrice":"100","bid1Price":"99.9","ask1Price":"100.1","markPrice":"100","ts":str(now-age)},
                    {"symbol":"CRYPTOUSDT","category":"USDT-FUTURES","price24hPcnt":"-0.25","lastPrice":"10","bid1Price":"9.9","ask1Price":"10.1","markPrice":"10","ts":str(now-age)},
                ]}, {"http_status":200,"elapsed_seconds":0.01}
            raise AssertionError(path)
        return fake

    def test_ranks_bitget_source_percentage_without_recomputing(self):
        with patch.object(market, "get_json", side_effect=self._mock_get()):
            result = market.build_ranking_report()
        self.assertEqual(result["gates"]["ranking"], "PASS")
        self.assertEqual(result["leaders"]["long_stock_top_gainer"]["symbol"], "STOCKUSDT")
        self.assertEqual(result["leaders"]["short_crypto_bottom_loser"]["symbol"], "CRYPTOUSDT")
        self.assertEqual(result["leaders"]["long_stock_top_gainer"]["return_24h_pct"], "12.500")
        self.assertEqual(result["leaders"]["short_crypto_bottom_loser"]["return_24h_pct"], "-25.00")

    def test_stale_ticker_blocks_complete_universe_ranking(self):
        with patch.object(market, "get_json", side_effect=self._mock_get(stale=True)):
            result = market.build_ranking_report()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["gates"]["ticker_coverage"], "FAIL")


if __name__ == "__main__":
    unittest.main()
