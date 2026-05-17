#!/usr/bin/env bash
# Installs the "Create Transcription" Quick Action into ~/Library/Services.
# After install: right-click any video in Finder → Quick Actions →
# "Create Transcription" launches Describely.app with the selected files.
#
# Bundled inside the DMG next to the .app; double-click it after dragging
# the .app into /Applications.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_BUNDLE="$SCRIPT_DIR/CreateTranscription.workflow"
TARGET_DIR="$HOME/Library/Services"
TARGET_BUNDLE="$TARGET_DIR/CreateTranscription.workflow"

if [[ ! -d "$TEMPLATE_BUNDLE" ]]; then
  osascript -e 'display alert "Quick Action template missing" message "CreateTranscription.workflow not found next to this script."'
  exit 1
fi

mkdir -p "$TARGET_DIR"
rm -rf "$TARGET_BUNDLE"
cp -R "$TEMPLATE_BUNDLE" "$TARGET_BUNDLE"

# Refresh Launch Services so Finder picks up the new Service immediately.
/System/Library/CoreServices/pbs -flush 2>/dev/null || true
/usr/bin/pluginkit -e use -i "com.apple.automator.workflow.CreateTranscription" 2>/dev/null || true
touch "$TARGET_BUNDLE"

osascript -e 'display notification "Installed — right-click a video and choose Quick Actions → Create Transcription" with title "Describely"'
