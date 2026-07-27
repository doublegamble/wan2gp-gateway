@echo off
setlocal enabledelayedexpansion
:: ?? PowerShell ?????????? (0.0.0.0) ??????? IP
for /f "usebackq tokens=*" %%a in (`powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex (Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Select-Object -First 1).InterfaceIndex).IPAddress"`) do (
    set HOST_IP=%%a
)

if "!HOST_IP!"=="" set HOST_IP=127.0.0.1
set WAN2GP_BASE_URL=http://!HOST_IP!:58080
echo [Wan2GP Gateway] ????????? IP: !HOST_IP!
docker compose up -d --build
