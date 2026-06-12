"""Capital-preserving HFT scalping execution framework."""

from execution.scalping.atomic_protect import (
    emergency_close_and_halt,
    submit_atomic_limit_entry,
    verify_protection_or_emergency,
)
from execution.scalping.config import is_scalping_enabled, scalping_settings
from execution.scalping.dynamic_spread_filter import DynamicSpreadFilter, get_spread_filter
from execution.scalping.entry_halt import (
    clear_entry_halt_for_tests,
    entry_halt_detail,
    halt_entries,
    is_entry_halted,
)
from execution.scalping.equity_circuit_breaker import (
    EquityCircuitBreaker,
    get_equity_circuit_breaker,
)

__all__ = [
    "DynamicSpreadFilter",
    "EquityCircuitBreaker",
    "clear_entry_halt_for_tests",
    "emergency_close_and_halt",
    "entry_halt_detail",
    "get_equity_circuit_breaker",
    "get_spread_filter",
    "halt_entries",
    "is_entry_halted",
    "is_scalping_enabled",
    "scalping_settings",
    "submit_atomic_limit_entry",
    "verify_protection_or_emergency",
]
