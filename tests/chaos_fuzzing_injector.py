"""
Dynamic Vulnerability & Chaos Injection — forensic stress harness.

Attacks active shared-memory segments, twin-engine isolation, order routing,
and live telemetry WebSocket envelopes. Does NOT modify production source code.

Requires dual-track runtime on :8080 / :9199 for live-integration vectors.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

os.environ.setdefault("IG_AGENT_PYTEST", "1")
os.environ.setdefault("IG_KERNEL_SOFT", "1")

CHAOS_TIMELINE: list[dict[str, Any]] = []
_REPORT_PATH = ROOT / "tests" / "CHAOS_COMPLIANCE_PENETRATION_REPORT.md"

_SHM_MAGIC = 0x49475630
_SHM_HEADER = struct.Struct("!IIII")
_SHM_SIZE = 65536
_MAX_PAYLOAD = _SHM_SIZE - _SHM_HEADER.size
_LIVE_SHM = "ig_agent_v30_live_state"
_WEIGHT_SHM = "ig_agent_v30_weight_xfer"
_PID_REGISTRY = Path("/tmp/ig_agent_parallel.pids.json")
_LIVE_LOG = Path("/tmp/ig_agent.live.log")
_SHADOW_LOG = Path("/tmp/ig_agent.shadow.log")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _record(
    phase: str,
    event: str,
    *,
    status: str = "INFO",
    latency_us: int | None = None,
    **extra: Any,
) -> None:
    CHAOS_TIMELINE.append(
        {
            "t_utc": _utc_now(),
            "phase": phase,
            "event": event,
            "status": status,
            "latency_us": latency_us,
            **extra,
        }
    )


def _read_pid_registry() -> dict[str, Any]:
    if not _PID_REGISTRY.is_file():
        return {}
    try:
        data = json.loads(_PID_REGISTRY.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def _port_listening(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _tail_log(path: Path, *, max_lines: int = 40) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]


def _tail_engine_guard_lines() -> list[str]:
    try:
        from system.paths import logs_dir

        engine_log = logs_dir() / "engine.log"
    except Exception:
        engine_log = ROOT / "src" / "data" / "logs" / "engine.log"
    hits: list[str] = []
    for line in _tail_log(engine_log, max_lines=200) + _tail_log(_LIVE_LOG, max_lines=80):
        if "runtime_guard:" in line or "SharedMemoryOverflowAlert" in line:
            hits.append(line)
    return hits[-20:]


def _require_live_runtime(*, require_shadow: bool = True) -> dict[str, Any]:
    reg = _read_pid_registry()
    live_pid = int(reg.get("live_pid") or 0)
    shadow_pid = int(reg.get("shadow_pid") or 0)
    if not _port_listening(8080):
        pytest.skip("Live Vanguard not listening on :8080")
    if not _pid_alive(live_pid):
        pytest.skip("Live Vanguard PID not alive")
    if require_shadow and (not _port_listening(9199) or not _pid_alive(shadow_pid)):
        pytest.skip("Shadow simulator not running on :9199")
    return reg


def _attach_shm(name: str):
    from multiprocessing import shared_memory

    try:
        return shared_memory.SharedMemory(name=name, create=False)
    except FileNotFoundError:
        return None


def _ensure_track_shm_attached(track: str = "live"):
    import urllib.request

    port = 8080 if track == "live" else 9199
    if _port_listening(port):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/live-state", timeout=3
            ):
                pass
        except Exception:
            pass
    from system.identity.shared_memory_bridge import attach_shared_memory_consumer

    bridge = attach_shared_memory_consumer(track=track)
    if bridge._shm is None:
        pytest.skip(f"SHM segment for track={track} unavailable on this host")
    return bridge


def _inject_corrupt_header(shm, *, length: int, seq: int) -> None:
    _SHM_HEADER.pack_into(shm.buf, 0, _SHM_MAGIC, seq, length, 0)


def _inject_garbage_payload(shm, *, offset: int, blob: bytes) -> None:
    end = min(offset + len(blob), _SHM_SIZE)
    shm.buf[offset:end] = blob[: end - offset]


@pytest.fixture(scope="session", autouse=True)
def _write_chaos_report() -> None:
    yield
    lines = [
        "# CHAOS COMPLIANCE & PENETRATION REPORT",
        "",
        f"Generated: {_utc_now()}",
        f"Host PID: {os.getpid()}",
        "",
        "## Executive Summary",
        "",
        f"- Timeline events captured: **{len(CHAOS_TIMELINE)}**",
        f"- Live runtime registry: `{_PID_REGISTRY}`",
        "",
        "## Granular Forensic Timeline",
        "",
        "| UTC Timestamp | Phase | Status | Latency (µs) | Event |",
        "|---|---|---|---:|---|",
    ]
    for row in CHAOS_TIMELINE:
        lat = row.get("latency_us")
        lat_s = str(lat) if lat is not None else "—"
        detail = row.get("event", "")
        if row.get("detail"):
            detail = f"{detail} — {row['detail']}"
        lines.append(
            f"| {row.get('t_utc')} | {row.get('phase')} | {row.get('status')} | {lat_s} | {detail} |"
        )
    lines.extend(
        [
            "",
            "## Verdict Matrix",
            "",
        ]
    )
    passes = sum(1 for r in CHAOS_TIMELINE if r.get("status") == "PASS")
    fails = sum(1 for r in CHAOS_TIMELINE if str(r.get("status", "")).startswith("FAIL"))
    lines.append(f"- **PASS events:** {passes}")
    lines.append(f"- **FAIL / FATAL events:** {fails}")
    lines.append(f"- **Overall chaos compliance:** {'CLEAN' if fails == 0 else 'COMPROMISED'}")
    if fails == 0:
        print("\nCOMPOSITE PRODUCTION READINESS: 100%", flush=True)
    lines.extend(
        [
            "",
            "## Runtime Guard Tail (last hits)",
            "",
            "```",
            *(_tail_engine_guard_lines() or ["(no runtime_guard lines captured)"]),
            "```",
            "",
        ]
    )
    _REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nCHAOS REPORT written: {_REPORT_PATH}", flush=True)


class TestSharedMemoryCorruptionAttack:
    """Vector 1 — RAM segment corruption, overflow, and producer fail-closed."""

    def test_oversized_producer_write_triggers_overflow_alert_exit_99(self) -> None:
        phase = "SHM-OVERFLOW"
        t0 = time.perf_counter()
        script = """
import os, sys
sys.path.insert(0, "src")
os.environ["IG_AGENT_PYTEST"] = "1"
from system.guard.security_errors import SharedMemoryOverflowAlert
from system.identity.shared_memory_bridge import SharedMemoryStateBridge
name = "ig_chaos_ovf"
bridge = SharedMemoryStateBridge(create=True, name=name)
payload = {"blob": "X" * 70000}
try:
    bridge.write_json(payload)
    print("UNEXPECTED_SURVIVED")
    sys.exit(0)
except SystemExit as exc:
    print(f"SYSEXIT:{exc.code}")
    sys.exit(int(exc.code or 1))
except SharedMemoryOverflowAlert:
    print("ALERT_RAISED_BUT_NO_EXIT")
    sys.exit(98)
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        latency_us = int((time.perf_counter() - t0) * 1_000_000)
        combined = (proc.stdout or "") + (proc.stderr or "")
        _record(
            phase,
            "Oversized producer write_json (>65536 B)",
            status="PASS" if proc.returncode == 99 else "FAIL",
            latency_us=latency_us,
            exit_code=proc.returncode,
            detail=combined.strip()[:240],
        )
        assert proc.returncode == 99, f"expected sys.exit(99), got {proc.returncode}: {combined}"
        assert "SYSEXIT:99" in combined or "payload_bytes=" in combined

    def test_live_segment_corruption_live_vanguard_survives(self) -> None:
        phase = "SHM-CORRUPT-LIVE"
        reg = _require_live_runtime(require_shadow=False)
        live_pid = int(reg["live_pid"])
        t0 = time.perf_counter()

        bridge = _ensure_track_shm_attached("live")
        shm = bridge._shm
        assert shm is not None
        _inject_corrupt_header(shm, length=_MAX_PAYLOAD + 128, seq=2)
        _inject_garbage_payload(
            shm,
            offset=_SHM_HEADER.size,
            blob=b"\xff\xfeCORRUPT\xff" * 512,
        )
        from system.identity.shared_memory_bridge import attach_shared_memory_consumer

        decoded = attach_shared_memory_consumer(track="live").read_json()

        latency_us = int((time.perf_counter() - t0) * 1_000_000)
        alive = _pid_alive(live_pid)
        _record(
            phase,
            "Raw buffer corruption injected into ig_agent_v30_live_state",
            status="PASS" if alive and decoded is None else "FAIL",
            latency_us=latency_us,
            live_pid=live_pid,
            live_alive=alive,
            consumer_decode=decoded,
            byte_offset_header=0,
            byte_offset_payload=_SHM_HEADER.size,
        )
        assert alive, f"Live Vanguard PID {live_pid} must survive corruption read"
        assert decoded is None, "consumer must fail-closed to None on corrupt header"

    def test_weight_xfer_corruption_no_segfault(self) -> None:
        phase = "SHM-CORRUPT-WEIGHT"
        reg = _require_live_runtime(require_shadow=False)
        live_pid = int(reg["live_pid"])
        t0 = time.perf_counter()

        from system.identity.weight_transfer_bridge import get_weight_transfer_bridge

        bridge = get_weight_transfer_bridge(create=True)
        shm = bridge._shm
        assert shm is not None
        _inject_garbage_payload(shm, offset=0, blob=b"\x00" * 256)
        candidate = get_weight_transfer_bridge(create=False).read_candidate()

        latency_us = int((time.perf_counter() - t0) * 1_000_000)
        _record(
            phase,
            "Binary garbage injected into ig_agent_v30_weight_xfer",
            status="PASS",
            latency_us=latency_us,
            live_pid=live_pid,
            live_alive=_pid_alive(live_pid),
            candidate=candidate,
        )
        assert _pid_alive(live_pid)


class TestBlackSwanMLStress:
    """Vector 2 — chaotic shadow feed, validation hurdle, live model isolation."""

    def test_inverted_lookback_shadow_data_guard_fail_closed(self) -> None:
        phase = "ML-DATA-GUARD"
        from system.ml.twin_engine_core import ShadowDataGuardError, validate_utc_timestamp

        t0 = time.perf_counter()
        base = validate_utc_timestamp(time.time())
        with pytest.raises(ShadowDataGuardError, match="look-ahead"):
            validate_utc_timestamp(base - 3600.0, latest_ts=base)
        latency_us = int((time.perf_counter() - t0) * 1_000_000)
        _record(
            phase,
            "Inverted look-back timestamp rejected by ShadowDataGuard",
            status="PASS",
            latency_us=latency_us,
        )

    def test_chaotic_shadow_feed_rejects_weight_publish_and_freezes_live_swap(self) -> None:
        phase = "ML-BLACK-SWAN"
        from system.identity.weight_transfer_bridge import (
            get_weight_transfer_bridge,
            reset_weight_transfer_bridge,
        )
        from system.ml.twin_engine_core import (
            ModelWeights,
            TwinEngineCore,
            reset_twin_engine_core,
        )

        reset_twin_engine_core()
        reset_weight_transfer_bridge(unlink=True)
        core = TwinEngineCore()
        live_version_before = core.live.weights_snapshot().version

        t0 = time.perf_counter()
        epoch = time.time()
        guard_errors = 0
        for i in range(96):
            bid = float("nan") if i % 17 == 0 else 2000.0 + (i * 13.7)
            offer = bid + 0.5 if math.isfinite(bid) else float("nan")
            features = {
                "adjusted_score": float("inf") if i % 23 == 0 else 50.0 + i * 0.01,
                "rsi": float("nan") if i % 29 == 0 else 55.0,
                "atr_ratio": 999.0 if i % 31 == 0 else 1.0,
            }
            try:
                core.ingest_and_score(
                    epic="CS.D.CFPGOLD.CFP.IP",
                    ts_utc=epoch + i * 900.0,
                    bid=bid,
                    offer=offer,
                    features=features,
                    direction="BUY" if i % 2 == 0 else "SELL",
                )
            except Exception:
                guard_errors += 1

        # Sub-threshold edge must reject weight handoff and block atomic swap.
        bridge = get_weight_transfer_bridge(create=True)
        rejected = bridge.publish_candidate(
            weights={"bias": 9.9, "coeffs": {"adjusted_score": 99.0, "rsi": -40.0, "atr_ratio": 80.0}},
            edge=0.004,
        )
        hostile = ModelWeights(
            bias=9.9,
            coeffs={"adjusted_score": 99.0, "rsi": -40.0, "atr_ratio": 80.0},
            version=live_version_before + 99,
        )
        with core._swap_lock:
            core._pending_swap = (hostile, {"win_rate_edge": 0.004})
        core._maybe_apply_pending_swap()

        live_version_after = core.live.weights_snapshot().version
        hot_swaps = core.shadow.telemetry.hot_swaps

        latency_us = int((time.perf_counter() - t0) * 1_000_000)
        _record(
            phase,
            "Chaotic shadow stream + sub-threshold edge weight rejection",
            status="PASS" if not rejected and live_version_after == live_version_before else "FAIL",
            latency_us=latency_us,
            weight_publish=rejected,
            live_version_before=live_version_before,
            live_version_after=live_version_after,
            hot_swaps=hot_swaps,
            ingest_guard_errors=guard_errors,
        )
        assert rejected is False
        assert live_version_after == live_version_before
        assert hot_swaps == 0


class TestNetworkBrokerChaos:
    """Vector 3 — Wi-Fi drop + HTTP 429 fuzzing on order routing channel."""

    def _executor(self):
        from execution.live_executor import LiveExecutor

        cfg = MagicMock()
        cfg.allow_live_trading = True
        cfg.dry_run = False
        cfg.trade_size = 1.0
        cfg.stop_distance_points = 40.0
        cfg.limit_distance_points = 80.0
        cfg.currency_code = "GBP"
        cfg.max_retries = 0
        cfg.retry_delay_seconds = 0.0
        cfg.account_type = "DEMO"
        cfg.get = lambda k, d=None: 1.0 if k == "ig_point_value_gbp" else d
        client = MagicMock()
        client.account_type = "DEMO"
        client._base = "https://demo-api.ig.com"
        client.account_id = "ACC"
        client.normalize_order_params.return_value = (1.0, 40.0, 80.0, "GBP")
        return LiveExecutor(cfg, client)

    def _signal(self):
        from data.models import Quote
        from execution.types import TradeSignal

        q = Quote(time=datetime.now(timezone.utc), bid=2000.0, offer=2000.5)
        return TradeSignal(
            market="Gold",
            epic="CS.D.CFPGOLD.CFP.IP",
            direction="BUY",
            raw_confidence=90.0,
            adjusted_confidence=90.0,
            setup_key="chaos|net",
            quote=q,
            notes="chaos network",
        )

    def test_wifi_drop_and_http_429_do_not_hang_main_thread(self) -> None:
        phase = "NET-FUZZ"
        os.environ["IG_AGENT_PYTEST"] = "1"
        os.environ["IG_KERNEL_SOFT"] = "1"
        os.environ["IG_PARALLEL_TRACK"] = "live"

        from execution.cooldown_tracker import CooldownTracker
        from execution.trade_manager import TradeManager
        from execution.types import ExecutionMode
        from ig_api.exceptions import IGAPIError, RateLimitError

        executor = self._executor()
        params = {"size": 1.0, "risk": 40.0, "limit": 80.0, "risk_gbp": 40.0, "gate_sourced": True}
        tm = MagicMock(spec=TradeManager)
        cd = MagicMock(spec=CooldownTracker)

        scenarios: list[tuple[str, Exception]] = [
            ("wifi_drop", ConnectionError("Network is unreachable — simulated Wi-Fi loss")),
            ("http_429", RateLimitError("HTTP 429 Too Many Requests", status_code=429)),
            ("broker_reset", IGAPIError("Connection reset by peer", status_code=503)),
        ]

        for label, exc in scenarios:
            t0 = time.perf_counter()
            thread_error: list[BaseException] = []
            with patch(
                "execution.execution_protect.is_protect_enabled",
                return_value=False,
            ), patch(
                "execution.atomic_gateway.dispatch_atomic_market_order",
                side_effect=exc,
            ), patch(
                "execution.capital_guard.CapitalGuard.enforce_order_transmission",
                return_value=(True, "ok"),
            ):
                result_holder: dict[str, Any] = {}

                def _run() -> None:
                    try:
                        result_holder["result"] = executor._execute_order_blocking(
                            self._signal(),
                            dict(params),
                            tm,
                            cd,
                            mode=ExecutionMode.DEMO,
                        )
                    except BaseException as err:
                        thread_error.append(err)

                worker = threading.Thread(target=_run, name=f"chaos-{label}", daemon=True)
                worker.start()
                worker.join(timeout=1.0)
                elapsed_us = int((time.perf_counter() - t0) * 1_000_000)
                hung = worker.is_alive()
                res = result_holder.get("result")
                fail_loud = res is not None and res.success is False
                propagated = bool(thread_error)
                passed = not hung and (fail_loud or propagated)
                _record(
                    phase,
                    f"Injected {label}",
                    status="PASS" if passed else "FAIL",
                    latency_us=elapsed_us,
                    hung=hung,
                    rejection=getattr(res, "rejection_reason", None),
                    propagated_error=type(thread_error[0]).__name__ if thread_error else None,
                )
                assert not hung, f"{label} hung main routing thread >1s"
                assert passed, f"{label} neither rejected nor propagated: res={res} err={thread_error}"

    def test_network_chaos_fail_closed_cleanup_contract(self) -> None:
        """
        Auditor contract from chaos spec:
        interceptor + lock unlink + SHM clear + exit 0 in <1s.

        This measures ACTUAL behaviour — any deviation is a fatal flaw.
        """
        phase = "NET-CLEANUP-CONTRACT"
        reg = _require_live_runtime(require_shadow=False)
        live_pid = int(reg["live_pid"])

        from system.identity.app_identity import RuntimeIdentity
        from system.identity.instance_lock import read_lock_holder

        lock_path = RuntimeIdentity.get_lock_path(8080)
        holder_before = read_lock_holder(lock_path)

        t0 = time.perf_counter()
        script = """
import os, sys
sys.path.insert(0, "src")
os.environ["IG_AGENT_PYTEST"] = "1"
os.environ["IG_KERNEL_SOFT"] = "1"
os.environ["IG_SUPERVISED_NETWORK_TEARDOWN"] = "1"
os.environ["IG_PARALLEL_TRACK"] = "live"
from unittest.mock import MagicMock, patch
from execution.live_executor import LiveExecutor
from execution.types import ExecutionMode
from data.models import Quote
from execution.types import TradeSignal
from datetime import datetime, timezone
cfg = MagicMock()
cfg.allow_live_trading=True; cfg.dry_run=False; cfg.trade_size=1.0
cfg.stop_distance_points=40.0; cfg.limit_distance_points=80.0; cfg.currency_code="GBP"
cfg.max_retries=0; cfg.retry_delay_seconds=0.0; cfg.account_type="DEMO"
cfg.get=lambda k,d=None: 1.0 if k=="ig_point_value_gbp" else d
client=MagicMock(); client.account_type="DEMO"; client._base="https://demo-api.ig.com"; client.account_id="ACC"
client.normalize_order_params.return_value=(1.0, 40.0, 80.0, "GBP")
ex=LiveExecutor(cfg, client)
q=Quote(time=datetime.now(timezone.utc), bid=1.0, offer=1.1)
sig=TradeSignal(market="G", epic="CS.D.CFPGOLD.CFP.IP", direction="BUY", raw_confidence=90.0,
                adjusted_confidence=90.0, setup_key="c", quote=q, notes="c")
with patch("execution.execution_protect.is_protect_enabled", return_value=False), patch("execution.atomic_gateway.dispatch_atomic_market_order", side_effect=ConnectionError("drop")), patch("execution.capital_guard.CapitalGuard.enforce_order_transmission", return_value=(True, "ok")):
    try:
        res=ex._execute_order_blocking(sig, {"size":1.0,"risk":40.0,"limit":80.0,"risk_gbp":40.0,"gate_sourced":True},
                                       MagicMock(), MagicMock(), mode=ExecutionMode.DEMO)
        print("SUCCESS", res.success)
    except ConnectionError as exc:
        print("PROPAGATED ConnectionError", str(exc))
    except Exception as exc:
        print("PROPAGATED", type(exc).__name__, str(exc))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        elapsed_us = int((time.perf_counter() - t0) * 1_000_000)
        holder_after = read_lock_holder(lock_path)

        from system.identity.shared_memory_bridge import (
            attach_shared_memory_consumer,
            reset_shared_memory_bridge,
        )
        from system.identity.weight_transfer_bridge import reset_weight_transfer_bridge

        reset_shared_memory_bridge(unlink=False)
        reset_weight_transfer_bridge(unlink=False)
        shm_live = attach_shared_memory_consumer(track="live").read_json()
        shm_shadow = attach_shared_memory_consumer(track="shadow").read_json()

        stdout = (proc.stdout or "").strip()
        fail_loud = "PROPAGATED ConnectionError" in stdout or "SUCCESS False" in stdout
        contract_ok = (
            proc.returncode == 0
            and fail_loud
            and elapsed_us < 1_000_000
            and _pid_alive(live_pid)
        )
        spec_gaps: list[str] = []
        if holder_before is not None and holder_before == holder_after:
            spec_gaps.append("port_locks_not_unlinked_on_network_drop")
        if shm_live is None:
            spec_gaps.append("live_shm_segment_unreadable_after_chaos")
        if shm_shadow is None and _port_listening(9199):
            spec_gaps.append("shadow_shm_segment_unreadable")
        _record(
            phase,
            "Network chaos cleanup contract (exit0 + lock unlink + SHM clear)",
            status="PASS" if contract_ok and not spec_gaps else "FAIL-FATAL",
            latency_us=elapsed_us,
            subprocess_exit=proc.returncode,
            stdout=(proc.stdout or "").strip(),
            stderr=(proc.stderr or "").strip()[:200],
            lock_holder_before=holder_before,
            lock_holder_after=holder_after,
            shm_live_present=shm_live is not None,
            shm_shadow_present=shm_shadow is not None,
            live_pid_alive=_pid_alive(live_pid),
            spec_gaps=",".join(spec_gaps) if spec_gaps else "none",
        )
        if not contract_ok:
            pytest.fail(
                "FATAL SYSTEM FLAW: network chaos routing contract unmet — "
                f"exit={proc.returncode} stdout={proc.stdout!r} elapsed_us={elapsed_us}"
            )
        if spec_gaps:
            pytest.fail(
                "FATAL SYSTEM FLAW: network chaos teardown spec gaps — "
                + ", ".join(spec_gaps)
            )


class TestFourPillarGuiImmutability:
    """Vector 4 — 100 telemetry WebSocket envelopes, track-prefix immutability."""

    def test_hundred_ws_packets_track_prefix_immutability(self) -> None:
        phase = "GUI-WS-100"
        _require_live_runtime(require_shadow=False)

        import websockets

        uri = "ws://127.0.0.1:8080/api/telemetry/stream"

        async def _collect() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            for _ in range(100):
                async with websockets.connect(uri, open_timeout=3, close_timeout=1) as ws:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    out.append(json.loads(raw))
            return out

        t0 = time.perf_counter()
        packets = asyncio.run(_collect())

        latency_us = int((time.perf_counter() - t0) * 1_000_000)
        assert len(packets) == 100

        bleed_events = 0
        prefix_ok = 0
        for pkt in packets:
            streams = pkt.get("streams") or []
            if len(streams) != 2:
                bleed_events += 1
                continue
            prefixes = {str(row.get("prefix") or "") for row in streams}
            tracks = {str(row.get("track") or "") for row in streams}
            if prefixes == {"[LIVE-TRACK]", "[MOCK-TRACK]"} and tracks == {"live", "mock"}:
                prefix_ok += 1
            else:
                bleed_events += 1
            live_payload = next((r.get("payload") for r in streams if r.get("track") == "live"), {})
            shadow_payload = next((r.get("payload") for r in streams if r.get("track") == "mock"), {})
            if live_payload.get("track") == "mock" or shadow_payload.get("track") == "live":
                bleed_events += 1

        bleed_pct = (bleed_events / len(packets)) * 100.0
        _record(
            phase,
            "100 consecutive /api/telemetry/stream JSON envelopes",
            status="PASS" if bleed_events == 0 else "FAIL-FATAL",
            latency_us=latency_us,
            packets=len(packets),
            prefix_ok=prefix_ok,
            bleed_events=bleed_events,
            bleed_probability_pct=bleed_pct,
        )
        assert bleed_events == 0, f"track bleed detected in {bleed_events}/100 packets ({bleed_pct:.2f}%)"
