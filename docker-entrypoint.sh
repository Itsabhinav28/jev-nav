#!/usr/bin/env bash
# Starts the ORB-SLAM3 TCP service (loopback only) and the jev-nav dashboard server in one container.
set -euo pipefail

export HTTPS_PORT="${HTTPS_PORT:-8443}"
export HTTP_PORT="${HTTP_PORT:-8080}"
export SLAM_PORT="${SLAM_PORT:-5555}"
export PUBLIC_HOST="${PUBLIC_HOST:-localhost}"
export SLAM_HOST=127.0.0.1   # slam_server is only reachable inside the container
export SLAM_AUTOSTART=0      # the app must not try to spawn its own (WSL) slam_server

SLAM_ROOT=/opt/ORB_SLAM3
SLAM_BIN="$SLAM_ROOT/Examples/Monocular/slam_server"
SLAM_PID=""
APP_PID=""

stop() {
    trap '' TERM INT
    [ -n "$APP_PID" ] && kill -TERM "$APP_PID" 2>/dev/null || true
    [ -n "$SLAM_PID" ] && kill -TERM "$SLAM_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap 'stop; exit 143' TERM INT

echo "[entrypoint] starting slam_server on 127.0.0.1:${SLAM_PORT}"
"$SLAM_BIN" "$SLAM_ROOT/Vocabulary/ORBvoc.txt" "$SLAM_PORT" 127.0.0.1 &
SLAM_PID=$!

# Wait for the port to accept connections (vocabulary load takes a while).
ready=0
for _ in $(seq 1 120); do
    if ! kill -0 "$SLAM_PID" 2>/dev/null; then
        echo "[entrypoint] FATAL: slam_server exited during startup" >&2
        exit 1
    fi
    if (exec 3<>"/dev/tcp/127.0.0.1/${SLAM_PORT}") 2>/dev/null; then
        ready=1
        break
    fi
    sleep 0.5
done
if [ "$ready" != 1 ]; then
    echo "[entrypoint] FATAL: slam_server did not open port ${SLAM_PORT} within 60 s" >&2
    stop
    exit 1
fi
echo "[entrypoint] slam_server is up"

cd /app
# Not exec'd: keeping the shell as PID 1 lets it forward signals to both children.
python streaming_server/server.py --slam --port "$HTTPS_PORT" --local-port "$HTTP_PORT" --host "$PUBLIC_HOST" "$@" &
APP_PID=$!
rc=0
wait "$APP_PID" || rc=$?
stop
exit "$rc"
