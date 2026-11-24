#!/usr/bin/env python3
"""Isolated worktree provisioning, `start-remediation`, and `test-local` CI-parity gate (`worktree_ops.py`).

Extracted from `pr_remediator.py` to separate isolated git worktree creation,
branch resumption, pre-commit single-pass execution, selective testing, and coverage verification.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
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
from dev_flow_optimizer import normalize_pre_commit_status  # noqa: E402
from git_client import GitVcsIntegrationClient, stamp_repo_version_manifests  # noqa: E402
import orchestration_control as _orch_ctrl  # noqa: E402
from lock_store import (  # noqa: E402
    PR_DEVELOPER_SUBAGENT_NAME,
    PR_REVIEWER_SUBAGENT_NAME,
    PR_SUBAGENT_MODEL,
    _HEX8_CID_RE,
    _facade_attr,
    _is_same_conversation_id,
    _is_slot_scoped_rb_key,
    _lookup_slot_repo_branch_entry,
    _resolve_main_git_repo_root,
    _resolve_max_repo_slots,
    _resolve_repo_slug_and_dir,
    _sanitize_home_paths,
    _slugify_branch_token,
    _utc_now_iso,
    acquire_dev_pr_lock,
    cleanup_worktree,
    format_remediation_commit_title,
    format_timestamped_branch_name,
    generate_branch_timestamp,
    get_dev_pr_lock,
    sync_dev_pr_lock_with_git,
    update_dev_pr_lock,
)
from parallel_scope import (  # noqa: E402
    OVERLAP_NONE,
    OVERLAP_TRANSITIVE,
    TaskAnchorSpec,
    analyze_pair_overlap,
)
from queue_store import (  # noqa: E402
    _auto_enqueue_blocked_task,
    _sync_active_lock_to_dev_queue,
)

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


_UUID_FULL_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _find_existing_repo_branch_for_conversation(
    repo_dir: Path,
    cid_clean: str,
    existing_lock: dict[str, Any],
    repo_slug: str = "",
    same_conv: bool = False,
    *,
    action_id: str = "",
    excluded_branches: set[str] | None = None,
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

    excluded = {str(b).strip() for b in (excluded_branches or set()) if str(b).strip()}
    is_suffixed_cid = len(cid_clean) > 8 and not _HEX8_CID_RE.fullmatch(cid_clean.lower()) and not _UUID_FULL_RE.fullmatch(cid_clean.lower())
    act_slug = _slugify_branch_token(action_id) if action_id.strip() else ""

    candidates: list[str] = []
    for raw_ref in (proc_refs.stdout or "").splitlines():
        ref_clean = raw_ref.strip()
        if ref_clean.startswith("origin/"):
            ref_clean = ref_clean[len("origin/") :]
        if ref_clean in excluded:
            continue
        if is_suffixed_cid and act_slug and f"-{act_slug}-{cid8}" not in ref_clean:
            continue
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
    dev_model: str = "",
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
    _facade_attr("sync_dev_pr_lock_with_git", sync_dev_pr_lock_with_git)(
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
        or _is_same_conversation_id(holder_cid, cid_clean)
        or any(_is_same_conversation_id(s, cid_clean) for s in existing_siblings)
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
                _is_same_conversation_id(s2_cid, cid_clean)
                or any(_is_same_conversation_id(s, cid_clean) for s in s2_sibs)
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

    # Graceful Drain Pause Guard: allow already-active locks owned by the same conversation to finish
    # (e.g. review iteration with --reuse-active), while blocking all NEW lock / Slot #2 acquisitions.
    is_reusing_existing_active = bool(
        existing_lock.get("active")
        and ((same_conv and not allocating_slot2) or (allocating_slot2 and same_conv_slot2))
    )
    orch_ctrl = _orch_ctrl.load_orchestration_control(portfolio_dir=portfolio_dir)
    if bool(orch_ctrl.get("paused")) and not is_reusing_existing_active:
        return {
            "status": "paused",
            "acquired": False,
            "paused": True,
            "repo_slug": repo_slug,
            "orchestration_control": orch_ctrl,
            "error": "orchestration_paused",
            "message": (
                f"Orchestration is paused (Graceful Drain): cannot acquire a new lock on `{repo_slug}` for "
                f"`{cid_clean}`. Ongoing active-lock tasks may finish, but zero new tasks will be dispatched "
                "until orchestration is resumed."
            ),
        }

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

    excluded_branches: set[str] = set()
    if existing_lock.get("active"):
        if not same_conv:
            s1_br_ex = str(existing_lock.get("branch_name") or "").strip()
            if s1_br_ex:
                excluded_branches.add(s1_br_ex)
            for rb_k, rb_v in (existing_lock.get("repo_branches") or {}).items():
                if not _is_slot_scoped_rb_key(str(rb_k)) and isinstance(rb_v, dict):
                    rb_br_ex = str(rb_v.get("branch_name") or "").strip()
                    if rb_br_ex:
                        excluded_branches.add(rb_br_ex)
        if not same_conv_slot2 and isinstance(slot2_existing, dict) and bool(slot2_existing.get("active", True)):
            s2_br_ex = str(slot2_existing.get("branch_name") or "").strip()
            if s2_br_ex:
                excluded_branches.add(s2_br_ex)

    reused_repo_branch, reused_repo_ts = _find_existing_repo_branch_for_conversation(
        repo_dir=target_repo_dir,
        cid_clean=cid_clean,
        existing_lock=lock_for_reuse if (same_conv or same_conv_slot2) else {},
        repo_slug=repo_slug,
        same_conv=bool(same_conv or same_conv_slot2),
        action_id=action_id,
        excluded_branches=excluded_branches,
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
        dev_model=dev_model,
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
    if verification_results.get("ci_parity"):
        meta_parts.append("**Mode**: `ci-parity`")
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
    cov_info = verification_results.get("coverage") if isinstance(verification_results.get("coverage"), dict) else {}
    bats_info = verification_results.get("bats") if isinstance(verification_results.get("bats"), dict) else {}
    pc_info = verification_results.get("pre_commit") if isinstance(verification_results.get("pre_commit"), dict) else {}

    qa_st = str(qa_info.get("status") or "skipped")
    qa_mode_suffix = f" ({qa_info['mode']})" if qa_info.get("mode") in ("full", "incremental") else ""
    qa_det = (
        f"`{int(qa_info.get('errors') or 0)} errors, {int(qa_info.get('warnings') or 0)} warnings{qa_mode_suffix}`"
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
    cov_pct_raw = cov_info.get("coverage_pct", verification_results.get("coverage_pct"))
    cov_thr_raw = cov_info.get("coverage_threshold", verification_results.get("coverage_threshold"))
    cov_st = str(cov_info.get("status") or ("pass" if cov_pct_raw is not None else "not_run"))
    if cov_pct_raw is not None:
        cov_pct_val = float(cov_pct_raw)
        if cov_thr_raw is not None:
            cov_thr_str = _format_threshold_number(float(cov_thr_raw))
            cmp_op = ">=" if cov_st in ("pass", "passed") else "<"
            cov_det = f"`{cov_pct_val:.2f}% {cmp_op} {cov_thr_str}% gate`"
        else:
            cov_det = f"`{cov_pct_val:.2f}% total coverage`"
    elif cov_st == "skipped":
        cov_reason = str(cov_info.get("reason") or "coverage skipped")
        cov_det = f"`{cov_reason}`"
    else:
        cov_det = "`not run (pass --ci-parity)`"

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
            f"| `coverage` | {_status_badge(cov_st)} | {cov_det} |",
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


def _format_threshold_number(val: float) -> str:
    """Format coverage threshold percentage cleanly without trailing `.0` when integer-valued."""
    rounded = round(float(val), 2)
    return str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}"


def _is_coverage_module_available() -> bool:
    """Return True when Python `coverage` is importable in the active interpreter."""
    try:
        return importlib.util.find_spec("coverage") is not None
    except (ImportError, ValueError):
        return False


def _parse_coverage_report_pct(output: str) -> float | None:
    """Parse total coverage percentage from `python3 -m coverage report` stdout/stderr."""
    if not output:
        return None
    matches = re.findall(r"(?m)^TOTAL\b.*?([0-9]+(?:\.[0-9]+)?)%\s*$", output)
    if matches:
        try:
            return round(float(matches[-1]), 2)
        except ValueError:
            return None
    return None


def _inspect_repo_ci_parity_config(target_dir: Path) -> dict[str, Any]:
    """Inspect `.github/workflows/*.y*ml` and `pyproject.toml` for `ENABLE_GHA_HOSTED_RUNNERS`, `--fail-under`, and coverage sources."""
    wf_dir = target_dir / ".github" / "workflows"
    gha_runners_gated = False
    fail_under_values: list[float] = []
    workflow_sources: list[str] = []
    pythonpath_entries: list[str] = []

    if wf_dir.is_dir():
        for wf_file in sorted(wf_dir.glob("*.y*ml")):
            if not wf_file.is_file():
                continue
            try:
                raw_text = wf_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            active_lines = [ln for ln in raw_text.splitlines() if not ln.lstrip().startswith("#")]
            active_text = "\n".join(active_lines)
            if "ENABLE_GHA_HOSTED_RUNNERS" in active_text:
                gha_runners_gated = True
            for m_fu in re.finditer(r"--(?:cov-)?fail-under(?:=|\s+)([0-9]+(?:\.[0-9]+)?)", active_text):
                try:
                    fu_val = float(m_fu.group(1))
                    if fu_val > 0:
                        fail_under_values.append(fu_val)
                except ValueError:
                    pass
            for m_cov in re.finditer(r"--cov=([A-Za-z0-9_./-]+)", active_text):
                cand = m_cov.group(1).strip().strip("/")
                if cand and cand not in workflow_sources:
                    workflow_sources.append(cand)
            for m_src in re.finditer(r"--source(?:=|\s+)([A-Za-z0-9_,./-]+)", active_text):
                for part in m_src.group(1).split(","):
                    cand = part.strip().strip("/")
                    if cand and cand not in workflow_sources:
                        workflow_sources.append(cand)
            for m_pp in re.finditer(r"(?m)^\s*PYTHONPATH\s*:\s*[\"']?([^\"'\n#]+)[\"']?", active_text):
                for pp_tok in m_pp.group(1).split(":"):
                    pp_clean = pp_tok.strip()
                    if pp_clean and pp_clean not in pythonpath_entries:
                        pythonpath_entries.append(pp_clean)

    threshold_source = "workflow"
    if fail_under_values:
        coverage_threshold = max(fail_under_values)
    else:
        pyproject_path = target_dir / "pyproject.toml"
        pyproj_thresholds: list[float] = []
        if pyproject_path.is_file():
            try:
                pyproj_text = pyproject_path.read_text(encoding="utf-8", errors="ignore")
                for m_pf in re.finditer(r"(?m)^\s*fail_under\s*=\s*([0-9]+(?:\.[0-9]+)?)\b", pyproj_text):
                    pf_val = float(m_pf.group(1))
                    if pf_val > 0:
                        pyproj_thresholds.append(pf_val)
            except (OSError, ValueError):
                pass
        if pyproj_thresholds:
            coverage_threshold = max(pyproj_thresholds)
            threshold_source = "pyproject"
        else:
            coverage_threshold = 65.0 if gha_runners_gated else 75.0
            threshold_source = "default"

    resolved_sources = [src for src in workflow_sources if (target_dir / src).exists()]
    if not resolved_sources:
        for default_dir in ("clients", "skills", "sidecars", "api", "src"):
            if (target_dir / default_dir).is_dir():
                resolved_sources.append(default_dir)
        if resolved_sources and (target_dir / "tests" / "scripts").is_dir():
            resolved_sources.append("tests/scripts")

    return {
        "gha_runners_gated": gha_runners_gated,
        "coverage_threshold": round(float(coverage_threshold), 2),
        "threshold_source": threshold_source,
        "coverage_sources": resolved_sources,
        "pythonpath_entries": pythonpath_entries,
    }


def _qa_script_supports_strict_flag(qa_script: Path) -> bool:
    """Return True when `qa_static_audit.py` explicitly declares a `--strict` CLI argument."""
    try:
        src = qa_script.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return bool(re.search(r"add_argument\(\s*[\"']--strict[\"']", src))


def run_local_validation_suite(
    workdir: Path | str | None = None,
    test_targets: list[str] | None = None,
    timeout: float = 480.0,
    *,
    portfolio_dir: Path | str | None = None,
    run_pre_commit: bool = False,
    pre_commit_status: str = "",
    run_bats: bool = True,
    run_all: bool = False,
    ci_parity: bool = False,
) -> dict[str, Any]:
    """Execute Step 6: Optional single-pass `pre-commit` (`SKIP=unit-tests,unit-tests-fast,qa-static-audit,bats-suite`), `qa_static_audit.py` (`--incremental` or full `--ci-parity`), `selective_test_runner.py` / `unittest` (+ local `coverage` gate in `--ci-parity`), and BATS, persisting unified `verification_results` bound to `head_sha` into `dev_pr_lock`."""
    vcs = GitVcsIntegrationClient()
    target_dir = Path(workdir).expanduser().resolve() if workdir else vcs.resolve_repo_dir()
    if not target_dir.is_dir():
        return {"status": "error", "passed": False, "error": f"Workdir not found: {target_dir}"}

    targets = list(test_targets or [])
    wf_cfg = _inspect_repo_ci_parity_config(target_dir)
    if ci_parity:
        effective_ci_parity = True
        ci_parity_reason = "explicit"
    elif not targets and run_all:
        effective_ci_parity = True
        ci_parity_reason = "run_all"
    elif not targets and wf_cfg["gha_runners_gated"]:
        effective_ci_parity = True
        ci_parity_reason = "gha_runners_gated"
    else:
        effective_ci_parity = False
        ci_parity_reason = ""
    effective_run_all = bool(run_all or effective_ci_parity)

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
                    run_all=effective_run_all,
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
    qa_mode = "full" if effective_ci_parity else "incremental"
    if qa_ran:
        qa_cmd = [sys.executable, str(qa_script)]
        if effective_ci_parity:
            if _qa_script_supports_strict_flag(qa_script):
                qa_cmd.append("--strict")
        else:
            qa_cmd.append("--incremental")
        proc_qa = subprocess.run(
            qa_cmd,
            cwd=str(target_dir),
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        qa_passed = proc_qa.returncode == 0
        qa_output = f"{proc_qa.stdout}\n{proc_qa.stderr}".strip()

    selective_runner = target_dir / "tests" / "scripts" / "selective_test_runner.py"
    tests_dir = target_dir / "tests"
    has_unittest_files = tests_dir.is_dir() and any(tests_dir.glob("test_*.py"))
    unittest_ran = True
    tests_passed = True
    test_output = ""

    coverage_threshold: float | None = wf_cfg["coverage_threshold"] if effective_ci_parity else None
    coverage_sources: list[str] = list(wf_cfg["coverage_sources"])
    coverage_pct: float | None = None
    coverage_passed = True
    coverage_output = ""
    coverage_entry: dict[str, Any] = {
        "status": "not_run",
        "passed": True,
        "coverage_pct": None,
        "coverage_threshold": coverage_threshold,
        "sources": coverage_sources if effective_ci_parity else [],
    }

    test_env = dict(clean_env)
    test_env.pop("PYTHONPATH", None)
    pp_parts: list[str] = []
    for cand_pp in (*wf_cfg["pythonpath_entries"], "clients", ".", "tests/scripts"):
        if cand_pp == "." or (target_dir / cand_pp).exists():
            resolved_pp = str(target_dir) if cand_pp == "." else str(target_dir / cand_pp)
            if resolved_pp not in pp_parts:
                pp_parts.append(resolved_pp)
    if pp_parts:
        test_env["PYTHONPATH"] = os.pathsep.join(pp_parts)

    if effective_ci_parity:
        if targets:
            unittest_args = ["-m", "unittest", *targets]
        elif has_unittest_files:
            unittest_args = ["-m", "unittest", "discover", "-s", "tests"]
            if (tests_dir / "__init__.py").is_file():
                unittest_args.extend(["-t", "."])
            unittest_args.extend(["-p", "test_*.py"])
        elif selective_runner.is_file():
            unittest_args = [str(selective_runner), "--all"]
        else:
            unittest_args = []
            unittest_ran = False

        can_measure_cov = bool(unittest_args) and bool(targets or has_unittest_files) and bool(coverage_sources)
        if can_measure_cov and _facade_attr("_is_coverage_module_available", _is_coverage_module_available)():
            with tempfile.TemporaryDirectory() as cov_tmp_dir:
                cov_data_file = Path(cov_tmp_dir) / ".coverage"
                cov_run_cmd = [
                    sys.executable,
                    "-m",
                    "coverage",
                    "run",
                    f"--data-file={cov_data_file}",
                    f"--source={','.join(coverage_sources)}",
                    *unittest_args,
                ]
                proc_test = subprocess.run(
                    cov_run_cmd,
                    cwd=str(target_dir),
                    env=test_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                if proc_test.returncode != 0 and "No module named coverage" in (proc_test.stderr or ""):
                    proc_fallback = subprocess.run(
                        [sys.executable, *unittest_args],
                        cwd=str(target_dir),
                        env=test_env,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        check=False,
                    )
                    tests_passed = proc_fallback.returncode == 0
                    test_output = f"{proc_fallback.stdout}\n{proc_fallback.stderr}".strip()
                    coverage_entry = {
                        "status": "skipped",
                        "passed": True,
                        "coverage_pct": None,
                        "coverage_threshold": coverage_threshold,
                        "sources": coverage_sources,
                        "reason": "coverage_not_installed",
                    }
                else:
                    tests_passed = proc_test.returncode == 0
                    test_output = f"{proc_test.stdout}\n{proc_test.stderr}".strip()
                    thr_arg = _format_threshold_number(float(coverage_threshold or 75.0))
                    cov_rep_cmd = [
                        sys.executable,
                        "-m",
                        "coverage",
                        "report",
                        f"--data-file={cov_data_file}",
                        "--precision=2",
                        f"--fail-under={thr_arg}",
                    ]
                    proc_cov = subprocess.run(
                        cov_rep_cmd,
                        cwd=str(target_dir),
                        env=test_env,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        check=False,
                    )
                    coverage_output = f"{proc_cov.stdout}\n{proc_cov.stderr}".strip()
                    if "No data to report" in coverage_output:
                        coverage_entry = {
                            "status": "skipped",
                            "passed": True,
                            "coverage_pct": None,
                            "coverage_threshold": coverage_threshold,
                            "sources": coverage_sources,
                            "reason": "no_coverage_data",
                        }
                    else:
                        coverage_pct = _parse_coverage_report_pct(coverage_output)
                        coverage_passed = (proc_cov.returncode == 0) and (
                            coverage_pct >= float(coverage_threshold)
                            if (coverage_pct is not None and coverage_threshold is not None)
                            else proc_cov.returncode == 0
                        )
                        coverage_entry = {
                            "status": "pass" if coverage_passed else "fail",
                            "passed": coverage_passed,
                            "coverage_pct": coverage_pct,
                            "coverage_threshold": coverage_threshold,
                            "sources": coverage_sources,
                        }
        else:
            if unittest_args:
                proc_test = subprocess.run(
                    [sys.executable, *unittest_args],
                    cwd=str(target_dir),
                    env=test_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
                tests_passed = proc_test.returncode == 0
                test_output = f"{proc_test.stdout}\n{proc_test.stderr}".strip()
            skip_reason = "coverage_not_installed" if can_measure_cov else "no_coverage_sources"
            coverage_entry = {
                "status": "skipped",
                "passed": True,
                "coverage_pct": None,
                "coverage_threshold": coverage_threshold,
                "sources": coverage_sources,
                "reason": skip_reason,
            }
    else:
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
                env=test_env,
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
        run_all=effective_run_all,
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
    overall_passed = bool(qa_passed and tests_passed and bats_passed and coverage_passed and (pre_commit_passed is not False))

    qa_summary = _parse_qa_static_audit_summary(qa_output, qa_passed, qa_ran)
    if qa_ran:
        qa_summary["mode"] = qa_mode

    verification_results: dict[str, Any] = {
        "head_sha": wt_state["head_sha"],
        "tree_sha": wt_state["tree_sha"],
        "dirty": wt_state["dirty"],
        "content_fingerprint": wt_state["content_fingerprint"],
        "verified_at_utc": _utc_now_iso(),
        "overall_passed": overall_passed,
        "ci_parity": effective_ci_parity,
        "ci_parity_reason": ci_parity_reason,
        "coverage_pct": coverage_pct,
        "coverage_threshold": coverage_threshold,
        "pre_commit_status": pre_commit_status_label,
        "qa_static_audit": qa_summary,
        "unittest": _parse_unittest_summary(test_output, tests_passed, unittest_ran),
        "coverage": coverage_entry,
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
        "ci_parity": effective_ci_parity,
        "ci_parity_reason": ci_parity_reason,
        "qa_static_passed": qa_passed,
        "unit_tests_passed": tests_passed,
        "coverage_passed": coverage_passed,
        "coverage_pct": coverage_pct,
        "coverage_threshold": coverage_threshold,
        "bats_passed": bats_passed,
        "pre_commit_passed": pre_commit_passed,
        "pre_commit_status": pre_commit_status_label,
        "head_sha": wt_state["head_sha"],
        "workdir": str(target_dir),
        "test_targets": targets,
        "verification_results": verification_results,
        "qa_output": qa_output[-1500:],
        "test_output": test_output[-2500:],
        "coverage_output": coverage_output[-1500:] if coverage_output else "",
        "pre_commit_output": pre_commit_output[-2000:] if pre_commit_output else "",
    }


def register_worktree_cli(sub: Any) -> None:
    """Register `start-remediation` and `test-local` CLI subcommands."""
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
    p_start.add_argument("--dev-model", dest="dev_model", default="", help="Optional actual developer model ID used on this branch (persisted on lock for cross-family review).")
    p_start.add_argument("--allow-slot2", "--parallel-slot", dest="allow_slot2", action="store_true", help="Allow allocating parallel Slot #2 when overlap check passes.")
    p_start.add_argument(
        "--reuse-active",
        "--continue-active",
        dest="reuse_active_branch",
        action="store_true",
        help="Reuse the currently active open PR branch when addressing PR comments or CI fixes.",
    )
    p_start.add_argument("--verbose", action="store_true", help="Emit full untruncated proposal_markdown and nested lock blobs (default is compact token-saving JSON).")

    p_test = sub.add_parser("test-local", help="Run local offline unit tests, static QA auditor, and persist verification_results on dev_pr_lock.")
    p_test.add_argument("--workdir", default="", help="Optional repository or worktree directory.")
    p_test.add_argument("--targets", nargs="*", default=[], help="Optional unittest target modules.")
    p_test.add_argument(
        "--all",
        "--all-files",
        dest="run_all",
        action="store_true",
        help="Force `--all-files` on pre-commit and full CI parity (`qa_static_audit`, `unittest`, `coverage`, and BATS).",
    )
    p_test.add_argument(
        "--ci-parity",
        dest="ci_parity",
        action="store_true",
        help="Run full GitHub Actions CI parity locally: full `qa_static_audit.py` (no `--incremental`), full `unittest` + BATS, and local `coverage` gate against workflow `--fail-under` threshold (auto-enabled when workflows gate on `ENABLE_GHA_HOSTED_RUNNERS` or `--all`).",
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


def _compact_start_remediation_output(res: dict[str, Any]) -> dict[str, Any]:
    """Strip redundant proposal_markdown & nested lock blobs from CLI stdout to minimize LLM context tokens."""
    if not isinstance(res, dict):
        return res
    out = {k: v for k, v in res.items() if k not in ("proposal_markdown", "lock", "active_lock")}
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
                "worker_cid",
                "dev_model",
                "pr_gate_ready",
            )
            if k in lock_src
        }
    active_src = res.get("active_lock") if isinstance(res.get("active_lock"), dict) else None
    if active_src is not None:
        out["active_lock"] = {
            k: active_src.get(k)
            for k in (
                "lock_id",
                "slot_id",
                "status",
                "conversation_id",
                "branch_name",
                "branch_timestamp",
                "manual_pr_url",
                "pr_gate_ready",
            )
            if k in active_src
        }
    out["compact"] = True
    return out


def dispatch_worktree_cli(args: Any) -> int | None:
    """Handle worktree & validation CLI subcommands; return exit code or None if unhandled."""
    if args.subcommand == "start-remediation":
        res = _facade_attr("start_remediation_branch_and_lock", start_remediation_branch_and_lock)(
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
            dev_model=str(getattr(args, "dev_model", "") or ""),
        )
        printed = res if getattr(args, "verbose", False) else _compact_start_remediation_output(res)
        print(json.dumps(printed, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "test-local":
        res = _facade_attr("run_local_validation_suite", run_local_validation_suite)(
            workdir=args.workdir or None,
            test_targets=args.targets or None,
            run_pre_commit=bool(getattr(args, "run_pre_commit", False)),
            pre_commit_status=str(getattr(args, "pre_commit_status", "") or ""),
            run_all=bool(getattr(args, "run_all", False)),
            ci_parity=bool(getattr(args, "ci_parity", False)),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("passed") else 1

    return None
