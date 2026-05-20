<#
.SYNOPSIS
    End-to-end Windows build: PyInstaller bundle + Inno Setup installer.

.DESCRIPTION
    Run from a developer prompt with the project venv activated:
        powershell -ExecutionPolicy Bypass -File build\windows\build.ps1

    Steps:
      1. Verify the embedded model is present.
      2. Install requirements-gui.txt + pyinstaller into the active venv.
      3. Run PyInstaller against build/pyinstaller/videote.spec.
      4. Run Inno Setup (iscc) against build/windows/installer.iss.

    Requires:
      * Python 3.10+ on PATH (or via .venv).
      * Inno Setup 6 installed; iscc.exe accessible.
#>

[CmdletBinding()]
param(
    [switch]$SkipInstaller,
    [string]$IsccPath = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $ProjectRoot
$env:VTE_PROJECT_ROOT = $ProjectRoot

Write-Host "==> Project root: $ProjectRoot"

$modelDir = Join-Path $ProjectRoot "models\large-v3"

# Ensure MSVC developer environment is available for building native extensions
# If tools like `nmake` / `cl.exe` are not on PATH and we haven't already
# re-invoked the script inside a dev environment, try to locate Visual
# Studio via vswhere and re-run this script inside the developer command
# environment so that CMake/pip can find compilers.
if (-not (Get-Command nmake -ErrorAction SilentlyContinue) -and -not $env:VTE_DEVENV) {
    Write-Host "==> MSVC tools not detected in PATH. Attempting to locate Visual Studio (vswhere)."
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path $vswhere) {
        $instPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath 2>$null
        if ($instPath) {
            $vsDevCmd = Join-Path $instPath "Common7\Tools\VsDevCmd.bat"
            if (-not (Test-Path $vsDevCmd)) { $vsDevCmd = Join-Path $instPath "VC\Auxiliary\Build\vcvarsall.bat" }
            if (Test-Path $vsDevCmd) {
                Write-Host "==> Found Visual Studio at $instPath. Re-running script inside developer environment..."
                # Build argument list to re-invoke this script with the same params
                $argList = @()
                if ($SkipInstaller) { $argList += "-SkipInstaller" }
                if ($IsccPath) { $argList += "-IsccPath '$IsccPath'" }
                $scriptPath = $PSCommandPath
                $joinedArgs = $argList -join ' '
                # Run a new cmd.exe which sources the VS dev batch and then launches PowerShell.
                # Build a PowerShell command that sets an env var and invokes this script.
                $innerPS = '$env:VTE_DEVENV=1; & ''{0}'' {1}' -f $scriptPath, $joinedArgs
                # Build the cmd.exe command string; use call to run the batch file so environment is set.
                $callCmd = 'call "{0}" x64 && powershell -NoProfile -ExecutionPolicy Bypass -Command "{1}"' -f $vsDevCmd, $innerPS
                Write-Host "==> Executing: cmd.exe /c $callCmd"
                & cmd.exe /c $callCmd
                $exitCode = $LASTEXITCODE
                exit $exitCode
            }
        }
    }
    Write-Warning "MSVC build tools not found or could not be activated automatically.\nRun this script from an 'x64 Native Tools Command Prompt for VS' or install Visual Studio Build Tools."
}
if (-not (Test-Path $modelDir)) {
    throw "Embedded model not found at $modelDir. Run: python scripts\fetch_model.py"
}

# Pick venv python if available, else fall back to PATH.
$venvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$python = if (Test-Path $venvPython) { $venvPython } else { (Get-Command python).Source }
Write-Host "==> Using Python: $python"

Write-Host "==> Installing build dependencies"
# Do NOT upgrade PyInstaller here — the requirements-gui.txt pins a
# known-good version (bootloader CVEs in the past). Let the requirements
# file decide.
#
# llama-cpp-python wheel selection: use the **Vulkan** index so summary
# generation can hit AMD, Intel, AND NVIDIA GPUs on Windows. Vulkan is
# slightly slower than CUDA on NVIDIA (~10-15%) but covers every GPU
# vendor in one wheel, which matches the project's "no compromises"
# GPU posture. Users without Vulkan-capable drivers fall through to CPU
# silently; the GPU-less extractive summarizer is still available as a
# last resort.
#
# Override via $env:VTE_LLAMA_WHEEL_INDEX before invoking the script:
#   cpu      — pure CPU, smallest bundle, no GPU acceleration
#   vulkan   — cross-vendor GPU (default; recommended)
#   cu124    — NVIDIA-only CUDA 12.4 (faster on NVIDIA, useless on AMD)
$LlamaIndex = if ($env:VTE_LLAMA_WHEEL_INDEX) { $env:VTE_LLAMA_WHEEL_INDEX } else { "vulkan" }
$LlamaIndexUrl = "https://abetlen.github.io/llama-cpp-python/whl/$LlamaIndex"
Write-Host "==> llama-cpp-python wheel index: $LlamaIndexUrl"
& $python -m pip install --upgrade pip
& $python -m pip install --prefer-binary --extra-index-url $LlamaIndexUrl -r (Join-Path $ProjectRoot "requirements-gui.txt")

# Optional: compile pywhispercpp from source with Vulkan enabled so the
# bundle can drive AMD / Intel / NVIDIA GPUs for transcription. The
# default install (above) doesn't touch pywhispercpp on Windows
# because the wheel is platform-gated to macOS. Set $env:VTE_WHISPER_VULKAN=1
# to opt in. Requires:
#   * Vulkan SDK (https://vulkan.lunarg.com/) installed; the build
#     looks for the VULKAN_SDK env var.
#   * CMake on PATH.
#   * MSVC Build Tools (already required for the rest of the build).
#
# Without these, the build proceeds without GPU Whisper and the runtime
# falls back to faster-whisper (CUDA on NVIDIA, CPU otherwise).
if ($env:VTE_WHISPER_VULKAN -eq "1") {
    if (-not $env:VULKAN_SDK) {
        Write-Warning "VTE_WHISPER_VULKAN=1 set but VULKAN_SDK is not defined."
        Write-Warning "Install the Vulkan SDK from https://vulkan.lunarg.com/ first."
        Write-Warning "Skipping pywhispercpp Vulkan build."
    } elseif (-not (Get-Command cmake -ErrorAction SilentlyContinue)) {
        Write-Warning "VTE_WHISPER_VULKAN=1 set but cmake is not on PATH."
        Write-Warning "Skipping pywhispercpp Vulkan build."
    } else {
        Write-Host "==> Building pywhispercpp from source with Vulkan (CMAKE_ARGS=-DGGML_VULKAN=ON)"
        Write-Host "==> Vulkan SDK: $env:VULKAN_SDK"
        $env:CMAKE_ARGS = "-DGGML_VULKAN=ON"
        & $python -m pip install --no-binary pywhispercpp --force-reinstall --no-cache-dir "pywhispercpp>=1.3,<2.0"
        if ($LASTEXITCODE -ne 0) {
            throw "pywhispercpp Vulkan build failed (exit $LASTEXITCODE). Inspect cmake's error output above."
        }
        Remove-Item Env:CMAKE_ARGS
    }
}

$dist = Join-Path $ProjectRoot "dist"
$buildDir = Join-Path $ProjectRoot "build\pyinstaller-work"
if (Test-Path $dist) {
    $cleaned = $false
    try {
        Remove-Item -Recurse -Force -ErrorAction Stop $dist
        $cleaned = $true
    } catch {
        Write-Warning "==> dist could not be removed: $($_.Exception.Message)"
        try {
            $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
            $stash = "$dist.old-$stamp"
            Rename-Item -LiteralPath $dist -NewName (Split-Path $stash -Leaf) -ErrorAction Stop
            Write-Host "==> Renamed locked dist to $stash."
            $cleaned = $true
        } catch {
            Write-Warning "==> dist could not be renamed either: $($_.Exception.Message)"
        }
    }
    if (-not $cleaned) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $dist = Join-Path $ProjectRoot ("dist-" + $stamp)
        Write-Warning "==> Falling back to fresh dist path: $dist"
    }
}
if (Test-Path $buildDir) {
    try { Remove-Item -Recurse -Force -ErrorAction Stop $buildDir } catch {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $buildDir = Join-Path $ProjectRoot ("build\pyinstaller-work-" + $stamp)
        Write-Warning "==> build dir locked; using fresh $buildDir"
    }
}

Write-Host "==> Running PyInstaller"
& $python -m PyInstaller `
    (Join-Path $ProjectRoot "build\pyinstaller\videote.spec") `
    --noconfirm `
    --workpath $buildDir `
    --distpath $dist
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

$bundle = Join-Path $dist "Describely"
if (-not (Test-Path $bundle)) {
    throw "Expected bundle directory not found: $bundle"
}
Write-Host "==> Bundle: $bundle"

if ($SkipInstaller) {
    Write-Host "==> --SkipInstaller set; done."
    exit 0
}

if (-not (Test-Path $IsccPath)) {
    Write-Warning "Inno Setup not found at '$IsccPath'. Set -IsccPath or install Inno Setup 6."
    Write-Host "==> Bundle is ready at $bundle; skipping installer step."
    exit 0
}

Write-Host "==> Building Inno Setup installer"
& $IsccPath (Join-Path $ProjectRoot "build\windows\installer.iss") "/DProjectRoot=$ProjectRoot" "/DBundleDir=$bundle"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit $LASTEXITCODE)" }

Write-Host "==> Installer produced under: $(Join-Path $ProjectRoot 'build\windows\out')"
