param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')][string]$User,
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')][string]$Account = 'primary',
    [datetime]$At = '09:00'
)
$ErrorActionPreference = 'Stop'
$launcher = Join-Path $PSScriptRoot 'Run-DonationReceipts.ps1'
$name = "DonationReceipts-$User-$Account-Delta"
$arguments = "-NoProfile -File `"$launcher`" -User $User -Account $Account -Action Delta"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited
