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
    env.setdefault("APP_MODE", "DEMO")
    env.setdefault("IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json")

    # Execute the script by path — never `bash -s` with the body on stdin.
    # Piping the script made heredocs inside functions steal the remaining
    # script bytes, so later top-level calls (clear_stale_agent_lock) failed
    # with "command not found" and the watchdog could not heal the agent.
    try:
        return int(
            subprocess.call(
                ["/bin/bash", str(script)],
                cwd=str(root),
                env=env,
            )
        )
    except Exception as exc:
        print(f"watchdog_launchd: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
