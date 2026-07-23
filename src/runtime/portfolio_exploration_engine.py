"""
Asynchronous capital deployment — multi-market portfolio exploration & allocation.

Removes static one-at-a-time trade caps when enabled; ranks markets by regime confidence
and tuner profit factors; enforces rolling 288-bar correlation guard vs open book.
Baseline equity target: £10,000 (hardcoded).
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from system.engine_log import log_engine
from system.market_data_hub import NIGHT_MATRIX_EPICS, get_market_data_hub

ACCOUNT_EQUITY_TARGET_GBP = 10_000.0
HARD_MARGIN_LIMIT_GBP = 10_000.0
MARGIN_FREEZE_THRESHOLD_GBP = 9_500.0
MARGIN_FREEZE_UTIL_PCT = 0.95
EXPECTATION_SCORE_MIN = 0.45
HIGH_CONVICTION_EXPECTATION = 0.60
HIGH_CONVICTION_ML_THRESHOLD = 0.65
ADAPTIVE_SPREAD_MEDIAN_MULT = 2.0
HIGH_CONVICTION_SPREAD_WIDEN = 1.25
FLASH_MARGIN_LIMIT_GBP = 9_800.0
FLASH_KELLY_CAP = 0.22
FLASH_HOLD_MAX_SEC = 10.0
CORRELATION_THRESHOLD = 0.70
CORRELATION_THRESHOLD_HIGH_CONVICTION = 0.82
CORRELATION_BARS = 288
BASE_MARGIN_FRACTION = 0.02  # 2% equity per slot baseline
KELLY_CAP = 0.25
KELLY_CAP_MEAN_REVERSION = 0.15
KELLY_CAP_MOMENTUM = 0.25
SCAN_INTERVAL_SEC = 2.0
MIN_REGIME_CONFIDENCE = 0.38
MIN_LIQUID_TPM = 3.0
_EXPLORE_STATES = frozenset({0, 1})  # mean reversion + HV trend

# --- Adaptive Spread Fuse (60s rolling σ, 2.5σ toxic window freeze) ---
_SPREAD_FUSE_WINDOW_SEC = 60.0
_SPREAD_FUSE_SIGMA_MULT = 2.5
_SPREAD_FUSE_FREEZE_SEC = 120.0
_spread_fuse_lock = threading.RLock()
_spread_fuse_samples: dict[str, deque[tuple[float, float]]] = {}
_spread_fuse_frozen_until: dict[str, float] = {}
_spread_fuse_last: dict[str, dict[str, Any]] = {}

# --- Regime Kalman smoother (suppress Markov flicker) ---
_REGIME_KALMAN_LOCK = threading.Lock()
_REGIME_KALMAN_STATE: dict[str, dict[str, float]] = {}

# --- 10s volume profile / session HVN gate ---
_HVN_WINDOW_SEC = 10.0
_HVN_ALIGN_TOLERANCE = 0.85
_volume_profile_lock = threading.Lock()
_volume_ticks: dict[str, deque[tuple[float, float]]] = {}
_session_hvn: dict[str, dict[int, float]] = {}

# --- Hardware-accelerated rolling covariance matrix (288-bar rings) ---
_COVARIANCE_REFRESH_SEC = 0.5
_COVARIANCE_RISK_PARITY_BOUND = 0.55
_covariance_lock = threading.Lock()
_covariance_last_ts = 0.0
_covariance_compression_factor = 1.0
_covariance_snapshot: dict[str, Any] = {
    "ok": False,
    "epics": [],
    "collective_coefficient": 0.0,
    "compression_factor": 1.0,
    "risk_parity_boundary": _COVARIANCE_RISK_PARITY_BOUND,
}


def record_spread_fuse_sample(epic: str, spread_pts: float) -> None:
    key = str(epic or "").strip()
    if not key or spread_pts <= 0:
        return
    now = time.time()
    with _spread_fuse_lock:
        hist = _spread_fuse_samples.setdefault(key, deque(maxlen=512))
        hist.append((now, float(spread_pts)))
        cutoff = now - _SPREAD_FUSE_WINDOW_SEC
        while hist and hist[0][0] < cutoff:
            hist.popleft()
        evaluate_adaptive_spread_fuse(key, float(spread_pts))


def evaluate_adaptive_spread_fuse(epic: str, spread_pts: float) -> dict[str, Any]:
    key = str(epic or "").strip()
    now = time.time()
    with _spread_fuse_lock:
        until = float(_spread_fuse_frozen_until.get(key) or 0.0)
        if until > now:
            row = {
                "epic": key,
                "frozen": True,
                "frozen_until": until,
                "spread_pts": round(float(spread_pts), 4),
                "reason": "spread_fuse_active",
            }
            _spread_fuse_last[key] = row
            return row
        hist = _spread_fuse_samples.get(key) or deque()
        spreads = [s for _, s in hist if s > 0]
        if len(spreads) < 5:
            row = {
                "epic": key,
                "frozen": False,
                "spread_pts": round(float(spread_pts), 4),
                "samples": len(spreads),
                "reason": "warming",
            }
            _spread_fuse_last[key] = row
            return row
        median = float(np.median(spreads))
        std = float(np.std(spreads))
        threshold = median + _SPREAD_FUSE_SIGMA_MULT * max(std, 1e-9)
        toxic = float(spread_pts) > threshold
        if toxic:
            _spread_fuse_frozen_until[key] = now + _SPREAD_FUSE_FREEZE_SEC
        row = {
            "epic": key,
            "frozen": toxic,
            "spread_pts": round(float(spread_pts), 4),
            "median_pts": round(median, 4),
            "std_pts": round(std, 4),
            "threshold_pts": round(threshold, 4),
            "sigma_mult": _SPREAD_FUSE_SIGMA_MULT,
            "samples": len(spreads),
        }
        _spread_fuse_last[key] = row
    if toxic:
        try:
            from runtime.master_orchestrator import freeze_epic_entries

            freeze_epic_entries(key, reason="adaptive_spread_fuse")
        except Exception:
            pass
        log_engine(
            f"SpreadFuse: FROZEN {key} spread={spread_pts:.3f} "
            f"threshold={threshold:.3f} (median={median:.3f} σ={std:.3f})"
        )
    return row


def is_spread_fuse_frozen(epic: str) -> bool:
    key = str(epic or "").strip()
    now = time.time()
    with _spread_fuse_lock:
        until = float(_spread_fuse_frozen_until.get(key) or 0.0)
        if until <= now:
            _spread_fuse_frozen_until.pop(key, None)
            return False
        return True


def get_spread_fuse_snapshot() -> dict[str, Any]:
    with _spread_fuse_lock:
        rows = [dict(v) for v in _spread_fuse_last.values()]
        frozen = [k for k, ts in _spread_fuse_frozen_until.items() if ts > time.time()]
    return {"ok": True, "frozen_epics": frozen, "assets": rows}


def smooth_regime_with_kalman(epic: str, raw_state: int, confidence: float) -> tuple[int, float]:
    """Secondary Kalman smoother on discrete regime belief — reduces whipsaw toggles."""
    key = str(epic or "").strip()
    measurement = float(raw_state)
    with _REGIME_KALMAN_LOCK:
        st = _REGIME_KALMAN_STATE.setdefault(
            key, {"x": measurement, "p": 1.0, "q": 0.02, "r": 0.35}
        )
        p_pred = st["p"] + st["q"]
        k = p_pred / (p_pred + st["r"])
        st["x"] = st["x"] + k * (measurement - st["x"])
        st["p"] = (1.0 - k) * p_pred
        smoothed = int(round(max(0.0, min(2.0, st["x"]))))
        conf = float(max(0.0, min(1.0, 0.65 * confidence + 0.35 * (1.0 - abs(st["x"] - measurement)))))
        st["last_state"] = float(smoothed)
        st["last_conf"] = conf
        return smoothed, conf


def get_regime_kalman_snapshot() -> dict[str, Any]:
    with _REGIME_KALMAN_LOCK:
        rows = {
            epic: {
                "smoothed_state": int(round(st.get("last_state", 0))),
                "belief": round(float(st.get("x", 0.0)), 3),
                "confidence": round(float(st.get("last_conf", 0.0)), 3),
            }
            for epic, st in _REGIME_KALMAN_STATE.items()
        }
    return {"ok": True, "epics": rows}


def _session_clock_minute() -> int:
    """Minute-of-day bucket for session HVN alignment (UTC)."""
    lt = time.gmtime()
    return int(lt.tm_hour) * 60 + int(lt.tm_min)


def record_volume_tick(epic: str, *, tpm: float = 1.0) -> None:
    """Accumulate 10-second tick volume proxy for HVN profiling."""
    key = str(epic or "").strip()
    if not key:
        return
    now = time.time()
    weight = max(0.1, float(tpm))
    with _volume_profile_lock:
        hist = _volume_ticks.setdefault(key, deque(maxlen=512))
        hist.append((now, weight))
        cutoff = now - _HVN_WINDOW_SEC
        while hist and hist[0][0] < cutoff:
            hist.popleft()
        bucket = _session_clock_minute()
        vol_10s = sum(v for _, v in hist)
        session = _session_hvn.setdefault(key, {})
        prev = float(session.get(bucket) or 0.0)
        session[bucket] = max(prev, vol_10s)


def _current_10s_volume(epic: str) -> float:
    key = str(epic or "").strip()
    now = time.time()
    with _volume_profile_lock:
        hist = _volume_ticks.get(key) or deque()
        cutoff = now - _HVN_WINDOW_SEC
        return float(sum(v for ts, v in hist if ts >= cutoff))


def volume_profile_aligns_with_hvn(epic: str) -> tuple[bool, str]:
    """
    Block entries unless 10s tick volume aligns with session high-volume node (HVN).
    """
    key = str(epic or "").strip()
    if not key:
        return False, "hvn_missing_epic"
    bucket = _session_clock_minute()
    with _volume_profile_lock:
        session = _session_hvn.get(key) or {}
        hvn = float(session.get(bucket) or 0.0)
    current = _current_10s_volume(key)
    if hvn <= 0.0:
        if current > 0.0:
            with _volume_profile_lock:
                _session_hvn.setdefault(key, {})[bucket] = current
        return True, "hvn_warming"
    if current >= hvn * _HVN_ALIGN_TOLERANCE:
        return True, ""
    return False, f"hvn_misaligned current={current:.2f} hvn={hvn:.2f}"


def get_volume_profile_snapshot() -> dict[str, Any]:
    bucket = _session_clock_minute()
    rows: list[dict[str, Any]] = []
    with _volume_profile_lock:
        for epic in sorted(set(_volume_ticks) | set(_session_hvn)):
            current = _current_10s_volume(epic)
            hvn = float((_session_hvn.get(epic) or {}).get(bucket) or 0.0)
            aligned, reason = volume_profile_aligns_with_hvn(epic)
            rows.append(
                {
                    "epic": epic,
                    "volume_10s": round(current, 2),
                    "session_hvn": round(hvn, 2),
                    "bucket_minute": bucket,
                    "aligned": aligned,
                    "reason": reason,
                }
            )
    return {"ok": True, "bucket_minute": bucket, "assets": rows}


def _covariance_universe_epics(epics: list[str] | None = None) -> list[str]:
    if epics:
        return [str(e).strip() for e in epics if str(e).strip()]
    keys: set[str] = set(NIGHT_MATRIX_EPICS)
    with _lock:
        for row in _ranked_universe:
            epic = str(row.get("epic") or "").strip()
            if epic:
                keys.add(epic)
        for row in _open_book:
            epic = str(row.get("epic") or "").strip()
            if epic:
                keys.add(epic)
    return sorted(keys)


def compute_portfolio_covariance_matrix(
    epics: list[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """
    Vectorized joint-volatility covariance from 288-bar NumPy ring log-returns.

    When collective off-diagonal correlation exceeds risk-parity boundary,
  sets a global compression factor for downstream sizing.
    """
    global _covariance_last_ts, _covariance_compression_factor, _covariance_snapshot
    now = time.time()
    if not force and (now - _covariance_last_ts) < _COVARIANCE_REFRESH_SEC:
        with _covariance_lock:
            return dict(_covariance_snapshot)

    universe = _covariance_universe_epics(epics)
    series: list[np.ndarray] = []
    valid: list[str] = []
    min_len = CORRELATION_BARS
    for epic in universe:
        rets = _log_returns(epic, n=CORRELATION_BARS)
        if rets is None or rets.size < 20:
            continue
        min_len = min(min_len, int(rets.size))
        series.append(rets)
        valid.append(epic)

    if len(valid) < 2 or min_len < 20:
        body = {
            "ok": False,
            "epics": valid,
            "bars": min_len,
            "collective_coefficient": 0.0,
            "compression_factor": 1.0,
            "risk_parity_boundary": _COVARIANCE_RISK_PARITY_BOUND,
            "reason": "insufficient_universe",
            "ts": now,
        }
        with _covariance_lock:
            _covariance_last_ts = now
            _covariance_compression_factor = 1.0
            _covariance_snapshot = body
        return dict(body)

    aligned = np.column_stack([s[-min_len:] for s in series]).astype(np.float64)
    cov = np.cov(aligned, rowvar=False)
    std = np.sqrt(np.maximum(np.diag(cov), 1e-12))
    denom = np.outer(std, std)
    corr = np.divide(cov, denom, out=np.zeros_like(cov), where=denom > 1e-12)
    n = corr.shape[0]
    mask = ~np.eye(n, dtype=bool)
    off_diag = corr[mask]
    collective = float(np.mean(np.abs(off_diag))) if off_diag.size else 0.0
    max_eig = float(np.max(np.linalg.eigvalsh(corr))) if n > 1 else 1.0
    joint_coef = max(collective, max_eig / max(n, 1))

    compression = 1.0
    if joint_coef > _COVARIANCE_RISK_PARITY_BOUND:
        compression = float(max(0.25, _COVARIANCE_RISK_PARITY_BOUND / joint_coef))

    cells: list[dict[str, Any]] = []
    for i, a in enumerate(valid):
        for j, b in enumerate(valid):
            if j < i:
                continue
            cells.append(
                {
                    "epic_a": a,
                    "epic_b": b,
                    "correlation": round(float(corr[i, j]), 4),
                    "covariance": round(float(cov[i, j]), 8),
                }
            )

    body = {
        "ok": True,
        "epics": valid,
        "bars": min_len,
        "collective_coefficient": round(joint_coef, 4),
        "mean_abs_correlation": round(collective, 4),
        "max_eigenvalue": round(max_eig, 4),
        "compression_factor": round(compression, 4),
        "risk_parity_boundary": _COVARIANCE_RISK_PARITY_BOUND,
        "matrix_cells": cells[:120],
        "ts": now,
    }
    with _covariance_lock:
        _covariance_last_ts = now
        _covariance_compression_factor = compression
        _covariance_snapshot = body
    try:
        from system.chaos_guardian import sync_portfolio_covariance_compression

        sync_portfolio_covariance_compression(compression)
    except Exception:
        pass
    return dict(body)


def refresh_portfolio_covariance_if_due() -> dict[str, Any]:
    """500ms cadence hook — safe from master ledger publisher."""
    return compute_portfolio_covariance_matrix()


def get_covariance_compression_factor() -> float:
    with _covariance_lock:
        return float(_covariance_compression_factor)


def apply_covariance_compression(size: float) -> float:
    return max(0.01, float(size) * get_covariance_compression_factor())


def get_portfolio_covariance_snapshot() -> dict[str, Any]:
    with _covariance_lock:
        return dict(_covariance_snapshot)


_lock = threading.RLock()
_enabled = True
_snapshot: dict[str, Any] = {
    "ok": True,
    "healthy": False,
    "enabled": True,
    "account_equity_target_gbp": ACCOUNT_EQUITY_TARGET_GBP,
    "universe_size": 0,
    "market_rankings": [],
    "capital_allocation_pct": 0.0,
    "margin_used_gbp": 0.0,
    "margin_available_gbp": ACCOUNT_EQUITY_TARGET_GBP,
    "max_concurrent_trades": 0,
    "open_positions": 0,
    "correlation_exposures": [],
    "position_tree": [],
    "margin_freeze_active": False,
    "entry_frozen": False,
    "ts": 0.0,
}
_ranked_universe: list[dict[str, Any]] = []
_open_book: list[dict[str, Any]] = []
_daemon_thread: threading.Thread | None = None
_daemon_stop = threading.Event()
_async_loop_thread: threading.Thread | None = None
_rotation_events: deque[dict[str, Any]] = deque(maxlen=32)
_rotation_dropped_epics: set[str] = set()
_last_rotation_counsel: str = ""
_rotation_sweep_count: int = 0
ROTATION_TICK_WINDOW_SEC = 10.0
ROTATION_MIN_TICKS = 5
SHADOW_WALK_VETO_STREAK_LIMIT = 2
_shadow_veto_streak: dict[str, int] = {}


@dataclass
class ExplorationAssessment:
    approved: bool
    size: float
    stop_distance: float
    limit_distance: float
    reason: str = ""
    size_factor: float = 1.0
    max_concurrent: int = 0
    allocation_weight: float = 0.0


@dataclass
class _MarketCandidate:
    epic: str
    asset_class: str
    state: int
    confidence: float
    profit_factor: float
    score: float
    size_factor: float = 1.0
    stop_factor: float = 1.0
    tpm: float = 0.0
    spread_pts: float = 0.0
    healthy: bool = True

    def to_ranking(self) -> dict[str, Any]:
        return {
            "epic": self.epic,
            "asset_class": self.asset_class,
            "regime_state": self.state,
            "confidence": round(self.confidence, 4),
            "profit_factor": round(self.profit_factor, 3),
            "score": round(self.score, 4),
            "size_factor": round(self.size_factor, 3),
            "tpm": round(self.tpm, 1),
            "spread_pts": round(self.spread_pts, 2),
        }


def portfolio_exploration_enabled() -> bool:
    with _lock:
        if not _enabled:
            return False
    try:
        from system.config_loader import get_config

        cfg = get_config()
        block = getattr(cfg, "portfolio_exploration", None)
        if isinstance(block, dict):
            return bool(block.get("enabled", True))
        if hasattr(cfg, "get"):
            raw = cfg.get("portfolio_exploration")
            if isinstance(raw, dict) and "enabled" in raw:
                return bool(raw.get("enabled"))
    except Exception:
        pass
    return True


def _asset_class_for_epic(epic: str) -> str:
    key = str(epic or "").upper()
    if key.startswith("CS.D.") and "USD" in key:
        return "fx"
    if key.startswith("CS.D."):
        return "commodities"
    if key.startswith("IX.D."):
        return "indices"
    if "BTC" in key or "ETH" in key or "CRYPTO" in key:
        return "crypto"
    return "other"


def _discover_epic_universe() -> list[str]:
    epics: set[str] = set(NIGHT_MATRIX_EPICS)
    try:
        from system.config_loader import get_config

        cfg = get_config()
        instruments = getattr(cfg, "instruments", None)
        if instruments is None and hasattr(cfg, "get"):
            instruments = cfg.get("instruments")
        if isinstance(instruments, dict):
            for row in instruments.values():
                if isinstance(row, dict) and row.get("enabled", True):
                    epic = str(row.get("epic") or "").strip()
                    if epic:
                        epics.add(epic)
        raw_markets = getattr(cfg, "markets", None)
        if raw_markets is None and hasattr(cfg, "get"):
            raw_markets = cfg.get("markets")
        if isinstance(raw_markets, list):
            for row in raw_markets:
                if isinstance(row, dict):
                    epic = str(row.get("epic") or "").strip()
                    if epic:
                        epics.add(epic)
    except Exception:
        pass
    try:
        from system.v26_config import load_v26_overlay

        overlay = load_v26_overlay()
        for key in ("core_epics", "epics"):
            raw = overlay.get(key) or (overlay.get("regime") or {}).get("index_epics")
            if isinstance(raw, list):
                for epic in raw:
                    e = str(epic or "").strip()
                    if e:
                        epics.add(e)
    except Exception:
        pass
    return sorted(epics)


def _multiplier_overlay(epic: str) -> float:
    """Orchestrator / tuner overlay for expectation score."""
    try:
        from runtime.master_orchestrator import get_strategy_route, get_scoreboard_size_multiplier

        route = get_strategy_route(epic)
        overlay = float((route or {}).get("size_factor_mult") or 1.0)
        return overlay * float(get_scoreboard_size_multiplier())
    except Exception:
        return 1.0


def compute_expectation_score(
    *,
    confidence: float,
    profit_factor: float,
    epic: str | None = None,
    multiplier_overlay: float | None = None,
) -> float:
    """Score = Confidence × Profit Factor × Multiplier Overlay."""
    overlay = (
        float(multiplier_overlay)
        if multiplier_overlay is not None
        else (_multiplier_overlay(epic) if epic else 1.0)
    )
    score = max(0.0, float(confidence) * float(profit_factor) * overlay)
    if epic:
        try:
            from runtime.master_orchestrator import get_lead_lag_score_boost

            boost = get_lead_lag_score_boost(str(epic))
            if boost > score:
                return boost
        except Exception:
            pass
    return score


def is_margin_entry_frozen(margin_used_gbp: float | None = None) -> tuple[bool, str]:
    """Freeze all new entries when utilization exceeds 95% (£9,500) of £10k target."""
    used = float(margin_used_gbp if margin_used_gbp is not None else _estimate_margin_used(_load_open_book()))
    if used >= HARD_MARGIN_LIMIT_GBP:
        return True, f"hard_margin_ceiling_{used:.0f}"
    if used >= MARGIN_FREEZE_THRESHOLD_GBP:
        return True, f"margin_freeze_{used:.0f}_gte_{MARGIN_FREEZE_THRESHOLD_GBP:.0f}"
    util = used / max(HARD_MARGIN_LIMIT_GBP, 1.0)
    if util >= MARGIN_FREEZE_UTIL_PCT:
        return True, f"margin_util_{util:.2%}_freeze"
    return False, ""


def regime_direction_aligned(
    epic: str,
    direction: str,
    *,
    z_score: float | None = None,
    bid: float = 0.0,
    offer: float = 0.0,
) -> tuple[bool, str]:
    """WHEN gate — localized direction must align with Markov regime classification."""
    try:
        from system.demo_execution_plane import execution_guards_relaxed

        if execution_guards_relaxed(epic=str(epic or "").strip()):
            return True, ""
    except Exception:
        pass
    if float(bid) > 0 and float(offer) > float(bid):
        try:
            from execution.pre_entry_regime_veto import sovereign_ml_obi_bypass_qualifies

            bypass_ok, bypass_detail = sovereign_ml_obi_bypass_qualifies(
                str(epic or ""),
                str(direction or "BUY"),
                bid=float(bid),
                offer=float(offer),
            )
            if bypass_ok:
                return True, bypass_detail
        except Exception:
            pass
    try:
        from runtime.regime_switch_engine import evaluate_epic_regime

        snap = evaluate_epic_regime(epic)
    except Exception as exc:
        return False, f"regime_eval_failed:{type(exc).__name__}"

    state = int(snap.state)
    dir_u = str(direction or "BUY").upper()
    z = float(z_score if z_score is not None else 0.0)

    if state == 0:
        if dir_u == "BUY" and (z <= 0.0 or snap.spread_z <= 0.25):
            return True, ""
        if dir_u == "SELL" and (z >= 0.0 or snap.spread_z >= -0.25):
            return True, ""
        return False, "regime0_direction_mismatch"

    if state == 1:
        if snap.adx < 18.0:
            return False, "regime1_weak_adx"
        if dir_u == "BUY" and (z >= 0.5 or snap.atr_ratio >= 1.0):
            return True, ""
        if dir_u == "SELL" and (z <= -0.5 or snap.atr_ratio >= 1.0):
            return True, ""
        return False, "regime1_breakout_direction_mismatch"

    return False, "regime_chop_blocked"


def passes_strategy_entry_gates(
    epic: str,
    direction: str,
    *,
    z_score: float | None = None,
) -> tuple[bool, str]:
    """Unified WHAT/WHEN/HOW-MUCH pre-check for strategy execution matrix.

    Profitability/safety gates (slot, session cap, thin ML) always apply —
    even when demo soak relaxes secondary guards.
    """
    key = str(epic or "").strip()

    # --- Always-on desk profitability gates (DualCore + pierce + micro) ---
    # Fail-CLOSED: any telemetry / gate exception blocks the entry.
    try:
        from system.config_loader import get_config

        cfg = get_config()
    except Exception as exc:
        return False, f"config_fail_closed:{type(exc).__name__}"

    # Instantaneous book for pre-network regime veto (no REST).
    try:
        from execution.pre_entry_regime_veto import evaluate_pre_entry_regime_veto
        from system.market_data_hub import get_market_data_hub

        snap = get_market_data_hub().get_snapshot(key)
        bid = float(getattr(snap, "bid", 0) or 0) if snap else 0.0
        offer = float(getattr(snap, "offer", 0) or 0) if snap else 0.0
        ok_rv, rv_reason = evaluate_pre_entry_regime_veto(
            key, str(direction or "BUY"), bid=bid, offer=offer, cfg=cfg
        )
        if not ok_rv:
            return False, rv_reason or "regime_veto"
    except Exception as exc:
        return False, f"regime_veto_fail_closed:{type(exc).__name__}"

    try:
        from execution.entry_gate_hardening import evaluate_entry_hardening

        hard_ok, hard_reason = evaluate_entry_hardening(
            key, str(direction or "BUY"), cfg=cfg
        )
        if not hard_ok:
            return False, hard_reason or "entry_hardening_blocked"
    except Exception as exc:
        return False, f"entry_hardening_fail_closed:{type(exc).__name__}"

    try:
        from system.strategy_quality_gate import evaluate_entry_slot_gate

        slot_ok, slot_reason = evaluate_entry_slot_gate(
            cfg,
            epic=key,
            direction=str(direction or "BUY"),
            bid=bid,
            offer=offer,
        )
        if not slot_ok:
            return False, slot_reason or "entry_slot_blocked"
    except Exception as exc:
        return False, f"slot_gate_fail_closed:{type(exc).__name__}"

    try:
        from trading.entry_protection import check_session_trade_cap

        blocked, cap_reason = check_session_trade_cap(key, cfg)
        if blocked:
            return False, cap_reason or "session_trade_cap"
    except Exception as exc:
        return False, f"session_cap_fail_closed:{type(exc).__name__}"

    try:
        from ml.core_b_entry_gate import core_b_ml_allows_entry

        ml_ok, ml_reason = core_b_ml_allows_entry(
            key, str(direction or "BUY"), cfg=cfg
        )
        if not ml_ok:
            return False, ml_reason or "core_b_ml_veto"
    except Exception as exc:
        return False, f"core_b_ml_fail_closed:{type(exc).__name__}"

    try:
        from system.demo_execution_plane import execution_guards_relaxed

        if execution_guards_relaxed(epic=key):
            return True, ""
    except Exception as exc:
        return False, f"demo_relax_fail_closed:{type(exc).__name__}"

    try:
        open_book = _load_open_book()
        margin_used = _estimate_margin_used(open_book)
        frozen, freeze_reason = is_margin_entry_frozen(margin_used)
        if frozen:
            return False, freeze_reason

        rank_row: dict[str, Any] = {}
        with _lock:
            for row in _ranked_universe:
                if row.get("epic") == key:
                    rank_row = dict(row)
                    break

        score = float(rank_row.get("score") or 0.0)
        if rank_row and score <= EXPECTATION_SCORE_MIN:
            return False, f"expectation_score_{score:.3f}_lte_{EXPECTATION_SCORE_MIN}"

        hvn_ok, hvn_reason = volume_profile_aligns_with_hvn(key)
        if not hvn_ok:
            return False, hvn_reason or "hvn_volume_misaligned"

        aligned, align_reason = regime_direction_aligned(
            key, direction, z_score=z_score, bid=bid, offer=offer
        )
        if not aligned:
            return False, align_reason

        blocked, corr_reason, _ = correlation_blocks_entry(key, direction, open_book)
        if blocked:
            return False, corr_reason or "correlation_guard"

        proposed = margin_used + regime_adjusted_margin_per_trade()
        if proposed > HARD_MARGIN_LIMIT_GBP:
            return (
                False,
                f"margin_ceiling_{proposed:.0f}_gt_{HARD_MARGIN_LIMIT_GBP:.0f}",
            )
    except Exception as exc:
        return False, f"portfolio_gate_fail_closed:{type(exc).__name__}"

    return True, ""


def _tuner_profit_factors() -> dict[int, float]:
    out: dict[int, float] = {0: 1.0, 1: 1.0, 2: 0.5}
    try:
        from runtime.parameter_tuner import get_tuner_state_snapshot

        snap = get_tuner_state_snapshot()
        for row in (snap.get("regime_metrics") or {}).values():
            if not isinstance(row, dict):
                continue
            st = int(row.get("regime_state", -1))
            pf = float(row.get("profit_factor") or 1.0)
            if st >= 0:
                out[st] = max(0.1, pf)
    except Exception:
        pass
    return out


def _quote_liquidity(epic: str) -> tuple[float, float]:
    """Return (tpm, spread_pts)."""
    tpm = 0.0
    spread = 99.0
    try:
        from runtime.dual_core_execution import _ticks_per_minute

        tpm = float(_ticks_per_minute(epic) or 0.0)
    except Exception:
        pass
    try:
        record_volume_tick(epic, tpm=max(0.5, tpm))
    except Exception:
        pass
    try:
        hub = get_market_data_hub()
        q = hub.get_quote(epic)
        if q is not None:
            bid = float(getattr(q, "bid", 0) or 0)
            offer = float(getattr(q, "offer", 0) or 0)
            if bid > 0 and offer > bid:
                spread = offer - bid
    except Exception:
        pass
    return tpm, spread


def historical_median_spread_pts(epic: str) -> float:
    """Rolling median spread — hub history with 288-bar regime ring fallback."""
    key = str(epic or "").strip()
    hub = get_market_data_hub()
    normal = float(hub.normal_spread(key, fallback=0.0) or 0.0)
    if normal > 0:
        return normal
    try:
        from runtime.regime_switch_engine import _engine

        eng = _engine(key)
        n = min(int(eng._count), CORRELATION_BARS)
        if n >= 5:
            spreads = np.asarray(eng._spread[:n], dtype=np.float64)
            spreads = spreads[spreads > 0]
            if spreads.size >= 5:
                return float(np.median(spreads))
    except Exception:
        pass
    _, live = _quote_liquidity(key)
    return max(float(live), 1.0)


def resolve_adaptive_spread_ceiling(
    epic: str,
    *,
    expectation_score: float = 0.0,
) -> float:
    """288-bar median × multiplier; 1.25× widen when ML expectation exceeds 0.65."""
    median = historical_median_spread_pts(epic)
    ceiling = median * ADAPTIVE_SPREAD_MEDIAN_MULT
    score = float(expectation_score or 0.0)
    if score > HIGH_CONVICTION_ML_THRESHOLD:
        ceiling *= HIGH_CONVICTION_SPREAD_WIDEN
    return max(ceiling, median * 1.05)


def vet_order_spread(
    epic: str,
    spread_pts: float,
    *,
    expectation_score: float = 0.0,
) -> tuple[bool, str, float]:
    """Adaptive spread ceiling vet — returns (approved, reason, ceiling_pts)."""
    ceiling = resolve_adaptive_spread_ceiling(epic, expectation_score=expectation_score)
    spread = float(spread_pts or 0.0)
    if spread <= ceiling:
        return True, "", ceiling
    return (
        False,
        f"spread_{spread:.2f}_gt_adaptive_ceiling_{ceiling:.2f}",
        ceiling,
    )


def evaluate_flash_allocation(
    *,
    execution_path: str = "",
    regime_state: int | None = None,
    target_hold_sec: float | None = None,
) -> bool:
    """Flash capital path — limit_chase_hf / regime 0 / emerald PP tier / sub-10s hold."""
    if str(execution_path or "").strip() != "limit_chase_hf":
        return False
    if int(regime_state if regime_state is not None else -1) != 0:
        return False
    hold = float(target_hold_sec if target_hold_sec is not None else 8.0)
    if hold >= FLASH_HOLD_MAX_SEC:
        return False
    try:
        from runtime.master_orchestrator import (
            PP_EXPANSION_THRESHOLD,
            TELEMETRY_TIER_EMERALD,
            get_platform_scoreboard,
        )

        sb = get_platform_scoreboard()
        tier = sb.telemetry_tier_unlocked()
        return tier == TELEMETRY_TIER_EMERALD or sb.total_pp >= PP_EXPANSION_THRESHOLD
    except Exception:
        return False


def adaptive_spread_telemetry(epic: str, *, expectation_score: float = 0.0) -> dict[str, Any]:
    median = historical_median_spread_pts(epic)
    ceiling = resolve_adaptive_spread_ceiling(epic, expectation_score=expectation_score)
    _, live = _quote_liquidity(epic)
    return {
        "epic": epic,
        "spread_pts": round(float(live), 4),
        "median_spread_pts": round(median, 4),
        "adaptive_ceiling_pts": round(ceiling, 4),
        "expectation_score": round(float(expectation_score), 4),
        "high_conviction_widen": float(expectation_score) > HIGH_CONVICTION_ML_THRESHOLD,
    }


async def _evaluate_candidate(epic: str, pf_map: dict[int, float]) -> _MarketCandidate | None:
    try:
        from runtime.regime_switch_engine import evaluate_epic_regime

        snap = await asyncio.to_thread(evaluate_epic_regime, epic)
    except Exception:
        return None
    if snap.state not in _EXPLORE_STATES:
        return None
    if float(snap.confidence) < MIN_REGIME_CONFIDENCE:
        return None
    gate = snap.strategy_gate or {}
    if not gate.get("allow_entries", True):
        return None
    tpm, spread = await asyncio.to_thread(_quote_liquidity, epic)
    hvn_ok, hvn_reason = await asyncio.to_thread(volume_profile_aligns_with_hvn, epic)
    if not hvn_ok:
        return None
    pf = pf_map.get(int(snap.state), 1.0)
    size_f = float(gate.get("size_factor") or 1.0)
    stop_f = float(gate.get("stop_factor") or 1.0)
    overlay = _multiplier_overlay(epic)
    score = compute_expectation_score(
        confidence=float(snap.confidence),
        profit_factor=pf,
        multiplier_overlay=overlay,
    )
    ceiling = resolve_adaptive_spread_ceiling(epic, expectation_score=score)
    if tpm < MIN_LIQUID_TPM and spread > ceiling:
        return None
    if score <= EXPECTATION_SCORE_MIN:
        return None
    return _MarketCandidate(
        epic=epic,
        asset_class=_asset_class_for_epic(epic),
        state=int(snap.state),
        confidence=float(snap.confidence),
        profit_factor=pf,
        score=score,
        size_factor=size_f,
        stop_factor=stop_f,
        tpm=tpm,
        spread_pts=spread,
        healthy=bool(snap.healthy),
    )


async def scan_universe_async(epics: list[str] | None = None) -> list[_MarketCandidate]:
    universe = epics if epics is not None else _discover_epic_universe()
    pf_map = _tuner_profit_factors()
    tasks = [_evaluate_candidate(epic, pf_map) for epic in universe]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    candidates: list[_MarketCandidate] = []
    for item in results:
        if isinstance(item, _MarketCandidate):
            candidates.append(item)
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def scan_universe(epics: list[str] | None = None) -> list[dict[str, Any]]:
    """Sync wrapper — runs async scanner on dedicated loop thread when needed."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(asyncio.run, scan_universe_async(epics))
                ranked = fut.result(timeout=30.0)
        else:
            ranked = loop.run_until_complete(scan_universe_async(epics))
    except RuntimeError:
        ranked = asyncio.run(scan_universe_async(epics))
    return [c.to_ranking() for c in ranked]


def regime_adjusted_margin_per_trade(
    *,
    size_factor: float = 1.0,
    stop_factor: float = 1.0,
    equity: float = ACCOUNT_EQUITY_TARGET_GBP,
) -> float:
    """Initial margin budget per concurrent slot (GBP)."""
    base = equity * BASE_MARGIN_FRACTION
    adj = base * float(stop_factor) / max(float(size_factor), 0.25)
    return max(50.0, adj)


def compute_max_concurrent_trades(
    *,
    available_margin_gbp: float,
    size_factor: float = 1.0,
    stop_factor: float = 1.0,
) -> int:
    per_trade = regime_adjusted_margin_per_trade(
        size_factor=size_factor, stop_factor=stop_factor
    )
    if available_margin_gbp <= 0 or per_trade <= 0:
        return 0
    return max(0, int(math.floor(available_margin_gbp / per_trade)))


def _risk_parity_weights(candidates: list[_MarketCandidate]) -> dict[str, float]:
    if not candidates:
        return {}
    inv_vols: list[float] = []
    for c in candidates:
        vol = max(0.05, 1.0 / max(c.stop_factor, 0.5))
        inv_vols.append(c.score / vol)
    total = sum(inv_vols) or 1.0
    return {c.epic: w / total for c, w in zip(candidates, inv_vols)}


def _kelly_fraction(confidence: float, profit_factor: float) -> float:
    """Fractional Kelly from win proxy and payoff ratio."""
    p = min(0.95, max(0.05, confidence))
    b = max(0.1, profit_factor - 1.0)
    if b <= 0:
        return 0.05
    q = 1.0 - p
    kelly = (p * b - q) / b
    return max(0.02, min(KELLY_CAP, kelly))


def _load_open_book() -> list[dict[str, Any]]:
    book: list[dict[str, Any]] = []
    try:
        from runtime.trade_lifecycle import snapshot as lifecycle_snapshot

        for deal_id, trade in (lifecycle_snapshot().get("active") or {}).items():
            if not isinstance(trade, dict):
                continue
            epic = str(trade.get("epic") or "").strip()
            if not epic:
                continue
            book.append(
                {
                    "deal_id": str(deal_id),
                    "epic": epic,
                    "direction": str(trade.get("direction") or "BUY").upper(),
                    "size": float(trade.get("size") or 0),
                }
            )
    except Exception:
        pass
    try:
        from system.config_loader import get_config
        from data.learning_store import LearningStore
        from runtime.active_lifecycle_trades import list_active_lifecycle_trades

        store = LearningStore(str(get_config().learning_db))
        seen = {r["epic"] for r in book}
        for row in list_active_lifecycle_trades(store):
            epic = str(row.get("epic") or "").strip()
            if not epic or epic in seen:
                continue
            book.append(
                {
                    "deal_id": str(row.get("deal_id") or row.get("ticket") or epic),
                    "epic": epic,
                    "direction": str(row.get("direction") or "BUY").upper(),
                    "size": float(row.get("size") or 0),
                }
            )
    except Exception:
        pass
    return book


def _log_returns(epic: str, n: int = CORRELATION_BARS) -> np.ndarray | None:
    try:
        from runtime.regime_switch_engine import get_epic_close_returns

        rets = get_epic_close_returns(epic, min_bars=20, max_bars=n)
        if rets is None or rets.size < 20:
            return None
        return rets
    except Exception:
        return None


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    n = min(a.size, b.size)
    if n < 10:
        return 0.0
    x = a[-n:]
    y = b[-n:]
    if float(np.std(x)) < 1e-12 or float(np.std(y)) < 1e-12:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def dynamic_correlation_threshold(expectation_score: float) -> float:
    """High-conviction entries (>0.60) widen correlation guard to 0.82."""
    if float(expectation_score) > HIGH_CONVICTION_EXPECTATION:
        return CORRELATION_THRESHOLD_HIGH_CONVICTION
    return CORRELATION_THRESHOLD


def _expectation_score_for_epic(epic: str) -> float:
    key = str(epic or "").strip()
    with _lock:
        for row in _ranked_universe:
            if row.get("epic") == key:
                return float(row.get("score") or 0.0)
    return 0.0


def correlation_blocks_entry(
    epic: str,
    direction: str,
    open_book: list[dict[str, Any]] | None = None,
) -> tuple[bool, str, list[dict[str, Any]]]:
    """
    Block when localized return direction correlates > threshold with an open position.
    Returns (blocked, reason, exposure_rows).
    """
    book = open_book if open_book is not None else _load_open_book()
    exposures: list[dict[str, Any]] = []
    if not book:
        return False, "", exposures
    cand_rets = _log_returns(epic)
    if cand_rets is None:
        return False, "", exposures
    sign = 1.0 if str(direction).upper() == "BUY" else -1.0
    aligned_c = cand_rets * sign
    corr_threshold = dynamic_correlation_threshold(_expectation_score_for_epic(epic))
    for row in book:
        open_epic = str(row.get("epic") or "")
        if not open_epic or open_epic == epic:
            continue
        open_rets = _log_returns(open_epic)
        if open_rets is None:
            continue
        open_sign = 1.0 if str(row.get("direction") or "BUY").upper() == "BUY" else -1.0
        aligned_o = open_rets * open_sign
        corr = _pearson(aligned_c, aligned_o)
        exposures.append(
            {
                "candidate_epic": epic,
                "open_epic": open_epic,
                "correlation": round(corr, 4),
                "threshold": corr_threshold,
            }
        )
        if corr > corr_threshold:
            return (
                True,
                f"correlation_{corr:.2f}_with_{open_epic}",
                exposures,
            )
    return False, "", exposures


def _estimate_margin_used(open_book: list[dict[str, Any]]) -> float:
    used = 0.0
    for row in open_book:
        size = float(row.get("size") or 0)
        used += max(50.0, size * regime_adjusted_margin_per_trade())
    return used


def _build_position_tree(
    open_book: list[dict[str, Any]],
    rankings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rank_map = {r["epic"]: r for r in rankings}
    tree: list[dict[str, Any]] = []
    for row in open_book:
        epic = str(row.get("epic") or "")
        meta = rank_map.get(epic, {})
        tree.append(
            {
                "epic": epic,
                "direction": row.get("direction"),
                "size": row.get("size"),
                "regime_state": meta.get("regime_state"),
                "score": meta.get("score"),
                "children": [],
            }
        )
    return tree


def refresh_exploration_snapshot(
    *,
    account_available: float | None = None,
    account_balance: float | None = None,
) -> dict[str, Any]:
    global _ranked_universe, _open_book
    equity = float(account_balance or ACCOUNT_EQUITY_TARGET_GBP)
    available = float(
        account_available if account_available is not None else equity
    )
    try:
        candidates = asyncio.run(scan_universe_async())
    except Exception as exc:
        with _lock:
            _snapshot["healthy"] = False
            _snapshot["last_error"] = f"{type(exc).__name__}: {exc}"
        return get_exploration_state_snapshot()

    rankings = [c.to_ranking() for c in candidates]
    open_book = _load_open_book()
    margin_used = _estimate_margin_used(open_book)
    margin_avail = max(0.0, available - margin_used)
    avg_sf = (
        float(np.mean([c.size_factor for c in candidates[:5]]))
        if candidates
        else 1.0
    )
    avg_stop = (
        float(np.mean([c.stop_factor for c in candidates[:5]]))
        if candidates
        else 1.0
    )
    max_conc = compute_max_concurrent_trades(
        available_margin_gbp=margin_avail,
        size_factor=avg_sf,
        stop_factor=avg_stop,
    )
    alloc_pct = (margin_used / equity * 100.0) if equity > 0 else 0.0
    corr_exposures: list[dict[str, Any]] = []
    for a in open_book:
        for b in open_book:
            if a["epic"] >= b["epic"]:
                continue
            ra = _log_returns(a["epic"])
            rb = _log_returns(b["epic"])
            if ra is None or rb is None:
                continue
            corr = _pearson(ra, rb)
            if abs(corr) >= 0.5:
                corr_exposures.append(
                    {
                        "epic_a": a["epic"],
                        "epic_b": b["epic"],
                        "correlation": round(corr, 4),
                    }
                )

    frozen, _freeze_reason = is_margin_entry_frozen(margin_used)
    body: dict[str, Any] = {
        "ok": True,
        "healthy": len(candidates) > 0,
        "enabled": portfolio_exploration_enabled(),
        "account_equity_target_gbp": ACCOUNT_EQUITY_TARGET_GBP,
        "hard_margin_limit_gbp": HARD_MARGIN_LIMIT_GBP,
        "expectation_score_min": EXPECTATION_SCORE_MIN,
        "universe_size": len(candidates),
        "market_rankings": rankings[:50],
        "capital_allocation_pct": round(alloc_pct, 2),
        "margin_used_gbp": round(margin_used, 2),
        "margin_available_gbp": round(margin_avail, 2),
        "margin_freeze_active": frozen,
        "entry_frozen": frozen,
        "max_concurrent_trades": max_conc,
        "open_positions": len(open_book),
        "correlation_exposures": corr_exposures[:20],
        "position_tree": _build_position_tree(open_book, rankings),
        "adaptive_spread_telemetry": [
            adaptive_spread_telemetry(
                str(r.get("epic") or ""),
                expectation_score=float(r.get("score") or 0.0),
            )
            for r in rankings[:12]
            if r.get("epic")
        ],
        "rotation_matrix": get_rotation_matrix_snapshot(),
        "ts": time.time(),
    }
    try:
        from system.market_data_hub import get_external_api_health_matrix

        body["api_ingest_health"] = get_external_api_health_matrix()
    except Exception:
        body["api_ingest_health"] = {"ok": False, "feeds": []}
    with _lock:
        _ranked_universe = rankings
        _open_book = open_book
        _snapshot.clear()
        _snapshot.update(body)
    return dict(body)


def get_exploration_state_snapshot() -> dict[str, Any]:
    with _lock:
        return dict(_snapshot)


def get_expectancy_metrics_snapshot() -> list[dict[str, Any]]:
    """Real-time expectancy optimizer telemetry for orchestrator / Flight Deck."""
    from execution.risk_manager import compute_continuous_kelly_fraction
    from runtime.parameter_tuner import get_slippage_adaptive_offsets
    from trading.probability_engine import resolve_dynamic_veto_floor

    veto_floor = float(resolve_dynamic_veto_floor())
    slip_offsets = get_slippage_adaptive_offsets()
    with _lock:
        rankings = list(_snapshot.get("market_rankings") or _ranked_universe or [])[:12]

    rows: list[dict[str, Any]] = []
    for rank in rankings:
        epic = str(rank.get("epic") or "").strip()
        if not epic:
            continue
        ml = float(rank.get("score") or rank.get("expectation_score") or 0.0)
        base_kelly = 0.10
        route_path = ""
        try:
            from runtime.master_orchestrator import get_strategy_route

            route = get_strategy_route(epic)
            if route:
                base_kelly = float(route.get("kelly_fraction") or 0.0)
                route_path = str(route.get("execution_path") or "")
        except Exception:
            pass
        if base_kelly <= 0:
            if route_path == "momentum_breakout":
                base_kelly = 0.25
            elif route_path == "limit_chase_hf":
                base_kelly = 0.15
            else:
                base_kelly = 0.10

        eff_kelly = (
            compute_continuous_kelly_fraction(
                base_kelly_cap=base_kelly,
                ml_expectation_score=ml,
                veto_floor=veto_floor,
            )
            if ml > veto_floor
            else 0.0
        )
        slip = slip_offsets.get(epic, {})
        rows.append(
            {
                "epic": epic,
                "ml_expectation_score": round(ml, 4),
                "veto_floor": round(veto_floor, 4),
                "base_kelly_cap": round(base_kelly, 4),
                "continuous_kelly_fraction": round(eff_kelly, 4),
                "slippage_avg_pips": round(float(slip.get("avg_slippage_pips") or 0.0), 4),
                "slippage_limit_factor_mult": round(
                    float(slip.get("limit_factor_mult") or 1.0), 4
                ),
                "profit_target_multiplier": round(
                    float(slip.get("profit_target_multiplier") or 1.0), 4
                ),
                "slippage_adaptive_active": bool(slip.get("active")),
            }
        )
    return rows


def get_dynamic_max_concurrent_trades(
    *,
    account_available: float | None = None,
    size_factor: float = 1.0,
    stop_factor: float = 1.0,
) -> int:
    if not portfolio_exploration_enabled():
        try:
            from execution.correlation_guard import _max_open_positions_global

            return _max_open_positions_global()
        except Exception:
            return 2
    with _lock:
        cached = int(_snapshot.get("max_concurrent_trades") or 0)
        if cached > 0:
            try:
                from runtime.master_orchestrator import get_scoreboard_capacity_multiplier

                return max(1, int(cached * get_scoreboard_capacity_multiplier()))
            except Exception:
                return cached
    avail = float(account_available or ACCOUNT_EQUITY_TARGET_GBP)
    return compute_max_concurrent_trades(
        available_margin_gbp=avail,
        size_factor=size_factor,
        stop_factor=stop_factor,
    )


def exploration_allows_hot_path(epic: str, cfg: Any | None = None) -> bool:
    """True when exploration ranks epic and capacity remains — bypasses static stack cap."""
    if not portfolio_exploration_enabled():
        return False
    key = str(epic or "").strip()
    if not key:
        return False
    try:
        from runtime.master_orchestrator import route_allows_entry

        allowed, _ = route_allows_entry(key)
        if not allowed:
            return False
    except Exception:
        pass
    with _lock:
        ranked = {r["epic"] for r in _ranked_universe}
        max_conc = int(_snapshot.get("max_concurrent_trades") or 0)
        open_n = int(_snapshot.get("open_positions") or 0)
    if key not in ranked:
        return False
    if max_conc > 0 and open_n >= max_conc:
        return False
    return True


def assess_portfolio_exploration(
    *,
    epic: str,
    direction: str,
    size: float,
    stop_distance: float,
    limit_distance: float,
    account_available: float | None = None,
    account_balance: float | None = None,
    execution_path: str = "",
    regime_state: int | None = None,
    target_hold_sec: float | None = None,
    win_probability: float = 0.0,
    flash_allocation: bool = False,
) -> ExplorationAssessment:
    """Final portfolio gate — correlation, margin capacity, fractional Kelly sizing."""
    try:
        from system.demo_execution_plane import execution_guards_relaxed

        if execution_guards_relaxed(epic=str(epic or "").strip()):
            return ExplorationAssessment(
                approved=True,
                size=size,
                stop_distance=stop_distance,
                limit_distance=limit_distance,
            )
    except Exception:
        pass
    if not portfolio_exploration_enabled():
        return ExplorationAssessment(
            approved=True,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
        )

    key = str(epic or "").strip()
    open_book = _load_open_book()
    blocked, corr_reason, _ = correlation_blocks_entry(key, direction, open_book)
    if blocked:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason=corr_reason or "correlation_guard",
        )

    equity = float(account_balance or ACCOUNT_EQUITY_TARGET_GBP)
    available = float(
        account_available if account_available is not None else equity
    )
    margin_used = _estimate_margin_used(open_book)
    margin_avail = max(0.0, min(HARD_MARGIN_LIMIT_GBP, available) - margin_used)
    flash = bool(flash_allocation) or evaluate_flash_allocation(
        execution_path=execution_path,
        regime_state=regime_state,
        target_hold_sec=target_hold_sec,
    )
    frozen, freeze_reason = is_margin_entry_frozen(margin_used)
    if frozen and not flash:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason=freeze_reason,
        )
    if flash and margin_used >= FLASH_MARGIN_LIMIT_GBP:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason=f"flash_margin_ceiling_{margin_used:.0f}",
        )

    aligned, align_reason = regime_direction_aligned(key, direction)
    if not aligned:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason=align_reason,
        )

    hvn_ok, hvn_reason = volume_profile_aligns_with_hvn(key)
    if not hvn_ok:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason=hvn_reason or "hvn_volume_misaligned",
        )

    rank_row: dict[str, Any] = {}
    ranked_epics: set[str] = set()
    with _lock:
        ranked_epics = {str(r.get("epic") or "") for r in _ranked_universe}
        for row in _ranked_universe:
            if row.get("epic") == key:
                rank_row = dict(row)
                break

    if ranked_epics and key not in ranked_epics:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason="not_in_exploration_universe",
        )

    size_factor = float(rank_row.get("size_factor") or 1.0)
    stop_factor = 1.0
    confidence = float(rank_row.get("confidence") or 0.5)
    pf = float(rank_row.get("profit_factor") or 1.0)

    max_conc = compute_max_concurrent_trades(
        available_margin_gbp=margin_avail,
        size_factor=size_factor,
        stop_factor=stop_factor,
    )
    if len(open_book) >= max_conc and max_conc > 0:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason=f"max_concurrent_{len(open_book)}/{max_conc}",
            max_concurrent=max_conc,
        )

    score = float(rank_row.get("score") or compute_expectation_score(confidence=confidence, profit_factor=pf, epic=key))
    if score <= EXPECTATION_SCORE_MIN:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason=f"expectation_score_{score:.3f}_lte_{EXPECTATION_SCORE_MIN}",
        )

    _, spread_live = _quote_liquidity(key)
    ml_score = float(win_probability or score)
    spread_ok, spread_reason, _ = vet_order_spread(key, spread_live, expectation_score=ml_score)
    if not spread_ok:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason=spread_reason,
        )

    margin_limit = FLASH_MARGIN_LIMIT_GBP if flash else HARD_MARGIN_LIMIT_GBP
    proposed_margin = margin_used + regime_adjusted_margin_per_trade(size_factor=size_factor, stop_factor=stop_factor)
    if proposed_margin > margin_limit:
        return ExplorationAssessment(
            approved=False,
            size=size,
            stop_distance=stop_distance,
            limit_distance=limit_distance,
            reason=f"margin_ceiling_{proposed_margin:.0f}",
        )

    kelly = _kelly_fraction(confidence, pf)
    alloc_weight = kelly * size_factor
    try:
        from runtime.master_orchestrator import get_scoreboard_size_multiplier, get_strategy_route

        route = get_strategy_route(key)
        if route:
            path = str(route.get("execution_path") or "")
            if path == "limit_chase_hf":
                alloc_weight *= float(route.get("size_factor_mult") or 1.0)
                stop_distance = stop_distance * float(route.get("stop_factor_mult") or 1.0)
            elif path == "momentum_breakout":
                alloc_weight *= float(route.get("kelly_fraction") or kelly)
                alloc_weight *= float(route.get("size_factor_mult") or 1.0)
        alloc_weight *= get_scoreboard_size_multiplier()
    except Exception:
        pass
    adjusted_size = max(size * alloc_weight, size * 0.25)
    adjusted_size = min(adjusted_size, size * 1.25)

    return ExplorationAssessment(
        approved=True,
        size=adjusted_size,
        stop_distance=stop_distance,
        limit_distance=limit_distance,
        size_factor=size_factor,
        max_concurrent=max_conc,
        allocation_weight=round(alloc_weight, 4),
    )


def get_last_rotation_counsel() -> str:
    with _lock:
        return str(_last_rotation_counsel or "")


def get_rotation_matrix_snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "sweep_count": int(_rotation_sweep_count),
            "dropped_epics": sorted(_rotation_dropped_epics),
            "recent_events": list(_rotation_events)[-8:],
            "last_counsel": str(_last_rotation_counsel or ""),
        }


def _epic_display_name(epic: str) -> str:
    try:
        from runtime.dual_core_execution import epic_display_name

        return epic_display_name(epic)
    except Exception:
        return str(epic or "?")


def _rotation_drop_reason(epic: str) -> str:
    key = str(epic or "").strip()
    if not key:
        return ""
    hub = get_market_data_hub()
    if hub.is_in_maintenance(key):
        return "stream_maintenance"
    snap = hub.get_snapshot(key)
    if snap is None:
        return "stream_missing"
    if snap.age_seconds() > 45.0:
        return "stream_stale"
    try:
        from runtime.dual_core_execution import ticks_per_window

        ticks = int(ticks_per_window(key, ROTATION_TICK_WINDOW_SEC))
        if ticks < ROTATION_MIN_TICKS:
            return f"tick_velocity_low_{ticks}_lt_{ROTATION_MIN_TICKS}_per_{int(ROTATION_TICK_WINDOW_SEC)}s"
    except Exception:
        pass
    try:
        from system.calendar_gate import news_proximity_features

        feats = news_proximity_features(key)
        if bool(feats.get("in_block_window")):
            return "session_close_block"
    except Exception:
        pass
    if _persistent_shadow_walk_veto(key):
        return "shadow_walk_veto"
    return ""


def _persistent_shadow_walk_veto(epic: str) -> bool:
    key = str(epic or "").strip()
    try:
        from trading.probability_engine import run_48bar_shadow_walk_expectation

        walk = run_48bar_shadow_walk_expectation(
            epic=key,
            direction="BUY",
            feature_payload={"vector": [0.0] * 128},
        )
        veto = bool(walk.get("veto"))
        streak = _shadow_veto_streak.get(key, 0)
        if veto:
            _shadow_veto_streak[key] = streak + 1
        else:
            _shadow_veto_streak[key] = 0
        return _shadow_veto_streak.get(key, 0) >= SHADOW_WALK_VETO_STREAK_LIMIT
    except Exception:
        return False


def _five_gate_preflight(epic: str) -> tuple[bool, str]:
    key = str(epic or "").strip()
    hub = get_market_data_hub()
    if hub.is_in_maintenance(key):
        return False, "G1_maintenance"
    snap = hub.get_snapshot(key)
    if snap is None or snap.age_seconds() > 45.0:
        return False, "G3_stale_quote"
    try:
        from system.calendar_gate import news_proximity_features

        if bool(news_proximity_features(key).get("in_block_window")):
            return False, "G2_session_block"
    except Exception:
        pass
    try:
        from runtime.regime_switch_engine import evaluate_epic_regime

        rs = evaluate_epic_regime(key)
        if not rs.healthy:
            return False, "G4_regime_unhealthy"
        gate = rs.strategy_gate or {}
        if not gate.get("allow_entries", True):
            return False, "G5_strategy_gate"
    except Exception as exc:
        return False, f"G4_{type(exc).__name__}"
    return True, "gates_ok"


def _drop_epic_from_evaluation_stack(epic: str, reason: str) -> None:
    key = str(epic or "").strip()
    if not key:
        return
    with _lock:
        _rotation_dropped_epics.add(key)
        _ranked_universe[:] = [r for r in _ranked_universe if r.get("epic") != key]
    try:
        from runtime.dual_core_execution import _evict_epic_from_active_memory

        _evict_epic_from_active_memory(key, reason)
    except Exception:
        pass
    log_engine(f"portfolio_rotation: dropped {key} ({reason})")


def _promote_rotated_epic(epic: str, ranking: dict[str, Any], *, reason: str) -> None:
    key = str(epic or "").strip()
    if not key:
        return
    with _lock:
        _rotation_dropped_epics.discard(key)
        merged = dict(ranking)
        merged["epic"] = key
        merged["rotation_promoted"] = True
        _ranked_universe[:] = [merged] + [r for r in _ranked_universe if r.get("epic") != key]
    log_engine(
        f"portfolio_rotation: promoted {key} score={ranking.get('score')} reason={reason}"
    )


def _record_rotation_counsel(
    *,
    from_epic: str,
    to_epic: str,
    drop_reason: str,
    ml_score: float,
    trade_ready: bool,
) -> None:
    global _last_rotation_counsel
    from_name = _epic_display_name(from_epic)
    to_name = _epic_display_name(to_epic)
    reason_human = drop_reason.replace("_", " ")
    ready = "TRADE READY" if trade_ready else "WARMING"
    counsel = (
        f"🔄 ROTATION: {from_name} entry blocked due to {reason_human}. "
        f"Rotating capital to {to_name} — ML Expectation: {ml_score:.2f} [{ready}]."
    )
    with _lock:
        _last_rotation_counsel = counsel
        _rotation_events.append(
            {
                "ts": time.time(),
                "from_epic": from_epic,
                "to_epic": to_epic,
                "drop_reason": drop_reason,
                "ml_expectation": round(float(ml_score), 4),
                "trade_ready": bool(trade_ready),
                "counsel": counsel,
            }
        )


def execute_market_rotation_sweep() -> dict[str, Any]:
    """
    Continuous flow core — drop stale assets and promote liquid replacements instantly.
    """
    global _rotation_sweep_count
    dropped: list[dict[str, Any]] = []
    promoted: list[dict[str, Any]] = []

    try:
        from runtime.dual_core_execution import evaluate_multi_source_rotation_sweep

        evaluate_multi_source_rotation_sweep()
    except Exception as exc:
        log_engine(f"portfolio_rotation: dual_core sweep {type(exc).__name__}: {exc}")

    with _lock:
        ranked = list(_ranked_universe)

    active: list[str] = []
    try:
        from runtime.dual_core_execution import get_active_stack_epics

        active = list(get_active_stack_epics())
    except Exception:
        pass
    if not active:
        active = [str(r.get("epic") or "") for r in ranked[:3] if r.get("epic")]

    for epic in active:
        reason = _rotation_drop_reason(epic)
        if not reason:
            continue
        _drop_epic_from_evaluation_stack(epic, reason)
        dropped.append({"epic": epic, "reason": reason})

    universe = _discover_epic_universe()
    eligible = [e for e in universe if e not in _rotation_dropped_epics]
    replacement_rows: list[dict[str, Any]] = []
    try:
        replacement_rows = scan_universe(eligible)
    except Exception as exc:
        log_engine(f"portfolio_rotation: scan failed {type(exc).__name__}: {exc}")

    for drop in dropped:
        if not replacement_rows:
            break
        for row in replacement_rows:
            epic = str(row.get("epic") or "")
            if not epic or epic == drop.get("epic"):
                continue
            ok, gate_reason = _five_gate_preflight(epic)
            score = float(row.get("score") or 0.0)
            if not ok or score <= EXPECTATION_SCORE_MIN:
                continue
            _promote_rotated_epic(epic, row, reason=f"rotation_from_{drop.get('epic')}")
            trade_ready = score >= HIGH_CONVICTION_ML_THRESHOLD
            _record_rotation_counsel(
                from_epic=str(drop.get("epic") or ""),
                to_epic=epic,
                drop_reason=str(drop.get("reason") or "stale"),
                ml_score=score,
                trade_ready=trade_ready,
            )
            promoted.append(
                {
                    "epic": epic,
                    "score": score,
                    "gate": gate_reason,
                    "from_epic": drop.get("epic"),
                }
            )
            break

    _rotation_sweep_count += 1
    body = {
        "ok": True,
        "sweep_count": _rotation_sweep_count,
        "dropped": dropped,
        "promoted": promoted,
        "last_counsel": get_last_rotation_counsel(),
        "ts": time.time(),
    }
    with _lock:
        _snapshot["rotation_matrix"] = get_rotation_matrix_snapshot()
    return body


def _daemon_loop() -> None:
    while not _daemon_stop.wait(SCAN_INTERVAL_SEC):
        try:
            execute_market_rotation_sweep()
            refresh_exploration_snapshot()
        except Exception as exc:
            log_engine(
                f"portfolio_exploration: refresh failed {type(exc).__name__}: {exc}"
            )
            with _lock:
                _snapshot["healthy"] = False


def start_portfolio_exploration_daemon() -> None:
    global _daemon_thread
    if _daemon_thread is not None and _daemon_thread.is_alive():
        return
    _daemon_stop.clear()
    try:
        refresh_exploration_snapshot()
    except Exception:
        pass
    _daemon_thread = threading.Thread(
        target=_daemon_loop, name="portfolio-exploration", daemon=True
    )
    _daemon_thread.start()
    log_engine("portfolio_exploration: daemon started")


def stop_portfolio_exploration_daemon() -> None:
    _daemon_stop.set()


def reset_portfolio_exploration_for_tests() -> None:
    global _enabled, _ranked_universe, _open_book, _daemon_thread, _snapshot
    global _last_rotation_counsel, _rotation_sweep_count
    _daemon_stop.set()
    _daemon_thread = None
    _rotation_events.clear()
    _rotation_dropped_epics.clear()
    _shadow_veto_streak.clear()
    _last_rotation_counsel = ""
    _rotation_sweep_count = 0
    with _spread_fuse_lock:
        _spread_fuse_samples.clear()
        _spread_fuse_frozen_until.clear()
        _spread_fuse_last.clear()
    with _REGIME_KALMAN_LOCK:
        _REGIME_KALMAN_STATE.clear()
    with _volume_profile_lock:
        _volume_ticks.clear()
        _session_hvn.clear()
    with _covariance_lock:
        global _covariance_last_ts, _covariance_compression_factor, _covariance_snapshot
        _covariance_last_ts = 0.0
        _covariance_compression_factor = 1.0
        _covariance_snapshot = {
            "ok": False,
            "epics": [],
            "collective_coefficient": 0.0,
            "compression_factor": 1.0,
            "risk_parity_boundary": _COVARIANCE_RISK_PARITY_BOUND,
        }
    with _lock:
        _enabled = True
        _ranked_universe = []
        _open_book = []
        _snapshot.clear()
        _snapshot.update(
            {
                "ok": True,
                "healthy": False,
                "enabled": True,
                "account_equity_target_gbp": ACCOUNT_EQUITY_TARGET_GBP,
                "universe_size": 0,
                "market_rankings": [],
                "capital_allocation_pct": 0.0,
                "margin_used_gbp": 0.0,
                "margin_available_gbp": ACCOUNT_EQUITY_TARGET_GBP,
                "max_concurrent_trades": 0,
                "open_positions": 0,
                "correlation_exposures": [],
                "position_tree": [],
                "ts": 0.0,
            }
        )


def inject_exploration_rankings_for_tests(
    rankings: list[dict[str, Any]],
    *,
    max_concurrent: int | None = None,
    open_positions: int = 0,
    margin_used_gbp: float = 0.0,
) -> None:
    """Test hook — seed ranked universe without running async scanner."""
    global _enabled
    with _lock:
        _enabled = True
        _ranked_universe.clear()
        _ranked_universe.extend(rankings)
        _snapshot["enabled"] = True
        _snapshot["healthy"] = True
        _snapshot["universe_size"] = len(rankings)
        _snapshot["market_rankings"] = list(rankings)
        _snapshot["max_concurrent_trades"] = (
            max_concurrent if max_concurrent is not None else max(1, len(rankings))
        )
        _snapshot["open_positions"] = open_positions
        _snapshot["margin_used_gbp"] = margin_used_gbp
        avail = max(0.0, ACCOUNT_EQUITY_TARGET_GBP - margin_used_gbp)
        _snapshot["margin_available_gbp"] = avail
        _snapshot["capital_allocation_pct"] = round(
            margin_used_gbp / ACCOUNT_EQUITY_TARGET_GBP * 100.0, 2
        )
        _snapshot["ts"] = time.time()
