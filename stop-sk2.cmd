@echo off
setlocal
title Stop SK2 Services

powershell.exe -NoLogo -NoExit -ExecutionPolicy Bypass -File "%~dp0stop-sk2.ps1"

endlocal
