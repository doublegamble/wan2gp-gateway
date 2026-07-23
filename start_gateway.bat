@echo off
chcp 65001 >nul 2>&1

:: Switch to app root directory
cd /d "%~dp0.."

:: Activate Python venv
if exist env\Scripts\activate.bat (
    call env\Scripts\activate.bat
)

:: Clear HF_ENDPOINT
set HF_ENDPOINT=

echo.
echo ========================================
echo   Wan2GP Gateway API (Standalone)
echo   Port: 50080
echo   Press Ctrl+C to stop (frees GPU memory)
echo ========================================
echo.

python gateway\gateway.py

pause
