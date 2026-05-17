"""Render the source SVG into platform icon binaries.

Output:
* ``build/assets/app.ico``  — Windows multi-res icon (16/32/48/64/128/256).
* ``build/assets/app.icns`` — macOS icon bundle (only when run on macOS,
                              since the conversion uses ``iconutil``).

Requirements:
    pip install pillow cairosvg

Run once whenever ``build/assets/icon.svg`` changes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG_PATH = ROOT / "build" / "assets" / "icon.svg"
OUT_DIR = ROOT / "build" / "assets"

WIN_SIZES = [16, 32, 48, 64, 128, 256]
MAC_SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _import_renderers():
    try:
        import cairosvg  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        print(
            "Missing deps. Install with: pip install pillow cairosvg",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def _render_png(svg_bytes: bytes, size: int, dest: Path) -> None:
    import cairosvg

    cairosvg.svg2png(
        bytestring=svg_bytes,
        write_to=str(dest),
        output_width=size,
        output_height=size,
        # Pad transparent margin so the glyph doesn't kiss the edges.
        parent_width=size,
        parent_height=size,
    )


def _make_windows_ico(svg_bytes: bytes) -> Path:
    from PIL import Image

    pngs = []
    with tempfile.TemporaryDirectory() as tmp:
        for size in WIN_SIZES:
            png_path = Path(tmp) / f"{size}.png"
            _render_png(svg_bytes, size, png_path)
            pngs.append(Image.open(png_path).convert("RGBA"))
        out = OUT_DIR / "app.ico"
        pngs[0].save(out, format="ICO", sizes=[(s, s) for s in WIN_SIZES])
    print(f"Wrote {out}")
    return out


def _make_macos_icns(svg_bytes: bytes) -> Path | None:
    if sys.platform != "darwin":
        print("Skipping .icns (iconutil only runs on macOS).")
        return None
    if not shutil.which("iconutil"):
        print("iconutil not found; skipping .icns.")
        return None
    iconset = OUT_DIR / "app.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True, exist_ok=True)
    for size in MAC_SIZES:
        _render_png(svg_bytes, size, iconset / f"icon_{size}x{size}.png")
        _render_png(svg_bytes, size * 2, iconset / f"icon_{size}x{size}@2x.png")
    out = OUT_DIR / "app.icns"
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
        check=True,
    )
    shutil.rmtree(iconset)
    print(f"Wrote {out}")
    return out


def main() -> int:
    _import_renderers()
    if not SVG_PATH.exists():
        print(f"Source SVG missing: {SVG_PATH}", file=sys.stderr)
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    svg_bytes = SVG_PATH.read_bytes()
    _make_windows_ico(svg_bytes)
    _make_macos_icns(svg_bytes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
