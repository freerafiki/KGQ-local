#!/usr/bin/env bash
# Gracefully stop any process listening on the KGQ ports.
set -e

kill_port() {
    local port=$1
    local pids
    pids=$(lsof -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
    if [ -z "$pids" ]; then
        echo "Port $port: nothing listening."
        return
    fi
    for pid in $pids; do
        echo "Port $port: stopping PID $pid (SIGTERM) ..."
        kill "$pid"
    done
    # Give them a moment to exit cleanly, then force-kill stragglers.
    sleep 2
    pids=$(lsof -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)
    for pid in $pids; do
        echo "Port $port: PID $pid did not exit, sending SIGKILL ..."
        kill -9 "$pid"
    done
}

kill_port 8000
kill_port 8080
kill_port 28000
kill_port 28080
