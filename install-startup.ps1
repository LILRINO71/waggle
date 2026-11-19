$startup = [Environment]::GetFolderPath("Startup")
# Background: Ollama + Hermes gateway (Telegram, scheduled jobs), hidden.
Copy-Item -Force "$PSScriptRoot\Waggle Gateway.vbs" (Join-Path $startup "Waggle Gateway.vbs")
Write-Host "Installed: $(Join-Path $startup 'Waggle Gateway.vbs')"
# Foreground: the voice window ("hey jarvis" / Ctrl+B), started minimized.
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $startup "Waggle Voice.lnk"))
$lnk.TargetPath = Join-Path $PSScriptRoot "Waggle Voice.bat"
$lnk.WorkingDirectory = $PSScriptRoot
$lnk.WindowStyle = 7
$lnk.Description = "Waggle voice: say hey jarvis"
$lnk.Save()
Write-Host "Installed: $(Join-Path $startup 'Waggle Voice.lnk') (minimized)"
