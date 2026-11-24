#!/usr/bin/env bash
# ==============================================================================
# Antigravity Multi-Repo Dev Queue TUI & Orchestrator Doctor Wrapper
# (`agy-queue` / `agy-doctor`)
# Portable POSIX/Bash/Zsh thin wrapper for `dev_queue_tui.py` and
# `pr_remediator.py queue-doctor`.
#
# Usage:
#   1. Direct execution / symlinks (`~/.local/bin/agy-queue` & `agy-doctor`):
#        agy-queue [--watch [SECS]] [--sync] [--json] [--version] [--help]
#        agy-queue watch [SECS] [--sync-interval SECS] [--no-auto-sync]
#        agy-queue doctor [--fix] [--wake] [--heal] [--check-only] [--json]
#        agy-queue optimize [--json] [--repos-dir DIR] [--stall-seconds N]
#        agy-queue compact [--apply] [--tier 1|all] [--repo SLUG] [--json]
#        agy-queue plan [--repo SLUG] [--importance TIER] [--include-done N] [--json]
#        agy-queue sync [--offline] [--json]
#        agy-queue list [--status STATUS] [--repo SLUG] [--all] [--json]
#        agy-queue show <ID> [--json]
#        agy-queue locks [--sync-git] [--repo SLUG] [--json]
#        agy-queue set-status <ID> <STATUS> [--notes "..."]
#        agy-queue pause [--reason "..."] [--json]
#        agy-queue resume [--no-wake] [--json]
#        agy-queue status [--json]
#        agy-doctor [--fix] [--wake] [--heal] [--check-only] [--json]
#   2. Shell profile sourcing (Bash or Zsh):
#        source "$HOME/.gemini/config/plugins/agy-dev-plugin/shell/agy_dev_queue.sh"
#        agy-queue [watch|doctor|optimize|sync|list|show|locks|set-status] ...
#        agy-doctor [--heal]
# ==============================================================================

_agy_queue_src=""
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  _agy_queue_src="${BASH_SOURCE[0]}"
elif [[ -n "${ZSH_VERSION:-}" ]]; then
  eval '_agy_queue_src="${(%):-%x}"'
else
  _agy_queue_src="$0"
fi

if [[ -n "${_agy_queue_src}" ]]; then
  _agy_queue_hops=0
  while [[ -L "${_agy_queue_src}" && "${_agy_queue_hops}" -lt 5 ]]; do
    _agy_queue_link="$(readlink "${_agy_queue_src}" 2>/dev/null || true)"
    if [[ -z "${_agy_queue_link}" ]]; then
      break
    fi
    if [[ "${_agy_queue_link}" == /* ]]; then
      _agy_queue_src="${_agy_queue_link}"
    else
      _agy_queue_src="$(dirname "${_agy_queue_src}")/${_agy_queue_link}"
    fi
    _agy_queue_hops=$((_agy_queue_hops + 1))
  done
  unset _agy_queue_link _agy_queue_hops
fi

if [[ -n "${_agy_queue_src}" && -f "${_agy_queue_src}" ]]; then
  _AGY_DEV_QUEUE_WRAPPER_DIR="$(cd "$(dirname "${_agy_queue_src}")" && pwd)"
else
  _AGY_DEV_QUEUE_WRAPPER_DIR=""
fi
unset _agy_queue_src

_agy_queue_resolve_script() {
  local candidate=""

  if [[ -n "${AGY_DEV_QUEUE_TUI_SCRIPT:-}" && -f "${AGY_DEV_QUEUE_TUI_SCRIPT}" ]]; then
    printf '%s\n' "${AGY_DEV_QUEUE_TUI_SCRIPT}"
    return 0
  fi

  if [[ -n "${_AGY_DEV_QUEUE_WRAPPER_DIR:-}" ]]; then
    candidate="${_AGY_DEV_QUEUE_WRAPPER_DIR}/../skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py"
    if [[ -f "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  fi

  candidate="${HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py"
  if [[ -f "${candidate}" ]]; then
    printf '%s\n' "${candidate}"
    return 0
  fi

  return 1
}

_agy_doctor_resolve_remediator() {
  local candidate=""

  if [[ -n "${AGY_PR_REMEDIATOR_SCRIPT:-}" && -f "${AGY_PR_REMEDIATOR_SCRIPT}" ]]; then
    printf '%s\n' "${AGY_PR_REMEDIATOR_SCRIPT}"
    return 0
  fi

  if [[ -n "${_AGY_DEV_QUEUE_WRAPPER_DIR:-}" ]]; then
    candidate="${_AGY_DEV_QUEUE_WRAPPER_DIR}/../skills/agy-code-remediation-pr/scripts/pr_remediator.py"
    if [[ -f "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  fi

  candidate="${HOME}/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py"
  if [[ -f "${candidate}" ]]; then
    printf '%s\n' "${candidate}"
    return 0
  fi

  return 1
}

agy_doctor() {
  local rem_script=""
  rem_script="$(_agy_doctor_resolve_remediator)" || {
    printf '%s\n' "[agy-doctor] Error: pr_remediator.py not found. Run ./install.sh to deploy agy-dev-plugin." >&2
    return 1
  }
  python3 "${rem_script}" queue-doctor "$@"
}

agy_queue() {
  if [[ "${1:-}" == "doctor" ]]; then
    shift
    agy_doctor "$@"
    return $?
  fi

  local tui_script=""
  tui_script="$(_agy_queue_resolve_script)" || {
    printf '%s\n' "[agy-queue] Error: dev_queue_tui.py not found. Run ./install.sh to deploy agy-dev-plugin." >&2
    return 1
  }
  python3 "${tui_script}" "$@"
}

_agy_queue_is_sourced=0
if [[ -n "${ZSH_VERSION:-}" ]]; then
  case "${ZSH_EVAL_CONTEXT:-}" in
    *:file*) _agy_queue_is_sourced=1 ;;
  esac
elif [[ -n "${BASH_VERSION:-}" ]]; then
  if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    _agy_queue_is_sourced=1
  fi
fi

if [[ "${_agy_queue_is_sourced}" -eq 1 ]]; then
  alias agy-queue='agy_queue'
  alias agy-queue-watch='agy_queue --watch'
  alias agy-doctor='agy_doctor'
  unset _agy_queue_is_sourced
else
  unset _agy_queue_is_sourced
  set -euo pipefail
  if [[ "$(basename "$0")" == "agy-doctor" ]]; then
    agy_doctor "$@"
  else
    agy_queue "$@"
  fi
fi
