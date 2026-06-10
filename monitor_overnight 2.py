"""
Overnight monitoring script — captures hourly snapshots of system state.
Run once before bed:  python3 monitor_overnight.py &
Kill it after the 6am analysis:  kill %1  (or just close the terminal)
Snapshots written to: src/data/state/overnight_snapshots.jsonl
"""

import json
import pathlib
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

BASE = pathlib.Path(__file__).parent
SNAP_FILE = BASE / "src/data/state/overnight_snapshots.jsonl"
LOG_DIR = BASE / "src/data/logs"
STATE_DIR = BASE / "src/data/state"
SHADOW_LOG = BASE / "src/data/shadow_log.jsonl"
LEARNING_DB = BASE / "src/data/learning_db.sqlite3"

INTERVAL_SECONDS = 1800  # snapshot every 30 minutes


def bst_now() -> str:
    from datetime import timezone, timedelta
    bst = timezone(timedelta(hours=1))
    return datetime.now(bst).isoformat()


def tail_lines(path: pathlib.Path, n: int = 20) -> list[str]:
    try:
        result = subprocess.run(
            ["tail", "-n", str(n), str(path)],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip().splitlines()
    except Exception:
        return []


def count_lines(path: pathlib.Path) -> int:
    try:
        result = subprocess.run(
            ["wc", "-l", str(path)],
            capture_output=True, text=True, timeout=5
        )
        return int(result.stdout.strip().split()[0])
    except Exception:
        return -1


def shadow_summary_since(entry_count_baseline: int) -> dict:
    """Aggregate new shadow_log entries since baseline."""
    try:
        lines = SHADOW_LOG.read_text().strip().splitlines()
        new_lines = lines[entry_count_baseline:]
        if not new_lines:
            return {"new_entries": 0}

        gate_counts: dict[str, int] = {}
        sessions: dict[str, int] = {}
        fired = 0
        markets: dict[str, int] = {}

        for raw in new_lines:
            try:
                e = json.loads(raw)
            except Exception:
                continue
            gate = e.get("gate_blocked_at") or "PASSED"
            gate_counts[gate] = gate_counts.get(gate, 0) + 1
            sess = e.get("session", "unknown")
            sessions[sess] = sessions.get(sess, 0) + 1
            mkt = e.get("market", "unknown")
            markets[mkt] = markets.get(mkt, 0) + 1
            if e.get("would_have_fired") or gate == "PASSED":
                fired += 1

        return {
            "new_entries": len(new_lines),
            "gate_blocked_at_counts": dict(sorted(gate_counts.items(), key=lambda x: -x[1])[:10]),
            "sessions": sessions,
            "markets_seen": markets,
            "signals_that_would_have_fired": fired,
        }
    except Exception as e:
        return {"error": str(e)}


def trade_audit_new(line_baseline: int) -> list[dict]:
    """Return new trade_audit.log entries since baseline."""
    try:
        path = LOG_DIR / "trade_audit.log"
        lines = path.read_text().strip().splitlines()
        new = lines[line_baseline:]
        parsed = []
        for raw in new:
            parts = raw.split(" | ", 2)
            if len(parts) == 3:
                ts, kind, payload = parts
                try:
                    data = json.loads(payload)
                except Exception:
                    data = {"raw": payload[:200]}
                parsed.append({"ts": ts.strip(), "kind": kind.strip(), "data": data})
        return parsed
    except Exception:
        return []


def learning_db_summary() -> list[dict]:
    try:
        conn = sqlite3.connect(f"file:{LEARNING_DB}?mode=ro", uri=True, timeout=5)
        cur = conn.cursor()
        # Try common schema patterns
        for query in [
            "SELECT setup_key, outcome, SUM(pnl_points) as total_pnl, COUNT(*) as n FROM trades GROUP BY setup_key, outcome ORDER BY n DESC LIMIT 20",
            "SELECT setup_key, result, SUM(pnl) as total_pnl, COUNT(*) as n FROM trades GROUP BY setup_key, result ORDER BY n DESC LIMIT 20",
        ]:
            try:
                rows = cur.execute(query).fetchall()
                cols = [d[0] for d in cur.description]
                conn.close()
                return [dict(zip(cols, r)) for r in rows]
            except Exception:
                continue
        conn.close()
        return []
    except Exception as e:
        return [{"error": str(e)}]


def read_json_safe(path: pathlib.Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def capture_snapshot(seq: int, baseline: dict) -> dict:
    snap = {
        "seq": seq,
        "bst": bst_now(),
        "log_line_counts": {
            "engine_log": count_lines(LOG_DIR / "engine.log"),
            "ig_agent_log": count_lines(LOG_DIR / "ig_agent.log"),
            "trade_audit": count_lines(LOG_DIR / "trade_audit.log"),
        },
        "shadow_since_baseline": shadow_summary_since(baseline["shadow_log_entries"]),
        "trade_audit_new": trade_audit_new(baseline["trade_audit_lines"]),
        "points_state": read_json_safe(STATE_DIR / "points_state.json"),
        "session_state": read_json_safe(STATE_DIR / "session_state.json"),
        "runtime_state_keys": list(read_json_safe(BASE / "src/data/runtime_state.json").keys())[:15],
        "engine_log_tail": tail_lines(LOG_DIR / "engine.log", 15),
        "ig_agent_log_tail": tail_lines(LOG_DIR / "ig_agent.log", 10),
        "learning_db": learning_db_summary(),
    }
    return snap


def main():
    baseline_path = STATE_DIR / "overnight_baseline.json"
    if not baseline_path.exists():
        print("ERROR: overnight_baseline.json not found — run from IG_Agent_v25 directory")
        return

    baseline = json.loads(baseline_path.read_text())
    print(f"[monitor] Started at {bst_now()} BST | interval={INTERVAL_SECONDS}s | baseline shadow={baseline.get('shadow_log_entries')}")
    print(f"[monitor] Snapshots → {SNAP_FILE}")

    SNAP_FILE.parent.mkdir(parents=True, exist_ok=True)

    seq = 0
    while True:
        seq += 1
        try:
            snap = capture_snapshot(seq, baseline)
            with SNAP_FILE.open("a") as f:
                f.write(json.dumps(snap) + "\n")
            pts = snap["points_state"].get("state", "?")
            audit_new = len(snap["trade_audit_new"])
            print(f"[monitor] #{seq} {snap['bst']} | points={pts} | new_audit_events={audit_new}")
        except Exception as e:
            print(f"[monitor] #{seq} ERROR: {e}")

        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
