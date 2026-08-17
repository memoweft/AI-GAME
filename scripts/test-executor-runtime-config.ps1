[CmdletBinding()]
param(
    [switch]$Live
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ConfigPath = Join-Path $ProjectRoot 'config\executor-runtime.env'
$ConsoleScript = Join-Path $PSScriptRoot 'console.ps1'
$SyncScript = Join-Path $PSScriptRoot 'sync-mumu-executor.ps1'

function Assert-Condition {
    param([Parameter(Mandatory = $true)][bool]$Condition, [Parameter(Mandatory = $true)][string]$Message)
    if (-not $Condition) { throw $Message }
}

function Read-StrictExecutorConfig {
    $values = @{}
    foreach ($rawLine in Get-Content -LiteralPath $ConfigPath) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        Assert-Condition ($line -match '^(AI_GAME_GUI_EXECUTOR_ENABLED|AI_GAME_ADB_PATH|AI_GAME_ADB_SERIAL)=(.+)$') "Invalid executor configuration line: $rawLine"
        Assert-Condition (-not $values.ContainsKey($matches[1])) "Duplicate executor configuration key: $($matches[1])"
        $values[$matches[1]] = $matches[2]
    }
    return $values
}

$config = Read-StrictExecutorConfig
Assert-Condition ($config.Count -eq 3) 'Executor configuration must contain exactly the three approved keys.'
Assert-Condition ($config['AI_GAME_GUI_EXECUTOR_ENABLED'] -eq '1') 'Local executor configuration must enable the executor.'
Assert-Condition ([System.IO.Path]::IsPathRooted($config['AI_GAME_ADB_PATH'])) 'Executor configuration must use an absolute adb.exe path.'
Assert-Condition ([System.IO.Path]::GetFileName($config['AI_GAME_ADB_PATH']) -ieq 'adb.exe') 'Executor configuration must point to adb.exe.'
Assert-Condition (Test-Path -LiteralPath $config['AI_GAME_ADB_PATH'] -PathType Leaf) 'Configured adb.exe does not exist.'
Assert-Condition ($config['AI_GAME_ADB_SERIAL'] -match '^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$') 'Executor configuration serial is not a valid ADB serial.'

$consoleContent = Get-Content -LiteralPath $ConsoleScript -Raw
Assert-Condition ($consoleContent.Contains('Import-ExecutorRuntimeConfiguration')) 'Console must import the executor runtime configuration.'
Assert-Condition ($consoleContent.Contains('$AllowedExecutorEnvironmentNames')) 'Console must whitelist executor environment keys.'
Assert-Condition (-not $consoleContent.Contains('. $ExecutorRuntimeConfig')) 'Console must not dot-source executor runtime configuration.'
Assert-Condition ($consoleContent.Contains('Assert-AdbPath')) 'Console must validate the configured adb.exe path.'

$syncContent = Get-Content -LiteralPath $SyncScript -Raw
foreach ($requiredText in @('info --vmindex', 'is_android_started', 'is_process_started', 'start_finished', '127.0.0.1', ' connect $serial', '-s $serial get-state', '[System.IO.File]::Move')) {
    Assert-Condition ($syncContent.Contains($requiredText)) "Sync script is missing required safety behavior: $requiredText"
}
Assert-Condition ($syncContent.Contains('[System.IO.File]::Replace')) `
    'Sync script must atomically replace an existing configuration file.'
Assert-Condition (-not $syncContent.Contains('[System.IO.File]::Move($temporaryPath, $ConfigPath, $true)')) `
    'Sync script must remain compatible with Windows PowerShell 5.1, which has no three-argument File.Move overload.'
Assert-Condition ($syncContent -notmatch '(?m)&\s+\$resolvedCliPath\s+(create|clone|delete|control|start)\b') `
    'Sync script must not invoke a MuMu lifecycle command.'

# A child process proves that an explicit process environment beats the file,
# while `status` observes only state and never starts/stops another process.
$previousEnabled = $env:AI_GAME_GUI_EXECUTOR_ENABLED
$previousSerial = $env:AI_GAME_ADB_SERIAL
try {
    $env:AI_GAME_GUI_EXECUTOR_ENABLED = '0'
    $env:AI_GAME_ADB_SERIAL = 'R58M1234AB'
    $statusOutput = & $ConsoleScript status *>&1 | Out-String
    $statusSucceeded = $?
    Assert-Condition $statusSucceeded "Console status failed while checking explicit environment precedence: $statusOutput"
    Assert-Condition ($statusOutput.Contains('Executor runtime config: enabled=0;')) 'Explicit AI_GAME_GUI_EXECUTOR_ENABLED=0 did not override config file.'
    Assert-Condition ($statusOutput.Contains('serial=R58M1234AB')) 'Console rejected or replaced a valid USB ADB serial.'
} finally {
    if ($null -eq $previousEnabled) { Remove-Item Env:AI_GAME_GUI_EXECUTOR_ENABLED -ErrorAction SilentlyContinue }
    else { $env:AI_GAME_GUI_EXECUTOR_ENABLED = $previousEnabled }
    if ($null -eq $previousSerial) { Remove-Item Env:AI_GAME_ADB_SERIAL -ErrorAction SilentlyContinue }
    else { $env:AI_GAME_ADB_SERIAL = $previousSerial }
}

if ($Live) {
    & $SyncScript -VmIndex 0
    Assert-Condition ($LASTEXITCODE -eq 0) 'Live MuMu executor sync failed.'
    $config = Read-StrictExecutorConfig
    Assert-Condition ($config['AI_GAME_GUI_EXECUTOR_ENABLED'] -eq '1') 'Live sync did not enable executor configuration.'
    Assert-Condition ($config['AI_GAME_ADB_SERIAL'] -match '^127\.0\.0\.1:([1-9][0-9]{0,4})$') 'Live sync wrote a non-loopback serial.'
}

[pscustomobject]@{
    StaticContract = 'verified'
    ExplicitEnvironmentPrecedence = 'verified'
    LiveMuMuSync = if ($Live) { 'verified' } else { 'not requested' }
} | Format-List
