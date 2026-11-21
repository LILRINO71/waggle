# Restart the Hermes gateway for real: stop EVERY running "hermes.exe gateway run" (with its python child), whoever
# started it, then start the "Waggle Gateway" task and wait until the new one logs "Gateway running".
# `hermes gateway stop` only stops a gateway it started itself: on 2026-11-20 it reported success while a gateway
# started by hand kept running the old plugin, so a deploy looked done and was not.
param([int]$TimeoutS = 120)
$ErrorActionPreference = "Stop"
# taskkill writes "not found" to stderr when a child already went with its parent: harmless, never fatal.
function Stop-Tree($id) { & cmd.exe /c "taskkill /PID $id /T /F >nul 2>&1" | Out-Null }
$hermesHome = Join-Path $env:USERPROFILE ".hermes"
$log = Join-Path $hermesHome "logs\gateway.log"

function Get-Gateways {
    Get-CimInstance Win32_Process -Filter "Name='hermes.exe' or Name='python.exe'" |
        Where-Object { $_.CommandLine -like "*gateway run*" -and $_.CommandLine -like "*$hermesHome*" }
}

$old = @(Get-Gateways)
$oldIds = $old | ForEach-Object { $_.ProcessId }
try { & (Join-Path $hermesHome "bin\hermes.exe") gateway stop --yes | Out-Null } catch {}
Start-Sleep -Seconds 3
foreach ($p in Get-Gateways) {
    if ($oldIds -contains $p.ProcessId) { Stop-Tree $p.ProcessId }
}
$startedAt = Get-Date
Start-ScheduledTask -TaskName "Waggle Gateway"
$deadline = $startedAt.AddSeconds($TimeoutS)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 3
    $new = @(Get-Gateways | Where-Object { $oldIds -notcontains $_.ProcessId })
    $running = Select-String -Path $log -Pattern "Gateway running" -SimpleMatch -ErrorAction SilentlyContinue |
        Where-Object { [datetime]::ParseExact($_.Line.Substring(0, 19), "yyyy-MM-dd HH:mm:ss", $null) -ge $startedAt.AddSeconds(-2) }
    if ($new.Count -gt 0 -and $running) {
        $stale = @(Get-Gateways | Where-Object { $oldIds -contains $_.ProcessId })
        if ($stale.Count) { Write-Output "WARNING: an old gateway is still running: $($stale.ProcessId -join ', ')"; exit 1 }
        Write-Output "gateway restarted: pid $($new[0].ProcessId)"
        exit 0
    }
}
Write-Output "gateway did not come back within $TimeoutS s"
exit 1
