#!/usr/bin/env bash
# Test Mac Mini SSH from this Mac. Run on MacBook Pro:
#   bash scripts/mac_mini_connect_check.sh
# Optional: bash scripts/mac_mini_connect_check.sh 192.168.6.238

set -euo pipefail

HOST_ALIAS="${MAC_MINI_SSH_HOST:-mac-mini}"
HOSTNAME="${MAC_MINI_HOSTNAME:-Chriss-Mac-mini.local}"
USER_NAME="${MAC_MINI_USER:-$(whoami)}"
IP_OVERRIDE="${1:-}"

echo ""
echo "Mac Mini connection check"
echo "========================="
echo "User:     ${USER_NAME}"
echo "Hostname: ${HOSTNAME}"
echo "SSH host: ${HOST_ALIAS} (~/.ssh/config)"
echo ""

pass() { echo "  OK   $*"; }
fail() { echo "  FAIL $*"; }
hint() { echo "       → $*"; }

resolve_ip() {
  if [[ -n "${IP_OVERRIDE}" ]]; then
    echo "${IP_OVERRIDE}"
    return
  fi
  python3 - <<PY 2>/dev/null || true
import socket
try:
    print(socket.gethostbyname("${HOSTNAME}"))
except OSError:
    pass
PY
}

IP="$(resolve_ip)"
if [[ -n "${IP}" ]]; then
  echo "Resolved IP: ${IP}"
else
  fail "Could not resolve ${HOSTNAME}"
  hint "On the Mini: System Settings → Sharing → Remote Login → ON"
  hint "Note the IP shown there, then run: bash scripts/mac_mini_connect_check.sh <that-ip>"
  exit 1
fi

if nc -z -G 5 "${IP}" 22 2>/dev/null; then
  pass "Port 22 open on ${IP}"
else
  fail "Port 22 not reachable on ${IP}"
  hint "Mini awake? Same Wi‑Fi as this Mac (not Guest)? Remote Login ON?"
  hint "Try: ping ${IP}"
  exit 1
fi

if ssh -o BatchMode=yes -o ConnectTimeout=10 -o AddressFamily=inet \
  "${USER_NAME}@${IP}" "echo connected && hostname && pwd" 2>/dev/null; then
  pass "SSH login works (${USER_NAME}@${IP})"
else
  fail "SSH login failed (${USER_NAME}@${IP})"
  hint "First time: ssh ${USER_NAME}@${IP}  (accept host key, enter password)"
  hint "Passwordless: ssh-copy-id ${USER_NAME}@${IP}"
  exit 1
fi

if ssh -o ConnectTimeout=10 -o AddressFamily=inet "${HOST_ALIAS}" "echo via-alias-ok" 2>/dev/null; then
  pass "SSH alias '${HOST_ALIAS}' works"
else
  echo ""
  echo "Alias '${HOST_ALIAS}' failed — updating ~/.ssh/config with IP ${IP}..."
  CONFIG="${HOME}/.ssh/config"
  mkdir -p "${HOME}/.ssh"
  chmod 700 "${HOME}/.ssh"
  if [[ -f "${CONFIG}" ]] && grep -q "^Host ${HOST_ALIAS}$" "${CONFIG}"; then
    # Replace HostName line under mac-mini block (simple sed)
    awk -v ip="${IP}" '
      /^Host '"${HOST_ALIAS}"'$/ { inblock=1 }
      inblock && /^[[:space:]]*HostName / { print "    HostName " ip; next }
      inblock && /^Host / && $2 != "'"${HOST_ALIAS}"'" { inblock=0 }
      { print }
    ' "${CONFIG}" > "${CONFIG}.tmp" && mv "${CONFIG}.tmp" "${CONFIG}"
  else
    cat >> "${CONFIG}" <<EOF

Host ${HOST_ALIAS}
    HostName ${IP}
    User ${USER_NAME}
    AddressFamily inet
EOF
  fi
  chmod 600 "${CONFIG}"
  if ssh -o ConnectTimeout=10 -o AddressFamily=inet "${HOST_ALIAS}" "echo via-alias-ok" 2>/dev/null; then
    pass "SSH alias '${HOST_ALIAS}' fixed"
  else
    fail "Could not fix alias — use: ssh ${USER_NAME}@${IP}"
    exit 1
  fi
fi

echo ""
echo "Ready for Cursor Remote-SSH"
echo "  1. Cmd+Shift+P → Remote-SSH: Connect to Host → ${HOST_ALIAS}"
echo "  2. Open folder: ~/Projects/IG_Agent_v25"
echo ""
echo "Copy project to Mini (from MacBook, project root):"
echo "  bash scripts/mac_mini_sync.sh"
echo ""
echo "Then on Mini (SSH or Cursor terminal):"
echo "  cd ~/Projects/IG_Agent_v25 && bash scripts/setup_mac_mini.sh"
echo ""
