"""
Live trade gate — block execution until fresh post-session signals (DEMO/LIVE).
"""

from __future__ import annotations

from datetime import datetime

from system.engine_log import log_engine


class LiveTradeGate:
    """
    Operational safeguard after DEMO/LIVE start — not a risk rule.

    Waits min_arming_ticks so warmup/historical indicator state can settle,
    then allows any actionable BUY/SELL that passed validation (no WAIT→edge
    requirement). Stacking spacing and margin-rejection pauses still apply.
    """

    def __init__(
        self,
        *,
        min_arming_ticks: int = 2,
        assessment_ticks: int = 6,
        session_start: datetime | None = None,
        stack_min_ticks: int = 1,
    ) -> None:
        self._session_start = session_start or datetime.now()
        self._min_arming_ticks = max(1, int(min_arming_ticks))
        self._assessment_ticks = max(1, int(assessment_ticks))  # legacy; unused
        self._stack_min_ticks = max(1, int(stack_min_ticks))
        self._ticks = 0
        self._armed = False
        self._last_entry_tick = 0
        self._last_block_reason = "Session arming — observing market"
        self._gate_open_count = 0
        self._margin_blocked = False
        self._margin_block_open_count = 0
        self._margin_block_tick = 0
        self._margin_cooldown_ticks = 12

    @property
    def gate_open_count(self) -> int:
        return self._gate_open_count

    @property
    def session_start(self) -> datetime:
        return self._session_start

    @property
    def last_block_reason(self) -> str:
        return self._last_block_reason

    @property
    def armed(self) -> bool:
        return self._armed

    def reset(self, session_start: datetime | None = None) -> None:
        self._session_start = session_start or datetime.now()
        self._ticks = 0
        self._armed = False
        self._last_entry_tick = 0
        self._last_block_reason = "Session arming — observing market"
        self._gate_open_count = 0
        self._margin_blocked = False
        self._margin_block_open_count = 0
        self._margin_block_tick = 0
        self._margin_cooldown_ticks = 12

    def note_broker_rejection(self, reason: str, *, open_count: int) -> None:
        """Pause stacking after IG margin rejections to avoid repeated failed orders."""
        text = str(reason or "").upper()
        if "INSUFFICIENT_FUNDS" not in text:
            return
        self._margin_blocked = True
        self._margin_block_open_count = max(0, int(open_count))
        self._margin_block_tick = self._ticks
        self._last_block_reason = (
            "IG margin insufficient — close a position or add funds before retrying"
        )

    def _allow_entry_now(self, sig: str, reason: str) -> tuple[bool, str]:
        self._last_entry_tick = self._ticks
        self._last_block_reason = reason
        self._gate_open_count += 1
        return True, reason

    def allow_execution(
        self,
        signal: str,
        quote_time: datetime | None = None,
        *,
        open_count: int = 0,
        max_positions: int = 1,
    ) -> tuple[bool, str]:
        self._ticks += 1
        sig = str(signal).upper()

        if quote_time and quote_time < self._session_start:
            self._last_block_reason = "Quote predates session start (ignored)"
            return False, self._last_block_reason

        if self._ticks < self._min_arming_ticks:
            self._last_block_reason = (
                f"Arming tick {self._ticks}/{self._min_arming_ticks} — no trades yet"
            )
            log_engine(
                f"LiveTradeGate arming: tick {self._ticks}/{self._min_arming_ticks} "
                "— not yet armed"
            )
            return False, self._last_block_reason

        if not self._armed:
            self._armed = True
            log_engine(
                f"LiveTradeGate armed after {self._ticks} ticks — ready to trade"
            )

        max_positions = max(1, int(max_positions))
        open_count = max(0, int(open_count))

        if self._margin_blocked:
            cleared = False
            if self._margin_block_open_count > 0 and open_count < self._margin_block_open_count:
                cleared = True
            elif self._ticks - self._margin_block_tick >= self._margin_cooldown_ticks:
                cleared = True
            if cleared:
                self._margin_blocked = False

        if sig not in ("BUY", "SELL"):
            self._last_block_reason = f"No actionable signal ({sig})"
            return False, self._last_block_reason

        if self._margin_blocked:
            return False, self._last_block_reason

        if (
            max_positions > 1
            and 0 < open_count < max_positions
        ):
            ticks_since_entry = self._ticks - self._last_entry_tick
            if self._last_entry_tick == 0 or ticks_since_entry >= self._stack_min_ticks:
                return self._allow_entry_now(
                    sig,
                    f"Stacking entry ({open_count + 1}/{max_positions})",
                )
            wait = self._stack_min_ticks - ticks_since_entry
            self._last_block_reason = (
                f"Stack spacing — wait {wait} tick(s) before slot {open_count + 1}/{max_positions}"
            )
            return False, self._last_block_reason

        return self._allow_entry_now(sig, "LiveTradeGate open — actionable signal")
