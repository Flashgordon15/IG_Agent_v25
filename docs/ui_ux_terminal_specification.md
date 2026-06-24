# ⚡ THE CYBER-QUANTUM TERMINAL ARCHITECTURE

**Document ID:** `docs/ui_ux_terminal_specification.md`  
**Author:** Principal UI/UX Architect, Quantum Trading Systems  
**Status:** Approved for Implementation Loop  

---

## 1. Architectural Strategy: The Next.js Decoupled UI Wrapper

To deliver an elite, zero-lag, Bloomberg Neo-style interface without tying your Python trading loops into knots, we implement a **Decoupled Next.js Engine**.

Instead of rendering inside the resource-heavy Python thread, the UI runs as an independent, ultra-lightweight web application on **Next.js (App Router)** and **Tailwind CSS v4**. It hooks directly into your running agent's FastAPI port via persistent, low-overhead WebSockets or high-performance local polling.

| Layer | Responsibility | Must NOT |
|-------|----------------|----------|
| **Python agent** (`src/main.py`) | Trading loops, risk, execution, snapshot cache | Block on React render or DOM work |
| **FastAPI** (`:8080`) | REST + WebSocket data plane only | Serve heavy SPA builds in hot path during dev |
| **Quantum Terminal** (`terminal/`) | Render, animation, operator UX | Import Python or mutate ledger state |

---

## 2. Data Plane — Agent Hooks (read-only)

All terminal traffic targets the **live Vanguard** bind (`127.0.0.1:8080`) unless `NEXT_PUBLIC_AGENT_URL` overrides.

### 2.1 WebSocket (primary — zero-lag telemetry)

| Endpoint | Payload | Hz |
|----------|---------|-----|
| `ws://127.0.0.1:8080/api/telemetry/stream` | Same envelope as `/api/live-state` | Push (~2.5 Hz institutional) |
| `ws://127.0.0.1:8080/ws/stream` | Tick runtime (`enrich_tick_runtime`) | Push on tick |

**Client rule:** one WebSocket per channel; exponential backoff reconnect; stale-frame guard (45s).

### 2.2 REST (secondary — structured snapshots)

| Endpoint | Use |
|----------|-----|
| `GET /api/health` | Boot phase, PID, `trading_healthy` |
| `GET /api/unified/fulfillment` | Gate diagnostics grid (500ms server cache) |
| `GET /api/live-state` | Polling fallback when WS unavailable |
| `GET /api/startup/status` | G1–G5 boot ribbon |

### 2.3 Admin (authenticated — never in public build)

Sensitive routes (`/api/admin/*`) require session cookie from `POST /api/auth/login`. The terminal **does not** embed admin actions in v1 shell.

---

## 3. Visual Language — Bloomberg Neo

- **Canvas:** `#090d1f` → `#050714` vertical gradient, fixed attachment  
- **Accent:** `#00b4d8` (cyan pulse for live feeds)  
- **Typography:** 13px UI / `ui-monospace` for prices and gate codes  
- **Density:** 8px grid, glass cards (`backdrop-blur`, `border #1c234a`)  
- **Motion:** pulse-dot on LIVE; no layout shift on tick update (CSS `tabular-nums`)

---

## 4. Terminal Zones (v1 shell)

```
┌─────────────────────────────────────────────────────────────┐
│ CONNECTION BAR — agent URL, WS state, boot %, trading_health│
├──────────────────────────┬──────────────────────────────────┤
│ GATE DIAGNOSTICS GRID    │ EPIC TELEMETRY STRIP             │
│ (fulfillment 500ms)      │ (WS telemetry epics + spreads)   │
├──────────────────────────┴──────────────────────────────────┤
│ ENGINE LOG TAIL (optional v1.1 — Flight Deck :8787 parity)    │
└─────────────────────────────────────────────────────────────┘
```

---

## 5. Development Workflow

```bash
# Terminal (independent process)
cd terminal && npm install && npm run dev   # http://localhost:3000

# Agent (must already be running)
PYTHONPATH=src python3 src/main.py          # http://localhost:8080
```

`next.config.ts` rewrites `/api/*` and `/ws/*` to `:8080` in development so the browser stays same-origin.

**Production:** build static export or Node standalone; operator opens `http://127.0.0.1:3000` alongside agent — Python `dist/` dashboard remains fallback until cutover.

---

## 6. Coexistence with Legacy Dashboard

| App | Path | Status |
|-----|------|--------|
| Vite dashboard | `dashboard/` → served at `:8080/` | Shipped v29.1 panels |
| Quantum Terminal | `terminal/` → `:3000` dev | **This spec** |
| Flight Deck | `:8787` cockpit | pywebview SHM bridge |

Cutover criterion: feature parity on fulfillment grid + live P&L + zero added REST budget from UI polling.

---

## 7. Implementation Phases

| Phase | Deliverable |
|-------|-------------|
| **P0** (current) | Next.js shell, WS telemetry hook, fulfillment poll, Neo theme |
| **P1** | Epic sparklines, gate rejection drill-down, auth-gated admin drawer |
| **P2** | WebGL tick lane (port `ApexWebGLRenderer`), SHM passive read via sidecar |
