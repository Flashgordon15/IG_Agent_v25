"""CERT tab payload — L0–L5 certification ladder."""

from __future__ import annotations

import json
import sys
from typing import Any

from system.paths import project_root


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
    return {
        **cert,
        "l1_certification": learning.get("l1_certification") or {},
        "learning_focus": learning.get("learning_focus") or [],
    }
