"""CLI for the boundary-triggered Bybit + CMC Stocks-vs-Memes paper trader."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from .daily_trader import BybitDailyTrader

def load_config(path: str) -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    if str(data.get("exchange", "")).casefold() != "bybit":
        raise ValueError("The active workflow is pinned to Bybit USDT linear perpetuals")
    if str(data.get("classification_provider", "")).casefold() != "coinmarketcap_keyless_public_api":
        raise ValueError("The active Meme taxonomy source must be CoinMarketCap's keyless public Categories API")
    return data

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="svm-paper",
        description="UTC-boundary Stocks-vs-Memes paper simulator (Bybit prices + CoinMarketCap category taxonomy)")
    parser.add_argument("--config", default="config/strategy.json", help="Path to strategy JSON config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="Read-only live verification of Bybit stock perpetuals and CMC Memes mappings")
    sub.add_parser("universe", help="Alias for the strict live source preflight")
    run = sub.add_parser("run", help="Process one UTC daily open or run the boundary-only scheduler")
    run.add_argument("--loop", action="store_true", help="Run the standalone always-on UTC daily-boundary scheduler")
    run.add_argument("--scheduled-once", action="store_true", help="For GitHub Actions: wait for the next UTC midnight if launched shortly before it, then process exactly one daily boundary")
    sub.add_parser("status", help="Show recent local daily sessions")
    report = sub.add_parser("report", help="Print one saved daily report")
    report.add_argument("--date", default=None, help="UTC date YYYY-MM-DD; defaults to latest completed day")
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    trader = None
    try:
        trader = BybitDailyTrader(load_config(args.config))
        if args.command in ("preflight", "universe"):
            result = trader.live_preflight()
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0 if result.get("passed") else 1
        if args.command == "run":
            if args.loop and args.scheduled_once:
                raise ValueError("Choose only one of --loop or --scheduled-once")
            if args.loop:
                trader.run_loop()
            elif args.scheduled_once:
                print(trader.run_scheduled_once())
            else:
                print(trader.process_once())
            return 0
        if args.command == "status":
            days = trader.status()
            if not days:
                print("No local paper sessions recorded yet.")
            for day in days:
                report = day.get("report") or {}
                roi, dd = report.get("ratio_roi_pct"), report.get("daily_ratio_drawdown_pct")
                suffix = f" | ratio ROI {roi:+.4f}% | ratio DD {dd:.4f}%" if roi is not None and dd is not None else ""
                print(f"{day['utc_date']}  {day['status']}{suffix}")
                if day.get("last_error"):
                    print(f"  reason: {day['last_error']}")
            return 0
        if args.command == "report":
            days = trader.status()
            found = next((x for x in days if x.get("report") and (args.date is None or x["utc_date"] == args.date)), None)
            if found is None:
                print("No matching completed report found in the local database.")
                return 1
            print(json.dumps(found["report"], indent=2, sort_keys=True))
            return 0
        return 0
    except KeyboardInterrupt:
        print("Stopped. Persisted paper state remains local; no live orders were placed.")
        return 130
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if trader is not None:
            trader.store.close()

if __name__ == "__main__":
    raise SystemExit(main())
