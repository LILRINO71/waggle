# Creates the test-only Python venv for the Telegram test client. Touches nothing else:
# no pip install into Hermes's environment or the system Python.
$ErrorActionPreference = "Stop"
$Telethon = "1.46.0"
$Root = Join-Path $env:USERPROFILE ".waggle-testclient"
$Venv = Join-Path $Root "venv"
$Py = Join-Path $Venv "Scripts\python.exe"
New-Item -ItemType Directory -Force $Root | Out-Null

foreach ($ver in @("3.14", "3.13", "3.12")) {
    if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
    & py "-$ver" -m venv $Venv 2>$null
    if ($LASTEXITCODE -ne 0) { continue }
    & $Py -m pip install --disable-pip-version-check --quiet "telethon==$Telethon"
    if ($LASTEXITCODE -ne 0) { Write-Host "telethon failed on Python $ver, trying the next one"; continue }
    & $Py -c "import telethon, sys; print('venv:', sys.executable); print('python:', sys.version.split()[0]); print('telethon:', telethon.__version__)"
    if ($LASTEXITCODE -eq 0) { exit 0 }
}
Write-Host "Could not build the venv with Python 3.14, 3.13 or 3.12"
exit 1
