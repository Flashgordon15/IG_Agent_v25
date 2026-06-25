"""
Dual-core execution plane — Macro Breakout Sentinel + Micro-Slippage Scalper.

Volatility compression (Z < 2.44 demo ceiling) arms ENGINE_B_MICRO_SCALPER mean-reversion
harvesting; Z < 0.00 marks high-conviction compressed setups; expansion (Z >= 2.45)
favours MACRO_BREAKOUT_SENTINEL.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from system.engine_log import log_engine
from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

MACRO_Z_THRESHOLD = 2.45
MICRO_Z_THRESHOLD = 2.44  # demo: arm Core B for all non-macro Z (incl. neutral dead zone)
MICRO_HIGH_CONVICTION_Z = 0.00  # Z below this = highly valid compressed setup
# Temporary manual override — any rolling Z in this band arms Core B (clears Gate 5 dead zone).
CORE_B_FORCE_CHANNEL_Z_MIN = -2.00
CORE_B_FORCE_CHANNEL_Z_MAX = 2.00
CORE_B_FORCE_CHANNEL_OVERRIDE = True
DEMO_BYPASS_15M_MACRO_TREND_LOCK = True
CORE_B_SATELLITE_UNCOUPLED = True  # test profile: Core B ignores 15m macro directional lock
CANARY_FX_LOT = 1.0
CANARY_INDEX_LOT = 0.5
CANARY_GOLD_LOT = 1.0
PRIMARY_STACKED_EPIC = "IX.D.DOW.IFM.IP"
SECONDARY_STACKED_EPIC = "CS.D.CFPGOLD.CFP.IP"
STACKED_DUAL_ASSETS: tuple[str, ...] = (PRIMARY_STACKED_EPIC, SECONDARY_STACKED_EPIC)
STACKED_POLL_SEC = 1.0
MODE_MACRO = "MACRO_BREAKOUT_SENTINEL"
MODE_MICRO = "LIGHTNING_MICRO_SCALPER"
MODE_NEUTRAL = "NEUTRAL"
ENGINE_B_MICRO_SCALPER = "ENGINE_B_MICRO_SCALPER"

MICRO_TP_POINTS = 1.5
MICRO_SL_POINTS = 2.0
Z_ROLLING_WINDOW = 20  # 20-tick rolling volatility window (telemetry export)
# Legacy alias — stacked dual-asset mode replaces erratic cascade switching.
COGNITIVE_CASCADE_EPICS = STACKED_DUAL_ASSETS
CASCADE_SWEEP_SEC = STACKED_POLL_SEC
_SHORT_WINDOW = 30
_LONG_WINDOW = 120
_MIN_SAMPLES = 12
_Z_HISTORY_MAX = 120


@dataclass
class DualCoreSnapshot:
    volatility_z_score: float
    execution_mode: str
    core_a_macro_active: bool
    core_b_micro_active: bool
    engine_b_armed: bool
    micro_channel_upper: float | None
    micro_channel_lower: float | None
    epic: str
    live_calculated_zscore: float = 0.0
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "volatility_z_score": round(self.volatility_z_score, 4),
            "live_calculated_zscore": round(self.live_calculated_zscore, 4),
            "execution_mode": self.execution_mode,
            "core_a_macro_active": self.core_a_macro_active,
            "core_b_micro_active": self.core_b_micro_active,
            "engine_b_armed": self.engine_b_armed,
            "micro_channel_upper": self.micro_channel_upper,
            "micro_channel_lower": self.micro_channel_lower,
            "epic": self.epic,
            "updated_at": self.updated_at,
        }


_lock = threading.Lock()
_snapshot = DualCoreSnapshot(
    volatility_z_score=0.0,
    live_calculated_zscore=0.0,
    execution_mode=MODE_NEUTRAL,
    core_a_macro_active=False,
    core_b_micro_active=False,
    engine_b_armed=False,
    micro_channel_upper=None,
    micro_channel_lower=None,
    epic="",
)
_mid_history: dict[str, deque[float]] = {
    epic: deque(maxlen=_LONG_WINDOW) for epic in NIGHT_MATRIX_EPICS
}
_width_history: dict[str, deque[float]] = {
    epic: deque(maxlen=_LONG_WINDOW) for epic in NIGHT_MATRIX_EPICS
}
_z_history: deque[float] = deque(maxlen=_Z_HISTORY_MAX)
_z_history_by_epic: dict[str, deque[float]] = {
    epic: deque(maxlen=_Z_HISTORY_MAX) for epic in STACKED_DUAL_ASSETS
}
_snapshots: dict[str, DualCoreSnapshot] = {}
_last_gate_suppression_reason: str = ""
_execution_focus_target: str = PRIMARY_STACKED_EPIC
_focus_tick_velocity: float = 0.0
_velocity_by_epic: dict[str, float] = {}
_last_mid_by_epic: dict[str, float] = {}
_tick_arrivals: dict[str, deque[float]] = {
    epic: deque(maxlen=256) for epic in STACKED_DUAL_ASSETS
}
_ml_dynamic_overrides: dict[str, Any] = {}
_ml_sovereignty_active: bool = False
_stacked_stop = threading.Event()
_stacked_thread: threading.Thread | None = None


def epic_display_name(epic: str) -> str:
    e = str(epic or "").upper()
    if "DOW" in e:
        return "Wall Street"
    if "CFPGOLD" in e or "GOLD" in e:
        return "Gold"
    if "EURUSD" in e:
        return "EUR/USD"
    return epic or "UNKNOWN"


def get_execution_focus_target() -> str:
    with _lock:
        return str(_execution_focus_target or "")


def get_effective_micro_z_threshold() -> float:
    with _lock:
        return float(_ml_dynamic_overrides.get("micro_z_threshold", MICRO_Z_THRESHOLD))


def get_effective_micro_tp_sl() -> tuple[float, float]:
    with _lock:
        tp = float(_ml_dynamic_overrides.get("micro_tp_points", MICRO_TP_POINTS))
        sl = float(_ml_dynamic_overrides.get("micro_sl_points", MICRO_SL_POINTS))
    return tp, sl


def apply_ml_cognitive_overrides(epic: str, overrides: dict[str, Any]) -> None:
    global _ml_dynamic_overrides, _ml_sovereignty_active
    with _lock:
        _ml_dynamic_overrides = dict(overrides)
        _ml_sovereignty_active = True
        _execution_focus_target = str(epic or _execution_focus_target)


def get_execution_focus_state() -> dict[str, Any]:
    channels = get_stacked_asset_channels()
    primary = get_dual_core_snapshot()
    z_stream = get_z_score_stream(PRIMARY_STACKED_EPIC)
    with _lock:
        return {
            "stacked_dual_asset_mode": True,
            "execution_focus_target": PRIMARY_STACKED_EPIC,
            "execution_focus_label": "Wall Street + Gold",
            "focus_tick_velocity": round(float(_focus_tick_velocity), 6),
            "velocity_by_epic": {k: round(v, 6) for k, v in _velocity_by_epic.items()},
            "focus_volatility_z": round(float(primary.volatility_z_score), 4),
            "focus_live_calculated_zscore": round(float(primary.live_calculated_zscore), 4),
            "focus_z_score_stream": z_stream,
            "stacked_asset_channels": channels,
            "ml_strategy_sovereignty": bool(_ml_sovereignty_active),
            "ml_dynamic_params": dict(_ml_dynamic_overrides),
        }


def get_stacked_snapshots() -> dict[str, DualCoreSnapshot]:
    with _lock:
        out: dict[str, DualCoreSnapshot] = {}
        for epic in STACKED_DUAL_ASSETS:
            snap = _snapshots.get(epic)
            if snap is not None:
                out[epic] = DualCoreSnapshot(**snap.__dict__)
            elif epic == PRIMARY_STACKED_EPIC:
                out[epic] = DualCoreSnapshot(**_snapshot.__dict__)
        return out


def get_stacked_asset_channels() -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []
    roles = ("PRIMARY", "SECONDARY")
    for idx, epic in enumerate(STACKED_DUAL_ASSETS):
        snap = get_stacked_snapshots().get(epic) or get_dual_core_snapshot()
        channels.append(
            {
                "epic": epic,
                "label": epic_display_name(epic),
                "role": roles[idx] if idx < len(roles) else "STACKED",
                "live_calculated_zscore": round(float(snap.live_calculated_zscore), 4),
                "volatility_z_score": round(float(snap.volatility_z_score), 4),
                "z_score_stream": get_z_score_stream(epic),
                "core_b_micro_active": bool(snap.core_b_micro_active),
                "execution_mode": snap.execution_mode,
                "canary_lot": canary_lot_size(epic),
            }
        )
    return channels


def get_z_score_stream(epic: str | None = None) -> list[float]:
    key = str(epic or PRIMARY_STACKED_EPIC).strip()
    with _lock:
        hist = _z_history_by_epic.get(key)
        if hist:
            return [round(v, 4) for v in list(hist)]
        return [round(v, 4) for v in list(_z_history)]


def _record_tick_velocity(epic: str, mid: float) -> float:
    """Composite tick velocity — arrivals per 500ms + normalized price impulse."""
    now = time.time()
    key = str(epic or "").strip()
    arrivals = _tick_arrivals.setdefault(key, deque(maxlen=256))
    arrivals.append(now)
    window = CASCADE_SWEEP_SEC
    tick_count = sum(1 for t in arrivals if now - t <= window)
    prev = _last_mid_by_epic.get(key)
    prev_impulse = abs(mid - prev) if prev is not None and prev > 0 else 0.0
    _last_mid_by_epic[key] = mid
    norm = prev_impulse / max(mid, 1e-9)
    return float(tick_count) + norm * 1000.0


def _elect_focus_from_velocity() -> str:
    best_epic = ""
    best_vel = -1.0
    for epic, vel in _velocity_by_epic.items():
        if vel > best_vel:
            best_vel = vel
            best_epic = epic
    return best_epic


def refresh_stacked_dual_assets() -> dict[str, DualCoreSnapshot | None]:
    """Parallel refresh — both Wall Street and Gold tracked every poll cycle."""
    hub = get_market_data_hub()
    results: dict[str, DualCoreSnapshot | None] = {}
    for epic in STACKED_DUAL_ASSETS:
        quote = hub.get_snapshot(epic)
        if quote is None or quote.bid <= 0 or quote.offer <= 0:
            results[epic] = None
            continue
        mid = (float(quote.bid) + float(quote.offer)) / 2.0
        results[epic] = ingest_hub_mid(epic, mid)
    return results


def refresh_focus_execution_plane() -> DualCoreSnapshot | None:
    """Legacy alias — stacked dual-asset refresh (no single-asset zone-in)."""
    refresh_stacked_dual_assets()
    return get_dual_core_snapshot()


def cognitive_cascade_sweep_once() -> str | None:
    """Legacy alias — stacked refresh returns primary epic."""
    refresh_stacked_dual_assets()
    return PRIMARY_STACKED_EPIC


def _stacked_dual_loop() -> None:
    while not _stacked_stop.wait(STACKED_POLL_SEC):
        try:
            refresh_stacked_dual_assets()
        except Exception as exc:
            log_engine(f"StackedDualAsset: refresh error {type(exc).__name__}: {exc}")


def start_stacked_dual_asset_tracks() -> None:
    global _stacked_thread, _execution_focus_target
    stop_cognitive_cascade()
    _execution_focus_target = PRIMARY_STACKED_EPIC
    if _stacked_thread is not None and _stacked_thread.is_alive():
        return
    _stacked_stop.clear()
    _stacked_thread = threading.Thread(
        target=_stacked_dual_loop, name="stacked-dual-asset", daemon=True
    )
    _stacked_thread.start()
    log_engine(
        f"StackedDualAsset: parallel tracks armed epics={list(STACKED_DUAL_ASSETS)} "
        f"poll={STACKED_POLL_SEC}s"
    )


def start_cognitive_cascade() -> None:
    """Legacy entry — routes to stacked dual-asset architecture."""
    start_stacked_dual_asset_tracks()


def stop_cognitive_cascade() -> None:
    _stacked_stop.set()


def stop_stacked_dual_asset_tracks() -> None:
    stop_cognitive_cascade()


def reset_cognitive_cascade_for_tests() -> None:
    global _execution_focus_target, _focus_tick_velocity, _ml_sovereignty_active
    stop_cognitive_cascade()
    with _lock:
        _execution_focus_target = PRIMARY_STACKED_EPIC
        _focus_tick_velocity = 0.0
        _velocity_by_epic.clear()
        _ml_dynamic_overrides.clear()
        _ml_sovereignty_active = False
        _z_history.clear()
        _snapshots.clear()
        for hist in _z_history_by_epic.values():
            hist.clear()


def get_dual_core_snapshot() -> DualCoreSnapshot:
    with _lock:
        return DualCoreSnapshot(**_snapshot.__dict__)


def dual_core_status_dict() -> dict[str, Any]:
    snap = get_dual_core_snapshot()
    stacked = get_stacked_snapshots()
    any_micro = any(s.core_b_micro_active for s in stacked.values())
    return {
        **snap.as_dict(),
        "core_b_micro_active": any_micro,
        "engine_b_armed": any_micro,
        "stacked_dual_asset_mode": True,
        "stacked_asset_channels": get_stacked_asset_channels(),
        "dual_core": {
            "label": "⚡ SYSTEM DUAL-CORE STATUS",
            "core_a": {
                "id": "CORE_A",
                "name": "MACRO_BREAKOUT",
                "active": snap.core_a_macro_active,
                "threshold_z": MACRO_Z_THRESHOLD,
            },
            "core_b": {
                "id": "CORE_B",
                "name": "MICRO_SCALPER",
                "active": any_micro,
                "engine": ENGINE_B_MICRO_SCALPER,
                "threshold_z": get_effective_micro_z_threshold(),
            },
        },
    }


def set_last_gate_suppression_reason(reason: str) -> None:
    global _last_gate_suppression_reason
    _last_gate_suppression_reason = str(reason or "").strip()


def get_last_gate_suppression_reason() -> str:
    return _last_gate_suppression_reason


def is_high_conviction_z(z: float) -> bool:
    """True when volatility Z is below zero — compressed, high-validity micro setup."""
    return float(z) < MICRO_HIGH_CONVICTION_Z


def is_force_channel_z(z: float) -> bool:
    """Manual override — rolling Z in [-2, +2] is a 100% valid micro-channel entry."""
    if not CORE_B_FORCE_CHANNEL_OVERRIDE:
        return False
    zf = float(z)
    return CORE_B_FORCE_CHANNEL_Z_MIN <= zf <= CORE_B_FORCE_CHANNEL_Z_MAX


def is_core_b_satellite_uncoupled() -> bool:
    """True when Core B micro-scalper runs outside the 15m macro trend satellite."""
    return bool(DEMO_BYPASS_15M_MACRO_TREND_LOCK and CORE_B_SATELLITE_UNCOUPLED)


def macro_15min_trend_allows_direction(direction: str, epic: str | None = None) -> bool:
    """15m EMA+RSI directional lock — bypassed when Core B satellite is uncoupled."""
    if is_core_b_satellite_uncoupled():
        return True
    trend = resolve_live_15min_macro_trend(epic)
    d = str(direction or "").upper()
    if trend == "BULLISH":
        return d == "BUY"
    if trend == "BEARISH":
        return d == "SELL"
    return trend != "MIXED"


def resolve_core_b_gate_stack() -> dict[str, Any]:
    """
    Live boolean gate matrix for Core B micro-scalper path (GUI diagnostic read-out).

    Gate 3 — stream coupled (hub quote fresh)
    Gate 4 — macro trend protection (uncoupled in test profile)
    Gate 5 — risk netting (process blocks, kill switch, REST budget)
    """
    snap = get_dual_core_snapshot()
    epic = str(snap.epic or PRIMARY_STACKED_EPIC)
    trend = resolve_live_15min_macro_trend(epic)
    uncoupled = is_core_b_satellite_uncoupled()

    stream_ok = True
    stream_detail_parts: list[str] = []
    try:
        hub = get_market_data_hub()
        for stacked_epic in STACKED_DUAL_ASSETS:
            quote = hub.get_snapshot(stacked_epic)
            if quote is None or quote.bid <= 0 or quote.offer <= 0:
                stream_ok = False
                stream_detail_parts.append(f"{epic_display_name(stacked_epic)}=missing")
                continue
            age = round(float(quote.age_seconds()), 2)
            fresh = age <= 45.0
            stream_detail_parts.append(f"{epic_display_name(stacked_epic)} age={age}s")
            if not fresh:
                stream_ok = False
    except Exception:
        stream_ok = False

    if stream_ok:
        g3 = {
            "gate": 3,
            "name": "Stream Coupled",
            "status": "PASSED",
            "detail": " · ".join(stream_detail_parts),
        }
    else:
        g3 = {
            "gate": 3,
            "name": "Stream Coupled",
            "status": "WAITING",
            "detail": " · ".join(stream_detail_parts) or "awaiting stacked quotes",
        }

    if uncoupled:
        g4 = {
            "gate": 4,
            "name": "Macro Trend Protection",
            "status": "UNCOUPLED",
            "detail": f"satellite bypass — 15m={trend} ignored for Core B mean-reversion",
            "macro_trend": trend,
            "blocking": False,
        }
    else:
        buy_ok = macro_15min_trend_allows_direction("BUY", epic)
        sell_ok = macro_15min_trend_allows_direction("SELL", epic)
        if trend == "BEARISH" and not buy_ok:
            g4_status = "BLOCKING"
        elif trend == "BULLISH" and not sell_ok:
            g4_status = "BLOCKING"
        elif trend in ("MIXED", "UNKNOWN"):
            g4_status = "MUTED"
        else:
            g4_status = "PASSED"
        g4 = {
            "gate": 4,
            "name": "Macro Trend Protection",
            "status": g4_status,
            "detail": f"15m macro={trend} buy_ok={buy_ok} sell_ok={sell_ok}",
            "macro_trend": trend,
            "blocking": g4_status == "BLOCKING",
        }

    risk_reasons: list[str] = []
    try:
        from runtime.strategy_kill_switch import is_strategy_kill_active

        if is_strategy_kill_active():
            risk_reasons.append("BROKER_STATE_MISMATCH")
    except Exception:
        pass
    try:
        from system.qmm_process_supervisor import process_entry_blocked

        blocked, detail = process_entry_blocked()
        if blocked and detail:
            risk_reasons.append(detail)
    except Exception:
        pass
    try:
        from api.agent_control import is_paused

        if is_paused():
            risk_reasons.append("api_trading_paused")
    except Exception:
        pass
    try:
        from system.rest_api_budget import get_rest_api_budget

        if get_rest_api_budget()._preemptive_pause_active():
            risk_reasons.append("rest_budget_preemptive_pause")
    except Exception:
        pass

    stacked = get_stacked_snapshots()
    any_micro = any(s.core_b_micro_active for s in stacked.values())
    if not any_micro:
        risk_reasons.append("core_b_not_armed")
    if risk_reasons:
        g5_status = "BLOCKING" if any(
            r in ("BROKER_STATE_MISMATCH", "COCKPIT_EMERGENCY_OVERRIDE", "MASTER_KILL_SWITCH_ACTIVE")
            for r in risk_reasons
        ) else "WAITING"
        g5_detail = "; ".join(risk_reasons)
    else:
        g5_status = "PASSED"
        g5_detail = "risk net clear — execution valve open"

    g5 = {"gate": 5, "name": "Risk Netting", "status": g5_status, "detail": g5_detail}

    all_clear = (
        g3["status"] == "PASSED"
        and g4["status"] in ("PASSED", "UNCOUPLED")
        and g5["status"] == "PASSED"
    )
    return {
        "core_b_satellite_uncoupled": uncoupled,
        "live_15min_macro_trend": trend,
        "gates": [g3, g4, g5],
        "all_clear": all_clear,
        "summary_lines": [
            f"Gate 3 ({g3['name']}): {g3['status']}",
            f"Gate 4 ({g4['name']}): {g4['status']}",
            f"Gate 5 ({g5['name']}): {g5['status']}",
        ],
    }


def resolve_live_15min_macro_trend(epic: str | None = None) -> str:
    """Current 15m macro bar directional alignment (BULLISH/BEARISH/MIXED/UNKNOWN)."""
    target = str(epic or "").strip()
    if not target:
        target = str(get_dual_core_snapshot().epic or "").strip()
    if not target and NIGHT_MATRIX_EPICS:
        target = NIGHT_MATRIX_EPICS[0]
    try:
        from api.agent_control import get_trading_loop

        orch = get_trading_loop()
        loops = list(getattr(orch, "loops", []) or []) if orch else []
        if not loops:
            return "UNKNOWN"
        for loop in loops:
            loop_epic = str(getattr(loop, "_epic", "") or "")
            market = str(getattr(loop, "_market", "") or "")
            if target and loop_epic and loop_epic != target:
                continue
            se = getattr(loop, "_signal_engine", None)
            if se is None or not market:
                continue
            _, _, c15, _ = se.candle_frames(market)
            if c15 is None or len(c15) < 2:
                continue
            row = c15.iloc[-2]
            if hasattr(se, "add_indicators"):
                c15i = se.add_indicators(c15)
                row = c15i.iloc[-2]
            fast = float(row.get("fast_ema", 0))
            slow = float(row.get("slow_ema", 0))
            if fast > slow:
                return "BULLISH"
            if fast < slow:
                return "BEARISH"
            return "MIXED"
    except Exception:
        pass
    return "UNKNOWN"


def _resolve_mode(z: float) -> tuple[str, bool, bool]:
    zf = float(z)
    if zf >= MACRO_Z_THRESHOLD:
        return MODE_MACRO, True, False
    if is_force_channel_z(zf):
        return MODE_MICRO, False, True
    micro_ceiling = get_effective_micro_z_threshold()
    if zf < micro_ceiling:
        return MODE_MICRO, False, True
    return MODE_NEUTRAL, False, False


def _z_score_from_widths(widths: deque[float], current: float) -> float:
    if len(widths) < _MIN_SAMPLES:
        return 0.0
    vals = list(widths)
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / max(len(vals) - 1, 1)
    std = math.sqrt(max(var, 1e-12))
    return (current - mean) / std


def ingest_hub_mid(epic: str, mid: float) -> DualCoreSnapshot | None:
    """Feed a live mid — updates volatility Z and dual-core mode (non-blocking)."""
    key = str(epic or "").strip()
    if not key or mid <= 0:
        return None
    hist = _mid_history.setdefault(key, deque(maxlen=_LONG_WINDOW))
    hist.append(float(mid))
    if len(hist) < _SHORT_WINDOW:
        return None

    recent = list(hist)[-_SHORT_WINDOW:]
    rolling = list(hist)[-Z_ROLLING_WINDOW:]
    upper = max(recent)
    lower = min(recent)
    width = max(upper - lower, 0.0)
    roll_upper = max(rolling) if rolling else upper
    roll_lower = min(rolling) if rolling else lower
    roll_width = max(roll_upper - roll_lower, 0.0)
    widths = _width_history.setdefault(key, deque(maxlen=_LONG_WINDOW))
    widths.append(width)
    z = _z_score_from_widths(widths, width)
    live_z = _z_score_from_widths(widths, roll_width) if len(rolling) >= _MIN_SAMPLES else z
    mode, macro_on, micro_on = _resolve_mode(z)

    snap = DualCoreSnapshot(
        volatility_z_score=z,
        live_calculated_zscore=live_z,
        execution_mode=mode,
        core_a_macro_active=macro_on,
        core_b_micro_active=micro_on,
        engine_b_armed=micro_on,
        micro_channel_upper=upper,
        micro_channel_lower=lower,
        epic=key,
        updated_at=time.time(),
    )
    with _lock:
        global _snapshot
        _snapshots[key] = snap
        _z_history_by_epic.setdefault(key, deque(maxlen=_Z_HISTORY_MAX)).append(live_z)
        if key == PRIMARY_STACKED_EPIC:
            _snapshot = snap
            _z_history.append(live_z)
    return snap


def refresh_dual_core_from_hub() -> DualCoreSnapshot | None:
    """Poll hub — stacked dual-asset parallel refresh."""
    refresh_stacked_dual_assets()
    return get_dual_core_snapshot()


def _is_fx_epic(epic: str) -> bool:
    e = str(epic or "").upper()
    return "EURUSD" in e or "GBPUSD" in e or ".CFD.IP" in e and "EUR" in e


def canary_lot_size(epic: str, cfg: Any | None = None) -> float:
    """Strict canary clamp — 0.5 Wall St index / 1.0 Gold / 1.0 FX."""
    _ = cfg
    e = str(epic or "").upper()
    if "CFPGOLD" in e or "GOLD" in e:
        return CANARY_GOLD_LOT
    if "DOW" in e:
        return CANARY_INDEX_LOT
    if _is_fx_epic(epic):
        return CANARY_FX_LOT
    return CANARY_INDEX_LOT


def resolve_micro_stop_limit_points(rest_client: Any, epic: str) -> tuple[float, float]:
    """Floor TP/SL to broker minStopOrProfitDistance metadata."""
    from execution.live_broker_order_router import floor_stop_distance_points

    tp_pts, sl_pts = get_effective_micro_tp_sl()
    tp = floor_stop_distance_points(rest_client, epic, tp_pts).effective_points
    sl = floor_stop_distance_points(rest_client, epic, sl_pts).effective_points
    return float(tp), float(sl)


def evaluate_micro_scalp_signal(
    *,
    epic: str,
    bid: float,
    offer: float,
    snap: DualCoreSnapshot | None = None,
) -> str | None:
    """
    Mean-reversion: SELL at upper micro-channel, BUY at lower micro-channel.
    Returns 'BUY' | 'SELL' | None.
    """
    snap = snap or get_dual_core_snapshot()
    if not snap.core_b_micro_active or snap.epic != epic:
        set_last_gate_suppression_reason("core_b_inactive_or_epic_mismatch")
        return None
    if snap.micro_channel_upper is None or snap.micro_channel_lower is None:
        set_last_gate_suppression_reason("micro_channel_uninitialized")
        return None
    mid = (bid + offer) / 2.0
    span = max(snap.micro_channel_upper - snap.micro_channel_lower, 1e-9)
    z = float(snap.volatility_z_score)
    if is_force_channel_z(z):
        center = (snap.micro_channel_upper + snap.micro_channel_lower) / 2.0
        direction = "SELL" if mid >= center else "BUY"
        set_last_gate_suppression_reason("")
        return direction
    touch_pct = 0.08
    if is_high_conviction_z(z):
        touch_pct = 0.40
    elif DEMO_BYPASS_15M_MACRO_TREND_LOCK:
        touch_pct = 0.22
    touch = span * touch_pct
    direction: str | None = None
    if mid >= snap.micro_channel_upper - touch:
        direction = "SELL"
    elif mid <= snap.micro_channel_lower + touch:
        direction = "BUY"
    elif DEMO_BYPASS_15M_MACRO_TREND_LOCK and z < get_effective_micro_z_threshold():
        center = (snap.micro_channel_upper + snap.micro_channel_lower) / 2.0
        direction = "SELL" if mid >= center else "BUY"
    if direction is None:
        set_last_gate_suppression_reason("awaiting_micro_channel_touch")
        return None
    if not is_core_b_satellite_uncoupled():
        if not macro_15min_trend_allows_direction(direction, epic):
            set_last_gate_suppression_reason("15m_macro_trend_lock")
            return None
    set_last_gate_suppression_reason("")
    return direction


def reset_dual_core_for_tests() -> None:
    with _lock:
        global _snapshot
        _snapshot = DualCoreSnapshot(
            volatility_z_score=0.0,
            live_calculated_zscore=0.0,
            execution_mode=MODE_NEUTRAL,
            core_a_macro_active=False,
            core_b_micro_active=False,
            engine_b_armed=False,
            micro_channel_upper=None,
            micro_channel_lower=None,
            epic="",
        )
    _mid_history.clear()
    _width_history.clear()
    _z_history.clear()
    _snapshots.clear()
    for hist in _z_history_by_epic.values():
        hist.clear()
    reset_cognitive_cascade_for_tests()
    global _last_gate_suppression_reason
    _last_gate_suppression_reason = ""
