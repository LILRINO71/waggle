# One-time setup for the small local tools (all pinned, no accounts, no keys, no telemetry). Safe to re-run.
#   markitdown venv  -> waggle/dropbox_markitdown.py (scans, .xlsx, pictures in the Drop folder)
#   pyright          -> waggle/pyright_check.py (type-error warning in proposal merge messages)
#   ccusage          -> waggle/usage_view.py (tokens by block and session)
# The hub skills (rss-feeds, Obsidian Bases / JSON Canvas / CLI) are installed by install.py through Hermes.
$ErrorActionPreference = "Stop"
$h = $env:USERPROFILE
if (-not (Test-Path "$h\.waggle-markitdown\Scripts\python.exe")) { py -3.14 -m venv "$h\.waggle-markitdown" }
& "$h\.waggle-markitdown\Scripts\python.exe" -m pip install -q "markitdown[pdf,docx,pptx,xlsx]==0.1.9" markitdown-ocr==0.1.2 openai==3.57.0
npm install --prefix "$h\.waggle-tools\pyright" --no-audit --no-fund --ignore-scripts pyright@1.1.409
npm install --prefix "$h\.waggle-tools\ccusage" --no-audit --no-fund --ignore-scripts ccusage@20.0.51
Write-Host "small tools ready"
