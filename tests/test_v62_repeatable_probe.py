"""V6.2 repeatable live-data probe — telemetry, breakout injection, order pipeline."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("IG_AGENT_PYTEST", "1")

from execution.ig_rest_traffic_governor import (
    MAX_TX_PER_60S,
    consume_positions_otc_transmit_slot,
    reset_ig_rest_traffic_governor_for_tests,
)
from execution.types import force_inject_gate_execution_params
from system.gating_reason import EXEC_ROUTE_OPEN, resolve_gating_reason
from system.recovery_mgr import (
    V2DisasterRecoveryManager,
    reset_disaster_recovery_for_tests,
    v62_probe_epics,
)
from trading.cache_reaper import govern_live_tick_ingest, reset_tick_governor_for_tests


class V62TelemetryProbeTests(unittest.TestCase):
  def test_v62_telemetry_daemon_1hz_seven_epics(self) -> None:
    from intelligence.telemetry_daemon import V2TelemetryDaemon, _gasket_config

    epics = v62_probe_epics()
    self.assertEqual(len(epics), 7)
    cfg = _gasket_config({"data_isolation_gasket": {"poll_hz_per_epic": 1.0}})
    self.assertAlmostEqual(float(cfg["poll_hz_per_epic"]), 1.0)
    reactor = V2TelemetryDaemon(poll_hz_per_epic=1.0)
    self.assertAlmostEqual(reactor._interval, 1.0, places=3)
    for epic in epics:
      self.assertTrue(epic.endswith(".IP"))

  def test_v62_reactor_alias_is_v4_micro_reactor(self) -> None:
    from intelligence.telemetry_daemon import V2TelemetryDaemon, V4MicroReactor

    self.assertIs(V2TelemetryDaemon, V4MicroReactor)


class V62BreakoutPipelineTests(unittest.TestCase):
  def setUp(self) -> None:
    reset_tick_governor_for_tests()
    reset_ig_rest_traffic_governor_for_tests()

  def test_v62_gold_breakout_tick_ingested_to_ram(self) -> None:
    epic = "CS.D.CFPGOLD.CFP.IP"
    govern_live_tick_ingest(epic, bid=2650.0, offer=2650.5, mid=2650.25)
    govern_live_tick_ingest(epic, bid=2665.0, offer=2665.8, mid=2665.4)
    from trading.cache_reaper import volatile_tick_slots_for_epic

    slots = volatile_tick_slots_for_epic(epic)
    self.assertGreaterEqual(len(slots), 1)

  def test_v62_gating_reason_exec_route_open_on_pass(self) -> None:
    reason = resolve_gating_reason(
      epic="CS.D.CFPGOLD.CFP.IP",
      wait_reason="",
      all_passed=True,
      gates=[],
    )
    self.assertEqual(reason, EXEC_ROUTE_OPEN)

  def test_v62_gate_execution_params_type_safe(self) -> None:
    out = force_inject_gate_execution_params(
      epic="CS.D.CFPGOLD.CFP.IP",
      size=1.0,
      stop_points=10.0,
      limit_points=20.0,
    )
    self.assertTrue(out.get("gate_sourced"))
    self.assertIn("stop_points", out)
    self.assertIn("limit_points", out)
    self.assertIn("actual_size", out)
    self.assertEqual(out["stop_points"], 10.0)
    self.assertEqual(out["limit_points"], 20.0)

  def test_v62_traffic_governor_three_per_minute_cap(self) -> None:
    allowed = 0
    blocked = 0
    for _ in range(MAX_TX_PER_60S + 2):
      ok, _ = consume_positions_otc_transmit_slot(
        epic="CS.D.CFPGOLD.CFP.IP",
        label="POST /v1/positions/otc — probe",
      )
      if ok:
        allowed += 1
      else:
        blocked += 1
    self.assertEqual(allowed, MAX_TX_PER_60S)
    self.assertGreaterEqual(blocked, 1)

  def test_v62_breakout_pipeline_order_dict(self) -> None:
    """Simulate ML pass-through → gated outbound payload."""
    gate = force_inject_gate_execution_params(
      epic="CS.D.CFPGOLD.CFP.IP",
      size=1.0,
    )
    ok, reason = consume_positions_otc_transmit_slot(epic="CS.D.CFPGOLD.CFP.IP")
    self.assertTrue(ok, reason)
    payload = {
      "epic": "CS.D.CFPGOLD.CFP.IP",
      "direction": "BUY",
      "gating_reason": EXEC_ROUTE_OPEN,
      "gate_execution_params": gate,
    }
    self.assertEqual(payload["gating_reason"], EXEC_ROUTE_OPEN)
    self.assertEqual(payload["gate_execution_params"]["stop_points"], 10.0)


class V62DisasterRecoveryProbeTests(unittest.TestCase):
  def setUp(self) -> None:
    reset_disaster_recovery_for_tests()

  def test_v62_cryptographic_checkpoint_writes_hash(self) -> None:
    mgr = V2DisasterRecoveryManager()
    with tempfile.TemporaryDirectory() as tmp:
      ledger = Path(tmp) / "trading_ledger.json"
      with patch("system.shutdown_cleanup._TRADING_LEDGER_PATH", ledger):
        with patch(
          "system.shutdown_cleanup._build_trading_ledger_snapshot",
          return_value={"open_positions": [], "mode": "probe"},
        ):
          with patch(
            "system.shutdown_cleanup._position_state_fingerprint",
            return_value="probe_fp",
          ):
            written = mgr.flush_cryptographic_checkpoint(reason="probe_test")
      self.assertTrue(written)
      self.assertTrue(ledger.is_file())
      import json

      body = json.loads(ledger.read_text(encoding="utf-8"))
      self.assertIn("checkpoint_sha256", body)
      self.assertEqual(len(body["checkpoint_sha256"]), 64)

  def test_v62_broker_reconcile_rebuilds_missing_position(self) -> None:
    mgr = V2DisasterRecoveryManager()
    broker = [
      {
        "market": {"epic": "CS.D.CFPGOLD.CFP.IP", "instrumentName": "Gold"},
        "position": {
          "dealId": "DIAAA123",
          "direction": "BUY",
          "size": 1.0,
          "level": 2650.0,
        },
      }
    ]
    mock_store = MagicMock()
    mock_store.active_trades.return_value = []
    mock_store.import_ig_position.return_value = 42
    with patch("system.config_loader.get_config") as gc:
      gc.return_value = MagicMock(learning_db=":memory:")
      with patch("data.learning_store.LearningStore", return_value=mock_store):
        rebuilt = mgr.reconcile_failsafe_cache(broker)
    self.assertEqual(rebuilt, 1)
    mock_store.import_ig_position.assert_called_once()
    from trading.cache_reaper import volatile_runtime_state_get

    ram = volatile_runtime_state_get()
    self.assertTrue(ram.get("disaster_recovery"))


if __name__ == "__main__":
  unittest.main()
