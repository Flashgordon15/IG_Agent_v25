#!/usr/bin/env python3
"""
Rebuild launcher/IG Agent v25.app for macOS.

Run from project root:
  python3 launcher/build_mac_app.py
"""

from __future__ import annotations

import os
import plistlib
import shutil
import struct
import subprocess
import sys
import zlib
from datetime import datetime
from pathlib import Path

BUNDLE_NAME = "IG Agent v25.app"
BUNDLE_ID = "com.igagent.v25"
DISPLAY_NAME = "IG Agent v25"
VERSION = "25.1.0"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ensure_placeholder_png(png_path: Path) -> None:
    """Ensure a branded 512×512 PNG exists for icon.icns generation."""
    if png_path.is_file() and png_path.stat().st_size > 4000:
        return
    png_path.parent.mkdir(parents=True, exist_ok=True)
    width, height = 512, 512
    bg = (13, 17, 23, 255)
    fg = (63, 185, 80, 255)

    def on_line(x: int, y: int) -> bool:
        pts = ((40, 380), (140, 280), (260, 320), (480, 120))
        for i in range(len(pts) - 1):
            x0, y0 = pts[i]
            x1, y1 = pts[i + 1]
            dx, dy = x1 - x0, y1 - y0
            length_sq = dx * dx + dy * dy
            if length_sq <= 0:
                continue
            t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / length_sq))
            px = x0 + t * dx
            py = y0 + t * dy
            if (x - px) ** 2 + (y - py) ** 2 <= 14 ** 2:
                return True
        return False

    raw = b""
    for y in range(height):
        raw += b"\x00"
        for x in range(width):
            r, g, b, a = fg if on_line(x, y) else bg
            raw += bytes((r, g, b, a))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        crc = zlib.crc32(body) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + body + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(raw, 9))
    png += chunk(b"IEND", b"")
    png_path.write_bytes(png)


def build_iconset(png_path: Path, iconset_dir: Path) -> None:
    entries = (
        (16, "", 16),
        (16, "@2x", 32),
        (32, "", 32),
        (32, "@2x", 64),
        (128, "", 128),
        (128, "@2x", 256),
        (256, "", 256),
        (256, "@2x", 512),
        (512, "", 512),
        (512, "@2x", 1024),
    )
    if iconset_dir.exists():
        shutil.rmtree(iconset_dir)
    iconset_dir.mkdir(parents=True)
    for logical, suffix, px in entries:
        out = iconset_dir / f"icon_{logical}x{logical}{suffix}.png"
        subprocess.run(
            ["sips", "-z", str(px), str(px), str(png_path), "--out", str(out)],
            check=True,
            capture_output=True,
        )


def build_icns(iconset_dir: Path, icns_path: Path) -> None:
    icns_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset_dir), "-o", str(icns_path)],
        check=True,
    )


def write_info_plist(plist_path: Path) -> None:
    data = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleExecutable": "Launcher",
        "CFBundleIconFile": "icon",
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": DISPLAY_NAME,
        "CFBundleDisplayName": DISPLAY_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "10.13",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)


def write_pkginfo(contents_dir: Path) -> None:
    (contents_dir / "PkgInfo").write_bytes(b"APPL????")


def compile_launcher_stub(launcher_dir: Path, macos_dir: Path) -> Path:
    stub_src = launcher_dir / "stub" / "main.c"
    stub_bin = launcher_dir / "stub" / "Launcher"
    subprocess.run(
        ["cc", "-o", str(stub_bin), str(stub_src)],
        check=True,
        capture_output=True,
    )
    dest = macos_dir / "Launcher"
    shutil.copy2(stub_bin, dest)
    dest.chmod(0o755)
    return dest


def install_launch_script(resources_dir: Path, template: Path) -> Path:
    dest = resources_dir / "launch.sh"
    shutil.copy2(template, dest)
    dest.chmod(0o755)
    return dest


def remove_quarantine(bundle: Path) -> None:
    subprocess.run(
        ["xattr", "-dr", "com.apple.quarantine", str(bundle)],
        check=False,
        capture_output=True,
    )


def adhoc_codesign(bundle: Path) -> None:
    subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(bundle)],
        check=True,
        capture_output=True,
    )


def log_build(root: Path, msg: str) -> None:
    log_dir = root / "src" / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    with open(log_dir / "launcher.log", "a", encoding="utf-8") as f:
        f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | {msg}\n")


def validate_bundle(bundle: Path, root: Path) -> list[str]:
    errors: list[str] = []
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    launcher = macos / "Launcher"
    plist = contents / "Info.plist"
    icns = resources / "icon.icns"

    for label, path in (
        ("Contents", contents),
        ("MacOS", macos),
        ("Resources", resources),
    ):
        if not path.is_dir():
            errors.append(f"missing directory: {path} ({label})")

    if not plist.is_file():
        errors.append(f"missing Info.plist: {plist}")
    else:
        try:
            subprocess.run(["plutil", "-lint", str(plist)], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            errors.append(f"invalid Info.plist: {e.stderr.decode() if e.stderr else e}")

    launch_sh = resources / "launch.sh"
    if not launch_sh.is_file():
        errors.append(f"missing launch.sh: {launch_sh}")
    elif not os.access(launch_sh, os.X_OK):
        errors.append(f"launch.sh is not executable: {launch_sh}")
    else:
        root_str = str(root.resolve())
        probe = subprocess.run(
            ["bash", str(launch_sh)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "PATH": os.environ.get("PATH", ""), "LAUNCHER_VALIDATE_ONLY": "1"},
        )
        resolved = (probe.stdout or "").strip().splitlines()[-1] if probe.stdout else ""
        if probe.returncode != 0:
            errors.append(
                f"launch.sh failed (exit {probe.returncode}): "
                f"{(probe.stderr or probe.stdout or 'unknown')[:200]}"
            )
        elif resolved != root_str:
            errors.append(
                f"launch.sh resolved project root to {resolved!r}, expected {root_str!r}"
            )

    if not launcher.is_file():
        errors.append(f"missing Launcher: {launcher}")
    elif not os.access(launcher, os.X_OK):
        errors.append(f"Launcher is not executable: {launcher}")
    else:
        file_type = subprocess.run(
            ["file", "-b", str(launcher)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if "Mach-O" not in file_type:
            errors.append(f"Launcher must be Mach-O executable, got: {file_type}")

    if not icns.is_file() or icns.stat().st_size < 100:
        errors.append(f"missing or invalid icon.icns: {icns}")

    return errors


def build() -> Path:
    root = project_root()
    launcher_dir = root / "launcher"
    bundle = launcher_dir / BUNDLE_NAME
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"

    if bundle.exists():
        shutil.rmtree(bundle)

    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    png_src = launcher_dir / "icon_source" / "icon.png"
    ensure_placeholder_png(png_src)

    iconset = launcher_dir / "icon_source" / "icon.iconset"
    icns = resources / "icon.icns"
    build_iconset(png_src, iconset)
    build_icns(iconset, icns)

    write_info_plist(contents / "Info.plist")
    write_pkginfo(contents)
    compile_launcher_stub(launcher_dir, macos)
    install_launch_script(resources, launcher_dir / "templates" / "launch.sh")
    remove_quarantine(bundle)
    adhoc_codesign(bundle)

    errors = validate_bundle(bundle, root)
    if errors:
        raise RuntimeError("Bundle validation failed:\n  " + "\n  ".join(errors))

    log_build(root, f"built and validated {bundle}")
    return bundle


def main() -> int:
    if sys.platform != "darwin":
        print("build_mac_app.py requires macOS.", file=sys.stderr)
        return 1
    try:
        bundle = build()
        print(f"Built: {bundle}")

        shortcut_script = project_root() / "launcher" / "create_desktop_shortcut.py"
        if shortcut_script.is_file():
            subprocess.run([sys.executable, str(shortcut_script)], check=True)

        return 0
    except Exception as e:
        print(f"Build failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
