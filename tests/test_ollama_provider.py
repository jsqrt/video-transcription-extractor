"""Unit tests for OllamaClient helpers.

The interesting logic in the provider is the dynamically-sized context
window (_estimate_num_ctx). A fixed 4096 window used to truncate the START
of long transcripts, so the summary silently lost the opening of the video.
These tests pin the sizing behaviour: short prompts stay at the cheap floor,
long ones grow, and growth is capped (with an env override for the cap).
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from app.providers.ollama_provider import (
    _NUM_CTX_CEIL_DEFAULT,
    _NUM_CTX_FLOOR,
    _estimate_num_ctx,
)


class EstimateNumCtxTest(unittest.TestCase):
    def test_short_prompt_stays_at_floor(self) -> None:
        # A short transcript must not pay for a bigger window than it needs;
        # the 4K floor is the memory-safe size right after Whisper.
        self.assertEqual(_estimate_num_ctx(prompt_chars=1500, num_predict=2048), _NUM_CTX_FLOOR)

    def test_long_prompt_grows_past_floor(self) -> None:
        # ~19k chars is the real videoplayback(1) news transcript that used
        # to come back summarising only its tail. It must now get a window
        # large enough to hold the whole thing.
        ctx = _estimate_num_ctx(prompt_chars=19000, num_predict=2048)
        self.assertGreater(ctx, _NUM_CTX_FLOOR)
        self.assertLessEqual(ctx, _NUM_CTX_CEIL_DEFAULT)

    def test_growth_is_capped_at_ceil(self) -> None:
        # A very long video must not blow past memory: growth stops at ceil.
        self.assertEqual(
            _estimate_num_ctx(prompt_chars=200_000, num_predict=2048),
            _NUM_CTX_CEIL_DEFAULT,
        )

    def test_result_is_rounded_to_1024(self) -> None:
        ctx = _estimate_num_ctx(prompt_chars=19000, num_predict=2048)
        self.assertEqual(ctx % 1024, 0)

    def test_env_override_raises_ceil(self) -> None:
        # DESCRIBELY_OLLAMA_NUM_CTX overrides the CEIL (not a fixed window),
        # so users with headroom can allow bigger windows for huge videos.
        with mock.patch.dict(os.environ, {"DESCRIBELY_OLLAMA_NUM_CTX": "32768"}):
            ctx = _estimate_num_ctx(prompt_chars=200_000, num_predict=2048)
        self.assertEqual(ctx, 32768)


if __name__ == "__main__":
    unittest.main()
