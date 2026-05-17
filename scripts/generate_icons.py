"""Render the source SVG into platform icon binaries.

Output:
* ``build/assets/app.ico``  — Windows multi-res icon (16/32/48/64/128/256).
* ``build/assets/app.icns`` — macOS icon bundle (only on macOS, since
                              this script shells out to ``iconutil``).

Uses Qt's ``QSvgRenderer`` for SVG → PNG rasterization so the only
extra dependency is **Pillow** (a pure-Python wheel that installs
cleanly on Windows / macOS / Linux). PySide6 is already required by
the GUI build.

Run once whenever ``build/assets/icon.svg`` changes.

Requirements:
    pip install pillow            # PySide6 already comes from requirements-gui.txt
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
# Padding around the glyph so it doesn't kiss the icon edges. The source
# SVG draws a 24×24 viewBox; we render into a square and inset the
# painted area by this fraction on every side.
PADDING_RATIO = 0.10


def _import_renderers():
    try:
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        print(
            "Pillow is missing. Install with: pip install pillow",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    try:
        from PySide6.QtSvg import QSvgRenderer  # noqa: F401
        from PySide6.QtGui import QGuiApplication  # noqa: F401
    except ImportError as exc:
        print(
            "PySide6 is missing. Install with: pip install -r requirements-gui.txt",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def _ensure_qt_app():
    """QGuiApplication is required before constructing QImage on most
    platforms. Reuse an existing one if the caller already created it."""
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:
        # Offscreen platform avoids opening a window or needing a display.
        return QGuiApplication(["--platform", "offscreen"])
    return QGuiApplication.instance()


def _render_png(svg_path: Path, size: int, dest: Path) -> None:
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(svg_path))
    if not renderer.isValid():
        raise SystemExit(f"Qt could not parse {svg_path}")

    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)

    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        pad = size * PADDING_RATIO
        target = QRectF(pad, pad, size - 2 * pad, size - 2 * pad)
        renderer.render(painter, target)
    finally:
        painter.end()

    if not image.save(str(dest), "PNG"):
        raise SystemExit(f"QImage.save failed for {dest}")


def _make_windows_ico() -> Path:
    from PIL import Image

    pngs = []
    with tempfile.TemporaryDirectory() as tmp:
        for size in WIN_SIZES:
            png_path = Path(tmp) / f"{size}.png"
            _render_png(SVG_PATH, size, png_path)
            pngs.append(Image.open(png_path).convert("RGBA").copy())
        out = OUT_DIR / "app.ico"
        # Pillow expects the BIGGEST image first when given a `sizes` list.
        biggest = max(pngs, key=lambda im: im.size[0])
        biggest.save(out, format="ICO", sizes=[(s, s) for s in WIN_SIZES])
    print(f"Wrote {out}")
    return out


def _make_macos_icns() -> Path | None:
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
        _render_png(SVG_PATH, size, iconset / f"icon_{size}x{size}.png")
        _render_png(SVG_PATH, size * 2, iconset / f"icon_{size}x{size}@2x.png")
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

    _ensure_qt_app()
    _make_windows_ico()
    _make_macos_icns()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
