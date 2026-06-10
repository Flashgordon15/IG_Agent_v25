#!/usr/bin/env python3
"""launchd-safe agent start — resolves python and execs src/main.py (no Desktop bash)."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _agent_root() -> Path:
    env = os.environ.get("IG_AGENT_ROOT", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def resolve_python(root: Path) -> Path:
    candidates = [
        root / ".venv" / "bin" / "python3",
        root / "venv" / "bin" / "python3",
        Path("/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"),
        Path("/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"),
        Path("/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"),
        Path("/opt/homebrew/bin/python3"),
        Path("/usr/local/bin/python3"),
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return Path(sys.executable)


def main() -> int:
    root = _agent_root()
    py = resolve_python(root)
    main_py = root / "src" / "main.py"
    if not main_py.is_file():
        print(f"start_agent_launchd: missing {main_py}", file=sys.stderr)
        return 1

    os.chdir(root)
    os.environ["IG_AGENT_ROOT"] = str(root)
    os.environ.setdefault("IG_AGENT_FROM_LAUNCHER", "1")
    os.environ.setdefault("IG_AGENT_SKIP_DEPLOY_CHECK", "1")
    src_path = str(root / "src")
    existing = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = src_path if not existing else f"{src_path}:{existing}"

    os.execv(str(py), [str(py), str(main_py)])


if __name__ == "__main__":
    raise SystemExit(main())
