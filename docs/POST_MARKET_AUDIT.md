# 📊 Post-Market Audit & Performance Playbook (V30 Kernel)

**Purpose:** Run this checklist every morning following a live session to verify system health, evaluate machine learning alpha, and track financial execution metrics.

---

## Step 1: System Longevity & Uptime Scan
Verify that your parallel daemons survived the night and did not undergo unexpected crashes or micro-restarts by executing:
```bash
source scripts/agent_control_aliases.sh
agent-status
```
*   **Audit Checkpoint:** Confirm that the active Process IDs (PIDs) match your launch numbers. If a PID changed, open `/tmp/ig_agent.orchestrator.log` to audit the `ParallelTrackSupervisor` timeline to see what time the sub-process dropped and how many milliseconds the native recovery sequence took to deploy.

---

## Step 2: The Machine Learning Alpha Hurdle Assessment
Extract yesterday's training scores from the archived shadow log files to mathematically prove your AI maintained a true statistical edge over the market:
```bash
zgrep "ShadowEngine" data/logs_archive/ig_agent.shadow_*.log.gz | tail -n 20
```
*   **Audit Checkpoint:** Trace the rolling win-rate edge percentages. Confirm that every single in-memory model hot-swap met your strict `> 2.5%` random-walk baseline threshold before updating your Live Vanguard execution track.

---

## Step 3: Transaction Execution & Trailing-Stop Audit
Verify that your hardcoded risk overlays and dynamic trailing stop boundaries protected capital flawlessly by parsing your completed order files:
```bash
PYTHONPATH=src .venv/bin/python3 -c "import json; print(json.dumps(json.load(open('/tmp/ig_agent_parallel.pids.json')), indent=2))"
```
*   **Audit Checkpoint 1 (The Capital Guard):** Verify that zero trades exceeded your hardcoded position ceiling limit (1.0 lot) and that single-day drawdowns stayed strictly within your 2.0% equity safety margin.
*   **Audit Checkpoint 2 (Trailing Stops):** Open your transaction logs and look for position closures marked with `[STOP-TRIGGERED]`. Verify that when an asset crossed a profit threshold (e.g., a 10% gain), the moving floor of the trailing stop locked in the profit cleanly, demonstrating the "win is a win" core principle.
