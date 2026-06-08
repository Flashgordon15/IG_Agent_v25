"""In-process v26 shadow tail — starts with the agent (no second terminal)."""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import Any

from system.engine_log import log_engine
from system.paths import project_root

_thread: threading.Thread | None = None
_refresh_thread: threading.Thread | None = None
_stop = threading.Event()


def _ensure_v26_path() -> None:
    v26 = project_root() / "v26"
    if str(v26) not in sys.path:
        sys.path.insert(0, str(v26))


def _shadow_settings() -> dict[str, Any]:
    try:
        from system.v26_config import load_v26_overlay

        block = load_v26_overlay().get("shadow_service") or {}
    except Exception:
        block = {}
    return {
        "enabled": bool(block.get("enabled", True)),
        "tail_poll_sec": float(block.get("tail_poll_sec") or 2.0),
        "snapshot_refresh_minutes": int(block.get("snapshot_refresh_minutes") or 60),
        "skip_in_pytest": bool(block.get("skip_in_pytest", True)),
        "catch_up_on_start": bool(block.get("catch_up_on_start", True)),
        "persist_offsets": bool(block.get("persist_offsets", True)),
    }


def _should_run() -> bool:
    cfg = _shadow_settings()
    if not cfg.get("enabled"):
        return False
    if cfg.get("skip_in_pytest") and os.environ.get("IG_AGENT_PYTEST") == "1":
        return False
    if os.environ.get("IG_AGENT_SHADOW", "").strip().lower() in ("0", "false", "no"):
        return False
    return True


def _feeder_path(day: str):
    _ensure_v26_path()
    from ingest.lake_reader import events_dir

    return events_dir() / f"{day}.jsonl"


def _feeder_offset_eof(day: str) -> int:
    path = _feeder_path(day)
    if path.is_file():
        return path.stat().st_size
    return 0


def _initial_offset(day: str) -> int:
    """Resume from persisted offset, replay day from 0, or EOF (live-only)."""
    cfg = _shadow_settings()
    if cfg.get("catch_up_on_start"):
        if cfg.get("persist_offsets"):
            try:
                from system.v26_shadow_offsets import load_offset

                saved = load_offset(day)
                if saved is not None:
                    log_engine(
                        f"v26_shadow_service: catch-up {day} from saved offset {saved}"
                    )
                    return saved
            except Exception:
                pass
        _ensure_v26_path()
        shadow_path = project_root() / "data_lake" / "shadow_v26" / f"{day}.jsonl"
        if shadow_path.is_file() and shadow_path.stat().st_size > 0:
            log_engine(
                f"v26_shadow_service: catch-up {day} from offset 0 "
                "(shadow exists, no saved offset)"
            )
            return 0
    eof = _feeder_offset_eof(day)
    log_engine(f"v26_shadow_service: tail {day} from EOF ({eof})")
    return eof


def _tail_loop() -> None:
    _ensure_v26_path()
    from ingest.lake_reader import event_utc_day, utc_today
    from shadow.runner import process_event

    cfg = _shadow_settings()
    poll = float(cfg.get("tail_poll_sec") or 2.0)
    persist_offsets = bool(cfg.get("persist_offsets"))
    offsets: dict[str, int] = {}
    warmed_days: set[str] = set()

    log_engine(
        f"v26_shadow_service: tail started (poll={poll}s, catch_up="
        f"{cfg.get('catch_up_on_start')}, persist_offsets={persist_offsets})"
    )
    while not _stop.is_set():
        try:
            active_day = utc_today()
            if active_day not in offsets:
                if active_day not in warmed_days:
                    from shadow.runner import warm_seen_from_shadow_day

                    seen_n = warm_seen_from_shadow_day(active_day)
                    if seen_n:
                        log_engine(
                            f"v26_shadow_service: warmed {seen_n} dedupe keys "
                            f"from shadow {active_day}"
                        )
                    warmed_days.add(active_day)
                offsets[active_day] = _initial_offset(active_day)
            path = _feeder_path(active_day)
            offset = offsets.get(active_day, 0)
            if path.is_file():
                with open(path, encoding="utf-8") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offsets[active_day] = f.tell()
                if persist_offsets and chunk:
                    try:
                        from system.v26_shadow_offsets import save_offset

                        save_offset(active_day, offsets[active_day])
                    except Exception:
                        pass
                for line in chunk.splitlines():
                    if _stop.is_set():
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        process_event(event, day=event_utc_day(event))
        except Exception as exc:
            log_engine(f"v26_shadow_service: {type(exc).__name__}: {exc}")
        _stop.wait(poll)
    log_engine("v26_shadow_service: tail stopped")


def _refresh_loop() -> None:
    cfg = _shadow_settings()
    interval = max(5, int(cfg.get("snapshot_refresh_minutes") or 60)) * 60
    log_engine(f"v26_shadow_service: snapshot refresh every {interval // 60}m")
    while not _stop.is_set():
        if _stop.wait(interval):
            break
        try:
            _ensure_v26_path()
            from datetime import datetime, timezone

            from research.trade_learning import write_trade_learning_snapshot

            write_trade_learning_snapshot()
            try:
                from research.learning_engine import write_learning_snapshot

                write_learning_snapshot()
            except Exception as exc:
                log_engine(
                    f"v26_shadow_service: learning snapshot: {type(exc).__name__}: {exc}"
                )
            try:
                from research.l4_forward import write_forward_cert

                write_forward_cert()
            except Exception as exc:
                log_engine(
                    f"v26_shadow_service: forward cert: {type(exc).__name__}: {exc}"
                )
            try:
                from research.gate_relaxation_report import write_gate_relaxation_report

                write_gate_relaxation_report(days=7)
            except Exception as exc:
                log_engine(
                    f"v26_shadow_service: gate relaxation: {type(exc).__name__}: {exc}"
                )
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            root = project_root()
            import subprocess

            subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts" / "v26_progress.py"),
                    "--day",
                    day,
                    "--write",
                ],
                cwd=str(root),
                env={**os.environ, "PYTHONPATH": "src:v26"},
                timeout=120,
                check=False,
            )
            log_engine("v26_shadow_service: progress snapshot refreshed")
        except Exception as exc:
            log_engine(f"v26_shadow_service refresh: {type(exc).__name__}: {exc}")
    log_engine("v26_shadow_service: refresh stopped")


def start_v26_shadow_service() -> None:
    """Start daemon shadow tail (+ optional snapshot refresh). Idempotent."""
    global _thread, _refresh_thread
    if not _should_run():
        log_engine("v26_shadow_service: disabled (config or test env)")
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(
        target=_tail_loop,
        name="v26-shadow-tail",
        daemon=True,
    )
    _thread.start()
    cfg = _shadow_settings()
    if int(cfg.get("snapshot_refresh_minutes") or 0) > 0:
        if _refresh_thread is None or not _refresh_thread.is_alive():
            _refresh_thread = threading.Thread(
                target=_refresh_loop,
                name="v26-shadow-refresh",
                daemon=True,
            )
            _refresh_thread.start()


def stop_v26_shadow_service() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=3.0)
    if _refresh_thread is not None:
        _refresh_thread.join(timeout=3.0)


def reset_v26_shadow_service_for_tests() -> None:
    stop_v26_shadow_service()
    global _thread, _refresh_thread
    _thread = None
    _refresh_thread = None
    _stop.clear()
