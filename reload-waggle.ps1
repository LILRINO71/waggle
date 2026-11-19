# Re-install the Waggle plugin + startup launcher and restart the gateway so new code loads.
$env:HERMES_HOME = "$env:USERPROFILE\.hermes"
$h = "$env:HERMES_HOME\bin\hermes.exe"
Set-Location $PSScriptRoot
python install.py --cron | Select-String "plugin:|enable|persona:|cron"
& "$PSScriptRoot\install-startup.ps1" | Select-String "Installed"
& $h gateway stop --yes
Start-Sleep -Seconds 3
wscript "$PSScriptRoot\Waggle Gateway.vbs"
Start-Sleep -Seconds 20
& $h gateway status | Select-Object -First 2
Write-Host "== reloaded"
