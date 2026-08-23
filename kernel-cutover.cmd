@echo off
setlocal
cd /d "%~dp0"

echo AI-GAME U7 Kernel cutover
echo.
echo 1. Status
echo 2. Enter draining mode
echo 3. Snapshot Legacy database
echo 4. Exercise snapshot restore on a controlled copy
echo 5. Activate Kernel
echo 6. Roll back to Legacy
echo.
choice /c 123456 /n /m "Choose [1-6]: "
if errorlevel 6 (
  set "CUTOVER_ACTION=rollback"
  goto run
)
if errorlevel 5 (
  set "CUTOVER_ACTION=activate"
  goto run
)
if errorlevel 4 (
  set "CUTOVER_ACTION=restore-check"
  goto run
)
if errorlevel 3 (
  set "CUTOVER_ACTION=snapshot"
  goto run
)
if errorlevel 2 (
  set "CUTOVER_ACTION=drain"
  goto run
)
set "CUTOVER_ACTION=status"

:run
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\kernel-cutover.ps1" "%CUTOVER_ACTION%"
if errorlevel 1 pause
