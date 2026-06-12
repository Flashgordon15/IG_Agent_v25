"""
Dashboard data helpers — trades, signals, splash, system info (Step 13).
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.closed_trades_display import deduplicate_ig_imports, is_excluded_display_row
from system.paths import data_dir, logs_dir, project_root


def version_json_path() -> Path:
    return data_dir() / "version.json"


def _build_date() -> str:
    root = project_root()
    try:
        out = subprocess.check_output(
            ["git", "log", "-1", "--format=%ci"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if out:
            return out[:10]
    except Exception:
        pass
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


CURRENT_VERSION = "29.1.0"


def read_version_state() -> dict[str, Any]:
    path = version_json_path()
    defaults: dict[str, Any] = {
        "version": CURRENT_VERSION,
        "shown": False,
        "build_date": _build_date(),
        "changelog": [],
    }
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("build_date", _build_date())
            data.setdefault("changelog", [])
            # Auto-show splash when version advances
            if str(data.get("version") or "") != CURRENT_VERSION:
                data["version"] = CURRENT_VERSION
                data["shown"] = False
                data["shown_for_version"] = str(data.get("shown_for_version") or "")
            elif str(data.get("shown_for_version") or "") != CURRENT_VERSION:
                # Shown field is for a different version — force re-show
                data["shown"] = False
            return data
    except Exception:
        pass
    return defaults


def dismiss_splash() -> dict[str, Any]:
    data = read_version_state()
    data["shown"] = True
    data["shown_for_version"] = CURRENT_VERSION
    data["dismissed_at"] = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    )
    path = version_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


def get_closed_trades(limit: int = 10) -> list[dict[str, Any]]:
    """Last *limit* closed trades by close time (no session/today cutoff)."""
    try:
        from data.learning_store import LearningStore
        from system.config_loader import ConfigLoader
        from system.paths import config_dir

        cfg = ConfigLoader(config_dir() / "config_v25.json").load_config()
        store = LearningStore(str(cfg.learning_db))
        want = max(1, int(limit))
        # Over-fetch so SIM/soak exclusions and deduplication still yield *want* rows.
        rows = store.recent_closed_trades(limit=max(want * 8, 80))
        filtered: list[dict[str, Any]] = [
            r for r in rows if not is_excluded_display_row(r)
        ]
        deduped = deduplicate_ig_imports(filtered)
        deduped.sort(key=lambda r: str(r.get("closed_at") or ""), reverse=True)
        out: list[dict[str, Any]] = []
        for row in deduped:
            out.append(_format_trade_row(row))
            if len(out) >= want:
                break
        return out
    except Exception:
        return []


_CONFIRMED_RESULTS = frozenset({"WIN", "LOSS", "BREAKEVEN", "CANCELLED"})
_UNRESOLVED_RESULTS = frozenset({"PENDING", "UNCONFIRMED", "", "NONE"})


def _format_trade_row(row: dict[str, Any]) -> dict[str, Any]:
    pnl_pts = float(row.get("pnl_points") or row.get("pnl") or 0)
    pnl_gbp = row.get("ig_pnl_currency")
    if pnl_gbp is not None:
        pnl_gbp = float(pnl_gbp)
    if row.get("closed_at") is None:
        result = "OPEN"
    elif pnl_gbp is not None:
        # IG-confirmed P&L — derive result from the actual value.
        if pnl_gbp > 0:
            result = "WIN"
        elif pnl_gbp < 0:
            result = "LOSS"
        else:
            result = "BREAKEVEN"
    else:
        # No IG-confirmed P&L yet: fall back to the stored result if it is a
        # known final state; only show PENDING for truly unresolved records.
        stored = str(row.get("result") or "").upper()
        if stored in _CONFIRMED_RESULTS:
            result = stored
        else:
            result = "PENDING"
    points_score = row.get("points_score")
    return {
        "direction": row.get("side") or row.get("direction"),
        "market": row.get("market") or row.get("epic"),
        "closed_at": row.get("closed_at"),
        "confidence": row.get("confidence"),
        "setup": row.get("setup_key") or row.get("setup"),
        "entry": row.get("entry_price") or row.get("entry"),
        "exit": row.get("exit_price") or row.get("exit"),
        "pnl_pts": pnl_pts,
        "pnl_gbp": pnl_gbp,
        "result": result,
        "points_score": points_score,
        "deal_id": row.get("deal_id") or row.get("ig_deal_id"),
        "pending": bool(row.get("pending")),
    }


def get_signal_log(limit: int = 50) -> list[dict[str, Any]]:
    log_path = logs_dir() / "engine.log"
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in reversed(lines[-5000:]):
        if "WAIT —" in line or "signal generated" in line.lower():
            entries.append(_parse_signal_line(line))
        if len(entries) >= limit:
            break
    return entries


def _parse_signal_line(line: str) -> dict[str, Any]:
    ts = line[:19] if len(line) >= 19 else ""
    is_trade = "signal generated" in line.lower()
    badge = "TRADE" if is_trade else "WAIT"
    reason = line
    if "|" in line:
        reason = line.split("|", 1)[-1].strip()
    elif "WAIT —" in line:
        reason = line.split("WAIT —", 1)[-1].strip()
    return {"timestamp": ts, "badge": badge, "reason": reason}


def get_system_info() -> dict[str, Any]:
    root = project_root()
    branch = ""
    commit = ""
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        pass

    ml_path = data_dir() / "ml_training_store.jsonl"
    ml_count = 0
    if ml_path.exists():
        try:
            ml_count = sum(1 for _ in ml_path.open(encoding="utf-8"))
        except OSError:
            pass

    # Caffeinate — macOS sleep-prevention utility
    caffeinate_pid: int | None = None
    try:
        result = subprocess.check_output(
            ["pgrep", "-x", "caffeinate"], text=True, stderr=subprocess.DEVNULL
        ).strip()
        if result:
            caffeinate_pid = int(result.splitlines()[0])
    except Exception:
        pass

    # OHLC cache — count market cache files
    ohlc_dir = data_dir() / "ohlc_cache"
    ohlc_markets_cached = 0
    try:
        ohlc_markets_cached = sum(
            1
            for f in ohlc_dir.iterdir()
            if f.suffix == ".jsonl" and not f.name.endswith(".synthetic")
        )
    except Exception:
        pass

    # Agent uptime
    uptime_s: float | None = None
    try:
        import time

        lock_path = data_dir() / ".ig_agent_v25.lock"
        if lock_path.exists():
            uptime_s = round(time.time() - lock_path.stat().st_mtime, 0)
    except Exception:
        pass

    # Session count — number of completed sessions from learning store
    sessions_passed: int | None = None
    try:
        import sqlite3

        db_path = data_dir() / "learning_db.sqlite3"
        if db_path.exists():
            with sqlite3.connect(str(db_path), timeout=5) as conn:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT date(open_time)) FROM trades "
                    "WHERE result IS NOT NULL AND result != 'OPEN'"
                ).fetchone()
                sessions_passed = int(row[0]) if row else 0
    except Exception:
        pass

    return {
        "branch": branch,
        "commit": commit,
        "ml_store_path": str(ml_path),
        "ml_record_count": ml_count,
        "ml_fields": 26,
        "caffeinate_pid": caffeinate_pid,
        "caffeinate_running": caffeinate_pid is not None,
        "ohlc_markets_cached": ohlc_markets_cached,
        "uptime_s": uptime_s,
        "sessions_passed": sessions_passed,
        "sessions_required": 3,
    }


def run_e2e_execution_check() -> dict[str, Any]:
    """Mock execution pipeline + IG DEMO routing validation (no order placed)."""
    from system.e2e_execution_check import run_e2e_execution_check as _run

    return _run(include_routing=True)


def run_system_tests() -> dict[str, Any]:
    """Run pytest in-process Python (same interpreter as the agent)."""
    import sys

    root = project_root()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=180,
            env={
                **dict(__import__("os").environ),
                "PYTHONPATH": str(root / "src"),
                "IG_AGENT_PYTEST": "1",
            },
        )
        out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        m = re.search(r"(\d+) passed", out)
        passed = int(m.group(1)) if m else 0
        failed_m = re.search(r"(\d+) failed", out)
        failed = int(failed_m.group(1)) if failed_m else 0
        err_m = re.search(r"(\d+) error", out)
        errors = int(err_m.group(1)) if err_m else 0
        summary = out.strip().splitlines()[-1] if out.strip() else ""
        if not summary and proc.returncode != 0:
            summary = (proc.stderr or proc.stdout or "pytest exited non-zero").strip()[
                :240
            ]
        ok = proc.returncode == 0 and failed == 0 and errors == 0
        result: dict[str, Any] = {
            "ok": ok,
            "passed": passed,
            "failed": failed,
            "errors": errors,
            "summary": summary,
        }
        if not ok and failed == 0 and errors == 0 and not summary:
            result["error"] = (
                "pytest did not run (install pytest in this Python or check logs)"
            )
        elif not ok and (proc.stderr or "").strip():
            result["error"] = proc.stderr.strip().splitlines()[-1][:240]
        return result
    except Exception as e:
        return {"ok": False, "error": str(e), "passed": 0, "failed": 0, "errors": 0}


def run_safe_to_leave() -> dict[str, Any]:
    """Run scripts/safe_to_leave.py and return structured check results."""
    import sys

    root = project_root()
    script = root / "scripts" / "safe_to_leave.py"
    if not script.is_file():
        return {
            "ok": False,
            "error": "safe_to_leave.py missing",
            "checks": [],
            "message": "",
        }
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--quick"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            env={
                **dict(__import__("os").environ),
                "PYTHONPATH": str(root / "src"),
                "IG_AGENT_SAFE_TO_LEAVE_QUICK": "1",
                "IG_AGENT_ENSURE_LAUNCHD": "1",
            },
        )
        output = (proc.stdout or "").strip()
        checks: list[dict[str, str]] = []
        message = ""
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("→ "):
                message = stripped[2:].strip()
                continue
            for prefix, status in (
                ("[PASS]", "pass"),
                ("[FAIL]", "fail"),
                ("[SKIP]", "skip"),
            ):
                if stripped.startswith(prefix):
                    body = stripped[len(prefix) :].strip()
                    label, _, detail = body.partition(" — ")
                    checks.append(
                        {
                            "status": status,
                            "label": label,
                            "detail": detail,
                        }
                    )
                    break
        ok = proc.returncode == 0
        from system.overnight_supervision import (
            mark_overnight_armed,
            overnight_supervision_summary,
        )

        supervision = overnight_supervision_summary()
        if ok:
            mark_overnight_armed(source="safe_to_leave_dashboard")
            supervision = overnight_supervision_summary()

        result: dict[str, Any] = {
            "ok": ok,
            "checks": checks,
            "message": message,
            "output": output,
            "supervision": supervision,
            "overnight_armed": bool(supervision.get("overnight_armed")),
            "independent_of_cursor": bool(supervision.get("independent_of_cursor")),
        }
        if not ok and proc.stderr:
            result["error"] = proc.stderr.strip().splitlines()[-1][:240]
        return result
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": "safe_to_leave timed out after 120s",
            "checks": [],
            "message": "",
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "checks": [], "message": ""}
