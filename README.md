# IG Agent v25

Standalone build from v24 (`IG_Agent_v24_ProGUI`). See spec `IG_Agent_v25_FINAL_SPEC_v4.pdf`.

- Trading engine: `src/trading/`
- No Tkinter GUI — web dashboard in `dashboard/`

## Running v25 (single command)

```bash
# Terminal 1 — everything
PYTHONPATH=src python3 src/main.py

# Then open in browser
http://localhost:8080

# Optional: remote access
ngrok http 8080
```

Build the React dashboard once (or after `dashboard/` changes): `cd dashboard && npm run build`
