#!/usr/bin/env bash
# Install Cursor handoff rule + docs on this machine (Mac Mini).
# Run from project root: bash scripts/install_cursor_handoff.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RULE_DIR="${ROOT}/.cursor/rules"
DOCS_DIR="${ROOT}/docs"
mkdir -p "${RULE_DIR}" "${DOCS_DIR}"

cat > "${RULE_DIR}/mac-mini-operations.mdc" <<'EOF'
---
description: Mac Mini 24/7 host — migration handoff and ops context (Jun 2026)
alwaysApply: true
---

# Mac Mini — IG Agent v29 host (handoff)

## Canonical project path (Mini)

- **Active repo:** `/Users/chrisgordon/Projects/IG_Agent_v25`
- **MacBook:** keep agent **stopped**; Mini is the sole 24/7 host.
- **Dashboard from MacBook:** `ssh -L 8080:127.0.0.1:8080 chrisgordon@192.168.6.44` → http://localhost:8080

## Current production state (post-migration 2026-06-12)

- Setup: `bash scripts/setup_mac_mini.sh` completed; launchd loaded (watchdog, caffeinate, scheduled v29 jobs).
- Agent: supervised by `com.igagent.v25.watchdog`; health was **trading_healthy True, quotes 6/6** after REST poll fix.
- **streaming_transport:** `rest_poll` in `config/config_v25.json` (Lightstreamer WebSocket flaps on Mini; REST works).
- **Credentials:** `config/credentials/credentials.json` (gitignored); includes IG + `telegram_bot_token` / `telegram_chat_id`.
- **Telegram in config:** `telegram.enabled: true` but empty `bot_token`/`chat_id` in JSON — runtime merges from credentials. `ensure_overnight_ready.sh` Telegram FAIL is a checker quirk (reads v29-only config); ignore if runtime notifier enabled.

## Watchdog gotchas (Mac Mini first boot)

- Fresh watchdog waits **720s startup grace** before first agent start — setup WARN at 3 min is normal.
- Manual start: `IG_AGENT_ROOT=$PWD PYTHONPATH=$PWD/src IG_AGENT_FROM_LAUNCHER=1 IG_AGENT_SKIP_DEPLOY_CHECK=1 nohup .venv/bin/python3 scripts/start_agent_launchd.py >> src/data/logs/agent_restart.log 2>&1 &`
- Do **not** run `./clean_launch.sh` casually on Mini — kills agent; watchdog restarts but delays apply.
- Avoid `echo "Started PID $!"` in zsh paste traps (`dquote>`); use `pgrep -fl main.py` instead.

## Health / ops commands

```bash
curl -s http://127.0.0.1:8080/api/health | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok:', d['ok'], 'trading_healthy:', d['trading_healthy'], 'quotes:', d['quotes_fresh_count'], '/', d['quotes_total'])"
pgrep -fl main.py
tail -20 src/data/logs/watchdog.log
./scripts/ensure_overnight_ready.sh
```

## Dev workflow on Mini with Cursor

- Open folder: `/Users/chrisgordon/Projects/IG_Agent_v25`
- Human-readable handoff: `docs/MAC_MINI_HANDOFF.md`
EOF

cat > "${DOCS_DIR}/MAC_MINI_HANDOFF.md" <<'EOF'
# Mac Mini handoff — IG Agent v29 (2026-06-12)

## Open in Cursor on the Mini

1. **File → Open Folder…** → `/Users/chrisgordon/Projects/IG_Agent_v25`
2. New chats load `.cursor/rules/mac-mini-operations.mdc` automatically.

## Architecture

- **Mac Mini** (`192.168.6.44`): 24/7 agent host
- **MacBook**: dashboard via SSH tunnel only — agent stopped

## Key fixes applied

- `streaming_transport`: `rest_poll` in config_v25.json (Lightstreamer fails on Mini)
- launchd + watchdog installed via setup_mac_mini.sh
- Telegram tokens in credentials.json (not config JSON)

## Dashboard from MacBook

```bash
ssh -L 8080:127.0.0.1:8080 chrisgordon@192.168.6.44
```

→ http://localhost:8080

## Health

```bash
curl -s http://127.0.0.1:8080/api/health | python3 -m json.tool
```

Target: trading_healthy true, quotes 6/6.
EOF

echo "Installed:"
echo "  ${RULE_DIR}/mac-mini-operations.mdc"
echo "  ${DOCS_DIR}/MAC_MINI_HANDOFF.md"
echo ""
echo "In Cursor on this Mac: File → Open Folder → ${ROOT}"
