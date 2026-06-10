"""
Points engine — tiered confidence, cumulative scoring, session/day guards.

Sections 4.2 and 4.3 of the v25 spec. Persists to src/data/state/points_state.json.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from data.learning_store import LearningStore
from system.engine_log import log_engine
from system.paths import data_dir
from system.state_manager import atomic_write_json, read_json_file

PointsStateName = Literal["HEALTHY", "CAUTION", "WARNING", "STOP"]

STATE_VERSION = 1
MIN_CONFIRMED_FOR_SCALED_SCORING = 5
ROLLING_TRADE_WINDOW = 20
SESSION_LOSS_STREAK_TRIGGER = 6
SIGNALS_TO_SKIP_AFTER_STREAK = 1
DAY_STOP_SESSION_SCORE = -50.0
RAPID_DRAWDOWN_GBP = 2000.0
RAPID_DRAWDOWN_WINDOW_SEC = 3600.0
RAPID_DRAWDOWN_COOLDOWN_SEC = 300.0

CONF_HIGH = 92.0
CONF_STANDARD_MIN = 85.0
CONF_MARGINAL_MIN = 55.0

_lock = threading.RLock()
_path_override: Path | None = None


def _default_path() -> Path:
    if _path_override is not None:
        return _path_override
    return data_dir() / "state" / "points_state.json"


def set_points_state_path_for_tests(path: Path | str | None) -> None:
    global _path_override
    with _lock:
        _path_override = Path(path) if path else None


HEALTHY_CUMULATIVE_MIN = 4.0  # was 6.0 — faster recovery to full size after drawdown
ROADMAP_COMPOUND_ENTER_CUMULATIVE = 15.0
ROADMAP_COMPOUND_EXIT_CUMULATIVE = 10.0
ROADMAP_COMPOUND_BOOST_MULTIPLIER = 2.5

EQUITY_LOCK_SESSION_MILESTONE = 3.0
EQUITY_LOCK_SIZE_MULTIPLIER = 0.5
EQUITY_LOCK_SIGNAL_THRESHOLD = 65.0

# Hard multiplier floors (applied before equity lock) for HEALTHY/CAUTION entries.
PROBE_MULTIPLIER_FLOOR = 0.5
CORE_MULTIPLIER_FLOOR = 0.8


def _clamp_multiplier_to_trade_size_bounds(
    multiplier: float,
    *,
    trade_size: float,
    min_size: float,
    max_size: float,
) -> float:
    """Maps sizing multiplier to actual trade volumes, clamps, and reverses mapping."""
    if multiplier <= 0.0 or trade_size <= 0.0:
        return multiplier
    implied = trade_size * multiplier
    clamped = max(min_size, min(max_size, implied))
    return clamped / trade_size


def _nominal_state(cumulative: float) -> PointsStateName:
    if cumulative > HEALTHY_CUMULATIVE_MIN:
        return "HEALTHY"
    if cumulative >= -5.0:
        return "CAUTION"
    if cumulative >= -30.0:
        return "WARNING"
    return "STOP"


def _confidence_band(confidence: float) -> str:
    if confidence >= CONF_HIGH:
        return "high"
    if confidence >= CONF_STANDARD_MIN:
        return "standard"
    if confidence >= CONF_MARGINAL_MIN:
        return "marginal"
    return "low"


@dataclass
class PointsSnapshot:
    cumulative: float = 0.0
    session_score: float = 0.0
    last_trade_score: float = 0.0
    consecutive_losses: int = 0
    signals_to_skip: int = 0
    recovery_wins: int = 0
    day_stopped: bool = False
    stop_latched: bool = False
    nominal_state: PointsStateName = "HEALTHY"
    bootstrap_wins: int = 0  # wins since bootstrap floor was first lowered


class PointsEngine:
    """Cumulative points, tiered thresholds, and session guards."""

    def __init__(
        self,
        store: LearningStore | None = None,
        *,
        state_path: Path | str | None = None,
    ) -> None:
        self._store = store
        self._path = Path(state_path) if state_path else _default_path()
        self._cumulative = 0.0
        self._session_score = 0.0
        self._last_trade_score = 0.0
        self._consecutive_losses = 0
        self._signals_to_skip = 0
        self._recovery_wins = 0
        self._bootstrap_wins = 0
        self._day_stopped = False
        self._stop_latched = False
        self._last_nominal: PointsStateName = "HEALTHY"
        self._telegram_last_effective: PointsStateName | None = None
        self._gbp_loss_events: list[tuple[float, float]] = []
        self._rapid_cooldown_until: float = 0.0
        self._roadmap_compound_boost = False
        self._equity_lock_announced = False
        self._load()

    def equity_lock_active(self) -> bool:
        """Session milestone protection — preserve gains after strong session_score."""
        try:
            with _lock:
                return self._session_score >= EQUITY_LOCK_SESSION_MILESTONE
        except Exception:
            return False

    def protected_signal_threshold_floor(self) -> float | None:
        """Raised entry bar when equity lock is active."""
        if self.equity_lock_active():
            return EQUITY_LOCK_SIGNAL_THRESHOLD
        return None

    def _snapshot(self) -> PointsSnapshot:
        nominal = _nominal_state(self._cumulative)
        return PointsSnapshot(
            cumulative=self._cumulative,
            session_score=self._session_score,
            last_trade_score=self._last_trade_score,
            consecutive_losses=self._consecutive_losses,
            signals_to_skip=self._signals_to_skip,
            recovery_wins=self._recovery_wins,
            day_stopped=self._day_stopped,
            stop_latched=self._stop_latched,
            nominal_state=nominal,
            bootstrap_wins=self._bootstrap_wins,
        )

    def _payload(self) -> dict[str, Any]:
        eff = self._effective_state_unlocked()
        return {
            "version": STATE_VERSION,
            "cumulative": self._cumulative,
            "cumulative_points": self._cumulative,
            "state": eff,
            "session_score": self._session_score,
            "last_trade_score": self._last_trade_score,
            "consecutive_losses": self._consecutive_losses,
            "signals_to_skip": self._signals_to_skip,
            "recovery_wins": self._recovery_wins,
            "bootstrap_wins": self._bootstrap_wins,
            "day_stopped": self._day_stopped,
            "stop_latched": self._stop_latched,
            "last_nominal": self._last_nominal,
            "rapid_cooldown_until": self._rapid_cooldown_until,
            "equity_lock_active": self.equity_lock_active(),
            "equity_lock_milestone": EQUITY_LOCK_SESSION_MILESTONE,
            "equity_lock_signal_threshold": EQUITY_LOCK_SIGNAL_THRESHOLD,
        }

    def _apply_payload(self, data: dict[str, Any]) -> None:
        cum = data.get("cumulative_points", data.get("cumulative", 0.0))
        self._cumulative = float(cum)
        self._session_score = float(data.get("session_score", 0.0))
        self._last_trade_score = float(data.get("last_trade_score", 0.0))
        self._consecutive_losses = int(data.get("consecutive_losses", 0))
        self._signals_to_skip = int(data.get("signals_to_skip", 0))
        self._recovery_wins = int(data.get("recovery_wins", 0))
        self._bootstrap_wins = int(data.get("bootstrap_wins", 0))
        self._day_stopped = bool(data.get("day_stopped", False))
        self._stop_latched = bool(data.get("stop_latched", False))
        last = data.get("last_nominal", "HEALTHY")
        self._last_nominal = (
            last if last in ("HEALTHY", "CAUTION", "WARNING", "STOP") else "HEALTHY"
        )
        restored_cooldown = float(data.get("rapid_cooldown_until", 0.0))
        self._rapid_cooldown_until = max(0.0, restored_cooldown)

    def _persist(self) -> None:
        try:
            atomic_write_json(self._path, self._payload())
        except Exception as e:
            log_engine(f"points_engine persist failed: {type(e).__name__}: {e}")

    def _load(self) -> None:
        data = read_json_file(self._path)
        if not data:
            return
        try:
            self._apply_payload(data)
            self._sync_stop_latch()
            self._last_nominal = _nominal_state(self._cumulative)
            log_engine(
                f"points_engine: loaded from state — cumulative={self._cumulative:.1f} "
                f"state={self.get_state()}"
            )
            self._maybe_notify_telegram_state()
        except Exception as e:
            log_engine(f"points_engine load failed: {type(e).__name__}: {e}")

    def _sync_stop_latch(self) -> None:
        if _nominal_state(self._cumulative) == "STOP":
            self._stop_latched = True

    def _maybe_notify_telegram_state(self) -> None:
        try:
            from system.telegram_notifier import get_telegram_notifier

            notifier = get_telegram_notifier()
            if notifier is None or not notifier.enabled:
                return
            new_state = self.get_state()
            old = self._telegram_last_effective
            if old is not None and new_state != old:
                notifier.notify_points_state_change(
                    old, new_state, float(self._cumulative)
                )
            self._telegram_last_effective = new_state
        except Exception as e:
            log_engine(f"points_engine telegram notify failed: {type(e).__name__}: {e}")

    def _on_nominal_transition(self, new_nominal: PointsStateName) -> None:
        old = self._last_nominal
        rank = {"HEALTHY": 0, "CAUTION": 1, "WARNING": 2, "STOP": 3}
        if rank[new_nominal] > rank[old]:
            self._recovery_wins = 0
        elif rank[new_nominal] < rank[old]:
            self._recovery_wins = 0
        self._last_nominal = new_nominal
        if new_nominal == "STOP":
            self._stop_latched = True

    def _confirmed_trade_stats(self) -> tuple[int, float, float]:
        """Return (count, avg_win_pnl, avg_loss_pnl) from last 20 IG-confirmed closes."""
        rows = self._fetch_confirmed_rows(ROLLING_TRADE_WINDOW)
        if not rows:
            return 0, 0.0, 0.0
        wins = [
            float(r["pnl"])
            for r in rows
            if r["result"] == "WIN" and float(r["pnl"]) > 0
        ]
        losses = [
            abs(float(r["pnl"]))
            for r in rows
            if r["result"] == "LOSS" and float(r["pnl"]) < 0
        ]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        return len(rows), avg_win, avg_loss

    def _fetch_confirmed_rows(self, limit: int) -> list[dict[str, Any]]:
        if self._store is None:
            return []
        try:
            return self._store.recent_confirmed_closed_trades(limit=limit)
        except Exception as e:
            log_engine(f"points_engine store query failed: {type(e).__name__}: {e}")
            return []

    def _use_flat_scoring(self) -> bool:
        count, _, _ = self._confirmed_trade_stats()
        return count < MIN_CONFIRMED_FOR_SCALED_SCORING

    def _score_trade(self, result: str, confidence: float, pnl_pts: float) -> float:
        result_u = str(result or "").upper()
        if result_u == "BREAKEVEN":
            return 0.0

        if self._use_flat_scoring():
            if result_u == "WIN":
                return 1.0
            if result_u == "LOSS":
                return -1.0
            return 0.0

        _, avg_win, avg_loss = self._confirmed_trade_stats()
        band = _confidence_band(confidence)
        pnl = float(pnl_pts)

        if result_u == "WIN":
            if band == "high":
                return (3.0 * pnl / avg_win) if avg_win > 0 else 1.0
            if band == "standard":
                return (2.0 * pnl / avg_win) if avg_win > 0 else 1.0
            if band == "marginal":
                return 1.0
            return 0.0

        if result_u == "LOSS":
            loss_amt = abs(pnl) if pnl != 0 else (avg_loss if avg_loss > 0 else 1.0)
            if band == "high":
                return -(4.0 * loss_amt / avg_loss) if avg_loss > 0 else -1.0
            if band == "standard":
                return -(2.0 * loss_amt / avg_loss) if avg_loss > 0 else -1.0
            if band == "marginal":
                return -1.0
            return 0.0

        return 0.0

    def note_realised_gbp_loss(self, loss_gbp: float) -> None:
        """Rolling 60-minute GBP loss — force WARNING for 30 minutes when > £100."""
        try:
            if loss_gbp >= 0:
                return
            amount = abs(float(loss_gbp))
            now = time.time()
            with _lock:
                self._gbp_loss_events.append((now, amount))
                cutoff = now - RAPID_DRAWDOWN_WINDOW_SEC
                self._gbp_loss_events = [
                    (t, a) for t, a in self._gbp_loss_events if t >= cutoff
                ]
                total = sum(a for _, a in self._gbp_loss_events)
                if total > RAPID_DRAWDOWN_GBP:
                    self._rapid_cooldown_until = now + RAPID_DRAWDOWN_COOLDOWN_SEC
                    log_engine("RAPID DRAWDOWN — cooling 30min")
                    self._persist()
        except Exception as e:
            log_engine(f"points_engine rapid drawdown failed: {type(e).__name__}: {e}")

    def _effective_state_unlocked(self) -> PointsStateName:
        if self._stop_latched:
            return "STOP"

        if time.time() < self._rapid_cooldown_until:
            nominal = _nominal_state(self._cumulative)
            rank = {"HEALTHY": 0, "CAUTION": 1, "WARNING": 2, "STOP": 3}
            forced: PointsStateName = "WARNING"
            if rank[nominal] > rank[forced]:
                return nominal
            return forced

        nominal = _nominal_state(self._cumulative)
        if nominal == "STOP":
            return "STOP"

        rank = {"HEALTHY": 0, "CAUTION": 1, "WARNING": 2, "STOP": 3}
        candidates: list[PointsStateName] = [nominal]
        if self._recovery_wins >= 5:
            candidates.append("HEALTHY")
        elif self._recovery_wins >= 3:
            candidates.append("CAUTION")
        return min(candidates, key=lambda s: rank[s])

    def record_trade(
        self,
        result: str,
        confidence: float,
        pnl_pts: float,
        *,
        pnl_gbp: float | None = None,
    ) -> float:
        """Score trade, update cumulative/session state, persist. Returns points scored."""
        try:
            with _lock:
                if pnl_gbp is not None and float(pnl_gbp) < 0:
                    self.note_realised_gbp_loss(float(pnl_gbp))
                score = self._score_trade(result, confidence, pnl_pts)
                result_u = str(result or "").upper()

                self._cumulative += score
                self._session_score += score
                self._last_trade_score = score

                if (
                    self._session_score >= EQUITY_LOCK_SESSION_MILESTONE
                    and not self._equity_lock_announced
                ):
                    self._equity_lock_announced = True
                    log_engine(
                        f"EQUITY LOCK active — session_score "
                        f"{self._session_score:.1f} >= {EQUITY_LOCK_SESSION_MILESTONE:.1f}: "
                        f"{EQUITY_LOCK_SIZE_MULTIPLIER:.1f}× size, "
                        f"{EQUITY_LOCK_SIGNAL_THRESHOLD:.0f}% signal threshold"
                    )

                if result_u == "LOSS":
                    self._consecutive_losses += 1
                    if self._consecutive_losses >= SESSION_LOSS_STREAK_TRIGGER:
                        self._signals_to_skip = max(
                            self._signals_to_skip, SIGNALS_TO_SKIP_AFTER_STREAK
                        )
                elif result_u == "WIN":
                    self._consecutive_losses = 0
                    self._recovery_wins += 1
                    self._bootstrap_wins += 1
                else:
                    self._consecutive_losses = 0

                # Day-stop via session points disabled — max_daily_loss_gbp gate
                # is the authoritative GBP-denominated risk control on large accounts.
                # self._day_stopped = self._session_score < DAY_STOP_SESSION_SCORE

                new_nominal = _nominal_state(self._cumulative)
                self._on_nominal_transition(new_nominal)
                self._sync_stop_latch()
                self._persist()
            self._maybe_notify_telegram_state()
            return score
        except Exception as e:
            log_engine(f"points_engine record_trade failed: {type(e).__name__}: {e}")
            return 0.0

    def get_state(self) -> PointsStateName:
        try:
            with _lock:
                return self._effective_state_unlocked()
        except Exception as exc:
            log_engine(
                f"points_engine: get_state EXCEPTION — safe-default HEALTHY"
                f" ({type(exc).__name__}: {exc})"
            )
            return "HEALTHY"

    def get_threshold(self) -> float:
        """Minimum confidence (%) required to trade in the current effective state."""
        try:
            state = self.get_state()
            if state in ("STOP",) or self.is_day_stopped():
                return 100.0
            if state == "WARNING":
                return CONF_HIGH
            return CONF_MARGINAL_MIN
        except Exception:
            return CONF_MARGINAL_MIN

    def trade_confidence_threshold(self, cfg: Any) -> float:
        """Effective entry bar for CAUTION/HEALTHY.

        Uses cfg.confidence_floor (configurable, default 55) as the tier floor,
        boosted by bootstrap_wins * recovery_per_win toward CONF_MARGINAL_MIN.
        """
        try:
            state = self.get_state()
            if state in ("STOP",) or self.is_day_stopped():
                return 100.0
            if state == "WARNING":
                return CONF_HIGH
            cfg_floor = float(getattr(cfg, "confidence_floor", CONF_MARGINAL_MIN))
            recovery = float(getattr(cfg, "confidence_floor_recovery_per_win", 1.0))
            with _lock:
                bootstrap_wins = self._bootstrap_wins
            # Floor rises with each win; caps at CONF_MARGINAL_MIN (55)
            effective_floor = min(
                cfg_floor + bootstrap_wins * recovery, CONF_MARGINAL_MIN
            )
            threshold = max(effective_floor, float(cfg.signal_threshold))
            prot = self.protected_signal_threshold_floor()
            if prot is not None:
                threshold = max(threshold, prot)
            return threshold
        except Exception:
            return CONF_MARGINAL_MIN

    def min_size_confidence_threshold(self) -> float:
        """Minimum confidence for meaningful size (0.5× band) in the current points state."""
        try:
            state = self.get_state()
            if state in ("STOP",) or self.is_day_stopped():
                return 100.0
            if state == "WARNING":
                return CONF_HIGH
            if state == "CAUTION":
                return CONF_MARGINAL_MIN
            if state == "HEALTHY":
                return CONF_STANDARD_MIN
            return CONF_MARGINAL_MIN
        except Exception:
            return CONF_MARGINAL_MIN

    def session_skips_remaining(self) -> int:
        try:
            with _lock:
                return max(0, int(self._signals_to_skip))
        except Exception:
            return 0

    def confidence_band(self, confidence: float) -> str:
        """Public confidence band label (high / standard / marginal / low)."""
        return _confidence_band(float(confidence))

    def _roadmap_cumulative_scale(self) -> float:
        """Roadmap Layer 2: 2.5× win-streak boost with 10–14.9 pt hysteresis band."""
        cumulative = self._cumulative
        if cumulative >= ROADMAP_COMPOUND_ENTER_CUMULATIVE:
            self._roadmap_compound_boost = True
            return ROADMAP_COMPOUND_BOOST_MULTIPLIER
        if cumulative < ROADMAP_COMPOUND_EXIT_CUMULATIVE:
            self._roadmap_compound_boost = False
            return 1.0
        return (
            ROADMAP_COMPOUND_BOOST_MULTIPLIER if self._roadmap_compound_boost else 1.0
        )

    def _finalize_size_multiplier(
        self,
        raw: float,
        *,
        confidence: float | None = None,
    ) -> float:
        if raw <= 0.0:
            return 0.0
        scaled = raw * self._roadmap_cumulative_scale()

        if confidence is not None:
            conf = float(confidence)
            state = self.get_state()
            entry_min = CONF_MARGINAL_MIN
            try:
                from system.risk_bands import bands_enabled, entry_confidence_floor

                if bands_enabled():
                    entry_min = entry_confidence_floor()
            except Exception:
                pass
            if state in ("HEALTHY", "CAUTION") and conf >= entry_min:
                mult_floor = CORE_MULTIPLIER_FLOOR
                try:
                    from system.risk_bands import (
                        bands_enabled,
                        risk_band_for_confidence,
                    )

                    if bands_enabled() and risk_band_for_confidence(conf) == "probe":
                        mult_floor = PROBE_MULTIPLIER_FLOOR
                except Exception:
                    pass
                scaled = max(scaled, mult_floor)

        if self.equity_lock_active():
            scaled *= EQUITY_LOCK_SIZE_MULTIPLIER
        try:
            from system.config_loader import get_config

            cfg = get_config()
            return _clamp_multiplier_to_trade_size_bounds(
                scaled,
                trade_size=float(cfg.trade_size),
                min_size=float(cfg.adaptive_min_trade_size),
                max_size=float(cfg.adaptive_max_trade_size),
            )
        except Exception:
            return scaled

    def get_size_multiplier(self, confidence: float) -> float:
        """Position size multiplier for confidence and current state (0 = no trade)."""
        try:
            if self.is_day_stopped() or self.is_session_paused():
                return 0.0
            state = self.get_state()
            if state == "STOP":
                return 0.0

            conf = float(confidence)
            entry_min = CONF_MARGINAL_MIN
            try:
                from system.risk_bands import bands_enabled, entry_confidence_floor

                if bands_enabled():
                    entry_min = entry_confidence_floor()
            except Exception:
                pass
            if state in ("HEALTHY", "CAUTION"):
                if conf < entry_min:
                    return 0.0
            elif state == "WARNING":
                if conf < CONF_HIGH:
                    return 0.0
            else:
                return 0.0

            band = _confidence_band(conf)

            if state == "HEALTHY":
                # Progressive multiplier — rewards sustained winning
                cum = self._cumulative
                if cum > 50.0:
                    tier_mult = 4.0  # EXCELLENT: cumulative > 50
                elif cum > 25.0:
                    tier_mult = 2.5  # THRIVING:  cumulative > 25
                elif cum > HEALTHY_CUMULATIVE_MIN:
                    tier_mult = 1.5  # HEALTHY: above nominal floor
                else:
                    tier_mult = 1.0
                try:
                    from system.risk_bands import (
                        bands_enabled,
                        core_size_multiplier,
                        risk_band_for_confidence,
                    )

                    if bands_enabled():
                        rb = risk_band_for_confidence(conf)
                        if rb == "probe":
                            return self._finalize_size_multiplier(
                                tier_mult * 0.25, confidence=conf
                            )
                        if rb == "core":
                            core = core_size_multiplier()
                            if band == "high":
                                return self._finalize_size_multiplier(
                                    tier_mult * core, confidence=conf
                                )
                            if band == "standard":
                                return self._finalize_size_multiplier(
                                    tier_mult * 0.5 * core, confidence=conf
                                )
                            if band == "marginal":
                                return self._finalize_size_multiplier(
                                    tier_mult * 0.25 * core, confidence=conf
                                )
                except Exception:
                    pass
                if band == "high":
                    return self._finalize_size_multiplier(tier_mult, confidence=conf)
                if band == "standard":
                    return self._finalize_size_multiplier(
                        tier_mult * 0.5, confidence=conf
                    )
                if band == "marginal":
                    return self._finalize_size_multiplier(
                        tier_mult * 0.25, confidence=conf
                    )
                return 0.0

            if state == "CAUTION":
                try:
                    from system.risk_bands import (
                        bands_enabled,
                        risk_band_for_confidence,
                    )

                    if bands_enabled() and risk_band_for_confidence(conf) == "probe":
                        return self._finalize_size_multiplier(0.25, confidence=conf)
                except Exception:
                    pass
                if conf >= CONF_MARGINAL_MIN:
                    return self._finalize_size_multiplier(0.5, confidence=conf)
                if conf >= entry_min:
                    return self._finalize_size_multiplier(0.25, confidence=conf)
                return 0.0

            if state == "WARNING":
                if band == "high":
                    return self._finalize_size_multiplier(0.25, confidence=conf)
                return 0.0

            return 0.0
        except Exception as exc:
            log_engine(
                f"points_engine: get_size_multiplier EXCEPTION — safe-default 0.0x (no trade)"
                f" ({type(exc).__name__}: {exc})"
            )
            return 0.0

    def is_session_paused(self) -> bool:
        try:
            with _lock:
                return self._signals_to_skip > 0
        except Exception:
            return False

    def is_day_stopped(self) -> bool:
        # Day-stop disabled — max_daily_loss_gbp is the authoritative hard stop.
        return False

    def consume_signal_skip(self) -> bool:
        """If session-paused, consume one skipped signal slot and return True."""
        with _lock:
            if self._signals_to_skip <= 0:
                return False
            self._signals_to_skip -= 1
            self._persist()
            return True

    def reset_session(self) -> None:
        """Called at each session open — clears session counters, not STOP latch."""
        with _lock:
            self._session_score = 0.0
            self._consecutive_losses = 0
            self._signals_to_skip = 0
            self._day_stopped = False
            self._recovery_wins = 0
            self._equity_lock_announced = False
            self._persist()

    def clear_stop(self) -> None:
        """Manual review — release STOP latch (spec: no auto-recovery from STOP)."""
        with _lock:
            self._stop_latched = False
            self._persist()

    def snapshot(self) -> PointsSnapshot:
        with _lock:
            return self._snapshot()
