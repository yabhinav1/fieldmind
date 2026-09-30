# Starts the cloud (Qdrant Server in Docker) and two edge devices, then opens both dashboards.
#   .\scripts\start.ps1            start everything and open the browser
#   .\scripts\start.ps1 -NoBrowser start everything, open nothing
param([switch]$NoBrowser)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

docker compose -f (Join-Path $root "docker-compose.yml") up -d
if ($LASTEXITCODE -ne 0) {
    Write-Host "The cloud did not start. Is Docker Desktop running? The devices still work offline." -ForegroundColor Yellow
}

$devices = @(
    @{ Name = "edge-a"; Port = 8001; Author = "technician-a" },
    @{ Name = "edge-b"; Port = 8002; Author = "technician-b" }
)
foreach ($d in $devices) {
    $busy = Get-NetTCPConnection -LocalPort $d.Port -State Listen -ErrorAction SilentlyContinue
    if ($busy) { Write-Host "$($d.Name) is already running on port $($d.Port)."; continue }
    Start-Process $python -WorkingDirectory $root -WindowStyle Hidden `
        -ArgumentList "-m", "fieldmind", "serve", "--device", $d.Name, "--port", $d.Port, "--author", $d.Author `
        -RedirectStandardOutput (Join-Path $logs "$($d.Name).log") `
        -RedirectStandardError (Join-Path $logs "$($d.Name).err.log")
}

foreach ($d in $devices) {
    $url = "http://127.0.0.1:$($d.Port)"
    $ready = $false
    foreach ($i in 1..60) {
        try { Invoke-RestMethod "$url/api/status" -TimeoutSec 2 | Out-Null; $ready = $true; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if ($ready) {
        Write-Host "$($d.Name) ready at $url" -ForegroundColor Green
        if (-not $NoBrowser) { Start-Process $url }
    } else {
        Write-Host "$($d.Name) did not start. See $logs\$($d.Name).err.log" -ForegroundColor Red
    }
}
