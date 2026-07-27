$ips = @(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'vEthernet|WSL|Loopback|Virtual|Tailscale|ZeroTier' -and $_.IPAddress -notmatch '^169\.254\.' -and $_.IPAddress -notmatch '^127\.' } | Select-Object -ExpandProperty IPAddress)
$avail = $ips -join ','
$default = if ($ips.Count -gt 0) { $ips[0] } else { '127.0.0.1' }
Write-Output "$default|$avail"
