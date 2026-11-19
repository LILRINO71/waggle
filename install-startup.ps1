$startup = [Environment]::GetFolderPath("Startup")
# Background: Ollama + Hermes gateway (Telegram, scheduled jobs), hidden.
Copy-Item -Force "$PSScriptRoot\Waggle Gateway.vbs" (Join-Path $startup "Waggle Gateway.vbs")
Write-Host "Installed: $(Join-Path $startup 'Waggle Gateway.vbs')"
# Foreground: the voice window ("waggle" / Ctrl+B), started minimized.
$lnk = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $startup "Waggle Voice.lnk"))
$lnk.TargetPath = Join-Path $PSScriptRoot "Waggle Voice.bat"
$lnk.WorkingDirectory = $PSScriptRoot
$lnk.WindowStyle = 7
$lnk.Description = "Waggle voice: say waggle"
$lnk.Save()
Write-Host "Installed: $(Join-Path $startup 'Waggle Voice.lnk') (minimized)"
# Desktop + Start menu "Waggle": brings the voice window up (or starts it). Ctrl+Alt+W from anywhere.
$icon = Join-Path $env:USERPROFILE ".hermes\bin\hermes.exe"
$programs = [Environment]::GetFolderPath("Programs")
foreach ($dir in @([Environment]::GetFolderPath("Desktop"), $programs)) {
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $dir "Waggle.lnk"))
    $s.TargetPath = "$env:WINDIR\System32\wscript.exe"
    $s.Arguments = "`"$PSScriptRoot\Open Waggle.vbs`""
    $s.WorkingDirectory = $PSScriptRoot
    if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
    $s.Description = "Open Waggle (Ctrl+Alt+W)"
    if ($dir -eq $programs) { $s.Hotkey = "Ctrl+Alt+W" }  # one hotkey owner, or Windows ignores both
    $s.Save()
    Write-Host "Installed: $(Join-Path $dir 'Waggle.lnk')"
}
