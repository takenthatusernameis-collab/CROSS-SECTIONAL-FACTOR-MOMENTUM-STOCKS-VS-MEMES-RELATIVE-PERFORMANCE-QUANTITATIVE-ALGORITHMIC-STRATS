"""SQLite-backed paper accounting for Bitget USDT perpetuals.

No order endpoints are used. Paper fills are depth-walk VWAP estimates.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable

D = Decimal


class PaperError(RuntimeError):
    pass


def dec(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise PaperError(f"{name} is missing")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PaperError(f"{name} is not numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise PaperError(f"{name} is invalid")
    return result


def fmt(value: Decimal) -> str:
    return format(value, "f")


def utc_date(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).date().isoformat()


def utc_iso(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def init_db(path: str | Path, initial_nav: Decimal) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target), timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=DELETE")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS account(
        id INTEGER PRIMARY KEY CHECK(id=1), initial_nav TEXT NOT NULL,
        cash TEXT NOT NULL, realized_gross_pnl TEXT NOT NULL DEFAULT '0',
        fees_cumulative TEXT NOT NULL DEFAULT '0', funding_cumulative TEXT NOT NULL DEFAULT '0');
      CREATE TABLE IF NOT EXISTS trades(
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT, leg TEXT NOT NULL CHECK(leg IN ('LONG','SHORT')),
        symbol TEXT NOT NULL, entry_time_ms INTEGER NOT NULL, entry_date TEXT NOT NULL,
        entry_qty TEXT NOT NULL, entry_price TEXT NOT NULL, entry_notional TEXT NOT NULL,
        entry_fee_rate TEXT NOT NULL, entry_fee TEXT NOT NULL,
        funding_pnl TEXT NOT NULL DEFAULT '0', exit_time_ms INTEGER, exit_date TEXT,
        exit_price TEXT, exit_notional TEXT, exit_fee_rate TEXT, exit_fee TEXT,
        gross_pnl TEXT, net_pnl TEXT, trade_roi_pct TEXT, status TEXT NOT NULL DEFAULT 'OPEN');
      CREATE INDEX IF NOT EXISTS trades_date_idx ON trades(entry_date, trade_id);
      CREATE TABLE IF NOT EXISTS positions(
        leg TEXT PRIMARY KEY CHECK(leg IN ('LONG','SHORT')), trade_id INTEGER NOT NULL UNIQUE,
        symbol TEXT NOT NULL, qty TEXT NOT NULL, entry_price TEXT NOT NULL,
        entry_notional TEXT NOT NULL, entry_fee_rate TEXT NOT NULL, entry_fee TEXT NOT NULL,
        last_funding_ms INTEGER NOT NULL,
        FOREIGN KEY(trade_id) REFERENCES trades(trade_id));
      CREATE TABLE IF NOT EXISTS ledger(
        id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT UNIQUE NOT NULL,
        snapshot_date TEXT NOT NULL, event_time_ms INTEGER NOT NULL,
        trade_id INTEGER, leg TEXT NOT NULL, event_type TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL, qty TEXT NOT NULL, price TEXT, notional TEXT NOT NULL,
        fee_rate TEXT, fee TEXT NOT NULL DEFAULT '0', gross_pnl TEXT NOT NULL DEFAULT '0',
        funding_rate TEXT, funding_cashflow TEXT NOT NULL DEFAULT '0', detail_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY(trade_id) REFERENCES trades(trade_id));
      CREATE INDEX IF NOT EXISTS ledger_date_idx ON ledger(snapshot_date,id);
      CREATE TABLE IF NOT EXISTS daily_snapshots(
        snapshot_date TEXT PRIMARY KEY, as_of_ms INTEGER NOT NULL,
        long_symbol TEXT NOT NULL, short_symbol TEXT NOT NULL,
        nav TEXT NOT NULL, cash TEXT NOT NULL, unrealized_pnl TEXT NOT NULL,
        realized_gross_pnl_cumulative TEXT NOT NULL, fees_cumulative TEXT NOT NULL,
        funding_cumulative TEXT NOT NULL, gross_exposure TEXT NOT NULL,
        daily_return_pct TEXT, hours_since_previous REAL,
        total_return_pct TEXT NOT NULL, long_position_json TEXT NOT NULL,
        short_position_json TEXT NOT NULL, long_fee_rate TEXT NOT NULL,
        short_fee_rate TEXT NOT NULL, ranking_run_at TEXT NOT NULL,
        closed_trades_count INTEGER NOT NULL DEFAULT 0);
    """)
    account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
    if account is None:
        db.execute("INSERT INTO account(id,initial_nav,cash) VALUES(1,?,?)", (fmt(initial_nav), fmt(initial_nav)))
    elif dec(account["initial_nav"], "stored initial NAV", positive=True) != initial_nav:
        db.close()
        raise PaperError("Initial NAV is immutable after the paper database is initialized")
    return db


def parse_book(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PaperError("Bitget depth response is not an object")
    result: dict[str, Any] = {}
    stamp = dec(payload.get("ts"), "order-book timestamp", positive=True)
    if stamp != stamp.to_integral_value():
        raise PaperError("Order-book timestamp is not an integer")
    result["timestamp_ms"] = int(stamp)
    for side in ("bids", "asks"):
        raw = payload.get(side)
        if not isinstance(raw, list) or not raw:
            raise PaperError(f"Bitget order book has no {side}")
        levels = []
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                raise PaperError(f"Malformed {side} level")
            levels.append((dec(row[0], "depth price", positive=True), dec(row[1], "depth quantity", positive=True)))
        if side == "bids" and any(a[0] < b[0] for a, b in zip(levels, levels[1:])):
            raise PaperError("Bid depth is not sorted descending")
        if side == "asks" and any(a[0] > b[0] for a, b in zip(levels, levels[1:])):
            raise PaperError("Ask depth is not sorted ascending")
        result[side] = levels
    if result["bids"][0][0] >= result["asks"][0][0]:
        raise PaperError("Order book is crossed or locked")
    return result


def vwap_for_qty(levels: list[tuple[Decimal, Decimal]], qty: Decimal) -> Decimal:
    if qty <= 0:
        raise PaperError("Simulated quantity must be positive")
    remaining, cost = qty, D("0")
    for price, available in levels:
        take = min(remaining, available)
        cost += take * price
        remaining -= take
        if remaining <= D("0.000000000000000001"):
            break
    if remaining > D("0.000000000000000001"):
        raise PaperError("Insufficient visible order-book depth for the full simulated fill")
    return cost / qty


def lot_rules(market: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal | None, Decimal | None]:
    min_qty = dec(market.get("minOrderQty"), f"{market.get('symbol')} minOrderQty", positive=True)
    raw_step = market.get("quantityMultiplier")
    if raw_step in (None, "", "0", 0):
        precision = market.get("quantityPrecision")
        if precision in (None, ""):
            raise PaperError(f"{market.get('symbol')}: quantity step and precision are missing")
        try:
            raw_step = D("1").scaleb(-int(precision))
        except (ValueError, TypeError) as exc:
            raise PaperError(f"{market.get('symbol')}: invalid quantityPrecision") from exc
    step = dec(raw_step, f"{market.get('symbol')} quantity step", positive=True)
    max_raw = market.get("maxMarketOrderQty")
    maximum = dec(max_raw, f"{market.get('symbol')} max order quantity", positive=True) if max_raw not in (None, "", "0", 0) else None
    min_amount_raw = market.get("minOrderAmount")
    min_amount = dec(min_amount_raw, f"{market.get('symbol')} minimum order amount", positive=True) if min_amount_raw not in (None, "", "0", 0) else None
    return min_qty, step, maximum, min_amount


def size_for_target_notional(target: Decimal, levels: list[tuple[Decimal, Decimal]],
                             market: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    minimum, step, maximum, min_amount = lot_rules(market)
    qty = (target / levels[0][0] / step).to_integral_value(rounding=ROUND_DOWN) * step
    for _ in range(80):
        if qty < minimum:
            raise PaperError(f"{market['symbol']}: target notional is below minimum quantity")
        if maximum is not None and qty > maximum:
            raise PaperError(f"{market['symbol']}: target quantity exceeds Bitget max market order quantity")
        price = vwap_for_qty(levels, qty)
        notional = price * qty
        if notional <= target:
            if min_amount is not None and notional < min_amount:
                raise PaperError(f"{market['symbol']}: simulated order is below minimum notional")
            return qty, price, notional
        revised = (target / price / step).to_integral_value(rounding=ROUND_DOWN) * step
        if revised >= qty:
            revised = qty - step
        qty = revised
    raise PaperError(f"{market['symbol']}: unable to size below target notional")


def fee_rate_for(market: dict[str, Any], override: Decimal | None) -> Decimal:
    rate = override if override is not None else dec(market.get("takerFeeRate"), f"{market['symbol']} takerFeeRate", positive=True)
    if rate < 0 or rate >= 1:
        raise PaperError(f"{market['symbol']}: invalid taker fee rate")
    return rate


def _insert_ledger(db: sqlite3.Connection, *, event_key: str, date: str, time_ms: int,
                   trade_id: int | None, leg: str, event_type: str, symbol: str,
                   side: str, qty: Decimal, price: Decimal | None = None,
                   notional: Decimal = D("0"), fee_rate: Decimal | None = None,
                   fee: Decimal = D("0"), gross_pnl: Decimal = D("0"),
                   funding_rate: Decimal | None = None, funding: Decimal = D("0"),
                   detail: dict[str, Any] | None = None) -> None:
    db.execute("""INSERT INTO ledger
      (event_key,snapshot_date,event_time_ms,trade_id,leg,event_type,symbol,side,qty,price,
       notional,fee_rate,fee,gross_pnl,funding_rate,funding_cashflow,detail_json)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (event_key,date,time_ms,trade_id,leg,event_type,symbol,side,fmt(qty),
       fmt(price) if price is not None else None,fmt(notional),
       fmt(fee_rate) if fee_rate is not None else None,fmt(fee),fmt(gross_pnl),
       fmt(funding_rate) if funding_rate is not None else None,fmt(funding),
       json.dumps(detail or {},sort_keys=True)))


def run_snapshot(report: dict[str, Any], db_path: str | Path, *,
                 depth_fetch: Callable[[str], dict[str, Any]],
                 funding_fetch: Callable[[str, int, int], list[dict[str, Any]]],
                 funding_mark_fetch: Callable[[str, int], Decimal],
                 initial_nav: Decimal = D("10000"),
                 fee_override: Decimal | None = None) -> dict[str, Any]:
    if report.get("gates", {}).get("ranking") != "PASS":
        raise PaperError("Ranking gate is not PASS; paper state was not changed")
    as_of = report.get("as_of_server_time_ms")
    if isinstance(as_of, bool) or not isinstance(as_of, int) or as_of <= 0:
        raise PaperError("Ranking report has no valid Bitget server timestamp")
    long_lead = report.get("leaders", {}).get("long_stock_top_gainer")
    short_lead = report.get("leaders", {}).get("short_crypto_bottom_loser")
    if not isinstance(long_lead, dict) or not isinstance(short_lead, dict):
        raise PaperError("Both selected leaders are required")
    long_symbol, short_symbol = long_lead.get("symbol"), short_lead.get("symbol")
    if not isinstance(long_symbol, str) or not isinstance(short_symbol, str) or long_symbol == short_symbol:
        raise PaperError("Selected symbols are invalid or duplicated")
    date = utc_date(as_of)
    initial_nav = dec(initial_nav, "initial NAV", positive=True)
    if fee_override is not None:
        fee_override = dec(fee_override, "fee override")
        if fee_override < 0 or fee_override >= 1:
            raise PaperError("Fee override must be in [0,1)")

    db = init_db(db_path, initial_nav)
    try:
        if db.execute("SELECT 1 FROM daily_snapshots WHERE snapshot_date=?", (date,)).fetchone():
            return {"status": "ALREADY_PROCESSED", "updated": False, "snapshot_date": date,
                    "message": "A paper snapshot already exists for this UTC date; no duplicate trades were created."}

        old_positions = {r["leg"]: dict(r) for r in db.execute("SELECT * FROM positions")}
        markets = {m.get("symbol"): m for m in report.get("market_results", []) if isinstance(m, dict)}
        for symbol, expected in ((long_symbol, "stock"), (short_symbol, "crypto")):
            market = markets.get(symbol)
            if not isinstance(market, dict):
                raise PaperError(f"{symbol}: selected contract is not in validated market results")
            if (market.get("symbolType") or "").casefold() != expected or market.get("status") != "online" or market.get("type") != "perpetual":
                raise PaperError(f"{symbol}: selected contract does not match the exact active {expected} perpetual taxonomy")
            if market.get("category") != "USDT-FUTURES" or market.get("quoteCoin") != "USDT":
                raise PaperError(f"{symbol}: selected contract is not USDT-settled")

        symbols_to_fetch = {long_symbol, short_symbol} | {p["symbol"] for p in old_positions.values()}
        books = {symbol: parse_book(depth_fetch(symbol)) for symbol in sorted(symbols_to_fetch)}
        for symbol, book in books.items():
            if abs(as_of - int(book["timestamp_ms"])) > 900_000:
                raise PaperError(f"{symbol}: public order book is stale relative to Bitget server time")

        funding_by_leg: dict[str, list[dict[str, Any]]] = {}
        for leg, position in old_positions.items():
            start_ms = int(position["last_funding_ms"])
            events = funding_fetch(position["symbol"], start_ms, as_of)
            if not isinstance(events, list):
                raise PaperError(f"{position['symbol']}: funding endpoint did not return an array")
            checked = []
            for event in events:
                if not isinstance(event, dict) or event.get("symbol") != position["symbol"]:
                    raise PaperError(f"{position['symbol']}: malformed funding event")
                event_ms = int(dec(event.get("fundingTime"), "fundingTime", positive=True))
                if start_ms < event_ms <= as_of:
                    rate = dec(event.get("fundingRate"), "fundingRate")
                    mark = dec(funding_mark_fetch(position["symbol"], event_ms), "funding mark price", positive=True)
                    checked.append({"symbol": position["symbol"], "fundingTime": event_ms,
                                    "fundingRate": rate, "markPrice": mark})
            funding_by_leg[leg] = sorted(checked, key=lambda x: x["fundingTime"])

        # Validate new leg execution sizing before opening the SQLite transaction.
        long_market, short_market = markets[long_symbol], markets[short_symbol]
        long_fee = fee_rate_for(long_market, fee_override)
        short_fee = fee_rate_for(short_market, fee_override)

        db.execute("BEGIN IMMEDIATE")
        account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
        cash = dec(account["cash"], "account cash")
        realized = dec(account["realized_gross_pnl"], "realized P&L")
        fees = dec(account["fees_cumulative"], "cumulative fees")
        funding_total = dec(account["funding_cumulative"], "cumulative funding")
        previous = db.execute("SELECT snapshot_date,as_of_ms,nav FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        previous_nav = dec(previous["nav"], "previous NAV", positive=True) if previous else None
        hours_since = ((as_of - int(previous["as_of_ms"])) / 3_600_000) if previous else None
        count_before = db.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED'").fetchone()[0]

        # Accrue every observed funding settlement for the existing positions.
        for leg, position in old_positions.items():
            qty = dec(position["qty"], "position quantity", positive=True)
            trade_id = int(position["trade_id"])
            for event in funding_by_leg[leg]:
                rate, mark = event["fundingRate"], event["markPrice"]
                direction = D("1") if leg == "LONG" else D("-1")
                payment = -direction * qty * mark * rate
                cash += payment
                funding_total += payment
                _insert_ledger(db, event_key=f"{trade_id}:funding:{event['fundingTime']}",
                    date=date, time_ms=event["fundingTime"], trade_id=trade_id, leg=leg,
                    event_type="FUNDING", symbol=position["symbol"], side=leg, qty=qty,
                    price=mark, notional=qty*mark, funding_rate=rate, funding=payment,
                    detail={"funding_mark_source": "Bitget historical 1m mark-price candle"})
                db.execute("UPDATE trades SET funding_pnl=? WHERE trade_id=?",
                    (fmt(dec(db.execute("SELECT funding_pnl FROM trades WHERE trade_id=?", (trade_id,)).fetchone()[0], "trade funding") + payment), trade_id))

        # Close both old legs at the visible opposing side of the public book.
        for leg in ("LONG", "SHORT"):
            position = old_positions.get(leg)
            if not position:
                continue
            qty = dec(position["qty"], "position quantity", positive=True)
            entry = dec(position["entry_price"], "entry price", positive=True)
            trade_id = int(position["trade_id"])
            sign = D("1") if leg == "LONG" else D("-1")
            close_levels = books[position["symbol"]]["bids" if leg == "LONG" else "asks"]
            close_price = vwap_for_qty(close_levels, qty)
            close_notional = close_price * qty
            # Reuse the rate captured at entry unless an explicit override is configured.
            exit_fee_rate = fee_override if fee_override is not None else dec(position["entry_fee_rate"], "stored entry fee rate")
            exit_fee = close_notional * exit_fee_rate
            gross_pnl = (close_price - entry) * qty * sign
            entry_fee = dec(position["entry_fee"], "entry fee")
            trade = db.execute("SELECT funding_pnl,entry_notional FROM trades WHERE trade_id=?", (trade_id,)).fetchone()
            trade_funding = dec(trade["funding_pnl"], "trade funding")
            entry_notional = dec(trade["entry_notional"], "entry notional", positive=True)
            net_pnl = gross_pnl - entry_fee - exit_fee + trade_funding
            cash += gross_pnl - exit_fee
            realized += gross_pnl
            fees += exit_fee
            trade_roi = net_pnl / entry_notional * D("100")
            db.execute("""UPDATE trades SET exit_time_ms=?,exit_date=?,exit_price=?,exit_notional=?,
                exit_fee_rate=?,exit_fee=?,gross_pnl=?,net_pnl=?,trade_roi_pct=?,status='CLOSED'
                WHERE trade_id=?""",
                (as_of,date,fmt(close_price),fmt(close_notional),fmt(exit_fee_rate),fmt(exit_fee),
                 fmt(gross_pnl),fmt(net_pnl),fmt(trade_roi),trade_id))
            _insert_ledger(db, event_key=f"{date}:{leg}:close:{trade_id}", date=date,
                time_ms=as_of, trade_id=trade_id, leg=leg, event_type="CLOSE",
                symbol=position["symbol"], side="SELL" if leg == "LONG" else "BUY",
                qty=qty, price=close_price, notional=close_notional,
                fee_rate=exit_fee_rate, fee=exit_fee, gross_pnl=gross_pnl,
                detail={"fill_model": "public order-book depth VWAP"})
            db.execute("DELETE FROM positions WHERE leg=?", (leg,))

        target_notional = cash / (D("1") + long_fee + short_fee)
        new_positions: dict[str, dict[str, Any]] = {}
        for leg, symbol, market, fee_rate in (
            ("LONG", long_symbol, long_market, long_fee),
            ("SHORT", short_symbol, short_market, short_fee),
        ):
            book = books[symbol]
            entry_levels = book["asks"] if leg == "LONG" else book["bids"]
            qty, entry_price, entry_notional = size_for_target_notional(target_notional, entry_levels, market)
            entry_fee = entry_notional * fee_rate
            cash -= entry_fee
            fees += entry_fee
            cursor = db.execute("""INSERT INTO trades
                (leg,symbol,entry_time_ms,entry_date,entry_qty,entry_price,entry_notional,
                 entry_fee_rate,entry_fee,status) VALUES(?,?,?,?,?,?,?,?,?,'OPEN')""",
                (leg,symbol,as_of,date,fmt(qty),fmt(entry_price),fmt(entry_notional),fmt(fee_rate),fmt(entry_fee)))
            trade_id = int(cursor.lastrowid)
            db.execute("INSERT INTO positions(leg,trade_id,symbol,qty,entry_price,entry_notional,entry_fee_rate,entry_fee,last_funding_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                (leg,trade_id,symbol,fmt(qty),fmt(entry_price),fmt(entry_notional),fmt(fee_rate),fmt(entry_fee),as_of))
            _insert_ledger(db,event_key=f"{date}:{leg}:open:{trade_id}",date=date,time_ms=as_of,
                trade_id=trade_id,leg=leg,event_type="OPEN",symbol=symbol,
                side="BUY" if leg=="LONG" else "SELL",qty=qty,price=entry_price,
                notional=entry_notional,fee_rate=fee_rate,fee=entry_fee,
                detail={"fill_model":"public order-book depth VWAP","target_notional_usdt":fmt(target_notional)})
            new_positions[leg] = {"trade_id":trade_id,"symbol":symbol,"qty":qty,
                "entry_price":entry_price,"entry_notional":entry_notional,
                "entry_fee":entry_fee,"fee_rate":fee_rate,
                "mark_price":dec(market["markPrice"],f"{symbol} markPrice",positive=True)}

        unrealized = D("0")
        gross_exposure = D("0")
        position_json: dict[str, str] = {}
        for leg, position in new_positions.items():
            sign = D("1") if leg == "LONG" else D("-1")
            mark = position["mark_price"]
            u_pnl = (mark-position["entry_price"])*position["qty"]*sign
            unrealized += u_pnl
            gross_exposure += mark*position["qty"]
            open_net_pnl = u_pnl-position["entry_fee"]
            trade_roi_to_date = open_net_pnl/position["entry_notional"]*D("100")
            record = {"trade_id":position["trade_id"],"leg":leg,"symbol":position["symbol"],
                "quantity":fmt(position["qty"]),"entry_price":fmt(position["entry_price"]),
                "mark_price":fmt(mark),"entry_notional_usdt":fmt(position["entry_notional"]),
                "unrealized_pnl_usdt":fmt(u_pnl),"entry_fee_usdt":fmt(position["entry_fee"]),
                "funding_pnl_usdt":"0","net_open_pnl_usdt":fmt(open_net_pnl),
                "trade_roi_to_date_pct":fmt(trade_roi_to_date),"entry_time_utc":utc_iso(as_of)}
            position_json[leg]=json.dumps(record,sort_keys=True)

        nav = cash + unrealized
        if nav <= 0:
            raise PaperError("Portfolio NAV is non-positive; refusing to commit snapshot")
        daily_return = (nav/previous_nav-D("1"))*D("100") if previous_nav else None
        total_return = (nav/initial_nav-D("1"))*D("100")
        closed_count = db.execute("SELECT COUNT(*) FROM trades WHERE status='CLOSED' AND exit_date=?", (date,)).fetchone()[0]
        db.execute("""INSERT INTO daily_snapshots(
          snapshot_date,as_of_ms,long_symbol,short_symbol,nav,cash,unrealized_pnl,
          realized_gross_pnl_cumulative,fees_cumulative,funding_cumulative,gross_exposure,
          daily_return_pct,hours_since_previous,total_return_pct,long_position_json,
          short_position_json,long_fee_rate,short_fee_rate,ranking_run_at,closed_trades_count)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
          (date,as_of,long_symbol,short_symbol,fmt(nav),fmt(cash),fmt(unrealized),fmt(realized),
           fmt(fees),fmt(funding_total),fmt(gross_exposure),fmt(daily_return) if daily_return is not None else None,
           hours_since,fmt(total_return),position_json["LONG"],position_json["SHORT"],
           fmt(long_fee),fmt(short_fee),report.get("run_at_utc",utc_iso(as_of)),closed_count))
        db.execute("UPDATE account SET cash=?,realized_gross_pnl=?,fees_cumulative=?,funding_cumulative=? WHERE id=1",
                   (fmt(cash),fmt(realized),fmt(fees),fmt(funding_total)))
        db.execute("COMMIT")

        closed_trades = [dict(r) for r in db.execute(
            "SELECT trade_id,leg,symbol,entry_date,exit_date,entry_notional,gross_pnl,entry_fee,exit_fee,funding_pnl,net_pnl,trade_roi_pct FROM trades WHERE status='CLOSED' AND exit_date=? ORDER BY trade_id", (date,))]
        return {"status":"UPDATED","updated":True,"snapshot_date":date,
            "snapshot":{"nav_usdt":fmt(nav),"cash_usdt":fmt(cash),"unrealized_pnl_usdt":fmt(unrealized),
                "daily_return_pct":fmt(daily_return) if daily_return is not None else None,
                "hours_since_previous_snapshot":round(hours_since,3) if hours_since is not None else None,
                "total_return_pct":fmt(total_return),"realized_gross_pnl_cumulative_usdt":fmt(realized),
                "fees_cumulative_usdt":fmt(fees),"funding_cumulative_usdt":fmt(funding_total),
                "gross_exposure_usdt":fmt(gross_exposure),"gross_exposure_to_nav":fmt(gross_exposure/nav),
                "open_positions":{leg:json.loads(raw) for leg,raw in position_json.items()},
                "closed_trades_today":closed_trades}}
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


def trade_report(db_path: str | Path) -> dict[str, Any]:
    """Return inspectable latest snapshot, open trades, and closed-trade ROI history."""
    path = Path(db_path)
    if not path.exists():
        return {"status":"NO_PORTFOLIO_YET","initial_nav_usdt":None,"latest_snapshot":None,
                "open_trades":[],"closed_trades":[]}
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    try:
        account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
        snapshot = db.execute("SELECT * FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
        open_rows = db.execute("""SELECT p.*,t.entry_notional,t.entry_fee,t.funding_pnl,t.entry_date
                                  FROM positions p JOIN trades t ON t.trade_id=p.trade_id ORDER BY p.leg""").fetchall()
        closed = db.execute("""SELECT trade_id,leg,symbol,entry_date,exit_date,entry_notional,
            gross_pnl,entry_fee,exit_fee,funding_pnl,net_pnl,trade_roi_pct
            FROM trades WHERE status='CLOSED' ORDER BY trade_id DESC LIMIT 100""").fetchall()
        snap_dict = dict(snapshot) if snapshot else None
        if snap_dict:
            snap_dict["long_position"] = json.loads(snap_dict.pop("long_position_json"))
            snap_dict["short_position"] = json.loads(snap_dict.pop("short_position_json"))
        return {"status":"OK","initial_nav_usdt":account["initial_nav"] if account else None,
            "latest_snapshot":snap_dict,"open_trades":[dict(x) for x in open_rows],
            "closed_trades":[dict(x) for x in closed],
            "portfolio_total_roi_pct":snap_dict.get("total_return_pct") if snap_dict else None}
    finally:
        db.close()
