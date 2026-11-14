import unittest
from decimal import Decimal as D
from unittest.mock import patch

from scripts import run_bitget_paper
from bitget_stocks_vs_crypto.risk import analyse_intraday_risk, expected_minute_starts


def bar(ts, open_price, high, low, close):
    return {
        "timestamp_ms": ts, "open": str(open_price), "high": str(high),
        "low": str(low), "close": str(close),
    }


class MinuteRiskTests(unittest.TestCase):
    def _inputs(self):
        start = 1_800_000_000_000
        timestamps = [start + i * 60_000 for i in range(3)]
        long_market = [
            bar(timestamps[0], 100, 102, 99, 100),
            bar(timestamps[1], 100, 103, 95, 97),
            bar(timestamps[2], 97, 101, 96, 100),
        ]
        short_market = [
            bar(timestamps[0], 10, 10.5, 9.8, 10),
            bar(timestamps[1], 10, 12, 9, 11),
            bar(timestamps[2], 11, 11.5, 9.2, 10),
        ]
        return {
            "start_ms": start,
            "end_ms": start + 3 * 60_000,
            "long_position": {"symbol": "STOCKUSDT", "qty": "10", "entry_price": "100"},
            "short_position": {"symbol": "CRYPTOUSDT", "qty": "100", "entry_price": "10"},
            "starting_cash": D("10000"),
            "previous_nav": D("10000"),
            "funding_cashflows": [],
            "market_candles": {"STOCKUSDT": long_market, "CRYPTOUSDT": short_market},
            "mark_candles": {"STOCKUSDT": long_market, "CRYPTOUSDT": short_market},
            "prior_peak_nav": D("10000"),
            "prior_peak_time_ms": start,
            "prior_mdd_pct": D("0"),
            "prior_mdd_peak_nav": None,
            "prior_mdd_trough_nav": None,
            "prior_mdd_peak_time_ms": None,
            "prior_mdd_trough_time_ms": None,
        }

    def test_complete_synchronised_candles_compute_spread_and_strategy_mdd(self):
        result = analyse_intraday_risk(**self._inputs())
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["expected_minutes"], 3)
        self.assertEqual(result["matched_minutes"], 3)
        self.assertEqual(result["coverage_pct"], "100")
        self.assertLess(D(result["spread_low_ratio"]), D(result["spread_high_ratio"]))
        self.assertLess(D(result["spread_drawdown_pct"]), D("0"))
        self.assertLess(D(result["daily_mdd_pct"]), D("0"))
        self.assertLess(D(result["strategy_mdd_pct"]), D("0"))
        self.assertEqual(len(result["minute_rows"]), 3)
        self.assertIsNotNone(result["nav_low_time_ms"])

    def test_missing_one_symbol_minute_does_not_update_strategy_mdd(self):
        args = self._inputs()
        args["mark_candles"]["CRYPTOUSDT"] = args["mark_candles"]["CRYPTOUSDT"][:1] + args["mark_candles"]["CRYPTOUSDT"][2:]
        result = analyse_intraday_risk(**args)
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertEqual(result["matched_minutes"], 2)
        self.assertEqual(result["missing_minutes"], 1)
        self.assertLess(D(result["spread_drawdown_pct"]), D("0"))
        self.assertEqual(result["strategy_mdd_pct"], "0")
        self.assertEqual(result["strategy_peak_nav"], "10000")

    def test_funding_cashflow_is_reflected_from_its_minute_onward(self):
        args = self._inputs()
        baseline = analyse_intraday_risk(**args)
        args["funding_cashflows"] = [{"time_ms": args["start_ms"] + 60_000, "payment": "-5"}]
        funded = analyse_intraday_risk(**args)
        self.assertLess(D(funded["nav_low"]), D(baseline["nav_low"]))

    def test_partial_start_and_end_minutes_are_excluded(self):
        start = 1_800_000_000_000 + 17
        end = 1_800_000_000_000 + 180_000 + 17
        self.assertEqual(
            expected_minute_starts(start, end),
            [1_800_000_060_000, 1_800_000_120_000],
        )

    def test_runner_paginates_1001_one_minute_bars_using_public_v3_endpoint(self):
        start = 1_800_000_000_000
        count = 1001
        calls = []

        def fake_get_json(path, params=None, stage="api"):
            self.assertEqual(path, "/api/v3/market/candles")
            calls.append(dict(params))
            first = int(params["startTime"])
            last = int(params["endTime"])
            rows = []
            for ts in range(first, last + 1, 60_000):
                if ts < start or ts >= start + count * 60_000:
                    continue
                rows.append([str(ts), "100", "101", "99", "100"])
            return {"data": rows}, {"http_status": 200}

        with patch.object(run_bitget_paper, "get_json", side_effect=fake_get_json):
            rows = run_bitget_paper.fetch_1m_candles(
                "STOCKUSDT", start, start + count * 60_000, "market"
            )
        self.assertEqual(len(rows), count)
        self.assertEqual(len(calls), 2)
        self.assertEqual([int(x["limit"]) for x in calls], [1000, 1000])
        self.assertEqual(calls[0]["category"], "USDT-FUTURES")
        self.assertEqual(calls[0]["interval"], "1m")
        self.assertEqual(calls[0]["type"], "market")
        self.assertEqual(rows[0]["timestamp_ms"], start)
        self.assertEqual(rows[-1]["timestamp_ms"], start + (count - 1) * 60_000)

    def test_bad_ohlc_relationship_fails_closed(self):
        args = self._inputs()
        args["market_candles"]["STOCKUSDT"][0]["high"] = "98"
        with self.assertRaises(ValueError):
            analyse_intraday_risk(**args)


if __name__ == "__main__":
    unittest.main()
