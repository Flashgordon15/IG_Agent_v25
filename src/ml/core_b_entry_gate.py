"""Thin quality gate for DualCore Core B / pierce / micro-scalp entries.

Fail-CLOSED: feed veto, setup-memory, conviction floor, and unexpected
errors all block entry. Spread/OBI hardening is applied upstream in
passes_strategy_entry_gates via entry_gate_hardening.
"""

from __future__ import annotations

from typing import Any


def core_b_ml_allows_entry(
    epic: str,
    direction: str,
    *,
    cfg: Any | None = None,
    quote: Any | None = None,
) -> tuple[bool, str]:
    """Return (allowed, reason). Fail-closed on unexpected errors."""
    try:
        if cfg is None:
            from system.config_loader import get_config

            cfg = get_config()
        use_ml = bool(cfg.get("USE_ML_SIGNAL", True)) if hasattr(cfg, "get") else True
        if not use_ml:
            return True, "ml_disabled"

        ml_block = cfg.get("ml_veto") if hasattr(cfg, "get") else {}
        if not isinstance(ml_block, dict):
            ml_block = {}
        min_p = float(ml_block.get("min_probability") or 0.52)
        min_n = int(ml_block.get("min_labelled_rows") or 30)

        try:
            from ml.feed_quality import evaluate_feed_quality

            feed = evaluate_feed_quality(cfg, quote=quote, epic=str(epic or ""))
            if getattr(feed, "veto", False):
                return False, f"feed_quality_veto:{getattr(feed, 'reason', 'stale')}"
        except Exception as exc:
            return False, f"feed_quality_fail_closed:{type(exc).__name__}"

        epic_s = str(epic or "")
        short = epic_s.split(".")[2] if epic_s.count(".") >= 2 else epic_s
        setup_key = f"{str(direction or 'BUY').upper()}|{short}"

        try:
            from ml.setup_memory import evaluate_setup_memory

            mem = evaluate_setup_memory(cfg, setup_key)
            if getattr(mem, "veto", False):
                return False, f"setup_memory_veto:{getattr(mem, 'reason', '')}"
            trades = int(getattr(mem, "trades", 0) or 0)
            wr = float(getattr(mem, "win_rate", 0.0) or 0.0)
            if trades >= max(8, min_n // 3) and wr < min_p:
                return False, f"setup_conviction_{wr:.3f}_lt_{min_p:.2f}_n={trades}"
        except Exception as exc:
            return False, f"setup_memory_fail_closed:{type(exc).__name__}"

        try:
            from trading.ml_scorer import get_ml_scorer

            scorer = get_ml_scorer()
            if scorer is not None and scorer.is_trained():
                features = _features_for_model(scorer.feature_names, quote)
                if features is not None:
                    prob = float(scorer.predict(features))
                    if prob < min_p:
                        return False, f"model_conviction_{prob:.3f}_lt_{min_p:.2f}"
        except Exception as exc:
            return False, f"model_fail_closed:{type(exc).__name__}"

        return True, "core_b_ml_ok"
    except Exception as exc:
        return False, f"core_b_ml_fail_closed:{type(exc).__name__}"


def _features_for_model(
    feature_names: list[str], quote: Any | None
) -> dict[str, float] | None:
    if not feature_names:
        return None
    out: dict[str, float] = {}
    for name in feature_names:
        key = str(name)
        val = None
        if quote is not None:
            raw = getattr(quote, key, None)
            if raw is None and isinstance(quote, dict):
                raw = quote.get(key)
            if raw is not None:
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    val = None
        if val is None:
            return None
        out[key] = val
    return out
