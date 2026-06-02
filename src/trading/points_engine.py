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
from system.closed_trades_display import is_excluded_display_row
from system.engine_log import log_engine
from system.paths import data_dir
from system.state_manager import atomic_write_json, read_json_file

PointsStateName = Literal["HEALTHY", "CAUTION", "WARNING", "STOP"]

STATE_VERSION = 1
MIN_CONFIRMED_FOR_SCALED_SCORING = 5
ROLLING_TRADE_WINDOW = 20
SESSION_LOSS_STREAK_TRIGGER = 3
SIGNALS_TO_SKIP_AFTER_STREAK = 3
DAY_STOP_SESSION_SCORE = -5.0
RAPID_DRAWDOWN_GBP = 100.0
RAPID_DRAWDOWN_WINDOW_SEC = 3600.0
RAPID_DRAWDOWN_COOLDOWN_SEC = 1800.0

CONF_HIGH = 92.0
CONF_STANDARD_MIN = 85.0
CONF_MARGINAL_MIN = 80.0

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


def _nominal_state(cumulative: float) -> PointsStateName:
    if cumulative > 10.0:
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
        self._day_stopped = False
        self._stop_latched = False
        self._last_nominal: PointsStateName = "HEALTHY"
        self._gbp_loss_events: list[tuple[float, float]] = []
        self._rapid_cooldown_until: float = 0.0
        self._load()

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
            "day_stopped": self._day_stopped,
            "stop_latched": self._stop_latched,
            "last_nominal": self._last_nominal,
        }

    def _apply_payload(self, data: dict[str, Any]) -> None:
        cum = data.get("cumulative_points", data.get("cumulative", 0.0))
        self._cumulative = float(cum)
        self._session_score = float(data.get("session_score", 0.0))
        self._last_trade_score = float(data.get("last_trade_score", 0.0))
        self._consecutive_losses = int(data.get("consecutive_losses", 0))
        self._signals_to_skip = int(data.get("signals_to_skip", 0))
        self._recovery_wins = int(data.get("recovery_wins", 0))
        self._day_stopped = bool(data.get("day_stopped", False))
        self._stop_latched = bool(data.get("stop_latched", False))
        last = data.get("last_nominal", "HEALTHY")
        self._last_nominal = last if last in ("HEALTHY", "CAUTION", "WARNING", "STOP") else "HEALTHY"

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
        except Exception as e:
            log_engine(f"points_engine load failed: {type(e).__name__}: {e}")

    def _sync_stop_latch(self) -> None:
        if _nominal_state(self._cumulative) == "STOP":
            self._stop_latched = True

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
        wins = [float(r["pnl"]) for r in rows if r["result"] == "WIN" and float(r["pnl"]) > 0]
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
            if pnl_gbp is not None and float(pnl_gbp) < 0:
                self.note_realised_gbp_loss(float(pnl_gbp))
            score = self._score_trade(result, confidence, pnl_pts)
            result_u = str(result or "").upper()

            self._cumulative += score
            self._session_score += score
            self._last_trade_score = score

            if result_u == "LOSS":
                self._consecutive_losses += 1
                if self._consecutive_losses >= SESSION_LOSS_STREAK_TRIGGER:
                    self._signals_to_skip = max(
                        self._signals_to_skip, SIGNALS_TO_SKIP_AFTER_STREAK
                    )
            elif result_u == "WIN":
                self._consecutive_losses = 0
                self._recovery_wins += 1
            else:
                self._consecutive_losses = 0

            if self._session_score < DAY_STOP_SESSION_SCORE:
                self._day_stopped = True

            new_nominal = _nominal_state(self._cumulative)
            self._on_nominal_transition(new_nominal)
            self._sync_stop_latch()
            self._persist()
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
        """Effective entry bar: higher of points-tier floor and config signal_threshold."""
        try:
            return max(self.get_threshold(), float(cfg.signal_threshold))
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
                return 88.0
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

    def get_size_multiplier(self, confidence: float) -> float:
        """Position size multiplier for confidence and current state (0 = no trade)."""
        try:
            if self.is_day_stopped() or self.is_session_paused():
                return 0.0
            state = self.get_state()
            if state == "STOP":
                return 0.0

            conf = float(confidence)
            if state in ("HEALTHY", "CAUTION"):
                if conf < CONF_MARGINAL_MIN:
                    return 0.0
            elif state == "WARNING":
                if conf < CONF_HIGH:
                    return 0.0
            else:
                return 0.0

            band = _confidence_band(conf)

            if state == "HEALTHY":
                if band == "high":
                    return 1.0
                if band == "standard":
                    return 0.5
                if band == "marginal":
                    return 0.25
                return 0.0

            if state == "CAUTION":
                if conf >= 88.0:
                    return 0.5
                if conf >= CONF_MARGINAL_MIN:
                    return 0.25
                return 0.0

            if state == "WARNING":
                if band == "high":
                    return 0.25
                return 0.0

            return 0.0
        except Exception as exc:
            log_engine(
                f"points_engine: get_size_multiplier EXCEPTION — safe-default 0.5x"
                f" ({type(exc).__name__}: {exc})"
            )
            return 0.5

    def is_session_paused(self) -> bool:
        try:
            with _lock:
                return self._signals_to_skip > 0
        except Exception:
            return False

    def is_day_stopped(self) -> bool:
        try:
            with _lock:
                return self._day_stopped
        except Exception:
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
            self._persist()

    def clear_stop(self) -> None:
        """Manual review — release STOP latch (spec: no auto-recovery from STOP)."""
        with _lock:
            self._stop_latched = False
            self._persist()

    def snapshot(self) -> PointsSnapshot:
        with _lock:
            return self._snapshot()
