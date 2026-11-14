#!/usr/bin/env python3
"""Read-only public API reachability probe for 24/7 equity perpetual venues."""
from __future__ import annotations
import json, os, platform, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TIMEOUT = 15
HEADERS = {"Accept": "application/json", "User-Agent": "equity-perp-public-market-data-probe/1.0"}

def get_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=HEADERS, method="GET")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read(5_000_001)
            if len(raw) > 5_000_000:
                raise ValueError("response exceeded 5 MB safety limit")
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON response root is not an object")
            return {"http_status": int(response.status),
                    "elapsed_seconds": round(time.monotonic()-started, 3),
                    "payload": payload, "error": None}
    except urllib.error.HTTPError as exc:
        try: detail = exc.read(1200).decode("utf-8", errors="replace")
        except Exception: detail = ""
        return {"http_status": int(exc.code),
                "elapsed_seconds": round(time.monotonic()-started, 3),
                "payload": {}, "error": detail[:1200] or str(exc)}
    except Exception as exc:
        return {"http_status": None,
                "elapsed_seconds": round(time.monotonic()-started, 3),
                "payload": {}, "error": f"{type(exc).__name__}: {exc}"[:1200]}

def rows(payload: dict[str, Any], *paths: str) -> list[dict[str, Any]]:
    value: Any = payload
    for key in paths:
        value = value.get(key) if isinstance(value, dict) else None
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []

def summary(label: str, r: dict[str, Any], ok: bool) -> dict[str, Any]:
    return {"request": label, "http_status": r["http_status"],
            "elapsed_seconds": r["elapsed_seconds"], "api_success": bool(ok),
            "error": r["error"]}

def base_record(name: str, hours: str) -> dict[str, Any]:
    return {"venue": name, "documented_stock_perp_hours": hours,
            "metadata": {}, "ticker": {}, "orderbook": {},
            "market_found": False, "live_quote_found": False,
            "usable_public_market_data": False, "sample_symbols": [], "notes": []}

def probe_kraken() -> dict[str, Any]:
    r = base_record("Kraken Futures", "24/7 for listed xStocks perpetuals.")
    base = "https://futures.kraken.com/derivatives/api/v3"
    m = get_json(base + "/instruments")
    mok = m["http_status"] == 200 and m["payload"].get("result") == "success"
    instruments = rows(m["payload"], "instruments")
    r["metadata"] = summary("GET /instruments", m, mok)
    targets = {"PF_AAPLXUSD","PF_NVDAXUSD","PF_TSLAXUSD","PF_GOOGLXUSD","PF_QQQXUSD",
               "PF_SPYXUSD","PF_HOODXUSD","PF_CRCLXUSD","PF_MSTRXUSD","PF_GLDXUSD"}
    found = [x for x in instruments if str(x.get("symbol","")).upper() in targets]
    r["market_found"] = bool(found)
    r["sample_symbols"] = [x.get("symbol") for x in found]
    t = get_json(base + "/tickers")
    tok = t["http_status"] == 200 and t["payload"].get("result") == "success"
    tickers = rows(t["payload"], "tickers")
    r["ticker"] = summary("GET /tickers", t, tok)
    q = next((x for x in tickers if str(x.get("symbol","")).upper() in targets), None)
    if q:
        symbol = q.get("symbol")
        r["ticker"].update({"sample_symbol":symbol,"last":q.get("last"),
                            "bid":q.get("bid"),"ask":q.get("ask"),
                            "suspended":q.get("suspended"),
                            "last_time":q.get("lastTime"),
                            "funding_rate":q.get("fundingRate")})
        r["live_quote_found"] = bool(q.get("last") is not None and q.get("bid") is not None
                                     and q.get("ask") is not None and not q.get("suspended",False))
        ob = get_json(base + "/orderbook?" + urllib.parse.urlencode({"symbol":symbol}))
        ook = ob["http_status"] == 200 and ob["payload"].get("result") == "success"
        r["orderbook"] = summary("GET /orderbook", ob, ook)
        obdata = ob["payload"].get("orderBook") or ob["payload"].get("orderbook") or {}
        if isinstance(obdata,dict):
            r["orderbook"]["bid_levels"] = len(obdata.get("bids") or [])
            r["orderbook"]["ask_levels"] = len(obdata.get("asks") or [])
    else:
        r["notes"].append("No listed xStocks perpetual was found in the public ticker set.")
    r["usable_public_market_data"] = bool(mok and tok and r["market_found"] and r["live_quote_found"])
    return r

def probe_bybit() -> dict[str, Any]:
    r = base_record("Bybit", "TradFi stock perpetuals documented as 24/7; weekend risk controls can be symbol-specific.")
    base = "https://api.bybit.com"
    url = base + "/v5/market/instrument-summary?" + urllib.parse.urlencode(
        {"category":"linear","marketRegion":"US","limit":1000})
    m = get_json(url)
    mok = m["http_status"] == 200 and m["payload"].get("retCode") == 0
    instruments = rows(m["payload"],"result","list")
    r["metadata"] = summary("GET /v5/market/instrument-summary",m,mok)
    targets = {"TSLAUSDT","AAPLUSDT","NVDAUSDT","MSTRUSDT","GOOGLUSDT"}
    q = next((x for x in instruments if str(x.get("symbol","")).upper() in targets),None)
    if q is None:
        q = next((x for x in instruments if x.get("underlyingTicker") in
                  ("TSLA","AAPL","NVDA","MSTR","GOOGL")),None)
    symbol = q.get("symbol") if q else None
    r["market_found"] = q is not None
    r["sample_symbols"] = [x.get("symbol") for x in instruments
                           if x.get("underlyingTicker") or x.get("marketRegion")][:20]
    if q:
        r["metadata"].update({"sample_symbol":symbol,"market_region":q.get("marketRegion"),
                              "underlying_ticker":q.get("underlyingTicker")})
        t = get_json(base + "/v5/market/tickers?" + urllib.parse.urlencode(
            {"category":"linear","symbol":symbol}))
        tok = t["http_status"] == 200 and t["payload"].get("retCode") == 0
        ts = rows(t["payload"],"result","list")
        r["ticker"] = summary("GET /v5/market/tickers",t,tok)
        if ts:
            z=ts[0]
            r["ticker"].update({"sample_symbol":z.get("symbol"),"last":z.get("lastPrice"),
                                "bid":z.get("bid1Price"),"ask":z.get("ask1Price"),
                                "funding_rate":z.get("fundingRate")})
            r["live_quote_found"] = bool(z.get("lastPrice") and z.get("bid1Price") and z.get("ask1Price"))
            ob=get_json(base + "/v5/market/orderbook?" + urllib.parse.urlencode(
                {"category":"linear","symbol":symbol,"limit":50}))
            ook=ob["http_status"] == 200 and ob["payload"].get("retCode") == 0
            r["orderbook"]=summary("GET /v5/market/orderbook",ob,ook)
            od=ob["payload"].get("result",{})
            r["orderbook"].update({"bid_levels":len(od.get("b") or []),"ask_levels":len(od.get("a") or [])})
        else:
            r["ticker"]["error"] = r["ticker"].get("error") or "No ticker row for stock symbol."
    else:
        r["notes"].append("No TradFi stock instrument found in public instrument-summary response.")
    r["usable_public_market_data"] = bool(mok and r["market_found"] and r["live_quote_found"])
    return r

def probe_bitget() -> dict[str, Any]:
    r=base_record("Bitget","Stock perpetuals documented as continuous 24/7.")
    base="https://api.bitget.com"
    m=get_json(base+"/api/v2/mix/market/contracts?productType=USDT-FUTURES")
    mok=m["http_status"] == 200 and m["payload"].get("code") == "00000"
    contracts=rows(m["payload"],"data")
    r["metadata"]=summary("GET /api/v2/mix/market/contracts",m,mok)
    targets={"TSLAUSDT","AAPLUSDT","NVDAUSDT","MSTRUSDT","GOOGLUSDT"}
    q=next((x for x in contracts if str(x.get("symbol","")).upper() in targets),None)
    rwa=[x for x in contracts if str(x.get("isRwa","")).upper()=="YES"]
    r["market_found"]=q is not None
    r["metadata"]["rwa_contract_count"]=len(rwa)
    r["sample_symbols"]=[x.get("symbol") for x in ( [q] if q else rwa[:15])]
    if q:
        symbol=q.get("symbol")
        r["metadata"].update({"sample_symbol":symbol,"is_rwa":q.get("isRwa")})
        t=get_json(base+"/api/v2/mix/market/tickers?productType=USDT-FUTURES")
        tok=t["http_status"] == 200 and t["payload"].get("code") == "00000"
        ts=rows(t["payload"],"data")
        r["ticker"]=summary("GET /api/v2/mix/market/tickers",t,tok)
        z=next((x for x in ts if str(x.get("symbol","")).upper()==symbol.upper()),None)
        if z:
            r["ticker"].update({"sample_symbol":symbol,"last":z.get("lastPr"),
                                "bid":z.get("bidPr"),"ask":z.get("askPr"),
                                "funding_rate":z.get("fundingRate")})
            r["live_quote_found"]=bool(z.get("lastPr") and z.get("bidPr") and z.get("askPr"))
            ob=get_json(base+"/api/v2/mix/market/merge-depth?"+urllib.parse.urlencode(
                {"symbol":symbol,"productType":"USDT-FUTURES","limit":50}))
            ook=ob["http_status"] == 200 and ob["payload"].get("code") == "00000"
            r["orderbook"]=summary("GET /api/v2/mix/market/merge-depth",ob,ook)
            od=ob["payload"].get("data",{})
            r["orderbook"].update({"bid_levels":len(od.get("bids") or []),"ask_levels":len(od.get("asks") or [])})
        else:
            r["ticker"]["error"]=r["ticker"].get("error") or "Stock ticker absent from public ticker list."
    else:
        r["notes"].append("No target stock contract found in USDT-FUTURES contracts catalog.")
    r["usable_public_market_data"]=bool(mok and r["market_found"] and r["live_quote_found"])
    return r

def probe_okx() -> dict[str, Any]:
    r=base_record("OKX","Stock perpetuals documented as 24/7.")
    base="https://www.okx.com"
    targets={"TSLA-USDT-SWAP","AAPL-USDT-SWAP","NVDA-USDT-SWAP","MSTR-USDT-SWAP","GOOGL-USDT-SWAP"}
    m=get_json(base+"/api/v5/public/instruments?instType=SWAP")
    mok=m["http_status"] == 200 and m["payload"].get("code") == "0"
    instruments=rows(m["payload"],"data")
    r["metadata"]=summary("GET /api/v5/public/instruments",m,mok)
    q=next((x for x in instruments if str(x.get("instId","")).upper() in targets),None)
    symbol=q.get("instId") if q else None
    r["market_found"]=q is not None
    r["sample_symbols"]=[x.get("instId") for x in instruments if x.get("instId") in targets]
    if q:
        r["metadata"].update({"sample_symbol":symbol,"state":q.get("state")})
        t=get_json(base+"/api/v5/market/ticker?"+urllib.parse.urlencode({"instId":symbol}))
        tok=t["http_status"] == 200 and t["payload"].get("code") == "0"
        ts=rows(t["payload"],"data")
        r["ticker"]=summary("GET /api/v5/market/ticker",t,tok)
        if ts:
            z=ts[0]
            r["ticker"].update({"sample_symbol":symbol,"last":z.get("last"),
                                "bid":z.get("bidPx"),"ask":z.get("askPx")})
            r["live_quote_found"]=bool(z.get("last") and z.get("bidPx") and z.get("askPx"))
            ob=get_json(base+"/api/v5/market/books?"+urllib.parse.urlencode({"instId":symbol,"sz":50}))
            ook=ob["http_status"] == 200 and ob["payload"].get("code") == "0"
            r["orderbook"]=summary("GET /api/v5/market/books",ob,ook)
            od=(ob["payload"].get("data") or [{}])[0]
            r["orderbook"].update({"bid_levels":len(od.get("bids") or []),"ask_levels":len(od.get("asks") or [])})
        else:
            r["ticker"]["error"]=r["ticker"].get("error") or "No ticker row for stock symbol."
    else:
        r["notes"].append("No target USDT stock perpetual found in SWAP instruments catalog.")
    r["usable_public_market_data"]=bool(mok and r["market_found"] and r["live_quote_found"])
    return r

def probe_bingx() -> dict[str, Any]:
    r=base_record("BingX","24/7 only for selected stock symbols; check current per-symbol schedules.")
    base="https://open-api.bingx.com"
    targets={"TSLA-USDT","CRCL-USDT","JPM-USDT","AAPL-USDT","NVDA-USDT"}
    m=get_json(base+"/openApi/swap/v2/quote/contracts")
    mok=m["http_status"] == 200 and m["payload"].get("code") in (0,"0")
    contracts=rows(m["payload"],"data")
    r["metadata"]=summary("GET /openApi/swap/v2/quote/contracts",m,mok)
    q=next((x for x in contracts if str(x.get("symbol","")).upper() in targets),None)
    symbol=q.get("symbol") if q else None
    r["market_found"]=q is not None
    r["sample_symbols"]=[x.get("symbol") for x in contracts if x.get("symbol") in targets]
    if q:
        t=get_json(base+"/openApi/swap/v2/quote/ticker?"+urllib.parse.urlencode({"symbol":symbol}))
        tok=t["http_status"] == 200 and t["payload"].get("code") in (0,"0")
        z=t["payload"].get("data",{})
        r["ticker"]=summary("GET /openApi/swap/v2/quote/ticker",t,tok)
        if isinstance(z,dict):
            r["ticker"].update({"sample_symbol":symbol,"last":z.get("lastPrice"),
                                "bid":z.get("bidPrice"),"ask":z.get("askPrice")})
            r["live_quote_found"]=bool(z.get("lastPrice") and z.get("bidPrice") and z.get("askPrice"))
            ob=get_json(base+"/openApi/swap/v2/quote/depth?"+urllib.parse.urlencode({"symbol":symbol,"limit":50}))
            ook=ob["http_status"] == 200 and ob["payload"].get("code") in (0,"0")
            r["orderbook"]=summary("GET /openApi/swap/v2/quote/depth",ob,ook)
            od=ob["payload"].get("data",{})
            r["orderbook"].update({"bid_levels":len(od.get("bids") or []),"ask_levels":len(od.get("asks") or [])})
    else:
        r["notes"].append("No selected 24/7 stock symbol found in public USDT swap catalog.")
    r["usable_public_market_data"]=bool(mok and r["market_found"] and r["live_quote_found"])
    return r

def render(report: dict[str, Any]) -> str:
    lines=["# 24/7 Equity Perpetual — Public API Probe","",
           f"- Run UTC: {report['run_at_utc']}",
           f"- Runner OS: {report['runner']['os']}",
           f"- Python: {report['runner']['python']}",
           f"- GitHub run: {report['runner'].get('github_run_id')}","",
           "Public unauthenticated GET requests only. No account access, order submission, or portfolio changes.","",
           "| Venue | Metadata API | Stock perp found | Live bid/ask/last | Public data usable |",
           "|---|---:|---:|---:|---:|"]
    for v in report["venues"]:
        lines.append(f"| {v['venue']} | {(v.get('metadata') or {}).get('api_success')} | "
                     f"{v.get('market_found')} | {v.get('live_quote_found')} | {v.get('usable_public_market_data')} |")
    for v in report["venues"]:
        lines.extend(["",f"## {v['venue']}","",f"- Documented hours: {v.get('documented_stock_perp_hours')}",
                      f"- Symbols: {', '.join(v.get('sample_symbols') or []) or 'none'}"])
        for section in ("metadata","ticker","orderbook"):
            d=v.get(section) or {}
            if d:
                lines.append(f"- {section.title()}: HTTP {d.get('http_status')}; API success {d.get('api_success')}; "
                             f"{d.get('elapsed_seconds')}s; {d.get('error') or ''}")
                if section=="ticker":
                    lines.append(f"  - Sample: {d.get('sample_symbol')}; last={d.get('last')}; "
                                 f"bid={d.get('bid')}; ask={d.get('ask')}")
        lines.extend(f"- Note: {x}" for x in v.get("notes",[]))
    lines.extend(["","API reachability is not permission to trade. 24/7 schedules remain subject to product eligibility, maintenance, risk controls, and instrument-specific suspensions.",""])
    return "\n".join(lines)

def main() -> int:
    venues=[]
    for fn in (probe_kraken,probe_bybit,probe_bitget,probe_okx,probe_bingx):
        try:
            venues.append(fn())
        except Exception as exc:
            venues.append({"venue":fn.__name__.replace("probe_","").upper(),
                           "market_found":False,"live_quote_found":False,
                           "usable_public_market_data":False,"sample_symbols":[],
                           "metadata":{},"ticker":{},"orderbook":{},
                           "notes":[f"{type(exc).__name__}: {exc}"]})
    report={"report_version":1,
            "run_at_utc":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
            "runner":{"os":platform.platform(),"python":platform.python_version(),
                      "github_runner_os":os.getenv("RUNNER_OS"),
                      "github_runner_name":os.getenv("RUNNER_NAME"),
                      "github_run_id":os.getenv("GITHUB_RUN_ID"),
                      "github_sha":os.getenv("GITHUB_SHA"),"github_ref":os.getenv("GITHUB_REF")},
            "safety":{"public_get_only":True,"authentication":"none",
                      "account_endpoints":False,"order_endpoints":False,
                      "portfolio_state_changed":False},
            "venues":venues,
            "venues_with_usable_public_market_data":[v["venue"] for v in venues if v.get("usable_public_market_data")]}
    out=Path("artifacts/equity-perp-probe")
    out.mkdir(parents=True,exist_ok=True)
    (out/"equity_perp_probe.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    (out/"equity_perp_probe.md").write_text(render(report),encoding="utf-8")
    print(json.dumps({"venues_with_usable_public_market_data":report["venues_with_usable_public_market_data"],
                      "results":[{"venue":v["venue"],
                                  "api_reachable":(v.get("metadata") or {}).get("api_success"),
                                  "market_found":v.get("market_found"),
                                  "live_quote_found":v.get("live_quote_found"),
                                  "usable_public_market_data":v.get("usable_public_market_data")}
                                 for v in venues]},indent=2))
    return 0

if __name__=="__main__":
    sys.exit(main())
