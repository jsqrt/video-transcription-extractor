from __future__ import annotations

import unittest

from app.cli import build_parser
from app.cli import _validate_args
from app.models.types import CliArgumentError


class CliParserTest(unittest.TestCase):
    def test_help_runs(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["transcribe", "--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_defaults_for_summary(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["transcribe", "--input", "x.mp4"])
        # Summary defaults to the Ollama-backed LLM pipeline. If Ollama
        # is not reachable at runtime the summary step is skipped (with a
        # logged warning) but the transcription file is still produced.
        self.assertEqual(args.summary, "ollama")
        self.assertEqual(args.timeout, 0)
        self.assertTrue(args.summary_file)

    def test_defaults_for_output_schema(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["transcribe", "--input", "x.mp4"])
        self.assertTrue(args.clean_file)
        self.assertEqual(args.clean_mode, "rule-based")

    def test_no_summary_file_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["transcribe", "--input", "x.mp4", "--no-summary-file"]
        )
        self.assertFalse(args.summary_file)

    def test_raw_flag_no_longer_exists(self) -> None:
        """Guard: --no-raw-file/--raw-file were removed in the GUI migration."""
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["transcribe", "--input", "x.mp4", "--no-raw-file"]
            )

    def test_no_clean_file_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["transcribe", "--input", "x.mp4", "--no-clean-file"]
        )
        self.assertFalse(args.clean_file)

    def test_clean_mode_accepts_llm(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["transcribe", "--input", "x.mp4", "--clean-mode", "llm"]
        )
        self.assertEqual(args.clean_mode, "llm")

    def test_clean_mode_accepts_raw(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["transcribe", "--input", "x.mp4", "--clean-mode", "raw"]
        )
        self.assertEqual(args.clean_mode, "raw")

    def test_clean_mode_rejects_unknown(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["transcribe", "--input", "x.mp4", "--clean-mode", "bogus"]
            )

    def test_negative_timeout_rejected(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["transcribe", "--input", "x.mp4", "--timeout", "-1"]
        )
        with self.assertRaises(CliArgumentError):
            _validate_args(args)

    def test_summary_choice_validated_by_argparse(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["transcribe", "--input", "x.mp4", "--summary", "gpt-4"]
            )


if __name__ == "__main__":
    unittest.main()
