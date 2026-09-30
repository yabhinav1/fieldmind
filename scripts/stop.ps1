# Stops the edge devices started by start.ps1.
#   .\scripts\stop.ps1          stop the devices
#   .\scripts\stop.ps1 -All     also stop the language model server and the cloud containers
param([switch]$All)

$root = Split-Path -Parent $PSScriptRoot
foreach ($port in 8001, 8002) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        Stop-Process -Id $listener.OwningProcess -Force
        Write-Host "Stopped the device on port $port."
    }
}
if ($All) {
    & (Join-Path $PSScriptRoot "llm.ps1") -Stop
    docker compose -f (Join-Path $root "docker-compose.yml") --profile linux-device down
}
