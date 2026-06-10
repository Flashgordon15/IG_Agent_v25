#!/usr/bin/env python3
"""launchd-safe watchdog entry — Python reads watchdog.sh; bash -s executes it.

macOS may block launchd from executing bash scripts under Desktop (exit 126 /
Operation not permitted). Python can read project files from launchd; piping the
script to bash -s avoids bash opening a protected path directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _agent_root() -> Path:
    env = os.environ.get("IG_AGENT_ROOT", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent


def main() -> int:
    root = _agent_root()
    script = root / "scripts" / "watchdog.sh"
    if not script.is_file():
        print(f"watchdog_launchd: missing {script}", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env["IG_AGENT_ROOT"] = str(root)
    env.setdefault("PYTHONPATH", str(root / "src"))

    try:
        proc = subprocess.Popen(
            ["/bin/bash", "-s"],
            stdin=subprocess.PIPE,
            cwd=str(root),
            env=env,
        )
        assert proc.stdin is not None
        proc.stdin.write(script.read_bytes())
        proc.stdin.close()
        return int(proc.wait())
    except Exception as exc:
        print(f"watchdog_launchd: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
