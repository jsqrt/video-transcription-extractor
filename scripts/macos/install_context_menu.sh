#!/usr/bin/env bash
# Installs the "Створити транскрипцію" Quick Action into
# ~/Library/Services so it appears in Finder's right-click menu
# (Services submenu) for audio and video files.
#
# Idempotent: re-running reinstalls the workflow in place.
#
# Usage:
#   ./install_context_menu.sh            # install with resolved paths
#   ./install_context_menu.sh --verbose  # echo the resolved paths
#   MENU_LABEL="Transcribe" ./install_context_menu.sh   # override label
#
# Requires macOS. Does not require sudo — writes only to $HOME.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." &>/dev/null && pwd)"
TEMPLATE_BUNDLE="$SCRIPT_DIR/CreateTranscription.workflow"
TARGET_DIR="$HOME/Library/Services"
TARGET_BUNDLE="$TARGET_DIR/CreateTranscription.workflow"
MENU_LABEL="${MENU_LABEL:-Створити транскрипцію}"

VERBOSE=0
for arg in "$@"; do
  case "$arg" in
    -v|--verbose) VERBOSE=1 ;;
    -h|--help)
      sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

log() { [[ "$VERBOSE" -eq 1 ]] && echo "• $*"; true; }

if [[ "$(uname)" != "Darwin" ]]; then
  echo "Error: this installer only runs on macOS." >&2
  exit 1
fi

if [[ ! -d "$TEMPLATE_BUNDLE" ]]; then
  echo "Error: template bundle not found at $TEMPLATE_BUNDLE" >&2
  exit 1
fi

log "Project root: $PROJECT_ROOT"
log "Target bundle: $TARGET_BUNDLE"
log "Menu label:   $MENU_LABEL"

mkdir -p "$TARGET_DIR"

# Re-install: blow away any previous copy first so we never end up with
# stale files inside the bundle.
if [[ -e "$TARGET_BUNDLE" ]]; then
  log "Removing existing bundle at $TARGET_BUNDLE"
  rm -rf "$TARGET_BUNDLE"
fi

log "Copying template to $TARGET_BUNDLE"
cp -R "$TEMPLATE_BUNDLE" "$TARGET_BUNDLE"

# Substitute the placeholder path inside document.wflow with the real
# absolute project root. Using a Python one-liner so we don't have to
# worry about sed's path-escape quirks.
python3 - "$TARGET_BUNDLE/Contents/document.wflow" "$PROJECT_ROOT" <<'PYEOF'
import sys, pathlib
wflow = pathlib.Path(sys.argv[1])
project_root = sys.argv[2]
text = wflow.read_text(encoding="utf-8")
if "__PROJECT_ROOT__" not in text:
    # Already substituted (reinstall from an edited copy, etc.) – harmless.
    print("(project root already substituted; nothing to do)", file=sys.stderr)
    sys.exit(0)
wflow.write_text(text.replace("__PROJECT_ROOT__", project_root), encoding="utf-8")
PYEOF

# Update the menu label if the caller asked for a custom one. The default
# label shipped with the template is "Створити транскрипцію"; we rewrite
# the Info.plist's NSMenuItem.default value in place with plistlib.
if [[ "$MENU_LABEL" != "Створити транскрипцію" ]]; then
  python3 - "$TARGET_BUNDLE/Contents/Info.plist" "$MENU_LABEL" <<'PYEOF'
import plistlib, sys, pathlib
p = pathlib.Path(sys.argv[1])
label = sys.argv[2]
data = plistlib.loads(p.read_bytes())
for svc in data.get("NSServices", []):
    svc.setdefault("NSMenuItem", {})["default"] = label
p.write_bytes(plistlib.dumps(data))
PYEOF
fi

# Ask Launch Services / pbs to rescan the Services directory so the new
# menu item shows up without a logout. Both commands are safe no-ops if
# the binary isn't found.
if command -v /System/Library/CoreServices/pbs >/dev/null 2>&1; then
  /System/Library/CoreServices/pbs -flush 2>/dev/null || true
fi
/usr/bin/pluginkit -e use -i "com.apple.automator.workflow.CreateTranscription" 2>/dev/null || true

# Bump mtime so Finder notices.
touch "$TARGET_BUNDLE"

cat <<EOF

OK  "${MENU_LABEL}" встановлено у ~/Library/Services/CreateTranscription.workflow

Далі:
  1. Клацни правою кнопкою на будь-якому відео/аудіо файлі в Finder
  2. Перейди в підменю  Швидкі дії / Quick Actions  (або  Services )
  3. Вибери  "${MENU_LABEL}"

Якщо меню не зʼявилось — відкрий:
  System Settings -> Privacy & Security -> Extensions -> Finder
  і увімкни галочку  "${MENU_LABEL}".

Для видалення запусти:  $SCRIPT_DIR/uninstall_context_menu.sh
EOF
