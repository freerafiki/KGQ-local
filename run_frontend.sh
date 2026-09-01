#!/usr/bin/env bash
# Serve the static search page and print the LAN address so testers know the URL.
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8080}"

echo "Frontend on:"
for ip in $(hostname -I 2>/dev/null | grep -v "^127\."); do
    echo "  http://${ip}:${PORT}   (visitors on the same network open this)"
done

exec python3 -m http.server "${PORT}" --directory static