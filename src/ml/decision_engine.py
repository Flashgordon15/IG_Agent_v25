"""
Unified ML confidence blending for TradingLoop.

Layers (in order):
  1. Feed quality — stale/wide-spread quote penalty or veto
  2. Setup memory — penalise/veto chronic losing setups from ML history
  3. ML filter overrides — synthetic-replay bounds (progressive ramp)
  4. Interim scorer / XGBoost model blend
  5. Profit policy — marginal ML veto, session hot/cold adjustment
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from system.config import Config
from system.engine_log import log_engine


@dataclass
class MLDecisionResult:
    confidence: float
    rules_confidence: float
    ml_prob: float | None = None
    mode: str = "rules"
    blended: bool = False
    interim_active: bool = False
    setup_veto: bool = False
    feed_veto: bool = False
    setup_penalty: float = 0.0
    notes: str = ""
    log_entry: dict[str, Any] = field(default_factory=dict)


_ML_CONVICTION = 0.15
_RULES_WEIGHT = 0.6
_ML_WEIGHT = 0.4


def blend_ml_confidence(
    *,
    cfg: Config,
    market: str,
    direction: str,
    snapshot: dict[str, Any],
    store: Any | None,
    rules_conf: float,
    setup_key: str = "",
    quote: Any | None = None,
    epic: str = "",
) -> MLDecisionResult:
    """Resolve final entry confidence from rules + ML layers."""
    conf = float(rules_conf)
    ml_prob: float | None = None
    mode = "rules"
    blended = False
    interim_active = False
    notes_parts: list[str] = []

    if not bool(cfg.get("USE_ML_SIGNAL", False)):
        return MLDecisionResult(
            confidence=conf,
            rules_confidence=rules_conf,
            mode="disabled",
            notes="USE_ML_SIGNAL=false",
        )

    # --- Feed quality ---
    try:
        from ml.feed_quality import evaluate_feed_quality

        feed = evaluate_feed_quality(cfg, quote=quote, epic=epic)
        if feed.veto:
            return MLDecisionResult(
                confidence=0.0,
                rules_confidence=rules_conf,
                mode="feed_veto",
                feed_veto=True,
                notes=feed.reason,
                log_entry={"feed_quality": feed.__dict__, "veto": True},
            )
        if feed.penalty_pts > 0:
            conf = max(0.0, conf - feed.penalty_pts)
            notes_parts.append(f"feed_penalty=-{feed.penalty_pts:.0f}")
    except Exception as exc:
        log_engine(f"ml decision feed_quality skipped: {type(exc).__name__}: {exc}")

    # --- Setup memory gate ---
    try:
        from ml.setup_memory import evaluate_setup_memory

        mem = evaluate_setup_memory(cfg, setup_key)
        if mem.veto:
            return MLDecisionResult(
                confidence=0.0,
                rules_confidence=rules_conf,
                mode="setup_veto",
                setup_veto=True,
                setup_penalty=mem.penalty_pts,
                notes=mem.reason,
                log_entry={
                    "setup_memory": mem.__dict__,
                    "veto": True,
                },
            )
        if mem.penalty_pts > 0:
            conf = max(0.0, conf - mem.penalty_pts)
            notes_parts.append(f"setup_penalty=-{mem.penalty_pts:.0f}")
    except Exception as exc:
        log_engine(f"ml decision setup_memory skipped: {type(exc).__name__}: {exc}")

    rules_after_gates = conf

    # --- Synthetic replay filter bounds ---
    try:
        snap = snapshot or {}
        last = snap.get("last")
        _last = last if (last is not None and hasattr(last, "get")) else {}
        _atr = float(_last.get("atr", 0) or 0)
        _stop = max(1.0, float(cfg.stop_distance_points))
        from system.ml_filter_overrides import evaluate_filter_block

        blocked, reason = evaluate_filter_block(
            adjusted_score=rules_after_gates,
            raw_score=float(snap.get("raw_confidence", rules_after_gates)),
            rsi=float(_last.get("rsi", 0) or 0),
            atr_ratio=_atr / _stop,
        )
        if blocked:
            return MLDecisionResult(
                confidence=0.0,
                rules_confidence=rules_conf,
                mode="filter_veto",
                notes=reason,
                log_entry={"filter_block": reason},
            )
    except Exception as exc:
        log_engine(f"ml decision filter_overrides skipped: {type(exc).__name__}: {exc}")

    try:
        from ml.interim_scorer import (
            get_interim_scorer,
            ml_clean_training_rows,
            should_use_interim_scorer,
        )
        from trading.ml_scorer import get_ml_scorer

        scorer = get_ml_scorer()
        _ml_records = ml_clean_training_rows(cfg)
        snap = snapshot or {}

        if should_use_interim_scorer(cfg):
            interim_active = True
            mode = "interim"
            interim = get_interim_scorer().score(
                cfg=cfg,
                market=market,
                direction=direction,
                snapshot=snap,
                store=store,
            )
            interim_total = float(interim.total)
            conf = (rules_after_gates * 0.45) + (interim_total * 0.55)
            conf = max(0.0, min(100.0, conf))
            ml_prob = conf / 100.0
            blended = True
            notes_parts.append(
                f"interim={interim_total:.0f} rules={rules_after_gates:.1f}"
            )
            log_engine(
                f"[ML DECISION] interim blend conf={conf:.1f} ({interim.notes})"
            )
        elif scorer.is_trained():
            mode = "model"
            last = snap.get("last")
            _last = last if (last is not None and hasattr(last, "get")) else {}
            _atr = float(_last.get("atr", 0) or 0)
            _stop = max(1.0, float(cfg.stop_distance_points))
            features = {
                "adjusted_score": rules_after_gates,
                "raw_score": float(snap.get("raw_confidence", rules_after_gates)),
                "rsi": float(_last.get("rsi", 0) or 0),
                "atr_ratio": _atr / _stop,
            }
            for opt_name, opt_val in (
                ("profit_tier_pct", 0.0),
                ("session_slot_idx", None),
            ):
                if opt_name in scorer.feature_names:
                    if opt_name == "session_slot_idx" and opt_val is None:
                        try:
                            import time as _time

                            from runtime.intraday_slot_tracker import slot_id_for_timestamp
                            from ml.auto_trainer import _session_slot_idx

                            slot_id = slot_id_for_timestamp(_time.time(), cfg)
                            opt_val = _session_slot_idx(slot_id) or 0.0
                        except Exception:
                            opt_val = 0.0
                    features[opt_name] = float(opt_val or 0.0)
            if all(f in features for f in scorer.feature_names):
                ml_prob = scorer.score(
                    features, use_ml_signal=True, timeout_s=0.5
                )
                if ml_prob > 0.0:
                    if abs(ml_prob - 0.5) >= _ML_CONVICTION:
                        conf = (rules_after_gates * _RULES_WEIGHT) + (
                            ml_prob * 100.0 * _ML_WEIGHT
                        )
                        conf = max(0.0, min(100.0, conf))
                        blended = True
                        notes_parts.append(
                            f"model_p={ml_prob:.3f} rules={rules_after_gates:.1f}"
                        )
                        log_engine(
                            f"[ML DECISION] model blend ml={ml_prob:.3f} "
                            f"rules={rules_after_gates:.1f} → {conf:.1f}"
                        )
                    else:
                        conf = rules_after_gates
                        notes_parts.append(
                            f"model_near50 p={ml_prob:.3f} rules_kept"
                        )
            else:
                notes_parts.append("model_feature_gap")
        else:
            notes_parts.append(
                f"rules_only records={_ml_records} trained={scorer.is_trained()}"
            )
            conf = rules_after_gates
    except Exception as exc:
        log_engine(f"ml decision blend failed: {type(exc).__name__}: {exc}")
        conf = rules_after_gates

    # --- Profit philosophy (marginal ML veto, session WR adj) ---
    try:
        from ml.profit_policy import apply_profit_policy

        pol = apply_profit_policy(cfg, conf, ml_prob=ml_prob, store=store)
        if pol.veto:
            return MLDecisionResult(
                confidence=0.0,
                rules_confidence=rules_conf,
                ml_prob=ml_prob,
                mode="profit_veto",
                notes=pol.reason,
                log_entry={"profit_policy": pol.__dict__},
            )
        conf = float(pol.confidence)
        if pol.boost_pts or pol.penalty_pts:
            notes_parts.append(pol.reason)
    except Exception as exc:
        log_engine(f"ml decision profit_policy skipped: {type(exc).__name__}: {exc}")

    log_entry = {
        "ts": datetime.now().strftime("%H:%M:%S"),
        "market": market,
        "direction": direction,
        "ml_prob": round(float(ml_prob), 3) if ml_prob is not None else None,
        "rules_conf": round(rules_conf, 1),
        "confidence": round(conf, 1),
        "blended": blended,
        "mode": mode,
        "setup": setup_key,
        "blend_note": " | ".join(notes_parts) if notes_parts else mode,
    }

    return MLDecisionResult(
        confidence=conf,
        rules_confidence=rules_conf,
        ml_prob=ml_prob,
        mode=mode,
        blended=blended,
        interim_active=interim_active,
        setup_penalty=rules_conf - rules_after_gates if rules_conf != rules_after_gates else 0.0,
        notes=" | ".join(notes_parts),
        log_entry=log_entry,
    )
