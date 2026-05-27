#!/usr/bin/env python3
"""
Create ~/Desktop/IG Agent v25.app shortcut to the launcher bundle.

Uses a Finder alias (reliable double-click on macOS; unix symlinks to .app often fail).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BUNDLE_NAME = "IG Agent v25.app"
SHORTCUT_NAME = BUNDLE_NAME


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def remove_existing_shortcut(link: Path) -> None:
    if not link.exists() and not link.is_symlink():
        return
    if link.is_symlink():
        link.unlink()
        return
    subprocess.run(
        ["osascript", "-e", f'tell application "Finder" to delete POSIX file "{link}"'],
        check=False,
        capture_output=True,
    )
    if link.exists():
        link.unlink(missing_ok=True)


def create_finder_alias(bundle: Path, link: Path) -> None:
    desktop = link.parent
    desktop.mkdir(parents=True, exist_ok=True)
    script = f'''
tell application "Finder"
    set desktopFolder to POSIX file "{desktop}"
    set targetApp to POSIX file "{bundle}"
    set aliasFile to make new alias file at desktopFolder to targetApp
    set name of aliasFile to "{SHORTCUT_NAME}"
end tell
'''
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True)


def create_shortcut() -> Path:
    root = project_root()
    bundle = (root / "launcher" / BUNDLE_NAME).resolve()
    if not bundle.is_dir():
        raise FileNotFoundError(
            f"App bundle not found: {bundle}\nRun: python3 launcher/build_mac_app.py"
        )

    link = Path.home() / "Desktop" / SHORTCUT_NAME
    remove_existing_shortcut(link)
    create_finder_alias(bundle, link)

    subprocess.run(["/usr/bin/touch", str(link)], check=False)
    for path in (bundle, link):
        subprocess.run(
            ["xattr", "-dr", "com.apple.quarantine", str(path)],
            check=False,
            capture_output=True,
        )
    return link


def main() -> int:
    if sys.platform != "darwin":
        print("create_desktop_shortcut.py requires macOS.", file=sys.stderr)
        return 1
    try:
        link = create_shortcut()
        target = link.resolve() if link.exists() else "(missing)"
        print(f"Desktop shortcut: {link}")
        print(f"  -> {target}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
