#!/usr/bin/env bash
# Removes the "Створити транскрипцію" Quick Action previously installed
# by ./install_context_menu.sh. Safe to run repeatedly.

set -euo pipefail

TARGET_BUNDLE="$HOME/Library/Services/CreateTranscription.workflow"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "Error: this uninstaller only runs on macOS." >&2
  exit 1
fi

if [[ -e "$TARGET_BUNDLE" ]]; then
  rm -rf "$TARGET_BUNDLE"
  echo "OK  видалено $TARGET_BUNDLE"
else
  echo "–   $TARGET_BUNDLE не знайдено, нічого видаляти"
fi

# Ask the Services infra to refresh.
if command -v /System/Library/CoreServices/pbs >/dev/null 2>&1; then
  /System/Library/CoreServices/pbs -flush 2>/dev/null || true
fi
/usr/bin/pluginkit -r "$TARGET_BUNDLE" 2>/dev/null || true
