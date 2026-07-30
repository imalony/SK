@echo off
setlocal
title SK2 Advertising Studio Stopper

powershell.exe -NoLogo -NoExit -ExecutionPolicy Bypass -File "%~dp0stop-sk2.ps1"

endlocal
