#!/usr/bin/env python3
"""Deterministic 0-LLM Dev Flow Optimizer, Stalled-Worker Detector & Lean Verification Gate.

Pure Python 3 stdlib module (Santa Lockdown compliant, Unix portable, Zero-PII) providing:
  1. `detect_stalled_lock`: idle-time analysis across lock timestamps, worktree mtimes, and worker transcripts.
  2. `build_branch_diff_verification_gate`: compact SHA-bound verification gate embedded in `branch-diff`.
  3. `analyze_dev_flow_optimizations`: 7-category deterministic self-diagnosing workflow optimizer for
     `pr_remediator.py queue-optimize` and compact `OPT:` footer hints in `dev_queue_tui.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SCRIPT_DIR.parents[2]
_INSTALLED_PLUGIN_ROOT = Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin"

for _cand_clients in (_PLUGIN_ROOT / "clients", _INSTALLED_PLUGIN_ROOT / "clients"):
    if _cand_clients.is_dir():
        _clients_str = str(_cand_clients)
        if _clients_str not in sys.path:
            sys.path.insert(0, _clients_str)
        break

if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import dev_common  # noqa: E402
import parallel_scope as _parallel_scope  # noqa: E402

DEFAULT_STALL_THRESHOLD_SECONDS = 900
DEFAULT_MAX_PARTIAL_WIP = 3
DEFAULT_MAX_ORCHESTRATOR_STEPS = 100
DEFAULT_HARD_ORCHESTRATOR_STEPS = 110
# Multi-slot per-repository engine bounds (mirrors `dev_common.resolve_max_repo_slots`, clamped `[1..4]`).
DEFAULT_MAX_REPO_SLOTS = 3
MAX_REPO_SLOTS_CAP = 4
MAX_REPO_SLOTS_ENV = "AGY_DEV_MAX_SLOTS_PER_REPO"
# Queue statuses that can never satisfy a dependency without manual re-activation / re-planning.
DEADLOCK_DEPENDENCY_STATUSES: frozenset[str] = frozenset({"standby", "cancelled", "rejected"})
# Dispatchable queue statuses whose unmet dependencies block `queue-next` (candidates for `standby_dependency_deadlock`).
DEPENDENCY_BLOCKED_OPEN_STATUSES: frozenset[str] = frozenset({"pending"})
STALL_ELIGIBLE_LOCK_STATUSES: frozenset[str] = frozenset(
    {"DEVELOPING", "NEEDS_REVISION_ITERATING", "AWAITING_AGENT_REVIEW"}
)
OPEN_QUEUE_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "in_progress",
        "pr_open",
        "hitl_blocked",
        "ready_awaiting_base_merge",
        "needs_rebase_reverify",
        "needs_agent_semantic_fix",
        "needs_agent_conflict_resolution",
    }
)
CODE_EXTENSIONS: frozenset[str] = frozenset({".py", ".js", ".css", ".html", ".sh"})
WALK_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        "tmp",
    }
)
MICRO_PR_POLISH_RE = re.compile(
    r"\b(ui|css|polish|tweak|typo|badge|tooltip|label|style|icon|copy|doc|docs|readme|lint|spacing|align|color|minor)\b",
    re.IGNORECASE,
)
SEVERITY_RANK: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_PENALTY: dict[str, int] = {"critical": 25, "high": 15, "medium": 8, "low": 4}
_REDUNDANT_REVIEW_TEST_RE = re.compile(
    r"\b(?:pre-commit\s+run|python3?(?:\.\d+)?\s+-m\s+unittest|selective_test_runner\.py)\b",
    re.IGNORECASE,
)
_LOCAL_HOME_PATH_RE = re.compile(r"/(?:Users|home)/(?!testuser\b|test\b|Shared\b)[A-Za-z0-9._-]+")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_path(text: str) -> str:
    """Replace local user home paths with `$HOME` to maintain Zero-PII output."""
    if not text:
        return ""
    cleaned = str(text)
    with contextlib.suppress(OSError):
        home_str = str(Path.home().resolve())
        if home_str and home_str != "/":
            cleaned = cleaned.replace(home_str, "$HOME")
    return _LOCAL_HOME_PATH_RE.sub("$HOME", cleaned)


def resolve_stall_threshold_seconds(threshold_seconds: int | float | None = None) -> int:
    """Resolve stalled-worker threshold in seconds (default 900s = 15m, env `AGY_DEV_STALL_THRESHOLD_SECONDS`)."""
    if threshold_seconds is not None:
        with contextlib.suppress(TypeError, ValueError):
            val = int(round(float(threshold_seconds)))
            if val > 0:
                return val
    env_raw = str(os.environ.get("AGY_DEV_STALL_THRESHOLD_SECONDS") or "").strip()
    if env_raw:
        with contextlib.suppress(TypeError, ValueError):
            val = int(round(float(env_raw)))
            if val > 0:
                return val
    return DEFAULT_STALL_THRESHOLD_SECONDS


def resolve_max_orchestrator_steps(max_steps: int | float | None = None) -> int:
    """Resolve proactive orchestrator auto-handoff rotation threshold in steps (default `100`, env `AGY_DEV_MAX_ORCHESTRATOR_STEPS`, clamped `>= 30`)."""
    if max_steps is not None:
        with contextlib.suppress(TypeError, ValueError):
            val = int(round(float(max_steps)))
            if val > 0:
                return max(30, val)
    env_raw = str(os.environ.get("AGY_DEV_MAX_ORCHESTRATOR_STEPS") or "").strip()
    if env_raw:
        with contextlib.suppress(TypeError, ValueError):
            val = int(round(float(env_raw)))
            if val > 0:
                return max(30, val)
    return DEFAULT_MAX_ORCHESTRATOR_STEPS


def resolve_hard_orchestrator_steps(max_steps: int | float | None = None) -> int:
    """Resolve urgent upper-bound orchestrator rotation threshold (`soft_threshold + 10`, default `110`)."""
    return resolve_max_orchestrator_steps(max_steps) + 10


def resolve_max_repo_slots(explicit_max_slots: Any = None) -> int:
    """Resolve per-repository slot capacity clamped to `[1..4]` (explicit -> `AGY_DEV_MAX_SLOTS_PER_REPO` -> `3`)."""
    shared_resolver = getattr(dev_common, "resolve_max_repo_slots", None)
    if callable(shared_resolver):
        with contextlib.suppress(Exception):
            return max(1, min(int(shared_resolver(explicit_max_slots)), MAX_REPO_SLOTS_CAP))
    for raw in (explicit_max_slots, str(os.environ.get(MAX_REPO_SLOTS_ENV) or "").strip()):
        if raw is None or raw == "" or isinstance(raw, bool):
            continue
        try:
            val = int(raw)
        except (TypeError, ValueError):
            continue
        if val >= 1:
            return min(val, MAX_REPO_SLOTS_CAP)
    return DEFAULT_MAX_REPO_SLOTS


def parse_iso_timestamp(ts_val: Any) -> float:
    """Parse an ISO-8601 UTC timestamp string or epoch float into POSIX seconds (`0.0` on invalid)."""
    if ts_val is None or ts_val == "":
        return 0.0
    if isinstance(ts_val, (int, float)):
        val = float(ts_val)
        return val if val > 0.0 else 0.0
    raw = str(ts_val).strip()
    if not raw:
        return 0.0
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    with contextlib.suppress(ValueError):
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return float(dt.timestamp())
    return 0.0


def format_idle_duration(idle_seconds: int) -> str:
    """Format an idle duration in seconds into a compact human label (e.g., `22m`, `1h42m`)."""
    secs = max(0, int(idle_seconds))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    rem_mins = mins % 60
    if hours < 24:
        return f"{hours}h{rem_mins:02d}m" if rem_mins else f"{hours}h"
    days = hours // 24
    rem_hours = hours % 24
    return f"{days}d{rem_hours}h" if rem_hours else f"{days}d"


def _newest_worktree_mtime(worktree_raw: str, *, max_entries: int = 8000) -> float:
    """Return the newest non-`.git` regular file `mtime` under `worktree_raw`, or `0.0`."""
    expanded = (worktree_raw or "").replace("$HOME", str(Path.home())).strip()
    if not expanded:
        return 0.0
    wt_path = Path(expanded).expanduser()
    if not wt_path.is_dir():
        return 0.0
    newest = 0.0
    seen = 0
    for root, dirnames, filenames in os.walk(wt_path, topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in WALK_SKIP_DIRS]
        root_p = Path(root)
        for fname in filenames:
            seen += 1
            if seen > max_entries:
                return newest
            fpath = root_p / fname
            with contextlib.suppress(OSError):
                st = fpath.lstat()
                if st.st_mtime > newest:
                    newest = float(st.st_mtime)
    return newest


def detect_stalled_lock(
    lock_info: dict[str, Any] | None,
    *,
    now_ts: float | None = None,
    threshold_seconds: int | float | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Detect whether an active `DEVELOPING` / `NEEDS_REVISION_ITERATING` / `AWAITING_AGENT_REVIEW` lock is stalled.

    Computes the latest observed activity timestamp and source (`transcript`, `reviewer_transcript`,
    `worktree`, `heartbeat`, or `lock_meta`) alongside total `elapsed_seconds` from `acquired_at`.
    """
    thresh = resolve_stall_threshold_seconds(threshold_seconds)
    if not isinstance(lock_info, dict):
        return {
            "stalled": False,
            "idle_seconds": 0,
            "idle_label": "0s",
            "elapsed_seconds": 0,
            "elapsed_label": "0s",
            "threshold_seconds": thresh,
            "last_activity_ts": 0.0,
            "last_activity_source": "",
            "worker_cid": "",
            "reviewer_subagent_id": "",
        }

    worker_cid = str(lock_info.get("worker_cid") or "").strip()
    reviewer_subagent_id = str(lock_info.get("reviewer_subagent_id") or "").strip()
    if lock_info.get("active") is False:
        return {
            "stalled": False,
            "idle_seconds": 0,
            "idle_label": "0s",
            "elapsed_seconds": 0,
            "elapsed_label": "0s",
            "threshold_seconds": thresh,
            "last_activity_ts": 0.0,
            "last_activity_source": "",
            "worker_cid": worker_cid,
            "reviewer_subagent_id": reviewer_subagent_id,
        }

    cur_ts = float(now_ts if now_ts is not None else time.time())
    acq_ts = parse_iso_timestamp(lock_info.get("acquired_at"))
    elapsed_seconds = max(0, int(cur_ts - acq_ts)) if acq_ts > 0.0 else 0
    elapsed_label = format_idle_duration(elapsed_seconds)

    status_u = str(lock_info.get("status") or "").strip().upper()
    if status_u not in STALL_ELIGIBLE_LOCK_STATUSES or bool(lock_info.get("pr_gate_ready")):
        return {
            "stalled": False,
            "idle_seconds": 0,
            "idle_label": "0s",
            "elapsed_seconds": elapsed_seconds,
            "elapsed_label": elapsed_label,
            "threshold_seconds": thresh,
            "last_activity_ts": 0.0,
            "last_activity_source": "",
            "worker_cid": worker_cid,
            "reviewer_subagent_id": reviewer_subagent_id,
        }

    hb_ts = parse_iso_timestamp(lock_info.get("last_activity_at"))
    upd_ts = parse_iso_timestamp(lock_info.get("updated_at"))
    wt_ts = _newest_worktree_mtime(str(lock_info.get("worktree_path") or ""))
    has_stage = bool(str(lock_info.get("stage_label") or "").strip())

    # Each candidate is (timestamp, tie_break_priority, source_name)
    source_candidates: list[tuple[float, int, str]] = []
    meta_ts = max(upd_ts, acq_ts, hb_ts)
    if meta_ts > 0.0:
        source_candidates.append((meta_ts, 1, "lock_meta"))
    if hb_ts > 0.0 and (has_stage or hb_ts > acq_ts or (upd_ts == 0.0 and acq_ts == 0.0)):
        source_candidates.append((hb_ts, 2, "heartbeat"))
    if wt_ts > 0.0:
        source_candidates.append((wt_ts, 3, "worktree"))

    if worker_cid:
        tpath = dev_common.resolve_transcript_path(worker_cid, brain_dir, home_dir=home_dir)
        if tpath is not None:
            with contextlib.suppress(OSError):
                w_mtime = float(tpath.stat().st_mtime)
                if w_mtime > 0.0:
                    source_candidates.append((w_mtime, 4, "transcript"))

    if reviewer_subagent_id:
        rpath = dev_common.resolve_transcript_path(reviewer_subagent_id, brain_dir, home_dir=home_dir)
        if rpath is not None:
            with contextlib.suppress(OSError):
                r_mtime = float(rpath.stat().st_mtime)
                if r_mtime > 0.0:
                    source_candidates.append((r_mtime, 5, "reviewer_transcript"))

    if not source_candidates:
        return {
            "stalled": False,
            "idle_seconds": 0,
            "idle_label": "0s",
            "elapsed_seconds": elapsed_seconds,
            "elapsed_label": elapsed_label,
            "threshold_seconds": thresh,
            "last_activity_ts": 0.0,
            "last_activity_source": "",
            "worker_cid": worker_cid,
            "reviewer_subagent_id": reviewer_subagent_id,
        }

    last_activity_ts, _, last_activity_source = max(source_candidates, key=lambda item: (item[0], item[1]))
    idle_seconds = max(0, int(cur_ts - last_activity_ts))
    return {
        "stalled": idle_seconds >= thresh,
        "idle_seconds": idle_seconds,
        "idle_label": format_idle_duration(idle_seconds),
        "elapsed_seconds": elapsed_seconds,
        "elapsed_label": elapsed_label,
        "threshold_seconds": thresh,
        "last_activity_ts": last_activity_ts,
        "last_activity_source": last_activity_source,
        "worker_cid": worker_cid,
        "reviewer_subagent_id": reviewer_subagent_id,
    }


def normalize_pre_commit_status(raw: str) -> tuple[str, str, bool | None]:
    """Normalize pre-commit status into `(short_status, upper_label, passed_bool_or_none)`."""
    norm = str(raw or "").strip().lower()
    if norm in ("pass", "passed", "ok", "true"):
        return ("pass", "PASSED", True)
    if norm in ("fail", "failed", "error", "false"):
        return ("fail", "FAILED", False)
    if norm in ("skip", "skipped"):
        return ("skipped", "SKIPPED", None)
    return ("not_run", "NOT_RUN", None)


def _git_rev_parse_branch(repo_dir: Path | str | None, head_branch: str = "") -> str:
    """Resolve current commit SHA for `head_branch` (or `HEAD`) inside `repo_dir`."""
    if not repo_dir:
        return ""
    rpath = Path(str(repo_dir).replace("$HOME", str(Path.home()))).expanduser()
    if not rpath.exists():
        return ""
    refs = []
    br = str(head_branch or "").strip()
    if br:
        refs.extend([br, f"refs/heads/{br}"])
    refs.append("HEAD")
    for ref in refs:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            proc = subprocess.run(
                ["git", "-C", str(rpath), "rev-parse", "--verify", ref],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
    return ""


def build_branch_diff_verification_gate(
    lock_info: dict[str, Any] | None,
    repo_dir: Path | str | None,
    head_branch: str = "",
) -> dict[str, Any]:
    """Build compact SHA-bound `verification_gate` summary embedded into `branch-diff` JSON."""
    head_sha = _git_rev_parse_branch(repo_dir, head_branch=head_branch)
    vr = (
        lock_info.get("verification_results")
        if isinstance(lock_info, dict) and isinstance(lock_info.get("verification_results"), dict)
        else {}
    )
    verified_head_sha = str(vr.get("head_sha") or "").strip()
    vr_dirty = bool(vr.get("dirty"))
    matches_head = bool(head_sha and verified_head_sha and head_sha == verified_head_sha and not vr_dirty)

    if not vr:
        return {
            "head_sha": head_sha,
            "verified_head_sha": "",
            "matches_head": False,
            "passed": False,
            "pre_commit_status": "NOT_RUN",
            "qa_passed": False,
            "tests_passed": False,
            "bats_passed": False,
            "skip_redundant_test_rerun": False,
        }

    qa_info = vr.get("qa_static_audit") if isinstance(vr.get("qa_static_audit"), dict) else {}
    ut_info = vr.get("unittest") if isinstance(vr.get("unittest"), dict) else {}
    bats_info = vr.get("bats") if isinstance(vr.get("bats"), dict) else {}
    pc_info = vr.get("pre_commit") if isinstance(vr.get("pre_commit"), dict) else {}

    qa_passed = bool(qa_info.get("passed")) if qa_info else False
    tests_passed = bool(ut_info.get("passed")) if ut_info else False
    bats_passed = bool(bats_info.get("passed", True)) if bats_info else True
    raw_pc = str(vr.get("pre_commit_status") or pc_info.get("status") or "not_run")
    _, pc_label, pc_passed_bool = normalize_pre_commit_status(raw_pc)
    if pc_info.get("passed") is False:
        pc_passed_bool = False
        pc_label = "FAILED"

    if "overall_passed" in vr:
        passed = bool(vr.get("overall_passed"))
    else:
        passed = bool(qa_passed and tests_passed and bats_passed and (pc_passed_bool is not False))

    return {
        "head_sha": head_sha,
        "verified_head_sha": verified_head_sha,
        "matches_head": matches_head,
        "passed": passed,
        "pre_commit_status": pc_label,
        "qa_passed": qa_passed,
        "tests_passed": tests_passed,
        "bats_passed": bats_passed,
        "skip_redundant_test_rerun": bool(matches_head and passed),
    }


def _make_finding(
    *,
    code: str,
    severity: str,
    category: str,
    title: str,
    evidence: dict[str, Any],
    recommendation: str,
    auto_action_hint: str,
    estimated_savings_min: int,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "category": category,
        "title": title,
        "evidence": evidence,
        "recommendation": recommendation,
        "auto_action_hint": auto_action_hint,
        "estimated_savings_min": max(0, int(estimated_savings_min)),
    }


def _cids_match(cid_a: str, cid_b: str) -> bool:
    """Return True on exact match or when one side is an 8-char hex / >=8-char prefix of the other."""
    return dev_common.cids_match(cid_a, cid_b)


def _iter_active_lock_slots(lock_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ordered active slot dicts (`slot_id` 1, 2, ...) from multi-slot or flat `lock_info`."""
    return dev_common.iter_active_lock_slots(lock_info)


def _slot_matches_cid(slot_info: dict[str, Any], cid: str) -> bool:
    """Return True when a single slot's primary or sibling conversation IDs match `cid`."""
    if not isinstance(slot_info, dict) or not cid:
        return False
    if _cids_match(str(slot_info.get("conversation_id") or ""), cid):
        return True
    for sib in slot_info.get("sibling_conversation_ids") or []:
        if _cids_match(str(sib or ""), cid):
            return True
    return False


def _lock_matches_cid(lock_info: dict[str, Any], cid: str) -> bool:
    """Return True when any active slot in `lock_info` matches `cid`."""
    if not isinstance(lock_info, dict) or not cid:
        return False
    if _slot_matches_cid(lock_info, cid):
        return True
    return any(_slot_matches_cid(slot, cid) for slot in _iter_active_lock_slots(lock_info))


def _detect_phantom_pr_open(
    items: list[dict[str, Any]],
    active_locks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect `pr_open` items with zero active locks and zero `pr_urls` (phantom PRs after partial merges)."""
    phantoms: list[dict[str, Any]] = []
    for it in items:
        if str(it.get("status") or "").strip().lower() != "pr_open":
            continue
        cid = str(it.get("conversation_id") or "").strip()
        if not cid:
            continue
        pr_urls = [str(u).strip() for u in (it.get("pr_urls") or []) if str(u).strip()]
        if pr_urls:
            continue
        has_active_lock = any(_lock_matches_cid(lk, cid) for lk in active_locks.values())
        if has_active_lock:
            continue
        phantoms.append(
            {
                "conversation_id": cid,
                "title": str(it.get("title") or ""),
                "repo_slugs": list(it.get("repo_slugs") or []),
                "merged_repos": list(it.get("merged_repos") or []),
            }
        )
    if not phantoms:
        return []
    cids = [p["conversation_id"] for p in phantoms]
    return [
        _make_finding(
            code="phantom_pr_open",
            severity="high",
            category="reliability",
            title=f"{len(phantoms)} phantom pr_open item(s) without active locks or PR URLs",
            evidence={
                "count": len(phantoms),
                "conversation_ids": cids,
                "items": phantoms,
            },
            recommendation=(
                "Run `pr_remediator.py queue-sync` to auto-revert phantom `pr_open` items back to `pending` "
                "(preserving `merged_repos`) so unlocked remaining repositories can execute cleanly."
            ),
            auto_action_hint="pr_remediator.py queue-sync",
            estimated_savings_min=len(phantoms) * 15,
        )
    ]


def _detect_untracked_active_locks(
    items: list[dict[str, Any]],
    active_locks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect active `dev_pr_locks` whose conversation is absent from open `dev_queue.json` items."""
    open_cids = [
        str(it.get("conversation_id") or "").strip()
        for it in items
        if str(it.get("status") or "pending").strip().lower() in OPEN_QUEUE_STATUSES
        and str(it.get("conversation_id") or "").strip()
    ]
    untracked: list[dict[str, Any]] = []
    for repo_slug, lk in sorted(active_locks.items()):
        if not isinstance(lk, dict) or not bool(lk.get("active", True)):
            continue
        for slot_entry in _iter_active_lock_slots(lk):
            l_cid = str(slot_entry.get("conversation_id") or "").strip()
            if any(_slot_matches_cid(slot_entry, ocid) for ocid in open_cids):
                continue
            untracked.append(
                {
                    "repo_slug": repo_slug,
                    "slot_id": int(slot_entry.get("slot_id") or 1),
                    "conversation_id": l_cid,
                    "branch_name": str(slot_entry.get("branch_name") or ""),
                    "status": str(slot_entry.get("status") or ""),
                    "pr_gate_ready": bool(slot_entry.get("pr_gate_ready")),
                }
            )
    if not untracked:
        return []
    repos_str = ", ".join(u["repo_slug"].split("/")[-1] for u in untracked)
    return [
        _make_finding(
            code="untracked_active_lock",
            severity="high",
            category="visibility",
            title=f"{len(untracked)} active ad-hoc lock(s) missing from dev_queue.json ({repos_str})",
            evidence={
                "count": len(untracked),
                "locks": untracked,
            },
            recommendation=(
                "Keep active ad-hoc locks tracked in `dev_queue.json` (auto-upserted by `start-remediation` / "
                "`record-review`) and verify in `agy-queue` so human-ready PRs and running workers are never hidden."
            ),
            auto_action_hint="agy-queue --sync",
            estimated_savings_min=len(untracked) * 10,
        )
    ]


def _detect_idle_orchestrator_with_runnable_work(
    items: list[dict[str, Any]],
    active_locks: dict[str, dict[str, Any]],
    brain_dir: Path | str | Sequence[Path | str] | None,
    *,
    orchestrator_cid: str = "",
    now_ts: float | None = None,
    threshold_seconds: int | float | None = None,
) -> list[dict[str, Any]]:
    """Detect when runnable pending tasks exist for unlocked repos while the orchestrator session is idle."""
    orch_clean = str(orchestrator_cid or "").strip()
    if not orch_clean or brain_dir is None:
        return []
    tpath = dev_common.resolve_transcript_path(orch_clean, brain_dir)
    if tpath is None:
        return []
    try:
        orch_mtime = float(tpath.stat().st_mtime)
    except OSError:
        return []

    thresh = resolve_stall_threshold_seconds(threshold_seconds)
    cur_ts = float(now_ts if now_ts is not None else time.time())
    idle_seconds = max(0, int(cur_ts - orch_mtime))
    if idle_seconds < thresh:
        return []

    status_by_cid = {
        str(it.get("conversation_id") or "").strip(): str(it.get("status") or "").strip().lower()
        for it in items
        if str(it.get("conversation_id") or "").strip()
    }
    locked_slugs = {
        str(k).strip().lower()
        for k, v in active_locks.items()
        if isinstance(v, dict) and bool(v.get("active", True))
    }

    runnable_ids: list[str] = []
    free_repos: list[str] = []
    for it in items:
        if str(it.get("status") or "pending").strip().lower() != "pending":
            continue
        unmet = False
        for dep in it.get("depends_on") or []:
            dep_clean = str(dep or "").strip()
            if not dep_clean:
                continue
            for cand_cid, cand_st in status_by_cid.items():
                if _cids_match(cand_cid, dep_clean) and cand_st not in ("merged", "cleaned"):
                    unmet = True
                    break
            if unmet:
                break
        if unmet:
            continue

        merged_set = {str(r).strip().lower() for r in (it.get("merged_repos") or []) if str(r).strip()}
        remaining = [
            str(r).strip()
            for r in (it.get("repo_slugs") or [])
            if str(r).strip() and str(r).strip().lower() not in merged_set
        ]
        if not remaining:
            runnable_ids.append(str(it.get("conversation_id") or ""))
            continue
        next_repo = remaining[0]
        if next_repo.lower() not in locked_slugs:
            runnable_ids.append(str(it.get("conversation_id") or ""))
            if next_repo not in free_repos:
                free_repos.append(next_repo)

    if not runnable_ids:
        return []
    idle_lbl = format_idle_duration(idle_seconds)
    return [
        _make_finding(
            code="idle_orchestrator_with_runnable_work",
            severity="high",
            category="throughput",
            title=(
                f"Orchestrator {orch_clean[:8]} idle {idle_lbl} with {len(runnable_ids)} runnable task(s) "
                f"on unlocked repo(s)"
            ),
            evidence={
                "orchestrator_cid": orch_clean,
                "idle_seconds": idle_seconds,
                "idle_label": idle_lbl,
                "threshold_seconds": thresh,
                "runnable_count": len(runnable_ids),
                "sample_runnable_ids": runnable_ids[:5],
                "free_target_repos": free_repos,
            },
            recommendation=(
                "Resume or rotate `agy-dev-orchestrator` and arm a 0-LLM background "
                "`pr_remediator.py wait-for-unlock --orchestrator-id <CID>` task (+ safety watchdog timer) "
                "so PR merges auto-wake the orchestrator in <15s."
            ),
            auto_action_hint=(
                f"pr_remediator.py wait-for-unlock --orchestrator-id {orch_clean} --interval 15 --max-duration 3600"
            ),
            estimated_savings_min=max(15, idle_seconds // 60),
        )
    ]


def _detect_stalled_workers(
    active_locks: dict[str, dict[str, Any]],
    *,
    now_ts: float | None,
    threshold_seconds: int | float | None,
    brain_dir: Path | str | Sequence[Path | str] | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for repo_slug, repo_lk in sorted(active_locks.items()):
        if not isinstance(repo_lk, dict):
            continue
        for lk in _iter_active_lock_slots(repo_lk):
            st = detect_stalled_lock(
                lk,
                now_ts=now_ts,
                threshold_seconds=threshold_seconds,
                brain_dir=brain_dir,
            )
            if not st.get("stalled"):
                continue
            wt_clean = _sanitize_path(str(lk.get("worktree_path") or lk.get("target_repo_dir") or ""))
            br_name = str(lk.get("branch_name") or "").strip()
            idle_lbl = str(st.get("idle_label") or "")
            findings.append(
                _make_finding(
                    code="stalled_worker",
                    severity="critical",
                    category="reliability",
                    title=f"Stalled worker on {repo_slug} (idle {idle_lbl})",
                    evidence={
                        "repo_slug": repo_slug,
                        "slot_id": int(lk.get("slot_id") or 1),
                        "branch_name": br_name,
                        "worktree_path": wt_clean,
                        "status": str(lk.get("status") or ""),
                        "conversation_id": str(lk.get("conversation_id") or ""),
                        "worker_cid": str(st.get("worker_cid") or ""),
                        "reviewer_subagent_id": str(st.get("reviewer_subagent_id") or ""),
                        "idle_seconds": int(st.get("idle_seconds") or 0),
                        "idle_label": idle_lbl,
                        "elapsed_seconds": int(st.get("elapsed_seconds") or 0),
                        "elapsed_label": str(st.get("elapsed_label") or "0s"),
                        "last_activity_source": str(st.get("last_activity_source") or ""),
                        "threshold_seconds": int(st.get("threshold_seconds") or DEFAULT_STALL_THRESHOLD_SECONDS),
                    },
                    recommendation=(
                        "Inspect worktree progress immediately; re-dispatch a fresh worker with `--reuse-active` "
                        "(or release the abandoned lock) and schedule a 1200s watchdog timer on dispatch."
                    ),
                    auto_action_hint=(
                        f"git -C {wt_clean or '.'} status -sb && pr_remediator.py lock-status --repo {repo_slug}"
                    ),
                    estimated_savings_min=max(15, int(st.get("idle_seconds") or 0) // 60),
                )
            )
    return findings


def _detect_micro_pr_batch_candidates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        if str(it.get("status") or "").lower() != "pending":
            continue
        repos = [str(r).strip() for r in (it.get("repo_slugs") or []) if str(r).strip()]
        if len(repos) != 1:
            continue
        tfiles = [str(f).strip() for f in (it.get("target_files") or []) if str(f).strip()]
        if len(tfiles) > 4:
            continue
        text_blob = f"{it.get('title') or ''} {it.get('action_id') or ''} {it.get('notes') or ''}"
        is_small = (1 <= len(tfiles) <= 4) or bool(MICRO_PR_POLISH_RE.search(text_blob))
        if not is_small:
            continue
        by_repo.setdefault(repos[0], []).append(it)

    findings: list[dict[str, Any]] = []
    for repo_slug, repo_items in sorted(by_repo.items()):
        if len(repo_items) < 2:
            continue
        ordered = sorted(
            repo_items,
            key=lambda x: (
                int(x.get("priority") if x.get("priority") is not None else 50),
                str(x.get("added_at") or ""),
                str(x.get("conversation_id") or ""),
            ),
        )
        i = 0
        while i < len(ordered):
            p_start = int(ordered[i].get("priority") if ordered[i].get("priority") is not None else 50)
            j = i + 1
            while j < len(ordered):
                p_cur = int(ordered[j].get("priority") if ordered[j].get("priority") is not None else 50)
                if p_cur - p_start <= 4:
                    j += 1
                else:
                    break
            cluster = ordered[i:j]
            if len(cluster) >= 2:
                n_items = len(cluster)
                prios = [int(c.get("priority") if c.get("priority") is not None else 50) for c in cluster]
                spread = max(prios) - min(prios)
                cids = [str(c.get("conversation_id") or "") for c in cluster]
                titles = [str(c.get("title") or "") for c in cluster]
                savings = (n_items - 1) * 18
                findings.append(
                    _make_finding(
                        code="micro_pr_batch_candidates",
                        severity="high" if n_items >= 3 else "medium",
                        category="throughput",
                        title=f"Batch {n_items} small pending PRs in {repo_slug} (~{savings}m saved)",
                        evidence={
                            "repo_slug": repo_slug,
                            "count": n_items,
                            "candidate_ids": cids,
                            "titles": titles,
                            "priorities": prios,
                            "priority_spread": spread,
                        },
                        recommendation=(
                            f"Combine these {n_items} small same-repo pending tasks (priority spread {spread}) into one "
                            "cohesive batched PR to eliminate repeated worktree setup, CI, and review round-trips."
                        ),
                        auto_action_hint=(
                            f"Invoke agy-dev-planner to merge {', '.join(c[:8] for c in cids)} into one batched PR in {repo_slug}."
                        ),
                        estimated_savings_min=savings,
                    )
                )
                i = j
            else:
                i += 1
    return findings


def _detect_repo_contention_hotspots(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    open_items = [it for it in items if str(it.get("status") or "pending").lower() in OPEN_QUEUE_STATUSES]
    open_count = len(open_items)
    if open_count == 0:
        return []

    repo_counts: dict[str, int] = {}
    multi_repo_ids: list[str] = []
    for it in open_items:
        unique_repos = sorted({str(r).strip() for r in (it.get("repo_slugs") or []) if str(r).strip()})
        for slug in unique_repos:
            repo_counts[slug] = repo_counts.get(slug, 0) + 1
        if len(unique_repos) >= 2:
            multi_repo_ids.append(str(it.get("conversation_id") or ""))

    hotspot_repos = [
        {"repo_slug": slug, "count": cnt, "share_pct": round((cnt / open_count) * 100.0, 1)}
        for slug, cnt in sorted(repo_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        if cnt >= 4 and (cnt / open_count) >= 0.60
    ]
    multi_repo_ratio = len(multi_repo_ids) / open_count
    multi_repo_hotspot = open_count >= 2 and len(multi_repo_ids) >= 2 and multi_repo_ratio >= 0.50

    if not hotspot_repos and not multi_repo_hotspot:
        return []

    top_share = max((float(h["share_pct"]) / 100.0 for h in hotspot_repos), default=0.0)
    severity = "high" if (top_share >= 0.75 or (multi_repo_hotspot and multi_repo_ratio >= 0.60)) else "medium"
    if hotspot_repos:
        top = hotspot_repos[0]
        title = f"Repo contention hotspot on {top['repo_slug']} ({top['count']}/{open_count} open items)"
    else:
        title = f"Multi-repo serialization hotspot ({len(multi_repo_ids)}/{open_count} open items span >=2 repos)"

    max_cnt = max((int(h["count"]) for h in hotspot_repos), default=len(multi_repo_ids))
    return [
        _make_finding(
            code="repo_contention_hotspot",
            severity=severity,
            category="throughput",
            title=title,
            evidence={
                "open_count": open_count,
                "repo_counts": repo_counts,
                "hotspot_repos": hotspot_repos,
                "multi_repo_count": len(multi_repo_ids),
                "multi_repo_ratio_pct": round(multi_repo_ratio * 100.0, 1),
                "multi_repo_ids": multi_repo_ids,
            },
            recommendation=(
                "Use contract-first parallel execution across distinct unlocked repositories and run read-only "
                "agy-dev-planner pre-anchoring during `pr_open` wait windows to avoid serial queue bottlenecks."
            ),
            auto_action_hint="Run `pr_remediator.py queue-next --unlocked` and parallelize contract-anchored repos.",
            estimated_savings_min=max(15, max_cnt * 8),
        )
    ]


def resolve_max_partial_wip(max_partial_wip: int | None = None) -> int:
    """Resolve partial-WIP accumulation threshold (default `3`, env `AGY_DEV_MAX_PARTIAL_WIP`)."""
    if max_partial_wip is not None:
        with contextlib.suppress(TypeError, ValueError):
            parsed = int(max_partial_wip)
            if parsed >= 1:
                return parsed
    env_raw = str(os.environ.get("AGY_DEV_MAX_PARTIAL_WIP") or "").strip()
    if env_raw:
        with contextlib.suppress(TypeError, ValueError):
            parsed = int(env_raw)
            if parsed >= 1:
                return parsed
    return DEFAULT_MAX_PARTIAL_WIP


def _raw_item_priority(item: dict[str, Any]) -> int:
    """Extract raw integer priority from a queue item (default `50`)."""
    try:
        val = item.get("priority", 50)
        return int(val) if val is not None else 50
    except (TypeError, ValueError):
        return 50


def _detect_priority_inversions(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detect open items depending on strictly lower-priority (higher-number) open prerequisites (e.g. P20 -> P26)."""
    open_items = [
        it
        for it in items
        if str(it.get("status") or "pending").strip().lower() in OPEN_QUEUE_STATUSES
        and str(it.get("conversation_id") or "").strip()
    ]
    if len(open_items) < 2:
        return []

    by_cid = {str(it.get("conversation_id") or "").strip(): it for it in open_items}
    open_cids = list(by_cid.keys())
    inversions: list[dict[str, Any]] = []

    for dep_cid, dep_item in by_cid.items():
        dep_prio = _raw_item_priority(dep_item)
        for raw_ref in dep_item.get("depends_on") or []:
            ref_clean = str(raw_ref or "").strip()
            if not ref_clean:
                continue
            matched = [c for c in open_cids if c.lower() == ref_clean.lower() and c != dep_cid]
            if not matched:
                matched = [c for c in open_cids if _cids_match(c, ref_clean) and c != dep_cid]
            for prereq_cid in matched:
                prereq_item = by_cid[prereq_cid]
                prereq_prio = _raw_item_priority(prereq_item)
                if prereq_prio > dep_prio:
                    inversions.append(
                        {
                            "dependent_id": dep_cid,
                            "dependent_title": str(dep_item.get("title") or ""),
                            "dependent_priority": dep_prio,
                            "dependency_id": prereq_cid,
                            "dependency_title": str(prereq_item.get("title") or ""),
                            "dependency_priority": prereq_prio,
                            "priority_gap": prereq_prio - dep_prio,
                        }
                    )

    if not inversions:
        return []

    inversions.sort(key=lambda inv: (int(inv["dependent_priority"]), -int(inv["priority_gap"]), inv["dependent_id"]))
    top = inversions[0]
    return [
        _make_finding(
            code="priority_inversion",
            severity="high",
            category="scheduling",
            title=(
                f"Priority inversion across {len(inversions)} open dependency edge(s) "
                f"(P{top['dependent_priority']} {top['dependent_id'][:8]} depends on "
                f"P{top['dependency_priority']} {top['dependency_id'][:8]})"
            ),
            evidence={
                "count": len(inversions),
                "inversions": inversions,
                "dependent_ids": [inv["dependent_id"] for inv in inversions],
                "dependency_ids": list(dict.fromkeys(inv["dependency_id"] for inv in inversions)),
            },
            recommendation=(
                "Promote blocking prerequisite priorities (`queue-add --id <DEP_CID> --priority <MIN_DEPENDENT_PRIO>`) "
                "to match their highest-priority open dependent (`effective_priority` auto-inherits during `queue-next`)."
            ),
            auto_action_hint=(
                f"pr_remediator.py queue-add --id {top['dependency_id']} --priority {top['dependent_priority']}"
            ),
            estimated_savings_min=len(inversions) * 12,
        )
    ]


def _detect_partial_wip_accumulation(
    items: list[dict[str, Any]],
    *,
    max_partial_wip: int | None = None,
) -> list[dict[str, Any]]:
    """Detect downstream repositories accumulating `>= AGY_DEV_MAX_PARTIAL_WIP` half-merged open items."""
    cap = resolve_max_partial_wip(max_partial_wip)
    open_items = [
        it
        for it in items
        if str(it.get("status") or "pending").strip().lower() in OPEN_QUEUE_STATUSES
    ]
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for it in open_items:
        merged_set = {
            dev_common.normalize_repo_slug(str(r))
            for r in (it.get("merged_repos") or [])
            if str(r).strip()
        }
        if not merged_set:
            continue
        repo_slugs = [
            dev_common.normalize_repo_slug(str(r))
            for r in (it.get("repo_slugs") or [])
            if str(r).strip()
        ]
        remaining: list[str] = []
        for slug in repo_slugs:
            if slug and slug not in merged_set and slug not in remaining:
                remaining.append(slug)
        for rem_slug in remaining:
            by_repo.setdefault(rem_slug, []).append(it)

    flagged: list[dict[str, Any]] = []
    for slug, waiting in sorted(by_repo.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(waiting) >= cap:
            flagged.append(
                {
                    "repo_slug": slug,
                    "count": len(waiting),
                    "conversation_ids": [str(w.get("conversation_id") or "") for w in waiting],
                    "titles": [str(w.get("title") or "") for w in waiting],
                }
            )

    if not flagged:
        return []

    top = flagged[0]
    return [
        _make_finding(
            code="partial_wip_accumulation",
            severity="high",
            category="throughput",
            title=(
                f"Partial-WIP accumulation on {top['repo_slug']} "
                f"({top['count']} half-merged items >= cap {cap})"
            ),
            evidence={
                "max_partial_wip": cap,
                "count": len(flagged),
                "repos": flagged,
                "top_repo_slug": top["repo_slug"],
                "top_count": top["count"],
                "conversation_ids": top["conversation_ids"],
            },
            recommendation=(
                "Close existing half-merged (`merged_repos` non-empty) items on the downstream repository before "
                f"starting new 0% partial multi-repo dispatches (`AGY_DEV_PRIORITIZE_PARTIAL_WIP=1`, `AGY_DEV_MAX_PARTIAL_WIP={cap}`)."
            ),
            auto_action_hint=(
                f"Finish half-merged PRs on {top['repo_slug']} via `pr_remediator.py queue-next --unlocked`."
            ),
            estimated_savings_min=sum(int(entry["count"]) * 10 for entry in flagged),
        )
    ]


def _detect_stale_follower_needs_rebase(
    active_locks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect any parallel follower / Slot #2..#N lock in `NEEDS_REBASE_REVERIFY` or `base_stale=True` needing `rebase-reverify`."""
    flagged: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for repo_slug, repo_lk in sorted(active_locks.items()):
        if not isinstance(repo_lk, dict) or not bool(repo_lk.get("active", True)):
            continue
        for slot in _iter_active_lock_slots(repo_lk):
            slot_id = int(slot.get("slot_id") or 1)
            st_u = str(slot.get("status") or "").strip().upper()
            base_stale = bool(slot.get("base_stale")) or bool(slot.get("needs_base_rebase"))
            if st_u != "NEEDS_REBASE_REVERIFY" and not base_stale:
                continue
            br = str(slot.get("branch_name") or "").strip()
            dedup_key = (repo_slug, br or f"slot-{slot_id}")
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            wt_clean = _sanitize_path(str(slot.get("worktree_path") or slot.get("target_repo_dir") or ""))
            flagged.append(
                {
                    "repo_slug": repo_slug,
                    "slot_id": slot_id,
                    "promoted_from_slot": slot.get("promoted_from_slot"),
                    "conversation_id": str(slot.get("conversation_id") or ""),
                    "branch_name": br,
                    "status": st_u or "NEEDS_REBASE_REVERIFY",
                    "base_stale": True,
                    "worktree_path": wt_clean,
                }
            )

    if not flagged:
        return []
    top = flagged[0]
    wt_arg = f" --workdir {top['worktree_path']}" if top["worktree_path"] else ""
    return [
        _make_finding(
            code="stale_follower_needs_rebase",
            severity="high",
            category="throughput",
            title=(
                f"{len(flagged)} follower lock(s) require rebase-reverify "
                f"({top['repo_slug']} · {top['branch_name'] or top['status']})"
            ),
            evidence={
                "count": len(flagged),
                "top_repo_slug": top["repo_slug"],
                "branch_name": top["branch_name"],
                "conversation_ids": [f["conversation_id"] for f in flagged if f["conversation_id"]],
                "slots": flagged,
            },
            recommendation=(
                "Run `pr_remediator.py rebase-reverify --fetch` so drifted follower branches rebase onto updated "
                "`origin/main` and run union cross-impact verification (`AUTO_PROMOTED_ZERO_LLM` when clean)."
            ),
            auto_action_hint=f"pr_remediator.py rebase-reverify --repo {top['repo_slug']}{wt_arg} --fetch",
            estimated_savings_min=len(flagged) * 12,
        )
    ]


def _detect_parallel_lookahead_opportunity(
    items: list[dict[str, Any]],
    active_locks: dict[str, dict[str, Any]],
    repo_dirs: dict[str, Path],
) -> list[dict[str, Any]]:
    """Detect when Slot #1 is `PENDING_HUMAN_PR_VALIDATION` and a Slot #2..#N is free (`active < max_slots`).

    Candidates must be parallel-safe pairwise against EVERY active slot of the repo (worst tier wins).
    """
    if not items or not active_locks:
        return []

    status_by_cid = {
        str(it.get("conversation_id") or "").strip(): str(it.get("status") or "").strip().lower()
        for it in items
        if str(it.get("conversation_id") or "").strip()
    }
    pending_sorted = sorted(
        (
            it
            for it in items
            if str(it.get("status") or "pending").strip().lower() == "pending"
            and str(it.get("conversation_id") or "").strip()
        ),
        key=lambda x: (
            int(
                x.get("effective_priority")
                if x.get("effective_priority") is not None
                else _raw_item_priority(x)
            ),
            _raw_item_priority(x),
            str(x.get("added_at") or ""),
            str(x.get("conversation_id") or ""),
        ),
    )
    if not pending_sorted:
        return []

    opportunities: list[dict[str, Any]] = []
    for repo_slug, repo_lk in sorted(active_locks.items()):
        if not isinstance(repo_lk, dict) or not bool(repo_lk.get("active", True)):
            continue
        max_slots = resolve_max_repo_slots(repo_lk.get("max_slots"))
        if max_slots < 2:
            continue

        active_slots = _iter_active_lock_slots(repo_lk)
        active_ids = [int(s.get("slot_id") or 1) for s in active_slots]
        if not active_slots or active_ids[0] != 1 or len(active_slots) >= max_slots:
            continue
        free_slot_id = next((sid for sid in range(2, max_slots + 1) if sid not in active_ids), 0)
        if not free_slot_id:
            continue
        top_level = {k: v for k, v in repo_lk.items() if k != "slots" and v not in (None, "", [], {})}
        slot1 = {**active_slots[0], **top_level, "slot_id": 1}
        s1_status = str(slot1.get("status") or "").strip().upper()
        if s1_status != "PENDING_HUMAN_PR_VALIDATION":
            continue
        if bool(slot1.get("base_stale")) or bool(slot1.get("needs_base_rebase")):
            continue

        norm_repo = dev_common.normalize_repo_slug(repo_slug) or repo_slug.lower()
        r_dir = repo_dirs.get(repo_slug) or repo_dirs.get(norm_repo)

        # Resolve anchors for EVERY active slot (git-diff fallback when a slot recorded no targets/anchors)
        slot_specs: list[dict[str, Any]] = []
        for slot_obj in [slot1, *active_slots[1:]]:
            s_targets = [str(f).strip() for f in (slot_obj.get("target_files") or []) if str(f).strip()]
            if not s_targets and not slot_obj.get("section_anchors"):
                s_wt_raw = str(slot_obj.get("worktree_path") or "").strip()
                wt_dir = Path(s_wt_raw.replace("$HOME", str(Path.home()))).expanduser() if s_wt_raw else r_dir
                if wt_dir is not None and (wt_dir / ".git").exists():
                    s_targets = _parallel_scope.collect_git_changed_files(
                        r_dir or wt_dir,
                        branch_name=str(slot_obj.get("branch_name") or ""),
                        base_sha=str(slot_obj.get("base_sha") or ""),
                        worktree_path=s_wt_raw,
                    )
            if not s_targets and not slot_obj.get("section_anchors"):
                break
            slot_specs.append({**slot_obj, "target_files": s_targets})
        if len(slot_specs) != len(active_slots):
            # At least one active slot cannot prove disjointness; never suggest a parallel dispatch
            continue

        disjoint_cands: list[dict[str, Any]] = []
        repo_structures: dict[str, Any] | None = None
        repo_forward_graph: dict[str, tuple[str, ...]] | None = None
        for cand in pending_sorted:
            cand_cid = str(cand.get("conversation_id") or "").strip()
            if _lock_matches_cid(repo_lk, cand_cid):
                continue

            unmet_dep = False
            for raw_dep in cand.get("depends_on") or []:
                dep_s = str(raw_dep or "").strip()
                if not dep_s:
                    continue
                if any(_slot_matches_cid(sl, dep_s) for sl in slot_specs):
                    unmet_dep = True
                    break
                for ocid, ost in status_by_cid.items():
                    if _cids_match(ocid, dep_s) and ost not in ("merged", "cleaned"):
                        unmet_dep = True
                        break
                if unmet_dep:
                    break
            if unmet_dep:
                continue

            merged_set = {
                dev_common.normalize_repo_slug(str(r)) or str(r).strip().lower()
                for r in (cand.get("merged_repos") or [])
                if str(r).strip()
            }
            rem_repos = [
                dev_common.normalize_repo_slug(str(r)) or str(r).strip().lower()
                for r in (cand.get("repo_slugs") or [])
                if str(r).strip()
                and (dev_common.normalize_repo_slug(str(r)) or str(r).strip().lower()) not in merged_set
            ]
            if not rem_repos or rem_repos[0] != norm_repo:
                continue

            cand_spec = _parallel_scope.TaskAnchorSpec.from_mapping(cand)
            if not cand_spec.target_files and not cand_spec.section_anchors:
                continue

            if repo_structures is None and r_dir is not None and r_dir.is_dir():
                repo_structures, repo_forward_graph = _parallel_scope.build_repository_import_graph(r_dir)

            # Pairwise validation against ALL active slots (worst tier wins), reusing the repo import graph
            multi_ov = _parallel_scope.analyze_candidate_against_slots(
                slot_specs,
                cand_spec,
                repo_dir=r_dir,
                repo_slug=repo_slug,
                structures=repo_structures,
                forward_graph=repo_forward_graph,
            )
            if multi_ov.get("parallel_safe"):
                disjoint_cands.append(
                    {
                        "conversation_id": cand_cid,
                        "title": str(cand.get("title") or ""),
                        "priority": _raw_item_priority(cand),
                        "overlap_tier": str(multi_ov.get("overlap_tier") or ""),
                    }
                )

        if disjoint_cands:
            best = disjoint_cands[0]
            opportunities.append(
                {
                    "repo_slug": repo_slug,
                    "slot1_conversation_id": str(slot1.get("conversation_id") or ""),
                    "slot1_branch": str(slot1.get("branch_name") or ""),
                    "max_slots": max_slots,
                    "active_slot_ids": active_ids,
                    "free_slot_id": free_slot_id,
                    "next_slot_id": free_slot_id,
                    "free_slot_count": max_slots - len(active_slots),
                    "candidate_count": len(disjoint_cands),
                    "top_candidate_id": best["conversation_id"],
                    "top_candidate_title": best["title"],
                    "top_overlap_tier": best["overlap_tier"],
                    "candidates": disjoint_cands[:5],
                }
            )

    if not opportunities:
        return []
    top_op = opportunities[0]
    top_slot = int(top_op["free_slot_id"])
    return [
        _make_finding(
            code="parallel_lookahead_opportunity",
            severity="high",
            category="throughput",
            title=(
                f"Slot #{top_slot} free on {top_op['repo_slug']} ({len(top_op['active_slot_ids'])}/{top_op['max_slots']} "
                f"slots active) while Slot #1 awaits PR merge "
                f"({top_op['candidate_count']} disjoint pending task(s), top: {top_op['top_candidate_id'][:8]} [{top_op['top_overlap_tier']}])"
            ),
            evidence={
                "count": len(opportunities),
                "top_repo_slug": top_op["repo_slug"],
                "top_candidate_id": top_op["top_candidate_id"],
                "top_overlap_tier": top_op["top_overlap_tier"],
                "free_slot_id": top_slot,
                "next_slot_id": top_slot,
                "opportunities": opportunities,
            },
            recommendation=(
                f"Dispatch the pairwise-disjoint pending candidate into parallel Slot #{top_slot} "
                "(`start-remediation --parallel-slot --worktree`; legacy alias `--allow-slot2` still works) "
                "while Slot #1 awaits human PR validation (Slots #2..#N stay `READY_AWAITING_BASE_MERGE`, "
                "preserving max 1 open GitHub PR per repo)."
            ),
            auto_action_hint=(
                f"pr_remediator.py queue-next --unlocked && pr_remediator.py start-remediation --id {top_op['top_candidate_id']} --allow-slot2 --worktree"
            ),
            estimated_savings_min=sum(int(op.get("free_slot_count") or 1) for op in opportunities) * 15,
        )
    ]


def _detect_standby_dependency_deadlock(
    items: list[dict[str, Any]],
    active_locks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Detect pending items whose `depends_on` (directly or transitively) waits on `standby`/`cancelled`/`rejected` items.

    Such items can never be dispatched by `queue-next` until a human re-activates the dependency or rewrites
    the dependency list, so they silently starve FREE repositories (no active lock) the most.
    """
    if not items:
        return []
    by_cid: dict[str, dict[str, Any]] = {}
    for it in items:
        cid = str(it.get("conversation_id") or "").strip()
        if cid:
            by_cid[cid] = it

    def _resolve_dep(dep_ref: str) -> tuple[str, dict[str, Any]] | None:
        for cand_cid, cand_it in by_cid.items():
            if _cids_match(cand_cid, dep_ref):
                return (cand_cid, cand_it)
        return None

    def _status(it: dict[str, Any]) -> str:
        return str(it.get("status") or "pending").strip().lower()

    pending = [it for it in items if _status(it) in DEPENDENCY_BLOCKED_OPEN_STATUSES and str(it.get("conversation_id") or "").strip()]
    # Direct blockers per pending item
    direct: dict[str, list[dict[str, Any]]] = {}
    pending_deps: dict[str, list[str]] = {}
    for it in pending:
        cid = str(it.get("conversation_id") or "").strip()
        for raw_dep in it.get("depends_on") or []:
            dep_ref = str(raw_dep or "").strip()
            if not dep_ref:
                continue
            resolved = _resolve_dep(dep_ref)
            if resolved is None:
                continue
            dep_cid, dep_it = resolved
            dep_st = _status(dep_it)
            if dep_st in DEADLOCK_DEPENDENCY_STATUSES:
                direct.setdefault(cid, []).append(
                    {
                        "conversation_id": dep_cid,
                        "depends_on_ref": dep_ref,
                        "status": dep_st,
                        "title": str(dep_it.get("title") or ""),
                    }
                )
            elif dep_st in DEPENDENCY_BLOCKED_OPEN_STATUSES:
                pending_deps.setdefault(cid, []).append(dep_cid)

    # Transitive propagation: a pending item waiting on a deadlocked pending item is deadlocked too (cycle-safe)
    deadlocked: dict[str, list[str]] = {cid: [] for cid in direct}
    changed = True
    while changed:
        changed = False
        for cid, dep_cids in pending_deps.items():
            if cid in deadlocked:
                continue
            via = [d for d in dep_cids if d in deadlocked]
            if via:
                deadlocked[cid] = via
                changed = True
    if not deadlocked:
        return []

    locked_slugs = {
        dev_common.normalize_repo_slug(str(k)) or str(k).strip().lower()
        for k, v in active_locks.items()
        if isinstance(v, dict) and bool(v.get("active", True))
    }

    def _root_blockers(cid: str, seen: set[str]) -> list[dict[str, Any]]:
        if cid in seen:
            return []
        seen.add(cid)
        out = list(direct.get(cid, []))
        for via_cid in deadlocked.get(cid, []):
            out.extend(_root_blockers(via_cid, seen))
        return out

    offenders: list[dict[str, Any]] = []
    for it in pending:
        cid = str(it.get("conversation_id") or "").strip()
        if cid not in deadlocked:
            continue
        merged_set = {
            dev_common.normalize_repo_slug(str(r)) or str(r).strip().lower()
            for r in (it.get("merged_repos") or [])
            if str(r).strip()
        }
        remaining = [
            dev_common.normalize_repo_slug(str(r)) or str(r).strip().lower()
            for r in (it.get("repo_slugs") or [])
            if str(r).strip()
        ]
        remaining = [r for r in remaining if r not in merged_set]
        next_repo = remaining[0] if remaining else ""
        on_free_repo = not next_repo or next_repo not in locked_slugs

        blockers = direct.get(cid, [])
        roots: list[dict[str, Any]] = []
        for rb in _root_blockers(cid, set()):
            if all(rb["conversation_id"] != r["conversation_id"] for r in roots):
                roots.append(rb)

        hints: list[str] = []
        if blockers:
            dead_refs = {b["depends_on_ref"] for b in blockers}
            keep_deps = [
                str(d).strip()
                for d in (it.get("depends_on") or [])
                if str(d).strip() and str(d).strip() not in dead_refs
            ]
            if keep_deps:
                hints.append(f"pr_remediator.py queue-add --id {cid} --replace-depends-on {' '.join(keep_deps)}")
            else:
                hints.append(f"pr_remediator.py queue-add --id {cid} --clear-depends-on")
        for rb in roots:
            if rb["status"] == "standby":
                hints.append(f"pr_remediator.py queue-set-status --id {rb['conversation_id']} --status pending")
        offenders.append(
            {
                "conversation_id": cid,
                "title": str(it.get("title") or ""),
                "priority": _raw_item_priority(it),
                "status": _status(it),
                "repo_slugs": list(it.get("repo_slugs") or []),
                "next_repo": next_repo,
                "on_free_repo": on_free_repo,
                "blocking_dependencies": blockers,
                "transitive_via": list(deadlocked.get(cid, [])),
                "root_blockers": roots,
                "remediation_hints": hints,
            }
        )

    offenders.sort(key=lambda o: (0 if o["on_free_repo"] else 1, o["priority"], o["conversation_id"]))
    free_count = sum(1 for o in offenders if o["on_free_repo"])
    root_ids: list[str] = []
    root_statuses: dict[str, str] = {}
    for o in offenders:
        for rb in o["root_blockers"]:
            if rb["conversation_id"] not in root_ids:
                root_ids.append(rb["conversation_id"])
                root_statuses[rb["conversation_id"]] = rb["status"]
    top = offenders[0]
    all_hints: list[str] = []
    for o in offenders:
        for h in o["remediation_hints"]:
            if h not in all_hints:
                all_hints.append(h)
    return [
        _make_finding(
            code="standby_dependency_deadlock",
            severity="high" if free_count else "medium",
            category="throughput",
            title=(
                f"{len(offenders)} pending item(s) deadlocked on standby/cancelled/rejected dependencies "
                f"({free_count} on free repo(s); blockers: {', '.join(c[:8] for c in root_ids[:3])})"
            ),
            evidence={
                "count": len(offenders),
                "free_repo_count": free_count,
                "conversation_ids": [o["conversation_id"] for o in offenders],
                "blocking_dependency_ids": root_ids,
                "blocking_dependency_statuses": root_statuses,
                "items": offenders,
                "remediation_hints": all_hints,
            },
            recommendation=(
                "Pending work cannot dispatch while it depends on `standby`/`cancelled`/`rejected` items. Either "
                "re-activate the blocker (`queue-set-status --id <dep> --status pending`) or rewrite the dependent's "
                "`depends_on` (`queue-add --id <cid> --replace-depends-on <ids...>` / `--clear-depends-on`)."
            ),
            auto_action_hint=(top["remediation_hints"][0] if top["remediation_hints"] else "pr_remediator.py queue-list --status standby"),
            estimated_savings_min=len(offenders) * 10 + free_count * 10,
        )
    ]


def _detect_unanchored_pending_tasks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unanchored = [
        it
        for it in items
        if str(it.get("status") or "").lower() == "pending"
        and not [str(f).strip() for f in (it.get("target_files") or []) if str(f).strip()]
    ]
    if not unanchored:
        return []
    cids = [str(it.get("conversation_id") or "") for it in unanchored]
    titles = [str(it.get("title") or "") for it in unanchored]
    return [
        _make_finding(
            code="unanchored_pending_tasks",
            severity="medium",
            category="planning",
            title=f"{len(unanchored)} pending task(s) lack pre-anchored target_files",
            evidence={
                "count": len(unanchored),
                "conversation_ids": cids,
                "titles": titles,
            },
            recommendation=(
                "Run read-only `agy-dev-planner` during `pr_open` idle windows to populate exact `target_files`, "
                "line ranges, and test targets so workers execute with zero cold-start discovery."
            ),
            auto_action_hint=(
                f"Pre-anchor {', '.join(c[:8] for c in cids[:5])} with read-only agy-dev-planner and update via queue-add."
            ),
            estimated_savings_min=len(unanchored) * 5,
        )
    ]


def _count_file_lines(fpath: Path) -> int:
    with contextlib.suppress(OSError):
        data = fpath.read_bytes()
        if not data:
            return 0
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
    return 0


def _detect_monolithic_god_files(
    repo_dirs: dict[str, Path],
    open_items: list[dict[str, Any]],
    *,
    line_threshold: int = 5000,
) -> list[dict[str, Any]]:
    queued_rel_paths: set[str] = set()
    queued_basenames: set[str] = set()
    for it in open_items:
        for raw_f in it.get("target_files") or []:
            norm = str(raw_f or "").strip().replace("\\", "/").lstrip("./")
            if norm:
                queued_rel_paths.add(norm)
                queued_basenames.add(Path(norm).name)

    hotspots: list[dict[str, Any]] = []
    for repo_slug, repo_path in sorted(repo_dirs.items()):
        if not repo_path.is_dir():
            continue
        for root, dirnames, filenames in os.walk(repo_path, topdown=True, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in WALK_SKIP_DIRS]
            root_p = Path(root)
            for fname in filenames:
                fpath = root_p / fname
                suffix = fpath.suffix.lower()
                if suffix not in CODE_EXTENSIONS:
                    continue
                line_cnt = _count_file_lines(fpath)
                if line_cnt <= line_threshold:
                    continue
                rel = fpath.relative_to(repo_path).as_posix()
                is_queued = (rel in queued_rel_paths) or (fname in queued_basenames)
                skill = "agy-web-ui-architecture" if suffix in (".js", ".css", ".html") else "agy-code-design-refactor"
                hotspots.append(
                    {
                        "repo_slug": repo_slug,
                        "path": rel,
                        "lines": line_cnt,
                        "queued_reference": is_queued,
                        "suggested_skill": skill,
                    }
                )

    if not hotspots:
        return []

    hotspots.sort(key=lambda h: (not h["queued_reference"], -int(h["lines"]), h["repo_slug"], h["path"]))
    top = hotspots[0]
    queued_hits = sum(1 for h in hotspots if h["queued_reference"])
    return [
        _make_finding(
            code="monolithic_god_files",
            severity="medium",
            category="architecture",
            title=(
                f"{len(hotspots)} monolithic file(s) >{line_threshold} lines "
                f"(top: {top['path']} @ {top['lines']}L)"
            ),
            evidence={
                "threshold_lines": line_threshold,
                "total_count": len(hotspots),
                "queued_referenced_count": queued_hits,
                "files": hotspots[:10],
            },
            recommendation=(
                "Decompose oversized files incrementally using characterization-first modularization "
                "(`agy-web-ui-architecture` for JS/HTML/CSS, `agy-code-design-refactor` for Python/Shell) "
                "to cut agent read/edit token costs and reduce merge conflicts."
            ),
            auto_action_hint=f"Plan modular split for {top['repo_slug']}:{top['path']} via {top['suggested_skill']}.",
            estimated_savings_min=min(60, len(hotspots) * 10),
        )
    ]


def _detect_missing_selective_test_runners(repo_dirs: dict[str, Path]) -> list[dict[str, Any]]:
    flagged: list[dict[str, Any]] = []
    for repo_slug, repo_path in sorted(repo_dirs.items()):
        pc_cfg = repo_path / ".pre-commit-config.yaml"
        if not pc_cfg.is_file():
            continue
        with contextlib.suppress(OSError):
            raw_text = pc_cfg.read_text(encoding="utf-8", errors="ignore")
            active_lines = [ln for ln in raw_text.splitlines() if not ln.lstrip().startswith("#")]
            active_text = "\n".join(active_lines)
            has_selective_file = (repo_path / "tests" / "scripts" / "selective_test_runner.py").is_file()
            uses_full_discover = bool(re.search(r"unittest\s+discover", active_text))
            missing_selective = uses_full_discover and (
                (not has_selective_file) or ("selective_test_runner.py" not in active_text)
            )
            qa_lines = [ln for ln in active_lines if "qa_static_audit.py" in ln]
            non_incremental_qa = bool(qa_lines) and any("--incremental" not in ln for ln in qa_lines)
            if missing_selective or non_incremental_qa:
                flagged.append(
                    {
                        "repo_slug": repo_slug,
                        "missing_selective_runner": missing_selective,
                        "non_incremental_qa_audit": non_incremental_qa,
                    }
                )
    if not flagged:
        return []
    any_missing_sel = any(bool(r["missing_selective_runner"]) for r in flagged)
    repo_list = ", ".join(str(r["repo_slug"]) for r in flagged)
    return [
        _make_finding(
            code="missing_selective_test_runner",
            severity="high" if any_missing_sel else "medium",
            category="testing",
            title=f"Pre-commit test/audit bottleneck in {len(flagged)} repo(s): {repo_list}",
            evidence={"repos": flagged},
            recommendation=(
                "Adopt `tests/scripts/selective_test_runner.py` in `.pre-commit-config.yaml` instead of full "
                "`unittest discover`, pass `--incremental` to `qa_static_audit.py`, and run single-pass "
                "`pr_remediator.py test-local --with-pre-commit`."
            ),
            auto_action_hint=f"Add selective_test_runner.py / --incremental pre-commit hooks in {repo_list}.",
            estimated_savings_min=len(flagged) * 8,
        )
    ]


def _inspect_single_transcript(tpath: Path) -> dict[str, Any]:
    """Extract compact efficiency metrics from one Antigravity `transcript.jsonl` file."""
    step_count = 0
    run_command_count = 0
    saw_branch_diff = False
    saw_record_review = False
    saw_reviewer_persona = False
    reran_tests_or_precommit = False
    invoked_any_subagent = False
    dev_invocations: list[str] = []

    def _walk_obj(obj: Any) -> None:
        nonlocal run_command_count, saw_branch_diff, saw_record_review
        nonlocal reran_tests_or_precommit, invoked_any_subagent
        if isinstance(obj, dict):
            name_l = str(obj.get("name") or obj.get("tool") or obj.get("toolName") or "").strip().lower()
            step_type_u = str(obj.get("type") or "").strip().upper()
            args_obj = obj.get("arguments") or obj.get("args") or obj.get("spec") or obj
            if name_l == "run_command" or step_type_u == "RUN_COMMAND":
                run_command_count += 1
                cmd_str = ""
                if isinstance(args_obj, dict):
                    cmd_str = str(args_obj.get("CommandLine") or args_obj.get("commandLine") or args_obj.get("command") or "")
                elif isinstance(obj.get("content"), str):
                    cmd_str = obj["content"]
                if "branch-diff" in cmd_str:
                    saw_branch_diff = True
                if "record-review" in cmd_str:
                    saw_record_review = True
                if _REDUNDANT_REVIEW_TEST_RE.search(cmd_str):
                    reran_tests_or_precommit = True
            elif name_l == "invoke_subagent" or (
                isinstance(obj.get("spec"), dict) and ("typeName" in obj["spec"] or "TypeName" in obj["spec"])
            ):
                invoked_any_subagent = True
                spec_d = args_obj if isinstance(args_obj, dict) else {}
                tname = str(spec_d.get("TypeName") or spec_d.get("typeName") or spec_d.get("type_name") or "").strip().lower()
                if tname == "agy-code-developer":
                    model = str(spec_d.get("Model") or spec_d.get("model") or "pro").strip().lower() or "pro"
                    dev_invocations.append(model)
            for k, v in obj.items():
                if k in ("tool_calls", "calls", "toolCall") and isinstance(v, (dict, list)):
                    _walk_obj(v)
        elif isinstance(obj, list):
            for entry in obj:
                _walk_obj(entry)

    try:
        with tpath.open("r", encoding="utf-8", errors="ignore") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                step_count += 1
                if step_count <= 5 and "agy-code-pr-reviewer" in line and "invoke_subagent" not in line:
                    saw_reviewer_persona = True
                if (
                    "run_command" not in line
                    and "RUN_COMMAND" not in line
                    and "invoke_subagent" not in line
                    and "agy-code-developer" not in line
                    and "branch-diff" not in line
                    and "record-review" not in line
                ):
                    continue
                with contextlib.suppress(json.JSONDecodeError):
                    parsed = json.loads(line)
                    if isinstance(parsed, dict):
                        _walk_obj(parsed)
                        content_val = parsed.get("content")
                        if isinstance(content_val, str) and content_val.strip().startswith("{"):
                            with contextlib.suppress(json.JSONDecodeError):
                                inner = json.loads(content_val)
                                if isinstance(inner, dict):
                                    _walk_obj(inner)
                        elif isinstance(content_val, dict):
                            _walk_obj(content_val)
    except OSError:
        return {
            "step_count": 0,
            "is_reviewer": False,
            "run_command_count": 0,
            "reran_tests_or_precommit": False,
            "dev_invocations": [],
        }

    is_reviewer = (not invoked_any_subagent) and (
        (saw_branch_diff and saw_record_review) or (saw_reviewer_persona and (saw_branch_diff or saw_record_review))
    )
    return {
        "step_count": step_count,
        "is_reviewer": is_reviewer,
        "run_command_count": run_command_count,
        "reran_tests_or_precommit": bool(is_reviewer and reran_tests_or_precommit),
        "dev_invocations": dev_invocations,
    }


def _detect_telemetry_efficiency_anomalies(
    brain_dir: Path | str | Sequence[Path | str] | None,
    *,
    orchestrator_cid: str = "",
    max_recent_transcripts: int = 25,
) -> tuple[list[dict[str, Any]], int]:
    """Read-only bounded scan of recent `transcript.jsonl` files for efficiency anomalies."""
    if brain_dir is None:
        return ([], 0)
    brain_roots = dev_common.resolve_antigravity_brain_dirs(brain_dir)
    if not brain_roots:
        return ([], 0)

    by_cid: dict[str, tuple[float, Path]] = {}
    for b_root in brain_roots:
        if not b_root.is_dir():
            continue
        with contextlib.suppress(OSError):
            for entry in b_root.iterdir():
                if not entry.is_dir():
                    continue
                tpath = entry / ".system_generated" / "logs" / "transcript.jsonl"
                if tpath.is_file():
                    with contextlib.suppress(OSError):
                        mt = float(tpath.stat().st_mtime)
                        prev = by_cid.get(entry.name)
                        if prev is None or mt > prev[0]:
                            by_cid[entry.name] = (mt, tpath)

    candidates: list[tuple[float, str, Path]] = [
        (mt, cid, tpath) for cid, (mt, tpath) in by_cid.items()
    ]
    if not candidates:
        return ([], 0)

    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = candidates[: max(1, int(max_recent_transcripts))]
    selected_cids = {cid for _, cid, _ in selected}

    orch_clean = str(orchestrator_cid or "").strip()
    if orch_clean and orch_clean not in selected_cids:
        orch_tpath = dev_common.resolve_transcript_path(orch_clean, brain_roots)
        if orch_tpath is not None:
            selected.append((0.0, orch_clean, orch_tpath))

    findings: list[dict[str, Any]] = []
    reviewer_runs: list[dict[str, Any]] = []
    dev_models: list[str] = []
    orch_steps = 0

    for _, cid, tpath in selected:
        metrics = _inspect_single_transcript(tpath)
        if orch_clean and cid.lower() == orch_clean.lower():
            orch_steps = int(metrics["step_count"])
        if metrics["is_reviewer"]:
            reviewer_runs.append(
                {
                    "cid": cid,
                    "run_commands": int(metrics["run_command_count"]),
                    "reran_tests_or_precommit": bool(metrics["reran_tests_or_precommit"]),
                }
            )
        dev_models.extend(metrics["dev_invocations"])

    max_orch_steps = resolve_max_orchestrator_steps()
    hard_orch_steps = max_orch_steps + 10
    if orch_clean and orch_steps >= max_orch_steps:
        is_urgent = orch_steps >= hard_orch_steps
        findings.append(
            _make_finding(
                code="bloated_orchestrator_session",
                severity="high" if is_urgent else "medium",
                category="telemetry",
                title=(
                    f"Orchestrator session {orch_clean[:8]} has {orch_steps} steps "
                    f"(>={hard_orch_steps if is_urgent else max_orch_steps}; auto-handoff window {max_orch_steps}-{hard_orch_steps})"
                ),
                evidence={
                    "orchestrator_cid": orch_clean,
                    "step_count": orch_steps,
                    "threshold": max_orch_steps,
                    "hard_threshold": hard_orch_steps,
                    "auto_handoff_recommended": True,
                    "rotation_urgency": "urgent" if is_urgent else "proactive",
                },
                recommendation=(
                    f"Proactively auto-handoff and rotate to a fresh `agy-dev-orchestrator` conversation "
                    f"(every {max_orch_steps}-{hard_orch_steps} steps or 4-6 PRs; configurable via "
                    "`AGY_DEV_MAX_ORCHESTRATOR_STEPS`) and register it via "
                    "`pr_remediator.py queue-next --unlocked --orchestrator-id <NEW_CID>` to prevent context bloat."
                ),
                auto_action_hint="Auto-handoff to a fresh agy-dev-orchestrator conversation and transfer orchestrator ID.",
                estimated_savings_min=20 if is_urgent else 15,
            )
        )

    if reviewer_runs:
        avg_cmds = sum(int(r["run_commands"]) for r in reviewer_runs) / len(reviewer_runs)
        redundant_cnt = sum(1 for r in reviewer_runs if r["reran_tests_or_precommit"])
        if avg_cmds >= 10.0 or redundant_cnt > 0:
            findings.append(
                _make_finding(
                    code="heavy_reviewer_overhead",
                    severity="medium",
                    category="telemetry",
                    title=(
                        f"Heavy reviewer overhead: avg {avg_cmds:.1f} run_command calls across "
                        f"{len(reviewer_runs)} review(s) ({redundant_cnt} redundant test/pre-commit reruns)"
                    ),
                    evidence={
                        "reviewer_runs": len(reviewer_runs),
                        "avg_run_commands": round(avg_cmds, 1),
                        "redundant_test_reruns": redundant_cnt,
                        "sample_cids": [str(r["cid"]) for r in reviewer_runs[:5]],
                    },
                    recommendation=(
                        "Follow the Lean Reviewer Protocol (target 3-5 turns): inspect `branch-diff` `verification_gate` "
                        "first and skip redundant `pre-commit` / `unittest` reruns when `skip_redundant_test_rerun == true`."
                    ),
                    auto_action_hint="Ensure agy-code-pr-reviewer checks verification_gate.skip_redundant_test_rerun first.",
                    estimated_savings_min=max(6, int(round(avg_cmds)) * len(reviewer_runs)),
                )
            )

    dev_total = len(dev_models)
    flash_cnt = sum(1 for m in dev_models if m == "flash")
    if dev_total >= 5 and flash_cnt == 0:
        findings.append(
            _make_finding(
                code="missing_flash_routing",
                severity="low",
                category="telemetry",
                title=f"0% flash routing across {dev_total} recent agy-code-developer dispatches",
                evidence={
                    "developer_invocations": dev_total,
                    "flash_invocations": 0,
                    "flash_ratio_pct": 0.0,
                },
                recommendation=(
                    "Route pre-anchored focused tasks (<=5 files, UI polish batches, doc/config updates) to "
                    "`agy-code-developer` with `Model='flash'`, reserving `Model='pro'` for complex refactors and PR reviews."
                ),
                auto_action_hint="Pass Model='flash' on focused pre-anchored developer subagent dispatches.",
                estimated_savings_min=dev_total * 2,
            )
        )

    return (findings, len(selected))


def _discover_managed_repo_dirs(
    all_locks: dict[str, dict[str, Any]],
    *,
    repos_dir: Path | str | None = None,
    allow_ambient_cwd: bool = False,
) -> dict[str, Path]:
    """Discover deduplicated local git repository roots from explicit `repos_dir` and recorded locks."""
    return dev_common.discover_managed_repo_dirs(
        all_locks,
        repos_dir=repos_dir,
        allow_ambient_cwd=allow_ambient_cwd,
    )


def _load_state_from_dir(state_dir: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read `dev_queue.json` and `dev_pr_locks` from `state_dir` without side effects."""
    queue_data = dev_common.load_json_dict(state_dir / "dev_queue.json", strict=False)
    if not queue_data:
        queue_data = {"items": [], "orchestrator_conversation_id": ""}
    active_locks, all_locks = dev_common.load_dev_locks_from_dir(
        state_dir,
        strict=False,
        normalize_slots=False,
    )
    return (queue_data, active_locks, all_locks)


def format_compact_opt_hint(analysis: dict[str, Any], max_width: int = 120) -> str:
    """Render a compact 1-line `OPT:` footer hint from critical/high findings (empty when healthy)."""
    findings = [
        f
        for f in (analysis.get("findings") or [])
        if isinstance(f, dict) and str(f.get("severity") or "").lower() in ("critical", "high")
    ]
    if not findings:
        return ""
    bits: list[str] = []
    for f in findings[:2]:
        code = str(f.get("code") or "")
        ev = f.get("evidence") if isinstance(f.get("evidence"), dict) else {}
        if code == "stalled_worker":
            repo_short = str(ev.get("repo_slug") or "").split("/")[-1]
            bits.append(f"stall {repo_short} ({ev.get('idle_label', '')})")
        elif code == "phantom_pr_open":
            bits.append(f"phantom PR ({ev.get('count', 1)})")
        elif code == "untracked_active_lock":
            bits.append(f"untracked lock ({ev.get('count', 1)})")
        elif code == "idle_orchestrator_with_runnable_work":
            bits.append(f"orch idle {ev.get('idle_label', '')} ({ev.get('runnable_count', 1)} ready)")
        elif code == "micro_pr_batch_candidates":
            repo_short = str(ev.get("repo_slug") or "").split("/")[-1]
            bits.append(f"batch {ev.get('count', 2)} PRs in {repo_short}")
        elif code == "pr_compaction_candidates":
            bits.append(f"compact -{ev.get('total_prs_saved', 1)} PRs ({ev.get('plan_count', 1)} grp)")
        elif code == "repo_contention_hotspot":
            h_repos = ev.get("hotspot_repos") or []
            if h_repos and isinstance(h_repos[0], dict):
                r_short = str(h_repos[0].get("repo_slug") or "").split("/")[-1]
                bits.append(f"hotspot {r_short} ({h_repos[0].get('count')}/{ev.get('open_count')})")
            else:
                bits.append(f"multi-repo contention ({ev.get('multi_repo_count')}/{ev.get('open_count')})")
        elif code == "missing_selective_test_runner":
            cnt = len(ev.get("repos") or [])
            bits.append(f"selective tests missing ({cnt} repo)")
        elif code == "priority_inversion":
            bits.append(f"prio inversion ({ev.get('count', 1)})")
        elif code == "partial_wip_accumulation":
            r_short = str(ev.get("top_repo_slug") or "").split("/")[-1]
            bits.append(f"partial WIP {r_short} ({ev.get('top_count', DEFAULT_MAX_PARTIAL_WIP)})")
        elif code == "stale_follower_needs_rebase":
            r_short = str(ev.get("top_repo_slug") or "").split("/")[-1]
            bits.append(f"rebase-reverify {r_short} ({ev.get('count', 1)})")
        elif code == "parallel_lookahead_opportunity":
            r_short = str(ev.get("top_repo_slug") or "").split("/")[-1]
            free_sid = int(ev.get("free_slot_id") or 2)
            bits.append(f"slot{free_sid} lookahead {r_short} ({ev.get('count', 1)})")
        elif code == "standby_dependency_deadlock":
            bits.append(f"dep deadlock ({ev.get('count', 1)}, {ev.get('free_repo_count', 0)} free repo)")
        else:
            bits.append(str(f.get("title") or code))

    extra = f" +{len(findings) - 2}" if len(findings) > 2 else ""
    text = f"OPT: {' · '.join(bits)}{extra} — run queue-optimize"
    if max_width > 4 and len(text) > max_width:
        return text[: max_width - 1] + "…"
    return text


def analyze_dev_flow_optimizations(
    *,
    portfolio_dir: Path | str | None = None,
    repos_dir: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    orchestrator_cid: str = "",
    stall_threshold_seconds: int | float | None = None,
    god_file_line_threshold: int = 5000,
    max_recent_transcripts: int = 25,
    now_ts: float | None = None,
    lightweight: bool = False,
    preloaded_items: list[dict[str, Any]] | None = None,
    preloaded_active_locks: dict[str, dict[str, Any]] | None = None,
    preloaded_all_locks: dict[str, dict[str, Any]] | None = None,
    preloaded_repo_dirs: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Run all 7 deterministic 0-LLM flow optimization checks and return structured findings + health_score."""
    if preloaded_items is not None and preloaded_active_locks is not None:
        items = list(preloaded_items)
        active_locks = dict(preloaded_active_locks)
        all_locks = dict(preloaded_all_locks if preloaded_all_locks is not None else preloaded_active_locks)
        queue_orch_cid = ""
    else:
        state_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
        queue_data, active_locks, all_locks = _load_state_from_dir(state_dir)
        items = [it for it in (queue_data.get("items") or []) if isinstance(it, dict)]
        queue_orch_cid = str(queue_data.get("orchestrator_conversation_id") or "").strip()

    eff_orch_cid = str(orchestrator_cid or "").strip() or queue_orch_cid

    # Hermetic brain_dir resolution: never touch real ~/.gemini/antigravity*/brain when caller isolated portfolio_dir
    if brain_dir is not None:
        resolved_brain_dirs = dev_common.resolve_antigravity_brain_dirs(brain_dir)
    elif portfolio_dir is None and not lightweight:
        resolved_brain_dirs = dev_common.resolve_antigravity_brain_dirs(None)
    else:
        resolved_brain_dirs = []

    if preloaded_repo_dirs is not None:
        repo_dirs = dict(preloaded_repo_dirs)
    else:
        repo_dirs = _discover_managed_repo_dirs(
            all_locks,
            repos_dir=repos_dir,
            allow_ambient_cwd=(portfolio_dir is None and not lightweight),
        )

    open_items = [it for it in items if str(it.get("status") or "pending").lower() in OPEN_QUEUE_STATUSES]

    findings: list[dict[str, Any]] = []
    # 1. Stalled workers
    findings.extend(
        _detect_stalled_workers(
            active_locks,
            now_ts=now_ts,
            threshold_seconds=stall_threshold_seconds,
            brain_dir=resolved_brain_dirs,
        )
    )
    # 1b. Phantom pr_open items (0 active locks & 0 pr_urls), untracked ad-hoc active locks,
    #     priority inversions, downstream partial-WIP accumulation, stale follower rebase, Slot #2..#N lookahead,
    #     and standby/cancelled/rejected dependency deadlocks
    findings.extend(_detect_phantom_pr_open(items, active_locks))
    findings.extend(_detect_untracked_active_locks(items, active_locks))
    findings.extend(_detect_priority_inversions(items))
    findings.extend(_detect_partial_wip_accumulation(items))
    findings.extend(_detect_stale_follower_needs_rebase(active_locks))
    findings.extend(_detect_parallel_lookahead_opportunity(items, active_locks, repo_dirs))
    findings.extend(_detect_standby_dependency_deadlock(items, active_locks))
    # 2. Micro-PR batch candidates + 0-LLM structural PR compaction candidates
    findings.extend(_detect_micro_pr_batch_candidates(items))
    try:
        from queue_compactor import (
            build_compaction_optimizer_finding,
            detect_pr_compaction_candidates,
        )

        comp_det = detect_pr_compaction_candidates(
            items,
            active_locks=active_locks,
            repo_dirs=repo_dirs or None,
            tier="all",
        )
        comp_finding = build_compaction_optimizer_finding(comp_det)
        if comp_finding is not None:
            findings.append(comp_finding)
    except Exception:
        pass
    # 3. Repo contention hotspots
    findings.extend(_detect_repo_contention_hotspots(items))
    # 4. Unanchored pending tasks
    findings.extend(_detect_unanchored_pending_tasks(items))

    scanned_transcripts = 0
    # 6. Missing selective test runner (fast: single small yaml read per repo)
    if repo_dirs:
        findings.extend(_detect_missing_selective_test_runners(repo_dirs))

    if not lightweight:
        # 5. Monolithic god files
        if repo_dirs:
            findings.extend(
                _detect_monolithic_god_files(
                    repo_dirs,
                    open_items,
                    line_threshold=max(1, int(god_file_line_threshold)),
                )
            )
        # 7. Telemetry efficiency anomalies + idle orchestrator with runnable work
        tel_findings, scanned_transcripts = _detect_telemetry_efficiency_anomalies(
            resolved_brain_dirs,
            orchestrator_cid=eff_orch_cid,
            max_recent_transcripts=max_recent_transcripts,
        )
        findings.extend(tel_findings)
        findings.extend(
            _detect_idle_orchestrator_with_runnable_work(
                items,
                active_locks,
                resolved_brain_dirs,
                orchestrator_cid=eff_orch_cid,
                now_ts=now_ts,
                threshold_seconds=stall_threshold_seconds,
            )
        )

    findings.sort(
        key=lambda f: (
            SEVERITY_RANK.get(str(f.get("severity") or "low"), 9),
            -int(f.get("estimated_savings_min") or 0),
            str(f.get("code") or ""),
        )
    )

    sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    penalty = 0
    total_savings = 0
    for f in findings:
        sev = str(f.get("severity") or "low").lower()
        if sev in sev_counts:
            sev_counts[sev] += 1
        penalty += SEVERITY_PENALTY.get(sev, 4)
        total_savings += int(f.get("estimated_savings_min") or 0)

    health_score = max(0, min(100, 100 - penalty))
    result: dict[str, Any] = {
        "status": "ok",
        "generated_at": _utc_now_iso(),
        "health_score": health_score,
        "summary": {
            "total_findings": len(findings),
            **sev_counts,
            "estimated_total_savings_min": total_savings,
            "open_queue_items": len(open_items),
            "active_locks": len(active_locks),
            "scanned_repos": len(repo_dirs),
            "scanned_transcripts": scanned_transcripts,
        },
        "findings": findings,
    }
    result["top_hint"] = format_compact_opt_hint(result)
    return result


analyze_dev_flow = analyze_dev_flow_optimizations
