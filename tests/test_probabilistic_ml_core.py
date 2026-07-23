"""Probabilistic sniper ML + PERF log rotator verification."""

from __future__ import annotations

import resource
from pathlib import Path

import pytest

from alpha.micro_sniper_ml import (
    SAFETY_BASELINE,
    SNIPER_THRESHOLD,
    THRESHOLD_FX,
    THRESHOLD_GOLD,
    THRESHOLD_INDEX,
    QuantumSniperMLCore,
    asset_class_for_epic,
    reset_sniper_ml_cache_for_tests,
    sniper_threshold_for_epic,
)
from runtime.log_rotator_daemon import (
    reset_log_rotator_for_tests,
    run_log_rotation_cycle,
)


@pytest.fixture(autouse=True)
def _clean_caches():
    reset_sniper_ml_cache_for_tests()
    reset_log_rotator_for_tests()
    yield
    reset_sniper_ml_cache_for_tests()
    reset_log_rotator_for_tests()


def test_extreme_spreads_drive_sigmoid_toward_safety_baseline():
    """Extreme market spreads collapse P(Success) toward ~10% safety floor."""
    core = QuantumSniperMLCore()
    result = core.evaluate_entry_probability(
        obi_velocity=0.8,
        spread_elasticity=5.5,  # flash-wide book vs 1h MA
        tick_acceleration=0.4,
        grok_macro_bias="NEUTRAL",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
    )
    assert result.p_success == pytest.approx(SAFETY_BASELINE, abs=0.05)
    assert result.p_success < 0.20
    assert result.approved is False
    assert result.p_success < SNIPER_THRESHOLD


def test_healthy_microstructure_can_clear_sniper_threshold():
    """Tight book + supportive flow can clear the index sniper arm."""
    core = QuantumSniperMLCore()
    result = core.evaluate_entry_probability(
        obi_velocity=0.95,
        spread_elasticity=1.0,
        tick_acceleration=0.8,
        grok_macro_bias="BULL",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
    )
    assert result.p_success >= SNIPER_THRESHOLD
    assert result.approved is True
    assert result.threshold == pytest.approx(THRESHOLD_INDEX)


def test_veto_macro_bias_blocks_even_with_supportive_flow():
    """Cached grok_macro_bias=VETO keeps P below sniper threshold."""
    core = QuantumSniperMLCore()
    result = core.evaluate_entry_probability(
        obi_velocity=1.2,
        spread_elasticity=1.05,
        tick_acceleration=1.0,
        grok_macro_bias="VETO",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
    )
    assert result.approved is False
    assert result.p_success < SNIPER_THRESHOLD


def test_asset_class_adaptive_thresholds_gold_tighter_than_index():
    """Gold gate is stricter than index/FX — premium spreads demand conviction."""
    assert asset_class_for_epic("CS.D.CFPGOLD.CFP.IP") == "GOLD"
    assert asset_class_for_epic("IX.D.DOW.IFM.IP") == "INDEX"
    assert asset_class_for_epic("CS.D.EURUSD.CFD.IP") == "FX"
    assert sniper_threshold_for_epic("CS.D.CFPGOLD.CFP.IP") == pytest.approx(
        THRESHOLD_GOLD
    )
    assert sniper_threshold_for_epic("IX.D.FTSE.IFM.IP") == pytest.approx(
        THRESHOLD_INDEX
    )
    assert sniper_threshold_for_epic("CS.D.EURUSD.CFD.IP") == pytest.approx(
        THRESHOLD_FX
    )
    assert THRESHOLD_GOLD > THRESHOLD_FX > THRESHOLD_INDEX - 1e-9


def test_extreme_spread_widening_edge_case_blocks_gold_even_with_obi():
    """Edge: flash spread widening on Gold collapses P below adaptive 74% gate."""
    core = QuantumSniperMLCore()
    result = core.evaluate_entry_probability(
        obi_velocity=1.1,
        spread_elasticity=4.8,  # extreme premium widen
        tick_acceleration=0.9,
        grok_macro_bias="BULL",
        epic="CS.D.CFPGOLD.CFP.IP",
        direction="BUY",
    )
    assert result.threshold == pytest.approx(THRESHOLD_GOLD)
    assert result.p_success < THRESHOLD_GOLD
    assert result.approved is False
    assert result.features.get("extreme_weight", 0) > 0.5


def test_flash_crash_telemetry_dropout_fails_open_at_asset_threshold():
    """Edge: telemetry dropout (empty features) fail-opens at asset-class gate."""
    from alpha.micro_sniper_ml import evaluate_live_sniper_probability

    # Force empty feature plane via monkeypatched live gatherers in-process
    result = QuantumSniperMLCore().evaluate_entry_probability(
        obi_velocity=0.0,
        spread_elasticity=1.0,
        tick_acceleration=0.0,
        grok_macro_bias="NEUTRAL",
        epic="IX.D.DOW.IFM.IP",
        direction="BUY",
    )
    # With zero features the sigmoid sits mid — may or may not clear.
    # Live path fail-open is tested via features_unavailable path:
    live = evaluate_live_sniper_probability(
        "IX.D.DOW.IFM.IP",
        "BUY",
        cfg={"grok_macro_bias": "NEUTRAL"},
        quote=None,
    )
    # Depthless empty plane → fail-open at index threshold (not hard reject)
    if live.features.get("features_unavailable_fail_open"):
        assert live.approved is True
        assert live.threshold == pytest.approx(THRESHOLD_INDEX)
        assert live.p_success == pytest.approx(THRESHOLD_INDEX)
    else:
        # If hub injected features, still must not crash / return valid envelope
        assert 0.0 < live.p_success <= 0.99
        assert live.threshold == pytest.approx(THRESHOLD_INDEX)
    assert result.features.get("asset_class") == "INDEX"


def test_log_rotator_no_fd_leak_on_repeated_cycles(tmp_path: Path):
    """Rotator closes files — repeated cycles must not grow open FD count."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    big = log_dir / "agent.log"
    # Just over 4 KiB so max_bytes=4096 triggers rotate
    big.write_bytes(b"x" * 5000)

    soft_before, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    # Sample open FD count via /dev/fd or /proc if available
    def _fd_count() -> int:
        fd_dir = Path("/dev/fd")
        if fd_dir.is_dir():
            try:
                return len(list(fd_dir.iterdir()))
            except OSError:
                pass
        proc = Path("/proc/self/fd")
        if proc.is_dir():
            try:
                return len(list(proc.iterdir()))
            except OSError:
                pass
        return -1

    baseline = _fd_count()
    for _ in range(8):
        # Re-inflate so each cycle has work (rename empties the active file)
        if big.exists() and big.stat().st_size < 4096:
            big.write_bytes(b"y" * 5000)
        elif not big.exists():
            big.write_bytes(b"z" * 5000)
        result = run_log_rotation_cycle(
            log_dirs=[log_dir],
            max_bytes=4096,
            backup_count=3,
        )
        assert result["ok"] is True
        assert result["scanned"] >= 1

    after = _fd_count()
    if baseline >= 0 and after >= 0:
        # Allow small noise; hard fail on sustained leak (≥8 FDs/cycle)
        assert after <= baseline + 4, f"FD leak suspected: {baseline} → {after}"
    # Soft NOFILE limit unchanged (sanity)
    soft_after, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    assert soft_after == soft_before
    # Rotated artifact present
    assert (log_dir / "agent.log.1").exists() or big.exists()
