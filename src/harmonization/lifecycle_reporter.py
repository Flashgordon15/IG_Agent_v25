"""
Lifecycle audit reporter — appends structured phase reports to lifecycle_audit.md.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from system.paths import project_root

_AUDIT_PATH = project_root() / "lifecycle_audit.md"
_BLOCKERS_PATH = project_root() / "blockers.log"


def ensure_audit_files() -> None:
    if not _AUDIT_PATH.is_file():
        _AUDIT_PATH.write_text(
            "# IG Agent Adversarial Harmonization Lifecycle Audit\n\n"
            f"Initialized: {datetime.now(timezone.utc).isoformat()}\n\n",
            encoding="utf-8",
        )
    if not _BLOCKERS_PATH.is_file():
        _BLOCKERS_PATH.write_text("", encoding="utf-8")


def log_blocker(phase: int, message: str) -> None:
    ensure_audit_files()
    line = (
        f"{datetime.now(timezone.utc).isoformat()} "
        f"[PHASE {phase}] {message}\n"
    )
    with _BLOCKERS_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line)


class LifecycleReporter:
    def __init__(self, audit_path: Path | None = None) -> None:
        ensure_audit_files()
        self._path = audit_path or _AUDIT_PATH

    def append_phase_report(
        self,
        phase: int,
        *,
        status: str,
        latency_ms: dict[str, float] | None = None,
        ml_assessment: dict[str, Any] | None = None,
        blockers: list[str] | None = None,
        hardening: str = "",
        close_down: str = "",
        traceback_text: str = "",
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        lines = [
            f"#### [PHASE {phase}/10] SYSTEM STATUS REPORT",
            f"- **Timestamp**: {ts}",
            f"- **Runtime Execution Status**: {status}",
        ]
        if traceback_text:
            lines.append(f"  ```\n{traceback_text.strip()}\n  ```")
        lat = latency_ms or {}
        lines.append(
            f"- **Lightstreamer Ingestion Latency**: "
            f"mean={lat.get('mean_ms', 'n/a')}ms / max_spike={lat.get('max_ms', 'n/a')}ms"
        )
        ml = ml_assessment or {}
        lines.append(
            f"- **ML Inference Assessment**: "
            f"tensor={ml.get('tensor', 'n/a')} / "
            f"confidence_spread={ml.get('confidence_spread', 'n/a')} / "
            f"signal={ml.get('signal', 'n/a')}"
        )
        if blockers:
            lines.append("- **Identified Trading Blockers**:")
            for b in blockers:
                lines.append(f"  - {b}")
        else:
            lines.append("- **Identified Trading Blockers**: none")
        lines.append(
            f"- **Codebase Hardening Action Implemented**: {hardening or 'none'}"
        )
        lines.append(
            f"- **Lifecycle Close-Down Verification**: {close_down or 'n/a'}"
        )
        lines.append("")
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

        if blockers:
            for b in blockers:
                log_blocker(phase, b)
