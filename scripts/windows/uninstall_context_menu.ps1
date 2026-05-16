<#
.SYNOPSIS
    Removes the "Створити транскрипцію" context menu entries installed by
    install_context_menu.ps1. HKCU only — no admin required.

.DESCRIPTION
    Iterates over the same default extension list as the installer and, if the
    per-extension `shell\CreateTranscription` key exists, deletes it. Running
    this script when nothing is installed is a no-op.

.PARAMETER Extensions
    Optional override for the extension list. By default both video and audio
    lists (matching the installer) are removed.

.EXAMPLE
    PS> .\uninstall_context_menu.ps1
#>
[CmdletBinding()]
param(
    [string[]]$Extensions
)

$ErrorActionPreference = 'Stop'

$KeyName = 'CreateTranscription'

$videoExtensions = @(
    'mp4','mkv','avi','mov','webm','wmv','flv','m4v','mpeg','mpg','ts','3gp'
)
$audioExtensions = @(
    'mp3','wav','flac','m4a','aac','ogg','opus','wma'
)
if (-not $Extensions -or $Extensions.Count -eq 0) {
    $Extensions = $videoExtensions + $audioExtensions
}

Write-Host "Видалення контекстного меню '$KeyName' для поточного користувача" -ForegroundColor Cyan

$removed = 0
$skipped = 0

foreach ($raw in $Extensions) {
    $ext = $raw.Trim().TrimStart('.').ToLowerInvariant()
    if ([string]::IsNullOrEmpty($ext)) { continue }

    $base = "HKCU:\Software\Classes\SystemFileAssociations\.$ext\shell\$KeyName"

    if (Test-Path -LiteralPath $base) {
        Remove-Item -LiteralPath $base -Recurse -Force
        Write-Host ("  [видалено] .{0}" -f $ext)
        $removed++
    }
    else {
        Write-Host ("  [немає]    .{0}" -f $ext) -ForegroundColor DarkGray
        $skipped++
    }
}

Write-Host ""
Write-Host ("Видалено: $removed, вже було відсутнє: $skipped.") -ForegroundColor Green
Write-Host "Перезапустіть Провідник, якщо пункти все ще видно:" -ForegroundColor DarkGray
Write-Host "    Stop-Process -Name explorer -Force" -ForegroundColor DarkGray
