"""v32 dual-port isolation — CLI parsing, state dirs, session locks."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from runtime.broker_snapshot import snapshot_path, write_snapshot
from runtime.session_lock import lock_path_for_scope, write_session_lock
from system.engine_cli import apply_engine_cli_env, parse_engine_cli
from system.engine_lane import (
    DEFAULT_ACCOUNT_CFD,
    DEFAULT_ACCOUNT_SB,
    ENGINE_CFD_SNIPER,
    ENGINE_ORIGIN_CFD,
    ENGINE_ORIGIN_SB,
    ENGINE_SB_SENTINEL,
    resolve_active_engine_id,
    resolve_journal_metadata,
)
from system.paths import shared_state_dir, state_dir


@pytest.fixture(autouse=True)
def _clear_dual_port_env(monkeypatch) -> None:
    for key in (
        "IG_V32_DUAL_PORT",
        "IG_ENGINE_ORIGIN",
        "IG_ACCOUNT_ID",
        "IG_ACCOUNT_SCOPE",
        "IG_API_PORT",
        "PORT",
        "IG_ACTIVE_ENGINE_ID",
        "IG_ENGINE_STATE_SUBDIR",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def isolated_data_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "v31-production"
    (root / "state").mkdir(parents=True)
    (root / "state_cfd").mkdir(parents=True)
    (root / "state_sb").mkdir(parents=True)
    monkeypatch.setenv("IG_AGENT_DATA_DIR", str(root))
    monkeypatch.setenv("IG_AGENT_PYTEST", "1")
    for key in (
        "IG_V32_DUAL_PORT",
        "IG_ENGINE_ORIGIN",
        "IG_ENGINE_STATE_SUBDIR",
        "IG_STATE_DIR",
        "IG_ACCOUNT_ID",
        "IG_ACTIVE_ENGINE_ID",
        "IG_API_PORT",
        "PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    return root


def test_parse_engine_cli_triplet_produces_distinct_engine_models(monkeypatch) -> None:
    for key in (
        "IG_V32_DUAL_PORT",
        "IG_ENGINE_ORIGIN",
        "IG_ACCOUNT_ID",
        "IG_ACCOUNT_SCOPE",
        "IG_API_PORT",
        "PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    cfd = parse_engine_cli(
        ["--port=8080", "--account-id=Z6BAH4", "--origin=QUANT_SNIPER"]
    )
    sb = parse_engine_cli(
        ["--port=8081", "--account-id=Z6BAH3", "--origin=MACRO_SENTINEL"]
    )

    assert cfd.dual_port_mode is True
    assert sb.dual_port_mode is True
    assert cfd != sb
    assert cfd.engine_id == ENGINE_CFD_SNIPER
    assert sb.engine_id == ENGINE_SB_SENTINEL
    assert cfd.state_subdir == "state_cfd"
    assert sb.state_subdir == "state_sb"

    with pytest.raises(SystemExit):
        parse_engine_cli(["--port=8080", "--origin=QUANT_SNIPER"])

    default = parse_engine_cli([])
    assert default.dual_port_mode is False
    assert default.port is None


def test_state_dirs_isolate_broker_snapshot_writes(
    isolated_data_root: Path, monkeypatch
) -> None:
    monkeypatch.setenv("IG_V32_DUAL_PORT", "1")
    monkeypatch.setenv("IG_ENGINE_ORIGIN", ENGINE_ORIGIN_CFD)
    monkeypatch.setenv("IG_ENGINE_STATE_SUBDIR", "state_cfd")
    monkeypatch.setenv("IG_ACCOUNT_ID", DEFAULT_ACCOUNT_CFD)

    cfd_snap = snapshot_path()
    write_snapshot(
        source="test",
        positions=[{"dealId": "CFD001", "epic": "IX.D.DOW.IFM.IP"}],
    )
    assert cfd_snap == isolated_data_root / "state_cfd" / "broker_snapshot.json"
    assert cfd_snap.is_file()
    assert not (isolated_data_root / "state_sb" / "broker_snapshot.json").exists()

    monkeypatch.setenv("IG_ENGINE_ORIGIN", ENGINE_ORIGIN_SB)
    monkeypatch.setenv("IG_ENGINE_STATE_SUBDIR", "state_sb")
    monkeypatch.setenv("IG_ACCOUNT_ID", DEFAULT_ACCOUNT_SB)

    sb_snap = snapshot_path()
    write_snapshot(
        source="test",
        positions=[{"dealId": "SB001", "epic": "IX.D.DOW.IFM.IP"}],
    )
    assert sb_snap == isolated_data_root / "state_sb" / "broker_snapshot.json"
    assert sb_snap.is_file()

    cfd_payload = json.loads(cfd_snap.read_text(encoding="utf-8"))
    sb_payload = json.loads(sb_snap.read_text(encoding="utf-8"))
    assert cfd_payload["positions"][0]["dealId"] == "CFD001"
    assert sb_payload["positions"][0]["dealId"] == "SB001"
    assert shared_state_dir() == isolated_data_root / "state"


def test_session_lock_paths_isolate_per_account(
    isolated_data_root: Path, monkeypatch
) -> None:
    monkeypatch.setenv("IG_V32_DUAL_PORT", "1")

    cfd_cli = parse_engine_cli(
        ["--port=8080", "--account-id=Z6BAH4", "--origin=QUANT_SNIPER"]
    )
    apply_engine_cli_env(cfd_cli)
    cfd_lock = lock_path_for_scope(f"ig:{DEFAULT_ACCOUNT_CFD}", isolated_data_root)
    write_session_lock(
        cfd_lock,
        pid=1001,
        port=8080,
        account_scope=f"ig:{DEFAULT_ACCOUNT_CFD}",
    )

    monkeypatch.setenv("IG_V32_DUAL_PORT", "1")
    sb_cli = parse_engine_cli(
        ["--port=8081", "--account-id=Z6BAH3", "--origin=MACRO_SENTINEL"]
    )
    apply_engine_cli_env(sb_cli)
    sb_lock = lock_path_for_scope(f"ig:{DEFAULT_ACCOUNT_SB}", isolated_data_root)
    write_session_lock(
        sb_lock,
        pid=1002,
        port=8081,
        account_scope=f"ig:{DEFAULT_ACCOUNT_SB}",
    )

    assert cfd_lock != sb_lock
    assert cfd_lock.name == f"session_ig_{DEFAULT_ACCOUNT_CFD}.lock"
    assert sb_lock.name == f"session_ig_{DEFAULT_ACCOUNT_SB}.lock"
    assert cfd_lock.is_file() and sb_lock.is_file()

    apply_engine_cli_env(cfd_cli)
    meta_cfd = resolve_journal_metadata()
    assert meta_cfd["account_id"] == DEFAULT_ACCOUNT_CFD
    assert meta_cfd["engine_origin"] == ENGINE_ORIGIN_CFD
    assert resolve_active_engine_id() == ENGINE_CFD_SNIPER

    apply_engine_cli_env(sb_cli)
    meta_sb = resolve_journal_metadata()
    assert meta_sb["account_id"] == DEFAULT_ACCOUNT_SB
    assert meta_sb["engine_origin"] == ENGINE_ORIGIN_SB
    assert resolve_active_engine_id() == ENGINE_SB_SENTINEL
    assert state_dir() == isolated_data_root / "state_sb"


def test_dual_port_cli_assigns_distinct_shm_ring_names(monkeypatch) -> None:
    for key in (
        "IG_SHM_RING_NAME",
        "IG_SHM_RING_CREATE",
        "IG_ACCOUNT_ID",
        "IG_ENGINE_ORIGIN",
        "IG_API_PORT",
        "PORT",
        "IG_V32_DUAL_PORT",
    ):
        monkeypatch.delenv(key, raising=False)

    cfd_cli = parse_engine_cli(
        ["--port=8080", "--account-id=Z6BAH4", "--origin=QUANT_SNIPER"]
    )
    sb_cli = parse_engine_cli(
        ["--port=8081", "--account-id=Z6BAH3", "--origin=MACRO_SENTINEL"]
    )
    apply_engine_cli_env(cfd_cli)
    cfd_ring = os.environ["IG_SHM_RING_NAME"]
    apply_engine_cli_env(sb_cli)
    sb_ring = os.environ["IG_SHM_RING_NAME"]

    assert cfd_ring == "ig_agent_v33_shm_cfd_8080"
    assert sb_ring == "ig_agent_v33_shm_sb_8081"
    assert cfd_ring != sb_ring

    from kernel.ring_buffer import resolve_position_ring_shm_name
    from system.ipc.cockpit_shm_passive import resolve_cockpit_shm_name

    apply_engine_cli_env(cfd_cli)
    assert resolve_position_ring_shm_name() == "ig_agent_v33_shm_cfd_8080"
    assert resolve_cockpit_shm_name() == "ig_agent_v33_cockpit_cfd_8080"
    apply_engine_cli_env(sb_cli)
    assert resolve_position_ring_shm_name() == "ig_agent_v33_shm_sb_8081"
    assert resolve_cockpit_shm_name() == "ig_agent_v33_cockpit_sb_8081"

    from system.identity.shared_memory_bridge import shm_name_for_track

    apply_engine_cli_env(cfd_cli)
    cfd_live = shm_name_for_track("live")
    apply_engine_cli_env(sb_cli)
    sb_live = shm_name_for_track("live")
    assert cfd_live == "ig_agent_v30_live_state_Z6BAH4"
    assert sb_live == "ig_agent_v30_live_state_Z6BAH3"
    assert cfd_live != sb_live


def test_session_registry_binds_distinct_clients_per_account(monkeypatch) -> None:
    from dataclasses import dataclass

    from runtime.session_registry import (
        get_session_registry,
        reset_session_registry_for_tests,
    )
    from system.credentials_loader import Credentials

    reset_session_registry_for_tests()
    monkeypatch.setenv("IG_SESSION_REGISTRY", "1")

    created: list[str] = []

    @dataclass
    class _FakeClient:
        account_id: str

    def _fake_ctor(credentials, *, account_id=None, **kwargs):
        aid = str(account_id or credentials.ig_account_id).upper()
        created.append(aid)
        return _FakeClient(account_id=aid)

    monkeypatch.setattr(
        "ig_api.rest_client.IGRestClient",
        _fake_ctor,
    )

    base = Credentials(
        ig_api_key="key",
        ig_username="user",
        ig_password="pass",
        ig_account_type="DEMO",
        ig_account_id="Z6BAH4",
    )
    registry = get_session_registry()
    cfd = registry.get_client_for_account(DEFAULT_ACCOUNT_CFD, base)
    sb = registry.get_client_for_account(DEFAULT_ACCOUNT_SB, base)

    assert cfd.account_id == DEFAULT_ACCOUNT_CFD
    assert sb.account_id == DEFAULT_ACCOUNT_SB
    assert cfd is not sb
    assert created == [DEFAULT_ACCOUNT_CFD, DEFAULT_ACCOUNT_SB]

    monkeypatch.setenv("IG_ACCOUNT_ID", DEFAULT_ACCOUNT_CFD)
    monkeypatch.setenv("IG_ENGINE_ORIGIN", ENGINE_ORIGIN_CFD)
    from system.ig_rest_session import get_shared_rest_client

    proc_client = get_shared_rest_client(base)
    assert proc_client.account_id == DEFAULT_ACCOUNT_CFD

    reset_session_registry_for_tests()
