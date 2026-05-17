# Building Describely installers

This document is for **maintainers** who produce releases. End users
only need the resulting `.exe` (Windows) or `.dmg` (macOS).

## One-time prerequisites

* Python 3.10+ (3.11 recommended).
* `ffmpeg` on `PATH` (used on the build machine; the user bundle ships
  its own audio decoder via PyAV, so end users do not need ffmpeg).
* A configured `.venv`:
  ```
  python -m venv .venv
  # Windows:
  .venv\Scripts\activate
  # macOS / Linux:
  source .venv/bin/activate
  pip install -r requirements-gui.txt
  ```

### Platform-specific

* **Windows:** [Inno Setup 6](https://jrsoftware.org/isinfo.php). The
  default `ISCC.exe` path is `C:\Program Files (x86)\Inno Setup 6\ISCC.exe`;
  override with `-IsccPath` in `build.ps1` if installed elsewhere.
* **macOS:** `brew install create-dmg`. Used to assemble the DMG with
  the `.app`, the Quick Action installer, and the Terms of Use.

### Optional, for nicer icons

```
pip install pillow cairosvg
```

Used by `scripts/generate_icons.py` to render `build/assets/icon.svg`
into platform binaries.

## One-time: pre-seed the embedded model

The big `large-v3` (~3 GB) ships inside every installer. Pull it into
`models/large-v3/` once before the first build:

```
python scripts/fetch_model.py
```

Verify:

```
ls models/large-v3/      # model.bin, tokenizer.json, vocabulary.json, ...
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
* `build/assets/app.ico`  — Windows (multi-resolution).
* `build/assets/app.icns` — macOS (only generated on macOS, because the
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

Bundle only (skip installer):

```
powershell -ExecutionPolicy Bypass -File build\windows\build.ps1 -SkipInstaller
```

Outputs:
* `dist\Describely\Describely.exe` — portable bundle.
* `build\windows\out\Describely-Setup-1.0.0.exe` — installer.

The installer:
* Installs per-user under `%LocalAppData%\Describely` (no admin).
* **Shows TERMS.md as the license agreement** — the user must accept
  it to proceed.
* Optionally registers "Create transcription" in the Explorer right-
  click menu for `.mp4 .mkv .mov .avi .webm .m4v .mp3 .wav .m4a .flac`.
* Uninstall is available from Settings → Apps.

## macOS build

```
./build/macos/build.sh
```

Steps performed:
1. Verifies `models/large-v3/`.
2. `pip install -r requirements-gui.txt` + PyInstaller.
3. PyInstaller produces `dist/Describely.app`.
4. Ad-hoc `codesign` so Gatekeeper accepts it on the build machine
   (end users still see the first-launch warning unless you replace
   this with a notarized signature — see below).
5. `create-dmg` packages the `.app`, `Install-QuickAction.command`,
   the workflow bundle, and `TERMS.md` into
   `build/macos/out/Describely-1.0.0.dmg`.

App only:

```
SKIP_DMG=1 ./build/macos/build.sh
```

What the user sees:
1. Opens the `.dmg`.
2. Drags `Describely.app` into `Applications`.
3. Reads `TERMS.md` (also shipped on the DMG).
4. Double-clicks `Install-QuickAction.command` → the Quick Action
   "Create Transcription" is registered in `~/Library/Services/`.

After that, right-click on a video in Finder → Quick Actions →
"Create Transcription" launches the `.app` with the selected files
(via `open -a`).

### Notarization (optional)

Without an Apple Developer ID, Gatekeeper shows a warning at first
launch (the user has to right-click → Open). If you have a Developer
ID certificate:

```
codesign --deep --options runtime --sign "Developer ID Application: NAME (TEAMID)" \
    dist/Describely.app
xcrun notarytool submit dist/Describely-1.0.0.dmg \
    --apple-id you@example.com --team-id TEAMID --password APP_SPECIFIC --wait
xcrun stapler staple dist/Describely-1.0.0.dmg
```

## Icons

Source: [build/assets/icon.svg](build/assets/icon.svg). After editing
it, re-run `python scripts/generate_icons.py` to refresh `app.ico` and
`app.icns`.

The Qt window icon loads the SVG directly, so dev runs do not need to
regenerate after every tweak.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `Embedded model not found at models/large-v3` | Did not run `scripts/fetch_model.py` | Run it. |
| PyInstaller warns `WARNING: Hidden import "X" not found` | Dynamic import that the analyzer missed | If the runtime breaks, add `X` to `hiddenimports` in `videote.spec`. |
| Windows installer does not register the right-click | User unchecked the "Context menu" task during install | Re-run the installer. |
| macOS Quick Action does not appear | Either `Install-QuickAction.command` was not run, **or** the `.app` is not located at `/Applications/Describely.app` | Drag the `.app` into `/Applications`, then run the `.command`. |
| `Gatekeeper: app is damaged and can't be opened` | macOS doesn't accept the ad-hoc signature | User runs `xattr -dr com.apple.quarantine /Applications/Describely.app` or you ship a notarized build. |
| Inno Setup compile fails: `LicenseFile not found` | `TERMS.md` got moved or renamed | Restore `TERMS.md` at the repo root (it is referenced by the installer script). |
