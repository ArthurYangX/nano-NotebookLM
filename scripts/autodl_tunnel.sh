#!/usr/bin/env bash
# Open an SSH tunnel from localhost:8001 -> AutoDL container's 8001
# Usage:
#   bash scripts/autodl_tunnel.sh           # foreground (Ctrl-C to stop)
#   bash scripts/autodl_tunnel.sh --bg      # background, writes pidfile
#   bash scripts/autodl_tunnel.sh --stop    # kill background tunnel
#
# Requires sshpass; password is hard-coded for this dev box.
set -e

REMOTE_HOST="connect.westd.seetacloud.com"
REMOTE_PORT=48293
REMOTE_USER="root"
REMOTE_PASS='g9oVntOeM6AP'
LOCAL_PORT=8001
REMOTE_TARGET_PORT=8001
PIDFILE="/tmp/autodl_tunnel.pid"

stop_tunnel() {
  if [[ -f "$PIDFILE" ]]; then
    pid=$(cat "$PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" && echo "Stopped tunnel pid=$pid"
    fi
    rm -f "$PIDFILE"
  else
    echo "No pidfile at $PIDFILE — nothing to stop"
  fi
}

case "${1:-}" in
  --stop)
    stop_tunnel
    exit 0
    ;;
  --bg)
    stop_tunnel
    nohup sshpass -p "$REMOTE_PASS" ssh -N \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_TARGET_PORT}" \
      -p "$REMOTE_PORT" "${REMOTE_USER}@${REMOTE_HOST}" \
      >/tmp/autodl_tunnel.log 2>&1 &
    echo $! > "$PIDFILE"
    echo "Tunnel started in background (pid=$(cat $PIDFILE)). Log: /tmp/autodl_tunnel.log"
    echo "Local: http://localhost:${LOCAL_PORT}/v1   →   AutoDL container :${REMOTE_TARGET_PORT}"
    ;;
  *)
    echo "Tunneling localhost:${LOCAL_PORT} → AutoDL :${REMOTE_TARGET_PORT}  (Ctrl-C to stop)"
    exec sshpass -p "$REMOTE_PASS" ssh -N \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
      -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_TARGET_PORT}" \
      -p "$REMOTE_PORT" "${REMOTE_USER}@${REMOTE_HOST}"
    ;;
esac
