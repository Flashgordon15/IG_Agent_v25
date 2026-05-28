"""
E2E execution checks — mock pipeline + IG DEMO routing (no order by default).

Used by scripts/e2e_execution_probe.py and POST /api/system/e2e.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from system.paths import project_root


def run_mock_pipeline_tests(*, timeout_sec: float = 120.0) -> dict[str, Any]:
    """Run tests/test_execution_pipeline_e2e.py (7 gates -> execute_trade)."""
    root = project_root()
    py = sys.executable
    try:
        proc = subprocess.run(
            [
                py,
                "-m",
                "pytest",
                "tests/test_execution_pipeline_e2e.py",
                "-q",
                "--tb=no",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env={
                **dict(__import__("os").environ),
                "PYTHONPATH": str(root / "src"),
                "IG_AGENT_PYTEST": "1",
            },
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        m = re.search(r"(\d+) passed", out)
        passed = int(m.group(1)) if m else 0
        failed_m = re.search(r"(\d+) failed", out)
        failed = int(failed_m.group(1)) if failed_m else 0
        return {
            "ok": proc.returncode == 0,
            "passed": passed,
            "failed": failed,
            "summary": out.strip().splitlines()[-1] if out.strip() else "",
            "detail": "Mock pipeline: 7 gates -> process_tick -> execute_trade",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "passed": 0,
            "failed": 1,
            "summary": "timeout",
            "error": f"pytest timed out after {timeout_sec:.0f}s",
            "detail": "Mock pipeline",
        }
    except Exception as e:
        return {
            "ok": False,
            "passed": 0,
            "failed": 1,
            "summary": str(e),
            "error": str(e),
            "detail": "Mock pipeline",
        }


def run_demo_routing_check() -> dict[str, Any]:
    """Validate IG DEMO REST routing (dry-run — no order placed)."""
    try:
        from system.config_loader import ConfigLoader
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import ensure_shared_authenticated

        status = try_load_credentials()
        if not status.ok or status.credentials is None:
            return {
                "ok": False,
                "error": status.error or "credentials not loaded",
                "detail": "IG DEMO routing (dry-run)",
            }

        cfg = ConfigLoader(project_root() / "config" / "config_v25.json").load_config()
        rest = ensure_shared_authenticated(status.credentials)
        epic = cfg.epic

        bid = 0.0
        offer = 0.0
        price_source = "rest"
        try:
            from system.market_data_hub import get_market_data_hub

            hub_snap = get_market_data_hub().get_snapshot(epic)
            if hub_snap is not None and hub_snap.bid > 0 and hub_snap.offer > 0:
                bid = float(hub_snap.bid)
                offer = float(hub_snap.offer)
                price_source = str(hub_snap.source or "hub")
        except Exception:
            pass

        from system.rest_api_budget import e2e_diagnostics_rest_window

        with e2e_diagnostics_rest_window():
            if bid <= 0 or offer <= 0:
                snap = rest.fetch_market_snapshot(epic)
                bid = float(snap.get("bid") or 0)
                offer = float(snap.get("offer") or 0)
                price_source = "rest"
            validation = rest.validate_demo_order_routing(
                epic=epic,
                dry_run=True,
                market_bid=bid,
                market_offer=offer,
                skip_balance_check=False,
            )
        if not validation.get("ok"):
            return {
                "ok": False,
                "error": str(validation.get("error", validation)),
                "detail": "IG DEMO routing (dry-run)",
                "epic": epic,
            }
        return {
            "ok": True,
            "detail": "IG DEMO routing (dry-run)",
            "epic": epic,
            "bid": bid,
            "offer": offer,
            "price_source": price_source,
            "balance": validation.get("balance"),
            "account_id": validation.get("account_id"),
            "base_url": validation.get("base_url"),
            "message": validation.get("message", "DEMO routing validated"),
        }
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "detail": "IG DEMO routing (dry-run)",
        }


def run_e2e_execution_check(*, include_routing: bool = True) -> dict[str, Any]:
    """
    Full E2E check for dashboard: mock pipeline + optional IG DEMO routing.

    Does not place orders.
    """
    mock = run_mock_pipeline_tests()
    steps: list[dict[str, Any]] = [
        {"name": "mock_pipeline", **mock},
    ]
    routing: dict[str, Any] | None = None
    if include_routing:
        routing = run_demo_routing_check()
        steps.append({"name": "demo_routing", **routing})

    ok = mock.get("ok") and (routing is None or routing.get("ok"))
    summary_parts: list[str] = []
    if mock.get("ok"):
        summary_parts.append(f"mock {mock.get('passed', 0)} passed")
    else:
        summary_parts.append(f"mock FAIL ({mock.get('summary', '')})")
    if routing is not None:
        if routing.get("ok"):
            summary_parts.append("IG DEMO routing OK")
        else:
            summary_parts.append(f"routing FAIL ({routing.get('error', '')})")

    return {
        "ok": ok,
        "summary": " · ".join(summary_parts),
        "steps": steps,
        "mock_pipeline": mock,
        "demo_routing": routing,
        "places_order": False,
    }
