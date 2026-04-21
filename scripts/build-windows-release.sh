#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="${ROOT_DIR}/desktop/electron"
DIST_DIR="${DESKTOP_DIR}/dist"
VERSION="${1:-$(node -p "require('${DESKTOP_DIR}/package.json').version")}"
PRODUCT_NAME="$(node -p "require('${DESKTOP_DIR}/package.json').build.productName")"
UNPACKED_DIR="${DIST_DIR}/win-unpacked"
RELEASE_NAME="${PRODUCT_NAME}-${VERSION}-windows-x64-portable"
RELEASE_DIR="${DIST_DIR}/${RELEASE_NAME}"
ZIP_PATH="${DIST_DIR}/${RELEASE_NAME}.zip"

if ! command -v pnpm >/dev/null 2>&1; then
  echo "pnpm not found. Please install pnpm first."
  exit 1
fi

if ! command -v powershell >/dev/null 2>&1 && ! command -v pwsh >/dev/null 2>&1; then
  echo "PowerShell not found. Windows Python runtime preparation requires powershell or pwsh."
  exit 1
fi

POWERSHELL_BIN="$(command -v pwsh || command -v powershell)"

echo "[build-windows-release] root: ${ROOT_DIR}"
echo "[build-windows-release] desktop: ${DESKTOP_DIR}"
echo "[build-windows-release] version: ${VERSION}"

cd "${DESKTOP_DIR}"

if [ ! -d "node_modules" ]; then
  echo "[build-windows-release] installing frontend/electron dependencies..."
  pnpm install --frozen-lockfile
fi

echo "[build-windows-release] preparing Windows Python runtime..."
"${POWERSHELL_BIN}" -ExecutionPolicy Bypass -File "${ROOT_DIR}/scripts/prepare-windows-python-runtime.ps1"

echo "[build-windows-release] building portable Windows directory..."
pnpm run dist:win

if [ ! -d "${UNPACKED_DIR}" ]; then
  echo "[build-windows-release] expected output missing: ${UNPACKED_DIR}"
  exit 1
fi

rm -rf "${RELEASE_DIR}"
mkdir -p "${RELEASE_DIR}"
cp -R "${UNPACKED_DIR}/." "${RELEASE_DIR}/"

mkdir -p "${RELEASE_DIR}/resources/runtime-data"
cat > "${RELEASE_DIR}/README-portable.txt" <<EOF
${PRODUCT_NAME} Windows Portable

Usage:
1. Keep the whole folder structure intact.
2. Run ${PRODUCT_NAME}.exe directly.
3. First-run writable data will be created under:
   resources/runtime-data

Notes:
- This is a portable build. No installer is required.
- Do not move the exe out of this folder.
EOF

rm -f "${ZIP_PATH}"
(
  cd "${DIST_DIR}"
  "${POWERSHELL_BIN}" -NoProfile -Command "Compress-Archive -Path '${RELEASE_NAME}\\*' -DestinationPath '${ZIP_PATH}' -Force" >/dev/null
)

echo "[build-windows-release] build done."
echo "[build-windows-release] portable dir: ${RELEASE_DIR}"
echo "[build-windows-release] portable zip: ${ZIP_PATH}"
