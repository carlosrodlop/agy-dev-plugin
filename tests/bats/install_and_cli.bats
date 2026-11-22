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
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/README.md" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/.env.example" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/agents/agy-dev-orchestrator.md" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/agents/agy-dev-planner.md" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/agents/agy-dev-executor.md" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/agents/agy-code-developer.md" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/agents/agy-code-pr-reviewer.md" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/clients/dev_common.py" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/clients/git_client.py" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py" ]
  [ -x "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py" ]
  [ -x "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/dev_flow_optimizer.py" ]
  [ -x "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/dev_queue_doctor.py" ]
  [ -x "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/parallel_scope.py" ]
  [ -x "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/rebase_gate.py" ]
  [ -x "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py" ]
  [ -x "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/shell/agy_dev_queue.sh" ]
  [ -f "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/sidecars/dev-queue-watchdog/sidecar.json" ]
  [ -x "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/sidecars/dev-queue-watchdog/watchdog.py" ]
  [ -L "${TMP_HOME}/.local/bin/agy-queue" ]
  [ -L "${TMP_HOME}/.local/bin/agy-doctor" ]
  run python3 "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/sidecars/dev-queue-watchdog/watchdog.py" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"--interval"* ]]
  run env HOME="${TMP_HOME}" "${TMP_HOME}/.local/bin/agy-queue" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"agy-queue"* ]]
  run env HOME="${TMP_HOME}" "${TMP_HOME}/.local/bin/agy-doctor" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"queue-doctor"* ]]
  [[ "$output" == *"--check-only"* ]]
  run env HOME="${TMP_HOME}" "${TMP_HOME}/.local/bin/agy-queue" doctor --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"queue-doctor"* ]]
  run grep -F "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/clients/dev_common.py" "${TMP_HOME}/.gemini/config/plugins/agy-dev-plugin/hooks.json"
  [ "$status" -eq 0 ]
  rm -rf "${TMP_HOME}"
}

@test "shell/agy_dev_queue.sh supports direct execution and Bash sourcing" {
  run "${REPO_ROOT}/shell/agy_dev_queue.sh" --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"dev_queue.json"* ]]
  run "${REPO_ROOT}/shell/agy_dev_queue.sh" doctor --help
  [ "$status" -eq 0 ]
  [[ "$output" == *"queue-doctor"* ]]
  run bash -c "source '${REPO_ROOT}/shell/agy_dev_queue.sh' && agy_queue --help && agy_doctor --help"
  [ "$status" -eq 0 ]
  [[ "$output" == *"dev_queue.json"* ]]
  [[ "$output" == *"queue-doctor"* ]]
}

@test "core CLIs respond cleanly to --help" {
  run python3 "${REPO_ROOT}/clients/dev_common.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/clients/git_client.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/pr_remediator.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/dev_flow_optimizer.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/dev_queue_doctor.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/parallel_scope.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/rebase_gate.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/pr_remediator.py" queue-optimize --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/pr_remediator.py" queue-doctor --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/pr_remediator.py" analyze-overlap --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-code-remediation-pr/scripts/pr_remediator.py" rebase-reverify --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py" --help
  [ "$status" -eq 0 ]
  run python3 "${REPO_ROOT}/skills/agy-project-standards/scripts/project_audit.py" --help
  [ "$status" -eq 0 ]
}
