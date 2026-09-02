[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$SourceProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$SourceConsoleScript = Join-Path $PSScriptRoot 'console.ps1'
$FixtureRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("ai-game-console-data-dir-" + [Guid]::NewGuid().ToString('N'))
$FixtureProjectRoot = Join-Path $FixtureRoot 'project'
$FixtureScriptsRoot = Join-Path $FixtureProjectRoot 'scripts'
$FixtureConsoleScript = Join-Path $FixtureScriptsRoot 'console.ps1'
$FixtureOverrideRoot = Join-Path $FixtureRoot 'stage-store\epoch'
$FixtureOtherRoot = Join-Path $FixtureRoot 'other-store\epoch'
$FixtureReparseTarget = Join-Path $FixtureRoot 'reparse-target'
$FixtureReparsePath = Join-Path $FixtureRoot 'reparse-data-dir'
$FixtureAncestorTarget = Join-Path $FixtureRoot 'ancestor-target'
$FixtureAncestorLink = Join-Path $FixtureRoot 'ancestor-link'
$CapabilitySentinel = 'fixture-capability-' + [Guid]::NewGuid().ToString('N')
$EnvironmentNames = @(
    'AI_GAME_DATA_DIR',
    'AI_GAME_HARNESS_TOKEN',
    'AI_GAME_CONSOLE_SHUTDOWN_TOKEN',
    'AI_GAME_RUNTIME_MODE',
    'AI_GAME_GUI_EXECUTOR_ENABLED',
    'AI_GAME_ADB_PATH',
    'AI_GAME_ADB_SERIAL'
)
$PreviousEnvironment = @{}

function Assert-Condition {
    param([Parameter(Mandatory = $true)][bool]$Condition, [Parameter(Mandatory = $true)][string]$Message)
    if (-not $Condition) { throw $Message }
}

function Assert-Throws {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Description
    )
    $threw = $false
    try { & $Action } catch { $threw = $true }
    Assert-Condition $threw "$Description did not fail closed."
}

function Remove-FixtureReparsePoint {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    Assert-Condition (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) `
        "Refusing to remove a non-reparse fixture path: $Path"
    [System.IO.Directory]::Delete($Path)
}

foreach ($name in $EnvironmentNames) {
    $PreviousEnvironment[$name] = [Environment]::GetEnvironmentVariable(
        $name, [EnvironmentVariableTarget]::Process
    )
}

try {
    New-Item -ItemType Directory -Force -Path $FixtureScriptsRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $FixtureOverrideRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $FixtureOtherRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $FixtureReparseTarget | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $FixtureAncestorTarget 'ordinary-child') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $FixtureProjectRoot '.git') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $FixtureProjectRoot 'apps') | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $FixtureProjectRoot 'config') | Out-Null
    Copy-Item -LiteralPath $SourceConsoleScript -Destination $FixtureConsoleScript
    Set-Content -LiteralPath (Join-Path $FixtureProjectRoot 'config\executor-runtime.env') -Value 'AI_GAME_GUI_EXECUTOR_ENABLED=0' -Encoding ascii

    Remove-Item Env:AI_GAME_DATA_DIR -ErrorAction SilentlyContinue
    $env:AI_GAME_HARNESS_TOKEN = $CapabilitySentinel
    Remove-Item Env:AI_GAME_CONSOLE_SHUTDOWN_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:AI_GAME_RUNTIME_MODE -ErrorAction SilentlyContinue
    $env:AI_GAME_GUI_EXECUTOR_ENABLED = '0'
    Remove-Item Env:AI_GAME_ADB_PATH -ErrorAction SilentlyContinue
    Remove-Item Env:AI_GAME_ADB_SERIAL -ErrorAction SilentlyContinue

    $bootstrapOutput = . $FixtureConsoleScript status -NoBrowser *>&1 | Out-String -Width 4096
    Assert-Condition ($bootstrapOutput.Contains('Console: stopped')) 'Isolated console fixture did not load in status-only mode.'
    $expectedDefault = [System.IO.Path]::GetFullPath((Join-Path $FixtureProjectRoot 'runtime\console'))
    $resolvedDefault = Resolve-ConsoleDataRoot
    Assert-Condition (Test-SameDataDirectory $resolvedDefault $expectedDefault) 'Missing override did not retain the default runtime\console directory.'
    Assert-Condition ($null -eq [Environment]::GetEnvironmentVariable('AI_GAME_DATA_DIR', [EnvironmentVariableTarget]::Process)) 'Status-only default resolution polluted the caller environment.'

    $rawExplicit = Join-Path $FixtureOverrideRoot '.'
    $env:AI_GAME_DATA_DIR = $rawExplicit
    $resolvedExplicit = Resolve-ConsoleDataRoot
    $expectedExplicit = [System.IO.Path]::GetFullPath($FixtureOverrideRoot)
    Assert-Condition (Test-SameDataDirectory $resolvedExplicit $expectedExplicit) 'Explicit absolute data directory was not normalized and preferred.'

    $env:AI_GAME_DATA_DIR = '   '
    Assert-Throws { Resolve-ConsoleDataRoot | Out-Null } 'Whitespace override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value 'relative\store' -Source 'test' -RequireExisting | Out-Null } 'Relative override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value 'C:relative\store' -Source 'test' -RequireExisting | Out-Null } 'Drive-relative override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value '\\server\share\store' -Source 'test' -RequireExisting | Out-Null } 'UNC override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value '\\?\C:\store' -Source 'test' -RequireExisting | Out-Null } 'Extended device-namespace override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value '\\.\C:\store' -Source 'test' -RequireExisting | Out-Null } 'Device-namespace override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value 'C:\store\*' -Source 'test' -RequireExisting | Out-Null } 'Wildcard override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value ([System.IO.Path]::GetPathRoot($FixtureRoot)) -Source 'test' -RequireExisting | Out-Null } 'Filesystem-root override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value $ProjectRoot -Source 'test' -RequireExisting | Out-Null } 'Project-root override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value $FixtureRoot -Source 'test' -RequireExisting | Out-Null } 'Over-broad project ancestor override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value (Join-Path $ProjectRoot 'scripts') -Source 'test' -RequireExisting | Out-Null } 'Project scripts override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value (Join-Path $ProjectRoot '.git') -Source 'test' -RequireExisting | Out-Null } 'Project .git override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value (Join-Path $ProjectRoot 'apps') -Source 'test' -RequireExisting | Out-Null } 'Project apps override'
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value (Join-Path $FixtureRoot 'missing-store\epoch') -Source 'test' -RequireExisting | Out-Null } 'Missing requested override'

    $existingFile = Join-Path $FixtureRoot 'not-a-directory.db'
    Set-Content -LiteralPath $existingFile -Value 'fixture' -Encoding ascii
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value $existingFile -Source 'test' -RequireExisting | Out-Null } 'Existing-file override'

    New-Item -ItemType Junction -Path $FixtureReparsePath -Target $FixtureReparseTarget | Out-Null
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value $FixtureReparsePath -Source 'test' -RequireExisting | Out-Null } 'Reparse-point override'
    New-Item -ItemType Junction -Path $FixtureAncestorLink -Target $FixtureAncestorTarget | Out-Null
    Assert-Throws { ConvertTo-NormalizedDataDirectory -Value (Join-Path $FixtureAncestorLink 'ordinary-child') -Source 'test' -RequireExisting | Out-Null } 'Reparse-ancestor override'

    $capturedChildDataRoot = $null
    $capturedChildCapability = $null
    $fixtureThrowOnStart = $false
    function Start-Process {
        param(
            [string]$FilePath,
            [object[]]$ArgumentList,
            [string]$WorkingDirectory,
            [string]$RedirectStandardOutput,
            [string]$RedirectStandardError,
            [string]$WindowStyle,
            [switch]$PassThru
        )
        $script:capturedChildDataRoot = [Environment]::GetEnvironmentVariable(
            'AI_GAME_DATA_DIR', [EnvironmentVariableTarget]::Process
        )
        $script:capturedChildCapability = [Environment]::GetEnvironmentVariable(
            'AI_GAME_HARNESS_TOKEN', [EnvironmentVariableTarget]::Process
        )
        if ($script:fixtureThrowOnStart) { throw 'fixture child start failure' }
        return [pscustomobject]@{ Id = 4242; HasExited = $false }
    }
    try {
        $env:AI_GAME_DATA_DIR = $rawExplicit
        $env:AI_GAME_CONSOLE_SHUTDOWN_TOKEN = 'parent-shutdown'
        $env:AI_GAME_RUNTIME_MODE = 'parent-runtime'
        $child = Start-ManagedConsoleChild -Arguments @('-m', 'fixture') -ShutdownToken ('a' * 32) -StartedRuntimeMode 'kernel_active' -StartedDataDir $expectedExplicit
        Assert-Condition ($child.Id -eq 4242) 'Managed child seam did not return the started process.'
        Assert-Condition (Test-SameDataDirectory $capturedChildDataRoot $expectedExplicit) 'Managed child did not inherit the normalized explicit data directory.'
        Assert-Condition ($capturedChildCapability -eq $CapabilitySentinel) 'Managed child did not inherit the process-only capability sentinel.'
        Assert-Condition ($env:AI_GAME_DATA_DIR -eq $rawExplicit) 'Managed child did not restore the caller explicit environment value.'
        Assert-Condition ($env:AI_GAME_CONSOLE_SHUTDOWN_TOKEN -eq 'parent-shutdown') 'Managed child did not restore the caller shutdown environment value.'
        Assert-Condition ($env:AI_GAME_RUNTIME_MODE -eq 'parent-runtime') 'Managed child did not restore the caller runtime-mode environment value.'

        Remove-Item Env:AI_GAME_DATA_DIR -ErrorAction SilentlyContinue
        $capturedChildDataRoot = $null
        Start-ManagedConsoleChild -Arguments @('-m', 'fixture') -ShutdownToken ('b' * 32) -StartedRuntimeMode 'kernel_active' -StartedDataDir $expectedDefault | Out-Null
        Assert-Condition (Test-SameDataDirectory $capturedChildDataRoot $expectedDefault) 'Managed child did not inherit the default data directory.'
        Assert-Condition ($null -eq [Environment]::GetEnvironmentVariable('AI_GAME_DATA_DIR', [EnvironmentVariableTarget]::Process)) 'Managed child did not remove its temporary default environment value.'

        $env:AI_GAME_DATA_DIR = $rawExplicit
        $env:AI_GAME_CONSOLE_SHUTDOWN_TOKEN = 'throw-parent-shutdown'
        $env:AI_GAME_RUNTIME_MODE = 'throw-parent-runtime'
        $script:fixtureThrowOnStart = $true
        Assert-Throws {
            Start-ManagedConsoleChild -Arguments @('-m', 'fixture') -ShutdownToken ('e' * 32) -StartedRuntimeMode 'kernel_active' -StartedDataDir $expectedExplicit | Out-Null
        } 'Throwing Start-Process seam'
        Assert-Condition ($env:AI_GAME_DATA_DIR -eq $rawExplicit) 'Throwing Start-Process did not restore caller data directory.'
        Assert-Condition ($env:AI_GAME_CONSOLE_SHUTDOWN_TOKEN -eq 'throw-parent-shutdown') 'Throwing Start-Process did not restore caller shutdown token.'
        Assert-Condition ($env:AI_GAME_RUNTIME_MODE -eq 'throw-parent-runtime') 'Throwing Start-Process did not restore caller runtime mode.'
        Assert-Condition ($env:AI_GAME_HARNESS_TOKEN -eq $CapabilitySentinel) 'Throwing Start-Process changed the process-only capability sentinel.'
        $script:fixtureThrowOnStart = $false
    } finally {
        Remove-Item Function:Start-Process -ErrorAction SilentlyContinue
    }

    New-Item -ItemType Directory -Force -Path $RunRoot | Out-Null
    $commonState = [ordered]@{
        project_root = $ProjectRoot
        host = '127.0.0.1'
        port = 4310
        launcher_pid = 111
        launcher_created_at = '2026-08-30T00:00:00.0000000Z'
        listener_pid = 222
        listener_created_at = '2026-08-30T00:00:01.0000000Z'
        written_at = '2026-08-30T00:00:02.0000000Z'
    }
    foreach ($version in 1, 2, 3) {
        $legacyState = [ordered]@{}
        $legacyState.schema_version = $version
        foreach ($entry in $commonState.GetEnumerator()) { $legacyState[$entry.Key] = $entry.Value }
        if ($version -ge 2) { $legacyState.shutdown_token = 'c' * 32 }
        if ($version -ge 3) { $legacyState.runtime_mode = 'kernel_active' }
        $legacyState | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding utf8
        $readLegacy = Read-ConsoleState
        Assert-Condition (Test-SameDataDirectory $readLegacy.data_dir $expectedDefault) "Schema v$version did not infer the default data directory."
    }

    $launcher = [pscustomobject]@{ ProcessId = 333; CreationDate = [DateTime]::UtcNow }
    $listener = [pscustomobject]@{ ProcessId = 444; CreationDate = [DateTime]::UtcNow.AddSeconds(1) }
    Write-ConsoleState $launcher $listener '127.0.0.1' 4310 ('d' * 32) 'kernel_active' $expectedExplicit
    $roundTrip = Read-ConsoleState
    Assert-Condition ([int]$roundTrip.schema_version -eq 4) 'Console state did not upgrade to schema v4.'
    Assert-Condition (Test-SameDataDirectory $roundTrip.data_dir $expectedExplicit) 'Schema v4 did not round-trip the normalized data directory.'

    $validV4Json = Get-Content -LiteralPath $StateFile -Raw
    $invalidV4 = $validV4Json | ConvertFrom-Json
    $invalidV4.PSObject.Properties.Remove('data_dir')
    $invalidV4 | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding utf8
    Assert-Throws { Read-ConsoleState | Out-Null } 'Schema v4 without data_dir'
    $invalidV4 = $validV4Json | ConvertFrom-Json
    $invalidV4.data_dir = 'relative\store'
    $invalidV4 | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding utf8
    Assert-Throws { Read-ConsoleState | Out-Null } 'Schema v4 with relative data_dir'
    $invalidV4 = $validV4Json | ConvertFrom-Json
    $invalidV4.data_dir = $ProjectRoot
    $invalidV4 | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding utf8
    Assert-Throws { Read-ConsoleState | Out-Null } 'Schema v4 with over-broad data_dir'
    $missingRecordedDataDir = Join-Path $FixtureRoot 'missing-recorded-store\epoch'
    $missingV4 = $validV4Json | ConvertFrom-Json
    $missingV4.data_dir = $missingRecordedDataDir
    $missingV4 | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding utf8
    $readMissingV4 = Read-ConsoleState
    Assert-Condition (Test-SameDataDirectory $readMissingV4.data_dir ([System.IO.Path]::GetFullPath($missingRecordedDataDir))) 'Schema v4 rejected a currently missing recorded data directory.'
    $validV4Json | Set-Content -LiteralPath $StateFile -Encoding utf8

    $originalGetOwned = (Get-Item Function:Get-OwnedConsoleInstance).ScriptBlock
    $originalRequestShutdown = (Get-Item Function:Request-GracefulConsoleShutdown).ScriptBlock
    $originalWaitExit = (Get-Item Function:Wait-VerifiedConsoleExit).ScriptBlock
    $originalGetPortOwner = (Get-Item Function:Get-PortOwner).ScriptBlock
    $originalRemoveState = (Get-Item Function:Remove-ConsoleStateFiles).ScriptBlock
    $originalEnsureDirectories = (Get-Item Function:Ensure-RuntimeDirectories).ScriptBlock
    try {
        function Get-OwnedConsoleInstance {
            if ($script:fixtureNoOwnedInstance) { return $null }
            return [pscustomobject]@{
                Host = '127.0.0.1'; Port = 4310; Url = 'http://127.0.0.1:4310'
                Launcher = $null; Listener = $null; ShutdownToken = $null
                RuntimeMode = 'kernel_active'; DataDir = $script:fixtureRecordedDataDir; Legacy = $false
            }
        }
        function Ensure-RuntimeDirectories { param($ConsoleDataRoot) $script:fixtureEnsureCalled = $true }
        $script:fixtureNoOwnedInstance = $false
        $script:fixtureEnsureCalled = $false
        $script:fixtureRecordedDataDir = $FixtureOtherRoot
        $env:AI_GAME_DATA_DIR = $expectedExplicit
        Assert-Throws { Start-Console } 'Start with requested/recorded data-directory mismatch'
        Assert-Condition (-not $fixtureEnsureCalled) 'Start mismatch wrote runtime directories before rejecting the store mismatch.'

        function Request-GracefulConsoleShutdown { param($Instance) return $false }
        function Wait-VerifiedConsoleExit { param($Instance, $TimeoutSeconds) return $true }
        function Get-PortOwner { param($TargetPort) return $null }
        function Remove-ConsoleStateFiles { $script:fixtureStateRemoved = $true }
        foreach ($invalidCallerOverride in @('relative\bad-store', (Join-Path $FixtureRoot 'missing-caller-store\epoch'))) {
            $script:fixtureStateRemoved = $false
            $script:fixtureRecordedDataDir = $missingRecordedDataDir
            $env:AI_GAME_DATA_DIR = $invalidCallerOverride
            $stopOutput = Stop-Console *>&1 | Out-String -Width 4096
            Assert-Condition $fixtureStateRemoved 'Stop did not complete under an invalid or missing caller override.'
            Assert-Condition ($stopOutput.Contains('Console stopped.')) 'Stop did not report completion under an invalid or missing caller override.'
        }

        $script:fixtureNoOwnedInstance = $true
        $env:AI_GAME_DATA_DIR = 'relative\bad-status-store'
        $statusWithInvalidCaller = Show-Status *>&1 | Out-String -Width 4096
        Assert-Condition ($statusWithInvalidCaller.Contains('Console: stopped')) 'Status was blocked by an invalid caller data-directory override.'
    } finally {
        Set-Item Function:Get-OwnedConsoleInstance -Value $originalGetOwned
        Set-Item Function:Request-GracefulConsoleShutdown -Value $originalRequestShutdown
        Set-Item Function:Wait-VerifiedConsoleExit -Value $originalWaitExit
        Set-Item Function:Get-PortOwner -Value $originalGetPortOwner
        Set-Item Function:Remove-ConsoleStateFiles -Value $originalRemoveState
        Set-Item Function:Ensure-RuntimeDirectories -Value $originalEnsureDirectories
    }

    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    Set-Content -LiteralPath $StdoutLog -Value 'fixture stdout without secrets' -Encoding ascii
    Set-Content -LiteralPath $StderrLog -Value 'fixture stderr without secrets' -Encoding ascii
    $launcherContent = Get-Content -LiteralPath $FixtureConsoleScript -Raw
    Assert-Condition ($launcherContent -match '(?s)function Test-Console.*test-console-data-dir\.ps1') 'The focused data-directory test is not wired into the managed console test gate.'
    foreach ($durableTarget in @($FixtureConsoleScript, $StateFile, $ExecutorRuntimeConfig, $StdoutLog, $StderrLog)) {
        if (-not (Test-Path -LiteralPath $durableTarget -PathType Leaf)) { continue }
        $durableContent = Get-Content -LiteralPath $durableTarget -Raw
        Assert-Condition (-not $durableContent.Contains('AI_GAME_HARNESS_TOKEN')) "Durable target persisted a capability-token field name: $durableTarget"
        Assert-Condition (-not $durableContent.Contains('WEFTMATE_AI_GAME_DEV_TOKEN')) "Durable target persisted a development-token field name: $durableTarget"
        Assert-Condition (-not $durableContent.Contains($CapabilitySentinel)) "Durable target persisted the capability sentinel value: $durableTarget"
    }

    [pscustomobject]@{
        DefaultDataDirectory = 'verified'
        ExplicitProcessOverride = 'verified'
        InvalidPathsFailClosed = 'verified'
        ParentEnvironmentRestored = 'verified'
        LegacyStateCompatibility = 'verified'
        StateV4RoundTrip = 'verified'
        StartMismatchRejected = 'verified'
        StopEnvironmentMismatch = 'verified'
        CapabilitySecretPersistence = 'absent'
        LiveRuntimeTouched = 'no'
    } | Format-List
} finally {
    foreach ($name in $EnvironmentNames) {
        $prior = $PreviousEnvironment[$name]
        if ($null -eq $prior) {
            Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:$name" -Value $prior
        }
    }
    Remove-FixtureReparsePoint $FixtureReparsePath
    Remove-FixtureReparsePoint $FixtureAncestorLink
    $normalizedFixture = [System.IO.Path]::GetFullPath($FixtureRoot)
    $normalizedTemp = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    if ($normalizedFixture.StartsWith($normalizedTemp, [System.StringComparison]::OrdinalIgnoreCase) -and
        $normalizedFixture -ne $normalizedTemp -and
        (Test-Path -LiteralPath $normalizedFixture)) {
        Remove-Item -LiteralPath $normalizedFixture -Recurse -Force
    }
}
