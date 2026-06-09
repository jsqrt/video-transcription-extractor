"""Unit tests for the monthly update-prompt cadence.

We do not exercise the Qt dialog itself (no QApplication in the test
environment); we only verify the pure-Python cadence logic — when the
prompt is and isn't due, and that the state file round-trips correctly.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.gui import update_prompt
from app.gui.update_prompt import (
    UPDATE_INTERVAL,
    _load_last_shown,
    _save_last_shown,
    _should_show,
)


class ShouldShowTest(unittest.TestCase):
    def test_first_run_does_not_show(self) -> None:
        self.assertFalse(_should_show(datetime.now(timezone.utc), last_shown=None))

    def test_recent_does_not_show(self) -> None:
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        last = now - (UPDATE_INTERVAL / 2)
        self.assertFalse(_should_show(now, last))

    def test_interval_boundary_shows(self) -> None:
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        last = now - UPDATE_INTERVAL
        self.assertTrue(_should_show(now, last))

    def test_long_ago_shows(self) -> None:
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        last = now - (UPDATE_INTERVAL * 5)
        self.assertTrue(_should_show(now, last))


class StateFileRoundTripTest(unittest.TestCase):
    def test_save_and_load(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(update_prompt, "user_data_dir", lambda: Path(tmp)):
                self.assertIsNone(_load_last_shown())
                ts = datetime(2026, 1, 15, 12, 30, tzinfo=timezone.utc)
                _save_last_shown(ts)
                loaded = _load_last_shown()
                self.assertEqual(loaded, ts)

    def test_corrupt_file_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(update_prompt, "user_data_dir", lambda: Path(tmp)):
                (Path(tmp) / "update-prompt.json").write_text("not json", encoding="utf-8")
                self.assertIsNone(_load_last_shown())

    def test_missing_field_returns_none(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(update_prompt, "user_data_dir", lambda: Path(tmp)):
                (Path(tmp) / "update-prompt.json").write_text(json.dumps({}), encoding="utf-8")
                self.assertIsNone(_load_last_shown())


if __name__ == "__main__":
    unittest.main()
