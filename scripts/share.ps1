# Shares the two running dashboards over the internet with temporary public links, so a
# teammate or judge can open them in a browser without installing anything.
# Uses Cloudflare quick tunnels (no account needed). The dashboards stay PIN-locked.
#   .\scripts\share.ps1          start sharing and print the two links
#   .\scripts\share.ps1 -Stop    stop sharing
param([switch]$Stop)

$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root "data\logs"
New-Item -ItemType Directory -Force $logs | Out-Null

if ($Stop) {
    Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
    Write-Host "Stopped sharing. The links no longer work."
    exit 0
}

$cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cloudflared) { $cloudflared = "C:\Program Files (x86)\cloudflared\cloudflared.exe" }
if (-not (Test-Path $cloudflared)) {
    Write-Host "cloudflared is not installed. Run:  winget install --id Cloudflare.cloudflared" -ForegroundColor Red
    exit 1
}

Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force
$links = @{}
foreach ($d in @(@{ Name = "edge-a"; Port = 8001 }, @{ Name = "edge-b"; Port = 8002 })) {
    $log = Join-Path $logs "share-$($d.Name).log"
    Remove-Item $log -ErrorAction SilentlyContinue
    Start-Process $cloudflared -ArgumentList "tunnel", "--url", "http://127.0.0.1:$($d.Port)", "--no-autoupdate" `
        -WindowStyle Hidden -RedirectStandardError $log -RedirectStandardOutput (Join-Path $logs "share-$($d.Name).out")
    foreach ($i in 1..60) {
        Start-Sleep -Milliseconds 500
        $match = Select-String -Path $log -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($match) { $links[$d.Name] = $match.Matches[0].Value; break }
    }
}

""
foreach ($name in "edge-a", "edge-b") {
    if ($links[$name]) { Write-Host ("{0,-8} {1}" -f $name, $links[$name]) -ForegroundColor Green }
    else { Write-Host ("{0,-8} no link yet, see {1}" -f $name, (Join-Path $logs "share-$name.log")) -ForegroundColor Yellow }
}
""
Write-Host "Send the links and the PIN. Stop sharing with:  .\scripts\share.ps1 -Stop"
