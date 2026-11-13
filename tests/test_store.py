from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from stocks_vs_memes.store import PaperStore


class StoreTests(unittest.TestCase):
    def test_open_day_survives_reopen_and_close_report_updates_equity(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = PaperStore(path)
            payload = {"starting_equity_usdt": 1000, "positions": {"long": {"contract": "A"}}}
            store.create_day("2026-01-01", 1767225600, 1767312000, payload)
            store.close()
            store = PaperStore(path)
            open_day = store.get_open_day()
            self.assertEqual(open_day["utc_date"], "2026-01-01")
            self.assertEqual(open_day["payload"]["positions"]["long"]["contract"], "A")
            store.save_report("2026-01-01", {"ending_equity_usdt": 1012.5, "net_pnl_usdt": 12.5})
            self.assertIsNone(store.get_open_day())
            self.assertEqual(store.latest_ending_equity(500), 1012.5)
            store.close()

    def test_skip_status_persists_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            store = PaperStore(Path(temp) / "state.sqlite3")
            store.create_day("2026-01-02", 1, 2, {"skip_reason": "missing meme category"})
            store.update_status("2026-01-02", "SKIPPED", "missing meme category")
            row = store.get_day("2026-01-02")
            self.assertEqual(row["status"], "SKIPPED")
            self.assertEqual(row["last_error"], "missing meme category")
            store.close()


if __name__ == "__main__":
    unittest.main()
