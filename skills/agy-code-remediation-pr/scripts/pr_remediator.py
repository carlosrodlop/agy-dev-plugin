#!/usr/bin/env python3
"""VCS-Interoperable Native Git Code Change, Pending-Branch Lock & Dual-Agent Evaluation Service (`pr_remediator.py`).

Implements the Universal VCS-Interoperable (GitHub, GitLab, Bitbucket, Gitea) Native `git` Timestamped Branch
Workflow for code, script, configuration, and `SKILL.md` changes—triggered interactively via user prompts
or escalated from companion incident audits:
  1. Optionally inspect execution records (`try_inspect_antigravity_conversation`) or capture the user prompt scope.
  2. Enforce Exclusive Pending-Branch Lock (`dev_pr_lock` in `actions_state.json`): if any timestamped branch is
     pending manual validation to create or reject a Pull Request, block all new code changes until evaluated.
  3. Create a Timestamped Branch (`fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>`) from `origin/main` using the exact
     `branch_timestamp` also embedded in branch review notifications.
  4. Propose and develop all changes and unit tests on the timestamped branch (never on `main`), verify locally
     (`unittest` + `qa_static_audit.py` + `pre-commit`), and push to `origin/<branch>` via native `git push`
     (verifying `files_count > 0` compared to `origin/main` so a branch is never pushed identical to `main`).
  5. Evaluate the pushed branch with `agy-code-pr-reviewer` (`Model="pro"`) using native `git diff origin/main...<branch>`
     and dispatch review notifications containing the matching `branch_timestamp`, branch name,
     reviewer verdict, and manual Pull/Merge Request compare link (`.../compare/main...<branch>?expand=1`).
  6. Manual Validation & Lock Release: the user compares the branch against `main` and manually creates/merges the
     Pull Request (or rejects the branch), releasing `dev_pr_lock` (`evaluate-branch` or automatic native `git` sync).
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
    _sys.modules.setdefault("pr_remediator", _sys.modules[__name__])
    _sys.modules["pr_remediator"] = _sys.modules[__name__]


import argparse
from collections.abc import Iterator, Mapping
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import urllib.parse

import dev_common  # noqa: E402
import git_client as _git_client  # noqa: E402
from git_client import GitVcsIntegrationClient, stamp_repo_version_manifests  # noqa: E402

_SCRIPT_DIR_STR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR_STR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR_STR)
import dev_flow_optimizer as _flow_opt  # noqa: E402
from dev_flow_optimizer import (  # noqa: E402
    DEFAULT_STALL_THRESHOLD_SECONDS,
    analyze_dev_flow_optimizations,
    build_branch_diff_verification_gate,
    detect_stalled_lock,
    format_compact_opt_hint,
    normalize_pre_commit_status,
)
import dev_queue_doctor as _queue_doctor  # noqa: E402
from dev_queue_doctor import (  # noqa: E402
    DEFAULT_WAKE_COOLDOWN_SECONDS,
    apply_safe_fixes,
    diagnose_orchestrator,
    render_doctor_report,
    run_queue_doctor,
    trigger_orchestrator_wake,
)
import parallel_scope as _parallel_scope  # noqa: E402
from parallel_scope import (  # noqa: E402
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
import rebase_gate as _rebase_gate  # noqa: E402
from rebase_gate import (  # noqa: E402
    OUTCOME_AUTO_PROMOTED,
    OUTCOME_NEEDS_CONFLICT_RESOLUTION,
    OUTCOME_NEEDS_SEMANTIC_FIX,
    compute_cross_impact_files,
    detect_base_sha_drift,
    probe_merge_tree_conflicts,
    rebase_and_reverify,
)

build_pro_dashboard_ui_url = dev_common.try_build_control_tower_ui_url
restart_pro_api_server = dev_common.try_restart_control_tower_api
stop_dev_api_server = dev_common.try_stop_dev_api_server

_REPO_CLIENTS_DIR = Path(__file__).resolve().parents[3] / "clients"
CLIENTS_DIR = _REPO_CLIENTS_DIR if _REPO_CLIENTS_DIR.is_dir() else (Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin" / "clients")
if str(CLIENTS_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTS_DIR))

PR_DEVELOPER_SUBAGENT_NAME = "agy-code-developer"
PR_REVIEWER_SUBAGENT_NAME = "agy-code-pr-reviewer"
PR_SUBAGENT_MODEL = "pro"
PR_REVIEW_PASS_MIN_SCORE = 91
MAX_PR_REVIEW_ITERATIONS = 3
DEFAULT_MAX_REPO_SLOTS = 2
SLOT_STATUS_READY_AWAITING_BASE_MERGE = "READY_AWAITING_BASE_MERGE"
SLOT_STATUS_NEEDS_REBASE_REVERIFY = "NEEDS_REBASE_REVERIFY"
SLOT_STATUS_NEEDS_AGENT_SEMANTIC_FIX = "NEEDS_AGENT_SEMANTIC_FIX"
SLOT_STATUS_NEEDS_AGENT_CONFLICT_RESOLUTION = "NEEDS_AGENT_CONFLICT_RESOLUTION"

FRONTEND_PATH_MARKERS = (
    "api/ui.py",
    "api/routers/",
    "api/app.py",
    "api/deps.py",
    "api/html_templates.py",
    "api/oauth.py",
    "src/web/",
    "web/",
)


def is_frontend_change_detected(files_changed: list[dict[str, Any] | str] | None) -> bool:
    """Return True if any changed file in the PR touches Control Center frontend or UI routes."""
    if not files_changed:
        return False
    for item in files_changed:
        if isinstance(item, dict):
            path_str = str(item.get("path") or item.get("file") or item.get("filename") or "").strip()
        else:
            path_str = str(item or "").strip()
        norm_path = path_str.replace("\\", "/")
        if any(marker in norm_path for marker in FRONTEND_PATH_MARKERS):
            return True
    return False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def generate_branch_timestamp(now: datetime | None = None) -> str:
    """Generate the canonical `YYYYMMDD-HHMMSS` timestamp shared identically by the Git branch name and notification email."""
    dt_val = now or datetime.now(timezone.utc)
    return dt_val.strftime("%Y%m%d-%H%M%S")


def _slugify_branch_token(text: str, max_len: int = 24) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return cleaned[:max_len].strip("-") or "remediation"


def _sanitize_home_paths(text: str) -> str:
    """Replace expanded local user home paths (`/Users/<user>` or `/home/<user>`) with `$HOME` to prevent PII path leaks in commit messages or lock state."""
    if not text:
        return ""
    cleaned = str(text)
    try:
        home_str = str(Path.home().resolve())
        if home_str and home_str != "/":
            cleaned = cleaned.replace(home_str, "$HOME")
    except OSError:
        pass
    cleaned = re.sub(r"/(?:Users|home)/(?!testuser\b|test\b|Shared\b)[A-Za-z0-9._-]+", "$HOME", cleaned)
    return cleaned


def format_remediation_commit_title(
    title: str,
    action_id: str = "",
    conversation_id: str = "",
    branch_timestamp: str = "",
) -> str:
    """Normalize a commit title to semantic `fix(<scope>): <summary>` format."""
    raw_title = (title or "").strip()
    scope_fallback = _slugify_branch_token(action_id or "pr-remediator", max_len=24)
    if not raw_title:
        cid8 = (conversation_id or "")[:8] or "update"
        ts_sfx = f" [{branch_timestamp}]" if branch_timestamp else ""
        return f"fix({scope_fallback}): update {cid8}{ts_sfx}"

    m_sem = re.match(r"^([a-z]+)(?:\(([^)]+)\))?:\s*(.+)$", raw_title, flags=re.IGNORECASE)
    if m_sem:
        commit_type = m_sem.group(1).lower()
        existing_scope = (m_sem.group(2) or "").strip()
        subject_rest = m_sem.group(3).strip()
        existing_scope = re.sub(r"^(?:auto-remediation|hitl)(?:/|$)", "", existing_scope, flags=re.IGNORECASE).strip("/")
        if existing_scope:
            return f"{commit_type}({existing_scope}): {subject_rest}"
        return f"{commit_type}({scope_fallback}): {subject_rest}"
    return f"fix({scope_fallback}): {raw_title}"


def format_timestamped_branch_name(
    branch_timestamp: str,
    action_id: str = "",
    conversation_id: str = "",
) -> str:
    """Build a deterministic timestamped branch name `fix/<branch_timestamp>-<action_slug>-<cid8>`."""
    ts_clean = re.sub(r"[^0-9T_-]+", "-", (branch_timestamp or "").strip()).strip("-") or generate_branch_timestamp()
    cid8 = re.sub(r"[^a-zA-Z0-9]+", "", (conversation_id or "").strip())[:8]
    act_slug = _slugify_branch_token(action_id) if action_id.strip() else ""
    parts = [p for p in (ts_clean, act_slug, cid8) if p]
    return f"fix/{'-'.join(parts)}"


# ==============================================================================
# Per-Repository Cross-Conversation Pending-Branch Lock State Machine
# ==============================================================================


def _resolve_main_git_repo_root(repo_or_wt: Path) -> Path:
    """Resolve the persistent primary repository root from either a checkout or an isolated git worktree."""
    cand = repo_or_wt.expanduser().resolve()
    dot_git = cand / ".git"
    if dot_git.is_dir():
        return cand
    if dot_git.is_file():
        proc = subprocess.run(
            ["git", "-C", str(cand), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            common_raw = Path((proc.stdout or "").strip())
            common_abs = common_raw if common_raw.is_absolute() else (cand / common_raw).resolve()
            if common_abs.name == ".git" and (common_abs.parent / ".git").exists():
                return common_abs.parent.resolve()
    return cand


def _find_git_root_from_path(candidate: Path) -> Path | None:
    """Walk `candidate` and its ancestors for an existing `.git` root without falling back to the default repo."""
    try:
        cand = candidate.expanduser()
    except OSError:
        return None
    if not cand.exists():
        return None
    try:
        resolved = cand.resolve()
    except OSError:
        resolved = cand
    if (resolved / ".git").exists():
        return resolved
    for parent in resolved.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _infer_repo_slug_from_missing_path(
    raw_path: str,
    *,
    locks_map: dict[str, dict[str, Any]] | None = None,
    portfolio_dir: Path | str | None = None,
) -> str:
    """Infer `owner/repo` for a missing/cleaned worktree path strictly from known locks, or return empty string."""
    clean_raw = str(raw_path or "").strip()
    if not clean_raw:
        return ""
    home_str = str(Path.home())
    cand_expanded = str(Path(clean_raw.replace("$HOME", home_str)).expanduser())
    cand_sanitized = _sanitize_home_paths(cand_expanded)
    cand_name = Path(cand_expanded).name

    known_locks = (
        locks_map
        if locks_map is not None
        else get_all_dev_pr_locks(portfolio_dir=portfolio_dir, include_inactive=True)
    )
    if not known_locks:
        return ""

    def _matches_path(recorded_raw: str) -> bool:
        rec = str(recorded_raw or "").strip()
        if not rec:
            return False
        rec_exp = str(Path(rec.replace("$HOME", home_str)).expanduser())
        rec_san = _sanitize_home_paths(rec_exp)
        if rec_exp == cand_expanded or rec_san == cand_sanitized:
            return True
        if cand_name.startswith("pr-worktree-") and Path(rec_exp).name == cand_name:
            return True
        return False

    for slug, entry in known_locks.items():
        if not isinstance(entry, dict):
            continue
        for key in ("worktree_path", "target_repo_dir"):
            if _matches_path(str(entry.get(key) or "")):
                return dev_common.normalize_repo_slug(slug) or slug
        slots_obj = entry.get("slots")
        if isinstance(slots_obj, dict):
            for slot_item in slots_obj.values():
                if isinstance(slot_item, dict):
                    for key in ("worktree_path", "target_repo_dir"):
                        if _matches_path(str(slot_item.get(key) or "")):
                            return dev_common.normalize_repo_slug(slug) or slug
        for rb_key in ("repo_branches", "last_repo_branches"):
            rb_map = entry.get(rb_key)
            if not isinstance(rb_map, dict):
                continue
            for rb_slug, rb_item in rb_map.items():
                if not isinstance(rb_item, dict):
                    continue
                for key in ("worktree_path", "target_repo_dir"):
                    if _matches_path(str(rb_item.get(key) or "")):
                        clean_rb = str(rb_slug).split("#", 1)[0]
                        return dev_common.normalize_repo_slug(clean_rb) or dev_common.normalize_repo_slug(slug) or slug

    m_wt = re.match(r"^pr-worktree-([a-z0-9-]+)-fix-", cand_name.lower())
    if m_wt:
        wt_repo_token = m_wt.group(1)
        matched_slugs = [
            slug
            for slug in known_locks
            if _slugify_branch_token(slug.rsplit("/", 1)[-1], max_len=24) == wt_repo_token
        ]
        if len(matched_slugs) == 1:
            return dev_common.normalize_repo_slug(matched_slugs[0]) or matched_slugs[0]
    return ""


def _resolve_repo_slug_and_dir(
    *,
    repo_slug: str = "",
    target_repo_dir: Path | str | None = None,
    workdir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    prefer_cwd: bool = False,
    portfolio_dir: Path | str | None = None,
    locks_map: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, Path | None]:
    """Resolve normalized `owner/repo` slug and repository directory from explicit overrides or git checkout.

    Never silently falls back to the default repository when an explicit `workdir`/`target_repo_dir`
    no longer exists on disk or is outside any Git checkout.
    """
    vcs = git_client or GitVcsIntegrationClient()
    explicit_dir = workdir if workdir else target_repo_dir
    resolved_dir: Path | None = None
    explicit_dir_provided = False
    raw_explicit = ""
    if explicit_dir:
        raw_explicit = str(explicit_dir).replace("$HOME", str(Path.home())).strip()
        if raw_explicit:
            explicit_dir_provided = True
            resolved_dir = _find_git_root_from_path(Path(raw_explicit))
    elif prefer_cwd or not str(repo_slug or "").strip():
        resolved_dir = (
            vcs.resolve_active_repo_dir(prefer_cwd=prefer_cwd)
            if hasattr(vcs, "resolve_active_repo_dir")
            else vcs.resolve_repo_dir()
        )

    clean_slug = dev_common.normalize_repo_slug(repo_slug)
    if not clean_slug and resolved_dir is not None:
        if hasattr(vcs, "resolve_repo"):
            try:
                clean_slug = dev_common.normalize_repo_slug(str(vcs.resolve_repo(repo_dir=resolved_dir) or ""))
            except TypeError:
                clean_slug = dev_common.normalize_repo_slug(str(vcs.resolve_repo() or ""))
        if not clean_slug:
            clean_slug = dev_common.resolve_repo_slug_from_path(resolved_dir)
    if not clean_slug and resolved_dir is not None:
        clean_slug = resolved_dir.name.lower()
    if not clean_slug and explicit_dir_provided and resolved_dir is None:
        clean_slug = _infer_repo_slug_from_missing_path(
            raw_explicit,
            locks_map=locks_map,
            portfolio_dir=portfolio_dir,
        )
        return clean_slug, None
    return clean_slug or ("" if explicit_dir_provided else "default"), resolved_dir


def _extract_repo_slug_from_lock_dict(lock_obj: dict[str, Any] | None, fallback_key: str = "") -> str:
    """Resolve normalized `owner/repo` slug stored on a lock object or fallback key."""
    if isinstance(lock_obj, dict):
        for key in ("repo_slug", "repo"):
            slug = dev_common.normalize_repo_slug(str(lock_obj.get(key) or ""))
            if slug:
                return slug
        for path_key in ("target_repo_dir", "worktree_path"):
            raw_p = str(lock_obj.get(path_key) or "").strip()
            if raw_p:
                resolved = dev_common.resolve_repo_slug_from_path(raw_p.replace("$HOME", str(Path.home())))
                if resolved:
                    return resolved
    return dev_common.normalize_repo_slug(fallback_key) or "default"


def _inactive_lock_snapshot(lock_obj: dict[str, Any] | None = None, repo_slug: str = "") -> dict[str, Any]:
    """Build a canonical inactive lock snapshot preserving `last_*` metadata."""
    obj = lock_obj if isinstance(lock_obj, dict) else {}
    return {
        "active": False,
        "repo_slug": repo_slug or str(obj.get("repo_slug") or obj.get("repo") or ""),
        "last_released_at": str(obj.get("last_released_at") or ""),
        "last_release_reason": str(obj.get("last_release_reason") or ""),
        "last_conversation_id": str(obj.get("last_conversation_id") or obj.get("conversation_id") or ""),
        "last_branch_name": str(obj.get("last_branch_name") or obj.get("branch_name") or ""),
        "last_branch_timestamp": str(obj.get("last_branch_timestamp") or obj.get("branch_timestamp") or ""),
    }


def _read_locks_state(audit_state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Load `dev_pr_locks` map from `actions_log_audit_state`, migrating active legacy `dev_pr_lock` cleanly."""
    locks_map: dict[str, dict[str, Any]] = {}
    raw_locks = audit_state.get("dev_pr_locks")
    if isinstance(raw_locks, dict):
        for k, v in raw_locks.items():
            if not isinstance(v, dict):
                continue
            slug = _extract_repo_slug_from_lock_dict(v, fallback_key=str(k))
            entry = dict(v)
            entry.setdefault("repo_slug", slug)
            entry.setdefault("repo", slug)
            locks_map[slug] = entry

    legacy = audit_state.get("dev_pr_lock")
    if isinstance(legacy, dict) and bool(legacy.get("active")):
        leg_slug = _extract_repo_slug_from_lock_dict(legacy)
        existing_entry = locks_map.get(leg_slug)
        if not isinstance(existing_entry, dict) or not bool(existing_entry.get("active")):
            migrated = dict(legacy)
            migrated["repo_slug"] = leg_slug
            migrated["repo"] = leg_slug
            locks_map[leg_slug] = migrated
    return locks_map


def _write_locks_state(
    audit_state: dict[str, Any],
    locks_map: dict[str, dict[str, Any]],
    *,
    touched_repo_slug: str = "",
    released_snapshot: dict[str, Any] | None = None,
) -> None:
    """Persist `dev_pr_locks` while maintaining `dev_pr_lock` parity and preserving foreign legacy locks untouched."""
    audit_state["dev_pr_locks"] = locks_map
    legacy = audit_state.get("dev_pr_lock")
    foreign_legacy_active = False
    if isinstance(legacy, dict) and bool(legacy.get("active")):
        legacy_slug = _extract_repo_slug_from_lock_dict(legacy)
        if touched_repo_slug and legacy_slug != touched_repo_slug:
            foreign_legacy_active = True

    if foreign_legacy_active:
        # Preserve an in-flight foreign legacy lock (e.g., sibling conversation using pre-migration plugin).
        return

    touched_lock = locks_map.get(touched_repo_slug) if touched_repo_slug else None
    if isinstance(touched_lock, dict) and bool(touched_lock.get("active")):
        audit_state["dev_pr_lock"] = dict(touched_lock)
        return

    active_candidates = [
        v for v in locks_map.values() if isinstance(v, dict) and bool(v.get("active"))
    ]
    if active_candidates:
        active_candidates.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        audit_state["dev_pr_lock"] = dict(active_candidates[0])
        return

    snap = released_snapshot if isinstance(released_snapshot, dict) else {}
    audit_state["dev_pr_lock"] = snap or _inactive_lock_snapshot(legacy if isinstance(legacy, dict) else None)


def _entry_matches_branch(entry: dict[str, Any], clean_branch: str, *, include_inactive: bool = False) -> bool:
    """Return True if a lock entry references `clean_branch` in active slots or (optionally) released metadata."""
    if not isinstance(entry, dict) or not clean_branch:
        return False
    if str(entry.get("branch_name") or "").strip() == clean_branch:
        return True
    slots_map = entry.get("slots")
    if isinstance(slots_map, dict) and any(
        isinstance(s_v, dict) and str(s_v.get("branch_name") or "").strip() == clean_branch
        for s_v in slots_map.values()
    ):
        return True
    rb = entry.get("repo_branches")
    if isinstance(rb, dict) and any(
        isinstance(rb_v, dict) and str(rb_v.get("branch_name") or "").strip() == clean_branch
        for rb_v in rb.values()
    ):
        return True
    if include_inactive:
        if str(entry.get("last_branch_name") or "").strip() == clean_branch:
            return True
        last_rb = entry.get("last_repo_branches")
        if isinstance(last_rb, dict) and any(
            isinstance(rb_v, dict) and str(rb_v.get("branch_name") or "").strip() == clean_branch
            for rb_v in last_rb.values()
        ):
            return True
    return False


_SLOT_SYNC_FIELDS: tuple[str, ...] = (
    "lock_id",
    "conversation_id",
    "sibling_conversation_ids",
    "action_id",
    "notification_source",
    "branch_timestamp",
    "branch_name",
    "base_sha",
    "overlap_tier",
    "landing_order",
    "section_anchors",
    "branch_pushed",
    "pr_gate_ready",
    "review_approved",
    "approved_head_sha",
    "deferred_send_email",
    "reviewed_head_sha",
    "staged_head_sha",
    "head_sha",
    "manual_pr_url",
    "target_repo_dir",
    "worktree_path",
    "issue_number",
    "issue_url",
    "pr_number",
    "pr_url",
    "status",
    "branch_action",
    "created_by",
    "summary",
    "proposed_fix",
    "executive_summary",
    "proposal_title",
    "proposal_body",
    "target_files",
    "frontend_changed",
    "dev_preview_url",
    "pro_url",
    "review_iteration",
    "review_history",
    "reviewer_verdict",
    "reviewer_score",
    "reviewer_summary",
    "reviewer_subagent_id",
    "subagent_conversation_ids",
    "review_markdown",
    "verification_results",
    "rebase_outcome",
    "last_rebase_result",
    "needs_base_rebase",
    "promoted_from_slot",
    "promoted_at",
    "previous_slot1_branch",
    "previous_slot1_release_reason",
    "acquired_at",
    "worker_cid",
    "stage_label",
    "last_activity_at",
    "updated_at",
)


def _resolve_max_repo_slots(explicit_max_slots: int | None = None) -> int:
    """Resolve `max_slots` per repository (default `2`, overrideable via `AGY_DEV_MAX_SLOTS_PER_REPO`)."""
    if explicit_max_slots is not None:
        with contextlib.suppress(TypeError, ValueError):
            val = int(explicit_max_slots)
            if val >= 1:
                return val
    env_raw = str(os.environ.get("AGY_DEV_MAX_SLOTS_PER_REPO") or "").strip()
    if env_raw:
        with contextlib.suppress(TypeError, ValueError):
            val = int(env_raw)
            if val >= 1:
                return val
    return DEFAULT_MAX_REPO_SLOTS


def _resolve_repo_base_sha(repo_dir: Path | None, base_branch: str = "main") -> str:
    """Resolve the 40-char commit SHA of `origin/<base_branch>` (or `<base_branch>` / `HEAD`) inside `repo_dir`."""
    if repo_dir is None or not (repo_dir / ".git").exists():
        return ""
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_PREFIX",
        )
    }
    for cand_ref in (f"origin/{base_branch}", base_branch, "HEAD"):
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--verify", cand_ref],
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return (proc.stdout or "").strip()
    return ""


def _build_slot_dict_from_mapping(source: Mapping[str, Any], slot_id: int) -> dict[str, Any]:
    """Extract a normalized slot dictionary for `slot_id` (`1` or `2`)."""
    slot_obj: dict[str, Any] = {
        "slot_id": int(slot_id),
        "active": bool(source.get("active", True)),
    }
    for key in _SLOT_SYNC_FIELDS:
        if key in source:
            val = source[key]
            if isinstance(val, list):
                slot_obj[key] = list(val)
            elif isinstance(val, dict):
                slot_obj[key] = dict(val)
            else:
                slot_obj[key] = val
    slot_obj.setdefault("base_sha", "")
    slot_obj.setdefault("overlap_tier", "")
    slot_obj.setdefault("landing_order", [])
    return slot_obj


_SLOT_SCOPED_RB_KEY_RE = re.compile(r"#slot([2-9]\d*)$")


def _is_slot_scoped_rb_key(key: str) -> bool:
    """Return True when a `repo_branches` key targets a secondary parallel slot (`<repo_slug>#slotN`)."""
    return bool(_SLOT_SCOPED_RB_KEY_RE.search(str(key or "").strip()))


def _slot_repo_branch_key(repo_slug: str, slot_id: int | str | None = 1) -> str:
    """Return the canonical `repo_branches` key (`<repo_slug>` for Slot #1, `<repo_slug>#slotN` for `slot_id > 1`)."""
    clean_slug = str(repo_slug or "").strip()
    if not clean_slug:
        return ""
    try:
        s_num = int(slot_id or 1)
    except (TypeError, ValueError):
        s_num = 1
    return f"{clean_slug}#slot{s_num}" if s_num > 1 else clean_slug


def _lookup_slot_repo_branch_entry(
    raw_rb: Mapping[str, Any] | None,
    repo_slug: str,
    slot_id: int | str | None = 1,
) -> dict[str, Any] | None:
    """Resolve `repo_branches` strictly for `(repo_slug, slot_id)` without ever falling back from Slot #2 to Slot #1."""
    if not isinstance(raw_rb, Mapping):
        return None
    rb_key = _slot_repo_branch_key(repo_slug, slot_id)
    if not rb_key:
        return None
    cand = raw_rb.get(rb_key)
    return dict(cand) if isinstance(cand, dict) else None


def _sync_lock_slots_structure(lock_obj: dict[str, Any], *, repo_slug: str = "") -> dict[str, Any]:
    """Normalize `lock_obj` so top-level fields mirror Slot #1 while `slots` preserves multi-slot state."""
    if not isinstance(lock_obj, dict) or not bool(lock_obj.get("active")):
        return dict(lock_obj or {})

    out = dict(lock_obj)
    eff_slug = repo_slug or str(out.get("repo_slug") or out.get("repo") or "").strip()
    max_slots = _resolve_max_repo_slots(out.get("max_slots"))
    raw_slots = out.get("slots") if isinstance(out.get("slots"), dict) else {}

    slot1 = _build_slot_dict_from_mapping(out, 1)
    if isinstance(raw_slots.get("1"), dict):
        # Preserve additive slot-1 keys (`base_sha`, `overlap_tier`, `landing_order`) when top-level omitted them
        for add_k in ("base_sha", "overlap_tier", "landing_order", "section_anchors", "review_approved", "approved_head_sha", "deferred_send_email"):
            if not slot1.get(add_k) and raw_slots["1"].get(add_k):
                slot1[add_k] = raw_slots["1"][add_k]
    slot1["slot_id"] = 1
    slot1["active"] = True

    slots_clean: dict[str, dict[str, Any]] = {"1": slot1}
    raw_slot2 = raw_slots.get("2")
    if isinstance(raw_slot2, dict) and bool(raw_slot2.get("active", True)):
        s2 = _build_slot_dict_from_mapping(raw_slot2, 2)
        s2["slot_id"] = 2
        s2["active"] = True
        slots_clean["2"] = s2

    for k, v in slot1.items():
        if k not in ("slot_id", "active"):
            out[k] = v
    out["active"] = True
    out["slot_id"] = 1
    out["max_slots"] = max_slots
    out["active_slots_count"] = len(slots_clean)
    out["slots"] = slots_clean

    # Keep `repo_branches` synchronized so `dev_common.get_active_dev_pr_lock_protections()` protects Slot #2 worktrees too
    raw_rb = out.get("repo_branches") if isinstance(out.get("repo_branches"), dict) else {}
    clean_rb = {
        str(k): dict(v)
        for k, v in raw_rb.items()
        if str(k).strip() and not _is_slot_scoped_rb_key(str(k)) and isinstance(v, dict)
    }
    if eff_slug and "2" in slots_clean:
        s2_entry = slots_clean["2"]
        s2_rb_key = _slot_repo_branch_key(eff_slug, 2)
        clean_rb[s2_rb_key] = {
            "repo_slug": eff_slug,
            "slot_id": 2,
            "branch_name": str(s2_entry.get("branch_name") or ""),
            "branch_timestamp": str(s2_entry.get("branch_timestamp") or ""),
            "target_repo_dir": str(s2_entry.get("target_repo_dir") or out.get("target_repo_dir") or ""),
            "worktree_path": str(s2_entry.get("worktree_path") or ""),
            "manual_pr_url": "",
            "branch_pushed": False,
            "head_sha": str(s2_entry.get("head_sha") or s2_entry.get("staged_head_sha") or ""),
            "updated_at": str(s2_entry.get("updated_at") or out.get("updated_at") or ""),
        }
    out["repo_branches"] = clean_rb
    return out


def _path_matches_slot_worktree(raw_query: Path | str | None, recorded_wt: str) -> bool:
    """Return True when `raw_query` resolves to, resides under, or shares the isolated worktree basename of `recorded_wt`."""
    if not raw_query or not recorded_wt:
        return False
    home_str = str(Path.home())
    q_exp = str(Path(str(raw_query).replace("$HOME", home_str)).expanduser())
    r_exp = str(Path(str(recorded_wt).replace("$HOME", home_str)).expanduser())
    with contextlib.suppress(OSError):
        q_exp = str(Path(q_exp).resolve())
    with contextlib.suppress(OSError):
        r_exp = str(Path(r_exp).resolve())
    if q_exp == r_exp or _sanitize_home_paths(q_exp) == _sanitize_home_paths(r_exp):
        return True
    r_name = Path(r_exp).name
    q_name = Path(q_exp).name
    if q_name.startswith("pr-worktree-") and q_name == r_name:
        return True
    if r_name.startswith("pr-worktree-") and q_exp.startswith(r_exp.rstrip(os.sep) + os.sep):
        return True
    return False


def _match_slot_key(
    lock_obj: dict[str, Any],
    *,
    branch_name: str = "",
    workdir: Path | str | None = None,
    conversation_id: str = "",
    slot_id: int | str | None = None,
) -> str:
    """Identify whether a selector targets Slot `'1'` or Slot `'2'` inside an active repository lock."""
    if slot_id is not None and str(slot_id).strip() in ("1", "2"):
        return str(slot_id).strip()
    slots_map = lock_obj.get("slots") if isinstance(lock_obj.get("slots"), dict) else {}
    slot2 = slots_map.get("2")
    if not isinstance(slot2, dict) or not bool(slot2.get("active", True)):
        return "1"

    s2_br = str(slot2.get("branch_name") or "").strip()
    s1_br = str(lock_obj.get("branch_name") or "").strip()
    clean_br = (branch_name or "").strip().removeprefix("refs/heads/")
    if clean_br:
        if s2_br and s2_br == clean_br:
            return "2"
        if s1_br and s1_br == clean_br:
            return "1"

    if workdir:
        if _path_matches_slot_worktree(workdir, str(slot2.get("worktree_path") or "")):
            return "2"
        if _path_matches_slot_worktree(workdir, str(lock_obj.get("worktree_path") or "")):
            return "1"
        wd_git_root = _find_git_root_from_path(
            Path(str(workdir).replace("$HOME", str(Path.home()))).expanduser()
        )
        if wd_git_root is not None and (wd_git_root / ".git").exists():
            br_proc = subprocess.run(
                ["git", "-C", str(wd_git_root), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            wd_br = (br_proc.stdout or "").strip() if br_proc.returncode == 0 else ""
            if wd_br and s2_br and wd_br == s2_br:
                return "2"
            if wd_br and s1_br and wd_br == s1_br:
                return "1"

    clean_cid = (conversation_id or "").strip()
    if clean_cid:
        s2_cid = str(slot2.get("conversation_id") or "").strip()
        s2_sibs = [str(s).strip() for s in (slot2.get("sibling_conversation_ids") or [])]
        if _matches_cid(s2_cid, clean_cid) or any(_matches_cid(s, clean_cid) for s in s2_sibs):
            return "2"

    return "1"


def _project_slot_view(lock_obj: dict[str, Any], slot_key: str) -> dict[str, Any]:
    """Return a caller-facing lock dict representing `slot_key` (`'1'` or `'2'`) without bleeding Slot #1 fields into Slot #2."""
    norm = _sync_lock_slots_structure(lock_obj)
    if slot_key != "2" or "2" not in norm.get("slots", {}):
        return norm
    slot2 = dict(norm["slots"]["2"])
    slot1 = dict(norm["slots"]["1"])
    projected: dict[str, Any] = {
        "active": True,
        "vcs_mode": str(norm.get("vcs_mode") or "native_git"),
        "repo_slug": str(norm.get("repo_slug") or norm.get("repo") or ""),
        "repo": str(norm.get("repo") or norm.get("repo_slug") or ""),
        "slot_id": 2,
        "max_slots": int(norm.get("max_slots") or DEFAULT_MAX_REPO_SLOTS),
        "active_slots_count": int(norm.get("active_slots_count") or len(norm.get("slots") or {})),
        "slots": norm.get("slots", {}),
        "repo_branches": norm.get("repo_branches", {}),
        "target_repo_dir": str(slot2.get("target_repo_dir") or norm.get("target_repo_dir") or ""),
    }
    for k, v in slot2.items():
        projected[k] = v
    projected["active"] = True
    projected["slot_id"] = 2
    projected["slot1_lock_id"] = str(slot1.get("lock_id") or "")
    projected["slot1_branch_name"] = str(slot1.get("branch_name") or "")
    projected["slot1_status"] = str(slot1.get("status") or "")
    return projected


def _match_lock_entry(
    locks_map: dict[str, dict[str, Any]],
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    branch_name: str = "",
    prefer_cwd: bool = True,
    portfolio_dir: Path | str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Select the most specific `(repo_slug, lock_dict)` from `locks_map` for a query/operation.

    When any explicit selector (`branch_name`, `repo_slug`, or `workdir`) is supplied, strictly
    scopes lookup and NEVER falls through to `prefer_cwd` or an unrelated repository's active lock.
    """
    clean_branch = branch_name.strip()
    has_explicit_repo_or_dir = bool(repo_slug.strip() or workdir)

    if clean_branch:
        for slug, entry in locks_map.items():
            if isinstance(entry, dict) and bool(entry.get("active")) and _entry_matches_branch(entry, clean_branch):
                return slug, entry
        for slug, entry in locks_map.items():
            if isinstance(entry, dict) and not bool(entry.get("active")) and _entry_matches_branch(
                entry, clean_branch, include_inactive=True
            ):
                return slug, entry

    if has_explicit_repo_or_dir:
        resolved_slug, _ = _resolve_repo_slug_and_dir(
            repo_slug=repo_slug,
            workdir=workdir,
            prefer_cwd=False,
            portfolio_dir=portfolio_dir,
            locks_map=locks_map,
        )
        if resolved_slug and resolved_slug in locks_map:
            cand_entry = locks_map[resolved_slug]
            if (
                clean_branch
                and isinstance(cand_entry, dict)
                and bool(cand_entry.get("active"))
                and not _entry_matches_branch(cand_entry, clean_branch)
            ):
                return resolved_slug, {}
            return resolved_slug, cand_entry
        return resolved_slug, {}

    if clean_branch:
        return "", {}

    if prefer_cwd:
        cwd_slug = dev_common.resolve_repo_slug_from_path(Path.cwd())
        if cwd_slug and cwd_slug in locks_map and bool(locks_map[cwd_slug].get("active")):
            return cwd_slug, locks_map[cwd_slug]

    for slug, entry in locks_map.items():
        if isinstance(entry, dict) and bool(entry.get("active")):
            return slug, entry
    for slug, entry in locks_map.items():
        if isinstance(entry, dict):
            return slug, entry
    return "", {}


def get_all_dev_pr_locks(
    portfolio_dir: Path | str | None = None,
    *,
    include_inactive: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return per-repository locks keyed by `repo_slug` from `actions_state.json`."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_obj = dev_common.atomic_read_json(p_dir / "actions_state.json", default={})
    audit_state = (
        state_obj.get("actions_log_audit_state")
        if isinstance(state_obj.get("actions_log_audit_state"), dict)
        else {}
    )
    locks_map = _read_locks_state(audit_state)
    if include_inactive:
        return {
            k: (_sync_lock_slots_structure(v, repo_slug=k) if bool(v.get("active")) else dict(v))
            for k, v in locks_map.items()
        }
    return {k: _sync_lock_slots_structure(v, repo_slug=k) for k, v in locks_map.items() if bool(v.get("active"))}


def get_dev_pr_lock(
    portfolio_dir: Path | str | None = None,
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    branch_name: str = "",
    conversation_id: str = "",
    slot_id: int | str | None = None,
) -> dict[str, Any]:
    """Read the per-repository remediation branch lock matching `repo_slug`/`workdir`/`branch_name` (or active fallback)."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_obj = dev_common.atomic_read_json(p_dir / "actions_state.json", default={})
    audit_state = (
        state_obj.get("actions_log_audit_state")
        if isinstance(state_obj.get("actions_log_audit_state"), dict)
        else {}
    )
    locks_map = _read_locks_state(audit_state)
    matched_slug, lock_obj = _match_lock_entry(
        locks_map,
        repo_slug=repo_slug,
        workdir=workdir,
        branch_name=branch_name,
        prefer_cwd=True,
        portfolio_dir=portfolio_dir,
    )
    if isinstance(lock_obj, dict) and bool(lock_obj.get("active")):
        norm_lock = _sync_lock_slots_structure(lock_obj, repo_slug=matched_slug)
        slot_key = _match_slot_key(
            norm_lock,
            branch_name=branch_name,
            workdir=workdir,
            conversation_id=conversation_id,
            slot_id=slot_id,
        )
        return _project_slot_view(norm_lock, slot_key)
    has_selector = bool(repo_slug.strip() or workdir or branch_name.strip())
    legacy = audit_state.get("dev_pr_lock") if isinstance(audit_state.get("dev_pr_lock"), dict) else {}
    snap_source = lock_obj if lock_obj else ({} if has_selector else legacy)
    return _inactive_lock_snapshot(snap_source, repo_slug=matched_slug)


def is_dev_pr_locked(
    portfolio_dir: Path | str | None = None,
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
) -> bool:
    """Return True if the scoped repository (or any repository when unscoped) has an active pending PR lock."""
    if repo_slug.strip() or workdir:
        return bool(get_dev_pr_lock(portfolio_dir=portfolio_dir, repo_slug=repo_slug, workdir=workdir).get("active"))
    return bool(get_all_dev_pr_locks(portfolio_dir=portfolio_dir))


def acquire_dev_pr_lock(
    conversation_id: str,
    action_id: str = "",
    issue_number: int | None = None,
    issue_url: str = "",
    branch_name: str = "",
    branch_timestamp: str = "",
    worktree_path: str = "",
    summary: str = "",
    proposed_fix: str = "",
    target_files: list[str] | None = None,
    sibling_ids: list[str] | None = None,
    reuse_active_branch: bool = False,
    scheduled: bool = False,
    portfolio_dir: Path | str | None = None,
    target_repo_dir: str = "",
    repo_slug: str = "",
    workdir: Path | str | None = None,
    worker_cid: str = "",
    *,
    section_anchors: list[str] | None = None,
    base_sha: str = "",
    max_slots: int | None = None,
    allow_slot2: bool = False,
    force_slot2: bool = False,
    overlap_tier: str = "",
    landing_order: list[str] | None = None,
) -> dict[str, Any]:
    """Acquire a 1-slot or 2-slot per-repository development lock (`dev_pr_locks[repo_slug]`).

    Backward-compatibly preserves Slot #1 on top-level lock fields while supporting parallel Slot #2
    acquisition in `slots["2"]` when `max_slots >= 2` and blast-radius overlap check passes (`NONE` or `TRANSITIVE`).
    """
    _ = scheduled
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_path = p_dir / "actions_state.json"
    lock_file = state_path.with_suffix(".json.lock")
    cid_clean = str(conversation_id or "").strip()
    if not cid_clean:
        return {"status": "error", "acquired": False, "error": "conversation_id is required to acquire lock"}

    now_iso = _utc_now_iso()
    resolved_ts = (branch_timestamp or "").strip() or generate_branch_timestamp()
    clean_siblings = [
        s_val
        for s in (sibling_ids or [])
        for s_val in [str(s).strip()]
        if s_val and s_val != cid_clean
    ]
    clean_targets = [str(f).strip() for f in (target_files or []) if str(f).strip()]
    clean_anchors = [str(a).strip() for a in (section_anchors or []) if str(a).strip()]
    default_branch = branch_name.strip() or format_timestamped_branch_name(
        branch_timestamp=resolved_ts,
        action_id=action_id,
        conversation_id=cid_clean,
    )
    resolved_slug, resolved_dir = _resolve_repo_slug_and_dir(
        repo_slug=repo_slug,
        target_repo_dir=target_repo_dir,
        workdir=workdir,
        prefer_cwd=not bool(repo_slug or target_repo_dir or workdir),
    )
    persistent_repo_dir = _resolve_main_git_repo_root(resolved_dir) if resolved_dir is not None else None
    clean_repo_dir = _sanitize_home_paths(str(persistent_repo_dir or target_repo_dir or "").strip())
    resolved_base_sha = (base_sha or "").strip() or _resolve_repo_base_sha(persistent_repo_dir)
    eff_max_slots = _resolve_max_repo_slots(max_slots)

    with dev_common.file_lock(lock_file):
        state_obj = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        if not isinstance(state_obj, dict):
            state_obj = {}
        audit_state = state_obj.get("actions_log_audit_state")
        if not isinstance(audit_state, dict):
            audit_state = {}
        locks_map = _read_locks_state(audit_state)
        existing_lock = locks_map.get(resolved_slug)
        repo_branches: dict[str, dict[str, Any]] = {}

        if isinstance(existing_lock, dict) and bool(existing_lock.get("active")):
            norm_existing = _sync_lock_slots_structure(existing_lock, repo_slug=resolved_slug)
            eff_max_slots = int(max_slots if max_slots is not None else norm_existing.get("max_slots") or eff_max_slots)
            slots_existing = dict(norm_existing.get("slots") or {})
            holder_cid = str(norm_existing.get("conversation_id") or "").strip()
            existing_siblings = [str(s).strip() for s in (norm_existing.get("sibling_conversation_ids") or [])]
            same_conv_slot1 = bool(
                not holder_cid
                or holder_cid == cid_clean
                or cid_clean.startswith(holder_cid[:8])
                or holder_cid.startswith(cid_clean[:8])
                or cid_clean in existing_siblings
            )
            slot2_existing = slots_existing.get("2")
            same_conv_slot2 = False
            if isinstance(slot2_existing, dict) and bool(slot2_existing.get("active", True)):
                s2_holder = str(slot2_existing.get("conversation_id") or "").strip()
                s2_sibs = [str(s).strip() for s in (slot2_existing.get("sibling_conversation_ids") or [])]
                same_conv_slot2 = bool(
                    s2_holder
                    and (
                        s2_holder == cid_clean
                        or cid_clean.startswith(s2_holder[:8])
                        or s2_holder.startswith(cid_clean[:8])
                        or cid_clean in s2_sibs
                    )
                )

            existing_status = str(norm_existing.get("status") or "")
            # Case A: Re-acquiring or allocating Slot #2
            if not same_conv_slot1:
                can_try_slot2 = eff_max_slots >= 2 and (same_conv_slot2 or "2" not in slots_existing)
                ov_tier_resolved = (overlap_tier or "").strip()
                ov_order_resolved = list(landing_order or [])
                ov_dict: dict[str, Any] = {}
                slot2_allowed = False

                if can_try_slot2:
                    if same_conv_slot2:
                        slot2_allowed = True
                        ov_tier_resolved = ov_tier_resolved or str(slot2_existing.get("overlap_tier") or OVERLAP_NONE)
                        ov_order_resolved = ov_order_resolved or list(slot2_existing.get("landing_order") or [])
                    elif force_slot2:
                        slot2_allowed = True
                        ov_tier_resolved = ov_tier_resolved or OVERLAP_TRANSITIVE
                    elif allow_slot2 or (bool(worktree_path) and bool(clean_targets) and bool(norm_existing.get("target_files"))):
                        slot1_spec = TaskAnchorSpec.from_mapping(norm_existing)
                        cand_spec = TaskAnchorSpec(
                            conversation_id=cid_clean,
                            title=str(summary or action_id or ""),
                            status="pending",
                            priority=50,
                            target_files=tuple(clean_targets),
                            section_anchors=tuple(clean_anchors),
                            branch_name=default_branch,
                            prose_context=f"{summary}\n{proposed_fix}",
                        )
                        ov_res = analyze_pair_overlap(
                            slot1_spec,
                            cand_spec,
                            repo_dir=persistent_repo_dir,
                            repo_slug=resolved_slug,
                        )
                        ov_dict = ov_res.to_dict()
                        ov_tier_resolved = ov_res.overlap_tier
                        ov_order_resolved = list(ov_res.landing_order)
                        slot2_allowed = bool(ov_res.parallel_safe)

                if slot2_allowed:
                    prev_s2 = dict(slot2_existing) if isinstance(slot2_existing, dict) else {}
                    s2_ts = (
                        str(prev_s2.get("branch_timestamp") or resolved_ts)
                        if (same_conv_slot2 and reuse_active_branch)
                        else resolved_ts
                    )
                    s2_branch = (
                        str(prev_s2.get("branch_name") or default_branch)
                        if (same_conv_slot2 and (reuse_active_branch or not branch_name.strip()))
                        else default_branch
                    )
                    slot2_obj: dict[str, Any] = {
                        "slot_id": 2,
                        "active": True,
                        "lock_id": f"lock-{cid_clean[:8]}-{s2_ts}",
                        "conversation_id": cid_clean,
                        "sibling_conversation_ids": clean_siblings,
                        "action_id": str(action_id or prev_s2.get("action_id") or "").strip(),
                        "notification_source": "Ad-Hoc",
                        "branch_timestamp": s2_ts,
                        "branch_name": s2_branch,
                        "base_sha": resolved_base_sha or str(prev_s2.get("base_sha") or ""),
                        "overlap_tier": ov_tier_resolved or OVERLAP_NONE,
                        "landing_order": ov_order_resolved or [holder_cid, cid_clean],
                        "section_anchors": clean_anchors or list(prev_s2.get("section_anchors") or []),
                        "branch_pushed": False,
                        "pr_gate_ready": False,
                        "review_approved": bool(prev_s2.get("review_approved")) if same_conv_slot2 else False,
                        "approved_head_sha": str(prev_s2.get("approved_head_sha") or "") if same_conv_slot2 else "",
                        "deferred_send_email": bool(prev_s2.get("deferred_send_email")) if same_conv_slot2 else False,
                        "reviewed_head_sha": str(prev_s2.get("reviewed_head_sha") or "") if same_conv_slot2 else "",
                        "staged_head_sha": str(prev_s2.get("staged_head_sha") or "") if same_conv_slot2 else "",
                        "head_sha": str(prev_s2.get("head_sha") or "") if same_conv_slot2 else "",
                        "manual_pr_url": "",
                        "pr_url": "",
                        "target_repo_dir": clean_repo_dir or str(norm_existing.get("target_repo_dir") or ""),
                        "worktree_path": _sanitize_home_paths(str(worktree_path or "").strip())
                        or (str(prev_s2.get("worktree_path") or "") if same_conv_slot2 else ""),
                        "issue_number": int(issue_number) if issue_number is not None else None,
                        "issue_url": str(issue_url or "").strip(),
                        "pr_number": prev_s2.get("pr_number") if same_conv_slot2 else None,
                        "status": str(prev_s2.get("status") or "DEVELOPING") if (same_conv_slot2 and reuse_active_branch) else "DEVELOPING",
                        "summary": _sanitize_home_paths(str(summary or prev_s2.get("summary") or "").strip()),
                        "proposed_fix": _sanitize_home_paths(str(proposed_fix or prev_s2.get("proposed_fix") or "").strip()),
                        "executive_summary": (
                            str(prev_s2.get("executive_summary") or "")
                            if same_conv_slot2 and prev_s2.get("executive_summary")
                            else _sanitize_home_paths(str(proposed_fix or summary or "").strip())
                        ),
                        "target_files": clean_targets or list(prev_s2.get("target_files") or []),
                        "review_iteration": int(prev_s2.get("review_iteration") or 0) if same_conv_slot2 else 0,
                        "review_history": list(prev_s2.get("review_history") or []) if same_conv_slot2 else [],
                        "verification_results": (
                            dict(prev_s2.get("verification_results") or {})
                            if same_conv_slot2 and isinstance(prev_s2.get("verification_results"), dict)
                            else {}
                        ),
                        "acquired_at": str(prev_s2.get("acquired_at") or now_iso) if same_conv_slot2 else now_iso,
                        "worker_cid": str(worker_cid or prev_s2.get("worker_cid") or "").strip(),
                        "last_activity_at": now_iso,
                        "updated_at": now_iso,
                    }
                    slots_existing["2"] = slot2_obj
                    norm_existing["slots"] = slots_existing
                    norm_existing["max_slots"] = eff_max_slots
                    updated_repo_lock = _sync_lock_slots_structure(norm_existing, repo_slug=resolved_slug)
                    locks_map[resolved_slug] = updated_repo_lock
                    _write_locks_state(audit_state, locks_map, touched_repo_slug=resolved_slug)
                    state_obj["actions_log_audit_state"] = audit_state
                    tmp_path = state_path.with_suffix(".tmp")
                    tmp_path.write_text(json.dumps(state_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                    with contextlib.suppress(OSError):
                        os.chmod(tmp_path, 0o600)
                    os.replace(tmp_path, state_path)

                    active_count = sum(1 for v in locks_map.values() if isinstance(v, dict) and bool(v.get("active")))
                    return {
                        "status": "ok",
                        "acquired": True,
                        "slot_id": 2,
                        "overlap_tier": slot2_obj["overlap_tier"],
                        "landing_order": slot2_obj["landing_order"],
                        "repo_slug": resolved_slug,
                        "active_locks_count": active_count,
                        "lock": _project_slot_view(updated_repo_lock, "2"),
                        "repo_lock": updated_repo_lock,
                    }

                blocked_payload: dict[str, Any] = {
                    "status": "locked",
                    "acquired": False,
                    "repo_slug": resolved_slug,
                    "error": (
                        f"Repository `{resolved_slug}` is locked by branch `{norm_existing.get('branch_name')}` "
                        f"(timestamp `{norm_existing.get('branch_timestamp')}`, Conversation ID `{holder_cid}`, "
                        f"status={existing_status}) pending manual PR validation. Sibling repositories remain unlocked."
                    ),
                    "active_lock": norm_existing,
                }
                if ov_tier_resolved:
                    blocked_payload["overlap_tier"] = ov_tier_resolved
                if ov_dict:
                    blocked_payload["overlap_analysis"] = ov_dict
                return blocked_payload

            # Case B: Same conversation as Slot #1
            raw_rb = norm_existing.get("repo_branches")
            if isinstance(raw_rb, dict):
                repo_branches = {
                    str(k): dict(v)
                    for k, v in raw_rb.items()
                    if str(k).strip() and not _is_slot_scoped_rb_key(str(k)) and isinstance(v, dict)
                }
            existing_repo_entry = repo_branches.get(resolved_slug)
            if existing_repo_entry and existing_repo_entry.get("branch_name"):
                default_branch = str(existing_repo_entry.get("branch_name") or default_branch)
                resolved_ts = str(existing_repo_entry.get("branch_timestamp") or resolved_ts)
            elif reuse_active_branch:
                resolved_ts = str(norm_existing.get("branch_timestamp") or resolved_ts)
                default_branch = str(norm_existing.get("branch_name") or default_branch)
            elif (
                existing_status == "PENDING_HUMAN_PR_VALIDATION"
                and branch_timestamp
                and branch_timestamp != norm_existing.get("branch_timestamp")
            ):
                return {
                    "status": "locked",
                    "acquired": False,
                    "repo_slug": resolved_slug,
                    "error": (
                        f"Repository `{resolved_slug}` is locked by branch `{norm_existing.get('branch_name')}` "
                        f"(timestamp `{norm_existing.get('branch_timestamp')}`, Conversation ID `{holder_cid}`, "
                        f"status={existing_status}) pending manual PR validation. Sibling repositories remain unlocked."
                    ),
                    "active_lock": norm_existing,
                }

        prev_entry = repo_branches.get(resolved_slug, {})
        prev_pushed = bool(prev_entry.get("branch_pushed", False))
        repo_branches[resolved_slug] = {
            "repo_slug": resolved_slug,
            "branch_name": default_branch,
            "branch_timestamp": resolved_ts,
            "target_repo_dir": clean_repo_dir or str(prev_entry.get("target_repo_dir") or ""),
            "manual_pr_url": str(prev_entry.get("manual_pr_url") or "") if prev_pushed else "",
            "branch_pushed": prev_pushed,
            "head_sha": str(prev_entry.get("head_sha") or ""),
            "updated_at": now_iso,
        }

        same_prev = isinstance(existing_lock, dict) and existing_lock.get("conversation_id") == cid_clean
        prev_gate_ready = bool(existing_lock.get("pr_gate_ready")) if (same_prev and not reuse_active_branch) else False
        preserved_slot2 = (
            existing_lock.get("slots", {}).get("2")
            if isinstance(existing_lock, dict) and isinstance(existing_lock.get("slots"), dict)
            else None
        )
        new_lock_raw: dict[str, Any] = {
            "active": True,
            "vcs_mode": "native_git",
            "repo_slug": resolved_slug,
            "repo": resolved_slug,
            "slot_id": 1,
            "max_slots": eff_max_slots,
            "base_sha": resolved_base_sha or (str(existing_lock.get("base_sha") or "") if same_prev else ""),
            "overlap_tier": (overlap_tier or "").strip() or (str(existing_lock.get("overlap_tier") or "") if same_prev else ""),
            "landing_order": list(landing_order or ([cid_clean] if not same_prev else (existing_lock.get("landing_order") or [cid_clean]))),
            "section_anchors": clean_anchors or (list(existing_lock.get("section_anchors") or []) if same_prev else []),
            "lock_id": f"lock-{cid_clean[:8]}-{resolved_ts}",
            "conversation_id": cid_clean,
            "sibling_conversation_ids": clean_siblings,
            "action_id": str(action_id or "").strip(),
            "notification_source": "Ad-Hoc",
            "branch_timestamp": resolved_ts,
            "branch_name": default_branch,
            "branch_pushed": bool(existing_lock.get("branch_pushed")) if (same_prev and prev_gate_ready) else False,
            "pr_gate_ready": prev_gate_ready,
            "reviewed_head_sha": str(existing_lock.get("reviewed_head_sha") or "") if (same_prev and prev_gate_ready) else "",
            "staged_head_sha": str(existing_lock.get("staged_head_sha") or "") if same_prev else "",
            "head_sha": str(existing_lock.get("head_sha") or "") if same_prev else "",
            "manual_pr_url": str(existing_lock.get("manual_pr_url") or "") if (same_prev and prev_gate_ready) else "",
            "target_repo_dir": clean_repo_dir or (str(existing_lock.get("target_repo_dir") or "") if same_prev else ""),
            "repo_branches": repo_branches,
            "worktree_path": _sanitize_home_paths(str(worktree_path or "").strip())
            or (str(existing_lock.get("worktree_path") or "") if same_prev else ""),
            "issue_number": int(issue_number) if issue_number is not None else None,
            "issue_url": str(issue_url or "").strip(),
            "pr_number": existing_lock.get("pr_number") if same_prev else None,
            "pr_url": str(existing_lock.get("pr_url") or "") if (same_prev and prev_gate_ready) else "",
            "status": "DEVELOPING",
            "summary": _sanitize_home_paths(str(summary or "").strip()),
            "proposed_fix": _sanitize_home_paths(str(proposed_fix or "").strip()),
            "executive_summary": (
                str(existing_lock.get("executive_summary") or "")
                if same_prev and existing_lock.get("executive_summary")
                else _sanitize_home_paths(str(proposed_fix or summary or "").strip())
            ),
            "target_files": clean_targets,
            "review_iteration": int(existing_lock.get("review_iteration") or 0) if same_prev else 0,
            "review_history": list(existing_lock.get("review_history") or []) if same_prev else [],
            "verification_results": (
                dict(existing_lock.get("verification_results") or {})
                if same_prev and isinstance(existing_lock.get("verification_results"), dict)
                else {}
            ),
            "acquired_at": (
                str(existing_lock.get("acquired_at"))
                if same_prev and existing_lock.get("acquired_at")
                else now_iso
            ),
            "worker_cid": (
                str(worker_cid or "").strip()
                or (str(existing_lock.get("worker_cid") or "").strip() if same_prev else "")
            ),
            "last_activity_at": now_iso,
            "updated_at": now_iso,
        }
        if isinstance(preserved_slot2, dict) and bool(preserved_slot2.get("active", True)):
            new_lock_raw["slots"] = {"2": dict(preserved_slot2)}
        new_lock = _sync_lock_slots_structure(new_lock_raw, repo_slug=resolved_slug)
        locks_map[resolved_slug] = new_lock
        _write_locks_state(audit_state, locks_map, touched_repo_slug=resolved_slug)
        state_obj["actions_log_audit_state"] = audit_state
        tmp_path = state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, state_path)

    active_count = sum(1 for v in locks_map.values() if isinstance(v, dict) and bool(v.get("active")))
    return {
        "status": "ok",
        "acquired": True,
        "slot_id": 1,
        "repo_slug": resolved_slug,
        "active_locks_count": active_count,
        "lock": new_lock,
    }


def update_dev_pr_lock(
    updates: dict[str, Any],
    portfolio_dir: Path | str | None = None,
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    branch_name: str = "",
    slot_id: int | str | None = None,
) -> dict[str, Any]:
    """Update fields on the matching active per-repository lock (Slot #1 or Slot #2) inside `actions_state.json`."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_path = p_dir / "actions_state.json"
    lock_file = state_path.with_suffix(".json.lock")
    now_iso = _utc_now_iso()
    sanitize_str_keys = (
        "summary",
        "proposed_fix",
        "executive_summary",
        "proposal_title",
        "proposal_body",
        "target_repo_dir",
        "worktree_path",
    )

    with dev_common.file_lock(lock_file):
        state_obj = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        if not isinstance(state_obj, dict):
            state_obj = {}
        audit_state = state_obj.get("actions_log_audit_state")
        if not isinstance(audit_state, dict):
            audit_state = {}
        locks_map = _read_locks_state(audit_state)
        req_branch = (branch_name or str(updates.get("branch_name") or "")).strip()
        target_slug, cand_lock = _match_lock_entry(
            locks_map,
            repo_slug=repo_slug,
            workdir=workdir,
            branch_name=req_branch,
            prefer_cwd=True,
            portfolio_dir=portfolio_dir,
        )
        if not isinstance(cand_lock, dict) or not bool(cand_lock.get("active")):
            return {"status": "error", "error": f"No active dev_pr_lock to update for `{target_slug or 'default'}`."}

        norm_cand = _sync_lock_slots_structure(cand_lock, repo_slug=target_slug)
        matched_slot = _match_slot_key(
            norm_cand,
            branch_name=req_branch,
            workdir=workdir,
            conversation_id=str(updates.get("conversation_id") or ""),
            slot_id=slot_id,
        )

        if matched_slot == "2":
            if "2" not in norm_cand.get("slots", {}):
                return {
                    "status": "error",
                    "error": f"No active Slot #2 dev_pr_lock to update for `{target_slug or 'default'}`.",
                }
            slot2_cur = dict(norm_cand["slots"]["2"])
            for k, v in updates.items():
                if v is not None and k not in ("slots", "repo_branches"):
                    if k in sanitize_str_keys and isinstance(v, str):
                        slot2_cur[k] = _sanitize_home_paths(v)
                    else:
                        slot2_cur[k] = v
            slot2_cur["slot_id"] = 2
            slot2_cur["active"] = True
            slot2_cur["last_activity_at"] = str(updates.get("last_activity_at") or now_iso)
            slot2_cur["updated_at"] = now_iso
            norm_cand["slots"]["2"] = slot2_cur
            current_lock = _sync_lock_slots_structure(norm_cand, repo_slug=target_slug)
            locks_map[target_slug] = current_lock
            _write_locks_state(audit_state, locks_map, touched_repo_slug=target_slug)
            state_obj["actions_log_audit_state"] = audit_state
            tmp_path = state_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(state_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            with contextlib.suppress(OSError):
                os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, state_path)
            return {
                "status": "ok",
                "repo_slug": target_slug,
                "slot_id": 2,
                "lock": _project_slot_view(current_lock, "2"),
            }

        existing_branch = str(norm_cand.get("branch_name") or "").strip()
        slot1_rb_branches = {
            str(rb_v.get("branch_name") or "").strip()
            for rb_k, rb_v in (norm_cand.get("repo_branches") or {}).items()
            if not _is_slot_scoped_rb_key(str(rb_k)) and isinstance(rb_v, dict) and rb_v.get("branch_name")
        }
        slot1_matches_req = bool(
            req_branch and (req_branch == existing_branch or req_branch in slot1_rb_branches)
        )
        if req_branch and existing_branch and not slot1_matches_req:
            req_cid = str(updates.get("conversation_id") or "").strip()
            holder_cid = str(norm_cand.get("conversation_id") or "").strip()
            same_conv = bool(
                req_cid
                and holder_cid
                and (
                    req_cid == holder_cid
                    or req_cid.startswith(holder_cid[:8])
                    or holder_cid.startswith(req_cid[:8])
                )
            )
            if not same_conv:
                return {
                    "status": "error",
                    "error": (
                        f"Refusing cross-branch/cross-repo lock overwrite on `{target_slug}`: "
                        f"active lock holds `{existing_branch}`, requested `{req_branch}`."
                    ),
                }
        current_lock = dict(norm_cand)
        for k, v in updates.items():
            if v is None or k == "slots":
                continue
            if k == "repo_branches" and isinstance(v, dict):
                current_lock["repo_branches"] = {
                    str(rb_k): dict(rb_v)
                    for rb_k, rb_v in v.items()
                    if str(rb_k).strip() and not _is_slot_scoped_rb_key(str(rb_k)) and isinstance(rb_v, dict)
                }
            elif k in sanitize_str_keys and isinstance(v, str):
                current_lock[k] = _sanitize_home_paths(v)
            else:
                current_lock[k] = v
        current_lock["repo_slug"] = target_slug
        current_lock["repo"] = target_slug
        current_lock["last_activity_at"] = str(updates.get("last_activity_at") or now_iso)
        current_lock["updated_at"] = now_iso
        current_lock = _sync_lock_slots_structure(current_lock, repo_slug=target_slug)
        locks_map[target_slug] = current_lock
        _write_locks_state(audit_state, locks_map, touched_repo_slug=target_slug)
        state_obj["actions_log_audit_state"] = audit_state
        tmp_path = state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, state_path)
    return {"status": "ok", "repo_slug": target_slug, "slot_id": 1, "lock": current_lock}


def record_dev_pr_lock_heartbeat(
    *,
    workdir: Path | str | None = None,
    repo_slug: str = "",
    branch_name: str = "",
    worker_cid: str = "",
    stage_label: str = "",
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Stamp `last_activity_at`, optional `worker_cid`, and optional `stage_label` on an active lock."""
    now_iso = _utc_now_iso()
    updates: dict[str, Any] = {"last_activity_at": now_iso}
    if str(worker_cid or "").strip():
        updates["worker_cid"] = str(worker_cid).strip()
    if str(stage_label or "").strip():
        updates["stage_label"] = str(stage_label).strip()[:32]
    res = update_dev_pr_lock(
        updates,
        portfolio_dir=portfolio_dir,
        repo_slug=repo_slug,
        workdir=workdir,
        branch_name=branch_name,
    )
    if res.get("status") == "ok":
        res["last_activity_at"] = now_iso
    return res


def _resolve_lock_target_repo_dir(lock_info: dict[str, Any] | None) -> Path | None:
    """Resolve `target_repo_dir` recorded on `dev_pr_lock` (or its `repo_branches` entries) back to a Path."""
    if not isinstance(lock_info, dict):
        return None
    lock_repo_raw = str(lock_info.get("target_repo_dir") or "").strip()
    if not lock_repo_raw and isinstance(lock_info.get("repo_branches"), dict):
        for rb_entry in lock_info["repo_branches"].values():
            if isinstance(rb_entry, dict) and str(rb_entry.get("target_repo_dir") or "").strip():
                lock_repo_raw = str(rb_entry.get("target_repo_dir") or "").strip()
                break
    if not lock_repo_raw:
        return None
    cand = Path(lock_repo_raw.replace("$HOME", str(Path.home()))).expanduser()
    return _resolve_main_git_repo_root(cand) if (cand / ".git").exists() else cand


def cleanup_worktree(worktree_path: str | Path, repo_dir: Path | None = None) -> None:
    """Safely remove an isolated git worktree directory if it exists."""
    if not worktree_path:
        return
    wt = Path(str(worktree_path).replace("$HOME", str(Path.home()))).expanduser()
    if not wt.exists():
        return
    resolved_repo = repo_dir or _resolve_main_git_repo_root(wt)
    if (resolved_repo / ".git").exists():
        subprocess.run(
            ["git", "-C", str(resolved_repo), "worktree", "remove", "--force", str(wt)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)


def _finalize_single_released_lock(
    *,
    released_lock: dict[str, Any],
    released_slug: str,
    audit_state: dict[str, Any],
    state_dir: Path,
    reason: str,
    resolution: str,
    summary: str,
    now_iso: str,
) -> dict[str, Any]:
    """Build released lock snapshot and sync ledger verdicts for one unlocked repository."""
    cid_clean = str(released_lock.get("conversation_id") or "").strip()
    siblings = list(released_lock.get("sibling_conversation_ids") or [])
    legacy_audited = audit_state.get("audited_runs") if isinstance(audit_state.get("audited_runs"), dict) else {}
    audited_runs = dev_common.try_sync_actions_ledger_verdicts(
        state_dir=state_dir,
        legacy_audited=legacy_audited,
    )
    pending_tier2 = (
        audit_state.get("pending_tier2_proposals")
        if isinstance(audit_state.get("pending_tier2_proposals"), dict)
        else {}
    )
    if cid_clean:
        rec = dict(audited_runs.get(cid_clean) or {})
        rec.update(
            {
                "conversation_id": cid_clean,
                "tier": "TIER_2",
                "resolution": resolution,
                "summary": summary.strip() or str(rec.get("summary") or f"Branch resolved ({reason})"),
                "repo_slug": released_slug,
                "branch_name": released_lock.get("branch_name", ""),
                "branch_timestamp": released_lock.get("branch_timestamp", ""),
                "issue_number": released_lock.get("issue_number"),
                "pr_number": released_lock.get("pr_number"),
                "pr_url": released_lock.get("manual_pr_url") or released_lock.get("pr_url", ""),
                "updated_at": now_iso,
            }
        )
        verdict_updates: dict[str, dict[str, Any]] = {cid_clean: rec}
        pending_tier2.pop(cid_clean, None)
        for sib in siblings:
            sib_rec = dict(rec)
            sib_rec["conversation_id"] = sib
            sib_rec["grouped_with"] = cid_clean
            verdict_updates[sib] = sib_rec
            pending_tier2.pop(sib, None)
        dev_common.try_sync_actions_ledger_verdicts(verdict_updates, state_dir=state_dir)

    audit_state.pop("audited_runs", None)
    audit_state["pending_tier2_proposals"] = pending_tier2
    return {
        "active": False,
        "repo_slug": released_slug,
        "repo": released_slug,
        "last_released_at": now_iso,
        "last_release_reason": reason,
        "last_conversation_id": cid_clean,
        "last_branch_name": released_lock.get("branch_name", ""),
        "last_branch_timestamp": released_lock.get("branch_timestamp", ""),
        "last_repo_branches": released_lock.get("repo_branches") or {},
        "last_subagent_conversation_ids": list(released_lock.get("subagent_conversation_ids") or []),
        "last_pr_number": released_lock.get("pr_number"),
        "last_issue_number": released_lock.get("issue_number"),
    }


def release_dev_pr_lock(
    reason: str = "PR_MERGED",
    resolution: str = "APPLIED_AND_SYNCED",
    summary: str = "",
    portfolio_dir: Path | str | None = None,
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    branch_name: str = "",
    slot_id: int | str | None = None,
    release_all: bool = False,
) -> dict[str, Any]:
    """Release one repository's pending-branch lock (`repo_slug`/`workdir`/`branch_name`), or all locks when `release_all=True`.

    When two parallel slots (`slots['1']` and `slots['2']`) are active on the target repository and `release_all=False`:
      - Releasing Slot #1 finalizes Slot #1 and promotes Slot #2 into Slot #1 (`READY_AWAITING_BASE_MERGE` -> `NEEDS_REBASE_REVERIFY`).
      - Releasing Slot #2 finalizes Slot #2 while keeping Slot #1 untouched.
    """
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_path = p_dir / "actions_state.json"
    lock_file = state_path.with_suffix(".json.lock")
    now_iso = _utc_now_iso()

    released_entries: list[tuple[str, dict[str, Any]]] = []
    promoted_slots: dict[str, dict[str, Any]] = {}
    remaining_active_slugs: list[str] = []
    with dev_common.file_lock(lock_file):
        state_obj = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        if not isinstance(state_obj, dict):
            state_obj = {}
        audit_state = state_obj.get("actions_log_audit_state")
        if not isinstance(audit_state, dict):
            audit_state = {}
        locks_map = _read_locks_state(audit_state)
        active_slugs = [k for k, v in locks_map.items() if isinstance(v, dict) and bool(v.get("active"))]

        target_slugs: list[str] = []
        if release_all:
            target_slugs = active_slugs or list(locks_map.keys())
        elif repo_slug.strip() or workdir or branch_name.strip():
            matched_slug, cand = _match_lock_entry(
                locks_map,
                repo_slug=repo_slug,
                workdir=workdir,
                branch_name=branch_name,
                prefer_cwd=False,
                portfolio_dir=portfolio_dir,
            )
            if cand:
                target_slugs = [matched_slug]
        elif len(active_slugs) == 1:
            target_slugs = [active_slugs[0]]
        elif len(active_slugs) > 1:
            matched_slug, cand = _match_lock_entry(locks_map, prefer_cwd=True)
            if not matched_slug or not cand.get("active"):
                return {
                    "status": "error",
                    "released": False,
                    "error": f"Multiple active repository locks exist ({', '.join(active_slugs)}); pass --repo, --workdir, --branch, or --all.",
                    "active_repos": active_slugs,
                }
            target_slugs = [matched_slug]
        elif locks_map:
            first_slug = next(iter(locks_map))
            target_slugs = [first_slug]

        last_snap: dict[str, Any] = {}
        last_slug = ""
        for slug in target_slugs:
            raw_cur = dict(locks_map.get(slug) or {})
            if not release_all and bool(raw_cur.get("active")):
                norm_cur = _sync_lock_slots_structure(raw_cur, repo_slug=slug)
                slots_cur = dict(norm_cur.get("slots") or {})
                if "2" in slots_cur:
                    rel_slot_key = _match_slot_key(
                        norm_cur,
                        branch_name=branch_name,
                        workdir=workdir,
                        slot_id=slot_id,
                    )
                    if rel_slot_key == "2":
                        slot2_rel = dict(slots_cur["2"])
                        slot2_rel["repo_branches"] = {}
                        released_entries.append((slug, slot2_rel))
                        _finalize_single_released_lock(
                            released_lock=slot2_rel,
                            released_slug=slug,
                            audit_state=audit_state,
                            state_dir=state_path.parent,
                            reason=reason,
                            resolution=resolution,
                            summary=summary,
                            now_iso=now_iso,
                        )
                        slots_cur.pop("2", None)
                        norm_cur["slots"] = slots_cur
                        locks_map[slug] = _sync_lock_slots_structure(norm_cur, repo_slug=slug)
                        last_slug = slug
                        continue

                    # Releasing Slot #1 while Slot #2 is active -> finalize Slot #1 and promote Slot #2 -> Slot #1
                    slot1_rel = dict(norm_cur)
                    slot1_rb = {
                        str(k): dict(v)
                        for k, v in (norm_cur.get("repo_branches") or {}).items()
                        if str(k).strip() and not _is_slot_scoped_rb_key(str(k)) and isinstance(v, dict)
                    }
                    slot1_rel["repo_branches"] = slot1_rb
                    slot1_rel.pop("slots", None)
                    released_entries.append((slug, slot1_rel))
                    snap1 = _finalize_single_released_lock(
                        released_lock=slot1_rel,
                        released_slug=slug,
                        audit_state=audit_state,
                        state_dir=state_path.parent,
                        reason=reason,
                        resolution=resolution,
                        summary=summary,
                        now_iso=now_iso,
                    )
                    slot2_prom = dict(slots_cur["2"])
                    slot2_prom["slot_id"] = 1
                    slot2_prom["active"] = True
                    slot2_prom["promoted_from_slot"] = 2
                    slot2_prom["promoted_at"] = now_iso
                    slot2_prom["previous_slot1_branch"] = str(slot1_rel.get("branch_name") or "")
                    slot2_prom["previous_slot1_release_reason"] = reason
                    if str(slot2_prom.get("status") or "") == SLOT_STATUS_READY_AWAITING_BASE_MERGE:
                        slot2_prom["status"] = SLOT_STATUS_NEEDS_REBASE_REVERIFY
                        slot2_prom["branch_action"] = SLOT_STATUS_NEEDS_REBASE_REVERIFY
                    elif reason in ("BRANCH_MERGED_INTO_MAIN", "HUMAN_VALIDATED_MANUAL_PR"):
                        slot2_prom["needs_base_rebase"] = True
                    slot2_prom["updated_at"] = now_iso

                    promoted_raw: dict[str, Any] = {
                        "active": True,
                        "vcs_mode": str(norm_cur.get("vcs_mode") or "native_git"),
                        "repo_slug": slug,
                        "repo": slug,
                        "max_slots": int(norm_cur.get("max_slots") or DEFAULT_MAX_REPO_SLOTS),
                        **slot2_prom,
                        "repo_branches": {
                            slug: {
                                "repo_slug": slug,
                                "branch_name": str(slot2_prom.get("branch_name") or ""),
                                "branch_timestamp": str(slot2_prom.get("branch_timestamp") or ""),
                                "target_repo_dir": str(slot2_prom.get("target_repo_dir") or norm_cur.get("target_repo_dir") or ""),
                                "worktree_path": str(slot2_prom.get("worktree_path") or ""),
                                "manual_pr_url": "",
                                "branch_pushed": False,
                                "head_sha": str(slot2_prom.get("head_sha") or slot2_prom.get("staged_head_sha") or ""),
                                "updated_at": now_iso,
                            }
                        },
                        "last_released_slot1": snap1,
                    }
                    promoted_raw.pop("slots", None)
                    promoted_norm = _sync_lock_slots_structure(promoted_raw, repo_slug=slug)
                    locks_map[slug] = promoted_norm
                    promoted_slots[slug] = promoted_norm
                    last_slug = slug
                    continue

            rel_lock = raw_cur
            if release_all and isinstance(rel_lock.get("slots"), dict) and isinstance(rel_lock["slots"].get("2"), dict):
                s2_extra = dict(rel_lock["slots"]["2"])
                s2_extra["repo_branches"] = {}
                released_entries.append((slug, s2_extra))
            released_entries.append((slug, rel_lock))
            snap = _finalize_single_released_lock(
                released_lock=rel_lock,
                released_slug=slug,
                audit_state=audit_state,
                state_dir=state_path.parent,
                reason=reason,
                resolution=resolution,
                summary=summary,
                now_iso=now_iso,
            )
            locks_map[slug] = snap
            last_snap = snap
            last_slug = slug

        if not released_entries:
            last_snap = _inactive_lock_snapshot(
                audit_state.get("dev_pr_lock") if isinstance(audit_state.get("dev_pr_lock"), dict) else None
            )
            last_snap["last_released_at"] = now_iso
            last_snap["last_release_reason"] = reason

        _write_locks_state(
            audit_state,
            locks_map,
            touched_repo_slug=last_slug,
            released_snapshot=last_snap,
        )
        remaining_active_slugs = [k for k, v in locks_map.items() if isinstance(v, dict) and bool(v.get("active"))]
        state_obj["actions_log_audit_state"] = audit_state
        tmp_path = state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, state_path)

    if reason in ("BRANCH_MERGED_INTO_MAIN", "HUMAN_VALIDATED_MANUAL_PR"):
        for released_slug, released_lock in released_entries:
            cid_clean = str(released_lock.get("conversation_id") or "").strip()
            if not cid_clean:
                continue
            siblings = list(released_lock.get("sibling_conversation_ids") or [])
            with contextlib.suppress(Exception):
                rb_map = released_lock.get("repo_branches") if isinstance(released_lock.get("repo_branches"), dict) else {}
                merged_branches = [
                    str(entry.get("branch_name") or "")
                    for k_rb, entry in rb_map.items()
                    if not _is_slot_scoped_rb_key(str(k_rb)) and isinstance(entry, dict) and entry.get("branch_name")
                ] or ([str(released_lock.get("branch_name") or "")] if released_lock.get("branch_name") else [])
                for q_cid in [cid_clean, *siblings]:
                    _mark_queue_item_repo_merged(
                        conversation_id=q_cid,
                        released_repo_slug=released_slug,
                        branch_names=merged_branches,
                        subagent_conversation_ids=list(released_lock.get("subagent_conversation_ids") or []),
                        remaining_active_locks=get_all_dev_pr_locks(portfolio_dir=portfolio_dir),
                        portfolio_dir=portfolio_dir,
                    )
    elif reason == "HUMAN_REJECTED_BRANCH":
        rem_locks = get_all_dev_pr_locks(portfolio_dir=portfolio_dir)
        for released_slug, released_lock in released_entries:
            cid_clean = str(released_lock.get("conversation_id") or "").strip()
            if not cid_clean:
                continue
            still_locked = any(
                _matches_cid(str(al.get("conversation_id") or ""), cid_clean)
                or any(_matches_cid(str(s), cid_clean) for s in (al.get("sibling_conversation_ids") or []))
                for al in rem_locks.values()
            )
            if still_locked:
                continue
            with contextlib.suppress(Exception):
                q_snap = load_dev_queue(portfolio_dir=portfolio_dir)
                existing_it = next(
                    (it for it in (q_snap.get("items") or []) if _matches_cid(str(it.get("conversation_id") or ""), cid_clean)),
                    None,
                )
                if isinstance(existing_it, dict) and str(existing_it.get("source") or "") == "ad_hoc_lock":
                    rel_br = str(released_lock.get("branch_name") or "").strip()
                    update_dev_queue_item_status(
                        conversation_id=cid_clean,
                        status="rejected",
                        strip_pr_urls_for_branches=[rel_br] if rel_br else None,
                        strip_repo_slug=released_slug,
                        portfolio_dir=portfolio_dir,
                    )

    for _, released_lock in released_entries:
        wt_path = str(released_lock.get("worktree_path") or "").strip()
        if wt_path:
            cleanup_worktree(wt_path, repo_dir=_resolve_lock_target_repo_dir(released_lock))

    stop_dev_api_server(portfolio_dir=portfolio_dir)
    restart_pro_api_server(portfolio_dir=portfolio_dir)

    primary_prev = released_entries[0][1] if released_entries else {}
    out_res: dict[str, Any] = {
        "status": "ok",
        "released": True,
        "released_repos": list(dict.fromkeys(s for s, _ in released_entries)),
        "remaining_active_repos": remaining_active_slugs,
        "reason": reason,
        "resolution": resolution,
        "previous_lock": primary_prev,
    }
    if promoted_slots:
        out_res["promoted_slots"] = promoted_slots
    return out_res


def _sync_single_repo_lock_with_git(
    *,
    repo_slug: str,
    lock_info: dict[str, Any],
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> dict[str, Any]:
    """Synchronize one repository's active lock against `origin` via native `git`."""
    lock_repo_path = _resolve_lock_target_repo_dir(lock_info)
    if git_client is not None:
        vcs = git_client
    else:
        env_ov: dict[str, str] = {}
        if lock_repo_path and (lock_repo_path / ".git").exists():
            env_ov["AGY_CONF_REPO_DIR"] = str(lock_repo_path)
        if portfolio_dir is not None:
            env_ov["AGY_DEV_STATE_DIR"] = str(dev_common.resolve_state_dir(portfolio_dir=portfolio_dir))
        vcs = GitVcsIntegrationClient(env_overrides=env_ov) if env_ov else GitVcsIntegrationClient()

    branch_name = str(lock_info.get("branch_name") or "").strip()
    branch_pushed = bool(lock_info.get("branch_pushed"))
    slot2_dict = (
        lock_info.get("slots", {}).get("2")
        if isinstance(lock_info.get("slots"), dict) and isinstance(lock_info["slots"].get("2"), dict)
        else {}
    )
    slot2_branch = str(slot2_dict.get("branch_name") or "").strip() if slot2_dict else ""
    extra_protected = [slot2_branch] if slot2_branch else []

    if not branch_name or not branch_pushed:
        prot_list = [b for b in (branch_name, slot2_branch) if b]
        repo_sync = (
            vcs.sync_and_clean_local_repo(
                base_branch="main",
                protected_branches=prot_list,
                checkout_base_if_current_gone=False,
            )
            if hasattr(vcs, "sync_and_clean_local_repo")
            else {}
        )
        return {
            "status": "ok",
            "repo_slug": repo_slug,
            "active": True,
            "released": False,
            "lock": lock_info,
            "repo_sync": repo_sync,
        }

    branch_state = vcs.inspect_remote_branch_state(branch_name=branch_name)
    if branch_state.get("status") != "ok":
        return {
            "status": "ok",
            "repo_slug": repo_slug,
            "active": True,
            "released": False,
            "lock": lock_info,
            "git_sync_warning": branch_state.get("error", ""),
        }

    is_merged_into_base = bool(branch_state.get("merged_into_base"))
    if (
        not is_merged_into_base
        and not bool(branch_state.get("remote_exists"))
        and isinstance(vcs, GitVcsIntegrationClient)
        and lock_repo_path is not None
        and (lock_repo_path / ".git").exists()
    ):
        loc_merged, _ = _inspect_local_branch_merged_readonly(lock_repo_path, branch_name, base_branch="main")
        if loc_merged:
            is_merged_into_base = True

    if is_merged_into_base:
        if bool(branch_state.get("remote_exists")) and hasattr(vcs, "delete_branch_native"):
            vcs.delete_branch_native(
                branch_name=branch_name,
                remote="origin",
                delete_remote=True,
                delete_local=False,
            )
        repo_sync = (
            vcs.sync_and_clean_local_repo(
                base_branch="main",
                protected_branches=extra_protected,
                force_clean_branches=[branch_name],
                fetch=bool(branch_state.get("remote_exists")),
            )
            if hasattr(vcs, "sync_and_clean_local_repo")
            else {}
        )
        rel = release_dev_pr_lock(
            reason="BRANCH_MERGED_INTO_MAIN",
            resolution="APPLIED_AND_SYNCED",
            summary=f"Branch `{branch_name}` (`{lock_info.get('branch_timestamp', '')}`) merged into main via manual PR.",
            portfolio_dir=portfolio_dir,
            repo_slug=repo_slug,
            branch_name=branch_name,
        )
        promoted = (rel.get("promoted_slots") or {}).get(repo_slug)
        if isinstance(promoted, dict):
            return {
                "status": "ok",
                "repo_slug": repo_slug,
                "active": True,
                "released": True,
                "slot1_merged": True,
                "reason": "SLOT1_MERGED_SLOT2_PROMOTED",
                "promoted_slot": promoted,
                "lock": promoted,
                "release_result": rel,
                "repo_sync": repo_sync,
            }
        return {
            "status": "ok",
            "repo_slug": repo_slug,
            "active": False,
            "released": True,
            "reason": "BRANCH_MERGED_INTO_MAIN",
            "release_result": rel,
            "repo_sync": repo_sync,
        }

    if not bool(branch_state.get("remote_exists")):
        repo_sync = (
            vcs.sync_and_clean_local_repo(
                base_branch="main",
                protected_branches=extra_protected,
                force_clean_branches=[branch_name],
                fetch=False,
            )
            if hasattr(vcs, "sync_and_clean_local_repo")
            else {}
        )
        rel = release_dev_pr_lock(
            reason="REMOTE_BRANCH_DELETED_AFTER_EVALUATION",
            resolution="VERIFIED_OK",
            summary=f"Remote branch `{branch_name}` was deleted on origin after human evaluation; lock released.",
            portfolio_dir=portfolio_dir,
            repo_slug=repo_slug,
            branch_name=branch_name,
        )
        promoted = (rel.get("promoted_slots") or {}).get(repo_slug)
        if isinstance(promoted, dict):
            return {
                "status": "ok",
                "repo_slug": repo_slug,
                "active": True,
                "released": True,
                "slot1_released": True,
                "reason": "SLOT1_DELETED_SLOT2_PROMOTED",
                "promoted_slot": promoted,
                "lock": promoted,
                "release_result": rel,
                "repo_sync": repo_sync,
            }
        return {
            "status": "ok",
            "repo_slug": repo_slug,
            "active": False,
            "released": True,
            "reason": "REMOTE_BRANCH_DELETED_AFTER_EVALUATION",
            "release_result": rel,
            "repo_sync": repo_sync,
        }

    repo_sync = (
        vcs.sync_and_clean_local_repo(
            base_branch="main",
            protected_branches=[b for b in (branch_name, slot2_branch) if b],
            fetch=False,
        )
        if hasattr(vcs, "sync_and_clean_local_repo")
        else {}
    )
    return {
        "status": "ok",
        "repo_slug": repo_slug,
        "active": True,
        "released": False,
        "lock": lock_info,
        "branch_state": branch_state,
        "repo_sync": repo_sync,
    }


def _resolve_repo_dir_for_slug(
    repo_slug: str,
    *,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> Path | None:
    """Resolve a local Git checkout root for an explicit `owner/repo` slug when `workdir` is not provided."""
    clean_slug = dev_common.normalize_repo_slug(repo_slug)
    if not clean_slug:
        return None

    all_known_locks = get_all_dev_pr_locks(portfolio_dir=portfolio_dir, include_inactive=True)
    known_entry = all_known_locks.get(clean_slug)
    if isinstance(known_entry, dict):
        cand_dir = _resolve_lock_target_repo_dir(known_entry)
        if cand_dir is not None and (cand_dir / ".git").exists():
            return _resolve_main_git_repo_root(cand_dir)
        last_rb = known_entry.get("last_repo_branches")
        if isinstance(last_rb, dict):
            rb_item = last_rb.get(clean_slug)
            if isinstance(rb_item, dict):
                raw_dir = str(rb_item.get("target_repo_dir") or "").strip()
                if raw_dir:
                    cand_rb = Path(raw_dir.replace("$HOME", str(Path.home()))).expanduser()
                    if (cand_rb / ".git").exists():
                        return _resolve_main_git_repo_root(cand_rb)

    base_vcs = git_client or GitVcsIntegrationClient()
    base_dir = base_vcs.resolve_repo_dir()
    main_base_dir = _resolve_main_git_repo_root(base_dir) if (base_dir / ".git").exists() else base_dir
    candidates: list[Path] = []
    if (main_base_dir / ".git").exists():
        candidates.append(main_base_dir)
    repo_name = clean_slug.rsplit("/", 1)[-1]
    if repo_name:
        candidates.append(main_base_dir.parent / repo_name)
        candidates.append(base_dir.parent / repo_name)
    if "/" in clean_slug:
        owner_part, name_part = clean_slug.split("/", 1)
        candidates.append(Path.home() / "Code" / "Github" / owner_part / name_part)

    seen: set[str] = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        if (cand / ".git").exists():
            cand_slug = dev_common.resolve_repo_slug_from_path(cand)
            if cand_slug == clean_slug:
                return _resolve_main_git_repo_root(cand)
    return None


def _sync_unlocked_repo_with_git(
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    scoped_slug: str = "",
    resolved_dir: Path | None = None,
    already_resolved: bool = False,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve and sync an unlocked repository using `workdir`/`repo_slug` without mutating injected `git_client`."""
    is_scoped = bool(repo_slug.strip() or workdir)
    target_slug = scoped_slug
    target_dir = resolved_dir

    if is_scoped:
        if not already_resolved and not target_slug and target_dir is None:
            target_slug, target_dir = _resolve_repo_slug_and_dir(
                repo_slug=repo_slug,
                workdir=workdir,
                git_client=git_client,
                prefer_cwd=False,
                portfolio_dir=portfolio_dir,
            )
        if git_client is None and target_dir is None and not workdir and target_slug and target_slug != "default":
            target_dir = _resolve_repo_dir_for_slug(
                target_slug,
                portfolio_dir=portfolio_dir,
            )
        if target_dir is not None and (target_dir / ".git").exists():
            target_dir = _resolve_main_git_repo_root(target_dir)

    state_dir_str = str(dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)) if portfolio_dir is not None else ""
    if git_client is not None:
        vcs = git_client
    elif target_dir is not None:
        env_ov: dict[str, str] = {"AGY_CONF_REPO_DIR": str(target_dir)}
        if state_dir_str:
            env_ov["AGY_DEV_STATE_DIR"] = state_dir_str
        vcs = GitVcsIntegrationClient(env_overrides=env_ov)
    elif is_scoped:
        skip_reason = "workdir_not_a_git_repo" if workdir and not target_slug else "repo_dir_not_found_for_slug"
        return target_slug, {
            "status": "skipped",
            "synced": False,
            "reason": skip_reason,
            "repo_slug": target_slug,
            "base_branch": "main",
        }
    else:
        vcs = (
            GitVcsIntegrationClient(env_overrides={"AGY_DEV_STATE_DIR": state_dir_str})
            if state_dir_str
            else GitVcsIntegrationClient()
        )

    if not hasattr(vcs, "sync_and_clean_local_repo"):
        return target_slug, {}
    if isinstance(vcs, GitVcsIntegrationClient) and portfolio_dir is not None:
        repo_sync = vcs.sync_and_clean_local_repo(base_branch="main", state_dir=portfolio_dir)
    else:
        repo_sync = vcs.sync_and_clean_local_repo(base_branch="main")
    return target_slug, repo_sync


def sync_dev_pr_lock_with_git(
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
) -> dict[str, Any]:
    """Inspect active per-repository locks via native `git`, release merged/deleted branches, and synchronize local `main`."""
    active_map = get_all_dev_pr_locks(portfolio_dir=portfolio_dir)
    if not active_map:
        stop_dev_api_server(portfolio_dir=portfolio_dir)
        scoped_slug, repo_sync = _sync_unlocked_repo_with_git(
            repo_slug=repo_slug,
            workdir=workdir,
            portfolio_dir=portfolio_dir,
            git_client=git_client,
        )
        queue_recon = reconcile_dev_queue_with_git(
            portfolio_dir=portfolio_dir,
            git_client=git_client,
            repo_slug=repo_slug,
            workdir=workdir,
            fetch=False,
        )
        res: dict[str, Any] = {
            "status": "ok",
            "active": False,
            "released": False,
            "lock": get_dev_pr_lock(portfolio_dir=portfolio_dir, repo_slug=repo_slug, workdir=workdir),
            "locks": {},
            "repo_sync": repo_sync,
            "queue_reconciliation": queue_recon,
        }
        if scoped_slug:
            res["repo_slug"] = scoped_slug
        return res

    target_entries: list[tuple[str, dict[str, Any]]]
    if repo_slug.strip() or workdir:
        scoped_slug, resolved_dir = _resolve_repo_slug_and_dir(
            repo_slug=repo_slug,
            workdir=workdir,
            git_client=git_client,
            prefer_cwd=False,
            portfolio_dir=portfolio_dir,
        )
        if scoped_slug and scoped_slug in active_map:
            target_entries = [(scoped_slug, active_map[scoped_slug])]
        else:
            scoped_slug, repo_sync = _sync_unlocked_repo_with_git(
                repo_slug=repo_slug,
                workdir=workdir,
                scoped_slug=scoped_slug,
                resolved_dir=resolved_dir,
                already_resolved=True,
                portfolio_dir=portfolio_dir,
                git_client=git_client,
            )
            queue_recon = reconcile_dev_queue_with_git(
                portfolio_dir=portfolio_dir,
                git_client=git_client,
                repo_slug=repo_slug,
                workdir=workdir,
                fetch=False,
            )
            return {
                "status": "ok",
                "active": bool(active_map),
                "repo_active": False,
                "released": False,
                "repo_slug": scoped_slug,
                "lock": get_dev_pr_lock(portfolio_dir=portfolio_dir, repo_slug=repo_slug, workdir=workdir),
                "locks": active_map,
                "repo_sync": repo_sync,
                "queue_reconciliation": queue_recon,
            }
    elif git_client is not None and len(active_map) > 1:
        first_slug = next(iter(active_map))
        target_entries = [(first_slug, active_map[first_slug])]
    else:
        target_entries = list(active_map.items())

    per_repo_results: dict[str, dict[str, Any]] = {}
    any_released = False
    first_release_reason = ""
    first_release_result: dict[str, Any] = {}
    for slug, linfo in target_entries:
        res_item = _sync_single_repo_lock_with_git(
            repo_slug=slug,
            lock_info=linfo,
            portfolio_dir=portfolio_dir,
            git_client=git_client if len(target_entries) == 1 else None,
        )
        per_repo_results[slug] = res_item
        if res_item.get("released"):
            any_released = True
            if not first_release_reason:
                first_release_reason = str(res_item.get("reason") or "")
                first_release_result = dict(res_item.get("release_result") or {})

    queue_recon = reconcile_dev_queue_with_git(
        portfolio_dir=portfolio_dir,
        git_client=git_client if len(target_entries) == 1 else None,
        repo_slug=repo_slug,
        workdir=workdir,
        fetch=False,
    )
    remaining = get_all_dev_pr_locks(portfolio_dir=portfolio_dir)
    if len(target_entries) == 1:
        single_res = next(iter(per_repo_results.values()))
        single_res.setdefault("locks", remaining)
        if repo_slug.strip() or workdir:
            single_res["any_active"] = bool(remaining)
        single_res["queue_reconciliation"] = queue_recon
        return single_res

    return {
        "status": "ok",
        "active": bool(remaining),
        "released": any_released,
        "reason": first_release_reason or ("active_locks_remaining" if remaining else "all_locks_released"),
        "release_result": first_release_result,
        "results_by_repo": per_repo_results,
        "locks": remaining,
        "queue_reconciliation": queue_recon,
    }


def run_pr_merge_watcher_loop(
    poll_interval_seconds: float = 15.0,
    max_duration_seconds: float = 14400.0,
    max_iterations: int | None = None,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> dict[str, Any]:
    """Poll `sync_dev_pr_lock_with_git()` until `dev_pr_lock` is released (PR merged into `main` or remote branch deleted), fast-forwarding local `main`, pruning the local `fix/*` branch, and self-terminating."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    tmp_dir = p_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    watcher_lock_path = tmp_dir / "pr_merge_watcher.lock"

    lock_fd = open(watcher_lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock_fd.close()
            return {
                "status": "ok",
                "already_running": True,
                "terminated": False,
                "reason": "watcher_already_running",
            }

        lock_fd.seek(0)
        lock_fd.truncate()
        lock_fd.write(f"{os.getpid()}\n")
        lock_fd.flush()

        start_mono = time.monotonic()
        iterations = 0
        last_sync: dict[str, Any] = {}

        while True:
            iterations += 1
            last_sync = sync_dev_pr_lock_with_git(portfolio_dir=portfolio_dir, git_client=git_client)
            if not last_sync.get("active"):
                return {
                    "status": "ok",
                    "already_running": False,
                    "terminated": True,
                    "reason": str(last_sync.get("reason") or "lock_inactive"),
                    "released": bool(last_sync.get("released")),
                    "iterations": iterations,
                    "last_sync": last_sync,
                }
            if max_iterations is not None and iterations >= max_iterations:
                return {
                    "status": "ok",
                    "already_running": False,
                    "terminated": False,
                    "reason": "max_iterations_reached",
                    "released": False,
                    "iterations": iterations,
                    "last_sync": last_sync,
                }
            if (time.monotonic() - start_mono) >= max_duration_seconds:
                return {
                    "status": "ok",
                    "already_running": False,
                    "terminated": True,
                    "reason": "max_duration_reached",
                    "released": False,
                    "iterations": iterations,
                    "last_sync": last_sync,
                }
            time.sleep(max(0.05, float(poll_interval_seconds)))
    finally:
        if not lock_fd.closed:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            try:
                lock_fd.close()
            except OSError:
                pass


def spawn_pr_merge_watcher_daemon(
    poll_interval_seconds: float = 15.0,
    max_duration_seconds: float = 14400.0,
    portfolio_dir: Path | str | None = None,
    force_spawn: bool = False,
) -> dict[str, Any]:
    """Spawn a detached, single-instance background `watch-pr-merge` process that polls `origin` every `poll_interval_seconds` while `dev_pr_lock` is active and exits immediately once the PR is merged or rejected."""
    if not force_spawn and (
        portfolio_dir is not None
        or "unittest" in sys.modules
        or "pytest" in sys.modules
        or os.environ.get("AGY_OFFLINE_ONLY") == "1"
        or os.environ.get("PRE_COMMIT_HOOK") == "1"
    ):
        return {
            "status": "skipped",
            "spawned": False,
            "reason": "hermetic_or_offline_mode",
        }

    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    tmp_dir = p_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    watcher_lock_path = tmp_dir / "pr_merge_watcher.lock"

    probe_fd = open(watcher_lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(probe_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe_fd.fileno(), fcntl.LOCK_UN)
        except OSError:
            return {
                "status": "ok",
                "spawned": False,
                "already_running": True,
                "lock_file": str(watcher_lock_path),
            }
    finally:
        try:
            probe_fd.close()
        except OSError:
            pass

    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "watch-pr-merge",
        "--interval",
        str(max(1, int(poll_interval_seconds))),
        "--max-duration",
        str(max(15, int(max_duration_seconds))),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return {
            "status": "ok",
            "spawned": True,
            "already_running": False,
            "pid": proc.pid,
            "poll_interval_seconds": poll_interval_seconds,
            "lock_file": str(watcher_lock_path),
        }
    except OSError as exc:
        return {
            "status": "error",
            "spawned": False,
            "error": str(exc),
        }


def wait_for_unlock_or_actionable_event(
    poll_interval_seconds: float = 15.0,
    max_duration_seconds: float = 3600.0,
    max_iterations: int | None = None,
    stall_threshold_seconds: int | float | None = None,
    orchestrator_conversation_id: str = "",
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    brain_dir: Path | str | None = None,
) -> dict[str, Any]:
    """0-LLM reactive watcher that polls `sync_dev_pr_lock_with_git()` and exits the moment a repo unlocks, runnable work appears, or a worker stalls."""
    initial_locks = get_all_dev_pr_locks(portfolio_dir=portfolio_dir)
    initial_locked_repos = set(initial_locks.keys())
    initial_lock_ids = {slug: str(lk.get("lock_id") or "") for slug, lk in initial_locks.items()}

    effective_brain: Path | str | None = brain_dir
    if effective_brain is None and portfolio_dir is not None:
        # Keep isolated test/custom portfolio directories hermetic from ~/.gemini/antigravity/brain
        effective_brain = ""

    start_mono = time.monotonic()
    iterations = 0

    while True:
        iterations += 1
        sync_res = sync_dev_pr_lock_with_git(portfolio_dir=portfolio_dir, git_client=git_client)
        current_locks = get_all_dev_pr_locks(portfolio_dir=portfolio_dir)
        current_locked_repos = set(current_locks.keys())

        released_set: set[str] = set(initial_locked_repos - current_locked_repos)
        for slug, prev_id in initial_lock_ids.items():
            cur_lk = current_locks.get(slug)
            if cur_lk is not None and prev_id and str(cur_lk.get("lock_id") or "") != prev_id:
                released_set.add(slug)
        rel_res_obj = sync_res.get("release_result")
        if isinstance(rel_res_obj, dict):
            for r_slug in rel_res_obj.get("released_repos") or []:
                if str(r_slug).strip():
                    released_set.add(dev_common.normalize_repo_slug(str(r_slug)))
        by_repo_sync = sync_res.get("results_by_repo")
        if isinstance(by_repo_sync, dict):
            for r_slug, r_info in by_repo_sync.items():
                if isinstance(r_info, dict) and bool(r_info.get("released")):
                    released_set.add(dev_common.normalize_repo_slug(str(r_slug)))
        released_repos = sorted(released_set)

        q_next = get_next_dev_queue_item(
            orchestrator_conversation_id=orchestrator_conversation_id,
            portfolio_dir=portfolio_dir,
            skip_locked_repos=True,
        )
        next_unlocked = q_next.get("next_unlocked_item")
        runnable_by_repo = q_next.get("runnable_by_repo") or {}
        summary = q_next.get("summary") or {}

        stalled_locks: list[dict[str, Any]] = []
        rebase_reverify_locks: list[dict[str, Any]] = []
        for slug, lk in sorted(current_locks.items()):
            if str(lk.get("status") or "").strip().upper() == SLOT_STATUS_NEEDS_REBASE_REVERIFY:
                rebase_reverify_locks.append(
                    {
                        "repo_slug": slug,
                        "slot_id": int(lk.get("slot_id") or 1),
                        "branch_name": str(lk.get("branch_name") or ""),
                        "conversation_id": str(lk.get("conversation_id") or ""),
                        "worktree_path": str(lk.get("worktree_path") or ""),
                        "base_sha": str(lk.get("base_sha") or ""),
                    }
                )
            slot_candidates: list[tuple[int, dict[str, Any]]] = [(1, lk)]
            s2_lk = lk.get("slots", {}).get("2") if isinstance(lk.get("slots"), dict) else None
            if isinstance(s2_lk, dict) and bool(s2_lk.get("active", True)):
                slot_candidates.append((2, s2_lk))
            for s_num, slot_entry in slot_candidates:
                st_info = detect_stalled_lock(
                    slot_entry,
                    threshold_seconds=stall_threshold_seconds,
                    brain_dir=effective_brain,
                )
                if st_info.get("stalled"):
                    stalled_locks.append(
                        {
                            "repo_slug": slug,
                            "slot_id": s_num,
                            "branch_name": str(slot_entry.get("branch_name") or ""),
                            "conversation_id": str(slot_entry.get("conversation_id") or ""),
                            "worker_cid": str(st_info.get("worker_cid") or ""),
                            "idle_seconds": int(st_info.get("idle_seconds") or 0),
                            "idle_label": str(st_info.get("idle_label") or ""),
                        }
                    )

        open_active_count = (
            int(summary.get("pending") or 0)
            + int(summary.get("in_progress") or 0)
            + int(summary.get("pr_open") or 0)
        )
        elapsed_seconds = round(max(0.0, time.monotonic() - start_mono), 2)

        def _payload(event_name: str, *, rearm: bool = False) -> dict[str, Any]:
            return {
                "status": "ok",
                "event": event_name,
                "released_repos": released_repos,
                "rebase_reverify_locks": rebase_reverify_locks,
                "active_lock_repos": sorted(current_locked_repos),
                "active_lock_count": len(current_locks),
                "stalled_locks": stalled_locks,
                "next_unlocked_item": next_unlocked,
                "runnable_by_repo": runnable_by_repo,
                "summary": summary,
                "iterations": iterations,
                "elapsed_seconds": elapsed_seconds,
                "rearm_recommended": rearm,
            }

        if rebase_reverify_locks:
            return _payload("slot2_needs_rebase_reverify", rearm=False)
        if released_repos:
            return _payload("repo_unlocked", rearm=False)
        if next_unlocked is not None:
            return _payload("runnable_work_available", rearm=False)
        if stalled_locks:
            return _payload("stalled_worker", rearm=False)
        if not current_locks and open_active_count == 0:
            return _payload("queue_drained", rearm=False)
        if max_iterations is not None and iterations >= max_iterations:
            return _payload("max_iterations", rearm=bool(current_locks or open_active_count > 0))
        if (time.monotonic() - start_mono) >= max_duration_seconds:
            return _payload("timeout", rearm=bool(current_locks or open_active_count > 0))

        time.sleep(max(0.05, float(poll_interval_seconds)))


# ==============================================================================
# Steps 1–4: RCA / Prompt Scope, Timestamped Branch Creation, Lock & Worktree
# ==============================================================================


def build_remediation_proposal_markdown(
    conversation_id: str,
    action_id: str,
    rca_summary: str,
    proposed_fix: str,
    target_files: list[str] | None = None,
    sibling_ids: list[str] | None = None,
    evidence: str = "",
    branch_timestamp: str = "",
    branch_name: str = "",
    scheduled: bool = False,
) -> tuple[str, str]:
    """Build a structured `(proposal_title, proposal_body_markdown)` for the timestamped branch."""
    _ = scheduled
    cid8 = conversation_id[:8]
    act_tag = action_id or "code-change"
    rca_summary = _sanitize_home_paths(rca_summary)
    proposed_fix = _sanitize_home_paths(proposed_fix)
    evidence = _sanitize_home_paths(evidence)
    short_sum = re.sub(r"\s+", " ", rca_summary.strip())[:72]
    ts_label = branch_timestamp or generate_branch_timestamp()
    resolved_branch = branch_name or format_timestamped_branch_name(ts_label, act_tag, cid8)
    title = format_remediation_commit_title(
        f"fix({act_tag}): {short_sum} [{ts_label}]",
        action_id=act_tag,
        conversation_id=cid8,
        branch_timestamp=ts_label,
    )

    files_bullets = (
        "\n".join(f"- `{fp}`" for fp in (target_files or []) if str(fp).strip())
        or "- `(see diff details)`"
    )
    siblings_line = (
        f"- **Grouped Sibling Conversation IDs**: {', '.join(f'`{s}`' for s in sibling_ids)}\n"
        if sibling_ids
        else ""
    )
    evidence_block = (
        f"\n### Technical Evidence / Stacktrace Excerpt\n```text\n{evidence.strip()[:1200]}\n```\n"
        if evidence.strip()
        else ""
    )

    body = (
        f"## Native Git Timestamped Branch Proposal — New Branch Created (`{ts_label}`)\n\n"
        f"- **Branch Action**: **NEW BRANCH CREATED** (`origin/{resolved_branch}` by `{PR_DEVELOPER_SUBAGENT_NAME}`)\n"
        f"- **New Branch Created**: `{resolved_branch}`\n"
        f"- **Branch Timestamp**: `{ts_label}`\n"
        f"- **Proposed Branch**: `{resolved_branch}`\n"
        f"- **Created & Validated By**: `{PR_DEVELOPER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL})` · `{PR_REVIEWER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL})`\n"
        f"- **Primary Conversation ID**: `{conversation_id}`\n"
        f"{siblings_line}"
        f"- **Action / Trigger**: `{act_tag}`\n"
        f"- **Generated At (UTC)**: `{_utc_now_iso()}`\n\n"
        f"---\n\n"
        f"## 1. Root Cause Analysis / Change Rationale\n\n"
        f"{rca_summary.strip()}\n"
        f"{evidence_block}\n"
        f"## 2. Proposed Code / Configuration Solution\n\n"
        f"{proposed_fix.strip() or 'Implement defensive guardrails and unit tests for the requested change.'}\n\n"
        f"### Target Files\n"
        f"{files_bullets}\n\n"
        f"---\n\n"
        f"## 3. Native Git Branch & Manual PR Validation Checklist\n"
        f"- [x] **Step 1**: Audit `actions_ledger.db` record or prompt scope, extract rationale, and propose solution\n"
        f"- [x] **Step 2**: Create timestamped branch `{resolved_branch}` (`{ts_label}`) & acquire `dev_pr_lock`\n"
        f"- [ ] **Step 3**: Develop proposed changes on `{resolved_branch}` & pass local unit tests + `qa_static_audit.py`\n"
        f"- [ ] **Step 4**: Push branch to `origin/{resolved_branch}` via native `git push` (never `main`) & run `{PR_REVIEWER_SUBAGENT_NAME}` evaluation\n"
        f"- [ ] **Step 5**: User compares `{resolved_branch}` against `main` and manually creates Pull Request (or rejects branch) to release `dev_pr_lock`\n"
    )
    return title, body


def prepare_isolated_worktree(
    branch_name: str,
    repo_dir: Path | None = None,
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Create an isolated Git worktree for `branch_name` so `main` is never left dirty during branch evaluation."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    vcs = GitVcsIntegrationClient()
    resolved_repo = repo_dir or vcs.resolve_repo_dir()
    if not (resolved_repo / ".git").exists():
        return {"status": "error", "error": f"Git repository not found at {resolved_repo}"}

    safe_slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", branch_name)
    repo_token = _slugify_branch_token(_resolve_main_git_repo_root(resolved_repo).name, max_len=24)
    wt_dir = p_dir / "tmp" / f"pr-worktree-{repo_token}-{safe_slug}"
    if wt_dir.exists():
        cleanup_worktree(wt_dir, repo_dir=resolved_repo)
    wt_dir.parent.mkdir(parents=True, exist_ok=True)

    start_ref = "HEAD"
    proc_origin_main = subprocess.run(
        ["git", "-C", str(resolved_repo), "rev-parse", "--verify", "origin/main"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if proc_origin_main.returncode == 0:
        start_ref = "origin/main"

    proc = subprocess.run(
        ["git", "-C", str(resolved_repo), "worktree", "add", "-B", branch_name, str(wt_dir), start_ref],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    if proc.returncode != 0:
        return {
            "status": "error",
            "error": (proc.stderr or proc.stdout or "git worktree add failed").strip(),
        }
    return {
        "status": "ok",
        "branch_name": branch_name,
        "worktree_path": str(wt_dir),
        "repo_dir": str(resolved_repo),
    }


def _find_existing_repo_branch_for_conversation(
    repo_dir: Path,
    cid_clean: str,
    existing_lock: dict[str, Any],
    repo_slug: str = "",
    same_conv: bool = False,
) -> tuple[str, str]:
    """Return `(existing_branch_name, existing_branch_timestamp)` if `repo_dir` already has an active branch for `cid_clean`."""
    if same_conv and isinstance(existing_lock, dict) and existing_lock.get("active"):
        slot_num = int(existing_lock.get("slot_id") or 1)
        raw_rb = existing_lock.get("repo_branches")
        if slot_num > 1:
            b_name = str(existing_lock.get("branch_name") or "").strip()
            b_ts = str(existing_lock.get("branch_timestamp") or "").strip()
            if b_name:
                return b_name, b_ts
        if repo_slug and isinstance(raw_rb, dict) and raw_rb:
            rb_entry = _lookup_slot_repo_branch_entry(raw_rb, repo_slug, slot_num)
            if isinstance(rb_entry, dict) and str(rb_entry.get("branch_name") or "").strip():
                b_name = str(rb_entry.get("branch_name") or "").strip()
                b_ts = str(rb_entry.get("branch_timestamp") or "").strip()
                return b_name, b_ts
        b_name = str(existing_lock.get("branch_name") or "").strip()
        b_ts = str(existing_lock.get("branch_timestamp") or "").strip()
        if b_name:
            return b_name, b_ts

    cid8 = re.sub(r"[^a-zA-Z0-9]+", "", cid_clean)[:8].lower()
    if len(cid8) < 6 or not isinstance(repo_dir, Path) or not (repo_dir / ".git").exists():
        return "", ""

    proc_refs = subprocess.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/heads/fix/*-{cid8}",
            f"refs/remotes/origin/fix/*-{cid8}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if proc_refs.returncode != 0:
        return "", ""

    candidates: list[str] = []
    for raw_ref in (proc_refs.stdout or "").splitlines():
        ref_clean = raw_ref.strip()
        if ref_clean.startswith("origin/"):
            ref_clean = ref_clean[len("origin/") :]
        if ref_clean.startswith("fix/") and ref_clean not in candidates:
            candidates.append(ref_clean)
    if not candidates:
        return "", ""

    candidates.sort(reverse=True)
    for cand_br in candidates:
        proc_ahead = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-list", "--count", f"origin/main..{cand_br}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc_ahead.returncode == 0:
            try:
                if int((proc_ahead.stdout or "0").strip()) > 0:
                    m_ts = re.match(r"^fix/(\d{8}-\d{6})-", cand_br)
                    return cand_br, (m_ts.group(1) if m_ts else "")
            except ValueError:
                pass
        else:
            m_ts = re.match(r"^fix/(\d{8}-\d{6})-", cand_br)
            return cand_br, (m_ts.group(1) if m_ts else "")
    return "", ""


def start_remediation_branch_and_lock(
    conversation_id: str,
    action_id: str = "",
    rca_summary: str = "",
    proposed_fix: str = "",
    target_files: list[str] | None = None,
    sibling_ids: list[str] | None = None,
    create_worktree: bool = False,
    checkout_branch: bool = False,
    branch_timestamp: str = "",
    reuse_active_branch: bool = False,
    scheduled: bool = False,
    workdir: Path | str | None = None,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    worker_cid: str = "",
    *,
    section_anchors: list[str] | None = None,
    allow_slot2: bool = False,
    force_slot2: bool = False,
    max_slots: int | None = None,
) -> dict[str, Any]:
    """Execute Steps 1–2: inspect/RCA, verify lock/slot availability (1-slot or safe 2-slot parallel), create timestamped git branch, and acquire `dev_pr_lock`."""
    vcs = git_client or GitVcsIntegrationClient()
    resolved_slug, resolved_target_dir = _resolve_repo_slug_and_dir(
        workdir=workdir,
        git_client=vcs,
        prefer_cwd=True,
    )
    target_repo_dir = resolved_target_dir or vcs.resolve_repo_dir()
    repo_slug = resolved_slug

    # Synchronize ONLY this repository's lock via native git in case a previous branch was merged/deleted
    sync_dev_pr_lock_with_git(
        portfolio_dir=portfolio_dir,
        git_client=git_client,
        repo_slug=repo_slug,
        workdir=target_repo_dir,
    )

    existing_lock = get_dev_pr_lock(
        portfolio_dir=portfolio_dir,
        repo_slug=repo_slug,
        workdir=target_repo_dir,
    )
    cid_clean = str(conversation_id or "").strip()
    holder_cid = str(existing_lock.get("conversation_id") or "").strip()
    existing_siblings = [str(s).strip() for s in (existing_lock.get("sibling_conversation_ids") or [])]
    same_conv = bool(
        not holder_cid
        or holder_cid == cid_clean
        or cid_clean.startswith(holder_cid[:8])
        or holder_cid.startswith(cid_clean[:8])
        or cid_clean in existing_siblings
    )
    slots_existing = existing_lock.get("slots") if isinstance(existing_lock.get("slots"), dict) else {}
    slot2_existing = slots_existing.get("2") if isinstance(slots_existing.get("2"), dict) else {}
    same_conv_slot2 = False
    if slot2_existing and bool(slot2_existing.get("active", True)):
        s2_cid = str(slot2_existing.get("conversation_id") or "").strip()
        s2_sibs = [str(s).strip() for s in (slot2_existing.get("sibling_conversation_ids") or [])]
        same_conv_slot2 = bool(
            s2_cid
            and (
                s2_cid == cid_clean
                or cid_clean.startswith(s2_cid[:8])
                or s2_cid.startswith(cid_clean[:8])
                or cid_clean in s2_sibs
            )
        )

    existing_status = str(existing_lock.get("status") or "")
    allocating_slot2 = False
    slot2_overlap_tier = ""
    slot2_landing_order: list[str] = []
    slot2_overlap_dict: dict[str, Any] = {}

    if existing_lock.get("active") and not same_conv:
        eff_max_slots = int(
            max_slots if max_slots is not None else existing_lock.get("max_slots") or _resolve_max_repo_slots()
        )
        slot2_free = eff_max_slots >= 2 and (same_conv_slot2 or "2" not in slots_existing)
        clean_tfiles = [str(f).strip() for f in (target_files or []) if str(f).strip()]
        clean_sanchors = [str(a).strip() for a in (section_anchors or []) if str(a).strip()]

        if same_conv_slot2:
            allocating_slot2 = True
            slot2_overlap_tier = str(slot2_existing.get("overlap_tier") or OVERLAP_NONE)
            slot2_landing_order = list(slot2_existing.get("landing_order") or [])
        elif slot2_free and force_slot2:
            allocating_slot2 = True
            slot2_overlap_tier = OVERLAP_TRANSITIVE
        elif slot2_free and (allow_slot2 or (create_worktree and bool(clean_tfiles) and bool(existing_lock.get("target_files")))):
            main_git_root = _resolve_main_git_repo_root(target_repo_dir) if isinstance(target_repo_dir, Path) else None
            slot1_spec = TaskAnchorSpec.from_mapping(existing_lock)
            cand_spec = TaskAnchorSpec(
                conversation_id=cid_clean,
                title=str(rca_summary or action_id or ""),
                status="pending",
                priority=50,
                target_files=tuple(clean_tfiles),
                section_anchors=tuple(clean_sanchors),
                prose_context=f"{rca_summary}\n{proposed_fix}",
            )
            ov_res = analyze_pair_overlap(
                slot1_spec,
                cand_spec,
                repo_dir=main_git_root,
                repo_slug=repo_slug,
            )
            slot2_overlap_dict = ov_res.to_dict()
            slot2_overlap_tier = ov_res.overlap_tier
            slot2_landing_order = list(ov_res.landing_order)
            if ov_res.parallel_safe:
                allocating_slot2 = True

    if existing_lock.get("active") and not allocating_slot2 and (
        not same_conv
        or (not reuse_active_branch and existing_status == "PENDING_HUMAN_PR_VALIDATION")
    ):
        pending_branch = existing_lock.get("branch_name") or "unknown-branch"
        pending_ts = existing_lock.get("branch_timestamp") or ""
        q_res: dict[str, Any] = {}
        if cid_clean and not same_conv:
            q_res = _auto_enqueue_blocked_task(
                conversation_id=cid_clean,
                action_id=action_id,
                rca_summary=rca_summary,
                proposed_fix=proposed_fix,
                target_files=target_files,
                section_anchors=section_anchors,
                repo_slugs=[repo_slug] if repo_slug else None,
                blocked_by_lock_id=str(existing_lock.get("lock_id") or ""),
                portfolio_dir=portfolio_dir,
            )
        locked_out: dict[str, Any] = {
            "status": "locked",
            "acquired": False,
            "repo_slug": repo_slug,
            "queued": bool(q_res.get("status") == "ok"),
            "queue_entry": q_res.get("item"),
            "error": (
                f"Cannot start new branch or make code changes in `{repo_slug}` for `{cid_clean}`: "
                f"branch `{pending_branch}` (timestamp `{pending_ts}`, conversation `{existing_lock.get('conversation_id')}`) "
                f"is currently pending manual validation to generate a Pull Request or reject it."
            ),
            "active_lock": existing_lock,
        }
        if slot2_overlap_tier:
            locked_out["overlap_tier"] = slot2_overlap_tier
        if slot2_overlap_dict:
            locked_out["overlap_analysis"] = slot2_overlap_dict
        return locked_out

    evidence = ""
    lock_for_reuse = slot2_existing if (allocating_slot2 and same_conv_slot2) else existing_lock
    if existing_lock.get("active") and (reuse_active_branch or same_conv or same_conv_slot2):
        existing_sum = str(lock_for_reuse.get("summary") or "").strip()
        existing_fix = str(lock_for_reuse.get("proposed_fix") or "").strip()
        if not rca_summary.strip() and existing_sum and existing_sum != "Tool/step execution error":
            rca_summary = existing_sum
        if not proposed_fix.strip() and existing_fix and not existing_fix.startswith("Inspect the failing CLI command flags"):
            proposed_fix = existing_fix

    if not rca_summary.strip() or not proposed_fix.strip():
        report = dev_common.try_inspect_antigravity_conversation(cid_clean)
        if isinstance(report, dict) and report.get("status") == "ok":
            action_id = action_id or str(report.get("action_id") or report.get("trigger") or "action-audit")
            rca_obj = report.get("rca_analysis") if isinstance(report.get("rca_analysis"), dict) else {}
            causes = list(rca_obj.get("root_causes") or [])
            if causes and isinstance(causes[0], dict):
                c0 = causes[0]
                cand_cause = str(c0.get("root_cause") or report.get("details") or "").strip()
                cand_fix = str(c0.get("proposed_fix") or "").strip()
                if cand_cause and cand_cause != "Tool/step execution error":
                    rca_summary = rca_summary.strip() or cand_cause
                if cand_fix and not cand_fix.startswith("Inspect the failing CLI command flags"):
                    proposed_fix = proposed_fix.strip() or cand_fix
                evidence = str(c0.get("evidence") or "")
            else:
                cand_details = str(report.get("details") or "").strip()
                if cand_details and cand_details != "Tool/step execution error":
                    rca_summary = rca_summary.strip() or cand_details
                proposed_fix = proposed_fix.strip() or "Add defensive validation and unit test coverage."
            if not target_files:
                target_files = list(report.get("touched_files") or [])

    rca_summary = _sanitize_home_paths(rca_summary.strip()) or f"Code/configuration change in conversation {cid_clean}"
    proposed_fix = _sanitize_home_paths(proposed_fix.strip()) or "Apply code/skill change on timestamped branch and add unit test coverage."

    reused_repo_branch, reused_repo_ts = _find_existing_repo_branch_for_conversation(
        repo_dir=target_repo_dir,
        cid_clean=cid_clean,
        existing_lock=lock_for_reuse if (same_conv or same_conv_slot2) else {},
        repo_slug=repo_slug,
        same_conv=bool(same_conv or same_conv_slot2),
    )
    resolved_ts = (
        branch_timestamp.strip()
        or reused_repo_ts
        or (str(lock_for_reuse.get("branch_timestamp") or "").strip() if (same_conv or same_conv_slot2) else "")
        or generate_branch_timestamp()
    )
    branch_name = (
        reused_repo_branch
        if reused_repo_branch
        else format_timestamped_branch_name(resolved_ts, action_id, cid_clean)
    )

    proposal_title, proposal_body = build_remediation_proposal_markdown(
        conversation_id=cid_clean,
        action_id=action_id,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        target_files=target_files,
        sibling_ids=sibling_ids,
        evidence=evidence,
        branch_timestamp=resolved_ts,
        branch_name=branch_name,
        scheduled=scheduled,
    )

    wt_path = ""
    if create_worktree or (allocating_slot2 and isinstance(target_repo_dir, Path) and (target_repo_dir / ".git").exists()):
        wt_res = prepare_isolated_worktree(
            branch_name=branch_name,
            repo_dir=_resolve_main_git_repo_root(target_repo_dir) if isinstance(target_repo_dir, Path) else None,
            portfolio_dir=portfolio_dir,
        )
        if wt_res.get("status") == "ok":
            wt_path = str(wt_res.get("worktree_path") or "")
            if wt_path and not allocating_slot2:
                stamp_repo_version_manifests(wt_path, branch_timestamp=resolved_ts)
        else:
            return {
                "status": "error",
                "step": "prepare_isolated_worktree",
                "error": wt_res.get("error", "Failed to create isolated git worktree"),
            }
    elif checkout_branch:
        repo_dir = target_repo_dir
        if isinstance(repo_dir, Path) and (repo_dir / ".git").exists():
            proc_dirty = subprocess.run(
                ["git", "-C", str(repo_dir), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            is_dirty = bool((proc_dirty.stdout or "").strip())
            proc_origin_main = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--verify", "origin/main"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            proc_existing_br = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--verify", f"refs/heads/{branch_name}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            proc_remote_br = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--verify", f"refs/remotes/origin/{branch_name}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc_existing_br.returncode == 0:
                subprocess.run(
                    ["git", "-C", str(repo_dir), "checkout", branch_name],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            elif proc_remote_br.returncode == 0 and not is_dirty:
                subprocess.run(
                    ["git", "-C", str(repo_dir), "checkout", "-B", branch_name, f"origin/{branch_name}"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            elif not reuse_active_branch and not reused_repo_branch and not is_dirty and proc_origin_main.returncode == 0:
                subprocess.run(
                    ["git", "-C", str(repo_dir), "checkout", "-B", branch_name, "origin/main"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            else:
                subprocess.run(
                    ["git", "-C", str(repo_dir), "checkout", "-B", branch_name],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            stamp_repo_version_manifests(repo_dir, branch_timestamp=resolved_ts)

    lock_res = acquire_dev_pr_lock(
        conversation_id=cid_clean,
        action_id=action_id,
        issue_number=None,
        issue_url="",
        branch_name=branch_name,
        branch_timestamp=resolved_ts,
        worktree_path=wt_path,
        summary=rca_summary,
        proposed_fix=proposed_fix,
        target_files=target_files,
        sibling_ids=sibling_ids,
        reuse_active_branch=reuse_active_branch or bool(reused_repo_branch),
        scheduled=scheduled,
        portfolio_dir=portfolio_dir,
        target_repo_dir=str(target_repo_dir) if isinstance(target_repo_dir, Path) else "",
        repo_slug=repo_slug,
        worker_cid=worker_cid,
        section_anchors=section_anchors,
        max_slots=max_slots,
        allow_slot2=allocating_slot2 or allow_slot2,
        force_slot2=force_slot2,
        overlap_tier=slot2_overlap_tier,
        landing_order=slot2_landing_order or None,
    )
    if not lock_res.get("acquired"):
        return lock_res

    if cid_clean:
        with contextlib.suppress(Exception):
            _sync_active_lock_to_dev_queue(
                conversation_id=cid_clean,
                status="in_progress",
                title=proposal_title,
                action_id=action_id,
                repo_slug=repo_slug,
                branch_name=branch_name,
                rca_summary=rca_summary,
                proposed_fix=proposed_fix,
                target_files=target_files,
                section_anchors=section_anchors,
                portfolio_dir=portfolio_dir,
            )

    return {
        "status": "ok",
        "acquired": True,
        "slot_id": int(lock_res.get("slot_id") or 1),
        "overlap_tier": str(lock_res.get("overlap_tier") or slot2_overlap_tier or ""),
        "vcs_mode": "native_git",
        "repo_slug": repo_slug,
        "target_repo_dir": _sanitize_home_paths(str(target_repo_dir)) if isinstance(target_repo_dir, Path) else "",
        "branch_timestamp": resolved_ts,
        "branch_name": branch_name,
        "proposal_title": proposal_title,
        "proposal_markdown": proposal_body,
        "manual_pr_url": "",
        "pr_gate_ready": False,
        "worktree_path": wt_path,
        "lock": lock_res.get("lock", {}),
    }


# ==============================================================================
# Step 6: Local Unit Test & Static QA Gate (Deterministic Verification Persistence)
# ==============================================================================

_GIT_HOOK_ENV_VARS: tuple[str, ...] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
)


def _clean_git_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a subprocess environment stripped of outer git hook `GIT_*` overrides."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_HOOK_ENV_VARS}
    if extra:
        env.update(extra)
    return env


def _snapshot_worktree_state(target_dir: Path, branch_name: str = "") -> dict[str, Any]:
    """Capture deterministic git `head_sha`, `tree_sha`, `dirty`, and `content_fingerprint` for `target_dir`."""
    if not (target_dir / ".git").exists():
        return {"head_sha": "", "tree_sha": "", "dirty": False, "content_fingerprint": ""}

    ref = branch_name.strip() or "HEAD"
    head_proc = subprocess.run(
        ["git", "-C", str(target_dir), "rev-parse", "--verify", ref],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    head_sha = (head_proc.stdout or "").strip() if head_proc.returncode == 0 else ""

    tree_proc = subprocess.run(
        ["git", "-C", str(target_dir), "rev-parse", "--verify", f"{ref}^{{tree}}"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    tree_sha = (tree_proc.stdout or "").strip() if tree_proc.returncode == 0 else ""

    status_proc = subprocess.run(
        ["git", "-C", str(target_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    porcelain = (status_proc.stdout or "").strip() if status_proc.returncode == 0 else ""
    is_dirty = bool(porcelain)

    hasher = hashlib.sha256()
    hasher.update(tree_sha.encode("utf-8"))
    if is_dirty:
        diff_proc = subprocess.run(
            ["git", "-C", str(target_dir), "diff", "HEAD"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        hasher.update(b"\x00diff\x00")
        hasher.update(diff_proc.stdout or b"")
        untracked_proc = subprocess.run(
            ["git", "-C", str(target_dir), "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        for rel_u in sorted((untracked_proc.stdout or "").splitlines()):
            rel_clean = rel_u.strip()
            if not rel_clean:
                continue
            u_path = target_dir / rel_clean
            if u_path.is_file():
                exec_mode = 0
                with contextlib.suppress(OSError):
                    exec_mode = u_path.stat().st_mode & 0o111
                hasher.update(
                    b"\x00untracked\x00"
                    + rel_clean.encode("utf-8")
                    + b"\x00"
                    + f"{exec_mode:03o}".encode("ascii")
                    + b"\x00"
                )
                with contextlib.suppress(OSError):
                    hasher.update(u_path.read_bytes())

    return {
        "head_sha": head_sha,
        "tree_sha": tree_sha,
        "dirty": is_dirty,
        "content_fingerprint": hasher.hexdigest()[:32],
    }


def _parse_qa_static_audit_summary(output: str, passed: bool, ran: bool) -> dict[str, Any]:
    """Extract deterministic error/warning counts from `qa_static_audit.py` output."""
    if not ran:
        return {"status": "skipped", "passed": True, "errors": 0, "warnings": 0}
    errors_cnt = 0 if passed else 1
    warnings_cnt = 0
    m_counts = re.search(r"\((\d+)\s+errors?,\s*(\d+)\s+warnings?\)", output or "", re.IGNORECASE)
    if m_counts:
        errors_cnt = int(m_counts.group(1))
        warnings_cnt = int(m_counts.group(2))
    return {
        "status": "pass" if passed else "fail",
        "passed": bool(passed),
        "errors": errors_cnt,
        "warnings": warnings_cnt,
    }


def _parse_unittest_summary(output: str, passed: bool, ran: bool) -> dict[str, Any]:
    """Extract deterministic passed/total/failed counts from `unittest` stderr/stdout."""
    if not ran:
        return {"status": "skipped", "passed": True, "passed_count": 0, "total_count": 0, "failed_count": 0}
    total = sum(int(m) for m in re.findall(r"Ran\s+(\d+)\s+tests?\s+in\b", output or ""))
    failures = sum(int(m) for m in re.findall(r"failures=(\d+)", output or ""))
    errors = sum(int(m) for m in re.findall(r"errors=(\d+)", output or ""))
    failed = failures + errors
    if not passed and failed == 0:
        failed = max(1, total)
    passed_cnt = max(0, total - failed) if total else (0 if not passed else 0)
    return {
        "status": "pass" if passed else "fail",
        "passed": bool(passed),
        "passed_count": passed_cnt,
        "total_count": total,
        "failed_count": failed,
    }


def _parse_bats_summary(output: str, passed: bool, ran: bool) -> dict[str, Any]:
    """Extract deterministic TAP passed/total counts from BATS output."""
    if not ran:
        return {"status": "skipped", "passed": True, "passed_count": 0, "total_count": 0}
    ok_cnt = len(re.findall(r"(?m)^ok\s+\d+\b", output or ""))
    not_ok_cnt = len(re.findall(r"(?m)^not ok\s+\d+\b", output or ""))
    plan_total = sum(int(m) for m in re.findall(r"(?m)^1\.\.(\d+)\s*$", output or ""))
    total = max(plan_total, ok_cnt + not_ok_cnt)
    return {
        "status": "pass" if passed else "fail",
        "passed": bool(passed),
        "passed_count": ok_cnt,
        "total_count": total,
    }


def _resolve_valid_verification_results(
    lock_info: dict[str, Any] | None,
    target_dir: Path | None,
    branch_name: str = "",
    *,
    pre_commit_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return `verification_results` bound to current commit/tree SHA, or empty dict if stale/absent."""
    if not isinstance(lock_info, dict):
        return {}
    vr = lock_info.get("verification_results")
    if not isinstance(vr, dict) or not vr:
        return {}

    if target_dir is None or not (target_dir / ".git").exists():
        return dict(vr)

    cur_state = _snapshot_worktree_state(target_dir, branch_name=branch_name)
    vr_head = str(vr.get("head_sha") or "").strip()
    vr_tree = str(vr.get("tree_sha") or "").strip()
    vr_fp = str(vr.get("content_fingerprint") or "").strip()
    vr_dirty = bool(vr.get("dirty"))

    matches = False
    if cur_state["head_sha"] and vr_head and vr_head == cur_state["head_sha"] and not vr_dirty and not cur_state["dirty"]:
        matches = True
    elif cur_state["tree_sha"] and vr_tree and vr_tree == cur_state["tree_sha"] and not vr_dirty and not cur_state["dirty"]:
        matches = True
    elif cur_state["content_fingerprint"] and vr_fp and vr_fp == cur_state["content_fingerprint"]:
        matches = True
    elif pre_commit_state and vr_fp and vr_fp == str(pre_commit_state.get("content_fingerprint") or ""):
        matches = True

    if not matches:
        return {}

    bound = dict(vr)
    if cur_state["head_sha"]:
        bound["head_sha"] = cur_state["head_sha"]
    if cur_state["tree_sha"]:
        bound["tree_sha"] = cur_state["tree_sha"]
    bound["dirty"] = bool(cur_state["dirty"])
    if cur_state["content_fingerprint"]:
        bound["content_fingerprint"] = cur_state["content_fingerprint"]
    return bound


def _format_verification_checks_markdown(verification_results: dict[str, Any] | None) -> list[str]:
    """Render deterministic `## 🧪 Automated Verification Checks` Markdown lines from `verification_results`."""
    lines: list[str] = [
        "",
        "## 🧪 Automated Verification Checks",
        "",
    ]
    if not isinstance(verification_results, dict) or not verification_results:
        lines.append("- **Status**: ⚠️ No verified `test-local` snapshot bound to current commit SHA yet.")
        return lines

    head_sha = str(verification_results.get("head_sha") or "").strip()
    tree_sha = str(verification_results.get("tree_sha") or "").strip()
    verified_at = str(verification_results.get("verified_at_utc") or "").strip()
    meta_parts: list[str] = []
    if head_sha:
        meta_parts.append(f"**Verified Commit SHA**: `{head_sha[:12]}`")
    if tree_sha:
        meta_parts.append(f"**Tree SHA**: `{tree_sha[:12]}`")
    if verified_at:
        meta_parts.append(f"**Verified At (UTC)**: `{verified_at}`")
    if meta_parts:
        lines.append("- " + " · ".join(meta_parts))
        lines.append("")

    def _status_badge(status_str: str) -> str:
        norm = status_str.strip().lower()
        if norm in ("pass", "passed"):
            return "✅ PASS"
        if norm in ("fail", "failed"):
            return "❌ FAIL"
        if norm == "not_run":
            return "⏭️ NOT RUN"
        return "⏭️ SKIPPED"

    qa_info = verification_results.get("qa_static_audit") if isinstance(verification_results.get("qa_static_audit"), dict) else {}
    ut_info = verification_results.get("unittest") if isinstance(verification_results.get("unittest"), dict) else {}
    bats_info = verification_results.get("bats") if isinstance(verification_results.get("bats"), dict) else {}
    pc_info = verification_results.get("pre_commit") if isinstance(verification_results.get("pre_commit"), dict) else {}

    qa_st = str(qa_info.get("status") or "skipped")
    qa_det = (
        f"`{int(qa_info.get('errors') or 0)} errors, {int(qa_info.get('warnings') or 0)} warnings`"
        if qa_st != "skipped"
        else "`qa_static_audit.py` not present"
    )
    ut_st = str(ut_info.get("status") or "skipped")
    ut_total = int(ut_info.get("total_count") or 0)
    ut_passed = int(ut_info.get("passed_count") or 0)
    ut_det = (
        f"`{ut_passed}/{ut_total} tests passed`"
        if ut_total > 0
        else ("`all targeted checks passed`" if ut_st == "pass" else "`tests failed`" if ut_st == "fail" else "`no tests/` directory")
    )
    bats_st = str(bats_info.get("status") or "skipped")
    bats_total = int(bats_info.get("total_count") or 0)
    bats_passed = int(bats_info.get("passed_count") or 0)
    bats_det = (
        f"`{bats_passed}/{bats_total} specs passed`"
        if bats_total > 0
        else ("`BATS suite passed`" if bats_st == "pass" else "`BATS suite failed`" if bats_st == "fail" else "`no BATS suite`")
    )
    pc_st = str(pc_info.get("status") or "not_run")
    if pc_st in ("pass", "fail"):
        if pc_info.get("mode") == "incremental":
            pc_det = f"`pre-commit run --files ({int(pc_info.get('files_count') or 0)} files)`"
        else:
            pc_det = "`pre-commit run --all-files`"
    else:
        pc_det = "`not recorded in test-local`"

    lines.extend(
        [
            "| Check | Status | Details |",
            "| :--- | :--- | :--- |",
            f"| `qa_static_audit` | {_status_badge(qa_st)} | {qa_det} |",
            f"| `unittest` | {_status_badge(ut_st)} | {ut_det} |",
            f"| `bats` | {_status_badge(bats_st)} | {bats_det} |",
            f"| `pre_commit` | {_status_badge(pc_st)} | {pc_det} |",
        ]
    )
    return lines


def _build_single_pass_pre_commit_skip(target_dir: Path, base_skip: str = "") -> tuple[str, list[str]]:
    """Build `SKIP` value for single-pass `test-local --with-pre-commit` avoiding duplicate audit/tests/BATS."""
    _ = target_dir
    hooks: list[str] = []
    for raw_tok in (base_skip or "").split(","):
        tok = raw_tok.strip()
        if tok and tok not in hooks:
            hooks.append(tok)
    for required_skip in ("unit-tests", "unit-tests-fast", "qa-static-audit", "bats-suite"):
        if required_skip not in hooks:
            hooks.append(required_skip)
    return (",".join(hooks), hooks)


def _detect_validation_changed_files(
    target_dir: Path,
    env: dict[str, str] | None = None,
) -> list[str]:
    """Return deduplicated modified/staged/untracked/branch-diff relative paths inside `target_dir`."""
    if not (target_dir / ".git").exists():
        return []

    seen: set[str] = set()
    changed: list[str] = []

    def _collect_nul(git_args: list[str]) -> None:
        proc = subprocess.run(
            ["git", "-C", str(target_dir), *git_args],
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return
        for raw in proc.stdout.split(b"\x00"):
            if not raw:
                continue
            rel = raw.decode("utf-8", errors="surrogateescape").strip().replace("\\", "/")
            while rel.startswith("./"):
                rel = rel[2:]
            if not rel or "__pycache__/" in rel or rel.endswith((".pyc", ".pyo")):
                continue
            if rel not in seen:
                seen.add(rel)
                changed.append(rel)

    for base_ref in ("origin/main", "main"):
        ref_proc = subprocess.run(
            ["git", "-C", str(target_dir), "rev-parse", "--verify", "--quiet", base_ref],
            env=env,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if ref_proc.returncode == 0:
            _collect_nul(["diff", "--name-only", "-z", f"{base_ref}...HEAD"])
            break

    _collect_nul(["diff", "--cached", "--name-only", "-z"])
    _collect_nul(["diff", "--name-only", "-z"])
    _collect_nul(["ls-files", "--others", "--exclude-standard", "-z"])
    return changed


def _build_single_pass_pre_commit_cmd(
    pc_bin: str,
    target_dir: Path,
    raw_changed_files: list[str],
    *,
    run_all: bool = False,
) -> tuple[list[str], str, list[str]]:
    """Build incremental `--files` or fallback `--all-files` command for `pre-commit run`."""
    cfg_changed = any(
        Path(rel).name in (".pre-commit-config.yaml", ".pre-commit-config.yml")
        for rel in raw_changed_files
    )
    existing_files = [
        rel
        for rel in raw_changed_files
        if (target_dir / rel).is_file() or (target_dir / rel).is_symlink()
    ]
    if run_all or cfg_changed or not existing_files:
        return ([pc_bin, "run", "--all-files"], "all-files", [])
    return ([pc_bin, "run", "--files", *existing_files], "incremental", existing_files)


def _resolve_selective_should_run_bats(
    selective_runner: Path,
    raw_changed_files: list[str],
    *,
    run_all: bool = False,
) -> bool:
    """Inspect `selective_test_runner.py` to determine whether BATS is required for `raw_changed_files`."""
    if run_all:
        return True
    if any(Path(rel).name in (".pre-commit-config.yaml", ".pre-commit-config.yml") for rel in raw_changed_files):
        return True
    if not selective_runner.is_file():
        return True

    try:
        spec = importlib.util.spec_from_file_location("_validation_selective_runner", str(selective_runner))
        if spec is not None and spec.loader is not None:
            mod = importlib.util.module_from_spec(spec)
            prev_no_bytecode = sys.dont_write_bytecode
            sys.dont_write_bytecode = True
            try:
                spec.loader.exec_module(mod)
            finally:
                sys.dont_write_bytecode = prev_no_bytecode
            if hasattr(mod, "should_run_bats"):
                return bool(mod.should_run_bats(list(raw_changed_files), run_all=run_all))
            if hasattr(mod, "build_test_plan"):
                plan = mod.build_test_plan(
                    selective_runner.parents[2],
                    changed_files=list(raw_changed_files),
                    run_all=run_all,
                )
                if isinstance(plan, dict) and "should_run_bats" in plan:
                    return bool(plan["should_run_bats"])
    except Exception:
        return True

    return any(rel.endswith((".sh", ".bats")) for rel in raw_changed_files)


@contextlib.contextmanager
def _temporarily_index_untracked_files(
    target_dir: Path,
    env: dict[str, str] | None = None,
) -> Iterator[list[str]]:
    """Temporarily register untracked non-ignored files with `git add -N` so `pre-commit` checks them."""
    if not (target_dir / ".git").exists():
        yield []
        return
    list_proc = subprocess.run(
        ["git", "-C", str(target_dir), "ls-files", "--others", "--exclude-standard", "-z"],
        env=env,
        capture_output=True,
        timeout=15,
        check=False,
    )
    if list_proc.returncode != 0 or not list_proc.stdout:
        yield []
        return
    untracked = [
        raw.decode("utf-8", errors="surrogateescape")
        for raw in list_proc.stdout.split(b"\x00")
        if raw
    ]
    if not untracked:
        yield []
        return

    indexed: list[str] = []
    batch_size = 200
    try:
        for start in range(0, len(untracked), batch_size):
            batch = untracked[start : start + batch_size]
            add_proc = subprocess.run(
                ["git", "-C", str(target_dir), "add", "-N", "--", *batch],
                env=env,
                capture_output=True,
                timeout=15,
                check=False,
            )
            if add_proc.returncode == 0:
                indexed.extend(batch)
        yield indexed
    finally:
        for start in range(0, len(indexed), batch_size):
            batch = indexed[start : start + batch_size]
            subprocess.run(
                ["git", "-C", str(target_dir), "rm", "--cached", "--quiet", "--ignore-unmatch", "--", *batch],
                env=env,
                capture_output=True,
                timeout=15,
                check=False,
            )


def run_local_validation_suite(
    workdir: Path | str | None = None,
    test_targets: list[str] | None = None,
    timeout: float = 180.0,
    *,
    portfolio_dir: Path | str | None = None,
    run_pre_commit: bool = False,
    pre_commit_status: str = "",
    run_bats: bool = True,
    run_all: bool = False,
) -> dict[str, Any]:
    """Execute Step 6: Optional single-pass `pre-commit` (`SKIP=unit-tests,unit-tests-fast,qa-static-audit,bats-suite`), `qa_static_audit.py --incremental`, `selective_test_runner.py` / `unittest`, and selective BATS, persisting unified `verification_results` bound to `head_sha` into `dev_pr_lock`."""
    vcs = GitVcsIntegrationClient()
    target_dir = Path(workdir).expanduser().resolve() if workdir else vcs.resolve_repo_dir()
    if not target_dir.is_dir():
        return {"status": "error", "passed": False, "error": f"Workdir not found: {target_dir}"}

    clean_env = _clean_git_subprocess_env(
        {
            "AGY_OFFLINE_ONLY": "1",
            "PRE_COMMIT_HOOK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    raw_changed_files = _detect_validation_changed_files(target_dir, env=clean_env)

    # Single-pass pre-commit execution FIRST (skipping duplicate test, static-audit & BATS hooks)
    pre_commit_entry: dict[str, Any] = {"status": "not_run", "passed": None}
    pre_commit_status_label = "NOT_RUN"
    pre_commit_output = ""
    if run_pre_commit:
        if (target_dir / ".pre-commit-config.yaml").is_file() or (target_dir / ".pre-commit-config.yml").is_file():
            pc_bin = shutil.which("pre-commit")
            if pc_bin:
                skip_val, skip_hooks = _build_single_pass_pre_commit_skip(target_dir, clean_env.get("SKIP", ""))
                pc_env = {**clean_env, "SKIP": skip_val}
                pc_cmd, pc_mode, pc_files = _build_single_pass_pre_commit_cmd(
                    pc_bin,
                    target_dir,
                    raw_changed_files,
                    run_all=run_all,
                )
                with _temporarily_index_untracked_files(target_dir, env=clean_env):
                    proc_pc = subprocess.run(
                        pc_cmd,
                        cwd=str(target_dir),
                        env=pc_env,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                pc_ok = proc_pc.returncode == 0
                pre_commit_output = f"{proc_pc.stdout}\n{proc_pc.stderr}".strip()
                pre_commit_status_label = "PASSED" if pc_ok else "FAILED"
                pre_commit_entry = {
                    "status": "pass" if pc_ok else "fail",
                    "passed": pc_ok,
                    "mode": pc_mode,
                    "files_count": len(pc_files),
                    "skipped_hooks": skip_hooks,
                }
            else:
                pre_commit_status_label = "SKIPPED"
                pre_commit_entry = {"status": "skipped", "passed": None}
        else:
            pre_commit_status_label = "SKIPPED"
            pre_commit_entry = {"status": "skipped", "passed": None}
    elif pre_commit_status.strip():
        norm_short, norm_upper, norm_passed = normalize_pre_commit_status(pre_commit_status)
        pre_commit_status_label = norm_upper
        pre_commit_entry = {"status": norm_short, "passed": norm_passed}

    qa_script = target_dir / "tests" / "scripts" / "qa_static_audit.py"
    qa_ran = qa_script.is_file()
    qa_passed = True
    qa_output = ""
    if qa_ran:
        proc_qa = subprocess.run(
            [sys.executable, str(qa_script), "--incremental"],
            cwd=str(target_dir),
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        qa_passed = proc_qa.returncode == 0
        qa_output = f"{proc_qa.stdout}\n{proc_qa.stderr}".strip()

    targets = list(test_targets or [])
    selective_runner = target_dir / "tests" / "scripts" / "selective_test_runner.py"
    tests_dir = target_dir / "tests"
    unittest_ran = True
    tests_passed = True
    test_output = ""
    if targets:
        cmd = [sys.executable, "-m", "unittest", *targets]
    elif selective_runner.is_file():
        cmd = [sys.executable, str(selective_runner), *(["--all"] if run_all else [])]
    elif tests_dir.is_dir():
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
    else:
        cmd = []
        unittest_ran = False

    if cmd:
        proc_test = subprocess.run(
            cmd,
            cwd=str(target_dir),
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        tests_passed = proc_test.returncode == 0
        test_output = f"{proc_test.stdout}\n{proc_test.stderr}".strip()

    # Capture BATS status either from selective_test_runner TAP output or direct execution when needed
    bats_files = sorted((target_dir / "tests" / "bats").glob("*.bats")) if (target_dir / "tests" / "bats").is_dir() else []
    bats_ran = False
    bats_passed = True
    bats_output = ""
    effective_run_bats = bool(run_bats) and _resolve_selective_should_run_bats(
        selective_runner,
        raw_changed_files,
        run_all=run_all,
    )
    if re.search(r"(?m)^1\.\.\d+\s*$", test_output):
        bats_ran = True
        bats_passed = tests_passed and not bool(re.search(r"(?m)^not ok\s+\d+\b", test_output))
        bats_output = test_output
    elif effective_run_bats and not targets and bats_files:
        bats_bin = shutil.which("bats")
        if bats_bin:
            bats_ran = True
            proc_bats = subprocess.run(
                [bats_bin, *(str(p) for p in bats_files)],
                cwd=str(target_dir),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            bats_passed = proc_bats.returncode == 0
            bats_output = f"{proc_bats.stdout}\n{proc_bats.stderr}".strip()

    wt_state = _snapshot_worktree_state(target_dir)
    # Preserve previously recorded pre_commit pass/fail if same code fingerprint
    existing_lock = get_dev_pr_lock(portfolio_dir=portfolio_dir, workdir=target_dir)
    if pre_commit_entry["status"] == "not_run" and existing_lock.get("active"):
        prev_vr = existing_lock.get("verification_results")
        if (
            isinstance(prev_vr, dict)
            and prev_vr.get("content_fingerprint") == wt_state["content_fingerprint"]
            and isinstance(prev_vr.get("pre_commit"), dict)
            and prev_vr["pre_commit"].get("status") in ("pass", "fail")
        ):
            pre_commit_entry = dict(prev_vr["pre_commit"])
            _, pre_commit_status_label, _ = normalize_pre_commit_status(str(pre_commit_entry.get("status") or ""))

    pre_commit_passed = pre_commit_entry.get("passed")
    overall_passed = bool(qa_passed and tests_passed and bats_passed and (pre_commit_passed is not False))

    verification_results: dict[str, Any] = {
        "head_sha": wt_state["head_sha"],
        "tree_sha": wt_state["tree_sha"],
        "dirty": wt_state["dirty"],
        "content_fingerprint": wt_state["content_fingerprint"],
        "verified_at_utc": _utc_now_iso(),
        "overall_passed": overall_passed,
        "pre_commit_status": pre_commit_status_label,
        "qa_static_audit": _parse_qa_static_audit_summary(qa_output, qa_passed, qa_ran),
        "unittest": _parse_unittest_summary(test_output, tests_passed, unittest_ran),
        "bats": _parse_bats_summary(bats_output, bats_passed, bats_ran),
        "pre_commit": pre_commit_entry,
    }

    if existing_lock.get("active"):
        with contextlib.suppress(Exception):
            update_dev_pr_lock(
                {"verification_results": verification_results},
                portfolio_dir=portfolio_dir,
                workdir=target_dir,
                branch_name=str(existing_lock.get("branch_name") or ""),
                slot_id=int(existing_lock.get("slot_id") or 1),
            )

    return {
        "status": "ok" if overall_passed else "error",
        "passed": overall_passed,
        "qa_static_passed": qa_passed,
        "unit_tests_passed": tests_passed,
        "bats_passed": bats_passed,
        "pre_commit_passed": pre_commit_passed,
        "pre_commit_status": pre_commit_status_label,
        "head_sha": wt_state["head_sha"],
        "workdir": str(target_dir),
        "test_targets": targets,
        "verification_results": verification_results,
        "qa_output": qa_output[-1500:],
        "test_output": test_output[-2500:],
        "pre_commit_output": pre_commit_output[-2000:] if pre_commit_output else "",
    }


# ==============================================================================
# 3-Agent Orchestration Plans (`agy-code-developer` & `agy-code-pr-reviewer`)
# ==============================================================================


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
) -> dict[str, Any]:
    """Build the `subagent_definition` and `subagent_invocation` for `agy-code-developer`.

    Supports optional model tiering (`flash` for bounded/pre-anchored tasks or `pro` [default]
    for complex multi-module/security work) while keeping `agy-code-pr-reviewer` on `pro`.
    Executes Local-First Review staging using pure native `git` (RCA -> Create Timestamped Branch & Acquire per-repo `dev_pr_locks` entry ->
    Propose Changes on Branch & Run Local Unit Tests -> Stage Locally via `push-branch`/`prepare-review` WITHOUT pushing to `origin`)
    and returns the branch details + `agy-code-pr-reviewer` invocation plan.
    """
    cid_clean = str(conversation_id or "").strip()
    files_list = [str(f).strip() for f in (target_files or []) if str(f).strip()]
    sib_list = [str(s).strip() for s in (sibling_ids or []) if str(s).strip()]
    resolved_model = str(model or "").strip().lower() or PR_SUBAGENT_MODEL
    if resolved_model not in ("pro", "flash", "inherit", "flash_lite"):
        resolved_model = PR_SUBAGENT_MODEL
    sched_flag = " --scheduled" if scheduled else ""
    files_arg = (" --target-files " + " ".join(f'"{f}"' for f in files_list)) if files_list else ""
    sib_arg = (" --sibling-ids " + " ".join(sib_list)) if sib_list else ""
    workdir_arg = f' --workdir "{workdir.strip()}"' if str(workdir or "").strip() else ""
    repo_line = f"- **Target Repository**: `{repo_slug.strip()}`\n" if str(repo_slug or "").strip() else ""
    anchored_clean = str(anchored_context or "").strip()

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
        f"6. Return the JSON output (including `branch_name`, `branch_timestamp`, `staged_head_sha`, `reviewer_subagent_definition`, and `reviewer_subagent_invocation`) so `{PR_REVIEWER_SUBAGENT_NAME}` (`Model=\"pro\"`) can audit locally and publish ONLY upon `PASS (>90/100)`."
    )

    task_prompt = (
        f"A code/skill change requires a native `git` timestamped branch proposal:\n"
        + repo_line
        + f"- **Incident / Conversation ID**: `{cid_clean}`\n"
        f"- **Action / Trigger ID**: `{action_id or 'code-change'}`\n"
        + (f"- **Sibling Conversation IDs**: `{', '.join(sib_list)}`\n" if sib_list else "")
        + (f"- **Initial RCA / Rationale**: {rca_summary}\n" if rca_summary else "")
        + (f"- **Initial Proposed Solution**: {proposed_fix}\n" if proposed_fix else "")
        + (f"- **Target Files**: `{', '.join(files_list)}`\n" if files_list else "")
        + (f"- **Pre-Anchored Context & Contracts**:\n{anchored_clean}\n" if anchored_clean else "")
        + "\nExecute the Local-First Native Git branch workflow now:\n"
        f"1. Run `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py start-remediation "
        f"--id {cid_clean} --action-id \"{action_id or 'code-change'}\"{workdir_arg} "
        f"--rca-summary \"<RCA_SUMMARY>\" --proposed-fix \"<PROPOSED_FIX>\"{files_arg}{sib_arg} --worktree`.\n"
        f"   - If `status == \"locked\"`, abort immediately and return the lock status.\n"
        f"2. Implement modular fixes and hermetic unit tests on the worktree branch, iterate with targeted `python3 -m unittest <target>`, then run single-pass `pr_remediator.py test-local --workdir <WORKTREE_PATH> --with-pre-commit` until `passed == true`.\n"
        f"3. Stage locally for review (without pushing to origin yet) via `pr_remediator.py prepare-review --workdir <WORKTREE_PATH> --title \"fix: remediate {action_id or cid_clean[:8]}\" --executive-summary \"...\"{sched_flag}`.\n"
        f"4. Return the full JSON result so `{PR_REVIEWER_SUBAGENT_NAME}` can immediately audit the local branch and publish only on `PASS (>90/100)`."
    )

    return {
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
) -> dict[str, Any]:
    """Build the `subagent_definition` and `subagent_invocation` for `agy-code-pr-reviewer` (`Model="pro"`)."""
    _ = pr_number, issue_number, scheduled, pr_url, issue_url
    resolved_ts = branch_timestamp or ""
    wd_flag = f' --workdir "{workdir.strip()}"' if str(workdir or "").strip() else ""
    repo_line = f"- **Target Repository**: `{repo_slug.strip()}`\n" if str(repo_slug or "").strip() else ""
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
        f"3. Record your structured verdict via `record-review` (which publishes `origin/<branch>`, starts `watch-pr-merge`, and dispatches `[🟩]` email ONLY if `PASS` and `score > 90`; otherwise keeps the branch local and returns `developer_remediation_plan` for auto-iteration up to {MAX_PR_REVIEW_ITERATIONS} rounds):\n"
        f"   `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py record-review --branch <BRANCH_NAME>{wd_flag} --verdict <PASS|WARN|NEEDS_REVISION> --score <0-100> --summary \"...\" --send-email`\n"
        f"4. Return the JSON evaluation summary (`ready_for_human_pr`, `needs_reiteration`, `manual_pr_url` only when `ready_for_human_pr == true`, or `developer_remediation_plan` otherwise)."
    )

    task_prompt = (
        f"Evaluate the locally staged timestamped branch `{branch_name}` (Timestamp: `{resolved_ts}`):\n"
        + repo_line
        + f"- **Conversation ID**: `{conversation_id}`\n"
        f"- **Proposed Branch**: `{branch_name}` (Local-First Review — NOT yet published to `origin`)\n"
        f"- **Branch Timestamp**: `{resolved_ts}`\n"
        f"- **Root Cause / Rationale**: {rca_summary}\n"
        f"- **Proposed Solution**: {proposed_fix}\n\n"
        f"Execute the following steps:\n"
        f"1. Run `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py branch-diff --branch \"{branch_name}\"{wd_flag}` to inspect the exact local diff and files changed via native `git`.\n"
        f"2. Score the branch changes (0-100) across Resolution Accuracy, Unit Tests, Zero-PII Security, and Code Quality, and give your verdict (`PASS`, `WARN`, or `NEEDS_REVISION`).\n"
        f"3. Run `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py record-review "
        f"--branch \"{branch_name}\"{wd_flag} --verdict <PASS|WARN|NEEDS_REVISION> --score <SCORE> "
        f"--summary \"<Your detailed technical review comment>\" --send-email`.\n"
        f"4. Return the full JSON evaluation summary (never surface any PR URL unless `ready_for_human_pr == true`)."
    )

    return {
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
            "Model": PR_SUBAGENT_MODEL,
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
        pro_url = build_pro_dashboard_ui_url("health", ensure_server=False)
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
    dev_preview_url = ""
    pro_url = build_pro_dashboard_ui_url("health", portfolio_dir=portfolio_dir, ensure_server=False)
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
    if slot_num == 1:
        if has_preview_bundle:
            dev_server_res = stop_dev_api_server(portfolio_dir=portfolio_dir)
            preview_dir.mkdir(parents=True, exist_ok=True)
            for asset_name in ("index.html", "styles.css", "dashboard.js", "favicon.svg"):
                src_asset = web_src_dir / asset_name
                if src_asset.is_file():
                    shutil.copy2(src_asset, preview_dir / asset_name)
            if api_src_dir.is_dir():
                shutil.copytree(
                    api_src_dir,
                    preview_dir / "api",
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
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

    if slot_num == 1 and (has_preview_bundle or has_staged_plugin):
        for asset_name in ("index.html", "styles.css", "dashboard.js", "favicon.svg"):
            staged_asset = preview_dir / asset_name
            if staged_asset.is_file():
                with contextlib.suppress(OSError):
                    os.utime(staged_asset, None)
        restart_pro_api_server(portfolio_dir=portfolio_dir)

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
        "verification_results": valid_vr,
        "lock": updated_lock.get("lock", {}),
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
) -> dict[str, Any]:
    """Build a structured remediation brief so the orchestrator can auto-iterate `agy-code-developer`."""
    anchored = (
        f"Review Round {review_iteration}/{MAX_PR_REVIEW_ITERATIONS} FAILED PR Readiness Gate "
        f"({verdict} {score}/100 — requires PASS >= {PR_REVIEW_PASS_MIN_SCORE}/100).\n"
        f"- Worktree / Branch: `{workdir}` on `{branch_name}` (reuse active lock/worktree).\n"
        f"- Reviewer Critique to Fix:\n{reviewer_feedback}\n"
        f"- Required Steps: (1) address all reviewer findings in `{workdir}`, (2) add/update hermetic tests, "
        f"(3) run `pre-commit run --all-files` + `pr_remediator.py test-local --workdir \"{workdir}\"`, "
        f"(4) re-stage via `pr_remediator.py prepare-review --workdir \"{workdir}\" --title \"...\"`, "
        f"(5) re-invoke `agy-code-pr-reviewer` (`Model=\"pro\"`)."
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
    )
    return {
        "conversation_id": conversation_id,
        "action_id": action_id,
        "repo_slug": repo_slug,
        "branch_name": branch_name,
        "workdir": _sanitize_home_paths(workdir),
        "review_iteration": review_iteration,
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
            "Re-invoke independent `agy-code-pr-reviewer` (`Model=\"pro\"`) until `ready_for_human_pr == true`.",
        ],
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
      publishes rich `manual_pr_url`, transitions queue to `pr_open`, spawns `watch-pr-merge`, and sends `[🟩]` email.
    """
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
    review_entry = {
        "iteration": current_iteration,
        "verdict": norm_verdict,
        "score": score_int,
        "gate_passed": gate_passed,
        "summary": _sanitize_home_paths(summary.strip()),
        "reviewer_subagent_id": reviewer_subagent_id.strip(),
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

    created_by_label = f"{PR_DEVELOPER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL}) · {PR_REVIEWER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL})"
    validator_banner_label = (
        f"{PR_REVIEWER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL}) [{norm_verdict} {score_int}/100 · Round {current_iteration}/{MAX_PR_REVIEW_ITERATIONS}]"
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
        )

        review_markdown = (
            f"### {'🚨' if circuit_breaker_tripped else '🔁'} Branch Review Gate NOT Passed (`{PR_REVIEWER_SUBAGENT_NAME}` · `{PR_SUBAGENT_MODEL}`)\n\n"
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
        # When Slot #2 passes review while Slot #1 is still active on the repo, DO NOT push to origin,
        # DO NOT surface `manual_pr_url`, and DO NOT email until Slot #1 merges + rebase-reverify passes!
        if is_slot2_branch:
            head_sha = amended_head_sha
            review_entry["head_sha"] = head_sha
            updated_history[-1] = review_entry
            slot1_br = str(lock_info.get("slot1_branch_name") or "")
            slot2_md = (
                f"### ⏳ Slot #2 Validated Locally — Awaiting Slot #1 Base Merge (`{PR_REVIEWER_SUBAGENT_NAME}` · `{PR_SUBAGENT_MODEL}`)\n\n"
                f"- **Slot**: `2` (behind active Slot #1 `{slot1_br}`)\n"
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
                    "deferred_send_email": bool(send_email),
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
                            f"Slot #2 passed {PR_REVIEWER_SUBAGENT_NAME} ({norm_verdict} {score_int}/100) locally; "
                            f"{SLOT_STATUS_READY_AWAITING_BASE_MERGE} behind Slot #1 ({slot1_br})."
                        ),
                        portfolio_dir=portfolio_dir,
                    )
            return {
                "status": "ok",
                "vcs_mode": "native_git",
                "repo_slug": resolved_repo_slug,
                "slot_id": 2,
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
        f"### 🛡️ New Branch Validated & Published (`{PR_REVIEWER_SUBAGENT_NAME}` · `{PR_SUBAGENT_MODEL}`)\n\n"
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

    email_sent = False
    email_subject = (
        f"PR Ready for Validation [{repo_name}]: {summary_or_branch} ({norm_verdict} {score_int}/100)"
    )

    if send_email:
        email_body = (
            f"# 🌿 New Branch Validated & Published by `{PR_DEVELOPER_SUBAGENT_NAME}` / `{PR_REVIEWER_SUBAGENT_NAME}`: `{resolved_branch}` (`{resolved_ts}`)\n\n"
            f"A **new timestamped Git branch** **`{resolved_branch}`** (`{resolved_ts}`) passed independent review and has been published via native Git "
            f"(`git_client.py` — never merged directly into `main`) following implementation by **`{PR_DEVELOPER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL})`** "
            f"and independent review by **`{PR_REVIEWER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL})`**:\n\n"
            f"- **Branch Action**: **NEW BRANCH CREATED (`origin/{resolved_branch}`)**\n"
            f"- **New Branch Created**: `{resolved_branch}`"
            + (f" (`{head_sha[:8]}`)" if head_sha else "")
            + "\n"
            f"- **Branch Timestamp**: `{resolved_ts}`\n"
            f"- **Created & Validated By**: `{created_by_label}`\n"
            f"- **Compare with `main` & Create Manual PR**: [{resolved_branch}]({manual_pr_url})\n"
            f"- **Conversation ID**: `{cid_clean}`\n"
            + (f"- **Root Cause (RCA)**: {rca_summary}\n" if rca_summary else "")
            + (f"- **Proposed Solution**: {proposed_fix}\n" if proposed_fix else "")
            + f"- **Review Verdict (`{PR_REVIEWER_SUBAGENT_NAME}`)**: **{badge} ({score_int}/100 · Round {current_iteration}/{MAX_PR_REVIEW_ITERATIONS})**\n"
            f"- **Evaluation Details**: {summary.strip()}\n"
            f"- **Concurrency Lock (`dev_pr_lock`)**: `ACTIVE (PENDING_HUMAN_PR_VALIDATION)` — "
            f"No other code changes are permitted in `{resolved_repo_slug}` until you evaluate this newly created branch.\n\n"
            f"{canonical_pr_body}\n"
            f"## 🚀 How to Compare with `main`, Create the Manual Pull Request & Release the Lock\n"
            f"1. Compare the newly created branch against `main` and create the Pull Request manually at [{manual_pr_url}]({manual_pr_url}) (or run `git fetch origin && git diff origin/main...origin/{resolved_branch}`).\n"
            f"2. To approve the branch after creating/merging the PR and unlock new changes:\n"
            f"   `pr_remediator.py evaluate-branch --branch \"{resolved_branch}\" --decision approve --comment \"Manual PR created and validated\"`\n"
            f"3. To reject the branch (deletes `origin/{resolved_branch}` with `git push origin --delete` and releases the lock):\n"
            f"   `pr_remediator.py evaluate-branch --branch \"{resolved_branch}\" --decision reject --comment \"Branch rejected\"`"
        )
        notify_res = dev_common.try_send_branch_review_notification(
            status="OK",
            subject=email_subject,
            body=email_body,
            conversation_id=cid_clean or resolved_ts,
            validator=validator_banner_label,
            branch_name=resolved_branch,
            target_url=notification_target_url,
            errors_only_override=False,
        )
        email_sent = bool(notify_res.get("sent"))

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
        spawn_pr_merge_watcher_daemon(portfolio_dir=portfolio_dir)
        if lock_info.get("active")
        else {"status": "skipped", "spawned": False}
    )

    frontend_changed = bool(lock_info.get("frontend_changed")) or is_frontend_change_detected(files_changed)
    dev_preview_url = ""
    pro_url = str(
        lock_info.get("pro_url")
        or build_pro_dashboard_ui_url("health", portfolio_dir=portfolio_dir, ensure_server=False)
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


# ==============================================================================
# Persistent Orchestrator Dev Queue (`dev_queue.json`) & Safe Post-Merge Cleanup
# ==============================================================================

DEV_QUEUE_FILENAME = "dev_queue.json"
DEV_QUEUE_STATUSES: tuple[str, ...] = (
    "pending",
    "in_progress",
    "pr_open",
    "merged",
    "standby",
    "hitl_blocked",
    "cleaned",
    "cancelled",
    "rejected",
)
DEV_QUEUE_DONE_STATUSES: tuple[str, ...] = ("merged", "cleaned", "rejected", "cancelled")
DEV_QUEUE_HOLD_STATUSES: tuple[str, ...] = ("standby", "hitl_blocked")
DEV_QUEUE_STATUS_RANK: dict[str, int] = {
    "in_progress": 0,
    "pr_open": 1,
    "pending": 2,
    "hitl_blocked": 3,
    "standby": 4,
    "merged": 5,
    "cleaned": 6,
    "cancelled": 7,
    "rejected": 7,
}
_CONVERSATION_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


def _atomic_write_json_0600(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON with strict 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)


def _normalize_str_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """Deduplicate non-empty strings while preserving order and sanitizing home paths."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in values or ():
        clean = _sanitize_home_paths(str(raw or "").strip())
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _matches_cid(cand: str, ref: str) -> bool:
    """Return True if two conversation IDs match exactly or share a unique >=8-char prefix."""
    c = str(cand or "").strip().lower()
    r = str(ref or "").strip().lower()
    if not c or not r:
        return False
    if c == r:
        return True
    short = min(len(c), len(r))
    return short >= 8 and (c.startswith(r) or r.startswith(c))


def get_dev_queue_path(portfolio_dir: Path | str | None = None) -> Path:
    """Return the path to `dev_queue.json` under the portfolio data directory."""
    return dev_common.resolve_state_dir(portfolio_dir=portfolio_dir) / DEV_QUEUE_FILENAME


def _empty_dev_queue(now_iso: str = "") -> dict[str, Any]:
    """Return a clean empty `dev_queue.json` document."""
    return {
        "schema_version": 1,
        "orchestrator_conversation_id": "",
        "updated_at": now_iso or _utc_now_iso(),
        "items": [],
    }


def _find_queue_item_index(items: list[dict[str, Any]], conversation_id: str) -> int:
    """Find the index of `conversation_id` in queue items via exact or unique >=8-char prefix match."""
    cid_clean = str(conversation_id or "").strip()
    if not cid_clean:
        return -1
    for idx, item in enumerate(items):
        if str(item.get("conversation_id") or "").strip().lower() == cid_clean.lower():
            return idx
    if len(cid_clean) >= 8:
        prefix_matches = [
            idx
            for idx, item in enumerate(items)
            if _matches_cid(str(item.get("conversation_id") or ""), cid_clean)
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
    return -1


def _sort_dev_queue_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort queue items deterministically by lifecycle bucket, numeric priority, added_at, and CID."""
    def _prio_val(it: dict[str, Any]) -> int:
        try:
            return int(it.get("priority", 50))
        except (TypeError, ValueError):
            return 50

    return sorted(
        items,
        key=lambda it: (
            DEV_QUEUE_STATUS_RANK.get(str(it.get("status") or "pending").lower(), 9),
            _prio_val(it),
            str(it.get("added_at") or ""),
            str(it.get("conversation_id") or ""),
        ),
    )


def get_dev_queue_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count items in `dev_queue.json` by status."""
    counts: dict[str, int] = {st: 0 for st in DEV_QUEUE_STATUSES}
    counts["total"] = len(items)
    for it in items:
        st = str(it.get("status") or "pending").lower()
        counts[st] = counts.get(st, 0) + 1
    return counts


def load_dev_queue(portfolio_dir: Path | str | None = None) -> dict[str, Any]:
    """Load `dev_queue.json` safely with normalized schema and sorted items."""
    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    raw = dev_common.atomic_read_json(q_path, default={})
    if not isinstance(raw, dict) or not raw:
        data = _empty_dev_queue()
    else:
        items = [dict(it) for it in (raw.get("items") or []) if isinstance(it, dict) and str(it.get("conversation_id") or "").strip()]
        data = {
            "schema_version": int(raw.get("schema_version") or 1),
            "orchestrator_conversation_id": str(raw.get("orchestrator_conversation_id") or "").strip(),
            "updated_at": str(raw.get("updated_at") or _utc_now_iso()),
            "items": _sort_dev_queue_items(items),
        }
    data["summary"] = get_dev_queue_summary(data["items"])
    data["queue_path"] = str(q_path)
    return data


def _read_dev_queue_locked(q_path: Path) -> dict[str, Any]:
    """Read raw `dev_queue.json` while holding its file lock."""
    if not q_path.is_file():
        return _empty_dev_queue()
    try:
        raw = json.loads(q_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_dev_queue()
    if not isinstance(raw, dict):
        return _empty_dev_queue()
    items = [dict(it) for it in (raw.get("items") or []) if isinstance(it, dict) and str(it.get("conversation_id") or "").strip()]
    return {
        "schema_version": int(raw.get("schema_version") or 1),
        "orchestrator_conversation_id": str(raw.get("orchestrator_conversation_id") or "").strip(),
        "updated_at": str(raw.get("updated_at") or _utc_now_iso()),
        "items": items,
    }


def set_dev_queue_orchestrator(
    orchestrator_conversation_id: str,
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Persist the active coordinator conversation ID in `dev_queue.json` for restart recovery and cleanup protection."""
    orch_clean = str(orchestrator_conversation_id or "").strip()
    if not orch_clean:
        return {"status": "error", "error": "orchestrator_conversation_id is required"}
    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    with dev_common.file_lock(q_path.with_suffix(".json.lock")):
        data = _read_dev_queue_locked(q_path)
        data["orchestrator_conversation_id"] = orch_clean
        data["updated_at"] = _utc_now_iso()
        data["items"] = _sort_dev_queue_items(data["items"])
        _atomic_write_json_0600(q_path, data)
    return {
        "status": "ok",
        "queue_path": str(q_path),
        "orchestrator_conversation_id": orch_clean,
        "summary": get_dev_queue_summary(data["items"]),
    }


def enqueue_dev_queue_item(
    conversation_id: str,
    title: str = "",
    action_id: str = "",
    priority: int = 50,
    repo_slugs: list[str] | None = None,
    status: str = "pending",
    depends_on: list[str] | None = None,
    rca_summary: str = "",
    proposed_fix: str = "",
    target_files: list[str] | None = None,
    branch_names: list[str] | None = None,
    pr_urls: list[str] | None = None,
    subagent_conversation_ids: list[str] | None = None,
    notes: str = "",
    orchestrator_conversation_id: str = "",
    source: str = "manual",
    blocked_by_lock_id: str = "",
    portfolio_dir: Path | str | None = None,
    *,
    section_anchors: list[str] | None = None,
) -> dict[str, Any]:
    """Upsert a development task entry into persistent `dev_queue.json` (`0600` + `file_lock`)."""
    cid_clean = str(conversation_id or "").strip()
    if not cid_clean:
        return {"status": "error", "error": "conversation_id is required"}
    norm_status = str(status or "pending").strip().lower()
    if norm_status not in DEV_QUEUE_STATUSES:
        return {
            "status": "error",
            "error": f"Invalid queue status '{status}'. Expected one of {DEV_QUEUE_STATUSES}.",
        }
    try:
        norm_priority = int(priority)
    except (TypeError, ValueError):
        norm_priority = 50

    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    now_iso = _utc_now_iso()
    with dev_common.file_lock(q_path.with_suffix(".json.lock")):
        data = _read_dev_queue_locked(q_path)
        items: list[dict[str, Any]] = list(data["items"])
        if orchestrator_conversation_id.strip():
            data["orchestrator_conversation_id"] = orchestrator_conversation_id.strip()

        idx = _find_queue_item_index(items, cid_clean)
        created = idx < 0
        existing = dict(items[idx]) if idx >= 0 else {}
        existing_status = str(existing.get("status") or "").lower()

        if created:
            resolved_status = norm_status
            resolved_prio = norm_priority
        elif source == "ad_hoc_lock":
            resolved_status = existing_status if existing_status in ("merged", "cleaned") else norm_status
            resolved_prio = int(existing.get("priority", norm_priority))
        elif source == "auto_lock" and existing_status in (*DEV_QUEUE_HOLD_STATUSES, "in_progress", "pr_open"):
            resolved_status = existing_status
            resolved_prio = int(existing.get("priority", norm_priority))
        elif source == "auto_lock":
            resolved_status = existing_status or norm_status
            resolved_prio = min(int(existing.get("priority", norm_priority)), norm_priority)
        else:
            resolved_status = norm_status
            resolved_prio = norm_priority

        existing_title = str(existing.get("title") or "").strip()
        if source == "ad_hoc_lock" and existing_title:
            resolved_title = existing_title
        else:
            resolved_title = title.strip() or existing_title or action_id.strip() or cid_clean[:8]

        item: dict[str, Any] = {
            "conversation_id": str(existing.get("conversation_id") or cid_clean),
            "title": resolved_title,
            "action_id": action_id.strip() or str(existing.get("action_id") or ""),
            "priority": resolved_prio,
            "status": resolved_status,
            "repo_slugs": _normalize_str_list([*(existing.get("repo_slugs") or []), *(repo_slugs or [])]),
            "depends_on": _normalize_str_list([*(existing.get("depends_on") or []), *(depends_on or [])]),
            "rca_summary": _sanitize_home_paths(rca_summary.strip()) or str(existing.get("rca_summary") or ""),
            "proposed_fix": _sanitize_home_paths(proposed_fix.strip()) or str(existing.get("proposed_fix") or ""),
            "target_files": _normalize_str_list([*(existing.get("target_files") or []), *(target_files or [])]),
            "branch_names": _normalize_str_list([*(existing.get("branch_names") or []), *(branch_names or [])]),
            "pr_urls": _dedupe_richest_pr_urls([*(existing.get("pr_urls") or []), *(pr_urls or [])]),
            "subagent_conversation_ids": _normalize_str_list(
                [*(existing.get("subagent_conversation_ids") or []), *(subagent_conversation_ids or [])]
            ),
            "notes": _sanitize_home_paths(notes.strip()) or str(existing.get("notes") or ""),
            "source": str(existing.get("source") or source or "manual"),
            "blocked_by_lock_id": blocked_by_lock_id.strip() or str(existing.get("blocked_by_lock_id") or ""),
            "added_at": str(existing.get("added_at") or now_iso),
            "updated_at": now_iso,
        }
        merged_anchors = _normalize_str_list([*(existing.get("section_anchors") or []), *(section_anchors or [])])
        if merged_anchors:
            item["section_anchors"] = merged_anchors
        if existing.get("merged_repos"):
            item["merged_repos"] = _normalize_str_list(
                [dev_common.normalize_repo_slug(str(r)) for r in existing.get("merged_repos") or [] if str(r).strip()]
            )
        if idx >= 0:
            items[idx] = item
        else:
            items.append(item)
        data["items"] = _sort_dev_queue_items(items)
        data["updated_at"] = now_iso
        _atomic_write_json_0600(q_path, data)

    return {
        "status": "ok",
        "created": created,
        "queue_path": str(q_path),
        "item": item,
        "summary": get_dev_queue_summary(data["items"]),
    }


def _sync_active_lock_to_dev_queue(
    *,
    conversation_id: str,
    status: str = "in_progress",
    title: str = "",
    action_id: str = "",
    repo_slug: str = "",
    branch_name: str = "",
    pr_urls: list[str] | None = None,
    subagent_conversation_ids: list[str] | None = None,
    rca_summary: str = "",
    proposed_fix: str = "",
    target_files: list[str] | None = None,
    section_anchors: list[str] | None = None,
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Ensure every active `dev_pr_lock` (including ad-hoc runs) is tracked in `dev_queue.json`."""
    cid_clean = str(conversation_id or "").strip()
    if not cid_clean:
        return {"status": "skipped", "reason": "missing_conversation_id"}
    norm_slug = dev_common.normalize_repo_slug(repo_slug)
    repo_list = [norm_slug] if (norm_slug and norm_slug != "default") else []
    clean_title = str(title or "").strip() or str(action_id or "").strip() or str(rca_summary or "").strip()[:96]
    return enqueue_dev_queue_item(
        conversation_id=cid_clean,
        title=clean_title,
        action_id=action_id,
        priority=15,
        repo_slugs=repo_list,
        status=status,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        target_files=target_files,
        section_anchors=section_anchors,
        branch_names=[branch_name] if str(branch_name or "").strip() else None,
        pr_urls=pr_urls,
        subagent_conversation_ids=subagent_conversation_ids,
        source="ad_hoc_lock",
        portfolio_dir=portfolio_dir,
    )


def update_dev_queue_item_status(
    conversation_id: str,
    status: str,
    branch_names: list[str] | None = None,
    pr_urls: list[str] | None = None,
    subagent_conversation_ids: list[str] | None = None,
    notes: str | None = None,
    orchestrator_conversation_id: str = "",
    portfolio_dir: Path | str | None = None,
    *,
    strip_pr_urls_for_branches: list[str] | None = None,
    strip_repo_slug: str = "",
) -> dict[str, Any]:
    """Transition an existing `dev_queue.json` entry to `status`, append artifacts, and deduplicate `pr_urls` keeping the richest verified URL per `(repo_slug, branch_name)`."""
    cid_clean = str(conversation_id or "").strip()
    norm_status = str(status or "").strip().lower()
    if norm_status not in DEV_QUEUE_STATUSES:
        return {
            "status": "error",
            "updated": False,
            "error": f"Invalid queue status '{status}'. Expected one of {DEV_QUEUE_STATUSES}.",
        }
    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    now_iso = _utc_now_iso()
    with dev_common.file_lock(q_path.with_suffix(".json.lock")):
        data = _read_dev_queue_locked(q_path)
        items = list(data["items"])
        if orchestrator_conversation_id.strip():
            data["orchestrator_conversation_id"] = orchestrator_conversation_id.strip()
        idx = _find_queue_item_index(items, cid_clean)
        if idx < 0:
            return {
                "status": "error",
                "updated": False,
                "error": f"Queue item `{cid_clean}` not found in `{q_path}`.",
            }
        item = dict(items[idx])
        item["status"] = norm_status
        item["branch_names"] = _normalize_str_list([*(item.get("branch_names") or []), *(branch_names or [])])
        item["pr_urls"] = _dedupe_richest_pr_urls(
            [*(item.get("pr_urls") or []), *(pr_urls or [])],
            strip_branches=strip_pr_urls_for_branches,
            strip_repo_slug=strip_repo_slug,
        )
        item["subagent_conversation_ids"] = _normalize_str_list(
            [*(item.get("subagent_conversation_ids") or []), *(subagent_conversation_ids or [])]
        )
        if notes is not None and notes.strip():
            item["notes"] = _sanitize_home_paths(notes.strip())
        item["updated_at"] = now_iso
        items[idx] = item
        data["items"] = _sort_dev_queue_items(items)
        data["updated_at"] = now_iso
        _atomic_write_json_0600(q_path, data)
    return {
        "status": "ok",
        "updated": True,
        "queue_path": str(q_path),
        "item": item,
        "summary": get_dev_queue_summary(data["items"]),
    }


_STALE_PR_COMPARE_MARKERS = (
    "/compare/",
    "/-/merge_requests/new",
    "/pull-requests/new",
)
_CREATED_PR_URL_RE = re.compile(
    r"^(.*?)/(?:pull|pull-requests|-/merge_requests)/(\d+)(?:/|$)",
    re.IGNORECASE,
)
_MERGED_LOCK_RELEASE_REASONS = ("BRANCH_MERGED_INTO_MAIN", "HUMAN_VALIDATED_MANUAL_PR")


def _extract_created_pr_repo_and_number(raw_url: str) -> tuple[str, str]:
    """Return `(normalized_repo_slug, pr_number)` if `raw_url` is a created PR/MR URL, else `('', '')`."""
    url_str = str(raw_url or "").strip()
    if not url_str:
        return "", ""
    parsed = urllib.parse.urlsplit(url_str)
    path = parsed.path or ""
    m = _CREATED_PR_URL_RE.match(path)
    if not m:
        return "", ""
    parts = [p for p in m.group(1).strip("/").split("/") if p]
    slug = dev_common.normalize_repo_slug(f"{parts[-2]}/{parts[-1]}") if len(parts) >= 2 else ""
    return slug, m.group(2)


def _score_pr_url_richness(raw_url: str) -> tuple[int, int]:
    """Rank PR/Compare URL richness: created `/pull/` (400) > verified PASS compare with title+body (300) > compare with title+body (200) > partial compare (150) > bare compare (100)."""
    url_str = str(raw_url or "").strip()
    if not url_str:
        return (0, 0)
    pr_slug, pr_num = _extract_created_pr_repo_and_number(url_str)
    if pr_num:
        return (400, len(url_str))
    parsed = urllib.parse.urlsplit(url_str)
    path_lower = (parsed.path or "").lower()
    if any(marker in path_lower for marker in _STALE_PR_COMPARE_MARKERS):
        qs = urllib.parse.parse_qs(parsed.query)
        has_title = bool(qs.get("title") or qs.get("merge_request[title]"))
        body_vals = qs.get("body") or qs.get("merge_request[description]") or qs.get("description") or []
        body_text = str(body_vals[0]) if body_vals else ""
        has_body = bool(body_text.strip())
        has_verified_pass = has_body and (
            "PASS" in body_text
            and (
                "Independent PR Review" in body_text
                or "Verdict" in body_text
                or "Automated Verification Checks" in body_text
            )
        )
        if has_title and has_verified_pass:
            return (300, len(url_str))
        if has_title and has_body:
            return (200, len(url_str))
        if has_title or has_body:
            return (150, len(url_str))
        return (100, len(url_str))
    _ = pr_slug
    return (50, len(url_str))


def _dedupe_richest_pr_urls(
    pr_urls: list[str] | tuple[str, ...] | None,
    *,
    strip_branches: list[str] | None = None,
    strip_repo_slug: str = "",
) -> list[str]:
    """Deduplicate `pr_urls` per `(repo_slug, branch_name)` keeping the richest verified URL (and letting created `/pull/` supersede compare links for that repo)."""
    norm_urls = _normalize_str_list(pr_urls)
    if not norm_urls:
        return []

    strip_branch_set = {
        str(b).strip().removeprefix("refs/heads/")
        for b in (strip_branches or ())
        if str(b).strip()
    }
    norm_strip_slug = dev_common.normalize_repo_slug(strip_repo_slug).lower()

    # Track repos that already have a created /pull/<n> URL so compare URLs for those repos are superseded
    created_pr_repos: set[str] = set()
    for url in norm_urls:
        c_slug, c_num = _extract_created_pr_repo_and_number(url)
        if c_num and c_slug:
            created_pr_repos.add(c_slug.lower())

    best_by_key: dict[tuple[str, ...], tuple[int, tuple[int, int], str]] = {}
    for idx, url in enumerate(norm_urls):
        c_slug, c_num = _extract_created_pr_repo_and_number(url)
        score = _score_pr_url_richness(url)
        if c_num:
            key: tuple[str, ...] = ("created_pr", c_slug.lower(), c_num)
        else:
            cmp_slug, cmp_branch = _extract_repo_slug_and_branch_from_pr_url(url)
            if cmp_branch:
                if cmp_branch in strip_branch_set and (
                    not norm_strip_slug or not cmp_slug or cmp_slug.lower() == norm_strip_slug
                ):
                    continue
                if cmp_slug and cmp_slug.lower() in created_pr_repos:
                    continue
                key = ("compare", cmp_slug.lower(), cmp_branch)
            else:
                key = ("raw", url)

        existing = best_by_key.get(key)
        if existing is None or score > existing[1]:
            first_idx = existing[0] if existing is not None else idx
            best_by_key[key] = (first_idx, score, url)

    ordered = sorted(best_by_key.values(), key=lambda entry: entry[0])
    return [entry[2] for entry in ordered]


def _extract_repo_slug_and_branch_from_pr_url(raw_url: str) -> tuple[str, str]:
    """Extract `(normalized_repo_slug, branch_name)` from a VCS compare/new-PR URL when recognizable."""
    url_str = str(raw_url or "").strip()
    if not url_str:
        return "", ""
    parsed = urllib.parse.urlsplit(url_str)
    path = parsed.path or ""
    path_lower = path.lower()

    def _slug_from_prefix(prefix_path: str) -> str:
        parts = [p for p in prefix_path.strip("/").split("/") if p]
        if len(parts) >= 2:
            return dev_common.normalize_repo_slug(f"{parts[-2]}/{parts[-1]}")
        return ""

    compare_idx = path_lower.find("/compare/")
    if compare_idx >= 0:
        slug = _slug_from_prefix(path[:compare_idx])
        tail = path[compare_idx + len("/compare/") :].strip("/")
        if "..." in tail:
            branch_raw = tail.split("...", 1)[1]
        elif ".." in tail:
            branch_raw = tail.split("..", 1)[1]
        else:
            branch_raw = tail
        branch = urllib.parse.unquote(branch_raw).strip().removeprefix("refs/heads/")
        return slug, branch

    gl_idx = path_lower.find("/-/merge_requests/new")
    if gl_idx >= 0:
        slug = _slug_from_prefix(path[:gl_idx])
        qs = urllib.parse.parse_qs(parsed.query)
        br_list = qs.get("merge_request[source_branch]") or []
        branch = str(br_list[0]).strip().removeprefix("refs/heads/") if br_list else ""
        return slug, branch

    bb_idx = path_lower.find("/pull-requests/new")
    if bb_idx >= 0:
        slug = _slug_from_prefix(path[:bb_idx])
        qs = urllib.parse.parse_qs(parsed.query)
        br_list = qs.get("source") or []
        branch = str(br_list[0]).strip().removeprefix("refs/heads/") if br_list else ""
        return slug, branch

    return "", ""


def _prune_merged_repo_pr_urls(
    pr_urls: list[str] | None,
    released_repo_slug: str = "",
    *,
    all_merged: bool = False,
    merged_repos: list[str] | None = None,
) -> list[str]:
    """Remove stale VCS compare/new-PR URLs for merged repos while preserving open-repo and archival URLs."""
    pruned_slugs: set[str] = set()
    for raw_slug in [released_repo_slug, *(merged_repos or [])]:
        norm = dev_common.normalize_repo_slug(str(raw_slug or ""))
        if norm and norm != "default":
            pruned_slugs.add(norm.lower())

    kept: list[str] = []
    for raw_url in _normalize_str_list(pr_urls):
        path_lower = urllib.parse.urlsplit(raw_url).path.lower()
        is_compare_url = any(marker in path_lower for marker in _STALE_PR_COMPARE_MARKERS)
        if not is_compare_url:
            kept.append(raw_url)
            continue
        if all_merged:
            continue
        if any(
            f"/{slug}{marker}" in path_lower
            for slug in pruned_slugs
            for marker in _STALE_PR_COMPARE_MARKERS
        ):
            continue
        kept.append(raw_url)
    return kept


def _mark_queue_item_repo_merged(
    *,
    conversation_id: str,
    released_repo_slug: str,
    branch_names: list[str] | None = None,
    subagent_conversation_ids: list[str] | None = None,
    remaining_active_locks: dict[str, dict[str, Any]] | None = None,
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Record a merged repository on a queue item, prune stale compare `pr_urls`, and promote to `merged` once all target repos finish."""
    cid_clean = str(conversation_id or "").strip()
    clean_slug = dev_common.normalize_repo_slug(released_repo_slug)
    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    now_iso = _utc_now_iso()
    with dev_common.file_lock(q_path.with_suffix(".json.lock")):
        data = _read_dev_queue_locked(q_path)
        items = list(data["items"])
        idx = _find_queue_item_index(items, cid_clean)
        if idx < 0:
            return {"status": "error", "updated": False, "error": f"Queue item `{cid_clean}` not found."}
        item = dict(items[idx])
        target_repos = [
            norm
            for r in (item.get("repo_slugs") or [])
            for norm in [dev_common.normalize_repo_slug(str(r))]
            if norm
        ]
        if target_repos and clean_slug and clean_slug != "default" and clean_slug not in target_repos:
            return {
                "status": "skipped",
                "updated": False,
                "reason": "released_repo_not_in_item_target_repos",
                "item": item,
            }
        merged_repos = _normalize_str_list(
            [
                *[dev_common.normalize_repo_slug(str(r)) for r in (item.get("merged_repos") or [])],
                *([clean_slug] if clean_slug and clean_slug != "default" else []),
            ]
        )
        still_locked_for_item = False
        still_locked_human_ready = False
        _human_ready_statuses = {
            "PENDING_HUMAN_PR_VALIDATION",
            "PR_OPEN",
            "REVIEW_PASSED",
            "REVIEW_APPROVED",
            "NEEDS_HUMAN_REVIEW",
        }
        for act_lock in (remaining_active_locks or {}).values():
            act_cid = str(act_lock.get("conversation_id") or "").strip()
            act_sibs = [str(s).strip() for s in (act_lock.get("sibling_conversation_ids") or [])]
            if _matches_cid(act_cid, cid_clean) or any(_matches_cid(s, cid_clean) for s in act_sibs):
                still_locked_for_item = True
                act_st_u = str(act_lock.get("status") or "").strip().upper()
                if act_st_u in _human_ready_statuses and bool(act_lock.get("pr_gate_ready", True)):
                    still_locked_human_ready = True
        all_target_repos_merged = (
            (not target_repos and not still_locked_for_item)
            or (clean_slug in ("", "default") and len(target_repos) <= 1 and not still_locked_for_item)
            or (set(target_repos).issubset(set(merged_repos)) and not still_locked_for_item)
        )

        item["merged_repos"] = merged_repos
        item["branch_names"] = _normalize_str_list([*(item.get("branch_names") or []), *(branch_names or [])])
        item["pr_urls"] = _prune_merged_repo_pr_urls(
            item.get("pr_urls"),
            released_repo_slug=clean_slug,
            all_merged=all_target_repos_merged,
            merged_repos=merged_repos,
        )
        item["subagent_conversation_ids"] = _normalize_str_list(
            [*(item.get("subagent_conversation_ids") or []), *(subagent_conversation_ids or [])]
        )
        if all_target_repos_merged:
            item["status"] = "merged"
        elif still_locked_human_ready or bool(item["pr_urls"]):
            item["status"] = "pr_open"
        elif still_locked_for_item:
            item["status"] = "in_progress"
        else:
            item["status"] = "pending"
        item["updated_at"] = now_iso
        items[idx] = item
        data["items"] = _sort_dev_queue_items(items)
        data["updated_at"] = now_iso
        _atomic_write_json_0600(q_path, data)
    return {"status": "ok", "updated": True, "item": item}


def _branch_found_in_base_git_log(
    repo_dir: Path,
    branch_name: str,
    base_branch: str = "main",
) -> bool:
    """Strictly read-only check for an exact branch reference inside recent `origin/<base>` / `<base>` commit history."""
    clean_branch = str(branch_name or "").strip().removeprefix("refs/heads/")
    if len(clean_branch) < 4 or not (repo_dir / ".git").exists():
        return False
    pattern = re.compile(rf"(?<![A-Za-z0-9_.-]){re.escape(clean_branch)}(?![A-Za-z0-9_./-])")
    for base_ref in (f"origin/{base_branch}", base_branch):
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "-n", "100", "--format=%s%n%b", base_ref],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            return bool(pattern.search(proc.stdout or ""))
    return False


def _inspect_local_branch_merged_readonly(
    repo_dir: Path,
    branch_name: str,
    base_branch: str = "main",
    remote: str = "origin",
) -> tuple[bool, str]:
    """Verify whether `branch_name` is merged into `origin/<base>` / `<base>` strictly via local refs & log (zero network calls)."""
    clean_branch = str(branch_name or "").strip().removeprefix("refs/heads/")
    clean_base = (base_branch or "main").strip()
    if not clean_branch or not (repo_dir / ".git").exists():
        return False, "repo_or_branch_missing"

    base_ref = ""
    base_sha = ""
    for cand_base in (f"refs/remotes/{remote}/{clean_base}", f"refs/heads/{clean_base}"):
        p_base = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--verify", cand_base],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if p_base.returncode == 0 and (p_base.stdout or "").strip():
            base_ref = cand_base
            base_sha = (p_base.stdout or "").strip()
            break
    if not base_ref or not base_sha:
        return False, "base_ref_missing"

    target_sha = ""
    for cand_ref in (f"refs/remotes/{remote}/{clean_branch}", f"refs/heads/{clean_branch}"):
        p_ref = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--verify", cand_ref],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if p_ref.returncode == 0 and (p_ref.stdout or "").strip():
            target_sha = (p_ref.stdout or "").strip()
            break

    if target_sha:
        if target_sha == base_sha:
            return True, f"git_merged_into_base:{clean_branch}"
        p_anc = subprocess.run(
            ["git", "-C", str(repo_dir), "merge-base", "--is-ancestor", target_sha, base_ref],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if p_anc.returncode == 0:
            return True, f"git_merged_into_base:{clean_branch}"
        p_cherry = subprocess.run(
            ["git", "-C", str(repo_dir), "cherry", base_ref, target_sha],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if p_cherry.returncode == 0:
            cherry_lines = [
                ln.strip()
                for ln in (p_cherry.stdout or "").splitlines()
                if ln.strip()
            ]
            if cherry_lines and not any(ln.startswith("+") for ln in cherry_lines):
                return True, f"git_merged_into_base:{clean_branch}"
        return False, "unmerged_local_ref_exists"

    if _branch_found_in_base_git_log(repo_dir, clean_branch, base_branch=clean_base):
        return True, f"git_log_merge_commit:{clean_branch}"
    return False, "not_merged"


def _collect_queue_item_repo_branches(
    item: dict[str, Any],
    target_slug: str,
) -> tuple[list[str], list[str]]:
    """Return `(candidate_branches, repo_specific_branches)` for `(item, target_slug)`."""
    clean_target = dev_common.normalize_repo_slug(target_slug)
    target_name = clean_target.rsplit("/", 1)[-1] if clean_target else ""
    url_owners: dict[str, set[str]] = {}
    repo_specific: list[str] = []

    for raw_url in item.get("pr_urls") or []:
        u_slug, u_br = _extract_repo_slug_and_branch_from_pr_url(str(raw_url))
        if u_slug and u_br:
            url_owners.setdefault(u_br, set()).add(u_slug)
            if u_slug == clean_target and u_br not in repo_specific:
                repo_specific.append(u_br)

    candidates: list[str] = list(repo_specific)
    for raw_br in item.get("branch_names") or []:
        br_str = str(raw_br or "").strip()
        if not br_str:
            continue
        if "::" in br_str:
            hint_raw, br_only = br_str.split("::", 1)
            clean_br = br_only.strip().removeprefix("refs/heads/")
            if not clean_br:
                continue
            hint_norm = dev_common.normalize_repo_slug(hint_raw)
            hint_name = hint_raw.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
            if hint_norm == clean_target or (target_name and hint_name == target_name.lower()):
                if clean_br not in repo_specific:
                    repo_specific.append(clean_br)
                if clean_br not in candidates:
                    candidates.append(clean_br)
            continue
        clean_br = br_str.removeprefix("refs/heads/")
        if not clean_br:
            continue
        owners = url_owners.get(clean_br)
        if owners and clean_target not in owners:
            continue
        if clean_br not in candidates:
            candidates.append(clean_br)
    return candidates, repo_specific


def _verify_queue_repo_merged_readonly(
    *,
    item: dict[str, Any],
    target_slug: str,
    all_locks: dict[str, dict[str, Any]],
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    is_scoped: bool = False,
    scoped_slug: str = "",
    scoped_repo_dir: Path | None = None,
    fetch: bool = False,
    fetched_dirs: set[str] | None = None,
) -> tuple[bool, str, list[str], list[str]]:
    """Verify via read-only lock history or Git history whether `(item, target_slug)` is genuinely merged."""
    item_cid = str(item.get("conversation_id") or "").strip()
    candidate_branches, repo_specific_branches = _collect_queue_item_repo_branches(item, target_slug)

    # Evidence (a): Inactive released lock history for `target_slug`
    inactive_lock = all_locks.get(target_slug)
    if isinstance(inactive_lock, dict) and not bool(inactive_lock.get("active")):
        rel_reason = str(inactive_lock.get("last_release_reason") or "").strip()
        if rel_reason in _MERGED_LOCK_RELEASE_REASONS:
            last_cid = str(inactive_lock.get("last_conversation_id") or "").strip()
            last_br = str(inactive_lock.get("last_branch_name") or "").strip()
            branch_matched = any(
                _entry_matches_branch(inactive_lock, br, include_inactive=True)
                for br in candidate_branches
            )
            cid_matched = _matches_cid(last_cid, item_cid)
            conflicting_specific_branch = bool(
                repo_specific_branches
                and last_br
                and not any(
                    _entry_matches_branch(inactive_lock, br, include_inactive=True)
                    for br in repo_specific_branches
                )
            )
            if branch_matched or (cid_matched and not conflicting_specific_branch):
                ev_branches: list[str] = []
                if last_br:
                    ev_branches.append(last_br)
                last_rb = inactive_lock.get("last_repo_branches")
                if isinstance(last_rb, dict):
                    rb_entry = last_rb.get(target_slug)
                    if isinstance(rb_entry, dict) and rb_entry.get("branch_name"):
                        rb_br = str(rb_entry["branch_name"]).strip()
                        if rb_br and rb_br not in ev_branches:
                            ev_branches.append(rb_br)
                ev_subagents = [
                    str(s).strip()
                    for s in (inactive_lock.get("last_subagent_conversation_ids") or [])
                    if str(s).strip()
                ]
                match_kind = "branch" if branch_matched else "conversation_id"
                return True, f"inactive_lock:{rel_reason}:{match_kind}", ev_branches, ev_subagents

    # Evidence (b): Strictly read-only native Git verification
    if not candidate_branches:
        return False, "no_candidate_branches", [], []

    vcs: Any = None
    repo_dir: Path | None = None
    use_injected_client = False
    if git_client is not None:
        if is_scoped and target_slug == scoped_slug:
            use_injected_client = True
        elif not is_scoped and hasattr(git_client, "resolve_repo"):
            with contextlib.suppress(Exception):
                injected_slug = dev_common.normalize_repo_slug(str(git_client.resolve_repo() or ""))
                use_injected_client = injected_slug == target_slug

    if use_injected_client and git_client is not None:
        vcs = git_client
        if is_scoped and target_slug == scoped_slug and scoped_repo_dir is not None:
            repo_dir = scoped_repo_dir
        elif hasattr(git_client, "resolve_repo_dir"):
            with contextlib.suppress(Exception):
                cand_d = git_client.resolve_repo_dir()
                if isinstance(cand_d, Path) and (cand_d / ".git").exists():
                    repo_dir = _resolve_main_git_repo_root(cand_d)
    else:
        if is_scoped and target_slug == scoped_slug and scoped_repo_dir is not None:
            repo_dir = scoped_repo_dir
        else:
            repo_dir = _resolve_repo_dir_for_slug(
                target_slug,
                portfolio_dir=portfolio_dir,
                git_client=git_client if (git_client is not None and hasattr(git_client, "resolve_repo_dir")) else None,
            )
        if repo_dir is None or not (repo_dir / ".git").exists():
            return False, "repo_dir_not_found", [], []
        vcs = GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_dir)})

    if not hasattr(vcs, "inspect_remote_branch_state"):
        return False, "vcs_missing_inspect_remote_branch_state", [], []

    for br_name in candidate_branches:
        eff_fetch = fetch
        dir_key = str(repo_dir) if repo_dir is not None else ""
        if eff_fetch and fetched_dirs is not None and dir_key and not use_injected_client:
            if dir_key in fetched_dirs:
                eff_fetch = False
            else:
                fetched_dirs.add(dir_key)

        if (
            not eff_fetch
            and isinstance(vcs, GitVcsIntegrationClient)
            and repo_dir is not None
            and (repo_dir / ".git").exists()
        ):
            loc_merged, loc_reason = _inspect_local_branch_merged_readonly(repo_dir, br_name, base_branch="main")
            if loc_merged:
                return True, loc_reason, [br_name], []
            continue

        try:
            st = vcs.inspect_remote_branch_state(
                branch_name=br_name,
                base_branch="main",
                repo_dir=repo_dir,
                fetch=eff_fetch,
            )
        except TypeError:
            st = vcs.inspect_remote_branch_state(branch_name=br_name)

        if isinstance(st, dict) and st.get("status") == "ok":
            if bool(st.get("merged_into_base")):
                return True, f"git_merged_into_base:{br_name}", [br_name], []
            remote_exists = bool(st.get("remote_exists") or st.get("exists_on_remote"))
            local_exists = False
            if repo_dir is not None and (repo_dir / ".git").exists():
                loc_proc = subprocess.run(
                    ["git", "-C", str(repo_dir), "rev-parse", "--verify", f"refs/heads/{br_name}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                local_exists = loc_proc.returncode == 0
            if remote_exists or local_exists:
                continue
            if repo_dir is not None and _branch_found_in_base_git_log(repo_dir, br_name, base_branch="main"):
                return True, f"git_log_merge_commit:{br_name}", [br_name], []
        elif repo_dir is not None and (repo_dir / ".git").exists():
            loc_merged, loc_reason = _inspect_local_branch_merged_readonly(repo_dir, br_name, base_branch="main")
            if loc_merged:
                return True, loc_reason, [br_name], []

    return False, "not_merged", [], []


def reconcile_dev_queue_with_git(
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    fetch: bool = False,
) -> dict[str, Any]:
    """Reconcile stale `pr_open` / branched `in_progress` items in `dev_queue.json` using read-only lock & Git evidence."""
    all_locks = get_all_dev_pr_locks(portfolio_dir=portfolio_dir, include_inactive=True)
    active_locks = {k: v for k, v in all_locks.items() if isinstance(v, dict) and bool(v.get("active"))}

    is_scoped = bool(str(repo_slug or "").strip() or workdir)
    scoped_slug = ""
    scoped_repo_dir: Path | None = None
    if is_scoped:
        scoped_slug, scoped_repo_dir = _resolve_repo_slug_and_dir(
            repo_slug=repo_slug,
            workdir=workdir,
            git_client=git_client if (git_client is not None and hasattr(git_client, "resolve_repo")) else None,
            prefer_cwd=False,
            portfolio_dir=portfolio_dir,
            locks_map=all_locks,
        )
        if scoped_repo_dir is not None and (scoped_repo_dir / ".git").exists():
            scoped_repo_dir = _resolve_main_git_repo_root(scoped_repo_dir)
        if not scoped_slug:
            return {
                "status": "skipped",
                "reason": "workdir_not_a_git_repo",
                "repo_slug": "",
                "checked_count": 0,
                "reconciled_count": 0,
                "reconciled_items": [],
            }

    q_data = load_dev_queue(portfolio_dir=portfolio_dir)
    items = list(q_data.get("items") or [])
    checked_count = 0
    reconciled_items: list[dict[str, Any]] = []
    fetched_dirs: set[str] = set()

    for raw_item in items:
        item = dict(raw_item)
        item_cid = str(item.get("conversation_id") or "").strip()
        item_status = str(item.get("status") or "").strip().lower()
        if not item_cid:
            continue
        has_branch_or_pr = bool(item.get("branch_names")) or bool(item.get("pr_urls"))
        if item_status != "pr_open" and not (item_status == "in_progress" and has_branch_or_pr):
            continue

        target_repos = _normalize_str_list(
            [dev_common.normalize_repo_slug(str(r)) for r in (item.get("repo_slugs") or []) if str(r).strip()]
        )
        if not target_repos:
            inferred_repos: list[str] = []
            for raw_url in item.get("pr_urls") or []:
                u_slug, _ = _extract_repo_slug_and_branch_from_pr_url(str(raw_url))
                if u_slug and u_slug not in inferred_repos:
                    inferred_repos.append(u_slug)
            if not inferred_repos:
                for l_slug, l_entry in all_locks.items():
                    if not isinstance(l_entry, dict):
                        continue
                    l_cid = str(l_entry.get("conversation_id") or l_entry.get("last_conversation_id") or "").strip()
                    if _matches_cid(l_cid, item_cid) or any(
                        _entry_matches_branch(l_entry, str(br).strip(), include_inactive=True)
                        for br in (item.get("branch_names") or [])
                    ):
                        norm_l = dev_common.normalize_repo_slug(l_slug)
                        if norm_l and norm_l not in inferred_repos:
                            inferred_repos.append(norm_l)
            target_repos = inferred_repos

        if not target_repos:
            continue

        already_merged = {
            dev_common.normalize_repo_slug(str(r))
            for r in (item.get("merged_repos") or [])
            if str(r).strip()
        }

        for cand_slug in target_repos:
            if is_scoped and cand_slug != scoped_slug:
                continue
            checked_count += 1
            if cand_slug in active_locks:
                continue

            if cand_slug in already_merged:
                item_still_locked = any(
                    _matches_cid(str(al.get("conversation_id") or ""), item_cid)
                    or any(_matches_cid(str(s), item_cid) for s in (al.get("sibling_conversation_ids") or []))
                    for al in active_locks.values()
                )
                all_done = set(target_repos).issubset(already_merged) and not item_still_locked
                pruned_urls = _prune_merged_repo_pr_urls(
                    item.get("pr_urls"),
                    released_repo_slug=cand_slug,
                    all_merged=all_done,
                    merged_repos=list(already_merged),
                )
                needs_cleanup = (
                    all_done and str(item.get("status") or "").lower() != "merged"
                ) or (pruned_urls != _normalize_str_list(item.get("pr_urls")))
                if needs_cleanup:
                    upd = _mark_queue_item_repo_merged(
                        conversation_id=item_cid,
                        released_repo_slug=cand_slug,
                        remaining_active_locks=active_locks,
                        portfolio_dir=portfolio_dir,
                    )
                    if upd.get("updated"):
                        item = dict(upd.get("item") or item)
                        reconciled_items.append(
                            {
                                "conversation_id": item_cid,
                                "repo_slug": cand_slug,
                                "evidence": "already_merged_repo_cleanup",
                                "status": str(item.get("status") or ""),
                                "merged_repos": list(item.get("merged_repos") or []),
                                "pr_urls_remaining": list(item.get("pr_urls") or []),
                            }
                        )
                continue

            is_merged, evidence, ev_branches, ev_subagents = _verify_queue_repo_merged_readonly(
                item=item,
                target_slug=cand_slug,
                all_locks=all_locks,
                portfolio_dir=portfolio_dir,
                git_client=git_client,
                is_scoped=is_scoped,
                scoped_slug=scoped_slug,
                scoped_repo_dir=scoped_repo_dir,
                fetch=fetch,
                fetched_dirs=fetched_dirs,
            )
            if not is_merged:
                continue

            upd = _mark_queue_item_repo_merged(
                conversation_id=item_cid,
                released_repo_slug=cand_slug,
                branch_names=ev_branches or None,
                subagent_conversation_ids=ev_subagents or None,
                remaining_active_locks=active_locks,
                portfolio_dir=portfolio_dir,
            )
            if upd.get("updated"):
                item = dict(upd.get("item") or item)
                already_merged.add(cand_slug)
                reconciled_items.append(
                    {
                        "conversation_id": item_cid,
                        "repo_slug": cand_slug,
                        "evidence": evidence,
                        "status": str(item.get("status") or ""),
                        "merged_repos": list(item.get("merged_repos") or []),
                        "pr_urls_remaining": list(item.get("pr_urls") or []),
                    }
                )

        # Self-heal phantom `pr_open` items left with zero active locks and zero open PR URLs
        cur_item_status = str(item.get("status") or "").strip().lower()
        remaining_unmerged = [r for r in target_repos if r not in already_merged]
        if cur_item_status == "pr_open" and remaining_unmerged and not _normalize_str_list(item.get("pr_urls")):
            item_still_locked = any(
                _matches_cid(str(al.get("conversation_id") or ""), item_cid)
                or any(_matches_cid(str(s), item_cid) for s in (al.get("sibling_conversation_ids") or []))
                for al in active_locks.values()
            )
            if not item_still_locked and (not is_scoped or scoped_slug in remaining_unmerged or scoped_slug in already_merged):
                upd_ph = update_dev_queue_item_status(
                    conversation_id=item_cid,
                    status="pending",
                    portfolio_dir=portfolio_dir,
                )
                if upd_ph.get("updated"):
                    item = dict(upd_ph.get("item") or item)
                    reconciled_items.append(
                        {
                            "conversation_id": item_cid,
                            "repo_slug": remaining_unmerged[0],
                            "evidence": "phantom_pr_open_reverted_to_pending",
                            "status": str(item.get("status") or "pending"),
                            "merged_repos": list(item.get("merged_repos") or []),
                            "pr_urls_remaining": list(item.get("pr_urls") or []),
                        }
                    )

    result: dict[str, Any] = {
        "status": "ok",
        "checked_count": checked_count,
        "reconciled_count": len(reconciled_items),
        "reconciled_items": reconciled_items,
    }
    if scoped_slug:
        result["repo_slug"] = scoped_slug
    return result


def _auto_enqueue_blocked_task(
    conversation_id: str,
    action_id: str = "",
    rca_summary: str = "",
    proposed_fix: str = "",
    target_files: list[str] | None = None,
    repo_slugs: list[str] | None = None,
    blocked_by_lock_id: str = "",
    portfolio_dir: Path | str | None = None,
    *,
    section_anchors: list[str] | None = None,
) -> dict[str, Any]:
    """Best-effort deterministic enqueue when a repository lock blocks a concurrent remediation attempt."""
    try:
        return enqueue_dev_queue_item(
            conversation_id=conversation_id,
            action_id=action_id,
            priority=40,
            repo_slugs=repo_slugs,
            status="pending",
            rca_summary=rca_summary,
            proposed_fix=proposed_fix,
            target_files=target_files,
            section_anchors=section_anchors,
            source="auto_lock",
            blocked_by_lock_id=blocked_by_lock_id,
            portfolio_dir=portfolio_dir,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def get_next_dev_queue_item(
    orchestrator_conversation_id: str = "",
    portfolio_dir: Path | str | None = None,
    *,
    skip_locked_repos: bool = False,
) -> dict[str, Any]:
    """Return the highest-priority runnable item from `dev_queue.json`, including safe Slot #2 lookahead candidates."""
    if orchestrator_conversation_id.strip():
        set_dev_queue_orchestrator(orchestrator_conversation_id.strip(), portfolio_dir=portfolio_dir)
    data = load_dev_queue(portfolio_dir=portfolio_dir)
    items: list[dict[str, Any]] = list(data.get("items") or [])
    status_by_cid = {str(it.get("conversation_id") or ""): str(it.get("status") or "").lower() for it in items}
    active_locks = get_all_dev_pr_locks(portfolio_dir=portfolio_dir)
    locked_repos = sorted(active_locks.keys())

    def _deps_unmet(item: dict[str, Any]) -> list[str]:
        unmet: list[str] = []
        for dep in item.get("depends_on") or []:
            dep_clean = str(dep or "").strip()
            if not dep_clean:
                continue
            matched_st = ""
            for cand_cid, cand_st in status_by_cid.items():
                if _matches_cid(cand_cid, dep_clean):
                    matched_st = cand_st
                    break
            if matched_st and matched_st not in ("merged", "cleaned"):
                unmet.append(f"{dep_clean}({matched_st})")
        return unmet

    def _lock_owns_cid(lock_obj: dict[str, Any], cid_val: str) -> bool:
        act_cid = str(lock_obj.get("conversation_id") or "").strip()
        act_sibs = [str(s).strip() for s in (lock_obj.get("sibling_conversation_ids") or [])]
        if _matches_cid(act_cid, cid_val) or any(_matches_cid(s, cid_val) for s in act_sibs):
            return True
        s2 = lock_obj.get("slots", {}).get("2") if isinstance(lock_obj.get("slots"), dict) else None
        if isinstance(s2, dict) and bool(s2.get("active", True)):
            s2_cid = str(s2.get("conversation_id") or "").strip()
            s2_sibs = [str(s).strip() for s in (s2.get("sibling_conversation_ids") or [])]
            if _matches_cid(s2_cid, cid_val) or any(_matches_cid(s, cid_val) for s in s2_sibs):
                return True
        return False

    def _item_repo_schedule(item: dict[str, Any]) -> tuple[list[str], list[str], list[str], list[str]]:
        """Compute `(remaining_repos, dispatch_repos, foreign_locked, self_locked)` using contract-first ordering and subtracting `merged_repos`."""
        item_repos = _normalize_str_list(
            [dev_common.normalize_repo_slug(str(r)) for r in (item.get("repo_slugs") or []) if str(r).strip()]
        )
        merged_set = {
            dev_common.normalize_repo_slug(str(r))
            for r in (item.get("merged_repos") or [])
            if str(r).strip()
        }
        remaining_repos = [r for r in item_repos if r not in merged_set]
        item_cid = str(item.get("conversation_id") or "").strip()
        foreign_locked: list[str] = []
        self_locked: list[str] = []
        for rslug in remaining_repos:
            act = active_locks.get(rslug)
            if not act:
                continue
            if _lock_owns_cid(act, item_cid):
                self_locked.append(rslug)
            else:
                foreign_locked.append(rslug)

        if not remaining_repos:
            return ([], [], [], [])
        next_contract_repo = remaining_repos[0]
        if next_contract_repo in foreign_locked or next_contract_repo in self_locked:
            return (remaining_repos, [], foreign_locked, self_locked)
        if not foreign_locked:
            dispatch_repos = [r for r in remaining_repos if r not in self_locked]
        else:
            # Contract-first partial multi-repo dispatch: advance unlocked prefix repo(s) ahead of downstream locked repo(s)
            dispatch_repos = []
            for r in remaining_repos:
                if r in foreign_locked or r in self_locked:
                    break
                dispatch_repos.append(r)
        return (remaining_repos, dispatch_repos, foreign_locked, self_locked)

    def _probe_slot2_lookahead(item: dict[str, Any], target_repo: str) -> tuple[bool, dict[str, Any], str]:
        """Return `(eligible, slot2_annotation, blocked_reason)` for a pending item on a Slot-#1-locked repo."""
        act = active_locks.get(target_repo)
        if not isinstance(act, dict) or not bool(act.get("active")):
            return (False, {}, "repo_not_locked")
        max_slots = int(act.get("max_slots") or _resolve_max_repo_slots())
        slots_map = act.get("slots") if isinstance(act.get("slots"), dict) else {}
        if max_slots < 2 or ("2" in slots_map and bool(slots_map["2"].get("active", True))):
            return (False, {}, "max_slots_occupied")
        if str(item.get("status") or "pending").lower() != "pending":
            return (False, {}, "item_not_pending")
        if not (item.get("target_files") or item.get("section_anchors")):
            return (False, {}, "candidate_unanchored")
        if not (
            act.get("target_files")
            or act.get("section_anchors")
            or act.get("branch_name")
            or act.get("worktree_path")
        ):
            return (False, {}, "slot1_unanchored")

        r_dir = _resolve_lock_target_repo_dir(act) or _resolve_repo_dir_for_slug(target_repo, portfolio_dir=portfolio_dir)
        slot1_spec = TaskAnchorSpec.from_mapping(act)
        cand_spec = TaskAnchorSpec.from_mapping(item)
        ov_res = analyze_pair_overlap(slot1_spec, cand_spec, repo_dir=r_dir, repo_slug=target_repo)
        if not ov_res.parallel_safe:
            return (
                False,
                {"overlap_tier": ov_res.overlap_tier},
                f"overlap_{ov_res.overlap_tier.lower()}",
            )

        annotated = dict(item)
        annotated["target_slot"] = 2
        annotated["slot_allocation"] = "slot_2_lookahead"
        annotated["dispatch_repos"] = [target_repo]
        annotated["overlap_tier"] = ov_res.overlap_tier
        annotated["monolith_disjoint"] = ov_res.monolith_disjoint
        annotated["requires_rebase_reverify"] = ov_res.requires_rebase_reverify
        annotated["landing_order"] = list(ov_res.landing_order)
        annotated["slot1_lock_id"] = str(act.get("lock_id") or "")
        annotated["slot1_branch_name"] = str(act.get("branch_name") or "")
        annotated["slot1_status"] = str(act.get("status") or "")
        return (True, annotated, "")

    next_item: dict[str, Any] | None = None
    next_pending_item: dict[str, Any] | None = None
    next_unlocked_item: dict[str, Any] | None = None
    runnable_by_repo: dict[str, dict[str, Any]] = {}
    slot2_candidates: list[dict[str, Any]] = []
    slot2_by_repo: dict[str, dict[str, Any]] = {}
    blocked_deps: list[dict[str, Any]] = []
    blocked_repos: list[dict[str, Any]] = []

    for it in items:
        st = str(it.get("status") or "pending").lower()
        if st not in ("in_progress", "pr_open", "pending"):
            continue
        unmet = _deps_unmet(it)
        if unmet:
            blocked_deps.append(
                {
                    "conversation_id": it.get("conversation_id"),
                    "title": it.get("title"),
                    "unmet_dependencies": unmet,
                }
            )
            continue
        remaining_repos, dispatch_repos, foreign_locked, self_locked = _item_repo_schedule(it)
        is_self_waiting = bool(remaining_repos and remaining_repos[0] in self_locked)
        is_contract_blocked = bool(remaining_repos and not dispatch_repos and foreign_locked)

        if is_contract_blocked:
            first_repo = remaining_repos[0]
            s2_ok, s2_annotated, s2_reason = (
                _probe_slot2_lookahead(it, first_repo)
                if first_repo not in slot2_by_repo
                else (False, {}, "slot2_already_selected")
            )
            if s2_ok:
                slot2_candidates.append(s2_annotated)
                slot2_by_repo[first_repo] = s2_annotated
                if first_repo not in runnable_by_repo:
                    runnable_by_repo[first_repo] = s2_annotated
                if next_unlocked_item is None:
                    next_unlocked_item = s2_annotated
                if next_item is None:
                    next_item = s2_annotated
                if st == "pending" and next_pending_item is None:
                    next_pending_item = s2_annotated
                continue

            blk_entry: dict[str, Any] = {
                "conversation_id": it.get("conversation_id"),
                "title": it.get("title"),
                "locked_repos": foreign_locked,
            }
            if s2_annotated.get("overlap_tier"):
                blk_entry["overlap_tier"] = s2_annotated["overlap_tier"]
            if s2_reason:
                blk_entry["slot2_blocked_reason"] = s2_reason
            blocked_repos.append(blk_entry)
            if skip_locked_repos:
                continue
        elif is_self_waiting:
            # Already actively developing or awaiting PR merge on its contract-first repo; do not re-dispatch as unlocked
            if skip_locked_repos:
                continue
        else:
            annotated = dict(it)
            if remaining_repos:
                annotated["target_slot"] = 1
                annotated["dispatch_repos"] = list(dispatch_repos)
                annotated["partial_multi_repo_dispatch"] = bool(foreign_locked)
                annotated["waiting_locked_repos"] = list(foreign_locked)
            if next_unlocked_item is None:
                next_unlocked_item = annotated
            for free_repo in dispatch_repos:
                if free_repo not in runnable_by_repo:
                    runnable_by_repo[free_repo] = annotated
            it_out = annotated
            if next_item is None:
                next_item = it_out
            if st == "pending" and next_pending_item is None:
                next_pending_item = it_out
            continue

        if next_item is None:
            next_item = it
        if st == "pending" and next_pending_item is None:
            next_pending_item = it

    return {
        "status": "ok",
        "queue_path": data.get("queue_path", ""),
        "orchestrator_conversation_id": data.get("orchestrator_conversation_id", ""),
        "has_next": next_item is not None,
        "next_item": next_item,
        "next_pending_item": next_pending_item,
        "next_unlocked_item": next_unlocked_item,
        "runnable_by_repo": runnable_by_repo,
        "slot2_candidates": slot2_candidates,
        "slot2_by_repo": slot2_by_repo,
        "locked_repos": locked_repos,
        "blocked_by_dependencies": blocked_deps,
        "blocked_by_repo_locks": blocked_repos,
        "summary": data.get("summary", {}),
    }


def _resolve_antigravity_dirs(
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
) -> tuple[Path, Path]:
    """Resolve Antigravity `brain/` and `conversations/` directories with test overrides."""
    b_dir = Path(brain_dir).expanduser() if brain_dir else (Path.home() / ".gemini" / "antigravity" / "brain")
    c_dir = (
        Path(conversations_dir).expanduser()
        if conversations_dir
        else (Path.home() / ".gemini" / "antigravity" / "conversations")
    )
    return b_dir, c_dir


def _dir_tree_bytes(path: Path) -> int:
    """Compute total byte size of a file or directory recursively."""
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _extract_invoke_subagent_cids_from_step(step: dict[str, Any]) -> list[str]:
    """Extract spawned child conversationIds strictly from structured `invoke_subagent` payloads (never prose)."""
    found: list[str] = []
    seen: set[str] = set()

    def _add_uuid(val: str) -> None:
        clean = str(val or "").strip().lower()
        if _CONVERSATION_UUID_RE.fullmatch(clean) and clean not in seen:
            seen.add(clean)
            found.append(clean)

    def _walk_structured(obj: Any, in_invoke_context: bool = False) -> None:
        if isinstance(obj, dict):
            name_val = str(obj.get("name") or obj.get("tool") or obj.get("function") or "").strip().lower()
            is_invoke = in_invoke_context or (name_val == "invoke_subagent")
            has_spawn_shape = (
                is_invoke
                or ("logAbsoluteUri" in obj)
                or ("spec" in obj and "result" in obj)
            )
            if has_spawn_shape:
                for k in ("conversationId", "conversation_id"):
                    val = obj.get(k)
                    if isinstance(val, str):
                        _add_uuid(val)
                log_uri = str(obj.get("logAbsoluteUri") or "")
                m_uri = re.search(r"/brain/([0-9a-f-]{36})/", log_uri, re.IGNORECASE)
                if m_uri:
                    _add_uuid(m_uri.group(1))
            for k, v in obj.items():
                if k in ("response", "result", "output", "tool_calls", "calls") and isinstance(v, (dict, list)):
                    _walk_structured(v, in_invoke_context=has_spawn_shape)
                elif isinstance(v, (dict, list)) and has_spawn_shape:
                    _walk_structured(v, in_invoke_context=True)
        elif isinstance(obj, list):
            for entry in obj:
                _walk_structured(entry, in_invoke_context=in_invoke_context)

    _walk_structured(step.get("tool_calls"), in_invoke_context=False)
    step_content = step.get("content")
    if isinstance(step_content, dict):
        _walk_structured(step_content, in_invoke_context=False)
    elif isinstance(step_content, str):
        stripped = step_content.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            with contextlib.suppress(json.JSONDecodeError):
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    _walk_structured(parsed, in_invoke_context=False)
    return found


def extract_spawned_subagent_ids(
    conversation_id: str,
    brain_dir: Path | str | None = None,
) -> list[str]:
    """Traverse `transcript.jsonl` recursively via structured `invoke_subagent` metadata to list child CIDs."""
    b_dir, _ = _resolve_antigravity_dirs(brain_dir=brain_dir)
    root_clean = str(conversation_id or "").strip().lower()
    if not root_clean:
        return []
    queue: list[str] = [root_clean]
    visited: set[str] = set()
    spawned: list[str] = []

    while queue:
        cid = queue.pop(0)
        if cid in visited:
            continue
        visited.add(cid)
        if cid != root_clean and cid not in spawned:
            spawned.append(cid)
        tpath = b_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if not tpath.is_file():
            continue
        try:
            raw_lines = tpath.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for raw_line in raw_lines:
            if "invoke_subagent" not in raw_line and "conversationId" not in raw_line and "logAbsoluteUri" not in raw_line:
                continue
            with contextlib.suppress(json.JSONDecodeError):
                step = json.loads(raw_line)
                if isinstance(step, dict):
                    for child in _extract_invoke_subagent_cids_from_step(step):
                        if child not in visited:
                            queue.append(child)
    return spawned


def is_conversation_fully_idle(
    conversation_id: str,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    min_idle_seconds: float = 60.0,
    now_ts: float | None = None,
) -> tuple[bool, str]:
    """Return `(True, 'idle')` only if a conversation shows no active step and no activity within `min_idle_seconds`."""
    b_dir, c_dir = _resolve_antigravity_dirs(brain_dir=brain_dir, conversations_dir=conversations_dir)
    cid = str(conversation_id or "").strip()
    tpath = b_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
    pb_path = c_dir / f"{cid}.pb"
    cur_ts = float(now_ts if now_ts is not None else time.time())

    if min_idle_seconds > 0:
        for artifact in (tpath, pb_path):
            if artifact.is_file():
                age = cur_ts - artifact.stat().st_mtime
                if age < min_idle_seconds:
                    return False, f"not_fully_idle:recent_activity({round(age, 1)}s<{int(min_idle_seconds)}s)"

    if tpath.is_file():
        last_line = ""
        try:
            for line in tpath.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip():
                    last_line = line
        except OSError:
            last_line = ""
        if last_line:
            with contextlib.suppress(json.JSONDecodeError):
                last_obj = json.loads(last_line)
                if isinstance(last_obj, dict):
                    st = str(last_obj.get("status") or "").strip().upper()
                    if st in ("RUNNING", "PENDING", "EXECUTING", "IN_PROGRESS", "QUEUED"):
                        return False, f"not_fully_idle:step_status_{st}"
    return True, "idle"


def collect_protected_conversation_ids(
    exclude_ids: list[str] | None = None,
    portfolio_dir: Path | str | None = None,
    brain_dir: Path | str | None = None,
    include_unaudited_check: bool = True,
) -> dict[str, list[str]]:
    """Collect protected conversation IDs/prefixes with exact reasons (orchestrator, pending queue, lock, unaudited)."""
    protected: dict[str, list[str]] = {}

    def _protect(cid_val: str, reason: str) -> None:
        clean = str(cid_val or "").strip().lower()
        if not clean:
            return
        reasons = protected.setdefault(clean, [])
        if reason not in reasons:
            reasons.append(reason)

    for cid in exclude_ids or ():
        _protect(cid, "explicit_exclude")
    for env_key in ("ANTIGRAVITY_CONVERSATION_ID", "AGY_CONVERSATION_ID"):
        if os.environ.get(env_key):
            _protect(os.environ[env_key], f"env:{env_key}")

    q_data = load_dev_queue(portfolio_dir=portfolio_dir)
    if q_data.get("orchestrator_conversation_id"):
        _protect(str(q_data["orchestrator_conversation_id"]), "active_orchestrator")
    for item in q_data.get("items") or []:
        st = str(item.get("status") or "pending").lower()
        if st not in DEV_QUEUE_DONE_STATUSES:
            _protect(str(item.get("conversation_id") or ""), f"dev_queue:{st}")
            for sub in item.get("subagent_conversation_ids") or []:
                _protect(str(sub), f"dev_queue_subagent:{st}")

    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_obj = dev_common.atomic_read_json(p_dir / "actions_state.json", default={})
    audit_state = (
        state_obj.get("actions_log_audit_state")
        if isinstance(state_obj.get("actions_log_audit_state"), dict)
        else {}
    )
    all_locks_map = _read_locks_state(audit_state)
    for active_lock in all_locks_map.values():
        if isinstance(active_lock, dict) and active_lock.get("active"):
            _protect(str(active_lock.get("conversation_id") or ""), "active_dev_pr_lock")
            for key in ("sibling_conversation_ids", "subagent_conversation_ids"):
                for cid in active_lock.get(key) or []:
                    _protect(str(cid), "active_dev_pr_lock")
            slots_prot = active_lock.get("slots") if isinstance(active_lock.get("slots"), dict) else {}
            for s_obj in slots_prot.values():
                if isinstance(s_obj, dict) and bool(s_obj.get("active", True)):
                    _protect(str(s_obj.get("conversation_id") or ""), "active_dev_pr_lock")
                    for key in ("sibling_conversation_ids", "subagent_conversation_ids"):
                        for cid in s_obj.get(key) or []:
                            _protect(str(cid), "active_dev_pr_lock")
    pending_t2 = audit_state.get("pending_tier2_proposals")
    if isinstance(pending_t2, dict):
        for cid in pending_t2:
            _protect(str(cid), "pending_tier2_proposal")

    if include_unaudited_check:
        analyzer_candidates = [
            Path.home()
            / ".gemini"
            / "config"
            / "plugins"
            / "wiz-tam-plugin"
            / "skills"
            / "agy-conversation-analyzer"
            / "scripts"
            / "conversation_analyzer.py",
            Path(__file__).resolve().parents[4]
            / "agy-tam-plugin"
            / "skills"
            / "agy-conversation-analyzer"
            / "scripts"
            / "conversation_analyzer.py",
            Path(__file__).resolve().parents[2]
            / "agy-conversation-analyzer"
            / "scripts"
            / "conversation_analyzer.py",
        ]
        analyzer_path = next((p for p in analyzer_candidates if p.is_file()), None)
        if analyzer_path is not None and analyzer_path.is_file():
            with contextlib.suppress(Exception):
                import importlib.util

                spec = importlib.util.spec_from_file_location("_conv_analyzer_guard", analyzer_path)
                if spec and spec.loader:
                    ca_mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(ca_mod)
                    b_dir, _ = _resolve_antigravity_dirs(brain_dir=brain_dir)
                    res = ca_mod.check_unaudited_action_runs(
                        limit=100,
                        portfolio_dir=p_dir,
                        antigravity_dir=b_dir.parent if brain_dir else None,
                        scheduled=False,
                    )
                    for key, label in (
                        ("unaudited_runs", "unaudited_run"),
                        ("grouped_incidents", "unaudited_incident"),
                        ("pending_tier2_proposals", "pending_tier2_proposal"),
                    ):
                        for item in res.get(key) or []:
                            if isinstance(item, dict):
                                for fld in ("conversation_id", "primary_conversation_id"):
                                    if item.get(fld):
                                        _protect(str(item[fld]), label)
                                for lfld in ("sibling_conversation_ids", "all_conversation_ids"):
                                    for scid in item.get(lfld) or []:
                                        _protect(str(scid), label)
    return protected


def _find_protection_reasons(cid: str, protected_map: dict[str, list[str]]) -> list[str]:
    """Return matching protection reasons for `cid` across exact and prefix keys."""
    reasons: list[str] = []
    for p_cid, p_reasons in protected_map.items():
        if _matches_cid(cid, p_cid):
            for r in p_reasons:
                if r not in reasons:
                    reasons.append(r)
    return reasons


def verify_branches_merged_into_base(
    branch_specs: list[str] | None = None,
    conversation_ids: list[str] | None = None,
    confirm_merged: bool = False,
    base_branch: str = "main",
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> dict[str, Any]:
    """Verify across all associated repositories that target branches are merged into `main`."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_obj = dev_common.atomic_read_json(p_dir / "actions_state.json", default={})
    audit_state = (
        state_obj.get("actions_log_audit_state")
        if isinstance(state_obj.get("actions_log_audit_state"), dict)
        else {}
    )
    all_locks_map = _read_locks_state(audit_state)
    lock_obj = audit_state.get("dev_pr_lock") if isinstance(audit_state.get("dev_pr_lock"), dict) else {}
    all_lock_entries: list[dict[str, Any]] = list(all_locks_map.values())
    if lock_obj and lock_obj not in all_lock_entries:
        all_lock_entries.append(lock_obj)
    q_data = load_dev_queue(portfolio_dir=portfolio_dir)

    targets: list[tuple[Path | None, str]] = []
    seen_targets: set[tuple[str, str]] = set()
    base_vcs = git_client or GitVcsIntegrationClient()
    base_repo_dir = base_vcs.resolve_repo_dir()

    def _resolve_repo_dir_hint(repo_raw: str | Path | None) -> Path | None:
        if isinstance(repo_raw, Path):
            return repo_raw if (repo_raw / ".git").exists() else None
        raw = str(repo_raw or "").strip()
        if not raw:
            return None
        cand = Path(raw.replace("$HOME", str(Path.home()))).expanduser()
        if (cand / ".git").exists():
            return cand
        repo_name = raw.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if repo_name:
            if base_repo_dir.name == repo_name and (base_repo_dir / ".git").exists():
                return base_repo_dir
            sibling = base_repo_dir.parent / repo_name
            if (sibling / ".git").exists():
                return sibling
        return None

    def _add_target(repo_raw: str | Path | None, branch: str) -> None:
        raw_br = str(branch or "").strip()
        if "::" in raw_br:
            r_hint, br_only = raw_br.split("::", 1)
            _add_target(r_hint, br_only)
            return
        br = raw_br.removeprefix("refs/heads/")
        if not br:
            return
        r_path = _resolve_repo_dir_hint(repo_raw)
        if r_path is None:
            if any(ex_br == br and ex_repo is not None for ex_repo, ex_br in targets):
                return
        else:
            targets[:] = [
                (ex_repo, ex_br)
                for ex_repo, ex_br in targets
                if not (ex_repo is None and ex_br == br)
            ]
            seen_targets.discard(("", br))
        key = (str(r_path or ""), br)
        if key not in seen_targets:
            seen_targets.add(key)
            targets.append((r_path, br))

    for spec in branch_specs or ():
        s = str(spec or "").strip()
        if "::" in s:
            r_part, br_part = s.split("::", 1)
            _add_target(r_part, br_part)
        else:
            _add_target(None, s)

    cids_clean = [str(c).strip() for c in (conversation_ids or ()) if str(c).strip()]
    if not targets and cids_clean:
        for cand_lock in all_lock_entries:
            last_cid = str(cand_lock.get("conversation_id") or cand_lock.get("last_conversation_id") or "")
            if any(_matches_cid(c, last_cid) for c in cids_clean):
                rb_map = cand_lock.get("repo_branches") if cand_lock.get("active") else cand_lock.get("last_repo_branches")
                if isinstance(rb_map, dict) and rb_map:
                    for rb_k, entry in rb_map.items():
                        if not _is_slot_scoped_rb_key(str(rb_k)) and isinstance(entry, dict) and entry.get("branch_name"):
                            _add_target(entry.get("target_repo_dir") or entry.get("repo_slug"), str(entry["branch_name"]))
                elif cand_lock.get("branch_name") or cand_lock.get("last_branch_name"):
                    _add_target(
                        cand_lock.get("target_repo_dir"),
                        str(cand_lock.get("branch_name") or cand_lock.get("last_branch_name") or ""),
                    )
            if cand_lock.get("active") and isinstance(cand_lock.get("slots"), dict):
                s2_cand = cand_lock["slots"].get("2")
                if isinstance(s2_cand, dict) and bool(s2_cand.get("active", True)):
                    s2_cid = str(s2_cand.get("conversation_id") or "")
                    if s2_cid and any(_matches_cid(c, s2_cid) for c in cids_clean) and s2_cand.get("branch_name"):
                        _add_target(
                            s2_cand.get("target_repo_dir") or cand_lock.get("target_repo_dir"),
                            str(s2_cand["branch_name"]),
                        )
        for q_item in q_data.get("items") or []:
            q_cid = str(q_item.get("conversation_id") or "")
            if any(_matches_cid(c, q_cid) for c in cids_clean):
                q_repos = [str(rs).strip() for rs in (q_item.get("repo_slugs") or []) if str(rs).strip()]
                fallback_repo = q_repos[0] if len(q_repos) == 1 else None
                for br in q_item.get("branch_names") or []:
                    _add_target(fallback_repo, str(br))

    for cand_lock in all_lock_entries:
        if cand_lock.get("active"):
            lock_cids = [str(cand_lock.get("conversation_id") or "")]
            s_map = cand_lock.get("slots") if isinstance(cand_lock.get("slots"), dict) else {}
            for s_v in s_map.values():
                if isinstance(s_v, dict) and bool(s_v.get("active", True)) and s_v.get("conversation_id"):
                    lock_cids.append(str(s_v["conversation_id"]))
            if any(_matches_cid(c, l_cid) for c in cids_clean for l_cid in lock_cids if l_cid):
                return {
                    "all_merged": False,
                    "reason": "active_dev_pr_lock_still_held",
                    "checked_count": 0,
                    "branches": [],
                    "unmerged": [{"branch_name": cand_lock.get("branch_name", ""), "reason": "active_dev_pr_lock_still_held"}],
                }

    if not targets:
        return {
            "all_merged": bool(confirm_merged),
            "reason": "confirmed_merged_no_branch_refs" if confirm_merged else "no_branches_to_verify",
            "checked_count": 0,
            "branches": [],
            "unmerged": [] if confirm_merged else [{"reason": "no_branches_to_verify_pass_branch_or_confirm_merged"}],
        }

    results: list[dict[str, Any]] = []
    last_reason = str(lock_obj.get("last_release_reason") or "")
    for repo_path, br_name in targets:
        if git_client is not None:
            vcs = git_client
        elif repo_path is not None:
            vcs = GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_path)})
        else:
            vcs = GitVcsIntegrationClient()

        resolved_dir = repo_path or vcs.resolve_repo_dir()
        st = vcs.inspect_remote_branch_state(
            branch_name=br_name,
            base_branch=base_branch,
            repo_dir=repo_path,
            fetch=True,
        )
        local_ref_proc = subprocess.run(
            ["git", "-C", str(resolved_dir), "rev-parse", "--verify", f"refs/heads/{br_name}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        local_exists = local_ref_proc.returncode == 0
        remote_exists = bool(st.get("remote_exists"))
        merged = False
        reason = ""

        if st.get("status") != "ok":
            merged = False
            reason = str(st.get("error") or "inspect_remote_branch_state_failed")
        elif bool(st.get("merged_into_base")):
            merged = True
            reason = "git_merged_into_base"
        elif remote_exists or local_exists:
            merged = False
            reason = "unmerged_branch_still_exists"
        else:
            log_proc = subprocess.run(
                ["git", "-C", str(resolved_dir), "log", "-n", "60", "--oneline", f"origin/{base_branch}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            cid_suffix = br_name.rsplit("-", 1)[-1] if "-" in br_name else ""
            log_text = log_proc.stdout or ""
            matched_q_merged = any(
                str(q_it.get("status") or "").lower() in ("merged", "cleaned")
                and br_name in (q_it.get("branch_names") or [])
                for q_it in (q_data.get("items") or [])
            )
            if br_name in log_text or (len(cid_suffix) >= 8 and cid_suffix in log_text):
                merged = True
                reason = "git_log_merge_commit_found"
            elif confirm_merged and (
                last_reason in ("BRANCH_MERGED_INTO_MAIN", "HUMAN_VALIDATED_MANUAL_PR") or matched_q_merged
            ):
                merged = True
                reason = "confirmed_merged_after_prune"
            elif confirm_merged:
                merged = True
                reason = "confirmed_merged_explicit_after_prune"
            else:
                merged = False
                reason = "branch_absent_pass_confirm_merged_if_pruned"

        results.append(
            {
                "repo_dir": str(resolved_dir),
                "branch_name": br_name,
                "remote_exists": remote_exists,
                "local_exists": local_exists,
                "merged": merged,
                "reason": reason,
            }
        )

    unmerged = [r for r in results if not r["merged"]]
    return {
        "all_merged": len(unmerged) == 0,
        "checked_count": len(results),
        "branches": results,
        "unmerged": unmerged,
    }


def cleanup_merged_conversations(
    conversation_ids: list[str] | None = None,
    subagents_only_of: list[str] | None = None,
    branch_specs: list[str] | None = None,
    exclude_ids: list[str] | None = None,
    min_idle_seconds: float = 60.0,
    dry_run: bool = False,
    confirm_merged: bool = False,
    mark_queue_cleaned: bool = True,
    include_unaudited_check: bool = True,
    portfolio_dir: Path | str | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> dict[str, Any]:
    """Safely delete merged conversation directories/protobufs and spawned subagents after verifying all protections."""
    root_cids = _normalize_str_list(conversation_ids)
    subonly_cids = _normalize_str_list(subagents_only_of)
    all_seed_cids = _normalize_str_list([*root_cids, *subonly_cids])
    if not all_seed_cids:
        return {"status": "error", "error": "Provide at least one --id or --subagents-only-of conversation ID."}

    merge_check = verify_branches_merged_into_base(
        branch_specs=branch_specs,
        conversation_ids=all_seed_cids,
        confirm_merged=confirm_merged,
        portfolio_dir=portfolio_dir,
        git_client=git_client,
    )
    if not merge_check.get("all_merged"):
        return {
            "status": "blocked",
            "reason": "unmerged_or_unverified_branches",
            "dry_run": bool(dry_run),
            "deleted_count": 0,
            "deleted": [],
            "skipped": [],
            "freed_bytes": 0,
            "freed_mb": 0.0,
            "merge_verification": merge_check,
        }

    b_dir, c_dir = _resolve_antigravity_dirs(brain_dir=brain_dir, conversations_dir=conversations_dir)
    candidates: list[str] = []
    seen_cand: set[str] = set()

    def _add_candidate(cid_val: str) -> None:
        clean = str(cid_val or "").strip().lower()
        if clean and clean not in seen_cand:
            seen_cand.add(clean)
            candidates.append(clean)

    for rcid in root_cids:
        _add_candidate(rcid)
        for child in extract_spawned_subagent_ids(rcid, brain_dir=b_dir):
            _add_candidate(child)

    extra_protected = list(exclude_ids or [])
    for orch_cid in subonly_cids:
        extra_protected.append(orch_cid)
        for child in extract_spawned_subagent_ids(orch_cid, brain_dir=b_dir):
            _add_candidate(child)

    # Also include known subagents recorded on matching queue/lock entries
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    q_data = load_dev_queue(portfolio_dir=p_dir)
    for q_it in q_data.get("items") or []:
        if any(_matches_cid(str(q_it.get("conversation_id") or ""), s) for s in all_seed_cids):
            for sub in q_it.get("subagent_conversation_ids") or []:
                _add_candidate(str(sub))

    protected_map = collect_protected_conversation_ids(
        exclude_ids=extra_protected,
        portfolio_dir=p_dir,
        brain_dir=b_dir,
        include_unaudited_check=include_unaudited_check,
    )

    deleted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    freed_bytes = 0

    for cid in candidates:
        reasons = _find_protection_reasons(cid, protected_map)
        if reasons:
            skipped.append({"conversation_id": cid, "reasons": reasons})
            continue
        idle_ok, idle_reason = is_conversation_fully_idle(
            cid,
            brain_dir=b_dir,
            conversations_dir=c_dir,
            min_idle_seconds=min_idle_seconds,
        )
        if not idle_ok:
            skipped.append({"conversation_id": cid, "reasons": [idle_reason]})
            continue

        b_path = b_dir / cid
        pb_path = c_dir / f"{cid}.pb"
        if not b_path.exists() and not pb_path.exists():
            skipped.append({"conversation_id": cid, "reasons": ["not_present_on_disk"]})
            continue

        sz = _dir_tree_bytes(b_path) + _dir_tree_bytes(pb_path)
        if not dry_run:
            if b_path.exists():
                shutil.rmtree(b_path)
            if pb_path.exists():
                pb_path.unlink()
        freed_bytes += sz
        deleted.append({"conversation_id": cid, "bytes": sz})

    deleted_ids = {d["conversation_id"] for d in deleted}
    if mark_queue_cleaned and not dry_run and deleted_ids:
        for rcid in root_cids:
            if rcid.lower() in deleted_ids:
                update_dev_queue_item_status(rcid, "cleaned", portfolio_dir=p_dir)

    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "deleted_count": len(deleted),
        "deleted": [d["conversation_id"] for d in deleted],
        "deleted_details": deleted,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        "merge_verification": merge_check,
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
    """Compute native git `branch-diff` and embed SHA-bound `verification_gate` for Lean Reviewer Protocol."""
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
    res["verification_gate"] = build_branch_diff_verification_gate(lock_info, repo_dir, resolved_branch)
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
    if include_active_lock:
        act_lock = get_dev_pr_lock(portfolio_dir=portfolio_dir, repo_slug=resolved_slug, workdir=workdir)
        if act_lock.get("active"):
            primary_cid = str(act_lock.get("conversation_id") or "")
            tasks_list.append(TaskAnchorSpec.from_mapping(act_lock))
            s2_lk = act_lock.get("slots", {}).get("2") if isinstance(act_lock.get("slots"), dict) else None
            if isinstance(s2_lk, dict) and bool(s2_lk.get("active", True)):
                tasks_list.append(TaskAnchorSpec.from_mapping(s2_lk))

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
    )
    out = batch_res.to_dict()
    out["mode"] = "queue_and_locks"
    out["repo_slug"] = resolved_slug
    out["primary_slot_id"] = primary_cid
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

            email_sent = False
            if send_email or bool(lock_info.get("deferred_send_email")):
                repo_name = resolved_slug.split("/")[-1] if resolved_slug and resolved_slug != "default" else eff_push_dir.name
                subj_sum = _format_pr_notification_subject_summary(proposal_title, exec_summary, resolved_branch)
                email_subj = f"PR Ready for Validation [{repo_name}]: {subj_sum} (PASS {rev_score}/100)"
                notif_url = _git_client.build_vcs_pr_notification_url(
                    branch_name=resolved_branch,
                    base_branch=base_branch,
                    pr_url=manual_pr_url,
                    repo_slug=resolved_slug,
                )
                n_res = dev_common.try_send_branch_review_notification(
                    status="OK",
                    subject=email_subj,
                    body=canonical_body,
                    conversation_id=cid_clean or resolved_ts,
                    validator=f"{PR_REVIEWER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL}) [PASS {rev_score}/100 · 0-LLM Rebase Promoted]",
                    branch_name=resolved_branch,
                    target_url=notif_url,
                    errors_only_override=False,
                )
                email_sent = bool(n_res.get("sent"))

            watcher_res = spawn_pr_merge_watcher_daemon(portfolio_dir=portfolio_dir)
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
            if (slot_num == 2 and was_review_approved)
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


# ==============================================================================
# CLI Entrypoint (`pr_remediator.py`)
# ==============================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for `pr_remediator.py`."""
    parser = argparse.ArgumentParser(
        prog="pr_remediator",
        description="VCS-Interoperable Native Git Timestamped Branch Remediation, Lock & Review Orchestrator.",
    )
    sub = parser.add_subparsers(dest="subcommand")

    p_lock = sub.add_parser("lock-status", help="Inspect or sync per-repository cross-conversation branch locks (`dev_pr_locks`).")
    p_lock.add_argument(
        "--sync-git",
        dest="sync_git",
        action="store_true",
        help="Inspect remote branch state via native git and auto-release lock if merged into main or deleted on origin.",
    )
    p_lock.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug filter.")
    p_lock.add_argument("--workdir", default="", help="Optional repository or worktree directory.")
    p_lock.add_argument("--all", dest="all_repos", action="store_true", help="Include all active repository locks.")

    p_acq = sub.add_parser("acquire-lock", help="Acquire a per-repository cross-conversation branch development lock.")
    p_acq.add_argument("--id", "--conversation-id", dest="conversation_id", required=True, help="Conversation ID.")
    p_acq.add_argument("--action-id", default="", help="Action or prompt scope ID (e.g., customer-sync-prep or prompt-refactor).")
    p_acq.add_argument("--issue", type=int, default=None, help="Optional Issue number.")
    p_acq.add_argument("--issue-url", default="", help="Optional compare/issue URL.")
    p_acq.add_argument("--branch", default="", help="Timestamped feature branch name.")
    p_acq.add_argument("--timestamp", default="", help="Canonical branch timestamp (YYYYMMDD-HHMMSS).")
    p_acq.add_argument("--summary", default="", help="RCA / change summary.")
    p_acq.add_argument("--proposed-fix", default="", help="Proposed fix summary.")
    p_acq.add_argument("--target-files", nargs="*", default=[], help="Optional target file paths.")
    p_acq.add_argument("--section-anchors", nargs="*", default=[], help="Optional section/symbol/line anchors.")
    p_acq.add_argument("--sibling-ids", nargs="*", default=[], help="Optional sibling Conversation IDs.")
    p_acq.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_acq.add_argument("--workdir", default="", help="Optional target repository directory.")
    p_acq.add_argument("--allow-slot2", "--parallel-slot", dest="allow_slot2", action="store_true", help="Allow acquiring parallel Slot #2 when overlap check passes.")
    p_acq.add_argument("--scheduled", action="store_true", help="Mark as scheduled run.")

    p_rel = sub.add_parser("release-lock", help="Release/delete a per-repository branch development lock.")
    p_rel.add_argument("--reason", default="MANUAL_RELEASE", help="Release reason.")
    p_rel.add_argument("--summary", default="", help="Resolution summary.")
    p_rel.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_rel.add_argument("--workdir", default="", help="Optional repository or worktree directory.")
    p_rel.add_argument("--branch", default="", help="Optional branch name to match lock.")
    p_rel.add_argument("--all", dest="release_all", action="store_true", help="Release all active repository locks.")

    p_hb = sub.add_parser("lock-heartbeat", help="Stamp last_activity_at, worker_cid, and optional stage label on an active dev_pr_lock.")
    p_hb.add_argument("--workdir", default="", help="Optional repository or worktree directory.")
    p_hb.add_argument("--repo", "--repo-slug", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_hb.add_argument("--branch", default="", help="Optional branch name to match lock.")
    p_hb.add_argument("--worker-cid", dest="worker_cid", default="", help="Active worker subagent conversation ID.")
    p_hb.add_argument("--stage", dest="stage_label", default="", help="Optional compact stage label (e.g., coding, tests).")

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
    p_plan_dev.add_argument("--model", choices=("pro", "flash", "inherit"), default="", help="Optional developer model tier.")
    p_plan_dev.add_argument("--anchored-context", default="", help="Optional pre-anchored file line ranges and contracts.")
    p_plan_dev.add_argument("--repo", dest="repo_slug", default="", help="Optional target owner/repo slug.")
    p_plan_dev.add_argument("--workdir", default="", help="Optional target repository directory.")

    p_start = sub.add_parser(
        "start-remediation",
        help="Verify no branch is pending validation, create timestamped branch fix/<timestamp>-..., and acquire dev_pr_lock.",
    )
    p_start.add_argument("--id", "--conversation-id", dest="conversation_id", required=True, help="Target Conversation ID.")
    p_start.add_argument("--action-id", default="", help="Action or prompt scope ID (e.g., prompt-code-change or manual-audit-log).")
    p_start.add_argument("--rca-summary", default="", help="Root Cause Analysis or prompt change rationale.")
    p_start.add_argument("--proposed-fix", default="", help="Proposed solution description.")
    p_start.add_argument("--target-files", nargs="*", default=[], help="Affected files.")
    p_start.add_argument("--section-anchors", nargs="*", default=[], help="Optional section/symbol/line anchors for monolith disjointness.")
    p_start.add_argument("--sibling-ids", nargs="*", default=[], help="Grouped sibling Conversation IDs.")
    p_start.add_argument("--timestamp", default="", help="Optional explicit timestamp (YYYYMMDD-HHMMSS).")
    p_start.add_argument("--scheduled", action="store_true", help="Mark as scheduled run.")
    p_start.add_argument("--worktree", action="store_true", help="Provision an isolated git worktree for the timestamped branch.")
    p_start.add_argument("--checkout", action="store_true", help="Checkout the timestamped branch directly in the local repository.")
    p_start.add_argument("--workdir", default="", help="Optional target repository directory (defaults to active cwd repo or AGY_CONF_REPO_DIR).")
    p_start.add_argument("--worker-cid", dest="worker_cid", default="", help="Optional active worker subagent conversation ID for stall monitoring.")
    p_start.add_argument("--allow-slot2", "--parallel-slot", dest="allow_slot2", action="store_true", help="Allow allocating parallel Slot #2 when overlap check passes.")
    p_start.add_argument(
        "--reuse-active",
        "--continue-active",
        dest="reuse_active_branch",
        action="store_true",
        help="Reuse the currently active open PR branch when addressing PR comments or CI fixes.",
    )

    p_test = sub.add_parser("test-local", help="Run local offline unit tests, static QA auditor, and persist verification_results on dev_pr_lock.")
    p_test.add_argument("--workdir", default="", help="Optional repository or worktree directory.")
    p_test.add_argument("--targets", nargs="*", default=[], help="Optional unittest target modules.")
    p_test.add_argument(
        "--all",
        "--all-files",
        dest="run_all",
        action="store_true",
        help="Force `--all-files` on pre-commit and `--all` on selective_test_runner / BATS.",
    )
    p_test.add_argument(
        "--with-pre-commit",
        "--run-pre-commit",
        "--pre-commit",
        dest="run_pre_commit",
        action="store_true",
        help="Single-pass mode: run incremental `pre-commit run --files` (or `--all-files` fallback) with SKIP=unit-tests,unit-tests-fast,qa-static-audit,bats-suite, then run qa_static_audit + tests once.",
    )
    p_test.add_argument(
        "--pre-commit-status",
        type=lambda v: normalize_pre_commit_status(v)[0] if str(v or "").strip() else "",
        choices=("", "pass", "fail", "skipped"),
        default="",
        help="Optional recorded status from an external `pre-commit run` invocation (pass/PASSED, fail/FAILED, skipped).",
    )

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

    p_diff = sub.add_parser("branch-diff", help="Compute changed files and unified diff for a branch via native git.")
    p_diff.add_argument("--branch", "--head", dest="branch", default="", help="Target branch name (defaults to locked branch_name).")
    p_diff.add_argument("--base", default="main", help="Base branch name (default: main).")
    p_diff.add_argument("--workdir", default="", help="Optional target repository directory.")
    p_diff.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")

    p_rev = sub.add_parser(
        "record-review",
        help="Record Agent 2 (agy-code-pr-reviewer) evaluation for the pushed branch and send the notification email with matching timestamp.",
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
    p_rev.add_argument("--send-email", action="store_true", help="Dispatch a branch-review notification with the branch timestamp when configured.")

    p_watch = sub.add_parser(
        "watch-pr-merge",
        help="Run or spawn the self-terminating background watcher that polls origin while dev_pr_locks are active and fast-forwards main + cleans the local branch once merged.",
    )
    p_watch.add_argument("--interval", type=float, default=15.0, help="Polling interval in seconds (default: 15).")
    p_watch.add_argument("--max-duration", type=float, default=14400.0, help="Maximum watcher lifetime in seconds (default: 14400 / 4h).")
    p_watch.add_argument("--max-iterations", type=int, default=None, help="Optional maximum poll iterations before exiting.")
    p_watch.add_argument("--daemon", action="store_true", help="Spawn the watcher as a detached background daemon and return immediately.")

    p_wait = sub.add_parser(
        "wait-for-unlock",
        help="0-LLM reactive background waiter for agy-dev-orchestrator: polls origin every N seconds and exits cleanly the moment any repo unlocks, runnable work appears, or a worker stalls.",
    )
    p_wait.add_argument("--interval", type=float, default=15.0, help="Polling interval in seconds (default: 15).")
    p_wait.add_argument("--max-duration", type=float, default=3600.0, help="Maximum wait duration in seconds before returning timeout with rearm_recommended (default: 3600).")
    p_wait.add_argument("--max-iterations", type=int, default=None, help="Optional maximum poll iterations before exiting.")
    p_wait.add_argument("--stall-seconds", dest="stall_seconds", type=int, default=None, help="Override stalled-worker idle threshold in seconds (default: 900).")
    p_wait.add_argument("--orchestrator-id", dest="orchestrator_id", default="", help="Active orchestrator conversation ID.")
    p_wait.add_argument("--brain-dir", dest="brain_dir", default=None, help="Optional Antigravity brain directory override for worker transcript mtime checks.")

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

    p_qlist = sub.add_parser("queue-list", help="Inspect persistent orchestrator development queue (`dev_queue.json`).")
    p_qlist.add_argument("--status", default="", help="Optional status filter.")

    p_qadd = sub.add_parser("queue-add", help="Add or update a task in `dev_queue.json`.")
    p_qadd.add_argument("--id", "--conversation-id", dest="conversation_id", required=True, help="Task Conversation ID.")
    p_qadd.add_argument("--title", default="", help="Short descriptive title.")
    p_qadd.add_argument("--action-id", default="", help="Action or prompt scope ID.")
    p_qadd.add_argument("--priority", type=int, default=50, help="Numeric priority (lower executes first).")
    p_qadd.add_argument("--repos", nargs="*", default=[], help="Target repository slugs.")
    p_qadd.add_argument("--status", choices=DEV_QUEUE_STATUSES, default="pending", help="Queue item status.")
    p_qadd.add_argument("--depends-on", nargs="*", default=[], help="Dependency conversation IDs.")
    p_qadd.add_argument("--rca-summary", default="", help="RCA summary.")
    p_qadd.add_argument("--proposed-fix", default="", help="Proposed fix summary.")
    p_qadd.add_argument("--target-files", nargs="*", default=[], help="Target file paths.")
    p_qadd.add_argument("--section-anchors", nargs="*", default=[], help="Optional section/symbol/line anchors.")
    p_qadd.add_argument("--branches", nargs="*", default=[], help="Associated branch names.")
    p_qadd.add_argument("--pr-urls", nargs="*", default=[], help="Associated PR or Compare URLs.")
    p_qadd.add_argument("--notes", default="", help="Orchestrator notes.")
    p_qadd.add_argument("--orchestrator-id", default="", help="Optional active orchestrator conversation ID.")

    p_qset = sub.add_parser("queue-set-status", help="Update the status or links of an item in `dev_queue.json`.")
    p_qset.add_argument("--id", "--conversation-id", dest="conversation_id", required=True, help="Task Conversation ID.")
    p_qset.add_argument("--status", choices=DEV_QUEUE_STATUSES, required=True, help="New status.")
    p_qset.add_argument("--branches", nargs="*", default=[], help="Branch names to attach.")
    p_qset.add_argument("--pr-urls", nargs="*", default=[], help="PR/Compare URLs to attach.")
    p_qset.add_argument("--notes", default="", help="Optional updated notes.")
    p_qset.add_argument("--orchestrator-id", default="", help="Optional active orchestrator conversation ID.")

    p_qnext = sub.add_parser("queue-next", help="Select the highest-priority runnable item from `dev_queue.json`.")
    p_qnext.add_argument("--orchestrator-id", default="", help="Optional active orchestrator conversation ID to record.")
    p_qnext.add_argument("--unlocked", action="store_true", help="Skip queue items whose target repo currently holds an active lock.")

    p_qsync = sub.add_parser(
        "queue-sync",
        help="Reconcile stale `pr_open` / branched `in_progress` items in `dev_queue.json` against lock history & native Git merge state, pruning stale compare `pr_urls`.",
    )
    p_qsync.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug to scope reconciliation.")
    p_qsync.add_argument("--workdir", default="", help="Optional target repository directory or worktree path to scope reconciliation.")
    p_qsync.add_argument("--fetch", action="store_true", help="Fetch origin refs before checking Git merge state.")

    p_qopt = sub.add_parser(
        "queue-optimize",
        help="Run deterministic 0-LLM self-diagnosing workflow optimization audit across locks, queue, managed repos, and bounded transcripts.",
    )
    p_qopt.add_argument("--json", dest="as_json", action="store_true", help="Emit structured JSON report (default output format).")
    p_qopt.add_argument("--orchestrator-id", dest="orchestrator_id", default="", help="Active orchestrator conversation ID for bloat diagnostics.")
    p_qopt.add_argument("--repos-dir", dest="repos_dir", default="", help="Optional parent directory of managed local git repositories.")
    p_qopt.add_argument("--brain-dir", dest="brain_dir", default=None, help="Optional Antigravity brain directory override for read-only transcript telemetry.")
    p_qopt.add_argument("--stall-seconds", dest="stall_seconds", type=int, default=None, help="Override stalled-worker idle threshold in seconds (default: 900).")

    p_qdoc = sub.add_parser(
        "queue-doctor",
        help="0-LLM orchestrator diagnosis, safe runtime auto-heal (--fix), cooldown+PID-gated wake-up (--wake), and PR-only improvement advisor.",
    )
    p_qdoc.add_argument("--json", dest="as_json", action="store_true", help="Emit structured JSON diagnosis report.")
    p_qdoc.add_argument("--sync-git", dest="sync_git", action="store_true", help="Sync per-repo locks and local main with git before diagnosing.")
    p_qdoc.add_argument("--fix", action="store_true", help="Apply safe runtime-only reconciliations (sync locks & phantom pr_open items; never mutates tracked code).")
    p_qdoc.add_argument("--wake", action="store_true", help="Launch detached hybrid `agentapi` (Desktop/sidecar) or `agy --agent agy-dev-orchestrator` (terminal) recovery when needed (cooldown + live-PID gated).")
    p_qdoc.add_argument("--heal", action="store_true", help="Shorthand for `--sync-git --fix --wake`.")
    p_qdoc.add_argument("--check-only", dest="check_only", action="store_true", help="0-LLM sidecar/cron health gate: exit 0 when healthy, exit 2 when anomaly requires wake or fix.")
    p_qdoc.add_argument("--dry-run", dest="dry_run", action="store_true", help="Preview safe fixes and exact wake command/prompt without spawning processes or mutating state.")
    p_qdoc.add_argument("--force", action="store_true", help="Override wake cooldown or healthy skip check when `--wake` is passed.")
    p_qdoc.add_argument("--cooldown-seconds", dest="cooldown_seconds", type=int, default=DEFAULT_WAKE_COOLDOWN_SECONDS, help=f"Minimum seconds between orchestrator wake-ups (default: {DEFAULT_WAKE_COOLDOWN_SECONDS}).")
    p_qdoc.add_argument("--stall-seconds", dest="stall_seconds", type=int, default=None, help="Override stalled-worker/orchestrator idle threshold in seconds (default: 900).")
    p_qdoc.add_argument("--repos-dir", dest="repos_dir", default="", help="Optional parent directory of managed local git repositories.")
    p_qdoc.add_argument("--state-dir", dest="state_dir", default="", help="Optional state directory override.")
    p_qdoc.add_argument("--brain-dir", dest="brain_dir", default=None, help="Optional Antigravity brain directory override.")
    p_qdoc.add_argument("--orchestrator-id", dest="orchestrator_id", default="", help="Optional orchestrator conversation ID override.")
    p_qdoc.add_argument("--print-prompt", dest="print_prompt", action="store_true", help="Print the pre-computed wake-up prompt to stdout.")
    p_qdoc.add_argument("--no-color", dest="no_color", action="store_true", help="Disable ANSI color codes in terminal report.")

    p_clean = sub.add_parser(
        "cleanup-merged-conversations",
        help="Safely delete merged conversation directories/protobufs and spawned subagents after verifying git merge and protections.",
    )
    p_clean.add_argument("--id", "--ids", "--conversation-ids", dest="conversation_ids", nargs="*", default=[], help="Merged root conversation IDs to clean up along with spawned subagents.")
    p_clean.add_argument("--subagents-only-of", nargs="*", default=[], help="Orchestrator conversation IDs whose spawned child subagents should be cleaned up while keeping the orchestrator itself protected.")
    p_clean.add_argument("--branch", "--branches", dest="branches", nargs="*", default=[], help="Explicit branch names or `repo_dir::branch` specs to verify merged.")
    p_clean.add_argument("--exclude-id", "--exclude-ids", dest="exclude_ids", nargs="*", default=[], help="Additional conversation IDs to protect from deletion.")
    p_clean.add_argument("--min-idle-seconds", type=float, default=60.0, help="Minimum idle age in seconds before deleting conversation artifacts (default: 60).")
    p_clean.add_argument("--confirm-merged", action="store_true", help="Allow cleanup when merged branch refs were already pruned from origin and local.")
    p_clean.add_argument("--skip-unaudited-check", action="store_true", help="Skip conversation_analyzer check-unaudited lookup (useful in isolated tests).")
    p_clean.add_argument("--dry-run", action="store_true", help="Preview candidates and freed MB without deleting files.")

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
    p_rbr.add_argument("--send-email", action="store_true", help="Send Ready-for-PR notification email when auto-promoted to Slot #1 human PR.")

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `pr_remediator.py`."""
    args = parse_args(argv)

    if args.subcommand == "lock-status":
        repo_s = getattr(args, "repo_slug", "")
        workdir_s = getattr(args, "workdir", "") or None
        all_locks = get_all_dev_pr_locks()
        if getattr(args, "sync_git", False):
            res = sync_dev_pr_lock_with_git(repo_slug=repo_s, workdir=workdir_s)
        else:
            res = {
                "status": "ok",
                "active_count": len(all_locks),
                "lock": get_dev_pr_lock(repo_slug=repo_s, workdir=workdir_s),
                "locks": all_locks,
            }
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "acquire-lock":
        res = acquire_dev_pr_lock(
            conversation_id=args.conversation_id,
            action_id=args.action_id,
            issue_number=args.issue,
            issue_url=args.issue_url,
            branch_name=args.branch,
            branch_timestamp=args.timestamp,
            summary=args.summary,
            proposed_fix=args.proposed_fix,
            target_files=getattr(args, "target_files", []) or None,
            sibling_ids=args.sibling_ids,
            scheduled=getattr(args, "scheduled", False),
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
            section_anchors=getattr(args, "section_anchors", []) or None,
            allow_slot2=bool(getattr(args, "allow_slot2", False)),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("acquired") else 1

    if args.subcommand == "release-lock":
        res = release_dev_pr_lock(
            reason=args.reason,
            summary=args.summary,
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
            branch_name=getattr(args, "branch", ""),
            release_all=bool(getattr(args, "release_all", False)),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("released") else 1

    if args.subcommand == "lock-heartbeat":
        res = record_dev_pr_lock_heartbeat(
            workdir=getattr(args, "workdir", "") or None,
            repo_slug=getattr(args, "repo_slug", ""),
            branch_name=getattr(args, "branch", ""),
            worker_cid=getattr(args, "worker_cid", ""),
            stage_label=getattr(args, "stage_label", ""),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "plan-developer":
        res = build_code_developer_subagent_plan(
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
        )
        print(json.dumps({"status": "ok", **res}, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "start-remediation":
        res = start_remediation_branch_and_lock(
            conversation_id=args.conversation_id,
            action_id=args.action_id,
            rca_summary=args.rca_summary,
            proposed_fix=args.proposed_fix,
            target_files=args.target_files,
            sibling_ids=args.sibling_ids,
            create_worktree=args.worktree,
            checkout_branch=args.checkout,
            branch_timestamp=args.timestamp,
            reuse_active_branch=getattr(args, "reuse_active_branch", False),
            scheduled=getattr(args, "scheduled", False),
            workdir=getattr(args, "workdir", "") or None,
            worker_cid=getattr(args, "worker_cid", ""),
            section_anchors=getattr(args, "section_anchors", []) or None,
            allow_slot2=bool(getattr(args, "allow_slot2", False)),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "test-local":
        res = run_local_validation_suite(
            workdir=args.workdir or None,
            test_targets=args.targets or None,
            run_pre_commit=bool(getattr(args, "run_pre_commit", False)),
            pre_commit_status=str(getattr(args, "pre_commit_status", "") or ""),
            run_all=bool(getattr(args, "run_all", False)),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("passed") else 1

    if args.subcommand in ("push-branch", "prepare-review", "create-pr"):
        res = push_remediation_branch_and_build_reviewer_plan(
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
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "branch-diff":
        res = get_branch_diff_with_verification_gate(
            branch=args.branch.strip(),
            base_branch=args.base,
            workdir=getattr(args, "workdir", "") or None,
            repo_slug=getattr(args, "repo_slug", ""),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "record-review":
        res = record_pr_review_evaluation(
            pr_number=args.pr,
            verdict=args.verdict,
            score=args.score,
            summary=args.summary,
            executive_summary=getattr(args, "executive_summary", ""),
            branch_name=args.branch,
            reviewer_subagent_id=args.subagent_id,
            send_email=args.send_email,
            workdir=getattr(args, "workdir", "") or None,
            repo_slug=getattr(args, "repo_slug", ""),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "watch-pr-merge":
        if getattr(args, "daemon", False):
            res = spawn_pr_merge_watcher_daemon(
                poll_interval_seconds=args.interval,
                max_duration_seconds=args.max_duration,
                force_spawn=True,
            )
        else:
            res = run_pr_merge_watcher_loop(
                poll_interval_seconds=args.interval,
                max_duration_seconds=args.max_duration,
                max_iterations=args.max_iterations,
            )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") in ("ok", "skipped") else 1

    if args.subcommand == "wait-for-unlock":
        res = wait_for_unlock_or_actionable_event(
            poll_interval_seconds=args.interval,
            max_duration_seconds=args.max_duration,
            max_iterations=args.max_iterations,
            stall_threshold_seconds=getattr(args, "stall_seconds", None),
            orchestrator_conversation_id=getattr(args, "orchestrator_id", ""),
            brain_dir=getattr(args, "brain_dir", None),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand in ("evaluate-branch", "finalize-pr"):
        res = evaluate_branch_and_release_lock(
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
                res["conversation_cleanup"] = cleanup_merged_conversations(
                    conversation_ids=[prev_cid],
                    exclude_ids=list(getattr(args, "exclude_ids", []) or []),
                    confirm_merged=True,
                )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "queue-list":
        q_res = load_dev_queue()
        st_filter = str(getattr(args, "status", "") or "").strip().lower()
        if st_filter:
            q_res["items"] = [it for it in q_res.get("items") or [] if str(it.get("status") or "").lower() == st_filter]
        print(json.dumps({"status": "ok", **q_res}, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "queue-add":
        q_res = enqueue_dev_queue_item(
            conversation_id=args.conversation_id,
            title=args.title,
            action_id=args.action_id,
            priority=args.priority,
            repo_slugs=args.repos,
            status=args.status,
            depends_on=args.depends_on,
            rca_summary=args.rca_summary,
            proposed_fix=args.proposed_fix,
            target_files=args.target_files,
            section_anchors=getattr(args, "section_anchors", []) or None,
            branch_names=args.branches,
            pr_urls=args.pr_urls,
            notes=args.notes,
            orchestrator_conversation_id=args.orchestrator_id,
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") == "ok" else 1

    if args.subcommand == "queue-set-status":
        q_res = update_dev_queue_item_status(
            conversation_id=args.conversation_id,
            status=args.status,
            branch_names=args.branches or None,
            pr_urls=args.pr_urls or None,
            notes=args.notes or None,
            orchestrator_conversation_id=args.orchestrator_id,
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") == "ok" else 1

    if args.subcommand == "queue-next":
        q_res = get_next_dev_queue_item(
            orchestrator_conversation_id=args.orchestrator_id,
            skip_locked_repos=bool(getattr(args, "unlocked", False)),
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") == "ok" else 1

    if args.subcommand == "queue-sync":
        q_res = reconcile_dev_queue_with_git(
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
            fetch=bool(getattr(args, "fetch", False)),
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") in ("ok", "skipped") else 1

    if args.subcommand == "queue-optimize":
        q_res = analyze_dev_flow_optimizations(
            repos_dir=getattr(args, "repos_dir", "") or None,
            brain_dir=getattr(args, "brain_dir", None),
            orchestrator_cid=getattr(args, "orchestrator_id", ""),
            stall_threshold_seconds=getattr(args, "stall_seconds", None),
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") == "ok" else 1

    if args.subcommand == "queue-doctor":
        doc_res = run_queue_doctor(
            state_dir=getattr(args, "state_dir", "") or None,
            repos_dir=getattr(args, "repos_dir", "") or None,
            brain_dir=getattr(args, "brain_dir", None),
            orchestrator_cid=getattr(args, "orchestrator_id", ""),
            sync_git=bool(getattr(args, "sync_git", False)),
            fix=bool(getattr(args, "fix", False)),
            wake=bool(getattr(args, "wake", False)),
            heal=bool(getattr(args, "heal", False)),
            check_only=bool(getattr(args, "check_only", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            force=bool(getattr(args, "force", False)),
            cooldown_seconds=int(getattr(args, "cooldown_seconds", DEFAULT_WAKE_COOLDOWN_SECONDS)),
            stall_threshold_seconds=getattr(args, "stall_seconds", None),
        )
        if bool(getattr(args, "as_json", False)):
            print(json.dumps(doc_res, indent=2, ensure_ascii=False))
        elif bool(getattr(args, "print_prompt", False)):
            print(str(doc_res.get("wake_prompt") or ""))
        else:
            color_on = (not bool(getattr(args, "no_color", False))) and bool(
                getattr(sys.stdout, "isatty", lambda: False)()
            ) and not bool(os.environ.get("NO_COLOR"))
            print(render_doctor_report(doc_res, color=color_on))
        return int(doc_res.get("exit_code", 0))

    if args.subcommand == "cleanup-merged-conversations":
        c_res = cleanup_merged_conversations(
            conversation_ids=args.conversation_ids,
            subagents_only_of=args.subagents_only_of,
            branch_specs=args.branches,
            exclude_ids=args.exclude_ids,
            min_idle_seconds=args.min_idle_seconds,
            dry_run=args.dry_run,
            confirm_merged=args.confirm_merged,
            include_unaudited_check=not args.skip_unaudited_check,
        )
        print(json.dumps(c_res, indent=2, ensure_ascii=False))
        return 0 if c_res.get("status") == "ok" else 1

    if args.subcommand == "analyze-overlap":
        ov_res = analyze_queue_or_tasks_overlap(
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
        rbr_res = run_slot_rebase_reverify(
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
            branch_name=str(getattr(args, "branch", "") or ""),
            base_branch=str(getattr(args, "base", "main") or "main"),
            fetch=bool(getattr(args, "fetch", False)),
            send_email=bool(getattr(args, "send_email", False)),
        )
        print(json.dumps(rbr_res, indent=2, ensure_ascii=False))
        return 0 if rbr_res.get("status") == "ok" else 1

    parse_args(["--help"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
