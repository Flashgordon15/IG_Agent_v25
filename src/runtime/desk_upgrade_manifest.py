"""Desk upgrade manifest — splash changelog + optional deploy append."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.paths import data_dir, project_root

_SEED = project_root() / "src" / "data" / "v31-production" / "state" / "desk_upgrade_manifest.json"


def manifest_path() -> Path:
    return data_dir() / "state" / "desk_upgrade_manifest.json"


def _ensure_seeded() -> Path:
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        return path
    if _SEED.is_file() and _SEED.resolve() != path.resolve():
        try:
            path.write_text(_SEED.read_text(encoding="utf-8"), encoding="utf-8")
            return path
        except OSError:
            pass
    # Minimal fallback
    payload = {
        "schema_version": 1,
        "product": "IG Agent Trading Desk",
        "version": "31.1.0",
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "upgrades": [],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_upgrade_manifest(*, limit: int = 20) -> dict[str, Any]:
    """Return manifest dict with upgrades newest-first (capped)."""
    path = _ensure_seeded()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    upgrades = list(raw.get("upgrades") or [])
    # Prefer explicit date desc
    def _key(u: Any) -> str:
        if not isinstance(u, dict):
            return ""
        return str(u.get("date") or "")

    upgrades = [u for u in upgrades if isinstance(u, dict)]
    upgrades.sort(key=_key, reverse=True)
    out = dict(raw)
    out["upgrades"] = upgrades[: max(1, int(limit))]
    out["path"] = str(path)
    return out


def upgrades_live(limit: int = 12) -> list[dict[str, Any]]:
    man = load_upgrade_manifest(limit=limit)
    return list(man.get("upgrades") or [])


def append_upgrade(
    *,
    upgrade_id: str,
    title: str,
    detail: str = "",
    date: str | None = None,
) -> Path:
    """Append (or upsert by id) an upgrade entry — deploy-time helper."""
    path = _ensure_seeded()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raw = {"schema_version": 1, "upgrades": []}
    if not isinstance(raw, dict):
        raw = {"schema_version": 1, "upgrades": []}
    upgrades = [u for u in (raw.get("upgrades") or []) if isinstance(u, dict)]
    day = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = {
        "id": str(upgrade_id).strip(),
        "date": day,
        "title": str(title).strip(),
        "detail": str(detail or "").strip(),
        "ts": time.time(),
    }
    upgrades = [u for u in upgrades if str(u.get("id") or "") != entry["id"]]
    upgrades.insert(0, entry)
    raw["upgrades"] = upgrades[:40]
    raw["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return path
