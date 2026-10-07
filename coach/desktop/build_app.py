"""Assemble ``Chess Coach.app``.

A real bundle rather than a renamed shell script: that is what gives the coach a
Dock icon, a menu bar and a Cmd-Q that stops the server instead of orphaning it.

The bundle is a thin wrapper - it execs the repo's own virtualenv rather than
vendoring a Python, so the app always runs the code you have checked out and
``make app`` after an edit is unnecessary. The trade is that the bundle is tied
to this checkout; it stores the absolute path and says so clearly if the repo has
moved.

    python -m coach.desktop.build_app [--dest DIR]
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from coach.config import ROOT

APP_NAME = "Chess Coach"
BUNDLE_ID = "local.chesscoach.app"

# The knight is drawn as a flat silhouette rather than set as a glyph, so the
# icon doesn't depend on which chess font the rendering machine happens to have.
ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#2C8A70"/>
      <stop offset="1" stop-color="#14503F"/>
    </linearGradient>
  </defs>
  <rect width="1024" height="1024" rx="228" fill="url(#g)"/>
  <g opacity="0.14" fill="#000">
    <rect x="0" y="0" width="128" height="128"/><rect x="256" y="0" width="128" height="128"/>
    <rect x="512" y="0" width="128" height="128"/><rect x="768" y="0" width="128" height="128"/>
    <rect x="128" y="128" width="128" height="128"/><rect x="384" y="128" width="128" height="128"/>
    <rect x="640" y="128" width="128" height="128"/><rect x="896" y="128" width="128" height="128"/>
    <rect x="0" y="896" width="128" height="128"/><rect x="256" y="896" width="128" height="128"/>
    <rect x="512" y="896" width="128" height="128"/><rect x="768" y="896" width="128" height="128"/>
  </g>
  <path fill="#F2EFE6" d="M636 806H330c0-58 26-88 74-114 62-34 96-58 118-104
    -34 14-70 22-104 16-44-8-72-36-86-78-8-24-6-46 4-72l-44 26-24-52 92-56
    c18-42 44-76 82-102 30-20 62-32 96-36l-8-44 56-10 10 52c66 18 116 58 148 122
    26 52 38 116 40 190 2 74-14 138-48 190-16 24-36 44-60 60z"/>
  <circle cx="404" cy="418" r="26" fill="#14503F"/>
</svg>
"""


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, **kw)


def build_icon(dest: Path, work: Path) -> bool:
    """Render the SVG into a multi-resolution .icns. Best effort."""
    if not (shutil.which("qlmanage") and shutil.which("sips") and shutil.which("iconutil")):
        return False
    svg = work / "icon.svg"
    svg.write_text(ICON_SVG)
    try:
        _run(["qlmanage", "-t", "-s", "1024", "-o", str(work), str(svg)])
    except subprocess.CalledProcessError:
        return False
    master = work / "icon.svg.png"
    if not master.is_file():
        return False

    iconset = work / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    # macOS wants each size at 1x and 2x.
    for size in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, f"{size}x{size}"), (2, f"{size}x{size}@2x")):
            px = size * scale
            try:
                _run(["sips", "-z", str(px), str(px), str(master),
                      "--out", str(iconset / f"icon_{suffix}.png")])
            except subprocess.CalledProcessError:
                return False
    try:
        _run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)])
    except subprocess.CalledProcessError:
        return False
    return dest.is_file()


LAUNCH_STUB = """#!/bin/bash
# Thin wrapper: run the checkout's own virtualenv so the app always reflects the
# code on disk. Nothing is vendored into the bundle.
set -e
REPO="{repo}"
PY="$REPO/.venv/bin/python"

if [ ! -x "$PY" ]; then
  osascript -e 'display alert "Chess Coach can’t find its code" message "Expected a virtualenv at {repo}/.venv. If you moved the repository, rebuild the app with: make app"'
  exit 1
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"   # Homebrew stockfish
cd "$REPO"
exec "$PY" -m coach.desktop.launcher
"""


def build(dest_dir: Path | None = None, repo: Path | None = None) -> Path:
    repo = (repo or ROOT).resolve()
    dest_dir = (dest_dir or repo).resolve()
    app = dest_dir / f"{APP_NAME}.app"
    contents = app / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"

    if app.exists():
        shutil.rmtree(app)
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    exe = macos / "chess-coach"
    exe.write_text(LAUNCH_STUB.format(repo=repo))
    exe.chmod(0o755)

    work = contents / "_iconwork"
    work.mkdir()
    has_icon = build_icon(resources / "AppIcon.icns", work)
    shutil.rmtree(work)

    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleExecutable": exe.name,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # A normal windowed app: Dock icon, menu bar, Cmd-Q.
        "LSUIElement": False,
        "LSApplicationCategoryType": "public.app-category.education",
        "NSHumanReadableCopyright": "Local personal training tool.",
    }
    if has_icon:
        info["CFBundleIconFile"] = "AppIcon"
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))

    # Ad-hoc sign so Gatekeeper treats it as a stable, unmodified app rather than
    # re-prompting every launch. Harmless if codesign is unavailable.
    if shutil.which("codesign"):
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)],
                       check=False, capture_output=True)

    # Drop the quarantine flag we may have inherited, so the first double-click
    # doesn't hit "unidentified developer" for an app built on this machine.
    subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(app)],
                   check=False, capture_output=True)
    return app


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build Chess Coach.app")
    ap.add_argument("--dest", type=Path, default=None,
                    help="where to put the .app (default: the repo root)")
    args = ap.parse_args(argv)
    if sys.platform != "darwin":
        print("This builds a macOS .app bundle; nothing to do on this platform.")
        return 1
    app = build(args.dest)
    icon = (app / "Contents/Resources/AppIcon.icns").is_file()
    print(f"Built {app}")
    print(f"  icon: {'yes' if icon else 'no (qlmanage/sips/iconutil unavailable)'}")
    print("  Double-click it, or drag it to your Dock / Applications.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
