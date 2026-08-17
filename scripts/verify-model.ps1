[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $projectRoot 'services\gui-model\model-manifest.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$modelRoot = Join-Path (Join-Path $projectRoot 'runtime\models') $manifest.modelId

$missing = [System.Collections.Generic.List[string]]::new()
$mismatches = [System.Collections.Generic.List[object]]::new()
[int64]$total = 0
foreach ($entry in $manifest.files) {
    $path = Join-Path $modelRoot $entry.path
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $missing.Add($entry.path)
        continue
    }
    $actual = (Get-Item -LiteralPath $path).Length
    $total += $actual
    if ($actual -ne [int64]$entry.bytes) {
        $mismatches.Add([pscustomobject]@{
            Path = $entry.path
            Expected = [int64]$entry.bytes
            Actual = $actual
        })
    }
}

if ($missing.Count -gt 0 -or $mismatches.Count -gt 0 -or $total -ne [int64]$manifest.expectedBytes) {
    $missing | ForEach-Object { Write-Error "Missing model file: $_" }
    $mismatches | Format-Table -AutoSize
    throw "Pinned model verification failed. Expected $($manifest.expectedBytes) bytes; found $total bytes."
}

[pscustomobject]@{
    ModelId = $manifest.modelId
    Revision = $manifest.revision
    Files = $manifest.files.Count
    Bytes = $total
    GiB = [math]::Round($total / 1GB, 3)
    SizeVerified = $true
} | Format-List
