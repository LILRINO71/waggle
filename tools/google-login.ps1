# One-time Google sign-in for Waggle (docs/GOOGLE.md). Stores the OAuth client you downloaded, then opens the
# browser so you can allow Waggle. The token goes into Windows Credential Manager (gog's keyring), never a file.
$ErrorActionPreference = 'Continue'
Set-Location (Split-Path -Parent $PSScriptRoot)
$exe = (python -c "import waggle.google as g; p=g.gog_exe(); print(p or '')").Trim()
if (-not $exe) {
    Write-Host 'Installing the pinned gog release (checked against its SHA-256)...'
    python -m waggle.google install
    $exe = (python -c "import waggle.google as g; print(g.gog_exe() or '')").Trim()
}
if (-not $exe) { Write-Host 'gog is not installed; stopping.'; exit 1 }
$gogHome = (python -c "import waggle.google as g; print(g.gog_home())").Trim()
$services = (python -c "import waggle.google as g; print(g.SERVICES)").Trim()

$have = & $exe --home $gogHome auth list --json --no-input 2>$null
if ($LASTEXITCODE -eq 0 -and $have -match '"email"') {
    Write-Host 'Already signed in:'; python -m waggle.google status; exit 0
}

$creds = Join-Path $gogHome 'data\credentials.json'
if (-not (Test-Path $creds)) {
    $dl = Join-Path $env:USERPROFILE 'Downloads'
    $file = Get-ChildItem -Path $dl -Filter 'client_secret*.json' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $file) {
        Write-Host 'I could not find the file you downloaded from Google (client_secret_....json) in your Downloads folder.'
        Write-Host 'Do steps 1 to 4 in docs\GOOGLE.md first, then double-click this again.'
        exit 1
    }
    Write-Host "Using $($file.Name) from Downloads."
    & $exe --home $gogHome auth credentials set $file.FullName
    if ($LASTEXITCODE -ne 0) { Write-Host 'gog did not accept that file.'; exit 1 }
}

$email = Read-Host 'Type your Gmail address'
Write-Host 'Your browser will open. Pick your account, then: Advanced > Go to Waggle (unsafe) > tick every box > Continue.'
& $exe --home $gogHome auth add $email --services $services
if ($LASTEXITCODE -ne 0) { Write-Host 'Sign-in did not finish. Run this again to retry.'; exit 1 }
python -m waggle.google status
Write-Host 'Done. You can delete the client_secret file from Downloads; Waggle keeps its own copy.'
