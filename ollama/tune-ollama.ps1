# Make the local model fast on a 12 GB GPU: flash attention + 8-bit KV cache (half the memory per token), so the
# 64k window Hermes requires fits on the GPU. Rebuilds waggle-4b, restarts Ollama through Waggle Gateway.vbs
# (which sets those variables and keeps Ollama alive after this window closes), and shows where it runs.
[Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1", "User")
[Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE", "q8_0", "User")

Get-Process "ollama app", "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
wscript "$PSScriptRoot\..\Waggle Gateway.vbs"
for ($i = 0; $i -lt 60; $i++) {
    try { Invoke-RestMethod http://127.0.0.1:11434/api/version -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Seconds 1 }
}
ollama create waggle-4b -f "$PSScriptRoot\Modelfile.waggle-4b"
$body = '{"model":"waggle-4b","prompt":"hi","stream":false,"keep_alive":"30m","options":{"num_predict":1}}'
Invoke-RestMethod http://127.0.0.1:11434/api/generate -Method Post -Body $body -ContentType 'application/json' | Out-Null
ollama ps
Write-Host "== tuned"
