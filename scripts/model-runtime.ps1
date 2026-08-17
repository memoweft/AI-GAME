[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('bootstrap', 'start', 'stop', 'status')]
    [string]$Action = 'status',

    [ValidateSet('Ubuntu')]
    [string]$Distribution = 'Ubuntu'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$resolvedRoot = (Resolve-Path -LiteralPath $projectRoot).Path
if ($resolvedRoot -ne 'F:\AI-GAME') {
    throw "Unexpected project root: $resolvedRoot"
}

$wslScriptRoot = '/mnt/f/AI-GAME/scripts/wsl'
$scriptName = switch ($Action) {
    'bootstrap' { 'bootstrap-gui-model.sh' }
    'start' { 'start-gui-model.sh' }
    'stop' { 'stop-gui-model.sh' }
    'status' { 'status-gui-model.sh' }
}

$arguments = @('-d', $Distribution, '--', 'bash', "$wslScriptRoot/$scriptName")

& wsl.exe @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Model runtime action '$Action' failed with exit code $LASTEXITCODE."
}

if ($Action -eq 'bootstrap') {
    $settings = @{}
    foreach ($line in Get-Content -LiteralPath (Join-Path $projectRoot 'config\model-runtime.env')) {
        if ($line -match '^([A-Z0-9_]+)=(.+)$') {
            $settings[$matches[1]] = $matches[2]
        }
    }
    $modelId = $settings['GUI_MODEL_ID']
    $revision = $settings['GUI_MODEL_REVISION']
    if ([string]::IsNullOrWhiteSpace($modelId) -or [string]::IsNullOrWhiteSpace($revision)) {
        throw 'The pinned GUI model ID or revision is missing from model-runtime.env.'
    }

    $modelRoot = Join-Path $projectRoot 'runtime\models'
    $modelDirectory = Join-Path $modelRoot $modelId
    New-Item -ItemType Directory -Path $modelDirectory -Force | Out-Null
    $env:UV_CACHE_DIR = Join-Path $projectRoot 'runtime\cache\uv-windows'
    $env:HF_HOME = Join-Path $projectRoot 'runtime\cache\huggingface-windows'
    $env:HF_XET_HIGH_PERFORMANCE = '1'

    & uv tool run --from 'huggingface-hub==1.27.0' hf download $modelId `
        --revision $revision `
        --local-dir $modelDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "Pinned model download failed with exit code $LASTEXITCODE."
    }
    Write-Host "Pinned model ready: $modelDirectory"
}
