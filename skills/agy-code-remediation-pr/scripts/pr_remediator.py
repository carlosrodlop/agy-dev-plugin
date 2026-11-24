#!/usr/bin/env python3
"""VCS-Interoperable Native Git Code Change, Pending-Branch Lock & Dual-Agent Evaluation CLI Facade (`pr_remediator.py`).

Thin CLI entrypoint and backward-compatible re-export facade delegating domain logic to:
  - `lock_store.py`: `dev_pr_locks` multi-slot CRUD, heartbeats, worktree cleanup, and git lock sync
  - `queue_store.py`: `dev_queue.json` CRUD, dependency-aware priority ranking, and git reconciliation
  - `worktree_ops.py`: isolated worktrees, `start-remediation`, and `test-local` CI-parity validation
  - `review_gate.py`: `prepare-review`, `branch-diff`, `record-review`, `evaluate-branch`, overlap & rebase gates
  - `merge_watcher.py`: `watch-pr-merge`, `wait-for-unlock`, and safe post-merge conversation cleanup
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path

for _cand_clients in (
    _Path(__file__).resolve().parents[3] / "clients",
    _Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin" / "clients",
):
    if _cand_clients.is_dir():
        _clients_str = str(_cand_clients)
        if _clients_str not in _sys.path:
            _sys.path.insert(0, _clients_str)
        break
import dev_common as _dev_common  # noqa: F401, E402
if __name__ != "__main__":
    _current_mod = _sys.modules.get(__name__)
    if _current_mod is not None:
        _sys.modules.setdefault("pr_remediator", _current_mod)
        _sys.modules["pr_remediator"] = _current_mod

import argparse  # noqa: E402
from collections.abc import Sequence  # noqa: F401, E402
import contextlib  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import shutil  # noqa: F401, E402
import signal  # noqa: E402
import subprocess  # noqa: F401, E402
import sys  # noqa: E402

import dev_common  # noqa: F401, E402
import git_client as _git_client  # noqa: F401, E402
from git_client import GitVcsIntegrationClient, stamp_repo_version_manifests  # noqa: F401, E402

_SCRIPT_DIR_STR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR_STR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR_STR)

import dev_flow_optimizer as _flow_opt  # noqa: F401, E402
import dev_queue_planner as _queue_planner  # noqa: F401, E402
import queue_compactor as _queue_compactor  # noqa: F401, E402
from queue_compactor import (  # noqa: F401, E402
    DEFAULT_MAX_COMBINED_FILES,
    DEFAULT_MAX_COMBINED_FIX_CHARS,
    GLOBAL_HUB_FILES,
    build_compaction_optimizer_finding,
    compact_dev_queue,
    detect_pr_compaction_candidates,
    infer_repo_path_owners,
    is_compaction_excluded,
    is_hub_file,
    partition_item_files_by_repo,
    render_compaction_report,
    validate_queue_invariants,
)
from dev_queue_planner import (  # noqa: F401, E402
    EXECUTION_WAVES,
    IMPORTANCE_RANK,
    IMPORTANCE_TIERS,
    WAVE_RANK,
    build_component_planning_snapshot,
    classify_execution_wave,
    infer_importance,
    infer_subcomponent,
    normalize_component_name,
    normalize_importance_tier,
)
from dev_flow_optimizer import (  # noqa: F401, E402
    DEFAULT_STALL_THRESHOLD_SECONDS,
    analyze_dev_flow_optimizations,
    build_branch_diff_verification_gate,
    detect_stalled_lock,
    format_compact_opt_hint,
    normalize_pre_commit_status,
)
import dev_queue_doctor as _queue_doctor  # noqa: F401, E402
from dev_queue_doctor import (  # noqa: F401, E402
    DEFAULT_WAKE_COOLDOWN_SECONDS,
    apply_safe_fixes,
    diagnose_orchestrator,
    render_doctor_report,
    run_queue_doctor,
    trigger_orchestrator_wake,
)
import parallel_scope as _parallel_scope  # noqa: F401, E402
from parallel_scope import (  # noqa: F401, E402
    OVERLAP_DEEP_SHARED,
    OVERLAP_INTERFACE_SHARED,
    OVERLAP_NONE,
    OVERLAP_TRANSITIVE,
    PARALLEL_SAFE_TIERS,
    TaskAnchorSpec,
    analyze_candidates_overlap,
    analyze_pair_overlap,
    collect_git_changed_files,
    compute_landing_order,
    normalize_target_files_for_repo,
)
import rebase_gate as _rebase_gate  # noqa: F401, E402
from rebase_gate import (  # noqa: F401, E402
    OUTCOME_AUTO_PROMOTED,
    OUTCOME_NEEDS_CONFLICT_RESOLUTION,
    OUTCOME_NEEDS_SEMANTIC_FIX,
    compute_cross_impact_files,
    detect_base_sha_drift,
    probe_merge_tree_conflicts,
    rebase_and_reverify,
)
from lock_store import (  # noqa: F401, E402
    CLIENTS_DIR,
    DEFAULT_MAX_REPO_SLOTS,
    FRONTEND_PATH_MARKERS,
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
    _HEX8_CID_RE,
    _REPO_CLIENTS_DIR,
    _SLOT_SCOPED_RB_KEY_RE,
    _SLOT_SYNC_FIELDS,
    _branch_found_in_base_git_log,
    _build_slot_dict_from_mapping,
    _build_slot_lock_id,
    _entry_matches_branch,
    _extract_repo_slug_from_lock_dict,
    _facade_attr,
    _find_git_root_from_path,
    _inactive_lock_snapshot,
    _infer_repo_slug_from_missing_path,
    _inspect_local_branch_merged_readonly,
    _is_same_conversation_id,
    _is_slot_scoped_rb_key,
    _lookup_slot_repo_branch_entry,
    _match_lock_entry,
    _match_slot_key,
    _path_matches_slot_worktree,
    _project_slot_view,
    _read_locks_state,
    _register_facade_module,
    _resolve_lock_target_repo_dir,
    _resolve_main_git_repo_root,
    _resolve_max_repo_slots,
    _resolve_repo_base_sha,
    _resolve_repo_dir_for_slug,
    _resolve_repo_slug_and_dir,
    _sanitize_home_paths,
    _slot_repo_branch_key,
    _slugify_branch_token,
    _sync_lock_slots_structure,
    _sync_single_repo_lock_with_git,
    _sync_unlocked_repo_with_git,
    _utc_now_iso,
    _write_locks_state,
    acquire_dev_pr_lock,
    build_pro_dashboard_ui_url,
    cleanup_worktree,
    dispatch_lock_cli,
    format_remediation_commit_title,
    format_timestamped_branch_name,
    generate_branch_timestamp,
    get_all_dev_pr_locks,
    get_dev_pr_lock,
    is_dev_pr_locked,
    is_frontend_change_detected,
    record_dev_pr_lock_heartbeat,
    register_lock_cli,
    release_dev_pr_lock,
    restart_pro_api_server,
    stop_dev_api_server,
    sync_dev_pr_lock_with_git,
    update_dev_pr_lock,
)
from queue_store import (  # noqa: F401, E402
    DEFAULT_MAX_PARTIAL_WIP,
    DEV_QUEUE_DONE_STATUSES,
    DEV_QUEUE_FILENAME,
    DEV_QUEUE_HOLD_STATUSES,
    DEV_QUEUE_OPEN_STATUSES,
    DEV_QUEUE_STATUS_RANK,
    DEV_QUEUE_STATUSES,
    INFRA_PRIORITY_PRECEDENCE_MAX,
    _CONVERSATION_UUID_RE,
    _CREATED_PR_URL_RE,
    _MERGED_LOCK_RELEASE_REASONS,
    _STALE_PR_COMPARE_MARKERS,
    _atomic_write_json_0600,
    _auto_enqueue_blocked_task,
    _collect_queue_item_repo_branches,
    _dedupe_richest_pr_urls,
    _empty_dev_queue,
    _extract_created_pr_repo_and_number,
    _extract_repo_slug_and_branch_from_pr_url,
    _find_queue_item_index,
    _mark_queue_item_repo_merged,
    _matches_cid,
    _normalize_str_list,
    _prune_merged_repo_pr_urls,
    _raw_queue_priority,
    _read_dev_queue_locked,
    _resolve_max_partial_wip,
    _resolve_prioritize_partial_wip,
    _score_pr_url_richness,
    _sort_dev_queue_items,
    _sync_active_lock_to_dev_queue,
    _verify_queue_repo_merged_readonly,
    compute_effective_priorities,
    dispatch_queue_cli,
    enqueue_dev_queue_item,
    get_dev_queue_path,
    get_dev_queue_summary,
    get_next_dev_queue_item,
    load_dev_queue,
    reconcile_dev_queue_with_git,
    register_queue_cli,
    set_dev_queue_orchestrator,
    update_dev_queue_item_status,
)
from worktree_ops import (  # noqa: F401, E402
    _GIT_HOOK_ENV_VARS,
    _UUID_FULL_RE,
    _build_single_pass_pre_commit_cmd,
    _build_single_pass_pre_commit_skip,
    _clean_git_subprocess_env,
    _detect_validation_changed_files,
    _find_existing_repo_branch_for_conversation,
    _format_threshold_number,
    _format_verification_checks_markdown,
    _inspect_repo_ci_parity_config,
    _is_coverage_module_available,
    _parse_bats_summary,
    _parse_coverage_report_pct,
    _parse_qa_static_audit_summary,
    _parse_unittest_summary,
    _qa_script_supports_strict_flag,
    _resolve_selective_should_run_bats,
    _resolve_valid_verification_results,
    _snapshot_worktree_state,
    _temporarily_index_untracked_files,
    build_remediation_proposal_markdown,
    dispatch_worktree_cli,
    prepare_isolated_worktree,
    register_worktree_cli,
    run_local_validation_suite,
    start_remediation_branch_and_lock,
)
from merge_watcher import (  # noqa: F401, E402
    DEFAULT_ACTION_CLEANUP_DELIVERABLE_HOURS,
    DEFAULT_ACTION_CLEANUP_NOOP_HOURS,
    DEFAULT_ACTION_CLEANUP_UNAUDITED_CAP_HOURS,
    DEFAULT_GHOST_PURGE_ARM_TTL_SECONDS,
    DEFAULT_NOOP_ACTION_RETENTION_DAYS,
    DEFAULT_SIDECAR_EVENT_RETENTION_HOURS,
    GHOST_PURGE_ARMED_FILENAME,
    GHOST_PURGE_ARM_TTL_ENV,
    GHOST_PURGE_BLOCKING_LOCK_STATUSES,
    GHOST_PURGE_RESULT_FILENAME,
    STALE_STEP_RUNNING_IDLE_SECONDS,
    _QUEUE_REF_TOKEN_RE,
    _SUPERSEDED_NOTE_RE,
    _SYNTHETIC_PREFIX8_RE,
    _build_antigravity_root_spec,
    _build_reload_exec_argv,
    _check_sqlite_wal_or_lock_active,
    _collect_conversation_disk_artifacts,
    _collect_disk_present_cids,
    _collect_repo_branch_merge_index,
    _collect_watched_script_paths,
    _default_quit_antigravity,
    _detect_active_antigravity_processes,
    _dir_tree_bytes,
    _discover_non_queue_merged_conversations,
    _discover_noop_action_conversations,
    _discover_orchestrator_roots_and_subagents,
    _discover_scheduled_action_conversations,
    _empty_cleanup_breakdown,
    _extract_invoke_subagent_cids_from_step,
    _extract_prefix8_from_synthetic_or_uuid,
    _filter_desktop_antigravity_procs,
    _find_protection_reasons,
    _find_unmerged_local_branches_for_queue_item,
    _is_desktop_antigravity_proc_desc,
    _is_superseded_cancelled_queue_item,
    _load_conversation_summaries_graph,
    _matches_companion_artifact_name,
    _maybe_auto_reload_process,
    _open_sqlite_readonly,
    _parse_iso_epoch_seconds,
    _prune_app_storage_ghosts,
    _prune_sidecar_events,
    _resolve_antigravity_dirs,
    _resolve_antigravity_roots,
    _resolve_cleanup_hours,
    _resolve_uuid_candidates_for_queue_id,
    _snapshot_file_fingerprints,
    _sweep_orphan_companion_artifacts,
    _validate_python_syntax,
    _write_ghost_purge_result,
    ack_ghost_purge_result,
    arm_ghost_purge,
    cancel_ghost_purge,
    check_ghost_purge_restart_guard,
    cleanup_merged_conversations,
    collect_protected_conversation_ids,
    count_ghost_sidebar_entries,
    dispatch_watcher_cli,
    extract_spawned_subagent_ids,
    get_ghost_purge_status,
    is_conversation_fully_idle,
    prune_ghost_conversation_summaries,
    register_watcher_cli,
    restart_and_purge,
    run_ghost_purge_worker,
    run_pr_merge_watcher_loop,
    spawn_pr_merge_watcher_daemon,
    verify_branches_merged_into_base,
    wait_for_unlock_or_actionable_event,
)
import orchestration_control as _orch_ctrl  # noqa: F401, E402
from orchestration_control import (  # noqa: F401, E402
    DEFAULT_PAUSED_POLL_INTERVAL_SECONDS,
    NON_ONGOING_LOCK_STATUSES,
    ORCHESTRATION_CONTROL_FILENAME,
    ORCHESTRATION_CONTROL_SCHEMA_VERSION,
    PAUSE_MODE_GRACEFUL_DRAIN,
    count_ongoing_lock_slots,
    default_orchestration_control,
    dispatch_control_cli,
    get_orchestration_control_path,
    has_unhandled_resume_request,
    is_orchestration_paused,
    load_orchestration_control,
    mark_resume_handled,
    pause_orchestration,
    register_control_cli,
    resume_orchestration,
    stop_chrome_devtools_mcp_processes,
    summarize_orchestration_phase,
)
from review_gate import (  # noqa: F401, E402
    _PREVIEW_TOP_LEVEL_ASSETS,
    _PREVIEW_WEB_SUBDIRS,
    _build_developer_remediation_plan,
    _format_pr_notification_subject_summary,
    _resolve_worktree_for_branch,
    _stage_preview_plugin_bundle,
    _sync_branch_head_commit_message,
    _sync_branch_head_commit_message_detailed,
    _sync_preview_subtree,
    analyze_queue_or_tasks_overlap,
    build_canonical_pr_description,
    build_code_developer_subagent_plan,
    build_pr_reviewer_subagent_plan,
    dispatch_review_cli,
    evaluate_branch_and_release_lock,
    get_branch_diff_with_verification_gate,
    push_remediation_branch_and_build_reviewer_plan,
    record_pr_review_evaluation,
    register_review_cli,
    run_slot_rebase_reverify,
)

if __name__ != "__main__":
    _register_facade_module(_sys.modules.get(__name__))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for `pr_remediator.py`."""
    parser = argparse.ArgumentParser(
        prog="pr_remediator",
        description="VCS-Interoperable Native Git Timestamped Branch Remediation, Lock & Review Orchestrator.",
    )
    sub = parser.add_subparsers(dest="subcommand")
    register_lock_cli(sub)
    register_review_cli(sub)
    register_worktree_cli(sub)
    register_watcher_cli(sub)
    register_queue_cli(sub)
    register_control_cli(sub)
    return parser.parse_args(argv)


def _silence_broken_pipe_stdout() -> int:
    """Redirect stdout fd to `/dev/null` on `BrokenPipeError` so pipe consumers (`head`, `jq`) never trigger tracebacks."""
    with contextlib.suppress(OSError, ValueError):
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        finally:
            os.close(devnull_fd)
    return 0


def _install_sigpipe_hygiene() -> None:
    """Ensure `SIGPIPE` raises `BrokenPipeError` cleanly (never crashing with unhandled signal tracebacks)."""
    sigpipe = getattr(signal, "SIGPIPE", None)
    if sigpipe is not None:
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sigpipe, signal.SIG_IGN)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `pr_remediator.py` with `SIGPIPE`/`BrokenPipeError` hygiene."""
    _install_sigpipe_hygiene()
    try:
        return _main_impl(argv)
    except BrokenPipeError:
        return _silence_broken_pipe_stdout()


def _main_impl(argv: list[str] | None = None) -> int:
    """Internal CLI dispatcher for `pr_remediator.py`."""
    args = parse_args(argv)
    for rc in (
        dispatch_lock_cli(args),
        dispatch_worktree_cli(args),
        dispatch_review_cli(args),
        dispatch_watcher_cli(args, argv),
        dispatch_queue_cli(args),
        dispatch_control_cli(args),
    ):
        if rc is not None:
            return int(rc)
    parse_args(["--help"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
