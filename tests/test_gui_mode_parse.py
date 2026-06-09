"""Guard the OS context-menu --mode parsing in app.gui.__main__.

The Windows installer and the macOS Quick Action register two verbs that
launch the GUI with ``--mode transcription`` and ``--mode summary``. There
is no summary-only JobMode (a summary needs a transcript), so "summary"
must resolve to BOTH. This used to fall through silently to the default;
these tests pin the explicit alias so the menu labels stay honest.
"""

from __future__ import annotations

import unittest

from app.gui.__main__ import _parse_mode
from app.gui.worker import JobMode


class ParseModeTests(unittest.TestCase):
    def test_transcription_maps_to_transcription(self):
        self.assertEqual(
            _parse_mode(["--mode", "transcription", "/tmp/a.mp4"]),
            JobMode.TRANSCRIPTION,
        )

    def test_summary_maps_to_both(self):
        # "Create summary" verb → both artifacts (transcript + summary).
        self.assertEqual(
            _parse_mode(["--mode", "summary", "/tmp/a.mp4"]),
            JobMode.BOTH,
        )

    def test_both_maps_to_both(self):
        self.assertEqual(_parse_mode(["--mode=both", "/tmp/a.mp4"]), JobMode.BOTH)

    def test_equals_form_is_accepted(self):
        self.assertEqual(
            _parse_mode(["--mode=transcription"]), JobMode.TRANSCRIPTION
        )

    def test_missing_or_unknown_defaults_to_both(self):
        self.assertEqual(_parse_mode(["/tmp/a.mp4"]), JobMode.BOTH)
        self.assertEqual(_parse_mode(["--mode", "bogus"]), JobMode.BOTH)


if __name__ == "__main__":
    unittest.main()
