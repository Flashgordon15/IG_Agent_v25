#!/usr/bin/env python3
"""
Create ~/Desktop/IG Agent v25.app shortcut to the launcher bundle.

Uses a symlink to launcher/IG Agent v25.app inside the project (reliable for local dev).
"""

from __future__ import annotations

import os
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


def create_symlink(bundle: Path, link: Path) -> None:
    os.symlink(str(bundle), str(link))


def create_shortcut() -> Path:
    root = project_root()
    bundle = (root / "launcher" / BUNDLE_NAME).resolve()
    if not bundle.is_dir():
        raise FileNotFoundError(
            f"App bundle not found: {bundle}\nRun: python3 launcher/build_mac_app.py"
        )

    link = Path.home() / "Desktop" / SHORTCUT_NAME
    remove_existing_shortcut(link)
    create_symlink(bundle, link)

    subprocess.run(["/usr/bin/touch", str(link)], check=False)
    subprocess.run(
        ["xattr", "-dr", "com.apple.quarantine", str(bundle)],
        check=False,
        capture_output=True,
    )
    subprocess.run(
        ["xattr", "-dr", "com.apple.quarantine", str(link)],
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


def bundle_path_error() -> str:
    return str((project_root() / "launcher" / SHORTCUT_NAME).resolve())


if __name__ == "__main__":
    raise SystemExit(main())
