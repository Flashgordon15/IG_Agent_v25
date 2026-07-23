#!/usr/bin/env python3
"""Repo-root CLI shim for offline forensic preflight."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from runtime.forensics_preflight import run_forensics_cli  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run_forensics_cli())
