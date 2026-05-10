#!/usr/bin/env bash
# nano-NOTEBOOKLM dev shortcut.
#   ./dev.sh                # up (idempotent)
#   ./dev.sh up|down|restart|status|logs|test|ingest|install|open|help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
PORT="${NANO_NLM_PORT:-8000}"
LOG="${NANO_NLM_LOG:-/tmp/nano-nlm.log}"
PIDFILE="${NANO_NLM_PID:-/tmp/nano-nlm.pid}"
URL="http://127.0.0.1:$PORT"

activate() {
  if [[ ! -d "$VENV" ]]; then
    echo "no .venv at $VENV — run './dev.sh install' first" >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
}

running_pid() {
  lsof -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null || true
}

wait_healthy() {
  for _ in $(seq 1 30); do
    if curl -sf -o /dev/null "$URL/api/health"; then return 0; fi
    sleep 0.5
  done
  return 1
}

cmd="${1:-up}"
shift || true

case "$cmd" in
  up|start)
    activate
    if pid=$(running_pid) && [[ -n "$pid" ]]; then
      echo "already running on :$PORT (pid $pid). use './dev.sh restart' to reload."
      exit 0
    fi
    echo "starting → $URL  (log: $LOG)"
    nohup python api/server.py >"$LOG" 2>&1 &
    echo $! >"$PIDFILE"
    if wait_healthy; then
      echo "ready · pid $(cat "$PIDFILE")"
    else
      echo "did not become healthy within 15s — see $LOG" >&2
      exit 1
    fi
    ;;

  down|stop)
    if pids=$(running_pid) && [[ -n "$pids" ]]; then
      echo "$pids" | xargs kill
      echo "stopped (pid $pids)"
    else
      echo "not running"
    fi
    rm -f "$PIDFILE"
    ;;

  restart|reload)
    "$0" down || true
    sleep 1
    "$0" up
    ;;

  status)
    if pid=$(running_pid) && [[ -n "$pid" ]]; then
      echo "up (pid $pid) on :$PORT"
      curl -s "$URL/api/status" 2>/dev/null | head -c 600 || true
      echo
    else
      echo "down"
    fi
    ;;

  logs|tail)
    [[ -f "$LOG" ]] || { echo "no log at $LOG"; exit 1; }
    tail -f "$LOG"
    ;;

  test)
    activate
    if [[ $# -gt 0 ]]; then
      pytest -q "$@"
    else
      pytest -q
    fi
    ;;

  ingest)
    activate
    python scripts/ingest_all.py "$@"
    ;;

  install|setup)
    if [[ ! -d "$VENV" ]]; then
      python3 -m venv "$VENV"
    fi
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    pip install -e ".[test]"
    if [[ ! -f .env ]] && [[ -f .env.example ]]; then
      cp .env.example .env
      echo "created .env — fill OPENAI_API_KEY / OPENAI_BASE_URL before './dev.sh up'"
    fi
    echo "install ok"
    ;;

  open)
    if command -v open >/dev/null 2>&1; then
      open "$URL/"
    elif command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$URL/"
    else
      echo "$URL/"
    fi
    ;;

  -h|--help|help)
    cat <<EOF
nano-NOTEBOOKLM dev shortcut

usage: ./dev.sh <command> [args]

  up        start server (default; idempotent — skips if already up)
  down      stop server on :$PORT
  restart   down + up
  status    show pid + /api/status snapshot
  logs      tail $LOG
  test [.]  pytest -q [optional pytest args]
  ingest    rebuild corpus (scripts/ingest_all.py)
  install   create .venv + pip install + .env
  open      open $URL/ in default browser
  help      this message

env overrides: NANO_NLM_PORT, NANO_NLM_LOG, NANO_NLM_PID
EOF
    ;;

  *)
    echo "unknown command: $cmd  (try './dev.sh help')" >&2
    exit 2
    ;;
esac
