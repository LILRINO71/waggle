# Installs Obsidian (free, via winget), registers ~/WaggleVault as a vault, and opens it.
# Safe to re-run.
$vault = "$env:USERPROFILE\WaggleVault"
$exe = "$env:LOCALAPPDATA\Programs\Obsidian\Obsidian.exe"

if (-not (Test-Path $exe)) {
    Write-Host "Installing Obsidian..."
    winget install -e --id Obsidian.Obsidian --accept-source-agreements --accept-package-agreements --silent
}
if (-not (Test-Path $exe)) { Write-Host "Obsidian install failed."; exit 1 }

# Register the vault so Obsidian opens straight into it instead of the vault picker.
$cfgDir = "$env:APPDATA\obsidian"
$cfg = "$cfgDir\obsidian.json"
New-Item -ItemType Directory -Force $cfgDir | Out-Null
$data = if (Test-Path $cfg) { Get-Content $cfg -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
if (-not $data.vaults) { $data | Add-Member -NotePropertyName vaults -NotePropertyValue ([pscustomobject]@{}) -Force }
$known = $data.vaults.PSObject.Properties | Where-Object { $_.Value.path -eq $vault }
if (-not $known) {
    $id = -join ((48..57) + (97..102) | Get-Random -Count 16 | ForEach-Object { [char]$_ })
    $ts = [DateTimeOffset]::Now.ToUnixTimeMilliseconds()
    $data.vaults | Add-Member -NotePropertyName $id -NotePropertyValue ([pscustomobject]@{ path = $vault; ts = $ts; open = $true })
    [IO.File]::WriteAllText($cfg, ($data | ConvertTo-Json -Depth 5))  # no BOM: Obsidian's JSON parser rejects it
    Write-Host "Registered vault: $vault"
}

Start-Process ("obsidian://open?path=" + [uri]::EscapeDataString("$vault\Me\Profile.md"))
Write-Host "== Obsidian open on WaggleVault"
