# Generate a new local XML; optional installation requires explicit -Install.
param([switch]$Install)
$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Project Python missing' }
$folder = Join-Path $repo 'runtime\registry'
New-Item -ItemType Directory -Path $folder -Force | Out-Null
$path = Join-Path $folder ('weekly-task-' + [guid]::NewGuid().ToString('N') + '.xml')
$command = $python
$escape = [System.Security.SecurityElement]
$commandXml = $escape::Escape($command)
$argumentsXml = $escape::Escape('-m src.registry_sync')
$workingXml = $escape::Escape($repo)
$userIdXml = $escape::Escape([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$boundary = (Get-Date).Date.AddDays(1).AddHours(9).ToString('s')
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Weekly nonprofit registry refresh; daily due check; independent of Codex</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>$boundary</StartBoundary><Enabled>true</Enabled><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Principals><Principal id="CurrentUser"><UserId>$userIdXml</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><StartWhenAvailable>true</StartWhenAvailable><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable><ExecutionTimeLimit>PT10M</ExecutionTimeLimit></Settings>
  <Actions Context="CurrentUser"><Exec><Command>$commandXml</Command><Arguments>$argumentsXml</Arguments><WorkingDirectory>$workingXml</WorkingDirectory></Exec></Actions>
</Task>
"@
# Exclusive creation, so exported task definitions are never overwritten.
$stream = [System.IO.File]::Open($path, 'CreateNew', 'Write', 'None')
try {
    $preamble = [System.Text.Encoding]::Unicode.GetPreamble()
    $stream.Write($preamble, 0, $preamble.Length)
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($xml)
    $stream.Write($bytes, 0, $bytes.Length)
} finally { $stream.Dispose() }
Write-Output $path
if ($Install) {
    # No -Force: an existing task is never silently replaced.
    Register-ScheduledTask -TaskName 'DonationReceipts-WeeklyRegistry' -Xml $xml | Out-Null
    Write-Output 'Installed DonationReceipts-WeeklyRegistry for current logged-on user; daily wakeup, weekly download'
}
