#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_NAME="agy-dev-plugin"
CONFIG_ROOT="${HOME}/.gemini/config"
PLUGIN_DEST="${CONFIG_ROOT}/plugins/${PLUGIN_NAME}"
STATE_DIR="${AGY_DEV_STATE_DIR:-${HOME}/.local/share/${PLUGIN_NAME}}"

DRY_RUN="false"
SKIP_VENV="false"

for arg in "$@"; do
  case "${arg}" in
    --dry-run) DRY_RUN="true" ;;
    --skip-venv) SKIP_VENV="true" ;;
    --help|-h)
      cat <<'EOF'
Usage: ./install.sh [--dry-run] [--skip-venv]

Deploys agy-dev-plugin into $HOME/.gemini/config/plugins/agy-dev-plugin/
using declarative asdf / Python 3 tooling (Santa Lockdown safe).
EOF
      exit 0
      ;;
    *)
      echo "[install] Unknown option: ${arg}" >&2
      exit 2
      ;;
  esac
done

echo "[install] Source repository: ${SCRIPT_DIR}"
echo "[install] Target plugin root: ${PLUGIN_DEST}"

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[install] DRY_RUN=true: skipping filesystem mutations."
  exit 0
fi

mkdir -p "${CONFIG_ROOT}/plugins" "${PLUGIN_DEST}" "${STATE_DIR}"
chmod 700 "${STATE_DIR}" || true

for item in plugin.json pyproject.toml assets rules agents skills clients; do
  if [[ -e "${SCRIPT_DIR}/${item}" ]]; then
    rm -rf "${PLUGIN_DEST:?}/${item}"
    cp -R "${SCRIPT_DIR}/${item}" "${PLUGIN_DEST}/${item}"
  fi
done

if [[ -f "${SCRIPT_DIR}/hooks.json" ]]; then
  python3 - "${SCRIPT_DIR}/hooks.json" "${PLUGIN_DEST}/hooks.json" "${HOME}" <<'PYEOF'
import pathlib, sys
src, dst, home_str = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
raw = src.read_text(encoding="utf-8")
dst.write_text(raw.replace("$HOME/", f"{home_str}/"), encoding="utf-8")
PYEOF
fi

find "${PLUGIN_DEST}" -name "__pycache__" -type d -prune -exec rm -rf {} + || true
chmod 755 "${PLUGIN_DEST}/clients"/*.py 2>/dev/null || true
chmod 755 "${PLUGIN_DEST}/skills"/*/scripts/*.py 2>/dev/null || true

if [[ -f "${HOME}/.env" ]]; then
  chmod 600 "${HOME}/.env" || true
fi

if [[ "${SKIP_VENV}" == "false" && ! -d "${SCRIPT_DIR}/.venv" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    echo "[install] Creating local virtualenv (.venv) via system/asdf python3..."
    python3 -m venv "${SCRIPT_DIR}/.venv" || true
  fi
fi

echo "[install] Deployed ${PLUGIN_NAME} cleanly to ${PLUGIN_DEST}"
