<#  Removes the ZapretAutoUpdater scheduled task. Run from an elevated PowerShell. #>
$ErrorActionPreference = 'Stop'
$TaskName = 'ZapretAutoUpdater'

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' removed." -ForegroundColor Green
} else {
    Write-Host "Task '$TaskName' is not registered." -ForegroundColor Yellow
}
