#!/usr/bin/env python3
"""Per-repository multi-slot lock state machine & shared VCS helpers (`lock_store.py`).

Extracted from `pr_remediator.py` to separate `dev_pr_locks` CRUD, slot routing,
heartbeats, worktree cleanup, and readonly git merge inspection from CLI orchestration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
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
import git_client as _git_client  # noqa: E402
from git_client import GitVcsIntegrationClient  # noqa: E402
import parallel_scope as _parallel_scope  # noqa: E402
from parallel_scope import (  # noqa: E402
    OVERLAP_NONE,
    OVERLAP_TRANSITIVE,
    TaskAnchorSpec,
    analyze_pair_overlap,
)
import rebase_gate as _rebase_gate  # noqa: E402

build_pro_dashboard_ui_url = dev_common.try_build_control_tower_ui_url
restart_pro_api_server = dev_common.try_restart_control_tower_api
stop_dev_api_server = dev_common.try_stop_dev_api_server

_REPO_CLIENTS_DIR = Path(__file__).resolve().parents[3] / "clients"
CLIENTS_DIR = (
    _REPO_CLIENTS_DIR
    if _REPO_CLIENTS_DIR.is_dir()
    else (Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin" / "clients")
)
if str(CLIENTS_DIR) not in sys.path:
    sys.path.insert(0, str(CLIENTS_DIR))


_FACADE_MODULES: list[Any] = []


def _register_facade_module(mod: Any) -> None:
    """Register every loaded `pr_remediator` facade module instance so `mock.patch.object` on any alias propagates."""
    if mod is not None and mod not in _FACADE_MODULES:
        _FACADE_MODULES.append(mod)


def _facade_attr(name: str, fallback: Any) -> Any:
    """Resolve `name` via loaded `pr_remediator` facade(s) so `mock.patch.object(prs, ...)` propagates even after module reloads."""
    cur_mod = sys.modules.get("pr_remediator")
    if cur_mod is not None:
        val = getattr(cur_mod, name, fallback)
        if val is not fallback:
            return val
    for mod in reversed(_FACADE_MODULES):
        val = getattr(mod, name, fallback)
        if val is not fallback:
            return val
    return fallback


def _get_queue_store() -> Any:
    """Lazy-load `queue_store` (falling back to `pr_remediator` during incremental staging)."""
    try:
        import queue_store as _qs
        return _qs
    except ImportError:
        return sys.modules["pr_remediator"]

PR_DEVELOPER_SUBAGENT_NAME = "agy-code-developer"
PR_REVIEWER_SUBAGENT_NAME = "agy-code-pr-reviewer"
PR_DEV_DEFAULT_MODEL = "flash"
PR_SUBAGENT_MODEL = "pro"
PR_REVIEW_PASS_MIN_SCORE = 91
MAX_PR_REVIEW_ITERATIONS = 3
DEFAULT_MAX_REPO_SLOTS = 3
MAX_REPO_SLOTS_CAP = 4
MAX_REPO_SLOTS_ENV = "AGY_DEV_MAX_SLOTS_PER_REPO"
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

_HEX8_CID_RE = re.compile(r"^[0-9a-f]{8}$")


def _is_same_conversation_id(cid_a: str, cid_b: str) -> bool:
    """Return True on exact match or when one side is an 8-char hex short ID prefix of the other."""
    a = str(cid_a or "").strip().lower()
    b = str(cid_b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    if _HEX8_CID_RE.fullmatch(a) and b.startswith(a):
        return True
    if _HEX8_CID_RE.fullmatch(b) and a.startswith(b):
        return True
    return False


def _build_slot_lock_id(
    cid_clean: str,
    branch_timestamp: str,
    *,
    slot_id: int = 1,
    action_id: str = "",
    existing_lock_ids: set[str] | None = None,
) -> str:
    """Build a unique `lock_id` (`lock-<cid8>-<ts>[-s2[-<scope>]]`) across slots sharing `cid[:8]`."""
    cid8 = cid_clean[:8] or "lock"
    base_id = f"lock-{cid8}-{branch_timestamp}"
    taken = {str(x).strip() for x in (existing_lock_ids or set()) if str(x).strip()}
    if slot_id <= 1 and base_id not in taken:
        return base_id
    act_slug = _slugify_branch_token(action_id, max_len=16) if action_id.strip() else ""
    if slot_id > 1:
        cand = f"{base_id}-s{slot_id}"
        if cand in taken or any(ex.startswith(f"lock-{cid8}-") for ex in taken):
            if act_slug:
                cand = f"{base_id}-s{slot_id}-{act_slug}"
            if cand in taken or any(ex == cand for ex in taken):
                digest = hashlib.sha256(cid_clean.encode("utf-8")).hexdigest()[:6]
                cand = f"{base_id}-s{slot_id}-{digest}"
        return cand
    if act_slug:
        cand = f"{base_id}-{act_slug}"
        if cand not in taken:
            return cand
    digest = hashlib.sha256(cid_clean.encode("utf-8")).hexdigest()[:6]
    return f"{base_id}-{digest}"


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
    "dev_model",
    "last_reviewer_model",
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
    """Resolve `max_slots` per repository clamped to `[1..MAX_REPO_SLOTS_CAP]` (default `3`, env `AGY_DEV_MAX_SLOTS_PER_REPO`)."""
    for raw in (explicit_max_slots, str(os.environ.get(MAX_REPO_SLOTS_ENV) or "").strip()):
        if raw is None or raw == "" or isinstance(raw, bool):
            continue
        with contextlib.suppress(TypeError, ValueError):
            val = int(raw)
            if val >= 1:
                return min(val, MAX_REPO_SLOTS_CAP)
    return DEFAULT_MAX_REPO_SLOTS


def _slot_sort_key(raw_key: Any) -> int:
    """Return the integer slot number for a `slots` dict key (non-numeric keys sort last)."""
    try:
        return int(str(raw_key).strip())
    except (TypeError, ValueError):
        return 10_000


def _secondary_slot_keys(slots_map: Mapping[str, Any] | None) -> list[str]:
    """Return ordered active secondary slot keys (`'2'..'N'`) from a normalized `slots` mapping."""
    if not isinstance(slots_map, Mapping):
        return []
    keys = [
        str(k)
        for k, v in slots_map.items()
        if isinstance(v, dict)
        and bool(v.get("active", True))
        and 2 <= _slot_sort_key(k) <= MAX_REPO_SLOTS_CAP
    ]
    return sorted(keys, key=_slot_sort_key)


def _next_free_slot_id(slots_map: Mapping[str, Any] | None, max_slots: int) -> int | None:
    """Return the lowest free parallel slot id in `[2..max_slots]`, or None when every slot is occupied."""
    taken = {_slot_sort_key(k) for k in _secondary_slot_keys(slots_map)}
    for cand in range(2, max(1, min(int(max_slots), MAX_REPO_SLOTS_CAP)) + 1):
        if cand not in taken:
            return cand
    return None


def _select_promotion_slot_key(lock_obj: Mapping[str, Any], follower_keys: Sequence[str]) -> str:
    """Pick the follower slot to promote into Slot #1: earliest in combined `landing_order`, then lowest slot id."""
    keys = [str(k) for k in follower_keys]
    if not keys:
        return ""
    slots_map = lock_obj.get("slots") if isinstance(lock_obj.get("slots"), Mapping) else {}
    combined: list[str] = [str(c).strip() for c in (lock_obj.get("landing_order") or []) if str(c).strip()]
    for f_key in sorted(keys, key=_slot_sort_key):
        f_obj = slots_map.get(f_key) if isinstance(slots_map, Mapping) else None
        if isinstance(f_obj, Mapping):
            combined.extend(str(c).strip() for c in (f_obj.get("landing_order") or []) if str(c).strip())
    combined = list(dict.fromkeys(combined))

    def _rank(f_key: str) -> tuple[int, int]:
        f_obj = slots_map.get(f_key) if isinstance(slots_map, Mapping) else None
        f_cid = str(f_obj.get("conversation_id") or "").strip() if isinstance(f_obj, Mapping) else ""
        pos = next((i for i, c in enumerate(combined) if f_cid and _is_same_conversation_id(c, f_cid)), len(combined) + 1)
        return (pos, _slot_sort_key(f_key))

    return min(keys, key=_rank)


def _slot_owned_by_conversation(slot_obj: Mapping[str, Any] | None, cid_clean: str) -> bool:
    """Return True when `cid_clean` owns `slot_obj` (`conversation_id` or `sibling_conversation_ids`)."""
    if not isinstance(slot_obj, Mapping) or not cid_clean:
        return False
    holder = str(slot_obj.get("conversation_id") or "").strip()
    sibs = [str(s).strip() for s in (slot_obj.get("sibling_conversation_ids") or [])]
    return bool(holder) and (
        _is_same_conversation_id(holder, cid_clean) or any(_is_same_conversation_id(s, cid_clean) for s in sibs)
    )


def _hydrate_anchors_from_queue(
    conversation_id: str,
    *,
    repo_slug: str = "",
    portfolio_dir: Path | str | None = None,
) -> tuple[list[str], list[str]]:
    """Return repo-scoped `(target_files, section_anchors)` recorded on the `dev_queue.json` item for `conversation_id`.

    Repo scoping: entries prefixed with `<owner/repo>:` (or `<repo>:`) are kept (prefix stripped) only when the
    prefix matches `repo_slug`; entries prefixed with any other repository are dropped; unprefixed entries are kept.
    Per-repo maps (`target_files_by_repo` / `section_anchors_by_repo`) take precedence when present.
    """
    cid_clean = str(conversation_id or "").strip()
    if not cid_clean:
        return [], []
    try:
        q_snap = _get_queue_store().load_dev_queue(portfolio_dir=portfolio_dir)
    except Exception:  # noqa: BLE001 - hydration is best-effort and must never block lock acquisition
        return [], []
    items = q_snap.get("items") if isinstance(q_snap, dict) else None
    if not isinstance(items, list):
        return [], []
    match = next(
        (
            it
            for it in items
            if isinstance(it, dict) and _is_same_conversation_id(str(it.get("conversation_id") or ""), cid_clean)
        ),
        None,
    )
    if not isinstance(match, dict):
        return [], []
    clean_slug = dev_common.normalize_repo_slug(repo_slug) or str(repo_slug or "").strip().lower()

    def _scope(raw_vals: Any, by_repo_key: str) -> list[str]:
        by_repo = match.get(by_repo_key)
        if isinstance(by_repo, dict) and clean_slug:
            for rk, rv in by_repo.items():
                if _parallel_scope._repo_prefix_matches_slug(str(rk), clean_slug) and isinstance(rv, list):
                    return list(dict.fromkeys(str(v).strip() for v in rv if str(v).strip()))
        if isinstance(raw_vals, str):
            raw_vals = [raw_vals]
        elif isinstance(raw_vals, Mapping):
            raw_vals = [
                f"{fk}#{str(fv).strip()}"
                for fk, fvals in raw_vals.items()
                for fv in (fvals if isinstance(fvals, (list, tuple, set)) else [fvals])
                if str(fv).strip()
            ]
        out: list[str] = []
        for raw in raw_vals if isinstance(raw_vals, (list, tuple)) else []:
            val = str(raw or "").strip()
            if not val:
                continue
            pref, rest = _parallel_scope.split_repo_prefixed_target(val)
            if pref:
                if clean_slug and not _parallel_scope._repo_prefix_matches_slug(pref, clean_slug):
                    continue
                val = rest.strip()
            if val:
                out.append(val)
        return list(dict.fromkeys(out))

    return (
        _scope(match.get("target_files"), "target_files_by_repo"),
        _scope(match.get("section_anchors"), "section_anchors_by_repo"),
    )


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
        for add_k in ("base_sha", "overlap_tier", "landing_order", "section_anchors", "review_approved", "approved_head_sha"):
            if not slot1.get(add_k) and raw_slots["1"].get(add_k):
                slot1[add_k] = raw_slots["1"][add_k]
    slot1["slot_id"] = 1
    slot1["active"] = True

    slots_clean: dict[str, dict[str, Any]] = {"1": slot1}
    for s_key in _secondary_slot_keys(raw_slots):
        s_num = _slot_sort_key(s_key)
        s_obj = _build_slot_dict_from_mapping(raw_slots[s_key], s_num)
        s_obj["slot_id"] = s_num
        s_obj["active"] = True
        slots_clean[str(s_num)] = s_obj

    for k, v in slot1.items():
        if k not in ("slot_id", "active"):
            out[k] = v
    out["active"] = True
    out["slot_id"] = 1
    # Never shrink below the number of occupied slots (env downgrade must not orphan in-flight followers).
    out["max_slots"] = min(MAX_REPO_SLOTS_CAP, max(max_slots, max(_slot_sort_key(k) for k in slots_clean)))
    out["active_slots_count"] = len(slots_clean)
    out["slot_occupancy"] = f"{len(slots_clean)}/{out['max_slots']}"
    out["slots"] = slots_clean

    # Keep `repo_branches` synchronized so `dev_common.get_active_dev_pr_lock_protections()` protects Slot #2..#N worktrees too
    raw_rb = out.get("repo_branches") if isinstance(out.get("repo_branches"), dict) else {}
    clean_rb = {
        str(k): dict(v)
        for k, v in raw_rb.items()
        if str(k).strip() and not _is_slot_scoped_rb_key(str(k)) and isinstance(v, dict)
    }
    if eff_slug:
        for s_key in _secondary_slot_keys(slots_clean):
            s_entry = slots_clean[s_key]
            s_num = _slot_sort_key(s_key)
            clean_rb[_slot_repo_branch_key(eff_slug, s_num)] = {
                "repo_slug": eff_slug,
                "slot_id": s_num,
                "branch_name": str(s_entry.get("branch_name") or ""),
                "branch_timestamp": str(s_entry.get("branch_timestamp") or ""),
                "target_repo_dir": str(s_entry.get("target_repo_dir") or out.get("target_repo_dir") or ""),
                "worktree_path": str(s_entry.get("worktree_path") or ""),
                "manual_pr_url": "",
                "branch_pushed": False,
                "head_sha": str(s_entry.get("head_sha") or s_entry.get("staged_head_sha") or ""),
                "updated_at": str(s_entry.get("updated_at") or out.get("updated_at") or ""),
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
    """Identify which active slot key (`'1'`..`'N'`) a selector targets inside an active repository lock."""
    slots_map = lock_obj.get("slots") if isinstance(lock_obj.get("slots"), dict) else {}
    sec_keys = _secondary_slot_keys(slots_map)
    if slot_id is not None and str(slot_id).strip():
        req_key = str(slot_id).strip()
        if req_key == "1" or req_key in sec_keys:
            return req_key
        if _slot_sort_key(req_key) <= MAX_REPO_SLOTS_CAP:
            # Explicit but inactive secondary slot: preserve caller intent (callers error out cleanly).
            return req_key
    if not sec_keys:
        return "1"

    s1_br = str(lock_obj.get("branch_name") or "").strip()
    clean_br = (branch_name or "").strip().removeprefix("refs/heads/")
    if clean_br:
        for s_key in sec_keys:
            if str(slots_map[s_key].get("branch_name") or "").strip() == clean_br:
                return s_key
        if s1_br and s1_br == clean_br:
            return "1"

    if workdir:
        for s_key in sec_keys:
            if _path_matches_slot_worktree(workdir, str(slots_map[s_key].get("worktree_path") or "")):
                return s_key
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
            if wd_br:
                for s_key in sec_keys:
                    if str(slots_map[s_key].get("branch_name") or "").strip() == wd_br:
                        return s_key
                if s1_br and wd_br == s1_br:
                    return "1"

    clean_cid = (conversation_id or "").strip()
    if clean_cid:
        for s_key in sec_keys:
            if _slot_owned_by_conversation(slots_map[s_key], clean_cid):
                return s_key

    return "1"


def _project_slot_view(lock_obj: dict[str, Any], slot_key: str) -> dict[str, Any]:
    """Return a caller-facing lock dict representing `slot_key` (`'1'`..`'N'`) without bleeding Slot #1 fields into followers."""
    norm = _sync_lock_slots_structure(lock_obj)
    slot_key = str(slot_key or "1").strip()
    if slot_key == "1" or slot_key not in norm.get("slots", {}):
        return norm
    slot_n = dict(norm["slots"][slot_key])
    slot_num = _slot_sort_key(slot_key)
    slot1 = dict(norm["slots"]["1"])
    projected: dict[str, Any] = {
        "active": True,
        "vcs_mode": str(norm.get("vcs_mode") or "native_git"),
        "repo_slug": str(norm.get("repo_slug") or norm.get("repo") or ""),
        "repo": str(norm.get("repo") or norm.get("repo_slug") or ""),
        "slot_id": slot_num,
        "max_slots": int(norm.get("max_slots") or DEFAULT_MAX_REPO_SLOTS),
        "active_slots_count": int(norm.get("active_slots_count") or len(norm.get("slots") or {})),
        "slots": norm.get("slots", {}),
        "repo_branches": norm.get("repo_branches", {}),
        "target_repo_dir": str(slot_n.get("target_repo_dir") or norm.get("target_repo_dir") or ""),
    }
    for k, v in slot_n.items():
        projected[k] = v
    projected["active"] = True
    projected["slot_id"] = slot_num
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
    dev_model: str = "",
    allow_parallel_slot: bool = False,
) -> dict[str, Any]:
    """Acquire a 1..N-slot per-repository development lock (`dev_pr_locks[repo_slug]`, `N = max_slots`, default 3, cap 4).

    Backward-compatibly preserves Slot #1 on top-level lock fields while supporting parallel follower slots
    `slots["2"]..slots["N"]` when the candidate's blast-radius overlap is `NONE`/`TRANSITIVE` against EVERY active slot.
    `allow_slot2` / `force_slot2` are kept as backward-compatible aliases for any parallel follower slot
    (`allow_parallel_slot` is the generalized alias). Omitted `target_files` / `section_anchors` are auto-hydrated
    from the repo-scoped `dev_queue.json` item for `conversation_id` (explicit values always win).
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
    allow_slot2 = bool(allow_slot2 or allow_parallel_slot)

    # (B1) Auto-hydrate omitted `target_files` / `section_anchors` from `dev_queue.json` (explicit overrides win).
    hydrated_fields: list[str] = []
    if not clean_targets or not clean_anchors:
        h_targets, h_anchors = _hydrate_anchors_from_queue(
            cid_clean,
            repo_slug=resolved_slug,
            portfolio_dir=portfolio_dir,
        )
        if not clean_targets and h_targets:
            clean_targets = h_targets
            hydrated_fields.append("target_files")
        if not clean_anchors and h_anchors:
            clean_anchors = h_anchors
            hydrated_fields.append("section_anchors")

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
            slots_existing = dict(norm_existing.get("slots") or {})
            holder_cid = str(norm_existing.get("conversation_id") or "").strip()
            existing_siblings = [str(s).strip() for s in (norm_existing.get("sibling_conversation_ids") or [])]
            same_conv_slot1 = bool(
                not holder_cid
                or _is_same_conversation_id(holder_cid, cid_clean)
                or any(_is_same_conversation_id(s, cid_clean) for s in existing_siblings)
            )
            slot2_existing: dict[str, Any] = {}
            same_conv_slot2 = False
            same_conv_key = next(
                (k for k in _secondary_slot_keys(slots_existing) if _slot_owned_by_conversation(slots_existing[k], cid_clean)),
                "",
            )
            if same_conv_key:
                slot2_existing = dict(slots_existing[same_conv_key])
                same_conv_slot2 = True

            existing_status = str(norm_existing.get("status") or "")
            # Case A: Re-acquiring or allocating a parallel follower Slot #2..#N
            if not same_conv_slot1:
                target_slot_num: int | None = (
                    _slot_sort_key(same_conv_key) if same_conv_key else _next_free_slot_id(slots_existing, eff_max_slots)
                )
                can_try_slot2 = target_slot_num is not None and (same_conv_slot2 or eff_max_slots >= 2)
                ov_tier_resolved = (overlap_tier or "").strip()
                ov_order_resolved = list(landing_order or [])
                ov_dict: dict[str, Any] = {}
                slot2_allowed = False
                active_slot_list = [slots_existing["1"], *(slots_existing[k] for k in _secondary_slot_keys(slots_existing))]

                if can_try_slot2:
                    if same_conv_slot2:
                        slot2_allowed = True
                        ov_tier_resolved = ov_tier_resolved or str(slot2_existing.get("overlap_tier") or OVERLAP_NONE)
                        ov_order_resolved = ov_order_resolved or list(slot2_existing.get("landing_order") or [])
                    elif force_slot2:
                        slot2_allowed = True
                        ov_tier_resolved = ov_tier_resolved or OVERLAP_TRANSITIVE
                    elif allow_slot2 or (
                        bool(worktree_path)
                        and bool(clean_targets)
                        and any(bool(s.get("target_files")) for s in active_slot_list)
                    ):
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
                        multi_res = _parallel_scope.analyze_candidate_against_slots(
                            [
                                TaskAnchorSpec.from_mapping(norm_existing if str(s.get("slot_id") or 1) == "1" else s)
                                for s in active_slot_list
                            ],
                            cand_spec,
                            repo_dir=persistent_repo_dir,
                            repo_slug=resolved_slug,
                        )
                        pairs = list(multi_res.get("pairwise") or [])
                        for p_idx, p_dict in enumerate(pairs):
                            p_dict["slot_id"] = int(active_slot_list[p_idx].get("slot_id") or p_idx + 1)
                        ov_dict = dict(pairs[0]) if len(pairs) == 1 else {}
                        ov_dict.update(
                            {
                                "overlap_tier": multi_res.get("overlap_tier"),
                                "parallel_safe": bool(multi_res.get("parallel_safe")),
                                "landing_order": list(multi_res.get("landing_order") or []),
                                "pairwise": pairs,
                                "blocking_slot_ids": [
                                    int(p.get("slot_id") or 0) for p in pairs if not bool(p.get("parallel_safe"))
                                ],
                            }
                        )
                        ov_tier_resolved = str(multi_res.get("overlap_tier") or "")
                        ov_order_resolved = list(multi_res.get("landing_order") or [])
                        slot2_allowed = bool(multi_res.get("parallel_safe"))

                if slot2_allowed and target_slot_num is not None:
                    slot_num = int(target_slot_num)
                    slot_key = str(slot_num)
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
                    slot1_lock_ids = {
                        str(norm_existing.get("lock_id") or "").strip(),
                        *(
                            str(s_val.get("lock_id") or "").strip()
                            for s_k, s_val in slots_existing.items()
                            if isinstance(s_val, dict) and str(s_k) != slot_key
                        ),
                    }
                    s2_lock_id = (
                        str(prev_s2.get("lock_id") or "").strip()
                        if (same_conv_slot2 and reuse_active_branch and str(prev_s2.get("lock_id") or "").strip())
                        else _build_slot_lock_id(
                            cid_clean,
                            s2_ts,
                            slot_id=slot_num,
                            action_id=str(action_id or prev_s2.get("action_id") or ""),
                            existing_lock_ids=slot1_lock_ids,
                        )
                    )
                    default_order = [
                        str(s.get("conversation_id") or "").strip()
                        for s in active_slot_list
                        if str(s.get("conversation_id") or "").strip()
                        and not _is_same_conversation_id(str(s.get("conversation_id") or ""), cid_clean)
                    ] + [cid_clean]
                    slot2_obj: dict[str, Any] = {
                        "slot_id": slot_num,
                        "active": True,
                        "lock_id": s2_lock_id,
                        "conversation_id": cid_clean,
                        "sibling_conversation_ids": clean_siblings,
                        "action_id": str(action_id or prev_s2.get("action_id") or "").strip(),
                        "notification_source": "Ad-Hoc",
                        "branch_timestamp": s2_ts,
                        "branch_name": s2_branch,
                        "base_sha": resolved_base_sha or str(prev_s2.get("base_sha") or ""),
                        "overlap_tier": ov_tier_resolved or OVERLAP_NONE,
                        "landing_order": ov_order_resolved or default_order,
                        "section_anchors": clean_anchors or list(prev_s2.get("section_anchors") or []),
                        "branch_pushed": False,
                        "pr_gate_ready": False,
                        "review_approved": bool(prev_s2.get("review_approved")) if same_conv_slot2 else False,
                        "approved_head_sha": str(prev_s2.get("approved_head_sha") or "") if same_conv_slot2 else "",
                        "deferred_send_email": False,
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
                        "dev_model": str(dev_model or prev_s2.get("dev_model") or "").strip(),
                        "last_activity_at": now_iso,
                        "updated_at": now_iso,
                    }
                    slots_existing[slot_key] = slot2_obj
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
                    slot_res: dict[str, Any] = {
                        "status": "ok",
                        "acquired": True,
                        "slot_id": slot_num,
                        "overlap_tier": slot2_obj["overlap_tier"],
                        "landing_order": slot2_obj["landing_order"],
                        "repo_slug": resolved_slug,
                        "max_slots": int(updated_repo_lock.get("max_slots") or eff_max_slots),
                        "active_slots_count": int(updated_repo_lock.get("active_slots_count") or 0),
                        "active_locks_count": active_count,
                        "lock": _project_slot_view(updated_repo_lock, slot_key),
                        "repo_lock": updated_repo_lock,
                    }
                    if hydrated_fields:
                        slot_res["hydrated_from_queue"] = list(hydrated_fields)
                    if ov_dict:
                        slot_res["overlap_analysis"] = ov_dict
                    return slot_res

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
                    "max_slots": eff_max_slots,
                    "active_slots_count": len(active_slot_list),
                    "slots_full": target_slot_num is None,
                }
                if target_slot_num is None:
                    blocked_payload["error"] += (
                        f" All {eff_max_slots} parallel slot(s) are occupied (max_slots={eff_max_slots}, "
                        f"`{MAX_REPO_SLOTS_ENV}` clamps 1..{MAX_REPO_SLOTS_CAP})."
                    )
                if hydrated_fields:
                    blocked_payload["hydrated_from_queue"] = list(hydrated_fields)
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

        same_prev = isinstance(existing_lock, dict) and _is_same_conversation_id(
            str(existing_lock.get("conversation_id") or ""),
            cid_clean,
        )
        prev_gate_ready = bool(existing_lock.get("pr_gate_ready")) if (same_prev and not reuse_active_branch) else False
        raw_prev_slots = (
            existing_lock.get("slots")
            if isinstance(existing_lock, dict) and isinstance(existing_lock.get("slots"), dict)
            else {}
        )
        preserved_followers: dict[str, dict[str, Any]] = {
            k: dict(raw_prev_slots[k]) for k in _secondary_slot_keys(raw_prev_slots)
        }
        existing_s2_lock_ids = {
            str(s_val.get("lock_id") or "").strip()
            for s_val in preserved_followers.values()
            if str(s_val.get("lock_id") or "").strip()
        }
        s1_lock_id = (
            str(existing_lock.get("lock_id") or "").strip()
            if (same_prev and reuse_active_branch and str(existing_lock.get("lock_id") or "").strip())
            else _build_slot_lock_id(
                cid_clean,
                resolved_ts,
                slot_id=1,
                action_id=str(action_id or ""),
                existing_lock_ids=existing_s2_lock_ids,
            )
        )
        s1_cid_recorded = (
            str(existing_lock.get("conversation_id") or "").strip()
            if (same_prev and len(str(existing_lock.get("conversation_id") or "").strip()) > len(cid_clean))
            else cid_clean
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
            "landing_order": list(landing_order or ([s1_cid_recorded] if not same_prev else (existing_lock.get("landing_order") or [s1_cid_recorded]))),
            "section_anchors": clean_anchors or (list(existing_lock.get("section_anchors") or []) if same_prev else []),
            "lock_id": s1_lock_id,
            "conversation_id": s1_cid_recorded,
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
            "target_files": clean_targets or (list(existing_lock.get("target_files") or []) if same_prev else []),
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
            "dev_model": (
                str(dev_model or "").strip()
                or (str(existing_lock.get("dev_model") or "").strip() if same_prev else "")
            ),
            "last_activity_at": now_iso,
            "updated_at": now_iso,
        }
        if preserved_followers:
            new_lock_raw["slots"] = preserved_followers
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
    s1_res: dict[str, Any] = {
        "status": "ok",
        "acquired": True,
        "slot_id": 1,
        "repo_slug": resolved_slug,
        "max_slots": int(new_lock.get("max_slots") or eff_max_slots),
        "active_slots_count": int(new_lock.get("active_slots_count") or 1),
        "active_locks_count": active_count,
        "lock": new_lock,
    }
    if hydrated_fields:
        s1_res["hydrated_from_queue"] = list(hydrated_fields)
    return s1_res


def _merge_subagent_cids(existing: Any, incoming: Any) -> list[str]:
    """Return deduplicated non-empty subagent conversation IDs preserving insertion order."""
    merged: list[str] = []
    for raw_list in (existing if isinstance(existing, (list, tuple)) else [], incoming if isinstance(incoming, (list, tuple)) else []):
        for raw in raw_list:
            clean = str(raw or "").strip()
            if clean and clean not in merged:
                merged.append(clean)
    return merged


def update_dev_pr_lock(
    updates: dict[str, Any],
    portfolio_dir: Path | str | None = None,
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    branch_name: str = "",
    slot_id: int | str | None = None,
    preserve_statuses: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Update fields on the matching active per-repository lock (Slot #1 or follower Slot #2..#N) inside `actions_state.json`."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_path = p_dir / "actions_state.json"
    lock_file = state_path.with_suffix(".json.lock")
    now_iso = _utc_now_iso()
    protected_statuses = {str(s).strip().upper() for s in (preserve_statuses or ()) if str(s).strip()}
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

        if matched_slot != "1":
            if matched_slot not in norm_cand.get("slots", {}):
                return {
                    "status": "error",
                    "error": f"No active Slot #{matched_slot} dev_pr_lock to update for `{target_slug or 'default'}`.",
                }
            slot2_cur = dict(norm_cand["slots"][matched_slot])
            cur_s2_status = str(slot2_cur.get("status") or "").strip().upper()
            protect_s2 = bool(protected_statuses and cur_s2_status in protected_statuses)
            for k, v in updates.items():
                if v is None or k in ("slots", "repo_branches"):
                    continue
                if protect_s2 and k == "status" and str(v).strip().upper() not in protected_statuses:
                    continue
                if k == "subagent_conversation_ids" and isinstance(v, (list, tuple)):
                    slot2_cur[k] = _merge_subagent_cids(slot2_cur.get(k), v)
                elif k in sanitize_str_keys and isinstance(v, str):
                    slot2_cur[k] = _sanitize_home_paths(v)
                else:
                    slot2_cur[k] = v
            slot2_cur["slot_id"] = _slot_sort_key(matched_slot)
            slot2_cur["active"] = True
            slot2_cur["last_activity_at"] = str(updates.get("last_activity_at") or now_iso)
            slot2_cur["updated_at"] = now_iso
            norm_cand["slots"][matched_slot] = slot2_cur
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
                "slot_id": _slot_sort_key(matched_slot),
                "lock": _project_slot_view(current_lock, matched_slot),
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
            same_conv = _is_same_conversation_id(req_cid, holder_cid)
            if not same_conv:
                return {
                    "status": "error",
                    "error": (
                        f"Refusing cross-branch/cross-repo lock overwrite on `{target_slug}`: "
                        f"active lock holds `{existing_branch}`, requested `{req_branch}`."
                    ),
                }
        current_lock = dict(norm_cand)
        slot1_mirror = (
            dict(current_lock["slots"]["1"])
            if isinstance(current_lock.get("slots"), dict) and isinstance(current_lock["slots"].get("1"), dict)
            else None
        )
        cur_s1_status = str(current_lock.get("status") or "").strip().upper()
        protect_s1 = bool(protected_statuses and cur_s1_status in protected_statuses)
        for k, v in updates.items():
            if v is None or k == "slots":
                continue
            if protect_s1 and k == "status" and str(v).strip().upper() not in protected_statuses:
                continue
            if protect_s1 and cur_s1_status == "PENDING_HUMAN_PR_VALIDATION" and bool(current_lock.get("branch_pushed")) and k == "head_sha":
                continue
            if k == "repo_branches" and isinstance(v, dict):
                current_lock["repo_branches"] = {
                    str(rb_k): dict(rb_v)
                    for rb_k, rb_v in v.items()
                    if str(rb_k).strip() and not _is_slot_scoped_rb_key(str(rb_k)) and isinstance(rb_v, dict)
                }
            elif k == "subagent_conversation_ids" and isinstance(v, (list, tuple)):
                merged_cids = _merge_subagent_cids(current_lock.get(k), v)
                current_lock[k] = merged_cids
                if slot1_mirror is not None:
                    slot1_mirror[k] = merged_cids
            elif k in sanitize_str_keys and isinstance(v, str):
                san_val = _sanitize_home_paths(v)
                current_lock[k] = san_val
                if slot1_mirror is not None:
                    slot1_mirror[k] = san_val
            else:
                current_lock[k] = v
                if slot1_mirror is not None:
                    slot1_mirror[k] = v
        if slot1_mirror is not None and isinstance(current_lock.get("slots"), dict):
            current_lock["slots"] = {**current_lock["slots"], "1": slot1_mirror}
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
    reviewer_subagent_id: str = "",
    stage_label: str = "",
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Stamp `last_activity_at` and optional `worker_cid`, `reviewer_subagent_id`, and `stage_label` on an active lock."""
    now_iso = _utc_now_iso()
    updates: dict[str, Any] = {"last_activity_at": now_iso}
    if str(worker_cid or "").strip():
        updates["worker_cid"] = str(worker_cid).strip()
    rev_cid = str(reviewer_subagent_id or "").strip()
    if rev_cid:
        updates["reviewer_subagent_id"] = rev_cid
        updates["subagent_conversation_ids"] = [rev_cid]
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

    When parallel follower slots (`slots['2']..slots['N']`) are active on the target repository and `release_all=False`:
      - Releasing Slot #1 finalizes Slot #1 and promotes the next follower (earliest in `landing_order`, then lowest
        slot id) into Slot #1 (`READY_AWAITING_BASE_MERGE` -> `NEEDS_REBASE_REVERIFY`); other followers keep their slots.
      - Releasing a follower Slot #K finalizes only that slot while keeping Slot #1 and other followers untouched.
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
                follower_keys = _secondary_slot_keys(slots_cur)
                if follower_keys:
                    rel_slot_key = _match_slot_key(
                        norm_cur,
                        branch_name=branch_name,
                        workdir=workdir,
                        slot_id=slot_id,
                    )
                    if rel_slot_key != "1" and rel_slot_key in follower_keys:
                        slot2_rel = dict(slots_cur[rel_slot_key])
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
                        slots_cur.pop(rel_slot_key, None)
                        norm_cur["slots"] = slots_cur
                        locks_map[slug] = _sync_lock_slots_structure(norm_cur, repo_slug=slug)
                        last_slug = slug
                        continue
                    if rel_slot_key != "1":
                        # Explicit selector for an inactive follower slot: never fall through to releasing Slot #1.
                        continue

                    # Releasing Slot #1 while followers are active -> finalize Slot #1 and promote the next follower
                    # in `landing_order` (fallback: lowest slot id) into Slot #1; remaining followers keep their slots
                    # and stay `READY_AWAITING_BASE_MERGE` until their own promotion (max 1 open GitHub PR per repo).
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
                    promote_key = _select_promotion_slot_key(norm_cur, follower_keys)
                    promoted_from = _slot_sort_key(promote_key)
                    slot2_prom = dict(slots_cur[promote_key])
                    slot2_prom["slot_id"] = 1
                    slot2_prom["active"] = True
                    slot2_prom["promoted_from_slot"] = promoted_from
                    slot2_prom["promoted_at"] = now_iso
                    slot2_prom["previous_slot1_branch"] = str(slot1_rel.get("branch_name") or "")
                    slot2_prom["previous_slot1_release_reason"] = reason
                    if str(slot2_prom.get("status") or "") == SLOT_STATUS_READY_AWAITING_BASE_MERGE:
                        slot2_prom["status"] = SLOT_STATUS_NEEDS_REBASE_REVERIFY
                        slot2_prom["branch_action"] = SLOT_STATUS_NEEDS_REBASE_REVERIFY
                    elif reason in ("BRANCH_MERGED_INTO_MAIN", "HUMAN_VALIDATED_MANUAL_PR"):
                        slot2_prom["needs_base_rebase"] = True
                    slot2_prom["updated_at"] = now_iso
                    remaining_followers: dict[str, dict[str, Any]] = {}
                    for f_key in follower_keys:
                        if f_key == promote_key:
                            continue
                        f_obj = dict(slots_cur[f_key])
                        if reason in ("BRANCH_MERGED_INTO_MAIN", "HUMAN_VALIDATED_MANUAL_PR"):
                            f_obj["needs_base_rebase"] = True
                        remaining_followers[f_key] = f_obj

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
                    if remaining_followers:
                        promoted_raw["slots"] = remaining_followers
                    promoted_norm = _sync_lock_slots_structure(promoted_raw, repo_slug=slug)
                    locks_map[slug] = promoted_norm
                    promoted_slots[slug] = promoted_norm
                    last_slug = slug
                    continue

            rel_lock = raw_cur
            if release_all and isinstance(rel_lock.get("slots"), dict):
                for f_key in _secondary_slot_keys(rel_lock["slots"]):
                    s2_extra = dict(rel_lock["slots"][f_key])
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
                _qs = _get_queue_store()
                for q_cid in [cid_clean, *siblings]:
                    _qs._mark_queue_item_repo_merged(
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
                _is_same_conversation_id(str(al.get("conversation_id") or ""), cid_clean)
                or any(_is_same_conversation_id(str(s), cid_clean) for s in (al.get("sibling_conversation_ids") or []))
                for al in rem_locks.values()
            )
            if still_locked:
                continue
            with contextlib.suppress(Exception):
                _qs = _get_queue_store()
                q_snap = _qs.load_dev_queue(portfolio_dir=portfolio_dir)
                existing_it = next(
                    (it for it in (q_snap.get("items") or []) if _is_same_conversation_id(str(it.get("conversation_id") or ""), cid_clean)),
                    None,
                )
                if isinstance(existing_it, dict) and str(existing_it.get("source") or "") == "ad_hoc_lock":
                    rel_br = str(released_lock.get("branch_name") or "").strip()
                    _qs.update_dev_queue_item_status(
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

    _facade_attr("stop_dev_api_server", stop_dev_api_server)(portfolio_dir=portfolio_dir)
    is_orch_paused = False
    with contextlib.suppress(Exception):
        import orchestration_control as _orch_ctrl_mod

        is_orch_paused = bool(_orch_ctrl_mod.is_orchestration_paused(portfolio_dir=portfolio_dir))
    if not is_orch_paused:
        _facade_attr("restart_pro_api_server", restart_pro_api_server)(portfolio_dir=portfolio_dir)

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
    raw_slots_info = lock_info.get("slots") if isinstance(lock_info.get("slots"), dict) else {}
    follower_branches = [
        br
        for f_key in _secondary_slot_keys(raw_slots_info)
        for br in [str(raw_slots_info[f_key].get("branch_name") or "").strip()]
        if br
    ]
    extra_protected = list(follower_branches)

    if not branch_name or not branch_pushed:
        prot_list = [b for b in (branch_name, *follower_branches) if b]
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
            protected_branches=[b for b in (branch_name, *follower_branches) if b],
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
        _facade_attr("stop_dev_api_server", stop_dev_api_server)(portfolio_dir=portfolio_dir)
        scoped_slug, repo_sync = _sync_unlocked_repo_with_git(
            repo_slug=repo_slug,
            workdir=workdir,
            portfolio_dir=portfolio_dir,
            git_client=git_client,
        )
        queue_recon = _get_queue_store().reconcile_dev_queue_with_git(
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
            queue_recon = _get_queue_store().reconcile_dev_queue_with_git(
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

    queue_recon = _get_queue_store().reconcile_dev_queue_with_git(
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


def _escape_md_cell(value: Any, *, max_len: int = 120) -> str:
    """Sanitize a table cell for GitHub-Flavored Markdown rendering."""
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ").strip()
    text = text.replace("|", "\\|")
    if max_len > 3 and len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text


def _summarize_lock_slot(slot_obj: Mapping[str, Any], *, fallback_slot_id: int = 1) -> dict[str, Any]:
    """Project one lock slot dictionary into a minimal, token-saving summary."""
    raw_files = slot_obj.get("target_files")
    target_files = [str(p) for p in raw_files if str(p or "").strip()] if isinstance(raw_files, Sequence) and not isinstance(raw_files, (str, bytes)) else []
    try:
        slot_id = int(slot_obj.get("slot_id") or fallback_slot_id)
    except (TypeError, ValueError):
        slot_id = fallback_slot_id
    return {
        "slot_id": slot_id,
        "lock_id": str(slot_obj.get("lock_id") or ""),
        "conversation_id": str(slot_obj.get("conversation_id") or ""),
        "action_id": str(slot_obj.get("action_id") or ""),
        "status": str(slot_obj.get("status") or ""),
        "stage_label": str(slot_obj.get("stage_label") or ""),
        "branch_name": str(slot_obj.get("branch_name") or ""),
        "pr_gate_ready": bool(slot_obj.get("pr_gate_ready", False)),
        "branch_pushed": bool(slot_obj.get("branch_pushed", False)),
        "manual_pr_url": str(slot_obj.get("manual_pr_url") or ""),
        "worker_cid": str(slot_obj.get("worker_cid") or ""),
        "overlap_tier": str(slot_obj.get("overlap_tier") or ""),
        "needs_base_rebase": bool(slot_obj.get("needs_base_rebase", False)),
        "target_files_count": len(target_files),
        "target_files": target_files[:12],
        "summary": str(slot_obj.get("summary") or "")[:180],
    }


def _summarize_repo_lock_entry(repo_slug: str, lock_obj: Mapping[str, Any]) -> dict[str, Any]:
    """Project one per-repository lock entry (normalizing `slots` dict/list into a flat list)."""
    raw_slots = lock_obj.get("slots")
    slot_summaries: list[dict[str, Any]] = []
    if isinstance(raw_slots, Mapping):
        for s_key, s_val in raw_slots.items():
            if not isinstance(s_val, Mapping):
                continue
            try:
                fb_id = int(s_key)
            except (TypeError, ValueError):
                fb_id = len(slot_summaries) + 1
            slot_summaries.append(_summarize_lock_slot(s_val, fallback_slot_id=fb_id))
    elif isinstance(raw_slots, Sequence) and not isinstance(raw_slots, (str, bytes)):
        for idx, s_val in enumerate(raw_slots, start=1):
            if isinstance(s_val, Mapping):
                slot_summaries.append(_summarize_lock_slot(s_val, fallback_slot_id=idx))
    elif bool(lock_obj.get("active", False)):
        slot_summaries.append(_summarize_lock_slot(lock_obj, fallback_slot_id=1))
    slot_summaries.sort(key=lambda item: int(item.get("slot_id") or 1))

    try:
        max_slots = int(lock_obj.get("max_slots") or DEFAULT_MAX_REPO_SLOTS)
    except (TypeError, ValueError):
        max_slots = DEFAULT_MAX_REPO_SLOTS
    active_slots_count = len(slot_summaries) if slot_summaries else int(lock_obj.get("active_slots_count") or 0)
    occupancy = str(lock_obj.get("slot_occupancy") or f"{active_slots_count}/{max_slots}")
    is_active = bool(lock_obj.get("active", bool(slot_summaries)))
    resolved_slug = str(lock_obj.get("repo_slug") or lock_obj.get("repo") or repo_slug)
    return {
        "repo_slug": resolved_slug,
        "active": is_active,
        "status": str(lock_obj.get("status") or ("INACTIVE" if not is_active else "")),
        "slot_occupancy": occupancy,
        "active_slots_count": active_slots_count,
        "max_slots": max_slots,
        "conversation_id": str(lock_obj.get("conversation_id") or ""),
        "branch_name": str(lock_obj.get("branch_name") or ""),
        "pr_gate_ready": bool(lock_obj.get("pr_gate_ready", False)),
        "manual_pr_url": str(lock_obj.get("manual_pr_url") or ""),
        "slots": slot_summaries,
    }


def build_compact_lock_status_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a compact, token-saving JSON projection of a `lock-status` payload."""
    raw_locks = raw.get("locks")
    locks_map: dict[str, dict[str, Any]] = {}
    active_locks: list[dict[str, Any]] = []
    if isinstance(raw_locks, Mapping):
        for slug in sorted(raw_locks.keys()):
            val = raw_locks[slug]
            if isinstance(val, Mapping):
                entry = _summarize_repo_lock_entry(str(slug), val)
                locks_map[str(slug)] = entry
                if entry["active"]:
                    active_locks.append(entry)

    compact_payload: dict[str, Any] = {
        "status": str(raw.get("status") or "ok"),
        "compact": True,
        "active_count": len(active_locks),
        "active": bool(raw.get("active", bool(active_locks))),
    }
    if "released" in raw:
        compact_payload["released"] = bool(raw.get("released", False))
    if raw.get("repo_slug"):
        compact_payload["repo_slug"] = str(raw.get("repo_slug"))
    raw_single = raw.get("lock")
    if isinstance(raw_single, Mapping) and raw_single:
        single_slug = str(raw_single.get("repo_slug") or raw.get("repo_slug") or "")
        compact_payload["lock"] = _summarize_repo_lock_entry(single_slug, raw_single)
    compact_payload["locks"] = locks_map
    compact_payload["active_locks"] = active_locks

    q_recon = raw.get("queue_reconciliation")
    if isinstance(q_recon, Mapping):
        compact_payload["queue_reconciliation"] = {
            "status": str(q_recon.get("status") or "ok"),
            "checked_count": int(q_recon.get("checked_count") or 0),
            "reconciled_count": int(q_recon.get("reconciled_count") or 0),
            "reconciled_items": list(q_recon.get("reconciled_items") or [])[:10],
        }
    return compact_payload


def render_lock_status_markdown(raw: Mapping[str, Any]) -> str:
    """Render a compact Markdown summary table for `pr_remediator.py lock-status`."""
    compact = build_compact_lock_status_payload(raw)
    active_locks = list(compact.get("active_locks") or [])
    header_parts = [f"`{len(active_locks)}` active repo(s)"]
    if compact.get("repo_slug"):
        header_parts.append(f"scope `{compact['repo_slug']}`")
    if "released" in compact:
        header_parts.append(f"released=`{compact['released']}`")
    lines = [f"### Per-Repository Dev PR Locks ({' · '.join(header_parts)})", ""]
    if not active_locks:
        lines.append("_No active repository locks._")
        return "\n".join(lines)

    lines.append("| Repo | Slot | Occupancy | Status | Conversation ID | Branch | PR Gate | Files |")
    lines.append("| :--- | :---: | :---: | :--- | :--- | :--- | :---: | :---: |")
    for repo_entry in active_locks:
        repo_lbl = _escape_md_cell(repo_entry.get("repo_slug") or "", max_len=60)
        occ = _escape_md_cell(repo_entry.get("slot_occupancy") or "", max_len=12)
        slots = repo_entry.get("slots") or []
        if not slots:
            lines.append(
                f"| `{repo_lbl}` | - | `{occ}` | `{_escape_md_cell(repo_entry.get('status') or '', max_len=36)}` "
                f"| `{_escape_md_cell(repo_entry.get('conversation_id') or '-', max_len=36)}` "
                f"| `{_escape_md_cell(repo_entry.get('branch_name') or '-', max_len=60)}` "
                f"| `{'yes' if repo_entry.get('pr_gate_ready') else 'no'}` | `0` |"
            )
            continue
        for slot in slots:
            slot_lbl = f"#{slot.get('slot_id', 1)}"
            st_lbl = _escape_md_cell(slot.get("status") or repo_entry.get("status") or "", max_len=36)
            cid_lbl = _escape_md_cell(slot.get("conversation_id") or "-", max_len=36)
            br_lbl = _escape_md_cell(slot.get("branch_name") or "-", max_len=60)
            pr_lbl = "ready" if slot.get("pr_gate_ready") else ("pushed" if slot.get("branch_pushed") else "local")
            f_cnt = int(slot.get("target_files_count") or 0)
            lines.append(
                f"| `{repo_lbl}` | `{slot_lbl}` | `{occ}` | `{st_lbl}` | `{cid_lbl}` | `{br_lbl}` | `{pr_lbl}` | `{f_cnt}` |"
            )
    return "\n".join(lines)


def register_lock_cli(sub: Any) -> None:
    """Register per-repository lock CLI subcommands (`lock-status`, `acquire-lock`, `release-lock`, `lock-heartbeat`)."""
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
    p_lock.add_argument(
        "--summary",
        dest="summary",
        action="store_true",
        help="Emit compact token-saving JSON projection (shorthand for `--format compact`).",
    )
    p_lock.add_argument(
        "--format",
        dest="output_format",
        choices=("json", "compact", "md"),
        default="json",
        help="Output format: `json` (full default), `compact` (minimal JSON), or `md` (Markdown table).",
    )

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
    p_acq.add_argument(
        "--allow-slot2",
        "--parallel-slot",
        "--allow-parallel-slot",
        dest="allow_slot2",
        action="store_true",
        help="Allow acquiring the next free parallel follower Slot #2..#N when overlap vs ALL active slots passes (`--allow-slot2` kept as alias).",
    )
    p_acq.add_argument(
        "--max-slots",
        dest="max_slots",
        type=int,
        default=None,
        help="Optional per-repo slot capacity override (clamped 1..4; default AGY_DEV_MAX_SLOTS_PER_REPO or 3).",
    )
    p_acq.add_argument("--scheduled", action="store_true", help="Mark as scheduled run.")

    p_rel = sub.add_parser("release-lock", help="Release/delete a per-repository branch development lock.")
    p_rel.add_argument("--reason", default="MANUAL_RELEASE", help="Release reason.")
    p_rel.add_argument("--summary", default="", help="Resolution summary.")
    p_rel.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_rel.add_argument("--workdir", default="", help="Optional repository or worktree directory.")
    p_rel.add_argument("--branch", default="", help="Optional branch name to match lock.")
    p_rel.add_argument("--all", dest="release_all", action="store_true", help="Release all active repository locks.")

    p_hb = sub.add_parser(
        "lock-heartbeat",
        help="Stamp last_activity_at, worker_cid, reviewer_subagent_id, and optional stage label on an active dev_pr_lock.",
    )
    p_hb.add_argument("--workdir", default="", help="Optional repository or worktree directory.")
    p_hb.add_argument("--repo", "--repo-slug", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_hb.add_argument("--branch", default="", help="Optional branch name to match lock.")
    p_hb.add_argument("--worker-cid", dest="worker_cid", default="", help="Active worker subagent conversation ID.")
    p_hb.add_argument(
        "--reviewer-subagent-id",
        "--reviewer-cid",
        dest="reviewer_subagent_id",
        default="",
        help="Active reviewer subagent conversation ID (alias: --reviewer-cid).",
    )
    p_hb.add_argument("--stage", dest="stage_label", default="", help="Optional compact stage label (e.g., coding, tests).")


def dispatch_lock_cli(args: Any) -> int | None:
    """Handle lock-store CLI subcommands; return exit code or None if unhandled."""
    if args.subcommand == "lock-status":
        repo_s = getattr(args, "repo_slug", "")
        workdir_s = getattr(args, "workdir", "") or None
        all_locks = _facade_attr("get_all_dev_pr_locks", get_all_dev_pr_locks)()
        if getattr(args, "sync_git", False):
            res = _facade_attr("sync_dev_pr_lock_with_git", sync_dev_pr_lock_with_git)(
                repo_slug=repo_s,
                workdir=workdir_s,
            )
        else:
            res = {
                "status": "ok",
                "active_count": len(all_locks),
                "lock": _facade_attr("get_dev_pr_lock", get_dev_pr_lock)(repo_slug=repo_s, workdir=workdir_s),
                "locks": all_locks,
            }
        output_fmt = str(getattr(args, "output_format", "json") or "json").strip().lower()
        if output_fmt == "json" and bool(getattr(args, "summary", False)):
            output_fmt = "compact"
        if output_fmt == "md":
            print(render_lock_status_markdown(res))
        elif output_fmt == "compact":
            print(json.dumps(build_compact_lock_status_payload(res), indent=2, ensure_ascii=False))
        else:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "acquire-lock":
        res = _facade_attr("acquire_dev_pr_lock", acquire_dev_pr_lock)(
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
            max_slots=getattr(args, "max_slots", None),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("acquired") else 1

    if args.subcommand == "release-lock":
        res = _facade_attr("release_dev_pr_lock", release_dev_pr_lock)(
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
        hb_kwargs: dict[str, Any] = {
            "workdir": getattr(args, "workdir", "") or None,
            "repo_slug": getattr(args, "repo_slug", ""),
            "branch_name": getattr(args, "branch", ""),
            "worker_cid": getattr(args, "worker_cid", ""),
            "stage_label": getattr(args, "stage_label", ""),
        }
        rev_cid_arg = str(getattr(args, "reviewer_subagent_id", "") or "").strip()
        if rev_cid_arg:
            hb_kwargs["reviewer_subagent_id"] = rev_cid_arg
        res = _facade_attr("record_dev_pr_lock_heartbeat", record_dev_pr_lock_heartbeat)(**hb_kwargs)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    return None
