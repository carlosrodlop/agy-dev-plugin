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
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from typing import Any, TextIO
import urllib.parse
import uuid

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
from conversation_service_client import (  # noqa: E402
    ConversationServiceClient,
    ConversationServiceError,
    resolve_conversation_service_config,
)
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
    _secondary_slot_keys,
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


def _is_merged_lock_sync_result(sync_res: Mapping[str, Any]) -> bool:
    """Return True when `sync_dev_pr_lock_with_git()` released one or more repository locks as merged."""
    if not sync_res or not bool(sync_res.get("released")):
        return False
    reason = str(sync_res.get("reason") or "").strip()
    if reason in _MERGED_LOCK_RELEASE_REASONS_SET or bool(sync_res.get("slot1_merged")):
        return True
    per_repo = sync_res.get("results_by_repo")
    if isinstance(per_repo, Mapping):
        for r_info in per_repo.values():
            if not isinstance(r_info, Mapping) or not bool(r_info.get("released")):
                continue
            r_reason = str(r_info.get("reason") or "").strip()
            if r_reason in _MERGED_LOCK_RELEASE_REASONS_SET or bool(r_info.get("slot1_merged")):
                return True
    return False


def _run_post_merge_auto_cleanup(
    *,
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    repos_dir: Path | str | None = None,
    now_ts: float | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute 0-LLM post-merge cleanup safely without crashing the merge watcher or touching real `$HOME` in tests."""
    eff_env = os.environ if env is None else env
    has_root_override = any(
        x is not None for x in (antigravity_dirs, brain_dir, conversations_dir, cli_dir)
    ) or bool(str(eff_env.get("AGY_ANTIGRAVITY_DIR") or "").strip() or str(eff_env.get("AGY_ANTIGRAVITY_CLI_DIR") or "").strip())
    if not has_root_override and ("unittest" in sys.modules or "pytest" in sys.modules):
        return {"status": "skipped", "reason": "hermetic_test_guard"}
    try:
        fn = _facade_attr("cleanup_merged_conversations", cleanup_merged_conversations)
        return fn(
            all_merged=True,
            include_noop_actions=True,
            include_scheduled_actions=True,
            include_sidecar_events=True,
            include_orphans=True,
            portfolio_dir=portfolio_dir,
            brain_dir=brain_dir,
            conversations_dir=conversations_dir,
            cli_dir=cli_dir,
            antigravity_dirs=antigravity_dirs,
            git_client=git_client,
            repos_dir=repos_dir,
            now_ts=now_ts,
            env=env,
        )
    except Exception as exc:
        return {"status": "error", "error": f"{exc.__class__.__name__}: {exc}"}


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
    auto_cleanup: bool = True,
    antigravity_dirs: Sequence[Path | str] | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    repos_dir: Path | str | None = None,
    now_ts: float | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Poll `sync_dev_pr_lock_with_git()` until `dev_pr_lock` is released (PR merged into `main` or remote branch deleted), auto-reloading on clean script updates, fast-forwarding local `main`, running immediate 0-LLM post-merge cleanup, and self-terminating."""
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
        last_cleanup: dict[str, Any] | None = None

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
            if auto_cleanup and _is_merged_lock_sync_result(last_sync):
                last_cleanup = _run_post_merge_auto_cleanup(
                    portfolio_dir=portfolio_dir,
                    git_client=git_client,
                    brain_dir=brain_dir,
                    conversations_dir=conversations_dir,
                    cli_dir=cli_dir,
                    antigravity_dirs=antigravity_dirs,
                    repos_dir=repos_dir,
                    now_ts=now_ts,
                    env=env,
                )
            if not last_sync.get("active"):
                out_done: dict[str, Any] = {
                    "status": "ok",
                    "already_running": False,
                    "terminated": True,
                    "reason": str(last_sync.get("reason") or "lock_inactive"),
                    "released": bool(last_sync.get("released")),
                    "iterations": iterations,
                    "last_sync": last_sync,
                }
                if last_cleanup is not None:
                    out_done["post_merge_cleanup"] = last_cleanup
                return out_done
            if max_iterations is not None and iterations >= max_iterations:
                out_max_iter: dict[str, Any] = {
                    "status": "ok",
                    "already_running": False,
                    "terminated": False,
                    "reason": "max_iterations_reached",
                    "released": False,
                    "iterations": iterations,
                    "last_sync": last_sync,
                }
                if last_cleanup is not None:
                    out_max_iter["post_merge_cleanup"] = last_cleanup
                return out_max_iter
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
            lk_slots = lk.get("slots") if isinstance(lk.get("slots"), dict) else {}
            for f_key in _secondary_slot_keys(lk_slots):
                slot_candidates.append((int(f_key), lk_slots[f_key]))
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
_FIX_BRANCH_CID8_RE = re.compile(r"\bfix/[A-Za-z0-9._-]+-([0-9a-f]{8})\b", re.IGNORECASE)
_COMPANION_UUID_FILE_RE = re.compile(
    r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.(?:pbtxt|lock)$",
    re.IGNORECASE,
)
DEFAULT_ACTION_CLEANUP_NOOP_HOURS = 24.0
DEFAULT_ACTION_CLEANUP_DELIVERABLE_HOURS = 24.0
DEFAULT_ACTION_CLEANUP_UNAUDITED_CAP_HOURS = 72.0
DEFAULT_SIDECAR_EVENT_RETENTION_HOURS = 24.0
DEFAULT_NOOP_ACTION_RETENTION_DAYS = DEFAULT_ACTION_CLEANUP_NOOP_HOURS / 24.0
DEFAULT_AUTO_ARCHIVE_ADHOC_DAYS = 3.0
STALE_STEP_RUNNING_IDLE_SECONDS = 3600.0
_MERGED_LOCK_RELEASE_REASONS_SET: frozenset[str] = frozenset(
    {
        "BRANCH_MERGED_INTO_MAIN",
        "HUMAN_VALIDATED_MANUAL_PR",
        "SLOT1_MERGED_SLOT2_PROMOTED",
    }
)
_AUDITED_RESOLVED_VERDICTS: frozenset[str] = frozenset(
    {
        "HEALED",
        "STAGE_PROPOSED",
        "APPLIED_AND_SYNCED",
        "VERIFIED_OK",
        "OBSOLETE_PRUNED",
        "OBSOLETE",
    }
)


def _resolve_cleanup_hours(
    env_var: str,
    default_hours: float,
    *,
    env: Mapping[str, str] | None = None,
) -> float:
    """Resolve a cleanup retention threshold in hours from environment or default (>= 0.0)."""
    eff_env = os.environ if env is None else env
    raw = str(eff_env.get(env_var) or "").strip()
    if raw:
        with contextlib.suppress(TypeError, ValueError):
            return max(0.0, float(raw))
    return max(0.0, float(default_hours))


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


_CONV_DB_SUFFIXES: tuple[str, ...] = (".db-journal", ".db-wal", ".db-shm", ".db")


def _build_disk_artifacts_index(roots: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Single-pass `os.scandir` index over `brain/`, `conversations/`, `annotations/`, and `presence/` across `roots`."""
    root_entries: list[dict[str, Any]] = []
    disk_present_cids: set[str] = set()
    prefix8_uuids: dict[str, list[str]] = {}
    seen_p8: dict[str, set[str]] = {}

    def _record_uuid_prefix(cand_uuid: str) -> None:
        if _CONVERSATION_UUID_RE.fullmatch(cand_uuid):
            p8 = cand_uuid[:8]
            bucket = seen_p8.setdefault(p8, set())
            if cand_uuid not in bucket:
                bucket.add(cand_uuid)
                prefix8_uuids.setdefault(p8, []).append(cand_uuid)

    for root in roots:
        runtime = str(root.get("label") or "desktop")
        b_dir = Path(root["brain_dir"])
        c_dir = Path(root["conversations_dir"])
        ann_dir = Path(root["annotations_dir"])
        pres_dir = Path(root["presence_dir"])

        brain_by_low: dict[str, Path] = {}
        with contextlib.suppress(OSError):
            if b_dir.is_dir():
                with os.scandir(b_dir) as it_b:
                    for entry in it_b:
                        low = entry.name.lower()
                        p = Path(entry.path)
                        brain_by_low[low] = p
                        if _CONVERSATION_UUID_RE.fullmatch(low):
                            with contextlib.suppress(OSError):
                                if entry.is_dir(follow_symlinks=True):
                                    disk_present_cids.add(low)
                            _record_uuid_prefix(low)

        conv_by_low: dict[str, list[tuple[Path, str]]] = {}
        with contextlib.suppress(OSError):
            if c_dir.is_dir():
                with os.scandir(c_dir) as it_c:
                    for entry in it_c:
                        name = entry.name
                        low = name.lower()
                        stem_low = low.split(".", 1)[0].strip()
                        if _CONVERSATION_UUID_RE.fullmatch(stem_low):
                            disk_present_cids.add(stem_low)
                            _record_uuid_prefix(stem_low)
                        matched_kind = ""
                        matched_stem = ""
                        for db_sfx in _CONV_DB_SUFFIXES:
                            if low.endswith(db_sfx):
                                matched_kind = "conversations_db"
                                matched_stem = low[: -len(db_sfx)]
                                break
                        if not matched_kind and low.endswith(".pb"):
                            matched_kind = "conversations_pb"
                            matched_stem = low[:-3]
                        if matched_kind and matched_stem:
                            conv_by_low.setdefault(matched_stem, []).append((Path(entry.path), matched_kind))
        for entries_list in conv_by_low.values():
            entries_list.sort(key=lambda pair: pair[0].name)

        def _scan_companion(comp_dir: Path) -> tuple[dict[str, list[Path]], list[tuple[str, Path]]]:
            by_uuid: dict[str, list[Path]] = {}
            all_list: list[tuple[str, Path]] = []
            with contextlib.suppress(OSError):
                if comp_dir.is_dir():
                    raw_names: list[tuple[str, Path]] = []
                    with os.scandir(comp_dir) as it_comp:
                        for entry in it_comp:
                            raw_names.append((entry.name, Path(entry.path)))
                    raw_names.sort(key=lambda item: item[0])
                    for orig_name, ep in raw_names:
                        low = orig_name.lower()
                        all_list.append((low, ep))
                        if len(low) >= 36 and _CONVERSATION_UUID_RE.fullmatch(low[:36]):
                            if len(low) == 36 or low[36] in (".", "-", "_"):
                                u36 = low[:36]
                                by_uuid.setdefault(u36, []).append(ep)
                                _record_uuid_prefix(u36)
            return by_uuid, all_list

        ann_by_uuid, ann_all = _scan_companion(ann_dir)
        pres_by_uuid, pres_all = _scan_companion(pres_dir)

        root_entries.append(
            {
                "runtime": runtime,
                "brain_by_low": brain_by_low,
                "conv_by_low": conv_by_low,
                "ann_by_uuid": ann_by_uuid,
                "ann_all": ann_all,
                "pres_by_uuid": pres_by_uuid,
                "pres_all": pres_all,
            }
        )

    for p8, u_list in prefix8_uuids.items():
        u_list.sort()

    return {
        "roots": root_entries,
        "disk_present_cids": disk_present_cids,
        "prefix8_uuids": prefix8_uuids,
    }


def _has_conversation_disk_artifacts(
    cid: str,
    roots: Sequence[Mapping[str, Any]],
    *,
    disk_index: Mapping[str, Any] | None = None,
) -> bool:
    """Return True when `cid` has at least one disk artifact across `roots` (O(1) with `disk_index`, zero `rglob`)."""
    clean = str(cid or "").strip()
    if not clean:
        return False
    if disk_index is None:
        return bool(_collect_conversation_disk_artifacts(clean, roots))
    c_low = clean.lower()
    is_uuid = bool(_CONVERSATION_UUID_RE.fullmatch(c_low))
    for r_idx in disk_index.get("roots") or ():
        if c_low in r_idx["brain_by_low"] or c_low in r_idx["conv_by_low"]:
            return True
        if is_uuid:
            if c_low in r_idx["ann_by_uuid"] or c_low in r_idx["pres_by_uuid"]:
                return True
        else:
            if any(_matches_companion_artifact_name(nm, c_low) for nm, _ in r_idx["ann_all"]):
                return True
            if any(_matches_companion_artifact_name(nm, c_low) for nm, _ in r_idx["pres_all"]):
                return True
    return False


def _collect_conversation_disk_artifacts(
    cid: str,
    roots: Sequence[Mapping[str, Any]],
    *,
    disk_index: Mapping[str, Any] | None = None,
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

    if disk_index is not None:
        c_low = clean.lower()
        is_uuid = bool(_CONVERSATION_UUID_RE.fullmatch(c_low))
        for r_idx in disk_index.get("roots") or ():
            runtime = str(r_idx["runtime"])
            b_path = r_idx["brain_by_low"].get(c_low)
            if b_path is not None:
                _add(b_path, "brain", runtime)
            for c_path, c_kind in r_idx["conv_by_low"].get(c_low, ()):
                _add(c_path, c_kind, runtime)
            if is_uuid:
                for ap in r_idx["ann_by_uuid"].get(c_low, ()):
                    _add(ap, "annotations", runtime)
                for pp in r_idx["pres_by_uuid"].get(c_low, ()):
                    _add(pp, "presence", runtime)
            else:
                for nm, ap in r_idx["ann_all"]:
                    if _matches_companion_artifact_name(nm, c_low):
                        _add(ap, "annotations", runtime)
                for nm, pp in r_idx["pres_all"]:
                    if _matches_companion_artifact_name(nm, c_low):
                        _add(pp, "presence", runtime)
        return artifacts

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
    status_by_cid: dict[str, str] = {}
    agent_name_by_cid: dict[str, str] = {}
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
                    sel_agent = "COALESCE(agent_name, '')" if "agent_name" in cols else "''"
                    sql = (
                        f"SELECT conversation_id, {sel_status}, {sel_parent}, {sel_mtime}, {sel_title}, {sel_agent} "
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
                        agn_val = str(row[5] or "").strip().lower()
                        if st_val in ("CASCADE_RUN_STATUS_RUNNING", "RUNNING") or st_val.endswith("_RUNNING"):
                            running_cids.add(cid)
                            status_by_cid[cid] = st_val
                        elif st_val and cid not in running_cids:
                            status_by_cid.setdefault(cid, st_val)
                        if agn_val and cid not in agent_name_by_cid:
                            agent_name_by_cid[cid] = agn_val
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
        "status_by_cid": status_by_cid,
        "agent_name_by_cid": agent_name_by_cid,
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
    disk_index: Mapping[str, Any] | None = None,
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
    if disk_index is not None:
        is_uuid = bool(_CONVERSATION_UUID_RE.fullmatch(cid_lower))
        for r_spec, r_idx in zip(roots, disk_index.get("roots") or ()):
            b_dir = Path(r_spec["brain_dir"])
            b_hit = r_idx["brain_by_low"].get(cid_lower)
            if b_hit is not None:
                tp = b_hit / ".system_generated" / "logs" / "transcript.jsonl"
                transcript_paths.append(tp)
                mtime_artifacts.append(tp)
            else:
                tp = b_dir / cid_lower / ".system_generated" / "logs" / "transcript.jsonl"
                transcript_paths.append(tp)
            for c_path, _ in r_idx["conv_by_low"].get(cid_lower, ()):
                mtime_artifacts.append(c_path)
            if is_uuid:
                mtime_artifacts.extend(r_idx["pres_by_uuid"].get(cid_lower, ()))
                mtime_artifacts.extend(r_idx["ann_by_uuid"].get(cid_lower, ()))
            else:
                for nm, pp in r_idx["pres_all"]:
                    if _matches_companion_artifact_name(nm, cid_lower):
                        mtime_artifacts.append(pp)
                for nm, ap in r_idx["ann_all"]:
                    if _matches_companion_artifact_name(nm, cid_lower):
                        mtime_artifacts.append(ap)
    else:
        for r_spec in roots:
            b_dir = Path(r_spec["brain_dir"])
            c_dir = Path(r_spec["conversations_dir"])
            ann_dir = Path(r_spec["annotations_dir"])
            pres_dir = Path(r_spec["presence_dir"])
            for cand_id in dict.fromkeys((cid, cid_lower)):
                tp = b_dir / cand_id / ".system_generated" / "logs" / "transcript.jsonl"
                transcript_paths.append(tp)
                mtime_artifacts.append(tp)
                for ext in (".db", ".db-wal", ".db-shm", ".db-journal", ".pb"):
                    mtime_artifacts.append(c_dir / f"{cand_id}{ext}")
            for comp_dir in (pres_dir, ann_dir):
                with contextlib.suppress(OSError):
                    if comp_dir.is_dir():
                        for p_entry in comp_dir.iterdir():
                            if _matches_companion_artifact_name(p_entry.name, cid_lower):
                                mtime_artifacts.append(p_entry)

    min_disk_age: float | None = None
    seen_art: set[str] = set()
    for artifact in mtime_artifacts:
        key = str(artifact)
        if key in seen_art:
            continue
        seen_art.add(key)
        try:
            if artifact.is_file():
                age = cur_ts - artifact.stat().st_mtime
                if min_disk_age is None or age < min_disk_age:
                    min_disk_age = age
                if min_idle_seconds > 0 and age < min_idle_seconds:
                    return False, f"not_fully_idle:recent_activity({round(age, 1)}s<{int(min_idle_seconds)}s)"
        except OSError:
            continue

    summary_status = str((graph.get("status_by_cid") or {}).get(cid_lower) or "").strip().upper()
    summary_reports_idle = summary_status in ("CASCADE_RUN_STATUS_IDLE", "IDLE")
    allow_stale_running_override = (
        summary_reports_idle
        and min_disk_age is not None
        and min_disk_age >= STALE_STEP_RUNNING_IDLE_SECONDS
    )

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
                        if allow_stale_running_override:
                            continue
                        return False, f"not_fully_idle:step_status_{st}"
    return True, "idle"


def _max_artifact_epoch(cid: str, roots: Sequence[Mapping[str, Any]]) -> float | None:
    """Return latest mtime epoch across existing disk artifacts for `cid`, or `None` if absent."""
    latest: float | None = None
    for art in _collect_conversation_disk_artifacts(cid, roots):
        p = Path(art["path"])
        with contextlib.suppress(OSError):
            if p.is_symlink():
                mt = float(p.lstat().st_mtime)
            elif p.exists():
                mt = float(p.stat().st_mtime)
            else:
                continue
            if latest is None or mt > latest:
                latest = mt
    return latest


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
    now_ts: float | None = None,
    env: Mapping[str, str] | None = None,
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

    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
    )
    cur_now = float(now_ts if now_ts is not None else time.time())
    unaudited_cap_seconds = (
        _resolve_cleanup_hours(
            "AGY_ACTION_CLEANUP_UNAUDITED_CAP_HOURS",
            DEFAULT_ACTION_CLEANUP_UNAUDITED_CAP_HOURS,
            env=env,
        )
        * 3600.0
    )

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
                    has_root_override = any(
                        x is not None for x in (brain_dir, conversations_dir, cli_dir, antigravity_dirs)
                    )
                    agy_dir_arg = Path(roots[0]["root_dir"]) if (has_root_override and roots) else None
                    res = ca_mod.check_unaudited_action_runs(
                        limit=100,
                        portfolio_dir=p_dir,
                        antigravity_dir=agy_dir_arg,
                        scheduled=False,
                    )
                    for key, label in (
                        ("unaudited_runs", "unaudited_run"),
                        ("grouped_incidents", "unaudited_incident"),
                        ("pending_tier2_proposals", "pending_tier2_proposal"),
                    ):
                        for item in res.get(key) or []:
                            if not isinstance(item, dict):
                                continue
                            cids_in_item: list[str] = []
                            for fld in ("conversation_id", "primary_conversation_id"):
                                if item.get(fld):
                                    cids_in_item.append(str(item[fld]))
                            for lfld in ("sibling_conversation_ids", "all_conversation_ids"):
                                for scid in item.get(lfld) or []:
                                    if scid:
                                        cids_in_item.append(str(scid))
                            if label != "pending_tier2_proposal" and unaudited_cap_seconds > 0:
                                ts_epochs: list[float] = []
                                for ts_fld in ("latest_timestamp", "timestamp", "updated_at"):
                                    ep = _parse_iso_epoch_seconds(str(item.get(ts_fld) or ""))
                                    if ep is not None:
                                        ts_epochs.append(ep)
                                for c_cand in cids_in_item:
                                    disk_ep = _max_artifact_epoch(c_cand, roots)
                                    if disk_ep is not None:
                                        ts_epochs.append(disk_ep)
                                if ts_epochs and (cur_now - max(ts_epochs)) >= unaudited_cap_seconds:
                                    continue
                            for c_val in cids_in_item:
                                _protect(c_val, label)

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
                for f_key in _secondary_slot_keys(cand_lock["slots"]):
                    s2_cand = cand_lock["slots"][f_key]
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
    *,
    disk_index: Mapping[str, Any] | None = None,
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

    if disk_index is not None:
        for cand_u in (disk_index.get("prefix8_uuids") or {}).get(pfx8, ()):
            _add_match(cand_u)
    else:
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


def _collect_existing_branch_refs_for_repo(repo_dir: Path) -> set[str] | None:
    """Return all existing `refs/heads/*` and `refs/remotes/*` refnames in `repo_dir` via one local `git for-each-ref` call."""
    if not (repo_dir / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "for-each-ref", "--format=%(refname)", "refs/heads/", "refs/remotes/"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}


def _find_unmerged_local_branches_for_queue_item(
    item: Mapping[str, Any],
    *,
    p_dir: Path,
    git_client: GitVcsIntegrationClient | None = None,
    repo_refs_cache: dict[str, set[str] | None] | None = None,
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
            if repo_refs_cache is not None:
                try:
                    r_key = str(r_dir.resolve())
                except OSError:
                    r_key = str(r_dir)
                if r_key not in repo_refs_cache:
                    repo_refs_cache[r_key] = _collect_existing_branch_refs_for_repo(r_dir)
                existing_refs = repo_refs_cache[r_key]
                if (
                    existing_refs is not None
                    and f"refs/heads/{clean_br}" not in existing_refs
                    and f"refs/remotes/origin/{clean_br}" not in existing_refs
                ):
                    continue
            merged_ok, reason = _inspect_local_branch_merged_readonly(r_dir, clean_br)
            if not merged_ok and reason == "unmerged_local_ref_exists" and clean_br not in unmerged:
                unmerged.append(clean_br)
    return unmerged


def _discover_scheduled_action_conversations(
    p_dir: Path,
    roots: Sequence[Mapping[str, Any]],
    summaries_graph: Mapping[str, Any],
    *,
    noop_hours: float,
    deliverable_hours: float,
    unaudited_cap_hours: float,
    now_ts: float,
    queue_items: Sequence[Mapping[str, Any]] = (),
    include_deliverable_and_unaudited: bool = True,
    disk_index: Mapping[str, Any] | None = None,
) -> list[tuple[str, str]]:
    """Discover idle scheduled action roots classified as `noop_actions`, `deliverable_actions`, or `unaudited_expired_actions`."""
    noop_cutoff_ts = float(now_ts) - max(0.0, float(noop_hours)) * 3600.0
    deliverable_cutoff_ts = float(now_ts) - max(0.0, float(deliverable_hours)) * 3600.0
    unaudited_cutoff_ts = float(now_ts) - max(0.0, float(unaudited_cap_hours)) * 3600.0

    parent_by_cid: Mapping[str, str] = summaries_graph.get("parent_by_cid") or {}
    last_mod_by_cid: Mapping[str, str] = summaries_graph.get("last_modified_by_cid") or {}
    titles_by_cid: Mapping[str, str] = summaries_graph.get("titles_by_cid") or {}
    running_cids: set[str] = set(summaries_graph.get("running_cids") or set())

    queue_cids = [
        str(it.get("conversation_id") or "").strip().lower()
        for it in queue_items
        if str(it.get("conversation_id") or "").strip()
    ]
    discovered: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _is_older_than_cutoff(cid: str, extra_epochs: Sequence[float], cutoff_ts: float) -> bool:
        epochs: list[float] = [float(e) for e in extra_epochs if e and float(e) > 0]
        mod_iso = last_mod_by_cid.get(cid, "")
        mod_ep = _parse_iso_epoch_seconds(mod_iso)
        if mod_ep is not None:
            epochs.append(mod_ep)
        for art in _collect_conversation_disk_artifacts(cid, roots, disk_index=disk_index):
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

    deliverable_cids: set[str] = set()
    alert_cids: set[str] = set()
    audited_resolved_cids: set[str] = set()

    state_obj = dev_common.atomic_read_json(p_dir / "actions_state.json", default={})
    audit_state = (
        state_obj.get("actions_log_audit_state")
        if isinstance(state_obj.get("actions_log_audit_state"), dict)
        else {}
    )
    legacy_audited = audit_state.get("audited_runs") if isinstance(audit_state.get("audited_runs"), dict) else {}
    for a_cid, a_rec in legacy_audited.items():
        if isinstance(a_rec, dict) and str(a_rec.get("resolution") or "").strip().upper() in _AUDITED_RESOLVED_VERDICTS:
            audited_resolved_cids.add(str(a_cid).strip().lower())

    ledger_db = p_dir / "actions_ledger.db"
    conn = _open_sqlite_readonly(ledger_db)
    if conn is not None:
        try:
            tables = {
                str(r[0] or "").strip()
                for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
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
            if "action_audit_verdicts" in tables:
                with contextlib.suppress(sqlite3.Error):
                    for r in conn.execute(
                        "SELECT LOWER(conversation_id), UPPER(COALESCE(resolution, '')) FROM action_audit_verdicts"
                    ).fetchall():
                        if r[0] and str(r[1] or "").strip() in _AUDITED_RESOLVED_VERDICTS:
                            audited_resolved_cids.add(str(r[0]).strip())

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
                            or _matches_any_queue_item(cid)
                        ):
                            continue
                        try:
                            parsed = json.loads(str(row[3] or "{}"))
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(parsed, dict):
                            continue

                        last_st = str(parsed.get("last_step_status") or "").strip().upper()
                        if last_st in ("RUNNING", "PENDING", "EXECUTING", "IN_PROGRESS", "QUEUED"):
                            continue

                        val_verdict = str(parsed.get("validator_verdict") or "").strip().upper()
                        has_warn_or_error = bool(
                            cid in alert_cids
                            or parsed.get("step_errors")
                            or parsed.get("cmd_nonzero_exits")
                            or parsed.get("last_cmd_failed_unrecovered")
                            or parsed.get("last_tool_step_failed")
                            or last_st == "ERROR"
                            or val_verdict == "NEEDS_REVISION"
                        )
                        is_audited = cid in audited_resolved_cids or cid[:8] in audited_resolved_cids

                        has_deliverable_markers = bool(
                            cid in deliverable_cids
                            or parsed.get("deliverable_urls")
                            or parsed.get("action_deliverable_op")
                            or parsed.get("sync_committed_or_pushed")
                            or parsed.get("remediation_branch")
                            or parsed.get("manual_pr_url")
                        )
                        explicit_outcome = str(parsed.get("outcome") or "").strip().upper()
                        is_explicit_noop = explicit_outcome in ("NO_OP", "NO_OP_GATE", "NOOP")
                        if not explicit_outcome:
                            scripts = [str(s) for s in (parsed.get("scripts") or [])]
                            tools = [str(t) for t in (parsed.get("tools") or [])]
                            total_steps = int(parsed.get("total_steps") or 0)
                            act_name = str(parsed.get("detected_action") or "").strip()
                            implicit_deliverable = (
                                any(s in scripts for s in ("gmail_draft_client.py", "response_validator.py"))
                                or any("invoke_subagent" in t for t in tools)
                                or (act_name == "morning-system-health-check")
                                or (act_name == "agy-conf-sync" and total_steps >= 8)
                                or total_steps >= 12
                            )
                        else:
                            implicit_deliverable = not is_explicit_noop

                        is_deliverable = has_deliverable_markers or implicit_deliverable
                        if has_warn_or_error and not is_audited:
                            if not include_deliverable_and_unaudited:
                                continue
                            cat = "unaudited_expired_actions"
                            cutoff = unaudited_cutoff_ts
                        elif is_deliverable or (has_warn_or_error and is_audited):
                            if not include_deliverable_and_unaudited:
                                continue
                            cat = "deliverable_actions"
                            cutoff = deliverable_cutoff_ts
                        else:
                            cat = "noop_actions"
                            cutoff = noop_cutoff_ts

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

                        if _is_older_than_cutoff(cid, extra_ts, cutoff) and _has_conversation_disk_artifacts(cid, roots, disk_index=disk_index):
                            seen.add(cid)
                            discovered.append((cid, cat))
        except sqlite3.Error:
            pass
        finally:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    # Fallback discovery via `conversation_summaries.db` `Scheduled Action:` titles + transcripts
    for cid, title in sorted(titles_by_cid.items()):
        if (
            cid in seen
            or cid in running_cids
            or cid in parent_by_cid
            or not str(title or "").strip().lower().startswith("scheduled action:")
            or _matches_any_queue_item(cid)
        ):
            continue
        if not _has_conversation_disk_artifacts(cid, roots, disk_index=disk_index):
            continue

        has_error_step = cid in alert_cids
        has_active_step = False
        has_deliv_marker = (
            cid in deliverable_cids
            or "morning-system-health-check" in str(title).lower()
        )
        step_count = 0
        for r_spec in roots:
            tp = Path(r_spec["brain_dir"]) / cid / ".system_generated" / "logs" / "transcript.jsonl"
            if not tp.is_file():
                continue
            try:
                raw_txt = tp.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                has_active_step = True
                break
            if any(
                marker in raw_txt
                for marker in ("docs.google.com/", "mail.google.com/", "invoke_subagent", "gmail_draft_client.py")
            ):
                has_deliv_marker = True
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
                    if st_u in ("RUNNING", "PENDING", "EXECUTING", "IN_PROGRESS", "QUEUED"):
                        has_active_step = True
                    elif st_u == "ERROR":
                        has_error_step = True
        if has_active_step:
            continue
        is_audited = cid in audited_resolved_cids or cid[:8] in audited_resolved_cids
        if has_error_step and not is_audited:
            if not include_deliverable_and_unaudited:
                continue
            cat = "unaudited_expired_actions"
            cutoff = unaudited_cutoff_ts
        elif has_deliv_marker or step_count >= 12 or (has_error_step and is_audited):
            if not include_deliverable_and_unaudited:
                continue
            cat = "deliverable_actions"
            cutoff = deliverable_cutoff_ts
        else:
            cat = "noop_actions"
            cutoff = noop_cutoff_ts

        if _is_older_than_cutoff(cid, (), cutoff):
            seen.add(cid)
            discovered.append((cid, cat))

    return discovered


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
    pairs = _discover_scheduled_action_conversations(
        p_dir,
        roots,
        summaries_graph,
        noop_hours=max(0.0, float(older_than_days)) * 24.0,
        deliverable_hours=DEFAULT_ACTION_CLEANUP_DELIVERABLE_HOURS,
        unaudited_cap_hours=DEFAULT_ACTION_CLEANUP_UNAUDITED_CAP_HOURS,
        now_ts=now_ts,
        queue_items=queue_items,
        include_deliverable_and_unaudited=False,
    )
    return [cid for cid, cat in pairs if cat == "noop_actions"]


def _collect_disk_present_cids(roots: Sequence[Mapping[str, Any]]) -> set[str]:
    """Collect lowercase CIDs/UUIDs that have `brain/` or `conversations/` artifacts on disk across `roots`."""
    present: set[str] = set()
    for r_spec in roots:
        b_dir = Path(r_spec["brain_dir"])
        with contextlib.suppress(OSError):
            if b_dir.is_dir():
                for entry in b_dir.iterdir():
                    if entry.is_dir() and _CONVERSATION_UUID_RE.fullmatch(entry.name.strip()):
                        present.add(entry.name.strip().lower())
        c_dir = Path(r_spec["conversations_dir"])
        with contextlib.suppress(OSError):
            if c_dir.is_dir():
                for entry in c_dir.iterdir():
                    stem = entry.name.split(".", 1)[0].strip()
                    if _CONVERSATION_UUID_RE.fullmatch(stem):
                        present.add(stem.lower())
    return present


def _read_transcript_head_text(cid: str, roots: Sequence[Mapping[str, Any]], max_bytes: int = 16384) -> str:
    """Read the leading `max_bytes` of `transcript.jsonl` for `cid` across `roots`."""
    for r_spec in roots:
        for cand_id in dict.fromkeys((cid, cid.lower())):
            tp = Path(r_spec["brain_dir"]) / cand_id / ".system_generated" / "logs" / "transcript.jsonl"
            if tp.is_file():
                with contextlib.suppress(OSError), open(tp, "rb") as fh:
                    return fh.read(max_bytes).decode("utf-8", errors="ignore")
    return ""


def _discover_orchestrator_roots_and_subagents(
    q_data: Mapping[str, Any],
    roots: Sequence[Mapping[str, Any]],
    summaries_graph: Mapping[str, Any],
) -> tuple[list[str], list[str]]:
    """Discover rotated idle orchestrator roots and all active/rotated orchestrator CIDs for subagent sweeps."""
    active_orch_raw = str(q_data.get("orchestrator_conversation_id") or "").strip()
    active_orch_cids: set[str] = set()
    if active_orch_raw:
        for resolved in _resolve_uuid_candidates_for_queue_id(active_orch_raw, roots, summaries_graph):
            active_orch_cids.add(resolved.lower())

    parent_by_cid: Mapping[str, str] = summaries_graph.get("parent_by_cid") or {}
    titles_by_cid: Mapping[str, str] = summaries_graph.get("titles_by_cid") or {}
    agent_name_by_cid: Mapping[str, str] = summaries_graph.get("agent_name_by_cid") or {}

    candidate_roots: set[str] = set(summaries_graph.get("all_cids") or ()) | _collect_disk_present_cids(roots)
    rotated_orchestrators: list[str] = []
    all_orchestrators: list[str] = sorted(active_orch_cids)
    seen_orch: set[str] = set(active_orch_cids)

    for cid in sorted(candidate_roots):
        c_low = cid.strip().lower()
        if not c_low or c_low in seen_orch or c_low in parent_by_cid:
            continue
        title_l = str(titles_by_cid.get(c_low) or "").strip().lower()
        agent_l = str(agent_name_by_cid.get(c_low) or "").strip().lower()
        is_orch = (
            title_l.startswith("[agy-doctor] orchestrator recovery")
            or title_l in ("main dev queue orchestrator", "dev queue orchestrator")
            or agent_l == "agy-dev-orchestrator"
        )
        if not is_orch:
            head_txt = _read_transcript_head_text(c_low, roots)
            if head_txt and (
                "[agy-doctor Auto-Wake Recovery]" in head_txt
                or ("agy-dev-orchestrator.md" in head_txt and "Previous Orchestrator CID:" in head_txt)
            ):
                is_orch = True
        if is_orch:
            seen_orch.add(c_low)
            rotated_orchestrators.append(c_low)
            all_orchestrators.append(c_low)

    return rotated_orchestrators, all_orchestrators


def _collect_repo_dirs_for_cleanup(
    p_dir: Path,
    q_data: Mapping[str, Any],
    *,
    git_client: GitVcsIntegrationClient | None = None,
    repos_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> list[Path]:
    """Collect distinct local `.git` repository directories referenced by state, queue, `repos_dir`, or `git_client`."""
    eff_env = os.environ if env is None else env
    repo_dirs: list[Path] = []
    seen_resolved: set[str] = set()

    def _add_repo(raw_path: Path | str | None) -> None:
        if raw_path is None:
            return
        s = str(raw_path).strip()
        if not s:
            return
        cand = Path(s.replace("$HOME", str(Path.home()))).expanduser()
        if not (cand / ".git").exists():
            return
        with contextlib.suppress(OSError):
            key = str(cand.resolve())
            if key not in seen_resolved:
                seen_resolved.add(key)
                repo_dirs.append(cand)

    base_repo: Path | None = None
    if git_client is not None:
        base_repo = git_client.resolve_repo_dir()
        _add_repo(base_repo)
    elif str(eff_env.get("AGY_CONF_REPO_DIR") or "").strip():
        _add_repo(str(eff_env["AGY_CONF_REPO_DIR"]).strip())
    else:
        with contextlib.suppress(Exception):
            base_repo = GitVcsIntegrationClient().resolve_repo_dir()
            _add_repo(base_repo)

    raw_repos_dir = repos_dir or str(eff_env.get("AGY_REPOS_DIR") or "").strip() or None
    if raw_repos_dir:
        r_parent = Path(str(raw_repos_dir)).expanduser()
        with contextlib.suppress(OSError):
            if r_parent.is_dir():
                for child in sorted(r_parent.iterdir(), key=lambda p: p.name):
                    if child.is_dir():
                        _add_repo(child)

    def _add_hint(hint: str) -> None:
        clean = str(hint or "").strip()
        if not clean:
            return
        _add_repo(clean)
        r_name = clean.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        if r_name and base_repo is not None:
            if base_repo.name == r_name:
                _add_repo(base_repo)
            _add_repo(base_repo.parent / r_name)

    state_obj = dev_common.atomic_read_json(p_dir / "actions_state.json", default={})
    audit_state = (
        state_obj.get("actions_log_audit_state")
        if isinstance(state_obj.get("actions_log_audit_state"), dict)
        else {}
    )
    for lock_entry in _read_locks_state(audit_state).values():
        if not isinstance(lock_entry, dict):
            continue
        for key in ("target_repo_dir", "repo_slug"):
            if lock_entry.get(key):
                _add_hint(str(lock_entry[key]))
        for rb_key in ("repo_branches", "last_repo_branches"):
            rb_map = lock_entry.get(rb_key)
            if isinstance(rb_map, dict):
                for rb_val in rb_map.values():
                    if isinstance(rb_val, dict):
                        _add_hint(str(rb_val.get("target_repo_dir") or rb_val.get("repo_slug") or ""))

    for q_it in q_data.get("items") or []:
        for slug in q_it.get("repo_slugs") or []:
            _add_hint(str(slug))

    return repo_dirs


def _collect_repo_branch_merge_index(
    p_dir: Path,
    q_data: Mapping[str, Any],
    *,
    git_client: GitVcsIntegrationClient | None = None,
    repos_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[set[str], set[str]]:
    """Return `(merged_cid8, unmerged_cid8)` across local Git repos and inactive released locks (zero network calls)."""
    merged_cid8: set[str] = set()
    unmerged_cid8: set[str] = set()

    repo_dirs = _collect_repo_dirs_for_cleanup(
        p_dir,
        q_data,
        git_client=git_client,
        repos_dir=repos_dir,
        env=env,
    )
    for r_dir in repo_dirs:
        base_ref = ""
        for cand_base in ("refs/remotes/origin/main", "refs/heads/main"):
            p_base = subprocess.run(
                ["git", "-C", str(r_dir), "rev-parse", "--verify", cand_base],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if p_base.returncode == 0:
                base_ref = cand_base
                break
        if not base_ref:
            continue

        merged_branches_in_repo: set[str] = set()
        log_proc = subprocess.run(
            ["git", "-C", str(r_dir), "log", "-n", "200", "--format=%s%n%b", base_ref],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if log_proc.returncode == 0 and log_proc.stdout:
            for match in _FIX_BRANCH_CID8_RE.finditer(log_proc.stdout):
                merged_cid8.add(match.group(1).lower())
                merged_branches_in_repo.add(match.group(0).lower())

        for merged_cmd in (
            ["git", "-C", str(r_dir), "branch", "--merged", base_ref, "--list", "fix/*"],
            ["git", "-C", str(r_dir), "branch", "-r", "--merged", base_ref, "--list", "origin/fix/*"],
        ):
            m_proc = subprocess.run(merged_cmd, capture_output=True, text=True, timeout=5, check=False)
            if m_proc.returncode == 0 and m_proc.stdout:
                for line in m_proc.stdout.splitlines():
                    br = line.strip().lstrip("*+ ").removeprefix("origin/")
                    m_cid = _FIX_BRANCH_CID8_RE.search(br)
                    if m_cid:
                        merged_cid8.add(m_cid.group(1).lower())
                        merged_branches_in_repo.add(br.lower())

        ref_proc = subprocess.run(
            ["git", "-C", str(r_dir), "for-each-ref", "--format=%(refname:short)", "refs/heads/fix/", "refs/remotes/origin/fix/"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if ref_proc.returncode == 0 and ref_proc.stdout:
            for raw_ref in ref_proc.stdout.splitlines():
                ref_s = raw_ref.strip()
                br_norm = ref_s.removeprefix("origin/")
                m_cid = _FIX_BRANCH_CID8_RE.search(br_norm)
                if not m_cid:
                    continue
                if br_norm.lower() in merged_branches_in_repo:
                    continue
                anc = subprocess.run(
                    ["git", "-C", str(r_dir), "merge-base", "--is-ancestor", ref_s, base_ref],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if anc.returncode != 0:
                    unmerged_cid8.add(m_cid.group(1).lower())

    # Include inactive released locks whose `last_branch_name` was merged into main
    state_obj = dev_common.atomic_read_json(p_dir / "actions_state.json", default={})
    audit_state = (
        state_obj.get("actions_log_audit_state")
        if isinstance(state_obj.get("actions_log_audit_state"), dict)
        else {}
    )
    for lock_entry in _read_locks_state(audit_state).values():
        if not isinstance(lock_entry, dict) or lock_entry.get("active"):
            continue
        rel_reason = str(lock_entry.get("last_release_reason") or "").strip()
        if rel_reason not in _MERGED_LOCK_RELEASE_REASONS_SET:
            continue
        last_br = str(lock_entry.get("last_branch_name") or "").strip()
        m_cid = _FIX_BRANCH_CID8_RE.search(last_br)
        if m_cid and m_cid.group(1).lower() not in unmerged_cid8:
            merged_cid8.add(m_cid.group(1).lower())
        last_cid = str(lock_entry.get("last_conversation_id") or "").strip().lower()
        if _CONVERSATION_UUID_RE.fullmatch(last_cid) and last_br.startswith("fix/") and last_cid[:8] not in unmerged_cid8:
            merged_cid8.add(last_cid[:8])

    return merged_cid8, unmerged_cid8


def _discover_non_queue_merged_conversations(
    roots: Sequence[Mapping[str, Any]],
    summaries_graph: Mapping[str, Any],
    *,
    q_items: Sequence[Mapping[str, Any]],
    merged_cid8: set[str],
    unmerged_cid8: set[str],
    exclude_cids: set[str] | None = None,
) -> list[str]:
    """Discover non-queue conversations whose `fix/*` branches are verified merged into `main`."""
    eligible_cid8 = merged_cid8 - unmerged_cid8
    if not eligible_cid8:
        return []

    queue_cids = [
        str(it.get("conversation_id") or "").strip().lower()
        for it in q_items
        if str(it.get("conversation_id") or "").strip()
    ]
    titles_by_cid: Mapping[str, str] = summaries_graph.get("titles_by_cid") or {}
    parent_by_cid: Mapping[str, str] = summaries_graph.get("parent_by_cid") or {}
    excluded = exclude_cids or set()

    disk_cids = _collect_disk_present_cids(roots)
    discovered: list[str] = []
    for cid in sorted(disk_cids):
        if cid in excluded or cid in parent_by_cid:
            continue
        if cid[:8] not in eligible_cid8:
            continue
        if any(_matches_cid(cid, q_cid) for q_cid in queue_cids):
            continue
        if str(titles_by_cid.get(cid) or "").strip().lower().startswith("scheduled action:"):
            continue
        discovered.append(cid)
    return discovered


def _prune_sidecar_events(
    roots: Sequence[Mapping[str, Any]],
    *,
    retention_hours: float,
    now_ts: float,
    dry_run: bool,
) -> dict[str, Any]:
    """Prune `<root>/sidecar_data/*/events/*.json` and `<root>/sidecar_data/*/*/events/*.json` older than `retention_hours`."""
    cutoff_ts = float(now_ts) - max(0.0, float(retention_hours)) * 3600.0
    pruned_count = 0
    pruned_bytes = 0
    seen_roots: set[str] = set()
    seen_files: set[str] = set()
    by_runtime_bytes: dict[str, int] = {"desktop": 0, "cli": 0}

    for r_spec in roots:
        runtime = str(r_spec.get("label") or "desktop")
        root_dir = Path(r_spec["root_dir"])
        r_key = str(root_dir)
        if r_key in seen_roots:
            continue
        seen_roots.add(r_key)
        sidecar_root = root_dir / "sidecar_data"
        if not sidecar_root.is_dir():
            continue

        event_dirs: list[Path] = []
        with contextlib.suppress(OSError):
            for lvl1 in sorted(sidecar_root.iterdir(), key=lambda p: p.name):
                if lvl1.is_symlink() or not lvl1.is_dir():
                    continue
                ev1 = lvl1 / "events"
                if ev1.is_dir() and not ev1.is_symlink():
                    event_dirs.append(ev1)
                with contextlib.suppress(OSError):
                    for lvl2 in sorted(lvl1.iterdir(), key=lambda p: p.name):
                        if lvl2.is_symlink() or not lvl2.is_dir():
                            continue
                        ev2 = lvl2 / "events"
                        if ev2.is_dir() and not ev2.is_symlink():
                            event_dirs.append(ev2)

        for ev_dir in event_dirs:
            with contextlib.suppress(OSError):
                for ev_file in sorted(ev_dir.iterdir(), key=lambda p: p.name):
                    if ev_file.is_symlink() or not ev_file.is_file() or ev_file.suffix.lower() != ".json":
                        continue
                    fkey = str(ev_file)
                    if fkey in seen_files:
                        continue
                    seen_files.add(fkey)
                    st = ev_file.stat()
                    if float(st.st_mtime) <= cutoff_ts:
                        sz = int(st.st_size)
                        pruned_count += 1
                        pruned_bytes += sz
                        if runtime in by_runtime_bytes:
                            by_runtime_bytes[runtime] += sz
                        if not dry_run:
                            with contextlib.suppress(OSError):
                                ev_file.unlink(missing_ok=True)

    return {
        "count": pruned_count,
        "bytes": pruned_bytes,
        "retention_hours": float(retention_hours),
        "by_runtime_bytes": by_runtime_bytes,
    }


def _sweep_orphan_companion_artifacts(
    roots: Sequence[Mapping[str, Any]],
    protected_map: dict[str, list[str]],
    *,
    min_idle_seconds: float,
    now_ts: float,
    dry_run: bool,
) -> dict[str, Any]:
    """Delete orphaned `annotations/*.pbtxt` and `presence/*.lock` whose UUID has no `conversations/` or `brain/` artifacts."""
    active_disk_cids = _collect_disk_present_cids(roots)
    pruned_count = 0
    pruned_bytes = 0
    by_artifact: dict[str, dict[str, int]] = {
        "annotations": {"count": 0, "bytes": 0},
        "presence": {"count": 0, "bytes": 0},
    }
    by_runtime_bytes: dict[str, int] = {"desktop": 0, "cli": 0}
    seen_files: set[str] = set()

    for r_spec in roots:
        runtime = str(r_spec.get("label") or "desktop")
        for dir_key, art_kind in (("annotations_dir", "annotations"), ("presence_dir", "presence")):
            comp_dir = Path(r_spec[dir_key])
            if not comp_dir.is_dir():
                continue
            with contextlib.suppress(OSError):
                for entry in sorted(comp_dir.iterdir(), key=lambda p: p.name):
                    if entry.is_symlink() or not entry.is_file():
                        continue
                    m = _COMPANION_UUID_FILE_RE.fullmatch(entry.name)
                    if not m:
                        continue
                    if art_kind == "annotations" and not entry.name.lower().endswith(".pbtxt"):
                        continue
                    if art_kind == "presence" and not entry.name.lower().endswith(".lock"):
                        continue
                    uuid_low = m.group(1).lower()
                    if uuid_low in active_disk_cids:
                        continue
                    if _find_protection_reasons(uuid_low, protected_map):
                        continue
                    ekey = str(entry)
                    if ekey in seen_files:
                        continue
                    seen_files.add(ekey)
                    st = entry.stat()
                    if min_idle_seconds > 0 and (float(now_ts) - float(st.st_mtime)) < min_idle_seconds:
                        continue
                    sz = int(st.st_size)
                    pruned_count += 1
                    pruned_bytes += sz
                    by_artifact[art_kind]["count"] += 1
                    by_artifact[art_kind]["bytes"] += sz
                    if runtime in by_runtime_bytes:
                        by_runtime_bytes[runtime] += sz
                    if not dry_run:
                        with contextlib.suppress(OSError):
                            entry.unlink(missing_ok=True)

    return {
        "count": pruned_count,
        "bytes": pruned_bytes,
        "by_artifact": by_artifact,
        "by_runtime_bytes": by_runtime_bytes,
    }


def _strip_prompt_args_from_cmd(cmd: str) -> tuple[str, list[str]]:
    """Split a `ps` command line while stripping prompt payloads so prompt text never triggers process matches."""
    raw = str(cmd or "").strip()
    if not raw:
        return ("", [])
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    cleaned: list[str] = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        low = tok.lower()
        if low in ("-p", "--prompt", "-i", "--prompt-interactive", "--prompt-file"):
            skip_next = True
            continue
        if low.startswith(("--prompt=", "--prompt-interactive=", "-p=", "-i=", "--prompt-file=")):
            continue
        cleaned.append(tok)
    exe = Path(cleaned[0]).name.lower() if cleaned else ""
    return (exe, cleaned)


def _detect_active_antigravity_processes(
    *,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
) -> list[str]:
    """Return active Antigravity Desktop / `language_server` / `agy` daemon process descriptions."""
    raw_ps = ps_output
    if raw_ps is None:
        if ps_runner is not None:
            raw_ps = ps_runner()
        else:
            try:
                proc = subprocess.run(
                    ["ps", "-eo", "pid,args"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                raw_ps = proc.stdout if proc.returncode == 0 else ""
            except (OSError, subprocess.SubprocessError):
                raw_ps = ""

    active: list[str] = []
    for line in (raw_ps or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.upper().startswith("PID "):
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid_str, cmd_str = parts[0], parts[1]
        exe, tokens = _strip_prompt_args_from_cmd(cmd_str)
        joined_low = " ".join(tokens).lower()
        first_tok_low = tokens[0].lower() if tokens else ""
        is_agy_proc = (
            "/antigravity.app/" in first_tok_low
            or "antigravity helper" in first_tok_low
            or exe in ("antigravity", "antigravity.exe", "language_server", "language_server_macos", "language_server_linux", "agy")
            or exe.startswith("language_server")
        )
        if is_agy_proc and "pr_remediator.py" not in joined_low:
            active.append(f"pid={pid_str}:{exe or first_tok_low}")
    return active


def _check_sqlite_wal_or_lock_active(db_path: Path) -> tuple[bool, str]:
    """Fail closed if a SQLite `-wal` sidecar has pending frames or a database lock is held."""
    wal_path = Path(f"{db_path}-wal")
    with contextlib.suppress(OSError):
        if wal_path.exists() and wal_path.stat().st_size > 0:
            return True, f"wal_non_empty:{wal_path.name}"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=0.0)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return False, "unlocked"
    except sqlite3.OperationalError as exc:
        return True, f"sqlite_lock_held:{exc}"
    except sqlite3.Error as exc:
        return True, f"sqlite_error:{exc}"
    finally:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


DEFAULT_GHOST_PURGE_ARM_TTL_SECONDS = 86400
GHOST_PURGE_ARM_TTL_ENV = "AGY_GHOST_PURGE_ARM_TTL_SECONDS"
GHOST_PURGE_ARMED_FILENAME = "ghost_purge_armed.json"
GHOST_PURGE_RESULT_FILENAME = "ghost_purge_last_result.json"
GHOST_PURGE_LOCK_FILENAME = "ghost_purge_armed.lock"
GHOST_PURGE_WORKER_LOCK_FILENAME = "ghost_purge_worker.lock"
GHOST_PURGE_BLOCKING_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "DEVELOPING",
        "NEEDS_REVISION_ITERATING",
        "AWAITING_AGENT_REVIEW",
    }
)
_LAYOUT_V3_KEY_PREFIX = "antigravity-multi-conversation-layout-v3-"
_LAYOUT_V3_INDEX_KEY = "antigravity-multi-conversation-layout-v3-index"
_PINNED_ORDER_KEY = "pinned_conversations_order"
_QUEUED_EXPANDED_PREFIX = "queued-messages-card-expanded-"
_ANY_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _resolve_ghost_purge_arm_ttl_seconds(
    ttl_seconds: float | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve `AGY_GHOST_PURGE_ARM_TTL_SECONDS` (default 86400s / 24h, minimum 1s)."""
    if ttl_seconds is not None and not isinstance(ttl_seconds, bool):
        with contextlib.suppress(TypeError, ValueError):
            return max(1, int(float(ttl_seconds)))
    eff_env = os.environ if env is None else env
    raw = str(eff_env.get(GHOST_PURGE_ARM_TTL_ENV) or "").strip()
    if raw:
        with contextlib.suppress(TypeError, ValueError):
            return max(1, int(float(raw)))
    return DEFAULT_GHOST_PURGE_ARM_TTL_SECONDS


def _resolve_summaries_db_candidates(
    roots: Sequence[Mapping[str, Any]],
    *,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
) -> list[Path]:
    """Resolve unique existing `conversation_summaries.db` file paths from override or roots."""
    db_candidates: list[Path] = []
    if summaries_db is not None:
        if isinstance(summaries_db, Sequence) and not isinstance(summaries_db, (str, bytes)):
            for raw_p in summaries_db:
                if str(raw_p or "").strip():
                    db_candidates.append(Path(str(raw_p).strip()).expanduser())
        elif str(summaries_db).strip():
            db_candidates.append(Path(str(summaries_db).strip()).expanduser())
    else:
        for r in roots:
            if r.get("summaries_db"):
                db_candidates.append(Path(r["summaries_db"]))

    existing_dbs: list[Path] = []
    seen_db: set[str] = set()
    for cand in db_candidates:
        key = str(cand)
        if key not in seen_db and cand.is_file():
            seen_db.add(key)
            existing_dbs.append(cand)
    return existing_dbs


def _resolve_app_storage_paths(
    roots: Sequence[Mapping[str, Any]],
    *,
    app_storage_path: Path | str | Sequence[Path | str] | None = None,
    has_explicit_root_override: bool = False,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    platform_name: str | None = None,
) -> list[Path]:
    """Resolve existing cross-platform `app_storage.json` files while keeping test overrides hermetic."""
    eff_env = os.environ if env is None else env
    candidates: list[Path] = []
    if app_storage_path is not None:
        if isinstance(app_storage_path, Sequence) and not isinstance(app_storage_path, (str, bytes)):
            for raw_p in app_storage_path:
                if str(raw_p or "").strip():
                    candidates.append(Path(str(raw_p).strip()).expanduser())
        elif str(app_storage_path).strip():
            candidates.append(Path(str(app_storage_path).strip()).expanduser())
    else:
        env_app = str(eff_env.get("AGY_APP_STORAGE_PATH") or "").strip()
        if env_app:
            candidates.append(Path(env_app).expanduser())
        else:
            for r in roots:
                root_dir = Path(r["root_dir"])
                candidates.append(root_dir / "app_storage.json")
            if not has_explicit_root_override or home_dir is not None:
                base_home = Path(home_dir).expanduser() if home_dir is not None else Path.home()
                plat = (platform_name or sys.platform).lower()
                if plat == "darwin":
                    candidates.append(base_home / "Library" / "Application Support" / "Antigravity" / "app_storage.json")
                elif plat.startswith("win"):
                    appdata = str(eff_env.get("APPDATA") or "").strip()
                    roaming = Path(appdata).expanduser() if appdata else (base_home / "AppData" / "Roaming")
                    candidates.append(roaming / "Antigravity" / "app_storage.json")
                else:
                    xdg_cfg = str(eff_env.get("XDG_CONFIG_HOME") or "").strip()
                    cfg_root = Path(xdg_cfg).expanduser() if xdg_cfg else (base_home / ".config")
                    candidates.append(cfg_root / "Antigravity" / "app_storage.json")

    existing: list[Path] = []
    seen: set[str] = set()
    for cand in candidates:
        key = str(cand)
        if key not in seen and cand.is_file():
            seen.add(key)
            existing.append(cand)
    return existing


def _has_valid_roots_on_disk(roots: Sequence[Mapping[str, Any]]) -> bool:
    """Return True when at least one Antigravity root or artifact directory exists on disk."""
    for r in roots:
        for key in ("root_dir", "brain_dir", "conversations_dir"):
            p = r.get(key)
            if p and Path(p).is_dir():
                return True
    return False


def _is_ghost_cid(
    cid: str,
    *,
    roots: Sequence[Mapping[str, Any]],
    protected_map: Mapping[str, list[str]],
    running_cids: set[str],
    disk_index: Mapping[str, Any] | None = None,
) -> bool:
    """Return True when `cid` is neither running, protected, nor backed by any disk artifact."""
    c_val = str(cid or "").strip().lower()
    if not c_val:
        return False
    if c_val in running_cids or _find_protection_reasons(c_val, protected_map):
        return False
    return not _has_conversation_disk_artifacts(c_val, roots, disk_index=disk_index)


def _scan_summaries_db_ghosts(
    db_p: Path,
    *,
    roots: Sequence[Mapping[str, Any]],
    protected_map: Mapping[str, list[str]],
    disk_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only scan of one `conversation_summaries.db` returning ghost CIDs, running CIDs, and table flags."""
    ro_conn = _open_sqlite_readonly(db_p)
    if ro_conn is None:
        return {
            "opened": False,
            "ghost_cids": [],
            "running_cids": set(),
            "has_summaries": False,
            "has_fts": False,
            "has_parents": False,
        }

    ghost_cids: list[str] = []
    running_in_db: set[str] = set()
    has_summaries = False
    has_fts = False
    has_parents = False
    try:
        tables = {
            str(r[0] or "").strip()
            for r in ro_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        has_summaries = "conversation_summaries" in tables
        has_fts = "conversations_fts" in tables
        has_parents = "conversation_parents" in tables
        db_cids: set[str] = set()
        if has_summaries:
            cols = {str(r[1] or "").strip() for r in ro_conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()}
            sel_st = "COALESCE(status, '')" if "status" in cols else "''"
            for row in ro_conn.execute(f"SELECT conversation_id, {sel_st} FROM conversation_summaries").fetchall():
                c_val = str(row[0] or "").strip().lower()
                if not c_val:
                    continue
                db_cids.add(c_val)
                st_u = str(row[1] or "").strip().upper()
                if st_u in ("CASCADE_RUN_STATUS_RUNNING", "RUNNING") or st_u.endswith("_RUNNING"):
                    running_in_db.add(c_val)
        if has_fts:
            with contextlib.suppress(sqlite3.Error):
                for row in ro_conn.execute("SELECT DISTINCT LOWER(conversation_id) FROM conversations_fts").fetchall():
                    if row[0]:
                        db_cids.add(str(row[0]).strip().lower())
        if has_parents:
            with contextlib.suppress(sqlite3.Error):
                for row in ro_conn.execute("SELECT conversation_id, parent_id FROM conversation_parents").fetchall():
                    for raw_col in row:
                        c_val = str(raw_col or "").strip().lower()
                        if c_val:
                            db_cids.add(c_val)
        eff_disk_index = disk_index if disk_index is not None else _build_disk_artifacts_index(roots)
        for c_val in sorted(db_cids):
            if _is_ghost_cid(
                c_val,
                roots=roots,
                protected_map=protected_map,
                running_cids=running_in_db,
                disk_index=eff_disk_index,
            ):
                ghost_cids.append(c_val)
    finally:
        with contextlib.suppress(sqlite3.Error):
            ro_conn.close()

    return {
        "opened": True,
        "ghost_cids": ghost_cids,
        "running_cids": running_in_db,
        "has_summaries": has_summaries,
        "has_fts": has_fts,
        "has_parents": has_parents,
    }


def _extract_uuids_from_value(value: Any) -> list[str]:
    """Extract lowercase UUID strings recursively from nested lists/strings."""
    if isinstance(value, str):
        return [u.lower() for u in _ANY_UUID_RE.findall(value)]
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        found: list[str] = []
        for item in value:
            found.extend(_extract_uuids_from_value(item))
        return found
    return []


def _parse_json_list_or_raw(raw_val: Any) -> tuple[list[Any] | None, bool]:
    """Return `(parsed_list, was_json_string)` for list-or-JSON-string values in `app_storage.json`."""
    if isinstance(raw_val, list):
        return list(raw_val), False
    if isinstance(raw_val, str):
        with contextlib.suppress(json.JSONDecodeError, TypeError, ValueError):
            parsed = json.loads(raw_val)
            if isinstance(parsed, list):
                return parsed, True
    return None, False


def _inspect_app_storage_ghosts(
    storage_path: Path,
    *,
    roots: Sequence[Mapping[str, Any]],
    protected_map: Mapping[str, list[str]],
    running_cids: set[str],
    disk_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only inspection of ghost references in `app_storage.json` returning updated dict in memory."""
    breakdown = {"pinned": 0, "layout_keys": 0, "layout_index_pairs": 0, "expanded_keys": 0}
    empty_res: dict[str, Any] = {
        "changed": False,
        "updated_data": None,
        "ghost_cids": [],
        "entries_removed": 0,
        "breakdown": breakdown,
    }
    try:
        raw_obj = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return empty_res
    if not isinstance(raw_obj, dict):
        return empty_res

    updated = dict(raw_obj)
    ghost_set: set[str] = set()
    eff_disk_index = disk_index if disk_index is not None else _build_disk_artifacts_index(roots)

    def _check_ghost(uid: str) -> bool:
        if _is_ghost_cid(
            uid,
            roots=roots,
            protected_map=protected_map,
            running_cids=running_cids,
            disk_index=eff_disk_index,
        ):
            ghost_set.add(uid.lower())
            return True
        return False

    # 1. pinned_conversations_order
    if _PINNED_ORDER_KEY in updated:
        pinned_list, was_str = _parse_json_list_or_raw(updated[_PINNED_ORDER_KEY])
        if pinned_list is not None:
            kept_pinned: list[Any] = []
            for entry in pinned_list:
                entry_uuids = _extract_uuids_from_value(entry)
                if entry_uuids and any(_check_ghost(u) for u in entry_uuids):
                    breakdown["pinned"] += 1
                else:
                    kept_pinned.append(entry)
            if breakdown["pinned"] > 0:
                updated[_PINNED_ORDER_KEY] = (
                    json.dumps(kept_pinned, separators=(",", ":"), ensure_ascii=False)
                    if was_str
                    else kept_pinned
                )

    # 2. antigravity-multi-conversation-layout-v3-index
    if _LAYOUT_V3_INDEX_KEY in updated:
        idx_list, was_str = _parse_json_list_or_raw(updated[_LAYOUT_V3_INDEX_KEY])
        if idx_list is not None:
            kept_idx: list[Any] = []
            for pair in idx_list:
                pair_uuids = _extract_uuids_from_value(pair)
                if pair_uuids and any(_check_ghost(u) for u in pair_uuids):
                    breakdown["layout_index_pairs"] += 1
                else:
                    kept_idx.append(pair)
            if breakdown["layout_index_pairs"] > 0:
                updated[_LAYOUT_V3_INDEX_KEY] = (
                    json.dumps(kept_idx, separators=(",", ":"), ensure_ascii=False)
                    if was_str
                    else kept_idx
                )

    # 3. antigravity-multi-conversation-layout-v3-<uuid>* and queued-messages-card-expanded-<uuid>
    keys_to_delete: list[str] = []
    for k in list(updated.keys()):
        if k.startswith(_LAYOUT_V3_KEY_PREFIX) and k != _LAYOUT_V3_INDEX_KEY:
            suffix = k[len(_LAYOUT_V3_KEY_PREFIX) :]
            uuids = [u.lower() for u in _ANY_UUID_RE.findall(suffix)]
            if uuids and any(_check_ghost(u) for u in uuids):
                keys_to_delete.append(k)
                breakdown["layout_keys"] += 1
        elif k.startswith(_QUEUED_EXPANDED_PREFIX):
            suffix = k[len(_QUEUED_EXPANDED_PREFIX) :]
            uuids = [u.lower() for u in _ANY_UUID_RE.findall(suffix)]
            if uuids and any(_check_ghost(u) for u in uuids):
                keys_to_delete.append(k)
                breakdown["expanded_keys"] += 1
    for k in keys_to_delete:
        updated.pop(k, None)

    entries_removed = sum(breakdown.values())
    changed = entries_removed > 0
    return {
        "changed": changed,
        "updated_data": updated if changed else None,
        "ghost_cids": sorted(ghost_set),
        "entries_removed": entries_removed,
        "breakdown": breakdown,
    }


def _prune_app_storage_ghosts(
    app_storage_paths: Sequence[Path],
    *,
    roots: Sequence[Mapping[str, Any]],
    protected_map: Mapping[str, list[str]],
    running_cids: set[str],
    dry_run: bool,
    ts_slug: str,
    disk_index: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Prune ghost UUIDs from `app_storage.json` with `0600` timestamped backup and atomic replace."""
    agg_breakdown = {"pinned": 0, "layout_keys": 0, "layout_index_pairs": 0, "expanded_keys": 0}
    backups: list[str] = []
    ghost_cids: set[str] = set()
    files_modified = 0
    entries_removed = 0
    eff_disk_index = disk_index if disk_index is not None else _build_disk_artifacts_index(roots)

    for storage_path in app_storage_paths:
        inspected = _inspect_app_storage_ghosts(
            storage_path,
            roots=roots,
            protected_map=protected_map,
            running_cids=running_cids,
            disk_index=eff_disk_index,
        )
        if not inspected["changed"]:
            continue
        files_modified += 1
        entries_removed += int(inspected["entries_removed"])
        ghost_cids.update(inspected["ghost_cids"])
        for bk, bv in inspected["breakdown"].items():
            agg_breakdown[bk] = agg_breakdown.get(bk, 0) + int(bv)
        if not dry_run and inspected["updated_data"] is not None:
            backup_path = storage_path.with_name(f"{storage_path.name}.bak.{ts_slug}")
            shutil.copy2(storage_path, backup_path)
            with contextlib.suppress(OSError):
                os.chmod(backup_path, 0o600)
            backups.append(str(backup_path))
            dev_common.atomic_write_json(storage_path, inspected["updated_data"], mode=0o600)

    return {
        "files_checked": len(app_storage_paths),
        "files_modified": files_modified,
        "backups": backups,
        "ghost_cids": sorted(ghost_cids),
        "entries_removed": entries_removed,
        "breakdown": agg_breakdown,
    }


def _infer_antigravity_dirs_from_summaries_db(
    *,
    brain_dir: Path | str | None,
    conversations_dir: Path | str | None,
    cli_dir: Path | str | None,
    antigravity_dirs: Sequence[Path | str] | None,
    summaries_db: Path | str | Sequence[Path | str] | None,
) -> list[Path] | None:
    """Infer hermetic Antigravity root directories from explicit `summaries_db` when no root override is given."""
    if any(x is not None for x in (brain_dir, conversations_dir, cli_dir, antigravity_dirs)):
        return None
    if summaries_db is None:
        return None
    if str(os.environ.get("AGY_ANTIGRAVITY_DIR") or "").strip() or str(os.environ.get("AGY_ANTIGRAVITY_CLI_DIR") or "").strip():
        return None
    raw_items: Sequence[Path | str] = (
        summaries_db
        if isinstance(summaries_db, Sequence) and not isinstance(summaries_db, (str, bytes))
        else [summaries_db]
    )
    inferred: list[Path] = []
    seen: set[str] = set()
    for raw_p in raw_items:
        s = str(raw_p or "").strip()
        if not s:
            continue
        parent = Path(s).expanduser().parent
        key = str(parent)
        if key not in seen:
            seen.add(key)
            inferred.append(parent)
    return inferred or None


def count_ghost_sidebar_entries(
    *,
    portfolio_dir: Path | str | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    app_storage_path: Path | str | Sequence[Path | str] | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Read-only count of ghost sidebar entries across `conversation_summaries.db` and `app_storage.json` (zero writes)."""
    eff_agy_dirs = antigravity_dirs
    if eff_agy_dirs is None:
        eff_agy_dirs = _infer_antigravity_dirs_from_summaries_db(
            brain_dir=brain_dir,
            conversations_dir=conversations_dir,
            cli_dir=cli_dir,
            antigravity_dirs=antigravity_dirs,
            summaries_db=summaries_db,
        )
    has_explicit_root_override = any(
        x is not None for x in (brain_dir, conversations_dir, cli_dir, eff_agy_dirs)
    )
    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=eff_agy_dirs,
    )
    existing_dbs = _resolve_summaries_db_candidates(roots, summaries_db=summaries_db)
    existing_app_storage = _resolve_app_storage_paths(
        roots,
        app_storage_path=app_storage_path,
        has_explicit_root_override=has_explicit_root_override,
        home_dir=home_dir,
        env=env,
    )

    protected_map = collect_protected_conversation_ids(
        portfolio_dir=portfolio_dir,
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=eff_agy_dirs,
        summaries_db=existing_dbs,
        include_unaudited_check=False,
        now_ts=now_ts,
        env=env,
    )

    disk_index = _build_disk_artifacts_index(roots)
    db_ghosts: set[str] = set()
    running_cids: set[str] = set()
    for db_p in existing_dbs:
        scan = _scan_summaries_db_ghosts(db_p, roots=roots, protected_map=protected_map, disk_index=disk_index)
        db_ghosts.update(scan["ghost_cids"])
        running_cids.update(scan["running_cids"])

    app_ghosts: set[str] = set()
    app_entries = 0
    app_breakdown = {"pinned": 0, "layout_keys": 0, "layout_index_pairs": 0, "expanded_keys": 0}
    if existing_app_storage and (_has_valid_roots_on_disk(roots) or bool(existing_dbs)):
        for st_p in existing_app_storage:
            insp = _inspect_app_storage_ghosts(
                st_p,
                roots=roots,
                protected_map=protected_map,
                running_cids=running_cids,
                disk_index=disk_index,
            )
            app_ghosts.update(insp["ghost_cids"])
            app_entries += int(insp["entries_removed"])
            for bk, bv in insp["breakdown"].items():
                app_breakdown[bk] = app_breakdown.get(bk, 0) + int(bv)

    all_ghosts = sorted(db_ghosts | app_ghosts)
    return {
        "status": "ok",
        "total_ghost_cids": len(all_ghosts),
        "ghost_cids": all_ghosts,
        "db_ghost_count": len(db_ghosts),
        "db_ghost_cids": sorted(db_ghosts),
        "app_storage_ghost_count": len(app_ghosts),
        "app_storage_ghost_cids": sorted(app_ghosts),
        "app_storage_entries_count": app_entries,
        "app_storage_breakdown": app_breakdown,
        "has_ghosts": bool(all_ghosts or app_entries > 0),
        "dbs_checked": len(existing_dbs),
        "app_storage_files_checked": len(existing_app_storage),
    }


def prune_ghost_conversation_summaries(
    *,
    dry_run: bool = False,
    include_app_storage: bool = True,
    portfolio_dir: Path | str | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    app_storage_path: Path | str | Sequence[Path | str] | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Fail-closed offline prune of ghost rows in `conversation_summaries.db` and `app_storage.json` with `0600` backups."""
    active_procs = _detect_active_antigravity_processes(ps_output=ps_output, ps_runner=ps_runner)
    if active_procs:
        return {
            "status": "blocked",
            "reason": "antigravity_process_active",
            "dry_run": bool(dry_run),
            "active_processes": active_procs,
            "pruned_count": 0,
            "pruned_cids": [],
            "app_storage_entries_removed": 0,
            "backups": [],
        }

    eff_agy_dirs = antigravity_dirs
    if eff_agy_dirs is None:
        eff_agy_dirs = _infer_antigravity_dirs_from_summaries_db(
            brain_dir=brain_dir,
            conversations_dir=conversations_dir,
            cli_dir=cli_dir,
            antigravity_dirs=antigravity_dirs,
            summaries_db=summaries_db,
        )
    has_explicit_root_override = any(
        x is not None for x in (brain_dir, conversations_dir, cli_dir, eff_agy_dirs)
    )
    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=eff_agy_dirs,
    )
    existing_dbs = _resolve_summaries_db_candidates(roots, summaries_db=summaries_db)
    existing_app_storage = (
        _resolve_app_storage_paths(
            roots,
            app_storage_path=app_storage_path,
            has_explicit_root_override=has_explicit_root_override,
            home_dir=home_dir,
            env=env,
        )
        if include_app_storage
        else []
    )

    if not existing_dbs and not existing_app_storage:
        return {
            "status": "ok",
            "dry_run": bool(dry_run),
            "pruned_count": 0,
            "pruned_cids": [],
            "app_storage_entries_removed": 0,
            "backups": [],
            "reason": "no_summaries_db_found",
        }

    for db_p in existing_dbs:
        locked, lock_reason = _check_sqlite_wal_or_lock_active(db_p)
        if locked:
            return {
                "status": "blocked",
                "reason": lock_reason,
                "dry_run": bool(dry_run),
                "db_path": str(db_p),
                "pruned_count": 0,
                "pruned_cids": [],
                "app_storage_entries_removed": 0,
                "backups": [],
            }

    protected_map = collect_protected_conversation_ids(
        portfolio_dir=portfolio_dir,
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=eff_agy_dirs,
        summaries_db=existing_dbs,
        include_unaudited_check=False,
        now_ts=now_ts,
        env=env,
    )

    disk_index = _build_disk_artifacts_index(roots)
    cur_now = float(now_ts if now_ts is not None else time.time())
    ts_slug = datetime.fromtimestamp(cur_now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups: list[str] = []
    pruned_cids_all: list[str] = []
    seen_pruned: set[str] = set()
    running_in_all_dbs: set[str] = set()

    for db_p in existing_dbs:
        scan = _scan_summaries_db_ghosts(db_p, roots=roots, protected_map=protected_map, disk_index=disk_index)
        running_in_all_dbs.update(scan["running_cids"])
        if not scan["opened"]:
            continue
        ghost_cids = list(scan["ghost_cids"])
        has_summaries = bool(scan["has_summaries"])
        has_fts = bool(scan["has_fts"])
        has_parents = bool(scan["has_parents"])

        if not ghost_cids:
            continue

        if dry_run:
            for c_val in ghost_cids:
                if c_val not in seen_pruned:
                    seen_pruned.add(c_val)
                    pruned_cids_all.append(c_val)
            continue

        backup_path = db_p.with_name(f"{db_p.name}.bak.{ts_slug}")
        shutil.copy2(db_p, backup_path)
        with contextlib.suppress(OSError):
            os.chmod(backup_path, 0o600)
        backups.append(str(backup_path))

        rw_conn: sqlite3.Connection | None = None
        try:
            rw_conn = sqlite3.connect(str(db_p), timeout=0.0)
            rw_conn.execute("BEGIN IMMEDIATE")
            for idx in range(0, len(ghost_cids), 200):
                chunk = ghost_cids[idx : idx + 200]
                placeholders = ",".join("?" for _ in chunk)
                if has_fts:
                    rw_conn.execute(
                        f"DELETE FROM conversations_fts WHERE LOWER(conversation_id) IN ({placeholders})",
                        chunk,
                    )
                if has_summaries:
                    rw_conn.execute(
                        f"DELETE FROM conversation_summaries WHERE LOWER(conversation_id) IN ({placeholders})",
                        chunk,
                    )
                if has_parents:
                    rw_conn.execute(
                        f"DELETE FROM conversation_parents WHERE LOWER(conversation_id) IN ({placeholders}) "
                        f"OR LOWER(parent_id) IN ({placeholders})",
                        [*chunk, *chunk],
                    )
            rw_conn.commit()
            for c_val in ghost_cids:
                if c_val not in seen_pruned:
                    seen_pruned.add(c_val)
                    pruned_cids_all.append(c_val)
        except sqlite3.Error as exc:
            if rw_conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    rw_conn.rollback()
            return {
                "status": "blocked",
                "reason": f"sqlite_write_failed:{exc}",
                "dry_run": False,
                "db_path": str(db_p),
                "backups": backups,
                "pruned_count": len(pruned_cids_all),
                "pruned_cids": pruned_cids_all,
                "app_storage_entries_removed": 0,
            }
        finally:
            if rw_conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    rw_conn.close()

    app_storage_res: dict[str, Any] = {
        "files_checked": 0,
        "files_modified": 0,
        "backups": [],
        "ghost_cids": [],
        "entries_removed": 0,
        "breakdown": {"pinned": 0, "layout_keys": 0, "layout_index_pairs": 0, "expanded_keys": 0},
    }
    if include_app_storage and existing_app_storage and (_has_valid_roots_on_disk(roots) or bool(existing_dbs)):
        app_storage_res = _prune_app_storage_ghosts(
            existing_app_storage,
            roots=roots,
            protected_map=protected_map,
            running_cids=running_in_all_dbs,
            dry_run=dry_run,
            ts_slug=ts_slug,
            disk_index=disk_index,
        )
        backups.extend(app_storage_res["backups"])

    all_pruned = sorted(set(pruned_cids_all) | set(app_storage_res["ghost_cids"]))
    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "pruned_count": len(pruned_cids_all),
        "pruned_cids": pruned_cids_all,
        "total_ghosts_pruned_count": len(all_pruned),
        "app_storage_pruned": app_storage_res,
        "app_storage_entries_removed": int(app_storage_res["entries_removed"]),
        "backups": backups,
        "dbs_checked": len(existing_dbs),
    }


def _resolve_ghost_purge_state_dir(
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> Path:
    """Resolve the state directory storing `ghost_purge_armed.json` and `ghost_purge_last_result.json`."""
    return dev_common.resolve_state_dir(portfolio_dir=portfolio_dir, home_dir=home_dir)


def _iter_active_lock_slots_for_guard(lock_data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return active slot entries from a flat or multi-slot `dev_pr_lock` dictionary."""
    if not isinstance(lock_data, Mapping) or not bool(lock_data.get("active")):
        return []
    slots_map = lock_data.get("slots")
    if isinstance(slots_map, Mapping) and slots_map:
        out: list[Mapping[str, Any]] = []
        for s_val in slots_map.values():
            if isinstance(s_val, Mapping) and bool(s_val.get("active", True)):
                out.append(s_val)
        if out:
            return out
    return [lock_data]


def check_ghost_purge_restart_guard(
    *,
    portfolio_dir: Path | str | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Fail-closed check refusing `restart_and_purge` when any conversation is `RUNNING` or any repo lock is in `DEVELOPING`/`NEEDS_REVISION_ITERATING`/`AWAITING_AGENT_REVIEW`."""
    st_dir = _resolve_ghost_purge_state_dir(portfolio_dir=portfolio_dir, home_dir=home_dir)
    all_locks = get_all_dev_pr_locks(portfolio_dir=st_dir)
    blocking_locks: list[tuple[str, str]] = []
    for slug, lk in sorted(all_locks.items()):
        for slot in _iter_active_lock_slots_for_guard(lk):
            st_u = str(slot.get("status") or "").strip().upper()
            if st_u in GHOST_PURGE_BLOCKING_LOCK_STATUSES:
                blocking_locks.append((slug, st_u))

    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
    )
    existing_dbs = _resolve_summaries_db_candidates(roots, summaries_db=summaries_db)
    running_cids: set[str] = set()
    for db_p in existing_dbs:
        ro_conn = _open_sqlite_readonly(db_p)
        if ro_conn is None:
            continue
        try:
            tables = {
                str(r[0] or "").strip()
                for r in ro_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if "conversation_summaries" in tables:
                cols = {str(r[1] or "").strip() for r in ro_conn.execute("PRAGMA table_info(conversation_summaries)").fetchall()}
                if "conversation_id" in cols and "status" in cols:
                    for row in ro_conn.execute("SELECT conversation_id, COALESCE(status, '') FROM conversation_summaries").fetchall():
                        c_val = str(row[0] or "").strip().lower()
                        st_u = str(row[1] or "").strip().upper()
                        if c_val and (st_u in ("CASCADE_RUN_STATUS_RUNNING", "RUNNING") or st_u.endswith("_RUNNING")):
                            running_cids.add(c_val)
        finally:
            with contextlib.suppress(sqlite3.Error):
                ro_conn.close()

    running_count = len(running_cids)
    active_lock_repos = sorted({slug for slug, _ in blocking_locks})
    active_lock_statuses = sorted({st for _, st in blocking_locks})

    if running_count > 0 and blocking_locks:
        return {
            "can_restart_now": False,
            "blocked_reason": "running_conversations_and_active_locks",
            "blocked_message": (
                f"Blocked: {running_count} RUNNING conversation(s) and "
                f"{len(blocking_locks)} active repo lock(s) ({', '.join(active_lock_statuses)})"
            ),
            "running_count": running_count,
            "active_lock_repos": active_lock_repos,
            "active_lock_statuses": active_lock_statuses,
        }
    if running_count > 0:
        return {
            "can_restart_now": False,
            "blocked_reason": "running_conversations_active",
            "blocked_message": f"Blocked: {running_count} RUNNING conversation(s) active",
            "running_count": running_count,
            "active_lock_repos": [],
            "active_lock_statuses": [],
        }
    if blocking_locks:
        return {
            "can_restart_now": False,
            "blocked_reason": "active_dev_locks",
            "blocked_message": (
                f"Blocked: active repo lock(s) in {', '.join(active_lock_statuses)} "
                f"({', '.join(active_lock_repos)})"
            ),
            "running_count": 0,
            "active_lock_repos": active_lock_repos,
            "active_lock_statuses": active_lock_statuses,
        }
    return {
        "can_restart_now": True,
        "blocked_reason": "",
        "blocked_message": "",
        "running_count": 0,
        "active_lock_repos": [],
        "active_lock_statuses": [],
    }


def _read_unexpired_ghost_purge_arm(
    st_dir: Path,
    *,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """Read `ghost_purge_armed.json` if armed and unexpired; return None otherwise."""
    arm_path = st_dir / GHOST_PURGE_ARMED_FILENAME
    raw = dev_common.atomic_read_json(arm_path, default=None)
    if not isinstance(raw, Mapping) or not bool(raw.get("armed")):
        return None
    cur_now = float(now_ts if now_ts is not None else time.time())
    expires_ts = raw.get("expires_at_ts")
    if isinstance(expires_ts, (int, float)) and float(expires_ts) <= cur_now:
        return None
    return dict(raw)


def _is_ghost_purge_worker_running(st_dir: Path) -> bool:
    """Return True when the single-flight `ghost_purge_worker.lock` is held by a live worker."""
    lock_path = st_dir / "tmp" / GHOST_PURGE_WORKER_LOCK_FILENAME
    if not lock_path.exists():
        return False
    try:
        probe_fd = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        try:
            fcntl.flock(probe_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe_fd.fileno(), fcntl.LOCK_UN)
            return False
        except OSError:
            return True
    finally:
        with contextlib.suppress(OSError):
            probe_fd.close()


def _spawn_ghost_purge_worker_daemon(
    *,
    mode: str = "on_quit",
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    app_storage_path: Path | str | Sequence[Path | str] | None = None,
    force_spawn: bool = False,
    popen_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Spawn a detached single-flight `prune-ghost-summaries --ghost-purge-worker` process."""
    if not force_spawn and (
        "unittest" in sys.modules
        or "pytest" in sys.modules
        or os.environ.get("AGY_OFFLINE_ONLY") == "1"
        or os.environ.get("PRE_COMMIT_HOOK") == "1"
    ):
        return {
            "status": "skipped",
            "spawned": False,
            "reason": "hermetic_or_offline_mode",
        }

    st_dir = _resolve_ghost_purge_state_dir(portfolio_dir=portfolio_dir, home_dir=home_dir)
    if _is_ghost_purge_worker_running(st_dir):
        return {
            "status": "ok",
            "spawned": False,
            "already_running": True,
        }

    cmd = [
        sys.executable,
        str(Path(__file__).with_name("pr_remediator.py").resolve()),
        "prune-ghost-summaries",
        "--ghost-purge-worker",
        "--mode",
        str(mode or "on_quit"),
        "--portfolio-dir",
        str(st_dir),
    ]
    if summaries_db is not None and isinstance(summaries_db, (str, Path)) and str(summaries_db).strip():
        cmd.extend(["--summaries-db", str(summaries_db)])
    if app_storage_path is not None and isinstance(app_storage_path, (str, Path)) and str(app_storage_path).strip():
        cmd.extend(["--app-storage", str(app_storage_path)])

    spawner = popen_fn if popen_fn is not None else subprocess.Popen
    try:
        proc = spawner(
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
            "pid": getattr(proc, "pid", None),
        }
    except OSError as exc:
        return {
            "status": "error",
            "spawned": False,
            "error": str(exc),
        }


def arm_ghost_purge(
    *,
    mode: str = "on_quit",
    ttl_seconds: float | None = None,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    app_storage_path: Path | str | Sequence[Path | str] | None = None,
    spawn_worker: bool = True,
    force_spawn: bool = False,
    fail_if_already_armed: bool = False,
    pre_write_guard: Callable[[], dict[str, Any]] | None = None,
    popen_fn: Callable[..., Any] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Single-flight arming of `ghost_purge_armed.json` (`0600`) with TTL and detached worker spawn."""
    clean_mode = "restart_now" if str(mode or "").strip().lower() in ("restart_now", "restart_and_purge") else "on_quit"
    st_dir = _resolve_ghost_purge_state_dir(portfolio_dir=portfolio_dir, home_dir=home_dir)
    arm_path = st_dir / GHOST_PURGE_ARMED_FILENAME
    lock_path = st_dir / GHOST_PURGE_LOCK_FILENAME
    cur_now = float(now_ts if now_ts is not None else time.time())
    resolved_ttl = _resolve_ghost_purge_arm_ttl_seconds(ttl_seconds, env=env)

    already_armed = False
    with dev_common.file_lock(lock_path):
        existing = _read_unexpired_ghost_purge_arm(st_dir, now_ts=cur_now)
        if existing is not None:
            if fail_if_already_armed:
                return {
                    "status": "blocked",
                    "reason": "armed_already",
                    "blocked_reason": "armed_already",
                    "blocked_message": f"Blocked: ghost purge is already armed ({existing.get('mode') or 'on_quit'})",
                    "armed": True,
                    "already_armed": True,
                    "arm_state": existing,
                }
            already_armed = True
            arm_doc = existing
        else:
            if pre_write_guard is not None:
                guard = pre_write_guard()
                if not guard.get("can_restart_now"):
                    return {
                        "status": "blocked",
                        "reason": guard["blocked_reason"],
                        "blocked_reason": guard["blocked_reason"],
                        "blocked_message": guard["blocked_message"],
                        "running_count": guard["running_count"],
                        "active_lock_repos": guard["active_lock_repos"],
                        "armed": False,
                    }
            exp_ts = cur_now + float(resolved_ttl)
            arm_doc = {
                "schema_version": 1,
                "armed": True,
                "mode": clean_mode,
                "arm_id": uuid.uuid4().hex[:16],
                "armed_at": datetime.fromtimestamp(cur_now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "armed_at_ts": round(cur_now, 3),
                "ttl_seconds": resolved_ttl,
                "expires_at": datetime.fromtimestamp(exp_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "expires_at_ts": round(exp_ts, 3),
                "status": "armed",
            }
            dev_common.atomic_write_json(arm_path, arm_doc, mode=0o600)

    spawn_res: dict[str, Any] = {"status": "skipped", "spawned": False}
    if spawn_worker:
        spawn_res = _spawn_ghost_purge_worker_daemon(
            mode=str(arm_doc.get("mode") or clean_mode),
            portfolio_dir=st_dir,
            home_dir=home_dir,
            summaries_db=summaries_db,
            app_storage_path=app_storage_path,
            force_spawn=force_spawn,
            popen_fn=popen_fn,
        )

    return {
        "status": "ok",
        "armed": True,
        "already_armed": already_armed,
        "arm_state": arm_doc,
        "worker": spawn_res,
    }


def cancel_ghost_purge(
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Disarm `ghost_purge_armed.json` cleanly under file lock."""
    st_dir = _resolve_ghost_purge_state_dir(portfolio_dir=portfolio_dir, home_dir=home_dir)
    arm_path = st_dir / GHOST_PURGE_ARMED_FILENAME
    lock_path = st_dir / GHOST_PURGE_LOCK_FILENAME
    was_armed = False
    with dev_common.file_lock(lock_path):
        raw = dev_common.atomic_read_json(arm_path, default=None)
        if isinstance(raw, Mapping) and bool(raw.get("armed")):
            was_armed = True
        with contextlib.suppress(OSError):
            arm_path.unlink(missing_ok=True)
    return {
        "status": "ok",
        "armed": False,
        "cancelled": was_armed,
    }


def _is_desktop_antigravity_proc_desc(desc: str) -> bool:
    """Return True when a `_detect_active_antigravity_processes` entry represents the Desktop GUI app (not `agy` CLI or `language_server`)."""
    raw = str(desc or "").strip().lower()
    if not raw or ":" not in raw:
        return False
    _, label = raw.split(":", 1)
    if label in ("agy", "language_server") or label.startswith("language_server"):
        return False
    return (
        label in ("antigravity", "antigravity.exe")
        or "/antigravity.app/" in label
        or "antigravity helper" in label
    )


def _filter_desktop_antigravity_procs(active_procs: Sequence[str]) -> list[str]:
    """Filter active process descriptions down to Antigravity Desktop GUI processes only."""
    return [p for p in active_procs if _is_desktop_antigravity_proc_desc(p)]


def _default_quit_antigravity(platform_name: str, active_procs: Sequence[str]) -> bool:
    """Platform-gated graceful quit request for Desktop GUI processes only (Darwin `osascript`, Linux `SIGTERM`)."""
    plat = platform_name.lower()
    desktop_procs = _filter_desktop_antigravity_procs(active_procs)
    if plat == "darwin":
        try:
            proc = subprocess.run(
                ["osascript", "-e", 'tell application "Antigravity" to quit'],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            return proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    if plat.startswith("linux"):
        signaled = False
        for desc in desktop_procs:
            m = re.match(r"^pid=(\d+):", str(desc))
            if not m:
                continue
            pid = int(m.group(1))
            if pid <= 1 or pid == os.getpid():
                continue
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)
                signaled = True
        return signaled
    return False


def _default_reopen_antigravity(platform_name: str) -> bool:
    """Platform-gated detached reopen of Antigravity Desktop (`open -a Antigravity` or `antigravity`)."""
    plat = platform_name.lower()
    if plat == "darwin":
        try:
            subprocess.Popen(
                ["open", "-a", "Antigravity"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            return True
        except OSError:
            return False
    if plat.startswith("linux"):
        bin_path = shutil.which("antigravity")
        if not bin_path:
            return False
        try:
            subprocess.Popen(
                [bin_path],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
            return True
        except OSError:
            return False
    return False


def _write_ghost_purge_result(
    st_dir: Path,
    *,
    status: str,
    mode: str,
    now_ts: float,
    prune_res: Mapping[str, Any] | None = None,
    reopened: bool = False,
    reason: str = "",
) -> dict[str, Any]:
    """Write atomic `0600` `ghost_purge_last_result.json` with Zero-PII summary fields."""
    pr = prune_res or {}
    doc: dict[str, Any] = {
        "schema_version": 1,
        "status": str(status),
        "mode": str(mode),
        "reason": str(reason or pr.get("reason") or ""),
        "completed_at": datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "completed_at_ts": round(now_ts, 3),
        "pruned_count": int(pr.get("pruned_count") or 0),
        "total_ghosts_pruned_count": int(pr.get("total_ghosts_pruned_count") or pr.get("pruned_count") or 0),
        "app_storage_entries_removed": int(pr.get("app_storage_entries_removed") or 0),
        "backups_count": len(pr.get("backups") or []),
        "reopened": bool(reopened),
        "acked": False,
    }
    dev_common.atomic_write_json(st_dir / GHOST_PURGE_RESULT_FILENAME, doc, mode=0o600)
    return doc


def ack_ghost_purge_result(
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Mark `ghost_purge_last_result.json` as acknowledged/dismissed (`0600`)."""
    st_dir = _resolve_ghost_purge_state_dir(portfolio_dir=portfolio_dir, home_dir=home_dir)
    res_path = st_dir / GHOST_PURGE_RESULT_FILENAME
    raw = dev_common.atomic_read_json(res_path, default=None)
    if not isinstance(raw, Mapping):
        return {"status": "ok", "acked": False, "last_result": None}
    cur_now = float(now_ts if now_ts is not None else time.time())
    updated = dict(raw)
    updated["acked"] = True
    updated["acked_at"] = datetime.fromtimestamp(cur_now, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    dev_common.atomic_write_json(res_path, updated, mode=0o600)
    return {"status": "ok", "acked": True, "last_result": updated}


def get_ghost_purge_status(
    *,
    portfolio_dir: Path | str | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    app_storage_path: Path | str | Sequence[Path | str] | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Return complete read-only `ghost_purge` snapshot for sidecar state and CLI inspection."""
    st_dir = _resolve_ghost_purge_state_dir(portfolio_dir=portfolio_dir, home_dir=home_dir)
    cur_now = float(now_ts if now_ts is not None else time.time())
    summary = count_ghost_sidebar_entries(
        portfolio_dir=st_dir,
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
        summaries_db=summaries_db,
        app_storage_path=app_storage_path,
        home_dir=home_dir,
        env=env,
        now_ts=cur_now,
    )
    guard = check_ghost_purge_restart_guard(
        portfolio_dir=st_dir,
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
        summaries_db=summaries_db,
        home_dir=home_dir,
    )
    arm_doc = _read_unexpired_ghost_purge_arm(st_dir, now_ts=cur_now)
    armed_payload = (
        {
            "armed": True,
            "mode": str(arm_doc.get("mode") or "on_quit"),
            "arm_id": str(arm_doc.get("arm_id") or ""),
            "armed_at": str(arm_doc.get("armed_at") or ""),
            "expires_at": str(arm_doc.get("expires_at") or ""),
            "ttl_seconds": int(arm_doc.get("ttl_seconds") or _resolve_ghost_purge_arm_ttl_seconds(env=env)),
        }
        if arm_doc is not None
        else {
            "armed": False,
            "mode": "",
            "arm_id": "",
            "armed_at": "",
            "expires_at": "",
            "ttl_seconds": _resolve_ghost_purge_arm_ttl_seconds(env=env),
        }
    )
    raw_last = dev_common.atomic_read_json(st_dir / GHOST_PURGE_RESULT_FILENAME, default=None)
    last_result = dict(raw_last) if isinstance(raw_last, Mapping) else None

    return {
        "summary": {
            "total_ghost_cids": int(summary["total_ghost_cids"]),
            "db_ghost_count": int(summary["db_ghost_count"]),
            "app_storage_ghost_count": int(summary["app_storage_ghost_count"]),
            "app_storage_entries_count": int(summary["app_storage_entries_count"]),
            "app_storage_breakdown": dict(summary["app_storage_breakdown"]),
            "has_ghosts": bool(summary["has_ghosts"]),
            "dbs_checked": int(summary["dbs_checked"]),
            "app_storage_files_checked": int(summary["app_storage_files_checked"]),
        },
        "armed": armed_payload,
        "can_restart_now": bool(guard["can_restart_now"]),
        "blocked_reason": str(guard["blocked_reason"]),
        "blocked_message": str(guard["blocked_message"]),
        "running_count": int(guard["running_count"]),
        "active_lock_repos": list(guard["active_lock_repos"]),
        "last_result": last_result,
    }


def restart_and_purge(
    *,
    ttl_seconds: float | None = None,
    portfolio_dir: Path | str | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    app_storage_path: Path | str | Sequence[Path | str] | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    spawn_worker: bool = True,
    force_spawn: bool = False,
    popen_fn: Callable[..., Any] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Fail-closed guarded trigger for detached graceful quit, ghost purge, and Antigravity reopen."""
    st_dir = _resolve_ghost_purge_state_dir(portfolio_dir=portfolio_dir, home_dir=home_dir)
    arm_res = arm_ghost_purge(
        mode="restart_now",
        ttl_seconds=ttl_seconds,
        portfolio_dir=st_dir,
        home_dir=home_dir,
        env=env,
        summaries_db=summaries_db,
        app_storage_path=app_storage_path,
        spawn_worker=spawn_worker,
        force_spawn=force_spawn,
        fail_if_already_armed=True,
        pre_write_guard=lambda: check_ghost_purge_restart_guard(
            portfolio_dir=st_dir,
            brain_dir=brain_dir,
            conversations_dir=conversations_dir,
            cli_dir=cli_dir,
            antigravity_dirs=antigravity_dirs,
            summaries_db=summaries_db,
            home_dir=home_dir,
        ),
        popen_fn=popen_fn,
        now_ts=now_ts,
    )
    if arm_res.get("status") != "ok":
        return arm_res
    return {
        **arm_res,
        "mode": "restart_now",
        "can_restart_now": True,
    }


def run_ghost_purge_worker(
    *,
    mode: str = "on_quit",
    poll_interval_seconds: float = 2.0,
    max_duration_seconds: float = 86400.0,
    quit_timeout_seconds: float = 60.0,
    max_iterations: int | None = None,
    portfolio_dir: Path | str | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    app_storage_path: Path | str | Sequence[Path | str] | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    ps_runner: Callable[[], str] | None = None,
    quit_fn: Callable[[str, list[str]], bool] | None = None,
    reopen_fn: Callable[[str], bool] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    clock_fn: Callable[[], float] | None = None,
    platform_name: str | None = None,
) -> dict[str, Any]:
    """Detached 0-LLM ghost-purge worker loop for `on_quit` and guarded `restart_now` modes."""
    st_dir = _resolve_ghost_purge_state_dir(portfolio_dir=portfolio_dir, home_dir=home_dir)
    tmp_dir = st_dir / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    worker_lock_path = tmp_dir / GHOST_PURGE_WORKER_LOCK_FILENAME
    lock_fd = open(worker_lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return {"status": "skipped", "reason": "worker_already_running"}

        plat = (platform_name or sys.platform).lower()
        effective_clock = clock_fn if clock_fn is not None else time.time
        effective_sleep = sleep_fn if sleep_fn is not None else time.sleep
        effective_quit = quit_fn if quit_fn is not None else _default_quit_antigravity
        effective_reopen = reopen_fn if reopen_fn is not None else _default_reopen_antigravity
        arm_path = st_dir / GHOST_PURGE_ARMED_FILENAME

        start_ts = float(effective_clock())
        clean_mode = "restart_now" if str(mode or "").strip().lower() in ("restart_now", "restart_and_purge") else "on_quit"

        if clean_mode == "restart_now":
            guard = check_ghost_purge_restart_guard(
                portfolio_dir=st_dir,
                brain_dir=brain_dir,
                conversations_dir=conversations_dir,
                cli_dir=cli_dir,
                antigravity_dirs=antigravity_dirs,
                summaries_db=summaries_db,
                home_dir=home_dir,
            )
            if not guard["can_restart_now"]:
                cancel_ghost_purge(portfolio_dir=st_dir, home_dir=home_dir)
                res_doc = _write_ghost_purge_result(
                    st_dir,
                    status="blocked",
                    mode="restart_now",
                    now_ts=float(effective_clock()),
                    reason=str(guard["blocked_reason"]),
                )
                return {"status": "blocked", "event": "guard_blocked", "result": res_doc}

            if plat != "darwin" and not plat.startswith("linux"):
                cancel_ghost_purge(portfolio_dir=st_dir, home_dir=home_dir)
                res_doc = _write_ghost_purge_result(
                    st_dir,
                    status="blocked",
                    mode="restart_now",
                    now_ts=float(effective_clock()),
                    reason=f"unsupported_platform:{plat}",
                )
                return {"status": "blocked", "event": "unsupported_platform", "result": res_doc}

            active_procs = _detect_active_antigravity_processes(ps_runner=ps_runner)
            desktop_procs = _filter_desktop_antigravity_procs(active_procs)
            if desktop_procs:
                effective_quit(plat, desktop_procs)

        iterations = 0
        last_prune_reason = ""
        while True:
            cur_now = float(effective_clock())
            raw_arm = dev_common.atomic_read_json(arm_path, default=None)
            if not isinstance(raw_arm, Mapping) or not bool(raw_arm.get("armed")):
                return {"status": "ok", "event": "cancelled", "iterations": iterations}

            exp_ts = raw_arm.get("expires_at_ts")
            if isinstance(exp_ts, (int, float)) and float(exp_ts) <= cur_now:
                cancel_ghost_purge(portfolio_dir=st_dir, home_dir=home_dir)
                return {"status": "ok", "event": "expired", "iterations": iterations}

            eff_mode = "restart_now" if str(raw_arm.get("mode") or clean_mode) == "restart_now" else "on_quit"
            iterations += 1
            active_procs = _detect_active_antigravity_processes(ps_runner=ps_runner)
            if not active_procs:
                prune_res = prune_ghost_conversation_summaries(
                    dry_run=False,
                    include_app_storage=True,
                    portfolio_dir=st_dir,
                    brain_dir=brain_dir,
                    conversations_dir=conversations_dir,
                    cli_dir=cli_dir,
                    antigravity_dirs=antigravity_dirs,
                    summaries_db=summaries_db,
                    app_storage_path=app_storage_path,
                    home_dir=home_dir,
                    env=env,
                    ps_output="",
                    now_ts=cur_now,
                )
                if prune_res.get("status") == "ok":
                    reopened = bool(effective_reopen(plat)) if eff_mode == "restart_now" else False
                    res_doc = _write_ghost_purge_result(
                        st_dir,
                        status="ok",
                        mode=eff_mode,
                        now_ts=cur_now,
                        prune_res=prune_res,
                        reopened=reopened,
                    )
                    cancel_ghost_purge(portfolio_dir=st_dir, home_dir=home_dir)
                    return {
                        "status": "ok",
                        "event": "purged_and_reopened" if eff_mode == "restart_now" else "purged_on_quit",
                        "iterations": iterations,
                        "prune_result": prune_res,
                        "result": res_doc,
                    }
                last_prune_reason = str(prune_res.get("reason") or "")

            timeout_limit = float(quit_timeout_seconds if eff_mode == "restart_now" else max_duration_seconds)
            if (cur_now - start_ts) >= timeout_limit or (max_iterations is not None and iterations >= max_iterations):
                if eff_mode == "restart_now":
                    cur_desktop = _filter_desktop_antigravity_procs(active_procs)
                    reopened = bool(effective_reopen(plat)) if not cur_desktop else False
                    cancel_ghost_purge(portfolio_dir=st_dir, home_dir=home_dir)
                    res_doc = _write_ghost_purge_result(
                        st_dir,
                        status="error",
                        mode="restart_now",
                        now_ts=cur_now,
                        reopened=reopened,
                        reason=last_prune_reason or "quit_timeout",
                    )
                    return {
                        "status": "error",
                        "event": "quit_timeout",
                        "iterations": iterations,
                        "reopened": reopened,
                        "result": res_doc,
                    }
                return {"status": "ok", "event": "max_iterations_or_timeout", "iterations": iterations}

            effective_sleep(max(0.01, float(poll_interval_seconds)))
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            lock_fd.close()


def _empty_cleanup_breakdown() -> dict[str, Any]:
    """Build a zero-initialized MB/count breakdown structure for `cleanup_merged_conversations`."""
    artifact_keys = ("brain", "conversations_db", "conversations_pb", "annotations", "presence", "sidecar_events")
    runtime_keys = ("desktop", "cli")
    category_keys = (
        "merged_roots",
        "cleaned_leaked_roots",
        "superseded_cancelled_roots",
        "rotated_orchestrators",
        "non_queue_merged_roots",
        "subagents",
        "noop_actions",
        "deliverable_actions",
        "unaudited_expired_actions",
        "sidecar_events",
        "orphan_artifacts",
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
    include_scheduled_actions: bool = False,
    include_sidecar_events: bool | None = None,
    include_orphans: bool | None = None,
    older_than_days: float | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    now_ts: float | None = None,
    repos_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    conversation_client: ConversationServiceClient | None = None,
) -> dict[str, Any]:
    """Safely delete merged conversation artifacts: Native-First via `ConversationService/DeleteConversation` when Desktop is active, or direct disk cleanup when Desktop is closed/CLI/hermetic."""
    root_cids = _normalize_str_list(conversation_ids)
    subonly_cids = _normalize_str_list(subagents_only_of)
    explicit_seed_cids = _normalize_str_list([*root_cids, *subonly_cids])
    eff_include_sidecar_events = (
        bool(include_sidecar_events)
        if include_sidecar_events is not None
        else bool(all_merged or include_scheduled_actions)
    )
    eff_include_orphans = (
        bool(include_orphans)
        if include_orphans is not None
        else bool(all_merged or include_scheduled_actions)
    )
    if (
        not explicit_seed_cids
        and not all_merged
        and not include_noop_actions
        and not include_scheduled_actions
        and not include_sidecar_events
        and not include_orphans
    ):
        return {
            "status": "error",
            "error": "Provide at least one --id, --subagents-only-of, --all-merged, --include-noop-actions, or --include-actions flag.",
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

    cur_now = float(now_ts if now_ts is not None else time.time())
    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
    )
    disk_index = _build_disk_artifacts_index(roots)
    repo_refs_cache: dict[str, set[str] | None] = {}
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
        now_ts=cur_now,
        env=env,
    )

    candidates: list[str] = []
    seen_cand: set[str] = set()
    candidate_category: dict[str, str] = {}
    breakdown = _empty_cleanup_breakdown()
    skipped: list[dict[str, Any]] = []
    queue_items_to_mark_cleaned: dict[str, list[str]] = {}
    unmerged_cid8_global: set[str] = set()

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
            if only_if_present and not _has_conversation_disk_artifacts(child, roots, disk_index=disk_index):
                continue
            _add_candidate(child, child_category)

    for rcid in root_cids:
        resolved_roots = _resolve_uuid_candidates_for_queue_id(rcid, roots, summaries_graph, disk_index=disk_index)
        for u_cid in resolved_roots:
            _add_candidate(u_cid, "merged_roots")
            if not _find_protection_reasons(u_cid, protected_map):
                _expand_children_for(u_cid, "subagents")

    for orch_cid in subonly_cids:
        for u_orch in _resolve_uuid_candidates_for_queue_id(orch_cid, roots, summaries_graph, disk_index=disk_index):
            _expand_children_for(u_orch, "subagents")

    if explicit_seed_cids:
        for q_it in q_items:
            q_cid_raw = str(q_it.get("conversation_id") or "").strip()
            if q_cid_raw and any(_matches_cid(q_cid_raw, s) for s in explicit_seed_cids):
                if str(q_it.get("status") or "").lower() == "merged" and any(_matches_cid(q_cid_raw, r) for r in root_cids):
                    queue_items_to_mark_cleaned.setdefault(
                        q_cid_raw,
                        _resolve_uuid_candidates_for_queue_id(q_cid_raw, roots, summaries_graph, disk_index=disk_index),
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

            unmerged_brs = _find_unmerged_local_branches_for_queue_item(
                q_it,
                p_dir=p_dir,
                git_client=git_client,
                repo_refs_cache=repo_refs_cache,
            )
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

            resolved_roots = _resolve_uuid_candidates_for_queue_id(q_cid_raw, roots, summaries_graph, disk_index=disk_index)
            if q_st == "merged":
                queue_items_to_mark_cleaned.setdefault(q_cid_raw, resolved_roots)

            only_present = q_st == "cleaned"
            for u_cid in resolved_roots:
                has_disk = _has_conversation_disk_artifacts(u_cid, roots, disk_index=disk_index)
                if not only_present or has_disk:
                    _add_candidate(u_cid, cat)
                if not _find_protection_reasons(u_cid, protected_map):
                    _expand_children_for(u_cid, "subagents", only_if_present=only_present)

            if not _find_protection_reasons(q_cid_raw, protected_map) and all(
                not _find_protection_reasons(u_cid, protected_map) for u_cid in resolved_roots
            ):
                for sub in q_it.get("subagent_conversation_ids") or []:
                    if not only_present or _has_conversation_disk_artifacts(str(sub), roots, disk_index=disk_index):
                        _add_candidate(str(sub), "subagents")

        merged_cid8, unmerged_cid8 = _collect_repo_branch_merge_index(
            p_dir,
            q_data,
            git_client=git_client,
            repos_dir=repos_dir,
            env=env,
        )
        unmerged_cid8_global.update(unmerged_cid8)

        rotated_orchs, all_orchs = _discover_orchestrator_roots_and_subagents(q_data, roots, summaries_graph)
        for r_orch in rotated_orchs:
            if r_orch[:8] in unmerged_cid8_global:
                skipped.append({"conversation_id": r_orch, "reasons": [f"unmerged_branch_prefix:{r_orch[:8]}"]})
                continue
            _add_candidate(r_orch, "rotated_orchestrators")
        for o_cid in all_orchs:
            _expand_children_for(o_cid, "subagents")

        non_queue_merged = _discover_non_queue_merged_conversations(
            roots,
            summaries_graph,
            q_items=q_items,
            merged_cid8=merged_cid8,
            unmerged_cid8=unmerged_cid8,
            exclude_cids=set(all_orchs),
        )
        for nq_cid in non_queue_merged:
            _add_candidate(nq_cid, "non_queue_merged_roots")
            if not _find_protection_reasons(nq_cid, protected_map):
                _expand_children_for(nq_cid, "subagents")

    eff_noop_days: float | None = None
    if include_noop_actions or include_scheduled_actions:
        noop_hours = (
            float(older_than_days) * 24.0
            if older_than_days is not None
            else _resolve_cleanup_hours("AGY_ACTION_CLEANUP_NOOP_HOURS", DEFAULT_ACTION_CLEANUP_NOOP_HOURS, env=env)
        )
        deliverable_hours = _resolve_cleanup_hours(
            "AGY_ACTION_CLEANUP_DELIVERABLE_HOURS",
            DEFAULT_ACTION_CLEANUP_DELIVERABLE_HOURS,
            env=env,
        )
        unaudited_cap_hours = _resolve_cleanup_hours(
            "AGY_ACTION_CLEANUP_UNAUDITED_CAP_HOURS",
            DEFAULT_ACTION_CLEANUP_UNAUDITED_CAP_HOURS,
            env=env,
        )
        eff_noop_days = round(noop_hours / 24.0, 4) if older_than_days is None else float(older_than_days)
        action_pairs = _discover_scheduled_action_conversations(
            p_dir,
            roots,
            summaries_graph,
            noop_hours=noop_hours,
            deliverable_hours=deliverable_hours,
            unaudited_cap_hours=unaudited_cap_hours,
            now_ts=cur_now,
            queue_items=q_items,
            include_deliverable_and_unaudited=bool(include_scheduled_actions),
            disk_index=disk_index,
        )
        for a_cid, a_cat in action_pairs:
            _add_candidate(a_cid, a_cat)
            if not _find_protection_reasons(a_cid, protected_map):
                _expand_children_for(a_cid, a_cat)
        breakdown["noop_older_than_days"] = eff_noop_days
        breakdown["action_retention_hours"] = {
            "noop": noop_hours,
            "deliverable": deliverable_hours,
            "unaudited_cap": unaudited_cap_hours,
        }

    deleted: list[dict[str, Any]] = []
    freed_bytes = 0
    eff_env = os.environ if env is None else env
    has_explicit_root_override = any(
        x is not None for x in (brain_dir, conversations_dir, cli_dir, antigravity_dirs)
    ) or bool(
        str(eff_env.get("AGY_ANTIGRAVITY_DIR") or "").strip()
        or str(eff_env.get("AGY_ANTIGRAVITY_CLI_DIR") or "").strip()
    )
    in_test_harness = "unittest" in sys.modules or "pytest" in sys.modules
    has_explicit_proc_probe = ps_output is not None or ps_runner is not None
    hermetic_test_override = bool(
        in_test_harness
        and has_explicit_root_override
        and not has_explicit_proc_probe
        and conversation_client is None
    )
    if hermetic_test_override:
        desktop_active = False
    else:
        active_procs = _detect_active_antigravity_processes(ps_output=ps_output, ps_runner=ps_runner)
        desktop_active = bool(_filter_desktop_antigravity_procs(active_procs))
    ls_client = (
        conversation_client
        if conversation_client is not None
        else ConversationServiceClient(env=eff_env)
    )
    native_delete_count = 0
    deferred_desktop_count = 0

    for cid in candidates:
        reasons = _find_protection_reasons(cid, protected_map)
        if reasons:
            skipped.append({"conversation_id": cid, "reasons": reasons})
            continue
        cat = candidate_category.get(cid, "merged_roots")
        if cat in ("subagents", "rotated_orchestrators", "non_queue_merged_roots") and cid[:8] in unmerged_cid8_global:
            skipped.append({"conversation_id": cid, "reasons": [f"unmerged_branch_prefix:{cid[:8]}"]})
            continue
        idle_ok, idle_reason = is_conversation_fully_idle(
            cid,
            brain_dir=brain_dir,
            conversations_dir=conversations_dir,
            min_idle_seconds=min_idle_seconds,
            now_ts=cur_now,
            cli_dir=cli_dir,
            antigravity_dirs=antigravity_dirs,
            summaries_db=summaries_db,
            summaries_graph=summaries_graph,
            disk_index=disk_index,
        )
        if not idle_ok:
            skipped.append({"conversation_id": cid, "reasons": [idle_reason]})
            continue

        artifacts = _collect_conversation_disk_artifacts(cid, roots, disk_index=disk_index)
        if not artifacts:
            skipped.append({"conversation_id": cid, "reasons": ["not_present_on_disk"]})
            continue

        desktop_artifacts = [a for a in artifacts if str(a.get("runtime") or "desktop") == "desktop"]
        cli_artifacts = [a for a in artifacts if str(a.get("runtime") or "desktop") != "desktop"]
        delete_method = "disk_rmtree"
        artifacts_to_delete = artifacts

        if desktop_artifacts and desktop_active:
            if not ls_client.is_configured():
                deferred_desktop_count += 1
                skipped.append(
                    {
                        "conversation_id": cid,
                        "runtime": "desktop",
                        "reason": "deferred_desktop_active_no_ls",
                        "reasons": ["deferred_desktop_active_no_ls"],
                    }
                )
                artifacts_to_delete = cli_artifacts
                if not artifacts_to_delete:
                    continue
            else:
                delete_method = "conversation_service"
                if not dry_run:
                    live_desktop_exists = any(
                        Path(a["path"]).is_symlink() or Path(a["path"]).exists()
                        for a in desktop_artifacts
                    )
                    if live_desktop_exists:
                        try:
                            ls_client.delete_conversation(cid)
                            native_delete_count += 1
                        except ConversationServiceError as exc:
                            deferred_desktop_count += 1
                            skipped.append(
                                {
                                    "conversation_id": cid,
                                    "runtime": "desktop",
                                    "reason": "conversation_service_delete_failed",
                                    "reasons": ["conversation_service_delete_failed"],
                                    "error": f"{exc.code}: {exc.message}",
                                }
                            )
                            artifacts_to_delete = cli_artifacts
                            if not artifacts_to_delete:
                                continue

        sz = 0
        per_kind_bytes: dict[str, int] = {}
        touched_runtimes: set[str] = set()
        for art in artifacts_to_delete:
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
                "delete_method": "disk_rmtree" if touched_runtimes == {"cli"} else delete_method,
                "runtimes": sorted(touched_runtimes),
                "artifacts": per_kind_bytes,
            }
        )

    sidecar_events_pruned: dict[str, Any] = {"count": 0, "bytes": 0, "retention_hours": 0.0, "by_runtime_bytes": {}}
    if eff_include_sidecar_events:
        ev_retention_hours = _resolve_cleanup_hours(
            "AGY_SIDECAR_EVENT_RETENTION_HOURS",
            DEFAULT_SIDECAR_EVENT_RETENTION_HOURS,
            env=env,
        )
        sidecar_events_pruned = _prune_sidecar_events(
            roots,
            retention_hours=ev_retention_hours,
            now_ts=cur_now,
            dry_run=dry_run,
        )
        ev_cnt = int(sidecar_events_pruned["count"])
        ev_bytes = int(sidecar_events_pruned["bytes"])
        freed_bytes += ev_bytes
        breakdown["discovered_counts"]["sidecar_events"] += ev_cnt
        breakdown["by_artifact"]["sidecar_events"]["count"] += ev_cnt
        breakdown["by_artifact"]["sidecar_events"]["bytes"] += ev_bytes
        breakdown["by_category"]["sidecar_events"]["count"] += ev_cnt
        breakdown["by_category"]["sidecar_events"]["bytes"] += ev_bytes
        for rt_lbl, rt_b in sidecar_events_pruned["by_runtime_bytes"].items():
            if rt_lbl in breakdown["by_runtime"]:
                breakdown["by_runtime"][rt_lbl]["bytes"] += int(rt_b)

    orphan_artifacts_pruned: dict[str, Any] = {"count": 0, "bytes": 0, "by_artifact": {}, "by_runtime_bytes": {}}
    if eff_include_orphans:
        orphan_artifacts_pruned = _sweep_orphan_companion_artifacts(
            roots,
            protected_map,
            min_idle_seconds=min_idle_seconds,
            now_ts=cur_now,
            dry_run=dry_run,
        )
        or_cnt = int(orphan_artifacts_pruned["count"])
        or_bytes = int(orphan_artifacts_pruned["bytes"])
        freed_bytes += or_bytes
        breakdown["discovered_counts"]["orphan_artifacts"] += or_cnt
        breakdown["by_category"]["orphan_artifacts"]["count"] += or_cnt
        breakdown["by_category"]["orphan_artifacts"]["bytes"] += or_bytes
        for art_k, art_stats in orphan_artifacts_pruned["by_artifact"].items():
            if art_k in breakdown["by_artifact"]:
                breakdown["by_artifact"][art_k]["count"] += int(art_stats["count"])
                breakdown["by_artifact"][art_k]["bytes"] += int(art_stats["bytes"])
        for rt_lbl, rt_b in orphan_artifacts_pruned["by_runtime_bytes"].items():
            if rt_lbl in breakdown["by_runtime"]:
                breakdown["by_runtime"][rt_lbl]["bytes"] += int(rt_b)

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
        "include_scheduled_actions": bool(include_scheduled_actions),
        "include_sidecar_events": eff_include_sidecar_events,
        "include_orphans": eff_include_orphans,
        "desktop_active": bool(desktop_active),
        "hermetic_test_override": bool(hermetic_test_override),
        "native_delete_count": int(native_delete_count),
        "deferred_desktop_count": int(deferred_desktop_count),
        "deleted_count": len(deleted),
        "deleted": [d["conversation_id"] for d in deleted],
        "deleted_details": deleted,
        "sidecar_events_pruned_count": int(sidecar_events_pruned["count"]),
        "sidecar_events_pruned_bytes": int(sidecar_events_pruned["bytes"]),
        "orphan_artifacts_pruned_count": int(orphan_artifacts_pruned["count"]),
        "orphan_artifacts_pruned_bytes": int(orphan_artifacts_pruned["bytes"]),
        "skipped_count": len(skipped),
        "skipped": skipped,
        "freed_bytes": freed_bytes,
        "freed_mb": round(freed_bytes / (1024 * 1024), 2),
        "breakdown": breakdown,
        "merge_verification": merge_check,
    }


def _resolve_auto_archive_adhoc_days(
    adhoc_days: float | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> float:
    """Resolve ad-hoc idle auto-archive threshold in days (`AGY_AUTO_ARCHIVE_ADHOC_DAYS`, default `3`, `0` disables)."""
    eff_env = os.environ if env is None else env
    if adhoc_days is not None:
        with contextlib.suppress(TypeError, ValueError):
            val = float(adhoc_days)
            return 0.0 if val <= 0.0 else val
    raw = str(eff_env.get("AGY_AUTO_ARCHIVE_ADHOC_DAYS") or "").strip()
    if raw:
        with contextlib.suppress(TypeError, ValueError):
            val = float(raw)
            return 0.0 if val <= 0.0 else val
    return float(DEFAULT_AUTO_ARCHIVE_ADHOC_DAYS)


def _queue_item_matches_cid(cid: str, q_item: Mapping[str, Any]) -> bool:
    """Return True if `cid` matches a queue item's `conversation_id` or its synthetic 8-char hex prefix."""
    q_cid = str(q_item.get("conversation_id") or "").strip().lower()
    if not q_cid:
        return False
    if _matches_cid(cid, q_cid):
        return True
    if not _CONVERSATION_UUID_RE.fullmatch(q_cid):
        pfx8 = _extract_prefix8_from_synthetic_or_uuid(q_cid)
        if pfx8 and _matches_cid(cid, pfx8):
            return True
    return False


def _collect_app_storage_pinned_cids(
    roots: Sequence[Mapping[str, Any]],
    *,
    has_explicit_root_override: bool,
    in_test_harness: bool,
    env: Mapping[str, str] | None = None,
) -> set[str]:
    """Read pinned UUIDs from `app_storage.json` strictly read-only without probing host paths in hermetic tests."""
    if in_test_harness and not has_explicit_root_override:
        return set()
    pinned: set[str] = set()
    app_paths = _resolve_app_storage_paths(
        roots,
        has_explicit_root_override=has_explicit_root_override,
        env=env,
    )
    for app_p in app_paths:
        with contextlib.suppress(OSError, UnicodeDecodeError, json.JSONDecodeError):
            raw_obj = json.loads(app_p.read_text(encoding="utf-8"))
            if isinstance(raw_obj, dict) and _PINNED_ORDER_KEY in raw_obj:
                pinned_list, _ = _parse_json_list_or_raw(raw_obj[_PINNED_ORDER_KEY])
                if pinned_list is not None:
                    for entry in pinned_list:
                        for uid in _extract_uuids_from_value(entry):
                            pinned.add(uid.lower())
    return pinned


def _is_orchestrator_conversation_payload(conv: Mapping[str, Any]) -> bool:
    """Return True when a `ConversationService` payload carries orchestrator title, summary, or tags."""
    annotations = conv.get("annotations") if isinstance(conv.get("annotations"), Mapping) else {}
    metadata = conv.get("metadata") if isinstance(conv.get("metadata"), Mapping) else {}
    state = conv.get("state") if isinstance(conv.get("state"), Mapping) else {}
    for raw_text in (annotations.get("title"), state.get("summary")):
        txt = str(raw_text or "").strip().lower()
        if not txt:
            continue
        if (
            txt.startswith("[agy-doctor] orchestrator recovery")
            or txt in ("main dev queue orchestrator", "dev queue orchestrator")
            or "agy-dev-orchestrator" in txt
            or "dev-queue-orchestrator" in txt
        ):
            return True
    for raw_tags in (annotations.get("tags"), metadata.get("tags")):
        if isinstance(raw_tags, Sequence) and not isinstance(raw_tags, (str, bytes)):
            for t in raw_tags:
                t_low = str(t or "").strip().lower()
                if t_low in ("orchestrator", "agy-dev-orchestrator", "dev-queue-orchestrator"):
                    return True
    return False


def auto_archive_stale_conversations(
    *,
    adhoc_days: float | None = None,
    include_cancelled_queue: bool = True,
    exclude_ids: Sequence[str] | None = None,
    min_idle_seconds: float = 60.0,
    include_unaudited_check: bool = True,
    dry_run: bool = False,
    portfolio_dir: Path | str | None = None,
    brain_dir: Path | str | None = None,
    conversations_dir: Path | str | None = None,
    cli_dir: Path | str | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    summaries_db: Path | str | Sequence[Path | str] | None = None,
    now_ts: float | None = None,
    env: Mapping[str, str] | None = None,
    conversation_client: ConversationServiceClient | None = None,
) -> dict[str, Any]:
    """Auto-archive stale ad-hoc chats (`> AGY_AUTO_ARCHIVE_ADHOC_DAYS`) and cancelled/compacted queue items via `UpdateConversation(archived=true)`."""
    eff_env = os.environ if env is None else env
    eff_adhoc_days = _resolve_auto_archive_adhoc_days(adhoc_days, env=eff_env)
    ls_client = (
        conversation_client
        if conversation_client is not None
        else ConversationServiceClient(env=eff_env)
    )
    if not ls_client.is_configured():
        return {
            "status": "skipped",
            "reason": "no_ls_env",
            "dry_run": bool(dry_run),
            "adhoc_days": eff_adhoc_days,
            "include_cancelled_queue": bool(include_cancelled_queue),
            "scanned_count": 0,
            "archived_count": 0,
            "archived": [],
            "archived_details": [],
            "skipped_count": 0,
            "skipped": [],
            "by_category": {"cancelled_compacted_queue": 0, "cancelled_queue": 0, "stale_adhoc_chat": 0},
        }

    try:
        conversations = ls_client.list_all_conversations(include_archived=False)
    except ConversationServiceError as exc:
        return {
            "status": "error",
            "reason": "conversation_service_list_failed",
            "error": f"{exc.code}: {exc.message}",
            "dry_run": bool(dry_run),
            "adhoc_days": eff_adhoc_days,
            "include_cancelled_queue": bool(include_cancelled_queue),
            "scanned_count": 0,
            "archived_count": 0,
            "archived": [],
            "archived_details": [],
            "skipped_count": 0,
            "skipped": [],
            "by_category": {"cancelled_compacted_queue": 0, "cancelled_queue": 0, "stale_adhoc_chat": 0},
        }

    cur_now = float(now_ts if now_ts is not None else time.time())
    p_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    has_explicit_root_override = any(
        x is not None for x in (brain_dir, conversations_dir, cli_dir, antigravity_dirs)
    )
    in_test_harness = "unittest" in sys.modules or "pytest" in sys.modules
    roots = _resolve_antigravity_roots(
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
    )
    disk_index = _build_disk_artifacts_index(roots)
    summaries_graph = _load_conversation_summaries_graph(roots, summaries_db=summaries_db)
    q_data = load_dev_queue(portfolio_dir=p_dir)
    q_items = list(q_data.get("items") or [])

    eff_excludes = list(exclude_ids or [])
    for env_key in ("ANTIGRAVITY_CONVERSATION_ID", "AGY_CONVERSATION_ID"):
        env_cid = str(eff_env.get(env_key) or "").strip()
        if env_cid:
            eff_excludes.append(env_cid)

    protected_map = collect_protected_conversation_ids(
        exclude_ids=eff_excludes,
        portfolio_dir=p_dir,
        brain_dir=brain_dir,
        conversations_dir=conversations_dir,
        cli_dir=cli_dir,
        antigravity_dirs=antigravity_dirs,
        summaries_db=summaries_db,
        summaries_graph=summaries_graph,
        include_unaudited_check=include_unaudited_check,
        now_ts=cur_now,
        env=eff_env,
    )
    _, all_orch_cids = _discover_orchestrator_roots_and_subagents(q_data, roots, summaries_graph)
    orch_cid_set = {str(o).strip().lower() for o in all_orch_cids if str(o).strip()}
    if q_data.get("orchestrator_conversation_id"):
        orch_cid_set.add(str(q_data["orchestrator_conversation_id"]).strip().lower())

    app_pinned_cids = _collect_app_storage_pinned_cids(
        roots,
        has_explicit_root_override=has_explicit_root_override,
        in_test_harness=in_test_harness,
        env=eff_env,
    )
    parent_by_cid: Mapping[str, str] = summaries_graph.get("parent_by_cid") or {}
    last_modified_by_cid: Mapping[str, str] = summaries_graph.get("last_modified_by_cid") or {}

    archived: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    by_category: dict[str, int] = {
        "cancelled_compacted_queue": 0,
        "cancelled_queue": 0,
        "stale_adhoc_chat": 0,
    }

    for conv in conversations:
        cid = str(conv.get("id") or "").strip().lower()
        if not cid:
            continue
        annotations = conv.get("annotations") if isinstance(conv.get("annotations"), Mapping) else {}
        metadata = conv.get("metadata") if isinstance(conv.get("metadata"), Mapping) else {}
        state = conv.get("state") if isinstance(conv.get("state"), Mapping) else {}

        if bool(annotations.get("archived")):
            skipped.append({"conversation_id": cid, "reasons": ["already_archived"]})
            continue
        if bool(annotations.get("pinned")) or cid in app_pinned_cids:
            skipped.append({"conversation_id": cid, "reasons": ["pinned"]})
            continue
        if bool(annotations.get("starred")):
            skipped.append({"conversation_id": cid, "reasons": ["starred"]})
            continue

        run_state = str(state.get("runState") or "").strip().upper()
        if (
            run_state in ("RUN_STATE_RUNNING", "RUN_STATE_CANCELLING", "RUNNING", "CANCELLING")
            or run_state.endswith(("_RUNNING", "_CANCELLING"))
        ):
            skipped.append({"conversation_id": cid, "reasons": [f"run_state:{run_state}"]})
            continue

        if str(metadata.get("parentConversationId") or "").strip() or cid in parent_by_cid:
            skipped.append({"conversation_id": cid, "reasons": ["subagent_child"]})
            continue

        if any(_matches_cid(cid, o_cid) for o_cid in orch_cid_set) or _is_orchestrator_conversation_payload(conv):
            skipped.append({"conversation_id": cid, "reasons": ["orchestrator"]})
            continue

        prot_reasons = _find_protection_reasons(cid, protected_map)
        if prot_reasons:
            skipped.append({"conversation_id": cid, "reasons": prot_reasons})
            continue

        idle_ok, idle_reason = is_conversation_fully_idle(
            cid,
            brain_dir=brain_dir,
            conversations_dir=conversations_dir,
            min_idle_seconds=min_idle_seconds,
            now_ts=cur_now,
            cli_dir=cli_dir,
            antigravity_dirs=antigravity_dirs,
            summaries_db=summaries_db,
            summaries_graph=summaries_graph,
            disk_index=disk_index,
        )
        if not idle_ok:
            skipped.append({"conversation_id": cid, "reasons": [idle_reason]})
            continue

        matching_q = [it for it in q_items if _queue_item_matches_cid(cid, it)]
        category = ""
        inactive_days: float | None = None

        if matching_q:
            cancelled_matches = [
                it for it in matching_q if str(it.get("status") or "").strip().lower() == "cancelled"
            ]
            if not include_cancelled_queue or not cancelled_matches:
                skipped.append({"conversation_id": cid, "reasons": ["queue_item_non_archivable"]})
                continue
            is_compacted = any(
                bool(str(it.get("superseded_by") or "").strip())
                or bool(_SUPERSEDED_NOTE_RE.search(str(it.get("notes") or "")))
                for it in cancelled_matches
            )
            category = "cancelled_compacted_queue" if is_compacted else "cancelled_queue"
        else:
            if eff_adhoc_days <= 0.0:
                skipped.append({"conversation_id": cid, "reasons": ["adhoc_archive_disabled"]})
                continue
            activity_epochs: list[float] = []
            for raw_ts in (
                state.get("updateTime"),
                metadata.get("createTime"),
                last_modified_by_cid.get(cid),
            ):
                ep = _parse_iso_epoch_seconds(str(raw_ts or ""))
                if ep is not None:
                    activity_epochs.append(ep)
            disk_ep = _max_artifact_epoch(cid, roots)
            if disk_ep is not None:
                activity_epochs.append(disk_ep)
            if not activity_epochs:
                skipped.append({"conversation_id": cid, "reasons": ["missing_activity_timestamp"]})
                continue
            last_ep = max(activity_epochs)
            inactive_seconds = cur_now - last_ep
            inactive_days = round(inactive_seconds / 86400.0, 2)
            if inactive_seconds <= (eff_adhoc_days * 86400.0):
                skipped.append({"conversation_id": cid, "reasons": ["within_adhoc_retention_window"]})
                continue
            category = "stale_adhoc_chat"

        if not dry_run:
            try:
                ls_client.update_conversation(cid, archived=True)
            except ConversationServiceError as exc:
                skipped.append(
                    {
                        "conversation_id": cid,
                        "reasons": ["conversation_service_update_failed"],
                        "error": f"{exc.code}: {exc.message}",
                    }
                )
                continue

        by_category[category] = by_category.get(category, 0) + 1
        detail: dict[str, Any] = {
            "conversation_id": cid,
            "category": category,
            "title": str(annotations.get("title") or state.get("summary") or ""),
        }
        if inactive_days is not None:
            detail["inactive_days"] = inactive_days
        archived.append(detail)

    return {
        "status": "ok",
        "dry_run": bool(dry_run),
        "adhoc_days": eff_adhoc_days,
        "include_cancelled_queue": bool(include_cancelled_queue),
        "scanned_count": len(conversations),
        "archived_count": len(archived),
        "archived": [a["conversation_id"] for a in archived],
        "archived_details": archived,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "by_category": by_category,
    }


def register_watcher_cli(sub: Any) -> None:
    """Register `watch-pr-merge`, `wait-for-unlock`, `cleanup-merged-conversations`, and `prune-ghost-summaries` subcommands."""
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
        help="Safely delete merged conversation directories/SQLite .db files/protobufs, sidecar events, orphans, and spawned subagents after verifying git merge and protections.",
    )
    p_clean.add_argument("--id", "--ids", "--conversation-ids", dest="conversation_ids", nargs="*", default=[], help="Merged root conversation IDs to clean up along with spawned subagents.")
    p_clean.add_argument("--subagents-only-of", nargs="*", default=[], help="Orchestrator conversation IDs whose spawned child subagents should be cleaned up while keeping the orchestrator itself protected.")
    p_clean.add_argument("--all-merged", action="store_true", help="Auto-discover merged/cleaned/superseded queue items, rotated idle orchestrators, finished subagents, non-queue merged fix/* chats, sidecar events, and orphans.")
    p_clean.add_argument("--include-noop-actions", action="store_true", help="Discover idle scheduled NO_OP action conversations older than --older-than-days (default: AGY_ACTION_CLEANUP_NOOP_HOURS=24h / 1d).")
    p_clean.add_argument("--include-actions", "--include-scheduled-actions", dest="include_scheduled_actions", action="store_true", help="Enable unified 24h NO_OP/deliverable + 72h unaudited WARN/ERROR cap scheduled-action retention.")
    p_clean.add_argument("--prune-ghost-summaries", dest="prune_ghost_summaries", action="store_true", help="Fail-closed offline prune of ghost rows in conversation_summaries.db (refuses if Antigravity Desktop/daemon or WAL lock is active).")
    p_clean.add_argument("--older-than-days", type=float, default=None, help="Minimum age in days for --include-noop-actions discovery (default: AGY_ACTION_CLEANUP_NOOP_HOURS / 24).")
    p_clean.add_argument("--branch", "--branches", dest="branches", nargs="*", default=[], help="Explicit branch names or `repo_dir::branch` specs to verify merged.")
    p_clean.add_argument("--exclude-id", "--exclude-ids", dest="exclude_ids", nargs="*", default=[], help="Additional conversation IDs to protect from deletion.")
    p_clean.add_argument("--min-idle-seconds", type=float, default=60.0, help="Minimum idle age in seconds before deleting conversation artifacts (default: 60).")
    p_clean.add_argument("--confirm-merged", action="store_true", help="Allow cleanup when merged branch refs were already pruned from origin and local.")
    p_clean.add_argument("--skip-unaudited-check", action="store_true", help="Skip conversation_analyzer check-unaudited lookup (useful in isolated tests).")
    p_clean.add_argument("--auto-archive", dest="auto_archive", action="store_true", help="Also auto-archive cancelled/compacted queue items and idle ad-hoc chats via ConversationService/UpdateConversation.")
    p_clean.add_argument("--adhoc-days", dest="adhoc_days", type=float, default=None, help="Override AGY_AUTO_ARCHIVE_ADHOC_DAYS threshold for ad-hoc chat archiving (default: 3, 0 disables).")
    p_clean.add_argument("--dry-run", action="store_true", help="Preview candidates and freed MB breakdown without deleting files.")

    p_archive = sub.add_parser(
        "auto-archive-conversations",
        help="Archive cancelled/compacted dev_queue items and idle unpinned/unstarred ad-hoc chats via ConversationService/UpdateConversation.",
    )
    p_archive.add_argument("--adhoc-days", dest="adhoc_days", type=float, default=None, help="Override AGY_AUTO_ARCHIVE_ADHOC_DAYS threshold in days (default: 3, 0 disables).")
    p_archive.add_argument("--no-cancelled-queue", dest="include_cancelled_queue", action="store_false", default=True, help="Skip archiving cancelled/compacted dev_queue items.")
    p_archive.add_argument("--exclude-id", "--exclude-ids", dest="exclude_ids", nargs="*", default=[], help="Additional conversation IDs to protect from archiving.")
    p_archive.add_argument("--min-idle-seconds", type=float, default=60.0, help="Minimum idle age in seconds before archiving (default: 60).")
    p_archive.add_argument("--skip-unaudited-check", action="store_true", help="Skip conversation_analyzer check-unaudited lookup.")
    p_archive.add_argument("--portfolio-dir", dest="portfolio_dir", default=None, help="Optional state/portfolio directory override.")
    p_archive.add_argument("--dry-run", action="store_true", help="Preview archive candidates without mutating conversations.")

    p_ghost = sub.add_parser(
        "prune-ghost-summaries",
        help="Fail-closed offline prune or detached worker control for ghost rows in conversation_summaries.db and app_storage.json.",
    )
    p_ghost.add_argument("--summaries-db", dest="summaries_db", default=None, help="Optional explicit conversation_summaries.db path.")
    p_ghost.add_argument("--app-storage", dest="app_storage", default=None, help="Optional explicit app_storage.json path.")
    p_ghost.add_argument("--portfolio-dir", dest="portfolio_dir", default=None, help="Optional state/portfolio directory override.")
    p_ghost.add_argument("--no-app-storage", dest="no_app_storage", action="store_true", help="Skip app_storage.json cleanup when pruning DB ghosts.")
    p_ghost.add_argument("--count-only", dest="count_only", action="store_true", help="Read-only ghost count across conversation_summaries.db and app_storage.json.")
    p_ghost.add_argument("--arm-ghost-purge", dest="arm_ghost_purge", action="store_true", help="Arm detached 0-LLM worker to purge ghost entries after Antigravity quits.")
    p_ghost.add_argument("--restart-and-purge", dest="restart_and_purge", action="store_true", help="Fail-closed guarded graceful quit, ghost purge, and Antigravity reopen via detached worker.")
    p_ghost.add_argument("--cancel-ghost-purge", dest="cancel_ghost_purge", action="store_true", help="Cancel any armed ghost purge and disarm the detached worker.")
    p_ghost.add_argument("--ghost-purge-worker", dest="ghost_purge_worker", action="store_true", help="Internal detached 0-LLM ghost-purge worker loop.")
    p_ghost.add_argument("--mode", dest="ghost_mode", default="on_quit", choices=("on_quit", "restart_now"), help="Ghost purge worker mode (default: on_quit).")
    p_ghost.add_argument("--poll-interval", dest="ghost_poll_interval", type=float, default=2.0, help="Worker poll interval in seconds (default: 2.0).")
    p_ghost.add_argument("--max-duration", dest="ghost_max_duration", type=float, default=86400.0, help="Maximum on_quit worker lifetime in seconds (default: 86400).")
    p_ghost.add_argument("--quit-timeout", dest="ghost_quit_timeout", type=float, default=60.0, help="Maximum restart_now quit wait in seconds (default: 60).")
    p_ghost.add_argument("--max-iterations", dest="ghost_max_iterations", type=int, default=None, help="Optional maximum worker poll iterations.")
    p_ghost.add_argument("--ttl-seconds", dest="ghost_ttl_seconds", type=float, default=None, help="Optional arm TTL override in seconds (default: AGY_GHOST_PURGE_ARM_TTL_SECONDS / 86400).")
    p_ghost.add_argument("--dry-run", action="store_true", help="Preview ghost CIDs without creating backup or modifying files.")


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

    if args.subcommand == "prune-ghost-summaries":
        p_dir = getattr(args, "portfolio_dir", None)
        s_db = getattr(args, "summaries_db", None)
        a_st = getattr(args, "app_storage", None)
        if getattr(args, "count_only", False):
            g_res = _facade_attr("count_ghost_sidebar_entries", count_ghost_sidebar_entries)(
                portfolio_dir=p_dir,
                summaries_db=s_db,
                app_storage_path=a_st,
            )
        elif getattr(args, "cancel_ghost_purge", False):
            g_res = _facade_attr("cancel_ghost_purge", cancel_ghost_purge)(
                portfolio_dir=p_dir,
            )
        elif getattr(args, "ghost_purge_worker", False):
            g_res = _facade_attr("run_ghost_purge_worker", run_ghost_purge_worker)(
                mode=str(getattr(args, "ghost_mode", "on_quit") or "on_quit"),
                poll_interval_seconds=float(getattr(args, "ghost_poll_interval", 2.0) or 2.0),
                max_duration_seconds=float(getattr(args, "ghost_max_duration", 86400.0) or 86400.0),
                quit_timeout_seconds=float(getattr(args, "ghost_quit_timeout", 60.0) or 60.0),
                max_iterations=getattr(args, "ghost_max_iterations", None),
                portfolio_dir=p_dir,
                summaries_db=s_db,
                app_storage_path=a_st,
            )
        elif getattr(args, "arm_ghost_purge", False):
            g_res = _facade_attr("arm_ghost_purge", arm_ghost_purge)(
                mode="on_quit",
                ttl_seconds=getattr(args, "ghost_ttl_seconds", None),
                portfolio_dir=p_dir,
                summaries_db=s_db,
                app_storage_path=a_st,
                spawn_worker=True,
                force_spawn=True,
            )
        elif getattr(args, "restart_and_purge", False):
            g_res = _facade_attr("restart_and_purge", restart_and_purge)(
                ttl_seconds=getattr(args, "ghost_ttl_seconds", None),
                portfolio_dir=p_dir,
                summaries_db=s_db,
                app_storage_path=a_st,
                spawn_worker=True,
                force_spawn=True,
            )
        else:
            g_res = _facade_attr("prune_ghost_conversation_summaries", prune_ghost_conversation_summaries)(
                dry_run=bool(getattr(args, "dry_run", False)),
                include_app_storage=not bool(getattr(args, "no_app_storage", False)),
                portfolio_dir=p_dir,
                summaries_db=s_db,
                app_storage_path=a_st,
            )
        print(json.dumps(g_res, indent=2, ensure_ascii=False))
        return 0 if g_res.get("status") in ("ok", "skipped") else 1

    if args.subcommand == "auto-archive-conversations":
        arch_res = _facade_attr("auto_archive_stale_conversations", auto_archive_stale_conversations)(
            adhoc_days=getattr(args, "adhoc_days", None),
            include_cancelled_queue=bool(getattr(args, "include_cancelled_queue", True)),
            exclude_ids=list(getattr(args, "exclude_ids", []) or []),
            min_idle_seconds=float(getattr(args, "min_idle_seconds", 60.0)),
            include_unaudited_check=not bool(getattr(args, "skip_unaudited_check", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            portfolio_dir=getattr(args, "portfolio_dir", None),
        )
        print(json.dumps(arch_res, indent=2, ensure_ascii=False))
        return 0 if arch_res.get("status") in ("ok", "skipped") else 1

    if args.subcommand == "cleanup-merged-conversations":
        prune_ghosts = bool(getattr(args, "prune_ghost_summaries", False))
        do_auto_archive = bool(getattr(args, "auto_archive", False))
        has_clean_scope = any(
            (
                args.conversation_ids,
                args.subagents_only_of,
                getattr(args, "all_merged", False),
                getattr(args, "include_noop_actions", False),
                getattr(args, "include_scheduled_actions", False),
            )
        )
        if do_auto_archive and not has_clean_scope and not prune_ghosts:
            arch_res = _facade_attr("auto_archive_stale_conversations", auto_archive_stale_conversations)(
                adhoc_days=getattr(args, "adhoc_days", None),
                exclude_ids=list(getattr(args, "exclude_ids", []) or []),
                min_idle_seconds=float(getattr(args, "min_idle_seconds", 60.0)),
                include_unaudited_check=not bool(getattr(args, "skip_unaudited_check", False)),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
            print(json.dumps(arch_res, indent=2, ensure_ascii=False))
            return 0 if arch_res.get("status") in ("ok", "skipped") else 1

        if prune_ghosts and not has_clean_scope:
            g_res = _facade_attr("prune_ghost_conversation_summaries", prune_ghost_conversation_summaries)(
                dry_run=bool(args.dry_run),
            )
            print(json.dumps(g_res, indent=2, ensure_ascii=False))
            return 0 if g_res.get("status") == "ok" else 1

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
            include_scheduled_actions=bool(getattr(args, "include_scheduled_actions", False)),
            older_than_days=getattr(args, "older_than_days", None),
        )
        if prune_ghosts and c_res.get("status") == "ok":
            g_res = _facade_attr("prune_ghost_conversation_summaries", prune_ghost_conversation_summaries)(
                dry_run=bool(args.dry_run),
            )
            c_res["ghost_summaries_prune"] = g_res
            if g_res.get("status") != "ok":
                c_res["status"] = g_res.get("status", "blocked")
                print(json.dumps(c_res, indent=2, ensure_ascii=False))
                return 1
        if do_auto_archive and c_res.get("status") == "ok":
            c_res["auto_archive"] = _facade_attr(
                "auto_archive_stale_conversations",
                auto_archive_stale_conversations,
            )(
                adhoc_days=getattr(args, "adhoc_days", None),
                exclude_ids=list(getattr(args, "exclude_ids", []) or []),
                min_idle_seconds=float(getattr(args, "min_idle_seconds", 60.0)),
                include_unaudited_check=not bool(getattr(args, "skip_unaudited_check", False)),
                dry_run=bool(getattr(args, "dry_run", False)),
            )
        print(json.dumps(c_res, indent=2, ensure_ascii=False))
        return 0 if c_res.get("status") == "ok" else 1

    return None
