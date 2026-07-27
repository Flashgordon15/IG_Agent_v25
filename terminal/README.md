# Quantum Terminal

Decoupled **Next.js (App Router) + Tailwind v4** Bloomberg Neo shell.  
Spec: [`docs/ui_ux_terminal_specification.md`](../docs/ui_ux_terminal_specification.md)

## Desk Intent

The **Desk Intent** strip (under the SYSTEM OPERATIONAL banner) answers three pilot questions only:

1. **Can we trade?** — per engine `ARMED` | `PAUSED` | `BLOCKED` (CFD A2 pause preserved via `health.trading_paused`)
2. **Confidence on the focus market?** — sniper ML band + WAIT/SETUP from `/api/desk/ops_strip` (prefer SB when CFD paused)
3. **Is rotation occurring?** — merged `/api/rotation_state` from :8080 + :8081 (prefer SB when CFD paused)

**Ranked rotator (GUI):** when `rotation.ranked_rotator.active`, Desk Intent shows market focus = **dominant**, a **Promoted** set (top-N, e.g. DOW · GOLD), and a rotation line like `ranked · dominant DOW · promoted DOW,GOLD · wait EURUSD·FTSE`. The AI Market Scanner chips/rows mirror promoted / eligible / waiting; Nikkei stays excluded from hot path. See [`docs/ROTATION_FAILOVER_POLICY.md`](../docs/ROTATION_FAILOVER_POLICY.md).

It does not duplicate REST/core-pin noise from the truth strip or lane cards.

## Run

```bash
# Agent must be live on :8080
cd terminal && npm install && npm run dev
# → http://localhost:3000
```

Dev rewrites proxy `/api/*` and `/ws/*` to `127.0.0.1:8080`.

Override agent URL:

```bash
NEXT_PUBLIC_AGENT_URL=http://127.0.0.1:8080 npm run dev
```

## v0.1 zones

- Connection bar (health + WS state)
- Gate diagnostics grid (`/api/unified/fulfillment` @ 500ms)
- Epic telemetry strip (`/api/telemetry/stream` WebSocket)

Legacy Vite dashboard remains at `dashboard/` — served by FastAPI `dist/` until cutover.
