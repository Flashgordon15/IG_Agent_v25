#!/usr/bin/env python3
"""Bridge legacy src/data artifacts into the session IG_DATA_ROOT tree.

Safe to run read-only with --check. Use --apply at a flat session boundary
(or when no writer holds the learning DB) so health data_root and data_dir()
agree.

  PYTHONPATH=src python3 scripts/unify_data_root.py --check
  PYTHONPATH=src python3 scripts/unify_data_root.py --apply
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Report only")
    parser.add_argument("--apply", action="store_true", help="Run bridge")
    args = parser.parse_args()
    if not args.check and not args.apply:
        args.check = True

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    os.environ.setdefault("APP_MODE", "DEMO")
    os.environ.setdefault(
        "IG_AGENT_CONFIG", "config/config_v31_demo_throughput.json"
    )

    from runtime.app_mode import resolve_app_mode, resolve_data_root
    from system.paths import bridge_legacy_data_into, data_dir, legacy_src_data_dir

    mode = resolve_app_mode()
    target = Path(os.environ.get("IG_DATA_ROOT") or resolve_data_root(mode)).resolve()
    legacy = legacy_src_data_dir().resolve()
    print(f"mode={mode.value}")
    print(f"legacy={legacy}")
    print(f"target={target}")
    print(f"data_dir()={data_dir().resolve()}")

    for rel in (
        "learning_db.sqlite3",
        "trade_support_status.json",
        "state/broker_snapshot.json",
        "runtime_state.json",
    ):
        src = legacy / rel
        dst = target / rel
        src_sz = src.stat().st_size if src.exists() else None
        dst_sz = dst.stat().st_size if dst.exists() else None
        print(f"  {rel}: legacy={src_sz} target={dst_sz} symlink={dst.is_symlink() if dst.exists() else False}")

    if args.apply:
        actions = bridge_legacy_data_into(target, legacy=legacy)
        print("actions:", actions or ["noop"])
        # Re-enter data_dir so IG_AGENT_DATA_DIR consumers see bridged tree.
        os.environ.setdefault("IG_DATA_ROOT", str(target))
        os.environ.setdefault("IG_AGENT_DATA_DIR", str(target))
        print(f"data_dir() after apply={data_dir().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
