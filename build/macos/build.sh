#!/usr/bin/env bash
# End-to-end macOS build: PyInstaller .app + signed .pkg installer.
#
# Output:
#   build/macos/out/Describely-1.0.0.pkg
#
# Usage (from the repo root):
#   ./build/macos/build.sh
#
# Optional env vars:
#   PY            Python interpreter (default: ./.venv/bin/python or python3)
#   SKIP_PKG=1    Build only the .app, skip the installer step.
#
# Requires:
#   * Python 3.10+ with the project venv prepared.
#   * Pre-seeded model under ./models/large-v3/ (scripts/fetch_model.py).
#   * macOS native tooling: pkgbuild, productbuild, iconutil. All ship
#     with the OS — no Homebrew needed for the .pkg path. (create-dmg
#     is no longer required.)

set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"
export VTE_PROJECT_ROOT="$PROJECT_ROOT"

APP_VERSION="1.0.0"
APP_NAME="Describely"
BUNDLE_ID="com.describely.app"

MODEL_DIR="$PROJECT_ROOT/models/large-v3"
if [[ ! -d "$MODEL_DIR" ]]; then
  echo "Embedded model not found at $MODEL_DIR." >&2
  echo "Run: python scripts/fetch_model.py" >&2
  exit 1
fi

if [[ -z "${PY:-}" ]]; then
  if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    PY="$PROJECT_ROOT/.venv/bin/python"
  else
    PY="$(command -v python3)"
  fi
fi
echo "==> Using Python: $PY"

echo "==> Installing build dependencies"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements-gui.txt

DIST="$PROJECT_ROOT/dist"
WORK="$PROJECT_ROOT/build/pyinstaller-work"
rm -rf "$DIST" "$WORK"

echo "==> Running PyInstaller"
"$PY" -m PyInstaller \
  "$PROJECT_ROOT/build/pyinstaller/videote.spec" \
  --noconfirm \
  --workpath "$WORK" \
  --distpath "$DIST"

APP_BUNDLE="$DIST/${APP_NAME}.app"
if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "Expected .app bundle not found: $APP_BUNDLE" >&2
  exit 1
fi
echo "==> .app bundle: $APP_BUNDLE"

# Ad-hoc codesign so Gatekeeper accepts the bundle on the build
# machine. End users still see the first-launch warning unless this is
# replaced by a Developer ID signature.
codesign --force --deep --sign - "$APP_BUNDLE" >/dev/null 2>&1 || true

if [[ "${SKIP_PKG:-0}" == "1" ]]; then
  echo "==> SKIP_PKG=1; .app is ready."
  exit 0
fi

# ---- Build the installer ---------------------------------------------------
OUT_DIR="$PROJECT_ROOT/build/macos/out"
mkdir -p "$OUT_DIR"
PKG_PATH="$OUT_DIR/${APP_NAME}-${APP_VERSION}.pkg"
rm -f "$PKG_PATH"

# Stage the postinstall script. pkgbuild requires a dedicated scripts
# directory and every file in it must be executable.
SCRIPTS_STAGE="$(mktemp -d -t describely-pkg-scripts)"
trap 'rm -rf "$SCRIPTS_STAGE"' EXIT
cp "$PROJECT_ROOT/build/macos/pkg/scripts/postinstall" "$SCRIPTS_STAGE/"
chmod +x "$SCRIPTS_STAGE/postinstall"

# Stage the productbuild resources (welcome, conclusion, license).
# productbuild looks for a flat directory of HTML / RTF / TXT files
# referenced from Distribution.xml.
RES_STAGE="$(mktemp -d -t describely-pkg-res)"
trap 'rm -rf "$SCRIPTS_STAGE" "$RES_STAGE"' EXIT
cp "$PROJECT_ROOT/build/macos/pkg/resources/welcome.html"    "$RES_STAGE/"
cp "$PROJECT_ROOT/build/macos/pkg/resources/conclusion.html" "$RES_STAGE/"
# license.txt is just the Terms of Use — the installer's License page
# requires the user to click "Agree" before the Install button enables.
cp "$PROJECT_ROOT/TERMS.md" "$RES_STAGE/license.txt"

COMPONENT_DIR="$(mktemp -d -t describely-pkg-component)"
trap 'rm -rf "$SCRIPTS_STAGE" "$RES_STAGE" "$COMPONENT_DIR"' EXIT
COMPONENT_PKG="$COMPONENT_DIR/Describely-component.pkg"

# Use ``--component`` against the .app directly (canonical single-app
# layout) instead of ``--root`` against a staging tree. The staging /
# install-location combo trips pkgbuild on macOS 13+ when the source
# contains PySide6 Qt frameworks with their nested symlinks. The
# ``--ownership recommended`` flag tells pkgbuild to record sensible
# defaults (root:wheel for system paths) instead of preserving the
# build user's UID/GID, which would otherwise fail the install on
# target machines.
echo "==> Running pkgbuild (component package)"
pkgbuild \
  --component "$APP_BUNDLE" \
  --identifier "$BUNDLE_ID" \
  --version "$APP_VERSION" \
  --install-location "/Applications" \
  --ownership recommended \
  --scripts "$SCRIPTS_STAGE" \
  "$COMPONENT_PKG"

echo "==> Running productbuild (distribution installer)"
productbuild \
  --distribution "$PROJECT_ROOT/build/macos/pkg/Distribution.xml" \
  --resources "$RES_STAGE" \
  --package-path "$COMPONENT_DIR" \
  "$PKG_PATH"

echo "==> Installer produced: $PKG_PATH"
