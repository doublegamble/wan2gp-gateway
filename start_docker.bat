@echo off
setlocal enabledelayedexpansion

:: 抓取 IPv4 位址並過濾空白
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
    set RAW_IP=%%a
    set HOST_IP=!RAW_IP: =!
)

if "%HOST_IP%"=="" (
    echo [Wan2GP Gateway] 無法自動偵測實體 IP，將使用預設值 127.0.0.1
    set HOST_IP=127.0.0.1
)

set WAN2GP_BASE_URL=http://%HOST_IP%:58080

echo [Wan2GP Gateway] ======================================
echo [Wan2GP Gateway] 偵測到實體區網 IP: %HOST_IP%
echo [Wan2GP Gateway] 自動設定註冊 URL: %WAN2GP_BASE_URL%
echo [Wan2GP Gateway] ======================================
echo.
echo [Wan2GP Gateway] 正在啟動 Docker 容器...
docker compose up -d --build
