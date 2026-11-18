$startup = [Environment]::GetFolderPath("Startup")
Copy-Item -Force "$PSScriptRoot\Waggle Gateway.vbs" (Join-Path $startup "Waggle Gateway.vbs")
Write-Host "Installed: $(Join-Path $startup 'Waggle Gateway.vbs')"
Get-ChildItem $startup | Select-Object Name
