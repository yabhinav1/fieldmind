#!/usr/bin/env sh
# Stops the devices started by start.sh.
#   ./scripts/stop.sh
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
python="$root/.venv/bin/python"
if [ ! -x "$python" ]; then
  echo "No virtual environment found. Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
exec "$python" -m fieldmind stop "$@"
