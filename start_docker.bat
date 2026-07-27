@echo off
setlocal enabledelayedexpansion

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0get_ips.ps1" > "%TEMP%\ip_out.txt"
for /f "usebackq tokens=1,2 delims=|" %%a in ("%TEMP%\ip_out.txt") do (
    set HOST_IP=%%a
    set AVAILABLE_IPS=%%b
)
del "%TEMP%\ip_out.txt"

if "%HOST_IP%"=="" set HOST_IP=127.0.0.1
if "%AVAILABLE_IPS%"=="" set AVAILABLE_IPS=%HOST_IP%

set WAN2GP_BASE_URL=http://%HOST_IP%:58080
echo [Wan2GP Gateway] ?????? IP: %HOST_IP%
echo [Wan2GP Gateway] ?????IP ???: %AVAILABLE_IPS%
docker compose up -d --build
