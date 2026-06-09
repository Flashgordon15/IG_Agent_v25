"""Secure staging sandbox with runtime write-barrier (§21)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ai.paths import STAGING_FORBIDDEN_PREFIXES, staging_audit_path, staging_root
from system.paths import project_root


class StagingBoundaryError(PermissionError):
    """Raised when staging attempts a forbidden production write."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _audit(event: str, **payload: Any) -> None:
    row = {"ts": _utc_now(), "event": event, **payload}
    path = staging_audit_path()
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def normalize_rel_path(rel: str) -> str:
    return str(rel or "").replace("\\", "/").lstrip("/")


def assert_staging_may_target(rel_path: str) -> None:
    """Enforce write-barrier: never overwrite src/trading/ or src/runtime/ at runtime."""
    rel = normalize_rel_path(rel_path)
    for prefix in STAGING_FORBIDDEN_PREFIXES:
        if rel.startswith(prefix) or rel == prefix.rstrip("/"):
            raise StagingBoundaryError(
                f"staging write-barrier: direct runtime write to '{rel}' forbidden"
            )


class StagingEnvelope:
    """Discrete optimization_id workspaces under src/ai/staging/."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or staging_root()

    def optimization_dir(self, optimization_id: str) -> Path:
        safe = "".join(c for c in str(optimization_id) if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError("optimization_id required")
        d = self.root / safe
        d.mkdir(parents=True, exist_ok=True)
        (d / "patch").mkdir(parents=True, exist_ok=True)
        return d

    def create_manifest(
        self,
        optimization_id: str,
        *,
        patches: list[dict[str, str]],
        evidence: dict[str, Any] | None = None,
    ) -> Path:
        opt_dir = self.optimization_dir(optimization_id)
        for item in patches:
            assert_staging_may_target(item["relative_target"])
        manifest = {
            "optimization_id": optimization_id,
            "status": "staged",
            "created_at": _utc_now(),
            "patches": patches,
            "evidence": evidence or {},
        }
        path = opt_dir / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        _audit("staging_manifest_created", optimization_id=optimization_id)
        return path

    def load_manifest(self, optimization_id: str) -> dict[str, Any]:
        path = self.optimization_dir(optimization_id) / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"manifest missing for {optimization_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("invalid manifest")
        return data

    def resolve_patch_targets(self, optimization_id: str) -> list[tuple[Path, Path]]:
        """Return (patch_source, production_target) pairs for promotion."""
        manifest = self.load_manifest(optimization_id)
        opt_dir = self.optimization_dir(optimization_id)
        root = project_root()
        pairs: list[tuple[Path, Path]] = []
        for item in manifest.get("patches") or []:
            rel = normalize_rel_path(str(item.get("relative_target") or ""))
            patch_file = str(item.get("patch_file") or "")
            if not rel or not patch_file:
                continue
            assert_staging_may_target(rel)
            src = opt_dir / patch_file
            dest = root / rel
            if not src.exists():
                raise FileNotFoundError(f"patch file missing: {src}")
            pairs.append((src, dest))
        return pairs

    def mark_status(self, optimization_id: str, status: str, **extra: Any) -> None:
        manifest = self.load_manifest(optimization_id)
        manifest["status"] = status
        manifest["updated_at"] = _utc_now()
        manifest.update(extra)
        path = self.optimization_dir(optimization_id) / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
