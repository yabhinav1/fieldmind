# Starts the cloud, the on-device language model and the edge devices, then opens the dashboards.
#
#   .\scripts\start.ps1                          two devices on this laptop (edge-a, edge-b)
#   .\scripts\start.ps1 -Pin 2468                same, with the device PIN pre-set
#   .\scripts\start.ps1 -Only edge-b -CloudUrl http://192.168.1.20:6333
#                                                one device on a second laptop, syncing with the first
#   .\scripts\start.ps1 -CloudUrl https://x.cloud.qdrant.io:6333 -CloudApiKey KEY
#                                                use Qdrant Cloud instead of Docker
#   .\scripts\start.ps1 -Lan                     let phones and other computers open the dashboards
#
# Settings can also live in a .env file (see .env.example).
param(
    [string]$Only,
    [string]$CloudUrl,
    [string]$CloudApiKey,
    [string]$Pin,
    [switch]$Lan,
    [switch]$NoLlm,
    [switch]$NoBrowser
)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

if (-not (Test-Path $python)) {
    Write-Host "No virtual environment found. Run:  py -m venv .venv ; .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}

# A cloud URL from the command line or .env means the cloud runs somewhere else.
$envFile = Join-Path $root ".env"
if (-not $CloudUrl -and (Test-Path $envFile)) {
    $line = Get-Content $envFile | Where-Object { $_ -match '^\s*FIELDMIND_CLOUD_URL\s*=' } | Select-Object -First 1
    if ($line) { $CloudUrl = ($line -split '=', 2)[1].Trim() }
}
if ($CloudUrl) { $env:FIELDMIND_CLOUD_URL = $CloudUrl }
if ($CloudApiKey) { $env:FIELDMIND_CLOUD_API_KEY = $CloudApiKey }
if ($Pin) { $env:FIELDMIND_PIN = $Pin }

$localCloud = (-not $CloudUrl) -or ($CloudUrl -match '//(localhost|127\.0\.0\.1)[:/]')
if ($localCloud) {
    docker compose -f (Join-Path $root "docker-compose.yml") up -d qdrant
    if ($LASTEXITCODE -ne 0) {
        Write-Host "The cloud did not start. Is Docker Desktop running? The devices still work offline." -ForegroundColor Yellow
    }
} else {
    Write-Host "Using the cloud at $CloudUrl"
}

# The language model is optional. It starts only once its model has been downloaded.
if (-not $NoLlm -and (Test-Path (Join-Path $root "models\ollama\manifests"))) {
    & (Join-Path $PSScriptRoot "llm.ps1")
} elseif (-not $NoLlm) {
    Write-Host "No language model downloaded. Answers are composed from notes. Run .\scripts\llm.ps1 once to add one."
}

$devices = @(
    @{ Name = "edge-a"; Port = 8001; Author = "technician-a" },
    @{ Name = "edge-b"; Port = 8002; Author = "technician-b" }
)
if ($Only) {
    $devices = @($devices | Where-Object { $_.Name -eq $Only })
    if (-not $devices) { $devices = @(@{ Name = $Only; Port = 8001; Author = "technician" }) }
}
$bind = if ($Lan) { "0.0.0.0" } else { "127.0.0.1" }

foreach ($d in $devices) {
    $busy = Get-NetTCPConnection -LocalPort $d.Port -State Listen -ErrorAction SilentlyContinue
    if ($busy) { Write-Host "$($d.Name) is already running on port $($d.Port)."; continue }
    Start-Process $python -WorkingDirectory $root -WindowStyle Hidden `
        -ArgumentList "-m", "fieldmind", "serve", "--device", $d.Name, "--port", $d.Port, "--author", $d.Author, "--host", $bind `
        -RedirectStandardOutput (Join-Path $logs "$($d.Name).log") `
        -RedirectStandardError (Join-Path $logs "$($d.Name).err.log")
}

foreach ($d in $devices) {
    $url = "http://127.0.0.1:$($d.Port)"
    $ready = $false
    foreach ($i in 1..80) {
        try { Invoke-RestMethod "$url/api/auth/state" -TimeoutSec 2 | Out-Null; $ready = $true; break } catch { Start-Sleep -Milliseconds 500 }
    }
    if ($ready) {
        Write-Host "$($d.Name) ready at $url" -ForegroundColor Green
        if (-not $NoBrowser) { Start-Process $url }
    } else {
        Write-Host "$($d.Name) did not start. See $logs\$($d.Name).err.log" -ForegroundColor Red
    }
}

if ($Lan) {
    $address = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -eq "Dhcp" } | Select-Object -First 1).IPAddress
    if ($address) { Write-Host "From other devices on this network, open http://${address}:$($devices[0].Port)" }
}
