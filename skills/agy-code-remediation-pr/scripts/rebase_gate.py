#!/usr/bin/env python3
"""Deterministic 0-LLM Native Git Rebase & Cross-Impact Re-Verification Gate (`rebase_gate.py`).

Pure Python 3 standard-library module using native `/usr/bin/git` (`shell=False`, Zero-PII) providing:
  1. `detect_base_sha_drift`: compares a slot's `base_sha` / `merge-base` against `origin/main` / `main`.
  2. `probe_merge_tree_conflicts`: in-memory `git merge-tree --write-tree` dry-run conflict probe
     (with safe graceful fallback to classic 3-way `git merge-tree` on older Git versions).
  3. `compute_cross_impact_files`: computes `files(B) ∪ files(merged_A)` so selective verification covers
     both the rebased follower Slot #2..#N branch (`B`) and every file modified by merged Slot #1 (`A`).
  4. `rebase_and_reverify`: executes dry-run conflict probe + `git rebase` + cross-impact verification,
     returning deterministic outcomes:
       - `AUTO_PROMOTED_ZERO_LLM`
       - `NEEDS_AGENT_SEMANTIC_FIX`
       - `NEEDS_AGENT_CONFLICT_RESOLUTION` (with clean `git rebase --abort` leaving worktree pristine).
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import contextlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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

DEFAULT_REBASE_VERIFY_TIMEOUT_SECONDS: float = 480.0
ENV_REBASE_VERIFY_TIMEOUT: str = "AGY_DEV_REBASE_VERIFY_TIMEOUT"

OUTCOME_AUTO_PROMOTED = "AUTO_PROMOTED_ZERO_LLM"
OUTCOME_NEEDS_SEMANTIC_FIX = "NEEDS_AGENT_SEMANTIC_FIX"
OUTCOME_NEEDS_CONFLICT_RESOLUTION = "NEEDS_AGENT_CONFLICT_RESOLUTION"

REBASE_OUTCOMES: tuple[str, ...] = (
    OUTCOME_AUTO_PROMOTED,
    OUTCOME_NEEDS_SEMANTIC_FIX,
    OUTCOME_NEEDS_CONFLICT_RESOLUTION,
)

OUTCOME_SPECULATIVE_READY = "SPECULATIVE_PRE_REBASE_READY"
OUTCOME_SPECULATIVE_CONFLICT = "SPECULATIVE_CONFLICT_DETECTED"
OUTCOME_SPECULATIVE_SEMANTIC_FAILURE = "SPECULATIVE_SEMANTIC_FAILURE"
OUTCOME_SPECULATIVE_SKIPPED = "SPECULATIVE_SKIPPED"
SPECULATIVE_REF_PREFIX = "refs/agy-speculative/"

OUTCOME_TRAIN_BUNDLED = "RELEASE_TRAIN_BUNDLED"
OUTCOME_TRAIN_DRY_RUN_READY = "RELEASE_TRAIN_DRY_RUN_READY"
OUTCOME_TRAIN_ROLLED_BACK_CONFLICT = "RELEASE_TRAIN_ROLLED_BACK_CONFLICT"
OUTCOME_TRAIN_ROLLED_BACK_VERIFICATION = "RELEASE_TRAIN_ROLLED_BACK_VERIFICATION"
OUTCOME_TRAIN_NOT_ELIGIBLE = "RELEASE_TRAIN_NOT_ELIGIBLE"
DEFAULT_TRAIN_MIN_SLOTS = 2
DEFAULT_TRAIN_MAX_SLOTS = 3

MAX_CAPTURED_DIFF_CHARS = 24000
_GIT_HOOK_ENV_VARS: frozenset[str] = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
    }
)
_LOCAL_HOME_PATH_RE = re.compile(r"/(?:Users|home)/(?!testuser\b|test\b|Shared\b)[A-Za-z0-9._-]+")
_CONFLICT_IN_MSG_RE = re.compile(
    r"CONFLICT\s*\([^)]+\):\s*(?:Merge conflict in\s+(.+?)|(.+?)\s+deleted in\s+.+)$"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_path(text: str) -> str:
    """Replace local workstation home paths with `$HOME` to preserve Zero-PII outputs."""
    if not text:
        return ""
    cleaned = str(text)
    with contextlib.suppress(OSError):
        home_str = str(Path.home().resolve())
        if home_str and home_str != "/":
            cleaned = cleaned.replace(home_str, "$HOME")
    return _LOCAL_HOME_PATH_RE.sub("$HOME", cleaned)


def _clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Strip inherited outer git hook `GIT_*` overrides so subprocesses stay hermetic."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_HOOK_ENV_VARS}
    if extra:
        env.update(extra)
    return env


def _git_bin() -> str:
    return shutil.which("git") or "/usr/bin/git"


def _run_git(
    repo_dir: Path,
    args: Sequence[str],
    *,
    timeout: float = 25.0,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute native `git -C <repo_dir> ...` with `shell=False` and sanitized hook env."""
    return subprocess.run(
        [_git_bin(), "-C", str(repo_dir), *args],
        env=_clean_git_env(extra_env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _is_local_git_remote(repo_dir: Path, remote: str = "origin") -> bool:
    """Return True when `<remote>` points to a local filesystem or `file://` repository."""
    proc = _run_git(repo_dir, ["remote", "get-url", remote], timeout=5.0)
    if proc.returncode != 0:
        return False
    raw = (proc.stdout or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if low.startswith("file://"):
        return True
    if low.startswith(("ssh://", "http://", "https://", "git://", "git@")):
        return False
    if "://" in raw or re.match(r"^[A-Za-z0-9_.-]+@[^/:]+:", raw):
        return False
    cand = Path(raw).expanduser()
    return cand.is_absolute() or cand.exists() or (repo_dir / raw).exists()


def compute_cross_impact_files(
    branch_b_files: Sequence[str],
    merged_a_files: Sequence[str],
) -> list[str]:
    """Compute normalized sorted union `files(B) ∪ files(merged_A)`."""
    merged: set[str] = set()
    for raw in (*branch_b_files, *merged_a_files):
        norm = str(raw or "").strip().replace("\\", "/").lstrip("./")
        if norm:
            merged.add(norm)
    return sorted(merged)


@dataclass(frozen=True)
class BaseDriftResult:
    """Structured result of comparing a slot branch's base SHA against `origin/main` / `main`."""

    status: str
    drifted: bool
    recorded_base_sha: str
    merge_base_sha: str
    current_base_sha: str
    base_ref: str
    branch_name: str
    branch_head_sha: str
    ahead_count: int
    behind_count: int
    branch_b_files: tuple[str, ...] = ()
    merged_a_files: tuple[str, ...] = ()
    union_files: tuple[str, ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize drift result to JSON-friendly dict."""
        data = asdict(self)
        data["branch_b_files"] = list(self.branch_b_files)
        data["merged_a_files"] = list(self.merged_a_files)
        data["union_files"] = list(self.union_files)
        return data


@dataclass(frozen=True)
class MergeTreeProbeResult:
    """In-memory `git merge-tree` dry-run conflict probe result (zero worktree mutation)."""

    clean: bool
    supported_write_tree: bool
    tree_sha: str = ""
    conflicted_files: tuple[str, ...] = ()
    raw_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize merge-tree probe result to JSON-friendly dict."""
        return {
            "clean": self.clean,
            "supported_write_tree": self.supported_write_tree,
            "tree_sha": self.tree_sha,
            "conflicted_files": list(self.conflicted_files),
            "raw_output": self.raw_output,
        }


@dataclass(frozen=True)
class RebaseReverifyResult:
    """Structured deterministic result returned by `rebase_and_reverify`."""

    status: str
    outcome: str
    rebased: bool
    drifted: bool
    aborted_cleanly: bool
    worktree_pristine: bool
    branch_name: str
    workdir: str
    previous_head_sha: str
    new_head_sha: str
    previous_base_sha: str
    new_base_sha: str
    branch_b_files: tuple[str, ...] = ()
    merged_a_files: tuple[str, ...] = ()
    union_files: tuple[str, ...] = ()
    conflicted_files: tuple[str, ...] = ()
    merged_a_commits: tuple[str, ...] = ()
    merged_a_diff: str = ""
    verification_passed: bool = False
    verification_results: dict[str, Any] = field(default_factory=dict)
    failure_details: str = ""
    merge_tree_probe: dict[str, Any] = field(default_factory=dict)
    speculative_cache_hit: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize rebase-reverify result to JSON-friendly dict."""
        data = asdict(self)
        data["branch_b_files"] = list(self.branch_b_files)
        data["merged_a_files"] = list(self.merged_a_files)
        data["union_files"] = list(self.union_files)
        data["conflicted_files"] = list(self.conflicted_files)
        data["merged_a_commits"] = list(self.merged_a_commits)
        return data


@dataclass(frozen=True)
class SpeculativePreRebaseResult:
    """Structured 0-LLM speculative pre-rebase result over frozen Slot #1 HEAD."""

    status: str
    outcome: str
    rebased: bool
    clean: bool
    aborted_cleanly: bool
    worktree_pristine: bool
    follower_branch: str
    slot1_ref: str
    slot1_head_sha: str
    slot1_tree_sha: str
    follower_head_sha: str
    speculative_head_sha: str = ""
    speculative_tree_sha: str = ""
    speculative_ref: str = ""
    union_files: tuple[str, ...] = ()
    conflicted_files: tuple[str, ...] = ()
    verification_passed: bool = False
    verification_results: dict[str, Any] = field(default_factory=dict)
    failure_details: str = ""
    merge_tree_probe: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize speculative pre-rebase result to JSON-friendly dict."""
        data = asdict(self)
        data["union_files"] = list(self.union_files)
        data["conflicted_files"] = list(self.conflicted_files)
        return data


@dataclass(frozen=True)
class ReleaseTrainBundleResult:
    """Structured 0-LLM Release Train (`bundle-ready-slots`) integration result with atomic rollback telemetry."""

    status: str
    outcome: str
    bundled: bool
    rolled_back: bool
    worktree_pristine: bool
    train_branch: str
    train_worktree: str
    base_ref: str
    base_sha: str
    train_head_sha: str = ""
    included_branches: tuple[str, ...] = ()
    included_slot_ids: tuple[int, ...] = ()
    included_conversation_ids: tuple[str, ...] = ()
    union_files: tuple[str, ...] = ()
    conflicted_branch: str = ""
    conflicted_files: tuple[str, ...] = ()
    verification_passed: bool = False
    verification_results: dict[str, Any] = field(default_factory=dict)
    failure_details: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize Release Train bundle result to JSON-friendly dict."""
        data = asdict(self)
        data["included_branches"] = list(self.included_branches)
        data["included_slot_ids"] = list(self.included_slot_ids)
        data["included_conversation_ids"] = list(self.included_conversation_ids)
        data["union_files"] = list(self.union_files)
        data["conflicted_files"] = list(self.conflicted_files)
        return data


def _resolve_base_ref_and_sha(
    repo_dir: Path,
    base_branch: str = "main",
    remote: str = "origin",
) -> tuple[str, str]:
    """Resolve `('origin/main', <sha>)` or fallback `('main', <sha>)`."""
    clean_base = (base_branch or "main").strip()
    for cand in (f"{remote}/{clean_base}", clean_base):
        proc = _run_git(repo_dir, ["rev-parse", "--verify", cand], timeout=5.0)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            return cand, (proc.stdout or "").strip()
    return "", ""


def _resolve_branch_head_sha(repo_dir: Path, branch_name: str = "") -> tuple[str, str]:
    """Resolve `(branch_label, head_sha)` inside `repo_dir`."""
    clean_br = (branch_name or "").strip().removeprefix("refs/heads/")
    candidates = [clean_br, f"refs/heads/{clean_br}"] if clean_br else ["HEAD"]
    for cand in candidates:
        proc = _run_git(repo_dir, ["rev-parse", "--verify", cand], timeout=5.0)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            label = clean_br
            if not label:
                ab = _run_git(repo_dir, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5.0)
                label = (ab.stdout or "").strip() if ab.returncode == 0 else "HEAD"
            return label, (proc.stdout or "").strip()
    return clean_br or "HEAD", ""


def _git_diff_names(repo_dir: Path, rev_range: str) -> list[str]:
    """Return sorted changed file paths for `rev_range` (`a..b` or `a...b`)."""
    proc = _run_git(repo_dir, ["diff", "--name-only", rev_range], timeout=10.0)
    if proc.returncode != 0 or not proc.stdout:
        return []
    return sorted(
        {
            ln.strip().replace("\\", "/").lstrip("./")
            for ln in proc.stdout.splitlines()
            if ln.strip()
        }
    )


def detect_base_sha_drift(
    repo_dir: Path | str,
    *,
    branch_name: str = "",
    recorded_base_sha: str = "",
    base_branch: str = "main",
    remote: str = "origin",
    fetch: bool = False,
) -> BaseDriftResult:
    """Inspect whether `branch_name` (or `HEAD`) has drifted behind `origin/<base_branch>` / `<base_branch>`."""
    r_dir = Path(str(repo_dir).replace("$HOME", str(Path.home()))).expanduser().resolve()
    if not (r_dir / ".git").exists():
        return BaseDriftResult(
            status="error",
            drifted=False,
            recorded_base_sha=recorded_base_sha,
            merge_base_sha="",
            current_base_sha="",
            base_ref="",
            branch_name=branch_name,
            branch_head_sha="",
            ahead_count=0,
            behind_count=0,
            error=f"Git repository not found at {_sanitize_path(str(r_dir))}",
        )

    is_offline = os.environ.get("AGY_OFFLINE_ONLY") == "1" or os.environ.get("PRE_COMMIT_HOOK") == "1"
    if fetch and (not is_offline or _is_local_git_remote(r_dir, remote)):
        _run_git(r_dir, ["fetch", remote, "--prune", "--quiet"], timeout=25.0)

    base_ref, current_base_sha = _resolve_base_ref_and_sha(r_dir, base_branch=base_branch, remote=remote)
    resolved_br, branch_head_sha = _resolve_branch_head_sha(r_dir, branch_name=branch_name)
    if not current_base_sha or not branch_head_sha:
        return BaseDriftResult(
            status="error",
            drifted=False,
            recorded_base_sha=recorded_base_sha,
            merge_base_sha="",
            current_base_sha=current_base_sha,
            base_ref=base_ref,
            branch_name=resolved_br,
            branch_head_sha=branch_head_sha,
            ahead_count=0,
            behind_count=0,
            error="Could not resolve base SHA or branch HEAD SHA.",
        )

    mb_proc = _run_git(r_dir, ["merge-base", current_base_sha, branch_head_sha], timeout=5.0)
    merge_base_sha = (mb_proc.stdout or "").strip() if mb_proc.returncode == 0 else ""

    ahead_cnt = 0
    behind_cnt = 0
    if merge_base_sha:
        p_ahead = _run_git(r_dir, ["rev-list", "--count", f"{merge_base_sha}..{branch_head_sha}"], timeout=5.0)
        if p_ahead.returncode == 0:
            with contextlib.suppress(ValueError):
                ahead_cnt = int((p_ahead.stdout or "0").strip())
        p_behind = _run_git(r_dir, ["rev-list", "--count", f"{merge_base_sha}..{current_base_sha}"], timeout=5.0)
        if p_behind.returncode == 0:
            with contextlib.suppress(ValueError):
                behind_cnt = int((p_behind.stdout or "0").strip())

    rec_clean = (recorded_base_sha or "").strip()
    drifted = bool(behind_cnt > 0 or (rec_clean and rec_clean != current_base_sha))

    a_start = rec_clean if (rec_clean and merge_base_sha and rec_clean == merge_base_sha) else (merge_base_sha or rec_clean)
    if rec_clean and rec_clean != current_base_sha:
        chk_rec = _run_git(r_dir, ["cat-file", "-e", f"{rec_clean}^{{commit}}"], timeout=5.0)
        if chk_rec.returncode == 0:
            a_start = rec_clean

    merged_a_files = _git_diff_names(r_dir, f"{a_start}..{current_base_sha}") if a_start else []
    b_start = merge_base_sha or current_base_sha
    branch_b_files = _git_diff_names(r_dir, f"{b_start}..{branch_head_sha}")
    union_files = compute_cross_impact_files(branch_b_files, merged_a_files)

    return BaseDriftResult(
        status="ok",
        drifted=drifted,
        recorded_base_sha=rec_clean,
        merge_base_sha=merge_base_sha,
        current_base_sha=current_base_sha,
        base_ref=base_ref,
        branch_name=resolved_br,
        branch_head_sha=branch_head_sha,
        ahead_count=ahead_cnt,
        behind_count=behind_cnt,
        branch_b_files=tuple(branch_b_files),
        merged_a_files=tuple(merged_a_files),
        union_files=tuple(union_files),
    )


def _parse_write_tree_conflicts(output: str) -> list[str]:
    """Extract conflicted file paths from `git merge-tree --write-tree` stdout/stderr."""
    files: set[str] = set()
    for raw_ln in (output or "").splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if "\t" in ln:
            meta, path_part = ln.split("\t", 1)
            parts = meta.split()
            if len(parts) == 3 and parts[2] in ("1", "2", "3") and path_part.strip():
                files.add(path_part.strip().strip('"'))
                continue
        m_conf = _CONFLICT_IN_MSG_RE.search(ln)
        if m_conf:
            cand = (m_conf.group(1) or m_conf.group(2) or "").strip().strip('"')
            if cand:
                files.add(cand)
    return sorted(files)


def _parse_classic_merge_tree_conflicts(output: str) -> tuple[bool, list[str]]:
    """Parse classic 3-way `git merge-tree <base> <branch1> <branch2>` output as fallback."""
    text = output or ""
    has_markers = ("+<<<<<<<" in text) or ("\n<<<<<<<" in text) or ("changed in both" in text)
    if not has_markers:
        return False, []

    files: set[str] = set()
    blocks = re.split(r"(?m)^(changed in both|added in both|removed in \w+)\s*$", text)
    for chunk in blocks:
        for m_path in re.finditer(r"(?m)^\s+(?:base|our|their)\s+\d+\s+[0-9a-f]{40}\s+(.+)$", chunk):
            fpath = m_path.group(1).strip()
            if fpath:
                files.add(fpath)
    return True, sorted(files)


def probe_merge_tree_conflicts(
    repo_dir: Path | str,
    base_sha: str,
    branch_sha: str,
    *,
    merge_base_sha: str = "",
) -> MergeTreeProbeResult:
    """Run an in-memory `git merge-tree --write-tree` conflict probe with classic fallback."""
    r_dir = Path(str(repo_dir).replace("$HOME", str(Path.home()))).expanduser().resolve()
    proc = _run_git(
        r_dir,
        ["merge-tree", "--write-tree", "--messages", base_sha, branch_sha],
        timeout=15.0,
    )
    combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
    stderr_low = (proc.stderr or "").lower()
    unsupported = proc.returncode == 129 or (
        proc.returncode not in (0, 1)
        and ("unknown option" in stderr_low or "usage:" in stderr_low and "merge-tree" in stderr_low)
    )

    if not unsupported:
        if proc.returncode == 0:
            first_line = (proc.stdout or "").splitlines()[0].strip() if (proc.stdout or "").strip() else ""
            return MergeTreeProbeResult(
                clean=True,
                supported_write_tree=True,
                tree_sha=first_line,
                conflicted_files=(),
                raw_output=combined[:4000],
            )
        conflicted = _parse_write_tree_conflicts(combined)
        return MergeTreeProbeResult(
            clean=False,
            supported_write_tree=True,
            tree_sha="",
            conflicted_files=tuple(conflicted),
            raw_output=combined[:4000],
        )

    # Classic 3-way fallback: `git merge-tree <merge-base> <base_sha> <branch_sha>`
    mb = (merge_base_sha or "").strip()
    if not mb:
        mb_proc = _run_git(r_dir, ["merge-base", base_sha, branch_sha], timeout=5.0)
        mb = (mb_proc.stdout or "").strip() if mb_proc.returncode == 0 else base_sha
    classic = _run_git(r_dir, ["merge-tree", mb, base_sha, branch_sha], timeout=15.0)
    classic_out = f"{classic.stdout or ''}\n{classic.stderr or ''}".strip()
    has_conf, conf_files = _parse_classic_merge_tree_conflicts(classic_out)
    return MergeTreeProbeResult(
        clean=not has_conf,
        supported_write_tree=False,
        tree_sha="",
        conflicted_files=tuple(conf_files),
        raw_output=classic_out[:4000],
    )


def _find_worktree_for_branch(repo_dir: Path, branch_name: str) -> Path | None:
    """Return the linked worktree path where `branch_name` is checked out, if any."""
    clean_br = (branch_name or "").strip().removeprefix("refs/heads/")
    if not clean_br:
        return None
    proc = _run_git(repo_dir, ["worktree", "list", "--porcelain"], timeout=10.0)
    if proc.returncode != 0 or not proc.stdout:
        return None
    target_ref = f"refs/heads/{clean_br}"
    cur_wt = ""
    for raw_ln in proc.stdout.splitlines():
        ln = raw_ln.strip()
        if not ln:
            cur_wt = ""
        elif ln.startswith("worktree "):
            cur_wt = ln[len("worktree ") :].strip()
        elif ln.startswith("branch ") and cur_wt:
            if ln[len("branch ") :].strip() == target_ref:
                p = Path(cur_wt)
                if p.is_dir():
                    return p.resolve()
    return None


def _is_rebase_in_progress(worktree_dir: Path) -> bool:
    """Return True if Git has an unfinished `rebase-merge` or `rebase-apply` state directory."""
    for rel_git_path in ("rebase-merge", "rebase-apply"):
        proc = _run_git(worktree_dir, ["rev-parse", "--git-path", rel_git_path], timeout=5.0)
        if proc.returncode == 0 and (proc.stdout or "").strip():
            raw_p = Path((proc.stdout or "").strip())
            abs_p = raw_p if raw_p.is_absolute() else (worktree_dir / raw_p)
            if abs_p.exists():
                return True
    return False


def _is_ephemeral_bytecode_entry(porcelain_line: str) -> bool:
    """Return True when an untracked `??` porcelain entry is purely Python bytecode/pytest cache."""
    if not porcelain_line.startswith("?? "):
        return False
    rel = porcelain_line[3:].strip().strip('"').replace("\\", "/")
    parts = [p for p in rel.split("/") if p]
    if any(p in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache") for p in parts):
        return True
    return rel.endswith((".pyc", ".pyo"))


def _is_worktree_clean(worktree_dir: Path) -> bool:
    """Return True when `git status --porcelain` has no tracked/untracked source changes and no rebase is active."""
    if _is_rebase_in_progress(worktree_dir):
        return False
    proc = _run_git(worktree_dir, ["status", "--porcelain"], timeout=10.0)
    if proc.returncode != 0:
        return False
    meaningful = [
        ln
        for ln in (proc.stdout or "").splitlines()
        if ln.strip() and not _is_ephemeral_bytecode_entry(ln)
    ]
    return not bool(meaningful)


def _snapshot_clean_fingerprint(worktree_dir: Path, head_sha: str) -> dict[str, Any]:
    """Build commit/tree/fingerprint metadata matching `pr_remediator._snapshot_worktree_state`."""
    t_proc = _run_git(worktree_dir, ["rev-parse", "--verify", f"{head_sha}^{{tree}}"], timeout=5.0)
    tree_sha = (t_proc.stdout or "").strip() if t_proc.returncode == 0 else ""
    fp = hashlib.sha256(tree_sha.encode("utf-8")).hexdigest()[:32] if tree_sha else ""
    return {
        "head_sha": head_sha,
        "tree_sha": tree_sha,
        "dirty": False,
        "content_fingerprint": fp,
    }


def _resolve_rebase_verify_timeout(timeout: float | None = None) -> float:
    """Resolve rebase verification subprocess timeout (explicit > `AGY_DEV_REBASE_VERIFY_TIMEOUT` > 480.0s)."""
    if timeout is not None:
        try:
            explicit_val = float(timeout)
            if explicit_val > 0:
                return explicit_val
        except (TypeError, ValueError):
            pass
    raw_env = str(os.environ.get(ENV_REBASE_VERIFY_TIMEOUT) or "").strip()
    if raw_env:
        try:
            env_val = float(raw_env)
            if env_val > 0:
                return env_val
        except (TypeError, ValueError):
            pass
    return DEFAULT_REBASE_VERIFY_TIMEOUT_SECONDS


def _coerce_subprocess_output(raw: Any) -> str:
    """Safely convert subprocess stdout/stderr (str, bytes, or None) to str."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _format_timeout_output(step_label: str, timeout_seconds: float, exc: subprocess.TimeoutExpired) -> str:
    """Build diagnostic timeout text preserving any partial stdout/stderr from `TimeoutExpired`."""
    partial = "\n".join(
        part
        for part in (
            _coerce_subprocess_output(getattr(exc, "stdout", None)).strip(),
            _coerce_subprocess_output(getattr(exc, "stderr", None)).strip(),
        )
        if part
    ).strip()
    header = f"TIMEOUT after {timeout_seconds:.1f}s in {step_label}"
    return f"{header}\n{partial}".strip() if partial else header


def _build_verification_env(worktree_dir: Path) -> dict[str, str]:
    """Build sanitized verification environment with repo `PYTHONPATH` entries."""
    clean_env = _clean_git_env(
        {
            "AGY_OFFLINE_ONLY": "1",
            "PRE_COMMIT_HOOK": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    pp_parts: list[str] = []
    for cand in (worktree_dir, worktree_dir / "clients", worktree_dir / "tests" / "scripts"):
        if cand.exists():
            resolved = str(cand)
            if resolved not in pp_parts:
                pp_parts.append(resolved)
    existing_pp = clean_env.get("PYTHONPATH", "")
    if existing_pp:
        for part in existing_pp.split(os.pathsep):
            if part and part not in pp_parts:
                pp_parts.append(part)
    if pp_parts:
        clean_env["PYTHONPATH"] = os.pathsep.join(pp_parts)
    return clean_env


def _load_repo_selective_runner(worktree_dir: Path) -> Any | None:
    """Dynamically load `tests/scripts/selective_test_runner.py` from `worktree_dir` if present."""
    runner_path = worktree_dir / "tests" / "scripts" / "selective_test_runner.py"
    if not runner_path.is_file():
        return None
    prev_no_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("_rebase_selective_runner", str(runner_path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except (Exception, SystemExit):
        return None
    finally:
        sys.dont_write_bytecode = prev_no_bytecode


def _extract_cli_plan_targets(plan_payload: dict[str, Any]) -> tuple[list[str], bool, bool, bool]:
    """Extract `(targets, run_all, skip_unit_tests, should_run_bats)` from selective plan JSON."""
    run_all = bool(plan_payload.get("run_all", False))
    skip_unit_tests = bool(plan_payload.get("skip_unit_tests", False))
    should_run_bats = bool(plan_payload.get("should_run_bats", False))
    unittest_targets = [
        str(item).strip() for item in (plan_payload.get("unittest_targets") or []) if str(item).strip()
    ]
    resolved_methods = [
        str(item).strip() for item in (plan_payload.get("resolved_test_methods") or []) if str(item).strip()
    ]
    targets = list(dict.fromkeys(unittest_targets))
    for method_target in resolved_methods:
        if method_target not in targets and not any(
            method_target == existing or method_target.startswith(f"{existing}.") for existing in targets
        ):
            targets.append(method_target)
    return targets, run_all, skip_unit_tests, should_run_bats


def _build_discover_command(worktree_dir: Path) -> list[str]:
    """Build `unittest discover` command when `tests/test_*.py` exist."""
    tests_dir = worktree_dir / "tests"
    if not (tests_dir.is_dir() and any(tests_dir.glob("test_*.py"))):
        return []
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
    if (tests_dir / "__init__.py").is_file():
        cmd.extend(["-t", "."])
    cmd.extend(["-p", "test_*.py"])
    return cmd


def _resolve_selective_execution_plan(
    worktree_dir: Path,
    union_files: Sequence[str],
    clean_env: dict[str, str],
    *,
    timeout: float,
) -> dict[str, Any]:
    """Resolve test targets across legacy `select_test_modules` and CLI/plan `selective_test_runner.py`."""
    discover_cmd = _build_discover_command(worktree_dir)
    runner_path = worktree_dir / "tests" / "scripts" / "selective_test_runner.py"
    runner_mod = _load_repo_selective_runner(worktree_dir)
    files_list = list(union_files)

    if runner_mod is not None and hasattr(runner_mod, "select_test_modules"):
        selected_modules = list(runner_mod.select_test_modules(files_list, run_all=not bool(files_list)))
        run_bats = (
            bool(runner_mod.should_run_bats(files_list, run_all=not bool(files_list)))
            if hasattr(runner_mod, "should_run_bats")
            else False
        )
        cmd = [sys.executable, "-m", "unittest", *selected_modules, "-v"] if selected_modules else discover_cmd
        return {
            "mode": "legacy_module",
            "selected_modules": selected_modules,
            "cmd": cmd,
            "discover_cmd": discover_cmd,
            "unittest_ran": bool(cmd),
            "run_bats": run_bats,
            "plan_timed_out": False,
            "plan_timeout_output": "",
        }

    if runner_path.is_file():
        probe_candidates: list[list[str]] = [
            [
                sys.executable,
                str(runner_path),
                *(["--changed-files", *files_list] if files_list else ["--all"]),
                "--dry-run",
                "--json",
            ],
            [
                sys.executable,
                str(runner_path),
                *(files_list if files_list else ["--all"]),
                "--plan-only",
                "--format",
                "json",
            ],
        ]
        for probe_cmd in probe_candidates:
            try:
                proc_plan = subprocess.run(
                    probe_cmd,
                    cwd=str(worktree_dir),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                return {
                    "mode": "cli_plan",
                    "selected_modules": [],
                    "cmd": [],
                    "discover_cmd": discover_cmd,
                    "unittest_ran": True,
                    "run_bats": False,
                    "plan_timed_out": True,
                    "plan_timeout_output": _format_timeout_output("selective_test_runner (--dry-run)", timeout, exc),
                }
            if proc_plan.returncode == 0:
                try:
                    plan_payload = json.loads(proc_plan.stdout or "")
                except (ValueError, TypeError):
                    plan_payload = None
                if isinstance(plan_payload, dict):
                    targets, run_all, skip_unit_tests, plan_bats = _extract_cli_plan_targets(plan_payload)
                    bats_needed = (
                        plan_bats
                        if "should_run_bats" in plan_payload
                        else (run_all or any(str(rel).endswith((".sh", ".bats")) for rel in files_list))
                    )
                    if skip_unit_tests and not run_all and not targets:
                        return {
                            "mode": "cli_plan",
                            "selected_modules": [],
                            "cmd": [],
                            "discover_cmd": discover_cmd,
                            "unittest_ran": False,
                            "run_bats": bats_needed,
                            "plan_timed_out": False,
                            "plan_timeout_output": "",
                        }
                    if not run_all and targets:
                        return {
                            "mode": "cli_plan",
                            "selected_modules": targets,
                            "cmd": [sys.executable, "-m", "unittest", *targets, "-v"],
                            "discover_cmd": discover_cmd,
                            "unittest_ran": True,
                            "run_bats": bats_needed,
                            "plan_timed_out": False,
                            "plan_timeout_output": "",
                        }
                    return {
                        "mode": "cli_plan",
                        "selected_modules": [],
                        "cmd": discover_cmd,
                        "discover_cmd": discover_cmd,
                        "unittest_ran": bool(discover_cmd),
                        "run_bats": bats_needed,
                        "plan_timed_out": False,
                        "plan_timeout_output": "",
                    }

        cli_cmd = [
            sys.executable,
            str(runner_path),
            *(["--changed-files", *files_list] if files_list else ["--all"]),
        ]
        return {
            "mode": "cli_direct",
            "selected_modules": [],
            "cmd": cli_cmd,
            "discover_cmd": discover_cmd,
            "unittest_ran": True,
            "run_bats": any(str(rel).endswith((".sh", ".bats")) for rel in files_list),
            "plan_timed_out": False,
            "plan_timeout_output": "",
        }

    return {
        "mode": "discover" if discover_cmd else "none",
        "selected_modules": [],
        "cmd": discover_cmd,
        "discover_cmd": discover_cmd,
        "unittest_ran": bool(discover_cmd),
        "run_bats": False,
        "plan_timed_out": False,
        "plan_timeout_output": "",
    }


def _did_direct_cli_runner_execute_tests(output: str) -> bool:
    """Return True when direct `selective_test_runner.py` CLI output shows actual test execution."""
    return bool(
        re.search(r"Ran\s+\d+\s+tests?\s+in\b", output)
        or "FAILED (" in output
        or "[Selective Test Runner] FAILED" in output
        or "[Selective Test Runner] PASSED" in output
    )


def _run_default_union_verification(
    worktree_dir: Path,
    union_files: Sequence[str],
    *,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Execute `qa_static_audit.py --incremental` + selective tests covering `files(B) ∪ files(merged_A)`."""
    effective_timeout = _resolve_rebase_verify_timeout(timeout)
    clean_env = _build_verification_env(worktree_dir)

    qa_script = worktree_dir / "tests" / "scripts" / "qa_static_audit.py"
    qa_ran = qa_script.is_file()
    qa_passed = True
    qa_timed_out = False
    qa_output = ""
    if qa_ran:
        try:
            proc_qa = subprocess.run(
                [sys.executable, str(qa_script), "--incremental"],
                cwd=str(worktree_dir),
                env=clean_env,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                check=False,
            )
            qa_passed = proc_qa.returncode == 0
            qa_output = f"{proc_qa.stdout or ''}\n{proc_qa.stderr or ''}".strip()
        except subprocess.TimeoutExpired as exc:
            qa_passed = False
            qa_timed_out = True
            qa_output = _format_timeout_output("qa_static_audit", effective_timeout, exc)

    selected_modules: list[str] = []
    unittest_ran = False
    tests_passed = True
    tests_timed_out = False
    test_output = ""
    run_bats = False

    if not qa_timed_out:
        plan_info = _resolve_selective_execution_plan(
            worktree_dir,
            union_files,
            clean_env,
            timeout=effective_timeout,
        )
        selected_modules = list(plan_info["selected_modules"])
        unittest_ran = bool(plan_info["unittest_ran"])
        run_bats = bool(plan_info["run_bats"])

        if plan_info["plan_timed_out"]:
            tests_passed = False
            tests_timed_out = True
            test_output = str(plan_info["plan_timeout_output"])
        elif plan_info["cmd"]:
            try:
                proc_ut = subprocess.run(
                    list(plan_info["cmd"]),
                    cwd=str(worktree_dir),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                    check=False,
                )
                tests_passed = proc_ut.returncode == 0
                test_output = f"{proc_ut.stdout or ''}\n{proc_ut.stderr or ''}".strip()
                if (
                    not tests_passed
                    and plan_info["mode"] == "cli_direct"
                    and plan_info["discover_cmd"]
                    and not _did_direct_cli_runner_execute_tests(test_output)
                ):
                    proc_fb = subprocess.run(
                        list(plan_info["discover_cmd"]),
                        cwd=str(worktree_dir),
                        env=clean_env,
                        capture_output=True,
                        text=True,
                        timeout=effective_timeout,
                        check=False,
                    )
                    tests_passed = proc_fb.returncode == 0
                    test_output = f"{proc_fb.stdout or ''}\n{proc_fb.stderr or ''}".strip()
            except subprocess.TimeoutExpired as exc:
                tests_passed = False
                tests_timed_out = True
                test_output = _format_timeout_output(
                    f"unittest ({', '.join(selected_modules) or plan_info['mode']})",
                    effective_timeout,
                    exc,
                )

    bats_ran = False
    bats_passed = True
    bats_timed_out = False
    bats_output = ""
    bats_dir = worktree_dir / "tests" / "bats"
    if run_bats and not (qa_timed_out or tests_timed_out) and bats_dir.is_dir():
        bats_files = sorted(bats_dir.glob("*.bats"))
        bats_bin = shutil.which("bats")
        if bats_files and bats_bin:
            bats_ran = True
            try:
                proc_b = subprocess.run(
                    [bats_bin, *(str(p) for p in bats_files)],
                    cwd=str(worktree_dir),
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    timeout=effective_timeout,
                    check=False,
                )
                bats_passed = proc_b.returncode == 0
                bats_output = f"{proc_b.stdout or ''}\n{proc_b.stderr or ''}".strip()
            except subprocess.TimeoutExpired as exc:
                bats_passed = False
                bats_timed_out = True
                bats_output = _format_timeout_output("bats", effective_timeout, exc)

    head_proc = _run_git(worktree_dir, ["rev-parse", "HEAD"], timeout=5.0)
    head_sha = (head_proc.stdout or "").strip() if head_proc.returncode == 0 else ""
    snap = _snapshot_clean_fingerprint(worktree_dir, head_sha)

    total_ut = sum(int(m) for m in re.findall(r"Ran\s+(\d+)\s+tests?\s+in\b", test_output))
    fail_ut = sum(int(m) for m in re.findall(r"failures=(\d+)", test_output)) + sum(
        int(m) for m in re.findall(r"errors=(\d+)", test_output)
    )
    if not tests_passed and fail_ut == 0:
        fail_ut = max(1, total_ut or 1)
    passed_ut = max(0, total_ut - fail_ut) if total_ut else 0

    any_timed_out = bool(qa_timed_out or tests_timed_out or bats_timed_out)
    overall_passed = bool(qa_passed and tests_passed and bats_passed and not any_timed_out)
    failure_chunks: list[str] = []
    if not qa_passed and qa_output:
        failure_chunks.append(f"[qa_static_audit]\n{qa_output[-2000:]}")
    if not tests_passed and test_output:
        failure_chunks.append(f"[unittest ({', '.join(selected_modules) or 'discover'})]\n{test_output[-3000:]}")
    if not bats_passed and bats_output:
        failure_chunks.append(f"[bats]\n{bats_output[-1500:]}")

    qa_entry: dict[str, Any] = {
        "status": "timeout" if qa_timed_out else (("pass" if qa_passed else "fail") if qa_ran else "skipped"),
        "passed": bool(qa_passed),
        "errors": 0 if qa_passed else 1,
        "warnings": 0,
        "timed_out": bool(qa_timed_out),
    }
    if qa_timed_out:
        qa_entry["timeout_seconds"] = effective_timeout

    ut_entry: dict[str, Any] = {
        "status": "timeout" if tests_timed_out else (("pass" if tests_passed else "fail") if unittest_ran else "skipped"),
        "passed": bool(tests_passed),
        "passed_count": passed_ut,
        "total_count": total_ut,
        "failed_count": fail_ut,
        "timed_out": bool(tests_timed_out),
    }
    if tests_timed_out:
        ut_entry["timeout_seconds"] = effective_timeout

    bats_entry: dict[str, Any] = {
        "status": "timeout" if bats_timed_out else (("pass" if bats_passed else "fail") if bats_ran else "skipped"),
        "passed": bool(bats_passed),
        "passed_count": 0,
        "total_count": 0,
        "timed_out": bool(bats_timed_out),
    }
    if bats_timed_out:
        bats_entry["timeout_seconds"] = effective_timeout

    verification_results: dict[str, Any] = {
        "head_sha": snap["head_sha"],
        "tree_sha": snap["tree_sha"],
        "dirty": False,
        "content_fingerprint": snap["content_fingerprint"],
        "verified_at_utc": _utc_now_iso(),
        "overall_passed": overall_passed,
        "timed_out": any_timed_out,
        "timeout_seconds": effective_timeout,
        "pre_commit_status": "SKIPPED",
        "cross_impact_union_files": list(union_files),
        "selected_test_modules": selected_modules,
        "qa_static_audit": qa_entry,
        "unittest": ut_entry,
        "bats": bats_entry,
        "pre_commit": {"status": "skipped", "passed": None},
    }
    return {
        "passed": overall_passed,
        "verification_results": verification_results,
        "failure_details": "\n\n".join(failure_chunks),
    }


def _collect_merged_a_context(
    repo_dir: Path,
    old_base_sha: str,
    new_base_sha: str,
) -> tuple[list[str], str]:
    """Capture commit log lines and bounded unified diff for `old_base_sha..new_base_sha` (`merged_A`)."""
    if not old_base_sha or not new_base_sha or old_base_sha == new_base_sha:
        return [], ""
    log_proc = _run_git(repo_dir, ["log", "--oneline", f"{old_base_sha}..{new_base_sha}"], timeout=10.0)
    commits = [ln.strip() for ln in (log_proc.stdout or "").splitlines() if ln.strip()] if log_proc.returncode == 0 else []

    diff_proc = _run_git(repo_dir, ["diff", f"{old_base_sha}..{new_base_sha}"], timeout=15.0)
    diff_txt = diff_proc.stdout or "" if diff_proc.returncode == 0 else ""
    if len(diff_txt) > MAX_CAPTURED_DIFF_CHARS:
        diff_txt = diff_txt[:MAX_CAPTURED_DIFF_CHARS] + "\n...(merged_a_diff truncated)"
    return commits, diff_txt


def rebase_and_reverify(
    workdir: Path | str,
    *,
    branch_name: str = "",
    recorded_base_sha: str = "",
    base_branch: str = "main",
    remote: str = "origin",
    fetch: bool = False,
    timeout: float | None = None,
    verify_fn: Callable[[Path, list[str]], dict[str, Any]] | None = None,
    restamp_fn: Callable[[Path, list[str]], None] | None = None,
    speculative_cache: Mapping[str, Any] | None = None,
) -> RebaseReverifyResult:
    """Rebase `branch_name` onto updated `<remote>/<base_branch>` and re-verify over `files(B) ∪ files(merged_A)`.

    Outcomes:
      - `AUTO_PROMOTED_ZERO_LLM`: clean rebase + green cross-impact verification -> promote without LLM tokens.
      - `NEEDS_AGENT_SEMANTIC_FIX`: clean rebase, but cross-impact verification failed -> includes `merged_a_diff` + traceback.
      - `NEEDS_AGENT_CONFLICT_RESOLUTION`: conflict detected in dry-run or rebase -> `git rebase --abort` leaves worktree pristine.
    """
    raw_dir = Path(str(workdir).replace("$HOME", str(Path.home()))).expanduser().resolve()
    if not (raw_dir / ".git").exists():
        return RebaseReverifyResult(
            status="error",
            outcome="",
            rebased=False,
            drifted=False,
            aborted_cleanly=False,
            worktree_pristine=False,
            branch_name=branch_name,
            workdir=_sanitize_path(str(raw_dir)),
            previous_head_sha="",
            new_head_sha="",
            previous_base_sha=recorded_base_sha,
            new_base_sha="",
            error=f"Workdir is not a Git repository or worktree: {_sanitize_path(str(raw_dir))}",
        )

    wt_for_br = _find_worktree_for_branch(raw_dir, branch_name) if branch_name else None
    effective_dir = wt_for_br if wt_for_br is not None else raw_dir

    drift = detect_base_sha_drift(
        effective_dir,
        branch_name=branch_name,
        recorded_base_sha=recorded_base_sha,
        base_branch=base_branch,
        remote=remote,
        fetch=fetch,
    )
    if drift.status != "ok":
        return RebaseReverifyResult(
            status="error",
            outcome="",
            rebased=False,
            drifted=False,
            aborted_cleanly=False,
            worktree_pristine=_is_worktree_clean(effective_dir),
            branch_name=drift.branch_name,
            workdir=_sanitize_path(str(effective_dir)),
            previous_head_sha=drift.branch_head_sha,
            new_head_sha=drift.branch_head_sha,
            previous_base_sha=recorded_base_sha,
            new_base_sha=drift.current_base_sha,
            error=drift.error,
        )

    resolved_br = drift.branch_name
    pre_head_sha = drift.branch_head_sha
    old_base_sha = drift.recorded_base_sha or drift.merge_base_sha
    target_base_sha = drift.current_base_sha
    merged_a_commits, merged_a_diff = _collect_merged_a_context(effective_dir, old_base_sha, target_base_sha)

    if not _is_worktree_clean(effective_dir):
        return RebaseReverifyResult(
            status="error",
            outcome="",
            rebased=False,
            drifted=drift.drifted,
            aborted_cleanly=False,
            worktree_pristine=False,
            branch_name=resolved_br,
            workdir=_sanitize_path(str(effective_dir)),
            previous_head_sha=pre_head_sha,
            new_head_sha=pre_head_sha,
            previous_base_sha=old_base_sha,
            new_base_sha=target_base_sha,
            branch_b_files=drift.branch_b_files,
            merged_a_files=drift.merged_a_files,
            union_files=drift.union_files,
            error=f"Refusing to rebase dirty worktree at {_sanitize_path(str(effective_dir))}.",
        )

    # Ensure target branch is checked out inside `effective_dir` when Rebasing
    cur_br_proc = _run_git(effective_dir, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=5.0)
    cur_checked_out = (cur_br_proc.stdout or "").strip() if cur_br_proc.returncode == 0 else ""
    if resolved_br and resolved_br != "HEAD" and cur_checked_out != resolved_br:
        co_proc = _run_git(effective_dir, ["checkout", resolved_br], timeout=15.0)
        if co_proc.returncode != 0:
            return RebaseReverifyResult(
                status="error",
                outcome="",
                rebased=False,
                drifted=drift.drifted,
                aborted_cleanly=False,
                worktree_pristine=_is_worktree_clean(effective_dir),
                branch_name=resolved_br,
                workdir=_sanitize_path(str(effective_dir)),
                previous_head_sha=pre_head_sha,
                new_head_sha=pre_head_sha,
                previous_base_sha=old_base_sha,
                new_base_sha=target_base_sha,
                error=(co_proc.stderr or co_proc.stdout or f"Failed to checkout {resolved_br}").strip(),
            )

    cache_ok = (
        isinstance(speculative_cache, Mapping)
        and bool(speculative_cache.get("verification_passed"))
        and str(speculative_cache.get("follower_head_sha") or "").strip() == pre_head_sha
    )
    cache_slot1_sha = str((speculative_cache or {}).get("slot1_head_sha") or "").strip() if cache_ok else ""
    cache_spec_sha = str((speculative_cache or {}).get("speculative_head_sha") or "").strip() if cache_ok else ""
    cache_spec_tree = str((speculative_cache or {}).get("speculative_tree_sha") or "").strip() if cache_ok else ""
    cache_vr = (
        dict((speculative_cache or {}).get("verification_results") or {})
        if cache_ok and isinstance((speculative_cache or {}).get("verification_results"), Mapping)
        else {}
    )

    probe_dict: dict[str, Any] = {}
    rebased = False
    speculative_cache_hit = False
    fast_path_tree_sha = ""

    if drift.behind_count > 0:
        # Speculative fast-path 1: Slot #1 fast-forwarded directly onto main -> reset branch directly to pre-verified commit.
        if cache_ok and cache_slot1_sha == target_base_sha and cache_spec_sha:
            chk_spec = _run_git(effective_dir, ["rev-parse", "--verify", f"{cache_spec_sha}^{{tree}}"], timeout=5.0)
            spec_tree_live = (chk_spec.stdout or "").strip() if chk_spec.returncode == 0 else ""
            if spec_tree_live and (not cache_spec_tree or spec_tree_live == cache_spec_tree):
                rst_proc = _run_git(effective_dir, ["reset", "--hard", cache_spec_sha], timeout=15.0)
                if rst_proc.returncode == 0:
                    rebased = True
                    speculative_cache_hit = True
                    fast_path_tree_sha = spec_tree_live

        if not speculative_cache_hit:
            probe = probe_merge_tree_conflicts(
                effective_dir,
                target_base_sha,
                pre_head_sha,
                merge_base_sha=drift.merge_base_sha,
            )
            probe_dict = probe.to_dict()
            if not probe.clean and probe.supported_write_tree:
                # In-memory `merge-tree --write-tree` deterministically proved a conflict without touching the worktree!
                conf_list = list(probe.conflicted_files) or sorted(set(drift.branch_b_files) & set(drift.merged_a_files))
                return RebaseReverifyResult(
                    status="ok",
                    outcome=OUTCOME_NEEDS_CONFLICT_RESOLUTION,
                    rebased=False,
                    drifted=True,
                    aborted_cleanly=True,
                    worktree_pristine=_is_worktree_clean(effective_dir),
                    branch_name=resolved_br,
                    workdir=_sanitize_path(str(effective_dir)),
                    previous_head_sha=pre_head_sha,
                    new_head_sha=pre_head_sha,
                    previous_base_sha=old_base_sha,
                    new_base_sha=target_base_sha,
                    branch_b_files=drift.branch_b_files,
                    merged_a_files=drift.merged_a_files,
                    union_files=drift.union_files,
                    conflicted_files=tuple(conf_list),
                    merged_a_commits=tuple(merged_a_commits),
                    merged_a_diff=merged_a_diff,
                    verification_passed=False,
                    merge_tree_probe=probe_dict,
                )

            rb_proc = _run_git(effective_dir, ["rebase", target_base_sha], timeout=45.0)
            if rb_proc.returncode != 0:
                unmerged_proc = _run_git(effective_dir, ["diff", "--name-only", "--diff-filter=U"], timeout=5.0)
                unmerged_files = [
                    ln.strip() for ln in (unmerged_proc.stdout or "").splitlines() if ln.strip()
                ]
                parsed_rb_conf = _parse_write_tree_conflicts(f"{rb_proc.stdout or ''}\n{rb_proc.stderr or ''}")
                conflicted = sorted(set(unmerged_files) | set(parsed_rb_conf) | set(probe.conflicted_files))
                if not conflicted:
                    conflicted = sorted(set(drift.branch_b_files) & set(drift.merged_a_files))

                abort_proc = _run_git(effective_dir, ["rebase", "--abort"], timeout=15.0)
                if abort_proc.returncode != 0 and _is_rebase_in_progress(effective_dir):
                    _run_git(effective_dir, ["reset", "--hard", pre_head_sha], timeout=10.0)
                pristine = _is_worktree_clean(effective_dir)
                _, restored_sha = _resolve_branch_head_sha(effective_dir, branch_name=resolved_br)
                aborted_ok = pristine and (restored_sha == pre_head_sha)

                return RebaseReverifyResult(
                    status="ok",
                    outcome=OUTCOME_NEEDS_CONFLICT_RESOLUTION,
                    rebased=False,
                    drifted=True,
                    aborted_cleanly=aborted_ok,
                    worktree_pristine=pristine,
                    branch_name=resolved_br,
                    workdir=_sanitize_path(str(effective_dir)),
                    previous_head_sha=pre_head_sha,
                    new_head_sha=restored_sha or pre_head_sha,
                    previous_base_sha=old_base_sha,
                    new_base_sha=target_base_sha,
                    branch_b_files=drift.branch_b_files,
                    merged_a_files=drift.merged_a_files,
                    union_files=drift.union_files,
                    conflicted_files=tuple(conflicted),
                    merged_a_commits=tuple(merged_a_commits),
                    merged_a_diff=merged_a_diff,
                    verification_passed=False,
                    merge_tree_probe=probe_dict,
                    failure_details=f"{rb_proc.stdout or ''}\n{rb_proc.stderr or ''}".strip()[:3000],
                )
            rebased = True

    if restamp_fn is not None and rebased:
        with contextlib.suppress(Exception):
            restamp_fn(effective_dir, list(drift.branch_b_files))

    _, post_head_sha = _resolve_branch_head_sha(effective_dir, branch_name=resolved_br)
    post_b_files = _git_diff_names(effective_dir, f"{target_base_sha}..{post_head_sha}") if post_head_sha else list(drift.branch_b_files)
    union_files = compute_cross_impact_files(post_b_files or drift.branch_b_files, drift.merged_a_files)

    # Speculative fast-path 2: squash/merge commit produced byte-for-byte identical tree SHA -> reuse pre-verified results!
    post_snap = _snapshot_clean_fingerprint(effective_dir, post_head_sha or pre_head_sha)
    post_tree_sha = str(post_snap.get("tree_sha") or "")
    if speculative_cache_hit and post_tree_sha != fast_path_tree_sha:
        # `restamp_fn` amended the pre-verified commit: the cached verification no longer covers this tree.
        speculative_cache_hit = False
    if not speculative_cache_hit and cache_ok and cache_spec_tree and post_tree_sha == cache_spec_tree:
        speculative_cache_hit = True

    if speculative_cache_hit and cache_vr:
        ver_out: dict[str, Any] = {
            "passed": True,
            "verification_results": {
                **cache_vr,
                "head_sha": post_head_sha or pre_head_sha,
                "tree_sha": str(post_snap.get("tree_sha") or cache_spec_tree),
                "dirty": False,
                "content_fingerprint": str(post_snap.get("content_fingerprint") or cache_vr.get("content_fingerprint") or ""),
                "speculative_cache_hit": True,
                "cross_impact_union_files": list(union_files),
            },
            "failure_details": "",
        }
    elif verify_fn is not None:
        ver_out = verify_fn(effective_dir, union_files)
    else:
        effective_timeout = _resolve_rebase_verify_timeout(timeout)
        ver_out = _run_default_union_verification(effective_dir, union_files, timeout=effective_timeout)

    ver_passed = bool(ver_out.get("passed"))
    ver_results = dict(ver_out.get("verification_results") or {})
    failure_details = str(ver_out.get("failure_details") or ver_out.get("test_output") or ver_out.get("qa_output") or "").strip()

    # Best-effort cleanup: a promoted follower has consumed its pinned speculative commit.
    consumed_ref = (
        str(speculative_cache.get("speculative_ref") or "").strip() if isinstance(speculative_cache, Mapping) else ""
    )
    if ver_passed and rebased and consumed_ref.startswith(SPECULATIVE_REF_PREFIX):
        _run_git(effective_dir, ["update-ref", "-d", consumed_ref], timeout=5.0)

    outcome = OUTCOME_AUTO_PROMOTED if ver_passed else OUTCOME_NEEDS_SEMANTIC_FIX
    return RebaseReverifyResult(
        status="ok",
        outcome=outcome,
        rebased=rebased,
        drifted=drift.drifted,
        aborted_cleanly=False,
        worktree_pristine=_is_worktree_clean(effective_dir),
        branch_name=resolved_br,
        workdir=_sanitize_path(str(effective_dir)),
        previous_head_sha=pre_head_sha,
        new_head_sha=post_head_sha or pre_head_sha,
        previous_base_sha=old_base_sha,
        new_base_sha=target_base_sha,
        branch_b_files=tuple(post_b_files or drift.branch_b_files),
        merged_a_files=drift.merged_a_files,
        union_files=tuple(union_files),
        conflicted_files=(),
        merged_a_commits=tuple(merged_a_commits),
        merged_a_diff=merged_a_diff if not ver_passed else "",
        verification_passed=ver_passed,
        verification_results=ver_results,
        failure_details=failure_details,
        merge_tree_probe=probe_dict,
        speculative_cache_hit=speculative_cache_hit,
    )


# ==============================================================================
# Phase 2: Speculative Pre-Rebase over Frozen Slot #1 HEAD & Release Train Bundling
# ==============================================================================


def _resolve_main_repo_root(repo_or_wt: Path) -> Path:
    """Resolve the primary repository checkout root from any linked worktree or repo path."""
    proc = _run_git(repo_or_wt, ["rev-parse", "--git-common-dir"], timeout=5.0)
    if proc.returncode == 0 and (proc.stdout or "").strip():
        common_raw = Path((proc.stdout or "").strip())
        common_abs = common_raw if common_raw.is_absolute() else (repo_or_wt / common_raw)
        common_res = common_abs.resolve()
        if common_res.name == ".git" and common_res.parent.is_dir():
            return common_res.parent
    return repo_or_wt.resolve()


def _slugify_ref_token(raw: str, max_len: int = 64) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(raw or "").strip()).strip("-.")
    return cleaned[:max_len].strip("-.") or "slot"


def _remove_worktree_cleanly(main_repo: Path, wt_path: Path) -> None:
    """Force-remove a temporary or train worktree and prune stale git worktree records."""
    if wt_path.exists():
        _run_git(main_repo, ["worktree", "remove", "--force", str(wt_path)], timeout=20.0)
        if wt_path.exists():
            shutil.rmtree(wt_path, ignore_errors=True)
    _run_git(main_repo, ["worktree", "prune"], timeout=10.0)


def discard_release_train(
    repo_dir: Path | str,
    *,
    train_branch: str,
    train_worktree: Path | str | None = None,
) -> None:
    """Best-effort removal of a local Release Train worktree and branch (rollback or failed publish)."""
    main_repo = _resolve_main_repo_root(Path(str(repo_dir).replace("$HOME", str(Path.home()))).expanduser().resolve())
    wt_raw = str(train_worktree or "").strip()
    if wt_raw:
        wt_path = Path(wt_raw.replace("$HOME", str(Path.home()))).expanduser().resolve()
        if wt_path.exists():
            _run_git(wt_path, ["cherry-pick", "--abort"], timeout=10.0)
        _remove_worktree_cleanly(main_repo, wt_path)
    clean_branch = str(train_branch or "").strip().removeprefix("refs/heads/")
    if clean_branch:
        _run_git(main_repo, ["branch", "-D", clean_branch], timeout=10.0)


def speculative_pre_rebase_slot(
    repo_dir: Path | str,
    *,
    follower_branch: str,
    slot1_ref: str = "",
    slot1_head_sha: str = "",
    verify: bool = True,
    timeout: float | None = None,
    verify_fn: Callable[[Path, list[str]], dict[str, Any]] | None = None,
) -> SpeculativePreRebaseResult:
    """Speculatively pre-rebase `follower_branch` over frozen Slot #1 `HEAD` inside an isolated temp worktree.

    Never mutates `follower_branch` or its live worktree: pins a verified commit under
    `refs/agy-speculative/<slug>` so post-merge promotion via `rebase_and_reverify` reuses the
    verified tree/commit directly with near-zero latency. `verification_passed` is True only when
    verification actually ran and passed (never for `verify=False` or `SPECULATIVE_SKIPPED`), so an
    unverified result can never satisfy the promotion cache.
    """
    r_dir = Path(str(repo_dir).replace("$HOME", str(Path.home()))).expanduser().resolve()
    clean_follower = str(follower_branch or "").strip().removeprefix("refs/heads/")
    target_ref = str(slot1_head_sha or slot1_ref or "").strip()
    if not (r_dir / ".git").exists():
        return SpeculativePreRebaseResult(
            status="error",
            outcome="",
            rebased=False,
            clean=False,
            aborted_cleanly=False,
            worktree_pristine=False,
            follower_branch=clean_follower,
            slot1_ref=target_ref,
            slot1_head_sha="",
            slot1_tree_sha="",
            follower_head_sha="",
            error=f"Git repository not found at {_sanitize_path(str(r_dir))}",
        )
    if not clean_follower or not target_ref:
        return SpeculativePreRebaseResult(
            status="error",
            outcome="",
            rebased=False,
            clean=False,
            aborted_cleanly=False,
            worktree_pristine=_is_worktree_clean(r_dir),
            follower_branch=clean_follower,
            slot1_ref=target_ref,
            slot1_head_sha="",
            slot1_tree_sha="",
            follower_head_sha="",
            error="Both `follower_branch` and `slot1_ref`/`slot1_head_sha` are required.",
        )

    main_repo = _resolve_main_repo_root(r_dir)
    _, resolved_follower_sha = _resolve_branch_head_sha(main_repo, branch_name=clean_follower)
    s1_proc = _run_git(main_repo, ["rev-parse", "--verify", target_ref], timeout=5.0)
    resolved_s1_sha = (s1_proc.stdout or "").strip() if s1_proc.returncode == 0 else ""
    if not resolved_follower_sha or not resolved_s1_sha:
        return SpeculativePreRebaseResult(
            status="error",
            outcome="",
            rebased=False,
            clean=False,
            aborted_cleanly=False,
            worktree_pristine=_is_worktree_clean(main_repo),
            follower_branch=clean_follower,
            slot1_ref=target_ref,
            slot1_head_sha=resolved_s1_sha,
            slot1_tree_sha="",
            follower_head_sha=resolved_follower_sha,
            error="Could not resolve follower branch or frozen Slot #1 HEAD SHA.",
        )

    s1_tree_proc = _run_git(main_repo, ["rev-parse", "--verify", f"{resolved_s1_sha}^{{tree}}"], timeout=5.0)
    s1_tree_sha = (s1_tree_proc.stdout or "").strip() if s1_tree_proc.returncode == 0 else ""

    # Skip if follower already contains frozen Slot #1 HEAD
    anc_proc = _run_git(main_repo, ["merge-base", "--is-ancestor", resolved_s1_sha, resolved_follower_sha], timeout=5.0)
    if anc_proc.returncode == 0:
        f_snap = _snapshot_clean_fingerprint(main_repo, resolved_follower_sha)
        return SpeculativePreRebaseResult(
            status="ok",
            outcome=OUTCOME_SPECULATIVE_SKIPPED,
            rebased=False,
            clean=True,
            aborted_cleanly=False,
            worktree_pristine=_is_worktree_clean(main_repo),
            follower_branch=clean_follower,
            slot1_ref=target_ref,
            slot1_head_sha=resolved_s1_sha,
            slot1_tree_sha=s1_tree_sha,
            follower_head_sha=resolved_follower_sha,
            speculative_head_sha=resolved_follower_sha,
            speculative_tree_sha=str(f_snap.get("tree_sha") or ""),
            verification_passed=False,
        )

    mb_proc = _run_git(main_repo, ["merge-base", resolved_s1_sha, resolved_follower_sha], timeout=5.0)
    mb_sha = (mb_proc.stdout or "").strip() if mb_proc.returncode == 0 else ""
    if not mb_sha:
        return SpeculativePreRebaseResult(
            status="error",
            outcome="",
            rebased=False,
            clean=False,
            aborted_cleanly=False,
            worktree_pristine=_is_worktree_clean(main_repo),
            follower_branch=clean_follower,
            slot1_ref=target_ref,
            slot1_head_sha=resolved_s1_sha,
            slot1_tree_sha=s1_tree_sha,
            follower_head_sha=resolved_follower_sha,
            error="Could not compute merge-base between frozen Slot #1 HEAD and follower branch.",
        )

    slot1_files = _git_diff_names(main_repo, f"{mb_sha}..{resolved_s1_sha}")
    follower_files = _git_diff_names(main_repo, f"{mb_sha}..{resolved_follower_sha}")
    union_files = compute_cross_impact_files(follower_files, slot1_files)

    probe = probe_merge_tree_conflicts(
        main_repo,
        resolved_s1_sha,
        resolved_follower_sha,
        merge_base_sha=mb_sha,
    )
    probe_dict = probe.to_dict()
    if not probe.clean and probe.supported_write_tree:
        conf_list = list(probe.conflicted_files) or sorted(set(follower_files) & set(slot1_files))
        return SpeculativePreRebaseResult(
            status="ok",
            outcome=OUTCOME_SPECULATIVE_CONFLICT,
            rebased=False,
            clean=False,
            aborted_cleanly=True,
            worktree_pristine=_is_worktree_clean(main_repo),
            follower_branch=clean_follower,
            slot1_ref=target_ref,
            slot1_head_sha=resolved_s1_sha,
            slot1_tree_sha=s1_tree_sha,
            follower_head_sha=resolved_follower_sha,
            union_files=tuple(union_files),
            conflicted_files=tuple(conf_list),
            verification_passed=False,
            merge_tree_probe=probe_dict,
        )

    spec_ref = f"{SPECULATIVE_REF_PREFIX}{_slugify_ref_token(clean_follower)}"
    with tempfile.TemporaryDirectory(prefix="agy-spec-rebase-") as tmp_root:
        tmp_wt = (Path(tmp_root) / "wt").resolve()
        add_proc = _run_git(main_repo, ["worktree", "add", "--detach", str(tmp_wt), resolved_follower_sha], timeout=30.0)
        if add_proc.returncode != 0:
            return SpeculativePreRebaseResult(
                status="error",
                outcome="",
                rebased=False,
                clean=False,
                aborted_cleanly=False,
                worktree_pristine=_is_worktree_clean(main_repo),
                follower_branch=clean_follower,
                slot1_ref=target_ref,
                slot1_head_sha=resolved_s1_sha,
                slot1_tree_sha=s1_tree_sha,
                follower_head_sha=resolved_follower_sha,
                union_files=tuple(union_files),
                merge_tree_probe=probe_dict,
                error=(add_proc.stderr or add_proc.stdout or "Failed to create speculative temp worktree.").strip(),
            )
        try:
            rb_proc = _run_git(tmp_wt, ["rebase", "--onto", resolved_s1_sha, mb_sha, "HEAD"], timeout=45.0)
            if rb_proc.returncode != 0:
                unmerged_proc = _run_git(tmp_wt, ["diff", "--name-only", "--diff-filter=U"], timeout=5.0)
                unmerged_files = [ln.strip() for ln in (unmerged_proc.stdout or "").splitlines() if ln.strip()]
                parsed_conf = _parse_write_tree_conflicts(f"{rb_proc.stdout or ''}\n{rb_proc.stderr or ''}")
                conflicted = sorted(set(unmerged_files) | set(parsed_conf) | set(probe.conflicted_files))
                if not conflicted:
                    conflicted = sorted(set(follower_files) & set(slot1_files))
                _run_git(tmp_wt, ["rebase", "--abort"], timeout=15.0)
                _run_git(main_repo, ["update-ref", "-d", spec_ref], timeout=5.0)
                return SpeculativePreRebaseResult(
                    status="ok",
                    outcome=OUTCOME_SPECULATIVE_CONFLICT,
                    rebased=False,
                    clean=False,
                    aborted_cleanly=True,
                    worktree_pristine=True,
                    follower_branch=clean_follower,
                    slot1_ref=target_ref,
                    slot1_head_sha=resolved_s1_sha,
                    slot1_tree_sha=s1_tree_sha,
                    follower_head_sha=resolved_follower_sha,
                    union_files=tuple(union_files),
                    conflicted_files=tuple(conflicted),
                    verification_passed=False,
                    failure_details=f"{rb_proc.stdout or ''}\n{rb_proc.stderr or ''}".strip()[:3000],
                    merge_tree_probe=probe_dict,
                )

            spec_head_proc = _run_git(tmp_wt, ["rev-parse", "HEAD"], timeout=5.0)
            spec_head_sha = (spec_head_proc.stdout or "").strip() if spec_head_proc.returncode == 0 else ""
            spec_snap = _snapshot_clean_fingerprint(tmp_wt, spec_head_sha)
            spec_tree_sha = str(spec_snap.get("tree_sha") or "")

            ver_passed = True
            verified = False
            ver_results: dict[str, Any] = {}
            failure_details = ""
            if verify:
                verified = True
                if verify_fn is not None:
                    ver_out = verify_fn(tmp_wt, union_files)
                else:
                    ver_out = _run_default_union_verification(
                        tmp_wt,
                        union_files,
                        timeout=_resolve_rebase_verify_timeout(timeout),
                    )
                ver_passed = bool(ver_out.get("passed"))
                ver_results = dict(ver_out.get("verification_results") or {})
                ver_results["head_sha"] = spec_head_sha
                ver_results["tree_sha"] = spec_tree_sha
                ver_results["dirty"] = False
                ver_results["content_fingerprint"] = str(spec_snap.get("content_fingerprint") or "")
                failure_details = str(
                    ver_out.get("failure_details") or ver_out.get("test_output") or ver_out.get("qa_output") or ""
                ).strip()

            if ver_passed and spec_head_sha:
                _run_git(main_repo, ["update-ref", spec_ref, spec_head_sha], timeout=5.0)
                return SpeculativePreRebaseResult(
                    status="ok",
                    outcome=OUTCOME_SPECULATIVE_READY,
                    rebased=True,
                    clean=True,
                    aborted_cleanly=False,
                    worktree_pristine=True,
                    follower_branch=clean_follower,
                    slot1_ref=target_ref,
                    slot1_head_sha=resolved_s1_sha,
                    slot1_tree_sha=s1_tree_sha,
                    follower_head_sha=resolved_follower_sha,
                    speculative_head_sha=spec_head_sha,
                    speculative_tree_sha=spec_tree_sha,
                    speculative_ref=spec_ref,
                    union_files=tuple(union_files),
                    conflicted_files=(),
                    verification_passed=verified,
                    verification_results=ver_results,
                    merge_tree_probe=probe_dict,
                )

            _run_git(main_repo, ["update-ref", "-d", spec_ref], timeout=5.0)
            return SpeculativePreRebaseResult(
                status="ok",
                outcome=OUTCOME_SPECULATIVE_SEMANTIC_FAILURE,
                rebased=True,
                clean=False,
                aborted_cleanly=True,
                worktree_pristine=True,
                follower_branch=clean_follower,
                slot1_ref=target_ref,
                slot1_head_sha=resolved_s1_sha,
                slot1_tree_sha=s1_tree_sha,
                follower_head_sha=resolved_follower_sha,
                speculative_head_sha=spec_head_sha,
                speculative_tree_sha=spec_tree_sha,
                speculative_ref="",
                union_files=tuple(union_files),
                conflicted_files=(),
                verification_passed=False,
                verification_results=ver_results,
                failure_details=failure_details,
                merge_tree_probe=probe_dict,
            )
        finally:
            _remove_worktree_cleanly(main_repo, tmp_wt)


def _run_train_ci_parity_verification(
    train_wt: Path,
    *,
    run_pre_commit: bool = False,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Run full `test-local --mode ci-parity` validation inside the Release Train integration worktree.

    Fails closed: a crash inside the gate is reported as a failed verification instead of silently
    downgrading to the weaker union verification (no coverage gate, no pre-commit).
    """
    eff_timeout = _resolve_rebase_verify_timeout(timeout)
    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    try:
        import worktree_ops as _wt_ops

        res = _wt_ops.run_local_validation_suite(
            workdir=train_wt,
            timeout=eff_timeout,
            run_pre_commit=run_pre_commit,
            ci_parity=True,
        )
        vr = dict(res.get("verification_results") or {})
        passed = bool(res.get("passed")) and bool(vr.get("overall_passed", True))
        failure_parts: list[str] = []
        if not passed:
            for key in ("pre_commit_output", "qa_output", "test_output", "coverage_output", "bats_output", "error"):
                val = str(res.get(key) or "").strip()
                if val:
                    failure_parts.append(f"[{key}]\n{val[-2000:]}")
        return {
            "passed": passed,
            "verification_results": vr,
            "failure_details": "\n\n".join(failure_parts),
        }
    except Exception as exc:
        return {
            "passed": False,
            "verification_results": {"overall_passed": False, "ci_parity": True},
            "failure_details": (
                f"Release Train ci-parity verification crashed ({exc.__class__.__name__}: {exc}); "
                "refusing to downgrade to union verification."
            ),
        }


def bundle_ready_slots_git(
    repo_dir: Path | str,
    slot_entries: Sequence[Mapping[str, Any]],
    *,
    base_branch: str = "main",
    remote: str = "origin",
    base_sha_override: str = "",
    fetch: bool = False,
    train_branch: str = "",
    train_worktree_dir: Path | str | None = None,
    worktree_base_dir: Path | str | None = None,
    min_slots: int = DEFAULT_TRAIN_MIN_SLOTS,
    max_slots: int = DEFAULT_TRAIN_MAX_SLOTS,
    dry_run: bool = False,
    run_pre_commit: bool = False,
    timeout: float | None = None,
    ci_parity_verify_fn: Callable[[Path], dict[str, Any]] | None = None,
) -> ReleaseTrainBundleResult:
    """Combine 2-3 verified disjoint ready slots onto one Release Train branch with `ci-parity` gate + atomic rollback."""
    r_dir = Path(str(repo_dir).replace("$HOME", str(Path.home()))).expanduser().resolve()
    eff_min = max(2, int(min_slots))
    eff_max = max(eff_min, min(4, int(max_slots)))
    if not (r_dir / ".git").exists():
        return ReleaseTrainBundleResult(
            status="error",
            outcome=OUTCOME_TRAIN_NOT_ELIGIBLE,
            bundled=False,
            rolled_back=False,
            worktree_pristine=False,
            train_branch=train_branch,
            train_worktree="",
            base_ref="",
            base_sha="",
            error=f"Git repository not found at {_sanitize_path(str(r_dir))}",
        )

    main_repo = _resolve_main_repo_root(r_dir)
    is_offline = os.environ.get("AGY_OFFLINE_ONLY") == "1" or os.environ.get("PRE_COMMIT_HOOK") == "1"
    if fetch and (not is_offline or _is_local_git_remote(main_repo, remote)):
        _run_git(main_repo, ["fetch", remote, "--prune", "--quiet"], timeout=25.0)

    if base_sha_override.strip():
        ov_proc = _run_git(main_repo, ["rev-parse", "--verify", base_sha_override.strip()], timeout=5.0)
        base_ref = base_sha_override.strip()
        base_sha = (ov_proc.stdout or "").strip() if ov_proc.returncode == 0 else ""
    else:
        base_ref, base_sha = _resolve_base_ref_and_sha(main_repo, base_branch=base_branch, remote=remote)

    if not base_sha:
        return ReleaseTrainBundleResult(
            status="error",
            outcome=OUTCOME_TRAIN_NOT_ELIGIBLE,
            bundled=False,
            rolled_back=False,
            worktree_pristine=_is_worktree_clean(main_repo),
            train_branch=train_branch,
            train_worktree="",
            base_ref=base_ref,
            base_sha="",
            error="Could not resolve base SHA for release train bundling.",
        )

    selected_entries = [dict(e) for e in slot_entries[:eff_max]]
    if len(selected_entries) < eff_min:
        return ReleaseTrainBundleResult(
            status="ok",
            outcome=OUTCOME_TRAIN_NOT_ELIGIBLE,
            bundled=False,
            rolled_back=False,
            worktree_pristine=_is_worktree_clean(main_repo),
            train_branch=train_branch,
            train_worktree="",
            base_ref=base_ref,
            base_sha=base_sha,
            error=f"Need at least {eff_min} eligible ready slots to form a release train (found {len(selected_entries)}).",
        )

    resolved_slots: list[dict[str, Any]] = []
    all_union_files: list[str] = []
    for entry in selected_entries:
        br = str(entry.get("branch_name") or "").strip().removeprefix("refs/heads/")
        if not br:
            return ReleaseTrainBundleResult(
                status="error",
                outcome=OUTCOME_TRAIN_NOT_ELIGIBLE,
                bundled=False,
                rolled_back=False,
                worktree_pristine=_is_worktree_clean(main_repo),
                train_branch=train_branch,
                train_worktree="",
                base_ref=base_ref,
                base_sha=base_sha,
                error="Slot entry is missing `branch_name`.",
            )
        _, head_sha = _resolve_branch_head_sha(main_repo, branch_name=br)
        if not head_sha:
            return ReleaseTrainBundleResult(
                status="error",
                outcome=OUTCOME_TRAIN_NOT_ELIGIBLE,
                bundled=False,
                rolled_back=False,
                worktree_pristine=_is_worktree_clean(main_repo),
                train_branch=train_branch,
                train_worktree="",
                base_ref=base_ref,
                base_sha=base_sha,
                error=f"Could not resolve branch `{br}` in repository.",
            )
        pinned_head = str(entry.get("expected_head_sha") or "").strip()
        if pinned_head and pinned_head != head_sha:
            return ReleaseTrainBundleResult(
                status="ok",
                outcome=OUTCOME_TRAIN_NOT_ELIGIBLE,
                bundled=False,
                rolled_back=False,
                worktree_pristine=_is_worktree_clean(main_repo),
                train_branch=train_branch,
                train_worktree="",
                base_ref=base_ref,
                base_sha=base_sha,
                error=f"Branch `{br}` HEAD {head_sha[:12]} moved since review (expected {pinned_head[:12]}).",
            )
        # Prefer the live merge-base with the train base: a recorded `base_sha` goes stale once the slot is rebased,
        # and cherry-picking from it would replay commits the base already contains. Honor the recorded base only
        # for stacked branches, where it sits strictly ahead of the live merge-base.
        rec_base = str(entry.get("base_sha") or "").strip()
        mb_p = _run_git(main_repo, ["merge-base", base_sha, head_sha], timeout=5.0)
        slot_mb = (mb_p.stdout or "").strip() if mb_p.returncode == 0 else ""
        if rec_base and rec_base != slot_mb:
            rec_in_head = _run_git(main_repo, ["merge-base", "--is-ancestor", rec_base, head_sha], timeout=5.0)
            rec_behind_live_mb = bool(slot_mb) and (
                _run_git(main_repo, ["merge-base", "--is-ancestor", rec_base, slot_mb], timeout=5.0).returncode == 0
            )
            if rec_in_head.returncode == 0 and not rec_behind_live_mb:
                slot_mb = rec_base
        if not slot_mb:
            return ReleaseTrainBundleResult(
                status="error",
                outcome=OUTCOME_TRAIN_NOT_ELIGIBLE,
                bundled=False,
                rolled_back=False,
                worktree_pristine=_is_worktree_clean(main_repo),
                train_branch=train_branch,
                train_worktree="",
                base_ref=base_ref,
                base_sha=base_sha,
                error=f"Could not resolve merge-base for branch `{br}`.",
            )
        cnt_p = _run_git(main_repo, ["rev-list", "--count", f"{slot_mb}..{head_sha}"], timeout=5.0)
        commit_cnt = int((cnt_p.stdout or "0").strip() or 0) if cnt_p.returncode == 0 else 0
        if commit_cnt <= 0:
            return ReleaseTrainBundleResult(
                status="ok",
                outcome=OUTCOME_TRAIN_NOT_ELIGIBLE,
                bundled=False,
                rolled_back=False,
                worktree_pristine=_is_worktree_clean(main_repo),
                train_branch=train_branch,
                train_worktree="",
                base_ref=base_ref,
                base_sha=base_sha,
                error=f"Branch `{br}` has 0 commits ahead of base `{slot_mb[:8]}`.",
            )
        br_files = _git_diff_names(main_repo, f"{slot_mb}..{head_sha}")
        all_union_files = compute_cross_impact_files(all_union_files, br_files)
        resolved_slots.append(
            {
                "branch_name": br,
                "slot_id": int(entry.get("slot_id") or 0),
                "conversation_id": str(entry.get("conversation_id") or "").strip(),
                "head_sha": head_sha,
                "merge_base_sha": slot_mb,
                "files": br_files,
            }
        )

    included_branches = tuple(s["branch_name"] for s in resolved_slots)
    included_slot_ids = tuple(s["slot_id"] for s in resolved_slots if s["slot_id"] > 0)
    included_cids = tuple(s["conversation_id"] for s in resolved_slots if s["conversation_id"])

    # In-memory preflight probes against base and between adjacent slot heads
    for idx_s, s_obj in enumerate(resolved_slots):
        probe_base = probe_merge_tree_conflicts(
            main_repo,
            base_sha,
            s_obj["head_sha"],
            merge_base_sha=s_obj["merge_base_sha"],
        )
        if not probe_base.clean and probe_base.supported_write_tree:
            return ReleaseTrainBundleResult(
                status="ok",
                outcome=OUTCOME_TRAIN_ROLLED_BACK_CONFLICT,
                bundled=False,
                rolled_back=True,
                worktree_pristine=_is_worktree_clean(main_repo),
                train_branch=train_branch,
                train_worktree="",
                base_ref=base_ref,
                base_sha=base_sha,
                included_branches=included_branches,
                included_slot_ids=included_slot_ids,
                included_conversation_ids=included_cids,
                union_files=tuple(all_union_files),
                conflicted_branch=s_obj["branch_name"],
                conflicted_files=probe_base.conflicted_files,
                verification_passed=False,
                failure_details=probe_base.raw_output,
            )
        for other_s in resolved_slots[idx_s + 1 :]:
            probe_pair = probe_merge_tree_conflicts(main_repo, s_obj["head_sha"], other_s["head_sha"])
            if not probe_pair.clean and probe_pair.supported_write_tree:
                return ReleaseTrainBundleResult(
                    status="ok",
                    outcome=OUTCOME_TRAIN_ROLLED_BACK_CONFLICT,
                    bundled=False,
                    rolled_back=True,
                    worktree_pristine=_is_worktree_clean(main_repo),
                    train_branch=train_branch,
                    train_worktree="",
                    base_ref=base_ref,
                    base_sha=base_sha,
                    included_branches=included_branches,
                    included_slot_ids=included_slot_ids,
                    included_conversation_ids=included_cids,
                    union_files=tuple(all_union_files),
                    conflicted_branch=other_s["branch_name"],
                    conflicted_files=probe_pair.conflicted_files,
                    verification_passed=False,
                    failure_details=probe_pair.raw_output,
                )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest8 = hashlib.sha256("|".join(included_branches).encode("utf-8")).hexdigest()[:8]
    eff_train_branch = (
        str(train_branch or "").strip().removeprefix("refs/heads/")
        or f"fix/{stamp}-release-train-{len(resolved_slots)}slots-{digest8}"
    )

    if dry_run:
        return ReleaseTrainBundleResult(
            status="ok",
            outcome=OUTCOME_TRAIN_DRY_RUN_READY,
            bundled=False,
            rolled_back=False,
            worktree_pristine=_is_worktree_clean(main_repo),
            train_branch=eff_train_branch,
            train_worktree="",
            base_ref=base_ref,
            base_sha=base_sha,
            included_branches=included_branches,
            included_slot_ids=included_slot_ids,
            included_conversation_ids=included_cids,
            union_files=tuple(all_union_files),
            verification_passed=False,
        )

    if train_worktree_dir is not None and str(train_worktree_dir).strip():
        train_wt = Path(str(train_worktree_dir).replace("$HOME", str(Path.home()))).expanduser().resolve()
    else:
        base_parent = (
            Path(str(worktree_base_dir).replace("$HOME", str(Path.home()))).expanduser().resolve()
            if (worktree_base_dir is not None and str(worktree_base_dir).strip())
            else main_repo.parent
        )
        base_parent.mkdir(parents=True, exist_ok=True)
        wt_slug = _slugify_ref_token(eff_train_branch.replace("/", "-"), max_len=72)
        train_wt = (base_parent / f"pr-worktree-{main_repo.name}-{wt_slug}").resolve()

    def _rollback_train() -> None:
        discard_release_train(main_repo, train_branch=eff_train_branch, train_worktree=train_wt)

    add_proc = _run_git(
        main_repo,
        ["worktree", "add", "-b", eff_train_branch, str(train_wt), base_sha],
        timeout=30.0,
    )
    if add_proc.returncode != 0:
        _rollback_train()
        return ReleaseTrainBundleResult(
            status="error",
            outcome=OUTCOME_TRAIN_ROLLED_BACK_CONFLICT,
            bundled=False,
            rolled_back=True,
            worktree_pristine=_is_worktree_clean(main_repo),
            train_branch=eff_train_branch,
            train_worktree=_sanitize_path(str(train_wt)),
            base_ref=base_ref,
            base_sha=base_sha,
            included_branches=included_branches,
            included_slot_ids=included_slot_ids,
            included_conversation_ids=included_cids,
            union_files=tuple(all_union_files),
            error=(add_proc.stderr or add_proc.stdout or "Failed to create release train worktree.").strip(),
        )

    for s_obj in resolved_slots:
        cp_proc = _run_git(
            train_wt,
            ["cherry-pick", f"{s_obj['merge_base_sha']}..{s_obj['head_sha']}"],
            timeout=45.0,
        )
        if cp_proc.returncode != 0:
            unmerged_p = _run_git(train_wt, ["diff", "--name-only", "--diff-filter=U"], timeout=5.0)
            unmerged = [ln.strip() for ln in (unmerged_p.stdout or "").splitlines() if ln.strip()]
            parsed_c = _parse_write_tree_conflicts(f"{cp_proc.stdout or ''}\n{cp_proc.stderr or ''}")
            conf_files = sorted(set(unmerged) | set(parsed_c))
            _rollback_train()
            return ReleaseTrainBundleResult(
                status="ok",
                outcome=OUTCOME_TRAIN_ROLLED_BACK_CONFLICT,
                bundled=False,
                rolled_back=True,
                worktree_pristine=_is_worktree_clean(main_repo),
                train_branch=eff_train_branch,
                train_worktree="",
                base_ref=base_ref,
                base_sha=base_sha,
                included_branches=included_branches,
                included_slot_ids=included_slot_ids,
                included_conversation_ids=included_cids,
                union_files=tuple(all_union_files),
                conflicted_branch=s_obj["branch_name"],
                conflicted_files=tuple(conf_files),
                verification_passed=False,
                failure_details=f"{cp_proc.stdout or ''}\n{cp_proc.stderr or ''}".strip()[:3000],
            )

    head_proc = _run_git(train_wt, ["rev-parse", "HEAD"], timeout=5.0)
    train_head_sha = (head_proc.stdout or "").strip() if head_proc.returncode == 0 else ""
    final_union_files = _git_diff_names(train_wt, f"{base_sha}..{train_head_sha}") or all_union_files

    if ci_parity_verify_fn is not None:
        ver_out = ci_parity_verify_fn(train_wt)
    else:
        ver_out = _run_train_ci_parity_verification(
            train_wt,
            run_pre_commit=run_pre_commit,
            timeout=timeout,
        )

    ver_results = dict(ver_out.get("verification_results") or {})
    ver_passed = bool(ver_out.get("passed")) and bool(ver_results.get("overall_passed", True))
    failure_details = str(
        ver_out.get("failure_details") or ver_out.get("test_output") or ver_out.get("qa_output") or ""
    ).strip()

    if not ver_passed:
        _rollback_train()
        return ReleaseTrainBundleResult(
            status="ok",
            outcome=OUTCOME_TRAIN_ROLLED_BACK_VERIFICATION,
            bundled=False,
            rolled_back=True,
            worktree_pristine=_is_worktree_clean(main_repo),
            train_branch=eff_train_branch,
            train_worktree="",
            base_ref=base_ref,
            base_sha=base_sha,
            train_head_sha="",
            included_branches=included_branches,
            included_slot_ids=included_slot_ids,
            included_conversation_ids=included_cids,
            union_files=tuple(final_union_files),
            verification_passed=False,
            verification_results=ver_results,
            failure_details=failure_details,
        )

    snap = _snapshot_clean_fingerprint(train_wt, train_head_sha)
    ver_results["head_sha"] = train_head_sha
    ver_results["tree_sha"] = str(snap.get("tree_sha") or "")
    ver_results["dirty"] = False
    ver_results["content_fingerprint"] = str(snap.get("content_fingerprint") or "")
    ver_results["overall_passed"] = True
    return ReleaseTrainBundleResult(
        status="ok",
        outcome=OUTCOME_TRAIN_BUNDLED,
        bundled=True,
        rolled_back=False,
        worktree_pristine=_is_worktree_clean(train_wt),
        train_branch=eff_train_branch,
        train_worktree=_sanitize_path(str(train_wt)),
        base_ref=base_ref,
        base_sha=base_sha,
        train_head_sha=train_head_sha,
        included_branches=included_branches,
        included_slot_ids=included_slot_ids,
        included_conversation_ids=included_cids,
        union_files=tuple(final_union_files),
        verification_passed=True,
        verification_results=ver_results,
    )


def main(argv: list[str] | None = None) -> int:
    """Standalone CLI entrypoint for `rebase_gate.py` (`bundle-ready-slots`, `speculative-rebase`)."""
    parser = argparse.ArgumentParser(description="0-LLM Native Git Rebase, Speculative Pre-Rebase & Release Train Bundler.")
    sub = parser.add_subparsers(dest="subcommand")

    p_bun = sub.add_parser("bundle-ready-slots", help="Bundle 2-3 disjoint ready slot branches into one verified Release Train branch.")
    p_bun.add_argument("--workdir", required=True, help="Target git repository or worktree directory.")
    p_bun.add_argument("--branches", nargs="+", required=True, help="Ordered 2-3 ready branch names to combine.")
    p_bun.add_argument("--base", default="main", help="Base branch name (default: main).")
    p_bun.add_argument("--base-sha", dest="base_sha", default="", help="Optional explicit base SHA override (e.g. frozen Slot #1 HEAD).")
    p_bun.add_argument("--train-branch", dest="train_branch", default="", help="Optional explicit integration branch name.")
    p_bun.add_argument("--train-worktree", dest="train_worktree", default="", help="Optional explicit train worktree directory.")
    p_bun.add_argument("--fetch", action="store_true", help="Fetch origin before bundling.")
    p_bun.add_argument("--dry-run", dest="dry_run", action="store_true", help="Check conflict cleanliness without mutating git.")
    p_bun.add_argument("--with-pre-commit", dest="run_pre_commit", action="store_true", help="Include single-pass pre-commit in ci-parity gate.")

    p_spec = sub.add_parser("speculative-rebase", help="Speculatively pre-rebase a follower branch over frozen Slot #1 HEAD.")
    p_spec.add_argument("--workdir", required=True, help="Target git repository or worktree directory.")
    p_spec.add_argument("--branch", required=True, help="Follower branch name.")
    p_spec.add_argument("--slot1-ref", dest="slot1_ref", required=True, help="Frozen Slot #1 HEAD SHA or branch ref.")
    p_spec.add_argument("--no-verify", dest="no_verify", action="store_true", help="Skip cross-impact verification during speculative rebase.")

    args = parser.parse_args(argv)
    if args.subcommand == "bundle-ready-slots":
        entries = [{"branch_name": br, "slot_id": idx + 2} for idx, br in enumerate(args.branches)]
        res = bundle_ready_slots_git(
            args.workdir,
            entries,
            base_branch=args.base,
            base_sha_override=args.base_sha,
            fetch=bool(args.fetch),
            train_branch=args.train_branch,
            train_worktree_dir=args.train_worktree or None,
            dry_run=bool(args.dry_run),
            run_pre_commit=bool(args.run_pre_commit),
        )
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return 0 if res.status == "ok" else 1

    if args.subcommand == "speculative-rebase":
        res_s = speculative_pre_rebase_slot(
            args.workdir,
            follower_branch=args.branch,
            slot1_ref=args.slot1_ref,
            verify=not bool(args.no_verify),
        )
        print(json.dumps(res_s.to_dict(), indent=2, ensure_ascii=False))
        return 0 if res_s.status == "ok" else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
