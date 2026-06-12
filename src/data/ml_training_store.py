"""
Append-only ML training log — one JSON line per IG-confirmed closed trade.

Section 7. File: src/data/ml_training_store.jsonl. Never truncate. Never block the loop.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.closed_trades_display import EXCLUDED_SOURCES, is_excluded_display_row
from system.engine_log import log_engine
from system.paths import data_dir

ML_VERSION = "25.1.0"
DEFAULT_FILENAME = "ml_training_store.jsonl"

REQUIRED_FIELDS = (
    "confidence",
    "confidence_band",
    "setup_name",
    "trend_bias",
    "rsi",
    "atr",
    "spread",
    "volume_regime",
    "session_window",
    "entry_price",
    "entry_time",
    "fitness_score",
    "points_state",
    "size_multiplier",
    "instrument",
    "exit_price",
    "exit_time",
    "pts_pnl",
    "gbp_pnl",
    "exit_reason",
    "result",
    "points_scored",
    "confirmed",
    "deal_id",
    "source",
    "version",
)

_lock = threading.RLock()
_path_override: Path | None = None
_entry_buffer: dict[str, dict[str, Any]] = {}


def default_store_path() -> Path:
    if _path_override is not None:
        return _path_override
    return data_dir() / DEFAULT_FILENAME


def set_store_path_for_tests(path: Path | str | None) -> None:
    global _path_override
    with _lock:
        _path_override = Path(path) if path else None


def reset_ml_training_store_for_tests() -> None:
    global _entry_buffer
    with _lock:
        _entry_buffer.clear()
        _path_override = None


def _is_excluded(deal_id: str, data: dict[str, Any]) -> bool:
    row = dict(data)
    row.setdefault("deal_id", deal_id)
    row.setdefault("ig_deal_id", deal_id)
    if is_excluded_display_row(row):
        return True
    if is_ig_import_setup_key(data.get("setup_name") or data.get("setup_key")):
        return True
    src = str(data.get("source") or "").lower()
    if src in EXCLUDED_SOURCES:
        return True
    ref = str(deal_id or "").upper()
    if ref.startswith("SIM-"):
        return True
    return False


def _normalize_record(
    entry: dict[str, Any], exit_data: dict[str, Any], deal_id: str
) -> dict[str, Any]:
    merged = {**entry, **exit_data}
    record = {
        "confidence": float(merged.get("confidence", 0.0)),
        "confidence_band": str(merged.get("confidence_band", "marginal")),
        "setup_name": str(merged.get("setup_name", "")),
        "trend_bias": str(merged.get("trend_bias", "mixed")),
        "rsi": float(merged.get("rsi", 0.0)),
        "atr": float(merged.get("atr", 0.0)),
        "spread": float(merged.get("spread", 0.0)),
        "volume_regime": str(merged.get("volume_regime", "volnormal")),
        "session_window": str(merged.get("session_window", "")),
        "entry_price": float(merged.get("entry_price", 0.0)),
        "entry_time": str(merged.get("entry_time", "")),
        "fitness_score": float(merged.get("fitness_score", 0.0)),
        "points_state": str(merged.get("points_state", "HEALTHY")),
        "size_multiplier": float(merged.get("size_multiplier", 1.0)),
        "instrument": str(merged.get("instrument", "")),
        "exit_price": float(merged.get("exit_price", 0.0)),
        "exit_time": str(merged.get("exit_time", "")),
        "pts_pnl": float(merged.get("pts_pnl", 0.0)),
        "gbp_pnl": float(merged.get("gbp_pnl", 0.0)),
        "exit_reason": str(merged.get("exit_reason", "")),
        "result": str(merged.get("result", "")),
        "points_scored": float(merged.get("points_scored", 0.0)),
        "confirmed": bool(merged.get("confirmed", False)),
        "deal_id": str(deal_id),
        "source": str(merged.get("source", "agent")),
        "version": str(merged.get("version", ML_VERSION)),
    }
    return record


def _append_line(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def _read_last_nonempty_line(path: Path) -> tuple[str | None, int]:
    """Return (last non-empty line text, byte offset where that line starts)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        if end == 0:
            return None, 0

        pos = end
        while pos > 0:
            f.seek(pos - 1)
            if f.read(1) in (b"\n", b"\r"):
                pos -= 1
            else:
                break

        if pos == 0:
            return None, 0

        line_end = pos
        line_start = pos
        while line_start > 0:
            f.seek(line_start - 1)
            if f.read(1) == b"\n":
                break
            line_start -= 1

        f.seek(line_start)
        raw = f.read(line_end - line_start)
        return raw.decode("utf-8"), line_start


def _truncate_file(path: Path, byte_offset: int) -> None:
    with open(path, "r+b") as f:
        f.truncate(byte_offset)
        f.flush()
        os.fsync(f.fileno())


def _count_nonempty_lines(path: Path) -> int:
    count = 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _validate_store_on_startup(path: Path) -> None:
    try:
        if not path.exists():
            return
        if path.stat().st_size == 0:
            return

        last_line, line_start = _read_last_nonempty_line(path)
        if last_line is None:
            return

        try:
            json.loads(last_line)
        except json.JSONDecodeError:
            _truncate_file(path, line_start)
            log_engine("WARNING: Repaired truncated ML store entry on startup")
            return

        count = _count_nonempty_lines(path)
        log_engine(f"ML store intact: {count} records")
    except Exception as e:
        log_engine(
            f"ml_training_store startup validation failed: {type(e).__name__}: {e}"
        )


class MLTrainingStore:
    """Buffers entry context until IG-confirmed exit, then appends one JSONL line."""

    def __init__(
        self, path: Path | str | None = None, *, version: str = ML_VERSION
    ) -> None:
        self._path = Path(path) if path else default_store_path()
        self._version = str(version)
        _validate_store_on_startup(self._path)

    @property
    def path(self) -> Path:
        return self._path

    def record_entry(self, deal_id: str, entry_data: dict[str, Any]) -> None:
        try:
            did = str(deal_id or "").strip()
            if not did:
                return
            data = dict(entry_data or {})
            data.setdefault("version", self._version)
            if _is_excluded(did, data):
                log_engine(f"ml_training_store skip entry (excluded) deal={did}")
                return
            with _lock:
                _entry_buffer[did] = data
        except Exception as e:
            log_engine(
                f"ml_training_store record_entry failed deal={deal_id}: "
                f"{type(e).__name__}: {e}"
            )

    def record_exit(self, deal_id: str, exit_data: dict[str, Any]) -> None:
        try:
            did = str(deal_id or "").strip()
            if not did:
                return
            exit_payload = dict(exit_data or {})
            exit_payload.setdefault("version", self._version)

            with _lock:
                entry = _entry_buffer.get(did)
            if entry is None:
                log_engine(
                    f"ml_training_store exit skipped — no entry buffer deal={did}"
                )
                return

            if _is_excluded(did, {**entry, **exit_payload}):
                with _lock:
                    _entry_buffer.pop(did, None)
                log_engine(f"ml_training_store exit skipped (excluded) deal={did}")
                return

            confirmed = bool(exit_payload.get("confirmed", False))
            ig_pnl = exit_payload.get("ig_pnl_currency")
            if ig_pnl is not None:
                exit_payload["gbp_pnl"] = float(ig_pnl)
                confirmed = True
            exit_payload["confirmed"] = confirmed

            if not confirmed:
                log_engine(
                    f"ml_training_store exit buffered — not IG-confirmed deal={did}"
                )
                return

            record = _normalize_record(entry, exit_payload, did)
            _append_line(self._path, record)
            with _lock:
                _entry_buffer.pop(did, None)
        except Exception as e:
            log_engine(
                f"ml_training_store record_exit failed deal={deal_id}: "
                f"{type(e).__name__}: {e}"
            )

    def is_pending(self, deal_id: str) -> bool:
        try:
            with _lock:
                return str(deal_id or "").strip() in _entry_buffer
        except Exception:
            return False

    def flush(self) -> int:
        """No-op for incomplete records; returns 0. Confirmed exits write immediately."""
        return 0

    def record_count(self) -> int:
        try:
            if not self._path.exists():
                return 0
            count = 0
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        count += 1
            return count
        except Exception as e:
            log_engine(
                f"ml_training_store record_count failed: {type(e).__name__}: {e}"
            )
            return 0

    @staticmethod
    def confirmed_from_ig_row(row: dict[str, Any]) -> bool:
        """True when IG transaction sync has set ig_pnl_currency (PENDING promotion)."""
        return row.get("ig_pnl_currency") is not None

    @staticmethod
    def iso_now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
