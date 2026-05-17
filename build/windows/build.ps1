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
& $python -m pip install --upgrade pip
& $python -m pip install -r (Join-Path $ProjectRoot "requirements-gui.txt")

$dist = Join-Path $ProjectRoot "dist"
$buildDir = Join-Path $ProjectRoot "build\pyinstaller-work"
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }

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
& $IsccPath (Join-Path $ProjectRoot "build\windows\installer.iss") "/DProjectRoot=$ProjectRoot"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed (exit $LASTEXITCODE)" }

Write-Host "==> Installer produced under: $(Join-Path $ProjectRoot 'build\windows\out')"
