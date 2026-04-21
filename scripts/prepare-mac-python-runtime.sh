#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Prepare a bundled macOS Python runtime for the Electron desktop app.

Usage:
  prepare-mac-python-runtime.sh [--variant full|thin-no-browser] [--target-dir PATH] [--python-version 3.12]

Environment overrides:
  DEER_FLOW_MAC_PORTABLE_VARIANT
  DEER_FLOW_MAC_PYTHON_TARGET_DIR
  DEER_FLOW_MAC_PYTHON_VERSION
EOF
}

variant="${DEER_FLOW_MAC_PORTABLE_VARIANT:-full}"
target_dir="${DEER_FLOW_MAC_PYTHON_TARGET_DIR:-}"
python_version="${DEER_FLOW_MAC_PYTHON_VERSION:-3.12}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --variant)
      variant="${2:-}"
      shift 2
      ;;
    --target-dir)
      target_dir="${2:-}"
      shift 2
      ;;
    --python-version)
      python_version="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

case "${variant}" in
  full|thin-no-browser)
    ;;
  *)
    echo "Unsupported variant: ${variant}" >&2
    exit 1
    ;;
esac

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script is only for macOS." >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required on PATH to prepare the macOS Python runtime." >&2
  exit 1
fi

if ! command -v ditto >/dev/null 2>&1; then
  echo "ditto is required on PATH to copy the portable Python runtime." >&2
  exit 1
fi

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${target_dir:-${root_dir}/desktop/electron/runtime/python}"
wheelhouse_dir="${root_dir}/desktop/electron/.wheelhouse-mac"
third_party_requirements="${wheelhouse_dir}/requirements-${variant}.txt"
harness_dist="${wheelhouse_dir}/harness-dist"
backend_dist="${wheelhouse_dir}/backend-dist"
playwright_browsers_dir="${output_dir}/ms-playwright"
runtime_info_path="${output_dir}/runtime-build.json"

echo "Preparing DeerFlow macOS Python runtime..."
echo "Output: ${output_dir}"
echo "Variant: ${variant}"
echo "Python: ${python_version}"

mkdir -p "${wheelhouse_dir}"
rm -rf "${output_dir}" "${harness_dist}" "${backend_dist}"

uv python install "${python_version}" >/dev/null

managed_python="$(
  UV_NO_CONFIG=1 uv python find --managed-python --no-project --resolve-links "${python_version}"
)"
managed_python_dir="$(cd "$(dirname "${managed_python}")/.." && pwd)"

mkdir -p "$(dirname "${output_dir}")"
ditto "${managed_python_dir}" "${output_dir}"

runtime_python="${output_dir}/bin/python3"
if [[ ! -x "${runtime_python}" ]]; then
  runtime_python="${output_dir}/bin/python3.12"
fi

if [[ ! -x "${runtime_python}" ]]; then
  echo "Bundled Python executable was not found under ${output_dir}/bin" >&2
  exit 1
fi

# uv-managed CPython carries an EXTERNALLY-MANAGED marker. Once copied into our
# app bundle, it becomes private runtime state and can be safely made writable
# for package installation.
find "${output_dir}/lib" -maxdepth 2 -name 'EXTERNALLY-MANAGED' -delete

(
  cd "${root_dir}/backend"
  uv export \
    --frozen \
    --no-dev \
    --no-emit-local \
    --format requirements-txt \
    > "${third_party_requirements}"
)

"${runtime_python}" -m pip install --require-hashes -r "${third_party_requirements}"
"${runtime_python}" -m pip wheel --no-deps --wheel-dir "${harness_dist}" "${root_dir}/backend/packages/harness"
"${runtime_python}" -m pip install --no-deps --no-index --find-links "${harness_dist}" deerflow-harness
site_packages_dir="$("${runtime_python}" - <<'PY'
import sysconfig
print(sysconfig.get_paths()["purelib"])
PY
)"

remove_site_package_patterns() {
  local dir="$1"
  shift
  local pattern
  shopt -s nullglob
  for pattern in "$@"; do
    for candidate in "${dir}"/${pattern}; do
      echo "Pruning site-packages entry: $(basename "${candidate}")"
      rm -rf "${candidate}"
    done
  done
  shopt -u nullglob
}

pruned_patterns=()
if [[ "${variant}" == "thin-no-browser" ]]; then
  pruned_patterns=(
    "sympy*"
    "pandas*"
    "speech_recognition*"
    "onnxruntime*"
    "kubernetes*"
    "volcengine*"
    "youtube_transcript_api*"
  )
  rm -rf "${playwright_browsers_dir}"
  remove_site_package_patterns "${site_packages_dir}" "${pruned_patterns[@]}"
else
  mkdir -p "${playwright_browsers_dir}"
  PLAYWRIGHT_BROWSERS_PATH="${playwright_browsers_dir}" \
    "${runtime_python}" -m playwright install chromium

  if ! find "${playwright_browsers_dir}" -maxdepth 1 -type d -name 'chromium-*' | grep -q .; then
    echo "Playwright Chromium install failed under ${playwright_browsers_dir}" >&2
    exit 1
  fi
fi

PRUNED_PATTERNS_JSON="[]"
if ((${#pruned_patterns[@]} > 0)); then
  PRUNED_PATTERNS_JSON="$("${runtime_python}" - <<'PY' "${pruned_patterns[@]}"
import json
import sys
print(json.dumps(sys.argv[1:]))
PY
)"
fi

PLAYWRIGHT_BROWSER_MODE="bundled"
if [[ "${variant}" == "thin-no-browser" ]]; then
  PLAYWRIGHT_BROWSER_MODE="system"
fi

cat >"${runtime_info_path}" <<EOF
{
  "variant": "${variant}",
  "generatedAt": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "playwrightBrowserMode": "${PLAYWRIGHT_BROWSER_MODE}",
  "prunedSitePackagePatterns": ${PRUNED_PATTERNS_JSON}
}
EOF

echo ""
echo "Python runtime prepared."
echo "Bundled runtime: ${runtime_python}"
if [[ "${variant}" == "full" ]]; then
  echo "Playwright browser mode: bundled Chromium"
else
  echo "Playwright browser mode: system Chrome or Edge required at runtime"
fi
echo "Runtime metadata: ${runtime_info_path}"
