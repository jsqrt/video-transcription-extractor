# Cutting your first Describely release (v1.0.0)

A start-to-finish guide for shipping
`Describely-Setup-1.0.0.exe` (Windows),
`Describely-1.0.0-arm64.pkg` (Apple Silicon macOS), and
`Describely-1.0.0-x86_64.pkg` (Intel macOS) to end users.

You need access to **three machines** for a full release:

* A Windows 10/11 x64 machine (runs identically on Intel and AMD CPUs).
* An **Apple Silicon** macOS 12+ machine (M-series).
* An **Intel** macOS 12+ machine.

PyInstaller cannot truly cross-compile native Python deps between Mac
architectures, so each `.pkg` must be built on a host matching its
target arch. Each `.pkg`'s `Distribution.xml` carries a
`hostArchitectures` filter, so users can't accidentally install the
wrong one.

> **Single-machine fallback** (Apple Silicon only): you can produce
> the x86_64 build on an Apple Silicon Mac with Rosetta + an x86_64
> Python venv — see §5.2 below. The output is identical to building
> on an Intel host. Slower (~2-3× via Rosetta), so a real Intel Mac is
> still the recommended path for shipping.

---

## 0. One-time setup on every build machine

### Windows build host

1. Install **Python 3.11** from python.org. During install: tick "Add
   to PATH" and "Install for all users" (optional).
2. Install [**Inno Setup 6**](https://jrsoftware.org/isinfo.php).
   Accept the default install path so `ISCC.exe` ends up at
   `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`.
3. Open PowerShell in the repo root and create the venv:
   ```powershell
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements-gui.txt
   ```
4. (Optional but recommended) Install Pillow + cairosvg if you want
   nice PNG-backed icons:
   ```powershell
   pip install pillow cairosvg
   ```

### macOS build host (do this on BOTH the arm64 and x86_64 Mac)

1. Install **Python 3.11**. From python.org installer is fine on both
   machines; `brew install python@3.11` also works (it installs an
   arch-matching Python). Verify:
   ```
   python3 -c "import platform; print(platform.machine())"
   ```
   Output must match the host: `arm64` on Apple Silicon, `x86_64` on
   Intel. If the venv reports the wrong arch, you've got a brew that
   was originally installed on a different machine — start over.

2. No extra tools — `pkgbuild` and `productbuild` ship with macOS.

3. Create the venv with the matching Python:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements-gui.txt
   ```

4. (Optional) `pip install pillow cairosvg` for .icns generation.

---

## 1. Pre-seed the embedded models (5-8 GB total per host)

Models live next to the binary inside each installer. Which models you
fetch depends on the host:

| Host                       | Models to pre-seed                                 |
|----------------------------|----------------------------------------------------|
| Windows / Intel macOS      | `large-v3/` (CT2, ~3 GB) + `llm/*.gguf` (~2 GB)    |
| **Apple Silicon macOS**    | `large-v3/` **and** `whisper-ggml/*.bin` (~3 GB each) + `llm/*.gguf` (~2 GB) |

The Apple Silicon build ships **both** the CTranslate2 model and the
GGML model: the GGML feeds `whisper.cpp` (Metal-accelerated path,
default on macOS) and the CTranslate2 model stays as the CPU
fallback for hosts where `pywhispercpp` fails to load.

Fetch commands:

```
python scripts/fetch_model.py            # CTranslate2 large-v3 (all platforms)
python scripts/fetch_llm.py              # Qwen 2.5-3B GGUF (all platforms)
python scripts/fetch_whisper_ggml.py     # GGML large-v3 — Apple Silicon only
```

After all relevant fetches complete:

```
ls models/large-v3/    # config.json, model.bin, tokenizer.json, vocabulary.json, ...
ls models/llm/         # describely-summary.gguf
```

The `models/` directory is gitignored — do not commit it.

If you maintain multiple build machines and want to avoid the bandwidth:
zip the populated `models/` directory once and distribute internally.

---

## 2. Generate platform icons (optional, recommended)

After saving / editing `build/assets/icon.svg`:

```
python scripts/generate_icons.py
```

Produces:
* `build/assets/app.ico`   — Windows (multi-resolution).
* `build/assets/app.icns`  — macOS (only on a macOS host).

Skipping this step is safe — the build will fall back to the default
Python icon, just uglier.

Commit the regenerated `app.ico` / `app.icns` to git so other
maintainers don't need cairosvg.

---

## 3. Sanity-check before building

On **both** hosts, from the repo root:

```
python -m unittest discover -s tests --buffer
```

Expect: `Ran 105 tests in ~20s OK`.

If anything is red, fix it before continuing — do not ship a broken
release.

Quick GUI smoke test (does not transcribe anything, just opens the
window):

```
python -m app.gui
```

Click around, drop a tiny video, cancel mid-progress, close. Expect
no crashes.

---

## 4. Build the Windows installer

From PowerShell in the repo root, with the venv activated:

```powershell
powershell -ExecutionPolicy Bypass -File build\windows\build.ps1
```

The script:
1. Verifies `models/large-v3/` exists.
2. Installs / updates build deps (`pyinstaller`, `imageio-ffmpeg`,
   PySide6).
3. Runs PyInstaller → `dist\Describely\` (bundle, ~5 GB).
4. Runs Inno Setup → `build\windows\out\Describely-Setup-1.0.0.exe`
   (installer, ~2.5 GB compressed).

Expected runtime: 10–25 minutes on a modern laptop.

### Verify the Windows build before shipping

On a **clean** Windows test machine (or a fresh local user account):

1. Copy the installer over.
2. Double-click. SmartScreen may warn; click "More info" → "Run anyway".
3. The Terms of Use page appears — confirm the file is readable and
   the "I accept the agreement" radio works.
4. Click through, keep the "Add Create transcription to right-click"
   task checked.
5. Open File Explorer, right-click any `.mp4` → confirm "Create
   transcription" appears.
6. Click it → Describely window opens with the file already queued.
   The TERMS modal appears on first launch — click "Accept and
   Continue".
7. Let it process. Verify `<name>.clean.md` and `<name>.summary.md`
   land next to the video.
8. Settings → Apps → Describely → Uninstall — confirm clean removal,
   confirm the right-click menu entry disappears, confirm
   `%LocalAppData%\Describely` is removed.

If anything is off, fix and rebuild before shipping.

---

## 5. Build the macOS installers

You run the same script twice — once per Mac. The script reads
`VTE_MAC_ARCH`; if you leave it unset, it defaults to the host arch
(`uname -m`), so on each machine you can just run:

```
./build/macos/build.sh
```

…and get the correct `.pkg` for that host:

| Host                 | Output                                  | hostArchitectures filter |
|----------------------|------------------------------------------|--------------------------|
| Apple Silicon (M1+)  | `build/macos/out/Describely-1.0.0-arm64.pkg`  | `arm64` |
| Intel                | `build/macos/out/Describely-1.0.0-x86_64.pkg` | `x86_64` |

Each `.pkg` refuses installation on the wrong architecture (the
installer wizard shows "Describely can't be installed on this
computer."). This is intentional — a Rosetta-emulated Whisper on
Apple Silicon would be 3-5× slower than the native arm64 path.

What the script does on each run:

1. Reads `uname -m`, sets `VTE_MAC_ARCH` if you didn't.
2. Sanity-checks host vs target combo (refuses arm64 build on Intel,
   warns about x86_64 build on Apple Silicon — see §5.2).
3. Verifies the Python interpreter contains the target slice using
   `file -L`.
4. Verifies `models/large-v3/` exists.
5. Installs / updates build deps.
6. Runs PyInstaller with `target_arch=$VTE_MAC_ARCH` →
   `dist/Describely.app`.
7. **Post-build arch check** — `file` against the main binary; aborts
   if the produced bundle lacks the requested arch.
8. Ad-hoc codesigns the .app.
9. `pkgbuild` → component package.
10. Renders `Distribution.xml` from `Distribution.xml.in` with
    `@HOST_ARCHITECTURES@` filled in.
11. `productbuild` → final `.pkg`.

Expected runtime per build: 10–20 minutes.

### 5.1 Verify each .pkg on a clean machine of the same arch

For the arm64 build: install on a second Apple Silicon Mac (or fresh
user account). For the x86_64 build: install on a second Intel Mac.
Then walk through the §5.3 checklist below.

To prove the arch filter works, also try installing the
**wrong-arch** `.pkg` on each Mac — the installer should refuse with a
clean error message before reaching the Welcome page. If it doesn't,
the `hostArchitectures` substitution in `Distribution.xml.in` is
broken; check the rendered XML emitted by the build (look at the
script's tempdir output).

### 5.2 Building x86_64 on Apple Silicon (Rosetta path)

If your Intel Mac is unavailable, you can still ship the x86_64 `.pkg`
from an Apple Silicon Mac. The bundle is binary-identical to a
native-Intel-built one; the only cost is build time (~2-3× slower via
Rosetta).

```
# One-time, on Apple Silicon:
softwareupdate --install-rosetta --agree-to-license

# Per build, in a fresh terminal:
arch -x86_64 /bin/zsh
# IMPORTANT: install Python from python.org using the x86_64 installer
# (or use an existing x86_64 brew at /usr/local/bin/python3 — NOT the
# arm64 brew at /opt/homebrew/bin/python3).
/usr/local/bin/python3 -m venv .venv-x86_64
source .venv-x86_64/bin/activate
python -c "import platform; print(platform.machine())"   # must print x86_64
pip install -r requirements-gui.txt

VTE_MAC_ARCH=x86_64 ./build/macos/build.sh
```

The script's Python arch probe will yell if you accidentally used the
arm64 Python under Rosetta — Rosetta doesn't change the Python binary
itself, only the shell.

### 5.3 Install-time verification checklist

Run this on a Mac matching the arch of the `.pkg` you're testing —
once for arm64, once for x86_64.

1. Copy the `.pkg` over to `~/Downloads/`.
2. If Gatekeeper says "damaged", clear the quarantine bit (substitute
   the right filename for the arch you're testing):
   ```
   xattr -dr com.apple.quarantine ~/Downloads/Describely-1.0.0-arm64.pkg
   ```
3. Double-click the `.pkg`. Walk through the installer:
   * **Welcome** screen — reads OK.
   * **Software License Agreement** — TERMS appear; click **Agree**.
     The Install button only enables after Agree.
   * **Install** — enter your Mac password if prompted. Wait ~10
     seconds while the .app copies to `/Applications`.
   * **Finish** — verify Describely auto-launches at this point.
4. Inside Describely, confirm the TERMS modal does **not** appear (the
   installer already recorded the acceptance).
5. Quit Describely.
6. In Finder, right-click any video → **Quick Actions** → expect to
   see both **Create Transcription** and **Create Summary**.
   * If they don't show: System Settings → Privacy & Security →
     Extensions → Finder → enable them. (Sometimes a Finder relaunch
     is needed: `killall Finder`.)
7. Click "Create Summary". A Describely window opens with the file in
   the queue. Watch it process. Verify `<name>.summary.md` (and only
   that, not `.clean.md`) appears next to the video.
8. Repeat with "Create Transcription" → expect only `<name>.clean.md`.
9. Test cancellation: queue a long video, click Cancel on its row,
   confirm status becomes "Cancelled" within a few seconds.

To uninstall and test cleanup:
```
rm -rf /Applications/Describely.app
rm -rf "$HOME/Library/Services/Describely Create Transcription.workflow"
rm -rf "$HOME/Library/Services/Describely Create Summary.workflow"
rm -rf "$HOME/Library/Application Support/Describely"
```

---

## 6. Tag the release in git

Once both installers pass verification:

```
git tag -a v1.0.0 -m "Describely v1.0.0"
git push origin v1.0.0
```

If you are pushing to GitHub and want a release page:

```
gh release create v1.0.0 \
    "build/windows/out/Describely-Setup-1.0.0.exe" \
    "build/macos/out/Describely-1.0.0.pkg" \
    --title "Describely v1.0.0" \
    --notes-file RELEASE_NOTES.md
```

(Create `RELEASE_NOTES.md` yourself with the changelog highlights.
Keep it terse — bullet list of features, known issues, system
requirements link.)

---

## 7. Post-release smoke test on real user machines

Recruit one Windows user and one macOS user from outside the build
team. Watch them install and run the app over a screen share. Track:

* SmartScreen friction on Windows (severity, how clear is "Run anyway").
* Gatekeeper friction on macOS (do they figure out right-click → Open).
* Whether right-click integration is discoverable.
* Whether the TERMS modal is read or instantly clicked through (the
  latter is fine — it's a legal speedbump, not a tutorial).
* Time-to-first-transcript end-to-end. Aim for < 2 minutes from
  installer click to first `.clean.md` for a 1-minute video.

Capture whatever broke and put it in a `v1.0.1` milestone.

---

## 8. Known limitations to communicate up front

Add these to the release notes verbatim so users are not surprised:

* **No code signing yet.** Windows SmartScreen and macOS Gatekeeper
  will warn at first launch. The TERMS file explains the publisher;
  users have to actively choose to trust it.
* **First launch is slow** — Whisper has to load ~3 GB of weights
  into memory once. Subsequent files in the same session reuse the
  loaded model.
* **macOS Whisper acceleration.** Apple Silicon uses `whisper.cpp`
  with the Metal backend (~2-4× real-time on M-series). Intel Mac
  uses `whisper.cpp` with the Accelerate framework on CPU (~0.5-1×
  real-time). If `pywhispercpp` fails to import for any reason, the
  build falls back to `faster-whisper`/CTranslate2 on CPU. LLM summary
  always uses Metal on Apple Silicon.
* **Windows GPU coverage (summary):** the bundled `llama-cpp-python`
  uses the **Vulkan** backend by default — summary generation runs on
  any modern AMD, Intel, or NVIDIA GPU with up-to-date drivers. No
  user-side install needed. Hosts with no GPU driver at all fall back
  to CPU silently.
* **Windows GPU coverage (Whisper / transcription):** depends on
  which Windows build you ship.
  * **Default build** — faster-whisper. NVIDIA gets CUDA; AMD / Intel
    GPU owners get CPU.
  * **Build with `$env:VTE_WHISPER_VULKAN=1`** — also includes a
    Vulkan-compiled `pywhispercpp`. Runtime probe: NVIDIA still goes
    to faster-whisper CUDA (faster), AMD / Intel GPU owners get
    whisper.cpp Vulkan (GPU-accelerated, ~3-5× CPU). Requires Vulkan
    SDK + CMake on the build machine — see BUILD.md.
* **Ollama summary is opt-in.** If the user has not installed Ollama
  separately, summaries are generated by the offline extractive
  summarizer (less fluent, but factual).
* **English / Ukrainian summaries** are best (the LLM prompts and the
  offline summarizer's filler / stopword lists are tuned for them).
  Other languages produce transcripts fine but summaries may read
  poorly.

---

## 9. What to do for v1.0.1+ (deferred from v1.0)

Open issues for each item so they aren't lost:

* **Code-signing certificates.** ~$200/year EV cert for Windows
  (kills the SmartScreen warning). $99/year Apple Developer ID for
  macOS notarization (kills the Gatekeeper warning).
* **Bundled ffmpeg auto-update flow.** Right now `imageio-ffmpeg`
  pins one ffmpeg version per `imageio-ffmpeg` release. Bump
  `requirements-gui.txt` whenever upstream patches a CVE.
* **Model integrity check.** Add a `models/large-v3/SHA256SUMS` file,
  verified on first launch (defense-in-depth against tampered
  installer).
* ~~AMD GPU Whisper on Windows.~~ Shipped in v1.0 — opt-in via
  `$env:VTE_WHISPER_VULKAN=1` at build time. Compiles pywhispercpp
  from source with `GGML_VULKAN=ON`. Runtime probe at
  [app/gui/gpu_detect.py](app/gui/gpu_detect.py) decides which path to
  use per host.
* ~~AMD GPU summary on Windows.~~ Shipped in v1.0 — `build/windows/build.ps1`
  pulls llama-cpp-python from the Vulkan wheel index by default.
* **Auto-update channel.** Probably overkill for a desktop tool that
  is launched on-demand; revisit if user feedback asks for it.

---

That's the whole loop. Welcome to the maintenance phase.
