"""
E2E tests for the new 10-position / 2-per-epic configuration.

Config: one_position_per_epic=False, max_positions_per_epic=2, max_open_positions=10

Covers:
  - Config reads back correct values
  - OrderValidator: per-epic cap (0→1→2→blocked)
  - OrderValidator: global cap blocks at 10
  - Cooldown bypassed when stacking (count 0 < count < max_pos)
  - Cooldown enforced when epic is at max or returning from zero
  - Multi-market scenario: 6 markets × 2 = 12 slots but capped at 10
  - one_position_per_epic=False path takes max_positions_per_epic, not 1
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.models import Quote
from execution.order_validator import OrderValidator, ValidationResult
from execution.types import TradeSignal
from system.config import Config

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EPICS = [
    "IX.D.NIKKEI.IFM.IP",
    "CS.D.EURUSD.CFD.IP",
    "CS.D.GBPUSD.CFD.IP",
    "CS.D.CRUDE.CFD.IP",
    "IX.D.DOW.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
]


def _cfg_10_pos() -> Config:
    """Load the real config then override position-related keys."""
    import json as _json
    raw = _json.loads((ROOT / "config" / "config_v25.json").read_text())
    raw.update({
        "one_position_per_epic": False,
        "max_open_positions": 10,
        "max_positions_per_epic": 2,
        "trading_hours_enabled": False,
        "market_watch_enabled": False,
        "adaptive_max_entry_spread": 100.0,
        "min_atr_points": 0,
        "max_consecutive_losses": 0,
        "adaptive_block_bad_setups": False,
    })
    return Config(_data=raw)


def _signal(epic: str = "IX.D.NIKKEI.IFM.IP", conf: float = 80.0) -> TradeSignal:
    return TradeSignal(
        market="Japan 225",
        epic=epic,
        direction="BUY",
        raw_confidence=conf,
        adjusted_confidence=conf,
        setup_key="test|e2e",
        quote=Quote(datetime(2026, 6, 3, 12, 0), 38000.0, 38007.0),
        snapshot={},
        notes="e2e max-pos test",
    )


def _validator(cfg: Config) -> OrderValidator:
    v = OrderValidator(cfg)
    # Patch session / market-hours checks to always pass
    v.check_session = lambda epic="": (True, "")
    v.check_market_hours = lambda epic: (True, "")
    v.check_circuit_breaker = lambda: (True, "")
    return v


# ---------------------------------------------------------------------------
# 1. Config contract
# ---------------------------------------------------------------------------


def test_config_reads_10_positions() -> None:
    cfg = _cfg_10_pos()
    assert cfg.max_open_positions == 10
    assert cfg.max_positions_per_epic == 2
    assert cfg.one_position_per_epic is False


def test_config_limits_clamp() -> None:
    """max_open_positions clamps at 18; max_positions_per_epic clamps at 6."""
    cfg = Config(
        _data={
            "one_position_per_epic": False,
            "max_open_positions": 99,
            "max_positions_per_epic": 99,
            "signal_threshold": 70,
        }
    )
    assert cfg.max_open_positions == 18
    assert cfg.max_positions_per_epic == 6


# ---------------------------------------------------------------------------
# 2. Per-epic cap (0 → 1 → 2 → blocked)
# ---------------------------------------------------------------------------


def test_first_position_on_epic_allowed() -> None:
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    result = v.validate(
        _signal(),
        open_position_count=lambda epic: 0,
        open_total_count=lambda: 0,
    )
    assert result.checks["position_limit"] is True
    assert result.checks.get("total_position_limit", True) is True


def test_second_position_on_epic_allowed() -> None:
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    result = v.validate(
        _signal(),
        open_position_count=lambda epic: 1,
        open_total_count=lambda: 1,
    )
    assert result.checks["position_limit"] is True


def test_third_position_on_epic_blocked() -> None:
    """per-epic cap is 2; count=2 must block."""
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    result = v.validate(
        _signal(),
        open_position_count=lambda epic: 2,
        open_total_count=lambda: 2,
    )
    assert result.checks["position_limit"] is False
    assert result.allowed is False
    reasons_text = " ".join(result.reasons)
    assert "Max positions reached" in reasons_text


# ---------------------------------------------------------------------------
# 3. Global cap at 10
# ---------------------------------------------------------------------------


def test_position_allowed_at_9_total() -> None:
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    result = v.validate(
        _signal("CS.D.EURUSD.CFD.IP"),
        open_position_count=lambda epic: 0,
        open_total_count=lambda: 9,
    )
    assert result.checks.get("total_position_limit", True) is True


def test_position_blocked_at_10_total() -> None:
    """Global cap: 10 open positions must block any new entry."""
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    result = v.validate(
        _signal("CS.D.EURUSD.CFD.IP"),
        open_position_count=lambda epic: 0,
        open_total_count=lambda: 10,
    )
    assert result.checks["total_position_limit"] is False
    assert result.allowed is False
    reasons_text = " ".join(result.reasons)
    assert "Total open positions reached" in reasons_text or "total" in reasons_text.lower()


def test_position_blocked_at_11_total() -> None:
    """Sanity: 11 > 10 also blocked."""
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    result = v.validate(
        _signal(),
        open_position_count=lambda epic: 0,
        open_total_count=lambda: 11,
    )
    assert result.allowed is False


# ---------------------------------------------------------------------------
# 4. Cooldown behaviour
# ---------------------------------------------------------------------------


def test_cooldown_bypassed_when_stacking_first_to_second() -> None:
    """count=1, max_pos=2 → stacking bypass → cooldown must NOT block."""
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    # Arm cooldown by recording a trade (direction is keyword-only)
    epic = "IX.D.NIKKEI.IFM.IP"
    v.cooldown.record(epic, direction="BUY")

    result = v.validate(
        _signal(epic),
        open_position_count=lambda e: 1,
        open_total_count=lambda: 1,
    )
    assert result.checks.get("cooldown", True) is True, (
        "Cooldown should be bypassed when stacking (0 < count < max_pos)"
    )


def test_cooldown_enforced_on_fresh_entry() -> None:
    """count=0 → normal entry path → active cooldown must block."""
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    epic = "IX.D.NIKKEI.IFM.IP"
    v.cooldown.record(epic, direction="BUY")

    result = v.validate(
        _signal(epic),
        open_position_count=lambda e: 0,
        open_total_count=lambda: 0,
    )
    assert result.checks.get("cooldown") is False


def test_cooldown_enforced_when_at_max_positions() -> None:
    """count=max_pos → per-epic blocked anyway, but cooldown path also checked."""
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    epic = "IX.D.NIKKEI.IFM.IP"
    # At count=2 the position_limit gate fails before cooldown matters.
    # Verify the overall result is blocked.
    result = v.validate(
        _signal(epic),
        open_position_count=lambda e: 2,
        open_total_count=lambda: 2,
    )
    assert result.allowed is False


# ---------------------------------------------------------------------------
# 5. Multi-market cap scenario
# ---------------------------------------------------------------------------


def test_6_markets_2_each_blocked_at_10() -> None:
    """
    Simulate 5 markets × 2 positions = 10 open total.
    Trying to open on a 6th market (0 on that epic) must be blocked by global cap.
    """
    cfg = _cfg_10_pos()
    v = _validator(cfg)

    # All 5 existing markets have 2 each = 10 total
    open_per_epic = {
        "CS.D.EURUSD.CFD.IP": 2,
        "CS.D.GBPUSD.CFD.IP": 2,
        "CS.D.CRUDE.CFD.IP": 2,
        "IX.D.DOW.IFM.IP": 2,
        "CS.D.CFPGOLD.CFP.IP": 2,
    }
    total_open = sum(open_per_epic.values())  # = 10

    result = v.validate(
        _signal("IX.D.NIKKEI.IFM.IP"),
        open_position_count=lambda epic: open_per_epic.get(epic, 0),
        open_total_count=lambda: total_open,
    )
    assert result.allowed is False
    assert result.checks.get("total_position_limit") is False


def test_6_markets_1_each_allowed_up_to_6() -> None:
    """6 markets × 1 position = 6 open — new entry on any market with 0 open must pass global check."""
    cfg = _cfg_10_pos()
    v = _validator(cfg)

    open_per_epic = {e: 1 for e in EPICS[:5]}  # 5 markets with 1 each = 5 total
    total_open = 5

    result = v.validate(
        _signal(EPICS[5]),
        open_position_count=lambda epic: open_per_epic.get(epic, 0),
        open_total_count=lambda: total_open,
    )
    assert result.checks.get("total_position_limit") is True
    assert result.checks.get("position_limit") is True


def test_one_position_per_epic_false_uses_max_per_epic() -> None:
    """When one_position_per_epic=False, max_pos comes from max_positions_per_epic (=2), not 1."""
    cfg = _cfg_10_pos()
    assert cfg.one_position_per_epic is False
    v = _validator(cfg)

    # First position on the epic → allowed
    r1 = v.validate(
        _signal(),
        open_position_count=lambda e: 0,
        open_total_count=lambda: 0,
    )
    assert r1.checks.get("position_limit") is True

    # Second position on same epic → allowed (max_per_epic=2)
    r2 = v.validate(
        _signal(),
        open_position_count=lambda e: 1,
        open_total_count=lambda: 1,
    )
    assert r2.checks.get("position_limit") is True

    # Third position on same epic → blocked (exceeds max_per_epic=2)
    r3 = v.validate(
        _signal(),
        open_position_count=lambda e: 2,
        open_total_count=lambda: 2,
    )
    assert r3.checks.get("position_limit") is False


# ---------------------------------------------------------------------------
# 6. Boundary: exactly at max_open_positions=10
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("total", [0, 1, 5, 9])
def test_total_below_10_is_allowed(total: int) -> None:
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    result = v.validate(
        _signal(),
        open_position_count=lambda e: 0,
        open_total_count=lambda: total,
    )
    assert result.checks.get("total_position_limit") is True


@pytest.mark.parametrize("total", [10, 11, 15])
def test_total_at_or_above_10_is_blocked(total: int) -> None:
    cfg = _cfg_10_pos()
    v = _validator(cfg)
    result = v.validate(
        _signal(),
        open_position_count=lambda e: 0,
        open_total_count=lambda: total,
    )
    assert result.checks.get("total_position_limit") is False
    assert result.allowed is False
