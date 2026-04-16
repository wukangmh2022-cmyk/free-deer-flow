#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DESKTOP_DIR="${ROOT_DIR}/desktop/electron"

if ! command -v pnpm >/dev/null 2>&1; then
  echo "pnpm not found. Please install pnpm first."
  exit 1
fi

echo "[build-windows-exe] root: ${ROOT_DIR}"
echo "[build-windows-exe] desktop: ${DESKTOP_DIR}"

cd "${DESKTOP_DIR}"

if [ ! -d "node_modules" ]; then
  echo "[build-windows-exe] installing dependencies..."
  pnpm install --frozen-lockfile
fi

echo "[build-windows-exe] building Windows NSIS installer (.exe)..."
pnpm run dist:win

echo "[build-windows-exe] build done. artifacts:"
find "${DESKTOP_DIR}/dist" -maxdepth 2 -type f \( -name "*.exe" -o -name "*.yml" -o -name "*.blockmap" \) | sort
