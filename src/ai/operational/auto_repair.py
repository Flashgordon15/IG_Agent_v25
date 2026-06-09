"""Auto-repair, dead-drop protocol, and validation anchor (§17 + §19)."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ai.paths import (
    ai_backups_dir,
    config_snapshot_backup_path,
    default_config_paths,
    operational_audit_path,
    operational_safety_freeze_path,
    strategy_proposals_path,
    telemetry_dead_drop_dir,
)
from ai.staging.envelope import (
    StagingBoundaryError,
    StagingEnvelope,
    assert_staging_may_target,
)
from system.paths import data_dir, find_python_executable, project_root


class OperationalBoundaryError(PermissionError):
    """Raised when Operational AI attempts to mutate frozen trading parameters."""


FROZEN_CONFIG_KEYS = frozenset(
    {
        "confidence_floor",
        "entry_confidence_floor",
        "risk_bands",
        "probe_risk_gbp_min",
        "probe_risk_gbp_max",
        "core_size_multiplier",
        "full_size_min_confidence",
    }
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _audit(event: str, **payload: Any) -> None:
    row = {"ts": _utc_now(), "event": event, **payload}
    path = operational_audit_path()
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class AutoRepairEngine:
    """Telemetry Dead-Drop, config backup, and Cross-Module Validation Anchor."""

    monitor: Any | None = None
    flatten_callback: Callable[[str], int] | None = None
    e2e_script: Path = field(
        default_factory=lambda: (
            project_root() / "scripts" / "e2e_platform_validation.py"
        )
    )
    _frozen: bool = False

    def is_frozen(self) -> bool:
        if self._frozen:
            return True
        path = operational_safety_freeze_path()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return bool(data.get("operational_safety_freeze"))
        except (OSError, json.JSONDecodeError):
            return False

    def clear_safety_freeze(self, *, reason: str = "bootstrap_clear") -> None:
        """Remove stale operational safety freeze once bootstrap is healthy."""
        self._frozen = False
        path = operational_safety_freeze_path()
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
        _audit("safety_freeze_cleared", reason=reason)

    def write_config_snapshot_backup(
        self,
        *,
        reason: str,
        epic: str = "",
    ) -> Path:
        """Mandatory backup before auto-repair (§17.5)."""
        backup_dir = ai_backups_dir()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        stamped = backup_dir / f"config_snapshot_backup_{ts}.json"
        target = config_snapshot_backup_path()

        config_entries: list[dict[str, Any]] = []
        for cfg_path in default_config_paths():
            config_entries.append(
                {
                    "path": str(cfg_path.relative_to(project_root())),
                    "sha256": _sha256_file(cfg_path),
                    "bytes": cfg_path.read_bytes().decode("utf-8"),
                }
            )

        dash = data_dir() / "state" / "dashboard_snapshot.json"
        payload: dict[str, Any] = {
            "ts": _utc_now(),
            "reason": reason,
            "epic": epic or None,
            "config_files": config_entries,
            "dashboard_snapshot_path": str(dash) if dash.exists() else None,
            "dashboard_snapshot_sha256": _sha256_file(dash) if dash.exists() else None,
        }
        if dash.exists():
            payload["dashboard_snapshot_bytes"] = dash.read_bytes().decode("utf-8")

        raw = json.dumps(payload, indent=2)
        stamped.write_text(raw, encoding="utf-8")
        target.write_text(raw, encoding="utf-8")
        _audit("config_snapshot_backup", path=str(target), reason=reason)
        return target

    def restore_config_snapshot_backup(self, backup_path: Path | None = None) -> bool:
        path = backup_path or config_snapshot_backup_path()
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False

        root = project_root()
        for entry in data.get("config_files") or []:
            rel = entry.get("path")
            content = entry.get("bytes")
            if not rel or content is None:
                continue
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")

        dash_bytes = data.get("dashboard_snapshot_bytes")
        dash_path = data_dir() / "state" / "dashboard_snapshot.json"
        if dash_bytes is not None:
            dash_path.parent.mkdir(parents=True, exist_ok=True)
            dash_path.write_text(dash_bytes, encoding="utf-8")

        _audit("config_snapshot_restored", path=str(path))
        return True

    def attempt_bounded_repair(
        self,
        epic: str,
        *,
        loop_error: bool,
        stream_disconnected: bool,
    ) -> dict[str, Any]:
        """Run auto-repair after mandatory backup."""
        backup = self.write_config_snapshot_backup(
            reason="pre_auto_repair",
            epic=epic,
        )
        actions: list[str] = []
        if stream_disconnected:
            actions.append("stream_resubscribe_requested")
        if loop_error:
            actions.append("loop_cache_refresh_requested")
        _audit(
            "auto_repair",
            epic=epic,
            actions=actions,
            backup=str(backup),
        )
        return {"ok": True, "backup": str(backup), "actions": actions}

    def execute_dead_drop(
        self,
        epic: str,
        *,
        reason: str,
        loop_error: bool = False,
        stream_disconnected: bool = False,
    ) -> dict[str, Any]:
        """Telemetry Dead-Drop Protocol (§17.4)."""
        if self.is_frozen():
            return {"ok": False, "detail": "already_frozen"}

        self.write_config_snapshot_backup(reason="pre_dead_drop", epic=epic)
        self.attempt_bounded_repair(
            epic,
            loop_error=loop_error,
            stream_disconnected=stream_disconnected,
        )

        closed = self._flatten_epic(epic)
        freeze = self._write_safety_freeze(epic, reason=reason)
        payload_path = self._write_dead_drop_telemetry(
            epic, reason=reason, closed=closed
        )

        _audit(
            "telemetry_dead_drop",
            epic=epic,
            reason=reason,
            closed=closed,
            freeze=str(freeze),
            telemetry=str(payload_path),
        )
        return {
            "ok": True,
            "epic": epic,
            "closed": closed,
            "safety_freeze": str(freeze),
            "telemetry": str(payload_path),
        }

    def _flatten_epic(self, epic: str) -> int:
        if self.flatten_callback is not None:
            try:
                return int(self.flatten_callback(epic))
            except Exception:
                pass
        return self._flatten_via_api()

    def _flatten_via_api(self) -> int:
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8080/api/flatten/all",
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return int(body.get("count") or 0)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
            return 0

    def _write_safety_freeze(self, epic: str, *, reason: str) -> Path:
        self._frozen = True
        path = operational_safety_freeze_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "operational_safety_freeze": True,
            "ts": _utc_now(),
            "reason": reason,
            "epic": epic,
            "exit_reason": "operational_dead_drop",
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def _write_dead_drop_telemetry(
        self, epic: str, *, reason: str, closed: int
    ) -> Path:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = telemetry_dead_drop_dir() / f"telemetry_dead_drop_{epic}_{ts}.json"
        payload = {
            "ts": _utc_now(),
            "epic": epic,
            "reason": reason,
            "positions_closed": closed,
            "operational_safety_freeze": True,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    def run_e2e_validation(self) -> dict[str, Any]:
        """Full platform validation — requires 30/30 PASS (§19)."""
        root = project_root()
        python = find_python_executable()
        env = {**dict(__import__("os").environ), "PYTHONPATH": str(root / "src")}
        proc = subprocess.run(
            [python, str(self.e2e_script)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        total_match = re.search(r"TOTAL\s+(\d+)/(\d+)", out)
        passed = int(total_match.group(1)) if total_match else 0
        total = int(total_match.group(2)) if total_match else 0
        perfect = proc.returncode == 0 and passed == 30 and total == 30
        return {
            "ok": perfect,
            "exit_code": proc.returncode,
            "passed": passed,
            "total": total,
            "perfect_30_30": perfect,
            "output_tail": out.strip().splitlines()[-8:] if out.strip() else [],
        }

    def apply_proposal_config_patch(self, proposal: dict[str, Any]) -> list[str]:
        """Sandbox-only staging apply for approved proposals (non-frozen keys only)."""
        patch = proposal.get("config_patch")
        if not isinstance(patch, dict) or not patch:
            return []

        touched: list[str] = []
        cfg_paths = default_config_paths()
        if not cfg_paths:
            return touched

        filename_keys = {p.name for p in cfg_paths}
        if any(k in filename_keys for k in patch):
            deltas = [(k, patch[k]) for k in patch if k in filename_keys]
        else:
            deltas = [(cfg_paths[0].name, patch)]

        for rel_key, delta in deltas:
            if not isinstance(delta, dict):
                continue
            for key in delta:
                if key in FROZEN_CONFIG_KEYS:
                    raise OperationalBoundaryError(
                        f"Operational AI blocked write to frozen key: {key}"
                    )
            cfg_path = next((p for p in cfg_paths if p.name == rel_key), cfg_paths[0])
            current = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(current, dict):
                merged = {**current, **delta}
                cfg_path.write_text(
                    json.dumps(merged, indent=2) + "\n", encoding="utf-8"
                )
                touched.append(str(cfg_path))
        return touched

    def process_approved_proposal(self, proposal: dict[str, Any]) -> dict[str, Any]:
        """Cross-Module Validation Anchor — approve → e2e → commit or rollback (§19)."""
        proposal_id = str(proposal.get("id") or "")
        backup = self.write_config_snapshot_backup(
            reason="pre_strategy_approval",
            epic=str(proposal.get("epic") or ""),
        )
        touched: list[str] = []
        try:
            touched = self.apply_proposal_config_patch(proposal)
        except OperationalBoundaryError as exc:
            self.restore_config_snapshot_backup(backup)
            return {
                "ok": False,
                "proposal_id": proposal_id,
                "aborted": True,
                "reason": str(exc),
            }

        e2e = self.run_e2e_validation()
        if not e2e.get("perfect_30_30"):
            self.restore_config_snapshot_backup(backup)
            for path_str in touched:
                p = Path(path_str)
                if p.exists() and backup.exists():
                    pass  # restore already rewrote from backup bytes
            _audit(
                "validation_anchor_abort",
                proposal_id=proposal_id,
                e2e=e2e,
            )
            return {
                "ok": False,
                "proposal_id": proposal_id,
                "aborted": True,
                "validation": e2e,
            }

        _audit("validation_anchor_pass", proposal_id=proposal_id, e2e=e2e)
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "validated": True,
            "validation": e2e,
            "touched": touched,
        }

    def check_approved_proposals(self) -> list[dict[str, Any]]:
        """Poll strategy_proposals.json for human-approved entries (§19)."""
        path = strategy_proposals_path()
        if not path.exists():
            return []
        try:
            store = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        proposals = store.get("proposals") or []
        results: list[dict[str, Any]] = []
        changed = False

        for item in proposals:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "")
            if status != "approved":
                continue
            if item.get("validation_pending") is False and item.get("validated_at"):
                continue

            item["validation_pending"] = True
            result = self.process_approved_proposal(item)
            changed = True
            if result.get("ok"):
                item["status"] = "validated"
                item["validated_at"] = _utc_now()
                item["validation"] = result.get("validation")
            else:
                item["status"] = "aborted_validation_failed"
                item["aborted_at"] = _utc_now()
                item["validation"] = result.get("validation")
                item["abort_reason"] = result.get("reason") or "e2e_failed"
            item["validation_pending"] = False
            results.append(result)

        if changed:
            store["proposals"] = proposals
            store["last_updated"] = _utc_now()
            path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")

        return results

    def _hot_reload_config(self) -> bool:
        """Reload in-process config dictionary after verified promotion (§21)."""
        try:
            from system.config_loader import get_config

            get_config(reload=True)
            _audit("config_hot_reload", ok=True)
            return True
        except Exception as exc:
            _audit("config_hot_reload", ok=False, error=f"{type(exc).__name__}: {exc}")
            return False

    def _validate_staged_config_bytes(self, dest: Path, content: bytes) -> None:
        """Reject §21 patches that touch frozen trading-law keys (same bar as §19)."""
        rel = str(dest.relative_to(project_root())).replace("\\", "/")
        if not rel.startswith("config/") or not rel.endswith(".json"):
            return
        try:
            data = json.loads(content.decode("utf-8"))
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return

        def _scan(block: dict[str, Any], *, scope: str) -> None:
            for key in FROZEN_CONFIG_KEYS:
                if key in block:
                    raise OperationalBoundaryError(
                        f"§21 staging blocked write to frozen key '{key}' in {scope}"
                    )

        _scan(data, scope=rel)
        instruments = data.get("instruments")
        if isinstance(instruments, dict):
            for iid, block in instruments.items():
                if isinstance(block, dict):
                    _scan(block, scope=f"{rel} instruments.{iid}")

    def apply_staged_patch_files(self, optimization_id: str) -> list[str]:
        """Copy staged patch files to production targets (barrier-checked)."""
        envelope = StagingEnvelope()
        touched: list[str] = []
        for src, dest in envelope.resolve_patch_targets(optimization_id):
            rel = dest.relative_to(project_root())
            assert_staging_may_target(str(rel).replace("\\", "/"))
            content = src.read_bytes()
            self._validate_staged_config_bytes(dest, content)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)
            touched.append(str(dest))
        return touched

    def promote_staged_optimization(self, optimization_id: str) -> dict[str, Any]:
        """§21 Verification Gateway — backup → apply → e2e → hot-reload or rollback."""
        envelope = StagingEnvelope()
        backup = self.write_config_snapshot_backup(
            reason="pre_staged_promotion",
            epic="",
        )
        touched: list[str] = []
        try:
            touched = self.apply_staged_patch_files(optimization_id)
        except (
            StagingBoundaryError,
            OperationalBoundaryError,
            FileNotFoundError,
            ValueError,
        ) as exc:
            self.restore_config_snapshot_backup(backup)
            envelope.mark_status(
                optimization_id,
                "aborted_verification_failed",
                abort_reason=str(exc),
                warning_code="aborted_verification_failed",
            )
            _audit(
                "verification_gateway_abort",
                optimization_id=optimization_id,
                reason=str(exc),
                warning_code="aborted_verification_failed",
            )
            return {
                "ok": False,
                "optimization_id": optimization_id,
                "aborted": True,
                "warning_code": "aborted_verification_failed",
                "reason": str(exc),
            }

        e2e = self.run_e2e_validation()
        if not e2e.get("perfect_30_30"):
            self.restore_config_snapshot_backup(backup)
            envelope.mark_status(
                optimization_id,
                "aborted_verification_failed",
                validation=e2e,
                warning_code="aborted_verification_failed",
            )
            _audit(
                "verification_gateway_abort",
                optimization_id=optimization_id,
                e2e=e2e,
                warning_code="aborted_verification_failed",
            )
            return {
                "ok": False,
                "optimization_id": optimization_id,
                "aborted": True,
                "warning_code": "aborted_verification_failed",
                "validation": e2e,
            }

        reloaded = self._hot_reload_config()
        envelope.mark_status(
            optimization_id,
            "validated",
            validation=e2e,
            config_hot_reload=reloaded,
        )
        _audit(
            "verification_gateway_pass",
            optimization_id=optimization_id,
            e2e=e2e,
            touched=touched,
        )
        return {
            "ok": True,
            "optimization_id": optimization_id,
            "validated": True,
            "validation": e2e,
            "touched": touched,
            "config_hot_reload": reloaded,
        }
