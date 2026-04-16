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

PROVIDER_PID=""
SERVE_PID=""
CHROME_PID=""

cleanup() {
  local exit_code=$?
  echo "[desktop-mac] shutting down child processes..."
  if [[ -n "${CHROME_PID}" ]]; then
    kill "${CHROME_PID}" 2>/dev/null || true
  fi
  if [[ -n "${SERVE_PID}" ]]; then
    kill "${SERVE_PID}" 2>/dev/null || true
    wait "${SERVE_PID}" 2>/dev/null || true
  fi
  if [[ -n "${PROVIDER_PID}" ]]; then
    kill "${PROVIDER_PID}" 2>/dev/null || true
    wait "${PROVIDER_PID}" 2>/dev/null || true
  fi
  echo "[desktop-mac] stopped"
  exit "${exit_code}"
}

trap cleanup EXIT INT TERM HUP

echo "[desktop-mac] stopping stale listeners on :${PROVIDER_PORT} and :${WEB_PORT}"
kill "$(lsof -tiTCP:${PROVIDER_PORT} -sTCP:LISTEN)" 2>/dev/null || true
kill "$(lsof -tiTCP:${WEB_PORT} -sTCP:LISTEN)" 2>/dev/null || true

echo "[desktop-mac] starting local provider at http://${PROVIDER_HOST}:${PROVIDER_PORT}"
(
  cd "${BACKEND_DIR}"
  uv run uvicorn app.deepseek_local_provider:app \
    --host "${PROVIDER_HOST}" \
    --port "${PROVIDER_PORT}" \
    >"${LOG_DIR}/deepseek_local_provider.log" 2>&1
) &
PROVIDER_PID=$!

echo "[desktop-mac] starting deer-flow (managed child)"
(
  cd "${ROOT_DIR}"
  ./scripts/serve.sh --dev >"${LOG_DIR}/desktop_serve.log" 2>&1
) &
SERVE_PID=$!

echo "[desktop-mac] waiting for services..."
for _ in {1..40}; do
  if curl --noproxy '*' -fsS "http://127.0.0.1:${PROVIDER_PORT}/health" >/dev/null 2>&1 \
    && curl --noproxy '*' -fsS "http://127.0.0.1:${WEB_PORT}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

if ! curl --noproxy '*' -fsS "http://127.0.0.1:${WEB_PORT}" >/dev/null 2>&1; then
  echo "[desktop-mac] deer-flow web is not ready on :${WEB_PORT}. Check ${LOG_DIR}/desktop_serve.log"
  exit 1
fi

DESKTOP_URL="http://localhost:${WEB_PORT}/workspace"
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if [[ -x "${CHROME_BIN}" ]]; then
  echo "[desktop-mac] opening Chrome app window"
  "${CHROME_BIN}" --app="${DESKTOP_URL}" >/dev/null 2>&1 &
  CHROME_PID=$!
  echo "[desktop-mac] ready"
  echo "[desktop-mac] close the Chrome app window to stop all services"
  echo "[desktop-mac] provider log: ${LOG_DIR}/deepseek_local_provider.log"
  echo "[desktop-mac] serve log: ${LOG_DIR}/desktop_serve.log"
  wait "${CHROME_PID}"
else
  echo "[desktop-mac] Google Chrome not found, opening default browser."
  echo "[desktop-mac] services are running as child processes; press Ctrl+C to stop all."
  open "${DESKTOP_URL}" >/dev/null 2>&1 || true
  wait
fi
