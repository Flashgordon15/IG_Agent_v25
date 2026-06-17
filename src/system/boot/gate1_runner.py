"""Gate 1 — lightweight preflight and API-bind readiness (<2s)."""

from __future__ import annotations

from typing import Any

from system.boot.exceptions import Gate1FatalError


class Gate1Runner:
    """
    Synchronous preflight executed before uvicorn binds :8080.

    Ports ``main.run_preflight`` + log rotation + port check without heavy cleanup.
    Import trading/ML/DB stacks only inside ``_execute()``.
    """

    EXIT_LOCK = 2
    EXIT_CONFIG = 3
    EXIT_INSTANCE = 4

    def __init__(
        self,
        state: Any | None = None,
        context: Any | None = None,
    ) -> None:
        from system.boot.context import BootContext
        from system.system_state import get_system_state

        self._state = state or get_system_state()
        self._context = context or BootContext()

    @property
    def context(self) -> Any:
        return self._context

    def run(self) -> None:
        from system.system_state import BootPhase, GateStatus

        self._state.update_state(
            BootPhase.G1,
            0,
            "System Booting",
            gates_dict={
                "G1": {"status": GateStatus.RUNNING, "detail": "Preflight checks…"},
            },
        )

        try:
            self._execute()
        except Gate1FatalError:
            raise
        except Exception as exc:
            self._fail(
                f"Gate 1 preflight failed: {type(exc).__name__}: {exc}",
                exit_code=3,
            )
            raise Gate1FatalError(str(exc), exit_code=3) from exc

        self._state.update_state(
            BootPhase.G1,
            10,
            "API Online",
            gates_dict={
                "G1": {"status": GateStatus.COMPLETE, "detail": "Preflight passed"},
                "G2": {"status": GateStatus.PENDING, "detail": ""},
                "G3": {"status": GateStatus.PENDING, "detail": ""},
                "G4": {"status": GateStatus.PENDING, "detail": ""},
                "G5": {"status": GateStatus.PENDING, "detail": ""},
            },
        )
        from system.engine_log import log_engine

        log_engine("Gate1: preflight complete — ready for API bind")

    def _execute(self) -> None:
        from system.env_loader import load_dotenv, prepare_boot_env

        load_dotenv()
        prepare_boot_env()

        from system.app_identity import APP_DISPLAY_NAME
        from system.boot.preflight_helpers import (
            check_port_available,
            config_path,
            is_benign_startup_lock_failure,
            load_raw_config_dict,
            merge_credentials_for_validation,
            rotate_oversized_logs,
        )
        from system.config import Config
        from system.config_validator import (
            apply_config_defaults,
            emergency_stop_lock_present,
            validate_config,
        )
        from system.credentials_holder import bootstrap_credentials
        from system.engine_log import log_engine
        from system.instance_lock import acquire_instance_lock
        from system.paths import logs_dir

        logs_dir().mkdir(parents=True, exist_ok=True)
        rotate_oversized_logs()

        if emergency_stop_lock_present():
            self._fail(
                f"{APP_DISPLAY_NAME}: emergency_stop.lock present — delete it to restart.",
                exit_code=self.EXIT_LOCK,
            )
            raise Gate1FatalError("emergency_stop.lock present", exit_code=self.EXIT_LOCK)

        if not check_port_available():
            self._fail(
                f"{APP_DISPLAY_NAME}: port 8080 is already in use — stop the other process first.",
                exit_code=self.EXIT_INSTANCE,
            )
            raise Gate1FatalError("port 8080 already in use", exit_code=self.EXIT_INSTANCE)

        try:
            raw = load_raw_config_dict()
            self._context.raw_config = raw
        except Exception as exc:
            self._fail(
                f"{APP_DISPLAY_NAME}: config load failed: {exc}",
                exit_code=self.EXIT_CONFIG,
            )
            raise Gate1FatalError(f"config load failed: {exc}", exit_code=self.EXIT_CONFIG)

        validation_cfg = merge_credentials_for_validation(raw)
        valid, messages = validate_config(validation_cfg)
        if not valid:
            err = next(
                (line for line in messages if line.startswith("ERROR:")),
                "config validation failed",
            )
            self._fail(f"{APP_DISPLAY_NAME}: {err}", exit_code=self.EXIT_CONFIG)
            raise Gate1FatalError(err, exit_code=self.EXIT_CONFIG)

        try:
            from system.demo_guard import validate_demo_only_startup

            demo_ok, demo_msg = validate_demo_only_startup(validation_cfg)
            if not demo_ok:
                self._fail(f"{APP_DISPLAY_NAME}: {demo_msg}", exit_code=self.EXIT_CONFIG)
                raise Gate1FatalError(demo_msg, exit_code=self.EXIT_CONFIG)
            log_engine(f"Gate1: {demo_msg}")
        except Gate1FatalError:
            raise
        except Exception as exc:
            log_engine(f"Gate1: demo guard error (continuing): {type(exc).__name__}: {exc}")

        ok, msg = acquire_instance_lock()
        if not ok:
            try:
                if not is_benign_startup_lock_failure(msg):
                    from system.watchdog_banner import record_startup_failure

                    record_startup_failure(msg)
            except Exception:
                pass
            self._fail(f"{APP_DISPLAY_NAME}: {msg}", exit_code=self.EXIT_INSTANCE)
            raise Gate1FatalError(msg, exit_code=self.EXIT_INSTANCE)

        try:
            from system.watchdog_banner import record_startup_success

            record_startup_success()
        except Exception:
            pass

        holder = bootstrap_credentials()
        if holder.credentials:
            log_engine(
                f"Gate1: credentials loaded ({holder.credentials.account_type})"
            )
        else:
            log_engine(f"Gate1: credentials not ready — {holder.status.error}")

        raw = self._context.raw_config
        if raw is None:
            self._fail(
                f"{APP_DISPLAY_NAME}: config not loaded",
                exit_code=self.EXIT_CONFIG,
            )
            raise Gate1FatalError("config not loaded", exit_code=self.EXIT_CONFIG)
        merged = apply_config_defaults(raw)
        self._context.config = Config(_data=merged)
        log_engine(f"Gate1: config loaded from {config_path()}")

    def _fail(self, message: str, *, exit_code: int) -> None:
        from system.engine_log import log_engine

        log_engine(f"Gate1 FATAL: {message}")
        self._state.mark_gate_failed("G1", error=message, detail="Preflight failed")
