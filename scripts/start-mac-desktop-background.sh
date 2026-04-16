#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is only for macOS."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
LOG_DIR="${ROOT_DIR}/logs"
mkdir -p "${LOG_DIR}"

PROVIDER_HOST="${DEEPSEEK_LOCAL_PROVIDER_HOST:-127.0.0.1}"
PROVIDER_PORT="${DEEPSEEK_LOCAL_PROVIDER_PORT:-8765}"
WEB_PORT="${DEER_FLOW_WEB_PORT:-2026}"

export DEEPSEEK_LOCAL_MODEL="${DEEPSEEK_LOCAL_MODEL:-DeepSeekV4}"
export DEEPSEEK_LOCAL_INTERFACE_MODE="${DEEPSEEK_LOCAL_INTERFACE_MODE:-both}"
export DEER_FLOW_SANDBOX_HOST_ROOT="${DEER_FLOW_SANDBOX_HOST_ROOT:-$HOME}"
export DEER_FLOW_SANDBOX_PROJECT_ROOT="${DEER_FLOW_SANDBOX_PROJECT_ROOT:-$HOME/Downloads}"

kill "$(lsof -tiTCP:${PROVIDER_PORT} -sTCP:LISTEN)" 2>/dev/null || true
kill "$(lsof -tiTCP:${WEB_PORT} -sTCP:LISTEN)" 2>/dev/null || true

(
  cd "${BACKEND_DIR}"
  nohup uv run uvicorn app.deepseek_local_provider:app \
    --host "${PROVIDER_HOST}" \
    --port "${PROVIDER_PORT}" \
    >"${LOG_DIR}/deepseek_local_provider.log" 2>&1 &
)

(
  cd "${ROOT_DIR}"
  nohup ./scripts/serve.sh --dev --daemon >"${LOG_DIR}/desktop_serve.log" 2>&1 &
)

for _ in {1..40}; do
  if curl --noproxy '*' -fsS "http://127.0.0.1:${PROVIDER_PORT}/health" >/dev/null 2>&1 \
    && curl --noproxy '*' -fsS "http://127.0.0.1:${WEB_PORT}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

DESKTOP_URL="http://localhost:${WEB_PORT}/workspace"
if [[ -d "/Applications/Google Chrome.app" ]]; then
  open -na "Google Chrome" --args --app="${DESKTOP_URL}" >/dev/null 2>&1 || true
else
  open "${DESKTOP_URL}" >/dev/null 2>&1 || true
fi
