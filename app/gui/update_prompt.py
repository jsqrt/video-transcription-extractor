"""Monthly "new version available" nag.

A timestamp at ``user_data_dir() / update-prompt.json`` records when the
prompt was last shown. On launch, if at least :data:`UPDATE_INTERVAL_DAYS`
have passed (or the file is missing on first ever launch), the user sees
a modal with two buttons:

* **Yes** — opens :data:`UPDATE_URL` in the default browser and resets
  the timer.
* **Not now** — only resets the timer; nothing else happens.

The check is local-only — no network call. It's a periodic reminder
that an updated build may be available at the download page, not a
real "there is a newer version" check.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import QMessageBox, QWidget

from app.gui.app_logger import log as _file_log
from app.gui.model_manager import user_data_dir

UPDATE_INTERVAL_DAYS = 44
UPDATE_INTERVAL = timedelta(days=UPDATE_INTERVAL_DAYS)
UPDATE_URL = "https://describely.lovable.app/update"
_STATE_FILENAME = "update-prompt.json"


def _state_path():
    return user_data_dir() / _STATE_FILENAME


def _load_last_shown() -> Optional[datetime]:
    path = _state_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("last_shown")
        if not raw:
            return None
        return datetime.fromisoformat(raw)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _save_last_shown(when: datetime) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"last_shown": when.isoformat()}),
        encoding="utf-8",
    )


def _should_show(now: datetime, last_shown: Optional[datetime]) -> bool:
    if last_shown is None:
        return False  # First ever launch — don't nag right after install.
    return (now - last_shown) >= UPDATE_INTERVAL


def maybe_prompt_update(
    parent: Optional[QWidget] = None,
    icon: Optional[QIcon] = None,
) -> None:
    """Show the update prompt if at least a month has passed.

    On the very first launch, the state file is created with today's
    date and no dialog is shown. The reminder fires from launch number
    two onward, only once :data:`UPDATE_INTERVAL_DAYS` have elapsed.
    """
    now = datetime.now(timezone.utc)
    last_shown = _load_last_shown()

    if last_shown is None:
        _save_last_shown(now)
        _file_log("update_prompt: first run; seeded timestamp")
        return

    if not _should_show(now, last_shown):
        return

    _file_log(
        f"update_prompt: showing (last_shown={last_shown.isoformat()})"
    )

    box = QMessageBox(parent)
    box.setWindowTitle("Describely")
    box.setIcon(QMessageBox.Information)
    box.setText("A new version of Describely is available.")
    box.setInformativeText("Would you like to update?")
    yes_btn = box.addButton("Yes", QMessageBox.AcceptRole)
    box.addButton("Not now", QMessageBox.RejectRole)
    box.setDefaultButton(yes_btn)
    if icon is not None:
        box.setWindowIcon(icon)

    box.exec()
    clicked = box.clickedButton()

    # Reset the timer regardless of which button was used.
    _save_last_shown(now)

    if clicked is yes_btn:
        _file_log(f"update_prompt: opening {UPDATE_URL}")
        QDesktopServices.openUrl(QUrl(UPDATE_URL))
    else:
        _file_log("update_prompt: deferred for another month")
