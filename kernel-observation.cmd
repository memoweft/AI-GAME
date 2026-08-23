@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\kernel-observation.ps1" %*
set "AI_GAME_OBSERVATION_EXIT=%ERRORLEVEL%"
if not "%AI_GAME_OBSERVATION_EXIT%"=="0" pause
exit /b %AI_GAME_OBSERVATION_EXIT%
