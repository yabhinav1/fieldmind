#!/bin/sh
# Builds fieldmind-pterodactyl.zip: everything a panel server needs, nothing else.
#   sh deploy/pterodactyl/pack.sh [output.zip]
set -e
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="${1:-$root/fieldmind-pterodactyl.zip}"
stage="$(mktemp -d)"
cp -R "$root/fieldmind" "$stage/fieldmind"
find "$stage/fieldmind" -name __pycache__ -type d -prune -exec rm -rf {} +
cp "$root/requirements.txt" "$root/pyproject.toml" "$root/LICENSE" "$stage/"
cp "$root/deploy/pterodactyl/start.sh" "$root/deploy/pterodactyl/.env" "$stage/"
cp "$root/deploy/pterodactyl/README.md" "$stage/README.md"
rm -f "$out"
(cd "$stage" && zip -qr "$out" .)
rm -rf "$stage"
echo "Wrote $out ($(du -h "$out" | cut -f1))"
