# Set up Waggle's dead-man switch: deploy the free Cloudflare Worker, give it its secrets, point Waggle at it.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File tools\deadman-setup.ps1
#
# Do first (once): make the free Cloudflare account and run  npx wrangler login  in cloud\deadman  (docs\DEADMAN.md).
# Safe to run again: it reuses the KV namespace, re-puts the secrets and redeploys. It never prints a secret:
# each one goes from Waggle's own store straight into `wrangler secret put`.
#   token           Windows Credential Manager (made by `python -m waggle.deadman setup`)
#   Telegram        ~\.hermes\.env  (TELEGRAM_BOT_TOKEN, TELEGRAM_HOME_CHANNEL)
#   ntfy topic      ~\.waggle\config.json  (made on first use)
param([switch]$NoTest)
$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $repo "cloud\deadman"
$cfg = "wrangler.deploy.toml"
$env:PYTHONPATH = $repo
$env:WRANGLER_SEND_METRICS = "false"

function Step($m) { Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Fail($m) { Write-Host ""; Write-Host "STOPPED: $m" -ForegroundColor Red; exit 1 }
function Wr([string]$cmdline) { (& cmd.exe /c "$cmdline 2>&1") | ForEach-Object { "$_" } }   # stdout+stderr as plain lines

Step "Checking Node, npx and Python"
foreach ($t in "node", "npx", "python") {
    if (-not (Get-Command $t -ErrorAction SilentlyContinue)) { Fail "$t is not installed or not on PATH." }
}

Step "Making Waggle's shared token (kept in Windows Credential Manager, not shown)"
& python -m waggle.deadman setup | Out-Null
if ($LASTEXITCODE) { Fail "python -m waggle.deadman setup failed." }

Set-Location $dir
Step "Checking you are logged in to Cloudflare"
$who = (Wr "npx wrangler whoami") -join "`n"
if ($who -match "not authenticated|not logged in|wrangler login") {
    Fail "Not logged in. Run  npx wrangler login  in this folder ($dir), click Allow in the browser, then run this script again."
}
Write-Host "Logged in."

Step "Finding or making the storage (a free KV namespace)"
function Find-Kv {
    $text = (Wr "npx wrangler kv namespace list") -join "`n"
    $i = $text.IndexOf("[")
    if ($i -lt 0) { return $null }
    try { $all = $text.Substring($i) | ConvertFrom-Json } catch { return $null }
    $hit = @($all | Where-Object { $_.title -like "*WAGGLE_DEADMAN*" })
    if ($hit.Count) { return $hit[0].id }
    return $null
}
$kv = Find-Kv
if (-not $kv) {
    $tmp = Join-Path $env:TEMP "waggle-deadman-kv"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    Push-Location $tmp          # outside the project, so wrangler does not offer to edit a config
    Wr "npx wrangler kv namespace create WAGGLE_DEADMAN" | Out-Null
    Pop-Location
    $kv = Find-Kv
}
if (-not $kv) { Fail "Could not find or make the KV namespace. Run  npx wrangler kv namespace list  and tell Claude what it says." }
Write-Host "Storage ready."

Step "Writing the deploy settings (wrangler.deploy.toml, not committed)"
$base = Get-Content (Join-Path $dir "wrangler.toml") -Raw
$deploy = $base + "`r`n[[kv_namespaces]]`r`nbinding = `"KV`"`r`nid = `"$kv`"`r`n"
Set-Content -Path (Join-Path $dir $cfg) -Value $deploy -Encoding ASCII

Step "Deploying the Worker"
$out = Wr "npx wrangler deploy -c $cfg"
$out | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -and ($out -join "`n") -match "workers.dev subdomain") {
    Write-Host "Cloudflare wants you to pick a name for your free workers.dev address. Answer its question below." -ForegroundColor Yellow
    & npx wrangler deploy -c $cfg
    $out = @()
}
$url = $null
foreach ($line in $out) { if ($line -match "(https://[A-Za-z0-9.-]+\.workers\.dev)") { $url = $Matches[1] } }
if (-not $url) {
    $url = (Read-Host "Paste the https://....workers.dev address shown above").Trim()
}
if ($url -notmatch "^https://[A-Za-z0-9.-]+\.workers\.dev$") { Fail "That does not look like a workers.dev address: $url" }

Step "Giving the Worker its secrets (nothing is shown)"
$secrets = [ordered]@{
    BEAT_TOKEN         = "token"
    TELEGRAM_BOT_TOKEN = "telegram_token"
    TELEGRAM_CHAT_ID   = "telegram_chat"
    NTFY_TOPIC         = "ntfy_topic"
}
foreach ($name in $secrets.Keys) {
    $val = & python -m waggle.deadman secret $secrets[$name]
    if ($LASTEXITCODE -or -not $val) { Fail "Waggle has no value for $name on this PC ($($secrets[$name]))." }
    $val | & npx wrangler secret put $name -c $cfg | Out-Null
    if ($LASTEXITCODE) { Fail "wrangler could not store $name." }
    $val = $null
    Write-Host "  stored $name"
}

Step "Telling Waggle where the Worker is"
& python -m waggle.deadman seturl $url
if ($LASTEXITCODE) { Fail "Could not write deadman_url." }

if (-not $NoTest) {
    Step "Testing: you should get a message on Telegram and in the ntfy app"
    $tok = & python -m waggle.deadman secret token
    try {
        $r = Invoke-RestMethod -Method Post -Uri "$url/test" -Headers @{ Authorization = "Bearer $tok" } -TimeoutSec 30
        Write-Host ("  Telegram: " + $(if ($r.telegram) { "sent" } else { "FAILED" }))
        Write-Host ("  ntfy:     " + $(if ($r.ntfy) { "sent" } else { "FAILED (is the ntfy app subscribed to your topic?)" }))
    } catch { Write-Host "  The test call failed: $($_.Exception.Message)" -ForegroundColor Yellow }
    $tok = $null
    Step "First heartbeat"
    & python -m waggle.deadman beat
    & python -m waggle.deadman status
}

Write-Host ""
Write-Host "Done. Waggle sends a heartbeat every 5 minutes from now on (the Waggle core must be running; if you just updated" -ForegroundColor Green
Write-Host "Waggle, run tools\deploy.ps1 once so the core loads the new part)." -ForegroundColor Green
