[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("setup", "build", "start", "stop", "status", "test")]
    [string]$Action = "start",

    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BackendRoot = Join-Path $ProjectRoot "apps\console\backend"
$FrontendRoot = Join-Path $ProjectRoot "apps\console\frontend"
$RuntimeRoot = Join-Path $ProjectRoot "runtime"
$DataRoot = Join-Path $RuntimeRoot "console"
$RunRoot = Join-Path $RuntimeRoot "run"
$LogRoot = Join-Path $RuntimeRoot "logs"
$EnvRoot = Join-Path $RuntimeRoot "envs\console"
$PythonExe = Join-Path $EnvRoot "Scripts\python.exe"
$PidFile = Join-Path $RunRoot "console.pid"
$StateFile = Join-Path $RunRoot "console.state.json"
$StdoutLog = Join-Path $LogRoot "console.out.log"
$StderrLog = Join-Path $LogRoot "console.err.log"
$FrontendIndex = Join-Path $FrontendRoot "dist\index.html"
$ExecutorRuntimeConfig = Join-Path $ProjectRoot "config\executor-runtime.env"

# This file is deliberately parsed as data, never dot-sourced.  It is a
# small, non-secret handoff from the MuMu discovery script to the console
# launcher; accepting arbitrary PowerShell here would turn configuration into
# code execution.
$AllowedExecutorEnvironmentNames = @(
    "AI_GAME_GUI_EXECUTOR_ENABLED",
    "AI_GAME_ADB_PATH",
    "AI_GAME_ADB_SERIAL"
)

function Assert-AdbSerial {
    param([Parameter(Mandatory = $true)][string]$Serial)

    if ($Serial.Length -gt 256 -or $Serial -notmatch '^[A-Za-z0-9][A-Za-z0-9._:-]*$') {
        throw "AI_GAME_ADB_SERIAL must be an ADB USB, emulator, or host:port serial."
    }
    if ($Serial -match '^[^:]+:([0-9]+)$' -and [int64]$matches[1] -gt 65535) {
        throw "AI_GAME_ADB_SERIAL contains an invalid TCP port."
    }
}

function Assert-AdbPath {
    param([Parameter(Mandatory = $true)][string]$AdbPath)

    $isRooted = [System.IO.Path]::IsPathRooted($AdbPath)
    $candidate = [System.IO.Path]::GetFullPath($AdbPath)
    if (-not $isRooted -or
        -not [System.IO.Path]::GetFileName($candidate).Equals('adb.exe', [System.StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "AI_GAME_ADB_PATH must be an absolute path to an existing adb.exe."
    }
}

function Import-ExecutorRuntimeConfiguration {
    if (-not (Test-Path -LiteralPath $ExecutorRuntimeConfig -PathType Leaf)) { return }

    $settings = @{}
    $lineNumber = 0
    foreach ($rawLine in Get-Content -LiteralPath $ExecutorRuntimeConfig) {
        $lineNumber++
        $line = $rawLine.Trim()
        if ([string]::IsNullOrWhiteSpace($line) -or $line.StartsWith('#')) { continue }
        if ($line -notmatch '^([A-Z0-9_]+)=(.*)$') {
            throw "Invalid executor runtime configuration at line ${lineNumber}: expected KEY=VALUE."
        }
        $name = $matches[1]
        $value = $matches[2].Trim()
        if ($name -notin $AllowedExecutorEnvironmentNames) {
            throw "Unsupported executor runtime setting '$name' at line $lineNumber."
        }
        if ($settings.ContainsKey($name)) {
            throw "Duplicate executor runtime setting '$name' at line $lineNumber."
        }
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Executor runtime setting '$name' must not be empty."
        }
        $settings[$name] = $value
    }

    foreach ($name in $AllowedExecutorEnvironmentNames) {
        # An explicitly supplied process environment is the operator override.
        $processValue = [Environment]::GetEnvironmentVariable($name, [EnvironmentVariableTarget]::Process)
        if (-not [string]::IsNullOrWhiteSpace($processValue)) { continue }
        if ($settings.ContainsKey($name)) {
            Set-Item -Path "Env:$name" -Value $settings[$name]
        }
    }
}

function Assert-ExecutorRuntimeEnvironment {
    if (-not [string]::IsNullOrWhiteSpace($env:AI_GAME_GUI_EXECUTOR_ENABLED) -and
        $env:AI_GAME_GUI_EXECUTOR_ENABLED -notin @('0', '1')) {
        throw "AI_GAME_GUI_EXECUTOR_ENABLED must be 0 or 1."
    }
    if (-not [string]::IsNullOrWhiteSpace($env:AI_GAME_ADB_PATH)) {
        Assert-AdbPath $env:AI_GAME_ADB_PATH
    }
    if (-not [string]::IsNullOrWhiteSpace($env:AI_GAME_ADB_SERIAL)) {
        Assert-AdbSerial $env:AI_GAME_ADB_SERIAL
    }
}

Import-ExecutorRuntimeConfiguration
Assert-ExecutorRuntimeEnvironment

$HostName = if ($env:AI_GAME_CONSOLE_HOST) { $env:AI_GAME_CONSOLE_HOST } else { "127.0.0.1" }
$Port = if ($env:AI_GAME_CONSOLE_PORT) { [int]$env:AI_GAME_CONSOLE_PORT } else { 4310 }
$ConsoleUrl = "http://${HostName}:${Port}"
$HealthUrl = "$ConsoleUrl/health"

if ($HostName -ne "127.0.0.1") {
    throw "AI_GAME_CONSOLE_HOST must remain 127.0.0.1. The console is local-only."
}
if ($Port -lt 1024 -or $Port -gt 65535) {
    throw "AI_GAME_CONSOLE_PORT must be between 1024 and 65535."
}

function Ensure-RuntimeDirectories {
    foreach ($path in @($DataRoot, $RunRoot, $LogRoot, $EnvRoot)) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
    }
}

function Assert-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name"
    }
}

function Install-Backend {
    Assert-Command "uv"
    Ensure-RuntimeDirectories

    if (-not (Test-Path -LiteralPath $PythonExe)) {
        Write-Host "Creating the isolated console environment..."
        & uv venv --python 3.11 $EnvRoot
        if ($LASTEXITCODE -ne 0) { throw "Could not create the console environment." }
    }

    Write-Host "Installing the local console backend..."
    & uv pip install --python $PythonExe --link-mode=copy --editable "${BackendRoot}[dev]"
    if ($LASTEXITCODE -ne 0) { throw "Could not install the console backend." }
}

function Install-Frontend {
    Assert-Command "npm"
    if (Test-Path -LiteralPath (Join-Path $FrontendRoot "package-lock.json")) {
        & npm --prefix $FrontendRoot ci
    } else {
        & npm --prefix $FrontendRoot install
    }
    if ($LASTEXITCODE -ne 0) { throw "Could not install the console frontend." }
}

function Build-Frontend {
    Assert-Command "npm"
    if (-not (Test-Path -LiteralPath (Join-Path $FrontendRoot "node_modules"))) {
        Install-Frontend
    }
    Write-Host "Building the browser console..."
    & npm --prefix $FrontendRoot run build
    if ($LASTEXITCODE -ne 0) { throw "Could not build the browser console." }
}

function Test-FrontendBuildRequired {
    if (-not (Test-Path -LiteralPath $FrontendIndex)) { return $true }
    $buildTime = (Get-Item -LiteralPath $FrontendIndex).LastWriteTimeUtc
    $frontendInputs = @(Get-ChildItem -LiteralPath (Join-Path $FrontendRoot "src") -File -Recurse)
    foreach ($relativePath in @(
        "index.html",
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "vite.config.ts"
    )) {
        $candidate = Join-Path $FrontendRoot $relativePath
        if (Test-Path -LiteralPath $candidate) {
            $frontendInputs += Get-Item -LiteralPath $candidate
        }
    }
    $newerSource = $frontendInputs |
        Where-Object { $_.LastWriteTimeUtc -gt $buildTime } |
        Select-Object -First 1
    return $null -ne $newerSource
}

function Get-CimProcessById {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    return Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction SilentlyContinue
}

function Get-ProcessCreationStamp {
    param([Parameter(Mandatory = $true)]$Process)
    return $Process.CreationDate.ToUniversalTime().ToString("o")
}

function Test-ProcessCreationStamp {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)]$ExpectedStamp
    )
    $normalizedExpected = if ($ExpectedStamp -is [DateTime]) {
        $ExpectedStamp.ToUniversalTime().ToString("o")
    } else {
        [DateTimeOffset]::Parse(
            [string]$ExpectedStamp,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind
        ).UtcDateTime.ToString("o")
    }
    return (Get-ProcessCreationStamp $Process) -eq $normalizedExpected
}

function Test-ConsoleCommand {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$ExpectedHost,
        [Parameter(Mandatory = $true)][int]$ExpectedPort
    )
    $commandLine = [string]$Process.CommandLine
    $matchesAddress = $commandLine -like "*--host $ExpectedHost*" -and
        $commandLine -like "*--port $ExpectedPort*"
    $legacyUvicorn = $commandLine -like "*-m uvicorn*" -and
        $commandLine -like "*ai_game_console.app:app*"
    $managedMain = $commandLine -like "*-m ai_game_console.main*"
    return $matchesAddress -and ($legacyUvicorn -or $managedMain)
}

function Test-LauncherIdentity {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][string]$ExpectedHost,
        [Parameter(Mandatory = $true)][int]$ExpectedPort
    )
    $expectedPython = [System.IO.Path]::GetFullPath($PythonExe)
    $actualPython = if ($Process.ExecutablePath) {
        [System.IO.Path]::GetFullPath($Process.ExecutablePath)
    } else { "" }
    return $actualPython.Equals($expectedPython, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-ConsoleCommand $Process $ExpectedHost $ExpectedPort)
}

function Test-DescendantProcess {
    param(
        [Parameter(Mandatory = $true)][int]$ChildProcessId,
        [Parameter(Mandatory = $true)][int]$AncestorProcessId
    )
    $currentId = $ChildProcessId
    for ($depth = 0; $depth -lt 16; $depth++) {
        if ($currentId -eq $AncestorProcessId) { return $true }
        $current = Get-CimProcessById $currentId
        if ($null -eq $current -or $current.ParentProcessId -le 0) { return $false }
        $currentId = [int]$current.ParentProcessId
    }
    return $false
}

function Get-PortOwner {
    param([Parameter(Mandatory = $true)][int]$TargetPort)
    return Get-NetTCPConnection -LocalPort $TargetPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Remove-ConsoleStateFiles {
    Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $StateFile -Force -ErrorAction SilentlyContinue
}

function Read-ConsoleState {
    if (-not (Test-Path -LiteralPath $StateFile)) { return $null }
    try {
        $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
    } catch {
        throw "The console state file is invalid: $StateFile"
    }
    foreach ($requiredName in @(
        "schema_version", "project_root", "host", "port",
        "launcher_pid", "launcher_created_at", "listener_pid", "listener_created_at"
    )) {
        if ($null -eq $state.$requiredName) {
            throw "The console state file is missing '$requiredName': $StateFile"
        }
    }
    $schemaVersion = [int]$state.schema_version
    if ($schemaVersion -notin @(1, 2)) {
        throw "Unsupported console state version: $($state.schema_version)"
    }
    if ($schemaVersion -eq 2 -and (
        [string]::IsNullOrWhiteSpace([string]$state.shutdown_token) -or
        [string]$state.shutdown_token -notmatch '^[a-f0-9]{32}$'
    )) {
        throw "The console state has an invalid graceful shutdown token: $StateFile"
    }
    if (-not ([string]$state.project_root).Equals($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "The console state belongs to another project root. No process was changed."
    }
    if ([string]$state.host -ne "127.0.0.1" -or [int]$state.port -lt 1024 -or [int]$state.port -gt 65535) {
        throw "The console state contains a non-local or invalid address. No process was changed."
    }
    return $state
}

function Write-ConsoleState {
    param(
        [Parameter(Mandatory = $true)]$Launcher,
        [Parameter(Mandatory = $true)]$Listener,
        [Parameter(Mandatory = $true)][string]$StartedHost,
        [Parameter(Mandatory = $true)][int]$StartedPort,
        [Parameter(Mandatory = $true)][string]$ShutdownToken
    )
    $state = [ordered]@{
        schema_version = 2
        project_root = $ProjectRoot
        host = $StartedHost
        port = $StartedPort
        launcher_pid = [int]$Launcher.ProcessId
        launcher_created_at = Get-ProcessCreationStamp $Launcher
        listener_pid = [int]$Listener.ProcessId
        listener_created_at = Get-ProcessCreationStamp $Listener
        shutdown_token = $ShutdownToken
        written_at = [DateTimeOffset]::UtcNow.ToString("o")
    }
    $temporaryStateFile = "$StateFile.tmp"
    $state | ConvertTo-Json | Set-Content -LiteralPath $temporaryStateFile -Encoding utf8
    Move-Item -LiteralPath $temporaryStateFile -Destination $StateFile -Force
    Set-Content -LiteralPath $PidFile -Value $Launcher.ProcessId -Encoding ascii
}

function Get-OwnedConsoleInstance {
    $state = Read-ConsoleState
    if ($null -ne $state) {
        $instanceHost = [string]$state.host
        $instancePort = [int]$state.port
        $launcher = Get-CimProcessById ([int]$state.launcher_pid)
        $listener = Get-CimProcessById ([int]$state.listener_pid)

        if ($null -eq $launcher -and $null -eq $listener) {
            Remove-ConsoleStateFiles
            return $null
        }
        if ($null -ne $launcher -and (
            -not (Test-ProcessCreationStamp $launcher $state.launcher_created_at) -or
            -not (Test-LauncherIdentity $launcher $instanceHost $instancePort)
        )) {
            throw "The recorded launcher PID no longer belongs to the AI-GAME console. No process was changed."
        }
        if ($null -ne $listener -and (
            -not (Test-ProcessCreationStamp $listener $state.listener_created_at) -or
            -not (Test-ConsoleCommand $listener $instanceHost $instancePort)
        )) {
            throw "The recorded listener PID no longer belongs to the AI-GAME console. No process was changed."
        }

        $portOwner = Get-PortOwner $instancePort
        if ($null -ne $portOwner -and (
            $null -eq $listener -or [int]$portOwner.OwningProcess -ne [int]$listener.ProcessId
        )) {
            throw "Port $instancePort is now owned by another process. No process was changed."
        }

        return [pscustomobject]@{
            Host = $instanceHost
            Port = $instancePort
            Url = "http://${instanceHost}:${instancePort}"
            Launcher = $launcher
            Listener = $listener
            ShutdownToken = if ([int]$state.schema_version -eq 2) { [string]$state.shutdown_token } else { $null }
            Legacy = [int]$state.schema_version -eq 1
        }
    }

    if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
    $rawPid = (Get-Content -LiteralPath $PidFile -Raw).Trim()
    $parsedPid = 0
    if (-not [int]::TryParse($rawPid, [ref]$parsedPid)) {
        throw "The console PID file is invalid: $PidFile"
    }
    $launcher = Get-CimProcessById $parsedPid
    if ($null -eq $launcher) {
        Remove-ConsoleStateFiles
        return $null
    }
    if (-not (Test-LauncherIdentity $launcher $HostName $Port)) {
        throw "PID $parsedPid is not the AI-GAME console. It will not be stopped or replaced."
    }

    $listener = $null
    $portOwner = Get-PortOwner $Port
    if ($null -ne $portOwner) {
        $candidate = Get-CimProcessById ([int]$portOwner.OwningProcess)
        if ($null -eq $candidate -or
            -not (Test-ConsoleCommand $candidate $HostName $Port) -or
            -not (Test-DescendantProcess ([int]$candidate.ProcessId) ([int]$launcher.ProcessId))) {
            throw "Port $Port is not owned by the recorded AI-GAME console tree. No process was changed."
        }
        $listener = $candidate
    }

    return [pscustomobject]@{
        Host = $HostName
        Port = $Port
        Url = $ConsoleUrl
        Launcher = $launcher
        Listener = $listener
        ShutdownToken = $null
        Legacy = $true
    }
}

function Stop-VerifiedProcess {
    param(
        [Parameter(Mandatory = $true)]$Snapshot,
        [Parameter(Mandatory = $true)][string]$Description
    )
    $expectedStamp = Get-ProcessCreationStamp $Snapshot
    $current = Get-CimProcessById ([int]$Snapshot.ProcessId)
    if ($null -eq $current) { return }
    if (-not (Test-ProcessCreationStamp $current $expectedStamp)) {
        throw "$Description PID $($Snapshot.ProcessId) was reused. No replacement process was stopped."
    }

    Stop-Process -Id $Snapshot.ProcessId
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        $current = Get-CimProcessById ([int]$Snapshot.ProcessId)
        if ($null -eq $current) { return }
        if (-not (Test-ProcessCreationStamp $current $expectedStamp)) {
            throw "$Description PID $($Snapshot.ProcessId) was reused while stopping."
        }
        Start-Sleep -Milliseconds 250
    }
    Stop-Process -Id $Snapshot.ProcessId -Force
    for ($attempt = 0; $attempt -lt 8; $attempt++) {
        if ($null -eq (Get-CimProcessById ([int]$Snapshot.ProcessId))) { return }
        Start-Sleep -Milliseconds 250
    }
    throw "$Description PID $($Snapshot.ProcessId) did not stop."
}

function Start-Console {
    Ensure-RuntimeDirectories

    $running = Get-OwnedConsoleInstance
    if ($null -ne $running) {
        Write-Host "Console is already running at $($running.Url)."
        if (-not $NoBrowser) { Start-Process $running.Url }
        return
    }

    $portOwner = Get-PortOwner $Port
    if ($null -ne $portOwner) {
        throw "Port $Port is already in use by PID $($portOwner.OwningProcess). No process was stopped."
    }

    if (-not (Test-Path -LiteralPath $PythonExe)) { Install-Backend }
    if (Test-FrontendBuildRequired) { Build-Frontend }

    $env:AI_GAME_PROJECT_ROOT = $ProjectRoot
    $env:AI_GAME_DATA_DIR = $DataRoot
    $shutdownToken = [Guid]::NewGuid().ToString("N")
    $previousShutdownToken = [Environment]::GetEnvironmentVariable(
        "AI_GAME_CONSOLE_SHUTDOWN_TOKEN", [EnvironmentVariableTarget]::Process
    )
    $arguments = @(
        "-m", "ai_game_console.main",
        "--host", $HostName,
        "--port", $Port.ToString()
    )

    Write-Host "Starting the local console..."
    Set-Item -Path "Env:AI_GAME_CONSOLE_SHUTDOWN_TOKEN" -Value $shutdownToken
    try {
        $process = Start-Process -FilePath $PythonExe `
            -ArgumentList $arguments `
            -WorkingDirectory $BackendRoot `
            -RedirectStandardOutput $StdoutLog `
            -RedirectStandardError $StderrLog `
            -WindowStyle Hidden `
            -PassThru
    } finally {
        if ($null -eq $previousShutdownToken) {
            Remove-Item -Path "Env:AI_GAME_CONSOLE_SHUTDOWN_TOKEN" -ErrorAction SilentlyContinue
        } else {
            Set-Item -Path "Env:AI_GAME_CONSOLE_SHUTDOWN_TOKEN" -Value $previousShutdownToken
        }
    }
    Set-Content -LiteralPath $PidFile -Value $process.Id -Encoding ascii

    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($process.HasExited) { break }
        try {
            $health = Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 1
            if ($health.status -eq "ok") {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }

    if (-not $ready) {
        $launcherSnapshot = Get-CimProcessById ([int]$process.Id)
        if ($null -ne $launcherSnapshot) {
            $consoleChildren = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
                $_.ProcessId -ne $launcherSnapshot.ProcessId -and
                (Test-ConsoleCommand $_ $HostName $Port) -and
                (Test-DescendantProcess ([int]$_.ProcessId) ([int]$launcherSnapshot.ProcessId))
            })
            foreach ($child in $consoleChildren) {
                Stop-VerifiedProcess $child "Console child"
            }
            Stop-VerifiedProcess $launcherSnapshot "Console launcher"
        }
        Remove-ConsoleStateFiles
        $tail = if (Test-Path -LiteralPath $StderrLog) {
            (Get-Content -LiteralPath $StderrLog -Tail 30) -join [Environment]::NewLine
        } else { "No error log was written." }
        throw "The console did not become ready.`n$tail"
    }

    $launcher = Get-CimProcessById ([int]$process.Id)
    $listenerConnection = Get-PortOwner $Port
    $listener = if ($null -ne $listenerConnection) {
        Get-CimProcessById ([int]$listenerConnection.OwningProcess)
    } else { $null }
    if ($null -eq $launcher -or
        $null -eq $listener -or
        -not (Test-LauncherIdentity $launcher $HostName $Port) -or
        -not (Test-ConsoleCommand $listener $HostName $Port) -or
        -not (Test-DescendantProcess ([int]$listener.ProcessId) ([int]$launcher.ProcessId))) {
        if ($null -ne $listener -and $null -ne $launcher -and
            (Test-ConsoleCommand $listener $HostName $Port) -and
            (Test-DescendantProcess ([int]$listener.ProcessId) ([int]$launcher.ProcessId))) {
            Stop-VerifiedProcess $listener "Console listener"
        }
        if ($null -ne $launcher -and (Test-LauncherIdentity $launcher $HostName $Port)) {
            Stop-VerifiedProcess $launcher "Console launcher"
        }
        Remove-ConsoleStateFiles
        throw "The console responded, but its launcher/listener process tree could not be verified."
    }

    Write-ConsoleState $launcher $listener $HostName $Port $shutdownToken
    Write-Host "Console is ready at $ConsoleUrl (listener PID $($listener.ProcessId))."
    if (-not $NoBrowser) { Start-Process $ConsoleUrl }
}

function Request-GracefulConsoleShutdown {
    param([Parameter(Mandatory = $true)]$Instance)

    if ($null -eq $Instance.Listener -or
        [string]::IsNullOrWhiteSpace([string]$Instance.ShutdownToken)) {
        return $false
    }
    try {
        $response = Invoke-RestMethod `
            -Uri "$($Instance.Url)/api/v1/shutdown" `
            -Method Post `
            -Headers @{
                "X-AI-Game-Client" = "console-v1"
                "X-AI-Game-Shutdown-Token" = [string]$Instance.ShutdownToken
            } `
            -TimeoutSec 5
    } catch {
        Write-Warning "The graceful console shutdown request failed; using the verified fallback."
        return $false
    }
    if ($response.status -ne "accepted") {
        Write-Warning "The console did not accept the graceful shutdown request; using the verified fallback."
        return $false
    }
    return $true
}

function Wait-VerifiedConsoleExit {
    param(
        [Parameter(Mandatory = $true)]$Instance,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $snapshots = @()
    $seenProcessIds = @{}
    foreach ($snapshot in @($Instance.Launcher, $Instance.Listener)) {
        if ($null -eq $snapshot) { continue }
        $processId = [int]$snapshot.ProcessId
        if ($seenProcessIds.ContainsKey($processId)) { continue }
        $seenProcessIds[$processId] = $true
        $snapshots += $snapshot
    }
    $attempts = [Math]::Ceiling($TimeoutSeconds * 4)
    for ($attempt = 0; $attempt -lt $attempts; $attempt++) {
        $stillRunning = $false
        foreach ($snapshot in $snapshots) {
            $current = Get-CimProcessById ([int]$snapshot.ProcessId)
            if ($null -eq $current) { continue }
            if (-not (Test-ProcessCreationStamp $current (Get-ProcessCreationStamp $snapshot))) {
                throw "Verified console PID $($snapshot.ProcessId) was reused while waiting for graceful shutdown."
            }
            $stillRunning = $true
        }
        if (-not $stillRunning) { return $true }
        Start-Sleep -Milliseconds 250
    }
    return $false
}

function Stop-Console {
    $instance = Get-OwnedConsoleInstance
    if ($null -eq $instance) {
        Write-Host "Console is not running."
        return
    }

    Write-Host "Stopping console at $($instance.Url)..."
    $stoppedGracefully = $false
    if (Request-GracefulConsoleShutdown $instance) {
        Write-Host "Graceful shutdown accepted; waiting for runtime cleanup..."
        $stoppedGracefully = Wait-VerifiedConsoleExit $instance 90
        if (-not $stoppedGracefully) {
            Write-Warning "The console did not exit after graceful shutdown; using the verified fallback."
        }
    } elseif ([string]::IsNullOrWhiteSpace([string]$instance.ShutdownToken)) {
        Write-Warning "This console predates graceful shutdown support; using the verified fallback."
    }

    if (-not $stoppedGracefully) {
        if ($null -ne $instance.Listener -and (
            $null -eq $instance.Launcher -or
            [int]$instance.Listener.ProcessId -ne [int]$instance.Launcher.ProcessId
        )) {
            Stop-VerifiedProcess $instance.Listener "Console listener"
        }
        if ($null -ne $instance.Launcher) {
            Stop-VerifiedProcess $instance.Launcher "Console launcher"
        } elseif ($null -ne $instance.Listener) {
            Stop-VerifiedProcess $instance.Listener "Console listener"
        }
    }

    $remainingOwner = $null
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        $remainingOwner = Get-PortOwner ([int]$instance.Port)
        if ($null -eq $remainingOwner) { break }
        Start-Sleep -Milliseconds 250
    }
    if ($null -ne $remainingOwner) {
        $knownPids = @()
        if ($null -ne $instance.Launcher) { $knownPids += [int]$instance.Launcher.ProcessId }
        if ($null -ne $instance.Listener) { $knownPids += [int]$instance.Listener.ProcessId }
        if ([int]$remainingOwner.OwningProcess -in $knownPids) {
            throw "The verified console process stopped incompletely; port $($instance.Port) is still owned by PID $($remainingOwner.OwningProcess)."
        }
        Write-Warning "The console stopped, but port $($instance.Port) is now used by unrelated PID $($remainingOwner.OwningProcess). It was not changed."
    }

    Remove-ConsoleStateFiles
    Write-Host "Console stopped."
}

function Show-Status {
    $executorEnabled = if ($env:AI_GAME_GUI_EXECUTOR_ENABLED -eq '1') { '1' } else { '0' }
    $adbPath = if ($env:AI_GAME_ADB_PATH) { $env:AI_GAME_ADB_PATH } else { 'not configured' }
    $adbSerial = if ($env:AI_GAME_ADB_SERIAL) { $env:AI_GAME_ADB_SERIAL } else { 'not configured' }
    Write-Host "Executor runtime config: enabled=$executorEnabled; adb=$adbPath; serial=$adbSerial"

    $instance = Get-OwnedConsoleInstance
    if ($null -eq $instance) {
        Write-Host "Console: stopped"
        return
    }
    $instanceHealthUrl = "$($instance.Url)/health"
    try {
        $health = Invoke-RestMethod -Uri $instanceHealthUrl -TimeoutSec 2
        Write-Host "Console: $($health.status)"
        Write-Host "Address: $($instance.Url)"
        if ($null -ne $instance.Listener) {
            Write-Host "Listener PID: $($instance.Listener.ProcessId)"
        }
        Write-Host "Version: $($health.version)"
    } catch {
        Write-Host "Console process exists, but readiness could not be confirmed."
        Write-Host "Address: $($instance.Url)"
    }
}

function Test-Console {
    if (-not (Test-Path -LiteralPath $PythonExe)) { Install-Backend }
    if (-not (Test-Path -LiteralPath (Join-Path $FrontendRoot "node_modules"))) { Install-Frontend }

    & $PythonExe -m pytest (Join-Path $ProjectRoot "apps\console\tests\backend")
    if ($LASTEXITCODE -ne 0) { throw "Backend tests failed." }
    & npm --prefix $FrontendRoot test
    if ($LASTEXITCODE -ne 0) { throw "Frontend tests failed." }
    Build-Frontend
}

switch ($Action) {
    "setup" {
        Install-Backend
        Install-Frontend
        Build-Frontend
    }
    "build" { Build-Frontend }
    "start" { Start-Console }
    "stop" { Stop-Console }
    "status" { Show-Status }
    "test" { Test-Console }
}
