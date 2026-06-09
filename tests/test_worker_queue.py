"""Unit tests for TranscriptionWorker queue-management primitives.

These cover the public API the GUI relies on — pause/resume, reorder,
remove_finished, forced_language plumbing on Job. They don't spin up
the worker thread (no Qt event loop in CI); they exercise the data
structures under the public lock, which is exactly what the GUI
interacts with."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt

from app.gui.worker import (
    Job,
    JobMode,
    JobStatus,
    TranscribeMode,
    TranscriptionWorker,
    _PipelineServices,
)


# A QCoreApplication has to exist for any QObject signal/slot to work,
# even with DirectConnection — QObject's metaobject machinery refuses
# to wire connections without it. We create one process-wide instance
# the first time a test imports this module; subsequent tests reuse it.
_app = QCoreApplication.instance() or QCoreApplication(sys.argv)


def _make_worker() -> TranscriptionWorker:
    """Construct a worker without ever starting its QThread."""
    return TranscriptionWorker()


class AddFilesTest(unittest.TestCase):
    def test_jobs_default_to_no_forced_language(self) -> None:
        worker = _make_worker()
        jobs = worker.add_files([Path("/tmp/a.mp4")], mode=JobMode.BOTH)
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0].forced_language)
        self.assertIsNone(jobs[0].detected_language)

    def test_forced_language_propagates_to_each_job(self) -> None:
        # The right-click "Re-run as Ukrainian" path passes
        # forced_language; the worker has to thread it onto every new
        # Job so the run_pipeline call later picks it up as
        # ``options.language``.
        worker = _make_worker()
        jobs = worker.add_files(
            [Path("/tmp/a.mp4"), Path("/tmp/b.mp4")],
            mode=JobMode.TRANSCRIPTION,
            forced_language="uk",
        )
        self.assertEqual([j.forced_language for j in jobs], ["uk", "uk"])
        self.assertEqual([j.mode for j in jobs], [JobMode.TRANSCRIPTION, JobMode.TRANSCRIPTION])

    def test_transcribe_mode_defaults_to_speed(self) -> None:
        # Speed is the default everywhere unless the user explicitly picks
        # Quality in the GUI popup. add_files() with no transcribe_mode
        # must therefore land on Speed (the GUI passes the sticky choice
        # explicitly; this guards the underlying default).
        worker = _make_worker()
        jobs = worker.add_files([Path("/tmp/a.mp4")], mode=JobMode.BOTH)
        self.assertEqual(jobs[0].transcribe_mode, TranscribeMode.SPEED)

    def test_transcribe_mode_propagates_to_each_job(self) -> None:
        worker = _make_worker()
        jobs = worker.add_files(
            [Path("/tmp/a.mp4"), Path("/tmp/b.mp4")],
            mode=JobMode.BOTH,
            transcribe_mode=TranscribeMode.SPEED,
        )
        self.assertEqual(
            [j.transcribe_mode for j in jobs],
            [TranscribeMode.SPEED, TranscribeMode.SPEED],
        )


class SetTranscribeModeTest(unittest.TestCase):
    def test_changes_mode_on_queued_job(self) -> None:
        worker = _make_worker()
        jobs = worker.add_files([Path("/tmp/a.mp4")], mode=JobMode.BOTH)
        ok = worker.set_transcribe_mode(jobs[0].job_id, TranscribeMode.SPEED)
        self.assertTrue(ok)
        self.assertEqual(
            worker.find_job(jobs[0].job_id).transcribe_mode,
            TranscribeMode.SPEED,
        )

    def test_rejects_change_on_running_job(self) -> None:
        # Once a job is PROCESSING the model is already loaded — there
        # is no coherent way to switch backends mid-run, so the worker
        # rejects the request and the UI knows to roll the combo back.
        worker = _make_worker()
        jobs = worker.add_files([Path("/tmp/a.mp4")], mode=JobMode.BOTH)
        # Job defaults to Speed; attempt to flip it to Quality mid-run so
        # the "not mutated" assertion below is meaningful (a different
        # value from the starting one).
        jobs[0].status = JobStatus.PROCESSING
        ok = worker.set_transcribe_mode(jobs[0].job_id, TranscribeMode.QUALITY)
        self.assertFalse(ok)
        # Mode must NOT have been mutated.
        self.assertEqual(
            worker.find_job(jobs[0].job_id).transcribe_mode,
            TranscribeMode.SPEED,
        )

    def test_rejects_change_on_missing_job(self) -> None:
        worker = _make_worker()
        self.assertFalse(worker.set_transcribe_mode(999, TranscribeMode.SPEED))


class BackendRoutingTest(unittest.TestCase):
    """``_PipelineServices._prefer_whisper_cpp`` is the single decision
    point that picks the ASR backend per job. Get it wrong and the
    Quality switch silently does nothing on macOS, so we guard every
    branch explicitly."""

    def test_quality_routes_to_faster_whisper_on_macos(self) -> None:
        # The whole point of Quality mode: even on macOS (where Speed
        # would pick whisper.cpp for Metal acceleration) Quality must
        # go to faster-whisper so we get multilingual decoding.
        with _patched_platform("darwin"):
            self.assertFalse(
                _PipelineServices._prefer_whisper_cpp(
                    override="",
                    transcribe_mode=TranscribeMode.QUALITY,
                    logger_fn=lambda _msg: None,
                )
            )

    def test_speed_routes_to_whisper_cpp_on_macos(self) -> None:
        with _patched_platform("darwin"):
            self.assertTrue(
                _PipelineServices._prefer_whisper_cpp(
                    override="",
                    transcribe_mode=TranscribeMode.SPEED,
                    logger_fn=lambda _msg: None,
                )
            )

    def test_quality_routes_to_faster_whisper_on_windows(self) -> None:
        # No CUDA assumption needed — Quality always picks faster-whisper.
        with _patched_platform("win32"):
            self.assertFalse(
                _PipelineServices._prefer_whisper_cpp(
                    override="",
                    transcribe_mode=TranscribeMode.QUALITY,
                    logger_fn=lambda _msg: None,
                )
            )

    def test_speed_on_windows_with_nvidia_picks_faster_whisper(self) -> None:
        # NVIDIA CUDA via CTranslate2 is the fastest path on Windows,
        # so Speed defers to faster-whisper there even though it would
        # otherwise prefer whisper.cpp's accelerated paths.
        with _patched_platform("win32"), _patched_nvidia(True):
            self.assertFalse(
                _PipelineServices._prefer_whisper_cpp(
                    override="",
                    transcribe_mode=TranscribeMode.SPEED,
                    logger_fn=lambda _msg: None,
                )
            )

    def test_speed_on_windows_without_nvidia_picks_whisper_cpp(self) -> None:
        with _patched_platform("win32"), _patched_nvidia(False):
            self.assertTrue(
                _PipelineServices._prefer_whisper_cpp(
                    override="",
                    transcribe_mode=TranscribeMode.SPEED,
                    logger_fn=lambda _msg: None,
                )
            )

    def test_global_override_wins_over_mode(self) -> None:
        # The support env var DESCRIBELY_ASR_BACKEND must keep working
        # even with Quality/Speed set, so existing support workflows
        # don't break.
        with _patched_platform("darwin"):
            self.assertTrue(
                _PipelineServices._prefer_whisper_cpp(
                    override="whisper-cpp",
                    transcribe_mode=TranscribeMode.QUALITY,
                    logger_fn=lambda _msg: None,
                )
            )
            self.assertFalse(
                _PipelineServices._prefer_whisper_cpp(
                    override="faster-whisper",
                    transcribe_mode=TranscribeMode.SPEED,
                    logger_fn=lambda _msg: None,
                )
            )


# Helpers ---------------------------------------------------------------------


class _patched_platform:
    """Temporarily flip ``sys.platform`` for routing tests."""

    def __init__(self, platform: str) -> None:
        self._platform = platform
        self._original = sys.platform

    def __enter__(self) -> "_patched_platform":
        sys.platform = self._platform
        return self

    def __exit__(self, *exc) -> None:
        sys.platform = self._original


class _patched_nvidia:
    """Force ``has_nvidia_gpu`` to a known answer for routing tests."""

    def __init__(self, present: bool) -> None:
        self._present = present
        self._original = None

    def __enter__(self) -> "_patched_nvidia":
        from app.gui import gpu_detect

        self._original = gpu_detect.has_nvidia_gpu
        gpu_detect.has_nvidia_gpu = lambda: self._present  # type: ignore[assignment]
        return self

    def __exit__(self, *exc) -> None:
        from app.gui import gpu_detect

        gpu_detect.has_nvidia_gpu = self._original  # type: ignore[assignment]


class PauseTest(unittest.TestCase):
    def test_pause_state_round_trips(self) -> None:
        worker = _make_worker()
        self.assertFalse(worker.is_paused())
        worker.set_paused(True)
        self.assertTrue(worker.is_paused())
        worker.set_paused(False)
        self.assertFalse(worker.is_paused())

    def test_pause_state_changed_emits_only_on_transition(self) -> None:
        # Setting the same state twice must not flood the GUI with
        # redundant signals (the toolbar checkbox flicker that caused).
        # DirectConnection sidesteps the worker thread's event loop
        # (which we haven't started in tests) so emits are delivered
        # synchronously on this thread.
        worker = _make_worker()
        seen: list[bool] = []
        worker.pause_state_changed.connect(seen.append, Qt.DirectConnection)
        worker.set_paused(True)
        worker.set_paused(True)  # duplicate
        worker.set_paused(False)
        worker.set_paused(False)  # duplicate
        self.assertEqual(seen, [True, False])


class ReorderTest(unittest.TestCase):
    def test_reorder_only_moves_queued_jobs(self) -> None:
        # Done / processing jobs must keep their relative positions —
        # reordering a finished row is meaningless and reordering a
        # running row would race with the worker thread.
        worker = _make_worker()
        jobs = worker.add_files(
            [Path(f"/tmp/{c}.mp4") for c in "abcde"], mode=JobMode.BOTH
        )
        # Simulate: A is done, B is processing, C/D/E are still queued.
        jobs[0].status = JobStatus.DONE
        jobs[1].status = JobStatus.PROCESSING

        # Reverse the queued segment: ask for E, D, C.
        worker.reorder_queued([jobs[4].job_id, jobs[3].job_id, jobs[2].job_id])
        snapshot = worker.snapshot()
        ids_in_order = [j.job_id for j in snapshot]
        # A and B keep their slots; C/D/E reverse in their queued slots.
        self.assertEqual(
            ids_in_order,
            [
                jobs[0].job_id,  # A — DONE
                jobs[1].job_id,  # B — PROCESSING
                jobs[4].job_id,  # E — was queued
                jobs[3].job_id,  # D — was queued
                jobs[2].job_id,  # C — was queued
            ],
        )

    def test_reorder_appends_unmentioned_queued_jobs(self) -> None:
        # If the GUI tells us about only some of the queued jobs, the
        # rest must not vanish — they keep their tail position.
        worker = _make_worker()
        jobs = worker.add_files(
            [Path(f"/tmp/{c}.mp4") for c in "abc"], mode=JobMode.BOTH
        )
        worker.reorder_queued([jobs[2].job_id])
        snapshot_ids = [j.job_id for j in worker.snapshot()]
        # C moves to the front of the queued block; A and B follow.
        self.assertEqual(
            snapshot_ids,
            [jobs[2].job_id, jobs[0].job_id, jobs[1].job_id],
        )


class RemoveFinishedTest(unittest.TestCase):
    def test_removes_terminal_states_and_keeps_active(self) -> None:
        worker = _make_worker()
        jobs = worker.add_files(
            [Path(f"/tmp/{c}.mp4") for c in "abcd"], mode=JobMode.BOTH
        )
        jobs[0].status = JobStatus.DONE
        jobs[1].status = JobStatus.FAILED
        jobs[2].status = JobStatus.CANCELLED
        # jobs[3] stays QUEUED.

        removed = worker.remove_finished()
        self.assertEqual(sorted(removed), sorted([jobs[0].job_id, jobs[1].job_id, jobs[2].job_id]))
        remaining = worker.snapshot()
        self.assertEqual([j.job_id for j in remaining], [jobs[3].job_id])

    def test_no_terminal_jobs_is_a_noop(self) -> None:
        worker = _make_worker()
        worker.add_files([Path("/tmp/a.mp4")], mode=JobMode.BOTH)
        self.assertEqual(worker.remove_finished(), [])


if __name__ == "__main__":
    unittest.main()
