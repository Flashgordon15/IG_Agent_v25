"""
Release notes for the dashboard splash — parsed from CHANGELOG.md.

Single source of truth: bump APP_VERSION in app_identity.py and add a matching
## section here. The splash auto-shows when the stored version advances.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from system.app_identity import APP_VERSION, APP_VERSION_LABEL
from system.paths import project_root

_SECTION_RE = re.compile(r"^##\s+v?(?P<ver>\d+\.\d+\.\d+)\s*$", re.MULTILINE)
_BULLET_RE = re.compile(r"^-\s+(.+)$")
_TITLE_RE = re.compile(r"^Title:\s*(.+)$", re.IGNORECASE)


def changelog_path() -> Path:
    return project_root() / "CHANGELOG.md"


def _normalize_version(version: str) -> str:
    return str(version or "").strip().lstrip("vV")


@lru_cache(maxsize=16)
def _parse_changelog_file(path_str: str) -> dict[str, dict[str, Any]]:
    path = Path(path_str)
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    sections: dict[str, dict[str, Any]] = {}
    matches = list(_SECTION_RE.finditer(text))
    for idx, match in enumerate(matches):
        ver = _normalize_version(match.group("ver"))
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        block = text[start:end]
        title = f"v{ver} Upgrades"
        bullets: list[str] = []
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("###"):
                continue
            title_match = _TITLE_RE.match(line)
            if title_match:
                title = title_match.group(1).strip()
                continue
            bullet_match = _BULLET_RE.match(line)
            if bullet_match:
                bullets.append(bullet_match.group(1).strip())
        sections[ver] = {
            "title": title,
            "highlights": bullets,
        }
    return sections


def clear_release_notes_cache() -> None:
    _parse_changelog_file.cache_clear()


def get_release_notes_for_version(version: str | None = None) -> dict[str, Any]:
    """Return splash payload fields for *version* (defaults to APP_VERSION)."""
    ver = _normalize_version(version or APP_VERSION)
    path = changelog_path()
    sections = _parse_changelog_file(str(path.resolve()))
    entry = sections.get(ver) or {}
    highlights = list(entry.get("highlights") or [])
    if not highlights:
        highlights = [
            f"{APP_VERSION_LABEL} is running — add a ## {ver} section to CHANGELOG.md "
            "to populate this splash."
        ]
    label = APP_VERSION_LABEL
    if ver != _normalize_version(APP_VERSION):
        label = f"v{ver}"
    return {
        "version": ver,
        "version_label": label,
        "title": str(entry.get("title") or f"{label} Upgrades"),
        "highlights": highlights,
        "changelog_path": str(path),
        "changelog_found": bool(entry.get("highlights")),
    }


def current_release_notes() -> dict[str, Any]:
    return get_release_notes_for_version(APP_VERSION)


def validate_current_release_notes() -> tuple[bool, str]:
    """CI guard — current APP_VERSION must have ≥1 highlight in CHANGELOG.md."""
    notes = current_release_notes()
    if not notes.get("changelog_found"):
        return (
            False,
            f"CHANGELOG.md missing section for APP_VERSION {APP_VERSION} "
            f"(expected '## {APP_VERSION}')",
        )
    if len(notes.get("highlights") or []) < 1:
        return False, f"CHANGELOG.md section {APP_VERSION} has no bullet items"
    return True, "ok"
