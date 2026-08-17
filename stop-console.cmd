@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\console.ps1" stop -NoBrowser
if errorlevel 1 pause

