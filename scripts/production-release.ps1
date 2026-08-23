[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet("build", "verify")]
    [string]$Action = "build",

    [string]$OutputDirectory,

    [string]$ArchivePath
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$ConsoleScript = Join-Path $PSScriptRoot "console.ps1"
$PythonExe = Join-Path $ProjectRoot "runtime\envs\console\Scripts\python.exe"

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $ProjectRoot "runtime\releases"
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Console Python environment is missing. Run scripts\console.ps1 setup first."
}

switch ($Action) {
    "build" {
        # The release builder independently refuses a stale browser bundle.
        # Build first so the candidate contains the exact current frontend.
        & $ConsoleScript build -NoBrowser
        if ($LASTEXITCODE -ne 0) { throw "Browser console build failed." }
        & $PythonExe -m ai_game_console.production_release build `
            --project-root $ProjectRoot --output-dir $OutputDirectory
        if ($LASTEXITCODE -ne 0) { throw "Release candidate build failed." }
    }
    "verify" {
        if ([string]::IsNullOrWhiteSpace($ArchivePath)) {
            throw "Verify requires -ArchivePath pointing to one .zip release candidate."
        }
        & $PythonExe -m ai_game_console.production_release verify --archive $ArchivePath
        if ($LASTEXITCODE -ne 0) { throw "Release candidate verification failed." }
    }
}
