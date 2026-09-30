#!/usr/bin/env bash
# Serve the static search page for local development.
set -e
# Run from the repo root: static/ and .env are resolved relative to it.
cd "$(dirname "$0")/.."
PORT="${PORT:-28080}"

echo "Frontend on: http://localhost:${PORT}"

exec python3 -m http.server "${PORT}" --directory static