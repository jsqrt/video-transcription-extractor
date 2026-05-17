"""macOS integration: auto-register Finder Quick Actions.

On first launch we materialize two Quick Actions from a single template
into ``~/Library/Services/`` and ask Launch Services to re-scan:

* ``Describely Create Transcription.workflow`` → runs the GUI with
  ``--mode transcription`` (only ``.clean.md`` is produced).
* ``Describely Create Summary.workflow``       → runs the GUI with
  ``--mode summary`` (only ``.summary.md`` is produced).

The template (``_workflow_template/``) ships inside the .app bundle and
contains the literal placeholders ``__MENU_LABEL__`` and ``__MODE__``
that we substitute per-target.

Idempotent and best-effort: on TCC denial or any other failure we log
and move on — the .app still works, the user just won't see the right-
click entries. No-op on non-macOS.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.gui.app_logger import log as _file_log
from app.gui.model_manager import user_data_dir

TEMPLATE_DIRNAME = "_workflow_template"
INSTALL_FLAG_VERSION = "v2"  # bumped: v1 only installed one workflow
INSTALL_FLAG_FILENAME = f"quick-actions-installed-{INSTALL_FLAG_VERSION}.flag"
WORKFLOW_BUNDLE_ID = "com.apple.automator.workflow.CreateTranscription"


@dataclass(frozen=True)
class _QuickActionSpec:
    target_name: str  # filename under ~/Library/Services/
    menu_label: str   # visible text in the Finder right-click menu
    mode: str         # passed to Describely via --mode


_SPECS = (
    _QuickActionSpec(
        target_name="Describely Create Transcription.workflow",
        menu_label="Create Transcription",
        mode="transcription",
    ),
    _QuickActionSpec(
        target_name="Describely Create Summary.workflow",
        menu_label="Create Summary",
        mode="summary",
    ),
)


def _flag_path() -> Path:
    return user_data_dir() / INSTALL_FLAG_FILENAME


def _target_services_dir() -> Path:
    return Path.home() / "Library" / "Services"


def _candidate_template_paths() -> list[Path]:
    """Where might the workflow template live? Mirror model_manager."""
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots += [exe_dir, exe_dir.parent / "Resources"]
    # Dev mode: build/macos/_workflow_template/ in the repo.
    repo_root = Path(__file__).resolve().parents[2]
    roots.append(repo_root / "build" / "macos")
    return [r / TEMPLATE_DIRNAME for r in roots]


def _find_template() -> Optional[Path]:
    for candidate in _candidate_template_paths():
        if candidate.is_dir():
            return candidate
    return None


def _substitute_in_file(path: Path, replacements: dict[str, str]) -> None:
    """Replace literal placeholders in a text file. Skipped on binary."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    new = text
    for key, value in replacements.items():
        new = new.replace(key, value)
    if new != text:
        path.write_text(new, encoding="utf-8")


def _materialize_one(template: Path, target: Path, spec: _QuickActionSpec) -> None:
    """Copy template → target and fill in placeholders."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(template, target)
    replacements = {
        "__MENU_LABEL__": spec.menu_label,
        "__MODE__": spec.mode,
    }
    # The two template files we know contain placeholders.
    for relative in ("Contents/Info.plist", "Contents/document.wflow"):
        _substitute_in_file(target / relative, replacements)


def _record_installed() -> None:
    flag = _flag_path()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(
        f"Describely Quick Actions installed.\nversion={INSTALL_FLAG_VERSION}\n",
        encoding="utf-8",
    )


def quick_actions_installed() -> bool:
    return _flag_path().exists()


# Names produced by earlier versions of this code. They no longer match
# what we ship, so on every install we sweep them out — otherwise the
# user sees duplicate or stale "Create Transcription" entries pointing
# at command lines that lack the new ``--mode`` argument.
_LEGACY_WORKFLOW_NAMES = (
    "CreateTranscription.workflow",
    "CreateSummary.workflow",
)


def _remove_legacy_workflows(target_dir: Path) -> None:
    for name in _LEGACY_WORKFLOW_NAMES:
        legacy = target_dir / name
        if not legacy.exists():
            continue
        try:
            shutil.rmtree(legacy)
        except OSError:
            pass


def install_quick_action(force: bool = False) -> bool:
    """Materialize both Finder Quick Actions and refresh Launch Services.

    Returns True if the install succeeded (or was already done), False
    if anything failed silently. No-op on non-macOS (returns True).
    """
    if sys.platform != "darwin":
        return True

    _file_log("install_quick_action: start")
    target_dir = _target_services_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    # Always sweep legacy names — even if the v2 flag already exists,
    # a user upgrading from a pre-v2 build still has them on disk.
    _remove_legacy_workflows(target_dir)
    _file_log("install_quick_action: legacy swept")

    # If the current-version targets are missing for any reason, force
    # a reinstall regardless of the flag file. This makes the install
    # self-healing when the user moves / deletes a workflow manually.
    targets_missing = any(
        not (target_dir / spec.target_name).is_dir() for spec in _SPECS
    )
    if not force and not targets_missing and quick_actions_installed():
        _file_log("install_quick_action: already installed; skip")
        return True

    template = _find_template()
    if template is None:
        _file_log("install_quick_action: template missing", level="ERROR")
        return False
    _file_log(f"install_quick_action: template={template}")

    ok = True
    for spec in _SPECS:
        target = target_dir / spec.target_name
        try:
            _materialize_one(template, target, spec)
            _file_log(f"install_quick_action: materialized {spec.target_name}")
        except OSError as exc:
            _file_log(f"install_quick_action: failed {spec.target_name}: {exc}", level="ERROR")
            ok = False
            continue
        try:
            target.touch(exist_ok=True)
        except OSError:
            pass

    # Best-effort cache refresh. Both binaries are part of macOS, no
    # external dep. Failures are non-fatal — Finder will pick the
    # services up on next relaunch even without these.
    for command in (
        ["/System/Library/CoreServices/pbs", "-flush"],
        ["/usr/bin/pluginkit", "-e", "use", "-i", WORKFLOW_BUNDLE_ID],
    ):
        try:
            subprocess.run(command, check=False, capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass

    if ok:
        _record_installed()
    return ok
