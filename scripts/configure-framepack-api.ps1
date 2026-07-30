param(
    [string]$FramePackDir = "F:\SK2-runtime\FramePack"
)

$ErrorActionPreference = "Stop"

$demoFile = Join-Path $FramePackDir "demo_gradio.py"
if (-not (Test-Path $demoFile)) {
    throw "FramePack program was not found: $demoFile"
}

$content = Get-Content -Raw -Path $demoFile
if ($content -match 'start_button\.click\(.*api_name=["'']generate["'']') {
    Write-Host "FramePack API endpoint /generate is already configured."
    return
}

$original = 'start_button.click(fn=process, inputs=ips, outputs=[result_video, preview_image, progress_desc, progress_bar, start_button, end_button])'
$replacement = 'start_button.click(fn=process, inputs=ips, outputs=[result_video, preview_image, progress_desc, progress_bar, start_button, end_button], api_name="generate")'
if (-not $content.Contains($original)) {
    throw "Unsupported FramePack demo_gradio.py version. Could not configure the /generate API endpoint."
}

Set-Content -Path $demoFile -Value $content.Replace($original, $replacement) -Encoding utf8NoBOM
Write-Host "Configured FramePack API endpoint: /generate"
