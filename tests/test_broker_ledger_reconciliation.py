"""Closed-loop broker ledger reconciliation tests."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from datetime import datetime

from data.models import Quote
from runtime.strategy_kill_switch import (
    clear_strategy_kill_switch_for_tests,
    is_strategy_kill_active,
    trip_master_strategy_kill_switch,
)
from runtime.trade_manager import (
    DRIFT_ADVISORY_PCT,
    DRIFT_FATAL_PCT,
    PreflightVerdict,
    fetch_broker_ledger_sync,
    handle_critical_state_mismatch,
    mark_triage_closed_on_broker_anomaly,
    preflight_trailing_cycle,
)


def _broker_row(
    deal_id: str = "DEAL1",
    epic: str = "CS.D.EURUSD.CFD.IP",
    upl: float = 10.0,
) -> dict:
    return {
        "position": {
            "dealId": deal_id,
            "direction": "BUY",
            "level": 1.16,
            "size": 1.0,
            "upl": upl,
            "stopLevel": 1.15,
            "limitLevel": 1.17,
        },
        "market": {"epic": epic},
    }


class BrokerLedgerReconciliationTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_strategy_kill_switch_for_tests()

    def tearDown(self) -> None:
        clear_strategy_kill_switch_for_tests()

    def test_fetch_broker_ledger_indexes_by_deal_id(self) -> None:
        rest = MagicMock()
        rest.open_positions.return_value = [_broker_row("A1"), _broker_row("B2")]
        ledger = fetch_broker_ledger_sync(rest)
        self.assertEqual(set(ledger.keys()), {"A1", "B2"})
        self.assertEqual(ledger["A1"].epic, "CS.D.EURUSD.CFD.IP")

    def test_missing_deal_trips_kill_switch(self) -> None:
        rest = MagicMock()
        rest.open_positions.return_value = []
        flushed: list[str] = []

        def _flush(**kw: object) -> None:
            flushed.append(str(kw.get("deal_id")))

        with patch(
            "runtime.strategy_kill_switch.trip_master_strategy_kill_switch",
            wraps=trip_master_strategy_kill_switch,
        ) as trip:
            result = preflight_trailing_cycle(
                rest_client=rest,
                deal_id="GHOST",
                epic="CS.D.EURUSD.CFD.IP",
                side="BUY",
                entry=1.16,
                size=1.0,
                quote=Quote(datetime.utcnow(), 1.161, 1.162),
                flush_callback=_flush,
                trade_id=1,
            )
            trip.assert_called_once()
        self.assertEqual(result.verdict, PreflightVerdict.MISSING_ON_BROKER)
        self.assertTrue(is_strategy_kill_active())
        self.assertEqual(flushed, ["GHOST"])

    def test_pnl_drift_advisory_only(self) -> None:
        rest = MagicMock()
        rest.open_positions.return_value = [_broker_row(upl=100.0)]
        quote = Quote(datetime.utcnow(), 1.161, 1.162)
        with patch(
            "runtime.trade_manager.unrealized_from_quote",
            return_value=(1.161, 5.0, 108.0),
        ):
            result = preflight_trailing_cycle(
                rest_client=rest,
                deal_id="DEAL1",
                epic="CS.D.EURUSD.CFD.IP",
                side="BUY",
                entry=1.16,
                size=1.0,
                quote=quote,
            )
        self.assertEqual(result.verdict, PreflightVerdict.DRIFT_ADVISORY)
        self.assertGreater(result.drift_pct or 0, DRIFT_ADVISORY_PCT)
        self.assertFalse(is_strategy_kill_active())

    def test_pnl_drift_fatal_trips_kill_switch(self) -> None:
        rest = MagicMock()
        rest.open_positions.return_value = [_broker_row(upl=100.0)]
        quote = Quote(datetime.utcnow(), 1.161, 1.162)
        with patch(
            "runtime.trade_manager.unrealized_from_quote",
            return_value=(1.161, 5.0, 200.0),
        ), patch(
            "system.telegram_notifier.send_critical_alert",
            return_value=True,
        ):
            result = preflight_trailing_cycle(
                rest_client=rest,
                deal_id="DEAL1",
                epic="CS.D.EURUSD.CFD.IP",
                side="BUY",
                entry=1.16,
                size=1.0,
                quote=quote,
            )
        self.assertEqual(result.verdict, PreflightVerdict.DRIFT_FATAL)
        self.assertGreater(result.drift_pct or 0, DRIFT_FATAL_PCT)
        self.assertTrue(is_strategy_kill_active())

    def test_triage_mark_closed_on_broker_anomaly(self) -> None:
        from analytics.triage_db import connect_triage_sqlite

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "triage.db"
            conn = connect_triage_sqlite(db)
            conn.execute(
                """
                CREATE TABLE production_orders (
                    id INTEGER PRIMARY KEY,
                    deal_reference TEXT UNIQUE,
                    deal_id TEXT,
                    epic TEXT,
                    direction TEXT,
                    size REAL,
                    status TEXT,
                    broker_payload TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO production_orders
                (deal_reference, deal_id, epic, direction, size, status, broker_payload, created_at)
                VALUES ('R1', 'DZ1', 'CS.D.EURUSD.CFD.IP', 'BUY', 1.0, 'CONFIRMED', '{}', 'now')
                """
            )
            conn.commit()
            conn.close()
            with patch.dict(
                "os.environ", {"IG_TRIAGE_DB": str(db)}, clear=False
            ):
                mark_triage_closed_on_broker_anomaly("DZ1")
                row = connect_triage_sqlite(db).execute(
                    "SELECT status FROM production_orders WHERE deal_id='DZ1'"
                ).fetchone()
            self.assertEqual(row[0], "CLOSED_ON_BROKER_ANOMALY")


if __name__ == "__main__":
    unittest.main()
