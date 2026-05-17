#!/usr/bin/env bash
# Manual fallback: install the Describely Finder Quick Actions.
#
# NOT shipped in the v1 .pkg installer (Describely.app installs the
# Quick Actions automatically on first launch — see
# app/gui/macos_integration.py). Kept in the repo so a maintainer can
# hand it to a user whose auto-install was blocked (TCC denial, .app
# placed somewhere unusual) and who wants to retry without
# re-installing the .pkg.
#
# Generates two services in ~/Library/Services/:
#   * "Describely Create Transcription.workflow" → writes <name>.clean.md
#   * "Describely Create Summary.workflow"       → writes <name>.summary.md
# Both launch /Applications/Describely.app with --mode set accordingly.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_BUNDLE="$SCRIPT_DIR/_workflow_template"
TARGET_DIR="$HOME/Library/Services"

if [[ ! -d "$TEMPLATE_BUNDLE" ]]; then
  osascript -e 'display alert "Quick Action template missing" message "_workflow_template/ not found next to this script."'
  exit 1
fi

mkdir -p "$TARGET_DIR"

install_one() {
  local target_name="$1"
  local menu_label="$2"
  local mode="$3"
  local target="$TARGET_DIR/$target_name"
  rm -rf "$target"
  cp -R "$TEMPLATE_BUNDLE" "$target"
  # macOS sed needs an explicit backup suffix; '' means in-place no
  # backup file. Two substitutions per file.
  for f in "$target/Contents/Info.plist" "$target/Contents/document.wflow"; do
    sed -i '' "s|__MENU_LABEL__|$menu_label|g" "$f"
    sed -i '' "s|__MODE__|$mode|g" "$f"
  done
  touch "$target"
}

install_one "Describely Create Transcription.workflow" "Create Transcription" "transcription"
install_one "Describely Create Summary.workflow"       "Create Summary"       "summary"

# Refresh Launch Services so Finder picks up the new Services immediately.
/System/Library/CoreServices/pbs -flush 2>/dev/null || true
/usr/bin/pluginkit -e use -i "com.apple.automator.workflow.CreateTranscription" 2>/dev/null || true

osascript -e 'display notification "Installed — right-click a video and look under Quick Actions for Create Transcription / Create Summary" with title "Describely"'
