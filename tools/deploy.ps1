# Deploy the dev branch to the live checkout and restart EVERY long-lived Waggle process, then prove it works.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\deploy.ps1 [-Branch overnight] [-SkipCanary]
#
# Run it from anywhere; it works on the live checkout (the folder this script's repo lives in when run from the
# live checkout, else C:\Users\<you>\Claude\waggle). The full test suite must already have passed on the branch.
#
# Why a script: long-lived processes keep the code they started with. On 2026-11-20 the watchdog was still running
# that morning's code at night (it knew nothing of the off switch) and `hermes gateway stop` missed a gateway started
# by hand, so a "deployed" plug-in was not running. Every process that imports Waggle's code is restarted here:
# the core, the watchdog, PC voice, the dashboard and the Hermes gateway (which also starts the dashboard).
param([string]$Branch = "overnight", [switch]$SkipCanary)
$ErrorActionPreference = "Stop"
# taskkill writes "not found" to stderr when a child already went with its parent: harmless, never fatal.
function Stop-Tree($id) { & cmd.exe /c "taskkill /PID $id /T /F >nul 2>&1" | Out-Null }
$live = Join-Path $env:USERPROFILE "Claude\waggle"
Set-Location $live

function Step($m) { Write-Output "== $m" }
function Run($exe, [string[]]$a) { & $exe @a; if ($LASTEXITCODE) { throw "$exe $($a -join ' ') failed ($LASTEXITCODE)" } }

Step "merge $Branch into main"
$before = (& git rev-parse HEAD).Trim()
Run git @("fetch", "-q", "origin")
Run git @("merge", "--ff-only", "-q", "origin/$Branch")
Run git @("push", "-q", "origin", "main")
$after = (& git rev-parse HEAD).Trim()
Write-Output "main: $($before.Substring(0,7)) -> $($after.Substring(0,7))"
$changed = @(& git diff --name-only $before $after)

if ($changed | Where-Object { $_ -like "hermes_plugin/*" -or $_ -like "skills/*" -or $_ -like "persona/*" -or $_ -eq "install.py" }) {
    Step "install the Hermes plug-in and Waggle's skills"
    Run python @("install.py")
}

Step "safety manifest"
Run py @("-3.14", "-m", "waggle.guard", "approve", "--yes")
$verify = (& py -3.14 -m waggle.guard verify) -join "`n"
if ($verify.Trim() -ne "clean") { throw "guard verify is not clean: $verify" }

Step "restart the watchdog first (it must know the new code before the core goes down)"
Stop-ScheduledTask -TaskName "Waggle Core Watchdog" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*waggle.core watch*" } | ForEach-Object { Stop-Tree $_.ProcessId }
Start-ScheduledTask -TaskName "Waggle Core Watchdog"

Step "restart the core"
Run py @("-3.14", "-m", "waggle.core", "restart")

Step "restart PC voice"
Stop-ScheduledTask -TaskName "Waggle PC Voice" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*waggle.pcvoice*" } | ForEach-Object { Stop-Tree $_.ProcessId }
$off = Test-Path (Join-Path $env:USERPROFILE ".waggle\OFF")
if (-not $off) { Start-ScheduledTask -TaskName "Waggle PC Voice" }

Step "restart the dashboard and the gateway"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' or Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*waggle\dashboard.py*" } | ForEach-Object { Stop-Tree $_.ProcessId }
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $live "tools\restart-gateway.ps1")
if ($LASTEXITCODE) { throw "gateway restart failed" }

Step "status"
$deadline = (Get-Date).AddSeconds(90)
do { Start-Sleep -Seconds 3; $st = (& py -3.14 -m waggle.core status) -join "`n" } until ($st -match "Waggle core: healthy" -or (Get-Date) -gt $deadline)
$st -split "`n" | Select-Object -First 3
if ($st -notmatch "Waggle core: healthy") { throw "core is not healthy after the restart" }
if ($st -notmatch "version $($after.Substring(0,7))") { throw "core does not report version $($after.Substring(0,7))" }

if (-not $SkipCanary) {
    Step "live check (canary: code path through Telegram, no reminder, no Claude call)"
    & py -3.14 -m waggle.canary run --no-reminder --no-alert --code-path
    if ($LASTEXITCODE) { throw "the live canary failed: Waggle did not answer through Telegram" }
}
Write-Output "deployed $($after.Substring(0,7))"
