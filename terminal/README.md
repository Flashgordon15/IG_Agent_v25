# Quantum Terminal

Decoupled **Next.js (App Router) + Tailwind v4** Bloomberg Neo shell.  
Spec: [`docs/ui_ux_terminal_specification.md`](../docs/ui_ux_terminal_specification.md)

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
