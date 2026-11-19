# Re-install the Waggle plugin + startup launcher and restart the gateway so new code loads.
$env:HERMES_HOME = "$env:USERPROFILE\.hermes"
$h = "$env:HERMES_HOME\bin\hermes.exe"
Set-Location $PSScriptRoot
python install.py --cron | Select-String "plugin:|enable|persona:|cron"
& "$PSScriptRoot\install-startup.ps1" | Select-String "Installed"
& $h gateway stop --yes
# The Command Center is its own process; stop it so the launcher starts it on the new code.
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'waggle.dashboard\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
wscript "$PSScriptRoot\Waggle Gateway.vbs"
Start-Sleep -Seconds 20
& $h gateway status | Select-Object -First 2
Write-Host "== reloaded"
