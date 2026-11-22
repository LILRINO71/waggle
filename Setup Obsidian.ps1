# Installs Obsidian (free, via winget), registers the vault (the "vault" setting in ~/.waggle/config.json, default
# ~/PersonalIntelligenceVault) and opens Waggle's own folder in it ("vault_own", e.g. "12 - Waggle"). Safe to re-run.
$vault = "$env:USERPROFILE\PersonalIntelligenceVault"
$own = ""
$waggleCfg = Join-Path $(if ($env:WAGGLE_HOME) { $env:WAGGLE_HOME } else { "$env:USERPROFILE\.waggle" }) "config.json"
if (Test-Path $waggleCfg) {
    $wc = Get-Content $waggleCfg -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($wc.vault) { $vault = [string]$wc.vault }
    if ($wc.vault_own) { $own = ([string]$wc.vault_own).Trim("/", "\") }
}
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

$note = if ($own) { "$vault\$own\Waggle HQ.md" } else { "$vault\Me\Profile.md" }
Start-Process ("obsidian://open?path=" + [uri]::EscapeDataString($note))
Write-Host "== Obsidian open on $vault"
