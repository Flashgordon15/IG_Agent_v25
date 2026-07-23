"""Offline forensic / multiplex preflight — no trading, ports, or session locks."""

from __future__ import annotations

import os
from typing import Any


def check_forensics_dry_run() -> tuple[bool, list[str]]:
    errors: list[str] = []
    try:
        from runtime.session_lock import lock_path_for_scope
        from system.core_affinity import core_model_doc
        from system.engine_cli import apply_engine_cli_env, parse_engine_cli
        from system.engine_lane import (
            DEFAULT_ACCOUNT_CFD,
            DEFAULT_ACCOUNT_SB,
            ENGINE_CFD_SNIPER,
            ENGINE_SB_SENTINEL,
            infer_engine_id,
            resolve_journal_metadata,
        )
        from system.identity.shared_memory_bridge import shm_name_for_track
        from system.paths import data_dir, state_dir
    except Exception as exc:
        return False, [f"import: {type(exc).__name__}: {exc}"]

    if infer_engine_id(account_id=DEFAULT_ACCOUNT_CFD) != ENGINE_CFD_SNIPER:
        errors.append(f"infer_engine_id({DEFAULT_ACCOUNT_CFD}) != {ENGINE_CFD_SNIPER}")
    if infer_engine_id(account_id=DEFAULT_ACCOUNT_SB) != ENGINE_SB_SENTINEL:
        errors.append(f"infer_engine_id({DEFAULT_ACCOUNT_SB}) != {ENGINE_SB_SENTINEL}")

    root = data_dir()
    cfd_lock = lock_path_for_scope(f"ig:{DEFAULT_ACCOUNT_CFD}", root)
    sb_lock = lock_path_for_scope(f"ig:{DEFAULT_ACCOUNT_SB}", root)
    if cfd_lock.name != f"session_ig_{DEFAULT_ACCOUNT_CFD}.lock":
        errors.append(f"unexpected CFD lock name: {cfd_lock.name}")
    if sb_lock.name != f"session_ig_{DEFAULT_ACCOUNT_SB}.lock":
        errors.append(f"unexpected SB lock name: {sb_lock.name}")

    for account_id, engine_id in (
        (DEFAULT_ACCOUNT_CFD, ENGINE_CFD_SNIPER),
        (DEFAULT_ACCOUNT_SB, ENGINE_SB_SENTINEL),
    ):
        meta = resolve_journal_metadata(engine_id=engine_id, account_id=account_id)
        if meta.get("account_id") != account_id:
            errors.append(f"journal metadata account mismatch for {account_id}")

    cfd_cli = parse_engine_cli(
        ["--port=8080", f"--account-id={DEFAULT_ACCOUNT_CFD}", "--origin=QUANT_SNIPER"]
    )
    sb_cli = parse_engine_cli(
        ["--port=8081", f"--account-id={DEFAULT_ACCOUNT_SB}", "--origin=MACRO_SENTINEL"]
    )
    apply_engine_cli_env(cfd_cli)
    cfd_ring = os.environ.get("IG_SHM_RING_NAME", "")
    apply_engine_cli_env(sb_cli)
    sb_ring = os.environ.get("IG_SHM_RING_NAME", "")
    if cfd_ring != "ig_agent_v33_shm_cfd_8080":
        errors.append(f"CFD multiplex ring: {cfd_ring!r}")
    if sb_ring != "ig_agent_v33_shm_sb_8081":
        errors.append(f"SB multiplex ring: {sb_ring!r}")

    apply_engine_cli_env(cfd_cli)
    cfd_live = shm_name_for_track("live")
    apply_engine_cli_env(sb_cli)
    sb_live = shm_name_for_track("live")
    if cfd_live != f"ig_agent_v30_live_state_{DEFAULT_ACCOUNT_CFD}":
        errors.append(f"CFD live SHM track: {cfd_live!r}")
    if sb_live != f"ig_agent_v30_live_state_{DEFAULT_ACCOUNT_SB}":
        errors.append(f"SB live SHM track: {sb_live!r}")

    doc = core_model_doc()
    if doc.get("core1", {}).get("account") != DEFAULT_ACCOUNT_CFD:
        errors.append("core_model_doc.core1.account mismatch")
    if doc.get("core2", {}).get("account") != DEFAULT_ACCOUNT_SB:
        errors.append("core_model_doc.core2.account mismatch")

    forensic_log = state_dir() / "boot_stage_forensic.log"
    forensic_hb = state_dir() / "boot_stage_heartbeat.json"
    if not str(forensic_log).endswith("boot_stage_forensic.log"):
        errors.append(f"forensic log path invalid: {forensic_log}")
    if not str(forensic_hb).endswith("boot_stage_heartbeat.json"):
        errors.append(f"forensic heartbeat path invalid: {forensic_hb}")

    return len(errors) == 0, errors


def run_forensics_cli(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Offline forensic multiplex preflight")
    parser.add_argument(
        "--check-forensics",
        action="store_true",
        help="Validate Z6BAH3/Z6BAH4 multiplex + forensic path helpers",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Required with --check-forensics; no trading or lock acquisition",
    )
    args = parser.parse_args(argv)

    if args.check_forensics:
        if not args.dry_run:
            print("--check-forensics requires --dry-run", file=sys.stderr)
            return 2
        ok, errors = check_forensics_dry_run()
        if ok:
            print("forensics_dry_run: PASS")
            return 0
        for err in errors:
            print(f"forensics_dry_run: FAIL {err}", file=sys.stderr)
        return 1

    parser.print_help()
    return 2
