#!/usr/bin/env python3
"""Local-First PR review staging, `record-review`, `evaluate-branch`, overlap & rebase gate (`review_gate.py`).

Extracted from `pr_remediator.py` to isolate subagent plan builders, preview staging,
PR review scoring/circuit-breaker, branch evaluation, and parallel overlap/rebase orchestration.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Sequence
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

for _cand_clients in (
    Path(__file__).resolve().parents[3] / "clients",
    Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin" / "clients",
):
    if _cand_clients.is_dir():
        _clients_str = str(_cand_clients)
        if _clients_str not in sys.path:
            sys.path.insert(0, _clients_str)
        break

_SCRIPT_DIR_STR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR_STR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR_STR)

import dev_common  # noqa: E402
from dev_flow_optimizer import build_branch_diff_verification_gate  # noqa: E402
import git_client as _git_client  # noqa: E402
from git_client import GitVcsIntegrationClient, stamp_repo_version_manifests  # noqa: E402
from lock_store import (  # noqa: E402
    MAX_PR_REVIEW_ITERATIONS,
    PR_DEV_DEFAULT_MODEL,
    PR_DEVELOPER_SUBAGENT_NAME,
    PR_REVIEW_PASS_MIN_SCORE,
    PR_REVIEWER_SUBAGENT_NAME,
    PR_SUBAGENT_MODEL,
    SLOT_STATUS_NEEDS_AGENT_CONFLICT_RESOLUTION,
    SLOT_STATUS_NEEDS_AGENT_SEMANTIC_FIX,
    SLOT_STATUS_NEEDS_REBASE_REVERIFY,
    SLOT_STATUS_READY_AWAITING_BASE_MERGE,
    _extract_repo_slug_from_lock_dict,
    _facade_attr,
    _find_git_root_from_path,
    _lookup_slot_repo_branch_entry,
    _resolve_lock_target_repo_dir,
    _resolve_main_git_repo_root,
    _resolve_repo_dir_for_slug,
    _resolve_repo_slug_and_dir,
    _sanitize_home_paths,
    _secondary_slot_keys,
    _slot_repo_branch_key,
    _utc_now_iso,
    build_dev_dashboard_ui_url,
    build_pro_dashboard_ui_url,
    cleanup_worktree,
    format_remediation_commit_title,
    generate_branch_timestamp,
    get_dev_pr_lock,
    is_frontend_change_detected,
    release_dev_pr_lock,
    reload_dev_api_server,
    restart_pro_api_server,
    stop_dev_api_server,
    update_dev_pr_lock,
)
from merge_watcher import (  # noqa: E402
    cleanup_merged_conversations,
    spawn_pr_merge_watcher_daemon,
)
import orchestration_control as _orch_ctrl  # noqa: E402
from parallel_scope import (  # noqa: E402
    TaskAnchorSpec,
    analyze_candidates_overlap,
    analyze_pair_overlap,
)
from queue_store import (  # noqa: E402
    _matches_cid,
    _normalize_str_list,
    _sync_active_lock_to_dev_queue,
    load_dev_queue,
    update_dev_queue_item_status,
)
from rebase_gate import (  # noqa: E402
    OUTCOME_AUTO_PROMOTED,
    OUTCOME_NEEDS_SEMANTIC_FIX,
    rebase_and_reverify,
)
from worktree_ops import (  # noqa: E402
    _clean_git_subprocess_env,
    _format_verification_checks_markdown,
    _resolve_valid_verification_results,
    _snapshot_worktree_state,
)

def build_code_developer_subagent_plan(
    conversation_id: str,
    action_id: str = "",
    rca_summary: str = "",
    proposed_fix: str = "",
    target_files: list[str] | None = None,
    sibling_ids: list[str] | None = None,
    scheduled: bool = True,
    model: str = "",
    anchored_context: str = "",
    workdir: str = "",
    repo_slug: str = "",
    review_iteration: int = 1,
    risk_tier: str = "",
    lines_changed: int | None = None,
    portfolio_dir: Path | str | None = None,
    live_quotas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the `subagent_definition`, `subagent_invocation`, `risk_classification`, and `model_routing` for `agy-code-developer`.

    Uses 0-LLM 3-tier change risk classification (`micro` -> `flash_lite`, `standard` -> `flash`,
    `critical` -> highest-quota high-reasoning pool) and automatically escalates Round 2+ remediation
    (`review_iteration >= 2`) to `pro`/high-reasoning while applying Highest-Quota Cross-Family
    Balancing, preventive low-quota Eco (`<= 25%`), and 429 cooldown fallback.
    """
    cid_clean = str(conversation_id or "").strip()
    files_list = [str(f).strip() for f in (target_files or []) if str(f).strip()]
    sib_list = [str(s).strip() for s in (sibling_ids or []) if str(s).strip()]
    eff_round = max(1, int(review_iteration or 1))
    explicit_model = str(model or "").strip()
    explicit_tier = str(risk_tier or "").strip().lower()
    risk_cls = dev_common.classify_change_risk_tier(
        files_list,
        lines_changed=lines_changed,
        title=action_id,
        proposed_fix=proposed_fix,
    )
    if explicit_tier in ("micro", "standard", "critical"):
        eff_risk_tier = explicit_tier
        risk_cls = {**risk_cls, "risk_tier": explicit_tier}
    elif files_list:
        eff_risk_tier = str(risk_cls.get("risk_tier") or "")
    else:
        eff_risk_tier = ""

    escalated_from_flash = bool(not explicit_model and eff_round >= 2)
    if explicit_model:
        target_model = explicit_model
    elif eff_round >= 2:
        target_model = PR_SUBAGENT_MODEL
    elif eff_risk_tier == "micro":
        target_model = "flash_lite"
    elif eff_risk_tier == "critical":
        target_model = ""
    else:
        target_model = PR_DEV_DEFAULT_MODEL

    routing = dev_common.select_next_optimal_model(
        role="developer",
        preferred_model=target_model,
        live_quotas=live_quotas,
        state_dir=portfolio_dir,
        review_iteration=eff_round,
        risk_tier=eff_risk_tier,
    )
    routing["escalated_from_flash"] = escalated_from_flash
    routing["review_iteration"] = eff_round

    default_tier_fallback = (
        "flash_lite"
        if eff_risk_tier == "micro"
        else (PR_SUBAGENT_MODEL if (eff_round >= 2 or eff_risk_tier == "critical") else PR_DEV_DEFAULT_MODEL)
    )
    if routing.get("all_exhausted"):
        resolved_model = dev_common.map_model_to_subagent_tier(target_model, default=default_tier_fallback)
    elif routing.get("mode") == "normal" and target_model.lower() in ("pro", "flash", "flash_lite", "inherit"):
        resolved_model = target_model.lower()
    else:
        resolved_model = str(routing.get("subagent_model") or default_tier_fallback)

    selected_dev_model = str(routing.get("selected_model") or resolved_model or PR_DEV_DEFAULT_MODEL)
    dev_model_arg = f' --dev-model "{selected_dev_model}"' if selected_dev_model else ""
    sched_flag = " --scheduled" if scheduled else ""
    files_arg = (" --target-files " + " ".join(f'"{f}"' for f in files_list)) if files_list else ""
    sib_arg = (" --sibling-ids " + " ".join(sib_list)) if sib_list else ""
    workdir_arg = f' --workdir "{workdir.strip()}"' if str(workdir or "").strip() else ""
    repo_line = f"- **Target Repository**: `{repo_slug.strip()}`\n" if str(repo_slug or "").strip() else ""
    anchored_clean = str(anchored_context or "").strip()
    routing_note = ""
    if routing.get("requires_swarm_dispatch") and routing.get("selected_model"):
        routing_note = (
            f"- **Model Routing ({routing.get('mode')}, tier={eff_risk_tier or 'standard'})**: Target `{target_model or eff_risk_tier}` routed to "
            f"`{routing['selected_model']}` (invoke via `swarm(action=\"add\", ..., model=\"{routing['selected_model']}\")` "
            f"or fallback tier `Model=\"{resolved_model}\"`).\n"
        )

    system_prompt = (
        f"You are `{PR_DEVELOPER_SUBAGENT_NAME}`, the dedicated VCS-Interoperable Native Git Code Developer Agent "
        f"for `agy-dev-plugin` and companion repositories (`agy-tam-plugin`, `g-control-tower`, etc.).\n"
        f"You are invoked whenever a user prompt or incident audit requires a code, script, configuration, or `SKILL.md` change.\n"
        f"You MUST use pure native `git` only (zero proprietary GitHub/GitLab REST API calls; the human validates the branch and creates the Pull Request manually).\n"
        f"NEVER commit, merge, or push directly to `main`, and NEVER run direct `git push` on an unvalidated feature branch before `{PR_REVIEWER_SUBAGENT_NAME}` passes (>90/100).\n"
        f"Your mandatory responsibilities:\n"
        f"1. Consume coordinator-provided file anchors, exact line ranges, and contracts cleanly without redundant full-repo discovery; inspect `rca` only if additional root-cause detail is missing.\n"
        f"2. Create or reuse a timestamped branch (`fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>`) and acquire the per-repository lock (`dev_pr_locks[repo_slug]`) via:\n"
        f"   `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py start-remediation --id <CONVERSATION_ID> --action-id <ACTION_ID> --workdir <REPO_DIR> --rca-summary \"...\" --proposed-fix \"...\" --worktree`\n"
        f"   - If `status == \"locked\"` on this repository, `dev_queue.json` records the blocked item automatically; stop immediately and return the active lock status.\n"
        f"3. Implement modular Zero-PII code/skill fixes and hermetic unit test(s) in `tests/`, iterating quickly via targeted `python3 -m unittest <target>` checks.\n"
        f"4. Once targeted tests pass, run the single-pass final local gate (`pr_remediator.py test-local --workdir <WORKTREE_PATH> --with-pre-commit`) until `passed == true` so deterministic `verification_results` are persisted on `dev_pr_lock`.\n"
        f"5. Stage the branch locally for independent review (Local-First Review — commits locally WITHOUT pushing to `origin` yet):\n"
        f"   `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py prepare-review --workdir <WORKTREE_PATH> --title \"fix(<scope>): <summary>\" --executive-summary \"...\"`\n"
        f"6. Return the JSON output (including `branch_name`, `branch_timestamp`, `staged_head_sha`, `reviewer_subagent_definition`, and `reviewer_subagent_invocation`) so `{PR_REVIEWER_SUBAGENT_NAME}` (`Model=\"pro\"` or optimal fallback when quota is low/exhausted) can audit locally and publish ONLY upon `PASS (>90/100)`."
    )

    task_prompt = (
        f"A code/skill change requires a native `git` timestamped branch proposal:\n"
        + repo_line
        + routing_note
        + f"- **Incident / Conversation ID**: `{cid_clean}`\n"
        f"- **Action / Trigger ID**: `{action_id or 'code-change'}`\n"
        + (f"- **Sibling Conversation IDs**: `{', '.join(sib_list)}`\n" if sib_list else "")
        + (f"- **Initial RCA / Rationale**: {rca_summary}\n" if rca_summary else "")
        + (f"- **Initial Proposed Solution**: {proposed_fix}\n" if proposed_fix else "")
        + (f"- **Target Files**: `{', '.join(files_list)}`\n" if files_list else "")
        + (f"- **Pre-Anchored Context & Contracts**:\n{anchored_clean}\n" if anchored_clean else "")
        + "\nExecute the Local-First Native Git branch workflow now:\n"
        f"1. Run `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py start-remediation "
        f"--id {cid_clean} --action-id \"{action_id or 'code-change'}\"{workdir_arg}{dev_model_arg} "
        f"--rca-summary \"<RCA_SUMMARY>\" --proposed-fix \"<PROPOSED_FIX>\"{files_arg}{sib_arg} --worktree`.\n"
        f"   - If `status == \"locked\"`, abort immediately and return the lock status.\n"
        f"2. Implement modular fixes and hermetic unit tests on the worktree branch, iterate with targeted `python3 -m unittest <target>`, then run single-pass `pr_remediator.py test-local --workdir <WORKTREE_PATH> --with-pre-commit` until `passed == true`.\n"
        f"3. Stage locally for review (without pushing to origin yet) via `pr_remediator.py prepare-review --workdir <WORKTREE_PATH> --title \"fix: remediate {action_id or cid_clean[:8]}\" --executive-summary \"...\"{sched_flag}`.\n"
        f"4. Return the full JSON result so `{PR_REVIEWER_SUBAGENT_NAME}` can immediately audit the local branch and publish only on `PASS (>90/100)`."
    )

    return {
        "risk_classification": risk_cls,
        "model_routing": routing,
        "subagent_definition": {
            "Name": PR_DEVELOPER_SUBAGENT_NAME,
            "Description": (
                "VCS-Interoperable Native Git Code Developer Agent invoked when a code change is required (via audit or user prompt). "
                "Creates a timestamped git branch, acquires per-repo dev_pr_locks entry, implements and tests the change locally, and stages it locally for independent PASS>90 PR review."
            ),
            "SystemPrompt": system_prompt,
            "EnableWriteTools": True,
            "EnableMCPTools": True,
            "EnableSubagentTools": False,
        },
        "subagent_invocation": {
            "TypeName": PR_DEVELOPER_SUBAGENT_NAME,
            "Role": "Native Git Remediation Branch Developer",
            "Model": resolved_model,
            "Prompt": task_prompt,
        },
    }


def build_pr_reviewer_subagent_plan(
    pr_number: int | None = None,
    pr_url: str = "",
    issue_number: int | None = None,
    issue_url: str = "",
    conversation_id: str = "",
    branch_name: str = "",
    branch_timestamp: str = "",
    rca_summary: str = "",
    proposed_fix: str = "",
    scheduled: bool = True,
    workdir: str = "",
    repo_slug: str = "",
    model: str = "",
    risk_tier: str = "",
    files_changed: list[Any] | None = None,
    lines_changed: int | None = None,
    dev_model: str = "",
    portfolio_dir: Path | str | None = None,
    live_quotas: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the `subagent_definition`, `subagent_invocation`, `risk_classification`, and `model_routing` for `agy-code-pr-reviewer`."""
    _ = pr_number, issue_number, scheduled, pr_url, issue_url
    resolved_ts = branch_timestamp or ""
    wd_flag = f' --workdir "{workdir.strip()}"' if str(workdir or "").strip() else ""
    repo_line = f"- **Target Repository**: `{repo_slug.strip()}`\n" if str(repo_slug or "").strip() else ""

    explicit_model = str(model or "").strip()
    explicit_tier = str(risk_tier or "").strip().lower()
    risk_cls = dev_common.classify_change_risk_tier(
        files_changed,
        lines_changed=lines_changed,
        title=branch_name,
        proposed_fix=proposed_fix,
    )
    if explicit_tier in ("micro", "standard", "critical"):
        eff_risk_tier = explicit_tier
        risk_cls = {**risk_cls, "risk_tier": explicit_tier}
    elif files_changed:
        eff_risk_tier = str(risk_cls.get("risk_tier") or "")
    else:
        eff_risk_tier = ""

    preferred = explicit_model if explicit_model else ("" if eff_risk_tier else PR_SUBAGENT_MODEL)
    routing = dev_common.select_next_optimal_model(
        role="reviewer",
        preferred_model=preferred,
        live_quotas=live_quotas,
        state_dir=portfolio_dir,
        risk_tier=eff_risk_tier,
        dev_model=str(dev_model or "").strip(),
    )
    fallback_tier = "flash" if eff_risk_tier == "micro" else PR_SUBAGENT_MODEL
    if routing.get("all_exhausted"):
        subagent_model = dev_common.map_model_to_subagent_tier(preferred or fallback_tier, default=fallback_tier)
    elif routing.get("mode") == "normal" and preferred.lower() in ("pro", "flash", "flash_lite", "inherit"):
        subagent_model = preferred.lower()
    else:
        subagent_model = str(routing.get("subagent_model") or fallback_tier)

    selected_model = str(routing.get("selected_model") or preferred or PR_SUBAGENT_MODEL)
    model_flag = (
        f' --reviewer-model "{selected_model}"'
        if selected_model and selected_model.lower() != PR_SUBAGENT_MODEL
        else ""
    )
    routing_note = ""
    if routing.get("mode") not in ("normal", "") or eff_risk_tier:
        cross_tag = ", cross-family" if routing.get("cross_family_matched") else ""
        routing_note = (
            f"- **Reviewer Model Routing ({routing.get('mode')}{cross_tag}, tier={eff_risk_tier or 'default'})**: "
            f"Selected `{selected_model}` (pass `--reviewer-model \"{selected_model}\"` to `record-review`; "
            f"if `requires_swarm_dispatch`, dispatch via `swarm(action=\"add\", ..., model=\"{selected_model}\")`).\n"
        )

    system_prompt = (
        f"You are `{PR_REVIEWER_SUBAGENT_NAME}`, an independent Native Git Branch Quality & Security Evaluator Agent "
        f"(Maker-Checker pattern) for `agy-dev-plugin` and companion repositories (`agy-tam-plugin`, `g-control-tower`, etc.).\n"
        f"You run in a completely isolated conversation context right after a timestamped branch is staged locally (`AWAITING_AGENT_REVIEW`).\n"
        f"Hard PR Readiness Gate: ONLY `verdict == \"PASS\"` with `score > 90` (`>= {PR_REVIEW_PASS_MIN_SCORE}/100`) publishes `origin/<branch>` and exposes `manual_pr_url`.\n"
        f"Your mandatory responsibilities (Lean Reviewer Protocol — Target 3-5 turns):\n"
        f"1. Inspect the local staged branch diff, changed files, and embedded `verification_gate` against `origin/main` using pure native `git` via:\n"
        f"   `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py branch-diff --branch <BRANCH_NAME>{wd_flag}`\n"
        f"   - When `verification_gate.skip_redundant_test_rerun == true` (`matches_head == true` AND `passed == true`), DO NOT re-run `pre-commit` or the full `unittest` suite; focus directly on diff logic, security/Zero-PII, and test coverage quality.\n"
        f"2. Independently evaluate the proposed branch changes across 4 criteria (0-100 score) and determine your verdict (`PASS`, `WARN`, or `NEEDS_REVISION`):\n"
        f"   - Criterion A (40 pts): Root Cause / Requirement Resolution — Does the diff accurately implement the change without regressions?\n"
        f"   - Criterion B (25 pts): Unit Test Coverage — Are new or updated deterministic unit tests included in `tests/`?\n"
        f"   - Criterion C (20 pts): Security & Zero-PII Guardrails — Zero hardcoded customer names, emails, `/Users/<user>/` paths, or secrets.\n"
        f"   - Criterion D (15 pts): Code Style, Typing & Documentation — Clean English docstrings, type hints, and plugin / `$HOME` parity.\n"
        f"3. Record your structured verdict via `record-review` (which publishes `origin/<branch>`, starts `watch-pr-merge`, and surfaces `manual_pr_url` ONLY if `PASS` and `score > 90` with zero PR Ready emails; otherwise keeps the branch local and returns `developer_remediation_plan` for auto-iteration up to {MAX_PR_REVIEW_ITERATIONS} rounds, dispatching a `[🟥]` KO escalation alert only at round >= {MAX_PR_REVIEW_ITERATIONS} when `--send-email` is passed):\n"
        f"   `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py record-review --branch <BRANCH_NAME>{wd_flag}{model_flag} --verdict <PASS|WARN|NEEDS_REVISION> --score <0-100> --summary \"...\" --send-email`\n"
        f"4. Return the JSON evaluation summary (`ready_for_human_pr`, `needs_reiteration`, `manual_pr_url` only when `ready_for_human_pr == true`, or `developer_remediation_plan` otherwise)."
    )

    task_prompt = (
        f"Evaluate the locally staged timestamped branch `{branch_name}` (Timestamp: `{resolved_ts}`):\n"
        + repo_line
        + routing_note
        + f"- **Conversation ID**: `{conversation_id}`\n"
        f"- **Proposed Branch**: `{branch_name}` (Local-First Review — NOT yet published to `origin`)\n"
        f"- **Branch Timestamp**: `{resolved_ts}`\n"
        f"- **Root Cause / Rationale**: {rca_summary}\n"
        f"- **Proposed Solution**: {proposed_fix}\n\n"
        f"Execute the following steps:\n"
        f"1. Run `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py branch-diff --branch \"{branch_name}\"{wd_flag}` to inspect the exact local diff and files changed via native `git`.\n"
        f"2. Score the branch changes (0-100) across Resolution Accuracy, Unit Tests, Zero-PII Security, and Code Quality, and give your verdict (`PASS`, `WARN`, or `NEEDS_REVISION`).\n"
        f"3. Run `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py record-review "
        f"--branch \"{branch_name}\"{wd_flag}{model_flag} --verdict <PASS|WARN|NEEDS_REVISION> --score <SCORE> "
        f"--summary \"<Your detailed technical review comment>\" --send-email`.\n"
        f"4. Return the full JSON evaluation summary (never surface any PR URL unless `ready_for_human_pr == true`)."
    )

    return {
        "risk_classification": risk_cls,
        "model_routing": routing,
        "subagent_definition": {
            "Name": PR_REVIEWER_SUBAGENT_NAME,
            "Description": (
                "Independent Native Git Branch Reviewer Agent (agy-code-pr-reviewer) that evaluates locally staged timestamped branch diffs, "
                "unit tests, and security guardrails via native git, enforces the PASS>90/100 PR readiness gate, and publishes the branch only when approved."
            ),
            "SystemPrompt": system_prompt,
            "EnableWriteTools": True,
            "EnableMCPTools": True,
            "EnableSubagentTools": False,
        },
        "subagent_invocation": {
            "TypeName": PR_REVIEWER_SUBAGENT_NAME,
            "Role": "Independent Native Git Branch Reviewer",
            "Model": subagent_model,
            "Prompt": task_prompt,
        },
    }


def build_canonical_pr_description(
    *,
    commit_title: str = "",
    executive_summary: str = "",
    rca_summary: str = "",
    proposed_fix: str = "",
    custom_body: str = "",
    branch_name: str = "",
    branch_timestamp: str = "",
    conversation_id: str = "",
    files_changed: list[dict[str, Any]] | None = None,
    verification_results: dict[str, Any] | None = None,
    reviewer_verdict: str = "",
    reviewer_score: int | None = None,
    reviewer_summary: str = "",
    reviewer_subagent_id: str = "",
    review_iteration: int | None = None,
    max_review_iterations: int = MAX_PR_REVIEW_ITERATIONS,
) -> str:
    """Build a deterministic, VCS-agnostic Pull Request description guaranteeing `## 📋 Executive Summary`, `## 🧪 Automated Verification Checks`, and `## 🤖 Independent PR Review`.

    This canonical Markdown body is written into both:
      1. The Git branch HEAD commit body (`git commit --amend -m <title> -m <body>`) so GitHub, GitLab, Bitbucket,
         and Gitea auto-populate the PR description box from the branch commit.
      2. The URL query parameters (`?expand=1&title=...&body=...`) of `manual_pr_url` so multi-commit branches
         also auto-populate the PR comment box on click.
    """
    clean_exec = _sanitize_home_paths((executive_summary or "").strip())
    clean_custom = _sanitize_home_paths((custom_body or "").strip())
    clean_rca = _sanitize_home_paths((rca_summary or "").strip())
    clean_fix = _sanitize_home_paths((proposed_fix or "").strip())
    reviewer_summary = _sanitize_home_paths((reviewer_summary or "").strip())

    if not clean_exec:
        if clean_custom and "## 📋 Executive Summary" not in clean_custom and "## Executive Summary" not in clean_custom:
            clean_exec = clean_custom
        elif clean_fix and not clean_fix.startswith("## 📋 Executive Summary"):
            clean_exec = clean_fix
        elif clean_rca:
            clean_exec = clean_rca
        else:
            clean_exec = commit_title or f"Update on branch {branch_name}"

    _ = files_changed
    sections: list[str] = [
        "## 📋 Executive Summary",
        "",
    ]
    if clean_rca and clean_rca != clean_exec and clean_rca != "Tool/step execution error":
        sections.append(f"- **Context & Objective**: {clean_rca}")
    sections.append(f"- **Summary of Changes**: {clean_exec}")
    if branch_name:
        sections.append(
            f"- **Branch**: `{branch_name}`"
            + (f" (Timestamp `{branch_timestamp}`)" if branch_timestamp else "")
            + (f" · Conversation `{conversation_id}`" if conversation_id else "")
        )

    if (
        clean_custom
        and clean_custom != clean_exec
        and "## 📋 Executive Summary" not in clean_custom
        and "## Native Git Timestamped Branch Proposal" not in clean_custom
    ):
        sections.extend(["", "### Additional Implementation Notes", "", clean_custom])

    if is_frontend_change_detected(files_changed):
        pro_url = _facade_attr("build_pro_dashboard_ui_url", build_pro_dashboard_ui_url)("health", ensure_server=False)
        sections.extend(
            [
                "",
                "## 🖥️ Frontend Live Preview (`PRO` Control Center)",
                "",
                f"- 🟢 **PRO Control Center**: [{pro_url}]({pro_url})",
            ]
        )

    sections.extend(_format_verification_checks_markdown(verification_results))

    sections.extend(
        [
            "",
            f"## 🤖 Independent PR Review (`{PR_REVIEWER_SUBAGENT_NAME}` · `{PR_SUBAGENT_MODEL}`)",
            "",
        ]
    )
    if reviewer_verdict:
        v_norm = reviewer_verdict.strip().upper()
        score_val = int(reviewer_score if reviewer_score is not None else 95)
        passed_gate = v_norm == "PASS" and score_val >= PR_REVIEW_PASS_MIN_SCORE
        v_badge = "🟢 PASS" if passed_gate else ("🟧 WARN" if v_norm == "WARN" else "🟥 NEEDS_REVISION")
        sections.append(f"- **Verdict**: **[ {v_badge} ({score_val}/100) ]**")
        if review_iteration is not None and review_iteration > 0:
            sections.append(f"- **Review Round**: `{review_iteration}/{max_review_iterations}`")
        if reviewer_subagent_id:
            sections.append(f"- **Reviewer Subagent ID**: `{reviewer_subagent_id}`")
        if reviewer_summary:
            sections.append(f"- **Technical Review Summary**: {reviewer_summary.strip()}")
    else:
        sections.append(f"- **Status**: Pending independent evaluation by `{PR_REVIEWER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL})`.")

    return "\n".join(sections).strip() + "\n"


def _resolve_worktree_for_branch(repo_dir: Path, branch_name: str) -> Path | None:
    """Inspect `git worktree list --porcelain` and return the worktree directory where `branch_name` is checked out."""
    if not (repo_dir / ".git").exists() or not branch_name:
        return None
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return None

    target_ref = f"refs/heads/{branch_name.strip().removeprefix('refs/heads/')}"
    current_wt = ""
    for raw_line in (proc.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            current_wt = ""
            continue
        if line.startswith("worktree "):
            current_wt = line[len("worktree ") :].strip()
        elif line.startswith("branch ") and current_wt:
            br_ref = line[len("branch ") :].strip()
            if br_ref == target_ref:
                wt_path = Path(current_wt)
                if wt_path.is_dir():
                    return wt_path.resolve()
    return None


def _sync_branch_head_commit_message_detailed(
    target_repo_dir: Path,
    branch_name: str,
    base_branch: str,
    commit_title: str,
    commit_body: str,
) -> dict[str, Any]:
    """Worktree-aware HEAD commit message amend returning structured `{amended, effective_repo_dir, head_sha, error}`."""
    if not (target_repo_dir / ".git").exists() or not branch_name or branch_name in ("main", "master"):
        return {
            "amended": False,
            "effective_repo_dir": target_repo_dir,
            "head_sha": "",
            "error": f"Invalid repository or protected branch for commit amend: repo={target_repo_dir}, branch={branch_name}",
        }

    # Resolve linked worktree where `branch_name` is already checked out to avoid "already used by worktree" errors
    wt_dir = _resolve_worktree_for_branch(target_repo_dir, branch_name)
    effective_dir = wt_dir if wt_dir is not None else target_repo_dir

    cur_proc = subprocess.run(
        ["git", "-C", str(effective_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    original_branch = (cur_proc.stdout or "").strip()
    switched = False
    if original_branch != branch_name:
        co_proc = subprocess.run(
            ["git", "-C", str(effective_dir), "checkout", branch_name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if co_proc.returncode != 0:
            return {
                "amended": False,
                "effective_repo_dir": effective_dir,
                "head_sha": "",
                "error": (co_proc.stderr or co_proc.stdout or f"git checkout {branch_name} failed").strip(),
            }
        switched = True

    amended = False
    new_head_sha = ""
    err_msg = ""
    try:
        base_ref = f"origin/{base_branch}"
        chk_base = subprocess.run(
            ["git", "-C", str(effective_dir), "rev-parse", "--verify", base_ref],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if chk_base.returncode != 0:
            base_ref = base_branch

        cnt_proc = subprocess.run(
            ["git", "-C", str(effective_dir), "rev-list", "--count", f"{base_ref}..{branch_name}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        ahead_count = int((cnt_proc.stdout or "0").strip() or 0) if cnt_proc.returncode == 0 else 0
        if ahead_count <= 0:
            err_msg = f"Branch '{branch_name}' has 0 commits ahead of '{base_ref}'; cannot amend HEAD commit message."
        else:
            am_proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(effective_dir),
                    "commit",
                    "--amend",
                    "--no-verify",
                    "-m",
                    commit_title,
                    "-m",
                    commit_body,
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            amended = am_proc.returncode == 0
            if amended:
                sha_proc = subprocess.run(
                    ["git", "-C", str(effective_dir), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                new_head_sha = (sha_proc.stdout or "").strip() if sha_proc.returncode == 0 else ""
            else:
                err_msg = (am_proc.stderr or am_proc.stdout or "git commit --amend failed").strip()
    finally:
        if switched and original_branch and original_branch != branch_name:
            subprocess.run(
                ["git", "-C", str(effective_dir), "checkout", original_branch],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
    return {
        "amended": amended,
        "effective_repo_dir": effective_dir,
        "head_sha": new_head_sha,
        "error": err_msg,
    }


def _sync_branch_head_commit_message(
    target_repo_dir: Path,
    branch_name: str,
    base_branch: str,
    commit_title: str,
    commit_body: str,
) -> bool:
    """Ensure the HEAD commit on `branch_name` embeds `commit_title` and `commit_body` (`## 📋 Executive Summary`).

    Worktree-aware wrapper returning `True` iff HEAD commit message was amended cleanly.
    """
    return bool(
        _sync_branch_head_commit_message_detailed(
            target_repo_dir=target_repo_dir,
            branch_name=branch_name,
            base_branch=base_branch,
            commit_title=commit_title,
            commit_body=commit_body,
        ).get("amended")
    )


def _stage_preview_plugin_bundle(
    target_repo_dir: Path,
    preview_dir: Path,
) -> bool:
    """Stage active companion/dev plugin branch runtime under `<preview_dir>/plugin` without mutating `$HOME`."""
    dest_plugin = preview_dir / "plugin"
    is_tam_plugin_repo = (target_repo_dir / "plugin.json").is_file() and (
        target_repo_dir / "clients" / "wiz_common.py"
    ).is_file()
    is_dev_plugin_repo = (target_repo_dir / "plugin.json").is_file() and (
        target_repo_dir / "clients" / "dev_common.py"
    ).is_file()
    sibling_tam = target_repo_dir.parent / "agy-tam-plugin"
    sibling_dev = target_repo_dir.parent / "agy-dev-plugin"
    fixture_tar = target_repo_dir / "tests" / "fixtures" / "wiz_tam_plugin_ci.tar.gz"

    source_dir: Path | None = None
    if is_tam_plugin_repo or is_dev_plugin_repo:
        source_dir = target_repo_dir
    elif (sibling_tam / "plugin.json").is_file() and (sibling_tam / "clients" / "wiz_common.py").is_file():
        branch_probe = subprocess.run(
            ["git", "-C", str(sibling_tam), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sibling_branch = (branch_probe.stdout or "").strip() if branch_probe.returncode == 0 else ""
        if sibling_branch and sibling_branch not in ("main", "master", "HEAD"):
            source_dir = sibling_tam

    preview_dir.mkdir(parents=True, exist_ok=True)
    staged_any = False
    if source_dir is not None:
        if dest_plugin.exists():
            shutil.rmtree(dest_plugin, ignore_errors=True)
        dest_plugin.mkdir(parents=True, exist_ok=True)
        for fname in ("plugin.json", "hooks.json", "mcp_config.json"):
            src_f = source_dir / fname
            if src_f.is_file():
                shutil.copy2(src_f, dest_plugin / fname)
        for dname in ("clients", "skills"):
            src_d = source_dir / dname
            if src_d.is_dir():
                shutil.copytree(
                    src_d,
                    dest_plugin / dname,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
        staged_any = True

    # Also stage companion `agy-dev-plugin` when working in sibling repos
    if (sibling_dev / "plugin.json").is_file() and (sibling_dev / "clients" / "dev_common.py").is_file():
        dev_probe = subprocess.run(
            ["git", "-C", str(sibling_dev), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        dev_branch = (dev_probe.stdout or "").strip() if dev_probe.returncode == 0 else ""
        if dev_branch and dev_branch not in ("main", "master", "HEAD"):
            dest_dev = preview_dir / "agy-dev-plugin"
            if dest_dev.exists():
                shutil.rmtree(dest_dev, ignore_errors=True)
            dest_dev.mkdir(parents=True, exist_ok=True)
            for fname in ("plugin.json", "hooks.json"):
                src_f = sibling_dev / fname
                if src_f.is_file():
                    shutil.copy2(src_f, dest_dev / fname)
            for dname in ("clients", "skills"):
                src_d = sibling_dev / dname
                if src_d.is_dir():
                    shutil.copytree(
                        src_d,
                        dest_dev / dname,
                        dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                    )
            staged_any = True

    if staged_any:
        return True

    if fixture_tar.is_file():
        import tarfile

        if dest_plugin.exists():
            shutil.rmtree(dest_plugin, ignore_errors=True)
        dest_plugin.mkdir(parents=True, exist_ok=True)
        dest_resolved = dest_plugin.resolve()
        with tarfile.open(fixture_tar, mode="r:gz") as tf:
            safe_members = []
            for member in tf.getmembers():
                m_name = member.name.lstrip("./")
                if not m_name or member.issym() or member.islnk() or "__pycache__" in m_name.split("/"):
                    continue
                target_path = (dest_plugin / m_name).resolve()
                if target_path == dest_resolved or str(target_path).startswith(str(dest_resolved) + os.sep):
                    safe_members.append(member)
            tf.extractall(dest_plugin, members=safe_members)
        return (dest_plugin / "clients" / "wiz_common.py").is_file() or (dest_plugin / "clients" / "dev_common.py").is_file()

    return False


_PREVIEW_TOP_LEVEL_ASSETS: tuple[str, ...] = ("index.html", "styles.css", "dashboard.js", "favicon.svg")
_PREVIEW_WEB_SUBDIRS: tuple[str, ...] = ("css", "js")


def _sync_preview_subtree(
    src_dir: Path,
    dest_dir: Path,
    *,
    ignore_patterns: tuple[str, ...],
) -> None:
    """Prune stale `dest_dir` and copy `src_dir` with `symlinks=False` when present."""
    if dest_dir.is_symlink() or dest_dir.is_file():
        with contextlib.suppress(OSError):
            dest_dir.unlink()
    elif dest_dir.exists():
        shutil.rmtree(dest_dir, ignore_errors=True)
    if src_dir.is_dir():
        shutil.copytree(
            src_dir,
            dest_dir,
            symlinks=False,
            ignore=shutil.ignore_patterns(*ignore_patterns),
        )


def push_remediation_branch_and_build_reviewer_plan(
    title: str,
    body: str = "",
    executive_summary: str = "",
    head_branch: str = "",
    base_branch: str = "main",
    issue_number: int | None = None,
    scheduled: bool = False,
    workdir: Path | str | None = None,
    repo_slug: str = "",
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> dict[str, Any]:
    """Local-First Review staging (`push-branch` / `prepare-review`): commit changes locally on the timestamped branch (never `main`), stamp canonical `## 📋 Executive Summary` + deterministic `## 🧪 Automated Verification Checks` into the local HEAD commit WITHOUT pushing to `origin`, invalidate prior review state, and return the `agy-code-pr-reviewer` invocation plan."""
    if _orch_ctrl.is_hard_stop_active(portfolio_dir=portfolio_dir):
        return {
            "status": "hard_stopped",
            "paused": True,
            "hard_stopped": True,
            "error": "orchestration_hard_stopped",
            "message": (
                "Orchestration is HARD STOPPED (Hard Freeze): refusing `prepare-review` / `push-branch` staging. "
                "Existing locks and worktrees are preserved until orchestration is resumed."
            ),
        }
    vcs = git_client or GitVcsIntegrationClient()
    scoped_slug, scoped_dir = _resolve_repo_slug_and_dir(
        repo_slug=repo_slug,
        workdir=workdir,
        git_client=vcs,
        prefer_cwd=False,
    )
    lock_info = get_dev_pr_lock(
        portfolio_dir=portfolio_dir,
        repo_slug=repo_slug or (scoped_slug if (repo_slug or workdir) else ""),
        workdir=workdir,
        branch_name=head_branch,
    )
    if not lock_info.get("active"):
        return {
            "status": "error",
            "error": (
                "Cannot stage remediation branch for review without an active development lock (`dev_pr_lock`). "
                "Run `start-remediation` or `acquire-lock` first."
            ),
        }

    wt_str = str(workdir or lock_info.get("worktree_path") or "").strip()
    wt_expanded = Path(wt_str.replace("$HOME", str(Path.home()))).expanduser() if wt_str else None
    is_worktree = bool(wt_expanded and wt_expanded.exists())
    if is_worktree and wt_expanded is not None:
        target_repo_dir = wt_expanded.resolve()
    elif scoped_dir is not None:
        target_repo_dir = scoped_dir
    elif hasattr(vcs, "resolve_active_repo_dir"):
        target_repo_dir = vcs.resolve_active_repo_dir(workdir=workdir, prefer_cwd=True)
    else:
        target_repo_dir = vcs.resolve_repo_dir()

    resolved_repo_slug = _extract_repo_slug_from_lock_dict(
        {"repo_slug": repo_slug or scoped_slug, "target_repo_dir": str(target_repo_dir)}
    )
    if resolved_repo_slug == "default" and lock_info.get("repo_slug"):
        resolved_repo_slug = str(lock_info["repo_slug"])
    slot_num = int(lock_info.get("slot_id") or 1)
    raw_rb = lock_info.get("repo_branches") if isinstance(lock_info.get("repo_branches"), dict) else {}
    repo_entry = _lookup_slot_repo_branch_entry(raw_rb, resolved_repo_slug, slot_num)
    if slot_num > 1:
        resolved_head = (
            head_branch.strip()
            or str(lock_info.get("branch_name") or "").strip()
            or (str(repo_entry.get("branch_name") or "").strip() if isinstance(repo_entry, dict) else "")
        )
    else:
        resolved_head = (
            head_branch.strip()
            or (str(repo_entry.get("branch_name") or "").strip() if isinstance(repo_entry, dict) else "")
            or str(lock_info.get("branch_name") or "").strip()
        )
    if not resolved_head or resolved_head in ("main", "master"):
        return {
            "status": "error",
            "error": f"Refusing to stage protected or empty branch '{resolved_head}' for review.",
        }

    if slot_num > 1:
        resolved_ts = (
            str(lock_info.get("branch_timestamp") or "").strip()
            or (str(repo_entry.get("branch_timestamp") or "").strip() if isinstance(repo_entry, dict) else "")
            or generate_branch_timestamp()
        )
    else:
        resolved_ts = (
            (str(repo_entry.get("branch_timestamp") or "").strip() if isinstance(repo_entry, dict) else "")
            or str(lock_info.get("branch_timestamp") or "").strip()
            or generate_branch_timestamp()
        )
    resolved_issue = issue_number if issue_number is not None else lock_info.get("issue_number")
    cid_clean = str(lock_info.get("conversation_id") or "")
    act_id = str(lock_info.get("action_id") or "")
    rca_summary = str(lock_info.get("summary") or "")
    proposed_fix = str(lock_info.get("proposed_fix") or "")
    preserved_exec_summary = (
        _sanitize_home_paths(executive_summary.strip())
        or str(lock_info.get("executive_summary") or "").strip()
        or proposed_fix
        or rca_summary
    )

    commit_title = format_remediation_commit_title(
        title.strip(),
        action_id=act_id,
        conversation_id=cid_clean,
        branch_timestamp=resolved_ts,
    )
    pre_commit_state = (
        _snapshot_worktree_state(target_repo_dir)
        if (target_repo_dir / ".git").exists()
        else {"head_sha": "", "tree_sha": "", "dirty": False, "content_fingerprint": ""}
    )
    preliminary_vr = _resolve_valid_verification_results(
        lock_info,
        target_repo_dir,
        branch_name=resolved_head,
        pre_commit_state=pre_commit_state,
    )
    preliminary_body = build_canonical_pr_description(
        commit_title=commit_title,
        executive_summary=preserved_exec_summary,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        custom_body=body,
        branch_name=resolved_head,
        branch_timestamp=resolved_ts,
        conversation_id=cid_clean,
        verification_results=preliminary_vr,
    )

    # If target_repo_dir is a real git checkout/worktree, ensure changes are committed locally on resolved_head (never main)
    if (target_repo_dir / ".git").exists():
        wt_for_branch = _resolve_worktree_for_branch(target_repo_dir, resolved_head)
        if wt_for_branch is not None:
            target_repo_dir = wt_for_branch
            is_worktree = (target_repo_dir / ".git").is_file()

        cur_branch_proc = subprocess.run(
            ["git", "-C", str(target_repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        cur_branch = (cur_branch_proc.stdout or "").strip()
        if cur_branch != resolved_head:
            proc_existing_head = subprocess.run(
                ["git", "-C", str(target_repo_dir), "rev-parse", "--verify", f"refs/heads/{resolved_head}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc_existing_head.returncode == 0:
                co_proc = subprocess.run(
                    ["git", "-C", str(target_repo_dir), "checkout", resolved_head],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            else:
                co_proc = subprocess.run(
                    ["git", "-C", str(target_repo_dir), "checkout", "-B", resolved_head],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            if co_proc.returncode != 0:
                return {
                    "status": "error",
                    "step": "checkout_branch",
                    "branch_name": resolved_head,
                    "error": (co_proc.stderr or co_proc.stdout or f"Failed to checkout {resolved_head}").strip(),
                }

        if int(lock_info.get("slot_id") or 1) != 2:
            stamp_repo_version_manifests(target_repo_dir, branch_timestamp=resolved_ts)

        status_proc = subprocess.run(
            ["git", "-C", str(target_repo_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if status_proc.returncode == 0 and status_proc.stdout.strip():
            subprocess.run(
                ["git", "-C", str(target_repo_dir), "add", "-A"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            cm_proc = subprocess.run(
                ["git", "-C", str(target_repo_dir), "commit", "--no-verify", "-m", commit_title, "-m", preliminary_body],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if cm_proc.returncode != 0:
                return {
                    "status": "error",
                    "step": "local_commit",
                    "branch_name": resolved_head,
                    "error": (cm_proc.stderr or cm_proc.stdout or "Failed to commit changes locally").strip(),
                }

        # If not an isolated worktree, ensure local main is kept at origin/main so main is never ahead of origin/main
        if not is_worktree and base_branch in ("main", "master"):
            origin_base_proc = subprocess.run(
                ["git", "-C", str(target_repo_dir), "rev-parse", "--verify", f"origin/{base_branch}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if origin_base_proc.returncode == 0:
                subprocess.run(
                    ["git", "-C", str(target_repo_dir), "branch", "-f", base_branch, f"origin/{base_branch}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )

    diff_res = vcs.get_branch_diff(
        head_branch=resolved_head,
        base_branch=base_branch,
        repo_dir=target_repo_dir,
    )
    if int(diff_res.get("files_count") or 0) == 0:
        return {
            "status": "error",
            "branch_name": resolved_head,
            "base_branch": base_branch,
            "error": (
                f"Refusing to stage branch '{resolved_head}' for review because it has 0 file changes compared to '{base_branch}'. "
                f"All proposed code changes must be committed onto '{resolved_head}' first."
            ),
        }

    valid_vr = _resolve_valid_verification_results(
        lock_info,
        target_repo_dir,
        branch_name=resolved_head,
        pre_commit_state=pre_commit_state,
    )
    pre_review_gate = evaluate_pre_review_fail_fast_gate(
        target_repo_dir if (target_repo_dir / ".git").exists() else None,
        changed_files=diff_res.get("files", []),
        lock_info=lock_info,
        verification_results=valid_vr,
        head_branch=resolved_head,
        head_sha=str(diff_res.get("head_sha") or ""),
        pre_commit_state=pre_commit_state,
    )
    if not pre_review_gate.get("passed"):
        err_msgs = list(pre_review_gate.get("error_messages") or [])
        return {
            "status": "error",
            "step": "pre_review_gate",
            "repo_slug": resolved_repo_slug,
            "slot_id": slot_num,
            "branch_name": resolved_head,
            "base_branch": base_branch,
            "branch_pushed": False,
            "pr_gate_ready": False,
            "manual_pr_url": "",
            "pr_url": "",
            "pre_review_gate": pre_review_gate,
            "error": "; ".join(err_msgs) or "Pre-review fail-fast gate failed.",
        }

    final_body = build_canonical_pr_description(
        commit_title=commit_title,
        executive_summary=preserved_exec_summary,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        custom_body=body,
        branch_name=resolved_head,
        branch_timestamp=resolved_ts,
        conversation_id=cid_clean,
        files_changed=diff_res.get("files", []),
        verification_results=valid_vr,
    )

    staged_head_sha = str(diff_res.get("head_sha") or "")
    if (target_repo_dir / ".git").exists():
        amend_res = _sync_branch_head_commit_message_detailed(
            target_repo_dir=target_repo_dir,
            branch_name=resolved_head,
            base_branch=base_branch,
            commit_title=commit_title,
            commit_body=final_body,
        )
        if not amend_res.get("amended"):
            return {
                "status": "error",
                "step": "amend_local_commit",
                "branch_name": resolved_head,
                "branch_pushed": False,
                "pr_gate_ready": False,
                "manual_pr_url": "",
                "pr_url": "",
                "error": str(amend_res.get("error") or "Failed to amend local HEAD commit message before review."),
            }
        staged_head_sha = str(amend_res.get("head_sha") or staged_head_sha)
        if isinstance(amend_res.get("effective_repo_dir"), Path):
            target_repo_dir = amend_res["effective_repo_dir"]

    if valid_vr and staged_head_sha:
        valid_vr = {**valid_vr, "head_sha": staged_head_sha, "dirty": False}

    frontend_changed = is_frontend_change_detected(diff_res.get("files", []))
    dev_preview_url = (
        _facade_attr("build_dev_dashboard_ui_url", build_dev_dashboard_ui_url)("health")
        if frontend_changed
        else ""
    )
    pro_url = _facade_attr("build_pro_dashboard_ui_url", build_pro_dashboard_ui_url)("health", portfolio_dir=portfolio_dir, ensure_server=False)
    web_src_dir = (
        (target_repo_dir / "web")
        if (target_repo_dir / "web" / "index.html").is_file()
        else (target_repo_dir / "src" / "web")
    )
    api_src_dir = target_repo_dir / "api"
    dev_server_res: dict[str, Any] = {"status": "skipped", "stopped": False}
    preview_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir) / "tmp" / "dev_ui_preview"
    is_plugin_repo = (target_repo_dir / "plugin.json").is_file() and (
        (target_repo_dir / "clients" / "wiz_common.py").is_file()
        or (target_repo_dir / "clients" / "dev_common.py").is_file()
    )
    has_preview_bundle = bool((web_src_dir / "index.html").is_file() or api_src_dir.is_dir())
    has_staged_plugin = False
    is_orch_paused = _orch_ctrl.is_orchestration_paused(portfolio_dir=portfolio_dir)
    if slot_num == 1 and not is_orch_paused:
        if has_preview_bundle:
            dev_server_res = _facade_attr("stop_dev_api_server", stop_dev_api_server)(portfolio_dir=portfolio_dir)
            preview_dir.mkdir(parents=True, exist_ok=True)
            for asset_name in _PREVIEW_TOP_LEVEL_ASSETS:
                src_asset = web_src_dir / asset_name
                if src_asset.is_file():
                    shutil.copy2(src_asset, preview_dir / asset_name)
            for subdir in _PREVIEW_WEB_SUBDIRS:
                _sync_preview_subtree(
                    web_src_dir / subdir,
                    preview_dir / subdir,
                    ignore_patterns=("__pycache__", "*.pyc", ".DS_Store"),
                )
            _sync_preview_subtree(
                api_src_dir,
                preview_dir / "api",
                ignore_patterns=("__pycache__", "*.pyc"),
            )
            has_staged_plugin = _stage_preview_plugin_bundle(target_repo_dir, preview_dir)
        elif is_plugin_repo:
            has_staged_plugin = _stage_preview_plugin_bundle(target_repo_dir, preview_dir)

    returned_to_base = False
    if (target_repo_dir / ".git").is_dir() and base_branch in ("main", "master"):
        co_base = subprocess.run(
            ["git", "-C", str(target_repo_dir), "checkout", base_branch],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if co_base.returncode == 0:
            returned_to_base = True
            subprocess.run(
                ["git", "-C", str(target_repo_dir), "merge", "--ff-only", f"origin/{base_branch}"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

    if slot_num == 1 and not is_orch_paused and (has_preview_bundle or has_staged_plugin):
        for asset_name in _PREVIEW_TOP_LEVEL_ASSETS:
            staged_asset = preview_dir / asset_name
            if staged_asset.is_file():
                with contextlib.suppress(OSError):
                    os.utime(staged_asset, None)
        for subdir in _PREVIEW_WEB_SUBDIRS:
            staged_sub = preview_dir / subdir
            if staged_sub.is_dir():
                for staged_file in staged_sub.rglob("*"):
                    if staged_file.is_file():
                        with contextlib.suppress(OSError):
                            os.utime(staged_file, None)
        # Org-Prod (:8787) Isolation: NEVER restart :8787 on `prepare-review` staging!

    updated_rb = {
        str(k): dict(v)
        for k, v in (raw_rb or {}).items()
        if str(k).strip() and isinstance(v, dict)
    }
    persistent_repo_dir = _resolve_main_git_repo_root(target_repo_dir)
    wt_recorded = (
        _sanitize_home_paths(str(target_repo_dir))
        if (target_repo_dir / ".git").is_file()
        else _sanitize_home_paths(str(lock_info.get("worktree_path") or ""))
    )
    rb_write_key = _slot_repo_branch_key(resolved_repo_slug, slot_num)
    if rb_write_key:
        rb_payload: dict[str, Any] = {
            "repo_slug": resolved_repo_slug,
            "branch_name": resolved_head,
            "branch_timestamp": resolved_ts,
            "target_repo_dir": _sanitize_home_paths(str(persistent_repo_dir)),
            "manual_pr_url": "",
            "branch_pushed": False,
            "head_sha": staged_head_sha,
            "updated_at": _utc_now_iso(),
        }
        if slot_num > 1:
            rb_payload["slot_id"] = slot_num
            rb_payload["worktree_path"] = wt_recorded
        updated_rb[rb_write_key] = rb_payload

    # Local-First Review: DO NOT push to origin or expose manual_pr_url until agy-code-pr-reviewer passes (>90/100)
    updated_lock = update_dev_pr_lock(
        {
            "branch_name": resolved_head,
            "branch_timestamp": resolved_ts,
            "notification_source": "Ad-Hoc",
            "branch_pushed": False,
            "pr_gate_ready": False,
            "reviewed_head_sha": "",
            "staged_head_sha": staged_head_sha,
            "head_sha": staged_head_sha,
            "target_repo_dir": _sanitize_home_paths(str(persistent_repo_dir)),
            "worktree_path": wt_recorded,
            "repo_branches": updated_rb,
            "manual_pr_url": "",
            "pr_url": "",
            "frontend_changed": frontend_changed,
            "dev_preview_url": dev_preview_url,
            "pro_url": pro_url,
            "proposal_title": commit_title,
            "proposal_body": final_body,
            "executive_summary": preserved_exec_summary,
            "verification_results": valid_vr,
            "status": "AWAITING_AGENT_REVIEW",
        },
        portfolio_dir=portfolio_dir,
        repo_slug=resolved_repo_slug,
        workdir=target_repo_dir,
        branch_name=resolved_head,
        slot_id=slot_num,
    )

    if cid_clean:
        with contextlib.suppress(Exception):
            upd_q = update_dev_queue_item_status(
                conversation_id=cid_clean,
                status="in_progress",
                branch_names=[resolved_head],
                strip_pr_urls_for_branches=[resolved_head],
                strip_repo_slug=resolved_repo_slug,
                portfolio_dir=portfolio_dir,
            )
            if not upd_q.get("updated"):
                _sync_active_lock_to_dev_queue(
                    conversation_id=cid_clean,
                    status="in_progress",
                    title=commit_title,
                    action_id=str(lock_info.get("action_id") or ""),
                    repo_slug=resolved_repo_slug,
                    branch_name=resolved_head,
                    rca_summary=rca_summary,
                    proposed_fix=preserved_exec_summary,
                    portfolio_dir=portfolio_dir,
                )

    reviewer_plan = build_pr_reviewer_subagent_plan(
        pr_number=None,
        pr_url="",
        issue_number=int(resolved_issue) if resolved_issue else None,
        issue_url="",
        conversation_id=cid_clean,
        branch_name=resolved_head,
        branch_timestamp=resolved_ts,
        rca_summary=rca_summary,
        proposed_fix=preserved_exec_summary,
        scheduled=scheduled,
        workdir=str(target_repo_dir),
        repo_slug=resolved_repo_slug,
        files_changed=diff_res.get("files", []),
        dev_model=str(lock_info.get("dev_model") or PR_DEV_DEFAULT_MODEL),
        portfolio_dir=portfolio_dir,
    )

    return {
        "status": "ok",
        "vcs_mode": "native_git",
        "repo_slug": resolved_repo_slug,
        "slot_id": slot_num,
        "commit_title": commit_title,
        "branch_name": resolved_head,
        "branch_timestamp": resolved_ts,
        "head_sha": staged_head_sha,
        "staged_head_sha": staged_head_sha,
        "branch_pushed": False,
        "pr_gate_ready": False,
        "awaiting_agent_review": True,
        "returned_to_base_branch": returned_to_base,
        "pr_merge_watcher": {"status": "deferred_until_review_pass", "spawned": False},
        "manual_pr_url": "",
        "pr_url": "",
        "frontend_changed": frontend_changed,
        "dev_preview_url": dev_preview_url,
        "pro_url": pro_url,
        "dev_server": dev_server_res,
        "pr_number": int(resolved_issue or 0),
        "issue_number": resolved_issue,
        "files_changed": diff_res.get("files", []),
        "files_count": diff_res.get("files_count", 0),
        "risk_classification": reviewer_plan["risk_classification"],
        "verification_results": valid_vr,
        "pre_review_gate": pre_review_gate,
        "lock": updated_lock.get("lock", {}),
        "reviewer_model_routing": reviewer_plan["model_routing"],
        "reviewer_subagent_definition": reviewer_plan["subagent_definition"],
        "reviewer_subagent_invocation": reviewer_plan["subagent_invocation"],
    }


# ==============================================================================
# Agent 2 (`agy-code-pr-reviewer`) Evaluation, Hard PR Readiness Gate & Iteration Loop
# ==============================================================================


def _build_developer_remediation_plan(
    *,
    conversation_id: str,
    action_id: str,
    repo_slug: str,
    branch_name: str,
    workdir: str,
    review_iteration: int,
    verdict: str,
    score: int,
    reviewer_feedback: str,
    rca_summary: str,
    proposed_fix: str,
    target_files: list[str] | None = None,
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Build a structured remediation brief escalating `agy-code-developer` to `pro` (or optimal fallback) on Round 2+."""
    next_dev_round = max(2, int(review_iteration or 1) + 1)
    anchored = (
        f"Review Round {review_iteration}/{MAX_PR_REVIEW_ITERATIONS} FAILED PR Readiness Gate "
        f"({verdict} {score}/100 — requires PASS >= {PR_REVIEW_PASS_MIN_SCORE}/100).\n"
        f"- Worktree / Branch: `{workdir}` on `{branch_name}` (reuse active lock/worktree).\n"
        f"- Escalated Remediation Round: Round {next_dev_round} auto-escalated from `flash` to high-reasoning tier.\n"
        f"- Reviewer Critique to Fix:\n{reviewer_feedback}\n"
        f"- Required Steps: (1) address all reviewer findings in `{workdir}`, (2) add/update hermetic tests, "
        f"(3) run `pre-commit run --all-files` + `pr_remediator.py test-local --workdir \"{workdir}\"`, "
        f"(4) re-stage via `pr_remediator.py prepare-review --workdir \"{workdir}\" --title \"...\"`, "
        f"(5) re-invoke `agy-code-pr-reviewer` (`Model=\"pro\"` or quota fallback)."
    )
    dev_subagent = build_code_developer_subagent_plan(
        conversation_id=conversation_id,
        action_id=action_id,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        target_files=target_files,
        scheduled=False,
        anchored_context=anchored,
        workdir=workdir,
        repo_slug=repo_slug,
        review_iteration=next_dev_round,
        portfolio_dir=portfolio_dir,
    )
    return {
        "conversation_id": conversation_id,
        "action_id": action_id,
        "repo_slug": repo_slug,
        "branch_name": branch_name,
        "workdir": _sanitize_home_paths(workdir),
        "review_iteration": review_iteration,
        "next_dev_round": next_dev_round,
        "max_review_iterations": MAX_PR_REVIEW_ITERATIONS,
        "verdict": verdict,
        "score": score,
        "required_min_score": PR_REVIEW_PASS_MIN_SCORE,
        "reviewer_feedback": reviewer_feedback,
        "required_actions": [
            f"Address reviewer findings in worktree `{_sanitize_home_paths(workdir)}` on branch `{branch_name}`.",
            "Add or update deterministic hermetic unit tests covering every fix.",
            f"Run `pre-commit run --all-files` and `pr_remediator.py test-local --workdir \"{_sanitize_home_paths(workdir)}\"`.",
            f"Re-stage locally via `pr_remediator.py prepare-review --workdir \"{_sanitize_home_paths(workdir)}\" --title \"...\"`.",
            "Re-invoke independent `agy-code-pr-reviewer` (`Model=\"pro\"` or optimal fallback) until `ready_for_human_pr == true`.",
        ],
        "developer_model_routing": dev_subagent["model_routing"],
        "developer_subagent_definition": dev_subagent["subagent_definition"],
        "developer_subagent_invocation": dev_subagent["subagent_invocation"],
    }


def _format_pr_notification_subject_summary(
    proposal_title: str,
    executive_summary: str,
    branch_name: str,
    max_len: int = 80,
) -> str:
    """Build a normalized single-line summary (`<= 80` chars) for PR readiness email subjects."""
    raw = (
        str(proposal_title or "").strip()
        or str(executive_summary or "").strip()
        or str(branch_name or "").strip()
        or "branch"
    )
    single_line = re.sub(r"\s+", " ", raw).strip()
    if len(single_line) <= max_len:
        return single_line
    return single_line[: max(1, max_len - 3)].rstrip() + "..."


def record_pr_review_evaluation(
    pr_number: int | None = None,
    verdict: str = "PASS",
    score: int = 95,
    summary: str = "",
    executive_summary: str = "",
    branch_name: str = "",
    reviewer_subagent_id: str = "",
    reviewer_model: str = "",
    dev_model: str = "",
    send_email: bool = False,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    workdir: Path | str | None = None,
    repo_slug: str = "",
) -> dict[str, Any]:
    """Record `agy-code-pr-reviewer`'s evaluation and enforce the Hard PR Readiness Gate (`PASS` AND `score > 90`).

    - On non-PASS or `score <= 90`: suppresses `manual_pr_url`, keeps branch local (never pushed), increments
      `review_iteration`, strips stale compare `pr_urls`, preserves the real Executive Summary, suppresses
      Ready-for-PR email (rounds 1..2 -> `NEEDS_REVISION_ITERATING` + `developer_remediation_plan`;
      round >= 3 -> `ESCALATED_MAX_REVIEW_ITERATIONS` + `hitl_blocked` + `[🟥]` escalation email when `--send-email`).
    - On `PASS` (`score >= 91`): resolves the active worktree via `git worktree list --porcelain`, amends HEAD commit
      with preserved Executive Summary + deterministic `## 🧪 Automated Verification Checks` + `## 🤖 Independent PR Review`,
      pushes to `origin/<branch>` (failing loudly on any amend or push error), sets `pr_gate_ready=True`,
      publishes rich `manual_pr_url`, transitions queue to `pr_open`, spawns `watch-pr-merge`, and never sends
      `status='OK'` PR Ready for Validation emails (`notification_email_sent=False`; `--send-email` is retained
      strictly for Round-3 `[🟥]` KO circuit-breaker escalations).
    """
    if _orch_ctrl.is_hard_stop_active(portfolio_dir=portfolio_dir):
        return {
            "status": "hard_stopped",
            "paused": True,
            "hard_stopped": True,
            "error": "orchestration_hard_stopped",
            "message": (
                "Orchestration is HARD STOPPED (Hard Freeze): refusing `record-review` publish/mutation. "
                "Existing locks and worktrees are preserved until orchestration is resumed."
            ),
        }
    vcs = git_client or GitVcsIntegrationClient()
    score_int = int(score)
    norm_verdict = verdict.strip().upper()
    if norm_verdict not in ("PASS", "WARN", "NEEDS_REVISION"):
        norm_verdict = "PASS" if score_int >= PR_REVIEW_PASS_MIN_SCORE else "NEEDS_REVISION"
    gate_passed = (norm_verdict == "PASS") and (score_int >= PR_REVIEW_PASS_MIN_SCORE)

    lock_info = get_dev_pr_lock(
        portfolio_dir=portfolio_dir,
        repo_slug=repo_slug,
        workdir=workdir,
        branch_name=branch_name,
    )
    resolved_branch = (
        branch_name.strip()
        or str(lock_info.get("branch_name") or "").strip()
        or (f"fix/pr-{pr_number}" if pr_number else "fix/remediation")
    )
    resolved_ts = str(lock_info.get("branch_timestamp") or "").strip()
    if not resolved_ts:
        m_ts = re.search(r"(\d{8}-\d{6})", resolved_branch)
        resolved_ts = m_ts.group(1) if m_ts else generate_branch_timestamp()

    cid_clean = str(lock_info.get("conversation_id") or "")
    act_id = str(lock_info.get("action_id") or "")
    rca_summary = str(lock_info.get("summary") or "")
    proposed_fix = str(lock_info.get("proposed_fix") or "")
    lock_exec_summary = str(lock_info.get("executive_summary") or "").strip()
    head_sha = str(lock_info.get("head_sha") or lock_info.get("staged_head_sha") or "")

    wt_str = str(workdir or lock_info.get("worktree_path") or "").strip()
    wt_expanded = Path(wt_str.replace("$HOME", str(Path.home()))).expanduser() if wt_str else None
    is_worktree = bool(wt_expanded and wt_expanded.exists())
    lock_main_dir = _resolve_lock_target_repo_dir(lock_info)

    inferred_slug = dev_common.normalize_repo_slug(
        repo_slug or str(lock_info.get("repo_slug") or lock_info.get("repo") or "")
    )
    if not inferred_slug and workdir:
        inferred_slug, _ = _resolve_repo_slug_and_dir(
            repo_slug=repo_slug,
            workdir=workdir,
            git_client=git_client,
            prefer_cwd=False,
            portfolio_dir=portfolio_dir,
        )

    target_repo_dir: Path | None = None
    if is_worktree and wt_expanded is not None:
        wt_git_root = _find_git_root_from_path(wt_expanded)
        if wt_git_root is not None:
            target_repo_dir = wt_git_root
    if target_repo_dir is None and lock_main_dir is not None and (lock_main_dir / ".git").exists():
        target_repo_dir = lock_main_dir
    if target_repo_dir is None and inferred_slug and inferred_slug != "default":
        target_repo_dir = _resolve_repo_dir_for_slug(
            inferred_slug,
            portfolio_dir=portfolio_dir,
            git_client=git_client,
        )
    if target_repo_dir is None:
        if workdir and not is_worktree and not lock_info.get("active"):
            target_repo_dir = None
        else:
            target_repo_dir = (
                vcs.resolve_active_repo_dir(workdir=workdir, prefer_cwd=True)
                if hasattr(vcs, "resolve_active_repo_dir")
                else vcs.resolve_repo_dir()
            )
    resolved_repo_slug = inferred_slug or _extract_repo_slug_from_lock_dict(
        {
            "repo_slug": repo_slug or lock_info.get("repo_slug") or lock_info.get("repo"),
            "target_repo_dir": str(target_repo_dir) if target_repo_dir is not None else "",
        }
    )

    existing_commit_subject = ""
    existing_commit_body = ""
    if isinstance(target_repo_dir, Path) and (target_repo_dir / ".git").exists():
        for ref_cand in (resolved_branch, f"origin/{resolved_branch}"):
            proc_log = subprocess.run(
                ["git", "-C", str(target_repo_dir), "log", "-1", "--format=%s%x00%b", ref_cand],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc_log.returncode == 0 and proc_log.stdout:
                log_parts = proc_log.stdout.split("\x00", 1)
                subj_cand = log_parts[0].strip()
                if subj_cand:
                    existing_commit_subject = subj_cand
                    existing_commit_body = log_parts[1].strip() if len(log_parts) > 1 else ""
                    break
        proc_cur_sha = subprocess.run(
            ["git", "-C", str(target_repo_dir), "rev-parse", "--verify", resolved_branch],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc_cur_sha.returncode == 0 and (proc_cur_sha.stdout or "").strip():
            head_sha = (proc_cur_sha.stdout or "").strip()

    proposal_title = (
        str(lock_info.get("proposal_title") or "").strip()
        or existing_commit_subject
        or format_remediation_commit_title(
            f"update {act_id or resolved_branch}",
            action_id=act_id,
            conversation_id=cid_clean,
            branch_timestamp=resolved_ts,
        )
    )

    files_changed: list[dict[str, Any]] = []
    if isinstance(target_repo_dir, Path) and (target_repo_dir / ".git").exists():
        try:
            diff_info = vcs.get_branch_diff(head_branch=resolved_branch, base_branch="main", repo_dir=target_repo_dir)
            if isinstance(diff_info, dict):
                files_changed = list(diff_info.get("files") or diff_info.get("changed_files") or [])
        except Exception:
            files_changed = []

    existing_exec_summary = ""
    if existing_commit_body:
        m_exec = re.search(r"-\s*\*\*Summary of Changes\*\*:\s*(.+)", existing_commit_body)
        if m_exec and m_exec.group(1).strip():
            existing_exec_summary = m_exec.group(1).strip()

    # Strictly preserve real Executive Summary — NEVER overwrite it with reviewer critique `summary`
    resolved_exec_summary = (
        _sanitize_home_paths(executive_summary.strip())
        or lock_exec_summary
        or (proposed_fix if proposed_fix and not proposed_fix.startswith("Inspect the failing CLI command flags") else "")
        or (rca_summary if rca_summary and rca_summary != "Tool/step execution error" else "")
        or existing_exec_summary
        or proposal_title
    )

    current_iteration = int(lock_info.get("review_iteration") or 0) + 1
    existing_history = list(lock_info.get("review_history") or [])
    now_iso = _utc_now_iso()
    effective_reviewer_model = str(reviewer_model or "").strip() or PR_SUBAGENT_MODEL
    effective_dev_model = (
        str(dev_model or "").strip()
        or str(lock_info.get("dev_model") or "").strip()
        or (PR_SUBAGENT_MODEL if current_iteration >= 2 else PR_DEV_DEFAULT_MODEL)
    )
    review_entry = {
        "iteration": current_iteration,
        "verdict": norm_verdict,
        "score": score_int,
        "gate_passed": gate_passed,
        "summary": _sanitize_home_paths(summary.strip()),
        "reviewer_subagent_id": reviewer_subagent_id.strip(),
        "reviewer_model": effective_reviewer_model,
        "dev_model": effective_dev_model,
        "head_sha": head_sha,
        "evaluated_at_utc": now_iso,
    }
    updated_history = [*existing_history, review_entry]

    valid_vr = _resolve_valid_verification_results(
        lock_info,
        target_repo_dir,
        branch_name=resolved_branch,
    )

    canonical_pr_body = build_canonical_pr_description(
        commit_title=proposal_title,
        executive_summary=resolved_exec_summary,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        branch_name=resolved_branch,
        branch_timestamp=resolved_ts,
        conversation_id=cid_clean,
        files_changed=files_changed,
        verification_results=valid_vr,
        reviewer_verdict=norm_verdict,
        reviewer_score=score_int,
        reviewer_summary=summary.strip(),
        reviewer_subagent_id=reviewer_subagent_id.strip(),
        review_iteration=current_iteration,
        max_review_iterations=MAX_PR_REVIEW_ITERATIONS,
    )

    created_by_label = f"{PR_DEVELOPER_SUBAGENT_NAME} ({effective_dev_model}) · {PR_REVIEWER_SUBAGENT_NAME} ({effective_reviewer_model})"
    validator_banner_label = (
        f"{PR_REVIEWER_SUBAGENT_NAME} ({effective_reviewer_model}) [{norm_verdict} {score_int}/100 · Round {current_iteration}/{MAX_PR_REVIEW_ITERATIONS}]"
    )
    recorded_subagents = _normalize_str_list(
        [*(lock_info.get("subagent_conversation_ids") or []), reviewer_subagent_id.strip()]
    )

    slot_num = int(lock_info.get("slot_id") or 1) if lock_info.get("active") else 1
    is_slot2_branch = bool(lock_info.get("active")) and slot_num > 1
    rb_slot_key = _slot_repo_branch_key(resolved_repo_slug, slot_num)
    raw_rb = lock_info.get("repo_branches") if isinstance(lock_info.get("repo_branches"), dict) else {}
    updated_rb = {
        str(k): dict(v)
        for k, v in (raw_rb or {}).items()
        if str(k).strip() and isinstance(v, dict)
    }

    # ==========================================================================
    # HARD GATE PATH A: Review FAILED (non-PASS or score <= 90)
    # ==========================================================================
    if not gate_passed:
        circuit_breaker_tripped = current_iteration >= MAX_PR_REVIEW_ITERATIONS
        lock_status_next = "ESCALATED_MAX_REVIEW_ITERATIONS" if circuit_breaker_tripped else "NEEDS_REVISION_ITERATING"
        queue_status_next = "hitl_blocked" if circuit_breaker_tripped else "in_progress"
        badge = "🟧 WARN" if norm_verdict == "WARN" else "🟥 NEEDS_REVISION"
        worktree_for_plan = str(
            lock_info.get("worktree_path")
            or (str(target_repo_dir) if target_repo_dir is not None else "")
        )

        developer_remediation_plan = _build_developer_remediation_plan(
            conversation_id=cid_clean or resolved_ts,
            action_id=act_id,
            repo_slug=resolved_repo_slug,
            branch_name=resolved_branch,
            workdir=worktree_for_plan,
            review_iteration=current_iteration,
            verdict=norm_verdict,
            score=score_int,
            reviewer_feedback=summary.strip(),
            rca_summary=rca_summary,
            proposed_fix=proposed_fix,
            target_files=list(lock_info.get("target_files") or []),
            portfolio_dir=portfolio_dir,
        )

        review_markdown = (
            f"### {'🚨' if circuit_breaker_tripped else '🔁'} Branch Review Gate NOT Passed (`{PR_REVIEWER_SUBAGENT_NAME}` · `{effective_reviewer_model}`)\n\n"
            f"- **Branch Status**: `{lock_status_next}` (NOT pushed to `origin`, `manual_pr_url` suppressed)\n"
            f"- **Local Branch**: `{resolved_branch}` (Timestamp `{resolved_ts}`)\n"
            f"- **Verdict**: **{badge}** (`{score_int}/100` — requires `PASS >= {PR_REVIEW_PASS_MIN_SCORE}/100`)\n"
            f"- **Review Round**: `{current_iteration}/{MAX_PR_REVIEW_ITERATIONS}`\n"
            f"- **Evaluated At (UTC)**: `{now_iso}`\n"
            + (f"- **Reviewer Subagent ID**: `{reviewer_subagent_id}`\n" if reviewer_subagent_id else "")
            + f"\n{canonical_pr_body}\n"
        )

        if lock_info.get("active"):
            if rb_slot_key and rb_slot_key in updated_rb:
                updated_rb[rb_slot_key]["manual_pr_url"] = ""
                updated_rb[rb_slot_key]["branch_pushed"] = False
                updated_rb[rb_slot_key]["updated_at"] = now_iso
            update_dev_pr_lock(
                {
                    "status": lock_status_next,
                    "branch_action": lock_status_next,
                    "branch_pushed": False,
                    "pr_gate_ready": False,
                    "reviewed_head_sha": "",
                    "manual_pr_url": "",
                    "pr_url": "",
                    "repo_branches": updated_rb,
                    "proposal_title": proposal_title,
                    "executive_summary": resolved_exec_summary,
                    "review_iteration": current_iteration,
                    "review_history": updated_history,
                    "reviewer_verdict": norm_verdict,
                    "reviewer_score": score_int,
                    "reviewer_summary": summary.strip(),
                    "reviewer_subagent_id": reviewer_subagent_id.strip(),
                    "last_reviewer_model": effective_reviewer_model,
                    "subagent_conversation_ids": recorded_subagents,
                    "review_markdown": review_markdown,
                },
                portfolio_dir=portfolio_dir,
                repo_slug=resolved_repo_slug,
                workdir=target_repo_dir,
                branch_name=resolved_branch,
                slot_id=slot_num,
            )
            if cid_clean:
                with contextlib.suppress(Exception):
                    update_dev_queue_item_status(
                        conversation_id=cid_clean,
                        status=queue_status_next,
                        branch_names=[resolved_branch],
                        strip_pr_urls_for_branches=[resolved_branch],
                        strip_repo_slug=resolved_repo_slug,
                        subagent_conversation_ids=recorded_subagents,
                        notes=(
                            f"Circuit breaker tripped after {current_iteration}/{MAX_PR_REVIEW_ITERATIONS} failed review rounds "
                            f"(last: {norm_verdict} {score_int}/100); requires HITL intervention."
                            if circuit_breaker_tripped
                            else f"Review round {current_iteration}/{MAX_PR_REVIEW_ITERATIONS} returned {norm_verdict} ({score_int}/100); auto-iterating."
                        ),
                        portfolio_dir=portfolio_dir,
                    )

        email_sent = False
        email_subject = (
            f"[ESCALATION] Max Review Iterations ({current_iteration}/{MAX_PR_REVIEW_ITERATIONS}) Reached: "
            f"{resolved_branch} [{resolved_ts}] ({norm_verdict} {score_int}/100)"
            if circuit_breaker_tripped
            else f"Review Iteration {current_iteration}/{MAX_PR_REVIEW_ITERATIONS} ({norm_verdict} {score_int}/100) — {resolved_branch}"
        )
        # Suppress Ready-for-PR email during rounds 1..2; ONLY dispatch [🟥] escalation alert at round >= 3
        if circuit_breaker_tripped and send_email:
            history_lines = "\n".join(
                f"  - Round {h.get('iteration')}: **{h.get('verdict')} ({h.get('score')}/100)** — {h.get('summary')}"
                for h in updated_history
            )
            esc_body = (
                f"# 🚨 Escalation: PR Review Circuit Breaker Tripped (`{resolved_branch}` · `{resolved_ts}`)\n\n"
                f"Branch **`{resolved_branch}`** failed `{PR_REVIEWER_SUBAGENT_NAME}` across "
                f"**`{current_iteration}/{MAX_PR_REVIEW_ITERATIONS}`** consecutive rounds and was **NOT pushed to `origin`** "
                f"(`pr_gate_ready=False`, `dev_queue.json` status=`hitl_blocked`).\n\n"
                f"- **Repository**: `{resolved_repo_slug}`\n"
                f"- **Conversation ID**: `{cid_clean}`\n"
                f"- **Latest Verdict**: **{badge} ({score_int}/100)** (requires `PASS >= {PR_REVIEW_PASS_MIN_SCORE}/100`)\n"
                f"- **Local Worktree**: `{_sanitize_home_paths(worktree_for_plan)}`\n\n"
                f"## Review Iteration History\n{history_lines}\n\n"
                f"## Next Steps (HITL)\n"
                f"- Inspect local worktree `{_sanitize_home_paths(worktree_for_plan)}` to resolve findings manually, OR\n"
                f"- Reject and unlock via: `pr_remediator.py evaluate-branch --branch \"{resolved_branch}\" --decision reject --comment \"Escalated after max review rounds\"`\n"
            )
            notify_res = dev_common.try_send_branch_review_notification(
                status="KO",
                subject=email_subject,
                body=esc_body,
                conversation_id=cid_clean or resolved_ts,
                validator=validator_banner_label,
                branch_name=resolved_branch,
                target_url="",
                errors_only_override=False,
            )
            email_sent = bool(notify_res.get("sent"))

        return {
            "status": "ok",
            "vcs_mode": "native_git",
            "repo_slug": resolved_repo_slug,
            "slot_id": slot_num,
            "branch_action": lock_status_next,
            "notification_source": "Ad-Hoc",
            "email_subject": email_subject,
            "created_by": created_by_label,
            "branch_name": resolved_branch,
            "branch_timestamp": resolved_ts,
            "head_sha": head_sha,
            "manual_pr_url": "",
            "pr_url": "",
            "pr_gate_ready": False,
            "ready_for_human_pr": False,
            "branch_pushed": False,
            "needs_reiteration": not circuit_breaker_tripped,
            "circuit_breaker_tripped": circuit_breaker_tripped,
            "review_iteration": current_iteration,
            "max_review_iterations": MAX_PR_REVIEW_ITERATIONS,
            "review_history": updated_history,
            "pr_number": int(pr_number or 0),
            "verdict": norm_verdict,
            "score": score_int,
            "review_markdown": review_markdown,
            "notification_email_sent": email_sent,
            "developer_remediation_plan": developer_remediation_plan,
            "pr_merge_watcher": {"status": "skipped", "spawned": False},
        }

    # ==========================================================================
    # HARD GATE PATH B: Review PASSED (`verdict == "PASS"` AND `score > 90`)
    # ==========================================================================
    effective_repo_dir = target_repo_dir
    branch_published = False
    if lock_info.get("active"):
        if not isinstance(target_repo_dir, Path) or not (target_repo_dir / ".git").exists():
            return {
                "status": "error",
                "step": "resolve_repo_dir",
                "ready_for_human_pr": False,
                "pr_gate_ready": False,
                "branch_pushed": False,
                "manual_pr_url": "",
                "pr_url": "",
                "branch_name": resolved_branch,
                "verdict": norm_verdict,
                "score": score_int,
                "error": f"Cannot amend and publish branch '{resolved_branch}': git repository directory not found ({target_repo_dir}).",
            }

        amend_res = _sync_branch_head_commit_message_detailed(
            target_repo_dir=target_repo_dir,
            branch_name=resolved_branch,
            base_branch="main",
            commit_title=proposal_title,
            commit_body=canonical_pr_body,
        )
        if not amend_res.get("amended"):
            return {
                "status": "error",
                "step": "amend_commit_message",
                "ready_for_human_pr": False,
                "pr_gate_ready": False,
                "branch_pushed": False,
                "manual_pr_url": "",
                "pr_url": "",
                "branch_name": resolved_branch,
                "verdict": norm_verdict,
                "score": score_int,
                "error": str(amend_res.get("error") or f"Failed to amend HEAD commit message on '{resolved_branch}'."),
            }

        effective_repo_dir = (
            amend_res["effective_repo_dir"]
            if isinstance(amend_res.get("effective_repo_dir"), Path)
            else target_repo_dir
        )
        amended_head_sha = str(amend_res.get("head_sha") or head_sha)
        if valid_vr and amended_head_sha:
            valid_vr = {**valid_vr, "head_sha": amended_head_sha, "dirty": False}

        # MAX 1 OPEN GITHUB PR PER REPOSITORY INVARIANT:
        # When any follower Slot #2..#N passes review while Slot #1 is still active on the repo, DO NOT push to origin
        # and DO NOT surface `manual_pr_url` until Slot #1 merges + rebase-reverify passes (`deferred_send_email=False`).
        if is_slot2_branch:
            head_sha = amended_head_sha
            review_entry["head_sha"] = head_sha
            updated_history[-1] = review_entry
            slot1_br = str(lock_info.get("slot1_branch_name") or "")
            slot2_md = (
                f"### ⏳ Slot #{slot_num} Validated Locally — Awaiting Slot #1 Base Merge (`{PR_REVIEWER_SUBAGENT_NAME}` · `{effective_reviewer_model}`)\n\n"
                f"- **Slot**: `{slot_num}` (behind active Slot #1 `{slot1_br}`; promotes in `landing_order` via `rebase-reverify`)\n"
                f"- **Status**: `{SLOT_STATUS_READY_AWAITING_BASE_MERGE}` (`max 1 open GitHub PR per repo` enforced — NOT pushed to `origin` yet)\n"
                f"- **Local Branch**: `{resolved_branch}` (Timestamp `{resolved_ts}`, SHA `{head_sha[:12]}`)\n"
                f"- **Verdict**: **🟢 PASS** (`{score_int}/100` · Round `{current_iteration}/{MAX_PR_REVIEW_ITERATIONS}`)\n"
                f"- **Evaluated At (UTC)**: `{now_iso}`\n\n"
                f"{canonical_pr_body}\n"
            )
            if rb_slot_key:
                persistent_s2_dir = (
                    _resolve_main_git_repo_root(effective_repo_dir)
                    if isinstance(effective_repo_dir, Path)
                    else lock_main_dir
                )
                updated_rb[rb_slot_key] = {
                    "repo_slug": resolved_repo_slug,
                    "slot_id": slot_num,
                    "branch_name": resolved_branch,
                    "branch_timestamp": resolved_ts,
                    "target_repo_dir": _sanitize_home_paths(str(persistent_s2_dir or "")),
                    "worktree_path": _sanitize_home_paths(str(effective_repo_dir or lock_info.get("worktree_path") or "")),
                    "manual_pr_url": "",
                    "branch_pushed": False,
                    "head_sha": head_sha,
                    "updated_at": now_iso,
                }
            update_dev_pr_lock(
                {
                    "status": SLOT_STATUS_READY_AWAITING_BASE_MERGE,
                    "branch_action": SLOT_STATUS_READY_AWAITING_BASE_MERGE,
                    "branch_pushed": False,
                    "pr_gate_ready": False,
                    "review_approved": True,
                    "approved_head_sha": head_sha,
                    "reviewed_head_sha": head_sha,
                    "staged_head_sha": head_sha,
                    "deferred_send_email": False,
                    "head_sha": head_sha,
                    "repo_branches": updated_rb,
                    "manual_pr_url": "",
                    "pr_url": "",
                    "proposal_title": proposal_title,
                    "proposal_body": canonical_pr_body,
                    "executive_summary": resolved_exec_summary,
                    "verification_results": valid_vr,
                    "review_iteration": current_iteration,
                    "review_history": updated_history,
                    "reviewer_verdict": norm_verdict,
                    "reviewer_score": score_int,
                    "reviewer_summary": summary.strip(),
                    "reviewer_subagent_id": reviewer_subagent_id.strip(),
                    "subagent_conversation_ids": recorded_subagents,
                    "review_markdown": slot2_md,
                },
                portfolio_dir=portfolio_dir,
                repo_slug=resolved_repo_slug,
                workdir=effective_repo_dir,
                branch_name=resolved_branch,
                slot_id=slot_num,
            )
            if cid_clean:
                with contextlib.suppress(Exception):
                    update_dev_queue_item_status(
                        conversation_id=cid_clean,
                        status="in_progress",
                        branch_names=[resolved_branch],
                        strip_pr_urls_for_branches=[resolved_branch],
                        strip_repo_slug=resolved_repo_slug,
                        subagent_conversation_ids=recorded_subagents,
                        notes=(
                            f"Slot #{slot_num} passed {PR_REVIEWER_SUBAGENT_NAME} ({norm_verdict} {score_int}/100) locally; "
                            f"{SLOT_STATUS_READY_AWAITING_BASE_MERGE} behind Slot #1 ({slot1_br})."
                        ),
                        portfolio_dir=portfolio_dir,
                    )
            return {
                "status": "ok",
                "vcs_mode": "native_git",
                "repo_slug": resolved_repo_slug,
                "slot_id": slot_num,
                "branch_action": SLOT_STATUS_READY_AWAITING_BASE_MERGE,
                "lock_status": SLOT_STATUS_READY_AWAITING_BASE_MERGE,
                "awaiting_base_merge": True,
                "notification_source": "Ad-Hoc",
                "email_subject": "",
                "created_by": created_by_label,
                "branch_name": resolved_branch,
                "branch_timestamp": resolved_ts,
                "head_sha": head_sha,
                "reviewed_head_sha": head_sha,
                "branch_pushed": False,
                "pr_gate_ready": False,
                "ready_for_human_pr": False,
                "needs_reiteration": False,
                "circuit_breaker_tripped": False,
                "review_iteration": current_iteration,
                "max_review_iterations": MAX_PR_REVIEW_ITERATIONS,
                "review_history": updated_history,
                "manual_pr_url": "",
                "pr_url": "",
                "notification_target_url": "",
                "pr_number": int(pr_number or 0),
                "verdict": norm_verdict,
                "score": score_int,
                "review_markdown": slot2_md,
                "notification_email_sent": False,
                "pr_merge_watcher": {"status": "deferred_until_slot1_merge", "spawned": False},
            }

        try:
            try:
                push_amend = vcs.push_branch(
                    branch_name=resolved_branch,
                    remote="origin",
                    base_branch="main",
                    set_upstream=True,
                    force_with_lease=True,
                    title=proposal_title,
                    body=canonical_pr_body,
                    repo_dir=effective_repo_dir,
                )
            except TypeError:
                push_amend = vcs.push_branch(
                    branch_name=resolved_branch,
                    remote="origin",
                    base_branch="main",
                    set_upstream=True,
                    repo_dir=effective_repo_dir,
                )
        except Exception as exc:
            push_amend = {"status": "error", "error": str(exc)}

        if not isinstance(push_amend, dict) or push_amend.get("status") != "ok":
            return {
                "status": "error",
                "step": "push_branch",
                "ready_for_human_pr": False,
                "pr_gate_ready": False,
                "branch_pushed": False,
                "manual_pr_url": "",
                "pr_url": "",
                "branch_name": resolved_branch,
                "verdict": norm_verdict,
                "score": score_int,
                "error": str((push_amend or {}).get("error") or f"Failed to push verified branch '{resolved_branch}' to origin."),
                "push_result": push_amend if isinstance(push_amend, dict) else {},
            }

        branch_published = True
        head_sha = str(push_amend.get("head_sha") or amended_head_sha or head_sha)
        if valid_vr and head_sha:
            valid_vr = {**valid_vr, "head_sha": head_sha, "dirty": False}
        review_entry["head_sha"] = head_sha
        updated_history[-1] = review_entry

        # Keep primary checkout on main if this was a non-worktree checkout
        if (effective_repo_dir / ".git").is_dir():
            subprocess.run(
                ["git", "-C", str(effective_repo_dir), "checkout", "main"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )

    built_url = ""
    if isinstance(effective_repo_dir, Path) and (effective_repo_dir / ".git").exists():
        try:
            built_url = vcs.build_manual_pr_url(
                head_branch=resolved_branch,
                base_branch="main",
                title=proposal_title,
                body=canonical_pr_body,
                repo_dir=effective_repo_dir,
            )
        except TypeError:
            built_url = vcs.build_manual_pr_url(head_branch=resolved_branch, base_branch="main")
    manual_pr_url = str(
        built_url
        or lock_info.get("manual_pr_url")
        or lock_info.get("pr_url")
        or ""
    )

    badge = "🟢 PASS"
    review_markdown = (
        f"### 🛡️ New Branch Validated & Published (`{PR_REVIEWER_SUBAGENT_NAME}` · `{effective_reviewer_model}`)\n\n"
        f"- **Branch Action**: **NEW BRANCH CREATED** (`origin/{resolved_branch}`)\n"
        f"- **New Branch Created**: `{resolved_branch}`\n"
        f"- **Created & Validated By**: `{created_by_label}`\n"
        f"- **Branch Timestamp**: `{resolved_ts}`\n"
        f"- **Proposed Branch**: `{resolved_branch}`\n"
        f"- **Verdict**: **{badge}** (`{score_int}/100` · Round `{current_iteration}/{MAX_PR_REVIEW_ITERATIONS}`)\n"
        f"- **Evaluated At (UTC)**: `{now_iso}`\n"
        + (f"- **Reviewer Subagent ID**: `{reviewer_subagent_id}`\n" if reviewer_subagent_id else "")
        + f"\n{canonical_pr_body}\n"
        f"---\n"
        f"**Manual Pull Request Gate (`dev_pr_lock` ACTIVE · `pr_gate_ready=True`)**:\n"
        f"1. Compare `{resolved_branch}` against `main` and manually create the Pull Request / Merge Request in your VCS:\n"
        f"   - Compare / Create Manual PR: {manual_pr_url}\n"
        f"   - Native Git Diff: `git fetch origin && git diff origin/main...origin/{resolved_branch}`\n"
        f"2. While `{resolved_branch}` (`{resolved_ts}`) is pending your evaluation, **no other agent or conversation can make code changes in `{resolved_repo_slug}`**.\n"
        f"3. Once you create/merge the Pull Request (or decide to reject this branch), release the lock via:\n"
        f"   - Approve (PR Created / Merged): `pr_remediator.py evaluate-branch --branch \"{resolved_branch}\" --decision approve --comment \"<your comment>\"`\n"
        f"   - Reject (Delete Branch & Unlock): `pr_remediator.py evaluate-branch --branch \"{resolved_branch}\" --decision reject --comment \"<your comment>\"`\n"
    )

    if lock_info.get("active"):
        persistent_repo_dir = (
            _resolve_main_git_repo_root(effective_repo_dir)
            if isinstance(effective_repo_dir, Path)
            else lock_main_dir
        )
        if resolved_repo_slug:
            updated_rb[resolved_repo_slug] = {
                "repo_slug": resolved_repo_slug,
                "branch_name": resolved_branch,
                "branch_timestamp": resolved_ts,
                "target_repo_dir": _sanitize_home_paths(str(persistent_repo_dir or "")),
                "manual_pr_url": manual_pr_url,
                "branch_pushed": True,
                "head_sha": head_sha,
                "updated_at": now_iso,
            }
        update_dev_pr_lock(
            {
                "status": "PENDING_HUMAN_PR_VALIDATION",
                "branch_action": "NEW_BRANCH_CREATED",
                "branch_pushed": True,
                "pr_gate_ready": True,
                "reviewed_head_sha": head_sha,
                "notification_source": "Ad-Hoc",
                "created_by": created_by_label,
                "branch_name": resolved_branch,
                "branch_timestamp": resolved_ts,
                "head_sha": head_sha,
                "repo_branches": updated_rb,
                "manual_pr_url": manual_pr_url,
                "pr_url": manual_pr_url,
                "proposal_title": proposal_title,
                "proposal_body": canonical_pr_body,
                "executive_summary": resolved_exec_summary,
                "verification_results": valid_vr,
                "review_iteration": current_iteration,
                "review_history": updated_history,
                "reviewer_verdict": norm_verdict,
                "reviewer_score": score_int,
                "reviewer_summary": summary.strip(),
                "reviewer_subagent_id": reviewer_subagent_id.strip(),
                "last_reviewer_model": effective_reviewer_model,
                "subagent_conversation_ids": recorded_subagents,
                "review_markdown": review_markdown,
            },
            portfolio_dir=portfolio_dir,
            repo_slug=resolved_repo_slug,
            workdir=effective_repo_dir,
            branch_name=resolved_branch,
            slot_id=1,
        )
        if cid_clean:
            with contextlib.suppress(Exception):
                upd_q = update_dev_queue_item_status(
                    conversation_id=cid_clean,
                    status="pr_open",
                    branch_names=[resolved_branch],
                    pr_urls=[manual_pr_url],
                    subagent_conversation_ids=recorded_subagents,
                    portfolio_dir=portfolio_dir,
                )
                if not upd_q.get("updated"):
                    _sync_active_lock_to_dev_queue(
                        conversation_id=cid_clean,
                        status="pr_open",
                        title=proposal_title,
                        action_id=str(lock_info.get("action_id") or ""),
                        repo_slug=resolved_repo_slug,
                        branch_name=resolved_branch,
                        pr_urls=[manual_pr_url],
                        subagent_conversation_ids=recorded_subagents,
                        rca_summary=str(lock_info.get("summary") or ""),
                        proposed_fix=resolved_exec_summary,
                        portfolio_dir=portfolio_dir,
                    )

    if cid_clean:
        p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
        state_path = p_dir / "actions_state.json"
        lock_file = state_path.with_suffix(".json.lock")
        with dev_common.file_lock(lock_file):
            state_obj = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
            if isinstance(state_obj, dict):
                audit_state = state_obj.get("actions_log_audit_state")
                if not isinstance(audit_state, dict):
                    audit_state = {}
                legacy_audited = audit_state.get("audited_runs") if isinstance(audit_state.get("audited_runs"), dict) else {}
                audited_runs = dev_common.try_sync_actions_ledger_verdicts(
                    state_dir=p_dir,
                    legacy_audited=legacy_audited,
                )
                existing_run = audited_runs.get(cid_clean) if isinstance(audited_runs.get(cid_clean), dict) else {}
                updated_verdict = {
                    **existing_run,
                    "conversation_id": cid_clean,
                    "tier": str(existing_run.get("tier") or "TIER_2"),
                    "resolution": str(existing_run.get("resolution") or "STAGE_PROPOSED"),
                    "branch_action": "NEW_BRANCH_CREATED",
                    "branch_name": resolved_branch,
                    "branch_timestamp": resolved_ts,
                    "manual_pr_url": manual_pr_url,
                    "created_by": created_by_label,
                    "reviewer_verdict": norm_verdict,
                    "reviewer_score": score_int,
                    "review_iteration": current_iteration,
                    "summary": (
                        f"New branch validated & published by {PR_DEVELOPER_SUBAGENT_NAME} / {PR_REVIEWER_SUBAGENT_NAME}: "
                        f"{resolved_branch} [{resolved_ts}] ({norm_verdict} {score_int}/100, round {current_iteration}/{MAX_PR_REVIEW_ITERATIONS}) — ready for manual PR."
                    ),
                    "updated_at": now_iso,
                }
                dev_common.try_sync_actions_ledger_verdicts({cid_clean: updated_verdict}, state_dir=p_dir)
                audit_state.pop("audited_runs", None)
                state_obj["actions_log_audit_state"] = audit_state
                tmp_path = state_path.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(state_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                with contextlib.suppress(OSError):
                    os.chmod(tmp_path, 0o600)
                os.replace(tmp_path, state_path)

    clean_slug_for_name = str(resolved_repo_slug or "").strip().strip("/")
    if clean_slug_for_name and clean_slug_for_name.lower() != "default":
        repo_name = clean_slug_for_name.split("/")[-1]
    elif isinstance(effective_repo_dir, Path) and effective_repo_dir.name:
        repo_name = effective_repo_dir.name
    else:
        repo_name = "repo"
    summary_or_branch = _format_pr_notification_subject_summary(
        proposal_title,
        resolved_exec_summary,
        resolved_branch,
    )

    remote_url = ""
    if isinstance(effective_repo_dir, Path) and (effective_repo_dir / ".git").exists():
        resolver = getattr(vcs, "resolve_remote_url", None)
        if callable(resolver):
            try:
                cand_remote = resolver(repo_dir=effective_repo_dir)
            except TypeError:
                cand_remote = resolver()
            if isinstance(cand_remote, str):
                remote_url = cand_remote.strip()
    notification_target_url = _git_client.build_vcs_pr_notification_url(
        branch_name=resolved_branch,
        base_branch="main",
        remote_url=remote_url,
        pr_url=manual_pr_url,
        repo_slug=resolved_repo_slug,
    )

    # PR Ready for Validation (status='OK') emails are permanently disabled; readiness is surfaced
    # exclusively via dev_queue.json (`pr_open`), `agy-queue` TUI, `dev_queue_board.md`, and `manual_pr_url`.
    # (`--send-email` is retained strictly for Round-3 `[🟥]` KO circuit-breaker escalations above.)
    _ = send_email
    email_sent = False
    email_subject = (
        f"PR Ready for Validation [{repo_name}]: {summary_or_branch} ({norm_verdict} {score_int}/100)"
    )

    ask_question_prompt = {
        "question": (
            f"New Branch Created ({resolved_branch}, Timestamp: {resolved_ts}) by {PR_REVIEWER_SUBAGENT_NAME}: "
            f"{norm_verdict} ({score_int}/100). Have you validated the new branch to create the Pull Request manually, or do you want to reject it?"
        ),
        "options": [
            f"(Recommended) Approve New Branch {resolved_branch} [{resolved_ts}] (Manual PR created/merged — release dev_pr_lock)",
            f"Keep New Branch {resolved_branch} [{resolved_ts}] pending validation (keep dev_pr_lock active)",
            f"Reject New Branch {resolved_branch} [{resolved_ts}] (delete branch via native git & release dev_pr_lock)",
        ],
    }

    watcher_res = (
        _facade_attr("spawn_pr_merge_watcher_daemon", spawn_pr_merge_watcher_daemon)(portfolio_dir=portfolio_dir)
        if lock_info.get("active")
        else {"status": "skipped", "spawned": False}
    )

    frontend_changed = bool(lock_info.get("frontend_changed")) or is_frontend_change_detected(files_changed)
    dev_preview_url = ""
    pro_url = str(
        lock_info.get("pro_url")
        or _facade_attr("build_pro_dashboard_ui_url", build_pro_dashboard_ui_url)("health", portfolio_dir=portfolio_dir, ensure_server=False)
    )

    return {
        "status": "ok",
        "vcs_mode": "native_git",
        "repo_slug": resolved_repo_slug,
        "branch_action": "NEW_BRANCH_CREATED",
        "notification_source": "Ad-Hoc",
        "email_subject": email_subject,
        "created_by": created_by_label,
        "branch_name": resolved_branch,
        "branch_timestamp": resolved_ts,
        "head_sha": head_sha,
        "reviewed_head_sha": head_sha if lock_info.get("active") else "",
        "branch_pushed": branch_published if lock_info.get("active") else bool(lock_info.get("branch_pushed")),
        "pr_gate_ready": True,
        "ready_for_human_pr": True,
        "needs_reiteration": False,
        "circuit_breaker_tripped": False,
        "review_iteration": current_iteration,
        "max_review_iterations": MAX_PR_REVIEW_ITERATIONS,
        "review_history": updated_history,
        "manual_pr_url": manual_pr_url,
        "pr_url": manual_pr_url,
        "notification_target_url": notification_target_url,
        "frontend_changed": frontend_changed,
        "dev_preview_url": dev_preview_url,
        "pro_url": pro_url,
        "pr_number": int(pr_number or 0),
        "verdict": norm_verdict,
        "score": score_int,
        "review_markdown": review_markdown,
        "notification_email_sent": email_sent,
        "pr_merge_watcher": watcher_res,
        "ask_question_payload": ask_question_prompt,
    }


def evaluate_branch_and_release_lock(
    decision: str,
    comment: str,
    branch_name: str = "",
    pr_number: int | None = None,
    delete_remote_on_reject: bool = True,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Execute the manual validation gate: record user decision (`approve` after manual PR creation/merge, or `reject` to delete the branch via native `git`) and release the scoped repository lock.

    Enforces `pr_gate_ready == True` and `reviewed_head_sha == head_sha` on `approve` unless `force=True` is passed explicitly.
    """
    vcs = git_client or GitVcsIntegrationClient()
    norm_decision = decision.strip().lower()
    if norm_decision not in ("approve", "merge", "reject", "close"):
        return {
            "status": "error",
            "error": f"Invalid decision '{decision}'. Expected 'approve' or 'reject'.",
        }

    lock_info = get_dev_pr_lock(
        portfolio_dir=portfolio_dir,
        repo_slug=repo_slug,
        workdir=workdir,
        branch_name=branch_name,
    )
    resolved_slug = (
        dev_common.normalize_repo_slug(repo_slug)
        or str(lock_info.get("repo_slug") or lock_info.get("repo") or "").strip()
    )
    lock_repo_dir = _resolve_lock_target_repo_dir(lock_info)
    if lock_repo_dir is None and workdir:
        lock_repo_dir = _resolve_repo_slug_and_dir(workdir=workdir, prefer_cwd=False)[1]
    if git_client is None and lock_repo_dir is not None and (lock_repo_dir / ".git").exists():
        vcs = GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(lock_repo_dir)})
    resolved_branch = (
        branch_name.strip()
        or str(lock_info.get("branch_name") or "").strip()
        or (f"fix/pr-{pr_number}" if pr_number else "")
    )
    resolved_ts = str(lock_info.get("branch_timestamp") or "").strip()

    review_comment = comment.strip() or (
        f"Validated branch `{resolved_branch}` (`{resolved_ts}`) and created/merged Pull Request manually."
        if norm_decision in ("approve", "merge")
        else f"Rejected branch `{resolved_branch}` (`{resolved_ts}`)."
    )

    if norm_decision in ("approve", "merge"):
        if lock_info.get("active") and not force:
            if not bool(lock_info.get("pr_gate_ready")):
                return {
                    "status": "error",
                    "vcs_mode": "native_git",
                    "repo_slug": resolved_slug,
                    "decision": "blocked_pr_gate_not_ready",
                    "branch_name": resolved_branch,
                    "lock_released": False,
                    "error": (
                        f"Refusing to approve branch '{resolved_branch}': PR readiness gate has not passed "
                        f"(pr_gate_ready=False, status={lock_info.get('status')}). Run agy-code-pr-reviewer "
                        f"(PASS >= {PR_REVIEW_PASS_MIN_SCORE}/100) via `record-review` first, or pass `--force` explicitly."
                    ),
                }
            reviewed_sha = str(lock_info.get("reviewed_head_sha") or "").strip()
            live_head_sha = str(lock_info.get("head_sha") or "").strip()
            probe_dirs: list[Path] = []
            wt_raw = str(lock_info.get("worktree_path") or "").strip()
            if wt_raw:
                probe_dirs.append(Path(wt_raw.replace("$HOME", str(Path.home()))).expanduser())
            if lock_repo_dir is not None:
                probe_dirs.append(lock_repo_dir)
            for p_dir_cand in probe_dirs:
                if p_dir_cand.exists() and (p_dir_cand / ".git").exists() and resolved_branch:
                    sha_proc = subprocess.run(
                        ["git", "-C", str(p_dir_cand), "rev-parse", "--verify", resolved_branch],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    if sha_proc.returncode == 0 and (sha_proc.stdout or "").strip():
                        live_head_sha = (sha_proc.stdout or "").strip()
                        break
            if not reviewed_sha or (live_head_sha and reviewed_sha != live_head_sha):
                return {
                    "status": "error",
                    "vcs_mode": "native_git",
                    "repo_slug": resolved_slug,
                    "decision": "blocked_unreviewed_commits",
                    "branch_name": resolved_branch,
                    "reviewed_head_sha": reviewed_sha,
                    "head_sha": live_head_sha,
                    "lock_released": False,
                    "error": (
                        f"Refusing to approve branch '{resolved_branch}': current HEAD SHA '{live_head_sha[:12]}' "
                        f"does not match reviewed_head_sha '{reviewed_sha[:12]}'. Re-run agy-code-pr-reviewer "
                        f"or pass `--force` explicitly."
                    ),
                }
        eval_slot_id = int(lock_info.get("slot_id") or 1) if lock_info.get("active") else None
        sibling_protected_branches = [
            str(s_entry.get("branch_name") or "").strip()
            for s_k, s_entry in (lock_info.get("slots") or {}).items()
            if isinstance(s_entry, dict)
            and bool(s_entry.get("active", True))
            and str(s_k) != str(eval_slot_id or "")
            and str(s_entry.get("branch_name") or "").strip()
        ]
        repo_sync = (
            vcs.sync_and_clean_local_repo(
                base_branch="main",
                protected_branches=sibling_protected_branches or None,
            )
            if hasattr(vcs, "sync_and_clean_local_repo")
            else {}
        )
        rel_res = release_dev_pr_lock(
            reason="HUMAN_VALIDATED_MANUAL_PR",
            resolution="APPLIED_AND_SYNCED",
            summary=f"Branch `{resolved_branch}` (`{resolved_ts}`) approved for manual PR ({review_comment})",
            portfolio_dir=portfolio_dir,
            repo_slug=resolved_slug,
            workdir=workdir or lock_repo_dir,
            branch_name=resolved_branch,
            slot_id=eval_slot_id,
        )
        return {
            "status": "ok",
            "vcs_mode": "native_git",
            "repo_slug": resolved_slug,
            "decision": "approved_and_unlocked",
            "branch_name": resolved_branch,
            "branch_timestamp": resolved_ts,
            "pr_number": int(pr_number or 0),
            "comment": review_comment,
            "lock_released": True,
            "release_result": rel_res,
            "repo_sync": repo_sync,
        }

    eval_slot_id = int(lock_info.get("slot_id") or 1) if lock_info.get("active") else None
    sibling_protected_branches = [
        str(s_entry.get("branch_name") or "").strip()
        for s_k, s_entry in (lock_info.get("slots") or {}).items()
        if isinstance(s_entry, dict)
        and bool(s_entry.get("active", True))
        and str(s_k) != str(eval_slot_id or "")
        and str(s_entry.get("branch_name") or "").strip()
    ]
    wt_path = str(lock_info.get("worktree_path") or "").strip()
    if wt_path:
        cleanup_worktree(wt_path, repo_dir=lock_repo_dir)

    # Ensure repository is checked out on main before deleting a local feature branch
    repo_dir = vcs.resolve_repo_dir()
    if (repo_dir / ".git").exists():
        cur_proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if (cur_proc.stdout or "").strip() == resolved_branch:
            subprocess.run(
                ["git", "-C", str(repo_dir), "checkout", "main"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

    del_res: dict[str, Any] = {"status": "skipped"}
    if resolved_branch:
        del_res = vcs.delete_branch_native(
            branch_name=resolved_branch,
            remote="origin",
            delete_remote=delete_remote_on_reject,
            delete_local=True,
        )

    repo_sync = (
        vcs.sync_and_clean_local_repo(
            base_branch="main",
            protected_branches=sibling_protected_branches or None,
            force_clean_branches=[resolved_branch] if resolved_branch else [],
            fetch=False,
        )
        if hasattr(vcs, "sync_and_clean_local_repo")
        else {}
    )
    rel_res = release_dev_pr_lock(
        reason="HUMAN_REJECTED_BRANCH",
        resolution="VERIFIED_OK",
        summary=f"Branch `{resolved_branch}` (`{resolved_ts}`) rejected and removed ({review_comment})",
        portfolio_dir=portfolio_dir,
        repo_slug=resolved_slug,
        workdir=workdir or lock_repo_dir,
        branch_name=resolved_branch,
        slot_id=eval_slot_id,
    )
    return {
        "status": "ok",
        "vcs_mode": "native_git",
        "repo_slug": resolved_slug,
        "decision": "rejected_and_deleted",
        "branch_name": resolved_branch,
        "branch_timestamp": resolved_ts,
        "pr_number": int(pr_number or 0),
        "comment": review_comment,
        "branch_cleanup": del_res,
        "lock_released": True,
        "release_result": rel_res,
        "repo_sync": repo_sync,
    }






_PRE_REVIEW_DOC_EXTENSIONS = (".md", ".txt", ".rst", ".adoc", ".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")
_PRE_REVIEW_DOC_BASENAMES = frozenset({"license", "license.md", "license.txt", "copying"})
_PROJECT_AUDIT_MOD_CACHE: dict[str, Any] = {}


def _load_project_audit_module() -> Any | None:
    """Load `project_audit.py` from repo or installed plugin root without mutating sys.path globally."""
    cached = _PROJECT_AUDIT_MOD_CACHE.get("mod")
    if cached is not None:
        return cached
    candidates = (
        Path(__file__).resolve().parents[2] / "agy-project-standards" / "scripts" / "project_audit.py",
        Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin" / "skills" / "agy-project-standards" / "scripts" / "project_audit.py",
    )
    for cand in candidates:
        if not cand.is_file():
            continue
        spec = importlib.util.spec_from_file_location("agy_project_audit_mod", str(cand))
        if spec is None or spec.loader is None:
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(spec.name, None)
            continue
        _PROJECT_AUDIT_MOD_CACHE["mod"] = mod
        return mod
    return None


def _norm_diff_rel_path(rel: Any) -> str:
    """Normalize diff relative path (string or `{filename|path}` dict) to POSIX string without leading `./`."""
    raw = rel.get("filename") or rel.get("path") or "" if isinstance(rel, dict) else rel
    norm = str(raw or "").strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def _is_doc_only_diff_path(rel: str) -> bool:
    """Return True when `rel` is documentation or static image media (no code/config execution)."""
    norm = _norm_diff_rel_path(rel).lower()
    if not norm:
        return False
    base = norm.rsplit("/", 1)[-1]
    return base in _PRE_REVIEW_DOC_BASENAMES or norm.endswith(_PRE_REVIEW_DOC_EXTENSIONS)


def _is_test_diff_path(rel: str) -> bool:
    """Return True when `rel` is a unit/integration/BATS test file."""
    norm = _norm_diff_rel_path(rel).lower()
    if not norm:
        return False
    base = norm.rsplit("/", 1)[-1]
    return (
        norm.startswith("tests/")
        or "/tests/" in norm
        or norm.endswith(".bats")
        or (base.startswith("test_") and base.endswith(".py"))
        or base.endswith("_test.py")
    )


def _extract_audit_findings_dicts(findings: Sequence[Any]) -> list[dict[str, Any]]:
    """Normalize `AuditFinding` objects or dicts into compact JSON-serializable dicts."""
    out: list[dict[str, Any]] = []
    for item in findings:
        if isinstance(item, dict):
            out.append(
                {
                    "severity": str(item.get("severity") or "error"),
                    "rule_id": str(item.get("rule_id") or "audit"),
                    "file": _norm_diff_rel_path(str(item.get("file") or "")),
                    "line": int(item.get("line") or 0),
                    "message": _sanitize_home_paths(str(item.get("message") or "")),
                }
            )
        else:
            out.append(
                {
                    "severity": str(getattr(item, "severity", "error") or "error"),
                    "rule_id": str(getattr(item, "rule_id", "audit") or "audit"),
                    "file": _norm_diff_rel_path(str(getattr(item, "file", "") or "")),
                    "line": int(getattr(item, "line", 0) or 0),
                    "message": _sanitize_home_paths(str(getattr(item, "message", "") or "")),
                }
            )
    return out


def evaluate_pre_review_fail_fast_gate(
    repo_dir: Path | str | None,
    *,
    changed_files: Sequence[Any] = (),
    lock_info: dict[str, Any] | None = None,
    verification_results: dict[str, Any] | None = None,
    verification_gate: dict[str, Any] | None = None,
    head_branch: str = "",
    head_sha: str = "",
    pre_commit_state: dict[str, Any] | None = None,
    strict_audit: bool = True,
    project_audit_fn: Callable[[Path, bool], Any] | None = None,
) -> dict[str, Any]:
    """Run deterministic 0-LLM pre-review fail-fast checks before spawning `agy-code-pr-reviewer`.

    Checks:
      1. `project_audit --strict` (Zero-PII, Santa Lockdown, 5-language AST/shell/web/JSON checks).
      2. Diff test coverage presence (non-doc code changes require test coverage in diff or verified `test-local`).
      3. SHA parity (`head_sha` / tree state matches `verification_results` when present).
    """
    resolved_repo = Path(repo_dir).expanduser().resolve() if repo_dir is not None and str(repo_dir).strip() else None
    norm_changed = [_norm_diff_rel_path(p) for p in changed_files if _norm_diff_rel_path(p)]
    changed_set = set(norm_changed)
    doc_only = bool(norm_changed) and all(_is_doc_only_diff_path(p) for p in norm_changed)
    test_files = [p for p in norm_changed if _is_test_diff_path(p)]
    code_files = [p for p in norm_changed if not _is_doc_only_diff_path(p) and not _is_test_diff_path(p)]

    gate_errors: list[dict[str, Any]] = []
    gate_warnings: list[dict[str, Any]] = []

    # 1. `project_audit --strict` & Zero-PII / Santa compliance
    audit_summary: dict[str, Any] = {
        "status": "skipped",
        "passed": True,
        "repo_passed": True,
        "strict": bool(strict_audit),
        "errors_count": 0,
        "diff_errors_count": 0,
        "untouched_errors_count": 0,
        "warnings_count": 0,
        "errors": [],
        "warnings": [],
    }
    if resolved_repo is not None and resolved_repo.is_dir():
        audit_callable = project_audit_fn
        if audit_callable is None:
            pa_mod = _load_project_audit_module()
            if pa_mod is not None and callable(getattr(pa_mod, "audit_repository", None)):
                audit_callable = lambda r_dir, s_flag: pa_mod.audit_repository(r_dir, strict=s_flag)  # noqa: E731
        if audit_callable is not None:
            try:
                rep = audit_callable(resolved_repo, bool(strict_audit))
            except Exception as exc:
                audit_summary["status"] = "error"
                audit_summary["passed"] = False
                audit_summary["repo_passed"] = False
                gate_errors.append(
                    {
                        "code": "project_audit_exception",
                        "message": _sanitize_home_paths(f"project_audit failed: {exc.__class__.__name__}: {exc}"),
                    }
                )
            else:
                rep_errors = _extract_audit_findings_dicts(
                    rep.get("errors", []) if isinstance(rep, dict) else getattr(rep, "errors", [])
                )
                rep_warnings = _extract_audit_findings_dicts(
                    rep.get("warnings", []) if isinstance(rep, dict) else getattr(rep, "warnings", [])
                )
                rep_passed = bool(rep.get("passed")) if isinstance(rep, dict) else bool(getattr(rep, "passed", False))
                diff_errors: list[dict[str, Any]] = []
                diff_strict_warnings_count = 0
                untouched_errors_count = 0
                for err in rep_errors:
                    in_diff = bool(err["file"] and (err["file"] in changed_set or not changed_set))
                    if in_diff or not err["file"]:
                        diff_errors.append(err)
                        gate_errors.append(
                            {
                                "code": f"project_audit:{err['rule_id']}",
                                "file": err["file"],
                                "line": err["line"],
                                "message": f"{err['rule_id']} @ {err['file']}:{err['line']}: {err['message']}",
                            }
                        )
                    else:
                        untouched_errors_count += 1
                        gate_warnings.append(
                            {
                                "code": f"project_audit_untouched:{err['rule_id']}",
                                "file": err["file"],
                                "line": err["line"],
                                "message": f"{err['rule_id']} @ {err['file']}:{err['line']}: {err['message']}",
                            }
                        )
                for warn in rep_warnings:
                    in_diff = bool(warn["file"] and (warn["file"] in changed_set or not changed_set))
                    if strict_audit and in_diff:
                        diff_strict_warnings_count += 1
                        gate_errors.append(
                            {
                                "code": f"project_audit_strict:{warn['rule_id']}",
                                "file": warn["file"],
                                "line": warn["line"],
                                "message": f"{warn['rule_id']} @ {warn['file']}:{warn['line']}: {warn['message']}",
                            }
                        )
                    else:
                        gate_warnings.append(
                            {
                                "code": f"project_audit_warning:{warn['rule_id']}",
                                "file": warn["file"],
                                "line": warn["line"],
                                "message": f"{warn['rule_id']} @ {warn['file']}:{warn['line']}: {warn['message']}",
                            }
                        )
                diff_audit_passed = (
                    (len(diff_errors) == 0 and diff_strict_warnings_count == 0)
                    if (rep_errors or rep_warnings)
                    else rep_passed
                )
                audit_summary.update(
                    {
                        "status": "ok" if diff_audit_passed else "failed",
                        "passed": diff_audit_passed,
                        "repo_passed": rep_passed,
                        "errors_count": len(rep_errors),
                        "diff_errors_count": len(diff_errors),
                        "untouched_errors_count": untouched_errors_count,
                        "warnings_count": len(rep_warnings),
                        "errors": diff_errors[:25],
                        "warnings": rep_warnings[:25],
                    }
                )

    # 2. SHA parity when `verification_results` is present on lock
    raw_vr = (
        lock_info.get("verification_results")
        if isinstance(lock_info, dict) and isinstance(lock_info.get("verification_results"), dict)
        else {}
    )
    eff_vr = dict(verification_results or {})
    if not eff_vr and raw_vr and resolved_repo is not None and (resolved_repo / ".git").exists():
        eff_vr = _resolve_valid_verification_results(
            lock_info,
            resolved_repo,
            branch_name=head_branch,
            pre_commit_state=pre_commit_state,
        )

    sha_parity: dict[str, Any] = {
        "status": "not_present",
        "passed": True,
        "head_sha": str(head_sha or (verification_gate or {}).get("head_sha") or ""),
        "verified_head_sha": str(raw_vr.get("head_sha") or ""),
    }
    if raw_vr:
        vr_overall = bool(raw_vr.get("overall_passed", True))
        if verification_gate is not None:
            vg_matches = bool(verification_gate.get("matches_head"))
            vg_passed = bool(verification_gate.get("passed"))
            parity_ok = vg_matches and vg_passed
            sha_parity["head_sha"] = str(verification_gate.get("head_sha") or sha_parity["head_sha"])
        else:
            parity_ok = bool(eff_vr) and vr_overall
            if parity_ok and sha_parity["head_sha"] and eff_vr.get("head_sha"):
                # Allow pre-amend tree/fingerprint match while detecting mismatched commit SHA when clean.
                if not eff_vr.get("tree_sha") and str(eff_vr.get("head_sha")) != sha_parity["head_sha"]:
                    parity_ok = False
        if not parity_ok:
            sha_parity["status"] = "failed"
            sha_parity["passed"] = False
            if not vr_overall or (verification_gate is not None and not verification_gate.get("passed")):
                gate_errors.append(
                    {
                        "code": "verification_failed",
                        "message": "Recorded `verification_results` did not pass; fix failures and re-run `pr_remediator.py test-local`.",
                    }
                )
            else:
                gate_errors.append(
                    {
                        "code": "sha_parity_mismatch",
                        "message": (
                            "`verification_results` head_sha/tree_sha does not match current branch HEAD; "
                            "re-run `pr_remediator.py test-local` before `prepare-review`."
                        ),
                    }
                )
        else:
            sha_parity["status"] = "ok"
            sha_parity["passed"] = True

    # 3. Diff test coverage presence (non-doc code changes require test coverage in diff or verified test gate)
    vr_tests_passed = bool(
        sha_parity["passed"]
        and (
            bool((eff_vr.get("unittest") or {}).get("passed"))
            or bool(eff_vr.get("overall_passed"))
            or bool((verification_gate or {}).get("tests_passed") and (verification_gate or {}).get("matches_head"))
        )
    )
    has_tests_dir = bool(resolved_repo is not None and (resolved_repo / "tests").is_dir())
    if not norm_changed or doc_only or not code_files:
        cov_mode = "doc_only" if doc_only else "no_code_changes"
        cov_passed = True
    elif test_files:
        cov_mode = "tests_in_diff"
        cov_passed = True
    elif vr_tests_passed:
        cov_mode = "verified_test_gate"
        cov_passed = True
    elif has_tests_dir:
        cov_mode = "missing"
        cov_passed = False
        gate_errors.append(
            {
                "code": "missing_diff_test_coverage",
                "message": (
                    f"Non-doc code change(s) ({', '.join(code_files[:5])}) lack test coverage in diff "
                    "and lack a current verified `test-local` gate."
                ),
            }
        )
    else:
        cov_mode = "missing_no_tests_dir_warning"
        cov_passed = True
        gate_warnings.append(
            {
                "code": "missing_diff_test_coverage_warning",
                "message": (
                    f"Non-doc code change(s) ({', '.join(code_files[:5])}) have no `tests/` directory "
                    "and no verified `test-local` gate."
                ),
            }
        )

    diff_test_coverage = {
        "passed": cov_passed,
        "mode": cov_mode,
        "doc_only": doc_only,
        "code_files_count": len(code_files),
        "test_files_count": len(test_files),
        "verified_test_gate": vr_tests_passed,
    }
    max_warnings = 50
    capped_warnings = gate_warnings[:max_warnings]
    passed = not gate_errors
    return {
        "passed": passed,
        "status": "ok" if passed else "failed",
        "doc_only": doc_only,
        "project_audit": audit_summary,
        "sha_parity": sha_parity,
        "diff_test_coverage": diff_test_coverage,
        "errors": gate_errors,
        "warnings": capped_warnings,
        "warnings_count": len(gate_warnings),
        "error_messages": [e["message"] for e in gate_errors],
        "warning_messages": [w["message"] for w in capped_warnings],
    }


def get_branch_diff_with_verification_gate(
    *,
    branch: str = "",
    base_branch: str = "main",
    workdir: Path | str | None = None,
    repo_slug: str = "",
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> dict[str, Any]:
    """Compute native git `branch-diff` and embed SHA-bound `verification_gate` + `pre_review_gate`."""
    lock_info = get_dev_pr_lock(
        portfolio_dir=portfolio_dir,
        repo_slug=repo_slug,
        workdir=workdir,
        branch_name=branch.strip(),
    )
    resolved_branch = branch.strip() or str(lock_info.get("branch_name") or "").strip()
    wd_str = str(workdir or "").strip()
    wt_str = str(lock_info.get("worktree_path") or "").strip()
    cand_str = wd_str or wt_str
    cand_path = Path(cand_str.replace("$HOME", str(Path.home()))).expanduser() if cand_str else None
    repo_dir = (
        cand_path.resolve()
        if cand_path and cand_path.exists()
        else _resolve_lock_target_repo_dir(lock_info)
    )
    vcs = git_client or GitVcsIntegrationClient()
    res = vcs.get_branch_diff(head_branch=resolved_branch, base_branch=base_branch, repo_dir=repo_dir)
    vg = build_branch_diff_verification_gate(lock_info, repo_dir, resolved_branch)
    res["verification_gate"] = vg
    res["pre_review_gate"] = evaluate_pre_review_fail_fast_gate(
        repo_dir,
        changed_files=res.get("files", []) or [],
        lock_info=lock_info,
        verification_gate=vg,
        head_branch=resolved_branch,
        head_sha=str(res.get("head_sha") or vg.get("head_sha") or ""),
    )
    return res


# ==============================================================================
# Parallel Subagents Phase 1: Overlap Analysis & 0-LLM Rebase-Reverify Gate
# ==============================================================================


def analyze_queue_or_tasks_overlap(
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    candidate_ids: list[str] | None = None,
    files_a: list[str] | None = None,
    files_b: list[str] | None = None,
    anchors_a: list[str] | None = None,
    anchors_b: list[str] | None = None,
    tasks_json: str = "",
    include_active_lock: bool = True,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> dict[str, Any]:
    """Analyze 4-tier blast-radius overlap (`NONE`, `TRANSITIVE`, `INTERFACE_SHARED`, `DEEP_SHARED`) and `landing_order`."""
    resolved_slug, resolved_dir = _resolve_repo_slug_and_dir(
        repo_slug=repo_slug,
        workdir=workdir,
        git_client=git_client,
        prefer_cwd=not bool(repo_slug or workdir),
        portfolio_dir=portfolio_dir,
    )
    if resolved_dir is None and resolved_slug and resolved_slug != "default":
        resolved_dir = _resolve_repo_dir_for_slug(resolved_slug, portfolio_dir=portfolio_dir, git_client=git_client)
    main_repo_dir = _resolve_main_git_repo_root(resolved_dir) if resolved_dir is not None else None

    if files_a or files_b:
        spec_a = TaskAnchorSpec(
            conversation_id="task-a",
            title="Candidate A",
            status="in_progress",
            priority=10,
            target_files=tuple(str(f).strip() for f in (files_a or []) if str(f).strip()),
            section_anchors=tuple(str(a).strip() for a in (anchors_a or []) if str(a).strip()),
        )
        spec_b = TaskAnchorSpec(
            conversation_id="task-b",
            title="Candidate B",
            status="pending",
            priority=20,
            target_files=tuple(str(f).strip() for f in (files_b or []) if str(f).strip()),
            section_anchors=tuple(str(a).strip() for a in (anchors_b or []) if str(a).strip()),
        )
        pair_res = analyze_pair_overlap(spec_a, spec_b, repo_dir=main_repo_dir, repo_slug=resolved_slug)
        batch_res = analyze_candidates_overlap(
            [spec_a, spec_b],
            repo_dir=main_repo_dir,
            repo_slug=resolved_slug,
            primary_slot_id="task-a",
        )
        return {
            "status": "ok",
            "mode": "direct_pair",
            "repo_slug": resolved_slug,
            "overlap_tier": pair_res.overlap_tier,
            "parallel_safe": pair_res.parallel_safe,
            "monolith_disjoint": pair_res.monolith_disjoint,
            "requires_rebase_reverify": pair_res.requires_rebase_reverify,
            "landing_order": list(pair_res.landing_order),
            "pair": pair_res.to_dict(),
            "batch": batch_res.to_dict(),
        }

    if tasks_json.strip():
        try:
            parsed_tasks = json.loads(tasks_json)
        except json.JSONDecodeError as exc:
            return {"status": "error", "error": f"Invalid --tasks-json payload: {exc}"}
        if not isinstance(parsed_tasks, list) or len(parsed_tasks) < 1:
            return {"status": "error", "error": "--tasks-json must be a non-empty JSON array of task objects."}
        batch_res = analyze_candidates_overlap(parsed_tasks, repo_dir=main_repo_dir, repo_slug=resolved_slug)
        out = batch_res.to_dict()
        out["mode"] = "tasks_json"
        out["repo_slug"] = resolved_slug
        if len(batch_res.pairwise) == 1:
            p0 = batch_res.pairwise[0]
            out["overlap_tier"] = p0.overlap_tier
            out["parallel_safe"] = p0.parallel_safe
            out["monolith_disjoint"] = p0.monolith_disjoint
        return out

    tasks_list: list[TaskAnchorSpec] = []
    primary_cid = ""
    follower_cids: list[str] = []
    if include_active_lock:
        act_lock = get_dev_pr_lock(portfolio_dir=portfolio_dir, repo_slug=resolved_slug, workdir=workdir)
        if act_lock.get("active"):
            act_slots = act_lock.get("slots") if isinstance(act_lock.get("slots"), dict) else {}
            s1_lk = act_slots.get("1") if isinstance(act_slots.get("1"), dict) else act_lock
            primary_cid = str(s1_lk.get("conversation_id") or act_lock.get("conversation_id") or "")
            tasks_list.append(TaskAnchorSpec.from_mapping(s1_lk))
            for f_key in _secondary_slot_keys(act_slots):
                f_lk = act_slots[f_key]
                tasks_list.append(TaskAnchorSpec.from_mapping(f_lk))
                f_cid = str(f_lk.get("conversation_id") or "").strip()
                if f_cid:
                    follower_cids.append(f_cid)

    q_data = load_dev_queue(portfolio_dir=portfolio_dir)
    filter_ids = [str(c).strip() for c in (candidate_ids or []) if str(c).strip()]
    seen_cids = {t.conversation_id for t in tasks_list if t.conversation_id}
    for it in q_data.get("items") or []:
        cid_it = str(it.get("conversation_id") or "").strip()
        if not cid_it or any(_matches_cid(cid_it, sc) for sc in seen_cids):
            continue
        if filter_ids:
            if not any(_matches_cid(cid_it, fid) for fid in filter_ids):
                continue
        else:
            st_it = str(it.get("status") or "").lower()
            if st_it not in ("pending", "in_progress", "pr_open"):
                continue
            item_repos = [dev_common.normalize_repo_slug(str(r)) for r in (it.get("repo_slugs") or []) if str(r).strip()]
            if resolved_slug and resolved_slug != "default" and item_repos and resolved_slug not in item_repos:
                continue
        seen_cids.add(cid_it)
        tasks_list.append(TaskAnchorSpec.from_mapping(it))

    batch_res = analyze_candidates_overlap(
        tasks_list,
        repo_dir=main_repo_dir,
        repo_slug=resolved_slug,
        primary_slot_id=primary_cid,
        active_slot_ids=follower_cids,
    )
    out = batch_res.to_dict()
    out["mode"] = "queue_and_locks"
    out["repo_slug"] = resolved_slug
    out["primary_slot_id"] = primary_cid
    out["active_follower_slot_ids"] = follower_cids
    if len(batch_res.pairwise) == 1:
        p0 = batch_res.pairwise[0]
        out["overlap_tier"] = p0.overlap_tier
        out["parallel_safe"] = p0.parallel_safe
        out["monolith_disjoint"] = p0.monolith_disjoint
    return out


def run_slot_rebase_reverify(
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    branch_name: str = "",
    base_branch: str = "main",
    fetch: bool = False,
    send_email: bool = False,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    verify_fn: Any | None = None,
) -> dict[str, Any]:
    """Invoke `rebase_gate.rebase_and_reverify` for an active slot and transition lock/queue state deterministically.

    - `AUTO_PROMOTED_ZERO_LLM`: if Slot #1 already passed independent review (`review_approved` or `PASS >= 91`),
      amends HEAD commit metadata, pushes `origin/<branch>`, publishes `manual_pr_url`, transitions lock to
      `PENDING_HUMAN_PR_VALIDATION` and queue to `pr_open` with 0 LLM tokens!
    - `NEEDS_AGENT_SEMANTIC_FIX`: records `NEEDS_AGENT_SEMANTIC_FIX` + `merged_a_diff` + `developer_remediation_plan`.
    - `NEEDS_AGENT_CONFLICT_RESOLUTION`: clean `git rebase --abort`, records `NEEDS_AGENT_CONFLICT_RESOLUTION` + conflicted files.
    """
    if _orch_ctrl.is_hard_stop_active(portfolio_dir=portfolio_dir):
        return {
            "status": "hard_stopped",
            "paused": True,
            "hard_stopped": True,
            "error": "orchestration_hard_stopped",
            "message": (
                "Orchestration is HARD STOPPED (Hard Freeze): refusing `rebase-reverify` mutation. "
                "Existing locks and worktrees are preserved until orchestration is resumed."
            ),
        }
    vcs = git_client or GitVcsIntegrationClient()
    lock_info = get_dev_pr_lock(
        portfolio_dir=portfolio_dir,
        repo_slug=repo_slug,
        workdir=workdir,
        branch_name=branch_name,
    )
    if not lock_info.get("active"):
        return {
            "status": "error",
            "error": "No active dev_pr_lock found for rebase-reverify.",
        }

    slot_num = int(lock_info.get("slot_id") or 1)
    resolved_branch = branch_name.strip() or str(lock_info.get("branch_name") or "").strip()
    resolved_ts = str(lock_info.get("branch_timestamp") or "").strip() or generate_branch_timestamp()
    resolved_slug = dev_common.normalize_repo_slug(
        repo_slug or str(lock_info.get("repo_slug") or lock_info.get("repo") or "")
    )
    cid_clean = str(lock_info.get("conversation_id") or "").strip()
    act_id = str(lock_info.get("action_id") or "").strip()
    recorded_base_sha = str(lock_info.get("base_sha") or "").strip()

    wt_raw = str(workdir or lock_info.get("worktree_path") or "").strip()
    wt_cand = Path(wt_raw.replace("$HOME", str(Path.home()))).expanduser() if wt_raw else None
    lock_main_dir = _resolve_lock_target_repo_dir(lock_info)
    target_dir: Path | None = None
    if wt_cand is not None and wt_cand.exists():
        target_dir = _find_git_root_from_path(wt_cand) or wt_cand.resolve()
    elif lock_main_dir is not None and (lock_main_dir / ".git").exists():
        target_dir = lock_main_dir
    elif resolved_slug and resolved_slug != "default":
        target_dir = _resolve_repo_dir_for_slug(resolved_slug, portfolio_dir=portfolio_dir, git_client=git_client)

    if target_dir is None or not (target_dir / ".git").exists():
        return {
            "status": "error",
            "branch_name": resolved_branch,
            "error": f"Git repository or worktree not found for `{resolved_branch}`.",
        }

    def _restamp_if_manifest_touched(eff_wt: Path, touched_b: list[str]) -> None:
        manifest_names = {"plugin.json", "pyproject.toml", "api/__init__.py"}
        if not (set(touched_b) & manifest_names):
            return
        stamp_res = stamp_repo_version_manifests(eff_wt, branch_timestamp=resolved_ts, force=True)
        if stamp_res.get("updated"):
            subprocess.run(
                ["git", "-C", str(eff_wt), "add", *(stamp_res.get("updated_files") or [])],
                env=_clean_git_subprocess_env(),
                capture_output=True,
                timeout=10,
                check=False,
            )
            subprocess.run(
                ["git", "-C", str(eff_wt), "commit", "--amend", "--no-edit", "--no-verify"],
                env=_clean_git_subprocess_env(),
                capture_output=True,
                timeout=15,
                check=False,
            )

    rb_res = rebase_and_reverify(
        target_dir,
        branch_name=resolved_branch,
        recorded_base_sha=recorded_base_sha,
        base_branch=base_branch,
        fetch=fetch,
        verify_fn=verify_fn,
        restamp_fn=_restamp_if_manifest_touched,
    )
    if rb_res.status != "ok":
        return rb_res.to_dict()

    outcome = rb_res.outcome
    rca_summary = str(lock_info.get("summary") or "")
    proposed_fix = str(lock_info.get("proposed_fix") or "")
    exec_summary = str(lock_info.get("executive_summary") or proposed_fix or rca_summary)
    proposal_title = str(lock_info.get("proposal_title") or "").strip() or format_remediation_commit_title(
        f"update {act_id or resolved_branch}",
        action_id=act_id,
        conversation_id=cid_clean,
        branch_timestamp=resolved_ts,
    )

    if outcome == OUTCOME_AUTO_PROMOTED:
        was_review_approved = bool(lock_info.get("review_approved")) or (
            str(lock_info.get("reviewer_verdict") or "").upper() == "PASS"
            and int(lock_info.get("reviewer_score") or 0) >= PR_REVIEW_PASS_MIN_SCORE
        )
        # Promote directly to open human PR when this is Slot #1 and review already passed!
        if slot_num == 1 and was_review_approved:
            rev_score = int(lock_info.get("reviewer_score") or 95)
            rev_summary = str(lock_info.get("reviewer_summary") or "Validated prior to 0-LLM base rebase-reverify.")
            rev_sub_id = str(lock_info.get("reviewer_subagent_id") or "")
            rev_iter = max(1, int(lock_info.get("review_iteration") or 1))
            eff_wt_path = _resolve_worktree_for_branch(target_dir, resolved_branch) or target_dir
            stamp_promo = stamp_repo_version_manifests(eff_wt_path, branch_timestamp=resolved_ts, force=True)
            if stamp_promo.get("updated"):
                subprocess.run(
                    ["git", "-C", str(eff_wt_path), "add", *(stamp_promo.get("updated_files") or [])],
                    env=_clean_git_subprocess_env(),
                    capture_output=True,
                    timeout=10,
                    check=False,
                )
            diff_info = vcs.get_branch_diff(head_branch=resolved_branch, base_branch=base_branch, repo_dir=eff_wt_path)
            files_changed = list(diff_info.get("files") or [])
            canonical_body = build_canonical_pr_description(
                commit_title=proposal_title,
                executive_summary=exec_summary,
                rca_summary=rca_summary,
                proposed_fix=proposed_fix,
                branch_name=resolved_branch,
                branch_timestamp=resolved_ts,
                conversation_id=cid_clean,
                files_changed=files_changed,
                verification_results=rb_res.verification_results,
                reviewer_verdict="PASS",
                reviewer_score=rev_score,
                reviewer_summary=rev_summary,
                reviewer_subagent_id=rev_sub_id,
                review_iteration=rev_iter,
                max_review_iterations=MAX_PR_REVIEW_ITERATIONS,
            )
            amend_res = _sync_branch_head_commit_message_detailed(
                target_repo_dir=eff_wt_path,
                branch_name=resolved_branch,
                base_branch=base_branch,
                commit_title=proposal_title,
                commit_body=canonical_body,
            )
            if not amend_res.get("amended"):
                return {
                    "status": "error",
                    "step": "amend_rebased_commit",
                    "outcome": outcome,
                    "error": str(amend_res.get("error") or "Failed to amend rebased commit message."),
                }
            final_head_sha = str(amend_res.get("head_sha") or rb_res.new_head_sha)
            eff_push_dir = amend_res.get("effective_repo_dir") if isinstance(amend_res.get("effective_repo_dir"), Path) else eff_wt_path
            updated_vr = {**rb_res.verification_results, "head_sha": final_head_sha, "dirty": False}

            try:
                push_res = vcs.push_branch(
                    branch_name=resolved_branch,
                    remote="origin",
                    base_branch=base_branch,
                    set_upstream=True,
                    force_with_lease=True,
                    title=proposal_title,
                    body=canonical_body,
                    repo_dir=eff_push_dir,
                )
            except TypeError:
                push_res = vcs.push_branch(
                    branch_name=resolved_branch,
                    remote="origin",
                    base_branch=base_branch,
                    set_upstream=True,
                    repo_dir=eff_push_dir,
                )
            if not isinstance(push_res, dict) or push_res.get("status") != "ok":
                return {
                    "status": "error",
                    "step": "push_promoted_branch",
                    "outcome": outcome,
                    "error": str((push_res or {}).get("error") or f"Failed to push promoted branch '{resolved_branch}'."),
                }

            final_head_sha = str(push_res.get("head_sha") or final_head_sha)
            try:
                manual_pr_url = vcs.build_manual_pr_url(
                    head_branch=resolved_branch,
                    base_branch=base_branch,
                    title=proposal_title,
                    body=canonical_body,
                    repo_dir=eff_push_dir,
                )
            except TypeError:
                manual_pr_url = vcs.build_manual_pr_url(head_branch=resolved_branch, base_branch=base_branch)

            now_iso = _utc_now_iso()
            persistent_root = _resolve_main_git_repo_root(eff_push_dir)
            raw_rb = lock_info.get("repo_branches") if isinstance(lock_info.get("repo_branches"), dict) else {}
            upd_rb = {str(k): dict(v) for k, v in raw_rb.items() if str(k).strip() and isinstance(v, dict)}
            if resolved_slug:
                upd_rb[resolved_slug] = {
                    "repo_slug": resolved_slug,
                    "branch_name": resolved_branch,
                    "branch_timestamp": resolved_ts,
                    "target_repo_dir": _sanitize_home_paths(str(persistent_root)),
                    "manual_pr_url": manual_pr_url,
                    "branch_pushed": True,
                    "head_sha": final_head_sha,
                    "updated_at": now_iso,
                }

            update_dev_pr_lock(
                {
                    "status": "PENDING_HUMAN_PR_VALIDATION",
                    "branch_action": OUTCOME_AUTO_PROMOTED,
                    "base_sha": rb_res.new_base_sha,
                    "head_sha": final_head_sha,
                    "reviewed_head_sha": final_head_sha,
                    "approved_head_sha": final_head_sha,
                    "deferred_send_email": False,
                    "branch_pushed": True,
                    "pr_gate_ready": True,
                    "needs_base_rebase": False,
                    "manual_pr_url": manual_pr_url,
                    "pr_url": manual_pr_url,
                    "repo_branches": upd_rb,
                    "proposal_body": canonical_body,
                    "verification_results": updated_vr,
                    "rebase_outcome": OUTCOME_AUTO_PROMOTED,
                    "last_rebase_result": rb_res.to_dict(),
                },
                portfolio_dir=portfolio_dir,
                repo_slug=resolved_slug,
                workdir=eff_push_dir,
                branch_name=resolved_branch,
                slot_id=1,
            )
            if cid_clean:
                with contextlib.suppress(Exception):
                    update_dev_queue_item_status(
                        conversation_id=cid_clean,
                        status="pr_open",
                        branch_names=[resolved_branch],
                        pr_urls=[manual_pr_url],
                        notes=f"Auto-promoted via 0-LLM rebase-reverify onto {rb_res.new_base_sha[:8]}.",
                        portfolio_dir=portfolio_dir,
                    )

            # PR Ready for Validation (status='OK') emails are permanently disabled on 0-LLM follower Slot #2..#N promotion.
            _ = send_email
            email_sent = False

            watcher_res = _facade_attr("spawn_pr_merge_watcher_daemon", spawn_pr_merge_watcher_daemon)(portfolio_dir=portfolio_dir)
            return {
                **rb_res.to_dict(),
                "slot_id": 1,
                "promoted_to_human_pr": True,
                "ready_for_human_pr": True,
                "pr_gate_ready": True,
                "branch_pushed": True,
                "head_sha": final_head_sha,
                "manual_pr_url": manual_pr_url,
                "pr_url": manual_pr_url,
                "notification_email_sent": email_sent,
                "pr_merge_watcher": watcher_res,
            }

        next_st = (
            SLOT_STATUS_READY_AWAITING_BASE_MERGE
            if (slot_num > 1 and was_review_approved)
            else str(lock_info.get("status") or "DEVELOPING")
        )
        if next_st == SLOT_STATUS_NEEDS_REBASE_REVERIFY:
            next_st = "DEVELOPING"
        update_dev_pr_lock(
            {
                "status": next_st,
                "base_sha": rb_res.new_base_sha,
                "head_sha": rb_res.new_head_sha,
                "needs_base_rebase": False,
                "verification_results": rb_res.verification_results,
                "rebase_outcome": OUTCOME_AUTO_PROMOTED,
                "last_rebase_result": rb_res.to_dict(),
            },
            portfolio_dir=portfolio_dir,
            repo_slug=resolved_slug,
            workdir=target_dir,
            branch_name=resolved_branch,
            slot_id=slot_num,
        )
        return {
            **rb_res.to_dict(),
            "slot_id": slot_num,
            "promoted_to_human_pr": False,
            "ready_for_human_pr": False,
            "pr_gate_ready": False,
            "branch_pushed": False,
            "manual_pr_url": "",
        }

    # Needs agent intervention (`NEEDS_AGENT_SEMANTIC_FIX` or `NEEDS_AGENT_CONFLICT_RESOLUTION`)
    next_lock_status = (
        SLOT_STATUS_NEEDS_AGENT_SEMANTIC_FIX
        if outcome == OUTCOME_NEEDS_SEMANTIC_FIX
        else SLOT_STATUS_NEEDS_AGENT_CONFLICT_RESOLUTION
    )
    feedback_ctx = (
        f"Post-merge rebase-reverify returned `{outcome}`.\n"
        + (f"- Conflicted Files: `{', '.join(rb_res.conflicted_files)}`\n" if rb_res.conflicted_files else "")
        + f"- Cross-Impact Union Files (`files(B) ∪ files(merged_A)`): `{', '.join(rb_res.union_files)}`\n"
        + (f"- Merged Slot #1 Commits:\n" + "\n".join(f"  * {c}" for c in rb_res.merged_a_commits) + "\n" if rb_res.merged_a_commits else "")
        + (f"- Verification / Conflict Details:\n{rb_res.failure_details[:2000]}\n" if rb_res.failure_details else "")
        + (f"- Merged Slot #1 Diff Excerpt:\n```diff\n{rb_res.merged_a_diff[:4000]}\n```\n" if rb_res.merged_a_diff else "")
    )
    dev_plan = _build_developer_remediation_plan(
        conversation_id=cid_clean or resolved_ts,
        action_id=act_id,
        repo_slug=resolved_slug,
        branch_name=resolved_branch,
        workdir=str(target_dir),
        review_iteration=max(1, int(lock_info.get("review_iteration") or 1)),
        verdict="NEEDS_REVISION",
        score=0,
        reviewer_feedback=feedback_ctx,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        target_files=list(rb_res.union_files or lock_info.get("target_files") or []),
    )
    upd_fields: dict[str, Any] = {
        "status": next_lock_status,
        "branch_action": next_lock_status,
        "review_approved": False,
        "pr_gate_ready": False,
        "branch_pushed": False,
        "manual_pr_url": "",
        "pr_url": "",
        "rebase_outcome": outcome,
        "last_rebase_result": rb_res.to_dict(),
    }
    if outcome == OUTCOME_NEEDS_SEMANTIC_FIX:
        upd_fields["base_sha"] = rb_res.new_base_sha
        upd_fields["head_sha"] = rb_res.new_head_sha
        upd_fields["verification_results"] = rb_res.verification_results
    update_dev_pr_lock(
        upd_fields,
        portfolio_dir=portfolio_dir,
        repo_slug=resolved_slug,
        workdir=target_dir,
        branch_name=resolved_branch,
        slot_id=slot_num,
    )
    if cid_clean:
        with contextlib.suppress(Exception):
            update_dev_queue_item_status(
                conversation_id=cid_clean,
                status="in_progress",
                branch_names=[resolved_branch],
                strip_pr_urls_for_branches=[resolved_branch],
                strip_repo_slug=resolved_slug,
                notes=f"Rebase-reverify returned {outcome}; awaiting targeted developer remediation.",
                portfolio_dir=portfolio_dir,
            )

    return {
        **rb_res.to_dict(),
        "slot_id": slot_num,
        "lock_status": next_lock_status,
        "promoted_to_human_pr": False,
        "ready_for_human_pr": False,
        "pr_gate_ready": False,
        "branch_pushed": False,
        "manual_pr_url": "",
        "developer_remediation_plan": dev_plan,
    }


def register_review_cli(sub: Any) -> None:
    """Register developer-plan, review-staging, diff, evaluation, overlap & rebase subcommands."""
    p_plan_dev = sub.add_parser(
        "plan-developer",
        help="Build the subagent_definition and subagent_invocation for agy-code-developer when a code change is required.",
    )
    p_plan_dev.add_argument("--id", "--conversation-id", dest="conversation_id", required=True, help="Target Conversation ID.")
    p_plan_dev.add_argument("--action-id", default="", help="Optional Action or prompt scope ID.")
    p_plan_dev.add_argument("--rca-summary", default="", help="Root Cause Analysis or prompt rationale summary.")
    p_plan_dev.add_argument("--proposed-fix", default="", help="Proposed solution description.")
    p_plan_dev.add_argument("--target-files", nargs="*", default=[], help="Affected files.")
    p_plan_dev.add_argument("--sibling-ids", nargs="*", default=[], help="Grouped sibling Conversation IDs.")
    p_plan_dev.add_argument("--scheduled", action="store_true", help="Mark as scheduled run.")
    p_plan_dev.add_argument("--model", choices=("pro", "flash", "flash_lite", "inherit"), default="", help="Optional developer model tier (defaults to flash Round 1, pro Round 2+).")
    p_plan_dev.add_argument("--review-iteration", dest="review_iteration", type=int, default=1, help="Development/review iteration number (>=2 auto-escalates flash -> pro).")
    p_plan_dev.add_argument("--risk-tier", dest="risk_tier", choices=("", "micro", "standard", "critical"), default="", help="Optional 3-tier change risk override (auto-classified from --target-files when omitted).")
    p_plan_dev.add_argument("--anchored-context", default="", help="Optional pre-anchored file line ranges and contracts.")
    p_plan_dev.add_argument("--repo", dest="repo_slug", default="", help="Optional target owner/repo slug.")
    p_plan_dev.add_argument("--workdir", default="", help="Optional target repository directory.")
    p_plan_dev.add_argument("--verbose", action="store_true", help="Emit full subagent_definition SystemPrompt (default is compact token-saving JSON).")

    for push_cmd in ("push-branch", "prepare-review", "create-pr"):
        p_push = sub.add_parser(
            push_cmd,
            help="Local-First Review: commit locally on the timestamped branch (WITHOUT pushing to origin yet), stamp verification checks, and return the agy-code-pr-reviewer invocation plan.",
        )
        p_push.add_argument("--title", required=True, help="Commit / proposal title.")
        p_push.add_argument("--body", default="", help="Commit / proposal Markdown body.")
        p_push.add_argument("--executive-summary", "--summary", dest="executive_summary", default="", help="Explicit Executive Summary for the PR description.")
        p_push.add_argument("--head", "--branch", dest="head", default="", help="Branch name (defaults to locked branch_name).")
        p_push.add_argument("--base", default="main", help="Base branch name (default: main).")
        p_push.add_argument("--issue", type=int, default=None, help="Optional legacy issue number.")
        p_push.add_argument("--workdir", default="", help="Optional worktree or repo directory.")
        p_push.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
        p_push.add_argument("--scheduled", action="store_true", help="Mark as scheduled run so Agent 2 sends notification email upon PASS (>90/100).")
        p_push.add_argument("--verbose", action="store_true", help="Emit full reviewer_subagent_definition and nested lock blobs (default is compact token-saving JSON).")

    p_diff = sub.add_parser("branch-diff", help="Compute changed files and unified diff for a branch via native git.")
    p_diff.add_argument("--branch", "--head", dest="branch", default="", help="Target branch name (defaults to locked branch_name).")
    p_diff.add_argument("--base", default="main", help="Base branch name (default: main).")
    p_diff.add_argument("--workdir", default="", help="Optional target repository directory.")
    p_diff.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")

    p_rev = sub.add_parser(
        "record-review",
        help="Record Agent 2 (agy-code-pr-reviewer) evaluation and enforce the PASS>90 PR readiness gate (zero PR Ready emails).",
    )
    p_rev.add_argument("--branch", default="", help="Timestamped branch name (defaults to locked branch_name).")
    p_rev.add_argument("--workdir", default="", help="Optional worktree or target repository directory.")
    p_rev.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_rev.add_argument("--pr", type=int, default=None, help="Optional legacy PR number.")
    p_rev.add_argument("--verdict", choices=("PASS", "WARN", "NEEDS_REVISION"), required=True, help="Reviewer verdict.")
    p_rev.add_argument("--score", type=int, required=True, help="Quality score (0-100).")
    p_rev.add_argument("--summary", required=True, help="Detailed review evaluation summary.")
    p_rev.add_argument("--executive-summary", default="", help="Optional Executive Summary override.")
    p_rev.add_argument("--subagent-id", default="", help="Reviewer subagent conversationId.")
    p_rev.add_argument("--reviewer-model", dest="reviewer_model", default="", help="Optional actual reviewer model ID (e.g. pro or fallback model ID).")
    p_rev.add_argument("--dev-model", dest="dev_model", default="", help="Optional actual developer model ID override for created_by attribution.")
    p_rev.add_argument(
        "--send-email",
        action="store_true",
        help="Dispatch Round-3 [🟥] KO escalation alert if review circuit-breaker trips (deprecated no-op on PASS; PR Ready emails are disabled).",
    )

    for eval_cmd in ("evaluate-branch", "finalize-pr"):
        p_fin = sub.add_parser(
            eval_cmd,
            help="Record manual validation decision (approve after manual PR creation, or reject to delete branch) and release the repository lock.",
        )
        p_fin.add_argument("--branch", default="", help="Timestamped branch name (defaults to locked branch_name).")
        p_fin.add_argument("--workdir", default="", help="Optional target repository directory.")
        p_fin.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
        p_fin.add_argument("--pr", type=int, default=None, help="Optional legacy PR number.")
        p_fin.add_argument("--decision", choices=("approve", "reject"), required=True, help="Evaluation decision.")
        p_fin.add_argument("--comment", required=True, help="Review/validation comment.")
        p_fin.add_argument("--method", choices=("squash", "merge", "rebase"), default="squash", help="Optional merge method.")
        p_fin.add_argument("--close-issue", action="store_true", help="Optional legacy flag.")
        p_fin.add_argument("--cleanup-conversations", action="store_true", help="Safely clean up merged root and child conversations after approve release.")
        p_fin.add_argument("--exclude-id", "--exclude-ids", dest="exclude_ids", nargs="*", default=[], help="Protected conversation IDs (e.g., active orchestrator) to preserve during cleanup.")
        p_fin.add_argument("--force", action="store_true", help="Override PR readiness gate verification on approve (emergency HITL escape hatch only).")

    p_ov = sub.add_parser(
        "analyze-overlap",
        help="0-LLM AST/section blast-radius overlap & landing-order analyzer across active Slot #1 and candidate tasks.",
    )
    p_ov.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_ov.add_argument("--workdir", default="", help="Optional target repository directory.")
    p_ov.add_argument("--id", "--ids", dest="candidate_ids", nargs="*", default=[], help="Optional candidate queue conversation IDs.")
    p_ov.add_argument("--files-a", nargs="*", default=[], help="Direct pair mode: target files for Task A.")
    p_ov.add_argument("--anchors-a", nargs="*", default=[], help="Direct pair mode: section/symbol/line anchors for Task A.")
    p_ov.add_argument("--files-b", nargs="*", default=[], help="Direct pair mode: target files for Task B.")
    p_ov.add_argument("--anchors-b", nargs="*", default=[], help="Direct pair mode: section/symbol/line anchors for Task B.")
    p_ov.add_argument("--tasks-json", default="", help="Optional JSON array of task objects for batch overlap analysis.")
    p_ov.add_argument("--no-active-lock", dest="no_active_lock", action="store_true", help="Exclude active repository lock from queue overlap scan.")

    p_rbr = sub.add_parser(
        "rebase-reverify",
        help="0-LLM post-merge rebase & cross-impact re-verification gate (`AUTO_PROMOTED_ZERO_LLM`, `NEEDS_AGENT_SEMANTIC_FIX`, `NEEDS_AGENT_CONFLICT_RESOLUTION`).",
    )
    p_rbr.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_rbr.add_argument("--workdir", default="", help="Optional worktree or repository directory.")
    p_rbr.add_argument("--branch", default="", help="Optional branch name (defaults to locked slot branch).")
    p_rbr.add_argument("--base", default="main", help="Base branch name (default: main).")
    p_rbr.add_argument("--fetch", action="store_true", help="Fetch origin before rebasing.")
    p_rbr.add_argument(
        "--send-email",
        action="store_true",
        help="Retained for backward CLI compatibility (deprecated no-op; PR Ready emails are disabled).",
    )

    p_quota = sub.add_parser(
        "quota-status",
        help="Inspect or update 0600 model_quota_cooldowns.json and compute deterministic optimal/low-quota fallback model routing.",
    )
    p_quota.add_argument(
        "--role",
        choices=("developer", "reviewer", "orchestrator", "wake"),
        default="developer",
        help="Target role for model recommendation (default: developer).",
    )
    p_quota.add_argument("--preferred-model", dest="preferred_model", default="", help="Optional preferred model override.")
    p_quota.add_argument("--review-iteration", dest="review_iteration", type=int, default=1, help="Review/dev iteration number.")
    p_quota.add_argument("--risk-tier", dest="risk_tier", choices=("", "micro", "standard", "critical"), default="", help="Optional 3-tier change risk override (micro/standard/critical).")
    p_quota.add_argument("--files", dest="files", nargs="*", default=[], help="Optional changed/target files to auto-classify 3-tier change risk.")
    p_quota.add_argument("--lines-changed", dest="lines_changed", type=int, default=None, help="Optional changed LOC count for 3-tier risk classification.")
    p_quota.add_argument("--dev-model", dest="dev_model", default="", help="Optional developer model ID to enforce Cross-Family Maker-Checker separation.")
    p_quota.add_argument("--exclude-family", dest="exclude_family", default="", help="Optional family/pool to exclude during cross-family balancing.")
    p_quota.add_argument("--routing-strategy", dest="routing_strategy", choices=("", "balanced_cross_family", "sequential"), default="", help="Optional routing strategy override.")
    p_quota.add_argument("--record-swarm-json", dest="record_swarm_json", default="", help="Optional raw JSON string (or @path) from swarm(action='models', all=true) to persist.")
    p_quota.add_argument("--record-cooldown-model", dest="record_cooldown_model", default="", help="Optional exhausted model ID to record in cooldown ledger.")
    p_quota.add_argument("--resets-at", dest="resets_at", default="", help="Optional ISO-8601 UTC reset timestamp for --record-cooldown-model.")
    p_quota.add_argument("--resets-in-seconds", dest="resets_in_seconds", type=int, default=None, help="Optional reset duration in seconds for --record-cooldown-model.")

    p_prom = sub.add_parser(
        "promote-org-pr",
        help="Sync upstream/main across Personal-Fork DEV clones and emit Cross-Fork PR compare URLs (org:main...user:repo:main).",
    )
    p_prom.add_argument("--repo-dir", "--workdir", dest="repo_dir", default="", help="Optional single fork repository directory.")
    p_prom.add_argument("--dev-repos-dir", dest="dev_repos_dir", default="", help="Optional AGY_DEV_REPOS_DIR override.")
    p_prom.add_argument("--repos", nargs="*", default=[], help="Optional subset of repository names.")
    p_prom.add_argument("--upstream", default="upstream", help="Upstream Org remote name (default: upstream).")
    p_prom.add_argument("--fork-remote", dest="fork_remote", default="origin", help="Fork remote name (default: origin).")
    p_prom.add_argument("--base", default="main", help="Org target branch (default: main).")
    p_prom.add_argument("--head", default="main", help="Fork source branch (default: main).")
    p_prom.add_argument("--no-fetch", dest="no_fetch", action="store_true", help="Skip git fetch before comparing.")

    p_sprod = sub.add_parser(
        "sync-prod",
        help="Manual Sync Prod: pull Org clones (--ff-only), run 0-LLM cross-repo smoke-check, deploy $HOME, restart :8787 once, and store 0600 rollback SHAs.",
    )
    p_sprod.add_argument("--prod-repos-dir", dest="prod_repos_dir", default="", help="Optional AGY_PROD_REPOS_DIR override.")
    p_sprod.add_argument("--state-dir", dest="state_dir", default="", help="Optional state directory override.")
    p_sprod.add_argument("--home-dir", dest="home_dir", default="", help="Optional target $HOME override.")
    p_sprod.add_argument("--remote", default="origin", help="Git remote on Org clones (default: origin).")
    p_sprod.add_argument("--base", default="main", help="Base branch on Org clones (default: main).")
    p_sprod.add_argument("--repos", nargs="*", default=[], help="Optional subset of repository names.")
    p_sprod.add_argument("--no-fetch", dest="no_fetch", action="store_true", help="Use local remote-tracking ref without network git pull.")

    p_rbprod = sub.add_parser(
        "rollback-prod",
        help="1-Command Rollback Prod: restore AGY_PROD_REPOS_DIR to recorded previous_shas, smoke-check, redeploy $HOME, and restart :8787 once.",
    )
    p_rbprod.add_argument("--prod-repos-dir", dest="prod_repos_dir", default="", help="Optional AGY_PROD_REPOS_DIR override.")
    p_rbprod.add_argument("--state-dir", dest="state_dir", default="", help="Optional state directory override.")
    p_rbprod.add_argument("--home-dir", dest="home_dir", default="", help="Optional target $HOME override.")
    p_rbprod.add_argument("--base", default="main", help="Base branch on Org clones (default: main).")

    p_cdev = sub.add_parser(
        "clone-dev-state",
        help="Populate or refresh ~/.local/share/wiz-customer-portfolio-dev via macOS APFS CoW (cp -cR) with portable Unix fallback.",
    )
    p_cdev.add_argument("--src-dir", dest="src_dir", default="", help="Optional source Prod portfolio directory override.")
    p_cdev.add_argument("--dst-dir", dest="dst_dir", default="", help="Optional destination DEV portfolio directory override.")
    p_cdev.add_argument("--refresh", action="store_true", help="Force refresh even if DEV state already exists.")

    p_rdev = sub.add_parser(
        "reload-dev-server",
        help="Reload Personal-Fork DEV (:8786) with AGY_RUNTIME_ENV=dev, fork PYTHONPATH, and APFS state clone without touching :8787.",
    )
    p_rdev.add_argument("--dev-repos-dir", dest="dev_repos_dir", default="", help="Optional AGY_DEV_REPOS_DIR override.")
    p_rdev.add_argument("--port", type=int, default=None, help="Optional DEV port override (default: AGY_DEV_PORT / 8786).")
    p_rdev.add_argument("--refresh-state", dest="refresh_state", action="store_true", help="Force refresh APFS DEV state clone before reload.")


def _compact_review_cli_output(subcommand: str, res: dict[str, Any]) -> dict[str, Any]:
    """Strip bulky redundant prompts & nested lock blobs from CLI stdout to minimize LLM context tokens."""
    if not isinstance(res, dict):
        return res
    if subcommand == "plan-developer":
        out = {k: v for k, v in res.items() if k != "subagent_definition"}
        out["compact"] = True
        return out
    if subcommand in ("push-branch", "prepare-review", "create-pr"):
        drop_keys = ("reviewer_subagent_definition", "lock", "files_changed")
        out = {k: v for k, v in res.items() if k not in drop_keys}
        lock_src = res.get("lock") if isinstance(res.get("lock"), dict) else None
        if lock_src is not None:
            out["lock"] = {
                k: lock_src.get(k)
                for k in (
                    "lock_id",
                    "slot_id",
                    "status",
                    "repo_slug",
                    "branch_name",
                    "worktree_path",
                    "dev_model",
                    "staged_head_sha",
                    "pr_gate_ready",
                )
                if k in lock_src
            }
        vr = res.get("verification_results")
        if isinstance(vr, dict):
            out["verification_results"] = {
                k: v for k, v in vr.items() if k not in ("qa_output", "test_output", "coverage_output", "pre_commit_output")
            }
        out["compact"] = True
        return out
    return res


def dispatch_review_cli(args: Any) -> int | None:
    """Handle review-gate CLI subcommands; return exit code or None if unhandled."""
    if args.subcommand == "plan-developer":
        res = _facade_attr("build_code_developer_subagent_plan", build_code_developer_subagent_plan)(
            conversation_id=args.conversation_id,
            action_id=args.action_id,
            rca_summary=args.rca_summary,
            proposed_fix=args.proposed_fix,
            target_files=args.target_files,
            sibling_ids=args.sibling_ids,
            scheduled=args.scheduled,
            model=getattr(args, "model", ""),
            anchored_context=getattr(args, "anchored_context", ""),
            workdir=getattr(args, "workdir", ""),
            repo_slug=getattr(args, "repo_slug", ""),
            review_iteration=int(getattr(args, "review_iteration", 1) or 1),
            risk_tier=str(getattr(args, "risk_tier", "") or ""),
        )
        full_payload = {"status": "ok", **res}
        printed = full_payload if getattr(args, "verbose", False) else _compact_review_cli_output("plan-developer", full_payload)
        print(json.dumps(printed, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand in ("push-branch", "prepare-review", "create-pr"):
        res = _facade_attr(
            "push_remediation_branch_and_build_reviewer_plan",
            push_remediation_branch_and_build_reviewer_plan,
        )(
            title=args.title,
            body=args.body,
            executive_summary=getattr(args, "executive_summary", ""),
            head_branch=args.head,
            base_branch=args.base,
            issue_number=args.issue,
            scheduled=args.scheduled,
            workdir=args.workdir or None,
            repo_slug=getattr(args, "repo_slug", ""),
        )
        printed = res if getattr(args, "verbose", False) else _compact_review_cli_output(args.subcommand, res)
        print(json.dumps(printed, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "branch-diff":
        res = _facade_attr(
            "get_branch_diff_with_verification_gate",
            get_branch_diff_with_verification_gate,
        )(
            branch=args.branch.strip(),
            base_branch=args.base,
            workdir=getattr(args, "workdir", "") or None,
            repo_slug=getattr(args, "repo_slug", ""),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "record-review":
        res = _facade_attr("record_pr_review_evaluation", record_pr_review_evaluation)(
            pr_number=args.pr,
            verdict=args.verdict,
            score=args.score,
            summary=args.summary,
            executive_summary=getattr(args, "executive_summary", ""),
            branch_name=args.branch,
            reviewer_subagent_id=args.subagent_id,
            reviewer_model=getattr(args, "reviewer_model", ""),
            dev_model=getattr(args, "dev_model", ""),
            send_email=args.send_email,
            workdir=getattr(args, "workdir", "") or None,
            repo_slug=getattr(args, "repo_slug", ""),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand in ("evaluate-branch", "finalize-pr"):
        res = _facade_attr("evaluate_branch_and_release_lock", evaluate_branch_and_release_lock)(
            decision=args.decision,
            comment=args.comment,
            branch_name=args.branch,
            pr_number=args.pr,
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
            force=bool(getattr(args, "force", False)),
        )
        if res.get("status") == "ok" and getattr(args, "cleanup_conversations", False) and str(args.decision).lower() in ("approve", "merge"):
            prev_lock = (res.get("release_result") or {}).get("previous_lock") or {}
            prev_cid = str(prev_lock.get("conversation_id") or "").strip()
            if prev_cid:
                res["conversation_cleanup"] = _facade_attr(
                    "cleanup_merged_conversations",
                    cleanup_merged_conversations,
                )(
                    conversation_ids=[prev_cid],
                    exclude_ids=list(getattr(args, "exclude_ids", []) or []),
                    confirm_merged=True,
                )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "analyze-overlap":
        ov_res = _facade_attr("analyze_queue_or_tasks_overlap", analyze_queue_or_tasks_overlap)(
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
            candidate_ids=list(getattr(args, "candidate_ids", []) or []) or None,
            files_a=list(getattr(args, "files_a", []) or []) or None,
            files_b=list(getattr(args, "files_b", []) or []) or None,
            anchors_a=list(getattr(args, "anchors_a", []) or []) or None,
            anchors_b=list(getattr(args, "anchors_b", []) or []) or None,
            tasks_json=str(getattr(args, "tasks_json", "") or ""),
            include_active_lock=not bool(getattr(args, "no_active_lock", False)),
        )
        print(json.dumps(ov_res, indent=2, ensure_ascii=False))
        return 0 if ov_res.get("status") == "ok" else 1

    if args.subcommand == "rebase-reverify":
        rbr_res = _facade_attr("run_slot_rebase_reverify", run_slot_rebase_reverify)(
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
            branch_name=str(getattr(args, "branch", "") or ""),
            base_branch=str(getattr(args, "base", "main") or "main"),
            fetch=bool(getattr(args, "fetch", False)),
            send_email=bool(getattr(args, "send_email", False)),
        )
        print(json.dumps(rbr_res, indent=2, ensure_ascii=False))
        return 0 if rbr_res.get("status") == "ok" else 1

    if args.subcommand == "quota-status":
        snapshot_res: dict[str, Any] | None = None
        cooldown_res: dict[str, Any] | None = None
        raw_swarm = str(getattr(args, "record_swarm_json", "") or "").strip()
        if raw_swarm:
            try:
                if raw_swarm.startswith("@"):
                    payload_obj = json.loads(Path(raw_swarm[1:]).expanduser().read_text(encoding="utf-8"))
                else:
                    payload_obj = json.loads(raw_swarm)
                if isinstance(payload_obj, dict):
                    snapshot_res = dev_common.record_swarm_quota_snapshot(payload_obj)
            except (OSError, json.JSONDecodeError) as exc:
                print(json.dumps({"status": "error", "error": f"Invalid --record-swarm-json: {exc}"}, indent=2))
                return 1
        cd_model = str(getattr(args, "record_cooldown_model", "") or "").strip()
        if cd_model:
            cooldown_res = dev_common.record_model_quota_cooldown(
                cd_model,
                resets_at=str(getattr(args, "resets_at", "") or ""),
                resets_in_seconds=getattr(args, "resets_in_seconds", None),
                source="cli",
            )
        state = dev_common.load_model_quota_state()
        q_files = list(getattr(args, "files", []) or [])
        q_lines = getattr(args, "lines_changed", None)
        q_tier = str(getattr(args, "risk_tier", "") or "").strip().lower()
        risk_cls = (
            dev_common.classify_change_risk_tier(q_files, lines_changed=q_lines)
            if (q_files or q_lines is not None)
            else None
        )
        if not q_tier and isinstance(risk_cls, dict):
            q_tier = str(risk_cls.get("risk_tier") or "")
        routing = dev_common.select_next_optimal_model(
            role=str(getattr(args, "role", "developer") or "developer"),
            preferred_model=str(getattr(args, "preferred_model", "") or ""),
            review_iteration=int(getattr(args, "review_iteration", 1) or 1),
            risk_tier=q_tier,
            dev_model=str(getattr(args, "dev_model", "") or ""),
            exclude_family=str(getattr(args, "exclude_family", "") or ""),
            routing_strategy=str(getattr(args, "routing_strategy", "") or ""),
        )
        out: dict[str, Any] = {
            "status": "ok",
            "routing": routing,
            "state": {
                "updated_at": state.get("updated_at", ""),
                "active_cooldowns": state.get("cooldowns", {}),
                "snapshot_fresh": bool((state.get("quota_snapshot") or {}).get("fresh")),
                "snapshot_recorded_at": (state.get("quota_snapshot") or {}).get("recorded_at", ""),
            },
        }
        if risk_cls is not None:
            out["risk_classification"] = risk_cls
        if snapshot_res is not None:
            out["snapshot_recorded"] = snapshot_res
        if cooldown_res is not None:
            out["cooldown_recorded"] = cooldown_res
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "promote-org-pr":
        prom_res = _facade_attr("promote_fork_to_org_pr", _git_client.promote_fork_to_org_pr)(
            repo_dir=getattr(args, "repo_dir", "") or None,
            dev_repos_dir=getattr(args, "dev_repos_dir", "") or None,
            repo_names=list(getattr(args, "repos", []) or []) or None,
            upstream_remote=str(getattr(args, "upstream", "upstream") or "upstream"),
            fork_remote=str(getattr(args, "fork_remote", "origin") or "origin"),
            base_branch=str(getattr(args, "base", "main") or "main"),
            head_branch=str(getattr(args, "head", "main") or "main"),
            fetch=not bool(getattr(args, "no_fetch", False)),
        )
        print(json.dumps(prom_res, indent=2, ensure_ascii=False))
        return 0 if prom_res.get("status") == "ok" else 1

    if args.subcommand == "sync-prod":
        home_ov = (
            Path(args.home_dir).expanduser().resolve()
            if str(getattr(args, "home_dir", "") or "").strip()
            else None
        )
        sp_res = _facade_attr("sync_prod_environment", _git_client.sync_prod_environment)(
            prod_repos_dir=getattr(args, "prod_repos_dir", "") or None,
            state_dir=getattr(args, "state_dir", "") or None,
            home_dir=home_ov,
            remote=str(getattr(args, "remote", "origin") or "origin"),
            base_branch=str(getattr(args, "base", "main") or "main"),
            fetch=not bool(getattr(args, "no_fetch", False)),
            repo_names=list(getattr(args, "repos", []) or []) or None,
        )
        print(json.dumps(sp_res, indent=2, ensure_ascii=False))
        return 0 if sp_res.get("status") == "ok" else 1

    if args.subcommand == "rollback-prod":
        home_ov = (
            Path(args.home_dir).expanduser().resolve()
            if str(getattr(args, "home_dir", "") or "").strip()
            else None
        )
        rb_res = _facade_attr("rollback_prod_environment", _git_client.rollback_prod_environment)(
            prod_repos_dir=getattr(args, "prod_repos_dir", "") or None,
            state_dir=getattr(args, "state_dir", "") or None,
            home_dir=home_ov,
            base_branch=str(getattr(args, "base", "main") or "main"),
        )
        print(json.dumps(rb_res, indent=2, ensure_ascii=False))
        return 0 if rb_res.get("status") == "ok" else 1

    if args.subcommand == "clone-dev-state":
        cdev_res = dev_common.clone_dev_state_apfs(
            src_dir=getattr(args, "src_dir", "") or None,
            dst_dir=getattr(args, "dst_dir", "") or None,
            force_refresh=bool(getattr(args, "refresh", False)),
        )
        print(json.dumps(cdev_res, indent=2, ensure_ascii=False))
        return 0 if cdev_res.get("status") == "ok" else 1

    if args.subcommand == "reload-dev-server":
        rdev_res = _facade_attr("reload_dev_api_server", reload_dev_api_server)(
            dev_repos_dir=getattr(args, "dev_repos_dir", "") or None,
            dev_port=getattr(args, "port", None),
            refresh_state=bool(getattr(args, "refresh_state", False)),
        )
        print(json.dumps(rdev_res, indent=2, ensure_ascii=False))
        return 0 if rdev_res.get("status") in ("ok", "skipped") else 1

    return None
