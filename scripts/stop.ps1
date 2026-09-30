# Stops the edge devices started by start.ps1. Add -Cloud to stop the Qdrant Server too.
param([switch]$Cloud)

$root = Split-Path -Parent $PSScriptRoot
foreach ($port in 8001, 8002) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        Stop-Process -Id $listener.OwningProcess -Force
        Write-Host "Stopped the device on port $port."
    }
}
if ($Cloud) { docker compose -f (Join-Path $root "docker-compose.yml") down }
