# Building Describely installers

This document is for **maintainers** who produce releases. End users
only need the resulting `.exe` (Windows) or `.dmg` (macOS).

## One-time prerequisites

- Python 3.10+ (3.11 recommended).
- `ffmpeg` on `PATH` (used on the build machine; the user bundle ships
  its own audio decoder via PyAV, so end users do not need ffmpeg).
- On Windows: **Visual Studio Build Tools 2022** with the "Desktop
  development with C++" workload, OR install `llama-cpp-python` from
  prebuilt wheels (recommended, see below). Without one of these the
  pip install of `llama-cpp-python` will fail.
- A configured `.venv`:
  ```
  python -m venv .venv
  # Windows:
  .venv\Scripts\activate
  # macOS / Linux:
  source .venv/bin/activate
  pip install -r requirements-gui.txt
  ```

### Platform-specific

- **Windows:** [Inno Setup 6](https://jrsoftware.org/isinfo.php). The
  default `ISCC.exe` path is `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`;
  override with `-IsccPath` in `build.ps1` if installed elsewhere.
- **macOS:** Nothing extra to install. The .pkg installer is built via
  `pkgbuild` + `productbuild`, both shipped with macOS. (Earlier
  versions of this project used `create-dmg`; that flow is retired.)

### Optional, for nicer icons

```
pip install pillow
```

PySide6 (already a build dependency) handles the SVG rasterization, so
no additional system Cairo / `cairosvg` install is needed. The icon
generator falls back to skipping the step if Pillow is missing — the
build still succeeds with the default Python icon.

### Optional: AMD / Intel GPU Whisper on Windows (Vulkan)

By default the Windows bundle uses faster-whisper, which means:

* NVIDIA owners get CUDA acceleration.
* AMD / Intel GPU owners get CPU.

To enable GPU Whisper on AMD / Intel GPUs, compile `pywhispercpp` with
Vulkan at build time. The build script handles this when
`VTE_WHISPER_VULKAN=1` is set, and a runtime probe picks Vulkan
whisper.cpp automatically on AMD/Intel hosts (NVIDIA hosts still get
faster-whisper because CTranslate2 CUDA is ~10-15% faster).

```powershell
# One-time prerequisites:
#   1. Install Vulkan SDK from https://vulkan.lunarg.com/ (sets VULKAN_SDK)
#   2. Install CMake and add it to PATH
#   3. Have MSVC Build Tools (already required for the rest of the build)

$env:VTE_WHISPER_VULKAN = "1"
python scripts/fetch_whisper_ggml.py        # pull the GGML model
powershell -ExecutionPolicy Bypass -File build\windows\build.ps1
```

The build script will:

1. Verify Vulkan SDK + CMake are present (warns and skips if missing).
2. Reinstall pywhispercpp from source with `CMAKE_ARGS=-DGGML_VULKAN=ON`.
3. PyInstaller spec detects the import and bundles the GGML model
   under `models/whisper-ggml/` alongside the existing CT2 model.

Verify after install: look at the user-side log for the line
`ASR backend: whisper.cpp (Vulkan).` — that confirms the runtime
selector picked the Vulkan path. If it instead says `faster-whisper`,
either pywhispercpp didn't get bundled (rebuild) or an NVIDIA GPU is
present and got chosen instead (the intended behaviour).

End users do not need Vulkan SDK — the bundled wheel links against
`vulkan-1.dll` which ships with every modern AMD / Intel / NVIDIA
driver. Users without any GPU driver fall back to faster-whisper CPU.

### llama-cpp-python wheel choice

`pip install -r requirements-gui.txt` will try to install
`llama-cpp-python`. By default pip compiles it from source, which on
Windows requires Visual Studio Build Tools. Avoid that by pointing pip
at one of the prebuilt wheel indexes published at
`https://abetlen.github.io/llama-cpp-python/whl/<backend>/`:

| Backend  | GPU support                  | Notes                                                  |
|----------|------------------------------|--------------------------------------------------------|
| `vulkan` | **AMD + Intel + NVIDIA**     | **Shipping default on Windows.** ~10-15% slower than CUDA on NVIDIA but covers every GPU vendor. |
| `cu124`  | NVIDIA only (CUDA 12.4)      | Fastest on NVIDIA, useless on AMD/Intel. Override `$env:VTE_LLAMA_WHEEL_INDEX=cu124` when shipping to NVIDIA-only customers. |
| `cpu`    | none                         | Smallest bundle, slowest. Use when shipping to a customer who refuses GPU drivers. |
| `metal`  | Apple Silicon (Metal)        | Set automatically by pip on Apple Silicon — no manual flag needed. |

Manual install (skip if `build/windows/build.ps1` runs for you):

```
# Windows shipping default (AMD/Intel/NVIDIA via Vulkan)
pip install llama-cpp-python --prefer-binary \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/vulkan

# NVIDIA-only override
pip install llama-cpp-python --prefer-binary \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124

# Apple Silicon — default install picks Metal automatically
pip install llama-cpp-python
```

Run the matching command BEFORE `pip install -r requirements-gui.txt`
and the requirements file will see the package as already satisfied.

End users do not need any of this — the wheel is bundled by PyInstaller
along with its native `llama.dll` / `libllama.dylib`. Vulkan's runtime
loader (`vulkan-1.dll`) ships with every modern Windows GPU driver
(NVIDIA, AMD, Intel) — if a user has no GPU driver at all,
`llama-cpp-python` silently falls back to CPU and the extractive
summarizer becomes the safety net.

## One-time: pre-seed the embedded models

Up to three model files ship inside each installer, depending on
host:

1. **CTranslate2 Whisper `large-v3`** (~3 GB) — used by
   `faster-whisper`. Shipped in Windows / Intel macOS bundles, and
   kept as the CPU fallback on Apple Silicon.
2. **GGML Whisper `medium-q5_0`** (~540 MB) — used by `whisper.cpp`
   via `pywhispercpp` for Metal-accelerated ASR. Apple Silicon
   **only**. Default because it's the accuracy/speed sweet spot: ~2×
   faster than large-v3-q5_0 on Apple Silicon (≈5 min vs ≈10 min on a
   real 30-min recording) while keeping Ukrainian accuracy well above
   the small model, which hallucinates and repeat-loops. Combined with
   `beam_size=3` + `audio_ctx=768` decoding this is the macOS speed
   path. Pass `--variant large-v3-q5_0` (~1 GB) for maximum accuracy at
   ~2× the runtime, or `--variant small-q5_1` (~190 MB) for the
   lightest/fastest footprint. The runtime resolves whichever variant
   is present (medium-first), overridable via `DESCRIBELY_WHISPER_GGML`.
3. **Qwen 2.5-3B-Instruct GGUF** (~2 GB) — the abstractive
   summarization model used when the user has no Ollama instance
   running. All platforms.

Fetch commands:

```
python scripts/fetch_model.py            # CT2 large-v3 → models/large-v3/
python scripts/fetch_llm.py              # Qwen → models/llm/describely-summary.gguf
python scripts/fetch_whisper_ggml.py     # GGML medium-q5_0 → models/whisper-ggml/  (macOS only)
python scripts/fetch_summary_tokenizer.py  # Qwen2.5 tokenizer.json → models/tokenizer/  (all platforms)
```

`scripts/fetch_summary_tokenizer.py` pulls a ~7 MB `tokenizer.json` used
to size summarizer chunks by token count. If absent the runtime falls
back to a chars/token estimate, so it's optional but recommended.

`scripts/fetch_whisper_ggml.py` is harmless on Windows / Intel hosts
— the file will be present but the PyInstaller spec only bundles it
on Darwin (see `WHISPER_GGML_DIR` in `videote.spec`).

For a smaller dev build, the LLM fetcher accepts `--size 1.5b` or
`--size 0.5b`:

```
python scripts/fetch_llm.py --size 1.5b   # ~1 GB, weaker quality
```

Verify:

```
ls models/large-v3/      # model.bin, tokenizer.json, vocabulary.json, ...
ls models/llm/           # describely-summary.gguf
```

The `models/` directory is gitignored. Each maintainer fetches it
locally; alternatively, keep it in Git LFS or an internal artifact
store.

## One-time: generate icon binaries (optional)

The PySide6 app loads `build/assets/icon.svg` directly via `QIcon`, so
**dev runs need nothing**. PyInstaller and Inno Setup, however, need
platform-native icon formats. To produce them:

```
python scripts/generate_icons.py
```

Outputs:

- `build/assets/app.ico` — Windows (multi-resolution).
- `build/assets/app.icns` — macOS (only generated on macOS, because the
  script shells out to `iconutil`).

If you skip this step, the spec quietly falls back to no icon, and
the build still succeeds.

## Windows build

```
powershell -ExecutionPolicy Bypass -File build\windows\build.ps1
```

Steps performed:

1. Verifies `models/large-v3/`.
2. `pip install -r requirements-gui.txt` + PyInstaller.
3. `pyinstaller build/pyinstaller/videote.spec` →
   `dist/Describely/`.
4. Inno Setup packages the bundle into
   `build/windows/out/Describely-Setup-1.0.0.exe`.

```
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
    "<project-root>\build\windows\installer.iss" `
    "/DProjectRoot=<project-root>"
```

Bundle only (skip installer):

```
powershell -ExecutionPolicy Bypass -File build\windows\build.ps1 -SkipInstaller
```

Outputs:

- `dist\Describely\Describely.exe` — portable bundle.
- `build\windows\out\Describely-Setup-1.0.0.exe` — installer.

The installer:

- Installs per-user under `%LocalAppData%\Describely` (no admin).
- **Shows TERMS.md as the license agreement** — the user must accept
  it to proceed.
- Optionally registers "Create transcription" in the Explorer right-
  click menu for `.mp4 .mkv .mov .avi .webm .m4v .mp3 .wav .m4a .flac`.
- Uninstall is available from Settings → Apps.

## macOS build

We ship **two single-arch installers**, one per Mac architecture:

| Build host           | Command                       | Output                                  |
|----------------------|-------------------------------|-----------------------------------------|
| Apple Silicon (M1+)  | `./build/macos/build.sh`      | `Describely-1.0.0-arm64.pkg`            |
| Intel Mac            | `./build/macos/build.sh`      | `Describely-1.0.0-x86_64.pkg`           |

With no env vars set, the script defaults `VTE_MAC_ARCH` to
`$(uname -m)`, so you run the same command on each Mac. Each `.pkg`
carries a `hostArchitectures` filter so the installer wizard refuses
the wrong-arch download on the user side.

Why two builds and not one fat `.pkg`: in practice the wheels for
`ctranslate2`, `pyav`, `tokenizers`, `llama-cpp-python`, and
`onnxruntime` ship per-arch — `pip` installs only one slice per venv,
so PyInstaller cannot produce a universal2 binary without manual wheel
merging. Two single-arch installers are the reliable shipping path.

The script supports `VTE_MAC_ARCH=universal2` for future use once
upstream wheels catch up; the post-build arch check will currently
reject it.

Single-machine fallback for the x86_64 build (Rosetta on Apple
Silicon): see RELEASE.md §5.2.

Steps the script performs on each run:

1. Sanity-checks host vs target arch and the Python interpreter slice.
2. Verifies `models/large-v3/`.
3. `pip install -r requirements-gui.txt` + PyInstaller.
4. PyInstaller runs with `target_arch=$VTE_MAC_ARCH` → `dist/Describely.app`.
5. Post-build `file -L` check — aborts if the produced binary doesn't
   contain the requested architecture.
6. Ad-hoc `codesign` so Gatekeeper accepts the bundle on the build
   machine (end users still see the first-launch warning unless you
   replace this with a notarized signature — see below).
7. `pkgbuild` wraps the .app + the postinstall script into a
   component .pkg.
8. Renders `Distribution.xml` from `Distribution.xml.in`, substituting
   `@HOST_ARCHITECTURES@` so the installer wizard rejects the
   wrong-arch host.
9. `productbuild` wraps the component into a distribution installer
   with Welcome / License / Conclusion screens at
   `build/macos/out/Describely-1.0.0-${VTE_MAC_ARCH}.pkg`.

App only (skip installer):

```
SKIP_PKG=1 ./build/macos/build.sh
```

What the user sees:

1. Double-clicks the `.pkg` from Downloads.
2. Wizard: Welcome → **Software License Agreement** (must Agree to the
   TERMS) → Install → Finish.
3. The post-install script copies `Describely.app` into
   `/Applications`, marks the TERMS as accepted (so the GUI does not
   re-prompt), and **launches the .app** under the installing user's
   session.
4. On first launch Describely auto-registers two Finder Quick Actions
   ("Create Transcription" and "Create Summary") under
   `~/Library/Services/`. Right-click on a video in Finder → Quick
   Actions → either of them launches the .app with the selected file
   in the queue.

Earlier versions of Describely shipped a DMG with a separate
`Install-QuickAction.command`. That layout is retired — the .pkg flow
delivers a single double-click experience.

### Notarization (optional)

Without an Apple Developer ID, Gatekeeper shows a warning at first
launch (the user has to right-click → Open). If you have a Developer
ID certificate:

```
codesign --deep --options runtime --timestamp \
    --sign "Developer ID Application: NAME (TEAMID)" dist/Describely.app
productsign --sign "Developer ID Installer: NAME (TEAMID)" \
    build/macos/out/Describely-1.0.0.pkg \
    build/macos/out/Describely-1.0.0-signed.pkg
xcrun notarytool submit build/macos/out/Describely-1.0.0-signed.pkg \
    --apple-id you@example.com --team-id TEAMID \
    --password APP_SPECIFIC --wait
xcrun stapler staple build/macos/out/Describely-1.0.0-signed.pkg
```

## Icons

Source: [build/assets/icon.svg](build/assets/icon.svg). After editing
it, re-run `python scripts/generate_icons.py` to refresh `app.ico` and
`app.icns`.

The Qt window icon loads the SVG directly, so dev runs do not need to
regenerate after every tweak.

## Troubleshooting

| Symptom                                                  | Cause                                                                                                                | What to do                                                                                             |
| -------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `Embedded model not found at models/large-v3`            | Did not run `scripts/fetch_model.py`                                                                                 | Run it.                                                                                                |
| PyInstaller warns `WARNING: Hidden import "X" not found` | Dynamic import that the analyzer missed                                                                              | If the runtime breaks, add `X` to `hiddenimports` in `videote.spec`.                                   |
| Windows installer does not register the right-click      | User unchecked the "Context menu" task during install                                                                | Re-run the installer.                                                                                  |
| macOS Quick Action does not appear                       | Either `Install-QuickAction.command` was not run, **or** the `.app` is not located at `/Applications/Describely.app` | Drag the `.app` into `/Applications`, then run the `.command`.                                         |
| `Gatekeeper: app is damaged and can't be opened`         | macOS doesn't accept the ad-hoc signature                                                                            | User runs `xattr -dr com.apple.quarantine /Applications/Describely.app` or you ship a notarized build. |
| Inno Setup compile fails: `LicenseFile not found`        | `TERMS.md` got moved or renamed                                                                                      | Restore `TERMS.md` at the repo root (it is referenced by the installer script).                        |
