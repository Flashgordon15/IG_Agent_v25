"""Project root and path resolution — macOS .app bundle aware."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def project_root() -> Path:
    """Resolve IG Agent v24 ProGUI repository root."""
    env = os.environ.get("IG_AGENT_ROOT")
    if env:
        return Path(env).resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    exe = Path(sys.argv[0]).resolve()
    parts = exe.parts

    # launcher/IG Agent v24 Pro.app/Contents/MacOS/Launcher -> root is parents[4]
    if "Contents" in parts and "MacOS" in parts:
        idx = parts.index("Contents")
        bundle = Path(*parts[:idx])  # .../IG Agent v24 Pro.app
        # Bundle lives in launcher/ under project root
        if bundle.parent.name == "launcher":
            return bundle.parent.parent
        return bundle.parent

    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return project_root() / "config"


def data_dir() -> Path:
    d = project_root() / "src" / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    """Writable logs under src/data/logs/."""
    d = project_root() / "src" / "data" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_lake_dir() -> Path:
    """v25→v26 shared lake (events, features, models)."""
    d = project_root() / "data_lake"
    d.mkdir(parents=True, exist_ok=True)
    return d


def feeder_events_dir() -> Path:
    """Append-only v25 feeder events for v26 (jsonl per day)."""
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
