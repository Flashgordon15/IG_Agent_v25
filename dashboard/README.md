# IG Agent v29.1 Dashboard

React 18 + Vite + Tailwind — served by FastAPI at `http://localhost:8080/` from `dist/`.

Spec reference: `../IG_Agent_v29.1_COMPLETE_SPEC.md` §9.

## Development

```bash
npm install
npm run dev   # http://localhost:5173 — proxies API to :8080
```

## Production build

```bash
npm run build   # output → dashboard/dist/
```

**Required after any `src/` edit** before restart — the agent serves `dist/` only.

## v29.1 panels

| Area | Component |
|------|-----------|
| System → Learning Health | `LearningHealthPanel.jsx` |
| Daily digest | `DailyDigestModal.jsx` |
| Live P&L / FX pts | `LivePanel.jsx`, `utils/fmtPts.js` |
