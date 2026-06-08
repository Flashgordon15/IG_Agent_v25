"""Shared v26 certification windows — max 14d soak (config_v26.json)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_cert_config() -> dict[str, Any]:
    """Certification thresholds; defaults to 14-day max soak on £10k account."""
    cfg = _read_v26_config()
    cert = dict(cfg.get("certification") or {})
    milestones = cfg.get("milestones") or {}
    targets = milestones.get("targets") or {}
    prove = int(milestones.get("prove_days") or cert.get("max_soak_days") or 14)

    cert.setdefault("max_soak_days", prove)
    cert.setdefault("l1_window_days", prove)
    cert.setdefault("l1_min_days", prove)
    cert.setdefault("l1_median_daily_gbp", float(targets.get("M1") or 100))
    cert.setdefault("l1_min_pct_stretch_days", 0.21)
    cert.setdefault("l1_stretch_day_gbp", float(targets.get("M2") or 250))
    cert.setdefault("l1_max_drawdown_gbp", 500.0)
    cert.setdefault("l1_ohlc_wr_min", 0.52)
    cert.setdefault("l4_window_days", prove)
    cert.setdefault("l5_window_days", prove)
    return cert


def max_soak_days() -> int:
    return int(load_cert_config().get("max_soak_days") or 14)


def _read_v26_config() -> dict[str, Any]:
    path = _project_root() / "config" / "config_v26.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}
