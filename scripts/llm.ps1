# Runs a project-scoped Ollama server for on-device answers.
# It keeps its models inside this project and listens on its own port, so it does
# not depend on, or change, any Ollama settings elsewhere on the machine.
#   .\scripts\llm.ps1            start the server, downloading the model on first use
#   .\scripts\llm.ps1 -Stop      stop it
param(
    [string]$Model = "llama3.2:3b",
    [int]$Port = 11435,
    [switch]$Stop
)

$root = Split-Path -Parent $PSScriptRoot
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Stop) {
    if ($listener) { Stop-Process -Id $listener.OwningProcess -Force; Write-Host "Stopped the language model server." }
    exit 0
}

$ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollama) { $ollama = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe" }
if (-not (Test-Path $ollama)) {
    Write-Host "Ollama is not installed. Get it from https://ollama.com, then run this again." -ForegroundColor Yellow
    exit 1
}

$env:OLLAMA_MODELS = Join-Path $root "models\ollama"
$env:OLLAMA_HOST = "127.0.0.1:$Port"
New-Item -ItemType Directory -Force $env:OLLAMA_MODELS | Out-Null
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

if (-not $listener) {
    Start-Process $ollama -ArgumentList "serve" -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs "llm.log") -RedirectStandardError (Join-Path $logs "llm.err.log")
}
$ready = $false
foreach ($i in 1..40) {
    try { Invoke-RestMethod "http://127.0.0.1:$Port/api/version" -TimeoutSec 2 | Out-Null; $ready = $true; break } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $ready) { Write-Host "The language model server did not start. See $logs\llm.err.log" -ForegroundColor Red; exit 1 }

$have = (Invoke-RestMethod "http://127.0.0.1:$Port/api/tags").models | Where-Object { $_.name -eq $Model }
if (-not $have) {
    Write-Host "Downloading $Model (one time, about 2 GB)..."
    & $ollama pull $Model
    if ($LASTEXITCODE -ne 0) { Write-Host "Download failed." -ForegroundColor Red; exit 1 }
}
Write-Host "Language model $Model ready at http://127.0.0.1:$Port" -ForegroundColor Green
