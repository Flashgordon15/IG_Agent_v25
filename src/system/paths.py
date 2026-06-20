"""Project root and path resolution — macOS .app bundle aware."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_APEX_V30_SUPPORT = Path("Library") / "Application Support" / "IG Agent Apex" / "v30-production"


def is_v30_monolith() -> bool:
    """True when the active runtime is the v30 Apex monolith (not legacy v25/v29 data plane)."""
    try:
        from system.app_identity import APP_VERSION

        return str(APP_VERSION).startswith("30.")
    except Exception:
        return False


def apex_isolated_root() -> Path:
    """
    v30 Apex isolated data namespace — never shares legacy src/data blocks.

    macOS: ~/Library/Application Support/IG Agent Apex/v30-production/
    """
    if sys.platform == "darwin":
        root = Path.home() / _APEX_V30_SUPPORT
    else:
        root = Path.home() / ".ig-agent-apex" / "v30-production"
    root.mkdir(parents=True, exist_ok=True)
    return root


def legacy_data_roots() -> tuple[Path, ...]:
    """Writable legacy paths that v30 must never read or write."""
    root = project_root()
    return (
        root / "src" / "data",
        root / "src" / "analytics",
        root / "data_lake",
    )


def _use_apex_isolated_store() -> bool:
    if os.environ.get("IG_AGENT_LEGACY_DATA", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    if os.environ.get("IG_AGENT_DATA_DIR", "").strip():
        return False
    if is_v30_monolith():
        return True
    if os.environ.get("IG_APEX_DESKTOP") == "1":
        return True
    profile = os.environ.get("IG_NODE_PROFILE", "").strip().lower()
    if profile in ("shadow", "v30", "sandbox"):
        return True
    node_env = os.environ.get("NODE_ENV", "").strip().lower()
    return node_env in ("shadow", "v30", "sandbox", "development")


def is_legacy_data_path(path: Path | str) -> bool:
    """Return True if ``path`` resolves under a forbidden legacy data root."""
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    for legacy in legacy_data_roots():
        try:
            legacy.resolve()
            resolved.relative_to(legacy.resolve())
            return True
        except ValueError:
            continue
    return False


def coerce_writable_path(path: Path | str, *, subdir: str = "state") -> Path:
    """
    Redirect legacy v25/v29 paths into the v30 isolated namespace when active.

    No-op when legacy data mode is explicitly enabled.
    """
    p = Path(path)
    if not _use_apex_isolated_store() or not is_legacy_data_path(p):
        return p
    target = data_dir() / subdir / p.name
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def project_root() -> Path:
    """Resolve IG Agent repository root (code + read-only bundled config)."""
    env = os.environ.get("IG_AGENT_ROOT")
    if env:
        return Path(env).resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    exe = Path(sys.argv[0]).resolve()
    parts = exe.parts

    if "Contents" in parts and "MacOS" in parts:
        idx = parts.index("Contents")
        bundle = Path(*parts[:idx])
        if bundle.parent.name == "launcher":
            return bundle.parent.parent
        return bundle.parent

    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    """Read-only bundled config templates (never used as writable data store)."""
    return project_root() / "config"


def data_dir() -> Path:
    """Writable runtime state — v30 isolated namespace when Apex monolith is active."""
    env = os.environ.get("IG_AGENT_DATA_DIR", "").strip()
    if env:
        d = Path(env).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d
    if _use_apex_isolated_store():
        d = apex_isolated_root() / "data"
        for sub in ("logs", "state", "historical", "ohlc_cache"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        return d
    d = project_root() / "src" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    """Writable logs under active data namespace."""
    d = data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def analytics_dir() -> Path:
    """SQLite triage / analytics — isolated under v30-production when active."""
    if _use_apex_isolated_store():
        d = apex_isolated_root() / "analytics"
    else:
        d = project_root() / "src" / "analytics"
    d.mkdir(parents=True, exist_ok=True)
    return d


def triage_db_path() -> Path:
    """Worker D high-speed ledger — triage_v30.db in isolated analytics namespace."""
    env = os.environ.get("IG_TRIAGE_DB", "").strip()
    if env:
        return Path(env).resolve()
    try:
        from system.node_profile import get_node_profile

        return get_node_profile().triage_db
    except Exception:
        pass
    return analytics_dir() / "triage_v30.db"


def state_dir() -> Path:
    d = data_dir() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_lake_dir() -> Path:
    """Event lake — isolated under v30 namespace when active."""
    if _use_apex_isolated_store():
        d = apex_isolated_root() / "data_lake"
    else:
        d = project_root() / "data_lake"
    d.mkdir(parents=True, exist_ok=True)
    return d


def feeder_events_dir() -> Path:
    """Append-only feeder events for learning plane."""
    d = data_lake_dir() / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_path(relative: str) -> Path:
    p = Path(relative)
    return p if p.is_absolute() else project_root() / p


def chdir_to_root() -> None:
    os.chdir(project_root())


def find_python_executable() -> str:
    import shutil

    root = project_root()
    for rel in (
        ".venv/bin/python3",
        "venv/bin/python3",
        ".venv/bin/python",
        "venv/bin/python",
    ):
        candidate = root / rel
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    candidates = (
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "python3",
        "python",
    )
    for c in candidates:
        found = shutil.which(c)
        if found:
            return found
    return "python3"
