# Returns the demo to a clean slate: stops the devices, erases their data and the
# cloud collection, and starts them again. Use it between demo runs.
#   .\scripts\reset.ps1              clean slate, devices ask for a PIN on first open
#   .\scripts\reset.ps1 -Pin 2468    clean slate with the PIN already set
param([string]$Pin, [switch]$NoBrowser)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

& (Join-Path $PSScriptRoot "stop.ps1")
Start-Sleep -Seconds 1
& $python -m fieldmind reset --device edge-a --cloud
& $python -m fieldmind reset --device edge-b

$arguments = @{}
if ($Pin) { $arguments.Pin = $Pin }
if ($NoBrowser) { $arguments.NoBrowser = $true }
& (Join-Path $PSScriptRoot "start.ps1") @arguments
