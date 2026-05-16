<#
.SYNOPSIS
    Worker script invoked from the Windows context menu. Runs the project CLI
    against a single media file.

.DESCRIPTION
    Registered in HKCU via install_context_menu.ps1. Not meant to be called
    directly by a human — but it is safe to do so.

    The script resolves the project root (two levels above itself) and, in
    preference order:
        1. uses .venv\Scripts\python.exe if it exists;
        2. falls back to the first `python` on PATH.

    The CLI is invoked as:
        python -m app transcribe --input "<file>" --ext <ext> --progress

    The extension is passed explicitly because the scanner defaults to
    video-only extensions, but this shortcut also fires for audio files.

.PARAMETER FilePath
    Absolute path to the media file. Comes from "%1" in the shell command.

.NOTES
    Keep the console window open on exit so the user can read the output and
    copy the resulting paths before pressing Enter.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$FilePath
)

$ErrorActionPreference = 'Stop'

function Write-Section {
    param([string]$Text)
    Write-Host ""
    Write-Host "=== $Text ===" -ForegroundColor Cyan
}

function Exit-WithPause {
    param([int]$Code = 0)
    Write-Host ""
    Write-Host "Натисніть Enter щоб закрити це вікно..." -ForegroundColor DarkGray
    try { [void][System.Console]::ReadLine() } catch { Start-Sleep -Seconds 5 }
    exit $Code
}

try {
    $scriptRoot = Split-Path -Parent $PSCommandPath
    $projectRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)

    Write-Section "Створення транскрипції"
    Write-Host "Файл:         $FilePath"
    Write-Host "Проект:       $projectRoot"

    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
        Write-Host "Помилка: файл не знайдено." -ForegroundColor Red
        Exit-WithPause -Code 2
    }

    $ext = [System.IO.Path]::GetExtension($FilePath).TrimStart('.').ToLowerInvariant()
    if ([string]::IsNullOrWhiteSpace($ext)) {
        Write-Host "Помилка: у файла немає розширення, не можу визначити формат." -ForegroundColor Red
        Exit-WithPause -Code 2
    }

    $venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        $python = $venvPython
        Write-Host "Python:       $python (venv)"
    }
    else {
        $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $pythonCmd) {
            Write-Host "Помилка: не знайдено ані .venv\Scripts\python.exe, ані 'python' у PATH." -ForegroundColor Red
            Exit-WithPause -Code 3
        }
        $python = $pythonCmd.Source
        Write-Host "Python:       $python (PATH)"
    }

    Push-Location $projectRoot
    try {
        Write-Section "Запуск pipeline"
        & $python -m app transcribe --input "$FilePath" --ext $ext --progress
        $code = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    Write-Section "Готово"
    if ($code -eq 0) {
        Write-Host "Транскрипт збережено поруч з відео/аудіо (той самий каталог)." -ForegroundColor Green
    }
    else {
        Write-Host "Pipeline завершився з кодом $code." -ForegroundColor Yellow
    }
    Exit-WithPause -Code $code
}
catch {
    Write-Host ""
    Write-Host "Помилка: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.ScriptStackTrace) {
        Write-Host $_.ScriptStackTrace -ForegroundColor DarkRed
    }
    Exit-WithPause -Code 1
}
