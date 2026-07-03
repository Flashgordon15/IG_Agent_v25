#!/bin/bash
# Writes logs/launcher_status.json for the native splash to poll.
# shellcheck shell=bash

launcher_status_init() {
  LAUNCHER_SESSION_ID="${LAUNCHER_SESSION_ID:-$(uuidgen 2>/dev/null || echo "launch-$(date +%s)")}"
  export LAUNCHER_SESSION_ID
  launcher_status_set "init" "Launch session started" "Isolated clean boot" 0 9 ""
}

launcher_status_set() {
  local stage="$1" status="$2" detail="$3"
  local step="${4:-0}" total="${5:-9}" error="${6:-}" boot_tier="${7:-}"
  export _LS_STAGE="${stage}" _LS_STATUS="${status}" _LS_DETAIL="${detail}"
  export _LS_STEP="${step}" _LS_TOTAL="${total}" _LS_ERROR="${error}"
  export _LS_BOOT_TIER="${boot_tier}"
  "${IG_AGENT_PY}" - <<'PY' 2>/dev/null || true
import json, os
from datetime import datetime, timezone
from pathlib import Path

root = os.environ.get("IG_AGENT_ROOT", ".")
p = Path(root) / "logs" / "launcher_status.json"
err = os.environ.get("_LS_ERROR", "").strip()
stage = os.environ.get("_LS_STAGE", "")
tier = os.environ.get("_LS_BOOT_TIER", "").strip()
data = {
    "session_id": os.environ.get("LAUNCHER_SESSION_ID", ""),
    "stage": stage,
    "step": int(os.environ.get("_LS_STEP", "0") or 0),
    "total_steps": int(os.environ.get("_LS_TOTAL", "9") or 9),
    "status": os.environ.get("_LS_STATUS", ""),
    "detail": os.environ.get("_LS_DETAIL", ""),
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "ok": stage != "failed",
    "error": err or None,
}
if tier:
    data["boot_tier"] = tier
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(data, indent=2) + "\n")
PY
}

launcher_status_fail() {
  local status="$1" detail="$2" step="${3:-0}"
  launcher_status_set "failed" "${status}" "${detail}" "${step}" 9 "${detail}"
}
