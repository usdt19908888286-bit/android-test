@echo off
setlocal
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "Cloud Android Manager" pythonw "%~dp0cloud_phone_manager.py"
    exit /b 0
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "Cloud Android Manager" python "%~dp0cloud_phone_manager.py"
    exit /b 0
)

echo Python was not found in PATH.
pause
exit /b 1
