#!/usr/bin/env python3
"""CLI entrypoint for GUI/Desk supervisor (Phase 1 observe-only).

Prefer:
  PYTHONPATH=src .venv/bin/python3 scripts/gui_desk_supervisor.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from runtime.gui_desk_supervisor import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
