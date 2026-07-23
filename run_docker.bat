@echo off
chcp 65001 >nul 2>&1

cd /d "%~dp0"

echo 🚀 Launching Wan2GP Gateway via Docker Compose...
docker compose up -d --build

echo.
echo ✅ Gateway container is running!
echo 📡 Gateway API: http://localhost:50080
echo ⚙️  Settings UI: http://localhost:50080/
echo.
pause
