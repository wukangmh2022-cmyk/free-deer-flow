#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="${ROOT_DIR}/desktop/electron"
DIST_DIR="${DESKTOP_DIR}/dist"
UNPACKED_DIR="${DIST_DIR}/win-unpacked"

to_native_path() {
  local target="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$target"
  else
    printf '%s' "$target"
  fi
}

DESKTOP_PACKAGE_JSON_NATIVE="$(to_native_path "${DESKTOP_DIR}/package.json")"
VERSION="${1:-$(DESKTOP_PACKAGE_JSON="${DESKTOP_PACKAGE_JSON_NATIVE}" node -p "require(process.env.DESKTOP_PACKAGE_JSON).version")}"
PRODUCT_NAME="$(DESKTOP_PACKAGE_JSON="${DESKTOP_PACKAGE_JSON_NATIVE}" node -p "require(process.env.DESKTOP_PACKAGE_JSON).build.productName")"
RELEASE_NAME="${PRODUCT_NAME}-${VERSION}-windows-x64-portable"
RELEASE_DIR="${DIST_DIR}/${RELEASE_NAME}"
ZIP_PATH="${DIST_DIR}/${RELEASE_NAME}.zip"
PREPARE_PYTHON_RUNTIME_SCRIPT_NATIVE="$(to_native_path "${ROOT_DIR}/scripts/prepare-windows-python-runtime.ps1")"
RELEASE_DIR_NATIVE="$(to_native_path "${RELEASE_DIR}")"
ZIP_PATH_NATIVE="$(to_native_path "${ZIP_PATH}")"

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
"${POWERSHELL_BIN}" -ExecutionPolicy Bypass -File "${PREPARE_PYTHON_RUNTIME_SCRIPT_NATIVE}"

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
  RELEASE_DIR_NATIVE="${RELEASE_DIR_NATIVE}" ZIP_PATH_NATIVE="${ZIP_PATH_NATIVE}" \
    "${POWERSHELL_BIN}" -NoProfile -Command '
      $releaseDir = $env:RELEASE_DIR_NATIVE
      $zipPath = $env:ZIP_PATH_NATIVE
      if (Test-Path $zipPath) {
        Remove-Item -Force $zipPath
      }
      Compress-Archive -Path (Join-Path $releaseDir "*") -DestinationPath $zipPath -Force
    ' >/dev/null
)

echo "[build-windows-release] build done."
echo "[build-windows-release] portable dir: ${RELEASE_DIR}"
echo "[build-windows-release] portable zip: ${ZIP_PATH}"
