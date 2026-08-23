[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("status", "drain", "snapshot", "restore-check", "activate", "rollback")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConsoleScript = Join-Path $PSScriptRoot "console.ps1"
$PythonExe = Join-Path $ProjectRoot "runtime\envs\console\Scripts\python.exe"
$DataRoot = Join-Path $ProjectRoot "runtime\console"
$LegacyDatabase = Join-Path $DataRoot "mobile-tasks.db"
$BackupRoot = Join-Path $ProjectRoot "runtime\backups\kernel-cutover"
$RestoreRoot = Join-Path $BackupRoot "restore-exercises"
$OperatorJournal = Join-Path $ProjectRoot "runtime\logs\kernel-cutover.jsonl"
$ModeEndpoint = "http://127.0.0.1:4310/api/v1/runtime/mode"
$GatewayTasksEndpoint = "http://127.0.0.1:4310/api/v1/tasks"

function Write-OperatorEvent {
    param(
        [Parameter(Mandatory = $true)][string]$Event,
        [hashtable]$Details = @{}
    )
    $directory = Split-Path -Parent $OperatorJournal
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $record = [ordered]@{
        timestamp = [DateTimeOffset]::UtcNow.ToString("o")
        event = $Event
        details = $Details
    }
    Add-Content -LiteralPath $OperatorJournal -Value ($record | ConvertTo-Json -Compress -Depth 6) -Encoding utf8
}

function Get-ModeView {
    return Invoke-RestMethod -Uri $ModeEndpoint -TimeoutSec 5
}

function Get-LatestSnapshot {
    if (-not (Test-Path -LiteralPath $BackupRoot -PathType Container)) { return $null }
    return Get-ChildItem -LiteralPath $BackupRoot -Filter "mobile-tasks-*.db" -File |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
}

function Assert-Drained {
    $view = Get-ModeView
    if ($view.mode -ne "draining") {
        throw "Snapshot/activation requires the running console to be in draining mode."
    }
    if ([int]$view.active_legacy_task_count -ne 0) {
        $ids = @($view.active_task_ids) -join ", "
        throw "Legacy drain is incomplete ($($view.active_legacy_task_count) active): $ids"
    }
    return $view
}

function Show-CutoverStatus {
    & $ConsoleScript status -NoBrowser
    try {
        $view = Get-ModeView
        $view | ConvertTo-Json -Depth 6
    } catch {
        Write-Host "Runtime mode endpoint is unavailable."
    }
    $latest = Get-LatestSnapshot
    if ($null -ne $latest) {
        Write-Host "Latest snapshot: $($latest.FullName)"
    } else {
        Write-Host "Latest snapshot: none"
    }
    if (Test-Path -LiteralPath $OperatorJournal) {
        Write-Host "Operator journal: $OperatorJournal"
    }
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf) -and $Action -in @("snapshot", "restore-check")) {
    throw "Console Python environment is missing. Run scripts\console.ps1 setup first."
}

switch ($Action) {
    "status" {
        Show-CutoverStatus
    }
    "drain" {
        & $ConsoleScript stop -NoBrowser
        & $ConsoleScript start -RuntimeMode draining -NoBrowser
        $view = Get-ModeView
        Write-OperatorEvent "draining_started" @{
            active_legacy_task_count = [int]$view.active_legacy_task_count
            active_task_ids = @($view.active_task_ids)
        }
        $view | ConvertTo-Json -Depth 6
    }
    "snapshot" {
        $view = Assert-Drained
        New-Item -ItemType Directory -Force -Path $BackupRoot | Out-Null
        $result = & $PythonExe -m ai_game_console.cutover_cli snapshot `
            --source $LegacyDatabase --backup-dir $BackupRoot
        if ($LASTEXITCODE -ne 0) { throw "Legacy snapshot failed." }
        $payload = $result | ConvertFrom-Json
        Write-OperatorEvent "legacy_snapshot_created" @{
            snapshot = [string]$payload.snapshot
            active_legacy_task_count = [int]$view.active_legacy_task_count
        }
        $result
    }
    "restore-check" {
        $snapshot = Get-LatestSnapshot
        if ($null -eq $snapshot) { throw "No cutover snapshot exists." }
        New-Item -ItemType Directory -Force -Path $RestoreRoot | Out-Null
        $result = & $PythonExe -m ai_game_console.cutover_cli restore-check `
            --snapshot $snapshot.FullName --restore-dir $RestoreRoot
        if ($LASTEXITCODE -ne 0) { throw "Controlled snapshot restore failed." }
        $payload = $result | ConvertFrom-Json
        Write-OperatorEvent "snapshot_restore_verified" @{
            snapshot = [string]$payload.snapshot
            restored_copy = [string]$payload.restored_copy
            logical_sha256 = [string]$payload.logical_sha256
            integrity = [string]$payload.integrity
        }
        $result
    }
    "activate" {
        $view = Assert-Drained
        $snapshot = Get-LatestSnapshot
        if ($null -eq $snapshot) { throw "Kernel activation requires a completed Legacy snapshot." }
        & $ConsoleScript stop -NoBrowser
        & $ConsoleScript start -RuntimeMode kernel_active -NoBrowser
        $active = Get-ModeView
        if (-not $active.kernel_active -or $active.legacy_writable) {
            throw "Kernel-active post-check failed."
        }
        Write-OperatorEvent "kernel_activated" @{
            snapshot = $snapshot.FullName
            legacy_writable = [bool]$active.legacy_writable
        }
        $active | ConvertTo-Json -Depth 6
    }
    "rollback" {
        $view = Get-ModeView
        if ($view.mode -ne "kernel_active") {
            throw "Rollback exercise requires a running kernel_active console."
        }
        $tasks = Invoke-RestMethod -Uri $GatewayTasksEndpoint -TimeoutSec 10
        $activeStatuses = @("CREATED", "PLANNING", "RUNNING", "PAUSED", "STOPPING")
        $activeTasks = @($tasks.items | Where-Object { $_.status -in $activeStatuses })
        if ($activeTasks.Count -ne 0) {
            $ids = @($activeTasks | ForEach-Object { $_.id }) -join ", "
            throw "Kernel ownership is not settled; active tasks: $ids"
        }
        & $ConsoleScript stop -NoBrowser
        & $ConsoleScript start -RuntimeMode legacy -NoBrowser
        $legacy = Get-ModeView
        if ($legacy.mode -ne "legacy" -or -not $legacy.legacy_writable) {
            throw "Legacy rollback post-check failed."
        }
        Write-OperatorEvent "rollback_to_legacy_verified" @{
            inspected_kernel_task_count = @($tasks.items).Count
            legacy_writable = [bool]$legacy.legacy_writable
        }
        $legacy | ConvertTo-Json -Depth 6
    }
}
