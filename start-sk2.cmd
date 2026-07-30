@echo off
setlocal
title SK2 Advertising Studio Launcher

echo.
echo =============================================
echo   SK2 Advertising Studio
echo =============================================
echo.
echo Select services by number. Combine numbers, for example: 1235
echo   1 = API
echo   2 = Web frontend
echo   3 = ComfyUI local Wan
echo   4 = Ollama planning model
echo   5 = FramePack local long-video model
echo.
set /p SERVICE_SELECTION=Selection [12]:
if "%SERVICE_SELECTION%"=="" set "SERVICE_SELECTION=12"

powershell.exe -NoLogo -NoExit -ExecutionPolicy Bypass -File "%~dp0start-sk2.ps1" -Services "%SERVICE_SELECTION%"

endlocal
