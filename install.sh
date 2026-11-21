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

for item in plugin.json pyproject.toml README.md assets rules agents skills clients shell; do
  if [[ -e "${SCRIPT_DIR}/${item}" ]]; then
    rm -rf "${PLUGIN_DEST:?}/${item}"
    cp -R "${SCRIPT_DIR}/${item}" "${PLUGIN_DEST}/${item}"
  fi
done

for cfg_file in .tool-versions .tool-plugins .env.example; do
  if [[ -f "${SCRIPT_DIR}/${cfg_file}" ]]; then
    cp "${SCRIPT_DIR}/${cfg_file}" "${PLUGIN_DEST}/${cfg_file}"
  fi
done

if [[ -f "${SCRIPT_DIR}/hooks.json" ]]; then
  python3 - "${SCRIPT_DIR}/hooks.json" "${PLUGIN_DEST}/hooks.json" "${HOME}" <<'PYEOF'
import json, pathlib, sys
src, dst, home_str = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
expanded = src.read_text(encoding="utf-8").replace("$HOME/", f"{home_str}/")
json.loads(expanded)
dst.write_text(expanded, encoding="utf-8")
PYEOF
fi

if [[ -f "${PLUGIN_DEST}/plugin.json" && -f "${PLUGIN_DEST}/clients/git_client.py" ]]; then
  python3 - "${SCRIPT_DIR}" "${PLUGIN_DEST}/plugin.json" "${PLUGIN_DEST}/clients" <<'PYEOF' || true
import pathlib, sys
sys.path.insert(0, sys.argv[3])
import git_client
repo_dir, pjson = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
stamp, _ = git_client.resolve_repo_version_stamp(repo_dir)
pjson.write_bytes(git_client.stamp_plugin_json_bytes(pjson.read_bytes(), stamp))
PYEOF
fi

find "${PLUGIN_DEST}" -name "__pycache__" -type d -prune -exec rm -rf {} + || true
find "${PLUGIN_DEST}" \( -name "*.bak.*" -o -name "*.old" \) -type f -delete || true
chmod 755 "${PLUGIN_DEST}/clients"/*.py 2>/dev/null || true
chmod 755 "${PLUGIN_DEST}/skills"/*/scripts/*.py 2>/dev/null || true
chmod 755 "${PLUGIN_DEST}/shell"/*.sh 2>/dev/null || true

if [[ -x "${PLUGIN_DEST}/shell/agy_dev_queue.sh" ]]; then
  mkdir -p "${HOME}/.local/bin"
  ln -sfn "${PLUGIN_DEST}/shell/agy_dev_queue.sh" "${HOME}/.local/bin/agy-queue"
  ln -sfn "${PLUGIN_DEST}/shell/agy_dev_queue.sh" "${HOME}/.local/bin/agy-doctor"
fi

if [[ -f "${HOME}/.env" ]]; then
  chmod 600 "${HOME}/.env" || true
fi

if [[ "${SKIP_VENV}" == "false" ]]; then
  if [[ ! -d "${SCRIPT_DIR}/.venv" ]] && command -v python3 >/dev/null 2>&1; then
    echo "[install] Creating local virtualenv (.venv) via system/asdf python3..."
    python3 -m venv "${SCRIPT_DIR}/.venv" || true
  fi
  if [[ -x "${SCRIPT_DIR}/.venv/bin/python" ]]; then
    "${SCRIPT_DIR}/.venv/bin/python" -m pip install --quiet --no-build-isolation --no-deps -e "${SCRIPT_DIR}" >/dev/null 2>&1 || true
  fi
  if [[ -e "${SCRIPT_DIR}/.git" ]] && command -v pre-commit >/dev/null 2>&1; then
    echo "[install] Installing git pre-commit hook..."
    (cd "${SCRIPT_DIR}" && pre-commit install >/dev/null 2>&1) || true
  fi
fi

echo "[install] Deployed ${PLUGIN_NAME} cleanly to ${PLUGIN_DEST}"
