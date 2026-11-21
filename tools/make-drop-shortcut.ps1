<#
.SYNOPSIS
  Puts a "Feed Waggle" shortcut on the desktop that opens Waggle's Drop folder.

.DESCRIPTION
  Anything dropped into the Drop folder (PDF, DOCX, PPTX, TXT, MD, CSV, HTML, images) becomes a note in
  Waggle's Obsidian vault within a few minutes (waggle/dropbox_ingest.py). The shortcut is "Feed Waggle.lnk" with
  the standard folder icon, on the REAL desktop (resolved with [Environment]::GetFolderPath('Desktop'), which is
  under OneDrive on many PCs).

  The vault is found in this order: -Vault, the "vault" setting in ~/.waggle/config.json (WAGGLE_HOME if set),
  then ~/WaggleVault. The Drop folder is created when it is missing. Running the script twice just refreshes the
  shortcut. -WhatIf prints what would be done and changes nothing. -DesktopDir puts the shortcut somewhere else
  (for testing).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File tools\make-drop-shortcut.ps1 -WhatIf
  powershell -ExecutionPolicy Bypass -File tools\make-drop-shortcut.ps1
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$Vault = "",
    [string]$DesktopDir = "",
    [string]$Name = "Feed Waggle"
)

$ErrorActionPreference = "Stop"

function Get-WaggleVault {
    param([string]$Explicit)
    if ($Explicit) { return $Explicit }
    $home_dir = if ($env:WAGGLE_HOME) { $env:WAGGLE_HOME } else { Join-Path $env:USERPROFILE ".waggle" }
    $cfg = Join-Path $home_dir "config.json"
    if (Test-Path -LiteralPath $cfg) {
        try {
            $json = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($json.vault) { return [string]$json.vault }
        } catch {
            Write-Warning "Could not read $cfg ($($_.Exception.Message)); using the default vault."
        }
    }
    return (Join-Path $env:USERPROFILE "WaggleVault")
}

$vaultDir = Get-WaggleVault -Explicit $Vault
$dropDir = Join-Path $vaultDir "Drop"
$desktop = if ($DesktopDir) { $DesktopDir } else { [Environment]::GetFolderPath("Desktop") }
if (-not $desktop) { throw "Could not find the desktop folder." }
$lnk = Join-Path $desktop "$Name.lnk"
$icon = Join-Path $env:SystemRoot "System32\shell32.dll"

Write-Host "Vault:    $vaultDir"
Write-Host "Drop:     $dropDir"
Write-Host "Shortcut: $lnk"

if ($PSCmdlet.ShouldProcess($lnk, "create the shortcut to $dropDir")) {
    New-Item -ItemType Directory -Force -Path $dropDir | Out-Null
    New-Item -ItemType Directory -Force -Path $desktop | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    $sc = $shell.CreateShortcut($lnk)
    $sc.TargetPath = $dropDir
    $sc.WorkingDirectory = $dropDir
    $sc.IconLocation = "$icon,3"   # the standard folder icon
    $sc.Description = "Drop files here and Waggle files them in its vault"
    $sc.Save()
    [System.Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null
    Write-Host "Done. Double-click '$Name' on the desktop, drop files in, and Waggle files them within a few minutes."
}
