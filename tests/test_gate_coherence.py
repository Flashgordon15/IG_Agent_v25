"""Gate coherence audit and trade risk resolution."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.learning_store import LearningStore
from data.models import TradeRecord
from execution.trade_risk import (
    resolve_stop_price,
    risk_gbp_from_row,
)
from system.gate_coherence import (
    audit_market,
    audit_trading_readiness,
    repair_corrupt_trade_rows,
)


def _cfg():
    c = MagicMock()
    c.get = lambda k, d=None: (
        {
            "instruments": {
                "japan": {
                    "epic": "IX.D.NIKKEI.IFM.IP",
                    "stop_distance_points": 45,
                    "ig_point_value_gbp": 5.13,
                }
            }
        }
        if k == "instruments"
        else d
    )
    c.as_dict = lambda: {"instruments": c.get("instruments")}
    return c


class TradeRiskTests(unittest.TestCase):
    def test_resolve_stop_when_level_missing(self) -> None:
        stop = resolve_stop_price(
            entry=63663.6,
            side="SELL",
            stop_level=0.0,
            epic="IX.D.NIKKEI.IFM.IP",
            cfg=_cfg(),
        )
        self.assertAlmostEqual(stop, 63703.6)

    def test_risk_ignores_empty_epic(self) -> None:
        row = {"entry": 63663.6, "stop": 0.0, "size": 0.5, "epic": ""}
        self.assertEqual(risk_gbp_from_row(row, cfg=_cfg()), 0.0)


class GateCoherenceTests(unittest.TestCase):
    def test_repair_corrupt_nikkei_row(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        store = LearningStore(str(Path(tmp.name) / "t.db"))
        store.connect()
        store.open_trade(
            TradeRecord(
                id=None,
                market="Japan 225",
                epic="",
                side="SELL",
                entry=63663.6,
                exit=None,
                size=0.5,
                stop=0.0,
                target=63663.6,
                pnl_points=None,
                result=None,
                confidence=0,
                adjusted_confidence=0,
                setup_key="x",
                dry_run=False,
                deal_reference="BAD1",
                notes="",
            )
        )
        n = repair_corrupt_trade_rows(store, _cfg())
        self.assertEqual(n, 1)
        row = store.conn.execute("SELECT epic, stop FROM trades").fetchone()
        self.assertEqual(row["epic"], "IX.D.NIKKEI.IFM.IP")
        self.assertGreater(float(row["stop"]), 63663.0)
        store.close()
        tmp.cleanup()

    def test_critical_when_points_stop(self) -> None:
        report = audit_trading_readiness(
            _cfg(), None, points_state="STOP", repair_db=False
        )
        self.assertFalse(report.ok)
        self.assertTrue(any(i.code == "points_stop" for i in report.critical))

    def test_per_market_audit_includes_session_rules(self) -> None:
        inst = {
            "epic": "IX.D.NIKKEI.IFM.IP",
            "name": "Japan 225",
            "signal_threshold": 70,
            "trading_session_whitelist": ["asia_morning"],
            "stop_distance_points": 45,
            "risk_cap_gbp": 50,
        }
        mc = audit_market(
            instrument_id="japan",
            inst=inst,
            cfg=_cfg(),
            points_state="HEALTHY",
            v26={"ml_veto": {"enabled": False}},
            current_session="us_afternoon",
            s4_veto_epics=set(),
        )
        self.assertEqual(mc.epic, "IX.D.NIKKEI.IFM.IP")
        self.assertFalse(mc.session_allowed)
        self.assertTrue(any(i.code == "session_closed" for i in mc.issues))

    def test_ig_import_rows_excluded_from_daily_deploy_audit(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        store = LearningStore(str(Path(tmp.name) / "t.db"))
        store.connect()
        today = __import__("datetime").date.today().isoformat()
        store.conn.execute(
            """
            INSERT INTO trades (
                market, epic, side, entry, exit, size, stop, target,
                confidence, adjusted_confidence, setup_key, dry_run,
                deal_reference, opened_at, closed_at, source
            ) VALUES (
                'Spot Gold', 'CS.D.CFPGOLD.CFP.IP', 'BUY', 4213.62, NULL,
                10.0, 4183.22, NULL, 0, 0, 'ig|imported', 0,
                'DUP1', ?, ?, 'ig_import'
            )
            """,
            (f"{today} 00:01:22", f"{today} 00:01:23"),
        )
        store.conn.commit()
        report = audit_trading_readiness(
            _cfg(), store, points_state="HEALTHY", repair_db=False, per_market=False
        )
        self.assertFalse(
            any(i.code == "portfolio_daily_deploy_exceeded" for i in report.critical)
        )
        store.close()
        tmp.cleanup()

        c = MagicMock()
        c.get = lambda k, d=None: (
            {
                "japan": {
                    "epic": "IX.D.NIKKEI.IFM.IP",
                    "enabled": True,
                    "name": "Japan 225",
                    "signal_threshold": 70,
                    "trading_session_whitelist": ["asia_morning"],
                    "stop_distance_points": 45,
                    "risk_cap_gbp": 50,
                }
            }
            if k == "instruments"
            else d
        )
        c.as_dict = lambda: {"instruments": c.get("instruments")}
        report = audit_trading_readiness(
            c, None, points_state="HEALTHY", repair_db=False, per_market=True
        )
        self.assertGreaterEqual(len(report.markets), 1)
        self.assertTrue(report.generated_at)


if __name__ == "__main__":
    unittest.main()
