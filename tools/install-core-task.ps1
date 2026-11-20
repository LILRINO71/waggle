<#
.SYNOPSIS
  Installs (or removes) the scheduled tasks that keep the Waggle core running.

.DESCRIPTION
  "Waggle Core"           at logon of the current user: pythonw -m waggle.core run
                          restart on failure every 1 minute, 999 times; never a second instance; no time limit.
  "Waggle Core Watchdog"  every 5 minutes (and at logon): pythonw -m waggle.core watch
                          starts the core when it is down, restarts it when its heartbeat is stale or it asked
                          for a restart.
  Both run in the checkout this script lives in. -WhatIf prints what would be registered and changes nothing.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\install-core-task.ps1 -WhatIf
  powershell -ExecutionPolicy Bypass -File tools\install-core-task.ps1
  powershell -ExecutionPolicy Bypass -File tools\install-core-task.ps1 -Uninstall
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Uninstall,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$CoreTask = "Waggle Core"
$WatchTask = "Waggle Core Watchdog"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Find-Pythonw {
    if ($Python) { return $Python }
    $exe = $null
    try { $exe = (& py -3.14 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1) } catch {}
    if (-not $exe) {
        $cmd = Get-Command python -ErrorAction SilentlyContinue
        if ($cmd) { $exe = $cmd.Source }
    }
    if (-not $exe) { throw "No Python found; pass -Python <path to pythonw.exe>" }
    $w = Join-Path (Split-Path $exe) "pythonw.exe"
    if (Test-Path $w) { return $w }
    return $exe
}

function Show-Plan($name, $exe, $arguments, $trigger, $settings) {
    Write-Output "Task:              $name"
    Write-Output "  User:            $User (at logon, interactive, limited rights)"
    Write-Output "  Program:         $exe"
    Write-Output "  Arguments:       $arguments"
    Write-Output "  Start in:        $Repo"
    Write-Output "  Trigger:         $trigger"
    Write-Output "  Settings:        $settings"
}

if ($Uninstall) {
    foreach ($name in @($WatchTask, $CoreTask)) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if (-not $existing) { Write-Output "Not installed: $name"; continue }
        if ($PSCmdlet.ShouldProcess($name, "Stop and unregister scheduled task")) {
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Output "Removed: $name"
        }
    }
    if ($PSCmdlet.ShouldProcess("Waggle core", "Stop the running core")) {
        $exe = Find-Pythonw
        Push-Location $Repo
        try { & ($exe -replace 'pythonw\.exe$', 'python.exe') -m waggle.core stop } finally { Pop-Location }
    }
    return
}

$exe = Find-Pythonw
$coreArgs = "-m waggle.core run"
$watchArgs = "-m waggle.core watch"
$coreSettings = "RestartCount 999, RestartInterval 1 minute, MultipleInstances IgnoreNew, no execution time limit, runs on battery"
$watchSettings = "every 5 minutes indefinitely and at logon, MultipleInstances IgnoreNew, 2 minute time limit"

if ($WhatIfPreference) {
    Write-Output "WhatIf: nothing will be registered. Plan:"
    Show-Plan $CoreTask $exe $coreArgs "At logon of $User" $coreSettings
    Show-Plan $WatchTask $exe $watchArgs "At logon of $User, then every 5 minutes" $watchSettings
}

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

$coreAction = New-ScheduledTaskAction -Execute $exe -Argument $coreArgs -WorkingDirectory $Repo
$coreTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$coreSet = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable

$watchAction = New-ScheduledTaskAction -Execute $exe -Argument $watchArgs -WorkingDirectory $Repo
$watchLogon = New-ScheduledTaskTrigger -AtLogOn -User $User
$watchRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
if ($WhatIfPreference) {
    $dur = $watchRepeat.Repetition.Duration
    if (-not $dur) { $dur = "indefinite" }
    Write-Output "  Repetition:      interval $($watchRepeat.Repetition.Interval), duration $dur"
}
$watchSet = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

if ($PSCmdlet.ShouldProcess($CoreTask, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $CoreTask -Action $coreAction -Trigger $coreTrigger -Settings $coreSet `
        -Principal $principal -Description "Waggle core process (runtime store, jobs, timers, outbox, health)" -Force | Out-Null
    Write-Output "Registered: $CoreTask"
}
if ($PSCmdlet.ShouldProcess($WatchTask, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $WatchTask -Action $watchAction -Trigger @($watchLogon, $watchRepeat) `
        -Settings $watchSet -Principal $principal -Description "Restarts the Waggle core when it is down or hung" -Force | Out-Null
    Write-Output "Registered: $WatchTask"
}
if ($PSCmdlet.ShouldProcess($CoreTask, "Start the core now")) {
    Start-ScheduledTask -TaskName $CoreTask
    Write-Output "Started: $CoreTask (check with: python -m waggle.core status)"
}
