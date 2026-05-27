# IG Agent v25 Dashboard

React 18 + Vite 5 + Tailwind 3 (Section 5).

## Development

```bash
npm install
npm run dev   # http://localhost:5173 — proxies API to :8080
```

## Production build

```bash
npm run build   # output → dashboard/dist/
```

FastAPI serves `dist/` at `http://localhost:8080/` when the build exists.
