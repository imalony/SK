@echo off
setlocal
title SK2 Advertising Studio Launcher

echo.
echo =============================================
echo   SK2 Advertising Studio
echo =============================================
echo.
set /p START_LOCAL=Start local video model (ComfyUI and Ollama)? [y/N]:

set "MODEL_SWITCH="
if /I "%START_LOCAL%"=="Y" set "MODEL_SWITCH=-StartLocalModels"
if /I "%START_LOCAL%"=="YES" set "MODEL_SWITCH=-StartLocalModels"

powershell.exe -NoLogo -NoExit -ExecutionPolicy Bypass -File "%~dp0start-sk2.ps1" %MODEL_SWITCH%

endlocal
