#!/usr/bin/env bash
# End-to-end macOS build: PyInstaller .app + DMG with drag-to-Applications.
#
# Usage (run from the repo root):
#   ./build/macos/build.sh
#
# Optional env vars:
#   PY            Python interpreter to use (default: ./.venv/bin/python or python3)
#   SKIP_DMG=1    Build only the .app, skip create-dmg step.
#
# Requires:
#   * Python 3.10+ with the project venv prepared.
#   * pre-seeded model under ./models/large-v3/ (see scripts/fetch_model.py).
#   * `create-dmg` on PATH for the DMG step (`brew install create-dmg`).

set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"
export VTE_PROJECT_ROOT="$PROJECT_ROOT"

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
"$PY" -m pip install --upgrade pip pyinstaller
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

APP_BUNDLE="$DIST/Describely.app"
if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "Expected .app bundle not found: $APP_BUNDLE" >&2
  exit 1
fi
echo "==> .app bundle: $APP_BUNDLE"

# Ad-hoc codesign so Gatekeeper accepts the bundle on the build machine
# without a Developer ID. End users still see the first-launch warning
# unless you replace this with a notarized signature.
codesign --force --deep --sign - "$APP_BUNDLE" >/dev/null 2>&1 || true

if [[ "${SKIP_DMG:-0}" == "1" ]]; then
  echo "==> SKIP_DMG=1; .app is ready."
  exit 0
fi

if ! command -v create-dmg >/dev/null 2>&1; then
  echo "create-dmg not found on PATH (install: brew install create-dmg)." >&2
  echo "The .app is ready at $APP_BUNDLE; skipping DMG step." >&2
  exit 0
fi

OUT_DIR="$PROJECT_ROOT/build/macos/out"
mkdir -p "$OUT_DIR"
DMG_PATH="$OUT_DIR/Describely-1.0.0.dmg"
rm -f "$DMG_PATH"

# Stage a folder that holds .app + an Applications symlink + Terms.
# The Quick Action workflows are now registered automatically by the
# .app on first launch (see app/gui/macos_integration.py); no manual
# Install-QuickAction.command is required in the DMG.
STAGE="$(mktemp -d -t describely-dmg)"
trap 'rm -rf "$STAGE"' EXIT
cp -R "$APP_BUNDLE" "$STAGE/"
cp "$PROJECT_ROOT/TERMS.md" "$STAGE/TERMS.md"

echo "==> Building DMG"
create-dmg \
  --volname "Describely" \
  --window-size 560 400 \
  --icon-size 96 \
  --icon "Describely.app" 140 180 \
  --app-drop-link 420 180 \
  "$DMG_PATH" \
  "$STAGE"

echo "==> DMG produced: $DMG_PATH"
