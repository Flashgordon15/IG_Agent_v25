"""
Version metadata and config rollback support.

Reads/writes ``version.json`` at project root.
Backups stored under ``backups/{timestamp}/``.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from system.paths import config_dir, project_root


def _version_path() -> Path:
    return project_root() / "version.json"


def current_version() -> dict[str, Any]:
    """
    Load version.json.

    Expected keys: ``version``, ``build``, ``history``.
    """
    path = _version_path()
    if not path.exists():
        return {"version": "unknown", "build": "0", "history": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_version(data: dict[str, Any]) -> None:
    """Write version.json."""
    path = _version_path()
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def backup_config(label: str | None = None) -> Path:
    """
    Snapshot config and credentials before upgrade.

    :returns: Path to backup directory.
    """
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = label or stamp
    dest = project_root() / "backups" / name
    dest.mkdir(parents=True, exist_ok=True)

    cfg_src = config_dir()
    for rel in ("config_v24.json", "credentials/credentials.json"):
        src = cfg_src / rel
        if src.exists():
            out = dest / Path(rel).name
            shutil.copy2(src, out)

    meta = current_version()
    meta.setdefault("backups", []).append(
        {"label": name, "created_at": datetime.now().isoformat(), "path": str(dest)}
    )
    save_version(meta)
    return dest


def list_backups() -> list[dict[str, str]]:
    """Return backup history entries from version.json."""
    meta = current_version()
    entries = meta.get("backups") or []
    root = project_root() / "backups"
    if not entries and root.exists():
        return [
            {"label": p.name, "path": str(p)}
            for p in sorted(root.iterdir(), reverse=True)
            if p.is_dir()
        ]
    return entries


def rollback(label: str) -> bool:
    """
    Restore config from labeled backup.

    :returns: True if restore succeeded.
    """
    root = project_root() / "backups" / label
    if not root.is_dir():
        return False
    cfg = config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    for name in ("config_v24.json", "credentials.json"):
        src = root / name
        if not src.exists():
            continue
        if name == "credentials.json":
            dst = cfg / "credentials" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
        else:
            dst = cfg / name
        shutil.copy2(src, dst)
    return True
