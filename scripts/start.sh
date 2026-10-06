#!/usr/bin/env sh
# Starts the cloud and the demo devices on macOS or Linux. Same flags as the PowerShell script:
#   ./scripts/start.sh [--pin 2468] [--only edge-b] [--cloud-url URL] [--lan] [--no-browser]
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
python="$root/.venv/bin/python"
if [ ! -x "$python" ]; then
  echo "No virtual environment found. Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
exec "$python" -m fieldmind start "$@"
