#!/usr/bin/env python3
"""Run pytest per file in parallel; report failures."""
from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = ROOT / ".venv" / "bin" / "python3"
FILES = sorted(ROOT.glob("tests/test_*.py")) + sorted(ROOT.glob("tests/**/test_*.py"))
FILES = sorted({f for f in FILES if f.is_file()})


def run_file(path: Path) -> tuple[str, int, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    proc = subprocess.run(
        [str(PY), "-m", "pytest", str(path), "-p", "no:anyio", "-q", "--tb=line"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-8:])
    return str(path.relative_to(ROOT)), proc.returncode, tail


def main() -> int:
    workers = int(os.environ.get("PYTEST_WORKERS", "8"))
    failures: list[tuple[str, str]] = []
    passed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_file, f): f for f in FILES}
        for fut in concurrent.futures.as_completed(futures):
            rel, code, tail = fut.result()
            if code == 0:
                passed += 1
            else:
                failures.append((rel, tail))
                print(f"FAIL {rel}", flush=True)
    print(f"\n=== {passed} files passed, {len(failures)} files failed, {len(FILES)} total ===")
    for rel, tail in sorted(failures):
        print(f"\n--- {rel} ---\n{tail}")
    out = ROOT / "logs" / "pytest_parallel_failures.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n\n".join(f"=== {rel} ===\n{tail}" for rel, tail in sorted(failures)),
        encoding="utf-8",
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
