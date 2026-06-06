#!/usr/bin/env python3
"""CLI helper for external processes (watchdog.sh) to send Telegram critical alerts."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _configure_from_disk() -> None:
    import json

    from system.config import Config
    from system.config_loader import _sync_operating_mode_from_credentials
    from system.config_validator import apply_config_defaults
    from system.paths import config_dir
    from system.telegram_notifier import configure_telegram

    cfg_path = config_dir() / "config_v25.json"
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    merged = apply_config_defaults(raw)
    _sync_operating_mode_from_credentials(merged)
    configure_telegram(Config(_data=merged))


def main() -> int:
    from system.telegram_notifier import send_critical_alert

    _configure_from_disk()
    message = " ".join(sys.argv[1:]).strip() or "alert"
    ok = send_critical_alert(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
