#!/usr/bin/env sh
# Clean slate between demo runs: stops the devices, erases their data and the
# cloud collection, and starts them again.
#   ./scripts/reset.sh [--pin 2468] [--no-browser]
set -e
root="$(cd "$(dirname "$0")/.." && pwd)"
exec "$root/.venv/bin/python" -m fieldmind reset-all "$@"
