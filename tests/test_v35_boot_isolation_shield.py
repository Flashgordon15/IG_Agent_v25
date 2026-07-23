"""v35 dual-desk boot isolation shield — stagger, SoT fallback, env reset."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from runtime.boot_sot_fallback import (
    reset_boot_sot_fallback_for_tests,
    resolve_boot_sot_fallback,
    verify_broker_snapshot_for_boot,
)
from runtime.desk_stability_harness import (
    StabilityComponents,
    compute_boot_gate,
    grade_stability,
    note_boot_started,
    reset_desk_stability_harness_for_tests,
)
from system.boot.dual_desk_stagger import (
    MIN_POST_READY_STAGGER_SEC,
    cfd_engine_ready,
    plan_sb_spawn,
    sb_spawn_allowed,
    wait_cfd_ready_then_stagger,
)
from system.boot.env_reset import (
    flush_twin_runtime_locks,
    forceful_environmental_reset,
)


@pytest.fixture(autouse=True)
def _reset_boot_state():
    reset_desk_stability_harness_for_tests()
    reset_boot_sot_fallback_for_tests()
    note_boot_started(time.time())
    yield
    reset_desk_stability_harness_for_tests()
    reset_boot_sot_fallback_for_tests()


# ---------------------------------------------------------------------------
# 1. Stagger — SB not invoked until CFD ready + ≥4s
# ---------------------------------------------------------------------------


def test_cfd_engine_ready_requires_trading_healthy_or_trade_ready() -> None:
    assert cfd_engine_ready(None) is False
    assert cfd_engine_ready({"ok": True}) is False
    assert cfd_engine_ready({"ok": True, "trade_ready": True}) is True
    assert cfd_engine_ready({"trading_healthy": True}) is True


def test_sb_spawn_blocked_until_cfd_ready() -> None:
    assert sb_spawn_allowed(cfd_ready=False, cfd_ready_at_mono=None) is False
    plan = plan_sb_spawn(cfd_ready=False, cfd_ready_at_mono=None)
    assert plan["action"] == "wait_cfd"
    assert plan["sb_spawn_allowed"] is False


def test_sb_spawn_blocked_during_post_ready_stagger_window() -> None:
    t0 = 1000.0
    assert (
        sb_spawn_allowed(
            cfd_ready=True,
            cfd_ready_at_mono=t0,
            now_mono=t0 + 2.0,
            min_post_ready_sec=4.0,
        )
        is False
    )
    plan = plan_sb_spawn(
        cfd_ready=True,
        cfd_ready_at_mono=t0,
        now_mono=t0 + 2.0,
        min_post_ready_sec=4.0,
    )
    assert plan["action"] == "wait_stagger"
    assert plan["remaining_stagger_sec"] == pytest.approx(2.0)
    assert plan["min_post_ready_sec"] >= MIN_POST_READY_STAGGER_SEC


def test_sb_spawn_allowed_after_cfd_ready_plus_4s() -> None:
    t0 = 1000.0
    assert (
        sb_spawn_allowed(
            cfd_ready=True,
            cfd_ready_at_mono=t0,
            now_mono=t0 + 4.0,
            min_post_ready_sec=4.0,
        )
        is True
    )
    plan = plan_sb_spawn(
        cfd_ready=True,
        cfd_ready_at_mono=t0,
        now_mono=t0 + 4.01,
        min_post_ready_sec=4.0,
    )
    assert plan["sb_spawn_allowed"] is True
    assert plan["action"] == "spawn_sb"
    assert "rest_pressure" in plan["reason"]


def test_wait_cfd_ready_then_stagger_sequence_mocked() -> None:
    """SB spawn plan only after CFD ready + ≥4s sleep (mocked clock/sleep)."""
    calls: list[str] = []
    mono = {"t": 0.0}
    health_n = {"n": 0}

    def _mono() -> float:
        return mono["t"]

    def _sleep(sec: float) -> None:
        calls.append(f"sleep:{sec}")
        mono["t"] += float(sec)

    def _health(_port: int):
        health_n["n"] += 1
        if health_n["n"] < 3:
            return {"ok": True, "trade_ready": False}
        return {"ok": True, "trade_ready": True, "trading_healthy": True}

    result = wait_cfd_ready_then_stagger(
        port=8080,
        ready_timeout_sec=30.0,
        min_post_ready_sec=4.0,
        poll_sec=1.0,
        sleep_fn=_sleep,
        monotonic_fn=_mono,
        health_fetcher=_health,
    )
    assert result["sb_spawn_allowed"] is True
    assert result["cfd_ready"] is True
    assert result["stagger_slept_sec"] >= 4.0
    assert any(c.startswith("sleep:4") for c in calls)
    # Polls happened before ready
    assert health_n["n"] >= 3


def test_min_post_ready_stagger_never_below_4s() -> None:
    plan = plan_sb_spawn(
        cfd_ready=True,
        cfd_ready_at_mono=0.0,
        now_mono=10.0,
        min_post_ready_sec=1.0,  # underspec — floor to 4
    )
    assert plan["min_post_ready_sec"] >= 4.0


def test_v32_script_uses_readiness_stagger_not_simultaneous_spawn() -> None:
    text = Path("scripts/v32_runtime_start.sh").read_text(encoding="utf-8")
    assert "wait_cfd_ready_then_stagger_sb" in text
    assert "forceful_environmental_reset" in text
    assert "rest_pressure / init burst isolation" in text
    assert "IG_V32_SB_POST_READY_STAGGER_SEC" in text
    # Sequence inside start_both — CFD spawn → wait → SB spawn (not simultaneous).
    start_body = text.split("start_both()", 1)[1].split("stop_both()", 1)[0]
    cfd_idx = start_body.index('launch_engine "$CFD_PORT"')
    wait_idx = start_body.index("wait_cfd_ready_then_stagger_sb")
    sb_idx = start_body.index('launch_engine "$SB_PORT"')
    assert cfd_idx < wait_idx < sb_idx


# ---------------------------------------------------------------------------
# 2. Boot SoT timeout → broker_snapshot fallback advances gate
# ---------------------------------------------------------------------------


def test_verify_rejects_empty_stub_snapshot() -> None:
    assert verify_broker_snapshot_for_boot(None)["ok"] is False
    assert verify_broker_snapshot_for_boot({})["ok"] is False
    assert verify_broker_snapshot_for_boot({"ts": 0})["ok"] is False
    bad = verify_broker_snapshot_for_boot(
        {"ts": time.time(), "count": 2, "positions": []}
    )
    assert bad["ok"] is False
    assert "count_without_rows" in bad["reason"]


def test_verify_accepts_flat_verified_snapshot() -> None:
    ok = verify_broker_snapshot_for_boot(
        {"ts": time.time(), "count": 0, "positions": [], "source": "unit"}
    )
    assert ok["ok"] is True
    assert ok["broker_open"] == 0


def test_boot_sot_timeout_hydrates_from_broker_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime import broker_snapshot as bs

    monkeypatch.setattr(bs, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(bs, "legacy_src_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bs, "state_dir", lambda: tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True)
    bs.write_snapshot(source="unit_boot", positions=[])

    fb = resolve_boot_sot_fallback(
        booting=True,
        sot_age_sec=45.0,
        stale_budget_sec=30.0,
        network_timeout=True,
        sot_ok=False,
        broker_open=0,
    )
    assert fb["fallback_active"] is True
    assert fb["sot_ok"] is True
    assert fb["sot_source"] == "broker_snapshot_boot_fallback"
    assert fb["gate_status"] == "pass"
    assert fb["broker_open"] == 0


def test_boot_sot_missing_snapshot_soft_fails_no_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime import broker_snapshot as bs

    monkeypatch.setattr(bs, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(bs, "legacy_src_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bs, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(
        bs,
        "_mirror_paths",
        lambda: [tmp_path / "state" / "broker_snapshot.json"],
    )
    (tmp_path / "state").mkdir(parents=True)
    # No snapshot file → soft fail, not infinite freeze.
    fb = resolve_boot_sot_fallback(
        booting=True,
        sot_age_sec=99.0,
        stale_budget_sec=30.0,
        network_timeout=True,
        sot_ok=False,
        broker_open=0,
    )
    assert fb["fallback_active"] is True
    assert fb["soft_fail"] is True
    assert fb["gate_status"] == "warn"
    assert fb["sot_ok"] is False


def test_boot_gate_advances_on_snapshot_fallback_hydrate() -> None:
    c = StabilityComponents(
        health_ok=True,
        trade_ready=True,
        trading_path_live=True,
        desk_rag="G",
        broker_open=0,
        positions_verdict="FLAT",
        sot_ok=True,
        sot_age_sec=12.0,
        sot_source="broker_snapshot_boot_fallback",
        trade_support_running=False,
        rest_pressure_level="IDLE",
        opm_ok=True,
        liveness_ok=True,
        flat_book=True,
        boot_sot_fallback_active=True,
        boot_sot_fallback_reason="broker_snapshot_boot_hydrate",
    )
    grade, reasons = grade_stability(c)
    gate = compute_boot_gate(c, grade, reasons)
    assert gate["ready_for_desk"] is True
    assert gate["boot_sot_fallback"] is True
    assert "sot_stale" not in gate["blockers"]
    sot_check = next(ch for ch in gate["checks"] if ch["id"] == "trade_support_sot")
    assert sot_check["status"] == "pass"
    assert "boot_fallback" in str(sot_check["detail"])


def test_boot_gate_soft_fail_does_not_hard_block_sot() -> None:
    c = StabilityComponents(
        health_ok=True,
        trade_ready=True,
        trading_path_live=True,
        desk_rag="G",
        broker_open=0,
        sot_ok=False,
        sot_age_sec=40.0,
        rest_pressure_level="IDLE",
        opm_ok=True,
        liveness_ok=True,
        flat_book=True,
        boot_sot_fallback_active=True,
        boot_sot_fallback_reason="snapshot_missing_or_empty_stub",
        boot_sot_soft_fail=True,
    )
    grade, reasons = grade_stability(c)
    gate = compute_boot_gate(c, grade, reasons)
    assert "sot_stale" not in gate["blockers"]
    sot_check = next(ch for ch in gate["checks"] if ch["id"] == "trade_support_sot")
    assert sot_check["status"] == "warn"


def test_broker_snapshot_verify_for_boot_hydrate_delegate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime import broker_snapshot as bs

    monkeypatch.setattr(bs, "data_dir", lambda: tmp_path)
    monkeypatch.setattr(bs, "legacy_src_data_dir", lambda: tmp_path)
    monkeypatch.setattr(bs, "state_dir", lambda: tmp_path / "state")
    (tmp_path / "state").mkdir(parents=True)
    bs.write_snapshot(
        source="unit",
        positions=[
            {
                "deal_id": "D1",
                "epic": "IX.D.DOW.IFM.IP",
                "direction": "BUY",
                "size": 0.5,
                "entry": 40000.0,
            }
        ],
    )
    verified = bs.verify_for_boot_hydrate()
    assert verified["ok"] is True
    assert verified["broker_open"] == 1


# ---------------------------------------------------------------------------
# 3. Start-loop lock flush / env reset
# ---------------------------------------------------------------------------


def test_flush_twin_runtime_locks_removes_lock_files(tmp_path: Path) -> None:
    cfd = tmp_path / "state_cfd"
    sb = tmp_path / "state_sb"
    cfd.mkdir()
    sb.mkdir()
    (cfd / "agent.lock").write_text("x")
    (sb / "session.lock").write_text("x")
    (tmp_path / "orphan.lock").write_text("x")
    # Protected — must survive
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "manual_stop.json").write_text("{}")
    (tmp_path / "learning.db").write_text("db")

    result = flush_twin_runtime_locks(tmp_path)
    assert result["ok"] is True
    assert not (cfd / "agent.lock").exists()
    assert not (sb / "session.lock").exists()
    assert not (tmp_path / "orphan.lock").exists()
    assert (tmp_path / "state" / "manual_stop.json").exists()
    assert (tmp_path / "learning.db").exists()


def test_forceful_environmental_reset_clears_sot_cache(tmp_path: Path) -> None:
    (tmp_path / "trade_support_status.json").write_text(
        json.dumps({"ts": 1, "broker_open": 0})
    )
    (tmp_path / "state_cfd").mkdir()
    (tmp_path / "state_cfd" / "x.lock").write_text("1")
    result = forceful_environmental_reset(tmp_path, clear_sot_cache=True)
    assert result["ok"] is True
    assert not (tmp_path / "trade_support_status.json").exists()
    assert not (tmp_path / "state_cfd" / "x.lock").exists()


def test_v32_script_forceful_reset_commands_present() -> None:
    text = Path("scripts/v32_runtime_start.sh").read_text(encoding="utf-8")
    assert "fuser -k" in text
    assert 'rm -f "${CFD_STATE}"/*.lock' in text or "rm -f \"${CFD_STATE}\"/*.lock" in text
    assert "forceful_environmental_reset" in text
    # Called before engine spawn in start_both
    start_idx = text.index("start_both()")
    reset_idx = text.index("forceful_environmental_reset", start_idx)
    launch_idx = text.index("launch_engine", reset_idx)
    assert reset_idx < launch_idx


# ---------------------------------------------------------------------------
# 4. rest_pressure stays green under staggered init mock
# ---------------------------------------------------------------------------


def test_rest_pressure_green_under_staggered_init_plan() -> None:
    """Staggered plan itself does not imply REST pressure; harness stays IDLE/green."""
    t0 = 50.0
    plan = plan_sb_spawn(
        cfd_ready=True,
        cfd_ready_at_mono=t0,
        now_mono=t0 + 4.0,
        min_post_ready_sec=4.0,
    )
    assert plan["sb_spawn_allowed"] is True

    c = StabilityComponents(
        health_ok=True,
        trade_ready=True,
        trading_path_live=True,
        desk_rag="G",
        broker_open=0,
        sot_ok=True,
        sot_age_sec=5.0,
        rest_pressure_level="IDLE",
        opm_ok=True,
        ui_up=True,
        liveness_ok=True,
        flat_book=True,
        trade_support_running=True,
    )
    grade, reasons = grade_stability(c)
    gate = compute_boot_gate(c, grade, reasons)
    assert grade == "G"
    assert gate["ready_for_desk"] is True
    rest_check = next(ch for ch in gate["checks"] if ch["id"] == "rest_pressure")
    assert rest_check["status"] == "pass"
    assert "rest_elevated" not in gate["blockers"]
    assert "rest_high" not in gate["blockers"]


def test_staggered_wait_does_not_flag_rest_pressure_reason() -> None:
    mono = {"t": 0.0}

    def _sleep(sec: float) -> None:
        mono["t"] += float(sec)

    result = wait_cfd_ready_then_stagger(
        port=8080,
        ready_timeout_sec=5.0,
        min_post_ready_sec=4.0,
        poll_sec=0.1,
        sleep_fn=_sleep,
        monotonic_fn=lambda: mono["t"],
        health_fetcher=lambda _p: {
            "ok": True,
            "trade_ready": True,
            "trading_healthy": True,
        },
    )
    assert result["sb_spawn_allowed"] is True
    assert result["min_post_ready_sec"] >= 4.0
    assert result.get("reason") != "rest_pressure_high"
    assert "HIGH" not in str(result.get("reason", "")).upper()
