#!/usr/bin/env python3
"""Promote/demote per-epic ml_veto whitelist from S4 manifest val WR."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "config_v26.json"
MANIFEST_PATH = ROOT / "data_lake" / "models" / "s4" / "manifest.json"


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except json.JSONDecodeError:
        return {}


def build_per_epic(
    manifest: dict,
    *,
    min_val_wr: float,
    dry_run: bool,
) -> dict[str, dict]:
    per: dict[str, dict] = {}
    for epic, info in sorted((manifest.get("by_epic") or {}).items()):
        if not isinstance(info, dict) or not info.get("ok"):
            continue
        val_wr = float(info.get("val_wr") or 0)
        eligible = bool(info.get("veto_eligible")) and val_wr >= min_val_wr
        if eligible:
            per[epic] = {
                "enabled": True,
                "min_probability": float(info.get("recommended_min_prob") or 0.53),
            }
    return per


def apply_promotion(
    *,
    min_val_wr: float = 0.52,
    dry_run: bool = False,
) -> dict:
    manifest = _load_json(MANIFEST_PATH)
    cfg = _load_json(CONFIG_PATH)
    ml = dict(cfg.get("ml_veto") or {})
    s4 = dict(cfg.get("s4_ml_meta") or {})
    min_wr = float(s4.get("min_val_wr") or min_val_wr)

    new_per = build_per_epic(manifest, min_val_wr=min_wr, dry_run=dry_run)
    old_per = dict(ml.get("per_epic") or {})

    ml["per_epic"] = new_per
    if new_per and not ml.get("enabled"):
        ml["enabled"] = True
    cfg["ml_veto"] = ml

    report = {
        "manifest_version": manifest.get("version"),
        "min_val_wr": min_wr,
        "promoted": sorted(new_per.keys()),
        "removed": sorted(set(old_per.keys()) - set(new_per.keys())),
        "unchanged": sorted(set(old_per.keys()) & set(new_per.keys())),
        "dry_run": dry_run,
    }

    if not dry_run:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        try:
            sys.path.insert(0, str(ROOT / "src"))
            from system.v26_config import reset_v26_config_cache_for_tests

            reset_v26_config_cache_for_tests()
        except Exception:
            pass

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--min-val-wr",
        type=float,
        default=None,
        help="Override S4 min val WR (default: config s4_ml_meta.min_val_wr)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print promotion plan without writing config",
    )
    args = parser.parse_args()

    if not MANIFEST_PATH.is_file():
        print(f"Missing manifest: {MANIFEST_PATH}")
        print("Run: PYTHONPATH=src:v26 python3 scripts/v26_s4_retrain.py")
        return 1

    report = apply_promotion(
        min_val_wr=float(args.min_val_wr or 0.52),
        dry_run=bool(args.dry_run),
    )
    print("=== ml_veto promotion ===")
    print(f"Manifest: {report['manifest_version']}")
    print(f"Min val WR: {report['min_val_wr']}")
    print(
        f"Promoted ({len(report['promoted'])}): {', '.join(report['promoted']) or '—'}"
    )
    if report["removed"]:
        print(f"Removed: {', '.join(report['removed'])}")
    if args.dry_run:
        print("(dry-run — config not written)")
    else:
        print(f"Updated: {CONFIG_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
