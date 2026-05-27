"""
Session lifecycle — IG calendar-driven open/close, cold start, gap, flatten.

Section 4.5 Step 5 / 6.5. Never hardcodes BST/JST session hours.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from data.models import Quote
from system.engine_log import log_engine
from system.market_watch.calendar import (
    get_market_status,
    is_market_open,
    is_session_end_flatten_window,
)
from system.paths import data_dir
from system.state_manager import atomic_write_json, read_json_file

if TYPE_CHECKING:
    from signals.signal_engine import SignalEngine
    from trading.environment_scorer import EnvironmentScorer
    from trading.points_engine import PointsEngine

TickPhase = Literal["OPEN", "CLOSED", "FLATTEN", "MAINTENANCE"]

STATE_VERSION = 1
DEFAULT_STATE_FILE = "session_state.json"
COLD_START_BARS = 6
GAP_ATR_MULTIPLE = 1.0
MAINTENANCE_MAX_GAP_SEC = 2 * 3600
AUTOSAVE_INTERVAL_SEC = 30.0
FLATTEN_LEAD_MINUTES = 5.0


@dataclass
class SessionSnapshot:
    session_open: bool = False
    open_time: str | None = None
    bars_elapsed: int = 0
    gap_detected: bool = False
    last_close_time: str | None = None
    last_close_price: float | None = None
    maintenance_count_today: int = 0
    phase: TickPhase = "CLOSED"
    is_cold_start: bool = True
    is_maintenance: bool = False


class SessionManager:
    """Tracks IG market session edges, cold start, gap, and flatten window."""

    def __init__(
        self,
        epic: str,
        *,
        market: str = "",
        points_engine: PointsEngine | None = None,
        environment_scorer: EnvironmentScorer | None = None,
        signal_engine: SignalEngine | None = None,
        state_path: Path | str | None = None,
        maintenance_gap_hours: float = 2.0,
        flatten_lead_minutes: float = 5.0,
        autosave_interval_sec: float = 30.0,
    ) -> None:
        self._epic = str(epic)
        self._market = market or epic
        self._points = points_engine
        self._env_scorer = environment_scorer
        self._signal_engine = signal_engine
        self._path = (
            Path(state_path)
            if state_path
            else data_dir() / "state" / DEFAULT_STATE_FILE
        )
        self._maintenance_gap_sec = float(maintenance_gap_hours) * 3600.0
        self._flatten_lead = float(flatten_lead_minutes)
        self._autosave_interval = float(autosave_interval_sec)

        self._session_open = False
        self._open_time: datetime | None = None
        self._bars_at_open = 0
        self._gap_detected = False
        self._gap_checked = False
        self._last_close_time: datetime | None = None
        self._last_close_price: float | None = None
        self._maintenance_count_today = 0
        self._last_persist_ts = 0.0
        self._phase: TickPhase = "CLOSED"
        self._maintenance_reopen_active = False

        self._load_state()

    def _sync_session_open_from_calendar(self) -> None:
        try:
            self._session_open = is_market_open(self._epic)
        except Exception:
            self._session_open = False

    def _complete_bar_count(self) -> int:
        if self._signal_engine is None:
            return 0
        try:
            df = self._signal_engine.quote_df(self._market)
            c5 = self._signal_engine.candles(df, 5)
            return max(0, len(c5) - 1)
        except Exception:
            return 0

    def is_session_open(self, *, at: datetime | None = None) -> bool:
        try:
            return bool(is_market_open(self._epic, at=at))
        except Exception:
            return False

    def bars_since_open(self) -> int:
        return max(0, self._complete_bar_count() - int(self._bars_at_open))

    def is_cold_start(self) -> bool:
        return self.bars_since_open() < COLD_START_BARS

    def check_gap_open(self, atr: float, *, open_price: float | None = None) -> bool:
        """
        True when opening gap exceeds 1.0× ATR vs prior session close.
        Registers gap cap on environment_scorer once per session.
        """
        if self._gap_checked:
            return self._gap_detected
        self._gap_checked = True

        try:
            atr_v = float(atr)
            if atr_v <= 0:
                return False
            if open_price is None or self._last_close_price is None:
                return False
            gap_pts = abs(float(open_price) - float(self._last_close_price))
            if gap_pts <= GAP_ATR_MULTIPLE * atr_v:
                return False
            self._gap_detected = True
            if self._env_scorer is not None:
                self._env_scorer.register_gap_open(self._market)
            log_engine(
                f"session_manager gap open: {gap_pts:.1f} pts > "
                f"{GAP_ATR_MULTIPLE:.1f}x ATR ({atr_v:.1f})"
            )
            self._persist()
            return True
        except Exception as e:
            log_engine(f"session_manager check_gap_open failed: {type(e).__name__}: {e}")
            return False

    def should_flatten(self, *, at: datetime | None = None) -> bool:
        try:
            return is_session_end_flatten_window(
                self._epic,
                lead_minutes=self._flatten_lead,
                at=at,
            )
        except Exception:
            return False

    def _check_maintenance_reopen(self, now: datetime) -> bool:
        if self._last_close_time is None:
            return False
        elapsed = (now - self._last_close_time).total_seconds()
        return 0 < elapsed < self._maintenance_gap_sec

    def _is_daily_maintenance_closed(self, *, at: datetime | None = None) -> bool:
        status = get_market_status(self._epic, at=at)
        if status is None or status.open:
            return False
        reason = str(status.reason or "").lower()
        return "break" in reason or "maintenance" in reason

    def on_session_open(self, quote: Quote | None = None, *, at: datetime | None = None) -> None:
        now = at or (quote.time if quote is not None else datetime.now())
        maintenance = self._check_maintenance_reopen(now)
        self._maintenance_reopen_active = maintenance
        self._session_open = True
        self._open_time = now
        self._bars_at_open = self._complete_bar_count()
        self._gap_checked = False
        self._gap_detected = False

        if maintenance:
            self._maintenance_count_today += 1
            log_engine(
                f"session_manager maintenance reopen epic={self._epic} "
                f"(count today={self._maintenance_count_today}) — cold start reset only"
            )
            if self._env_scorer is not None:
                self._env_scorer.reset_session(self._market, opened_at=now)
        else:
            log_engine(f"session_manager session OPEN epic={self._epic}")
            if self._points is not None:
                self._points.reset_session()
            if self._env_scorer is not None:
                self._env_scorer.reset_session(self._market, opened_at=now)

        if quote is not None:
            px = float(quote.mid)
            atr = self._entry_atr_from_quote(quote)
            if atr > 0:
                self.check_gap_open(atr, open_price=px)

        self._persist(force=True)

    def on_session_close(self, quote: Quote | None = None, *, at: datetime | None = None) -> None:
        now = at or (quote.time if quote is not None else datetime.now())
        self._session_open = False
        self._last_close_time = now
        if quote is not None:
            self._last_close_price = float(quote.mid)
        self._phase = "CLOSED"
        log_engine(f"session_manager session CLOSE epic={self._epic}")
        self._persist(force=True)

    def on_tick(self, quote: Quote) -> TickPhase:
        try:
            now = quote.time if isinstance(quote.time, datetime) else datetime.now()
            was_open = self._session_open
            open_now = self.is_session_open(at=now)

            if not was_open and open_now:
                self.on_session_open(quote, at=now)
            elif was_open and not open_now:
                self.on_session_close(quote, at=now)

            self._session_open = open_now

            if not open_now:
                if self._is_daily_maintenance_closed(at=now):
                    self._phase = "MAINTENANCE"
                else:
                    self._phase = "CLOSED"
                self._maybe_persist()
                return self._phase

            if self.should_flatten(at=now):
                self._phase = "FLATTEN"
                self._maybe_persist()
                return self._phase

            self._phase = "OPEN"
            self._maybe_persist()
            return self._phase
        except Exception as e:
            log_engine(f"session_manager on_tick failed: {type(e).__name__}: {e}")
            self._phase = "CLOSED"
            return "CLOSED"

    def _entry_atr_from_quote(self, quote: Quote) -> float:
        if self._signal_engine is None:
            return 0.0
        try:
            self._signal_engine.add_quote(self._market, quote)
            df = self._signal_engine.quote_df(self._market)
            c5 = self._signal_engine.candles(df, 5)
            if len(c5) < 2:
                return 0.0
            c5i = self._signal_engine.add_indicators(c5)
            return float(c5i.iloc[-2].get("atr", 0) or 0)
        except Exception:
            return 0.0

    def get_state(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "version": STATE_VERSION,
            "session_open": snap.session_open,
            "open_time": snap.open_time,
            "bars_elapsed": snap.bars_elapsed,
            "gap_detected": snap.gap_detected,
            "last_close_time": snap.last_close_time,
            "last_close_price": snap.last_close_price,
            "maintenance_count_today": snap.maintenance_count_today,
            "phase": snap.phase,
            "is_cold_start": snap.is_cold_start,
            "is_maintenance": snap.is_maintenance,
            "epic": self._epic,
        }

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            session_open=self._session_open,
            open_time=self._open_time.isoformat() if self._open_time else None,
            bars_elapsed=self.bars_since_open(),
            gap_detected=self._gap_detected,
            last_close_time=(
                self._last_close_time.isoformat() if self._last_close_time else None
            ),
            last_close_price=self._last_close_price,
            maintenance_count_today=self._maintenance_count_today,
            phase=self._phase,
            is_cold_start=self.is_cold_start(),
            is_maintenance=self._maintenance_reopen_active,
        )

    def _payload(self) -> dict[str, Any]:
        return self.get_state()

    def _apply_payload(self, data: dict[str, Any]) -> None:
        self._session_open = bool(data.get("session_open", False))
        ot = data.get("open_time")
        self._open_time = datetime.fromisoformat(ot) if ot else None
        self._gap_detected = bool(data.get("gap_detected", False))
        lct = data.get("last_close_time")
        self._last_close_time = datetime.fromisoformat(lct) if lct else None
        lcp = data.get("last_close_price")
        self._last_close_price = float(lcp) if lcp is not None else None
        self._maintenance_count_today = int(data.get("maintenance_count_today", 0))
        phase = data.get("phase", "CLOSED")
        self._phase = phase if phase in ("OPEN", "CLOSED", "FLATTEN", "MAINTENANCE") else "CLOSED"
        bars_elapsed = int(data.get("bars_elapsed", 0))
        current = self._complete_bar_count()
        self._bars_at_open = max(0, current - bars_elapsed)

    def _load_state(self) -> None:
        data = read_json_file(self._path)
        if not data:
            self._sync_session_open_from_calendar()
            return
        try:
            self._apply_payload(data)
        except Exception as e:
            log_engine(f"session_manager load failed: {type(e).__name__}: {e}")
            self._sync_session_open_from_calendar()

    def _maybe_persist(self) -> None:
        now = time.time()
        if now - self._last_persist_ts < self._autosave_interval:
            return
        self._persist()

    def _persist(self, *, force: bool = False) -> None:
        if not force:
            now = time.time()
            if now - self._last_persist_ts < self._autosave_interval:
                return
        try:
            atomic_write_json(self._path, self._payload())
            self._last_persist_ts = time.time()
        except Exception as e:
            log_engine(f"session_manager persist failed: {type(e).__name__}: {e}")

    @staticmethod
    def reset_for_tests() -> None:
        """No-op placeholder — tests use fresh instances + temp state paths."""
