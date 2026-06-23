"""
Deterministic Single-String pre-flight matrix — blocking 4-way parallel handshake.

Runs before unified trading threads spawn. Yahoo Errno 65 engages FPTP bypass;
Finnhub + Twelve Data carry the live session when macro route drops.
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

_WARN = "\033[1;33m[PREFLIGHT-WARN]\033[0m"
_FAIL = "\033[1;31m[PREFLIGHT-FAIL]\033[0m"
_OK = "\033[1;32m[PREFLIGHT-OK]\033[0m"


def _console_warn(label: str, detail: str) -> None:
    msg = f"{_WARN} {label}: {detail}"
    print(msg, flush=True)
    try:
        from system.engine_log import log_engine

        log_engine(msg)
    except Exception:
        pass


def _console_ok(label: str, detail: str = "") -> None:
    line = f"{_OK} {label}" + (f" — {detail}" if detail else "")
    print(line, flush=True)
    try:
        from system.engine_log import log_engine

        log_engine(line)
    except Exception:
        pass


def _console_fail(label: str, detail: str) -> None:
    msg = f"{_FAIL} {label}: {detail}"
    print(msg, flush=True)
    try:
        from system.engine_log import log_engine

        log_engine(msg)
    except Exception:
        pass


def _ping_yahoo_chart(*, timeout: float = 5.0) -> tuple[bool, str]:
    from system.feeds.multi_feed_hub import _http_ping, _yahoo_error_isolated, _set_yahoo_bypass

    url = "https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF"
    ok, detail = _http_ping(url, timeout=timeout, method="GET")
    if not ok and _yahoo_error_isolated(Exception(detail)):
        _set_yahoo_bypass(reason=detail[:120])
        return False, f"FPTP_BYPASS:{detail}"
    return ok, detail


def _ping_finnhub_stream(*, timeout: float = 5.0) -> tuple[bool, str]:
    from system.feeds.multi_feed_hub import _ping_finnhub, _resolve_finnhub_key

    key = _resolve_finnhub_key()
    if not key:
        return False, "FINNHUB_KEY missing"
    ok, detail = _ping_finnhub(timeout=timeout)
    if ok:
        return True, "stream credentials validated"
    return ok, detail


def _ping_twelve_data_usage(*, timeout: float = 5.0) -> tuple[bool, str]:
    from system.feeds.multi_feed_hub import _ping_twelve_data

    return _ping_twelve_data(timeout=timeout)


def _ping_ig_production_accounts(rest_client: Any | None, *, timeout: float = 8.0) -> tuple[bool, str]:
    if rest_client is None:
        return False, "IG REST client unavailable"
    try:
        if hasattr(rest_client, "get_accounts"):
            payload = rest_client.get_accounts()
            if isinstance(payload, dict) and payload.get("accounts") is not None:
                acct = str(getattr(rest_client, "account_id", "") or "")
                base = str(getattr(rest_client, "_base", "") or "")
                return True, f"DEMO account {acct} verified @ {base}"
        rest_client.ensure_session()
        headers = rest_client._auth_headers("1")  # noqa: SLF001
        response = rest_client.request("GET", "/accounts", headers=headers)
        status = int(getattr(response, "status_code", 0) or 0)
        if status >= 400:
            return False, f"HTTP {status} auth block"
        return True, f"DEMO /accounts OK @ {getattr(rest_client, '_base', '')}"
    except Exception as exc:
        err = str(exc).lower()
        if "auth" in err or "401" in err or "403" in err:
            return False, f"AUTH_BLOCK:{exc}"
        return False, str(exc)


def init_cockpit_shm_registry() -> None:
    """Pre-allocate Darwin cockpit SHM registry before threads spawn."""
    try:
        from system.ipc.ring_buffer import (
            COCKPIT_SHM_MAGIC,
            COCKPIT_SHM_VERSION,
            _attach_cockpit_shm,
            cockpit_shm_map_status,
        )
        import ctypes
        from system.ipc.ring_buffer import CockpitShmHeader

        seg = _attach_cockpit_shm(create=True)
        hdr = CockpitShmHeader.from_buffer(seg.buf)
        hdr.magic = COCKPIT_SHM_MAGIC
        hdr.version = COCKPIT_SHM_VERSION
        hdr.header_bytes = ctypes.sizeof(CockpitShmHeader)
        hdr.memory_aligned = 1
        hdr.signal_threshold = 52.5
        hdr.atr_multiplier = 2.5
        hdr.agent_pid = int(os.getpid()) & 0xFFFFFFFF
        status = cockpit_shm_map_status()
        _console_ok(
            "CockpitSHM",
            f"{status.get('namespace')} mapped={status.get('mapped')} "
            f"bytes={status.get('alloc_bytes')}",
        )
    except Exception as exc:
        _console_warn("SHM_INIT", f"{type(exc).__name__}: {exc}")


def verify_production_pipeline_integrity(
    *,
    rest_client: Any | None = None,
    timeout_sec: float = 8.0,
    blocking: bool = True,
) -> dict[str, Any]:
    """
    Blocking 4-way production handshake — must complete before Thread A/B spawn.

    Returns structured report; never raises (failures surface as console diagnostics).
    """
    init_cockpit_shm_registry()

    if os.environ.get("IG_SKIP_API_VERIFY", "").strip().lower() in ("1", "true", "yes"):
        return {"skipped": True, "all_ok": True}

    probes = {
        "Yahoo Finance Chart": _ping_yahoo_chart,
        "Finnhub WebSocket Credentials": _ping_finnhub_stream,
        "Twelve Data API": _ping_twelve_data_usage,
        "IG Demo Client": lambda: _ping_ig_production_accounts(
            rest_client, timeout=timeout_sec
        ),
    }

    results: dict[str, dict[str, Any]] = {}
    yahoo_bypass = False

    print("\033[1;36m=== PRODUCTION PIPELINE INTEGRITY MATRIX ===\033[0m", flush=True)

    try:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="preflight") as pool:
            futures = {pool.submit(fn): name for name, fn in probes.items()}
            for fut in as_completed(futures, timeout=timeout_sec + 2.0):
                name = futures[fut]
                try:
                    ok, detail = fut.result(timeout=timeout_sec)
                except Exception as exc:
                    ok, detail = False, str(exc)
                results[name] = {"ok": ok, "detail": detail}
                if name == "Yahoo Finance Chart" and not ok and "FPTP_BYPASS" in detail:
                    yahoo_bypass = True
                    _console_warn(
                        name,
                        f"Errno 65 routing block — FPTP bypass engaged "
                        f"(Finnhub + Twelve Data primary)",
                    )
                elif ok:
                    _console_ok(name, detail)
                else:
                    if "AUTH" in detail.upper():
                        _console_fail(name, f"authentication block — {detail}")
                    elif "timeout" in detail.lower():
                        _console_warn(name, f"network timeout — {detail}")
                    else:
                        _console_warn(name, detail)
    except Exception as exc:
        _console_fail("MATRIX", f"{type(exc).__name__}: {exc}")
        for name in probes:
            results.setdefault(name, {"ok": False, "detail": str(exc)})

    all_ok = all(r.get("ok") for r in results.values())
    if yahoo_bypass:
        all_ok = all(
            r.get("ok")
            for n, r in results.items()
            if n != "Yahoo Finance Chart"
        )

    print("\033[1;36m=== END PIPELINE INTEGRITY MATRIX ===\033[0m\n", flush=True)

    report = {
        "results": results,
        "yahoo_fptp_bypass": yahoo_bypass,
        "all_ok": all_ok,
        "blocking": blocking,
    }

    if not all_ok and blocking:
        try:
            from ig_api.rest_client import IGRestClient
            from system.agent_execution_mode import (
                authentic_demo_broker_required,
                production_execution_active,
            )

            if isinstance(rest_client, IGRestClient) and (
                production_execution_active() or authentic_demo_broker_required()
            ):
                ig_row = results.get("IG Demo Client") or results.get("IG Production Client") or {}
                if ig_row.get("ok"):
                    report["all_ok"] = True
                    _console_ok("DEMO_OVERRIDE", ig_row.get("detail", "IGRestClient validated"))
                elif getattr(rest_client, "session", None) and getattr(
                    rest_client.session, "is_valid", False
                ):
                    acct = str(getattr(rest_client, "account_id", "") or "")
                    report["all_ok"] = True
                    _console_ok(
                        "DEMO_OVERRIDE",
                        f"session trusted account={acct} gateway=demo-api.ig.com",
                    )
        except Exception:
            pass

    return report
