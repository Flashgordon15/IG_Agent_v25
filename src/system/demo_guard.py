"""Demo-only deployment guard — blocks LIVE credentials and live trading flags."""

from __future__ import annotations

import os
from typing import Any


def demo_only_enforced(cfg: dict[str, Any]) -> bool:
    """True when config requires DEMO account (default for v29 learning deployment)."""
    if not bool(cfg.get("demo_only_deployment", True)):
        return False
    if os.environ.get("IG_AGENT_ALLOW_LIVE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return False
    return True


def validate_demo_only_startup(cfg: dict[str, Any]) -> tuple[bool, str]:
    """
    Return (ok, message). Blocks startup when demo-only is enforced but LIVE would run.
    """
    if not demo_only_enforced(cfg):
        return True, "demo guard skipped (demo_only_deployment off or IG_AGENT_ALLOW_LIVE set)"

    if bool(cfg.get("allow_live_trading")):
        return (
            False,
            "allow_live_trading=true — demo-only deployment; set false in config_v29.json",
        )

    op = str(cfg.get("operating_mode", "TEST")).upper()
    if op == "LIVE":
        return (
            False,
            "operating_mode=LIVE — demo-only deployment; use DEMO or TEST",
        )

    try:
        from system.credentials_holder import get_credentials_holder

        creds = get_credentials_holder().credentials
        if creds is not None and str(creds.account_type).upper() == "LIVE":
            return (
                False,
                "LIVE IG credentials detected — demo-only deployment; "
                "use DEMO credentials or IG_AGENT_ALLOW_LIVE=1 to override",
            )
    except Exception:
        pass

    return True, "demo-only deployment confirmed"
