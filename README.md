# Describely

Offline desktop transcription for Windows and macOS. Bundled Whisper
`large-v3` model, fully local execution, right-click integration in
Explorer / Finder.

* **Windows** — `.exe` installer, registers "Create transcription" in
  the right-click menu for common video and audio formats.
* **macOS** — `.dmg` with `.app` (drag-to-Applications) plus a Finder
  Quick Action.
* The `large-v3` model is embedded in the installer — nothing is
  downloaded at runtime.
* Optional Ollama summarizer on `127.0.0.1:11434` (auto-detected if
  running, otherwise falls back to a fast offline extractive
  summarizer).

## Output files

For each processed file, two artifacts appear next to it:

| File | Contents |
|---|---|
| `<name>.clean.md` | Cleaned transcript with thematic chapters `## [MM:SS] Chapter N: Title`. |
| `<name>.summary.md` | Markdown with four sections: `## Overview`, `## Key Facts`, `## Intents & Actions`, `## Per Chapter`. |

> The legacy `<name>.raw.txt` (verbatim Whisper) is no longer written;
> the cleaned `.clean.md` is now the single source of transcript text.

## Install

### Windows

1. Download `Describely-Setup-X.Y.Z.exe` from [Releases](../../releases).
2. Run it. SmartScreen may warn — click "More info" → "Run anyway"
   (the binary is not code-signed yet).
3. The installer shows the **Terms of Use** — you must accept them to
   continue.
4. The installer puts the app per-user in
   `%LocalAppData%\Describely` (no admin required).
5. Keep the **"Add «Create transcription» to the right-click menu"**
   task checked.

### macOS (Intel + Apple Silicon)

1. Download `Describely-X.Y.Z.dmg`.
2. Open it, drag `Describely.app` into `Applications`.
3. Double-click `Install-QuickAction.command` to register the right-
   click Quick Action in Finder.
4. **First launch:** right-click `Describely.app` in `/Applications` →
   Open → Open (to clear Gatekeeper). Only required once.

If `Install-QuickAction.command` fails with "damaged":

```bash
xattr -dr com.apple.quarantine /Applications/Describely.app
xattr -dr com.apple.quarantine "/Volumes/Describely/Install-QuickAction.command"
```

## Usage

**1. Right-click flow:**

1. Select one or more video / audio files in Explorer (Windows) or
   Finder (macOS).
2. Right-click → choose one of:
   * **Create transcription** — writes `<name>.clean.md` only.
   * **Create summary** — writes `<name>.summary.md` only.
3. On macOS, both items live under the Quick Actions submenu (you may
   need to enable them in System Settings → Privacy & Security →
   Extensions → Finder).
4. **Windows 11 note:** the new compact context menu hides legacy shell
   verbs under **"Show more options"** (or press **Shift+F10**). The
   two Describely entries appear there, not in the top-level menu. The
   only way to put them in the new menu is to ship Describely as an
   MSIX package, which is on the v1.1 backlog.
5. A queue window opens. Each file shows:
   * status (`Queued` / `Processing…` / `Done` / `Cancelled` /
     `Failed`),
   * a progress bar,
   * a `Cancel` button (this file only).
6. The toolbar has **"Cancel All"** (stops the current job and drains
   the queue).
7. Double-click any finished row to reveal the output in
   Finder / Explorer.

**2. Drag-and-drop:**

Launch Describely from the Start menu / Spotlight, then drag files or
folders into the window.

**3. "Add Files…" / "Add Folder…":**

Buttons on the toolbar.

## Defaults (hard-coded — no settings panel)

| Setting | Value | Why |
|---|---|---|
| Whisper model | `large-v3` (embedded) | Best quality for English and many other languages, including Ukrainian. |
| Language | Auto-detect | Whisper detects the language of each file. |
| Profile | `best` | Beam=5, slower but more accurate. |
| Cleanup | rule-based | Acoustic dedup + sentence stitching, no LLM in the path. |
| Summary | Ollama → extractive | If `127.0.0.1:11434` is reachable, use the LLM; otherwise run offline extractive. |
| Output | Next to source | Matches user expectations from the right-click flow. |

If you need a different parameter set, use the **CLI** (see below).

## CLI (for power users and scripts)

All flags are preserved, except the removed `--raw-file` /
`--no-raw-file`. Usage is unchanged:

```bash
python -m app transcribe --input ./video.mp4 --profile best --language en --progress
```

Common flags:
* `--input` — file or directory.
* `--ext mp4,mkv,mov` — extensions for batch directory scans.
* `--output-dir` — output directory.
* `--profile fast|best`.
* `--language en|uk|...` (omit for auto-detect).
* `--clean-mode raw|rule-based|llm`.
* `--summary none|extractive|ollama`.
* `--no-clean-file` / `--no-summary-file` — skip an artifact.
* `--timeout 600` — hard per-file deadline.

Full help:

```bash
python -m app transcribe --help
```

## MCP server

The same pipeline is exposed as the MCP tool `transcribe_media` (Claude
Desktop, Claude Code, or any other MCP client).

```bash
pip install "mcp>=1.0.0"
python -m mcp_server
```

The tool returns `transcript_path` (points at `.clean.md`) and
`summary_path`. The old `raw_transcript_path` field was removed in this
release.

Claude Desktop config (Windows):

```json
{
  "mcpServers": {
    "describely": {
      "command": "C:\\path\\to\\describely\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_server"],
      "cwd": "C:\\path\\to\\describely"
    }
  }
}
```

## System requirements

| Component | Minimum | Recommended |
|---|---|---|
| OS | Windows 10 / macOS 12 | Current versions |
| RAM | 8 GB | 16 GB |
| Disk | 4 GB free | 6 GB (model + temp cache) |
| CPU | x86_64 (Windows / Intel Mac) or Apple Silicon | M1+ / Ryzen 5+ / Core i5+ |
| Ollama | — | 0.3+, if you want LLM summaries |

A GPU is not required. On NVIDIA + CUDA 12 the model is accelerated
automatically. On Apple Silicon CTranslate2 uses optimized ARM kernels.

## Privacy

* No telemetry.
* No analytics.
* All processing is local. Network calls are blocked at the socket
  layer except for loopback (`127.0.0.0/8`, `::1`) — used only to talk
  to a local Ollama, if you opt in.

See [TERMS.md](TERMS.md) for the full Terms of Use, license, and
disclaimer.

## Development

```bash
git clone <repo>
cd describely
python -m venv .venv
source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-gui.txt

# Run GUI from source:
python -m app.gui

# Run with files pre-queued:
python -m app.gui /path/to/video1.mp4 /path/to/video2.mp4

# CLI:
python -m app transcribe --input ./video.mp4 --progress

# Tests:
python -m unittest discover -s tests --buffer
```

Building installers — see [BUILD.md](BUILD.md).

## Architecture

```
app/
  cli.py                          CLI entry (python -m app transcribe ...)
  __main__.py                     CLI bootstrap, applies network isolation
  gui/
    __main__.py                   GUI entry (python -m app.gui)
    main_window.py                Queue UI + drag-and-drop
    worker.py                     Background QThread, one cancel event per job
    model_manager.py              Resolves embedded large-v3 path (PyInstaller-aware)
  services/
    pipeline.py                   Single-file orchestrator used by CLI, MCP, GUI
    audio_extractor.py            ffmpeg wrapper
    transcriber.py                Provider-agnostic transcript parsing
    cleanup.py                    Rule-based + LLM cleanup of Whisper artifacts
    chapterizer.py                Splits transcripts into thematic chapters
    summarizer.py                 Extractive + LLM (Ollama) summarizer
    writer.py                     Writes <stem>.clean.md
    summary_writer.py             Writes <stem>.summary.md
  providers/
    faster_whisper_provider.py    Whisper backend, CUDA-with-CPU-fallback
    ollama_provider.py            httpx client for local Ollama
  security/
    network_isolation.py          Monkey-patches socket: loopback-only
mcp_server/                       MCP tool: transcribe_media
build/
  assets/                         Source icon (SVG) + generated .ico/.icns
  pyinstaller/videote.spec        Single PyInstaller spec for both platforms
  windows/                        Inno Setup script + build.ps1
  macos/                          DMG layout + Quick Action + build.sh
scripts/
  fetch_model.py                  Pre-seed models/large-v3/ before building
  generate_icons.py               Render icon.svg → app.ico + app.icns
TERMS.md                          Terms of Use shown at install time
```

## License

MIT — see [TERMS.md](TERMS.md).
