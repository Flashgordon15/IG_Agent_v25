"""Non-blocking Parameter Instrumentation Harness.

Reads fill-rate telemetry, computes optimized soft thresholds, and atomically
writes them into ``config/tuning_overlay.json`` for real-time absorption by
entry gates — no process restart, no Lightstreamer-lane HTTP/blocking I/O.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from system.engine_log import log_engine

# Soft-param defaults (never touch iron-cage risk limits)
DEFAULT_SPREAD_PCT = 0.0002
TIGHT_SPREAD_PCT = 0.00015
DEFAULT_OBI_ABS = 0.22
RELAXED_OBI_ABS = 0.30
DEFAULT_RR = 2.75
ELEVATED_RR = 2.75  # was 3.5 — slip-aware; 3.5×TP/1.0×SL broken under 0.5×spread IOC
FILL_TIGHTEN_THRESHOLD = 0.75
FILL_RELAX_OBI_THRESHOLD = 0.50
ATR_TOP_PERCENTILE = 0.80
_ATR_RING_MAX = 120
_OVERLAY_CACHE_TTL = 0.5

_IRON_CAGE_FORBIDDEN = frozenset(
    {
        "max_daily_loss_gbp",
        "max_daily_risk_loss",
        "rest_api_budget",
        "rest_budget",
        "max_rest_calls_per_min",
        "trade_ready",
        "iron_cage_override",
    }
)

_lock = threading.RLock()
_atr_samples: deque[float] = deque(maxlen=_ATR_RING_MAX)
_last_bias: str | None = None
_overlay_cache: dict[str, Any] = {}
_overlay_mtime: float = -1.0
_overlay_checked_at: float = 0.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def overlay_path() -> Path:
    env = (os.environ.get("IG_TUNING_OVERLAY") or "").strip()
    if env:
        return Path(env)
    return _repo_root() / "config" / "tuning_overlay.json"


def reset_param_tuner_for_tests() -> None:
    global _last_bias, _overlay_cache, _overlay_mtime, _overlay_checked_at
    with _lock:
        _atr_samples.clear()
        _last_bias = None
        _overlay_cache = {}
        _overlay_mtime = -1.0
        _overlay_checked_at = 0.0


def observe_atr_sample(atr: float) -> None:
    """Push a 1h ATR print into the percentile ring (hot-path safe)."""
    try:
        v = float(atr)
    except (TypeError, ValueError):
        return
    if v <= 0:
        return
    with _lock:
        _atr_samples.append(v)


def atr_percentile_rank(atr: float | None = None) -> float | None:
    """
    Empirical percentile rank of ``atr`` (or latest sample) in the 1h ring.

    Returns None until the ring has enough samples (≥20).
    """
    with _lock:
        samples = list(_atr_samples)
    if len(samples) < 20:
        return None
    if atr is None:
        probe = samples[-1]
    else:
        try:
            probe = float(atr)
        except (TypeError, ValueError):
            return None
    if probe <= 0:
        return None
    below = sum(1 for s in samples if s <= probe)
    return below / float(len(samples))


def _read_overlay_raw(path: Path | None = None) -> dict[str, Any]:
    p = path or overlay_path()
    if not p.is_file():
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_overlay_cached(*, force: bool = False) -> dict[str, Any]:
    """1s-ish mtime cache — safe for gate hot-path absorption."""
    global _overlay_cache, _overlay_mtime, _overlay_checked_at
    now = time.time()
    path = overlay_path()
    with _lock:
        if (
            not force
            and _overlay_cache
            and (now - _overlay_checked_at) < _OVERLAY_CACHE_TTL
        ):
            return dict(_overlay_cache)
        try:
            mtime = path.stat().st_mtime if path.is_file() else -1.0
        except OSError:
            mtime = -1.0
        if not force and mtime == _overlay_mtime and _overlay_cache:
            _overlay_checked_at = now
            return dict(_overlay_cache)
        body = _read_overlay_raw(path)
        _overlay_cache = body
        _overlay_mtime = mtime
        _overlay_checked_at = now
        return dict(body)


def hot_section(name: str) -> dict[str, Any]:
    """Return a nested overlay section for gate merge (empty dict if absent)."""
    body = load_overlay_cached()
    sec = body.get(name)
    return dict(sec) if isinstance(sec, dict) else {}


def merge_cfg_section(cfg: Any | None, name: str) -> dict[str, Any]:
    """Merge in-memory cfg section with hot overlay (overlay wins on keys)."""
    base: dict[str, Any] = {}
    if cfg is not None and hasattr(cfg, "get"):
        try:
            raw = cfg.get(name) or {}
            if isinstance(raw, dict):
                base = dict(raw)
        except Exception:
            base = {}
    overlay = hot_section(name)
    if overlay:
        base.update(overlay)
    return base


def _current_grok_bias() -> str:
    try:
        from execution.grok_macro_bias import resolve_grok_macro_bias

        return str(resolve_grok_macro_bias() or "NEUTRAL").upper()
    except Exception:
        body = load_overlay_cached()
        return str(body.get("grok_macro_bias") or "NEUTRAL").upper()


def _peek_live_atr() -> float | None:
    """Non-blocking hub ATR peek for DOW / primary epic."""
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        for epic in ("IX.D.DOW.IFM.IP", "IX.D.NIKKEI.IFM.IP"):
            snap = hub.get_snapshot(epic)
            if snap is None:
                continue
            for attr in ("atr", "atr_14", "live_atr"):
                v = getattr(snap, attr, None)
                if v is not None and float(v) > 0:
                    return float(v)
    except Exception:
        return None
    return None


def compute_instrumentation(
    *,
    fill_rate_15m: float | None = None,
    grok_bias: str | None = None,
    atr: float | None = None,
    previous_bias: str | None = None,
) -> dict[str, Any]:
    """
    Pure math — no disk I/O. Returns nested sections + metadata.
    """
    if fill_rate_15m is None:
        try:
            from diagnostics.fill_rate_monitor import get_fill_rate_monitor

            fill_rate_15m = get_fill_rate_monitor().rolling_fill_rate_15m()
            # Fall back to short rolling window when 15m cold
            if fill_rate_15m is None:
                fill_rate_15m = get_fill_rate_monitor().rolling_fill_rate(20)
        except Exception:
            fill_rate_15m = None

    bias = str(grok_bias or _current_grok_bias() or "NEUTRAL").upper()
    prev = previous_bias if previous_bias is not None else _last_bias

    spread_pct = DEFAULT_SPREAD_PCT
    obi_abs = DEFAULT_OBI_ABS
    rr = DEFAULT_RR
    reasons: list[str] = []

    if fill_rate_15m is not None and fill_rate_15m > FILL_TIGHTEN_THRESHOLD:
        spread_pct = TIGHT_SPREAD_PCT
        reasons.append(f"fill_rate_15m={fill_rate_15m:.2f}>0.75→spread_cap=0.015%")
    else:
        reasons.append(f"spread_cap_default={DEFAULT_SPREAD_PCT}")

    if fill_rate_15m is not None and fill_rate_15m < FILL_RELAX_OBI_THRESHOLD:
        obi_abs = RELAXED_OBI_ABS
        reasons.append(f"fill_rate_15m={fill_rate_15m:.2f}<0.50→obi_abs=0.25")
    else:
        reasons.append(f"obi_abs_default={DEFAULT_OBI_ABS}")

    # Asymmetric target scaler on bias exit from VETO
    transitioned = (
        prev is not None
        and str(prev).upper() == "VETO"
        and bias in ("NEUTRAL", "BULL", "BEAR")
    )
    live_atr = atr
    if live_atr is None:
        live_atr = _peek_live_atr()
    if live_atr is not None:
        observe_atr_sample(live_atr)
    pct = atr_percentile_rank(live_atr)
    if transitioned and pct is not None and pct >= ATR_TOP_PERCENTILE:
        rr = ELEVATED_RR
        reasons.append(
            f"bias {prev}→{bias} atr_pct={pct:.2f}≥0.80→rr={ELEVATED_RR} (SL=1.0x)"
        )
    elif transitioned:
        reasons.append(f"bias {prev}→{bias} atr_pct={pct} — rr stays {DEFAULT_RR}")

    return {
        "pre_entry_regime_veto": {"max_spread_pct": float(spread_pct)},
        "obi_filter": {"min_abs_ratio": float(obi_abs)},
        "volatility_bracket": {
            "elevated_vol_reward_risk": float(rr),
            # SL rigidly 1.0x risk unit — documented for adaptive bracket consumers
            "stop_risk_multiple": 1.0,
        },
        "instrumentation": {
            "fill_rate_15m": fill_rate_15m,
            "grok_macro_bias": bias,
            "previous_bias": prev,
            "atr": live_atr,
            "atr_percentile": pct,
            "reward_risk": rr,
            "reasons": reasons,
            "updated_at": time.time(),
            "target_daily_profit_gbp": 1000.0,
            "target_win_rate": 0.60,
        },
    }


def write_instrumentation_overlay(
    sections: dict[str, Any],
    *,
    path: Path | None = None,
) -> dict[str, Any]:
    """
    Atomically merge instrumentation sections into tuning_overlay.json.

    Preserves regime_matrix / params / grok_macro_bias. Never writes iron-cage keys.
    """
    target = path or overlay_path()
    with _lock:
        body = _read_overlay_raw(target)
        for key, val in sections.items():
            if key in _IRON_CAGE_FORBIDDEN:
                continue
            if key in ("pre_entry_regime_veto", "obi_filter", "volatility_bracket") and isinstance(
                val, dict
            ):
                existing = body.get(key) if isinstance(body.get(key), dict) else {}
                merged = dict(existing)
                for k, v in val.items():
                    if k in _IRON_CAGE_FORBIDDEN:
                        continue
                    merged[k] = v
                body[key] = merged
            elif key == "instrumentation" and isinstance(val, dict):
                body[key] = dict(val)
            else:
                # Do not clobber unrelated top-level keys unless instrumentation meta
                if key.startswith("_") or key == "instrumentation":
                    body[key] = val
        body["param_tuner_updated_at"] = time.time()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        # Bust cache
        global _overlay_cache, _overlay_mtime, _overlay_checked_at
        _overlay_cache = dict(body)
        try:
            _overlay_mtime = target.stat().st_mtime
        except OSError:
            _overlay_mtime = time.time()
        _overlay_checked_at = time.time()
        return {"ok": True, "path": str(target), "sections": list(sections.keys())}


def run_instrumentation_cycle(
    *,
    path: Path | None = None,
    fill_rate_15m: float | None = None,
    grok_bias: str | None = None,
    atr: float | None = None,
) -> dict[str, Any]:
    """
    Full non-blocking cycle: compute → atomic write → update bias memory.

    Safe to call from a daemon / timer — never invoked on the raw tick lane.
    """
    global _last_bias
    with _lock:
        prev = _last_bias
    sections = compute_instrumentation(
        fill_rate_15m=fill_rate_15m,
        grok_bias=grok_bias,
        atr=atr,
        previous_bias=prev,
    )
    meta = sections.get("instrumentation") or {}
    bias = str(meta.get("grok_macro_bias") or "NEUTRAL")
    result = write_instrumentation_overlay(sections, path=path)
    with _lock:
        _last_bias = bias
    try:
        log_engine(
            f"ParamTuner: overlay updated fill15m={meta.get('fill_rate_15m')} "
            f"spread={sections['pre_entry_regime_veto'].get('max_spread_pct')} "
            f"obi={sections['obi_filter'].get('min_abs_ratio')} "
            f"rr={sections['volatility_bracket'].get('elevated_vol_reward_risk')} "
            f"bias={bias}"
        )
    except Exception:
        pass
    result["instrumentation"] = meta
    result["computed"] = {
        "max_spread_pct": sections["pre_entry_regime_veto"]["max_spread_pct"],
        "min_abs_ratio": sections["obi_filter"]["min_abs_ratio"],
        "elevated_vol_reward_risk": sections["volatility_bracket"][
            "elevated_vol_reward_risk"
        ],
    }
    return result
