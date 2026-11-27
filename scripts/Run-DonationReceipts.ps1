param(
    [Parameter(Mandatory=$true)][string]$User,
    [ValidateSet('CreateUser','ConfigureAccount','Full','Delta','Potential','PotentialDelta','Review','Manual','Combined','Product')]
    [string]$Action = 'Full',
    [string]$Account = 'primary',
    [int]$Year = (Get-Date).Year,
    [string]$Mailbox,
    [string[]]$Options = @(),
    [switch]$ResumePending,
    [switch]$RefreshBase
)
$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $PSScriptRoot
$python = Join-Path $project '.venv\Scripts\python.exe'
$manifest = Get-Content -LiteralPath (Join-Path $project 'production-source-manifest.json') -Raw | ConvertFrom-Json
foreach ($entry in $manifest.PSObject.Properties) {
    $path = Join-Path $project $entry.Name
    $hash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($hash -ne $entry.Value) { throw "Installed source changed: $($entry.Name)" }
}
$cli = @('-u', '-B', '-m', 'src.main', '--user', $User)
$source = @('--account', $Account, '--year', "$Year")
switch ($Action) {
    CreateUser       { $cli += @('--mode', 'create-user') }
    ConfigureAccount { $cli += @('--mode', 'configure-account', '--account', $Account, '--mailbox', $Mailbox) }
    Full             { $cli += @('--mode', 'full') + $source }
    Delta            { $cli += @('--mode', 'sync', '--delta') + $source }
    Potential        { $cli += @('--mode', 'potential-receipts') + $source }
    PotentialDelta   { $cli += @('--mode', 'potential-receipts', '--delta') + $source }
    Manual           { $cli += @('--mode', 'manual', '--year', "$Year") }
    Combined         { $cli += @('--mode', 'combined', '--year', "$Year") }
    Product          { $cli += @('--mode', 'product', '--year', "$Year") }
    Review           { $cli += @('--mode', 'identifier-review') + $source }
}
if ($ResumePending) { $cli += '--resume-pending' }
if ($RefreshBase) { $cli += '--refresh-base' }
Push-Location $project
try {
    & $python @cli @Options
    if ($LASTEXITCODE -ne 0) { throw 'Donation Receipts did not complete' }
} finally { Pop-Location }
