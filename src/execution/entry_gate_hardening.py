"""Fail-closed entry hardening — spread veto + probabilistic sniper ML gate.

All unexpected exceptions in this module return (False, reason) — never allow.
Static OBI ratio boundaries are superseded by
``QuantumSniperMLCore.evaluate_entry_probability`` (P < 0.68 → chop block).
"""

from __future__ import annotations

from typing import Any


def _hub_quote(epic: str) -> Any | None:
    try:
        from system.market_data_hub import get_market_data_hub

        return get_market_data_hub().get_snapshot(str(epic or ""))
    except Exception:
        return None


def evaluate_spread_hard_veto(
    epic: str,
    *,
    cfg: Any | None = None,
    quote: Any | None = None,
) -> tuple[bool, str, float]:
    """
    Return (allowed, reason, spread_pts).

    Fail-closed: missing quote / unreadable spread → block when enabled.
    """
    try:
        block = {}
        if cfg is not None and hasattr(cfg, "get"):
            block = dict(cfg.get("feed_quality") or {})
        if not bool(block.get("enabled", True)):
            return True, "spread_gate_off", 0.0

        hard = bool(block.get("spread_hard_veto", True))
        if not hard:
            return True, "spread_veto_disabled", 0.0

        # Hot-path authority: RuntimeContext asset profile (not hardcoded 3.0).
        from system.memory_context import resolve_asset_profile

        profile = resolve_asset_profile(epic)
        max_spread = float(profile.max_spread_pts)
        if max_spread <= 0:
            return True, "spread_veto_disabled", 0.0

        q = quote if quote is not None else _hub_quote(epic)
        if q is None:
            return False, "spread_fail_closed_no_quote", 0.0

        bid = float(getattr(q, "bid", 0) or 0)
        offer = float(getattr(q, "offer", 0) or 0)
        if bid <= 0 or offer <= bid:
            return False, "spread_fail_closed_invalid_book", 0.0
        spread = offer - bid
        # Normalize FX price spread → pips via point_multiplier.
        from system.memory_context import get_runtime_context

        spread_pts = get_runtime_context().spread_points(epic, spread)
        # if current_tick.spread > system_state.active_asset.max_spread_pts: return False
        if spread_pts > max_spread:
            return (
                False,
                f"spread_hard_veto {spread_pts:.2f}>{max_spread:.2f}",
                spread_pts,
            )
        return True, "spread_ok", spread_pts
    except Exception as exc:
        return False, f"spread_fail_closed:{type(exc).__name__}", 0.0


def _obi_from_microkernel(epic: str) -> tuple[float | None, bool | None]:
    """Return (obi_ratio, order_flow_aligned) from microkernel when present."""
    try:
        from apex.microkernel import get_microkernel

        mt = get_microkernel().micro_trend_for(str(epic or ""))
        if not isinstance(mt, dict):
            return None, None
        aligned = mt.get("order_flow_aligned")
        ratio = mt.get("obi_ratio")
        if ratio is None:
            ratio = mt.get("obi")
        r = float(ratio) if ratio is not None else None
        a = bool(aligned) if aligned is not None else None
        return r, a
    except Exception:
        return None, None


def _obi_proxy_from_quote(epic: str, quote: Any | None) -> float:
    """
    Depth-free OBI proxy in [-1, 1].

    Uses short-horizon mid drift vs spread: crashing books show offer-heavy
    pressure (negative OBI) when mid is falling through a wide ask.
    """
    q = quote if quote is not None else _hub_quote(epic)
    if q is None:
        return 0.0
    try:
        bid = float(getattr(q, "bid", 0) or 0)
        offer = float(getattr(q, "offer", 0) or 0)
        if bid <= 0 or offer <= bid:
            return 0.0
        mid = (bid + offer) / 2.0
        spread = offer - bid
        # Prefer hub last mid if available
        prev = float(getattr(q, "prev_mid", 0) or getattr(q, "last_mid", 0) or 0)
        if prev <= 0:
            try:
                from system.market_data_hub import get_market_data_hub

                snap = get_market_data_hub().get_snapshot(str(epic or ""))
                hist = getattr(snap, "mid_history", None) or getattr(snap, "recent_mids", None)
                if hist and len(hist) >= 2:
                    prev = float(hist[-2])
            except Exception:
                prev = 0.0
        if prev <= 0:
            # Neutral when no history — caller may fail-closed separately
            return 0.0
        delta = mid - prev
        # Normalize by spread so one-tick drift ≈ meaningful imbalance
        raw = delta / max(spread, 1e-9)
        return max(-1.0, min(1.0, raw))
    except Exception:
        return 0.0


def resolve_raw_obi_ratio(
    epic: str,
    *,
    quote: Any | None = None,
) -> tuple[float, str]:
    """Return signed OBI ratio in [-1, 1] without entry-filter vetoes (for sovereign bypass)."""
    ratio, _aligned = _obi_from_microkernel(epic)
    source = "microkernel"
    if ratio is None or (
        source == "microkernel"
        and abs(float(ratio)) < 1e-9
        and _aligned is False
    ):
        ratio = _obi_proxy_from_quote(epic, quote)
        source = "quote_proxy"
    return float(ratio or 0.0), source


def evaluate_obi_entry_filter(
    epic: str,
    direction: str,
    *,
    cfg: Any | None = None,
    quote: Any | None = None,
) -> tuple[bool, str, float]:
    """
    Legacy OBI crash/melt-up guard retained for telemetry / tests.

    Live entry path uses ``evaluate_sniper_ml_gate`` instead of static
    OBI boundaries (see ``evaluate_entry_hardening``).
    """
    try:
        block = {}
        if cfg is not None and hasattr(cfg, "get"):
            block = dict(cfg.get("obi_filter") or {})
        try:
            from diagnostics.param_tuner import merge_cfg_section

            block = merge_cfg_section(cfg, "obi_filter") or block
        except Exception:
            pass
        if not bool(block.get("enabled", True)):
            return True, "obi_filter_off", 0.0

        min_abs = float(block.get("min_abs_ratio") or 0.22)
        try:
            from system.memory_context import resolve_asset_profile

            min_abs = max(min_abs, float(resolve_asset_profile(epic).obi_threshold))
        except Exception:
            pass
        require_align = bool(block.get("require_align", True))
        fail_closed_neutral = bool(block.get("fail_closed_on_neutral", False))
        min_tpm = float(block.get("min_tpm_confirm") or 8.0)
        dir_u = str(direction or "BUY").upper()

        ratio, aligned = _obi_from_microkernel(epic)
        source = "microkernel"
        # Yahoo / rest_poll hosts have no L2 depth → microkernel OBI stays ~0 and
        # order_flow_aligned stays False forever. Treat that as "no depth signal"
        # and fall through to the quote mid-drift proxy instead of a permanent veto.
        if ratio is None or (
            source == "microkernel"
            and abs(float(ratio)) < 1e-9
            and aligned is False
        ):
            ratio = _obi_proxy_from_quote(epic, quote)
            source = "quote_proxy"
            aligned = None

        # Depthless Mini: low-TPM quote_proxy OBI is noise — require volume confirm.
        tpm = 0.0
        try:
            from runtime.dual_core_execution import _ticks_per_minute

            tpm = float(_ticks_per_minute(str(epic or "")) or 0.0)
        except Exception:
            tpm = 0.0
        if source == "quote_proxy" and abs(float(ratio)) >= min_abs and tpm < min_tpm:
            return (
                False,
                f"obi_low_tpm_noise ratio={ratio:.3f} tpm={tpm:.0f}<{min_tpm:.0f}",
                float(ratio),
            )

        # Crash guard: never BUY when OBI strongly negative; never SELL when strongly positive
        if dir_u == "BUY" and ratio <= -min_abs:
            return False, f"obi_crash_guard ratio={ratio:.3f} src={source}", ratio
        if dir_u == "SELL" and ratio >= min_abs:
            return False, f"obi_meltup_guard ratio={ratio:.3f} src={source}", ratio

        # Microkernel explicit misalignment is a hard veto only when OBI is
        # informative (|ratio| ≥ min_abs). Depthless/neutral books must not
        # permanently freeze the hot path via require_align.
        if (
            require_align
            and aligned is False
            and abs(float(ratio)) >= min_abs
        ):
            return False, f"obi_not_aligned src={source}", float(ratio)

        # Optional strict mode: proxy must actively support the side.
        if (
            fail_closed_neutral
            and aligned is None
            and source == "quote_proxy"
        ):
            if dir_u == "BUY" and ratio < min_abs:
                return False, f"obi_proxy_not_supportive ratio={ratio:.3f}", ratio
            if dir_u == "SELL" and ratio > -min_abs:
                return False, f"obi_proxy_not_supportive ratio={ratio:.3f}", ratio

        return True, f"obi_ok ratio={ratio:.3f} src={source} tpm={tpm:.0f}", float(ratio)
    except Exception as exc:
        return False, f"obi_fail_closed:{type(exc).__name__}", 0.0


def evaluate_sniper_ml_gate(
    epic: str,
    direction: str,
    *,
    cfg: Any | None = None,
    quote: Any | None = None,
) -> tuple[bool, str, float]:
    """
    Probabilistic sniper gate — P(Success) via QuantumSniperMLCore.

    Returns (approved, reason, p_success). P < 0.68 → chop isolation block.
    Fail-closed on unexpected errors.
    """
    try:
        block: dict[str, Any] = {}
        if cfg is not None and hasattr(cfg, "get"):
            block = dict(cfg.get("sniper_ml") or {})
        if block and not bool(block.get("enabled", True)):
            return True, "sniper_ml_off", 1.0

        from alpha.micro_sniper_ml import evaluate_live_sniper_probability

        result = evaluate_live_sniper_probability(
            epic, direction, cfg=cfg, quote=quote
        )
        p = float(result.p_success)
        if not result.approved:
            return False, result.reason, p
        return True, result.reason, p
    except Exception as exc:
        # Fail-closed: treat as below threshold
        try:
            from alpha.micro_sniper_ml import SAFETY_BASELINE

            return (
                False,
                f"sniper_ml_fail_closed:{type(exc).__name__}",
                float(SAFETY_BASELINE),
            )
        except Exception:
            return False, f"sniper_ml_fail_closed:{type(exc).__name__}", 0.10


def evaluate_entry_hardening(
    epic: str,
    direction: str,
    *,
    cfg: Any | None = None,
    quote: Any | None = None,
) -> tuple[bool, str]:
    """Combined fail-closed pre-check for DualCore / strategy gates."""
    try:
        ok_s, reason_s, _ = evaluate_spread_hard_veto(epic, cfg=cfg, quote=quote)
        if not ok_s:
            return False, reason_s
        # Probabilistic sniper replaces static OBI boundaries on the live path.
        ok_ml, reason_ml, _p = evaluate_sniper_ml_gate(
            epic, direction, cfg=cfg, quote=quote
        )
        if not ok_ml:
            return False, reason_ml
        return True, "entry_hardening_ok"
    except Exception as exc:
        return False, f"entry_hardening_fail_closed:{type(exc).__name__}"
