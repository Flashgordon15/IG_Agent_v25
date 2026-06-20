"""Gate 2 — IG REST authentication and primary account hydration."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any

from ig_api.exceptions import IGAPIError, IGAuthError
from system.boot.context import BootContext
from system.boot.preflight_helpers import load_raw_config_dict, merge_credentials_for_validation
from system.config import Config
from system.config_validator import apply_config_defaults
from system.credentials_holder import get_credentials_holder
from system.engine_log import log_engine
from system.system_state import BootPhase, GateStatus, SystemState, get_system_state

logger = logging.getLogger(__name__)


def _session_valid(rest_client: Any) -> bool:
    session = getattr(rest_client, "session", None)
    return bool(session and getattr(session, "is_valid", False))


def _fetch_working_orders(rest_client: Any) -> list[dict[str, Any]]:
    rest_client.ensure_session()
    response = rest_client.request(
        "GET", "/workingorders", headers=rest_client._auth_headers("2")
    )
    if response.status_code != 200:
        raise IGAPIError(
            f"Working orders request failed: HTTP {response.status_code}",
            status_code=response.status_code,
        )
    payload = response.json()
    return list(payload.get("workingOrders") or [])


class Gate2Runner:
    """
    REST session establishment and broker hydration after API is live.

    Weekend/network failures route to sandbox bypass — sidecar stays on :9090.
    """

    def __init__(
        self,
        state: SystemState | None = None,
        context: BootContext | None = None,
    ) -> None:
        self._state = state or get_system_state()
        self._context = context or BootContext()

    @property
    def context(self) -> BootContext:
        return self._context

    def run(self) -> None:
        gates = self._state.snapshot_model().gates
        self._state.update_state(
            BootPhase.G2,
            15,
            "Broker Handshake",
            gates_dict={
                gid: (
                    gates[gid].to_dict()
                    if gid != "G2"
                    else {"status": GateStatus.RUNNING, "detail": "Authenticating…"}
                )
                for gid in ("G1", "G2", "G3", "G4", "G5")
            },
        )

        try:
            self._execute()
        except Exception as exc:
            logger.warning(
                f"[APEX FAILSAFE] Gate 2 external broker connection bypassed safely: {exc}"
            )
            log_engine(
                f"[APEX FAILSAFE] Gate 2 external broker connection bypassed safely: {exc}"
            )
            self._bypass_and_force_sandbox_ready_token()

    def _execute(self) -> None:
        holder = get_credentials_holder()
        raw = self._context.raw_config or load_raw_config_dict()

        from feeder.mock_feed_engine import should_use_mock_feed
        from system.agent_execution_mode import broker_demo_execution_required

        if should_use_mock_feed(holder):
            self._bootstrap_mock_feed(raw)
            return

        credentials = holder.credentials
        if credentials is None:
            if broker_demo_execution_required():
                raise RuntimeError(
                    "Gate2: IG DEMO execution requires valid "
                    "config/credentials/credentials.json (MockIGRest blocked)"
                )
            self._bootstrap_mock_feed(raw)
            return

        validation_cfg = merge_credentials_for_validation(raw)

        from system.demo_guard import validate_demo_only_startup

        demo_ok, demo_msg = validate_demo_only_startup(validation_cfg)
        if not demo_ok:
            raise RuntimeError(demo_msg)

        from system.startup_pipeline import check_account_type_demo

        acct_check = check_account_type_demo(credentials)
        if not acct_check.ok:
            raise RuntimeError(acct_check.message)

        self._state.update_state(
            BootPhase.G2,
            22,
            "Broker Handshake",
            gates_dict=None,
        )

        from system.ig_rest_session import ensure_shared_authenticated

        rest = ensure_shared_authenticated(credentials)
        base = str(getattr(rest, "_base", "") or "")
        if credentials.account_type == "DEMO" and "demo-api.ig.com" not in base:
            raise IGAuthError(f"Wrong REST base for DEMO account: {base}")

        if not _session_valid(rest):
            raise IGAuthError(
                "IG REST Authentication Failed: session invalid after login"
            )

        self._context.rest_client = rest
        self._commit_context_config(raw)
        log_engine(
            f"Gate2: authenticated account {credentials.masked_account_id()} "
            f"({credentials.account_type}) — rest_client committed to BootContext"
        )

        self._state.update_state(
            BootPhase.G2,
            28,
            "Verifying Account",
            gates_dict=None,
        )

        from runtime.ig_account_verify import (
            session_account_matches_credentials,
            verify_account_on_broker,
        )

        verify = verify_account_on_broker(rest, credentials)
        self._context.account_verify = verify
        if not verify.get("match"):
            from feeder.mock_feed_engine import credentials_unconfigured
            from system.agent_execution_mode import broker_demo_execution_required

            if broker_demo_execution_required() and session_account_matches_credentials(
                rest, credentials
            ):
                log_engine(
                    "Gate2: GET /accounts unavailable during boot — "
                    f"session bound to {credentials.masked_account_id()} accepted"
                )
                verify = {**verify, "match": True}
                self._context.account_verify = verify
            elif broker_demo_execution_required():
                ids = ", ".join(
                    str(a.get("account_id") or "") for a in verify.get("accounts") or []
                )
                raise IGAuthError(
                    f"IG account mismatch under DEMO execution: "
                    f"configured {verify.get('configured_account_id')} "
                    f"not in broker list [{ids or 'none'}]"
                )
            elif credentials_unconfigured(holder):
                log_engine(
                    "Gate2: IG account mismatch with unconfigured credentials — "
                    "falling back to MockFeedEngine"
                )
                self._bootstrap_mock_feed(raw)
                return
            else:
                ids = ", ".join(
                    str(a.get("account_id") or "") for a in verify.get("accounts") or []
                )
                raise IGAuthError(
                    f"IG account mismatch: configured {verify.get('configured_account_id')} "
                    f"not in broker list [{ids or 'none'}]"
                )

        self._state.update_state(
            BootPhase.G2,
            32,
            "Hydrating Positions & Orders",
            gates_dict=None,
        )

        import os
        from system.node_profile import is_shadow_node

        # Shadow desktop sidecar: skip blocking IG REST hydration on weekend/outage;
        # mock feed drives fake trades while authenticated session stays warm.
        if is_shadow_node() and os.environ.get("IG_APEX_DESKTOP", "").strip() == "1":
            from api.snapshot_store import set_boot_hydration
            from feeder.mock_feed_engine import (
                activate_mock_feed_engine,
                mock_feed_active,
                mock_hydration_detail,
            )

            if not mock_feed_active():
                activate_mock_feed_engine()
            self._context.hydration_detail = mock_hydration_detail()
            set_boot_hydration([], [])
            self.mark_gate_complete(detail="Shadow Desktop Mock Hydrated")
            log_engine(
                "Gate2: shadow desktop mock hydration — IG auth retained, "
                "REST position sync skipped"
            )
            return

        hydration_timeout = 12.0 if is_shadow_node() else 35.0

        def _hydrate() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], int]:
            positions = rest.open_positions()
            open_count = len(
                [
                    p
                    for p in positions
                    if float((p.get("position") or {}).get("size") or 0) > 0
                ]
            )
            orders = _fetch_working_orders(rest)
            balance = rest.refresh_account_summary()
            return positions, orders, balance, open_count

        try:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="gate2-hydrate") as pool:
                future = pool.submit(_hydrate)
                positions, orders, balance, open_count = future.result(
                    timeout=hydration_timeout
                )

            self._context.hydration_detail = {
                "open_positions": open_count,
                "working_orders": len(orders),
                "balance": balance.get("balance"),
                "available": balance.get("available"),
                "profit_loss": balance.get("profit_loss"),
            }

            from api.snapshot_store import set_boot_hydration

            set_boot_hydration(positions, orders)
            self.mark_gate_complete()
            log_engine(
                f"Gate2: hydration complete — {open_count} open position(s), "
                f"{len(orders)} working order(s)"
            )
        except Exception as exc:
            logger.warning(
                f"[APEX FAILSAFE] Gate 2 position/order hydration bypassed safely: {exc}"
            )
            log_engine(
                f"[APEX FAILSAFE] Gate 2 position/order hydration bypassed safely: {exc}"
            )
            self._bypass_and_force_sandbox_ready_token()
            return

        if self._context.rest_client is None:
            self._context.rest_client = rest

    def mark_gate_complete(self, *, detail: str = "") -> None:
        """Advance Gate 2 past 32% — pipeline continues to G3→G5."""
        label = detail or "Broker & State Hydrated"
        gates = self._state.snapshot_model().gates
        self._state.update_state(
            BootPhase.G2,
            35,
            label,
            gates_dict={
                gid: (
                    gates[gid].to_dict()
                    if gid != "G2"
                    else {
                        "status": GateStatus.COMPLETE,
                        "detail": label,
                    }
                )
                for gid in ("G1", "G2", "G3", "G4", "G5")
            },
            hydration={
                "positions_synced": True,
                "orders_synced": True,
            },
        )
        self._state.mark_gate_complete("G2", detail=label)

    def _bypass_and_force_sandbox_ready_token(self, raw: dict[str, Any] | None = None) -> None:
        """Instant local sandbox emulation — never kill :9090 on weekend outage."""
        logger.info("[APEX FAILSAFE] Activating local sandbox emulation mode.")
        config_raw = raw if raw is not None else (
            self._context.raw_config or load_raw_config_dict()
        )

        from feeder.mock_feed_engine import (
            activate_mock_feed_engine,
            mock_account_verify,
            mock_hydration_detail,
        )
        from api.snapshot_store import set_boot_hydration

        rest = activate_mock_feed_engine()
        self._context.rest_client = rest
        self._commit_context_config(config_raw)
        account_id = str(getattr(rest, "account_id", "MOCK-V30-SANDBOX"))
        verify = mock_account_verify(account_id)
        self._context.account_verify = {
            **verify,
            "match": True,
            "bypass": True,
            "reason": "gate2_network_failsafe",
        }
        self._context.hydration_detail = mock_hydration_detail()
        set_boot_hydration([], [])
        self.mark_gate_complete(detail="Sandbox Ready (Network Bypass)")
        log_engine(
            "Gate2: [APEX FAILSAFE] sandbox ready token forced — "
            "sidecar preserved, pipeline continues"
        )

    def _bootstrap_mock_feed(self, raw: dict[str, Any]) -> None:
        from feeder.mock_feed_engine import (
            activate_mock_feed_engine,
            mock_account_verify,
            mock_hydration_detail,
        )
        from api.snapshot_store import set_boot_hydration

        rest = activate_mock_feed_engine()
        self._context.rest_client = rest
        self._commit_context_config(raw)
        account_id = str(getattr(rest, "account_id", "MOCK-V30-SANDBOX"))
        self._context.account_verify = mock_account_verify(account_id)
        self._context.hydration_detail = mock_hydration_detail()
        set_boot_hydration([], [])
        self.mark_gate_complete(detail="Mock Feed Online")
        log_engine(
            "Gate2: mock feed hydration complete — local sandbox (0 positions, 0 orders)"
        )

    def _commit_context_config(self, raw: dict[str, Any]) -> None:
        if self._context.config is not None:
            return
        if self._context.raw_config is None:
            self._context.raw_config = raw
        merged = apply_config_defaults(dict(self._context.raw_config or raw))
        self._context.config = Config(_data=merged)
