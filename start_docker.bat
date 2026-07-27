@echo off
setlocal enabledelayedexpansion

:: 建立暫存的 PowerShell 腳本以進行多重網卡互動選擇
set PS_SCRIPT=%TEMP%\get_ip.ps1
echo $ips = @(Get-NetIPAddress -AddressFamily IPv4 ^| Where-Object { $_.InterfaceAlias -notmatch 'vEthernet^|WSL^|Loopback^|Virtual^|Tailscale^|ZeroTier' -and $_.IPAddress -notmatch '^^169\.254\.' -and $_.IPAddress -notmatch '^^127\.' }) > "%PS_SCRIPT%"
echo if ($ips.Count -eq 1) { >> "%PS_SCRIPT%"
echo     Write-Output $ips[0].IPAddress >> "%PS_SCRIPT%"
echo } elseif ($ips.Count -gt 1) { >> "%PS_SCRIPT%"
echo     Write-Host "========================================" -ForegroundColor Cyan >> "%PS_SCRIPT%"
echo     Write-Host "偵測到多個實體網路介面，請選擇要綁定的對外 IP：" -ForegroundColor Cyan >> "%PS_SCRIPT%"
echo     for ($i=0; $i -lt $ips.Count; $i++) { >> "%PS_SCRIPT%"
echo         Write-Host "[$($i+1)] $($ips[$i].IPAddress) ($($ips[$i].InterfaceAlias))" -ForegroundColor Yellow >> "%PS_SCRIPT%"
echo     } >> "%PS_SCRIPT%"
echo     Write-Host "========================================" -ForegroundColor Cyan >> "%PS_SCRIPT%"
echo     $choice = 0 >> "%PS_SCRIPT%"
echo     while ($choice -lt 1 -or $choice -gt $ips.Count) { >> "%PS_SCRIPT%"
echo         $choice = [int](Read-Host "請輸入編號 (1-$($ips.Count))") >> "%PS_SCRIPT%"
echo     } >> "%PS_SCRIPT%"
echo     Write-Output $ips[$choice-1].IPAddress >> "%PS_SCRIPT%"
echo } else { >> "%PS_SCRIPT%"
echo     Write-Output "127.0.0.1" >> "%PS_SCRIPT%"
echo } >> "%PS_SCRIPT%"

:: 執行腳本並擷取結果
for /f "usebackq tokens=*" %%a in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%"`) do (
    set HOST_IP=%%a
)

:: 清理暫存檔
del "%PS_SCRIPT%"

if "%HOST_IP%"=="" set HOST_IP=127.0.0.1
set WAN2GP_BASE_URL=http://%HOST_IP%:58080
echo [Wan2GP Gateway] 最終綁定 IP: %HOST_IP%
docker compose up -d --build
