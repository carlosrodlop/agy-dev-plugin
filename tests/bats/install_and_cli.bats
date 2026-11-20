#!/usr/bin/env bats

setup() {
  REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
}

@test "install.sh --help prints usage and succeeds" {
  run "${REPO_ROOT}/install.sh" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"agy-dev-plugin"* ]]
}

@test "install.sh --dry-run exits cleanly without side effects" {
  run "${REPO_ROOT}/install.sh" --dry-run
  [ "$status" -eq 0 ]
  [[ "$output" == *"DRY_RUN=true"* ]]
}

@test "install.sh deploys cleanly into isolated HOME and expands hooks.json" {
  TMP_HOME="$(mktemp -d)"
  run env HOME="${TMP_HOME}" AGY_DEV_STATE_DIR="${TMP_HOME}/.local/share/agy-dev-plugin" "${REPO_ROOT}/install.sh" --skip-venv
  [ "$status" -eq 0 ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/plugin.json" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/hooks.json" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/clients/dev_common.py" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/clients/git_client.py" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py" ]
  run grep -F "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/clients/dev_common.py" "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/hooks.json"
  [ "$status" -eq 0 ]
  rm -rf "${TMP_HOME}"
}

@test "core CLIs respond cleanly to --help" {
  run python3 "${REPO_ROOT}/clients/dev_common.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/clients/git_client.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/pr_remediator.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-project-standards/scripts/project_audit.py" --help
  [ "$status" -eq 0 ]
}
