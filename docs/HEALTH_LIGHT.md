# HealthLight — Lightweight System Health Endpoint

## Overview

`GET /api/health_light` returns an in-memory O(1) snapshot of system health with a <5ms response target. All expensive reads (routing state, feed status, provider availability) happen in a background 1s refresh thread — never on the HTTP path.

## Endpoint

```
GET /api/health_light
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `agent_online` | bool | Always `true` if handler runs |
| `execution_loop_active` | bool | Dual-core rotation sweep count is increasing |
| `routing_state` | object | `{armed: int, degraded: bool, none: int}` — from cached gui_status |
| `feed_heartbeat_age_ms` | float\|null | Min feed age (ms) across active stack |
| `ws_state` | object | `{connected: bool, degraded: bool, reconnecting: bool}` |
| `cached_api_latency_ms` | float\|null | Rolling p50 for `request:health` |
| `ig_available` | bool\|null | Cached IG availability (refreshed every 30s) |
| `yahoo_available` | bool\|null | Cached Yahoo availability (refreshed every 30s) |
| `data_feeds` | object | Per-feed status (`hub.fresh_count`, `hub.total`) |
| `heartbeat_ts` | string | ISO timestamp of last 1s snapshot |
| `heartbeat_mono` | float | Monotonic clock of last snapshot |
| `agent_version` | string | `APP_VERSION_LABEL` |
| `feed_stall` | bool | All active stack epics have tpm=0 |
| `rotation_escape_active` | bool | Universe escape hatch engaged |
| `last_rotation_reason` | string | Last dual-core rotation reason |
| `stack_tpm` | object | Per-epic ticks/minute on active stack |
| `boot_grace_active` | bool | Within 180s boot grace (velocity gate relaxed) |

## P1–P4 Feed & Launch Hardening (Cycle 4)

| Tier | Item | Status |
|------|------|--------|
| P1 | 30s tpm=0 rehydrate (`TPM_ZERO_REHYDRATE_SEC`) | Active |
| P1 | Hub age >15s → Yahoo first | Active (P0) |
| P1 | `pricing.yahoo_poll_sec: 5` in demo throughput config | Active |
| P2 | FEED STALL header + rotation telemetry banner | Active |
| P2 | `feed_stall` / `last_rotation_reason` in health_light | Active |
| P3 | Ordered post-G5: Yahoo → fulfillment SHM → guardian → bootstrap → stacked tracks | Active |
| P3 | Feed guardian dual-core tpm=0 heal | Active |
| P4 | Demo `stagnant_dead_zone_sec: 120` + dead Z + tpm=0 fast rotate (60s) | Active |


```
HTTP GET /api/health_light
    └── get_health_light_response()   # O(1) dict copy, ~0.1ms
            └── reads _snapshot (in-memory)

Background thread (1s interval)
    └── _refresh_snapshot()
            ├── get_rotation_state()            # dual_core (local)
            ├── _GUI_STATUS_CACHE               # gui_status cache
            ├── get_socket_heartbeat_state()    # dual_core (local)
            ├── get_ws_subscriber_count()       # state_ws (local)
            ├── timing_summary()                # endpoint_profiler (local)
            ├── get_market_data_hub()           # in-process hub
            └── _refresh_provider_availability() # every 30s only
```

## Heartbeat File

Written every 1s to `src/data/health_light_heartbeat.json`:
```json
{"ts": "2026-06-30T05:50:01.123+00:00", "pid": 12345, "session_id": "Z6BAH4"}
```

## Verification

```bash
# Health light response
curl -s http://127.0.0.1:8080/api/health_light | python3 -m json.tool

# Confirm <5ms (check cached_api_latency_ms or time it directly)
time curl -s http://127.0.0.1:8080/api/health_light > /dev/null

# Heartbeat file
cat src/data/health_light_heartbeat.json
```

## Tests

```bash
PYTHONPATH=src python3 -m pytest tests/test_health_light.py tests/test_stress_readiness.py -q
```

## Cockpit Widget

`SystemHealthWidget.tsx` polls `/api/health_light` every 2s and renders:
- Agent Online / Execution Loop / Routing Armed+Degraded
- Feed Age / WS State / API Latency (p50)
- IG / Yahoo availability badges

Integrated into `CockpitLayout.tsx` right-sidebar above `RoutingPanel`.
