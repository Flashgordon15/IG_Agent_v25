"""v32 dual-port engine CLI — parse argv and export process env before heavy boot."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from system.engine_lane import (
    DEFAULT_ACCOUNT_CFD,
    DEFAULT_ACCOUNT_SB,
    ENGINE_CFD_SNIPER,
    ENGINE_ORIGIN_CFD,
    ENGINE_ORIGIN_SB,
    ENGINE_SB_SENTINEL,
)

_VALID_ORIGINS = frozenset({ENGINE_ORIGIN_CFD, ENGINE_ORIGIN_SB})
_ORIGIN_TO_ENGINE = {
    ENGINE_ORIGIN_CFD: ENGINE_CFD_SNIPER,
    ENGINE_ORIGIN_SB: ENGINE_SB_SENTINEL,
}
_ORIGIN_TO_STATE_SUBDIR = {
    ENGINE_ORIGIN_CFD: "state_cfd",
    ENGINE_ORIGIN_SB: "state_sb",
}


@dataclass(frozen=True)
class EngineCliArgs:
    port: int | None
    account_id: str | None
    origin: str | None
    dual_port_mode: bool

    @property
    def engine_id(self) -> str | None:
        if self.origin:
            return _ORIGIN_TO_ENGINE.get(self.origin)
        return None

    @property
    def state_subdir(self) -> str | None:
        if self.origin:
            return _ORIGIN_TO_STATE_SUBDIR.get(self.origin)
        return None


def _normalize_origin(raw: str | None) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip().upper()
    return val or None


def _normalize_account(raw: str | None) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip().upper()
    return val or None


def build_engine_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--account-id", "--account_id", dest="account_id", default=None)
    parser.add_argument("--origin", default=None)
    return parser


def parse_engine_cli(argv: list[str] | None = None) -> EngineCliArgs:
    """Parse v32 dual-port flags without importing ``main``."""
    known, _unknown = build_engine_cli_parser().parse_known_args(
        list(argv if argv is not None else sys.argv[1:])
    )
    cli_port = known.port
    cli_account = _normalize_account(known.account_id)
    cli_origin = _normalize_origin(known.origin)

    cli_provided = sum(1 for x in (cli_port, cli_account, cli_origin) if x is not None)
    if cli_provided not in (0, 3):
        raise SystemExit(
            "v32 dual-port: --port, --account-id, and --origin must all be provided together"
        )

    port = cli_port
    account_id = cli_account
    origin = cli_origin

    if port is None:
        env_port = os.environ.get("IG_API_PORT", "").strip()
        if env_port.isdigit():
            port = int(env_port)
    if account_id is None:
        account_id = _normalize_account(os.environ.get("IG_ACCOUNT_ID"))
    if origin is None:
        origin = _normalize_origin(os.environ.get("IG_ENGINE_ORIGIN"))

    provided = sum(1 for x in (port, account_id, origin) if x is not None)
    dual = provided == 3

    if origin and origin not in _VALID_ORIGINS:
        raise SystemExit(
            f"v32 dual-port: invalid --origin {origin!r} "
            f"(expected {ENGINE_ORIGIN_CFD} or {ENGINE_ORIGIN_SB})"
        )

    if dual and port is not None and port <= 0:
        raise SystemExit("v32 dual-port: --port must be a positive integer")

    return EngineCliArgs(
        port=port,
        account_id=account_id,
        origin=origin,
        dual_port_mode=dual,
    )


def apply_engine_cli_env(cli: EngineCliArgs) -> None:
    """Export env vars consumed by paths, credentials, and engine lane resolution."""
    if not cli.dual_port_mode:
        return
    assert cli.port is not None
    assert cli.account_id is not None
    assert cli.origin is not None

    os.environ["IG_V32_DUAL_PORT"] = "1"
    os.environ["IG_SESSION_REGISTRY"] = "1"
    os.environ["IG_API_PORT"] = str(cli.port)
    os.environ["PORT"] = str(cli.port)
    os.environ["IG_ACCOUNT_ID"] = cli.account_id
    os.environ["IG_ACCOUNT_SCOPE"] = f"ig:{cli.account_id}"
    os.environ["IG_ENGINE_ORIGIN"] = cli.origin
    if cli.state_subdir:
        os.environ["IG_ENGINE_STATE_SUBDIR"] = cli.state_subdir
    if cli.engine_id:
        os.environ["IG_ACTIVE_ENGINE_ID"] = cli.engine_id

    from kernel.ring_buffer import resolve_dual_port_shm_lane_token, resolve_position_ring_shm_name

    os.environ.setdefault("IG_SHM_RING_CREATE", "1")
    lane = resolve_dual_port_shm_lane_token()
    if lane:
        os.environ["IG_SHM_RING_NAME"] = f"ig_agent_v33_shm_{lane}"
        os.environ["IG_COCKPIT_SHM_NAME"] = f"ig_agent_v33_cockpit_{lane}"
    else:
        os.environ["IG_SHM_RING_NAME"] = f"ig_agent_v33_shm_{cli.account_id}"
        os.environ["IG_COCKPIT_SHM_NAME"] = f"ig_agent_v33_cockpit_{cli.account_id}"
    # Ensure resolver agrees with explicit env (port/account isolation contract).
    assert resolve_position_ring_shm_name() == os.environ["IG_SHM_RING_NAME"]

    expected_account = (
        DEFAULT_ACCOUNT_CFD if cli.origin == ENGINE_ORIGIN_CFD else DEFAULT_ACCOUNT_SB
    )
    if cli.account_id != expected_account:
        os.environ["IG_V32_ACCOUNT_OVERRIDE"] = "1"


def bootstrap_engine_cli(argv: list[str] | None = None) -> EngineCliArgs:
    cli = parse_engine_cli(argv)
    apply_engine_cli_env(cli)
    try:
        from system.core_affinity import pin_current_process_to_engine

        pin_current_process_to_engine(cli.origin)
    except Exception:
        pass
    return cli


def reapply_engine_cli_env(cli: EngineCliArgs) -> None:
    """Re-assert CLI env after dotenv load so flags beat ``.env`` defaults."""
    apply_engine_cli_env(cli)


def is_v32_dual_port_mode() -> bool:
    return os.environ.get("IG_V32_DUAL_PORT", "").strip() == "1"
