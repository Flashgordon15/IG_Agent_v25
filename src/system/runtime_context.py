"""
Central RuntimeContext — v31 production-plane harness bootstrap.

Initializes config, shared IG REST session, and local API reachability for
operator scripts (e.g. ``scripts/force_production_demo_trade.py``).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from system.paths import project_root

_LIQUID_CANDIDATES: tuple[str, ...] = (
    "CS.D.EURUSD.CFD.IP",
    "IX.D.DOW.IFM.IP",
    "CS.D.CFPGOLD.CFP.IP",
    "IX.D.NIKKEI.IFM.IP",
)


def _apply_production_plane_env() -> None:
    """Map operator env aliases onto canonical Apex production keys."""
    prod = os.environ.get("PROD_MODE", "").strip().upper()
    if prod in ("PRODUCTION", "PROD", "LIVE"):
        os.environ.setdefault("IG_APEX_RUNTIME_MODE", "PRODUCTION")
        os.environ.setdefault("NODE_ENV", "production")
        os.environ.setdefault("IG_NODE_PROFILE", "production")
    if os.environ.get("IG_SHARE_ENGINE", "").strip() in ("1", "true", "yes"):
        os.environ.setdefault("IG_PRODUCTION_EXECUTION", "1")
    os.environ.setdefault(
        "IG_TRIAGE_DB",
        str(project_root() / "src" / "analytics" / "triage_v31.db"),
    )


@dataclass
class RuntimeContext:
    """Process-local runtime handle for v31 E2E validation scripts."""

    api_base: str = "http://127.0.0.1:8080"
    config: Any | None = None
    rest_client: Any | None = None
    connected: bool = False
    health: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _apply_production_plane_env()
        port = os.environ.get("IG_API_PORT", "").strip()
        if port.isdigit():
            self.api_base = f"http://127.0.0.1:{int(port)}"

    def initialize(self) -> "RuntimeContext":
        from system.config_loader import load_active_config
        from system.credentials_loader import try_load_credentials
        from system.ig_rest_session import ensure_shared_authenticated

        self.config = load_active_config(validate=False)
        cred_status = try_load_credentials()
        if not cred_status.ok or cred_status.credentials is None:
            raise RuntimeError(
                f"RuntimeContext: credentials unavailable — {cred_status.error}"
            )
        self.rest_client = ensure_shared_authenticated(cred_status.credentials)
        return self

    def connect_api(self, *, timeout_sec: float = 8.0) -> dict[str, Any]:
        url = f"{self.api_base.rstrip('/')}/api/health"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                payload = {"ok": True, "auth_required": True, "status": "alive"}
            else:
                raise RuntimeError(f"API health failed: HTTP {exc.code}") from exc
        except Exception as exc:
            raise RuntimeError(f"API unreachable at {url}: {exc}") from exc
        self.health = payload if isinstance(payload, dict) else {"raw": payload}
        self.connected = True
        return self.health

    def select_open_epic(
        self,
        candidates: tuple[str, ...] | list[str] | None = None,
    ) -> str:
        """Pick the first highly liquid epic that passes broker calendar rules."""
        from system.market_integrity import epic_market_open

        pool = tuple(candidates) if candidates else _LIQUID_CANDIDATES
        for epic in pool:
            key = str(epic or "").strip()
            if not key:
                continue
            if epic_market_open(key):
                return key
        # Fallback — REST market snapshot when calendar cache is cold.
        rest = self.rest_client
        if rest is None:
            raise RuntimeError("RuntimeContext: rest_client not initialized")
        for epic in pool:
            key = str(epic or "").strip()
            if not key:
                continue
            try:
                snap = rest.fetch_market_snapshot(key)
                status = str(snap.get("marketStatus") or snap.get("status") or "").upper()
                if status in ("TRADEABLE", "OPEN", "EDITS_ONLY"):
                    return key
                bid = float(snap.get("bid") or 0)
                offer = float(snap.get("offer") or 0)
                if bid > 0 and offer > 0:
                    return key
            except Exception:
                continue
        raise RuntimeError(
            f"RuntimeContext: no open liquid epic among {list(pool)}"
        )
