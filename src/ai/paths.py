"""Filesystem paths for v27 Autonomous Sentinel sandbox."""

from __future__ import annotations

from pathlib import Path

from system.paths import data_dir, data_lake_dir, project_root


def ai_backups_dir() -> Path:
    d = data_lake_dir() / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_snapshot_backup_path() -> Path:
    return ai_backups_dir() / "config_snapshot_backup.json"


def data_lake_state_dir() -> Path:
    d = data_lake_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def strategy_proposals_path() -> Path:
    return data_lake_state_dir() / "strategy_proposals.json"


def operational_safety_freeze_path() -> Path:
    return data_dir() / "state" / "operational_safety_freeze.json"


def sentinel_diagnostics_path() -> Path:
    return data_lake_state_dir() / "sentinel_diagnostics.jsonl"


def telemetry_dead_drop_dir() -> Path:
    d = data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def operational_audit_path() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "operational_ai_audit.jsonl"


def default_config_paths() -> list[Path]:
    cfg = project_root() / "config"
    paths: list[Path] = []
    for name in ("config_v25.json", "config_v26.json"):
        p = cfg / name
        if p.exists():
            paths.append(p)
    return paths


def profiler_latency_path() -> Path:
    return data_lake_state_dir() / "profiler_latency.jsonl"


def rca_diagnostics_dir() -> Path:
    d = data_lake_state_dir() / "rca_diagnostics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def staging_root() -> Path:
    d = project_root() / "src" / "ai" / "staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def staging_audit_path() -> Path:
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "staging_audit.jsonl"


# Runtime write-barrier roots (§21) — staging must never direct-write here.
STAGING_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "src/trading/",
    "src/runtime/",
)
