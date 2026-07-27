#!/usr/bin/env python3
"""CLI: read-only ML / strategy review scorecard.

Examples::

  PYTHONPATH=src .venv/bin/python3 scripts/ml_strategy_review.py --day 2026-07-24

  PYTHONPATH=src .venv/bin/python3 -m diagnostics.ml_strategy_review --day 2026-07-24

Never places orders. Never removes bleed locks. Never POST /api/start.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from diagnostics.ml_strategy_review import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
