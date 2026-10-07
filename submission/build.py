"""Assemble agent.zip for submission and assert the sandbox constraints.

Usage:  python -m submission.build [--out agent.zip]

Bundles: agent.py (at zip root), the engine/ package, and optional
strategy.json / model weights from data/. Refuses to produce a zip that
violates the 50 MB unzipped limit or contains native binaries.
"""

from __future__ import annotations

import argparse
import io
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_UNZIPPED_BYTES = 50 * 1024 * 1024
_NATIVE_SUFFIXES = {".so", ".dylib", ".dll", ".pyd", ".a", ".o", ".exe"}
_ALLOWED_WEIGHT_SUFFIXES = {".onnx", ".safetensors", ".pt", ".npz", ".bin", ".json"}


def _iter_engine_files():
    for path in sorted((ROOT / "engine").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, path.relative_to(ROOT)


def _iter_optional_assets():
    for name in ("strategy.json",):
        p = ROOT / name
        if p.is_file():
            yield p, Path(name)
    data = ROOT / "data"
    if data.is_dir():
        for p in sorted(data.iterdir()):
            if p.is_file() and p.suffix in _ALLOWED_WEIGHT_SUFFIXES and p.name != ".gitkeep":
                yield p, Path("data") / p.name


def build(out: Path) -> None:
    members: list[tuple[Path, Path]] = [(ROOT / "submission" / "agent.py", Path("agent.py"))]
    members += list(_iter_engine_files())
    members += list(_iter_optional_assets())

    total = 0
    for src, _ in members:
        if src.suffix in _NATIVE_SUFFIXES:
            raise SystemExit(f"native binary would be bundled: {src}")
        total += src.stat().st_size
    if total > MAX_UNZIPPED_BYTES:
        raise SystemExit(f"unzipped size {total/1e6:.1f} MB exceeds 50 MB limit")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arc in members:
            zf.write(src, str(arc))
    out.write_bytes(buf.getvalue())

    names = zipfile.ZipFile(io.BytesIO(buf.getvalue())).namelist()
    assert "agent.py" in names, "agent.py must be at the zip root"
    print(f"wrote {out} ({out.stat().st_size/1e6:.2f} MB zipped, "
          f"{total/1e6:.2f} MB unzipped, {len(names)} files)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "agent.zip")
    build(ap.parse_args().out)


if __name__ == "__main__":
    main()
