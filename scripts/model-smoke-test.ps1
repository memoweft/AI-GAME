[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:4243/v1',
    [string]$ApiKey = 'local-gui-owl',
    [string]$Model = 'gui-owl-1.5-8b-instruct'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot 'runtime\screenshots'
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$imagePath = Join-Path $runtimeDir 'synthetic-ui-smoke.png'

Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap 800, 500
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
try {
    $graphics.Clear([System.Drawing.Color]::FromArgb(245, 247, 250))
    $titleFont = New-Object System.Drawing.Font('Segoe UI', 24, [System.Drawing.FontStyle]::Bold)
    $buttonFont = New-Object System.Drawing.Font('Segoe UI', 20, [System.Drawing.FontStyle]::Bold)
    $bodyFont = New-Object System.Drawing.Font('Segoe UI', 14)
    $graphics.DrawString('Workflow Console', $titleFont, [System.Drawing.Brushes]::Black, 45, 45)
    $graphics.DrawString('Choose the next operation.', $bodyFont, [System.Drawing.Brushes]::DimGray, 48, 100)
    $startRect = New-Object System.Drawing.Rectangle 500, 340, 210, 80
    $cancelRect = New-Object System.Drawing.Rectangle 90, 340, 210, 80
    $graphics.FillRectangle([System.Drawing.Brushes]::ForestGreen, $startRect)
    $graphics.FillRectangle([System.Drawing.Brushes]::Gray, $cancelRect)
    $graphics.DrawString('START', $buttonFont, [System.Drawing.Brushes]::White, 555, 362)
    $graphics.DrawString('CANCEL', $buttonFont, [System.Drawing.Brushes]::White, 135, 362)
    $bitmap.Save($imagePath, [System.Drawing.Imaging.ImageFormat]::Png)
}
finally {
    $graphics.Dispose()
    $bitmap.Dispose()
}

$bytes = [System.IO.File]::ReadAllBytes($imagePath)
$dataUrl = 'data:image/png;base64,' + [Convert]::ToBase64String($bytes)
$headers = @{ Authorization = "Bearer $ApiKey" }
$body = @{
    model = $Model
    temperature = 0
    max_tokens = 128
    messages = @(
        @{
            role = 'user'
            content = @(
                @{ type = 'image_url'; image_url = @{ url = $dataUrl } },
                @{ type = 'text'; text = 'You are the local GUI actor. Inspect the screenshot and propose the next single action to click the green START button. Return an action and normalized 0-1000 coordinates. Do not execute anything.' }
            )
        }
    )
} | ConvertTo-Json -Depth 8

$models = Invoke-RestMethod -Uri "$BaseUrl/models" -Headers $headers -Method Get -TimeoutSec 30
if (-not $models.data) {
    throw 'The model API is reachable but returned no loaded models.'
}
$response = Invoke-RestMethod -Uri "$BaseUrl/chat/completions" -Headers $headers -ContentType 'application/json' -Method Post -Body $body -TimeoutSec 180
$content = $response.choices[0].message.content
if ([string]::IsNullOrWhiteSpace($content)) {
    throw 'The model returned an empty action candidate.'
}

[pscustomobject]@{
    ApiReady = $true
    LoadedModel = $models.data[0].id
    TestImage = $imagePath
    ActionCandidate = $content
} | Format-List
