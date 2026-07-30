param(
    [switch]$OpenBrowser,
    [switch]$StartLocalModels
)

$ErrorActionPreference = "Stop"

$rootDir = $PSScriptRoot
$runtimeDir = Join-Path $rootDir ".runtime"
$comfyRuntime = "F:\SK2-runtime\ComfyUI_windows_portable"
$comfyPython = Join-Path $comfyRuntime "python_embeded\python.exe"
$comfyMain = Join-Path $comfyRuntime "ComfyUI\main.py"
$apiDir = Join-Path $rootDir "apps\api"
$apiPython = Join-Path $apiDir ".venv\Scripts\python.exe"
$webDir = Join-Path $rootDir "apps\web"
$ollamaPath = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
$envFile = Join-Path $rootDir ".env"

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            return
        }
        $parts = $line -split "=", 2
        if ($parts.Count -ne 2) {
            throw "Invalid environment variable entry in $envFile"
        }
        $name = $parts[0].Trim()
        $value = $parts[1].Trim()
        if (
            $value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        Set-Item -Path "Env:$name" -Value $value
    }
    Write-Host "Loaded local environment variables from .env."
}

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

function Test-ListeningPort {
    param([int]$Port)

    return [bool](Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

function Wait-ForPort {
    param(
        [string]$Name,
        [int]$Port,
        [int]$TimeoutSeconds = 90
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-ListeningPort -Port $Port) {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "$Name did not start on port $Port within $TimeoutSeconds seconds."
}

function Start-BackgroundService {
    param(
        [string]$Name,
        [int]$Port,
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory
    )

    if (Test-ListeningPort -Port $Port) {
        Write-Host "$Name is already running on port $Port."
        return
    }

    Write-Host "Starting $Name..."
    Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runtimeDir "$Name.stdout.log") `
        -RedirectStandardError (Join-Path $runtimeDir "$Name.stderr.log")
    Wait-ForPort -Name $Name -Port $Port
}

if (-not (Test-Path $apiPython)) {
    throw "API virtual environment was not found: $apiPython"
}
if (-not (Test-Path (Join-Path $rootDir "providers.json"))) {
    throw "Provider configuration was not found: $(Join-Path $rootDir 'providers.json')"
}

if ($StartLocalModels) {
    if (-not (Test-Path $comfyPython)) {
        throw "ComfyUI Python was not found: $comfyPython"
    }

    if (-not (Test-ListeningPort -Port 11434)) {
        if (-not (Test-Path $ollamaPath)) {
            $ollamaCommand = Get-Command ollama.exe -ErrorAction SilentlyContinue
            if ($null -eq $ollamaCommand) {
                throw "Ollama was not found. Install Ollama or start it before running this script."
            }
            $ollamaPath = $ollamaCommand.Source
        }
        Start-BackgroundService `
            -Name "ollama" `
            -Port 11434 `
            -FilePath $ollamaPath `
            -ArgumentList @("serve") `
            -WorkingDirectory $rootDir
    }
    else {
        Write-Host "ollama is already running on port 11434."
    }

    Start-BackgroundService `
        -Name "comfyui" `
        -Port 8188 `
        -FilePath $comfyPython `
        -ArgumentList @(
            $comfyMain,
            "--listen", "127.0.0.1",
            "--port", "8188",
            "--lowvram",
            "--disable-pinned-memory",
            "--extra-model-paths-config", (Join-Path $rootDir "comfy-models.yaml")
        ) `
        -WorkingDirectory (Join-Path $comfyRuntime "ComfyUI")
}
else {
    Write-Host "Local video model was not started. Run the launcher and choose Y when you need local generation."
}

Start-BackgroundService `
    -Name "api" `
    -Port 8000 `
    -FilePath $apiPython `
    -ArgumentList @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000") `
    -WorkingDirectory $apiDir

$npmCommand = Get-Command npm.cmd -ErrorAction SilentlyContinue
if ($null -eq $npmCommand) {
    throw "npm.cmd was not found. Install Node.js before starting the frontend."
}
Start-BackgroundService `
    -Name "web" `
    -Port 5173 `
    -FilePath $npmCommand.Source `
    -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "5173") `
    -WorkingDirectory $webDir

try {
    $health = Invoke-RestMethod "http://127.0.0.1:8000/api/health"
    Write-Host "API health: ComfyUI=$($health.services.comfyui), Ollama=$($health.services.ollama)"
}
catch {
    Write-Warning "Services started, but the API health check did not complete: $($_.Exception.Message)"
}

Write-Host ""
Write-Host "SK2 Advertising Studio: http://127.0.0.1:5173/"
Write-Host "Technical test workspace: http://127.0.0.1:5173/test"
Write-Host "Keep this window open to view startup messages. Closing it does not stop background services."
if ($OpenBrowser) {
    Start-Process "http://127.0.0.1:5173"
}
