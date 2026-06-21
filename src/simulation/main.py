"""
Testbed simulation entry point — VS Code task / manual calibration daemon.

Sets HARDENED_TESTBED env defaults, claims daemon PID, and delegates to src/main.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap_env() -> Path:
    root = Path(__file__).resolve().parents[2]
    os.environ.setdefault("IG_AGENT_ROOT", str(root))
    os.environ.setdefault("PYTHONPATH", str(root / "src"))
    os.environ.setdefault("IG_APEX_RUNTIME_MODE", "HARDENED_TESTBED")
    os.environ.setdefault("IG_TESTBED_ROOT", "/tmp/apex_opt_calibration")
    os.environ.setdefault(
        "IG_HISTORICAL_REPLAY",
        "src/simulation/data/production_5day_archive.jsonl",
    )
    os.environ.setdefault("IG_REPLAY_SPEED", "100")
    os.environ.setdefault("IG_MATRIX_OPTIMIZATION", "1")
    os.environ.setdefault("IG_OPTIMIZATION_CHAOS", "1")
    os.environ.setdefault("TESTBED_ALLOW_ZOMBIE", "1")
    # Prevent inherited Electron/desktop fast-bind from spawning duplicate Gate1 cycles.
    os.environ["IG_APEX_DESKTOP"] = "0"
    os.environ.pop("IG_APEX_DAEMON", None)
    if str(root / "src") not in sys.path:
        sys.path.insert(0, str(root / "src"))
    return root


def _load_agent_main():
    """Load src/main.py without shadowing from this package directory."""
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    entry = root / "src" / "main.py"
    spec = importlib.util.spec_from_file_location("ig_agent_entry", entry)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load agent entry: {entry}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.main


def main() -> None:
    _bootstrap_env()
    from simulation.testbed_daemon import claim_daemon_pid

    claim_daemon_pid()
    try:
        from system.shutdown_cleanup import mark_manual_stop

        mark_manual_stop(source="testbed_simulation")
    except Exception:
        pass
    agent_main = _load_agent_main()
    agent_main()


if __name__ == "__main__":
    main()
