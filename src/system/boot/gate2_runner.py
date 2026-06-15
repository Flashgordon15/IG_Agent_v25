"""Gate 2 — IG REST authentication and primary account hydration."""

from __future__ import annotations

from typing import Any

from ig_api.exceptions import IGAPIError, IGAuthError
from system.boot.context import BootContext
from system.boot.preflight_helpers import merge_credentials_for_validation, load_raw_config_dict
from system.config import Config
from system.config_validator import apply_config_defaults
from system.credentials_holder import get_credentials_holder
from system.engine_log import log_engine
from system.system_state import BootPhase, GateStatus, SystemState, get_system_state


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

    Runs on the BootCoordinator worker thread. Failures stall the pipeline at
    ``FAILED`` without re-raising (background thread stays alive for dashboard).
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
            message = self._format_error(exc)
            log_engine(f"Gate2 FATAL: {message}")
            self._state.mark_gate_failed(
                "G2",
                error=message,
                detail="IG REST authentication or hydration failed",
            )

    def _execute(self) -> None:
        holder = get_credentials_holder()
        credentials = holder.credentials
        if credentials is None:
            raise IGAuthError("Credentials not loaded — check credentials.json")

        raw = self._context.raw_config or load_raw_config_dict()
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

        from runtime.ig_account_verify import verify_account_on_broker

        verify = verify_account_on_broker(rest, credentials)
        self._context.account_verify = verify
        if not verify.get("match"):
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

        self._state.update_state(
            BootPhase.G2,
            35,
            "Broker & State Hydrated",
            gates_dict=None,
            hydration={
                "positions_synced": True,
                "orders_synced": True,
            },
        )
        log_engine(
            f"Gate2: hydration complete — {open_count} open position(s), "
            f"{len(orders)} working order(s)"
        )
        if self._context.rest_client is None:
            self._context.rest_client = rest

    def _commit_context_config(self, raw: dict[str, Any]) -> None:
        """Ensure Gate 3+ can read validated config from the shared BootContext."""
        if self._context.config is not None:
            return
        if self._context.raw_config is None:
            self._context.raw_config = raw
        merged = apply_config_defaults(dict(self._context.raw_config or raw))
        self._context.config = Config(_data=merged)

    @staticmethod
    def _format_error(exc: Exception) -> str:
        if isinstance(exc, IGAuthError):
            return f"IG REST Authentication Failed: {exc}"
        if isinstance(exc, IGAPIError):
            code = getattr(exc, "status_code", None)
            suffix = f" (HTTP {code})" if code else ""
            return f"IG REST API Error: {exc}{suffix}"
        if isinstance(exc, TimeoutError):
            return f"IG REST Authentication Failed: connection timeout ({exc})"
        return f"IG REST Authentication Failed: {type(exc).__name__}: {exc}"
