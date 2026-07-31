param(
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

function Get-ListeningProcessIds {
    param([int]$Port)

    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

function Stop-ServiceOnPort {
    param(
        [string]$Name,
        [int]$Port
    )

    $processIds = Get-ListeningProcessIds -Port $Port
    if ($processIds.Count -eq 0) {
        Write-Host "$Name is not running on port $Port."
        return
    }

    if ($WhatIf) {
        Write-Host "Would stop $Name on port $Port (PID: $($processIds -join ', '))."
        return
    }

    Write-Host "Stopping $Name on port $Port..."
    foreach ($processId in $processIds) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

function Get-ManagedModelProcesses {
    $processes = Get-CimInstance Win32_Process
    return @(
        $processes | Where-Object {
            $commandLine = $_.CommandLine
            $commandLine -and (
                $commandLine -match '(?i)ComfyUI[\\/].*main\.py' -or
                $commandLine -match '(?i)ollama\.exe"\s+serve' -or
                $commandLine -match '(?i)ollama\s+app\.exe'
            )
        }
    )
}

function Stop-ManagedModelProcesses {
    $processes = Get-ManagedModelProcesses
    if ($processes.Count -eq 0) {
        Write-Host "No managed local model processes were found."
        return
    }

    $processIds = @($processes | Select-Object -ExpandProperty ProcessId -Unique)
    if ($WhatIf) {
        Write-Host "Would stop managed local model processes (PID: $($processIds -join ', '))."
        return
    }

    Write-Host "Stopping managed local model processes..."
    foreach ($processId in $processIds) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
}

if ($WhatIf) {
    Write-Host "WhatIf mode: no service will be stopped."
}
else {
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/stop" |
            Out-Null
        Write-Host "Requested API task cancellation and model release."
    }
    catch {
        Write-Warning "The API stop endpoint was unavailable: $($_.Exception.Message)"
    }
}

Stop-ServiceOnPort -Name "web" -Port 5173
Stop-ServiceOnPort -Name "api" -Port 8000
Stop-ServiceOnPort -Name "comfyui" -Port 8188
Stop-ServiceOnPort -Name "ollama" -Port 11434
Stop-ManagedModelProcesses

if (-not $WhatIf) {
    Write-Host "SK2 frontend, API, ComfyUI, and Ollama services stopped."
}
