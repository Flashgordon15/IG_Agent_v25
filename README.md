# IG Agent v25

Standalone build from v24 (`IG_Agent_v24_ProGUI`). See spec `IG_Agent_v25_FINAL_SPEC_v4.pdf`.

- Trading engine: `src/trading/`
- No Tkinter GUI — web dashboard in `dashboard/`

## Running v25

```bash
# 1. Start the trading agent (headless)
python main.py

# 2. Start the API server
uvicorn src.api.server:app --port 8080 --reload

# 3. Start the React dashboard
cd dashboard && npm run dev

# 4. Open in browser
http://localhost:5173

# Optional: remote access
ngrok http 5173
```
