# Unified Trading Architecture — IG Agent v29.1 + IG Cockpit

This document describes how boot, execution, lifecycle, sizing, stops, rotation, and GUI diagnostics connect through a single unified runtime state.

## Overview

All subsystems publish into `system/unified_runtime_state.py`. The FastAPI layer exposes snapshots for IG Cockpit polling. Trading logic remains in existing modules (`trade_manager`, `dual_core_execution`, `virtual_stop_loss`); the unified layer is observability and coordination only.

```mermaid
flowchart TB
    subgraph Boot["Boot A–G"]
        BO[boot_orchestrator]
        SH[subsystem_healer]
        PR[post_ready_services]
    end

    subgraph Execution["Execution plane"]
        DCE[dual_core_execution]
        TM[trade_manager]
        VSL[virtual_stop_loss]
        TLE[trailing_stop_engine]
    end

    subgraph IG["IG / Feeds"]
        REST[ig rest_client]
        HUB[market_data_hub]
        YF[yahoo poller]
    end

    subgraph State["Unified state"]
        URS[unified_runtime_state]
    end

    subgraph API["HTTP APIs"]
        BS[/api/boot_status]
        US[/api/unified_status]
        TL[/api/trade_lifecycle]
        RJ[/api/rejections]
        RT[/api/rotation_status]
        HL[/api/health_light]
    end

    subgraph GUI["IG Cockpit"]
        SPL[SplashScreen diagnostics]
        UTP[UnifiedTradingPanels]
        SHW[SystemHealthWidget]
    end

    BO --> URS
    SH --> BO
    PR --> VSL
    PR --> DCE
    HL --> URS
    DCE --> URS
    TM --> URS
    TM --> REST
    TM --> TL_SM[trade_lifecycle state machine]
    TL_SM --> URS
    VSL --> URS
    HUB --> VSL
    YF --> DCE

    URS --> US
    BO --> BS
    TL_SM --> TL
    URS --> RJ
    DCE --> RT
    HL --> SHW

    BS --> SPL
    US --> UTP
    TL --> UTP
    RJ --> UTP
    RT --> UTP
```

## Modules

| `gate2_async_hydration.py` | Non-blocking G2 position sync + size rules prefetch (<10s critical path) |
| `dynamic_limit_engine.py` | Volatility-aware profit targets, lifecycle `DYNAMIC_LIMIT_ACTIVE` |
| `trade_state_api.py` | `/api/trade_state`, `/api/trade_events`, `/api/rotation_state` |
| `unified_runtime_state.py` | Thread-safe singleton: boot, feeds, routing, execution, sizing, lifecycle, stops, rejections, events |
| `ig_size_validator.py` | Pre-trade size normalization (IG min/step, canary caps) |
| `trade_lifecycle.py` | Full state machine: SIGNAL_DETECTED → … → EXIT_FILLED / REJECTED |
| `broker_reject_guard.py` | Classified rejections + circuit breaker; always emits to unified state |
| `boot_orchestrator.py` | Stages A–G, startup diagnostics checklist, G2 ready_phase fallback |
| `health_light.py` | 1s background refresh → `update_from_health_light()` |

## Trade path (micro dispatch)

1. Guards (strategy, hard enforcement, REST budget, position sync)
2. `pre_trade_check()` — constraints cache, canary cap, min/step
3. `begin_trade()` lifecycle ORDER_SUBMITTED
4. `place_market_order` + `confirm_deal`
5. On `MINIMUM_ORDER_SIZE_ERROR`: one self-correct retry at IG min deal
6. On reject: `record_rejection()` + lifecycle REJECTED
7. On fill: lifecycle CONFIRMED → ARMED_STOP → ACTIVE; `register_virtual_stop()`

## API endpoints

- `GET /api/unified_status` — full snapshot
- `GET /api/trade_lifecycle` — state machine + lifecycle bus
- `GET /api/rejections` — classified rejection ring buffer
- `GET /api/rotation_status` — sweep count, focus epic
- `GET /api/trade_state` — lifecycle + stops + dynamic limits
- `GET /api/trade_events` — typed event stream for trading path
- `GET /api/rotation_state` — rotation alias with history

## Startup diagnostics (splash)

| Key | Meaning |
|-----|---------|
| `size_rules_loaded` | IG constraints fetched or config loaded |
| `trailing_stop_engine_active` | Virtual stop watchdog armed |
| `dynamic_limit_engine_active` | Dynamic limit tracking armed |
| `execution_loop_ready` | Stacked sweep thread + loop active |
| `ig_connectivity_validated` | health_light IG available |
| `rotation_logic_active` | sweep count > 0 |
| `feed_heartbeat_live` | hub ticks fresh |
| `routing_armed` | unified routes armed count > 0 |

## GUI

- **SplashScreen** — boot stages + startup diagnostics checklist
- **UnifiedTradingPanels** — lifecycle, rejections, rotation, stops/limits/size
- **SystemHealthWidget** — health_light (unchanged)

## Safety

- No changes to strategy scoring or signal engine
- Size validator respects canary caps and REST-cached constraints
- Rejections never silent: log + unified state + optional latch
- Agent not restarted by this integration; cold restart validation when flat
