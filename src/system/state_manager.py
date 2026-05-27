"""
Atomic agent state persistence for v25.

All durable component state uses temp-file + fsync + os.replace writes.
Load failures log once and return safe in-memory defaults — never block startup.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from system.engine_log import log_engine
from system.paths import data_dir

STATE_VERSION = 1
DEFAULT_STATE_FILENAME = "agent_state.json"

DumpFn = Callable[[], dict[str, Any]]
LoadFn = Callable[[dict[str, Any]], None]

_lock = threading.RLock()
_path_override: Path | None = None
_last_save_ts: float = 0.0
_min_write_interval_sec: float = 0.05
_autosave_interval_sec: float = 30.0
_last_autosave_ts: float = 0.0
_corrupt_warned: bool = False

_document: dict[str, Any] = {
    "version": STATE_VERSION,
    "saved_at": 0.0,
    "sections": {},
}
_collectors: dict[str, tuple[DumpFn, LoadFn]] = {}


def default_state_path() -> Path:
    if _path_override is not None:
        return _path_override
    return data_dir() / DEFAULT_STATE_FILENAME


def set_state_path_for_tests(path: Path | str | None) -> None:
    """Override persistence path (tests only)."""
    global _path_override, _last_save_ts, _last_autosave_ts, _corrupt_warned
    with _lock:
        _path_override = Path(path) if path else None
        _last_save_ts = 0.0
        _last_autosave_ts = 0.0
        _corrupt_warned = False


def reset_state_manager_for_tests() -> None:
    """Clear collectors, in-memory document, and test overrides."""
    global _last_save_ts, _last_autosave_ts, _corrupt_warned, _document
    with _lock:
        _path_override = None
        _last_save_ts = 0.0
        _last_autosave_ts = 0.0
        _corrupt_warned = False
        _document = {
            "version": STATE_VERSION,
            "saved_at": 0.0,
            "sections": {},
        }
        _collectors.clear()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically: mkstemp in target dir, fsync, os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".state_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json_file(path: Path) -> dict[str, Any] | None:
    """Read JSON dict from path. Returns None if missing or corrupt."""
    global _corrupt_warned
    p = Path(path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("root not a dict")
        return data
    except Exception as e:
        with _lock:
            warned = _corrupt_warned
            _corrupt_warned = True
        if not warned:
            log_engine(
                f"state_manager read failed (corrupt — using defaults): "
                f"{type(e).__name__}: {e}"
            )
        return None


def register_section(name: str, dump_fn: DumpFn, load_fn: LoadFn) -> None:
    """Register a named section for automatic save/load."""
    with _lock:
        _collectors[str(name)] = (dump_fn, load_fn)


def unregister_section(name: str) -> None:
    with _lock:
        _collectors.pop(str(name), None)


def get_section(name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a copy of a static section from the in-memory document."""
    with _lock:
        sections = _document.get("sections") or {}
        raw = sections.get(name)
        if isinstance(raw, dict):
            return deepcopy(raw)
    return deepcopy(default) if default is not None else {}


def set_section(name: str, payload: dict[str, Any]) -> None:
    """Set a static section in the in-memory document (call save to persist)."""
    with _lock:
        sections = _document.setdefault("sections", {})
        if not isinstance(sections, dict):
            sections = {}
            _document["sections"] = sections
        sections[str(name)] = deepcopy(payload)


def _build_document() -> dict[str, Any]:
    with _lock:
        sections: dict[str, Any] = {}
        static = _document.get("sections")
        if isinstance(static, dict):
            sections.update(deepcopy(static))
        collectors = dict(_collectors)
    for name, (dump_fn, _) in collectors.items():
        try:
            payload = dump_fn()
            if isinstance(payload, dict):
                sections[name] = payload
        except Exception as e:
            log_engine(
                f"state_manager section dump '{name}' failed: "
                f"{type(e).__name__}: {e}"
            )
    return {
        "version": STATE_VERSION,
        "saved_at": time.time(),
        "sections": sections,
    }


def _apply_document(data: dict[str, Any]) -> None:
    global _document
    sections = data.get("sections")
    if not isinstance(sections, dict):
        sections = {}
    with _lock:
        _document = {
            "version": int(data.get("version", STATE_VERSION)),
            "saved_at": float(data.get("saved_at", 0.0)),
            "sections": deepcopy(sections),
        }
        collectors = dict(_collectors)
    for name, (_, load_fn) in collectors.items():
        try:
            load_fn(sections.get(name) or {})
        except Exception as e:
            log_engine(
                f"state_manager section load '{name}' failed: "
                f"{type(e).__name__}: {e}"
            )


def request_save() -> None:
    """Persist state; throttled to avoid write storms during bursts."""
    global _last_save_ts
    now = time.time()
    with _lock:
        if now - _last_save_ts < _min_write_interval_sec:
            return
        _last_save_ts = now
    try:
        atomic_write_json(default_state_path(), _build_document())
    except Exception as e:
        log_engine(f"state_manager save failed: {type(e).__name__}: {e}")


def flush_save() -> None:
    """Write immediately, ignoring throttle."""
    global _last_save_ts, _last_autosave_ts
    try:
        atomic_write_json(default_state_path(), _build_document())
    except Exception as e:
        log_engine(f"state_manager flush failed: {type(e).__name__}: {e}")
        return
    now = time.time()
    with _lock:
        _last_save_ts = now
        _last_autosave_ts = now


def maybe_autosave(interval_sec: float | None = None) -> None:
    """Persist if at least *interval_sec* (default 30s) since last flush."""
    global _last_autosave_ts
    interval = _autosave_interval_sec if interval_sec is None else float(interval_sec)
    now = time.time()
    with _lock:
        if now - _last_autosave_ts < interval:
            return
    flush_save()


def load_state() -> bool:
    """Load disk state into memory and registered collectors. Never raises."""
    data = read_json_file(default_state_path())
    if data is None:
        return False
    try:
        _apply_document(data)
        return True
    except Exception as e:
        log_engine(f"state_manager apply failed: {type(e).__name__}: {e}")
        return False


class StateManager:
    """Optional OO wrapper for a dedicated state file path."""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else default_state_path()

    @property
    def path(self) -> Path:
        return self._path

    def get_section(self, name: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        return get_section(name, default)

    def set_section(self, name: str, payload: dict[str, Any]) -> None:
        set_section(name, payload)

    def load(self) -> bool:
        prev = _path_override
        set_state_path_for_tests(self._path)
        try:
            return load_state()
        finally:
            set_state_path_for_tests(prev)

    def save(self) -> None:
        prev = _path_override
        set_state_path_for_tests(self._path)
        try:
            flush_save()
        finally:
            set_state_path_for_tests(prev)

    def read_file(self) -> dict[str, Any] | None:
        return read_json_file(self._path)

    def write_file(self, data: dict[str, Any]) -> None:
        atomic_write_json(self._path, data)
