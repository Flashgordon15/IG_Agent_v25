"""Provenance + validity for ML score and hold stamps.

Two silent corruptions motivated this module:

1. ``latest_sniper_ml_snapshot(epic=...)`` returns the last cached row *for the
   epic*, so several deals on one epic inherit an identical ``p_success``. On
   2026-07-24 that produced 26 losers stamped exactly ``0.68`` (the sniper
   threshold constant) and two Gold rows stamped ``1.10`` — not a probability.
   Downstream shadow re-scoring then "scored" a constant and reported edge.

2. Broker-attached closes are discovered by transaction sync, so open and close
   timestamps collapse and ``hold_sec`` lands at ``0.0``. That is *unmeasured*,
   not a zero-second hold, but the autopsy read it as a micro-scalp breach.

Both stamps therefore carry a source tag so analysis can exclude untrustworthy
values instead of treating them as observations.
"""

from __future__ import annotations

from typing import Any

# Threshold constants that must never be mistaken for a model output. Any stamp
# landing exactly on one of these almost certainly came from a gate default
# rather than an inference call.
KNOWN_THRESHOLD_CONSTANTS: tuple[float, ...] = (
    0.68,  # SNIPER_THRESHOLD / MICRO_SCALP_MIN_ML_P_SUCCESS / SOVEREIGN_ML_THRESHOLD
    0.72,
    0.78,
    0.82,
)

_THRESHOLD_EPS = 1e-6

# Score sources, most → least trustworthy.
ML_SOURCE_MODEL = "model"
ML_SOURCE_EXECUTION_PARAMS = "execution_params"
ML_SOURCE_EPIC_SNAPSHOT = "epic_snapshot_fallback"
ML_SOURCE_THRESHOLD_CONSTANT = "threshold_constant"
ML_SOURCE_ABSENT = "absent"

TRUSTED_ML_SOURCES = frozenset({ML_SOURCE_MODEL, ML_SOURCE_EXECUTION_PARAMS})

# Hold sources.
HOLD_SOURCE_TRACKED = "tracked"
HOLD_SOURCE_OPEN_CLOSE = "open_close_delta"
HOLD_SOURCE_SYNC_ARTIFACT = "sync_artifact"
HOLD_SOURCE_ABSENT = "absent"

TRUSTED_HOLD_SOURCES = frozenset({HOLD_SOURCE_TRACKED, HOLD_SOURCE_OPEN_CLOSE})

# Exit authority — who actually closed the position.
EXIT_AUTHORITY_AGENT = "agent_risk_stack"
EXIT_AUTHORITY_BROKER = "broker_attached_stop"
EXIT_AUTHORITY_OPERATOR = "operator"
EXIT_AUTHORITY_UNKNOWN = "unknown"

_BROKER_EXIT_TOKENS = ("broker_attached", "broker", "ig_transaction_sync", "ig_sync")
_AGENT_EXIT_TOKENS = (
    "soft_loss",
    "trail",
    "virtual_stop",
    "gbp_exit",
    "micro_gbp_exit",
    "open_position_actions",
    "long_trade",
    "long_runner",
    "ltr",
    "dynamic_limit",
    "stagnant",
    "cap_breach",
)
_OPERATOR_EXIT_TOKENS = ("operator", "manual", "flatten_all", "desk_dev")


def is_threshold_constant(value: float) -> bool:
    """True when the value sits exactly on a known gate threshold."""
    return any(abs(value - c) < _THRESHOLD_EPS for c in KNOWN_THRESHOLD_CONSTANTS)


_PLACEHOLDER_REGIMES = frozenset(
    {"", "UNKNOWN", "NONE", "NULL", "N/A", "NA", "UNSET"}
)


def is_placeholder_regime(value: Any) -> bool:
    """True when MarketRegime is blank or a non-informative placeholder."""
    if value is None:
        return True
    return str(value).strip().upper() in _PLACEHOLDER_REGIMES


def clamp_probability(value: Any) -> tuple[float | None, bool]:
    """Coerce to a probability in [0, 1].

    Returns ``(clamped, was_out_of_bounds)``. Non-numeric input yields
    ``(None, False)`` so callers can distinguish "absent" from "repaired".
    """
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None, False
    if raw != raw:  # NaN
        return None, False
    if raw < 0.0:
        return 0.0, True
    if raw > 1.0:
        # Percent-style stamps (e.g. 68.0) are a different bug to a 1.10
        # overflow; both are still clamped, both are still flagged.
        return 1.0, True
    return raw, False


def classify_ml_score(
    value: Any,
    *,
    source: str = ML_SOURCE_ABSENT,
) -> dict[str, Any]:
    """Normalise one ML entry stamp into value + provenance + trust flag."""
    clamped, out_of_bounds = clamp_probability(value)
    if clamped is None:
        return {
            "ml_score_at_entry": None,
            "ml_score_source": ML_SOURCE_ABSENT,
            "ml_score_trusted": False,
            "ml_score_out_of_bounds": False,
        }

    resolved_source = str(source or ML_SOURCE_ABSENT)
    # A value landing exactly on a gate threshold is a default, whatever the
    # caller claimed — null it so it cannot masquerade as an inference or feed
    # shadow "edge" / entry floors. Observed 2026-07-24: 26 losers stamped 0.68.
    if is_threshold_constant(clamped):
        return {
            "ml_score_at_entry": None,
            "ml_score_source": ML_SOURCE_THRESHOLD_CONSTANT,
            "ml_score_trusted": False,
            "ml_score_out_of_bounds": out_of_bounds,
            "ml_score_threshold_rejected": clamped,
        }

    trusted = resolved_source in TRUSTED_ML_SOURCES and not out_of_bounds
    return {
        "ml_score_at_entry": round(clamped, 6),
        "ml_score_source": resolved_source,
        "ml_score_trusted": trusted,
        "ml_score_out_of_bounds": out_of_bounds,
    }


def is_broker_discovered_close(*, exit_reason: str = "", engine_origin: str = "") -> bool:
    """True when the close was found via broker sync rather than observed live.

    Timestamps collapse on this path, so ``hold_sec`` lands at 0. The origin is
    what matters here, not the exit reason: a row can carry a supervised-looking
    reason like ``soft_loss`` while still having been reconciled after the fact.
    """
    haystack = f"{exit_reason or ''} {engine_origin or ''}".lower()
    return any(tok in haystack for tok in _BROKER_EXIT_TOKENS)


def classify_hold(
    hold_sec: Any,
    *,
    exit_reason: str = "",
    engine_origin: str = "",
    source: str = HOLD_SOURCE_ABSENT,
) -> dict[str, Any]:
    """Normalise a hold stamp, demoting sync artifacts to 'unmeasured'."""
    try:
        hold = None if hold_sec is None else float(hold_sec)
    except (TypeError, ValueError):
        hold = None

    if hold is None:
        return {
            "hold_sec": None,
            "hold_sec_source": HOLD_SOURCE_ABSENT,
            "hold_sec_trusted": False,
        }

    resolved_source = str(source or HOLD_SOURCE_ABSENT)

    # A zero hold on a broker-discovered close is a collapsed timestamp, not a
    # zero-second trade. Keep the number but mark it untrustworthy.
    if hold <= 0.0 and is_broker_discovered_close(
        exit_reason=exit_reason, engine_origin=engine_origin
    ):
        resolved_source = HOLD_SOURCE_SYNC_ARTIFACT

    return {
        "hold_sec": round(hold, 1),
        "hold_sec_source": resolved_source,
        "hold_sec_trusted": resolved_source in TRUSTED_HOLD_SOURCES,
    }


def classify_exit_authority(
    *,
    exit_reason: str = "",
    engine_origin: str = "",
) -> str:
    """Who closed the position — our risk stack, the broker, or an operator."""
    haystack = f"{exit_reason or ''} {engine_origin or ''}".lower()
    if not haystack.strip():
        return EXIT_AUTHORITY_UNKNOWN
    if any(tok in haystack for tok in _OPERATOR_EXIT_TOKENS):
        return EXIT_AUTHORITY_OPERATOR
    # Agent tokens win over the generic "broker" substring so an explicit
    # supervised exit is not misread when both appear.
    if any(tok in haystack for tok in _AGENT_EXIT_TOKENS):
        return EXIT_AUTHORITY_AGENT
    if any(tok in haystack for tok in _BROKER_EXIT_TOKENS):
        return EXIT_AUTHORITY_BROKER
    return EXIT_AUTHORITY_UNKNOWN


def hold_is_measurable(row: dict[str, Any]) -> bool:
    """True when a row's hold may be used as an observation."""
    if row.get("hold_sec") is None:
        return False
    source = str(row.get("hold_sec_source") or "")
    if source:
        return source in TRUSTED_HOLD_SOURCES
    # Legacy rows without provenance: a zero hold on a broker-discovered close
    # is the known artifact; anything else is taken at face value.
    try:
        hold = float(row["hold_sec"])
    except (TypeError, ValueError):
        return False
    return not (
        hold <= 0.0
        and is_broker_discovered_close(
            exit_reason=str(row.get("exit_reason") or ""),
            engine_origin=str(row.get("engine_origin") or ""),
        )
    )


def ml_score_is_usable(row: dict[str, Any]) -> bool:
    """True when a row's ML stamp may be used for edge / calibration claims."""
    if row.get("ml_score_at_entry") is None:
        return False
    source = str(row.get("ml_score_source") or "")
    if source:
        return source in TRUSTED_ML_SOURCES
    # Legacy rows: reject threshold constants and out-of-range values.
    clamped, out_of_bounds = clamp_probability(row.get("ml_score_at_entry"))
    if clamped is None or out_of_bounds:
        return False
    return not is_threshold_constant(clamped)
