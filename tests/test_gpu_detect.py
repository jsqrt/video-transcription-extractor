"""GPU vendor detection.

We don't run nvidia-smi for real (it's not available on every CI
host) — we patch shutil.which / subprocess.run to exercise the
decision branches and the env-var override.
"""

from __future__ import annotations

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.gui import gpu_detect


class HasNvidiaGpuTest(unittest.TestCase):
    def setUp(self) -> None:
        gpu_detect.reset_cache()

    def tearDown(self) -> None:
        gpu_detect.reset_cache()

    def test_env_override_true(self) -> None:
        with patch.dict("os.environ", {"DESCRIBELY_FAKE_NVIDIA": "1"}, clear=False):
            self.assertTrue(gpu_detect.has_nvidia_gpu())

    def test_env_override_false_skips_probe(self) -> None:
        with patch.dict("os.environ", {"DESCRIBELY_FAKE_NVIDIA": "0"}, clear=False):
            with patch("shutil.which") as which:
                which.return_value = "/usr/bin/nvidia-smi"
                # Even if the binary exists, the override wins.
                self.assertFalse(gpu_detect.has_nvidia_gpu())
                which.assert_not_called()

    def test_macos_short_circuits(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            # Remove the override if it leaked in from another test.
            with patch.dict("os.environ", {"DESCRIBELY_FAKE_NVIDIA": ""}):
                with patch.object(gpu_detect.sys, "platform", "darwin"):
                    with patch("shutil.which") as which:
                        self.assertFalse(gpu_detect.has_nvidia_gpu())
                        which.assert_not_called()

    def test_no_nvidia_smi_means_no_gpu(self) -> None:
        with patch.dict("os.environ", {"DESCRIBELY_FAKE_NVIDIA": ""}):
            with patch.object(gpu_detect.sys, "platform", "linux"):
                with patch("shutil.which", return_value=None):
                    self.assertFalse(gpu_detect.has_nvidia_gpu())

    def test_smi_lists_gpu_returns_true(self) -> None:
        with patch.dict("os.environ", {"DESCRIBELY_FAKE_NVIDIA": ""}):
            with patch.object(gpu_detect.sys, "platform", "linux"):
                with patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
                    fake = SimpleNamespace(
                        returncode=0, stdout="GPU 0: NVIDIA GeForce RTX 4080\n"
                    )
                    with patch("subprocess.run", return_value=fake):
                        self.assertTrue(gpu_detect.has_nvidia_gpu())

    def test_smi_exits_nonzero_returns_false(self) -> None:
        with patch.dict("os.environ", {"DESCRIBELY_FAKE_NVIDIA": ""}):
            with patch.object(gpu_detect.sys, "platform", "linux"):
                with patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
                    fake = SimpleNamespace(returncode=255, stdout="")
                    with patch("subprocess.run", return_value=fake):
                        self.assertFalse(gpu_detect.has_nvidia_gpu())

    def test_smi_timeout_returns_false(self) -> None:
        with patch.dict("os.environ", {"DESCRIBELY_FAKE_NVIDIA": ""}):
            with patch.object(gpu_detect.sys, "platform", "linux"):
                with patch("shutil.which", return_value="/usr/bin/nvidia-smi"):
                    with patch(
                        "subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=3),
                    ):
                        self.assertFalse(gpu_detect.has_nvidia_gpu())


if __name__ == "__main__":
    unittest.main()
