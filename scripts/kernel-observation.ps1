[CmdletBinding()]
param(
    [ValidateRange(2, 1440)]
    [int]$DurationMinutes = 30,

    [ValidateRange(1, 1439)]
    [int]$RestartAfterMinutes = 15,

    [ValidateRange(5, 300)]
    [int]$SampleIntervalSeconds = 30,

    [ValidateRange(5, 120)]
    [int]$RestartRecoverySeconds = 30
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http

# [constraint-source: USER_DECISION; ref: D17 in
# docs/product/09_DECISIONS_AND_OPEN_QUESTIONS.md]
# The defaults are the U7 acceptance run. Parameter overrides are diagnostic
# only and cannot close U7.

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConsoleScript = Join-Path $PSScriptRoot "console.ps1"
$ConsoleStatePath = Join-Path $ProjectRoot "runtime\run\console.state.json"
$LogRoot = Join-Path $ProjectRoot "runtime\logs"
$RuntimeModeLog = Join-Path $LogRoot "runtime-mode.jsonl"
$CutoverLog = Join-Path $LogRoot "kernel-cutover.jsonl"
$ArchiveLogRoot = Join-Path $LogRoot "archive"
$ConsoleErrorLog = Join-Path $LogRoot "console.err.log"
$BaseUrl = "http://127.0.0.1:4310"
$RunStartedAt = [DateTimeOffset]::UtcNow
$RunId = $RunStartedAt.ToString("yyyyMMddTHHmmssfffffffZ")
$EvidencePath = Join-Path $LogRoot "u7-observation-${RunId}.jsonl"
$ExpectedPython = [System.IO.Path]::GetFullPath(
    (Join-Path $ProjectRoot "runtime\envs\console\Scripts\python.exe")
)

if ($RestartAfterMinutes -ge $DurationMinutes) {
    throw "RestartAfterMinutes must be less than DurationMinutes."
}

function Write-ObservationEvent {
    param(
        [Parameter(Mandatory = $true)][string]$Event,
        [Parameter(Mandatory = $true)]$Details
    )

    New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
    $record = [ordered]@{
        timestamp = [DateTimeOffset]::UtcNow.ToString("o")
        run_id = $RunId
        event = $Event
        details = $Details
    }
    Add-Content -LiteralPath $EvidencePath `
        -Value ($record | ConvertTo-Json -Compress -Depth 12) `
        -Encoding utf8
}

function Get-Json {
    param([Parameter(Mandatory = $true)][string]$Path)
    return Invoke-RestMethod -Uri "${BaseUrl}${Path}" -TimeoutSec 10
}

function Get-LogLineCount {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 0 }
    return @(Get-Content -LiteralPath $Path).Count
}

function Get-ConsoleState {
    if (-not (Test-Path -LiteralPath $ConsoleStatePath -PathType Leaf)) {
        throw "Console state is missing: $ConsoleStatePath"
    }
    return Get-Content -LiteralPath $ConsoleStatePath -Raw | ConvertFrom-Json
}

function Assert-ConsoleProcessIdentity {
    $state = Get-ConsoleState
    if ([string]$state.runtime_mode -ne "kernel_active") {
        throw "Recorded console mode is not kernel_active."
    }

    $listenerId = [int]$state.listener_pid
    $launcherId = [int]$state.launcher_pid
    $listener = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $listenerId" -ErrorAction SilentlyContinue
    $launcher = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $launcherId" -ErrorAction SilentlyContinue
    if ($null -eq $listener -or $null -eq $launcher) {
        throw "The recorded console launcher/listener process is not running."
    }

    $launcherExecutable = [System.IO.Path]::GetFullPath([string]$launcher.ExecutablePath)
    if (-not $launcherExecutable.Equals($ExpectedPython, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The console launcher executable does not match the owned runtime."
    }
    if ([string]$listener.CommandLine -notlike "*-m ai_game_console.main*" -or
        [string]$listener.CommandLine -notlike "*--host 127.0.0.1*" -or
        [string]$listener.CommandLine -notlike "*--port 4310*") {
        throw "The listener command line does not match the managed AI-GAME console."
    }
    if ([int]$listener.ParentProcessId -ne $launcherId) {
        throw "The listener is not owned by the recorded console launcher."
    }

    $expectedListenerCreated = [DateTimeOffset]::Parse(
        [string]$state.listener_created_at,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::RoundtripKind
    ).UtcDateTime
    $actualListenerCreated = $listener.CreationDate.ToUniversalTime()
    if ([Math]::Abs(($actualListenerCreated - $expectedListenerCreated).TotalSeconds) -gt 1) {
        throw "The listener PID creation time does not match the recorded owned process."
    }

    return [ordered]@{
        launcher_pid = $launcherId
        listener_pid = $listenerId
        listener_created_at = [string]$state.listener_created_at
    }
}

function Assert-LegacyWriteFence {
    $client = [System.Net.Http.HttpClient]::new()
    try {
        $client.DefaultRequestHeaders.Add("X-AI-Game-Client", "console-v1")
        $content = [System.Net.Http.StringContent]::new(
            "{}",
            [System.Text.Encoding]::UTF8,
            "application/json"
        )
        $response = $client.PostAsync(
            "${BaseUrl}/api/v1/learning/jobs",
            $content
        ).GetAwaiter().GetResult()
        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 403) {
            throw "Legacy write fence returned HTTP $([int]$response.StatusCode), expected 403."
        }
        $payload = $body | ConvertFrom-Json
        if ([string]$payload.error.code -ne "LEGACY_DEVICE_WRITE_DISABLED") {
            throw "Legacy write fence returned an unexpected error code."
        }
        return [ordered]@{
            status_code = [int]$response.StatusCode
            error_code = [string]$payload.error.code
        }
    } finally {
        $client.Dispose()
    }
}

function Assert-ReadSurfaces {
    $archive = Get-Json "/api/compat/v1/mobile-tasks?limit=1"
    $tasks = Get-Json "/api/v1/tasks?limit=500"
    $goals = Get-Json "/api/v2/goals?limit=500"
    return [ordered]@{
        legacy_archive_count = [int]$archive.count
        kernel_task_count = [int]$tasks.count
        goal_run_count = [int]$goals.count
    }
}

function Assert-ObservationSample {
    param(
        [Parameter(Mandatory = $true)][int]$Sequence,
        [Parameter(Mandatory = $true)][int]$MinimumRuntimeModeLines,
        [Parameter(Mandatory = $true)][int]$MinimumCutoverLines
    )

    $health = Get-Json "/health"
    if ([string]$health.status -ne "ok" -or [string]$health.database -ne "ready") {
        throw "Console health or database readiness is not green."
    }

    $mode = Get-Json "/api/v1/runtime/mode"
    if ([string]$mode.mode -ne "kernel_active" -or
        -not [bool]$mode.kernel_active -or
        [bool]$mode.legacy_writable -or
        [bool]$mode.draining -or
        [int]$mode.active_legacy_task_count -ne 0 -or
        @($mode.active_task_ids).Count -ne 0) {
        throw "Runtime mode or Legacy drain invariant failed."
    }

    $runtime = Get-Json "/api/v1/runtime"
    if ([string]$runtime.overall_status -ne "ready") {
        throw "Runtime capability projection is not ready."
    }
    $requiredCapabilities = @("model", "executor", "adb")
    foreach ($capabilityId in $requiredCapabilities) {
        $capability = @($runtime.capabilities | Where-Object { $_.id -eq $capabilityId }) |
            Select-Object -First 1
        if ($null -eq $capability -or [string]$capability.status -ne "ready") {
            throw "Required runtime capability is not ready: $capabilityId"
        }
    }

    $tasks = Get-Json "/api/v1/tasks?limit=500"
    $activeStatuses = @("CREATED", "PLANNING", "RUNNING", "PAUSED", "STOPPING")
    $activeTasks = @($tasks.items | Where-Object { $_.status -in $activeStatuses })
    if ($activeTasks.Count -ne 0) {
        throw "Kernel observation requires settled ownership; active Kernel tasks were found."
    }

    $leaseStats = Get-Json "/api/v1/runtime/leases/stats"
    if ([int]$leaseStats.stats.active -ne 0) {
        throw "Kernel observation requires settled ownership; active leases were found."
    }

    $process = Assert-ConsoleProcessIdentity
    $runtimeModeLines = Get-LogLineCount $RuntimeModeLog
    $cutoverLines = Get-LogLineCount $CutoverLog
    if ($runtimeModeLines -lt $MinimumRuntimeModeLines -or
        $cutoverLines -lt $MinimumCutoverLines) {
        throw "An append-only U7 log was truncated during observation."
    }

    $sample = [ordered]@{
        sequence = $Sequence
        health = [string]$health.status
        database = [string]$health.database
        mode = [string]$mode.mode
        legacy_writable = [bool]$mode.legacy_writable
        active_legacy_task_count = [int]$mode.active_legacy_task_count
        active_kernel_task_count = $activeTasks.Count
        active_lease_count = [int]$leaseStats.stats.active
        runtime_status = [string]$runtime.overall_status
        listener_pid = [int]$process.listener_pid
        runtime_mode_log_lines = $runtimeModeLines
        cutover_log_lines = $cutoverLines
    }
    Write-ObservationEvent "sample_passed" $sample
    Write-Host (
        "Sample {0}: PASS mode={1} listener={2} leases={3}" -f `
            $Sequence, $sample.mode, $sample.listener_pid, $sample.active_lease_count
    )
    return $sample
}

function Assert-RestartLogSequence {
    param([Parameter(Mandatory = $true)][int]$MinimumLineCount)

    $lineCount = Get-LogLineCount $RuntimeModeLog
    if ($lineCount -lt ($MinimumLineCount + 3)) {
        throw "Runtime mode journal did not append the required restart events."
    }
    $events = @(Get-Content -LiteralPath $RuntimeModeLog | ForEach-Object {
        $_ | ConvertFrom-Json
    } | Where-Object {
        [DateTimeOffset]::Parse([string]$_.timestamp) -ge $RunStartedAt
    })
    $stopped = @($events | Where-Object {
        $_.event -eq "runtime_stopped" -and $_.mode -eq "kernel_active"
    })
    $requested = @($events | Where-Object {
        $_.event -eq "composition_requested" -and
        $_.mode -eq "kernel_active" -and
        $_.details.kernel_binding_kind -eq "runtime_kernel"
    })
    $started = @($events | Where-Object {
        $_.event -eq "runtime_started" -and
        $_.mode -eq "kernel_active" -and
        $_.details.kernel_binding_kind -eq "runtime_kernel" -and
        $_.details.legacy_workers_started -eq $false
    })
    if ($stopped.Count -eq 0 -or $requested.Count -eq 0 -or $started.Count -eq 0) {
        throw "Runtime mode journal is missing the controlled Kernel restart sequence."
    }
    return [ordered]@{
        line_count = $lineCount
        stopped = $stopped.Count
        composition_requested = $requested.Count
        started = $started.Count
    }
}

function Assert-NoFatalConsoleError {
    $candidates = @()
    if (Test-Path -LiteralPath $ConsoleErrorLog -PathType Leaf) {
        $candidates += Get-Item -LiteralPath $ConsoleErrorLog
    }
    if (Test-Path -LiteralPath $ArchiveLogRoot -PathType Container) {
        $candidates += Get-ChildItem -LiteralPath $ArchiveLogRoot -File |
            Where-Object {
                $_.LastWriteTimeUtc -ge $RunStartedAt.UtcDateTime.AddSeconds(-2) -and
                $_.Name -like "console.err-*"
            }
    }
    $fatalPattern = "Traceback \(most recent call last\)|CRITICAL|Exception in ASGI application|Unhandled exception"
    $matches = @()
    foreach ($candidate in $candidates) {
        $matches += @(Select-String -LiteralPath $candidate.FullName -Pattern $fatalPattern)
    }
    if ($matches.Count -ne 0) {
        throw "A fatal or unhandled console error was recorded during observation."
    }
    return [ordered]@{
        inspected_file_count = $candidates.Count
        fatal_match_count = 0
    }
}

$runtimeModeBaseline = Get-LogLineCount $RuntimeModeLog
$cutoverBaseline = Get-LogLineCount $CutoverLog
$deadline = $RunStartedAt.AddMinutes($DurationMinutes)
$restartAt = $RunStartedAt.AddMinutes($RestartAfterMinutes)
$sampleSequence = 0
$restartCompleted = $false
$restartElapsedSeconds = $null
$initialState = $null
$finalSample = $null

Write-ObservationEvent "observation_started" ([ordered]@{
    duration_minutes = $DurationMinutes
    restart_after_minutes = $RestartAfterMinutes
    sample_interval_seconds = $SampleIntervalSeconds
    restart_recovery_seconds = $RestartRecoverySeconds
    runtime_mode_log_baseline = $runtimeModeBaseline
    cutover_log_baseline = $cutoverBaseline
})

try {
    $initialState = Get-ConsoleState
    $initialFence = Assert-LegacyWriteFence
    $initialReads = Assert-ReadSurfaces

    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        $sampleSequence++
        $finalSample = Assert-ObservationSample `
            -Sequence $sampleSequence `
            -MinimumRuntimeModeLines $runtimeModeBaseline `
            -MinimumCutoverLines $cutoverBaseline

        if (-not $restartCompleted -and [DateTimeOffset]::UtcNow -ge $restartAt) {
            $restartTimer = [Diagnostics.Stopwatch]::StartNew()
            & $ConsoleScript stop -NoBrowser
            & $ConsoleScript start -RuntimeMode kernel_active -NoBrowser
            $restartTimer.Stop()
            $restartElapsedSeconds = [Math]::Round($restartTimer.Elapsed.TotalSeconds, 3)
            if ($restartElapsedSeconds -gt $RestartRecoverySeconds) {
                throw "Controlled restart exceeded $RestartRecoverySeconds seconds."
            }

            $restartedState = Get-ConsoleState
            if ([int]$restartedState.listener_pid -eq [int]$initialState.listener_pid -and
                [string]$restartedState.listener_created_at -eq [string]$initialState.listener_created_at) {
                throw "Controlled restart did not create a new listener identity."
            }
            $restartLog = Assert-RestartLogSequence -MinimumLineCount $runtimeModeBaseline
            $postRestartFence = Assert-LegacyWriteFence
            $postRestartReads = Assert-ReadSurfaces
            $restartCompleted = $true
            Write-ObservationEvent "controlled_restart_passed" ([ordered]@{
                elapsed_seconds = $restartElapsedSeconds
                old_listener_pid = [int]$initialState.listener_pid
                new_listener_pid = [int]$restartedState.listener_pid
                runtime_mode_log = $restartLog
                legacy_write_fence = $postRestartFence
                read_surfaces = $postRestartReads
            })
        }

        $remainingSeconds = ($deadline - [DateTimeOffset]::UtcNow).TotalSeconds
        if ($remainingSeconds -gt 0) {
            $sleepSeconds = [Math]::Min($SampleIntervalSeconds, $remainingSeconds)
            Start-Sleep -Milliseconds ([int][Math]::Ceiling($sleepSeconds * 1000))
        }
    }

    $sampleSequence++
    $finalSample = Assert-ObservationSample `
        -Sequence $sampleSequence `
        -MinimumRuntimeModeLines $runtimeModeBaseline `
        -MinimumCutoverLines $cutoverBaseline
    if (-not $restartCompleted) {
        throw "The controlled midpoint restart did not run."
    }

    $finalFence = Assert-LegacyWriteFence
    $finalReads = Assert-ReadSurfaces
    $finalRestartLog = Assert-RestartLogSequence -MinimumLineCount $runtimeModeBaseline
    $fatalErrors = Assert-NoFatalConsoleError
    $elapsed = ([DateTimeOffset]::UtcNow - $RunStartedAt).TotalSeconds
    if ($elapsed -lt ($DurationMinutes * 60)) {
        throw "Observation ended before the required wall-clock duration."
    }

    $summary = [ordered]@{
        result = "PASS"
        run_id = $RunId
        evidence_path = $EvidencePath
        started_at = $RunStartedAt.ToString("o")
        ended_at = [DateTimeOffset]::UtcNow.ToString("o")
        elapsed_seconds = [Math]::Round($elapsed, 3)
        sample_count = $sampleSequence
        restart_elapsed_seconds = $restartElapsedSeconds
        initial_legacy_write_fence = $initialFence
        final_legacy_write_fence = $finalFence
        initial_read_surfaces = $initialReads
        final_read_surfaces = $finalReads
        restart_log = $finalRestartLog
        fatal_errors = $fatalErrors
        final_sample = $finalSample
    }
    Write-ObservationEvent "observation_passed" $summary
    $summary | ConvertTo-Json -Depth 12
} catch {
    Write-ObservationEvent "observation_failed" ([ordered]@{
        message = $_.Exception.Message
        sample_count = $sampleSequence
        restart_completed = $restartCompleted
    })
    throw
}
