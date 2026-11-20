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
import contextlib
from datetime import datetime, timezone
import fcntl
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
from git_client import GitVcsIntegrationClient, stamp_repo_version_manifests  # noqa: E402

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
        for rb_key in ("repo_branches", "last_repo_branches"):
            rb_map = entry.get(rb_key)
            if not isinstance(rb_map, dict):
                continue
            for rb_slug, rb_item in rb_map.items():
                if not isinstance(rb_item, dict):
                    continue
                for key in ("worktree_path", "target_repo_dir"):
                    if _matches_path(str(rb_item.get(key) or "")):
                        return dev_common.normalize_repo_slug(rb_slug) or dev_common.normalize_repo_slug(slug) or slug

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
    """Return True if a lock entry references `clean_branch` in active or (optionally) released metadata."""
    if not isinstance(entry, dict) or not clean_branch:
        return False
    if str(entry.get("branch_name") or "").strip() == clean_branch:
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
        return {k: dict(v) for k, v in locks_map.items()}
    return {k: dict(v) for k, v in locks_map.items() if bool(v.get("active"))}


def get_dev_pr_lock(
    portfolio_dir: Path | str | None = None,
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    branch_name: str = "",
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
        return dict(lock_obj)
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
) -> dict[str, Any]:
    """Acquire the exclusive cross-conversation pending-branch lock scoped to a single repository (`dev_pr_locks[repo_slug]`).

    While a timestamped branch is active or pending manual validation on `repo_slug`, no other conversation
    can start a competing branch in that same repository; sibling repositories remain free to acquire their own locks.
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
            holder_cid = str(existing_lock.get("conversation_id") or "").strip()
            existing_siblings = [str(s).strip() for s in (existing_lock.get("sibling_conversation_ids") or [])]
            same_conv = bool(
                not holder_cid
                or holder_cid == cid_clean
                or cid_clean.startswith(holder_cid[:8])
                or holder_cid.startswith(cid_clean[:8])
                or cid_clean in existing_siblings
            )
            existing_status = str(existing_lock.get("status") or "")
            raw_rb = existing_lock.get("repo_branches")
            if same_conv and isinstance(raw_rb, dict):
                repo_branches = {
                    str(k): dict(v)
                    for k, v in raw_rb.items()
                    if str(k).strip() and isinstance(v, dict)
                }
            existing_repo_entry = repo_branches.get(resolved_slug)
            if same_conv and existing_repo_entry and existing_repo_entry.get("branch_name"):
                default_branch = str(existing_repo_entry.get("branch_name") or default_branch)
                resolved_ts = str(existing_repo_entry.get("branch_timestamp") or resolved_ts)
            elif reuse_active_branch and same_conv:
                resolved_ts = str(existing_lock.get("branch_timestamp") or resolved_ts)
                default_branch = str(existing_lock.get("branch_name") or default_branch)
            elif not same_conv or (
                existing_status == "PENDING_HUMAN_PR_VALIDATION"
                and branch_timestamp
                and branch_timestamp != existing_lock.get("branch_timestamp")
            ):
                return {
                    "status": "locked",
                    "acquired": False,
                    "repo_slug": resolved_slug,
                    "error": (
                        f"Repository `{resolved_slug}` is locked by branch `{existing_lock.get('branch_name')}` "
                        f"(timestamp `{existing_lock.get('branch_timestamp')}`, Conversation ID `{holder_cid}`, "
                        f"status={existing_status}) pending manual PR validation. Sibling repositories remain unlocked."
                    ),
                    "active_lock": existing_lock,
                }

        prev_entry = repo_branches.get(resolved_slug, {})
        repo_branches[resolved_slug] = {
            "repo_slug": resolved_slug,
            "branch_name": default_branch,
            "branch_timestamp": resolved_ts,
            "target_repo_dir": clean_repo_dir or str(prev_entry.get("target_repo_dir") or ""),
            "manual_pr_url": str(prev_entry.get("manual_pr_url") or issue_url or ""),
            "branch_pushed": bool(prev_entry.get("branch_pushed", False)),
            "head_sha": str(prev_entry.get("head_sha") or ""),
            "updated_at": now_iso,
        }

        same_prev = isinstance(existing_lock, dict) and existing_lock.get("conversation_id") == cid_clean
        new_lock: dict[str, Any] = {
            "active": True,
            "vcs_mode": "native_git",
            "repo_slug": resolved_slug,
            "repo": resolved_slug,
            "lock_id": f"lock-{cid_clean[:8]}-{resolved_ts}",
            "conversation_id": cid_clean,
            "sibling_conversation_ids": clean_siblings,
            "action_id": str(action_id or "").strip(),
            "notification_source": "Ad-Hoc",
            "branch_timestamp": resolved_ts,
            "branch_name": default_branch,
            "branch_pushed": bool(existing_lock.get("branch_pushed")) if same_prev else False,
            "head_sha": str(existing_lock.get("head_sha") or "") if same_prev else "",
            "manual_pr_url": str(existing_lock.get("manual_pr_url") or "") if same_prev else "",
            "target_repo_dir": clean_repo_dir or (str(existing_lock.get("target_repo_dir") or "") if same_prev else ""),
            "repo_branches": repo_branches,
            "worktree_path": _sanitize_home_paths(str(worktree_path or "").strip()),
            "issue_number": int(issue_number) if issue_number is not None else None,
            "issue_url": str(issue_url or "").strip(),
            "pr_number": existing_lock.get("pr_number") if same_prev else None,
            "pr_url": str(existing_lock.get("pr_url") or "") if same_prev else "",
            "status": "DEVELOPING",
            "summary": _sanitize_home_paths(str(summary or "").strip()),
            "proposed_fix": _sanitize_home_paths(str(proposed_fix or "").strip()),
            "target_files": [str(f).strip() for f in (target_files or []) if str(f).strip()],
            "acquired_at": (
                str(existing_lock.get("acquired_at"))
                if same_prev and existing_lock.get("acquired_at")
                else now_iso
            ),
            "updated_at": now_iso,
        }
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
) -> dict[str, Any]:
    """Update fields on the matching active per-repository lock inside `actions_state.json`."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_path = p_dir / "actions_state.json"
    lock_file = state_path.with_suffix(".json.lock")
    now_iso = _utc_now_iso()

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
        existing_branch = str(cand_lock.get("branch_name") or "").strip()
        if req_branch and existing_branch and not _entry_matches_branch(cand_lock, req_branch):
            req_cid = str(updates.get("conversation_id") or "").strip()
            holder_cid = str(cand_lock.get("conversation_id") or "").strip()
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
        current_lock = dict(cand_lock)
        for k, v in updates.items():
            if v is not None:
                if k in ("summary", "proposed_fix", "proposal_title", "proposal_body", "target_repo_dir", "worktree_path") and isinstance(v, str):
                    current_lock[k] = _sanitize_home_paths(v)
                else:
                    current_lock[k] = v
        current_lock["repo_slug"] = target_slug
        current_lock["repo"] = target_slug
        current_lock["updated_at"] = now_iso
        locks_map[target_slug] = current_lock
        _write_locks_state(audit_state, locks_map, touched_repo_slug=target_slug)
        state_obj["actions_log_audit_state"] = audit_state
        tmp_path = state_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(state_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, state_path)
    return {"status": "ok", "repo_slug": target_slug, "lock": current_lock}


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
    release_all: bool = False,
) -> dict[str, Any]:
    """Release one repository's pending-branch lock (`repo_slug`/`workdir`/`branch_name`), or all locks when `release_all=True`."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    state_path = p_dir / "actions_state.json"
    lock_file = state_path.with_suffix(".json.lock")
    now_iso = _utc_now_iso()

    released_entries: list[tuple[str, dict[str, Any]]] = []
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
            rel_lock = dict(locks_map.get(slug) or {})
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
                    for entry in rb_map.values()
                    if isinstance(entry, dict) and entry.get("branch_name")
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

    for _, released_lock in released_entries:
        wt_path = str(released_lock.get("worktree_path") or "").strip()
        if wt_path:
            cleanup_worktree(wt_path, repo_dir=_resolve_lock_target_repo_dir(released_lock))

    stop_dev_api_server(portfolio_dir=portfolio_dir)
    restart_pro_api_server(portfolio_dir=portfolio_dir)

    primary_prev = released_entries[0][1] if released_entries else {}
    return {
        "status": "ok",
        "released": True,
        "released_repos": [s for s, _ in released_entries],
        "remaining_active_repos": remaining_active_slugs,
        "reason": reason,
        "resolution": resolution,
        "previous_lock": primary_prev,
    }


def _sync_single_repo_lock_with_git(
    *,
    repo_slug: str,
    lock_info: dict[str, Any],
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
) -> dict[str, Any]:
    """Synchronize one repository's active lock against `origin` via native `git`."""
    if git_client is not None:
        vcs = git_client
    else:
        env_ov: dict[str, str] = {}
        lock_repo_path = _resolve_lock_target_repo_dir(lock_info)
        if lock_repo_path and (lock_repo_path / ".git").exists():
            env_ov["AGY_CONF_REPO_DIR"] = str(lock_repo_path)
        if portfolio_dir is not None:
            env_ov["AGY_DEV_STATE_DIR"] = str(dev_common.resolve_state_dir(portfolio_dir=portfolio_dir))
        vcs = GitVcsIntegrationClient(env_overrides=env_ov) if env_ov else GitVcsIntegrationClient()

    branch_name = str(lock_info.get("branch_name") or "").strip()
    branch_pushed = bool(lock_info.get("branch_pushed"))
    if not branch_name or not branch_pushed:
        repo_sync = (
            vcs.sync_and_clean_local_repo(
                base_branch="main",
                protected_branches=[branch_name] if branch_name else [],
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

    if bool(branch_state.get("merged_into_base")):
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
            protected_branches=[branch_name],
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
        raw_rb = existing_lock.get("repo_branches")
        if repo_slug and isinstance(raw_rb, dict) and raw_rb:
            rb_entry = raw_rb.get(repo_slug)
            if isinstance(rb_entry, dict) and str(rb_entry.get("branch_name") or "").strip():
                b_name = str(rb_entry.get("branch_name") or "").strip()
                b_ts = str(rb_entry.get("branch_timestamp") or "").strip()
                return b_name, b_ts
        else:
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
) -> dict[str, Any]:
    """Execute Steps 1–2: inspect/RCA, verify no pending branch is awaiting validation (or reuse active PR branch), create timestamped git branch, and acquire `dev_pr_lock`."""
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
    existing_status = str(existing_lock.get("status") or "")
    if existing_lock.get("active") and (
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
                repo_slugs=[repo_slug] if repo_slug else None,
                blocked_by_lock_id=str(existing_lock.get("lock_id") or ""),
                portfolio_dir=portfolio_dir,
            )
        return {
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

    evidence = ""
    if existing_lock.get("active") and (reuse_active_branch or same_conv):
        existing_sum = str(existing_lock.get("summary") or "").strip()
        existing_fix = str(existing_lock.get("proposed_fix") or "").strip()
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
        existing_lock=existing_lock,
        repo_slug=repo_slug,
        same_conv=same_conv,
    )
    resolved_ts = (
        branch_timestamp.strip()
        or reused_repo_ts
        or str(existing_lock.get("branch_timestamp") or "").strip()
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
    if create_worktree:
        wt_res = prepare_isolated_worktree(
            branch_name=branch_name,
            repo_dir=target_repo_dir if isinstance(target_repo_dir, Path) else None,
            portfolio_dir=portfolio_dir,
        )
        if wt_res.get("status") == "ok":
            wt_path = str(wt_res.get("worktree_path") or "")
            if wt_path:
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

    try:
        manual_pr_url = vcs.build_manual_pr_url(
            head_branch=branch_name,
            base_branch="main",
            title=proposal_title,
            repo_dir=target_repo_dir if isinstance(target_repo_dir, Path) else None,
        )
    except TypeError:
        manual_pr_url = vcs.build_manual_pr_url(
            head_branch=branch_name,
            base_branch="main",
            title=proposal_title,
        )

    lock_res = acquire_dev_pr_lock(
        conversation_id=cid_clean,
        action_id=action_id,
        issue_number=None,
        issue_url=manual_pr_url,
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
    )
    if not lock_res.get("acquired"):
        return lock_res

    return {
        "status": "ok",
        "acquired": True,
        "vcs_mode": "native_git",
        "repo_slug": repo_slug,
        "target_repo_dir": _sanitize_home_paths(str(target_repo_dir)) if isinstance(target_repo_dir, Path) else "",
        "branch_timestamp": resolved_ts,
        "branch_name": branch_name,
        "proposal_title": proposal_title,
        "proposal_markdown": proposal_body,
        "manual_pr_url": manual_pr_url,
        "worktree_path": wt_path,
        "lock": lock_res.get("lock", {}),
    }


# ==============================================================================
# Step 6: Local Unit Test & Static QA Gate
# ==============================================================================


def run_local_validation_suite(
    workdir: Path | str | None = None,
    test_targets: list[str] | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Execute Step 6: Run local offline unit tests (`unittest`) and `qa_static_audit.py` to guarantee zero broken tests."""
    vcs = GitVcsIntegrationClient()
    target_dir = Path(workdir).expanduser().resolve() if workdir else vcs.resolve_repo_dir()
    if not target_dir.is_dir():
        return {"status": "error", "passed": False, "error": f"Workdir not found: {target_dir}"}

    qa_script = target_dir / "tests" / "scripts" / "qa_static_audit.py"
    qa_passed = True
    qa_output = ""
    if qa_script.is_file():
        proc_qa = subprocess.run(
            [sys.executable, str(qa_script), "--incremental"],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        qa_passed = proc_qa.returncode == 0
        qa_output = f"{proc_qa.stdout}\n{proc_qa.stderr}".strip()

    targets = list(test_targets or [])
    env = {**os.environ, "AGY_OFFLINE_ONLY": "1", "PRE_COMMIT_HOOK": "1"}
    selective_runner = target_dir / "tests" / "scripts" / "selective_test_runner.py"
    if targets:
        cmd = [sys.executable, "-m", "unittest", *targets]
    elif selective_runner.is_file():
        cmd = [sys.executable, str(selective_runner)]
    else:
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]
    proc_test = subprocess.run(
        cmd,
        cwd=str(target_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    tests_passed = proc_test.returncode == 0
    test_output = f"{proc_test.stdout}\n{proc_test.stderr}".strip()
    overall_passed = bool(qa_passed and tests_passed)

    return {
        "status": "ok" if overall_passed else "error",
        "passed": overall_passed,
        "qa_static_passed": qa_passed,
        "unit_tests_passed": tests_passed,
        "workdir": str(target_dir),
        "test_targets": targets,
        "qa_output": qa_output[-1500:],
        "test_output": test_output[-2500:],
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
    Executes Steps 1–4 using pure native `git` (RCA -> Create Timestamped Branch & Acquire per-repo `dev_pr_locks` entry ->
    Propose Changes on Branch & Run Local Unit Tests -> Push Branch to Remote `origin/<branch_name>`)
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
        f"NEVER commit, merge, or push directly to `main`.\n"
        f"Your mandatory responsibilities:\n"
        f"1. Consume coordinator-provided file anchors, exact line ranges, and contracts cleanly without redundant full-repo discovery; inspect `rca` only if additional root-cause detail is missing.\n"
        f"2. Create a timestamped branch (`fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>`) and acquire the per-repository lock (`dev_pr_locks[repo_slug]`) via:\n"
        f"   `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py start-remediation --id <CONVERSATION_ID> --action-id <ACTION_ID> --workdir <REPO_DIR> --rca-summary \"...\" --proposed-fix \"...\" --worktree`\n"
        f"   - If `status == \"locked\"` on this repository, `dev_queue.json` records the blocked item automatically; stop immediately and return the active lock status.\n"
        f"3. Implement modular Zero-PII code/skill fixes and hermetic unit test(s) in `tests/`, iterating quickly via targeted `python3 -m unittest <target>` checks.\n"
        f"4. Once targeted tests pass, run a single final gate (`pre-commit run --all-files` + `pr_remediator.py test-local --workdir <WORKTREE_PATH>`) until `passed == true`.\n"
        f"5. Commit and push the timestamped branch to `origin` via native `git` (never `main`):\n"
        f"   `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py push-branch --workdir <WORKTREE_PATH> --title \"fix(<scope>): <summary>\" --scheduled`\n"
        f"6. Return the JSON output from `push-branch` (including `branch_name`, `branch_timestamp`, `manual_pr_url`, `reviewer_subagent_definition`, and `reviewer_subagent_invocation`) so `{PR_REVIEWER_SUBAGENT_NAME}` (`Model=\"pro\"`) can evaluate immediately."
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
        + "\nExecute the native `git` branch workflow now:\n"
        f"1. Run `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py start-remediation "
        f"--id {cid_clean} --action-id \"{action_id or 'code-change'}\"{workdir_arg} "
        f"--rca-summary \"<RCA_SUMMARY>\" --proposed-fix \"<PROPOSED_FIX>\"{files_arg}{sib_arg} --worktree`.\n"
        f"   - If `status == \"locked\"`, abort immediately and return the lock status.\n"
        f"2. Implement modular fixes and hermetic unit tests on the worktree branch, iterate with targeted `python3 -m unittest <target>`, then run `pre-commit run --all-files` and `pr_remediator.py test-local --workdir <WORKTREE_PATH>` once until `passed == true`.\n"
        f"3. Commit and push the timestamped branch via `pr_remediator.py push-branch --workdir <WORKTREE_PATH> --title \"fix: remediate {action_id or cid_clean[:8]}\"{sched_flag}`.\n"
        f"4. Return the full JSON result so `{PR_REVIEWER_SUBAGENT_NAME}` can immediately evaluate the pushed branch."
    )

    return {
        "subagent_definition": {
            "Name": PR_DEVELOPER_SUBAGENT_NAME,
            "Description": (
                "VCS-Interoperable Native Git Code Developer Agent invoked when a code change is required (via audit or user prompt). "
                "Creates a timestamped git branch, acquires per-repo dev_pr_locks entry, implements and tests the change locally, and pushes the branch to origin for human PR validation."
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
    _ = pr_number, issue_number, scheduled
    resolved_ts = branch_timestamp or ""
    wd_flag = f' --workdir "{workdir.strip()}"' if str(workdir or "").strip() else ""
    repo_line = f"- **Target Repository**: `{repo_slug.strip()}`\n" if str(repo_slug or "").strip() else ""
    system_prompt = (
        f"You are `{PR_REVIEWER_SUBAGENT_NAME}`, an independent Native Git Branch Quality & Security Evaluator Agent "
        f"(Maker-Checker pattern) for `agy-dev-plugin` and companion repositories (`agy-tam-plugin`, `g-control-tower`, etc.).\n"
        f"You run in a completely isolated conversation context right after a timestamped branch is pushed.\n"
        f"Your mandatory responsibilities:\n"
        f"1. Inspect the pushed branch diff and changed files against `origin/main` using pure native `git` via:\n"
        f"   `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py branch-diff --branch <BRANCH_NAME>{wd_flag}`\n"
        f"2. Independently evaluate the proposed branch changes across 4 criteria (0-100 score) and determine your verdict (`PASS`, `WARN`, or `NEEDS_REVISION`):\n"
        f"   - Criterion A (40 pts): Root Cause / Requirement Resolution — Does the diff accurately implement the change without regressions?\n"
        f"   - Criterion B (25 pts): Unit Test Coverage — Are new or updated deterministic unit tests included in `tests/`?\n"
        f"   - Criterion C (20 pts): Security & Zero-PII Guardrails — Zero hardcoded customer names, emails, `/Users/<user>/` paths, or secrets.\n"
        f"   - Criterion D (15 pts): Code Style, Typing & Documentation — Clean English docstrings, type hints, and plugin / `$HOME` parity.\n"
        f"3. Record your structured verdict and dispatch review notification (embedding the exact branch timestamp `{resolved_ts}`) so the human can compare the branch against `main` and manually create the Pull Request:\n"
        f"   `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py record-review --branch <BRANCH_NAME>{wd_flag} --verdict <PASS|WARN|NEEDS_REVISION> --score <0-100> --summary \"...\" --send-email`\n"
        f"4. Return the JSON evaluation summary and manual PR validation instructions."
    )

    task_prompt = (
        f"Evaluate the pushed timestamped branch `{branch_name}` (Timestamp: `{resolved_ts}`):\n"
        + repo_line
        + f"- **Conversation ID**: `{conversation_id}`\n"
        f"- **Proposed Branch**: `{branch_name}`\n"
        f"- **Branch Timestamp**: `{resolved_ts}`\n"
        f"- **Manual PR / Compare URL**: {pr_url or issue_url or 'N/A'}\n"
        f"- **Root Cause / Rationale**: {rca_summary}\n"
        f"- **Proposed Solution**: {proposed_fix}\n\n"
        f"Execute the following steps:\n"
        f"1. Run `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py branch-diff --branch \"{branch_name}\"{wd_flag}` to inspect the exact diff and files changed via native `git`.\n"
        f"2. Score the branch changes (0-100) across Resolution Accuracy, Unit Tests, Zero-PII Security, and Code Quality, and give your verdict (`PASS`, `WARN`, or `NEEDS_REVISION`).\n"
        f"3. Run `~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py record-review "
        f"--branch \"{branch_name}\"{wd_flag} --verdict <PASS|WARN|NEEDS_REVISION> --score <SCORE> "
        f"--summary \"<Your detailed technical review comment>\" --send-email` so your verdict is recorded and the review notification (with timestamp `{resolved_ts}`) is dispatched to the human for manual Pull Request creation.\n"
        f"4. Return the JSON evaluation summary."
    )

    return {
        "subagent_definition": {
            "Name": PR_REVIEWER_SUBAGENT_NAME,
            "Description": (
                "Independent Native Git Branch Reviewer Agent (agy-code-pr-reviewer) that evaluates pushed timestamped branch diffs, "
                "unit tests, and security guardrails via native git, records its verdict, and sends the notification email (with matching branch_timestamp) "
                "so the human can compare against main and manually create the Pull Request."
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
    reviewer_verdict: str = "",
    reviewer_score: int | None = None,
    reviewer_summary: str = "",
    reviewer_subagent_id: str = "",
) -> str:
    """Build a deterministic, VCS-agnostic Pull Request description guaranteeing an `## 📋 Executive Summary` section.

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

    sections.extend(
        [
            "",
            f"## 🤖 Independent PR Review (`{PR_REVIEWER_SUBAGENT_NAME}` · `{PR_SUBAGENT_MODEL}`)",
            "",
        ]
    )
    if reviewer_verdict:
        v_norm = reviewer_verdict.strip().upper()
        v_badge = "🟢 PASS" if v_norm == "PASS" else ("🟧 WARN" if v_norm == "WARN" else "🟥 NEEDS_REVISION")
        score_val = int(reviewer_score if reviewer_score is not None else 95)
        sections.append(f"- **Verdict**: **[ {v_badge} ({score_val}/100) ]**")
        if reviewer_subagent_id:
            sections.append(f"- **Reviewer Subagent ID**: `{reviewer_subagent_id}`")
        if reviewer_summary:
            sections.append(f"- **Technical Review Summary**: {reviewer_summary.strip()}")
    else:
        sections.append(f"- **Status**: Pending independent evaluation by `{PR_REVIEWER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL})`.")

    return "\n".join(sections).strip() + "\n"


def _sync_branch_head_commit_message(
    target_repo_dir: Path,
    branch_name: str,
    base_branch: str,
    commit_title: str,
    commit_body: str,
) -> bool:
    """Ensure the HEAD commit on `branch_name` embeds `commit_title` and `commit_body` (`## 📋 Executive Summary`).

    Works deterministically even when the developer committed changes with a 1-line `git commit -m "..."` during
    `pre-commit` prior to calling `push-branch` or `record-review`.
    """
    if not (target_repo_dir / ".git").exists() or not branch_name or branch_name in ("main", "master"):
        return False

    cur_proc = subprocess.run(
        ["git", "-C", str(target_repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    original_branch = (cur_proc.stdout or "").strip()
    switched = False
    if original_branch != branch_name:
        co_proc = subprocess.run(
            ["git", "-C", str(target_repo_dir), "checkout", branch_name],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if co_proc.returncode != 0:
            return False
        switched = True

    amended = False
    try:
        base_ref = f"origin/{base_branch}"
        chk_base = subprocess.run(
            ["git", "-C", str(target_repo_dir), "rev-parse", "--verify", base_ref],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if chk_base.returncode != 0:
            base_ref = base_branch

        cnt_proc = subprocess.run(
            ["git", "-C", str(target_repo_dir), "rev-list", "--count", f"{base_ref}..{branch_name}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        ahead_count = int((cnt_proc.stdout or "0").strip() or 0) if cnt_proc.returncode == 0 else 0
        if ahead_count > 0:
            am_proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(target_repo_dir),
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
    finally:
        if switched and original_branch and original_branch != branch_name:
            subprocess.run(
                ["git", "-C", str(target_repo_dir), "checkout", original_branch],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
    return amended


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
    """Commit pending changes onto the timestamped branch (never `main`), stamp the canonical `## 📋 Executive Summary` PR body into the branch commit and compare URL, push to `origin/<branch>` via native `git`, and build the `agy-code-pr-reviewer` invocation plan."""
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
                "Cannot push remediation branch without an active development lock (`dev_pr_lock`). "
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
    raw_rb = lock_info.get("repo_branches") if isinstance(lock_info.get("repo_branches"), dict) else {}
    repo_entry = raw_rb.get(resolved_repo_slug) if (resolved_repo_slug and isinstance(raw_rb, dict)) else None
    resolved_head = (
        head_branch.strip()
        or (str(repo_entry.get("branch_name") or "").strip() if isinstance(repo_entry, dict) else "")
        or str(lock_info.get("branch_name") or "").strip()
    )
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

    commit_title = format_remediation_commit_title(
        title.strip(),
        action_id=act_id,
        conversation_id=cid_clean,
        branch_timestamp=resolved_ts,
    )
    preliminary_body = build_canonical_pr_description(
        commit_title=commit_title,
        executive_summary=executive_summary,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        custom_body=body,
        branch_name=resolved_head,
        branch_timestamp=resolved_ts,
        conversation_id=cid_clean,
    )

    # If target_repo_dir is a real git checkout/worktree, ensure changes are committed on resolved_head (never on main)
    if (target_repo_dir / ".git").exists():
        # Ensure we are on resolved_head before staging/committing any modified or untracked files
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
                subprocess.run(
                    ["git", "-C", str(target_repo_dir), "checkout", resolved_head],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            else:
                subprocess.run(
                    ["git", "-C", str(target_repo_dir), "checkout", "-B", resolved_head],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )

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
            subprocess.run(
                ["git", "-C", str(target_repo_dir), "commit", "--no-verify", "-m", commit_title, "-m", preliminary_body],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

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
                f"Refusing to push branch '{resolved_head}' because it has 0 file changes compared to '{base_branch}'. "
                f"All proposed code changes must be committed onto '{resolved_head}' before pushing."
            ),
        }

    final_body = build_canonical_pr_description(
        commit_title=commit_title,
        executive_summary=executive_summary,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        custom_body=body,
        branch_name=resolved_head,
        branch_timestamp=resolved_ts,
        conversation_id=cid_clean,
        files_changed=diff_res.get("files", []),
    )

    # Deterministically amend the HEAD commit on resolved_head so the Git commit body ALWAYS carries `## 📋 Executive Summary`
    _sync_branch_head_commit_message(
        target_repo_dir=target_repo_dir,
        branch_name=resolved_head,
        base_branch=base_branch,
        commit_title=commit_title,
        commit_body=final_body,
    )

    try:
        push_res = vcs.push_branch(
            branch_name=resolved_head,
            remote="origin",
            base_branch=base_branch,
            set_upstream=True,
            force_with_lease=True,
            title=commit_title,
            body=final_body,
            repo_dir=target_repo_dir,
        )
    except TypeError:
        push_res = vcs.push_branch(
            branch_name=resolved_head,
            remote="origin",
            base_branch=base_branch,
            set_upstream=True,
            repo_dir=target_repo_dir,
        )
    if push_res.get("status") != "ok":
        return push_res

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

    if has_preview_bundle or has_staged_plugin:
        for asset_name in ("index.html", "styles.css", "dashboard.js", "favicon.svg"):
            staged_asset = preview_dir / asset_name
            if staged_asset.is_file():
                with contextlib.suppress(OSError):
                    os.utime(staged_asset, None)
        restart_pro_api_server(portfolio_dir=portfolio_dir)

    try:
        built_url = vcs.build_manual_pr_url(
            head_branch=resolved_head,
            base_branch=base_branch,
            title=commit_title,
            body=final_body,
            repo_dir=target_repo_dir,
        )
    except TypeError:
        built_url = vcs.build_manual_pr_url(
            head_branch=resolved_head,
            base_branch=base_branch,
            title=commit_title,
            repo_dir=target_repo_dir,
        )
    manual_pr_url = str(built_url or push_res.get("manual_pr_url") or "")
    head_sha = str(push_res.get("head_sha") or diff_res.get("head_sha") or "")

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
    if resolved_repo_slug:
        updated_rb[resolved_repo_slug] = {
            "repo_slug": resolved_repo_slug,
            "branch_name": resolved_head,
            "branch_timestamp": resolved_ts,
            "target_repo_dir": _sanitize_home_paths(str(persistent_repo_dir)),
            "manual_pr_url": manual_pr_url,
            "branch_pushed": True,
            "head_sha": head_sha,
            "updated_at": _utc_now_iso(),
        }

    updated_lock = update_dev_pr_lock(
        {
            "branch_name": resolved_head,
            "branch_timestamp": resolved_ts,
            "notification_source": "Ad-Hoc",
            "branch_pushed": True,
            "head_sha": head_sha,
            "target_repo_dir": _sanitize_home_paths(str(persistent_repo_dir)),
            "worktree_path": wt_recorded,
            "repo_branches": updated_rb,
            "manual_pr_url": manual_pr_url,
            "pr_url": manual_pr_url,
            "frontend_changed": frontend_changed,
            "dev_preview_url": dev_preview_url,
            "pro_url": pro_url,
            "proposal_title": commit_title,
            "proposal_body": final_body,
            "status": "BRANCH_PUSHED_AWAITING_AGENT_REVIEW",
        },
        portfolio_dir=portfolio_dir,
        repo_slug=resolved_repo_slug,
        workdir=target_repo_dir,
        branch_name=resolved_head,
    )

    watcher_res = spawn_pr_merge_watcher_daemon(portfolio_dir=portfolio_dir)

    reviewer_plan = build_pr_reviewer_subagent_plan(
        pr_number=None,
        pr_url=manual_pr_url,
        issue_number=int(resolved_issue) if resolved_issue else None,
        issue_url=manual_pr_url,
        conversation_id=cid_clean,
        branch_name=resolved_head,
        branch_timestamp=resolved_ts,
        rca_summary=rca_summary,
        proposed_fix=final_body,
        scheduled=scheduled,
        workdir=str(target_repo_dir),
        repo_slug=resolved_repo_slug,
    )

    return {
        "status": "ok",
        "vcs_mode": "native_git",
        "repo_slug": resolved_repo_slug,
        "commit_title": commit_title,
        "branch_name": resolved_head,
        "branch_timestamp": resolved_ts,
        "head_sha": head_sha,
        "returned_to_base_branch": returned_to_base,
        "pr_merge_watcher": watcher_res,
        "manual_pr_url": manual_pr_url,
        "pr_url": manual_pr_url,
        "frontend_changed": frontend_changed,
        "dev_preview_url": dev_preview_url,
        "pro_url": pro_url,
        "dev_server": dev_server_res,
        "pr_number": int(resolved_issue or 0),
        "issue_number": resolved_issue,
        "files_changed": diff_res.get("files", []),
        "files_count": diff_res.get("files_count", 0),
        "lock": updated_lock.get("lock", {}),
        "reviewer_subagent_definition": reviewer_plan["subagent_definition"],
        "reviewer_subagent_invocation": reviewer_plan["subagent_invocation"],
    }


# ==============================================================================
# Agent 2 (`agy-code-pr-reviewer`) Evaluation, Email Notification & Manual Gate
# ==============================================================================


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
    """Record `agy-code-pr-reviewer`'s evaluation on the pushed timestamped branch, stamp the canonical `## 📋 Executive Summary` + Reviewer Verdict into the branch commit & compare URL, keep `dev_pr_lock` active for manual validation, and send the notification email."""
    vcs = git_client or GitVcsIntegrationClient()
    norm_verdict = verdict.strip().upper()
    if norm_verdict not in ("PASS", "WARN", "NEEDS_REVISION"):
        norm_verdict = "PASS" if score >= 90 else "NEEDS_REVISION"

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
    head_sha = str(lock_info.get("head_sha") or "")

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
    if not lock_info.get("active") and isinstance(target_repo_dir, Path) and (target_repo_dir / ".git").exists():
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

    resolved_exec_summary = (
        executive_summary.strip()
        or (proposed_fix if proposed_fix and not proposed_fix.startswith("Inspect the failing CLI command flags") else "")
        or (rca_summary if rca_summary and rca_summary != "Tool/step execution error" else "")
        or existing_exec_summary
        or summary.strip()
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
        reviewer_verdict=norm_verdict,
        reviewer_score=int(score),
        reviewer_summary=summary.strip(),
        reviewer_subagent_id=reviewer_subagent_id.strip(),
    )

    if lock_info.get("active") and isinstance(target_repo_dir, Path) and (target_repo_dir / ".git").exists():
        amended = _sync_branch_head_commit_message(
            target_repo_dir=target_repo_dir,
            branch_name=resolved_branch,
            base_branch="main",
            commit_title=proposal_title,
            commit_body=canonical_pr_body,
        )
        if amended:
            try:
                push_amend = vcs.push_branch(
                    branch_name=resolved_branch,
                    remote="origin",
                    base_branch="main",
                    set_upstream=True,
                    force_with_lease=True,
                    title=proposal_title,
                    body=canonical_pr_body,
                    repo_dir=target_repo_dir,
                )
                if isinstance(push_amend, dict) and push_amend.get("head_sha"):
                    head_sha = str(push_amend.get("head_sha") or head_sha)
            except Exception:
                pass

    built_url = ""
    if isinstance(target_repo_dir, Path) and (target_repo_dir / ".git").exists():
        try:
            built_url = vcs.build_manual_pr_url(
                head_branch=resolved_branch,
                base_branch="main",
                title=proposal_title,
                body=canonical_pr_body,
                repo_dir=target_repo_dir,
            )
        except TypeError:
            built_url = vcs.build_manual_pr_url(head_branch=resolved_branch, base_branch="main")
    manual_pr_url = str(
        built_url
        or lock_info.get("manual_pr_url")
        or lock_info.get("pr_url")
        or ""
    )

    badge = "🟢 PASS" if norm_verdict == "PASS" else ("🟧 WARN" if norm_verdict == "WARN" else "🟥 NEEDS_REVISION")
    created_by_label = f"{PR_DEVELOPER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL}) · {PR_REVIEWER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL})"
    validator_banner_label = (
        f"{PR_REVIEWER_SUBAGENT_NAME} ({PR_SUBAGENT_MODEL}) [{norm_verdict} {int(score)}/100]"
    )
    review_markdown = (
        f"### 🛡️ New Branch Created & Evaluated (`{PR_REVIEWER_SUBAGENT_NAME}` · `{PR_SUBAGENT_MODEL}`)\n\n"
        f"- **Branch Action**: **NEW BRANCH CREATED** (`origin/{resolved_branch}`)\n"
        f"- **New Branch Created**: `{resolved_branch}`\n"
        f"- **Created & Validated By**: `{created_by_label}`\n"
        f"- **Branch Timestamp**: `{resolved_ts}`\n"
        f"- **Proposed Branch**: `{resolved_branch}`\n"
        f"- **Verdict**: **{badge}** (`{int(score)}/100`)\n"
        f"- **Evaluated At (UTC)**: `{_utc_now_iso()}`\n"
        + (f"- **Reviewer Subagent ID**: `{reviewer_subagent_id}`\n" if reviewer_subagent_id else "")
        + f"\n{canonical_pr_body}\n"
        f"---\n"
        f"**Manual Pull Request Gate (`dev_pr_lock` ACTIVE)**:\n"
        f"1. Compare `{resolved_branch}` against `main` and manually create the Pull Request / Merge Request in your VCS:\n"
        f"   - Compare / Create Manual PR: {manual_pr_url}\n"
        f"   - Native Git Diff: `git fetch origin && git diff origin/main...origin/{resolved_branch}`\n"
        f"2. While `{resolved_branch}` (`{resolved_ts}`) is pending your evaluation, **no other agent or conversation can make code changes**.\n"
        f"3. Once you create/merge the Pull Request (or decide to reject this branch), release the lock via:\n"
        f"   - Approve (PR Created / Merged): `pr_remediator.py evaluate-branch --branch \"{resolved_branch}\" --decision approve --comment \"<your comment>\"`\n"
        f"   - Reject (Delete Branch & Unlock): `pr_remediator.py evaluate-branch --branch \"{resolved_branch}\" --decision reject --comment \"<your comment>\"`\n"
    )

    if lock_info.get("active"):
        recorded_subagents = _normalize_str_list(
            [*(lock_info.get("subagent_conversation_ids") or []), reviewer_subagent_id.strip()]
        )
        update_dev_pr_lock(
            {
                "status": "PENDING_HUMAN_PR_VALIDATION",
                "branch_action": "NEW_BRANCH_CREATED",
                "branch_pushed": True,
                "notification_source": "Ad-Hoc",
                "created_by": created_by_label,
                "branch_name": resolved_branch,
                "branch_timestamp": resolved_ts,
                "head_sha": head_sha,
                "manual_pr_url": manual_pr_url,
                "pr_url": manual_pr_url,
                "proposal_title": proposal_title,
                "proposal_body": canonical_pr_body,
                "reviewer_verdict": norm_verdict,
                "reviewer_score": int(score),
                "reviewer_summary": summary.strip(),
                "reviewer_subagent_id": reviewer_subagent_id.strip(),
                "subagent_conversation_ids": recorded_subagents,
                "review_markdown": review_markdown,
            },
            portfolio_dir=portfolio_dir,
            repo_slug=resolved_repo_slug,
            workdir=target_repo_dir,
            branch_name=resolved_branch,
        )
        if cid_clean:
            with contextlib.suppress(Exception):
                update_dev_queue_item_status(
                    conversation_id=cid_clean,
                    status="pr_open",
                    branch_names=[resolved_branch],
                    pr_urls=[manual_pr_url],
                    subagent_conversation_ids=recorded_subagents,
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
                    "reviewer_score": int(score),
                    "summary": (
                        f"New branch created by {PR_DEVELOPER_SUBAGENT_NAME} / {PR_REVIEWER_SUBAGENT_NAME}: "
                        f"{resolved_branch} [{resolved_ts}] ({norm_verdict} {int(score)}/100) — pending manual PR validation."
                    ),
                    "updated_at": _utc_now_iso(),
                }
                dev_common.try_sync_actions_ledger_verdicts({cid_clean: updated_verdict}, state_dir=p_dir)
                audit_state.pop("audited_runs", None)
                state_obj["actions_log_audit_state"] = audit_state
                tmp_path = state_path.with_suffix(".tmp")
                tmp_path.write_text(json.dumps(state_obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass
                os.replace(tmp_path, state_path)

    email_sent = False
    email_subject = (
        f"New Branch Created (by {PR_REVIEWER_SUBAGENT_NAME}): "
        f"Branch {resolved_branch} [{resolved_ts}] Ready for Manual PR ({norm_verdict} {int(score)}/100)"
    )

    if send_email:
        email_body = (
            f"# 🌿 New Branch Created by `{PR_DEVELOPER_SUBAGENT_NAME}` / `{PR_REVIEWER_SUBAGENT_NAME}`: `{resolved_branch}` (`{resolved_ts}`)\n\n"
            f"A **new timestamped Git branch** **`{resolved_branch}`** (`{resolved_ts}`) has been created and pushed via native Git "
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
            + f"- **Review Verdict (`{PR_REVIEWER_SUBAGENT_NAME}`)**: **{badge} ({int(score)}/100)**\n"
            f"- **Evaluation Details**: {summary.strip()}\n"
            f"- **Concurrency Lock (`dev_pr_lock`)**: `ACTIVE (PENDING_HUMAN_PR_VALIDATION)` — "
            f"No other code changes are permitted until you evaluate this newly created branch (by creating the Pull Request manually or rejecting it).\n\n"
            f"{canonical_pr_body}\n"
            f"## 🚀 How to Compare with `main`, Create the Manual Pull Request & Release the Lock\n"
            f"1. Compare the newly created branch against `main` and create the Pull Request manually at [{manual_pr_url}]({manual_pr_url}) (or run `git fetch origin && git diff origin/main...origin/{resolved_branch}`).\n"
            f"2. To approve the branch after creating/merging the PR and unlock new changes:\n"
            f"   `pr_remediator.py evaluate-branch --branch \"{resolved_branch}\" --decision approve --comment \"Manual PR created and validated\"`\n"
            f"3. To reject the branch (deletes `origin/{resolved_branch}` with `git push origin --delete` and releases the lock):\n"
            f"   `pr_remediator.py evaluate-branch --branch \"{resolved_branch}\" --decision reject --comment \"Branch rejected\"`"
        )
        notify_res = dev_common.try_send_branch_review_notification(
            status="OK" if norm_verdict == "PASS" else ("WARN" if norm_verdict == "WARN" else "KO"),
            subject=email_subject,
            body=email_body,
            conversation_id=cid_clean or resolved_ts,
            validator=validator_banner_label,
            branch_name=resolved_branch,
            errors_only_override=False,
        )
        email_sent = bool(notify_res.get("sent"))

    ask_question_prompt = {
        "question": (
            f"New Branch Created ({resolved_branch}, Timestamp: {resolved_ts}) by {PR_REVIEWER_SUBAGENT_NAME}: "
            f"{norm_verdict} ({int(score)}/100). Have you validated the new branch to create the Pull Request manually, or do you want to reject it?"
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
        "branch_action": "NEW_BRANCH_CREATED",
        "notification_source": "Ad-Hoc",
        "email_subject": email_subject,
        "created_by": created_by_label,
        "branch_name": resolved_branch,
        "branch_timestamp": resolved_ts,
        "manual_pr_url": manual_pr_url,
        "pr_url": manual_pr_url,
        "frontend_changed": frontend_changed,
        "dev_preview_url": dev_preview_url,
        "pro_url": pro_url,
        "pr_number": int(pr_number or 0),
        "verdict": norm_verdict,
        "score": int(score),
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
) -> dict[str, Any]:
    """Execute the manual validation gate: record user decision (`approve` after manual PR creation/merge, or `reject` to delete the branch via native `git`) and release the scoped repository lock."""
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
        repo_sync = (
            vcs.sync_and_clean_local_repo(base_branch="main")
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
        elif source == "auto_lock" and existing_status in (*DEV_QUEUE_HOLD_STATUSES, "in_progress", "pr_open"):
            resolved_status = existing_status
            resolved_prio = int(existing.get("priority", norm_priority))
        elif source == "auto_lock":
            resolved_status = existing_status or norm_status
            resolved_prio = min(int(existing.get("priority", norm_priority)), norm_priority)
        else:
            resolved_status = norm_status
            resolved_prio = norm_priority

        item: dict[str, Any] = {
            "conversation_id": str(existing.get("conversation_id") or cid_clean),
            "title": title.strip() or str(existing.get("title") or action_id.strip() or cid_clean[:8]),
            "action_id": action_id.strip() or str(existing.get("action_id") or ""),
            "priority": resolved_prio,
            "status": resolved_status,
            "repo_slugs": _normalize_str_list([*(existing.get("repo_slugs") or []), *(repo_slugs or [])]),
            "depends_on": _normalize_str_list([*(existing.get("depends_on") or []), *(depends_on or [])]),
            "rca_summary": _sanitize_home_paths(rca_summary.strip()) or str(existing.get("rca_summary") or ""),
            "proposed_fix": _sanitize_home_paths(proposed_fix.strip()) or str(existing.get("proposed_fix") or ""),
            "target_files": _normalize_str_list([*(existing.get("target_files") or []), *(target_files or [])]),
            "branch_names": _normalize_str_list([*(existing.get("branch_names") or []), *(branch_names or [])]),
            "pr_urls": _normalize_str_list([*(existing.get("pr_urls") or []), *(pr_urls or [])]),
            "subagent_conversation_ids": _normalize_str_list(
                [*(existing.get("subagent_conversation_ids") or []), *(subagent_conversation_ids or [])]
            ),
            "notes": _sanitize_home_paths(notes.strip()) or str(existing.get("notes") or ""),
            "source": str(existing.get("source") or source or "manual"),
            "blocked_by_lock_id": blocked_by_lock_id.strip() or str(existing.get("blocked_by_lock_id") or ""),
            "added_at": str(existing.get("added_at") or now_iso),
            "updated_at": now_iso,
        }
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


def update_dev_queue_item_status(
    conversation_id: str,
    status: str,
    branch_names: list[str] | None = None,
    pr_urls: list[str] | None = None,
    subagent_conversation_ids: list[str] | None = None,
    notes: str | None = None,
    orchestrator_conversation_id: str = "",
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Transition an existing `dev_queue.json` entry to `status` and append branch/PR/subagent artifacts."""
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
        item["pr_urls"] = _normalize_str_list([*(item.get("pr_urls") or []), *(pr_urls or [])])
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
_MERGED_LOCK_RELEASE_REASONS = ("BRANCH_MERGED_INTO_MAIN", "HUMAN_VALIDATED_MANUAL_PR")


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
        merged_repos = _normalize_str_list(
            [
                *[dev_common.normalize_repo_slug(str(r)) for r in (item.get("merged_repos") or [])],
                *([clean_slug] if clean_slug and clean_slug != "default" else []),
            ]
        )
        still_locked_for_item = False
        for act_lock in (remaining_active_locks or {}).values():
            act_cid = str(act_lock.get("conversation_id") or "").strip()
            act_sibs = [str(s).strip() for s in (act_lock.get("sibling_conversation_ids") or [])]
            if _matches_cid(act_cid, cid_clean) or any(_matches_cid(s, cid_clean) for s in act_sibs):
                still_locked_for_item = True
                break
        all_target_repos_merged = (
            (not target_repos and not still_locked_for_item)
            or (len(target_repos) <= 1 and not still_locked_for_item)
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
        else:
            cur_st = str(item.get("status") or "pr_open").lower()
            item["status"] = cur_st if cur_st in ("in_progress", "pr_open") else "pr_open"
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
            # Offline fallback when remote ls-remote fails: verify strictly against existing local refs/remotes/origin/main
            loc_proc = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--verify", f"refs/heads/{br_name}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            base_proc = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "--verify", "refs/remotes/origin/main"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if loc_proc.returncode == 0 and base_proc.returncode == 0:
                anc_proc = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repo_dir),
                        "merge-base",
                        "--is-ancestor",
                        f"refs/heads/{br_name}",
                        "refs/remotes/origin/main",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if anc_proc.returncode == 0:
                    return True, f"git_local_ancestor_origin_main:{br_name}", [br_name], []

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
    """Return the highest-priority runnable item from `dev_queue.json`, skipping standby/HITL-blocked/unmet deps."""
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

    def _item_repo_conflict(item: dict[str, Any]) -> list[str]:
        item_repos = [
            dev_common.normalize_repo_slug(str(r))
            for r in (item.get("repo_slugs") or [])
            if str(r).strip()
        ]
        item_cid = str(item.get("conversation_id") or "").strip()
        conflicts: list[str] = []
        for rslug in item_repos:
            act = active_locks.get(rslug)
            if act and not _matches_cid(str(act.get("conversation_id") or ""), item_cid):
                conflicts.append(rslug)
        return conflicts

    next_item: dict[str, Any] | None = None
    next_pending_item: dict[str, Any] | None = None
    next_unlocked_item: dict[str, Any] | None = None
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
        repo_conflicts = _item_repo_conflict(it)
        if repo_conflicts:
            blocked_repos.append(
                {
                    "conversation_id": it.get("conversation_id"),
                    "title": it.get("title"),
                    "locked_repos": repo_conflicts,
                }
            )
            if skip_locked_repos:
                continue
        else:
            if next_unlocked_item is None:
                next_unlocked_item = it
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
                    for entry in rb_map.values():
                        if isinstance(entry, dict) and entry.get("branch_name"):
                            _add_target(entry.get("target_repo_dir") or entry.get("repo_slug"), str(entry["branch_name"]))
                elif cand_lock.get("branch_name") or cand_lock.get("last_branch_name"):
                    _add_target(
                        cand_lock.get("target_repo_dir"),
                        str(cand_lock.get("branch_name") or cand_lock.get("last_branch_name") or ""),
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
            lock_cid = str(cand_lock.get("conversation_id") or "")
            if any(_matches_cid(c, lock_cid) for c in cids_clean):
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
    p_acq.add_argument("--sibling-ids", nargs="*", default=[], help="Optional sibling Conversation IDs.")
    p_acq.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_acq.add_argument("--workdir", default="", help="Optional target repository directory.")
    p_acq.add_argument("--scheduled", action="store_true", help="Mark as scheduled run.")

    p_rel = sub.add_parser("release-lock", help="Release/delete a per-repository branch development lock.")
    p_rel.add_argument("--reason", default="MANUAL_RELEASE", help="Release reason.")
    p_rel.add_argument("--summary", default="", help="Resolution summary.")
    p_rel.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug.")
    p_rel.add_argument("--workdir", default="", help="Optional repository or worktree directory.")
    p_rel.add_argument("--branch", default="", help="Optional branch name to match lock.")
    p_rel.add_argument("--all", dest="release_all", action="store_true", help="Release all active repository locks.")

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
    p_start.add_argument("--sibling-ids", nargs="*", default=[], help="Grouped sibling Conversation IDs.")
    p_start.add_argument("--timestamp", default="", help="Optional explicit timestamp (YYYYMMDD-HHMMSS).")
    p_start.add_argument("--scheduled", action="store_true", help="Mark as scheduled run.")
    p_start.add_argument("--worktree", action="store_true", help="Provision an isolated git worktree for the timestamped branch.")
    p_start.add_argument("--checkout", action="store_true", help="Checkout the timestamped branch directly in the local repository.")
    p_start.add_argument("--workdir", default="", help="Optional target repository directory (defaults to active cwd repo or AGY_CONF_REPO_DIR).")
    p_start.add_argument(
        "--reuse-active",
        "--continue-active",
        dest="reuse_active_branch",
        action="store_true",
        help="Reuse the currently active open PR branch when addressing PR comments or CI fixes.",
    )

    p_test = sub.add_parser("test-local", help="Run local offline unit tests and static QA auditor.")
    p_test.add_argument("--workdir", default="", help="Optional repository or worktree directory.")
    p_test.add_argument("--targets", nargs="*", default=[], help="Optional unittest target modules.")

    for push_cmd in ("push-branch", "create-pr"):
        p_push = sub.add_parser(
            push_cmd,
            help="Push the timestamped branch to origin via native git (never main) and generate the agy-code-pr-reviewer invocation plan.",
        )
        p_push.add_argument("--title", required=True, help="Commit / proposal title.")
        p_push.add_argument("--body", default="", help="Commit / proposal Markdown body.")
        p_push.add_argument("--executive-summary", default="", help="Explicit Executive Summary for the PR description.")
        p_push.add_argument("--head", "--branch", dest="head", default="", help="Branch name (defaults to locked branch_name).")
        p_push.add_argument("--base", default="main", help="Base branch name (default: main).")
        p_push.add_argument("--issue", type=int, default=None, help="Optional legacy issue number.")
        p_push.add_argument("--workdir", default="", help="Optional worktree or repo directory.")
        p_push.add_argument("--scheduled", action="store_true", help="Mark as scheduled run so Agent 2 sends notification email.")

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
            sibling_ids=args.sibling_ids,
            scheduled=getattr(args, "scheduled", False),
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
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
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "test-local":
        res = run_local_validation_suite(
            workdir=args.workdir or None,
            test_targets=args.targets or None,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("passed") else 1

    if args.subcommand in ("push-branch", "create-pr"):
        res = push_remediation_branch_and_build_reviewer_plan(
            title=args.title,
            body=args.body,
            executive_summary=getattr(args, "executive_summary", ""),
            head_branch=args.head,
            base_branch=args.base,
            issue_number=args.issue,
            scheduled=args.scheduled,
            workdir=args.workdir or None,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "branch-diff":
        lock_info = get_dev_pr_lock(
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
            branch_name=args.branch.strip(),
        )
        resolved_branch = args.branch.strip() or str(lock_info.get("branch_name") or "").strip()
        wd_str = str(getattr(args, "workdir", "") or "").strip()
        wt_str = str(lock_info.get("worktree_path") or "").strip()
        cand_str = wd_str or wt_str
        cand_path = Path(cand_str.replace("$HOME", str(Path.home()))).expanduser() if cand_str else None
        repo_dir = (
            cand_path.resolve()
            if cand_path and cand_path.exists()
            else _resolve_lock_target_repo_dir(lock_info)
        )
        vcs = GitVcsIntegrationClient()
        res = vcs.get_branch_diff(head_branch=resolved_branch, base_branch=args.base, repo_dir=repo_dir)
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

    if args.subcommand in ("evaluate-branch", "finalize-pr"):
        res = evaluate_branch_and_release_lock(
            decision=args.decision,
            comment=args.comment,
            branch_name=args.branch,
            pr_number=args.pr,
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
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

    parse_args(["--help"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
