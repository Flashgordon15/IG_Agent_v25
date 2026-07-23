#!/usr/bin/env bash
# v34 forensic ping — read-only desk health / leak probe (no orders, no kills).
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_ROOT="${IG_AGENT_DATA_DIR:-$ROOT/src/data/v31-production}"
PORTS=(8080 8081)
TIMEOUT=3

hr() { printf '%.0s-' {1..120}; echo; }

json_field() {
  local json="$1" key="$2" default="${3:-}"
  printf '%s' "$json" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    v = d
    for part in '''$key'''.split('.'):
        if isinstance(v, dict):
            v = v.get(part)
        else:
            v = None
            break
    if v is None:
        print('''$default''')
    else:
        print(v)
except Exception:
    print('''$default''')
" 2>/dev/null || echo "$default"
}

curl_json() {
  local url="$1"
  curl -sS --max-time "$TIMEOUT" -H 'Accept: application/json' "$url" 2>/dev/null || echo '{}'
}

human_bytes() {
  local n="${1:-0}"
  python3 -c "
n = int('''$n''' or 0)
for u in ('B','KB','MB','GB'):
    if n < 1024 or u == 'GB':
        print(f'{n:.0f}{u}' if u == 'B' else f'{n:.1f}{u}')
        break
    n /= 1024
" 2>/dev/null || echo "${n}B"
}

pid_rss_kb() {
  local pid="$1"
  if [[ -z "$pid" || "$pid" == "0" || ! "$pid" =~ ^[0-9]+$ ]]; then
    echo "n/a"
    return
  fi
  if ps -p "$pid" -o rss= >/dev/null 2>&1; then
    ps -p "$pid" -o rss= 2>/dev/null | awk '{printf "%.1fMB", $1/1024}'
  else
    echo "dead"
  fi
}

port_pid() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true
}

printf '\nIG Agent v34 forensic ping  %s\n' "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
hr
printf '%-6s %-8s %-8s %-10s %-12s %-8s %-18s %-12s %-10s\n' \
  'PORT' 'HTTP' 'HEALTH' 'ACCOUNT' 'ENGINE' 'PID/RSS' 'REST_BUDGET' 'OPS_OK' 'DETACHED'
hr

for port in "${PORTS[@]}"; do
  base="http://127.0.0.1:${port}"
  health="$(curl_json "${base}/api/health")"
  ops="$(curl_json "${base}/api/desk/ops_strip")"
  budget="$(curl_json "${base}/api/desk/rest_budget")"

  http_code="$(curl -sS --max-time "$TIMEOUT" -o /dev/null -w '%{http_code}' "${base}/api/health" 2>/dev/null || echo '000')"
  health_ok="$(json_field "$health" ok false)"
  account="$(json_field "$health" account_id -)"
  if [[ "$account" == "-" ]]; then
    account="$(json_field "$health" ig_account_id -)"
  fi
  engine="$(json_field "$health" engine_id -)"
  if [[ "$engine" == "-" ]]; then
    engine="$(json_field "$health" active_engine_id -)"
  fi
  ops_ok="$(json_field "$ops" ok false)"
  detached="$(json_field "$ops" core_detached false)"

  util="$(json_field "$budget" utilization_pct -)"
  if [[ "$util" == "-" ]]; then
    util="$(json_field "$budget" rest_budget_pct -)"
  fi
  paused="$(json_field "$budget" paused false)"
  rl="$(json_field "$budget" rate_limited false)"
  budget_label="${util}%"
  if [[ "$paused" == "True" || "$paused" == "true" ]]; then
    budget_label="${budget_label} PAUSED"
  fi
  if [[ "$rl" == "True" || "$rl" == "true" ]]; then
    budget_label="${budget_label} RL"
  fi

  pid="$(port_pid "$port")"
  rss="$(pid_rss_kb "${pid:-}")"
  pid_rss="${pid:-none}/${rss}"

  printf '%-6s %-8s %-8s %-10s %-12s %-8s %-18s %-12s %-10s\n' \
    "$port" "$http_code" "$health_ok" "$account" "$engine" "$pid_rss" "$budget_label" "$ops_ok" "$detached"
done

hr
printf '\nState dir file sizes (broker_snapshot + forensic logs)\n'
hr
printf '%-28s %-12s %-12s\n' 'PATH' 'SIZE' 'MTIME_UTC'
hr

for sub in state state_cfd state_sb; do
  dir="${DATA_ROOT}/${sub}"
  [[ -d "$dir" ]] || continue
  for name in broker_snapshot.json forensic_network.log boot_stage_forensic.log rest_budget_shared.json; do
    f="${dir}/${name}"
    if [[ -f "$f" ]]; then
      sz="$(stat -f '%z' "$f" 2>/dev/null || stat -c '%s' "$f" 2>/dev/null || echo 0)"
      mtime="$(stat -f '%Sm' -t '%Y-%m-%dT%H:%M:%SZ' "$f" 2>/dev/null || stat -c '%y' "$f" 2>/dev/null | cut -d. -f1)"
      printf '%-28s %-12s %-12s\n' "${sub}/${name}" "$(human_bytes "$sz")" "$mtime"
    fi
  done
  # Rotated forensic backup
  if [[ -f "${dir}/forensic_network.log.1" ]]; then
    sz="$(stat -f '%z' "${dir}/forensic_network.log.1" 2>/dev/null || stat -c '%s' "${dir}/forensic_network.log.1" 2>/dev/null || echo 0)"
    printf '%-28s %-12s %-12s\n' "${sub}/forensic_network.log.1" "$(human_bytes "$sz")" "(rotated)"
  fi
done

hr
printf '\nForensic log tail (last 3 order lines per engine, redacted on disk)\n'
hr

for sub in state_cfd state_sb; do
  f="${DATA_ROOT}/${sub}/forensic_network.log"
  if [[ -f "$f" ]]; then
    echo "--- ${sub}/forensic_network.log ---"
    tail -n 3 "$f" 2>/dev/null || true
  fi
done

echo
exit 0
