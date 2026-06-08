"""CERT tab payload — L0–L5 certification ladder."""

from __future__ import annotations

import json
import sys
from typing import Any

from system.paths import project_root


def _read_json(path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _load_certification() -> dict[str, Any]:
    root = project_root()
    v26 = root / "v26"
    if str(v26) not in sys.path:
        sys.path.insert(0, str(v26))
    try:
        from research.certification import build_certification_payload

        return build_certification_payload()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def build_cert_payload() -> dict[str, Any]:
    cert = _load_certification()
    learning_path = (
        project_root() / "data_lake" / "state" / "v26_learning_snapshot.json"
    )
    learning: dict[str, Any] = {}
    if learning_path.is_file():
        try:
            raw = json.loads(learning_path.read_text(encoding="utf-8"))
            learning = raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError):
            pass
    forward = (
        _read_json(project_root() / "data_lake" / "state" / "v26_forward_cert.json")
        or {}
    )
    gate_relax = (
        _read_json(
            project_root() / "data_lake" / "state" / "v26_gate_relaxation_report.json"
        )
        or {}
    )
    try:
        from system.gate_relaxation import relaxation_snapshot

        active_relaxation = relaxation_snapshot()
    except Exception:
        active_relaxation = {}

    l4_daily = (forward.get("l4") or {}).get("daily") or []

    return {
        **cert,
        "l1_certification": learning.get("l1_certification") or {},
        "learning_focus": learning.get("learning_focus") or [],
        "forward_cert": forward,
        "forward_daily": l4_daily,
        "gate_relaxation": {
            "active": active_relaxation,
            "report": gate_relax.get("rollup") or {},
            "recommendations": gate_relax.get("recommendations") or [],
        },
    }
