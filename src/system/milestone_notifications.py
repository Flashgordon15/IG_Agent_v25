"""
ML training milestone webhooks — Slack / Discord compatible.

Fires when agent-sourced ML training record count crosses 100, 250, or 500.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import requests
from requests.exceptions import RequestException, Timeout

from system.engine_log import log_engine
from system.ml_filter_overrides import (
    ML_MIN_TRAINING_RECORDS,
    scale_max_rsi,
    training_record_count,
)
from system.paths import data_dir

MILESTONE_THRESHOLDS = (100, 250, 500)
_STATE_FILENAME = "ml_milestone_notifications.json"

_lock = threading.RLock()


def _state_path() -> Path:
    state_dir = data_dir() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / _STATE_FILENAME


def _load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.is_file():
        return {"sent": []}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        sent = raw.get("sent") if isinstance(raw, dict) else []
        return {"sent": list(sent) if isinstance(sent, list) else []}
    except (json.JSONDecodeError, OSError):
        return {"sent": []}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    atomic_path = path.with_suffix(".tmp")
    atomic_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    atomic_path.replace(path)


def reset_milestone_state_for_tests() -> None:
    with _lock:
        path = _state_path()
        if path.is_file():
            path.unlink()


def _webhook_config() -> dict[str, Any]:
    try:
        from system.config_loader import get_config

        block = get_config().get("milestone_webhooks") or {}
        return dict(block) if isinstance(block, dict) else {}
    except Exception:
        return {}


def _webhook_urls() -> list[str]:
    cfg = _webhook_config()
    if not cfg.get("enabled", False):
        return []
    urls: list[str] = []
    for key in ("webhook_url", "slack_webhook_url", "discord_webhook_url"):
        raw = str(cfg.get(key) or "").strip()
        if raw and raw not in urls:
            urls.append(raw)
    return urls


def _strict_max_rsi_from_meta() -> float:
    path = data_dir() / "ml_model" / "meta.json"
    if not path.is_file():
        return 70.0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        overrides = raw.get("filter_overrides") if isinstance(raw, dict) else {}
        val = overrides.get("max_rsi") if isinstance(overrides, dict) else None
        return float(val) if val is not None else 70.0
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return 70.0


def format_milestone_message(record_count: int) -> str:
    strict = _strict_max_rsi_from_meta()
    effective = scale_max_rsi(strict, record_count)
    return (
        f"🚀 System Milestone Reached: Learning Plane at "
        f"[{record_count}/{ML_MIN_TRAINING_RECORDS}] records. "
        f"Dynamic filter ramp updated to max_rsi: [{effective:.2f}]."
    )


def _post_webhook(url: str, text: str) -> bool:
    payload: dict[str, Any]
    if "discord.com/api/webhooks" in url:
        payload = {"content": text}
    else:
        payload = {"text": text}
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code >= 400:
            log_engine(
                f"milestone webhook HTTP {resp.status_code}: {resp.text[:120]}"
            )
            return False
        return True
    except (Timeout, RequestException) as exc:
        log_engine(f"milestone webhook failed: {type(exc).__name__}: {exc}")
        return False


def _mark_sent(threshold: int) -> None:
    with _lock:
        state = _load_state()
        sent = {int(x) for x in state.get("sent") or []}
        sent.add(threshold)
        state["sent"] = sorted(sent)
        _save_state(state)


def _already_sent(threshold: int) -> bool:
    state = _load_state()
    sent = state.get("sent") or []
    return threshold in sent


def notify_milestone(threshold: int, record_count: int | None = None) -> bool:
    """Send milestone payload if webhooks configured. Returns True if any send succeeded."""
    count = record_count if record_count is not None else training_record_count()
    urls = _webhook_urls()
    message = format_milestone_message(count)
    if not urls:
        log_engine(f"milestone (no webhook configured): {message}")
        return False
    ok_any = False
    for url in urls:
        if _post_webhook(url, message):
            ok_any = True
    if ok_any:
        _mark_sent(threshold)
        log_engine(f"milestone webhook sent threshold={threshold} records={count}")
    return ok_any


def _dispatch_milestone_notify(threshold: int, record_count: int) -> None:
    """Fire-and-forget milestone webhook — never blocks trading or GUI loops."""
    threading.Thread(
        target=notify_milestone,
        args=(threshold, record_count),
        daemon=True,
        name=f"milestone-notify-{threshold}",
    ).start()


def on_training_records_changed(previous_count: int, new_count: int) -> None:
    """Call after ML store grows — fires webhooks on exact threshold crossings."""
    if new_count <= previous_count:
        return
    for threshold in MILESTONE_THRESHOLDS:
        if previous_count < threshold <= new_count and not _already_sent(threshold):
            _dispatch_milestone_notify(threshold, new_count)


def milestone_status_block() -> dict[str, Any]:
    """Status dict for /api/health system_status."""
    state = _load_state()
    count = training_record_count()
    sent = list(state.get("sent") or [])
    next_milestone = next((m for m in MILESTONE_THRESHOLDS if m > count), None)
    strict = _strict_max_rsi_from_meta()
    effective = scale_max_rsi(strict, count)
    return {
        "training_records": count,
        "training_records_required": ML_MIN_TRAINING_RECORDS,
        "milestones": list(MILESTONE_THRESHOLDS),
        "milestones_sent": sent,
        "next_milestone": next_milestone,
        "effective_max_rsi": round(effective, 2),
        "strict_max_rsi": round(strict, 2),
    }


def sync_milestones_on_startup() -> None:
    """Catch-up: notify any unsent milestones at or below current record count."""
    count = training_record_count()
    for threshold in MILESTONE_THRESHOLDS:
        if count >= threshold and not _already_sent(threshold):
            _dispatch_milestone_notify(threshold, count)
