#!/bin/sh
# FieldMind on a Pterodactyl panel (HidenCloud, Pterodactyl, Pelican...).
#
# Set the server's startup command to:   sh start.sh
# and put the device's settings in the .env file next to this script.
#
# One panel server = one edge device. Pterodactyl hands the allocated port to
# the process as SERVER_PORT; the dashboard binds to it on all interfaces. The
# cloud is a Qdrant Cloud cluster (free tier), because a panel server cannot run
# Docker. Everything is installed into ./.venv on the first start and kept in
# the server's persistent /home/container.
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python)"
if [ -z "$PY" ]; then
  echo "No Python found. Choose a Python 3.11 or 3.12 image for this server in the panel." >&2
  exit 1
fi

# Install (or update) the dependencies once per requirements.txt change.
STAMP=".venv/.requirements.sha"
WANT="$(sha256sum requirements.txt | cut -c1-16)"
if [ ! -x .venv/bin/python ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
  echo "Installing FieldMind's dependencies (first start only, a few minutes)..."
  "$PY" -m venv .venv 2>/dev/null || "$PY" -m venv --without-pip .venv
  if [ ! -x .venv/bin/pip ]; then
    .venv/bin/python -m ensurepip --upgrade 2>/dev/null || \
      (curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py && .venv/bin/python /tmp/get-pip.py -q)
  fi
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  echo "$WANT" > "$STAMP"
fi

export FIELDMIND_HOST="${FIELDMIND_HOST:-0.0.0.0}"
export FIELDMIND_PORT="${SERVER_PORT:-${FIELDMIND_PORT:-8001}}"
export FIELDMIND_DATA="${FIELDMIND_DATA:-$PWD/data}"
export FIELDMIND_MODELS="${FIELDMIND_MODELS:-$PWD/models}"
export HF_HUB_DISABLE_TELEMETRY=1

echo "FieldMind $(grep -m1 '^FIELDMIND_DEVICE=' .env 2>/dev/null | cut -d= -f2 || echo edge-a) starting on port $FIELDMIND_PORT"
echo "First start downloads the models (about 260 MB). Open the panel's address and enter the PIN."
exec .venv/bin/python -m fieldmind serve
