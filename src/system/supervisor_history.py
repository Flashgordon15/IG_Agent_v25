"""Permanent 24-hour supervisor triage ledger — JSONL append-only."""

from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from system.engine_log import log_engine
from system.paths import data_dir

_lock = threading.Lock()
_HISTORY_PATH = data_dir() / "logs" / "supervisor_history.jsonl"
_RETENTION_HOURS = 24.0
_SUPERSEDED_BREACH_TYPES = frozenset({"drawdown_ceiling_breach"})
_triage_generation = 0
_triage_boot_reset_pending = False


def sanitize_for_ws_json(value: Any) -> Any:
    """Recursively coerce payloads to JSON-safe primitives for Flight Deck WebSockets."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): sanitize_for_ws_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_for_ws_json(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return str(value)
        return value
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", errors="replace")
    return str(value)


def history_path() -> Path:
    return _HISTORY_PATH


def get_triage_generation() -> int:
    with _lock:
        return _triage_generation


def consume_triage_boot_reset_flag() -> bool:
    """True once after startup so Flight Deck clients drop stale triageBuffer."""
    global _triage_boot_reset_pending
    with _lock:
        pending = _triage_boot_reset_pending
        _triage_boot_reset_pending = False
        return pending


def record_supervisor_event(
    event_type: str,
    *,
    detail: str = "",
    payload: dict[str, Any] | None = None,
    source: str = "self_healing_supervisor",
) -> None:
    """Non-blocking append — never raises to callers."""
    record = {
        "ts": time.time(),
        "iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event_type": str(event_type),
        "source": source,
        "detail": detail,
        "payload": payload or {},
    }
    path = history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def _historic_drawdown_breach_superseded() -> tuple[bool, str]:
    """
    False drawdown breach ledger rows are stale when accounting reset or monitor is nominal.

    Suppresses historic ``drawdown_ceiling_breach`` triage rows when:
      - Superjet guard is not frozen/breached, AND
      - ``effective_daily_pnl`` is ~0.00 after baseline reset, OR
      - a manual ``daily_loss_reset_version`` was applied today.
    """
    try:
        from system.superjet_drawdown_guard import telemetry_snapshot

        snap = telemetry_snapshot()
        if snap.get("frozen") or snap.get("breached"):
            return False, "superjet_active"
    except Exception:
        pass

    try:
        from data.learning_store import LearningStore
        from system.balance_pnl_decimal import decimal_to_float, money_decimal
        from system.config_loader import get_config
        from system.daily_loss_policy import daily_loss_reset_snapshot, effective_daily_pnl

        cfg = get_config(reload=False)
        store = LearningStore(str(cfg.learning_db))
        reset = daily_loss_reset_snapshot(store)
        version = str(reset.get("version") or "").strip()
        reset_day = str(reset.get("reset_day") or "")
        today = date.today().isoformat()

        if version and reset_day == today:
            return True, f"baseline_reset_{version}"

        eff = money_decimal(effective_daily_pnl(store), field="effective_daily_pnl")
        if eff is not None and abs(decimal_to_float(eff)) < 0.005:
            try:
                from system.drawdown_monitor import operational_status

                if operational_status() in ("NOMINAL", "STANDBY"):
                    return True, "effective_pnl_zero_monitor_nominal"
            except Exception:
                return True, "effective_pnl_zero"
    except Exception as exc:
        return False, f"check_failed:{type(exc).__name__}"

    return False, "breach_still_relevant"


def filter_superseded_triage_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    superseded, _reason = _historic_drawdown_breach_superseded()
    if not superseded:
        return rows
    return [
        row
        for row in rows
        if str(row.get("event_type") or "") not in _SUPERSEDED_BREACH_TYPES
    ]


def purge_superseded_drawdown_breach_events_from_disk() -> int:
    """Remove stale false breach rows from JSONL when superseded."""
    superseded, reason = _historic_drawdown_breach_superseded()
    if not superseded:
        return 0

    path = history_path()
    if not path.is_file():
        return 0

    removed = 0
    kept: list[str] = []
    try:
        with _lock:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except json.JSONDecodeError:
                    kept.append(text)
                    continue
                if str(row.get("event_type") or "") in _SUPERSEDED_BREACH_TYPES:
                    removed += 1
                    continue
                kept.append(text)
            if removed:
                path.write_text(
                    "\n".join(kept) + ("\n" if kept else ""),
                    encoding="utf-8",
                )
    except OSError:
        return 0

    if removed:
        log_engine(
            f"triage ledger: purged {removed} superseded drawdown_ceiling_breach "
            f"({reason})"
        )
    return removed


def prepare_triage_ledger_on_startup() -> int:
    """
    Flight Deck startup hook — flush false breach rows and bump client cache generation.
    """
    global _triage_generation, _triage_boot_reset_pending
    removed = purge_superseded_drawdown_breach_events_from_disk()
    with _lock:
        _triage_generation += 1
        _triage_boot_reset_pending = True
        gen = _triage_generation
    log_engine(
        f"triage ledger: startup generation={gen} purged_breach_rows={removed}"
    )
    return gen


def read_history_last_24h(*, max_lines: int = 200) -> list[dict[str, Any]]:
    path = history_path()
    if not path.is_file():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_RETENTION_HOURS)
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                iso = row.get("iso") or ""
                try:
                    ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except ValueError:
                    ts = None
                if ts is not None and ts < cutoff:
                    continue
                out.append(row)
        if len(out) > max_lines:
            out = out[-max_lines:]
    except OSError:
        return []
    return out


def read_triage_events_for_ui(*, max_lines: int = 200) -> list[dict[str, Any]]:
    """24HR TRIAGE LOOKBACK feed — filters superseded false drawdown breaches."""
    rows = filter_superseded_triage_events(
        read_history_last_24h(max_lines=max_lines)
    )
    return [sanitize_for_ws_json(row) for row in rows if isinstance(row, dict)]


def reset_supervisor_history_for_tests() -> None:
    global _triage_generation, _triage_boot_reset_pending
    try:
        history_path().unlink(missing_ok=True)
    except OSError:
        pass
    with _lock:
        _triage_generation = 0
        _triage_boot_reset_pending = False
