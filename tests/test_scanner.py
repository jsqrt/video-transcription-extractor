from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.types import ScanError
from app.services.scanner import parse_extensions, scan_videos


class ParseExtensionsTest(unittest.TestCase):
    def test_default_extensions(self) -> None:
        self.assertEqual(parse_extensions(None), {".mp4", ".mov", ".mkv"})

    def test_comma_separated(self) -> None:
        self.assertEqual(parse_extensions("mp4, MOV,mkv"), {".mp4", ".mov", ".mkv"})

    def test_with_leading_dot(self) -> None:
        self.assertEqual(parse_extensions(".mp4,.mov"), {".mp4", ".mov"})

    def test_empty_raises(self) -> None:
        with self.assertRaises(ScanError):
            parse_extensions(", ,")


class ScanVideosTest(unittest.TestCase):
    def test_single_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "clip.mp4"
            video.write_bytes(b"fake")
            result = scan_videos(str(video), {".mp4"})
            self.assertEqual(result, [video.resolve()])

    def test_unsupported_extension_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "clip.avi"
            video.write_bytes(b"fake")
            with self.assertRaises(ScanError):
                scan_videos(str(video), {".mp4"})

    def test_directory_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.mp4").write_bytes(b"x")
            (root / "b.mov").write_bytes(b"x")
            (root / "c.txt").write_bytes(b"x")
            result = scan_videos(str(root), {".mp4", ".mov"})
            self.assertEqual(
                sorted(p.name for p in result),
                ["a.mp4", "b.mov"],
            )

    def test_directory_no_matches_raises(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_bytes(b"x")
            with self.assertRaises(ScanError):
                scan_videos(str(root), {".mp4"})


if __name__ == "__main__":
    unittest.main()
