#!/usr/bin/env python3
"""Background PR merge watcher, `wait-for-unlock`, and merged conversation cleanup (`merge_watcher.py`).

Extracted from `pr_remediator.py` to isolate long-lived watcher loops, AST-validated
`os.execv` auto-reload, idle/stall polling, and safe post-merge conversation cleanup.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
import contextlib
from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, TextIO
import urllib.parse

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
import dev_flow_optimizer as _flow_opt  # noqa: E402
from dev_flow_optimizer import detect_stalled_lock  # noqa: E402
import dev_queue_doctor as _queue_doctor  # noqa: E402
import git_client as _git_client  # noqa: E402
from git_client import GitVcsIntegrationClient  # noqa: E402
from lock_store import (  # noqa: E402
    SLOT_STATUS_NEEDS_REBASE_REVERIFY,
    _facade_attr,
    _inspect_local_branch_merged_readonly,
    _is_slot_scoped_rb_key,
    _read_locks_state,
    _resolve_lock_target_repo_dir,
    get_all_dev_pr_locks,
    get_dev_pr_lock,
    sync_dev_pr_lock_with_git,
)
import parallel_scope as _parallel_scope  # noqa: E402
import orchestration_control as _orch_ctrl  # noqa: E402
from queue_store import (  # noqa: E402
    DEV_QUEUE_DONE_STATUSES,
    _CONVERSATION_UUID_RE,
    _matches_cid,
    _normalize_str_list,
    get_next_dev_queue_item,
    load_dev_queue,
    update_dev_queue_item_status,
)
import rebase_gate as _rebase_gate  # noqa: E402

def _collect_watched_script_paths() -> list[Path]:
    """Collect resolved Python module paths whose updates should auto-reload long-lived watcher loops."""
    collected: list[Path] = []
    seen: set[Path] = set()

    def _add(cand: Path | str | None) -> None:
        if not cand:
            return
        with contextlib.suppress(OSError):
            p = Path(str(cand)).expanduser().resolve()
            if p.is_file() and p not in seen:
                seen.add(p)
                collected.append(p)

    _add(Path(__file__).with_name("pr_remediator.py"))
    _add(Path(__file__))
    for _sibling in ("lock_store.py", "queue_store.py", "worktree_ops.py", "review_gate.py", "merge_watcher.py", "orchestration_control.py"):
        _add(Path(__file__).with_name(_sibling))
    for mod in (_git_client, dev_common, _flow_opt, _queue_doctor, _parallel_scope, _rebase_gate, _orch_ctrl):
        mod_file = getattr(mod, "__file__", None) if mod is not None else None
        if mod_file:
            _add(mod_file)

    script_plugin_root = Path(__file__).resolve().parents[3]
    installed_plugin_root = Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin"
    for root in (script_plugin_root, installed_plugin_root):
        _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / "pr_remediator.py")
        for _mod_name in ("lock_store.py", "queue_store.py", "worktree_ops.py", "review_gate.py", "merge_watcher.py", "orchestration_control.py"):
            _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / _mod_name)
        _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / "dev_flow_optimizer.py")
        _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / "dev_queue_doctor.py")
        _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / "parallel_scope.py")
        _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / "rebase_gate.py")
        _add(root / "clients" / "dev_common.py")
        _add(root / "clients" / "git_client.py")
    return collected


def _snapshot_file_fingerprints(
    paths: Sequence[Path],
) -> dict[Path, tuple[int, int, int]]:
    """Capture `(st_ino, st_mtime_ns, st_size)` for watched Python scripts."""
    snap: dict[Path, tuple[int, int, int]] = {}
    for p in paths:
        try:
            st = p.stat()
            snap[p] = (int(st.st_ino), int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            snap[p] = (0, 0, -1)
    return snap


def _validate_python_syntax(paths: Sequence[Path]) -> tuple[bool, str]:
    """Verify every watched Python file parses cleanly via `ast.parse` before `os.execv`."""
    if not paths:
        return (False, "Reload skipped (no script paths)")
    for idx, p in enumerate(paths):
        try:
            src = p.read_text(encoding="utf-8")
            if idx == 0 and not src.strip():
                return (False, f"Reload skipped ({p.name}: empty file)")
            ast.parse(src, filename=str(p))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
            return (False, f"Reload skipped ({p.name}: {exc.__class__.__name__})")
    return (True, "")


def _build_reload_exec_argv(
    script_path: Path | None = None,
    raw_argv: Sequence[str] | None = None,
) -> list[str]:
    """Build `[sys.executable, resolved_script, *cli_args]` for `os.execv`."""
    resolved_script = str((script_path or Path(__file__).with_name("pr_remediator.py")).resolve())
    preserved = list(sys.argv[1:] if raw_argv is None else raw_argv)
    return [sys.executable, resolved_script, *preserved]


def _maybe_auto_reload_process(
    watched_paths: Sequence[Path],
    baseline_fp: dict[Path, tuple[int, int, int]],
    *,
    exec_fn: Callable[[str, list[str]], Any] | None = None,
    raw_argv: Sequence[str] | None = None,
    before_exec_cb: Callable[[], None] | None = None,
    warn_stream: TextIO | None = None,
) -> tuple[bool, dict[Path, tuple[int, int, int]], str]:
    """Compare script fingerprints and safely `os.execv` when watched Python files update cleanly."""
    cur_fp = _snapshot_file_fingerprints(watched_paths)
    if cur_fp == baseline_fp:
        return (False, baseline_fp, "")

    syntax_ok, syntax_err = _validate_python_syntax(watched_paths)
    out_err = warn_stream if warn_stream is not None else sys.stderr
    if not syntax_ok:
        with contextlib.suppress(OSError):
            out_err.write(f"[pr_remediator] WARNING: {syntax_err}\n")
            out_err.flush()
        return (False, cur_fp, syntax_err)

    script_target = watched_paths[0] if watched_paths else Path(__file__).with_name("pr_remediator.py").resolve()
    exec_argv = _build_reload_exec_argv(script_target, raw_argv)
    if before_exec_cb is not None:
        before_exec_cb()
    effective_exec_fn = exec_fn if exec_fn is not None else os.execv
    try:
        effective_exec_fn(sys.executable, exec_argv)
        return (True, cur_fp, "")
    except OSError as exc:
        err_msg = f"Reload failed ({script_target.name}: {exc.__class__.__name__})"
        with contextlib.suppress(OSError):
            out_err.write(f"[pr_remediator] WARNING: {err_msg}\n")
            out_err.flush()
        return (False, cur_fp, err_msg)


def run_pr_merge_watcher_loop(
    poll_interval_seconds: float = 15.0,
    max_duration_seconds: float = 14400.0,
    max_iterations: int | None = None,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    *,
    exec_fn: Callable[[str, list[str]], Any] | None = None,
    watched_paths_override: Sequence[Path] | None = None,
    raw_argv: Sequence[str] | None = None,
    warn_stream: TextIO | None = None,
) -> dict[str, Any]:
    """Poll `sync_dev_pr_lock_with_git()` until `dev_pr_lock` is released (PR merged into `main` or remote branch deleted), auto-reloading on clean script updates, fast-forwarding local `main`, pruning the local `fix/*` branch, and self-terminating."""
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    tmp_dir = p_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    watcher_lock_path = tmp_dir / "pr_merge_watcher.lock"

    watched_paths = (
        list(watched_paths_override)
        if watched_paths_override is not None
        else _collect_watched_script_paths()
    )
    baseline_fp = _snapshot_file_fingerprints(watched_paths)

    lock_fd = open(watcher_lock_path, "a+", encoding="utf-8")

    def _release_watcher_lock() -> None:
        if not lock_fd.closed:
            with contextlib.suppress(OSError, ValueError):
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                lock_fd.close()

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
        paused_elapsed_offset = 0.0
        iterations = 0
        last_sync: dict[str, Any] = {}

        while True:
            reloaded, baseline_fp, _ = _maybe_auto_reload_process(
                watched_paths,
                baseline_fp,
                exec_fn=exec_fn,
                raw_argv=raw_argv,
                before_exec_cb=_release_watcher_lock,
                warn_stream=warn_stream,
            )
            if reloaded:
                return {
                    "status": "ok",
                    "already_running": False,
                    "terminated": False,
                    "reloaded": True,
                    "reason": "auto_reloaded",
                    "iterations": iterations,
                    "last_sync": last_sync,
                }

            iterations += 1
            if _orch_ctrl.is_orchestration_paused(portfolio_dir=portfolio_dir):
                if max_iterations is not None and iterations >= max_iterations:
                    return {
                        "status": "ok",
                        "already_running": False,
                        "terminated": False,
                        "paused": True,
                        "reason": "paused_max_iterations_reached",
                        "released": False,
                        "iterations": iterations,
                        "last_sync": last_sync,
                    }
                pause_sleep = min(
                    max(0.05, float(poll_interval_seconds)),
                    _orch_ctrl.DEFAULT_PAUSED_POLL_INTERVAL_SECONDS,
                )
                sleep_started = time.monotonic()
                time.sleep(pause_sleep)
                paused_elapsed_offset += max(0.0, time.monotonic() - sleep_started)
                continue

            last_sync = _facade_attr("sync_dev_pr_lock_with_git", sync_dev_pr_lock_with_git)(portfolio_dir=portfolio_dir, git_client=git_client)
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
            if ((time.monotonic() - start_mono) - paused_elapsed_offset) >= max_duration_seconds:
                return {
                    "status": "ok",
                    "already_running": False,
                    "terminated": True,
                    "reason": "max_duration_reached",
                    "released": False,
                    "iterations": iterations,
                    "last_sync": last_sync,
                }
            reloaded, baseline_fp, _ = _maybe_auto_reload_process(
                watched_paths,
                baseline_fp,
                exec_fn=exec_fn,
                raw_argv=raw_argv,
                before_exec_cb=_release_watcher_lock,
                warn_stream=warn_stream,
            )
            if reloaded:
                return {
                    "status": "ok",
                    "already_running": False,
                    "terminated": False,
                    "reloaded": True,
                    "reason": "auto_reloaded",
                    "iterations": iterations,
                    "last_sync": last_sync,
                }
            time.sleep(max(0.05, float(poll_interval_seconds)))
    finally:
        _release_watcher_lock()


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
        str(Path(__file__).with_name("pr_remediator.py").resolve()),
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


MAX_SAFE_STALL_THRESHOLD_SECONDS = 3600


def wait_for_unlock_or_actionable_event(
    poll_interval_seconds: float = 15.0,
    max_duration_seconds: float = 3600.0,
    max_iterations: int | None = None,
    stall_threshold_seconds: int | float | None = None,
    allow_excessive_stall_seconds: bool = False,
    orchestrator_conversation_id: str = "",
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    brain_dir: Path | str | None = None,
    *,
    exec_fn: Callable[[str, list[str]], Any] | None = None,
    watched_paths_override: Sequence[Path] | None = None,
    raw_argv: Sequence[str] | None = None,
    warn_stream: TextIO | None = None,
) -> dict[str, Any]:
    """0-LLM reactive watcher that polls `sync_dev_pr_lock_with_git()`, auto-reloads on clean script updates, and exits the moment a repo unlocks, runnable work appears, or a worker stalls."""
    requested_stall_seconds = stall_threshold_seconds
    effective_stall_seconds = stall_threshold_seconds
    stall_threshold_clamped = False
    if (
        stall_threshold_seconds is not None
        and not allow_excessive_stall_seconds
        and float(stall_threshold_seconds) > float(MAX_SAFE_STALL_THRESHOLD_SECONDS)
    ):
        effective_stall_seconds = MAX_SAFE_STALL_THRESHOLD_SECONDS
        stall_threshold_clamped = True
        target_warn = warn_stream if warn_stream is not None else sys.stderr
        with contextlib.suppress(OSError):
            target_warn.write(
                f"[wait-for-unlock] Warning: --stall-seconds={int(round(float(stall_threshold_seconds)))} exceeds "
                f"safe ceiling ({MAX_SAFE_STALL_THRESHOLD_SECONDS}s); clamping to {MAX_SAFE_STALL_THRESHOLD_SECONDS}s "
                f"(pass --allow-excessive-stall-seconds to override).\n"
            )

    initial_locks = get_all_dev_pr_locks(portfolio_dir=portfolio_dir)
    initial_locked_repos = set(initial_locks.keys())
    initial_lock_ids = {slug: str(lk.get("lock_id") or "") for slug, lk in initial_locks.items()}

    effective_brain: Path | str | None = brain_dir
    if effective_brain is None and portfolio_dir is not None:
        # Keep isolated test/custom portfolio directories hermetic from ~/.gemini/antigravity/brain
        effective_brain = ""

    watched_paths = (
        list(watched_paths_override)
        if watched_paths_override is not None
        else _collect_watched_script_paths()
    )
    baseline_fp = _snapshot_file_fingerprints(watched_paths)

    start_mono = time.monotonic()
    paused_elapsed_offset = 0.0
    was_paused_during_wait = False
    iterations = 0

    while True:
        reloaded, baseline_fp, _ = _maybe_auto_reload_process(
            watched_paths,
            baseline_fp,
            exec_fn=exec_fn,
            raw_argv=raw_argv,
            warn_stream=warn_stream,
        )
        if reloaded:
            return {
                "status": "ok",
                "event": "auto_reloaded",
                "reloaded": True,
                "iterations": iterations,
                "elapsed_seconds": round(max(0.0, time.monotonic() - start_mono), 2),
                "rearm_recommended": False,
            }

        iterations += 1
        orch_ctrl = _orch_ctrl.load_orchestration_control(portfolio_dir=portfolio_dir)
        if bool(orch_ctrl.get("paused")):
            was_paused_during_wait = True
            if max_iterations is not None and iterations >= max_iterations:
                cur_locks_paused = get_all_dev_pr_locks(portfolio_dir=portfolio_dir)
                return {
                    "status": "ok",
                    "event": "paused_max_iterations",
                    "paused": True,
                    "orchestration_control": orch_ctrl,
                    "released_repos": [],
                    "rebase_reverify_locks": [],
                    "active_lock_repos": sorted(cur_locks_paused.keys()),
                    "active_lock_count": len(cur_locks_paused),
                    "stalled_locks": [],
                    "next_unlocked_item": None,
                    "runnable_by_repo": {},
                    "summary": {},
                    "iterations": iterations,
                    "elapsed_seconds": round(max(0.0, time.monotonic() - start_mono), 2),
                    "rearm_recommended": False,
                }
            pause_sleep = min(
                max(0.05, float(poll_interval_seconds)),
                _orch_ctrl.DEFAULT_PAUSED_POLL_INTERVAL_SECONDS,
            )
            sleep_started = time.monotonic()
            time.sleep(pause_sleep)
            paused_elapsed_offset += max(0.0, time.monotonic() - sleep_started)
            continue

        resume_triggered = bool(was_paused_during_wait or _orch_ctrl.has_unhandled_resume_request(orch_ctrl))
        if resume_triggered:
            _orch_ctrl.mark_resume_handled(
                portfolio_dir=portfolio_dir,
                resume_nonce=str(orch_ctrl.get("resume_nonce") or ""),
                handled_by="wait_for_unlock",
            )
            orch_ctrl = _orch_ctrl.load_orchestration_control(portfolio_dir=portfolio_dir)

        sync_res = _facade_attr("sync_dev_pr_lock_with_git", sync_dev_pr_lock_with_git)(portfolio_dir=portfolio_dir, git_client=git_client)
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
            slot_candidates: list[tuple[int, dict[str, Any]]] = [(1, lk)]
            s2_lk = lk.get("slots", {}).get("2") if isinstance(lk.get("slots"), dict) else None
            if isinstance(s2_lk, dict) and bool(s2_lk.get("active", True)):
                slot_candidates.append((2, s2_lk))
            for s_num, slot_entry in slot_candidates:
                slot_st_u = str(slot_entry.get("status") or "").strip().upper()
                if (
                    slot_st_u == SLOT_STATUS_NEEDS_REBASE_REVERIFY
                    or bool(slot_entry.get("needs_base_rebase"))
                    or bool(slot_entry.get("base_stale"))
                ):
                    rebase_reverify_locks.append(
                        {
                            "repo_slug": slug,
                            "slot_id": s_num,
                            "status": slot_st_u,
                            "needs_base_rebase": bool(slot_entry.get("needs_base_rebase")),
                            "base_stale": bool(slot_entry.get("base_stale")),
                            "branch_name": str(slot_entry.get("branch_name") or ""),
                            "conversation_id": str(slot_entry.get("conversation_id") or ""),
                            "worktree_path": str(slot_entry.get("worktree_path") or ""),
                            "base_sha": str(slot_entry.get("base_sha") or ""),
                        }
                    )
                st_info = detect_stalled_lock(
                    slot_entry,
                    threshold_seconds=effective_stall_seconds,
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
                            "reviewer_subagent_id": str(st_info.get("reviewer_subagent_id") or ""),
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
            out_payload: dict[str, Any] = {
                "status": "ok",
                "event": event_name,
                "paused": False,
                "orchestration_control": orch_ctrl,
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
            if stall_threshold_clamped:
                out_payload["stall_threshold_clamped"] = True
                out_payload["requested_stall_seconds"] = int(round(float(requested_stall_seconds or 0)))
                out_payload["effective_stall_seconds"] = int(round(float(effective_stall_seconds or 0)))
            return out_payload

        if resume_triggered:
            return _payload("orchestration_resumed", rearm=False)
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
        if ((time.monotonic() - start_mono) - paused_elapsed_offset) >= max_duration_seconds:
            return _payload("timeout", rearm=bool(current_locks or open_active_count > 0))

        reloaded, baseline_fp, _ = _maybe_auto_reload_process(
            watched_paths,
            baseline_fp,
            exec_fn=exec_fn,
            raw_argv=raw_argv,
            warn_stream=warn_stream,
        )
        if reloaded:
            return {
                "status": "ok",
                "event": "auto_reloaded",
                "reloaded": True,
                "iterations": iterations,
                "elapsed_seconds": round(max(0.0, time.monotonic() - start_mono), 2),
                "rearm_recommended": False,
            }

        time.sleep(max(0.05, float(poll_interval_seconds)))


_SYNTHETIC_PREFIX8_RE = re.compile(r"^([0-9a-f]{8})(?:$|-)", re.IGNORECASE)
_SUPERSEDED_NOTE_RE = re.compile(
    r"\b(?:supersed(?:ed|ing|es)?|consolidat(?:ed|ing)|absorb(?:ed|ing)|merged\s+into|duplicate\s+of)\b",
    re.IGNORECASE,
)
_QUEUE_REF_TOKEN_RE = re.compile(
    r"\b([0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|-[a-z0-9][a-z0-9_-]{1,80})?)\b",
    re.IGNORECASE,
)
DEFAULT_NOOP_ACTION_RETENTION_DAYS = 7.0


def _resolve_antigravity_dirs(
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
) -> tuple[Path, Path]:
    """Resolve primary Antigravity `brain/` and `conversations/` directories with test overrides."""
    b_dir = Path(brain_dir).expanduser() if brain_dir else (Path.home() / ".gemini" / "antigravity" / "brain")
    c_dir = (
        Path(conversations_dir).expanduser()
        if conversations_dir
        else (Path.home() / ".gemini" / "antigravity" / "conversations")
    )
    return b_dir, c_dir


def _build_antigravity_root_spec(
    label: str,
    root_dir: Path,
    *,
    brain_dir: Path | None = None,
    conversations_dir: Path | None = None,
) -> dict[str, Any]:
    """Build a normalized Antigravity app-data root descriptor (`desktop` or `cli`)."""
    b_dir = brain_dir if brain_dir is not None else (root_dir / "brain")
    c_dir = conversations_dir if conversations_dir is not None else (root_dir / "conversations")
    return {
        "label": label,
        "root_dir": root_dir,
        "brain_dir": b_dir,
        "conversations_dir": c_dir,
        "annotations_dir": root_dir / "annotations",
        "presence_dir": root_dir / "presence",
        "summaries_db": root_dir / "conversation_summaries.db",
    }


def _resolve_antigravity_roots(
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    *,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve Antigravity Desktop and CLI runtime directories while keeping custom test overrides 100% hermetic."""
    roots: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    def _append_spec(spec: dict[str, Any]) -> None:
        key = (str(spec["brain_dir"]), str(spec["conversations_dir"]))
        if key not in seen_keys:
            seen_keys.add(key)
            roots.append(spec)

    if antigravity_dirs is not None:
        for idx, raw_root in enumerate(antigravity_dirs):
            s = str(raw_root or "").strip()
            if not s:
                continue
            r_path = Path(s).expanduser()
            lbl = "cli" if ("cli" in r_path.name.lower() or idx > 0) else "desktop"
            _append_spec(_build_antigravity_root_spec(lbl, r_path))
        if cli_dir is not None and str(cli_dir).strip():
            c_root = Path(str(cli_dir).strip()).expanduser()
            _append_spec(_build_antigravity_root_spec("cli", c_root))
        return roots

    has_explicit_override = (
        brain_dir is not None
        or conversations_dir is not None
        or cli_dir is not None
    )
    if has_explicit_override:
        if brain_dir is not None or conversations_dir is not None:
            b_override = Path(brain_dir).expanduser() if brain_dir is not None else None
            c_override = Path(conversations_dir).expanduser() if conversations_dir is not None else None
            inferred_root = (
                b_override.parent
                if b_override is not None
                else (c_override.parent if c_override is not None else (Path.home() / ".gemini" / "antigravity"))
            )
            _append_spec(
                _build_antigravity_root_spec(
                    "desktop",
                    inferred_root,
                    brain_dir=b_override,
                    conversations_dir=c_override,
                )
            )
        if cli_dir is not None and str(cli_dir).strip():
            c_root = Path(str(cli_dir).strip()).expanduser()
            _append_spec(_build_antigravity_root_spec("cli", c_root))
        return roots

    env_agy = str(os.environ.get("AGY_ANTIGRAVITY_DIR") or "").strip()
    env_cli = str(os.environ.get("AGY_ANTIGRAVITY_CLI_DIR") or "").strip()
    if env_agy or env_cli:
        if env_agy:
            _append_spec(_build_antigravity_root_spec("desktop", Path(env_agy).expanduser()))
        if env_cli:
            _append_spec(_build_antigravity_root_spec("cli", Path(env_cli).expanduser()))
        return roots

    home = Path.home()
    _append_spec(_build_antigravity_root_spec("desktop", home / ".gemini" / "antigravity"))
    _append_spec(_build_antigravity_root_spec("cli", home / ".gemini" / "antigravity-cli"))
    return roots


def _dir_tree_bytes(path: Path) -> int:
    """Compute total byte size of a file or directory recursively without following symlinks."""
    try:
        if path.is_symlink():
            return path.lstat().st_size
        if not path.exists():
            return 0
        if path.is_file():
            return path.stat().st_size
        total = 0
        for f in path.rglob("*"):
            with contextlib.suppress(OSError):
                if f.is_symlink():
                    total += f.lstat().st_size
                elif f.is_file():
                    total += f.stat().st_size
        return total
    except OSError:
        return 0


def _matches_companion_artifact_name(filename: str, cid: str) -> bool:
    """Return True if `filename` in `annotations/` or `presence/` belongs to `cid` without prefix collisions."""
    low = str(filename or "").strip().lower()
    c = str(cid or "").strip().lower()
    if len(c) < 8 or not low:
        return False
    if _CONVERSATION_UUID_RE.fullmatch(c):
        return low == c or low.startswith((f"{c}.", f"{c}-", f"{c}_"))
    return low == c or low.startswith(f"{c}.")


def _collect_conversation_disk_artifacts(
    cid: str,
    roots: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Collect all existing disk paths for `cid` across `brain/`, `.db`/`.db-wal`/`.db-shm`, `.pb`, `annotations/`, and `presence/`."""
    clean = str(cid or "").strip()
    if not clean:
        return []
    artifacts: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    def _add(path: Path, kind: str, runtime: str) -> None:
        key = str(path)
        if key in seen_paths:
            return
        try:
            exists = path.is_symlink() or path.exists()
        except OSError:
            exists = False
        if not exists:
            return
        seen_paths.add(key)
        artifacts.append(
            {
                "path": path,
                "kind": kind,
                "runtime": runtime,
                "bytes": _dir_tree_bytes(path),
            }
        )

    for root in roots:
        runtime = str(root.get("label") or "desktop")
        b_dir = Path(root["brain_dir"])
        c_dir = Path(root["conversations_dir"])
        ann_dir = Path(root["annotations_dir"])
        pres_dir = Path(root["presence_dir"])

        for cand_id in dict.fromkeys((clean, clean.lower())):
            _add(b_dir / cand_id, "brain", runtime)
            for db_sfx in (".db", ".db-wal", ".db-shm", ".db-journal"):
                _add(c_dir / f"{cand_id}{db_sfx}", "conversations_db", runtime)
            _add(c_dir / f"{cand_id}.pb", "conversations_pb", runtime)

        for comp_dir, comp_kind in ((ann_dir, "annotations"), (pres_dir, "presence")):
            with contextlib.suppress(OSError):
                if comp_dir.is_dir():
                    for entry in sorted(comp_dir.iterdir(), key=lambda p: p.name):
                        if _matches_companion_artifact_name(entry.name, clean):
                            _add(entry, comp_kind, runtime)
    return artifacts


def _open_sqlite_readonly(db_path: Path) -> sqlite3.Connection | None:
    """Open a SQLite database strictly read-only without creating `-shm`/`-wal` sidecars on Linux or macOS."""
    try:
        if not db_path.is_file() or db_path.stat().st_size == 0:
            return None
        resolved = db_path.resolve()
    except OSError:
        return None

    has_live_wal_sidecar = False
    for sidecar_sfx in ("-wal", "-shm"):
        with contextlib.suppress(OSError):
            if Path(f"{resolved}{sidecar_sfx}").exists():
                has_live_wal_sidecar = True
                break

    # When neither `-wal` nor `-shm` exists (quiesced WAL DB or rollback-journal DB), open with
    # `immutable=1` first so Linux POSIX SQLite never materializes fresh `-shm`/`-wal` sidecars
    # inside a writable directory on `mode=ro`. When live WAL sidecars exist, prefer `mode=ro`
    # first so active WAL frames remain visible, falling back to `immutable=1` if `-shm` is restricted.
    queries = ("mode=ro", "mode=ro&immutable=1") if has_live_wal_sidecar else ("mode=ro&immutable=1", "mode=ro")
    quoted = urllib.parse.quote(str(resolved), safe="/:")
    for query in queries:
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{quoted}?{query}", uri=True, timeout=1.5)
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchall()
            return conn
        except sqlite3.Error:
            if conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
    return None


def _load_conversation_summaries_graph(
    roots: Sequence[Mapping[str, Any]],
    *,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Read `conversation_summaries.db` strictly read-only across runtimes, gracefully ignoring missing/corrupt DBs."""
    running_cids: set[str] = set()
    parent_by_cid: dict[str, str] = {}
    children_by_parent: dict[str, list[str]] = {}
    last_modified_by_cid: dict[str, str] = {}
    titles_by_cid: dict[str, str] = {}
    all_cids: set[str] = set()

    db_candidates: list[Path] = []
    if summaries_db is not None:
        if isinstance(summaries_db, Sequence) and not isinstance(summaries_db, (str, bytes)):
            for raw_p in summaries_db:
                if str(raw_p or "").strip():
                    db_candidates.append(Path(str(raw_p).strip()).expanduser())
        elif str(summaries_db).strip():
            db_candidates.append(Path(str(summaries_db).strip()).expanduser())
    else:
        env_db = str(os.environ.get("AGY_CONVERSATION_SUMMARIES_DB") or "").strip()
        if env_db and not roots:
            db_candidates.append(Path(env_db).expanduser())
        for r in roots:
            sdb = r.get("summaries_db")
            if sdb:
                db_candidates.append(Path(sdb))

    seen_dbs: set[str] = set()
    for cand_db in db_candidates:
        db_key = str(cand_db)
        if db_key in seen_dbs:
            continue
        seen_dbs.add(db_key)
        conn = _open_sqlite_readonly(cand_db)
        if conn is None:
            continue
        try:
            tables = {
                str(r[0] or "").strip()
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "conversation_summaries" in tables:
                cols = {
                    str(r[1] or "").strip()
                    for r in conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()
                }
                if "conversation_id" in cols:
                    sel_status = "COALESCE(status, '')" if "status" in cols else "''"
                    sel_parent = "COALESCE(parent_conversation_id, '')" if "parent_conversation_id" in cols else "''"
                    sel_mtime = "COALESCE(last_modified_time, '')" if "last_modified_time" in cols else "''"
                    sel_title = "COALESCE(title, '')" if "title" in cols else "''"
                    sql = (
                        f"SELECT conversation_id, {sel_status}, {sel_parent}, {sel_mtime}, {sel_title} "
                        "FROM conversation_summaries"
                    )
                    for row in conn.execute(sql).fetchall():
                        cid = str(row[0] or "").strip().lower()
                        if not cid:
                            continue
                        all_cids.add(cid)
                        st_val = str(row[1] or "").strip().upper()
                        par_val = str(row[2] or "").strip().lower()
                        mod_val = str(row[3] or "").strip()
                        ttl_val = str(row[4] or "").strip()
                        if st_val in ("CASCADE_RUN_STATUS_RUNNING", "RUNNING") or st_val.endswith("_RUNNING"):
                            running_cids.add(cid)
                        if par_val and par_val != cid:
                            parent_by_cid.setdefault(cid, par_val)
                            ch_list = children_by_parent.setdefault(par_val, [])
                            if cid not in ch_list:
                                ch_list.append(cid)
                        if mod_val and (cid not in last_modified_by_cid or mod_val > last_modified_by_cid[cid]):
                            last_modified_by_cid[cid] = mod_val
                        if ttl_val and cid not in titles_by_cid:
                            titles_by_cid[cid] = ttl_val

            if "conversation_parents" in tables:
                p_cols = {
                    str(r[1] or "").strip()
                    for r in conn.execute("PRAGMA table_info(conversation_parents)").fetchall()
                }
                if {"conversation_id", "parent_id"}.issubset(p_cols):
                    for prow in conn.execute("SELECT conversation_id, parent_id FROM conversation_parents").fetchall():
                        cid = str(prow[0] or "").strip().lower()
                        par_val = str(prow[1] or "").strip().lower()
                        if cid and par_val and cid != par_val:
                            all_cids.add(cid)
                            all_cids.add(par_val)
                            parent_by_cid.setdefault(cid, par_val)
                            ch_list = children_by_parent.setdefault(par_val, [])
                            if cid not in ch_list:
                                ch_list.append(cid)
        except sqlite3.Error:
            pass
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    return {
        "running_cids": running_cids,
        "parent_by_cid": parent_by_cid,
        "children_by_parent": children_by_parent,
        "last_modified_by_cid": last_modified_by_cid,
        "titles_by_cid": titles_by_cid,
        "all_cids": all_cids,
    }


def _extract_prefix8_from_synthetic_or_uuid(cid_val: str) -> str:
    """Return lowercase 8-char hex prefix if `cid_val` begins with `<hex8>` (`$` or `-`), else `''`."""
    m = _SYNTHETIC_PREFIX8_RE.match(str(cid_val or "").strip())
    return m.group(1).lower() if m else ""


def _parse_iso_epoch_seconds(raw_ts: str) -> float | None:
    """Parse an ISO-8601 / SQLite UTC timestamp into epoch seconds, returning `None` on invalid strings."""
    s = str(raw_ts or "").strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


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
    *,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    summaries_graph: Mapping[str, Any] | None = None,
) -> list[str]:
    """Traverse `transcript.jsonl` AND `conversation_summaries.db` `parent_conversation_id` downward to list child CIDs."""
    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
    )
    graph = (
        summaries_graph
        if summaries_graph is not None
        else _load_conversation_summaries_graph(roots, summaries_db=summaries_db)
    )
    children_by_parent: Mapping[str, Sequence[str]] = graph.get("children_by_parent") or {}

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

        for db_child in children_by_parent.get(cid, ()):
            clean_child = str(db_child or "").strip().lower()
            if clean_child and clean_child not in visited:
                queue.append(clean_child)

        for r_spec in roots:
            b_dir = Path(r_spec["brain_dir"])
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
    *,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    summaries_graph: Mapping[str, Any] | None = None,
) -> tuple[bool, str]:
    """Return `(True, 'idle')` only if a conversation shows no RUNNING summary/step and no recent `.db`/`.db-wal`/`.pb`/transcript activity."""
    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
    )
    cid = str(conversation_id or "").strip()
    cid_lower = cid.lower()
    if not cid_lower:
        return True, "idle"

    graph = (
        summaries_graph
        if summaries_graph is not None
        else _load_conversation_summaries_graph(roots, summaries_db=summaries_db)
    )
    if cid_lower in (graph.get("running_cids") or set()):
        return False, "not_fully_idle:cascade_run_status_running"

    cur_ts = float(now_ts if now_ts is not None else time.time())
    transcript_paths: list[Path] = []
    mtime_artifacts: list[Path] = []
    for r_spec in roots:
        b_dir = Path(r_spec["brain_dir"])
        c_dir = Path(r_spec["conversations_dir"])
        pres_dir = Path(r_spec["presence_dir"])
        for cand_id in dict.fromkeys((cid, cid_lower)):
            tp = b_dir / cand_id / ".system_generated" / "logs" / "transcript.jsonl"
            transcript_paths.append(tp)
            mtime_artifacts.append(tp)
            for ext in (".db", ".db-wal", ".db-shm", ".db-journal", ".pb"):
                mtime_artifacts.append(c_dir / f"{cand_id}{ext}")
        with contextlib.suppress(OSError):
            if pres_dir.is_dir():
                for p_entry in pres_dir.iterdir():
                    if _matches_companion_artifact_name(p_entry.name, cid_lower):
                        mtime_artifacts.append(p_entry)

    if min_idle_seconds > 0:
        seen_art: set[str] = set()
        for artifact in mtime_artifacts:
            key = str(artifact)
            if key in seen_art:
                continue
            seen_art.add(key)
            try:
                if artifact.is_file():
                    age = cur_ts - artifact.stat().st_mtime
                    if age < min_idle_seconds:
                        return False, f"not_fully_idle:recent_activity({round(age, 1)}s<{int(min_idle_seconds)}s)"
            except OSError:
                continue

    seen_tp: set[str] = set()
    for tpath in transcript_paths:
        tkey = str(tpath)
        if tkey in seen_tp or not tpath.is_file():
            continue
        seen_tp.add(tkey)
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
    *,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    summaries_graph: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    """Collect protected conversation IDs/prefixes with exact reasons (orchestrator, non-done queue/prefixes, lock, RUNNING, upward ancestors, unaudited)."""
    protected: dict[str, list[str]] = {}

    def _protect(cid_val: str, reason: str) -> None:
        clean = str(cid_val or "").strip().lower()
        if not clean:
            return
        reasons = protected.setdefault(clean, [])
        if reason not in reasons:
            reasons.append(reason)

    def _protect_with_synthetic_prefix(cid_val: str, reason: str, prefix_reason: str | None = None) -> None:
        clean = str(cid_val or "").strip().lower()
        if not clean:
            return
        _protect(clean, reason)
        if not _CONVERSATION_UUID_RE.fullmatch(clean):
            pfx8 = _extract_prefix8_from_synthetic_or_uuid(clean)
            if pfx8 and pfx8 != clean:
                _protect(pfx8, prefix_reason or reason)

    for cid in exclude_ids or ():
        _protect_with_synthetic_prefix(cid, "explicit_exclude")
    for env_key in ("ANTIGRAVITY_CONVERSATION_ID", "AGY_CONVERSATION_ID"):
        if os.environ.get(env_key):
            _protect_with_synthetic_prefix(os.environ[env_key], f"env:{env_key}")

    q_data = load_dev_queue(portfolio_dir=portfolio_dir)
    if q_data.get("orchestrator_conversation_id"):
        _protect_with_synthetic_prefix(str(q_data["orchestrator_conversation_id"]), "active_orchestrator")
    for item in q_data.get("items") or []:
        st = str(item.get("status") or "pending").lower()
        if st not in DEV_QUEUE_DONE_STATUSES:
            q_cid = str(item.get("conversation_id") or "").strip()
            _protect(q_cid, f"dev_queue:{st}")
            pfx8 = _extract_prefix8_from_synthetic_or_uuid(q_cid)
            if pfx8 and not _CONVERSATION_UUID_RE.fullmatch(q_cid):
                _protect(pfx8, f"dev_queue_prefix:{st}:{q_cid.lower()}")
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
            _protect_with_synthetic_prefix(str(active_lock.get("conversation_id") or ""), "active_dev_pr_lock")
            for single_key in ("worker_cid", "reviewer_subagent_id"):
                if active_lock.get(single_key):
                    _protect(str(active_lock[single_key]), "active_dev_pr_lock")
            for key in ("sibling_conversation_ids", "subagent_conversation_ids"):
                for cid in active_lock.get(key) or []:
                    _protect_with_synthetic_prefix(str(cid), "active_dev_pr_lock")
            slots_prot = active_lock.get("slots") if isinstance(active_lock.get("slots"), dict) else {}
            for s_obj in slots_prot.values():
                if isinstance(s_obj, dict) and bool(s_obj.get("active", True)):
                    _protect_with_synthetic_prefix(str(s_obj.get("conversation_id") or ""), "active_dev_pr_lock")
                    for single_key in ("worker_cid", "reviewer_subagent_id"):
                        if s_obj.get(single_key):
                            _protect(str(s_obj[single_key]), "active_dev_pr_lock")
                    for key in ("sibling_conversation_ids", "subagent_conversation_ids"):
                        for cid in s_obj.get(key) or []:
                            _protect_with_synthetic_prefix(str(cid), "active_dev_pr_lock")
    pending_t2 = audit_state.get("pending_tier2_proposals")
    if isinstance(pending_t2, dict):
        for cid in pending_t2:
            _protect_with_synthetic_prefix(str(cid), "pending_tier2_proposal")

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

    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
    )
    graph = (
        summaries_graph
        if summaries_graph is not None
        else _load_conversation_summaries_graph(roots, summaries_db=summaries_db)
    )
    for running_cid in sorted(graph.get("running_cids") or ()):
        _protect(running_cid, "summary_db:CASCADE_RUN_STATUS_RUNNING")

    parent_by_cid: Mapping[str, str] = graph.get("parent_by_cid") or {}
    if parent_by_cid:
        seed_keys = list(protected.keys())
        frontier: list[str] = []
        visited_up: set[str] = set()
        for p_key in seed_keys:
            if p_key in parent_by_cid and p_key not in visited_up:
                visited_up.add(p_key)
                frontier.append(p_key)
        for known_cid in parent_by_cid:
            if known_cid not in visited_up and any(_matches_cid(known_cid, p_key) for p_key in seed_keys):
                visited_up.add(known_cid)
                frontier.append(known_cid)
        while frontier:
            cur_cid = frontier.pop(0)
            anc_cid = str(parent_by_cid.get(cur_cid) or "").strip().lower()
            if anc_cid and anc_cid != cur_cid:
                _protect(anc_cid, "ancestor_of_active_conversation")
                if anc_cid not in visited_up:
                    visited_up.add(anc_cid)
                    frontier.append(anc_cid)

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


def _resolve_uuid_candidates_for_queue_id(
    raw_id: str,
    roots: Sequence[Mapping[str, Any]],
    summaries_graph: Mapping[str, Any],
) -> list[str]:
    """Resolve a queue or CLI conversation identifier (full UUID, 8-char hex, or synthetic `<hex8>-<slug>`) to lowercase root CIDs."""
    clean = str(raw_id or "").strip().lower()
    if not clean:
        return []
    if _CONVERSATION_UUID_RE.fullmatch(clean):
        return [clean]
    pfx8 = _extract_prefix8_from_synthetic_or_uuid(clean)
    if not pfx8:
        return [clean]

    matched_uuids: list[str] = []
    seen: set[str] = set()

    def _add_match(cand: str) -> None:
        c = str(cand or "").strip().lower()
        if _CONVERSATION_UUID_RE.fullmatch(c) and c.startswith(f"{pfx8}-") and c not in seen:
            seen.add(c)
            matched_uuids.append(c)

    for known_cid in sorted(summaries_graph.get("all_cids") or ()):
        _add_match(known_cid)

    for r_spec in roots:
        for dir_key in ("brain_dir", "conversations_dir", "annotations_dir", "presence_dir"):
            d_path = Path(r_spec[dir_key])
            with contextlib.suppress(OSError):
                if d_path.is_dir():
                    for entry in sorted(d_path.iterdir(), key=lambda p: p.name):
                        if entry.name.lower().startswith(f"{pfx8}-"):
                            stem = entry.name.split(".", 1)[0]
                            _add_match(stem[:36])

    return matched_uuids or [clean]


def _is_superseded_cancelled_queue_item(
    item: Mapping[str, Any],
    all_items: Sequence[Mapping[str, Any]],
) -> bool:
    """Return True when a `cancelled` queue item was superseded/consolidated and has no unmerged replacement item."""
    if str(item.get("status") or "").strip().lower() != "cancelled":
        return False
    notes = str(item.get("notes") or "").strip()
    sup_by = str(item.get("superseded_by") or "").strip()
    combined = f"{sup_by} {notes}".strip()
    if not sup_by and not _SUPERSEDED_NOTE_RE.search(notes):
        return False

    own_cid = str(item.get("conversation_id") or "").strip().lower()
    combined_lower = combined.lower()
    ref_tokens = {m.group(1).lower() for m in _QUEUE_REF_TOKEN_RE.finditer(combined_lower)}

    for other in all_items:
        other_cid = str(other.get("conversation_id") or "").strip().lower()
        if not other_cid or other_cid == own_cid:
            continue
        referenced = (
            other_cid in combined_lower
            or other_cid in ref_tokens
            or any(
                _CONVERSATION_UUID_RE.fullmatch(tok) and _matches_cid(other_cid, tok)
                for tok in ref_tokens
            )
        )
        if referenced:
            other_st = str(other.get("status") or "pending").strip().lower()
            if other_st not in ("merged", "cleaned"):
                return False
    return True


def _find_unmerged_local_branches_for_queue_item(
    item: Mapping[str, Any],
    *,
    p_dir: Path,
    git_client: GitVcsIntegrationClient | None = None,
) -> list[str]:
    """Check strictly via local Git refs (zero network calls) whether any branch on `item` still exists unmerged."""
    raw_branches = [str(b).strip() for b in (item.get("branch_names") or []) if str(b).strip()]
    if not raw_branches:
        return []
    base_vcs = git_client or GitVcsIntegrationClient()
    base_repo = base_vcs.resolve_repo_dir()
    q_repos = [str(r).strip() for r in (item.get("repo_slugs") or []) if str(r).strip()]

    def _hint_to_dir(hint: str) -> Path | None:
        clean = hint.strip()
        if not clean:
            return None
        cand = Path(clean.replace("$HOME", str(Path.home()))).expanduser()
        if (cand / ".git").exists():
            return cand
        r_name = clean.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if r_name:
            if base_repo.name == r_name and (base_repo / ".git").exists():
                return base_repo
            sib = base_repo.parent / r_name
            if (sib / ".git").exists():
                return sib
        return None

    _ = p_dir
    unmerged: list[str] = []
    for raw_br in raw_branches:
        if "::" in raw_br:
            r_hint, br_only = raw_br.split("::", 1)
            candidate_dirs = [d for d in (_hint_to_dir(r_hint),) if d is not None]
            clean_br = br_only.strip().removeprefix("refs/heads/")
        else:
            clean_br = raw_br.removeprefix("refs/heads/")
            candidate_dirs = [d for r in q_repos for d in (_hint_to_dir(r),) if d is not None]
            if not candidate_dirs and (base_repo / ".git").exists():
                candidate_dirs = [base_repo]
        for r_dir in candidate_dirs:
            merged_ok, reason = _inspect_local_branch_merged_readonly(r_dir, clean_br)
            if not merged_ok and reason == "unmerged_local_ref_exists" and clean_br not in unmerged:
                unmerged.append(clean_br)
    return unmerged


def _discover_noop_action_conversations(
    p_dir: Path,
    roots: Sequence[Mapping[str, Any]],
    summaries_graph: Mapping[str, Any],
    *,
    older_than_days: float,
    now_ts: float,
    queue_items: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """Discover idle scheduled `NO_OP` action root conversations older than `older_than_days` with zero deliverables/errors."""
    cutoff_ts = float(now_ts) - max(0.0, float(older_than_days)) * 86400.0
    parent_by_cid: Mapping[str, str] = summaries_graph.get("parent_by_cid") or {}
    last_mod_by_cid: Mapping[str, str] = summaries_graph.get("last_modified_by_cid") or {}
    titles_by_cid: Mapping[str, str] = summaries_graph.get("titles_by_cid") or {}
    running_cids: set[str] = set(summaries_graph.get("running_cids") or set())

    queue_cids = [str(it.get("conversation_id") or "").strip().lower() for it in queue_items if str(it.get("conversation_id") or "").strip()]
    discovered: list[str] = []
    seen: set[str] = set()

    def _is_older_than_cutoff(cid: str, extra_epochs: Sequence[float]) -> bool:
        epochs: list[float] = [float(e) for e in extra_epochs if e and float(e) > 0]
        mod_iso = last_mod_by_cid.get(cid, "")
        mod_ep = _parse_iso_epoch_seconds(mod_iso)
        if mod_ep is not None:
            epochs.append(mod_ep)
        for art in _collect_conversation_disk_artifacts(cid, roots):
            p = Path(art["path"])
            with contextlib.suppress(OSError):
                if p.is_symlink():
                    epochs.append(float(p.lstat().st_mtime))
                elif p.exists():
                    epochs.append(float(p.stat().st_mtime))
        if not epochs:
            return False
        return max(epochs) <= cutoff_ts

    def _matches_any_queue_item(cid: str) -> bool:
        for q_cid in queue_cids:
            if _matches_cid(cid, q_cid):
                return True
            pfx8 = _extract_prefix8_from_synthetic_or_uuid(q_cid)
            if pfx8 and cid.startswith(f"{pfx8}-"):
                return True
        return False

    ledger_db = p_dir / "actions_ledger.db"
    conn = _open_sqlite_readonly(ledger_db)
    if conn is not None:
        try:
            tables = {
                str(r[0] or "").strip()
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            deliverable_cids: set[str] = set()
            alert_cids: set[str] = set()
            if "action_deliverables_ledger" in tables:
                with contextlib.suppress(sqlite3.Error):
                    for r in conn.execute("SELECT DISTINCT LOWER(conversation_id) FROM action_deliverables_ledger").fetchall():
                        if r[0]:
                            deliverable_cids.add(str(r[0]).strip())
            if "notifications_ledger" in tables:
                with contextlib.suppress(sqlite3.Error):
                    for r in conn.execute(
                        "SELECT DISTINCT LOWER(conversation_id) FROM notifications_ledger "
                        "WHERE UPPER(COALESCE(status, '')) IN ('KO', 'ERROR', 'WARN')"
                    ).fetchall():
                        if r[0]:
                            alert_cids.add(str(r[0]).strip())

            if "transcript_parse_cache" in tables:
                cols = {
                    str(r[1] or "").strip()
                    for r in conn.execute("PRAGMA table_info(transcript_parse_cache)").fetchall()
                }
                if {"conversation_id", "is_scheduled_action", "parsed_json"}.issubset(cols):
                    sel_mtime = "COALESCE(mtime_ns, 0)" if "mtime_ns" in cols else "0"
                    sel_upd = "COALESCE(updated_at, '')" if "updated_at" in cols else "''"
                    rows = conn.execute(
                        f"SELECT conversation_id, {sel_mtime}, {sel_upd}, parsed_json "
                        "FROM transcript_parse_cache WHERE is_scheduled_action = 1"
                    ).fetchall()
                    for row in rows:
                        cid = str(row[0] or "").strip().lower()
                        if (
                            not cid
                            or cid in seen
                            or cid in running_cids
                            or cid in parent_by_cid
                            or cid in deliverable_cids
                            or cid in alert_cids
                            or _matches_any_queue_item(cid)
                        ):
                            continue
                        try:
                            parsed = json.loads(str(row[3] or "{}"))
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        if (
                            parsed.get("step_errors")
                            or parsed.get("cmd_nonzero_exits")
                            or parsed.get("last_cmd_failed_unrecovered")
                            or parsed.get("last_tool_step_failed")
                            or str(parsed.get("last_step_status") or "").strip().upper()
                            in ("ERROR", "RUNNING", "PENDING", "EXECUTING", "IN_PROGRESS", "QUEUED")
                            or str(parsed.get("validator_verdict") or "").strip().upper() == "NEEDS_REVISION"
                            or parsed.get("deliverable_urls")
                            or parsed.get("action_deliverable_op")
                            or parsed.get("sync_committed_or_pushed")
                            or parsed.get("remediation_branch")
                            or parsed.get("manual_pr_url")
                        ):
                            continue

                        explicit_outcome = str(parsed.get("outcome") or "").strip().upper()
                        if explicit_outcome:
                            if explicit_outcome not in ("NO_OP", "NO_OP_GATE", "NOOP"):
                                continue
                        else:
                            scripts = [str(s) for s in (parsed.get("scripts") or [])]
                            tools = [str(t) for t in (parsed.get("tools") or [])]
                            total_steps = int(parsed.get("total_steps") or 0)
                            act_name = str(parsed.get("detected_action") or "").strip()
                            if (
                                any(s in scripts for s in ("gmail_draft_client.py", "response_validator.py"))
                                or any("invoke_subagent" in t for t in tools)
                                or (act_name == "morning-system-health-check")
                                or (act_name == "agy-conf-sync" and total_steps >= 8)
                                or total_steps >= 12
                            ):
                                continue

                        extra_ts: list[float] = []
                        for ts_key in ("last_ts", "first_ts"):
                            ep = _parse_iso_epoch_seconds(str(parsed.get(ts_key) or ""))
                            if ep is not None:
                                extra_ts.append(ep)
                        mtime_ns = int(row[1] or 0)
                        if mtime_ns > 0:
                            extra_ts.append(mtime_ns / 1_000_000_000.0)
                        if not extra_ts:
                            upd_ep = _parse_iso_epoch_seconds(str(row[2] or ""))
                            if upd_ep is not None:
                                extra_ts.append(upd_ep)

                        if _is_older_than_cutoff(cid, extra_ts) and _collect_conversation_disk_artifacts(cid, roots):
                            seen.add(cid)
                            discovered.append(cid)
        except sqlite3.Error:
            pass
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    # Fallback discovery via `conversation_summaries.db` `Scheduled Action:` titles + clean short transcripts
    for cid, title in sorted(titles_by_cid.items()):
        if (
            cid in seen
            or cid in running_cids
            or cid in parent_by_cid
            or not str(title or "").strip().lower().startswith("scheduled action:")
            or _matches_any_queue_item(cid)
        ):
            continue
        if "morning-system-health-check" in str(title).lower():
            continue
        if not _collect_conversation_disk_artifacts(cid, roots) or not _is_older_than_cutoff(cid, ()):
            continue

        transcript_ok = True
        step_count = 0
        for r_spec in roots:
            tp = Path(r_spec["brain_dir"]) / cid / ".system_generated" / "logs" / "transcript.jsonl"
            if not tp.is_file():
                continue
            try:
                raw_txt = tp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                transcript_ok = False
                break
            if any(
                marker in raw_txt
                for marker in ("docs.google.com/", "mail.google.com/", "invoke_subagent", "gmail_draft_client.py")
            ):
                transcript_ok = False
                break
            for raw_ln in raw_txt.splitlines():
                if not raw_ln.strip():
                    continue
                step_count += 1
                try:
                    st_obj = json.loads(raw_ln)
                except json.JSONDecodeError:
                    continue
                if isinstance(st_obj, dict):
                    st_u = str(st_obj.get("status") or "").strip().upper()
                    if st_u in ("ERROR", "RUNNING", "PENDING", "EXECUTING", "IN_PROGRESS", "QUEUED"):
                        transcript_ok = False
                        break
        if transcript_ok and step_count < 12:
            seen.add(cid)
            discovered.append(cid)

    return discovered


def _empty_cleanup_breakdown() -> dict[str, Any]:
    """Build a zero-initialized MB/count breakdown structure for `cleanup_merged_conversations`."""
    artifact_keys = ("brain", "conversations_db", "conversations_pb", "annotations", "presence")
    runtime_keys = ("desktop", "cli")
    category_keys = (
        "merged_roots",
        "cleaned_leaked_roots",
        "superseded_cancelled_roots",
        "subagents",
        "noop_actions",
    )
    return {
        "total_bytes": 0,
        "total_mb": 0.0,
        "by_artifact": {k: {"count": 0, "bytes": 0, "mb": 0.0} for k in artifact_keys},
        "by_runtime": {k: {"count": 0, "bytes": 0, "mb": 0.0} for k in runtime_keys},
        "by_category": {k: {"count": 0, "bytes": 0, "mb": 0.0} for k in category_keys},
        "discovered_counts": {k: 0 for k in category_keys},
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
    *,
    all_merged: bool = False,
    include_noop_actions: bool = False,
    older_than_days: float | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Safely delete merged conversation directories/SQLite `.db`s/protobufs/annotations/presence and spawned subagents across Desktop & CLI."""
    root_cids = _normalize_str_list(conversation_ids)
    subonly_cids = _normalize_str_list(subagents_only_of)
    explicit_seed_cids = _normalize_str_list([*root_cids, *subonly_cids])
    if not explicit_seed_cids and not all_merged and not include_noop_actions:
        return {
            "status": "error",
            "error": "Provide at least one --id, --subagents-only-of, --all-merged, or --include-noop-actions conversation ID.",
        }

    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    if explicit_seed_cids or branch_specs:
        merge_check = verify_branches_merged_into_base(
            branch_specs=branch_specs,
            conversation_ids=explicit_seed_cids,
            confirm_merged=confirm_merged,
            portfolio_dir=p_dir,
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
                "breakdown": _empty_cleanup_breakdown(),
                "merge_verification": merge_check,
            }
    else:
        merge_check = {
            "all_merged": True,
            "reason": "auto_discovered_queue_and_noop",
            "checked_count": 0,
            "branches": [],
            "unmerged": [],
        }

    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
    )
    summaries_graph = _load_conversation_summaries_graph(roots, summaries_db=summaries_db)
    q_data = load_dev_queue(portfolio_dir=p_dir)
    q_items = list(q_data.get("items") or [])

    extra_protected = [*list(exclude_ids or []), *subonly_cids]
    protected_map = collect_protected_conversation_ids(
        exclude_ids=extra_protected,
        portfolio_dir=p_dir,
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
        summaries_db=summaries_db,
        summaries_graph=summaries_graph,
        include_unaudited_check=include_unaudited_check,
    )

    candidates: list[str] = []
    seen_cand: set[str] = set()
    candidate_category: dict[str, str] = {}
    breakdown = _empty_cleanup_breakdown()
    skipped: list[dict[str, Any]] = []
    queue_items_to_mark_cleaned: dict[str, list[str]] = {}

    def _add_candidate(cid_val: str, category: str) -> None:
        clean = str(cid_val or "").strip().lower()
        if clean and clean not in seen_cand:
            seen_cand.add(clean)
            candidates.append(clean)
            candidate_category[clean] = category
            if category in breakdown["discovered_counts"]:
                breakdown["discovered_counts"][category] += 1

    def _expand_children_for(parent_cid: str, child_category: str = "subagents", *, only_if_present: bool = False) -> None:
        for child in extract_spawned_subagent_ids(
            parent_cid,
            brain_dir=brain_dir,
            conversations_dir=conversations_dir,
            cli_dir=cli_dir,
            antigravity_dirs=antigravity_dirs,
            summaries_db=summaries_db,
            summaries_graph=summaries_graph,
        ):
            if only_if_present and not _collect_conversation_disk_artifacts(child, roots):
                continue
            _add_candidate(child, child_category)

    for rcid in root_cids:
        resolved_roots = _resolve_uuid_candidates_for_queue_id(rcid, roots, summaries_graph)
        for u_cid in resolved_roots:
            _add_candidate(u_cid, "merged_roots")
            if not _find_protection_reasons(u_cid, protected_map):
                _expand_children_for(u_cid, "subagents")

    for orch_cid in subonly_cids:
        for u_orch in _resolve_uuid_candidates_for_queue_id(orch_cid, roots, summaries_graph):
            _expand_children_for(u_orch, "subagents")

    if explicit_seed_cids:
        for q_it in q_items:
            q_cid_raw = str(q_it.get("conversation_id") or "").strip()
            if q_cid_raw and any(_matches_cid(q_cid_raw, s) for s in explicit_seed_cids):
                if str(q_it.get("status") or "").lower() == "merged" and any(_matches_cid(q_cid_raw, r) for r in root_cids):
                    queue_items_to_mark_cleaned.setdefault(
                        q_cid_raw,
                        _resolve_uuid_candidates_for_queue_id(q_cid_raw, roots, summaries_graph),
                    )
                if not _find_protection_reasons(q_cid_raw, protected_map):
                    for sub in q_it.get("subagent_conversation_ids") or []:
                        _add_candidate(str(sub), "subagents")

    if all_merged:
        for q_it in q_items:
            q_cid_raw = str(q_it.get("conversation_id") or "").strip()
            if not q_cid_raw:
                continue
            q_st = str(q_it.get("status") or "").strip().lower()
            if q_st == "merged":
                cat = "merged_roots"
            elif q_st == "cleaned":
                cat = "cleaned_leaked_roots"
            elif _is_superseded_cancelled_queue_item(q_it, q_items):
                cat = "superseded_cancelled_roots"
            else:
                continue

            unmerged_brs = _find_unmerged_local_branches_for_queue_item(q_it, p_dir=p_dir, git_client=git_client)
            if unmerged_brs:
                skipped.append(
                    {
                        "conversation_id": q_cid_raw.lower(),
                        "reasons": [f"unmerged_branch:{br}" for br in unmerged_brs],
                    }
                )
                for ubr in unmerged_brs:
                    merge_check["unmerged"].append({"branch_name": ubr, "reason": "unmerged_local_ref_exists"})
                continue

            resolved_roots = _resolve_uuid_candidates_for_queue_id(q_cid_raw, roots, summaries_graph)
            if q_st == "merged":
                queue_items_to_mark_cleaned.setdefault(q_cid_raw, resolved_roots)

            only_present = q_st == "cleaned"
            for u_cid in resolved_roots:
                has_disk = bool(_collect_conversation_disk_artifacts(u_cid, roots))
                if not only_present or has_disk:
                    _add_candidate(u_cid, cat)
                if not _find_protection_reasons(u_cid, protected_map):
                    _expand_children_for(u_cid, "subagents", only_if_present=only_present)

            if not _find_protection_reasons(q_cid_raw, protected_map) and all(
                not _find_protection_reasons(u_cid, protected_map) for u_cid in resolved_roots
            ):
                for sub in q_it.get("subagent_conversation_ids") or []:
                    if not only_present or _collect_conversation_disk_artifacts(str(sub), roots):
                        _add_candidate(str(sub), "subagents")

    eff_noop_days: float | None = None
    if include_noop_actions:
        eff_noop_days = float(older_than_days) if older_than_days is not None else DEFAULT_NOOP_ACTION_RETENTION_DAYS
        cur_now = float(now_ts if now_ts is not None else time.time())
        noop_roots = _discover_noop_action_conversations(
            p_dir,
            roots,
            summaries_graph,
            older_than_days=eff_noop_days,
            now_ts=cur_now,
            queue_items=q_items,
        )
        for n_cid in noop_roots:
            _add_candidate(n_cid, "noop_actions")
            if not _find_protection_reasons(n_cid, protected_map):
                _expand_children_for(n_cid, "noop_actions")
        breakdown["noop_older_than_days"] = eff_noop_days

    deleted: list[dict[str, Any]] = []
    freed_bytes = 0

    for cid in candidates:
        reasons = _find_protection_reasons(cid, protected_map)
        if reasons:
            skipped.append({"conversation_id": cid, "reasons": reasons})
            continue
        idle_ok, idle_reason = is_conversation_fully_idle(
            cid,
            brain_dir=brain_dir,
            conversations_dir=conversations_dir,
            min_idle_seconds=min_idle_seconds,
            now_ts=now_ts,
            cli_dir=cli_dir,
            antigravity_dirs=antigravity_dirs,
            summaries_db=summaries_db,
            summaries_graph=summaries_graph,
        )
        if not idle_ok:
            skipped.append({"conversation_id": cid, "reasons": [idle_reason]})
            continue

        artifacts = _collect_conversation_disk_artifacts(cid, roots)
        if not artifacts:
            skipped.append({"conversation_id": cid, "reasons": ["not_present_on_disk"]})
            continue

        sz = 0
        per_kind_bytes: dict[str, int] = {}
        touched_runtimes: set[str] = set()
        for art in artifacts:
            art_path = Path(art["path"])
            art_kind = str(art["kind"])
            art_rt = str(art["runtime"])
            art_bytes = int(art["bytes"])
            sz += art_bytes
            per_kind_bytes[art_kind] = per_kind_bytes.get(art_kind, 0) + art_bytes
            touched_runtimes.add(art_rt)
            if art_kind in breakdown["by_artifact"]:
                breakdown["by_artifact"][art_kind]["count"] += 1
                breakdown["by_artifact"][art_kind]["bytes"] += art_bytes
            if art_rt in breakdown["by_runtime"]:
                breakdown["by_runtime"][art_rt]["bytes"] += art_bytes
            if not dry_run:
                with contextlib.suppress(OSError):
                    if art_path.is_symlink() or art_path.is_file():
                        art_path.unlink(missing_ok=True)
                    elif art_path.is_dir():
                        shutil.rmtree(art_path, ignore_errors=True)

        for rt_lbl in touched_runtimes:
            if rt_lbl in breakdown["by_runtime"]:
                breakdown["by_runtime"][rt_lbl]["count"] += 1

        cat = candidate_category.get(cid, "merged_roots")
        if cat in breakdown["by_category"]:
            breakdown["by_category"][cat]["count"] += 1
            breakdown["by_category"][cat]["bytes"] += sz

        freed_bytes += sz
        deleted.append(
            {
                "conversation_id": cid,
                "bytes": sz,
                "mb": round(sz / (1024 * 1024), 4),
                "category": cat,
                "runtimes": sorted(touched_runtimes),
                "artifacts": per_kind_bytes,
            }
        )

    breakdown["total_bytes"] = freed_bytes
    breakdown["total_mb"] = round(freed_bytes / (1024 * 1024), 2)
    for bucket_name in ("by_artifact", "by_runtime", "by_category"):
        for entry in breakdown[bucket_name].values():
            entry["mb"] = round(int(entry["bytes"]) / (1024 * 1024), 2)

    deleted_ids = {d["conversation_id"] for d in deleted}
    blocked_skip_ids = {
        str(s["conversation_id"]).lower()
        for s in skipped
        if s.get("reasons") != ["not_present_on_disk"]
    }
    absent_skip_ids = {
        str(s["conversation_id"]).lower()
        for s in skipped
        if s.get("reasons") == ["not_present_on_disk"]
    }
    if mark_queue_cleaned and not dry_run:
        cleaned_queue_targets: set[str] = set()
        for rcid in root_cids:
            if rcid.lower() in deleted_ids:
                cleaned_queue_targets.add(rcid)
        for q_cid_raw, u_cids in queue_items_to_mark_cleaned.items():
            check_ids = {q_cid_raw.lower(), *(u.lower() for u in u_cids)}
            if check_ids & blocked_skip_ids:
                continue
            if (check_ids & deleted_ids) or (all_merged and u_cids and all(u.lower() in (deleted_ids | absent_skip_ids) for u in u_cids)):
                cleaned_queue_targets.add(q_cid_raw)
        for target_q_cid in sorted(cleaned_queue_targets):
            update_dev_queue_item_status(target_q_cid, "cleaned", portfolio_dir=p_dir)

    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "all_merged": bool(all_merged),
        "include_noop_actions": bool(include_noop_actions),
        "deleted_count": len(deleted),
        "deleted": [d["conversation_id"] for d in deleted],
        "deleted_details": deleted,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        "breakdown": breakdown,
        "merge_verification": merge_check,
    }


def register_watcher_cli(sub: Any) -> None:
    """Register `watch-pr-merge`, `wait-for-unlock`, and `cleanup-merged-conversations` subcommands."""
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
    p_wait.add_argument("--stall-seconds", dest="stall_seconds", type=int, default=None, help="Override stalled-worker idle threshold in seconds (default: 900, safe ceiling: 3600).")
    p_wait.add_argument("--allow-excessive-stall-seconds", dest="allow_excessive_stall_seconds", action="store_true", help="Allow --stall-seconds above the safe 3600s ceiling without clamping.")
    p_wait.add_argument("--orchestrator-id", dest="orchestrator_id", default="", help="Active orchestrator conversation ID.")
    p_wait.add_argument("--brain-dir", dest="brain_dir", default=None, help="Optional Antigravity brain directory override for worker transcript mtime checks.")

    p_clean = sub.add_parser(
        "cleanup-merged-conversations",
        help="Safely delete merged conversation directories/SQLite .db files/protobufs and spawned subagents after verifying git merge and protections.",
    )
    p_clean.add_argument("--id", "--ids", "--conversation-ids", dest="conversation_ids", nargs="*", default=[], help="Merged root conversation IDs to clean up along with spawned subagents.")
    p_clean.add_argument("--subagents-only-of", nargs="*", default=[], help="Orchestrator conversation IDs whose spawned child subagents should be cleaned up while keeping the orchestrator itself protected.")
    p_clean.add_argument("--all-merged", action="store_true", help="Auto-discover merged, previously cleaned, and superseded-cancelled queue conversations (plus orphaned subagents).")
    p_clean.add_argument("--include-noop-actions", action="store_true", help="Also discover idle scheduled NO_OP action conversations older than --older-than-days (default: 7).")
    p_clean.add_argument("--older-than-days", type=float, default=None, help="Minimum age in days for --include-noop-actions discovery (default: 7).")
    p_clean.add_argument("--branch", "--branches", dest="branches", nargs="*", default=[], help="Explicit branch names or `repo_dir::branch` specs to verify merged.")
    p_clean.add_argument("--exclude-id", "--exclude-ids", dest="exclude_ids", nargs="*", default=[], help="Additional conversation IDs to protect from deletion.")
    p_clean.add_argument("--min-idle-seconds", type=float, default=60.0, help="Minimum idle age in seconds before deleting conversation artifacts (default: 60).")
    p_clean.add_argument("--confirm-merged", action="store_true", help="Allow cleanup when merged branch refs were already pruned from origin and local.")
    p_clean.add_argument("--skip-unaudited-check", action="store_true", help="Skip conversation_analyzer check-unaudited lookup (useful in isolated tests).")
    p_clean.add_argument("--dry-run", action="store_true", help="Preview candidates and freed MB breakdown without deleting files.")


def dispatch_watcher_cli(args: Any, argv: list[str] | None = None) -> int | None:
    """Handle watcher and conversation cleanup CLI subcommands; return exit code or None if unhandled."""
    if args.subcommand == "watch-pr-merge":
        raw_cli_argv = list(sys.argv[1:]) if argv is None else list(argv)
        if getattr(args, "daemon", False):
            res = _facade_attr("spawn_pr_merge_watcher_daemon", spawn_pr_merge_watcher_daemon)(
                poll_interval_seconds=args.interval,
                max_duration_seconds=args.max_duration,
                force_spawn=True,
            )
        else:
            res = _facade_attr("run_pr_merge_watcher_loop", run_pr_merge_watcher_loop)(
                poll_interval_seconds=args.interval,
                max_duration_seconds=args.max_duration,
                max_iterations=args.max_iterations,
                raw_argv=raw_cli_argv,
            )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") in ("ok", "skipped") else 1

    if args.subcommand == "wait-for-unlock":
        raw_cli_argv = list(sys.argv[1:]) if argv is None else list(argv)
        res = _facade_attr("wait_for_unlock_or_actionable_event", wait_for_unlock_or_actionable_event)(
            poll_interval_seconds=args.interval,
            max_duration_seconds=args.max_duration,
            max_iterations=args.max_iterations,
            stall_threshold_seconds=getattr(args, "stall_seconds", None),
            allow_excessive_stall_seconds=bool(getattr(args, "allow_excessive_stall_seconds", False)),
            orchestrator_conversation_id=getattr(args, "orchestrator_id", ""),
            brain_dir=getattr(args, "brain_dir", None),
            raw_argv=raw_cli_argv,
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0 if res.get("status") == "ok" else 1

    if args.subcommand == "cleanup-merged-conversations":
        c_res = _facade_attr("cleanup_merged_conversations", cleanup_merged_conversations)(
            conversation_ids=args.conversation_ids,
            subagents_only_of=args.subagents_only_of,
            branch_specs=args.branches,
            exclude_ids=args.exclude_ids,
            min_idle_seconds=args.min_idle_seconds,
            dry_run=args.dry_run,
            confirm_merged=args.confirm_merged,
            include_unaudited_check=not args.skip_unaudited_check,
            all_merged=bool(getattr(args, "all_merged", False)),
            include_noop_actions=bool(getattr(args, "include_noop_actions", False)),
            older_than_days=getattr(args, "older_than_days", None),
        )
        print(json.dumps(c_res, indent=2, ensure_ascii=False))
        return 0 if c_res.get("status") == "ok" else 1

    return None
