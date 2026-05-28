"""Tests for trade autopsy writer — non-blocking, guarded writes."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import trading.trade_autopsy as autopsy
from trading.trade_autopsy import write_trade_autopsy


class TradeAutopsyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.autopsy_dir = base / "autopsy"
        self.jsonl_path = base / "trade_autopsy.jsonl"
        autopsy._AUTOPSY_DIR = self.autopsy_dir
        autopsy._JSONL = self.jsonl_path

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _wait_for_writes(self, deal_id: str, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        json_path = self.autopsy_dir / f"{deal_id}.json"
        while time.monotonic() < deadline:
            if json_path.is_file() and self.jsonl_path.is_file():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for autopsy writes for {deal_id}")

    def test_writes_per_deal_json(self) -> None:
        payload = {"setup_key": "BUY|bull", "pnl": 12.5}
        write_trade_autopsy("D1", payload, ig_pnl_currency_confirmed=True, source="agent")
        self._wait_for_writes("D1")
        data = json.loads((self.autopsy_dir / "D1.json").read_text(encoding="utf-8"))
        self.assertEqual(data["setup_key"], "BUY|bull")

    def test_appends_jsonl(self) -> None:
        payload = {"result": "WIN"}
        write_trade_autopsy("D2", payload, ig_pnl_currency_confirmed=True, source="agent")
        self._wait_for_writes("D2")
        lines = self.jsonl_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertEqual(row["deal_id"], "D2")
        self.assertEqual(row["result"], "WIN")

    def test_skip_pnl_not_confirmed(self) -> None:
        write_trade_autopsy(
            "D3",
            {"pnl": 1.0},
            ig_pnl_currency_confirmed=False,
            source="agent",
        )
        time.sleep(0.15)
        self.assertFalse((self.autopsy_dir / "D3.json").exists())
        self.assertFalse(self.jsonl_path.exists())

    def test_skip_source_sim(self) -> None:
        write_trade_autopsy(
            "D4",
            {"pnl": 1.0},
            ig_pnl_currency_confirmed=True,
            source="sim",
        )
        time.sleep(0.15)
        self.assertFalse((self.autopsy_dir / "D4.json").exists())
        self.assertFalse(self.jsonl_path.exists())

    def test_fast_return_daemon_thread(self) -> None:
        started = time.monotonic()
        write_trade_autopsy(
            "D5",
            {"pnl": 2.0},
            ig_pnl_currency_confirmed=True,
            source="agent",
        )
        elapsed_ms = (time.monotonic() - started) * 1000.0
        self.assertLess(elapsed_ms, 50.0)


if __name__ == "__main__":
    unittest.main()
