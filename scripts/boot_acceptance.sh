#!/bin/bash
# Boot contract — tiered success for launcher / verify (green | amber | red).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"
export PYTHONPATH="${ROOT}/src"
PY="${ROOT}/.venv/bin/python3"
if [[ ! -x "${PY}" ]]; then
  PY="$(command -v python3 || true)"
fi
PORT="${IG_API_PORT:-8080}"

tier_only=0
if [[ "${1:-}" == "--tier-only" ]]; then
  tier_only=1
fi

TIER="$("${PY}" - <<PY
import os
import sys

os.environ.setdefault("IG_API_PORT", "${PORT}")

try:
    from system.boot.iron_gauge import evaluate_startup_tier

    import json
    import urllib.request

    gauge = None
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int('${PORT}')}/api/iron_gauge", timeout=2.0
        ) as resp:
            gauge = json.loads(resp.read().decode("utf-8"))
    except Exception:
        gauge = None
    if gauge and str(gauge.get("tier") or "").lower() in ("green", "amber", "red"):
        print(str(gauge.get("tier")).lower())
        sys.exit(0)
    print(evaluate_startup_tier(port=int("${PORT}")))
    sys.exit(0)
except Exception:
    pass

import json
import socket
import urllib.error
import urllib.request

port = int("${PORT}")
base = f"http://127.0.0.1:{port}"


def port_accepts_tcp() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def fetch(path: str, timeout: float = 4.0):
    try:
        req = urllib.request.Request(
            f"{base}{path}",
            headers={"User-Agent": "IG-Agent-BootAcceptance/31"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


tcp_live = port_accepts_tcp()
if not tcp_live:
    print("red")
    sys.exit(0)

health = fetch("/health", 1.0) or fetch("/api/health_light", 1.0) or fetch("/api/health", 2.0)
if not health:
    print("amber")
    sys.exit(0)

# Bootstrap stub (/health) — API bound before full G5 hydration; enough for launcher Stage 6 amber.
if bool(health.get("bootstrap")) and health.get("ok") is not False:
    print("amber")
    sys.exit(0)

ss = health.get("system_state") or {}
phase = str(ss.get("phase") or "")
ready = bool(ss.get("ready")) or bool(health.get("ready"))
status = str(health.get("status") or "").upper()
gates = ss.get("gates") or {}
g2 = gates.get("G2") or {}
g2_status = str(g2.get("status") or "")
g2_detail = str(g2.get("detail") or "").lower()

boot = fetch("/api/boot_status") or {}
trade_ready = bool(boot.get("trade_ready"))
hl = fetch("/api/health_light") or {}
routing_armed = int((hl.get("routing_state") or {}).get("armed") or 0)
loops = ss.get("loops") or {}
accepting_ticks = bool(loops.get("accepting_ticks"))
execution_ready = trade_ready or routing_armed > 0 or accepting_ticks

if ready and status == "OPERATIONAL" and execution_ready:
    print("green")
    sys.exit(0)
if ready and status == "OPERATIONAL":
    print("amber")
    sys.exit(0)
if (phase in ("G5", "READY") or trade_ready) and execution_ready:
    print("green")
    sys.exit(0)

g2_advances = g2_status == "complete" or any(
    k in g2_detail for k in ("deferred", "armed", "async", "sandbox")
)
phase_advances = phase in ("G3", "G4", "G5")
g3_ok = (gates.get("G3") or {}).get("status") == "complete"

if status in ("HYDRATING", "OPERATIONAL", "OK", "DEGRADED") and (
    g2_advances or phase_advances or g3_ok
):
    print("amber")
    sys.exit(0)

if status in ("HYDRATING", "OPERATIONAL", "OK"):
    print("amber")
    sys.exit(0)

print("red")
PY
)"

if (( tier_only == 1 )); then
  echo "${TIER}"
  exit 0
fi

case "${TIER}" in
  green)
    echo "boot_acceptance: green — G5/trade_ready/operational"
    exit 0
    ;;
  amber)
    echo "boot_acceptance: amber — API live, boot degraded (IG auth/feeds may still hydrate)"
    exit 0
    ;;
  *)
    echo "boot_acceptance: red — /api/health unreachable or boot not started" >&2
    exit 1
    ;;
esac
