#!/usr/bin/env bash
# Switch nano-NOTEBOOKLM between the codex proxy and the local AutoDL Qwen2.5-7B-RAFT.
#
# Usage:
#   bash scripts/switch_backend.sh autodl   # → http://localhost:8001/v1 (needs tunnel)
#   bash scripts/switch_backend.sh codex    # → https://codex.ysaikeji.cn/v1
#   bash scripts/switch_backend.sh status   # show current
#
# Edits .env in place. The script does NOT restart the API server — you must do that yourself.
set -e

ENV_FILE="$(dirname "$0")/../.env"
ENV_FILE="$(cd "$(dirname "$ENV_FILE")" && pwd)/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: .env not found at $ENV_FILE" >&2
  exit 1
fi

cmd="${1:-status}"

current_url() {
  grep -E '^OPENAI_BASE_URL=' "$ENV_FILE" | head -1 | sed 's/^[^=]*=//'
}
current_model() {
  grep -E '^OPENAI_MODEL=' "$ENV_FILE" | head -1 | sed 's/^[^=]*=//'
}

set_var() {
  local key="$1"; local val="$2"
  if grep -qE "^${key}=" "$ENV_FILE"; then
    # macOS sed needs -i ''
    sed -i.bak -E "s|^${key}=.*$|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

case "$cmd" in
  autodl)
    set_var OPENAI_BASE_URL "http://localhost:8001/v1"
    set_var OPENAI_MODEL    "Qwen2.5-7B-RAFT"
    # Local server doesn't validate the key, but the openai client requires a non-empty value
    set_var OPENAI_API_KEY  "local-no-auth"
    rm -f "${ENV_FILE}.bak"
    echo "Switched to AutoDL Qwen2.5-7B-RAFT (http://localhost:8001/v1)"
    echo "Make sure: (1) GPU is allocated on AutoDL, (2) serve_openai.py is running, (3) tunnel is up."
    ;;
  codex)
    set_var OPENAI_BASE_URL "https://codex.ysaikeji.cn/v1"
    set_var OPENAI_MODEL    "gpt-5.4"
    rm -f "${ENV_FILE}.bak"
    echo "Switched to codex proxy (https://codex.ysaikeji.cn/v1, gpt-5.4)"
    echo "Note: OPENAI_API_KEY was not modified — fill it in manually if needed."
    ;;
  status|"")
    echo "OPENAI_BASE_URL=$(current_url)"
    echo "OPENAI_MODEL=$(current_model)"
    ;;
  *)
    echo "usage: $0 {autodl|codex|status}" >&2
    exit 1
    ;;
esac
