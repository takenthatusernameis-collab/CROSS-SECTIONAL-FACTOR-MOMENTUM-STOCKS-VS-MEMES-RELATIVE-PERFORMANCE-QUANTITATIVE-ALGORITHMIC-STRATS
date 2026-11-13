"""Durable paper ledger; public market-data GETs only, never exchange orders."""
from __future__ import annotations
import argparse, json, os, sqlite3, sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable

D = Decimal
INITIAL_NAV = D("10000")
LONG_FEE = D("0.0005")
SHORT_FEE = D("0.0005")

class PaperError(RuntimeError):
    pass

def number(value: Any, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise PaperError(f"{name} is missing")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PaperError(f"{name} is not numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise PaperError(f"{name} is invalid")
    return result

def s(value: Decimal) -> str:
    return format(value, "f")

def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")

def utc_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()

def init_db(path: str | Path, initial_nav: Decimal) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(p), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS account(
        id INTEGER PRIMARY KEY CHECK(id=1), initial_nav TEXT NOT NULL,
        cash TEXT NOT NULL, realized TEXT NOT NULL DEFAULT '0',
        fees TEXT NOT NULL DEFAULT '0', funding TEXT NOT NULL DEFAULT '0');
      CREATE TABLE IF NOT EXISTS positions(
        leg TEXT PRIMARY KEY CHECK(leg IN ('LONG','SHORT')), symbol TEXT NOT NULL,
        qty TEXT NOT NULL, entry TEXT NOT NULL, last_funding_ms INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS ledger(
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
        snapshot_date TEXT NOT NULL, event_time_ms INTEGER NOT NULL,
        leg TEXT NOT NULL, event_type TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL, qty TEXT NOT NULL, price TEXT,
        notional TEXT NOT NULL, fee_rate TEXT, fee TEXT NOT NULL DEFAULT '0',
        realized TEXT NOT NULL DEFAULT '0', funding_rate TEXT, funding TEXT NOT NULL DEFAULT '0',
        detail_json TEXT NOT NULL DEFAULT '{}');
      CREATE INDEX IF NOT EXISTS ledger_date_idx ON ledger(snapshot_date,id);
      CREATE TABLE IF NOT EXISTS daily_snapshots(
        snapshot_date TEXT PRIMARY KEY, as_of_ms INTEGER NOT NULL,
        long_symbol TEXT NOT NULL, short_symbol TEXT NOT NULL,
        nav TEXT NOT NULL, cash TEXT NOT NULL, unrealized TEXT NOT NULL,
        realized_cumulative TEXT NOT NULL, fees_cumulative TEXT NOT NULL,
        funding_cumulative TEXT NOT NULL, gross_exposure TEXT NOT NULL,
        return_since_previous_pct TEXT, days_since_previous INTEGER,
        total_return_pct TEXT NOT NULL, long_position_json TEXT NOT NULL,
        short_position_json TEXT NOT NULL, long_fee_rate TEXT NOT NULL,
        short_fee_rate TEXT NOT NULL, ranking_run_at TEXT NOT NULL);
    """)
    row = db.execute("SELECT * FROM account WHERE id=1").fetchone()
    if row is None:
        db.execute("INSERT INTO account(id,initial_nav,cash) VALUES(1,?,?)",
                   (s(initial_nav), s(initial_nav)))
    elif number(row["initial_nav"], "stored initial NAV") != initial_nav:
        db.close()
        raise PaperError("Initial NAV is immutable once the portfolio database exists")
    return db

def parse_book(payload: Any) -> dict[str, list[tuple[Decimal, Decimal]]]:
    if not isinstance(payload, dict):
        raise PaperError("Depth response is not an object")
    book = {}
    for side in ("bids", "asks"):
        raw = payload.get(side)
        if not isinstance(raw, list) or not raw:
            raise PaperError(f"Order book has no {side}")
        levels = [(number(x[0], "price", True), number(x[1], "size", True))
                  for x in raw if isinstance(x, (list, tuple)) and len(x) >= 2]
        if len(levels) != len(raw):
            raise PaperError(f"Malformed {side} levels")
        if side == "bids" and any(a[0] < b[0] for a,b in zip(levels, levels[1:])):
            raise PaperError("Bid depth is not sorted")
        if side == "asks" and any(a[0] > b[0] for a,b in zip(levels, levels[1:])):
            raise PaperError("Ask depth is not sorted")
        book[side] = levels
    if book["bids"][0][0] >= book["asks"][0][0]:
        raise PaperError("Crossed/locked book")
    return book

def vwap(levels: list[tuple[Decimal, Decimal]], qty: Decimal) -> Decimal:
    remaining, cost = qty, D("0")
    if qty <= 0:
        raise PaperError("Order quantity must be positive")
    for price, available in levels:
        take = min(remaining, available)
        cost += take * price
        remaining -= take
        if remaining == 0:
            break
    if remaining > 0:
        raise PaperError("Insufficient visible depth for full simulated fill")
    return cost / qty

def lot_rules(market: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal | None]:
    filters = market.get("filters")
    if not isinstance(filters, list):
        raise PaperError(f"{market.get('symbol')}: missing exchange filters")
    by_type = {x.get("filterType"): x for x in filters if isinstance(x, dict)}
    chosen = None
    for name in ("MARKET_LOT_SIZE", "LOT_SIZE"):
        f = by_type.get(name)
        if not isinstance(f, dict):
            continue
        try:
            minimum = number(f.get("minQty"), "minQty", True)
            step = number(f.get("stepSize"), "stepSize", True)
            max_raw = f.get("maxQty")
            maximum = number(max_raw, "maxQty", True) if max_raw not in (None, "0", 0) else D("Infinity")
            chosen = (minimum, step, maximum)
            break
        except PaperError:
            continue
    if chosen is None:
        raise PaperError(f"{market.get('symbol')}: no usable lot-size filter")
    min_notional = None
    for f in filters:
        if isinstance(f, dict):
            raw = f.get("notional", f.get("minNotional"))
            if raw not in (None, "", "0", 0):
                val = number(raw, "minimum notional", True)
                min_notional = max(min_notional or val, val)
    return *chosen, min_notional

def size_order(target: Decimal, levels: list[tuple[Decimal, Decimal]],
               market: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    minimum, step, maximum, min_notional = lot_rules(market)
    if target <= 0:
        raise PaperError("Target notional must be positive")
    qty = (target / levels[0][0] / step).to_integral_value(rounding=ROUND_DOWN) * step
    for _ in range(100):
        if qty < minimum:
            raise PaperError(f"{market['symbol']}: target is below minimum quantity")
        if maximum.is_finite() and qty > maximum:
            raise PaperError(f"{market['symbol']}: target exceeds max quantity")
        price = vwap(levels, qty)
        notional = qty * price
        if notional <= target:
            if min_notional is not None and notional < min_notional:
                raise PaperError(f"{market['symbol']}: below minimum notional")
            return qty, price, notional
        # Recalculate against the impact-adjusted VWAP instead of decrementing
        # one lot at a time. Enforce monotonic reduction to avoid oscillation.
        revised = (target / price / step).to_integral_value(rounding=ROUND_DOWN) * step
        if revised >= qty:
            revised = qty - step
        qty = revised
    raise PaperError(f"{market['symbol']}: cannot size within target notional")

def funding_cash(leg: str, qty: Decimal, event: dict[str, Any]) -> Decimal:
    rate = number(event.get("fundingRate"), "fundingRate")
    mark = number(event.get("markPrice"), "funding markPrice", True)
    sign = D("1") if leg == "LONG" else D("-1")
    return -sign * qty * mark * rate

def insert_event(db: sqlite3.Connection, key: str, date: str, time_ms: int,
                 leg: str, kind: str, symbol: str, side: str, qty: Decimal,
                 price: Decimal | None = None, notional: Decimal = D("0"),
                 fee_rate: Decimal | None = None, fee: Decimal = D("0"),
                 realized: Decimal = D("0"), funding_rate: Decimal | None = None,
                 funding: Decimal = D("0"), detail: dict | None = None) -> None:
    db.execute("""INSERT INTO ledger
      (event_key,snapshot_date,event_time_ms,leg,event_type,symbol,side,qty,price,
       notional,fee_rate,fee,realized,funding_rate,funding,detail_json)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (key,date,time_ms,leg,kind,symbol,side,s(qty),s(price) if price is not None else None,
       s(notional),s(fee_rate) if fee_rate is not None else None,s(fee),s(realized),
       s(funding_rate) if funding_rate is not None else None,s(funding),
       json.dumps(detail or {},sort_keys=True)))

def run_day(report: dict[str, Any], path: str | Path, *,
            depth_fetch: Callable[[str], Any],
            funding_fetch: Callable[[str,int,int], Any],
            initial_nav: Decimal = INITIAL_NAV,
            long_fee: Decimal = LONG_FEE,
            short_fee: Decimal = SHORT_FEE) -> dict[str, Any]:
    if report.get("gates", {}).get("ranking_diagnostic") != "PASS":
        raise PaperError("Ranking gate is not PASS; no portfolio change made")
    leaders = report.get("provisional_leaders", {})
    long_lead, short_lead = leaders.get("provisional_long_leader"), leaders.get("provisional_meme_short_leader")
    if not isinstance(long_lead, dict) or not isinstance(short_lead, dict):
        raise PaperError("Both ranked legs are required")
    long_sym, short_sym = long_lead.get("symbol"), short_lead.get("symbol")
    if not isinstance(long_sym,str) or not isinstance(short_sym,str) or long_sym == short_sym:
        raise PaperError("Selected symbols are invalid or duplicated")
    as_of = report.get("exchange_info",{}).get("as_of_server_time_ms")
    if isinstance(as_of,bool) or not isinstance(as_of,int) or as_of <= 0:
        raise PaperError("Missing Binance server timestamp")
    date = utc_date(as_of)
    long_fee, short_fee = number(long_fee,"long fee"), number(short_fee,"short fee")
    initial_nav = number(initial_nav,"initial NAV",True)
    if not (D("0") <= long_fee < 1 and D("0") <= short_fee < 1):
        raise PaperError("Fee rates must be in [0,1)")
    db = init_db(path, initial_nav)
    try:
        prior_day = db.execute("SELECT * FROM daily_snapshots WHERE snapshot_date=?", (date,)).fetchone()
        if prior_day:
            return {"status":"ALREADY_PROCESSED","updated":False,"snapshot_date":date,
                    "message":"A snapshot already exists for this UTC date; no duplicate trades added."}
        old = {r["leg"]:dict(r) for r in db.execute("SELECT * FROM positions")}
        markets = {m.get("symbol"):m for m in report.get("market_results",[]) if isinstance(m,dict)}
        for sym in (long_sym,short_sym):
            m = markets.get(sym)
            if not isinstance(m,dict) or not m.get("valid") or m.get("quote_asset") != "USDT" or m.get("margin_asset") != "USDT":
                raise PaperError(f"{sym}: selected market data or USDT contract metadata invalid")
        syms = {long_sym,short_sym} | {p["symbol"] for p in old.values()}
        books = {sym:parse_book(depth_fetch(sym)) for sym in sorted(syms)}
        funding_events = {}
        for leg,p in old.items():
            events = funding_fetch(p["symbol"], int(p["last_funding_ms"])+1, as_of)
            if not isinstance(events,list) or len(events) >= 1000:
                raise PaperError(f"{p['symbol']}: funding history missing or truncated")
            checked=[]
            for e in events:
                if not isinstance(e,dict) or e.get("symbol") != p["symbol"]:
                    raise PaperError("Malformed funding event")
                t=e.get("fundingTime")
                if isinstance(t,bool) or not isinstance(t,int):
                    raise PaperError("Invalid fundingTime")
                if int(p["last_funding_ms"]) < t <= as_of:
                    number(e.get("fundingRate"),"fundingRate")
                    number(e.get("markPrice"),"funding markPrice",True)
                    checked.append(e)
            funding_events[leg]=sorted(checked,key=lambda x:x["fundingTime"])
        db.execute("BEGIN IMMEDIATE")
        acct=db.execute("SELECT * FROM account WHERE id=1").fetchone()
        cash,realized,fees,funding=(number(acct[k],k) for k in ("cash","realized","fees","funding"))
        previous=db.execute("SELECT snapshot_date,nav FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        prev_nav=number(previous["nav"],"previous NAV") if previous else None
        days=(datetime.fromisoformat(date).date()-datetime.fromisoformat(previous["snapshot_date"]).date()).days if previous else None
        count_before=db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]
        for leg,p in old.items():
            qty=number(p["qty"],"position qty")
            for e in funding_events[leg]:
                payment=funding_cash(leg,qty,e)
                rate=number(e["fundingRate"],"fundingRate")
                mark=number(e["markPrice"],"funding markPrice",True)
                cash+=payment; funding+=payment
                insert_event(db,f"{date}:{leg}:funding:{e['fundingTime']}:{e.get('rateType','?')}",date,e["fundingTime"],leg,"FUNDING",p["symbol"],leg,qty,mark,qty*mark,funding_rate=rate,funding=payment,detail={"rate_type":e.get("rateType")})
        for leg in ("LONG","SHORT"):
            p=old.get(leg)
            if not p: continue
            qty=number(p["qty"],"position qty")
            entry=number(p["entry"],"entry")
            direction=D("1") if leg=="LONG" else D("-1")
            book=books[p["symbol"]]
            px=vwap(book["bids"] if leg=="LONG" else book["asks"],qty)
            pnl=(px-entry)*qty*direction
            fee_rate=long_fee if leg=="LONG" else short_fee
            notional=px*qty; fee=notional*fee_rate
            cash+=pnl-fee; realized+=pnl; fees+=fee
            insert_event(db,f"{date}:{leg}:close",date,as_of,leg,"CLOSE",p["symbol"],"SELL" if leg=="LONG" else "BUY",qty,px,notional,fee_rate,fee,pnl,detail={"model":"depth VWAP"})
            db.execute("DELETE FROM positions WHERE leg=?",(leg,))
        target=cash/(D("1")+long_fee+short_fee)
        new={}
        for leg,sym,fee_rate in (("LONG",long_sym,long_fee),("SHORT",short_sym,short_fee)):
            market=markets[sym]; book=books[sym]
            qty,px,notional=size_order(target,book["asks"] if leg=="LONG" else book["bids"],market)
            fee=notional*fee_rate; cash-=fee; fees+=fee
            insert_event(db,f"{date}:{leg}:open",date,as_of,leg,"OPEN",sym,"BUY" if leg=="LONG" else "SELL",qty,px,notional,fee_rate,fee,detail={"model":"depth VWAP","target_notional":s(target)})
            db.execute("INSERT INTO positions VALUES(?,?,?,?,?)",(leg,sym,s(qty),s(px),as_of))
            new[leg]={"leg":leg,"symbol":sym,"qty":qty,"entry":px}
        unrealized,gross= D("0"),D("0")
        mark_json={}
        for leg,p in new.items():
            book=books[p["symbol"]]
            mark=(book["bids"][0][0]+book["asks"][0][0])/2
            sign=D("1") if leg=="LONG" else D("-1")
            u=(mark-p["entry"])*p["qty"]*sign
            unrealized+=u; gross+=mark*p["qty"]
            mark_json[leg]=json.dumps({"leg":leg,"symbol":p["symbol"],"quantity":s(p["qty"]),
                "entry_price":s(p["entry"]),"mark_price":s(mark),"unrealized_pnl_usdt":s(u),
                "entry_time_utc":iso(as_of)},sort_keys=True)
        nav=cash+unrealized
        if nav<=0: raise PaperError("NAV is non-positive")
        period=(nav/prev_nav-1)*100 if prev_nav else None
        total=(nav/initial_nav-1)*100
        db.execute("""INSERT INTO daily_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (date,as_of,long_sym,short_sym,s(nav),s(cash),s(unrealized),s(realized),s(fees),s(funding),
           s(gross),s(period) if period is not None else None,days,s(total),mark_json["LONG"],
           mark_json["SHORT"],s(long_fee),s(short_fee),str(report.get("run_at_utc",iso(as_of)))))
        db.execute("UPDATE account SET cash=?,realized=?,fees=?,funding=? WHERE id=1",
                   (s(cash),s(realized),s(fees),s(funding)))
        added=db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]-count_before
        db.execute("COMMIT")
        return {"status":"UPDATED","updated":True,"snapshot_date":date,"ledger_rows_added":added,
          "snapshot":{"nav_usdt":s(nav),"cash_balance_usdt":s(cash),"unrealized_pnl_usdt":s(unrealized),
            "period_return_pct":s(period) if period is not None else None,"days_since_previous_snapshot":days,
            "total_return_pct":s(total),"realized_pnl_cumulative_usdt":s(realized),
            "fees_cumulative_usdt":s(fees),"funding_cumulative_usdt":s(funding),
            "gross_exposure_usdt":s(gross),"gross_exposure_to_nav":s(gross/nav),
            "long_position":json.loads(mark_json["LONG"]),"short_position":json.loads(mark_json["SHORT"])}}
    except Exception:
        if db.in_transaction: db.execute("ROLLBACK")
        raise
    finally:
        db.close()

def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ranking-report",required=True)
    parser.add_argument("--db-path",default="state/paper_portfolio.sqlite3")
    parser.add_argument("--output-dir",default="artifacts/binance-paper-portfolio")
    parser.add_argument("--initial-nav-usdt",default=os.getenv("PAPER_INITIAL_NAV_USDT","10000"))
    parser.add_argument("--long-taker-fee-rate",default=os.getenv("PAPER_LONG_TAKER_FEE_RATE","0.0005"))
    parser.add_argument("--short-taker-fee-rate",default=os.getenv("PAPER_SHORT_TAKER_FEE_RATE","0.0005"))
    a=parser.parse_args(argv); out=Path(a.output_dir); out.mkdir(parents=True,exist_ok=True)
    config={"initial_nav_usdt":str(a.initial_nav_usdt),"long_taker_fee_rate":str(a.long_taker_fee_rate),"short_taker_fee_rate":str(a.short_taker_fee_rate)}
    try:
        report=json.loads(Path(a.ranking_report).read_text(encoding="utf-8"))
        from .preflight import request_json
        def depth(sym): return request_json("depth",{"symbol":sym,"limit":"100"})[0]
        def funding_fn(sym,start,end):
            return request_json("fundingRate",{"symbol":sym,"startTime":str(start),"endTime":str(end),"limit":"1000"})[0]
        result=run_day(report,a.db_path,depth_fetch=depth,funding_fetch=funding_fn,
            initial_nav=number(a.initial_nav_usdt,"initial NAV",True),
            long_fee=number(a.long_taker_fee_rate,"long fee"),
            short_fee=number(a.short_taker_fee_rate,"short fee"))
    except Exception as exc:
        result={"status":"BLOCKED","updated":False,"snapshot_date":datetime.now(timezone.utc).date().isoformat(),"message":f"{type(exc).__name__}: {exc}"}
    (out/"paper_portfolio_report.json").write_text(json.dumps({"result":result,"config":config},indent=2,default=str)+"\n",encoding="utf-8")
    if result["status"]=="UPDATED":
        snap=result["snapshot"]
        lines=["# Binance paper portfolio snapshot","",f"- Date (UTC): {result['snapshot_date']}",f"- NAV: {snap['nav_usdt']} USDT",f"- Return since prior snapshot: {snap['period_return_pct']}%",f"- Total return: {snap['total_return_pct']}%",f"- Realized P&L: {snap['realized_pnl_cumulative_usdt']} USDT",f"- Unrealized P&L: {snap['unrealized_pnl_usdt']} USDT",f"- Fees: {snap['fees_cumulative_usdt']} USDT",f"- Funding: {snap['funding_cumulative_usdt']} USDT",f"- Gross exposure: {snap['gross_exposure_to_nav']}x NAV","","This is a depth-VWAP paper estimate, not exchange fills. Margin, liquidation, ADL, corporate actions and account-specific eligibility are not modeled. No orders or private endpoints are used.",""]
    else: lines=["# Binance paper portfolio processing", "",f"- Status: {result['status']}",f"- {result.get('message','No portfolio change committed.')}", ""]
    (out/"paper_portfolio_report.md").write_text("\n".join(lines),encoding="utf-8")
    print(json.dumps(result,indent=2,default=str))
    return 0 if result["status"] in ("UPDATED","ALREADY_PROCESSED") else 1

if __name__=="__main__":
    sys.exit(main())
