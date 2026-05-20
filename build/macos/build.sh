#!/usr/bin/env bash
# End-to-end macOS build: PyInstaller .app + signed .pkg installer.
#
# Producing both Intel and Apple Silicon installers is a two-machine
# job: run this script once on each, with the matching VTE_MAC_ARCH:
#
#   Apple Silicon host:  VTE_MAC_ARCH=arm64   ./build/macos/build.sh
#   Intel host:          VTE_MAC_ARCH=x86_64  ./build/macos/build.sh
#
# Output (per run):
#   build/macos/out/Describely-1.0.0-arm64.pkg   (Apple Silicon only)
#   build/macos/out/Describely-1.0.0-x86_64.pkg  (Intel only)
#
# Each .pkg refuses installation on the other architecture via
# Distribution.xml's hostArchitectures filter — users can't pick the
# wrong one by mistake.
#
# Optional env vars:
#   PY                Python interpreter (default: ./.venv/bin/python or python3)
#   SKIP_PKG=1        Build only the .app, skip the installer step.
#   VTE_MAC_ARCH      Target arch: arm64, x86_64, or universal2.
#                     Defaults to host arch (uname -m).
#                     universal2 is supported but in practice some
#                     wheels (ctranslate2, pyav, llama-cpp-python) only
#                     publish single-arch builds, so the post-build
#                     check will reject the result. Prefer the two-pkg
#                     flow above.
#
# Requires:
#   * Python 3.10+ matching the target arch.
#   * Pre-seeded model under ./models/large-v3/ (scripts/fetch_model.py).
#   * macOS native tooling: pkgbuild, productbuild, iconutil. All ship
#     with the OS — no Homebrew needed for the .pkg path.

set -euo pipefail

PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"
export VTE_PROJECT_ROOT="$PROJECT_ROOT"

APP_VERSION="1.0.0"
APP_NAME="Describely"
BUNDLE_ID="com.describely.app"

HOST_ARCH="$(uname -m)"
VTE_MAC_ARCH="${VTE_MAC_ARCH:-$HOST_ARCH}"
case "$VTE_MAC_ARCH" in
  arm64|x86_64|universal2) ;;
  *)
    echo "Invalid VTE_MAC_ARCH='$VTE_MAC_ARCH'. Use arm64, x86_64, or universal2." >&2
    exit 2
    ;;
esac
export VTE_MAC_ARCH
echo "==> Host arch:   $HOST_ARCH"
echo "==> Target arch: $VTE_MAC_ARCH"

# Sanity check the host/target combo. PyInstaller cannot truly
# cross-compile — the running Python interpreter must contain the
# target architecture as one of its slices.
case "$VTE_MAC_ARCH:$HOST_ARCH" in
  arm64:x86_64)
    echo "ERROR: arm64 build requires an Apple Silicon host." >&2
    echo "       Run this on an M-series Mac, or set VTE_MAC_ARCH=x86_64 here." >&2
    exit 2
    ;;
  x86_64:arm64)
    cat >&2 <<'EOF'
WARNING: building x86_64 on Apple Silicon needs Rosetta wrappers AND
         an x86_64 Python interpreter (not the default arm64 one).
         The standard recipe:

           softwareupdate --install-rosetta --agree-to-license  # once
           arch -x86_64 /bin/zsh
           python3 -m venv .venv-x86_64        # use an x86_64 Python
           source .venv-x86_64/bin/activate
           pip install -r requirements-gui.txt
           VTE_MAC_ARCH=x86_64 ./build/macos/build.sh

         The python interpreter's arch check below will catch the
         common mistake of using the arm64 Python.
EOF
    ;;
  universal2:x86_64)
    echo "ERROR: universal2 builds require an Apple Silicon host." >&2
    echo "       (uname -m reported: $HOST_ARCH)." >&2
    exit 2
    ;;
esac

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

# Verify the Python interpreter contains the slice we plan to build.
# ``file`` reports a Mach-O architecture summary that's easy to grep.
PY_REAL="$("$PY" -c 'import sys; print(sys.executable)')"
PY_ARCH_REPORT="$(file -L "$PY_REAL" 2>/dev/null || true)"
echo "==> Python arch: $PY_ARCH_REPORT"
case "$VTE_MAC_ARCH" in
  universal2)
    if ! grep -q "x86_64" <<<"$PY_ARCH_REPORT" || ! grep -q "arm64" <<<"$PY_ARCH_REPORT"; then
      echo "ERROR: Python at $PY_REAL is not universal2 (lacks one slice)." >&2
      echo "       Install Python from python.org (universal2 installer) and" >&2
      echo "       create the venv from it." >&2
      exit 2
    fi
    ;;
  arm64|x86_64)
    if ! grep -q "$VTE_MAC_ARCH" <<<"$PY_ARCH_REPORT"; then
      echo "ERROR: Python at $PY_REAL does not contain the $VTE_MAC_ARCH slice." >&2
      echo "       For an x86_64 build on Apple Silicon, run pip under" >&2
      echo "       arch -x86_64 with an x86_64 Python." >&2
      exit 2
    fi
    ;;
esac

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

# Verify the main executable contains the architectures we asked for.
# A silent single-arch result (because a wheel was the wrong arch)
# would ship a broken bundle to half the users.
MAIN_EXE="$APP_BUNDLE/Contents/MacOS/${APP_NAME}"
if [[ -f "$MAIN_EXE" ]]; then
  EXE_ARCH_REPORT="$(file -L "$MAIN_EXE" 2>/dev/null || true)"
  echo "==> Bundle arch: $EXE_ARCH_REPORT"
  case "$VTE_MAC_ARCH" in
    universal2)
      if ! grep -q "x86_64" <<<"$EXE_ARCH_REPORT" || ! grep -q "arm64" <<<"$EXE_ARCH_REPORT"; then
        cat >&2 <<EOF
ERROR: requested universal2 but the produced binary is single-arch.
       At least one Python dependency only shipped a single-arch wheel
       (typical offenders: ctranslate2, pyav, tokenizers,
       llama-cpp-python, onnxruntime).

       Fix path: ship two single-arch .pkg files instead. Build twice:
           VTE_MAC_ARCH=arm64  ./build/macos/build.sh
           VTE_MAC_ARCH=x86_64 ./build/macos/build.sh  # on / via Intel
EOF
        exit 3
      fi
      ;;
    arm64|x86_64)
      if ! grep -q "$VTE_MAC_ARCH" <<<"$EXE_ARCH_REPORT"; then
        echo "ERROR: requested $VTE_MAC_ARCH but produced binary lacks that arch." >&2
        exit 3
      fi
      ;;
  esac
fi

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

# Output filename and hostArchitectures filter both encode the target
# arch so two .pkg files can sit next to each other and each one only
# installs where it makes sense.
case "$VTE_MAC_ARCH" in
  universal2)
    PKG_PATH="$OUT_DIR/${APP_NAME}-${APP_VERSION}.pkg"
    HOST_ARCHITECTURES="x86_64,arm64"
    ;;
  arm64)
    PKG_PATH="$OUT_DIR/${APP_NAME}-${APP_VERSION}-arm64.pkg"
    HOST_ARCHITECTURES="arm64"
    ;;
  x86_64)
    PKG_PATH="$OUT_DIR/${APP_NAME}-${APP_VERSION}-x86_64.pkg"
    HOST_ARCHITECTURES="x86_64"
    ;;
esac
rm -f "$PKG_PATH"

# Stage the postinstall script. pkgbuild requires a dedicated scripts
# directory and every file in it must be executable.
SCRIPTS_STAGE="$(mktemp -d -t describely-pkg-scripts)"
trap 'rm -rf "$SCRIPTS_STAGE"' EXIT
cp "$PROJECT_ROOT/build/macos/pkg/scripts/postinstall" "$SCRIPTS_STAGE/"
chmod +x "$SCRIPTS_STAGE/postinstall"

# Stage the productbuild resources (welcome, conclusion, license) and
# the Distribution.xml rendered from the template with the right arch.
RES_STAGE="$(mktemp -d -t describely-pkg-res)"
trap 'rm -rf "$SCRIPTS_STAGE" "$RES_STAGE"' EXIT
cp "$PROJECT_ROOT/build/macos/pkg/resources/welcome.html"    "$RES_STAGE/"
cp "$PROJECT_ROOT/build/macos/pkg/resources/conclusion.html" "$RES_STAGE/"
# license.txt is just the Terms of Use — the installer's License page
# requires the user to click "Agree" before the Install button enables.
cp "$PROJECT_ROOT/TERMS.md" "$RES_STAGE/license.txt"

DIST_TEMPLATE="$PROJECT_ROOT/build/macos/pkg/Distribution.xml.in"
DIST_RENDERED="$(mktemp -t describely-Distribution.xml).out"
trap 'rm -rf "$SCRIPTS_STAGE" "$RES_STAGE" "$DIST_RENDERED"' EXIT
sed "s|@HOST_ARCHITECTURES@|${HOST_ARCHITECTURES}|g" \
    "$DIST_TEMPLATE" > "$DIST_RENDERED"

COMPONENT_DIR="$(mktemp -d -t describely-pkg-component)"
trap 'rm -rf "$SCRIPTS_STAGE" "$RES_STAGE" "$DIST_RENDERED" "$COMPONENT_DIR"' EXIT
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
  --distribution "$DIST_RENDERED" \
  --resources "$RES_STAGE" \
  --package-path "$COMPONENT_DIR" \
  "$PKG_PATH"

echo "==> Installer produced: $PKG_PATH"
echo "==> Host arch filter:   $HOST_ARCHITECTURES"
