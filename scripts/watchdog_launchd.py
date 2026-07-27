#!/usr/bin/env python3
"""launchd-safe watchdog entry — Python reads watchdog.sh; bash executes it.

macOS may block launchd from executing bash scripts under Desktop (exit 126 /
Operation not permitted). Python can read project files from launchd; invoking
bash with the script path avoids bash opening a protected path directly.

``--dual-port`` (v32 twin desk): sets IG_V32_DUAL_PORT=1 so watchdog.sh watches
:8080/:8081 and defers single-engine restarts (no fight with live twins).
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


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    dual = "--dual-port" in args or os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1"

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
    if dual:
        env["IG_V32_DUAL_PORT"] = "1"
        env.setdefault("IG_V32_WATCH_PORTS", "8080,8081")

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
