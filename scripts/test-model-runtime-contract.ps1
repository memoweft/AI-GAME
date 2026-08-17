[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$configPath = Join-Path $projectRoot 'config\model-runtime.env'
$settings = @{}
foreach ($line in Get-Content -LiteralPath $configPath) {
    if ($line -match '^([A-Z0-9_]+)=(.+)$') {
        $settings[$matches[1]] = $matches[2]
    }
}

$expectedModelDirectory = "$($settings['GUI_MODEL_ROOT'])/$($settings['GUI_MODEL_ID'])"
$expectations = @{
    'start-gui-model.sh' = @(
        'model_dir="$GUI_MODEL_ROOT/$model_id"',
        '"$cmdline" == *"$model_dir"*',
        'limit_mm_args="{\"image\":$GUI_MODEL_MAX_IMAGES}"',
        '--limit-mm-per-prompt "$limit_mm_args"',
        'export VLLM_USE_V2_MODEL_RUNNER=0',
        'export VLLM_USE_FLASHINFER_SAMPLER=0'
    )
    'status-gui-model.sh' = @(
        'model_dir="$GUI_MODEL_ROOT/$GUI_MODEL_ID"',
        '"$cmdline" == *"$model_dir"*'
    )
    'stop-gui-model.sh' = @(
        'model_dir="$GUI_MODEL_ROOT/$GUI_MODEL_ID"',
        '"$cmdline" != *"$model_dir"*'
    )
}

$failures = [System.Collections.Generic.List[string]]::new()
foreach ($entry in $expectations.GetEnumerator()) {
    $path = Join-Path $projectRoot "scripts\wsl\$($entry.Key)"
    $content = Get-Content -LiteralPath $path -Raw
    if ($content.Contains('/srv/ai-game/models/')) {
        $failures.Add("$($entry.Key) still contains the stale /srv/ai-game/models/ fingerprint.")
    }
    foreach ($requiredText in $entry.Value) {
        if (-not $content.Contains($requiredText)) {
            $failures.Add("$($entry.Key) is missing ownership check: $requiredText")
        }
    }
}

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ }
    throw 'GUI model lifecycle ownership contract failed.'
}

[pscustomobject]@{
    ExpectedModelDirectory = $expectedModelDirectory
    ScriptsChecked = $expectations.Count
    OwnershipContract = 'verified'
} | Format-List
