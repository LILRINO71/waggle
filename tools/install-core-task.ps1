<#
.SYNOPSIS
  Installs (or removes) the scheduled tasks that keep the Waggle core running.

.DESCRIPTION
  "Waggle Core"           at logon of the current user: pythonw -m waggle.core run
                          restart on failure every 1 minute, 999 times; never a second instance; no time limit.
  "Waggle Core Watchdog"  at logon: pythonw -m waggle.core watch --loop
                          ONE long-lived process that checks the core every 10 s and sleeps in between, so no
                          process starts every minute (a start can flash a window, show the busy cursor or take the
                          focus). Starts the core when it is down, restarts it when its heartbeat is over 50 s old
                          or it asked for a restart (a hung core is replaced in about 60-85 s). Task Scheduler
                          restarts the watchdog if it dies (1 minute, 999 times) and tries to start it every 15
                          minutes as a backstop; a second instance is ignored by the task and refused by the
                          watchdog's own lock, so there is never a second watchdog or a second core.
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
        try {
            & ($exe -replace 'pythonw\.exe$', 'python.exe') -m waggle.core watch --stop
            & ($exe -replace 'pythonw\.exe$', 'python.exe') -m waggle.core stop
        } finally { Pop-Location }
    }
    return
}

$exe = Find-Pythonw
$coreArgs = "-m waggle.core run"
$watchArgs = "-m waggle.core watch --loop"
$coreSettings = "RestartCount 999, RestartInterval 1 minute, MultipleInstances IgnoreNew, no execution time limit, runs on battery"
$watchSettings = "long-lived, MultipleInstances IgnoreNew, no execution time limit, restart on failure every 1 minute 999 times, start attempt every 15 minutes as a backstop"

if ($WhatIfPreference) {
    Write-Output "WhatIf: nothing will be registered. Plan:"
    Show-Plan $CoreTask $exe $coreArgs "At logon of $User" $coreSettings
    Show-Plan $WatchTask $exe $watchArgs "At logon of $User, then a start attempt every 15 minutes (ignored while it runs)" $watchSettings
}

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

$coreAction = New-ScheduledTaskAction -Execute $exe -Argument $coreArgs -WorkingDirectory $Repo
$coreTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$coreSet = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable

$watchAction = New-ScheduledTaskAction -Execute $exe -Argument $watchArgs -WorkingDirectory $Repo
$watchLogon = New-ScheduledTaskTrigger -AtLogOn -User $User
$watchRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 15)
if ($WhatIfPreference) {
    $dur = $watchRepeat.Repetition.Duration
    if (-not $dur) { $dur = "indefinite" }
    Write-Output "  Repetition:      interval $($watchRepeat.Repetition.Interval), duration $dur"
}
$watchSet = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable

if ($PSCmdlet.ShouldProcess($CoreTask, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $CoreTask -Action $coreAction -Trigger $coreTrigger -Settings $coreSet `
        -Principal $principal -Description "Waggle core process (runtime store, jobs, timers, outbox, health)" -Force | Out-Null
    Write-Output "Registered: $CoreTask"
}
if ($PSCmdlet.ShouldProcess($WatchTask, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $WatchTask -Action $watchAction -Trigger @($watchLogon, $watchRepeat) `
        -Settings $watchSet -Principal $principal -Description "Long-lived watchdog: restarts the Waggle core when it is down or hung" -Force | Out-Null
    Write-Output "Registered: $WatchTask"
}
if ($PSCmdlet.ShouldProcess($CoreTask, "Start the core now")) {
    Start-ScheduledTask -TaskName $CoreTask
    Write-Output "Started: $CoreTask (check with: python -m waggle.core status)"
}
if ($PSCmdlet.ShouldProcess($WatchTask, "Start the watchdog now")) {
    Start-ScheduledTask -TaskName $WatchTask
    Write-Output "Started: $WatchTask (python -m waggle.core status shows 'watchdog: running')"
}
