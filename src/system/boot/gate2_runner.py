"""Gate 2 — IG REST authentication and primary account hydration."""

from __future__ import annotations

import logging
import os
import time
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


def _ensure_authenticated_with_timeout(
    credentials: Any,
    *,
    timeout_sec: float = 45.0,
) -> Any:
    """Bound IG login so Gate 2 cannot wedge the boot pipeline indefinitely."""
    from system.ig_rest_session import ensure_shared_authenticated

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gate2-auth")
    future = pool.submit(ensure_shared_authenticated, credentials)
    try:
        return future.result(timeout=timeout_sec)
    except FuturesTimeoutError as exc:
        raise IGAuthError(
            f"IG login timed out after {timeout_sec:.0f}s"
        ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


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
            from system.agent_execution_mode import broker_demo_execution_required
            from system.guard.live_path_guard import is_live_production_track, mock_broker_forbidden

            if broker_demo_execution_required() and not is_live_production_track():
                holder = get_credentials_holder()
                raw = self._context.raw_config or load_raw_config_dict()
                if holder.credentials is not None:
                    self._complete_demo_deferred_auth(
                        holder.credentials,
                        raw,
                        f"{type(exc).__name__}: {exc}",
                    )
                    return

            if is_live_production_track() or (
                mock_broker_forbidden() and not broker_demo_execution_required()
            ):
                log_engine(
                    "Gate2: live PRODUCTION broker error — entering fail-closed "
                    f"network hold ({type(exc).__name__}: {exc})"
                )
                self._live_production_network_hold_until_connected()
                return
            logger.warning(
                f"[APEX FAILSAFE] Gate 2 external broker connection bypassed safely: {exc}"
            )
            log_engine(
                f"[APEX FAILSAFE] Gate 2 external broker connection bypassed safely: "
                f"{type(exc).__name__}: {exc}"
            )
            log_engine(
                "[APEX FAILSAFE] Gate 2 proceeding in sandbox mode — "
                "verify IG credentials and network if live execution required"
            )
            self._bypass_and_force_sandbox_ready_token()

    def _execute(self) -> None:
        holder = get_credentials_holder()
        raw = self._context.raw_config or load_raw_config_dict()

        if os.environ.get("IG_TEST_HARNESS", "").strip() == "1":
            self._bootstrap_mock_feed(raw)
            return

        try:
            from system.apex_runtime_mode import ApexRuntimeMode, get_apex_runtime_mode

            if get_apex_runtime_mode() is ApexRuntimeMode.HARDENED_TESTBED:
                self._bootstrap_testbed_loopback(raw)
                return
        except Exception as exc:
            from system.guard.runtime_guard import log_guarded_exception

            log_guarded_exception("gate2_runner", exc)

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

        from system.ig_rest_session import get_shared_rest_client

        rest = get_shared_rest_client(credentials)
        if broker_demo_execution_required() and not _session_valid(rest):
            self._complete_demo_deferred_auth(
                credentials,
                raw,
                "non-blocking DEMO boot",
            )
            return

        try:
            rest = _ensure_authenticated_with_timeout(credentials, timeout_sec=45.0)
        except IGAuthError as exc:
            if broker_demo_execution_required():
                self._complete_demo_deferred_auth(credentials, raw, str(exc))
                return
            raise
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

        from system.agent_execution_mode import (
            authentic_demo_broker_required,
            broker_demo_execution_required,
        )

        # DEMO/PRODUCTION: never block boot on slow IG position sync — hydrate async.
        if broker_demo_execution_required() or authentic_demo_broker_required():
            from system.boot.gate2_async_hydration import start_gate2_background_hydration

            start_gate2_background_hydration(rest, self._context, self._state)
            self.mark_gate_complete(detail="IGRestClient Armed (async hydration)")
            if self._context.rest_client is None:
                self._context.rest_client = rest
            return

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

        hydration_timeout = 8.0 if is_shadow_node() else 10.0

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

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gate2-hydrate")
        try:
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
            from system.guard.live_path_guard import is_live_production_track, mock_broker_forbidden

            if mock_broker_forbidden() or is_live_production_track():
                from system.agent_execution_mode import (
                    authentic_demo_broker_required,
                    production_execution_active,
                )

                rest = self._context.rest_client
                try:
                    from ig_api.rest_client import IGRestClient

                    if (
                        (production_execution_active() or authentic_demo_broker_required())
                        and isinstance(rest, IGRestClient)
                        and _session_valid(rest)
                    ):
                        from api.snapshot_store import set_boot_hydration

                        log_engine(
                            "Gate2: DEMO/PRODUCTION hydration timeout — authenticated IGRestClient "
                            f"retained, deferring position sync ({type(exc).__name__})"
                        )
                        set_boot_hydration([], [])
                        self._context.hydration_detail = {
                            "degraded": True,
                            "reason": f"{type(exc).__name__}: {exc}",
                        }
                        self.mark_gate_complete(
                            detail="IGRestClient Armed (hydration deferred)"
                        )
                        return
                except Exception:
                    pass
                logger.warning(
                    "[APEX FAILSAFE] Gate 2 position/order hydration failed on live "
                    f"PRODUCTION — entering network hold: {exc}"
                )
                log_engine(
                    "[APEX FAILSAFE] Gate 2 position/order hydration failed on live "
                    f"PRODUCTION — network hold engaged "
                    f"({type(exc).__name__}: {exc})"
                )
                self._live_production_network_hold_until_connected(raw)
                return
            logger.warning(
                f"[APEX FAILSAFE] Gate 2 position/order hydration bypassed safely: {exc}"
            )
            log_engine(
                f"[APEX FAILSAFE] Gate 2 position/order hydration bypassed safely: {exc}"
            )
            self._bypass_and_force_sandbox_ready_token()
            return
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        if self._context.rest_client is None:
            self._context.rest_client = rest

    def _complete_demo_deferred_auth(
        self,
        credentials: Any,
        raw: dict[str, Any],
        reason: str,
    ) -> None:
        """DEMO boot: advance G2 while IG auth/hydration retries off-thread."""
        import threading

        from api.snapshot_store import set_boot_hydration
        from system.ig_rest_session import get_shared_rest_client

        rest = get_shared_rest_client(credentials)
        self._context.rest_client = rest
        self._commit_context_config(raw)
        set_boot_hydration([], [])

        def _retry_auth_and_hydrate() -> None:
            try:
                from system.boot.gate2_async_hydration import (
                    start_gate2_background_hydration,
                )
                from system.ig_rest_session import ensure_shared_authenticated

                authed = _ensure_authenticated_with_timeout(
                    credentials, timeout_sec=90.0
                )
                self._context.rest_client = authed
                start_gate2_background_hydration(authed, self._context, self._state)
            except Exception as retry_exc:
                log_engine(
                    "Gate2-deferred: auth/hydration retry failed "
                    f"{type(retry_exc).__name__}: {retry_exc}"
                )

        threading.Thread(
            target=_retry_auth_and_hydrate,
            name="gate2-deferred-auth",
            daemon=True,
        ).start()
        self.mark_gate_complete(detail="IGRestClient Armed (auth deferred)")
        log_engine(
            f"Gate2: DEMO auth deferred — pipeline continuing ({reason})"
        )

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
        self._seed_hub_quote_source_metrics()

    def _seed_hub_quote_source_metrics(self) -> None:
        """Initialize night-matrix quote provenance slots in shared memory."""
        try:
            from system.identity.process_orchestrator import NIGHT_MATRIX_EPICS
            from system.identity.state_cache import get_live_state_cache

            cache = get_live_state_cache()
            for epic in NIGHT_MATRIX_EPICS:
                cache.update_hub_quote_source(
                    epic=epic,
                    source="ig_rest",
                    staleness_seconds=9999,
                )
        except Exception as exc:
            from system.guard.runtime_guard import log_guarded_exception

            log_guarded_exception("gate2_hub_quote_source_seed", exc)

    def _live_production_network_hold_until_connected(
        self, raw: dict[str, Any] | None = None
    ) -> None:
        """
        Infinite retry hold for live PRODUCTION — authentic IG REST only.

        MockIGRest, MockFeedEngine, and sandbox bypass tokens are never installed.
        """
        from system.guard.live_path_guard import mock_broker_forbidden

        if not mock_broker_forbidden():
            return

        config_raw = (
            raw
            if raw is not None
            else (self._context.raw_config or load_raw_config_dict())
        )
        holder = get_credentials_holder()
        credentials = holder.credentials
        if credentials is None:
            raise RuntimeError(
                "Gate2: live PRODUCTION network hold requires valid IG credentials"
            )

        validation_cfg = merge_credentials_for_validation(config_raw)

        from system.demo_guard import validate_demo_only_startup

        demo_ok, demo_msg = validate_demo_only_startup(validation_cfg)
        if not demo_ok:
            raise RuntimeError(demo_msg)

        from system.startup_pipeline import check_account_type_demo

        acct_check = check_account_type_demo(credentials)
        if not acct_check.ok:
            raise RuntimeError(acct_check.message)

        from runtime.ig_account_verify import verify_account_on_broker

        while True:
            logger.warning(
                "[APEX NETWORK] Production connection timeout. Entering fail-closed "
                "network hold. Retrying live IG broker handshake in 5 seconds..."
            )
            log_engine(
                "[APEX NETWORK] Production connection timeout. Entering fail-closed "
                "network hold. Retrying live IG broker handshake in 5 seconds..."
            )
            self._state.update_state(
                BootPhase.G2,
                28,
                "Network hold — retrying live IG broker",
                gates_dict=None,
            )
            try:
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
                self._commit_context_config(config_raw)
                log_engine(
                    f"Gate2: authenticated account {credentials.masked_account_id()} "
                    f"({credentials.account_type}) — rest_client committed to BootContext"
                )

                verify = verify_account_on_broker(rest, credentials)
                self._context.account_verify = verify
                if not verify.get("match"):
                    from runtime.ig_account_verify import (
                        session_account_matches_credentials,
                    )
                    from system.agent_execution_mode import broker_demo_execution_required

                    if broker_demo_execution_required() and session_account_matches_credentials(
                        rest, credentials
                    ):
                        verify = {**verify, "match": True}
                        self._context.account_verify = verify
                    else:
                        ids = ", ".join(
                            str(a.get("account_id") or "")
                            for a in verify.get("accounts") or []
                        )
                        raise IGAuthError(
                            f"IG account mismatch: configured "
                            f"{verify.get('configured_account_id')} "
                            f"not in broker list [{ids or 'none'}]"
                        )

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
                    f"{len(orders)} working order(s) "
                    f"(live PRODUCTION network hold cleared)"
                )
                return
            except Exception as retry_exc:
                logger.error(f"[APEX NETWORK] Handshake attempt failed: {retry_exc}")
                log_engine(
                    f"[APEX NETWORK] Handshake attempt failed: "
                    f"{type(retry_exc).__name__}: {retry_exc}"
                )
            time.sleep(5)

    def _bypass_and_force_sandbox_ready_token(self, raw: dict[str, Any] | None = None) -> None:
        """Instant local sandbox emulation — never kill :9090 on weekend outage."""
        from system.guard.live_path_guard import is_live_production_track, mock_broker_forbidden

        if mock_broker_forbidden() or is_live_production_track():
            logger.warning(
                "[APEX FAILSAFE] Gate 2 network failsafe bypass blocked on live "
                "PRODUCTION — MockIGRest forbidden; entering infinite network hold"
            )
            log_engine(
                "[APEX FAILSAFE] Gate 2 network failsafe bypass blocked on live "
                "PRODUCTION — entering infinite network hold (no mock client)"
            )
            self._live_production_network_hold_until_connected(raw)
            return

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

    def _bootstrap_testbed_loopback(self, raw: dict[str, Any]) -> None:
        """HARDENED_TESTBED — loopback replay only; zero outbound IG REST."""
        from api.snapshot_store import set_boot_hydration
        from feeder.mock_feed_engine import (
            install_mock_shared_rest,
            mock_account_verify,
            mock_hydration_detail,
        )
        from ig_api.mock_clients import MockIGRest, MockRESTConfig

        self._commit_context_config(raw)
        rest = MockIGRest(
            account_id="TESTBED-REPLAY",
            account_type="DEMO",
            mock_config=MockRESTConfig(balance=10_000.0),
        )
        rest.login()
        install_mock_shared_rest(rest)
        self._context.rest_client = rest
        self._context.account_verify = {
            **mock_account_verify("TESTBED-REPLAY"),
            "match": True,
            "bypass": True,
            "reason": "hardened_testbed_loopback",
        }
        self._context.hydration_detail = mock_hydration_detail()
        set_boot_hydration([], [])
        self.mark_gate_complete(detail="Testbed Loopback Ready (no IG REST)")
        replay = os.environ.get("IG_HISTORICAL_REPLAY", "").strip()
        log_engine(
            "Gate2: HARDENED_TESTBED loopback armed — REST blocked, "
            f"replay={replay or 'pending'}"
        )

    def _bootstrap_mock_feed(self, raw: dict[str, Any]) -> None:
        from system.guard.live_path_guard import enforce_live_production_no_mock

        enforce_live_production_no_mock("gate2_mock_feed_bootstrap")
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
