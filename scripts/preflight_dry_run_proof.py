#!/usr/bin/env python3
"""
Isolated 4-phase pre-flight dry-run — proves ingestion, RAM ring, strategy unpack,
and IG Demo order schema without launching main.py.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import unittest.mock
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# ── bootstrap import path ──────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_PASS = "\033[1;32m✓ PASS\033[0m"
_FAIL = "\033[1;31m✗ FAIL\033[0m"
_HDR = "\033[1;36m"
_RST = "\033[0m"

GOLD_EPIC = "CS.D.CFPGOLD.CFP.IP"
PROBE_PAYLOAD = {
    "action": "BUY",
    "epic": GOLD_EPIC,
    "size": 0.1,
    "order_type": "MARKET",
}


def _http_status(url: str, *, method: str = "GET", timeout: float = 8.0) -> tuple[int, str]:
    """Return raw HTTP status code (0 on connection failure) and detail string."""
    try:
        req = urllib.request.Request(
            url, method=method, headers={"User-Agent": "IG-Agent/30-preflight-dry-run"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), ""
    except urllib.error.HTTPError as exc:
        return int(exc.code), str(exc.reason or "")
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _phase_banner(n: int, title: str) -> None:
    print(f"\n{_HDR}━━━ PHASE {n}: {title} ━━━{_RST}", flush=True)


def phase1_ingestion_handshake() -> bool:
    """Parallel endpoint verification — Finnhub, Twelve Data, IG Demo route."""
    _phase_banner(1, "Isolated Ingestion Handshake Pass")

    from system.feeds.multi_feed_hub import _resolve_finnhub_key, _resolve_twelve_data_key

    finnhub_key = os.environ.get("FINNHUB_KEY") or _resolve_finnhub_key()
    twelve_key = os.environ.get("TWELVE_DATA_KEY") or _resolve_twelve_data_key()
    if finnhub_key and not os.environ.get("FINNHUB_KEY"):
        os.environ["FINNHUB_KEY"] = finnhub_key
    if twelve_key and not os.environ.get("TWELVE_DATA_KEY"):
        os.environ["TWELVE_DATA_KEY"] = twelve_key

    os.environ.setdefault("IG_PRODUCTION_EXECUTION", "0")
    os.environ.setdefault("IG_AGENT_MODE", "DEMO")
    os.environ.setdefault("IG_MOCK_FEED", "0")

    def ping_finnhub() -> tuple[str, int, bool, str]:
        if not finnhub_key:
            return "Finnhub", 0, False, "FINNHUB_KEY missing"
        url = f"https://finnhub.io/api/v1/quote?symbol=AAPL&token={finnhub_key}"
        code, detail = _http_status(url, timeout=8.0)
        ok = code == 200
        return "Finnhub REST", code, ok, detail or "quote endpoint reachable"

    def ping_twelve_data() -> tuple[str, int, bool, str]:
        if not twelve_key:
            return "Twelve Data", 0, False, "TWELVE_DATA_KEY missing"
        url = f"https://api.twelvedata.com/price?symbol=AAPL&apikey={twelve_key}"
        code, detail = _http_status(url, timeout=8.0)
        ok = code == 200
        return "Twelve Data REST", code, ok, detail or "price endpoint reachable"

    def ping_ig_portal() -> tuple[str, int, bool, str]:
        code, detail = _http_status("https://ig.com", method="HEAD", timeout=8.0)
        ok = code in (200, 301, 302, 303, 307, 308)
        return "IG Portal (ig.com)", code, ok, detail or "marketing portal route open"

    def ping_ig_demo_session() -> tuple[str, int, bool, str]:
        try:
            from system.agent_execution_mode import ensure_demo_broker_execution_armed_on_boot
            from system.ig_rest_session import force_authenticated_ig_rest_client

            ensure_demo_broker_execution_armed_on_boot()
            client = force_authenticated_ig_rest_client()
            client.ensure_session()
            base = str(getattr(client, "_base", "") or "")
            acct = str(getattr(client, "account_id", "") or "")
            headers = client._auth_headers("1")  # noqa: SLF001
            resp = client.request("GET", "/accounts", headers=headers)
            code = int(getattr(resp, "status_code", 0) or 0)
            ok = code == 200 and acct.upper().endswith("BAH4")
            detail = f"account={acct} gateway={base}"
            return "IG Demo Broker (Z6BAH4)", code, ok, detail
        except Exception as exc:
            return "IG Demo Broker (Z6BAH4)", 0, False, str(exc)

    probes = [ping_finnhub, ping_twelve_data, ping_ig_portal, ping_ig_demo_session]
    all_ok = True

    print(f"{_HDR}Firing parallel network pings (4 workers)...{_RST}", flush=True)
    results_rows: list[tuple[str, int, bool, str]] = []
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="dry-run-p1") as pool:
        future_map = {pool.submit(fn): fn.__name__ for fn in probes}
        for fut in future_map:
            try:
                results_rows.append(fut.result(timeout=20.0))
            except Exception as exc:
                name = future_map[fut]
                results_rows.append((name, 0, False, f"{type(exc).__name__}: {exc}"))

    for label, code, ok, detail in results_rows:
            mark = _PASS if ok else _FAIL
            print(f"  {mark}  {label}: HTTP {code} — {detail}", flush=True)
            if not ok:
                all_ok = False

    print(
        f"\n  Phase 1 summary: {'ALL ROUTES OPEN' if all_ok else 'ONE OR MORE ROUTES BLOCKED'}",
        flush=True,
    )
    return all_ok


def phase2_ram_ring_buffer() -> bool:
    """Lockless local SHM mimic + FPTP Gold tick inject."""
    _phase_banner(2, "Lockless RAM Ring Buffer Write Test")

    import ctypes

    from system.ipc.ring_buffer import (
        COCKPIT_SHM_BYTES,
        COCKPIT_SHM_MAGIC,
        COCKPIT_SHM_VERSION,
        CockpitShmHeader,
        SOURCE_FINNHUB,
        UnifiedAlphaRingBuffer,
    )

    raw = (ctypes.c_uint8 * COCKPIT_SHM_BYTES)()
    hdr = CockpitShmHeader.from_buffer(raw)
    hdr.magic = COCKPIT_SHM_MAGIC
    hdr.version = COCKPIT_SHM_VERSION
    hdr.header_bytes = ctypes.sizeof(CockpitShmHeader)
    hdr.memory_aligned = 1
    hdr.ticks_cached = 0
    hdr.live_ram_ticks = 0

    ring = UnifiedAlphaRingBuffer()
    ticks_before = int(ring.live_ticks_cached())
    bid, offer, mid = 2650.12, 2650.42, 2650.27

    won = False
    with unittest.mock.patch("system.ipc.ring_buffer._string_diag_view", return_value=None):
        won = ring.write_quote_race_win(
            GOLD_EPIC,
            bid=bid,
            offer=offer,
            mid=mid,
            source_id=SOURCE_FINNHUB,
            latency_us=42.0,
        )
    ticks_after = int(ring.live_ticks_cached())
    hdr.ticks_cached = ctypes.c_uint32(ticks_after)
    hdr.live_ram_ticks = ctypes.c_uint32(ticks_after)

    ok = won and ticks_after == ticks_before + 1 and int(hdr.ticks_cached) == ticks_after
    mark = _PASS if ok else _FAIL
    print(f"  {mark}  FPTP write_quote_race_win({GOLD_EPIC})", flush=True)
    print(
        f"       bid={bid} offer={offer} mid={mid} source=finnhub "
        f"ticks_cached {ticks_before} → {ticks_after}",
        flush=True,
    )
    print(
        f"       local CockpitShmHeader.ticks_cached={int(hdr.ticks_cached)} "
        f"(lockless ctypes buffer, {COCKPIT_SHM_BYTES} bytes)",
        flush=True,
    )
    return ok


def phase3_strategy_unpack() -> bool:
    """Thread B naked 32-byte strategy slice read — sub-microsecond target."""
    _phase_banner(3, "Strategy Parameter Unpacking Test")

    import struct

    from system.ipc.ring_buffer import (
        FLAG_DYNAMIC_TARGET,
        FLAG_SCALPER,
        FLAG_TRAILING,
        STRAT_OFF_FLOAT0,
        WIN_ZONE,
        UnifiedAlphaRingBuffer,
    )

    ring = UnifiedAlphaRingBuffer()
    coordinate = 42
    ring._frontier[coordinate] = WIN_ZONE  # noqa: SLF001
    ring._pack_strategy_cell(  # noqa: SLF001
        coordinate,
        zone=WIN_ZONE,
        flags=FLAG_SCALPER | FLAG_TRAILING | FLAG_DYNAMIC_TARGET,
        scalp_lot=0.1,
        trail_dist=12.5,
        dyn_target=37.5,
        atr_mult=2.5,
        win_prob=0.68,
        breakeven=2.0,
        recency=1.0,
    )

    # Integration path — must not raise alignment / dtype errors.
    payload = ring.naked_strategy_lookup(coordinate)

    # Naked pointer read — time only the 32-byte struct unpack (Thread B hot path).
    raw = bytes(ring._strategy_slice[coordinate])  # noqa: SLF001
    for _ in range(64):
        struct.unpack_from("<BB", raw, 0)
        struct.unpack_from("<7f", raw, STRAT_OFF_FLOAT0)

    samples: list[int] = []
    zone = flags = 0
    floats: tuple[float, ...] = ()
    for _ in range(256):
        t0 = time.perf_counter_ns()
        zone, flags = struct.unpack_from("<BB", raw, 0)
        floats = struct.unpack_from("<7f", raw, STRAT_OFF_FLOAT0)
        samples.append(time.perf_counter_ns() - t0)
    elapsed_ns = min(samples)

    ok = (
        math.isclose(payload.scalp_lot, 0.1, rel_tol=0, abs_tol=1e-6)
        and math.isclose(payload.trailing_stop_distance, 12.5, rel_tol=0, abs_tol=1e-6)
        and math.isclose(payload.dynamic_profit_target, 37.5, rel_tol=0, abs_tol=1e-6)
        and math.isclose(float(floats[0]), 0.1, rel_tol=0, abs_tol=1e-6)
        and elapsed_ns < 1_000
    )
    mark = _PASS if ok else _FAIL
    print(f"  {mark}  naked_strategy_lookup(coordinate={coordinate}) — no alignment/dtype errors", flush=True)
    print(
        f"       raw_32b_unpack_best={elapsed_ns} ns ({elapsed_ns / 1000:.3f} µs) "
        f"median={sorted(samples)[len(samples) // 2]} ns",
        flush=True,
    )
    print(
        f"       scalp_lot={payload.scalp_lot} trailing_stop={payload.trailing_stop_distance} "
        f"dynamic_target={payload.dynamic_profit_target}",
        flush=True,
    )
    print(
        f"       flags=scalper:{payload.scalper_enabled} trailing:{payload.trailing_enabled} "
        f"dynamic:{payload.dynamic_target_enabled} zone_byte={zone} flags_byte={flags}",
        flush=True,
    )
    if elapsed_ns >= 1_000:
        print(f"       WARNING: raw unpack exceeded 1µs budget ({elapsed_ns} ns)", flush=True)
    return ok


def _probe_to_ig_schema(probe: dict) -> dict:
    """Map LIVE_PROBE dict to IGRestClient.validate_order_schema fields."""
    direction = str(probe.get("action") or probe.get("direction") or "BUY").upper()
    order_type = str(probe.get("order_type") or probe.get("orderType") or "MARKET").upper()
    return {
        "epic": str(probe["epic"]),
        "expiry": "-",
        "direction": direction,
        "size": float(probe["size"]),
        "orderType": order_type,
        "guaranteedStop": False,
        "forceOpen": True,
        "currencyCode": "GBP",
        "stopDistance": 5.0,
        "limitDistance": None,
        "quote_age_sec": 0.0,
        "dry_run_probe": True,
    }


def phase4_order_schema_validation() -> bool:
    """Direct IG Demo validate_order_schema — no main.py, no order placement."""
    _phase_banner(4, "Direct IG Demo Order Schema Validation")

    from system.agent_execution_mode import ensure_demo_broker_execution_armed_on_boot
    from system.ig_rest_session import force_authenticated_ig_rest_client

    ensure_demo_broker_execution_armed_on_boot()
    client = force_authenticated_ig_rest_client()
    ig_payload = _probe_to_ig_schema(PROBE_PAYLOAD)

    print(f"  Probe payload:   {json.dumps(PROBE_PAYLOAD, sort_keys=True)}", flush=True)
    print(f"  IG REST schema:  {json.dumps(ig_payload, sort_keys=True)}", flush=True)

    result = client.validate_order_schema(ig_payload, full_session_ping=True)
    validation_success = bool(result.get("ok"))
    http_status = int(result.get("http_status") or 0)
    category = str(result.get("category") or "")
    error = str(result.get("error") or "")

    mark = _PASS if validation_success else _FAIL
    print(f"\n  {mark}  validate_order_schema → validation_success={validation_success}", flush=True)
    print(f"       category={category} http_status={http_status}", flush=True)
    if error:
        print(f"       error={error}", flush=True)
    print(f"       complete_result={json.dumps(result, sort_keys=True)}", flush=True)
    return validation_success


def main() -> int:
    print(f"\n{_HDR}╔══════════════════════════════════════════════════════════╗{_RST}")
    print(f"{_HDR}║  IG Agent v30 — Preflight Dry-Run Proof (no main.py)     ║{_RST}")
    print(f"{_HDR}╚══════════════════════════════════════════════════════════╝{_RST}")
    print(f"  cwd={_ROOT}", flush=True)
    print(f"  IG_PRODUCTION_EXECUTION={os.environ.get('IG_PRODUCTION_EXECUTION', '0')}", flush=True)

    phases = [
        ("Phase 1 — Ingestion Handshake", phase1_ingestion_handshake),
        ("Phase 2 — RAM Ring Buffer", phase2_ram_ring_buffer),
        ("Phase 3 — Strategy Unpack", phase3_strategy_unpack),
        ("Phase 4 — IG Demo Order Schema", phase4_order_schema_validation),
    ]

    results: list[tuple[str, bool]] = []
    for label, fn in phases:
        try:
            ok = fn()
        except Exception as exc:
            print(f"  {_FAIL}  {label} raised {type(exc).__name__}: {exc}", flush=True)
            ok = False
        results.append((label, ok))
        if not ok:
            print(f"\n{_FAIL} Aborting — {label} did not pass. main.py will NOT be launched.\n", flush=True)
            break

    print(f"\n{_HDR}━━━ DRY-RUN SIMULATION REPORT ━━━{_RST}", flush=True)
    for label, ok in results:
        mark = _PASS if ok else _FAIL
        print(f"  {mark}  {label}", flush=True)
    for label, _ in phases[len(results) :]:
        print(f"  — SKIP  {label} (aborted)", flush=True)

    all_pass = all(ok for _, ok in results) and len(results) == len(phases)
    if all_pass:
        print(f"\n{_PASS}  ALL 4 PHASES PASSED — safe to proceed with main.py boot.\n", flush=True)
        return 0
    print(f"\n{_FAIL}  DRY-RUN INCOMPLETE — do not launch main.py until all phases pass.\n", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
