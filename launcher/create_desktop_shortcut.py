#!/usr/bin/env python3
"""
Create Desktop shortcuts to the IG Agent v29.0 launcher bundle.

Creates:
  ~/Desktop/Desktop IG Agent v29.0.app  (symlink)
  ~/Desktop/IG Agent Cursor.app         (symlink — replaces legacy Cursor IDE stub)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BUNDLE_NAME = "IG Agent v29.0.app"
SHORTCUT_NAMES = (
    "Desktop IG Agent v29.0.app",
    "IG Agent Cursor.app",
)
LEGACY_SHORTCUT_NAMES = (
    "IG Agent v25.app",
    "Desktop IG Agent v25.app",
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def remove_existing_shortcut(link: Path) -> None:
    if not link.exists() and not link.is_symlink():
        return
    if link.is_symlink():
        link.unlink()
        return
    if link.is_dir():
        import shutil

        shutil.rmtree(link)
        return
    subprocess.run(
        ["osascript", "-e", f'tell application "Finder" to delete POSIX file "{link}"'],
        check=False,
        capture_output=True,
    )
    if link.exists():
        link.unlink(missing_ok=True)


def create_symlink_shortcut(bundle: Path, link: Path) -> None:
    desktop = link.parent
    desktop.mkdir(parents=True, exist_ok=True)
    remove_existing_shortcut(link)
    link.symlink_to(bundle, target_is_directory=True)


def create_shortcuts() -> list[Path]:
    root = project_root()
    bundle = (root / "launcher" / BUNDLE_NAME).resolve()
    if not bundle.is_dir():
        raise FileNotFoundError(
            f"App bundle not found: {bundle}\nRun: python3 launcher/build_mac_app.py"
        )

    links: list[Path] = []
    for name in SHORTCUT_NAMES:
        link = Path.home() / "Desktop" / name
        create_symlink_shortcut(bundle, link)
        links.append(link)

    for legacy_name in LEGACY_SHORTCUT_NAMES:
        legacy = Path.home() / "Desktop" / legacy_name
        if legacy.exists() or legacy.is_symlink():
            remove_existing_shortcut(legacy)
            print(f"Removed legacy Desktop shortcut: {legacy}")

    subprocess.run(["/usr/bin/touch", str(bundle)], check=False)
    for path in (bundle, *links):
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(path)],
            check=False,
            capture_output=True,
        )
    return links


def main() -> int:
    if sys.platform != "darwin":
        print("create_desktop_shortcut.py requires macOS.", file=sys.stderr)
        return 1
    try:
        bundle = (project_root() / "launcher" / BUNDLE_NAME).resolve()
        links = create_shortcuts()
        for link in links:
            print(f"Desktop shortcut: {link}")
            print(f"  -> {bundle}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
