#!/usr/bin/env python3
"""CLI helper for external processes (watchdog.sh) to send Telegram critical alerts."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from system.telegram_notifier import send_critical_alert  # noqa: E402


def main() -> int:
    message = " ".join(sys.argv[1:]).strip() or "alert"
    ok = send_critical_alert(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
