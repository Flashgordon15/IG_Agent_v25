#!/usr/bin/env bash
# Copy project to Mac Mini over SSH. Run on MacBook after mac_mini_connect_check.sh passes:
#   bash scripts/mac_mini_sync.sh

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${MAC_MINI_SSH_HOST:-mac-mini}"
DEST="${MAC_MINI_DEST:-Projects/IG_Agent_v25}"

echo ""
echo "Syncing to ${HOST}:~/${DEST}/"
echo "From: ${ROOT}"
echo ""

ssh -o ConnectTimeout=15 -o AddressFamily=inet "${HOST}" "mkdir -p ~/${DEST}"

rsync -avz --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'node_modules/' \
  --exclude 'dashboard/node_modules/' \
  --exclude 'src/data/logs/*.log' \
  --exclude 'src/data/.ig_agent_v25.lock' \
  --exclude '**/.DS_Store' \
  --exclude '**/*.icloud' \
  "${ROOT}/" "${HOST}:~/${DEST}/"

echo ""
echo "Sync complete."
echo "Next on Mini:"
echo "  ssh ${HOST}"
echo "  cd ~/${DEST} && bash scripts/setup_mac_mini.sh"
echo ""
