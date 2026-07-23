"""
Desk deploy hold — informational gate for active trading sessions.

When hold is active and the broker has open positions, boot logs a warning that
the deploy window is closed. This does not block trading; it reminds operators
to stack upgrades on disk and deploy once at a session boundary.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import shared_state_dir, project_root


def _hold_path() -> Path:
    """Resolve hold file dynamically — shared across v32 dual-port engines."""
    return shared_state_dir() / "deploy_hold.json"


def _under_pytest_or_harness() -> bool:
    return (
        os.environ.get("IG_TEST_HARNESS", "").strip() == "1"
        or os.environ.get("IG_AGENT_PYTEST", "").strip() == "1"
        or bool(os.environ.get("PYTEST_CURRENT_TEST"))
    )


def _is_production_state_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
        prod = (project_root() / "src" / "data" / "v31-production").resolve()
        return str(resolved).startswith(str(prod) + os.sep) or resolved == prod
    except OSError:
        return False


def _refuse_prod_write_under_test(path: Path) -> bool:
    """Block unit tests from mutating v31-production deploy_hold / pause flags."""
    return _under_pytest_or_harness() and _is_production_state_path(path)


def _read_hold_file() -> dict[str, Any]:
    path = _hold_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def deploy_hold_file_active() -> bool:
    """Operator file override — require ``active: true`` (existence alone ≠ hold)."""
    # Scan shared + CFD/SB lane mirrors so a leftover lane file cannot soft-block.
    try:
        from runtime.halt_sot import any_deploy_hold_file_active

        if not any_deploy_hold_file_active():
            return False
    except Exception:
        raw = _read_hold_file()
        if not raw or not bool(raw.get("active")):
            return False
    raw = _read_hold_file()
    until = float(raw.get("until") or 0) if raw else 0.0
    if until > 0 and time.time() > until:
        return False
    # Lane-mirror active with empty primary still counts as held.
    return True


def deploy_hold_config_active(cfg: Any | None = None) -> bool:
    block: dict[str, Any] = {}
    if cfg is None:
        try:
            from system.config_loader import get_config

            cfg = get_config()
        except Exception:
            cfg = None
    if cfg is not None:
        try:
            raw = (
                cfg.get("desk_deploy")
                if isinstance(cfg, dict)
                else getattr(cfg, "desk_deploy", None) or {}
            )
            if isinstance(raw, dict):
                block = raw
        except Exception:
            block = {}
    return bool(block.get("hold_active_session", False))


def is_deploy_hold_active(cfg: Any | None = None) -> bool:
    return deploy_hold_file_active() or deploy_hold_config_active(cfg)


def broker_open_count(rest: Any | None = None) -> int:
    if rest is not None:
        try:
            items = list(rest.open_positions(budget_priority=True) or [])
            return len(items)
        except Exception:
            pass
    try:
        import urllib.request

        with urllib.request.urlopen(
            "http://127.0.0.1:8080/api/positions/live", timeout=4.0
        ) as resp:
            body = json.loads(resp.read().decode())
        return int(body.get("count") or body.get("broker_open_count") or 0)
    except Exception:
        return 0


def warn_if_deploy_window_closed(
    rest: Any | None = None,
    cfg: Any | None = None,
) -> bool:
    """
    Log a warning on boot when deploy hold is active and broker has opens.

    Returns True when the deploy window is considered closed (informational).
    """
    if not is_deploy_hold_active(cfg):
        return False
    opens = broker_open_count(rest)
    if opens <= 0:
        return False
    log_engine(
        f"deploy_hold: active session — deploy window CLOSED "
        f"(broker_open={opens}). Stack upgrades on disk; deploy at session boundary "
        f"via scripts/desk_deploy.sh"
    )
    return True


def set_deploy_hold(
    *,
    active: bool = True,
    reason: str = "operator",
    until: float | None = None,
) -> Path:
    """Write operator hold file (optional CLI helper)."""
    path = _hold_path()
    if _refuse_prod_write_under_test(path):
        # Harness isolation — never pollute the live desk state tree from pytest.
        return path
    # Clear = delete file (existence alone is operator risk during soak).
    if not active:
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            pass
        # Also clear lane mirrors used by v32 dual-port.
        try:
            from system.paths import data_dir

            for lane in ("state_cfd", "state_sb", "state"):
                p = Path(data_dir()) / lane / "deploy_hold.json"
                if p.is_file():
                    p.unlink()
        except Exception:
            pass
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "active": bool(active),
        "reason": reason,
        "ts": time.time(),
    }
    if until is not None:
        payload["until"] = float(until)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path
