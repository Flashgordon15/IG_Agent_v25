"""Demo throughput execution plane — relax strategy/hard/unified guards for Core B soak."""

from __future__ import annotations

from typing import Any


def _throughput_block(cfg: Any | None = None) -> dict[str, Any]:
    if cfg is not None:
        try:
            raw = cfg.get("demo_throughput_mode") or {}
            return raw if isinstance(raw, dict) else {}
        except (AttributeError, TypeError):
            pass
    try:
        from system.config_loader import get_config

        raw = get_config().get("demo_throughput_mode") or {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def demo_throughput_active(cfg: Any | None = None) -> bool:
    return bool(_throughput_block(cfg).get("enabled"))


def execution_guards_relaxed(*, epic: str = "", cfg: Any | None = None) -> bool:
    """
    When demo throughput is armed, Core B micro / Path B handoff bypass
    strategy controller, hard enforcement, soft enforcement, and unified route blocks.
    """
    if not demo_throughput_active(cfg):
        return False
    block = _throughput_block(cfg)
    if not bool(block.get("bypass_execution_guards", True)):
        return False
    allowed = block.get("epics") or []
    if allowed and epic and epic not in allowed:
        return False
    return True


def demo_pierce_z_threshold(cfg: Any | None = None, default: float = 2.0) -> float:
    """Lower Z pierce bar during demo throughput (default ±2.0)."""
    block = _throughput_block(cfg)
    try:
        raw = block.get("pierce_z_threshold")
        if raw is not None:
            return max(0.5, float(raw))
    except (TypeError, ValueError):
        pass
    return default


def demo_order_cadence_sec(cfg: Any | None = None, default: float = 20.0) -> float:
    """Minimum spacing between live order transmits during demo soak."""
    block = _throughput_block(cfg)
    try:
        raw = block.get("order_cadence_sec")
        if raw is not None:
            cadence = float(raw)
            return 0.0 if cadence <= 0 else max(5.0, cadence)
    except (TypeError, ValueError):
        pass
    return max(5.0, float(default))


def demo_unlimited_open_positions(cfg: Any | None = None) -> bool:
    """When true, micro-scalper and correlation guard skip open-book caps."""
    if not demo_throughput_active(cfg):
        return False
    return bool(_throughput_block(cfg).get("unlimited_open_positions"))


def demo_unlimited_daily_trades(cfg: Any | None = None) -> bool:
    """When true, daily/session trade counters are not enforced."""
    if not demo_throughput_active(cfg):
        return False
    block = _throughput_block(cfg)
    if bool(block.get("unlimited_daily_trades")):
        return True
    try:
        return int(block.get("max_daily_trades") or 0) <= 0
    except (TypeError, ValueError):
        return False


def demo_micro_scalper_max_open(cfg: Any | None = None) -> int | None:
    """
    Max concurrent opens for Core B micro-scalper.
    None = unlimited (no position_already_open gate).
    """
    if demo_unlimited_open_positions(cfg):
        return None
    block = _throughput_block(cfg)
    try:
        raw = block.get("max_concurrent_open_positions")
        if raw is not None:
            cap = int(raw)
            return None if cap <= 0 else cap
    except (TypeError, ValueError):
        pass
    return 1


def arm_demo_unlimited_trading_session(*, clear_counts: bool = True) -> None:
    """Runtime hook — disable trade caps for demo throughput soak."""
    if not demo_unlimited_daily_trades():
        return
    try:
        from trading.entry_protection import inject_unlimited_trades_for_session

        inject_unlimited_trades_for_session(clear_counts=clear_counts)
    except Exception:
        pass
