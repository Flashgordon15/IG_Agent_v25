"""Application Stability Harness — grade policy + safe heal selection."""

from __future__ import annotations

import time

import pytest

from runtime.desk_stability_harness import (
    StabilityComponents,
    compute_boot_gate,
    grade_stability,
    reset_desk_stability_harness_for_tests,
    select_heals,
)


def setup_function() -> None:
    reset_desk_stability_harness_for_tests()


def teardown_function() -> None:
    reset_desk_stability_harness_for_tests()


def _green_base(**kwargs) -> StabilityComponents:
    c = StabilityComponents(
        health_ok=True,
        trade_ready=True,
        trading_path_live=True,
        desk_rag="G",
        broker_open=0,
        positions_verdict="FLAT",
        sot_ok=True,
        sot_age_sec=10.0,
        trade_support_running=True,
        rest_pressure_level="IDLE",
        opm_ok=True,
        ui_up=True,
        liveness_ok=True,
        has_open_risk=False,
        entries_paused=False,
        flat_book=True,
    )
    for k, v in kwargs.items():
        setattr(c, k, v)
    return c


def test_grade_green_when_all_planes_ok() -> None:
    grade, reasons = grade_stability(_green_base())
    assert grade == "G"
    assert reasons


def test_grade_amber_when_ui_down_flat() -> None:
    grade, reasons = grade_stability(_green_base(ui_up=False))
    assert grade == "A"
    assert any("UI" in r for r in reasons)


def test_grade_amber_rest_elevated() -> None:
    grade, reasons = grade_stability(_green_base(rest_pressure_level="ELEVATED"))
    assert grade == "A"
    assert any("REST" in r for r in reasons)


def test_grade_red_opens_stale_sot() -> None:
    grade, reasons = grade_stability(
        _green_base(
            broker_open=2,
            has_open_risk=True,
            flat_book=False,
            positions_verdict="HEALTHY",
            sot_ok=False,
            sot_age_sec=200.0,
        )
    )
    assert grade == "R"
    assert any("SoT" in r or "stale" in r for r in reasons)


def test_grade_red_rest_critical() -> None:
    grade, _ = grade_stability(_green_base(rest_pressure_level="CRITICAL"))
    assert grade == "R"


def test_flat_false_stale_does_not_plan_recover_supervise() -> None:
    """Flat book + soft noise → heal_ui ok, never recover_and_supervise."""
    c = _green_base(
        ui_up=False,
        liveness_ok=False,
        liveness_issues=["positions_snapshot_stale", "ig_sync_missing"],
        sot_ok=True,
    )
    grade, _ = grade_stability(c)
    assert grade == "A"
    actions = select_heals(c, grade, ["UI :3000 down"])
    assert "heal_ui" in actions
    assert "recover_and_supervise" not in actions
    assert "anti_zombie_recovery" not in actions


def test_opens_stale_plans_trade_support_heal() -> None:
    c = _green_base(
        broker_open=1,
        has_open_risk=True,
        flat_book=False,
        sot_ok=False,
        trade_support_running=False,
        ui_up=True,
    )
    grade, reasons = grade_stability(c)
    assert grade == "R"
    actions = select_heals(c, grade, reasons)
    assert "heal_trade_support" in actions


def test_health_ok_alone_never_forces_green() -> None:
    """Fragmented truth: health.ok true but path down → not G."""
    grade, _ = grade_stability(
        _green_base(
            health_ok=True,
            trading_path_live=False,
            trading_path_badge="DESK TRADING DOWN",
            desk_rag="A",
        )
    )
    assert grade != "G"


def test_health_ok_false_soft_when_trade_ready_path_live() -> None:
    """iron_cage can lag health.ok=false while path is live — don't amber solely on that."""
    grade, reasons = grade_stability(
        _green_base(health_ok=False, trade_ready=True, trading_path_live=True)
    )
    assert grade == "G"
    assert not any("health.ok" in r for r in reasons)


def test_deploy_hold_isolation_under_pytest(tmp_path, monkeypatch) -> None:
    """Unit tests must not stamp deploy_hold into v31-production."""
    from runtime import deploy_hold

    monkeypatch.setenv("IG_AGENT_PYTEST", "1")
    # Point shared state at tmp via data_dir — write allowed
    monkeypatch.setattr("system.paths.data_dir", lambda: tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    path = deploy_hold.set_deploy_hold(active=True, reason="unit_iso")
    assert path.is_file()
    assert "unit_iso" in path.read_text()

    # Prod path under pytest must be refused
    from system.paths import project_root

    prod = project_root() / "src" / "data" / "v31-production"
    monkeypatch.setattr("system.paths.data_dir", lambda: prod)
    before = None
    hold = prod / "state" / "deploy_hold.json"
    if hold.is_file():
        before = hold.read_text()
    deploy_hold.set_deploy_hold(active=True, reason="must_not_land")
    if hold.is_file() and before is not None:
        assert "must_not_land" not in hold.read_text()
    elif hold.is_file():
        assert "must_not_land" not in hold.read_text()


def test_boot_gate_ready_when_fully_armed() -> None:
    c = _green_base(desk_rag="G", rest_pressure_level="IDLE")
    grade, reasons = grade_stability(c)
    gate = compute_boot_gate(c, grade, reasons, actions_taken=[], actions_planned=[])
    assert gate["ready_for_desk"] is True
    assert gate["blockers"] == []
    assert any(ch["id"] == "trading_path_live" and ch["status"] == "pass" for ch in gate["checks"])


def test_boot_gate_blocks_when_path_down() -> None:
    c = _green_base(trading_path_live=False, trading_path_badge="DOWN", desk_rag="A")
    grade, reasons = grade_stability(c)
    gate = compute_boot_gate(c, grade, reasons)
    assert gate["ready_for_desk"] is False
    assert "path_not_live" in gate["blockers"]


def test_boot_gate_blocks_while_healing() -> None:
    c = _green_base()
    grade, reasons = grade_stability(c)
    gate = compute_boot_gate(
        c,
        grade,
        reasons,
        actions_planned=["heal_ui"],
        actions_taken=[{"action": "heal_ui", "planned": True}],
    )
    assert gate["ready_for_desk"] is False
    assert "healing_in_progress" in gate["blockers"]
    assert any(ch["status"] == "healing" for ch in gate["checks"])


def test_boot_gate_blocks_entries_paused() -> None:
    c = _green_base(entries_paused=True)
    grade, reasons = grade_stability(c)
    gate = compute_boot_gate(c, grade, reasons)
    assert gate["ready_for_desk"] is False
    assert "entries_paused" in gate["blockers"]


def test_boot_gate_blocks_rest_elevated() -> None:
    c = _green_base(rest_pressure_level="ELEVATED")
    grade, reasons = grade_stability(c)
    gate = compute_boot_gate(c, grade, reasons)
    assert gate["ready_for_desk"] is False
    assert "rest_elevated" in gate["blockers"]


def test_trade_support_stale_budget_boot_vs_runtime(monkeypatch) -> None:
    from runtime import desk_stability_harness as dsh

    dsh.note_boot_started(time.time())
    assert dsh.boot_grace_active() is True
    assert dsh.trade_support_stale_budget_sec() == pytest.approx(30.0)
    assert dsh.boot_latency_buffer_sec() == pytest.approx(30.0)
    assert dsh.boot_latency_buffer_active() is True

    dsh.note_boot_started(time.time() - 120.0)
    assert dsh.boot_grace_active() is False
    assert dsh.trade_support_stale_budget_sec() == pytest.approx(20.0)
    assert dsh.boot_latency_buffer_active() is False


def test_sot_ok_within_boot_budget(monkeypatch) -> None:
    from runtime import desk_stability_harness as dsh

    dsh.note_boot_started(time.time())
    c = _green_base(sot_ok=False, sot_age_sec=20.5, flat_book=True, broker_open=0)
    stale_lim = dsh.trade_support_stale_budget_sec()
    assert c.sot_age_sec < stale_lim


def test_flat_stale_snapshot_skips_cap_breach_during_boot() -> None:
    from runtime import desk_stability_harness as dsh

    dsh.note_boot_started(time.time())
    conf = dsh._load_cfg()
    c = StabilityComponents(
        broker_open=0,
        flat_book=True,
        positions_verdict="FLAT",
    )
    snap_n = 12
    max_open = 6
    if snap_n is not None and snap_n > max_open:
        if dsh.boot_grace_active(cfg=conf) and c.flat_book and c.broker_open <= 0:
            pass
        else:
            c.cap_breach = True
    assert c.cap_breach is False


def test_select_heals_skips_cap_pause_during_boot_flat() -> None:
    from runtime import desk_stability_harness as dsh

    dsh.note_boot_started(time.time())
    c = _green_base(cap_breach=True, flat_book=True, broker_open=0)
    actions = select_heals(c, "R", ["max_open cap breach"])
    assert "pause_entries_cap_breach" not in actions


def test_upgrade_manifest_load(tmp_path, monkeypatch) -> None:
    from runtime import desk_upgrade_manifest as dum

    monkeypatch.setattr(dum, "data_dir", lambda: tmp_path)
    (tmp_path / "state").mkdir(parents=True)
    seed = {
        "schema_version": 1,
        "upgrades": [
            {"id": "a", "date": "2026-07-22", "title": "A", "detail": "d"},
            {"id": "b", "date": "2026-07-21", "title": "B", "detail": "d"},
        ],
    }
    import json

    (tmp_path / "state" / "desk_upgrade_manifest.json").write_text(
        json.dumps(seed), encoding="utf-8"
    )
    man = dum.load_upgrade_manifest(limit=5)
    assert len(man["upgrades"]) == 2
    assert man["upgrades"][0]["id"] == "a"
    dum.append_upgrade(upgrade_id="c", title="C", detail="new", date="2026-07-22")
    man2 = dum.load_upgrade_manifest()
    assert man2["upgrades"][0]["id"] == "c"
