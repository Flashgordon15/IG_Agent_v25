"""Pre-trade validation — all limits from Config."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from execution.adaptive_engine import AdaptiveEngine
from execution.cooldown_tracker import CooldownTracker
from execution.types import TradeSignal
from signals.indicators import session_name
from system.config import Config
from system.config_loader import get_config


@dataclass
class ValidationResult:
    allowed: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)


class OrderValidator:
    def __init__(
        self,
        config: Config,
        adaptive: AdaptiveEngine | None = None,
        cooldown: CooldownTracker | None = None,
        store: Any | None = None,
        points_engine: Any | None = None,
    ) -> None:
        self._cfg = config
        self.adaptive = adaptive or AdaptiveEngine(config)
        self.cooldown = cooldown or CooldownTracker(config.cooldown_seconds)
        self._store = store
        self._points = points_engine

    @property
    def config(self) -> Config:
        return get_config()

    def validate(
        self,
        signal: TradeSignal,
        *,
        open_position_count: Callable[[str], int] | None = None,
        open_total_count: Callable[[], int] | None = None,
        has_open_position: Callable[[str], bool] | None = None,
        store_has_position: Callable[[str], bool] | None = None,
        has_pending_open: Callable[[str], bool] | None = None,
    ) -> ValidationResult:
        cfg = self._cfg
        reasons: list[str] = []
        checks: dict[str, bool] = {}

        if signal.direction not in ("BUY", "SELL"):
            checks["signal"] = False
            reasons.append("No actionable signal (WAIT)")
            return ValidationResult(allowed=False, reasons=reasons, checks=checks)
        checks["signal"] = True

        session_ok, session_msg = self.check_session(signal.epic)
        checks["session"] = session_ok
        if not session_ok:
            reasons.append(session_msg)

        market_ok, market_msg = self.check_market_hours(signal.epic)
        checks["market_hours"] = market_ok
        if not market_ok:
            reasons.append(market_msg)

        circuit_ok, circuit_msg = self.check_circuit_breaker()
        checks["circuit_breaker"] = circuit_ok
        if not circuit_ok:
            reasons.append(circuit_msg)

        blocked, block_reason = self.adaptive.should_block(
            signal.setup_key, signal.adjusted_confidence, signal.snapshot
        )
        checks["adaptive"] = not blocked
        if blocked:
            reasons.append(block_reason)

        spread_ok, spread_msg = self.check_spread(signal)
        checks["spread"] = spread_ok
        if not spread_ok:
            reasons.append(spread_msg)

        atr_ok, atr_msg = self.check_atr(signal)
        checks["atr"] = atr_ok
        if not atr_ok:
            reasons.append(atr_msg)

        conf_floor = (
            self._points.trade_confidence_threshold(cfg)
            if self._points is not None
            else float(cfg.signal_threshold)
        )
        conf_ok = signal.adjusted_confidence >= conf_floor
        checks["confidence"] = conf_ok
        if not conf_ok:
            reasons.append(
                f"Adjusted confidence {signal.adjusted_confidence:.0f}% "
                f"below threshold {conf_floor:.0f}"
            )

        pending_open = False
        if has_pending_open is not None:
            pending_open = bool(has_pending_open(signal.epic))
        else:
            try:
                from execution.live_executor import epic_has_pending_open

                pending_open = epic_has_pending_open(signal.epic)
            except Exception:
                pending_open = False
        checks["pending_order"] = not pending_open
        if pending_open:
            reasons.append("Entry already in flight — awaiting IG confirm")

        max_pos = cfg.max_positions_per_epic
        if cfg.one_position_per_epic:
            max_pos = 1

        count = 0
        if open_position_count:
            count = int(open_position_count(signal.epic))
        elif has_open_position and has_open_position(signal.epic):
            count = 1
        elif store_has_position and store_has_position(signal.epic):
            count = max(count, self._store_count_fallback(store_has_position, signal.epic))

        pos_ok = count < max_pos
        if not pos_ok:
            reasons.append(
                f"Max positions reached ({count}/{max_pos})"
            )
        checks["position_limit"] = pos_ok

        max_total = max(1, int(cfg.max_open_positions))
        total_count = 0
        if open_total_count is not None:
            total_raw = open_total_count()
            if isinstance(total_raw, (int, float)):
                total_count = int(total_raw)
        total_ok = total_count < max_total
        if not total_ok:
            reasons.append(
                f"Total open positions reached ({total_count}/{max_total})"
            )
        checks["total_position_limit"] = total_ok

        # Cooldown: when max_positions > 1, allow stacking up to the cap without
        # waiting between concurrent opens; still enforce cooldown after all slots close.
        if count > 0 and count < max_pos:
            cd_ok = True
        else:
            cd_ok = not self.cooldown.is_active(signal.epic, signal.direction)
        checks["cooldown"] = cd_ok
        if not cd_ok:
            reasons.append(
                f"Cooldown active ({self.cooldown.format_remaining(signal.epic, signal.direction)} remaining)"
            )

        allowed = all(checks.values()) and not reasons
        return ValidationResult(allowed=allowed, reasons=reasons, checks=checks)

    @staticmethod
    def _weekend_gate_check() -> tuple[bool, str]:
        """Block new entries during weekend risk windows.

        - Friday 20:30–23:59 UTC: markets approaching weekly close, gap risk high.
        - Sunday 22:00–22:15 UTC: first 15 minutes after weekly open, spreads wide.
        """
        now = datetime.utcnow()
        weekday = now.weekday()  # 0=Mon … 4=Fri, 5=Sat, 6=Sun
        hour, minute = now.hour, now.minute
        total_mins = hour * 60 + minute

        if weekday == 4 and total_mins >= 20 * 60 + 30:
            return False, "Friday close blackout — no new entries after 20:30 UTC"
        if weekday == 5:
            return False, "Weekend — markets closed Saturday"
        if weekday == 6 and total_mins < 22 * 60:
            return False, "Weekend — Sunday markets not yet open"
        if weekday == 6 and total_mins < 22 * 60 + 15:
            return False, "Sunday open blackout (22:00–22:15 UTC) — spread stabilising"
        return True, ""

    def check_session(self, epic: str = "") -> tuple[bool, str]:
        cfg = self._cfg
        # Weekend gap protection applies regardless of session whitelist config.
        ok, reason = self._weekend_gate_check()
        if not ok:
            return False, reason
        if not cfg.trading_hours_enabled:
            return True, ""
        whitelist: list[str] = []
        target_epic = str(epic or cfg.epic or "").strip()
        if target_epic:
            try:
                from trading.instrument_registry import InstrumentRegistry

                wl = InstrumentRegistry(cfg.as_dict()).session_whitelist_for_epic(
                    target_epic
                )
                if wl:
                    whitelist = wl
            except Exception:
                whitelist = []
        if not whitelist:
            whitelist = list(cfg.trading_session_whitelist)
        if not whitelist:
            return True, ""
        sess = session_name()
        if sess in whitelist:
            return True, ""
        return False, f"Outside allowed trading session (current={sess})"

    def check_market_hours(self, epic: str) -> tuple[bool, str]:
        cfg = self._cfg
        if not cfg.market_watch_enabled:
            return True, ""
        target = epic or cfg.epic
        from system.market_watch.japan225_session import (
            is_japan225_epic,
            is_japan225_open,
            japan225_closed_message,
        )

        if is_japan225_epic(target) and not is_japan225_open():
            return False, japan225_closed_message()
        from system.market_watch.calendar import get_market_status

        status = get_market_status(target)
        if status is None or status.open:
            return True, ""
        return False, status.message

    def check_spread(self, signal: TradeSignal) -> tuple[bool, str]:
        spread = float(signal.quote.spread)
        if spread > self._cfg.adaptive_max_entry_spread:
            return False, f"Spread {spread:.1f} > max {self._cfg.adaptive_max_entry_spread:.1f}"
        return True, ""

    def check_atr(self, signal: TradeSignal) -> tuple[bool, str]:
        min_atr = self._cfg.min_atr_points
        if min_atr <= 0:
            return True, ""
        last = signal.snapshot.get("last")
        if last is None:
            return True, ""
        try:
            atr_val = float(last.get("atr", 0) if hasattr(last, "get") else last["atr"])
        except (TypeError, ValueError, KeyError):
            atr_val = 0.0
        if atr_val < min_atr:
            return False, f"ATR {atr_val:.1f} < min {min_atr:.1f}"
        return True, ""

    @staticmethod
    def _store_count_fallback(store_has_position: Callable[[str], bool], epic: str) -> int:
        return 1 if store_has_position(epic) else 0

    def check_cooldown(self, epic: str, direction: str | None = None) -> tuple[bool, str]:
        if self.cooldown.is_active(epic, direction):
            return False, f"Cooldown ({self.cooldown.format_remaining(epic, direction)})"
        return True, ""

    def check_circuit_breaker(self) -> tuple[bool, str]:
        """Block new entries after max_consecutive_losses; auto-resume after pause at half size."""
        cfg = self._cfg
        max_losses = cfg.max_consecutive_losses
        if max_losses <= 0 or self._store is None:
            return True, ""
        try:
            consecutive = self._store.consecutive_losses(max_losses + 2)
            if consecutive < max_losses:
                self._store.clear_circuit_breaker_state()
                return True, ""

            pause_min = max(1, int(cfg.circuit_breaker_pause_minutes))
            tripped_at = self._store.get_runtime_state("circuit_breaker_tripped_at")
            now = datetime.now()
            if not tripped_at:
                self._store.set_runtime_state("circuit_breaker_tripped_at", now.isoformat(timespec="seconds"))
                return (
                    False,
                    f"Circuit breaker: {consecutive} consecutive losses "
                    f"(max {max_losses}) — pausing {pause_min}m",
                )

            try:
                started = datetime.fromisoformat(tripped_at)
            except ValueError:
                started = now
            elapsed = (now - started).total_seconds()
            remaining = max(0, pause_min * 60 - int(elapsed))
            if remaining > 0:
                mins = remaining // 60
                secs = remaining % 60
                return (
                    False,
                    f"Circuit breaker pause — resume in {mins}m {secs:02d}s "
                    f"({consecutive} consecutive losses)",
                )

            self._store.set_runtime_state("circuit_breaker_half_size", "1")
            return True, ""
        except Exception:
            pass
        return True, ""

    def attach_store(self, store: Any) -> None:
        """Attach learning store for circuit-breaker checks."""
        self._store = store
