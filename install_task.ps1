<#
    Installs the auto-updater and registers its hourly Scheduled Task.

    "Install" = copy the runtime files OUT of this (disposable) repo clone into a
    permanent folder that sits NEXT TO your zapret folder, then point the task at
    that permanent copy. After this runs, the cloned repo can be deleted.

    Target folder (default): a sibling of install_path from config.json, named
    "<zapret-folder>-updater".  e.g.  C:\Users\You\Documents\zapret
                                  ->  C:\Users\You\Documents\zapret-updater
    Override with -InstallDir "D:\somewhere\zapret-updater".

    Run ONCE, from an elevated PowerShell (Windows PowerShell 5.1 or 7 both work):
        powershell -ExecutionPolicy Bypass -File .\install_task.ps1

    The task runs at logon and every hour, in your interactive session with
    highest privileges (so winws.exe can load WinDivert without a UAC prompt),
    via pythonw.exe (no console window).

    To remove it later:  .\uninstall_task.ps1  (from the installed folder)
#>

param(
    [string]$InstallDir
)

$ErrorActionPreference = 'Stop'

$TaskName = 'ZapretAutoUpdater'
$SrcDir   = $PSScriptRoot
$SrcConf  = Join-Path $SrcDir 'config.json'

if (-not (Test-Path (Join-Path $SrcDir 'updater.py'))) {
    throw "updater.py not found next to this script ($SrcDir)"
}
if (-not (Test-Path $SrcConf)) {
    throw "config.json not found. Copy config.example.json to config.json and set install_path first."
}

# --- Work out the target (sibling-of-zapret) folder ---
$cfg = Get-Content $SrcConf -Raw | ConvertFrom-Json
$installPath = $cfg.install_path
if (-not $installPath) {
    throw "config.json has no 'install_path'."
}

if (-not $InstallDir) {
    $parent = Split-Path -Parent $installPath
    $leaf   = Split-Path -Leaf   $installPath
    $InstallDir = Join-Path $parent ("{0}-updater" -f $leaf)
}
Write-Host "Installing updater to: $InstallDir" -ForegroundColor Cyan

# --- Copy the runtime files (skip if installing from the target itself) ---
$srcFull = (Resolve-Path $SrcDir).Path
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}
$dstFull = (Resolve-Path $InstallDir).Path

if ($srcFull -ne $dstFull) {
    Copy-Item (Join-Path $SrcDir 'updater.py')          -Destination $InstallDir -Force
    Copy-Item (Join-Path $SrcDir 'uninstall_task.ps1')  -Destination $InstallDir -Force
    # Don't clobber a config that already lives in the installed folder.
    $dstConf = Join-Path $InstallDir 'config.json'
    if (Test-Path $dstConf) {
        Write-Host "Kept existing config.json in target (not overwritten)." -ForegroundColor Yellow
    } else {
        Copy-Item $SrcConf -Destination $dstConf -Force
    }
} else {
    Write-Host "Running from the target folder; nothing to copy." -ForegroundColor Yellow
}

$Updater = Join-Path $InstallDir 'updater.py'

# --- Find pythonw.exe (no console window); fall back to python.exe ---
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $python) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { throw "Neither pythonw.exe nor python.exe found in PATH." }
    $candidate = Join-Path (Split-Path $py) 'pythonw.exe'
    if (Test-Path $candidate) { $python = $candidate } else { $python = $py }
}
Write-Host "Using interpreter: $python"

# --- Define the task ---
$action = New-ScheduledTaskAction -Execute $python `
    -Argument "`"$Updater`"" -WorkingDirectory $InstallDir

# Trigger 1: at logon. Trigger 2: hourly, starting now.
# NB: do NOT use [TimeSpan]::MaxValue for the duration — Windows PowerShell 5.1
# serializes it to P99999999DT23H59M59S, which Task Scheduler rejects as
# out-of-range. A large finite duration (10 years) works in both 5.1 and 7.
$atLogon = New-ScheduledTaskTrigger -AtLogOn
$hourly  = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

# Run in the current interactive user session, elevated.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Highest

# IgnoreNew: never start a second copy while one is still running (the hourly
# tick can otherwise overlap a logon run or a slow update). updater.py also
# takes its own lock, which additionally covers manual runs.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

# Remove an old copy if present, then register.
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger @($atLogon, $hourly) `
    -Principal $principal -Settings $settings `
    -Description 'Hourly auto-updater for zapret-discord-youtube' | Out-Null

Write-Host ""
Write-Host "Task '$TaskName' registered, pointing at $InstallDir." -ForegroundColor Green
Write-Host "This repo clone is now disposable - the updater runs from the installed copy."
Write-Host ""
Write-Host "Test it now:" -ForegroundColor Cyan
Write-Host "    Start-ScheduledTask -TaskName $TaskName"
Write-Host "    Get-Content '$InstallDir\updater.log' -Wait"
