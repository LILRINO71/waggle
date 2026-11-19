# Make the local model fast on a 12 GB GPU: flash attention + 8-bit KV cache (half the memory per token),
# rebuild waggle-4b with a 32k window, restart Ollama, and show where the model runs (want 100% GPU).
[Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1", "User")
[Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE", "q8_0", "User")
$env:OLLAMA_FLASH_ATTENTION = "1"
$env:OLLAMA_KV_CACHE_TYPE = "q8_0"

Get-Process "ollama app", "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2
Start-Process "$env:LOCALAPPDATA\Programs\Ollama\ollama app.exe"
for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-RestMethod http://localhost:11434/api/version -TimeoutSec 2 | Out-Null; break } catch { Start-Sleep -Seconds 1 }
}
ollama create waggle-4b -f "$PSScriptRoot\Modelfile.waggle-4b"
$body = '{"model":"waggle-4b","prompt":"hi","stream":false,"keep_alive":"30m","options":{"num_predict":1}}'
Invoke-RestMethod http://localhost:11434/api/generate -Method Post -Body $body -ContentType 'application/json' | Out-Null
ollama ps
Write-Host "== tuned"
