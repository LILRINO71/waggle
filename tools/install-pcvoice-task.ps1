<#
.SYNOPSIS
  Installs (or removes) the scheduled task that starts Waggle's PC voice at logon.

.DESCRIPTION
  "Waggle PC Voice"  at logon of the current user: pythonw -m waggle.pcvoice run
                     windowless (pythonw, and the process itself starts no child processes); restart on failure
                     every 1 minute, 999 times; never a second instance (the task ignores a second start and the
                     process refuses one through its own lock file); no time limit.
  It runs in the checkout this script lives in and needs the Waggle core (its POST /voice) to answer.
  -WhatIf prints what would be registered and changes nothing. -Uninstall stops the process and removes the task.
  Switching it off without uninstalling: python -m waggle.pcvoice stop (it starts again at the next logon).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\install-pcvoice-task.ps1 -WhatIf
  powershell -ExecutionPolicy Bypass -File tools\install-pcvoice-task.ps1
  powershell -ExecutionPolicy Bypass -File tools\install-pcvoice-task.ps1 -Uninstall
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Uninstall,
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "Waggle PC Voice"
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

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Output "Not installed: $TaskName"
    } elseif ($PSCmdlet.ShouldProcess($TaskName, "Stop and unregister scheduled task")) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output "Removed: $TaskName"
    }
    if ($PSCmdlet.ShouldProcess("Waggle PC voice", "Stop the running process")) {
        $exe = Find-Pythonw
        Push-Location $Repo
        try { & ($exe -replace 'pythonw\.exe$', 'python.exe') -m waggle.pcvoice stop } finally { Pop-Location }
    }
    return
}

$exe = Find-Pythonw
$taskArgs = "-m waggle.pcvoice run"
$plan = "RestartCount 999, RestartInterval 1 minute, MultipleInstances IgnoreNew, no execution time limit, runs on battery"

if ($WhatIfPreference) {
    Write-Output "WhatIf: nothing will be registered. Plan:"
    Write-Output "Task:              $TaskName"
    Write-Output "  User:            $User (at logon, interactive, limited rights)"
    Write-Output "  Program:         $exe"
    Write-Output "  Arguments:       $taskArgs"
    Write-Output "  Start in:        $Repo"
    Write-Output "  Trigger:         At logon of $User"
    Write-Output "  Settings:        $plan"
}

$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction -Execute $exe -Argument $taskArgs -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task")) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $set `
        -Principal $principal -Description "Waggle's voice at the PC: hotkey, wake word, spoken answers" -Force | Out-Null
    Write-Output "Registered: $TaskName"
}
if ($PSCmdlet.ShouldProcess($TaskName, "Start the PC voice now")) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Output "Started: $TaskName (check with: python -m waggle.pcvoice status)"
}
