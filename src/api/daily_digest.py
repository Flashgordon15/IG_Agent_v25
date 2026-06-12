"""Serve the Daily Operator Digest for the dashboard popup."""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from system.paths import project_root

_LONDON = ZoneInfo("Europe/London")
_DAY_RE = re.compile(r"^#\s*Daily Operator Digest\s*—\s*(\d{4}-\d{2}-\d{2})")


def _digest_dir() -> Path:
    return project_root() / "docs" / "morning"


def _today_london() -> str:
    return datetime.now(_LONDON).strftime("%Y-%m-%d")


def _parse_day(markdown: str) -> str | None:
    for line in markdown.splitlines():
        m = _DAY_RE.match(line.strip())
        if m:
            return m.group(1)
    return None


def _write_digest_files(markdown: str, day: str) -> None:
    out_dir = _digest_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    dated = out_dir / f"DAILY_DIGEST_{day}.md"
    latest = out_dir / "DAILY_DIGEST_LATEST.md"
    dated.write_text(markdown, encoding="utf-8")
    latest.write_text(markdown, encoding="utf-8")


def _generate_digest_markdown(*, write_snapshot: bool = False) -> tuple[str, str]:
    root = project_root()
    scripts = root / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from daily_operator_digest import build_digest

    _, lines = build_digest(write_snapshot=write_snapshot)
    markdown = "\n".join(lines) + "\n"
    day = _parse_day(markdown) or _today_london()
    return day, markdown


def load_daily_digest(*, regenerate_if_stale: bool = True) -> dict[str, str]:
    """
    Return today's digest markdown for the dashboard.

    Reads docs/morning/DAILY_DIGEST_LATEST.md. When the file is missing or
    dated before today (Europe/London), regenerates live from current metrics.
    """
    out_dir = _digest_dir()
    latest = out_dir / "DAILY_DIGEST_LATEST.md"
    today = _today_london()
    markdown = ""
    day: str | None = None
    source = "missing"

    if latest.is_file():
        markdown = latest.read_text(encoding="utf-8")
        day = _parse_day(markdown)
        source = "file"

    stale = day != today
    if not markdown.strip() or (regenerate_if_stale and stale):
        day, markdown = _generate_digest_markdown(write_snapshot=not stale)
        _write_digest_files(markdown, day)
        source = "generated_live" if stale or not latest.is_file() else "refreshed"

    if not markdown.strip():
        raise FileNotFoundError("Daily digest could not be loaded or generated")

    mtime = (
        latest.stat().st_mtime
        if latest.is_file()
        else datetime.now(_LONDON).timestamp()
    )
    generated_at = datetime.fromtimestamp(mtime, tz=_LONDON).isoformat()

    return {
        "day": day or today,
        "markdown": markdown,
        "generated_at": generated_at,
        "source": source,
    }
