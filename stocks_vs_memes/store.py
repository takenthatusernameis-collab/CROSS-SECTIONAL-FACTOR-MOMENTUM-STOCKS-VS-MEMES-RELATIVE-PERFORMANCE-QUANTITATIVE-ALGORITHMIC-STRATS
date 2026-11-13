"""Local SQLite persistence for restart-safe paper sessions and minute equity data."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any

class PaperStore:
    def __init__(self, database_path: str | Path):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        self.connection.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS days (
            utc_date TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            entry_ts INTEGER,
            exit_ts INTEGER,
            payload_json TEXT NOT NULL,
            report_json TEXT,
            last_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS marks (
            utc_date TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            long_mark REAL NOT NULL,
            short_mark REAL NOT NULL,
            ratio_drift REAL NOT NULL,
            equity REAL NOT NULL,
            PRIMARY KEY (utc_date, timestamp),
            FOREIGN KEY (utc_date) REFERENCES days(utc_date)
        );
        CREATE TABLE IF NOT EXISTS funding (
            utc_date TEXT NOT NULL,
            contract TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            rate REAL NOT NULL,
            mark_price REAL NOT NULL,
            cashflow REAL NOT NULL,
            PRIMARY KEY (utc_date, contract, timestamp),
            FOREIGN KEY (utc_date) REFERENCES days(utc_date)
        );
        CREATE INDEX IF NOT EXISTS idx_days_status ON days(status);
        """)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def create_day(self, utc_date: str, entry_ts: int, exit_ts: int, payload: dict[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO days(utc_date,status,entry_ts,exit_ts,payload_json) VALUES(?,?,?,?,?)",
            (utc_date, "OPEN", entry_ts, exit_ts, json.dumps(payload, sort_keys=True)),
        )
        self.connection.commit()

    def update_payload(self, utc_date: str, payload: dict[str, Any]) -> None:
        self.connection.execute("UPDATE days SET payload_json=?,updated_at=CURRENT_TIMESTAMP WHERE utc_date=?",
            (json.dumps(payload, sort_keys=True), utc_date))
        self.connection.commit()

    def update_status(self, utc_date: str, status: str, error: str | None = None) -> None:
        self.connection.execute("UPDATE days SET status=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE utc_date=?",
            (status, error, utc_date))
        self.connection.commit()

    def save_report(self, utc_date: str, report: dict[str, Any]) -> None:
        self.connection.execute("UPDATE days SET status='CLOSED',report_json=?,last_error=NULL,updated_at=CURRENT_TIMESTAMP WHERE utc_date=?",
            (json.dumps(report, sort_keys=True), utc_date))
        self.connection.commit()

    def add_mark(self, utc_date: str, timestamp: int, long_mark: float, short_mark: float,
                 ratio_drift: float, equity: float) -> None:
        self.connection.execute("INSERT OR REPLACE INTO marks VALUES(?,?,?,?,?,?)",
            (utc_date, timestamp, long_mark, short_mark, ratio_drift, equity))
        self.connection.commit()

    def add_funding(self, utc_date: str, contract: str, timestamp: int, rate: float,
                    mark_price: float, cashflow: float) -> None:
        self.connection.execute("INSERT OR REPLACE INTO funding VALUES(?,?,?,?,?,?)",
            (utc_date, contract, timestamp, rate, mark_price, cashflow))
        self.connection.commit()

    def get_day(self, utc_date: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM days WHERE utc_date=?", (utc_date,)).fetchone()
        return self._decode_day(row) if row else None

    def get_open_day(self) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM days WHERE status IN ('OPEN','CLOSE_PENDING') ORDER BY entry_ts LIMIT 1").fetchone()
        return self._decode_day(row) if row else None

    def list_days(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM days ORDER BY utc_date DESC LIMIT ?", (int(limit),)).fetchall()
        return [self._decode_day(r) for r in rows]

    def marks(self, utc_date: str) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM marks WHERE utc_date=? ORDER BY timestamp", (utc_date,)).fetchall()
        return [dict(r) for r in rows]

    def latest_ending_equity(self, default: float) -> float:
        row = self.connection.execute("SELECT report_json FROM days WHERE status='CLOSED' AND report_json IS NOT NULL ORDER BY exit_ts DESC LIMIT 1").fetchone()
        if not row:
            return float(default)
        report = json.loads(row[0])
        return float(report.get("ending_equity_usdt", default))

    def _decode_day(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        if result.get("report_json"):
            result["report"] = json.loads(result.pop("report_json"))
        else:
            result.pop("report_json", None)
        return result
