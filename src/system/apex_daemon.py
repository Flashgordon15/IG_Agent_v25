"""
Apex detached trading daemon — spawned by the Electron shell on double-click.

Survives UI close / Cmd+Q; binds :9090 and runs the full Gate 1–5 boot pipeline.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _resolve_agent_root() -> Path:
    env_root = os.environ.get("IG_AGENT_ROOT", "").strip()
    if env_root:
        return Path(env_root).resolve()
    # src/system/apex_daemon.py → repo or packaged agent root
    return Path(__file__).resolve().parents[2]


def _bootstrap() -> Path:
    root = _resolve_agent_root()
    src = root / "src"
    if src.is_dir() and str(src) not in sys.path:
        sys.path.insert(0, str(src))
    os.environ.setdefault("IG_AGENT_ROOT", str(root))
    os.environ.setdefault("IG_AGENT_MODE", "DEMO")
    os.environ.setdefault("IG_MOCK_FEED", "0")
    os.environ.setdefault("NODE_ENV", "production")
    os.environ.setdefault("IG_APEX_DESKTOP", "1")
    os.environ.setdefault("IG_APEX_DAEMON", "1")
    os.environ.setdefault("IG_AGENT_SKIP_ORPHAN_KILL", "1")
    return root


def main() -> None:
    root = _bootstrap()
    entry = root / "src" / "main.py"
    if not entry.is_file():
        raise SystemExit(f"apex_daemon: missing entry {entry}")
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
