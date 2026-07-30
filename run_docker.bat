@echo off
chcp 65001 >nul 2>&1

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get_ips.ps1" > "%TEMP%\wan_ip_out.txt"
for /f "usebackq tokens=1,2 delims=|" %%a in ("%TEMP%\wan_ip_out.txt") do (
    set HOST_IP=%%a
    set AVAILABLE_IPS=%%b
)
del "%TEMP%\wan_ip_out.txt"

if "%HOST_IP%"=="" set HOST_IP=127.0.0.1
if "%AVAILABLE_IPS%"=="" set AVAILABLE_IPS=%HOST_IP%

echo [Wan2GP-Gateway] 選擇的 IP: %HOST_IP%
echo [Wan2GP-Gateway] 偵測到的IP 清單: %AVAILABLE_IPS%
echo.

> "%~dp0.env" echo HOST_IP=%HOST_IP%
>> "%~dp0.env" echo AVAILABLE_IPS=%AVAILABLE_IPS%
echo [Wan2GP-Gateway] 已將動態 IP 寫入 .env
echo.

echo 🚀 Launching Wan2GP Gateway via Docker Compose...
docker compose up -d

echo.
echo ✅ Gateway container is running!
echo 📡 Gateway API: http://localhost:58080
echo ⚙️  Settings UI: http://localhost:58080/
echo.
pause
