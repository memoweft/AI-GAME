@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Control\Scripts\console.ps1" stop -NoBrowser
if errorlevel 1 pause
