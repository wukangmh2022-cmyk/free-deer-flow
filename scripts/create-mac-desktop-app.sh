#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is only for macOS."
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/desktop-app"
TMP_DIR="${OUT_DIR}/.build"

mkdir -p "${TMP_DIR}"
rm -rf "${OUT_DIR}/DeerFlowWithDeepSeek.app" "${OUT_DIR}/DeerFlowWithDeepSeek Stop.app"

START_SCPT="${TMP_DIR}/start.scpt"
STOP_SCPT="${TMP_DIR}/stop.scpt"

cat >"${START_SCPT}" <<EOF
on run
  set projectRoot to "${ROOT_DIR}"
  set cmd to "cd " & quoted form of projectRoot & " && ./scripts/start-mac-desktop-background.sh >/tmp/deerflow_desktop_start.log 2>&1"
  do shell script cmd
end run
EOF

cat >"${STOP_SCPT}" <<EOF
on run
  set projectRoot to "${ROOT_DIR}"
  set cmd to "cd " & quoted form of projectRoot & " && ./scripts/stop-mac-desktop.sh >/tmp/deerflow_desktop_stop.log 2>&1"
  do shell script cmd
end run
EOF

osacompile -o "${OUT_DIR}/DeerFlowWithDeepSeek.app" "${START_SCPT}"
osacompile -o "${OUT_DIR}/DeerFlowWithDeepSeek Stop.app" "${STOP_SCPT}"

rm -rf "${TMP_DIR}"

echo "[desktop-mac] apps generated:"
echo "  ${OUT_DIR}/DeerFlowWithDeepSeek.app"
echo "  ${OUT_DIR}/DeerFlowWithDeepSeek Stop.app"
