"""
Pre-flight checks for a running IG Agent — log integrity, gate activity, live data.

Used by scripts/pre_flight_check.py and e2e platform validation layer 7.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from system.gate_activity import seconds_since_last_gate_eval
from system.paths import logs_dir

_BST = ZoneInfo("Europe/London")
_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_GATE_LINE = re.compile(
    r"WAIT —|gate .* error — WAIT:|stream_ready:|trading_loop started|orchestrator trading loop started"
)
_MAGICMOCK = re.compile(r"MagicMock|<MagicMock")
_SUMMARY_HEADER = re.compile(
    r"^IG Agent v(?:25|29(?:\.\d+)?)\s+—\s+Session Summary",
    re.MULTILINE,
)
_SUMMARY_REQUIRED = (
    "Trades:",
    "Final state:",
    "Stream uptime:",
)
_STREAM_READY_PATTERNS = (
    "stream_ready: market stream live",
    "stream_ready: hub quotes already live",
    "stream_ready_timeout",
    "timeout_proceed",
    "test_mode_no_stream",
)


@dataclass(frozen=True)
class PreFlightResult:
    check_id: str
    description: str
    passed: bool
    reason: str = ""


def _parse_log_ts(line: str) -> datetime | None:
    m = _LOG_TS.match(line.strip())
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _read_log_tail(path: Path, *, max_lines: int = 4000) -> list[str]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return lines


def _latest_gate_log_age_sec(
    *, now: datetime | None = None
) -> tuple[float | None, str]:
    """Return (age_seconds, source) from engine/launcher logs, newest first."""
    now = now or datetime.now()
    best_age: float | None = None
    best_src = ""
    for name in ("engine.log", "launcher.log"):
        path = logs_dir() / name
        for line in reversed(_read_log_tail(path)):
            if not _GATE_LINE.search(line):
                continue
            ts = _parse_log_ts(line)
            if ts is None:
                continue
            age = (now - ts).total_seconds()
            if age < 0:
                age = 0.0
            if best_age is None or age < best_age:
                best_age = age
                best_src = name
    return best_age, best_src


def check_anti_mock_session_summaries(logs: Path | None = None) -> PreFlightResult:
    """Fail if any recent session_summary_*.txt contains unittest MagicMock strings."""
    root = logs or logs_dir()
    now = datetime.now()
    bad: list[str] = []
    for path in sorted(root.glob("session_summary_*.txt")):
        try:
            age_h = (
                now - datetime.fromtimestamp(path.stat().st_mtime)
            ).total_seconds() / 3600.0
            if age_h > 48.0:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            bad.append(f"{path.name}: read error {e}")
            continue
        if _MAGICMOCK.search(text):
            bad.append(path.name)
    if bad:
        return PreFlightResult(
            "7.1",
            "Session summaries free of MagicMock (no test pollution)",
            False,
            reason=f"polluted files: {', '.join(bad)}",
        )
    return PreFlightResult(
        "7.1",
        "Session summaries free of MagicMock (no test pollution)",
        True,
    )


def check_session_summary_integrity(logs: Path | None = None) -> PreFlightResult:
    root = logs or logs_dir()
    files = sorted(root.glob("session_summary_*.txt"))
    if not files:
        return PreFlightResult(
            "7.2",
            "Session summary files well-formed (when present)",
            True,
            reason="no session summary files yet",
        )
    latest = files[-1]
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return PreFlightResult(
            "7.2",
            "Session summary files well-formed (when present)",
            False,
            reason=str(e),
        )
    missing = [needle for needle in _SUMMARY_REQUIRED if needle not in text]
    if not _SUMMARY_HEADER.search(text):
        missing.append("IG Agent session summary header")
    if _MAGICMOCK.search(text):
        missing.append("MagicMock contamination")
    if missing:
        return PreFlightResult(
            "7.2",
            "Session summary files well-formed (when present)",
            False,
            reason=f"{latest.name}: missing/invalid {missing}",
        )
    return PreFlightResult(
        "7.2",
        "Session summary files well-formed (when present)",
        True,
        reason=latest.name,
    )


def check_gate_evaluation_recent(*, max_age_sec: float = 60.0) -> PreFlightResult:
    """Gate activity from in-process tracker or log tail."""
    in_proc = seconds_since_last_gate_eval()
    if in_proc is not None and in_proc <= max_age_sec:
        return PreFlightResult(
            "7.3",
            f"Last gate evaluation within {int(max_age_sec)}s",
            True,
            reason=f"in-process {in_proc:.0f}s ago",
        )
    log_age, src = _latest_gate_log_age_sec()
    if log_age is not None and log_age <= max_age_sec:
        return PreFlightResult(
            "7.3",
            f"Last gate evaluation within {int(max_age_sec)}s",
            True,
            reason=f"{src} {log_age:.0f}s ago",
        )
    detail = "no recent gate activity"
    if in_proc is not None:
        detail = f"in-process {in_proc:.0f}s ago"
    elif log_age is not None:
        detail = f"{src} {log_age:.0f}s ago"
    return PreFlightResult(
        "7.3",
        f"Last gate evaluation within {int(max_age_sec)}s",
        False,
        reason=detail,
    )


def check_live_data_recent(*, max_tick_age_sec: float = 60.0) -> PreFlightResult:
    """Hub or dashboard snapshot has a fresh quote."""
    try:
        from system.market_data_hub import get_market_data_hub

        hub = get_market_data_hub()
        freshest: float | None = None
        epic_label = ""
        for epic in hub.list_epics():
            snap = hub.get_snapshot(epic)
            if snap is None or snap.bid <= 0:
                continue
            age = float(snap.age_seconds())
            if freshest is None or age < freshest:
                freshest = age
                epic_label = epic
        if freshest is not None and freshest <= max_tick_age_sec:
            return PreFlightResult(
                "7.4",
                f"Live market data fresh (<{int(max_tick_age_sec)}s)",
                True,
                reason=f"{epic_label} age={freshest:.1f}s",
            )
    except Exception as e:
        hub_err = f"{type(e).__name__}: {e}"
    else:
        hub_err = "no hub snapshots"

    try:
        from api.snapshot_store import get_tick

        tick = get_tick()
        age = tick.get("tick_age_s")
        epic = tick.get("epic") or tick.get("selected_epic") or "?"
        if age is not None and float(age) <= max_tick_age_sec:
            return PreFlightResult(
                "7.4",
                f"Live market data fresh (<{int(max_tick_age_sec)}s)",
                True,
                reason=f"dashboard {epic} age={float(age):.1f}s",
            )
    except Exception:
        pass

    return PreFlightResult(
        "7.4",
        f"Live market data fresh (<{int(max_tick_age_sec)}s)",
        False,
        reason=hub_err,
    )


def _find_stream_ready_log_line(
    *,
    within_minutes: float | None = 10.0,
) -> tuple[str, str] | None:
    """Return (log_name, line) when a stream_ready marker is found."""
    cutoff = (
        datetime.now() - timedelta(minutes=within_minutes)
        if within_minutes is not None
        else None
    )
    for name in ("engine.log", "launcher.log"):
        path = logs_dir() / name
        for line in reversed(_read_log_tail(path)):
            ts = _parse_log_ts(line)
            if cutoff is not None and ts is not None and ts < cutoff:
                break
            if any(p in line for p in _STREAM_READY_PATTERNS):
                return name, line
    return None


def check_startup_stream_gate_log(*, within_minutes: float = 10.0) -> PreFlightResult:
    """After startup, stream_ready or timeout proceed should appear in logs."""
    try:
        from system.stream_ready import is_stream_ready

        if is_stream_ready():
            return PreFlightResult(
                "7.5",
                "Startup stream_ready gate logged",
                True,
                reason="in-process stream_ready=True",
            )
    except Exception:
        pass

    hit = _find_stream_ready_log_line(within_minutes=within_minutes)
    if hit is not None:
        name, line = hit
        return PreFlightResult(
            "7.5",
            "Startup stream_ready gate logged",
            True,
            reason=f"{name}: {line.split('|', 1)[-1].strip()[:80]}",
        )

    # Warm 24/7 host: agent may have booted hours ago — accept any recent log marker.
    hit = _find_stream_ready_log_line(within_minutes=None)
    if hit is not None:
        name, line = hit
        return PreFlightResult(
            "7.5",
            "Startup stream_ready gate logged",
            True,
            reason=f"{name} (historical): {line.split('|', 1)[-1].strip()[:80]}",
        )

    return PreFlightResult(
        "7.5",
        "Startup stream_ready gate logged",
        False,
        reason=f"no stream_ready line in last {within_minutes:.0f} min",
    )


def check_gate_coherence() -> PreFlightResult:
    """Config/rules alignment — portfolio caps, points, sessions, ml_veto."""
    try:
        from data.learning_store import LearningStore
        from system.config_loader import ConfigLoader
        from system.gate_coherence import audit_trading_readiness
        from system.paths import data_dir
        from trading.points_engine import PointsEngine

        cfg = ConfigLoader().load()
        store = LearningStore(str(data_dir() / "learning_db.sqlite3"))
        store.connect()
        points = PointsEngine(store)
        report = audit_trading_readiness(
            cfg,
            store,
            points_state=points.get_state(),
            repair_db=True,
        )
        store.close()
        if report.critical:
            first = report.critical[0]
            return PreFlightResult(
                "7.0",
                "Gate coherence (config/rules aligned)",
                False,
                reason=f"{first.code}: {first.message}",
            )
        warn = report.warnings[0].message if report.warnings else ""
        return PreFlightResult(
            "7.0",
            "Gate coherence (config/rules aligned)",
            True,
            reason=warn or f"{len(report.issues)} checks ok",
        )
    except Exception as e:
        return PreFlightResult(
            "7.0",
            "Gate coherence (config/rules aligned)",
            False,
            reason=f"{type(e).__name__}: {e}",
        )


def check_ohlc_cache_warm() -> PreFlightResult:
    """Enabled instruments should have >=100 bars in local OHLC cache."""
    try:
        from system.config_loader import get_config
        from trading.instrument_registry import InstrumentRegistry
        from trading.ohlc_bootstrap import (
            MIN_CACHE_BARS_FOR_BOOTSTRAP,
            local_cache_bar_count,
        )

        cfg = get_config()
        registry = InstrumentRegistry(cfg.as_dict())
        gaps: list[str] = []
        for iid, inst in registry.get_enabled_with_ids():
            epic = str(inst.get("epic") or "").strip()
            if not epic:
                continue
            market = str(inst.get("name") or iid)
            bars = local_cache_bar_count(epic, market)
            if bars < MIN_CACHE_BARS_FOR_BOOTSTRAP:
                gaps.append(f"{iid}({bars}/{MIN_CACHE_BARS_FOR_BOOTSTRAP})")
        if gaps:
            return PreFlightResult(
                "7.3",
                "OHLC local cache warm (≥100 bars per enabled market)",
                False,
                reason="cold: " + ", ".join(gaps),
            )
        return PreFlightResult(
            "7.3",
            "OHLC local cache warm (≥100 bars per enabled market)",
            True,
            reason=f"{len(registry.get_enabled_with_ids())} markets ok",
        )
    except Exception as e:
        return PreFlightResult(
            "7.3",
            "OHLC local cache warm (≥100 bars per enabled market)",
            False,
            reason=f"{type(e).__name__}: {e}",
        )


def run_all_pre_flight_checks(
    *,
    require_live_agent: bool = False,
    max_gate_age_sec: float = 60.0,
) -> list[PreFlightResult]:
    results = [
        check_gate_coherence(),
        check_anti_mock_session_summaries(),
        check_session_summary_integrity(),
        check_ohlc_cache_warm(),
    ]
    if require_live_agent:
        results.extend(
            [
                check_gate_evaluation_recent(max_age_sec=max_gate_age_sec),
                check_live_data_recent(),
                check_startup_stream_gate_log(),
            ]
        )
    return results


def pre_flight_summary(results: list[PreFlightResult]) -> dict[str, Any]:
    passed = sum(1 for r in results if r.passed)
    return {
        "passed": passed,
        "total": len(results),
        "ok": all(r.passed for r in results),
        "results": [
            {
                "id": r.check_id,
                "description": r.description,
                "passed": r.passed,
                "reason": r.reason,
            }
            for r in results
        ],
    }
