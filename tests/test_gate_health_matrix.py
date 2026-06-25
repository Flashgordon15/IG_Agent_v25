"""Gate-aware /api/health matrix and os_surface_cleanse tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from api.gate_health_matrix import resolve_gate_health_matrix  # noqa: E402
from system.system_state import (  # noqa: E402
    BootPhase,
    GateSnapshot,
    GateStatus,
    SystemState,
)


@pytest.fixture(autouse=True)
def _reset_system_state() -> None:
    SystemState.reset_singleton_for_tests()
    yield
    SystemState.reset_singleton_for_tests()


def _set_gates(**statuses: str) -> None:
    state = SystemState.get()
    gates = {
        gid: GateSnapshot(status=GateStatus(status))
        for gid, status in statuses.items()
    }
    state.update_state(
        phase=BootPhase.BOOTING,
        percent=10,
        label="test",
        gates_dict=gates,
    )


def test_health_initializing_gate1() -> None:
    _set_gates(G1="running", G2="pending", G3="pending")
    code, body = resolve_gate_health_matrix()
    assert code == 503
    assert body == {"status": "INITIALIZING", "gate": 1, "ready": False}


def test_health_hydrating_gate2() -> None:
    _set_gates(G1="complete", G2="running", G3="pending")
    code, body = resolve_gate_health_matrix()
    assert code == 503
    assert body == {"status": "HYDRATING", "gate": 2, "ready": False}


def test_health_operational_gate3() -> None:
    _set_gates(G1="complete", G2="complete", G3="complete")
    code, body = resolve_gate_health_matrix()
    assert code == 200
    assert body == {"status": "OPERATIONAL", "ready": True}


def test_os_surface_cleanse_sigkills_foreign_listener() -> None:
    from system.identity.process_orchestrator import os_surface_cleanse

    with patch.dict(os.environ, {"IG_API_PORT": "18080"}, clear=False):
        with patch(
            "system.identity.process_orchestrator.subprocess.run",
            return_value=MagicMock(stdout="4242\n", returncode=0),
        ):
            with patch("system.identity.process_orchestrator.os.getpid", return_value=1000):
                with patch("system.identity.process_orchestrator.os.getppid", return_value=999):
                    with patch("system.identity.process_orchestrator.os.kill") as kill:
                        with patch(
                            "system.identity.process_orchestrator._evict_versioned_shm_partitions",
                            return_value=[],
                        ):
                            summary = os_surface_cleanse()
    assert summary["api_port"] == 18080
    assert summary["killed_pids"] == [4242]
    kill.assert_called_once_with(4242, 9)


def test_certify_boot_gate_timeout_constant() -> None:
    import scripts.certify_apex_limit as cert

    assert cert._BOOT_GATE_TIMEOUT_SEC == 30.0
