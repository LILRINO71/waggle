<#
.SYNOPSIS
  Installs (or removes) the scheduled task that starts ActivityWatch at logon, hidden.

.DESCRIPTION
  "ActivityWatch" at logon of the current user: wscript "ActivityWatch Launcher.vbs", which starts
  %LOCALAPPDATA%\Programs\ActivityWatch\aw-qt.exe with no window (aw-qt then starts the server, the window
  watcher and the AFK watcher). A scheduled task is used instead of the Startup folder because files written
  to the Startup folder from inside a packaged app (the Claude desktop app is one) land in that app's private
  copy and never run at logon. The same reason makes the task the right way to START it now: a process started
  from the app's shell would keep its database in the app's private copy of AppData.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\install-activitywatch-task.ps1
  powershell -ExecutionPolicy Bypass -File tools\install-activitywatch-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$Task = "ActivityWatch"
$Source = Join-Path $PSScriptRoot "ActivityWatch Launcher.vbs"
$Launcher = Join-Path $env:USERPROFILE ".waggle\ActivityWatch Launcher.vbs"  # a stable copy: the task must not depend on a worktree
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Task -Confirm:$false
        Write-Output "Removed: $Task (a running ActivityWatch is left alone; quit it from its tray icon)"
    } else {
        Write-Output "Not installed: $Task"
    }
    return
}

if (-not (Test-Path $Source)) { throw "Launcher not found: $Source" }
New-Item -ItemType Directory -Force (Split-Path $Launcher) | Out-Null
Copy-Item -Force $Source $Launcher
$exe = Join-Path $env:LOCALAPPDATA "Programs\ActivityWatch\aw-qt.exe"
if (-not (Test-Path $exe)) { throw "ActivityWatch is not installed at $exe (see docs/ACTIVITY.md)" }
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"' + $Launcher + '"') -WorkingDirectory (Split-Path $Launcher)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Starts ActivityWatch (window and AFK tracking, local only) at logon, hidden" -Force | Out-Null
Write-Output "Registered: $Task"
if (-not $NoStart) {
    Start-ScheduledTask -TaskName $Task
    Write-Output "Started: $Task"
}
