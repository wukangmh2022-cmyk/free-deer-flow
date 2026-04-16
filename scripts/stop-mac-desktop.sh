#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROVIDER_PORT="${DEEPSEEK_LOCAL_PROVIDER_PORT:-8765}"

echo "[desktop-mac] stopping deer-flow services"
(
  cd "${ROOT_DIR}"
  ./scripts/serve.sh --stop || true
)

echo "[desktop-mac] stopping local provider on :${PROVIDER_PORT}"
kill "$(lsof -tiTCP:${PROVIDER_PORT} -sTCP:LISTEN)" 2>/dev/null || true

echo "[desktop-mac] stopped"
