from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, Mock
import json

from stocks_vs_memes.bybit import BybitClient
from stocks_vs_memes.coinmarketcap import CoinMarketCapClient
from stocks_vs_memes.daily_trader import BybitDailyTrader
from stocks_vs_memes.spread import build_ratio_candle, build_universes, cumulative_ratio_metrics, RatioCandle
from stocks_vs_memes.store import PaperStore


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self): return self.payload


class BybitPublicClientTests(unittest.TestCase):
    def test_market_data_uses_public_get_and_no_auth(self):
        captured = {}
        payload = {"retCode": 0, "retMsg": "OK", "result": {"list": []}}
        def fake_urlopen(request, timeout):
            captured.update(method=request.get_method(), url=request.full_url, headers=dict(request.header_items()))
            return FakeResponse(payload)
        client = BybitClient(max_retries=0)
        with patch("stocks_vs_memes.bybit.urlopen", side_effect=fake_urlopen):
            client.tickers()
        self.assertEqual(captured["method"], "GET")
        self.assertIn("/v5/market/tickers?category=linear", captured["url"])
        self.assertFalse(any(k.lower() in {"x-bapi-api-key", "api-key", "authorization"} for k in captured["headers"]))

    def test_cmc_categories_uses_keyless_public_prefix_without_auth(self):
        captured = {}
        def fake_urlopen(request, timeout):
            captured.update(method=request.get_method(), url=request.full_url, headers=dict(request.header_items()))
            return FakeResponse({"data": [{"id": "99", "name": "Memes"}], "status": {"error_code": 0}})
        client = CoinMarketCapClient(max_retries=0)
        with patch("stocks_vs_memes.coinmarketcap.urlopen", side_effect=fake_urlopen):
            self.assertEqual(client.categories()[0]["name"], "Memes")
        self.assertIn("/public-api/v1/cryptocurrency/categories", captured["url"])
        self.assertNotIn("X-cmc_pro_api_key", {k.lower() for k in captured["headers"]})

    def test_cmc_symbol_map_requests_all_active_matches_without_api_key(self):
        captured = {}
        def fake_urlopen(request, timeout):
            captured.update(url=request.full_url, headers=dict(request.header_items()), method=request.get_method())
            return FakeResponse({"data": [{"id": 1, "symbol": "DOGE"}], "status": {"error_code": 0}})
        client = CoinMarketCapClient(max_retries=0)
        with patch("stocks_vs_memes.coinmarketcap.urlopen", side_effect=fake_urlopen):
            rows=client.cryptocurrency_map_for_symbols(["DOGE"])
        self.assertEqual(rows[0]["id"],1)
        self.assertIn("listing_status=active",captured["url"])
        self.assertIn("limit=5000",captured["url"])
        self.assertEqual(captured["method"],"GET")
        self.assertNotIn("X-cmc_pro_api_key",{k.lower() for k in captured["headers"]})

    def test_cmc_category_members_paginate_using_num_tokens(self):
        captured = []
        first_page = [{"id": i, "symbol": f"X{i}"} for i in range(1000)]
        second_page = [{"id": 1001, "symbol": "LAST"}]
        def fake_urlopen(request, timeout):
            from urllib.parse import urlparse, parse_qs
            params = parse_qs(urlparse(request.full_url).query)
            start = int(params["start"][0]); captured.append(start)
            if start == 1:
                return FakeResponse({"data": {"id": "123", "name": "Memes", "num_tokens": 1001, "coins": first_page}, "status": {"error_code": 0}})
            return FakeResponse({"data": {"id": "123", "name": "Memes", "num_tokens": 1001, "coins": second_page}, "status": {"error_code": 0}})
        client = CoinMarketCapClient(max_retries=0)
        with patch("stocks_vs_memes.coinmarketcap.urlopen", side_effect=fake_urlopen):
            members, meta = client.category_members("123")
        self.assertEqual(captured, [1, 1001]); self.assertEqual(len(members), 1001)
        self.assertEqual(meta["num_tokens"], 1001)

    def test_cmc_category_members_do_not_stop_without_total_count(self):
        captured = []
        first_page = [{"id": i, "symbol": f"X{i}"} for i in range(1000)]
        def fake_urlopen(request, timeout):
            from urllib.parse import urlparse, parse_qs
            start = int(parse_qs(urlparse(request.full_url).query)["start"][0]); captured.append(start)
            coins = first_page if start == 1 else []
            return FakeResponse({"data": {"id": "123", "name": "Memes", "coins": coins}, "status": {"error_code": 0}})
        client = CoinMarketCapClient(max_retries=0)
        with patch("stocks_vs_memes.coinmarketcap.urlopen", side_effect=fake_urlopen):
            members, _ = client.category_members("123")
        self.assertEqual(captured, [1, 1001]); self.assertEqual(len(members), 1000)


class FakeBybit:
    base_url = "https://api.bybit.com"
    def __init__(self):
        self.instrument_calls = 0; self.ticker_calls = 0; self.book_calls = 0
        self.instruments_data = [
            self.instrument("NVDAUSDT", "NVDA", "stock"), self.instrument("TSLAUSDT", "TSLA", "stock"),
            self.instrument("DOGEUSDT", "DOGE", "innovation"), self.instrument("PEPEUSDT", "PEPE", "innovation")]
        self.tickers_data = [
            {"symbol": "NVDAUSDT", "price24hPcnt": "0.10", "turnover24h": "9000000"},
            {"symbol": "TSLAUSDT", "price24hPcnt": "0.05", "turnover24h": "7000000"},
            {"symbol": "DOGEUSDT", "price24hPcnt": "-0.20", "turnover24h": "8000000"},
            {"symbol": "PEPEUSDT", "price24hPcnt": "-0.11", "turnover24h": "6000000"}]
    @staticmethod
    def instrument(symbol, base, typ):
        return {"symbol": symbol, "baseCoin": base, "quoteCoin": "USDT", "settleCoin": "USDT",
                "contractType": "LinearPerpetual", "status": "Trading", "symbolType": typ,
                "lotSizeFilter": {"minOrderQty": "0.1", "qtyStep": "0.1", "minNotionalValue": "5", "maxMktOrderQty": "100000"}}
    def instruments(self): self.instrument_calls += 1; return list(self.instruments_data)
    def tickers(self): self.ticker_calls += 1; return list(self.tickers_data)
    def order_book(self, symbol, limit=100):
        self.book_calls += 1
        mids = {"NVDAUSDT": 100.0, "TSLAUSDT": 50.0, "DOGEUSDT": 10.0, "PEPEUSDT": 0.01}
        mid = mids[symbol]
        return {"bids": [{"p": str(mid * .999), "s": "100000000"}],
                "asks": [{"p": str(mid * 1.001), "s": "100000000"}]}
    def mark_candles(self, symbol, start_ts, end_ts):
        data=[]
        for i in range(1440):
            ts=start_ts+i*60
            if ts>=end_ts: break
            if symbol == "NVDAUSDT":
                close=100.0 + 10.0*i/1439; op=100.0 if i==0 else close-0.01
            elif symbol == "DOGEUSDT":
                close=(10.0 + i/60.0) if i<=60 else (11.0 - 3.0*(i-60)/1379.0)
                op=10.0 if i==0 else close+0.01
            elif symbol == "TSLAUSDT":
                close=50.0 + 2.0*i/1439; op=50.0 if i==0 else close-0.01
            else:
                close=0.01 - 0.002*i/1439; op=0.01 if i==0 else close+0.00001
            data.append({"t":ts,"o":str(op),"h":str(max(op,close)),"l":str(min(op,close)),"c":str(close)})
        data.append({"t":end_ts,"o":"999999" if symbol == "NVDAUSDT" else "0.000001",
                     "h":"1000000","l":"0.000001","c":"999999" if symbol == "NVDAUSDT" else "0.000001"})
        return data
    def funding_rates(self, symbol, start_ts, end_ts):
        return [{"t": start_ts+8*3600, "r": "0.0001"}]


class FakeCMC:
    base_url = "https://pro-api.coinmarketcap.com/public-api"
    def __init__(self):
        self.category_name = "Memes"
        self.members = [{"id": 1, "symbol": "DOGE", "name": "Dogecoin"},
                        {"id": 2, "symbol": "PEPE", "name": "Pepe"}]
        self.map_rows = [{"id": 1, "symbol": "DOGE", "name": "Dogecoin"},
                         {"id": 2, "symbol": "PEPE", "name": "Pepe"},
                         {"id": 3, "symbol": "NVDA", "name": "NVIDIA"},
                         {"id": 4, "symbol": "TSLA", "name": "Tesla"}]
    def find_exact_category(self, exact_name):
        if exact_name.casefold() != self.category_name.casefold(): raise RuntimeError("no exact category")
        return {"id": "123", "name": "Memes", "title": "Memes"}
    def category_members(self, category_id): return list(self.members), {"id":category_id,"name":"Memes"}
    def cryptocurrency_map_for_symbols(self, symbols):
        wanted={x.upper() for x in symbols}
        return [x for x in self.map_rows if x["symbol"] in wanted]


class BybitDailyWorkflowTests(unittest.TestCase):
    def setup_trader(self, temp, bybit=None, cmc=None):
        bybit=bybit or FakeBybit(); cmc=cmc or FakeCMC()
        config={"initial_equity_usdt":10000,"gross_leverage":2.0,"taker_fee_rate":0.00055,
                "orderbook_levels":100,"entry_grace_seconds":60,"minimum_mark_coverage":1.0,
                "data_dir":str(Path(temp)/"data"),"reports_dir":str(Path(temp)/"reports"),"cmc_meme_category_name":"Memes"}
        store=PaperStore(Path(temp)/"data"/"paper.sqlite3")
        return BybitDailyTrader(config,bybit_client=bybit,cmc_client=cmc,store=store),store,bybit

    def test_preflight_requires_both_live_sources_and_ticker_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            trader,store,_=self.setup_trader(temp); result=trader.live_preflight()
            self.assertTrue(result["passed"]); self.assertTrue(result["feeds_ready"])
            self.assertFalse(result["paper_trading_authorized"])
            self.assertEqual(result["stock_contract_count"],2); self.assertEqual(result["meme_contract_count"],2)
            self.assertFalse(result["strategy_ranking_performed"])
            self.assertNotIn("current_stock_leader",result); self.assertNotIn("current_meme_laggard",result)
            store.close()

    def test_ambiguous_global_cmc_ticker_mapping_is_excluded(self):
        bybit=FakeBybit(); cmc=FakeCMC(); cmc.map_rows.append({"id":99,"symbol":"DOGE","name":"Different DOGE token"})
        stocks,memes,diagnostics=build_universes(bybit.instruments_data,bybit.tickers_data,cmc.members,cmc.map_rows)
        self.assertNotIn("DOGEUSDT",{x.contract for x in memes})
        self.assertIn("DOGE",diagnostics["cmc_ambiguous_symbols_excluded"])
        self.assertIn("PEPEUSDT",{x.contract for x in memes})

    def test_missing_category_blocks_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            bybit=FakeBybit(); cmc=FakeCMC(); cmc.members=[]; cmc.map_rows=[]
            trader,store,_=self.setup_trader(temp,bybit,cmc); result=trader.live_preflight()
            self.assertFalse(result["passed"]); self.assertFalse(result["paper_trading_authorized"]); store.close()

    def test_preflight_still_checks_cmc_when_bybit_feed_fails(self):
        class DownBybit(FakeBybit):
            def instruments(self): self.instrument_calls += 1; raise RuntimeError("simulated Bybit DNS failure")
        with tempfile.TemporaryDirectory() as temp:
            bybit=DownBybit(); cmc=FakeCMC(); trader,store,_=self.setup_trader(temp,bybit,cmc)
            result=trader.live_preflight()
            self.assertFalse(result["passed"])
            self.assertEqual(result["source_checks"]["bybit_market_feed"]["status"],"FAIL")
            self.assertEqual(result["source_checks"]["coinmarketcap_taxonomy_feed"]["status"],"PASS")
            self.assertFalse(result["paper_trading_authorized"]); store.close()

    def test_intraday_invocation_takes_no_action_and_makes_no_market_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            trader,store,bybit=self.setup_trader(temp)
            result=trader.process_daily_open(datetime(2026,1,1,12,0,tzinfo=timezone.utc))
            self.assertIn("waiting for next UTC daily open",result)
            self.assertEqual(bybit.instrument_calls,0); self.assertEqual(bybit.book_calls,0)
            self.assertIsNone(store.get_open_day()); store.close()

    def test_daily_open_closes_previous_then_uses_just_closed_ratio_candle_metrics(self):
        with tempfile.TemporaryDirectory() as temp:
            trader,store,_=self.setup_trader(temp)
            d1=datetime(2026,1,1,0,0,5,tzinfo=timezone.utc)
            self.assertIn("opened paper session",trader.process_daily_open(d1))
            open_day=store.get_open_day()
            self.assertEqual(open_day["payload"]["positions"]["long"]["contract"],"NVDAUSDT")
            self.assertEqual(open_day["payload"]["positions"]["short"]["contract"],"DOGEUSDT")
            d2=datetime(2026,1,2,0,0,5,tzinfo=timezone.utc)
            self.assertIn("opened paper session",trader.process_daily_open(d2))
            report=store.get_day("2026-01-01")["report"]; ratio=report["ratio_candle"]
            self.assertLess(ratio["high"],100); self.assertGreater(ratio["low"],1)
            self.assertAlmostEqual(report["ratio_roi_pct"],(ratio["close"]/ratio["open"]-1)*100,places=9)
            self.assertAlmostEqual(report["daily_ratio_drawdown_pct"],min(0.0,(ratio["low"]/ratio["open"]-1)*100),places=9)
            self.assertLess(report["daily_ratio_drawdown_pct"],0)
            self.assertEqual(report["performance_computed_at_utc"],d2.replace(hour=0,minute=0,second=0,microsecond=0).isoformat())
            self.assertEqual(report["daily_close_utc"],"2026-01-02T00:00:00+00:00")
            self.assertNotAlmostEqual(report["ratio_roi_pct"],report["net_portfolio_roi_pct"],places=4)
            self.assertEqual(store.get_open_day()["utc_date"],"2026-01-02"); store.close()

    def test_daily_ratio_candle_uses_only_synchronized_ratio_marks(self):
        long=[{"t":0,"o":"10","h":"15","l":"5","c":"11"},{"t":60,"o":"11","h":"13","l":"9","c":"12"}]
        short=[{"t":0,"o":"2","h":"3","l":"1","c":"2"},{"t":60,"o":"2","h":"4","l":"1","c":"3"}]
        candle=build_ratio_candle(long,short,expected_minutes=2,minimum_coverage=1)
        self.assertEqual(candle.open,5); self.assertEqual(candle.high,15)
        self.assertAlmostEqual(candle.low,5/3); self.assertEqual(candle.close,4)
        self.assertAlmostEqual(candle.roi,-0.2); self.assertAlmostEqual(candle.drawdown,5/3/5-1)

    def test_ratio_candle_requires_exact_first_and_last_minute(self):
        long=[{"t":60,"o":"10","h":"11","l":"9","c":"10"}]
        short=[{"t":60,"o":"2","h":"2.2","l":"1.8","c":"2"}]
        with self.assertRaises(Exception):
            build_ratio_candle(long,short,expected_minutes=1,minimum_coverage=1,expected_start_ts=0,expected_end_ts=120)

    def test_ratio_candle_rejects_missing_interior_minute_at_full_coverage(self):
        long=[{"t":0,"o":"10","h":"11","l":"9","c":"10"},{"t":120,"o":"10","h":"11","l":"9","c":"10"}]
        short=[{"t":0,"o":"2","h":"2.2","l":"1.8","c":"2"},{"t":120,"o":"2","h":"2.2","l":"1.8","c":"2"}]
        with self.assertRaises(Exception): build_ratio_candle(long,short,expected_minutes=3,minimum_coverage=1.0)

    def test_cumulative_ratio_drawdown_carries_forward_prior_worst_day(self):
        prior=[{"utc_date":"2026-01-01","cumulative_ratio_index_high":1.20,"cumulative_ratio_index_close":1.10,
                "cumulative_ratio_index_high_water":1.20,"maximum_ratio_drawdown_pct":-25.0}]
        result=cumulative_ratio_metrics(RatioCandle(open=200,high=220,low=190,close=210,samples=1440),prior)
        self.assertAlmostEqual(result["cumulative_ratio_index_close"],1.155)
        self.assertEqual(result["maximum_ratio_drawdown_pct"],-25.0)

    def test_cumulative_ratio_drawdown_updates_when_new_worse_low_occurs(self):
        prior=[{"utc_date":"2026-01-01","cumulative_ratio_index_high":1.20,"cumulative_ratio_index_close":1.10,
                "cumulative_ratio_index_high_water":1.20,"maximum_ratio_drawdown_pct":-25.0}]
        result=cumulative_ratio_metrics(RatioCandle(open=200,high=210,low=120,close=180,samples=1440),prior)
        self.assertLess(result["maximum_ratio_drawdown_pct"],-25.0)

    def test_failed_live_preflight_creates_no_new_positions_at_daily_open(self):
        with tempfile.TemporaryDirectory() as temp:
            bybit=FakeBybit(); cmc=FakeCMC(); cmc.members=[]; cmc.map_rows=[]
            trader,store,_=self.setup_trader(temp,bybit,cmc)
            result=trader.process_daily_open(datetime(2026,1,1,0,0,3,tzinfo=timezone.utc))
            self.assertIn("live Bybit/CMC preflight failed",result); self.assertEqual(bybit.book_calls,0)
            self.assertIsNone(store.get_open_day()); day=store.get_day("2026-01-01")
            self.assertEqual(day["status"],"SKIPPED"); self.assertFalse(day["payload"]["new_exposure_opened"]); store.close()

    def test_run_loop_at_midday_sleeps_without_market_or_position_actions(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls,tz=None): return datetime(2026,1,1,12,0,0,tzinfo=timezone.utc)
        class StopLoop(Exception): pass
        with tempfile.TemporaryDirectory() as temp:
            trader,store,bybit=self.setup_trader(temp)
            with patch("stocks_vs_memes.daily_trader.datetime",FrozenDateTime), patch("stocks_vs_memes.daily_trader.time.sleep",side_effect=StopLoop):
                with self.assertRaises(StopLoop): trader.run_loop()
            self.assertEqual(bybit.instrument_calls,0); self.assertEqual(bybit.ticker_calls,0); self.assertEqual(bybit.book_calls,0)
            self.assertIsNone(store.get_open_day()); store.close()

    def test_run_loop_daily_open_blocks_positions_when_live_category_is_missing(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls,tz=None): return datetime(2026,1,1,0,0,2,tzinfo=timezone.utc)
        class StopLoop(Exception): pass
        with tempfile.TemporaryDirectory() as temp:
            bybit=FakeBybit(); cmc=FakeCMC(); cmc.members=[]
            trader,store,_=self.setup_trader(temp,bybit,cmc)
            with patch("stocks_vs_memes.daily_trader.datetime",FrozenDateTime), patch("stocks_vs_memes.daily_trader.time.sleep",side_effect=StopLoop):
                with self.assertRaises(StopLoop): trader.run_loop()
            self.assertGreater(bybit.instrument_calls,0); self.assertEqual(bybit.book_calls,0)
            self.assertIsNone(store.get_open_day()); self.assertEqual(store.get_day("2026-01-01")["status"],"SKIPPED")
            store.close()


class GitHubScheduledRunnerTests(unittest.TestCase):
    def _bare_trader(self):
        trader = BybitDailyTrader.__new__(BybitDailyTrader)
        trader.entry_grace = 900
        trader.config = {"scheduled_lead_window_seconds": 600}
        trader.store = Mock()
        trader.store.get_day.return_value = None
        trader.store.get_open_day.return_value = None
        return trader

    def test_scheduled_runner_waits_until_next_utc_midnight_and_runs_once(self):
        times = [
            datetime(2026, 1, 1, 23, 58, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 1, 23, 58, 0, tzinfo=timezone.utc),
            datetime(2026, 1, 2, 0, 0, 1, tzinfo=timezone.utc),
            datetime(2026, 1, 2, 0, 0, 1, tzinfo=timezone.utc),
        ]
        class SequenceDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return times.pop(0)
        trader = self._bare_trader()
        calls = []
        trader.process_daily_open = lambda now: calls.append(now) or "processed one boundary"
        with patch("stocks_vs_memes.daily_trader.datetime", SequenceDateTime), patch("stocks_vs_memes.daily_trader.time.sleep") as sleeper:
            result = trader.run_scheduled_once()
        self.assertEqual(result, "processed one boundary")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], datetime(2026, 1, 2, 0, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(sleeper.call_count, 1)
        self.assertGreaterEqual(sleeper.call_args.args[0], 119)

    def test_scheduled_runner_accepts_github_queue_delay_inside_grace(self):
        now = datetime(2026, 1, 2, 0, 7, 0, tzinfo=timezone.utc)
        trader = self._bare_trader()
        calls = []
        trader.process_daily_open = lambda passed: calls.append(passed) or "processed current boundary"
        self.assertEqual(trader.run_scheduled_once(now), "processed current boundary")
        self.assertEqual(calls, [now])

    def test_scheduled_runner_skips_after_grace_without_market_actions(self):
        now = datetime(2026, 1, 2, 0, 16, 0, tzinfo=timezone.utc)
        trader = self._bare_trader()
        calls = []
        trader.process_daily_open = lambda passed: calls.append(passed) or "should not run"
        result = trader.run_scheduled_once(now)
        self.assertIn("missed UTC daily-open window", result)
        self.assertEqual(calls, [])
        trader.store.create_day.assert_called_once()
        trader.store.update_status.assert_called_once_with("2026-01-02", "SKIPPED", unittest.mock.ANY)




if __name__ == "__main__":
    unittest.main()
