<#
.SYNOPSIS
    Registers the "Створити транскрипцію" context menu item for the current
    user. Works for a curated list of video and audio extensions.

.DESCRIPTION
    Writes, per extension, the following key under HKCU (no admin required):

        HKCU:\Software\Classes\SystemFileAssociations\.<ext>\shell\CreateTranscription
            (Default)       = "Створити транскрипцію"
            MUIVerb         = "Створити транскрипцію"
            Icon            = <optional, if icon file is present>

        HKCU:\Software\Classes\SystemFileAssociations\.<ext>\shell\CreateTranscription\command
            (Default)       = powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<worker>" -FilePath "%1"

    The script is idempotent: re-running it simply rewrites the keys with the
    current values. No stale entries remain.

.PARAMETER MenuLabel
    Visible label in the Explorer context menu. Defaults to the Ukrainian
    string "Створити транскрипцію".

.PARAMETER Extensions
    Optional override for the list of extensions to register. By default both
    the video and audio lists below are used.

.PARAMETER PowerShellExe
    Path to powershell.exe to use in the command. Defaults to the one currently
    executing the installer, which is almost always what you want.

.EXAMPLE
    PS> .\install_context_menu.ps1

.EXAMPLE
    PS> .\install_context_menu.ps1 -MenuLabel "Create transcription"

.EXAMPLE
    PS> .\install_context_menu.ps1 -Extensions @('mp4','mkv','mp3')
#>
[CmdletBinding()]
param(
    [string]$MenuLabel = 'Створити транскрипцію',
    [string[]]$Extensions,
    [string]$PowerShellExe = (Join-Path $PSHOME 'powershell.exe')
)

$ErrorActionPreference = 'Stop'

# Canonical key name (ASCII, matches uninstall script).
$KeyName = 'CreateTranscription'

# Default extension sets. Keep lowercase, without the leading dot.
$videoExtensions = @(
    'mp4','mkv','avi','mov','webm','wmv','flv','m4v','mpeg','mpg','ts','3gp'
)
$audioExtensions = @(
    'mp3','wav','flac','m4a','aac','ogg','opus','wma'
)
if (-not $Extensions -or $Extensions.Count -eq 0) {
    $Extensions = $videoExtensions + $audioExtensions
}

# Resolve paths. The worker script sits next to this one.
$scriptRoot  = Split-Path -Parent $PSCommandPath
$worker      = Join-Path $scriptRoot 'Invoke-CreateTranscription.ps1'
$projectRoot = Split-Path -Parent (Split-Path -Parent $scriptRoot)

if (-not (Test-Path -LiteralPath $worker -PathType Leaf)) {
    throw "Не знайдено worker-скрипт: $worker"
}

# Optional icon discovery: first existing file wins. Supports .ico or .exe,ID.
$iconCandidates = @(
    (Join-Path $projectRoot 'scripts\windows\app.ico'),
    (Join-Path $projectRoot 'scripts\windows\icon.ico'),
    (Join-Path $projectRoot 'app\resources\app.ico'),
    (Join-Path $projectRoot 'app.ico')
)
$iconPath = $null
foreach ($candidate in $iconCandidates) {
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { $iconPath = $candidate; break }
}

# Command line with proper quoting. %1 is substituted by Explorer.
$commandLine = '"{0}" -NoProfile -ExecutionPolicy Bypass -File "{1}" -FilePath "%1"' -f $PowerShellExe, $worker

function Register-ContextMenuForExtension {
    param(
        [Parameter(Mandatory)] [string]$Extension,
        [Parameter(Mandatory)] [string]$Label,
        [Parameter(Mandatory)] [string]$Command,
        [string]$Icon
    )

    $ext = $Extension.Trim().TrimStart('.').ToLowerInvariant()
    if ([string]::IsNullOrEmpty($ext)) { return }

    $base    = "HKCU:\Software\Classes\SystemFileAssociations\.$ext\shell\$KeyName"
    $cmdKey  = "$base\command"

    # Idempotency: wipe any previous entry so the new values are authoritative.
    if (Test-Path -LiteralPath $base) {
        Remove-Item -LiteralPath $base -Recurse -Force
    }

    New-Item -Path $base   -Force | Out-Null
    New-Item -Path $cmdKey -Force | Out-Null

    Set-ItemProperty -LiteralPath $base -Name '(Default)' -Value $Label
    Set-ItemProperty -LiteralPath $base -Name 'MUIVerb'   -Value $Label
    if ($Icon) {
        Set-ItemProperty -LiteralPath $base -Name 'Icon' -Value $Icon
    }
    Set-ItemProperty -LiteralPath $cmdKey -Name '(Default)' -Value $Command

    Write-Host ("  .{0,-6} → HKCU\...\SystemFileAssociations\.{0}\shell\{1}" -f $ext, $KeyName)
}

Write-Host "Встановлення контекстного меню '$MenuLabel' для поточного користувача" -ForegroundColor Cyan
Write-Host "Worker:   $worker"
Write-Host "Command:  $commandLine"
if ($iconPath) { Write-Host "Icon:     $iconPath" }
Write-Host ""

$registered = 0
foreach ($ext in $Extensions) {
    Register-ContextMenuForExtension `
        -Extension $ext `
        -Label     $MenuLabel `
        -Command   $commandLine `
        -Icon      $iconPath
    $registered++
}

Write-Host ""
Write-Host "Готово. Зареєстровано розширень: $registered." -ForegroundColor Green
Write-Host "Якщо нові пункти не з'являються у Провіднику — зробіть Restart-Explorer:" -ForegroundColor DarkGray
Write-Host "    Stop-Process -Name explorer -Force" -ForegroundColor DarkGray
