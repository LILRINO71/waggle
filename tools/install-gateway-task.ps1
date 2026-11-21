<#
.SYNOPSIS
  Installs (or removes) the scheduled task that starts the Hermes gateway for Waggle at logon.

.DESCRIPTION
  "Waggle Gateway"  at logon of the current user: wscript "Waggle Gateway.vbs" (Ollama, the voice server, the
                    dashboard and the Hermes gateway, all hidden). The launcher exits once they are started; a
                    second gateway refuses to start because the first one holds a lock.
  A scheduled task is used instead of the Startup folder because files written to the Startup folder from inside
  a packaged app (the Claude desktop app is one) land in that app's private copy and never run at logon.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\install-gateway-task.ps1
  powershell -ExecutionPolicy Bypass -File tools\install-gateway-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$Task = "Waggle Gateway"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Launcher = Join-Path $Repo "Waggle Gateway.vbs"
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if ($Uninstall) {
    if (Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Task -Confirm:$false
        Write-Output "Removed: $Task (a running gateway is left alone; stop it with: hermes gateway stop --yes)"
    } else {
        Write-Output "Not installed: $Task"
    }
    return
}

if (-not (Test-Path $Launcher)) { throw "Launcher not found: $Launcher" }
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument ('"' + $Launcher + '"') -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
    -Description "Starts the Hermes gateway, voice server and dashboard for Waggle at logon" -Force | Out-Null
Write-Output "Registered: $Task"
if (-not $NoStart) {
    Start-ScheduledTask -TaskName $Task
    Write-Output "Started: $Task"
}
