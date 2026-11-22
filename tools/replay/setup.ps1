# Makes the dev-only venv for the replay suite (Inspect AI). Not the core interpreter. Safe to run again.
$venv = Join-Path $env:USERPROFILE ".waggle-tools\inspect"
if (-not (Test-Path (Join-Path $venv "Scripts\python.exe"))) { py -3.14 -m venv $venv }
& (Join-Path $venv "Scripts\python.exe") -m pip install --disable-pip-version-check --only-binary=:all: -r (Join-Path $PSScriptRoot "requirements.txt")
& (Join-Path $venv "Scripts\python.exe") -c "import inspect_ai; print('inspect_ai', inspect_ai.__version__)"
