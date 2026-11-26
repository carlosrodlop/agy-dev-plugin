#!/usr/bin/env python3
"""Atomic `0600` Orchestration Pause / Continue (Graceful Drain) Control (`orchestration_control.py`).

Pure Python 3 stdlib module (Santa Lockdown compliant, Unix portable, Zero-PII) providing a
single source of truth (`<state_dir>/dev_orchestration_control.json`) shared across:
  - `queue_store.py` (`queue-next --unlocked` returns zero new runnable dispatches while paused)
  - `worktree_ops.py` (`start-remediation` refuses new locks while allowing `--reuse-active` on ongoing tasks)
  - `merge_watcher.py` (`wait-for-unlock` sleeps silently while paused and wakes immediately on resume;
    `watch-pr-merge` defers `deploy-home` / API restarts while paused)
  - `dev_queue_doctor.py` & `sidecars/dev-queue-watchdog/watchdog.py` (suppresses wake-ups while paused;
    triggers fast force-wake on unhandled resume requests when no `wait-for-unlock` waiter is alive)
  - `sidecars/dev-queue-panel/` & `dev_queue_tui.py` / `agy-queue` CLI controls.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import uuid
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

import dev_common  # noqa: E402

ORCHESTRATION_CONTROL_SCHEMA_VERSION = 1
ORCHESTRATION_CONTROL_FILENAME = "dev_orchestration_control.json"
ORCHESTRATION_CONTROL_LOCK_FILENAME = "dev_orchestration_control.lock"
PAUSE_MODE_GRACEFUL_DRAIN = "graceful_drain"
PAUSE_MODE_HARD_STOP = "hard_stop"
VALID_PAUSE_MODES: frozenset[str] = frozenset({PAUSE_MODE_GRACEFUL_DRAIN, PAUSE_MODE_HARD_STOP})
DEFAULT_PAUSED_POLL_INTERVAL_SECONDS = 3.0
MAX_CONTROL_REASON_LENGTH = 160
ALLOWED_CONTROL_SOURCES: frozenset[str] = frozenset(
    {"sidecar_panel", "tui", "cli", "orchestrator", "api", "manual"}
)
NON_ONGOING_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "PENDING_HUMAN_PR_VALIDATION",
        "READY_AWAITING_BASE_MERGE",
        "ESCALATED_MAX_REVIEW_ITERATIONS",
        "RELEASED",
        "INACTIVE",
    }
)

_LOCAL_HOME_PATH_RE = re.compile(r"/(?:Users|home)/(?!testuser\b|test\b|Shared\b)[A-Za-z0-9._-]+")
_CHROME_DEVTOOLS_MCP_PROC_RE = re.compile(r"(?:^|[/\s\"'=])chrome[-_]devtools[-_]mcp(?:\b|\.js\b)", re.IGNORECASE)
_CHROME_DEVTOOLS_EXCLUDED_RE = re.compile(
    r"Google Chrome\.app|Antigravity\.app/Contents/MacOS/Antigravity\b|language_server\b|watchdog\.py\b|dev_queue_tui\.py\b",
    re.IGNORECASE,
)
_WORKTREE_SUBPROC_RE = re.compile(r"(?:pr-worktree-[A-Za-z0-9._-]+|dev_ui_preview)", re.IGNORECASE)
_WORKTREE_EXCLUDED_PROC_RE = re.compile(
    r"Google Chrome\.app|Antigravity\.app|language_server\b|watchdog\.py\b|dev_queue_tui\.py\b|"
    r"dev-queue-panel/server\.py\b|\bwait-for-unlock\b|\bwatch-pr-merge\b|\bapi\.server\s+serve\b",
    re.IGNORECASE,
)


def stop_chrome_devtools_mcp_processes(
    *,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    kill_fn: Callable[[int, int], None] | None = None,
    self_pid: int | None = None,
) -> dict[str, Any]:
    """Terminate running `chrome-devtools-mcp` helper processes on pause without touching Chrome browser tabs."""
    if ps_output is None and ps_runner is None and kill_fn is None and (
        "unittest" in sys.modules or "pytest" in sys.modules
    ):
        return {
            "status": "skipped_hermetic_test",
            "stopped_pids": [],
            "hook_gate_active": True,
            "errors": [],
        }

    raw_ps = ""
    if ps_output is not None:
        raw_ps = str(ps_output)
    elif ps_runner is not None:
        with contextlib.suppress(Exception):
            raw_ps = str(ps_runner() or "")
    else:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            proc = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode == 0:
                raw_ps = proc.stdout or ""

    cur_pid = int(self_pid if self_pid is not None else os.getpid())
    killer = kill_fn if kill_fn is not None else os.kill
    stopped_pids: list[int] = []
    errors: list[str] = []

    for line in raw_ps.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        cmd = parts[1].strip()
        if pid <= 1 or pid == cur_pid:
            continue
        if not _CHROME_DEVTOOLS_MCP_PROC_RE.search(cmd):
            continue
        if _CHROME_DEVTOOLS_EXCLUDED_RE.search(cmd):
            continue
        try:
            killer(pid, signal.SIGTERM)
            stopped_pids.append(pid)
        except ProcessLookupError:
            continue
        except OSError as exc:
            errors.append(f"pid={pid}:{exc.__class__.__name__}")

    return {
        "status": "stopped" if stopped_pids else "none_running",
        "stopped_pids": stopped_pids,
        "hook_gate_active": True,
        "errors": errors,
    }


def stop_active_worktree_processes(
    *,
    state_dir: Path | str | None = None,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
    active_worktrees: list[str] | tuple[str, ...] | None = None,
    stop_dev_server: bool = True,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    kill_fn: Callable[[int, int], None] | None = None,
    self_pid: int | None = None,
) -> dict[str, Any]:
    """Terminate child processes running inside `pr-worktree-*` or preview dev servers without touching Control Tower prod (`8787`) or Antigravity."""
    if ps_output is None and ps_runner is None and kill_fn is None and (
        "unittest" in sys.modules or "pytest" in sys.modules
    ):
        return {
            "status": "skipped_hermetic_test",
            "stopped_pids": [],
            "dev_server": {"status": "skipped", "stopped": False},
            "errors": [],
        }

    raw_ps = ""
    if ps_output is not None:
        raw_ps = str(ps_output)
    elif ps_runner is not None:
        with contextlib.suppress(Exception):
            raw_ps = str(ps_runner() or "")
    else:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            proc = subprocess.run(
                ["ps", "-eo", "pid=,args="],
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if proc.returncode == 0:
                raw_ps = proc.stdout or ""

    base_home = str((home_dir or Path.home()).expanduser())
    wt_tokens: list[str] = []
    for raw_wt in active_worktrees or ():
        clean_wt = str(raw_wt or "").strip()
        if not clean_wt:
            continue
        expanded = clean_wt.replace("$HOME", base_home)
        if expanded not in wt_tokens:
            wt_tokens.append(expanded)
        wt_name = Path(expanded).name
        if wt_name and wt_name not in wt_tokens:
            wt_tokens.append(wt_name)

    cur_pid = int(self_pid if self_pid is not None else os.getpid())
    parent_pid = int(os.getppid()) if self_pid is None else -1
    killer = kill_fn if kill_fn is not None else os.kill
    stopped_pids: list[int] = []
    errors: list[str] = []

    for line in raw_ps.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        cmd = parts[1].strip()
        if pid <= 1 or pid == cur_pid or (parent_pid > 1 and pid == parent_pid):
            continue
        if _WORKTREE_EXCLUDED_PROC_RE.search(cmd):
            continue
        matched = bool(_WORKTREE_SUBPROC_RE.search(cmd)) or any(tok in cmd for tok in wt_tokens)
        if not matched:
            continue
        try:
            killer(pid, signal.SIGTERM)
            stopped_pids.append(pid)
        except ProcessLookupError:
            continue
        except OSError as exc:
            errors.append(f"pid={pid}:{exc.__class__.__name__}")

    dev_server_res: dict[str, Any] = {"status": "skipped", "stopped": False}
    if stop_dev_server and ps_output is None and ps_runner is None and kill_fn is None:
        resolved_dir = resolve_control_state_dir(state_dir, portfolio_dir=portfolio_dir, home_dir=home_dir)
        with contextlib.suppress(Exception):
            dev_server_res = dev_common.try_stop_dev_api_server(portfolio_dir=resolved_dir)

    return {
        "status": "stopped" if (stopped_pids or dev_server_res.get("stopped")) else "none_running",
        "stopped_pids": stopped_pids,
        "dev_server": dev_server_res,
        "errors": errors,
    }


def _utc_now_iso(now_ts: float | None = None) -> str:
    dt = (
        datetime.fromtimestamp(float(now_ts), tz=timezone.utc)
        if now_ts is not None
        else datetime.now(timezone.utc)
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_compact_stamp(now_ts: float | None = None) -> str:
    dt = (
        datetime.fromtimestamp(float(now_ts), tz=timezone.utc)
        if now_ts is not None
        else datetime.now(timezone.utc)
    )
    return dt.strftime("%Y%m%d%H%M%S")


def sanitize_control_text(text: str, *, home_dir: Path | None = None) -> str:
    """Replace local user home paths with `$HOME` to preserve Zero-PII control state."""
    if not text:
        return ""
    cleaned = str(text)
    home_variants: list[str] = []
    for cand_home in ([home_dir] if home_dir is not None else []) + [Path.home()]:
        if cand_home is None:
            continue
        with contextlib.suppress(OSError):
            expanded = cand_home.expanduser()
            for variant in (str(expanded.resolve()), str(expanded)):
                if variant and variant != "/" and variant not in home_variants:
                    home_variants.append(variant)
    for variant in sorted(home_variants, key=len, reverse=True):
        cleaned = cleaned.replace(variant, "$HOME")
    return _LOCAL_HOME_PATH_RE.sub("$HOME", cleaned)


def sanitize_control_reason(reason: str, *, home_dir: Path | None = None) -> str:
    """Normalize and bound optional operator pause reason while scrubbing local paths."""
    collapsed = " ".join(str(reason or "").split()).strip()
    if not collapsed:
        return ""
    scrubbed = sanitize_control_text(collapsed, home_dir=home_dir)
    if len(scrubbed) > MAX_CONTROL_REASON_LENGTH:
        return scrubbed[: MAX_CONTROL_REASON_LENGTH - 1].rstrip() + "…"
    return scrubbed


def normalize_control_source(source: str) -> str:
    """Normalize control mutation origin label to a compact allowlisted tag."""
    cleaned = str(source or "").strip().lower()
    if cleaned in ALLOWED_CONTROL_SOURCES:
        return cleaned
    return "cli"


def resolve_control_state_dir(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> Path:
    """Resolve the runtime state directory holding `dev_orchestration_control.json`."""
    explicit = state_dir if state_dir is not None else portfolio_dir
    return dev_common.resolve_state_dir(explicit, home_dir=home_dir)


def get_orchestration_control_path(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> Path:
    """Return the path to `dev_orchestration_control.json` under the resolved state directory."""
    return resolve_control_state_dir(state_dir, portfolio_dir=portfolio_dir, home_dir=home_dir) / ORCHESTRATION_CONTROL_FILENAME


def default_orchestration_control() -> dict[str, Any]:
    """Return the canonical unpaused default control document."""
    return {
        "schema_version": ORCHESTRATION_CONTROL_SCHEMA_VERSION,
        "paused": False,
        "mode": PAUSE_MODE_GRACEFUL_DRAIN,
        "chrome_devtools_disabled": False,
        "reason": "",
        "source": "",
        "paused_at": "",
        "hard_stopped_at": "",
        "resumed_at": "",
        "resume_nonce": "",
        "resume_handled_nonce": "",
        "resume_handled_at": "",
        "resume_handled_by": "",
        "updated_at": "",
    }


def _normalize_control_dict(raw: Any, *, home_dir: Path | None = None) -> dict[str, Any]:
    """Coerce raw parsed JSON into a normalized, Zero-PII orchestration control snapshot."""
    base = default_orchestration_control()
    if not isinstance(raw, Mapping):
        return base
    base["paused"] = bool(raw.get("paused"))
    base["chrome_devtools_disabled"] = bool(raw.get("chrome_devtools_disabled", base["paused"]))
    mode_val = str(raw.get("mode") or PAUSE_MODE_GRACEFUL_DRAIN).strip().lower()
    base["mode"] = mode_val if mode_val in VALID_PAUSE_MODES else PAUSE_MODE_GRACEFUL_DRAIN
    base["reason"] = sanitize_control_reason(str(raw.get("reason") or ""), home_dir=home_dir)
    raw_source = str(raw.get("source") or "").strip().lower()
    base["source"] = raw_source if raw_source in ALLOWED_CONTROL_SOURCES else ""
    for ts_key in ("paused_at", "hard_stopped_at", "resumed_at", "resume_handled_at", "updated_at"):
        base[ts_key] = str(raw.get(ts_key) or "").strip()
    for nonce_key in ("resume_nonce", "resume_handled_nonce", "resume_handled_by"):
        base[nonce_key] = str(raw.get(nonce_key) or "").strip()
    return base


def load_orchestration_control(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Read `dev_orchestration_control.json` without creating or mutating files on disk."""
    ctrl_path = get_orchestration_control_path(state_dir, portfolio_dir=portfolio_dir, home_dir=home_dir)
    raw = dev_common.atomic_read_json(ctrl_path, default=None)
    return _normalize_control_dict(raw, home_dir=home_dir)


def is_orchestration_paused(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> bool:
    """Return True when orchestration is currently paused (`Graceful Drain` or `Hard Stop`)."""
    return bool(load_orchestration_control(state_dir, portfolio_dir=portfolio_dir, home_dir=home_dir).get("paused"))


def is_hard_stop_active(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> bool:
    """Return True iff orchestration is paused in non-destructive `hard_stop` (Hard Freeze) mode."""
    ctrl = load_orchestration_control(state_dir, portfolio_dir=portfolio_dir, home_dir=home_dir)
    return bool(ctrl.get("paused")) and str(ctrl.get("mode") or "").strip().lower() == PAUSE_MODE_HARD_STOP


def has_unhandled_resume_request(control: Mapping[str, Any] | None) -> bool:
    """Return True when an explicit Continue/Resume nonce has not yet been acknowledged by a wake handler."""
    if not isinstance(control, Mapping) or bool(control.get("paused")):
        return False
    nonce = str(control.get("resume_nonce") or "").strip()
    if not nonce:
        return False
    handled = str(control.get("resume_handled_nonce") or "").strip()
    return nonce != handled


def count_ongoing_lock_slots(active_locks: Mapping[str, Any] | None) -> int:
    """Count active lock slots that are still actively executing (developing/reviewing), excluding PR-ready/escalated slots."""
    if not isinstance(active_locks, Mapping):
        return 0
    ongoing = 0
    for lk in active_locks.values():
        if not isinstance(lk, Mapping) or not bool(lk.get("active")):
            continue
        slots_map = lk.get("slots")
        slot_entries: list[Mapping[str, Any]] = []
        if isinstance(slots_map, Mapping) and slots_map:
            for slot_val in slots_map.values():
                if isinstance(slot_val, Mapping) and bool(slot_val.get("active", True)):
                    slot_entries.append(slot_val)
        else:
            slot_entries.append(lk)
        for slot in slot_entries:
            st_u = str(slot.get("status") or "").strip().upper()
            if bool(slot.get("pr_gate_ready")) or st_u in NON_ONGOING_LOCK_STATUSES:
                continue
            ongoing += 1
    return ongoing


def summarize_orchestration_phase(
    control: Mapping[str, Any] | None,
    active_locks: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a unified Zero-PII status view (`active` | `draining` | `paused` | `hard_stopped`) across UI, TUI, and CLI."""
    norm = _normalize_control_dict(control)
    paused = bool(norm.get("paused"))
    mode = str(norm.get("mode") or PAUSE_MODE_GRACEFUL_DRAIN).strip().lower()
    hard_stopped = paused and mode == PAUSE_MODE_HARD_STOP
    ongoing = count_ongoing_lock_slots(active_locks)
    if not paused:
        phase = "active"
        banner_text = ""
    elif hard_stopped:
        phase = "hard_stopped"
        if ongoing > 0:
            task_word = "task" if ongoing == 1 else "tasks"
            banner_text = (
                f"HARD STOPPED — {ongoing} frozen {task_word} (locks & worktrees preserved) · "
                "Workers & restarts blocked · Chrome DevTools OFF"
            )
        else:
            banner_text = "HARD STOPPED — 0 active tasks · Workers & restarts blocked · Chrome DevTools OFF"
    elif ongoing > 0:
        phase = "draining"
        task_word = "task" if ongoing == 1 else "tasks"
        banner_text = f"DRAINING — Finishing {ongoing} ongoing {task_word} · 0 new dispatches · Chrome DevTools OFF"
    else:
        phase = "paused"
        banner_text = "PAUSED — 0 ongoing · 0 new dispatches · Chrome DevTools OFF · Watchdog silent"
    return {
        **norm,
        "phase": phase,
        "hard_stopped": hard_stopped,
        "ongoing_count": ongoing,
        "new_dispatches_blocked": paused,
        "chrome_devtools_disabled": paused,
        "banner_text": banner_text,
    }


def _collect_active_worktree_paths(active_locks: Mapping[str, Any] | None) -> list[str]:
    """Extract unique active worktree paths from `dev_pr_locks` mapping."""
    if not isinstance(active_locks, Mapping):
        return []
    paths: list[str] = []
    for lk in active_locks.values():
        if not isinstance(lk, Mapping) or not bool(lk.get("active")):
            continue
        slots_map = lk.get("slots")
        entries: list[Mapping[str, Any]] = []
        if isinstance(slots_map, Mapping) and slots_map:
            entries.extend(v for v in slots_map.values() if isinstance(v, Mapping) and bool(v.get("active", True)))
        else:
            entries.append(lk)
        for entry in entries:
            wt = str(entry.get("worktree_path") or "").strip()
            if wt and wt not in paths:
                paths.append(wt)
    return paths


def hard_stop_orchestration(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
    reason: str = "",
    source: str = "cli",
    stop_chrome_devtools: bool = True,
    stop_worktree_processes: bool = True,
    active_locks: Mapping[str, Any] | None = None,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    kill_fn: Callable[[int, int], None] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Atomically activate non-destructive Hard Stop (`Hard Freeze`) preserving locks & worktrees while stopping active worker execution."""
    resolved_dir = resolve_control_state_dir(state_dir, portfolio_dir=portfolio_dir, home_dir=home_dir)
    ctrl_path = resolved_dir / ORCHESTRATION_CONTROL_FILENAME
    lock_path = resolved_dir / ORCHESTRATION_CONTROL_LOCK_FILENAME
    now_iso = _utc_now_iso(now_ts)
    clean_reason = sanitize_control_reason(reason, home_dir=home_dir)
    clean_source = normalize_control_source(source)

    with dev_common.file_lock(lock_path):
        prev = _normalize_control_dict(dev_common.atomic_read_json(ctrl_path, default=None), home_dir=home_dir)
        was_paused = bool(prev.get("paused"))
        was_hard_stopped = was_paused and str(prev.get("mode") or "") == PAUSE_MODE_HARD_STOP
        updated = dict(prev)
        updated["schema_version"] = ORCHESTRATION_CONTROL_SCHEMA_VERSION
        updated["paused"] = True
        updated["mode"] = PAUSE_MODE_HARD_STOP
        updated["chrome_devtools_disabled"] = True
        updated["source"] = clean_source
        if clean_reason or not was_paused:
            updated["reason"] = clean_reason
        if not was_paused or not updated.get("paused_at"):
            updated["paused_at"] = now_iso
        if not was_hard_stopped or not updated.get("hard_stopped_at"):
            updated["hard_stopped_at"] = now_iso
        updated["updated_at"] = now_iso
        dev_common.atomic_write_json(ctrl_path, updated, mode=0o600)

    if stop_chrome_devtools:
        chrome_devtools_res = stop_chrome_devtools_mcp_processes(
            ps_output=ps_output,
            ps_runner=ps_runner,
            kill_fn=kill_fn,
        )
    else:
        chrome_devtools_res = {"status": "skipped", "stopped_pids": [], "hook_gate_active": True, "errors": []}

    eff_locks = active_locks
    if eff_locks is None and stop_worktree_processes:
        with contextlib.suppress(Exception):
            import lock_store as _ls

            eff_locks = _ls.get_all_dev_pr_locks(portfolio_dir=resolved_dir)

    if stop_worktree_processes:
        worktree_procs_res = stop_active_worktree_processes(
            state_dir=resolved_dir,
            home_dir=home_dir,
            active_worktrees=_collect_active_worktree_paths(eff_locks),
            ps_output=ps_output,
            ps_runner=ps_runner,
            kill_fn=kill_fn,
        )
    else:
        worktree_procs_res = {"status": "skipped", "stopped_pids": [], "dev_server": {"status": "skipped", "stopped": False}, "errors": []}

    return {
        "status": "ok",
        "paused": True,
        "hard_stopped": True,
        "mode": PAUSE_MODE_HARD_STOP,
        "changed": not was_hard_stopped,
        "was_paused": was_paused,
        "was_hard_stopped": was_hard_stopped,
        "chrome_devtools": chrome_devtools_res,
        "worktree_processes": worktree_procs_res,
        "control": updated,
        "control_path": sanitize_control_text(str(ctrl_path), home_dir=home_dir),
    }


def pause_orchestration(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
    reason: str = "",
    source: str = "cli",
    stop_chrome_devtools: bool = True,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    kill_fn: Callable[[int, int], None] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Atomically set orchestration to paused (`Graceful Drain`) with strict `0600` permissions and stop Chrome DevTools MCP."""
    resolved_dir = resolve_control_state_dir(state_dir, portfolio_dir=portfolio_dir, home_dir=home_dir)
    ctrl_path = resolved_dir / ORCHESTRATION_CONTROL_FILENAME
    lock_path = resolved_dir / ORCHESTRATION_CONTROL_LOCK_FILENAME
    now_iso = _utc_now_iso(now_ts)
    clean_reason = sanitize_control_reason(reason, home_dir=home_dir)
    clean_source = normalize_control_source(source)

    with dev_common.file_lock(lock_path):
        prev = _normalize_control_dict(dev_common.atomic_read_json(ctrl_path, default=None), home_dir=home_dir)
        was_paused = bool(prev.get("paused"))
        updated = dict(prev)
        updated["schema_version"] = ORCHESTRATION_CONTROL_SCHEMA_VERSION
        updated["paused"] = True
        updated["mode"] = PAUSE_MODE_GRACEFUL_DRAIN
        updated["chrome_devtools_disabled"] = True
        updated["source"] = clean_source
        if clean_reason or not was_paused:
            updated["reason"] = clean_reason
        if not was_paused or not updated.get("paused_at"):
            updated["paused_at"] = now_iso
        updated["updated_at"] = now_iso
        dev_common.atomic_write_json(ctrl_path, updated, mode=0o600)

    chrome_devtools_res: dict[str, Any]
    if stop_chrome_devtools:
        chrome_devtools_res = stop_chrome_devtools_mcp_processes(
            ps_output=ps_output,
            ps_runner=ps_runner,
            kill_fn=kill_fn,
        )
    else:
        chrome_devtools_res = {"status": "skipped", "stopped_pids": [], "hook_gate_active": True, "errors": []}

    return {
        "status": "ok",
        "paused": True,
        "changed": not was_paused,
        "was_paused": was_paused,
        "chrome_devtools": chrome_devtools_res,
        "control": updated,
        "control_path": sanitize_control_text(str(ctrl_path), home_dir=home_dir),
    }


def resume_orchestration(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
    source: str = "cli",
    request_wake: bool = True,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Atomically resume orchestration and stamp a fresh `resume_nonce` for `<=3s` reactive wake-up."""
    resolved_dir = resolve_control_state_dir(state_dir, portfolio_dir=portfolio_dir, home_dir=home_dir)
    ctrl_path = resolved_dir / ORCHESTRATION_CONTROL_FILENAME
    lock_path = resolved_dir / ORCHESTRATION_CONTROL_LOCK_FILENAME
    now_iso = _utc_now_iso(now_ts)
    clean_source = normalize_control_source(source)
    nonce = f"{_utc_compact_stamp(now_ts)}-{uuid.uuid4().hex[:8]}"

    with dev_common.file_lock(lock_path):
        prev = _normalize_control_dict(dev_common.atomic_read_json(ctrl_path, default=None), home_dir=home_dir)
        was_paused = bool(prev.get("paused"))
        updated = dict(prev)
        updated["schema_version"] = ORCHESTRATION_CONTROL_SCHEMA_VERSION
        updated["paused"] = False
        updated["mode"] = PAUSE_MODE_GRACEFUL_DRAIN
        updated["chrome_devtools_disabled"] = False
        updated["reason"] = ""
        updated["hard_stopped_at"] = ""
        updated["source"] = clean_source
        updated["resumed_at"] = now_iso
        updated["resume_nonce"] = nonce
        if not request_wake:
            updated["resume_handled_nonce"] = nonce
            updated["resume_handled_at"] = now_iso
            updated["resume_handled_by"] = f"{clean_source}_no_wake"
        updated["updated_at"] = now_iso
        dev_common.atomic_write_json(ctrl_path, updated, mode=0o600)

    return {
        "status": "ok",
        "paused": False,
        "changed": was_paused,
        "was_paused": was_paused,
        "resume_nonce": nonce,
        "control": updated,
        "control_path": sanitize_control_text(str(ctrl_path), home_dir=home_dir),
    }


def mark_resume_handled(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
    resume_nonce: str,
    handled_by: str = "",
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Acknowledge a specific `resume_nonce` so neither `wait-for-unlock` nor `watchdog.py` double-wakes."""
    nonce_clean = str(resume_nonce or "").strip()
    if not nonce_clean:
        return {"status": "ok", "acknowledged": False, "reason": "missing_resume_nonce"}

    resolved_dir = resolve_control_state_dir(state_dir, portfolio_dir=portfolio_dir, home_dir=home_dir)
    ctrl_path = resolved_dir / ORCHESTRATION_CONTROL_FILENAME
    lock_path = resolved_dir / ORCHESTRATION_CONTROL_LOCK_FILENAME
    now_iso = _utc_now_iso(now_ts)

    with dev_common.file_lock(lock_path):
        cur = _normalize_control_dict(dev_common.atomic_read_json(ctrl_path, default=None), home_dir=home_dir)
        if bool(cur.get("paused")):
            return {"status": "ok", "acknowledged": False, "reason": "currently_paused", "control": cur}
        cur_nonce = str(cur.get("resume_nonce") or "").strip()
        if cur_nonce != nonce_clean:
            return {"status": "ok", "acknowledged": False, "reason": "nonce_mismatch", "control": cur}
        if str(cur.get("resume_handled_nonce") or "").strip() == nonce_clean:
            return {"status": "ok", "acknowledged": False, "reason": "already_handled", "control": cur}

        cur["resume_handled_nonce"] = nonce_clean
        cur["resume_handled_at"] = now_iso
        cur["resume_handled_by"] = str(handled_by or "unknown").strip()[:64]
        cur["updated_at"] = now_iso
        dev_common.atomic_write_json(ctrl_path, cur, mode=0o600)

    return {
        "status": "ok",
        "acknowledged": True,
        "resume_nonce": nonce_clean,
        "handled_by": cur["resume_handled_by"],
        "control": cur,
    }


def register_control_cli(sub: Any) -> None:
    """Register `orchestration-status`, `orchestration-pause`, `orchestration-stop`, and `orchestration-resume` CLI subcommands."""
    p_status = sub.add_parser(
        "orchestration-status",
        help="Show current orchestration Pause / Hard Stop / Continue control state.",
    )
    p_status.add_argument("--portfolio-dir", default="", help="Optional state directory override.")

    p_pause = sub.add_parser(
        "orchestration-pause",
        help="Pause orchestration (Graceful Drain by default, or Hard Freeze with --hard).",
    )
    p_pause.add_argument("--reason", default="", help="Optional short Zero-PII reason (e.g. 'Customer focus').")
    p_pause.add_argument(
        "--hard",
        dest="hard",
        action="store_true",
        help="Activate Hard Stop (Hard Freeze: cut off active workers & kill worktree subprocesses while preserving locks).",
    )
    p_pause.add_argument("--source", default="cli", help="Mutation origin tag (cli, tui, sidecar_panel).")
    p_pause.add_argument("--portfolio-dir", default="", help="Optional state directory override.")

    p_stop = sub.add_parser(
        "orchestration-stop",
        help="Activate non-destructive Hard Stop (Hard Freeze: freeze active workers & kill worktree processes, preserving locks/worktrees).",
    )
    p_stop.add_argument("--reason", default="", help="Optional short Zero-PII reason (e.g. 'Live demo').")
    p_stop.add_argument("--source", default="cli", help="Mutation origin tag (cli, tui, sidecar_panel).")
    p_stop.add_argument("--portfolio-dir", default="", help="Optional state directory override.")

    p_resume = sub.add_parser(
        "orchestration-resume",
        help="Continue orchestration and wake the orchestrator automatically (via wait-for-unlock or doctor wake).",
    )
    p_resume.add_argument("--source", default="cli", help="Mutation origin tag (cli, tui, sidecar_panel).")
    p_resume.add_argument(
        "--no-wake",
        dest="no_wake",
        action="store_true",
        help="Unpause passively without triggering an automatic orchestrator wake-up.",
    )
    p_resume.add_argument("--dry-run", dest="dry_run", action="store_true", help="Preview wake command without spawning.")
    p_resume.add_argument("--portfolio-dir", default="", help="Optional state directory override.")


def dispatch_control_cli(args: Any) -> int | None:
    """Dispatch `orchestration-status`, `orchestration-pause`, `orchestration-stop`, and `orchestration-resume` commands."""
    subcmd = getattr(args, "subcommand", "")
    if subcmd not in ("orchestration-status", "orchestration-pause", "orchestration-stop", "orchestration-resume"):
        return None

    state_override: Path | str | None = str(getattr(args, "portfolio_dir", "") or "").strip() or None

    if subcmd == "orchestration-status":
        ctrl = load_orchestration_control(state_dir=state_override)
        ctrl_path = get_orchestration_control_path(state_dir=state_override)
        active_locks: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            import lock_store as _ls

            active_locks = _ls.get_all_dev_pr_locks(portfolio_dir=state_override)
        summary = summarize_orchestration_phase(ctrl, active_locks)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "paused": bool(summary.get("paused")),
                    "hard_stopped": bool(summary.get("hard_stopped")),
                    "mode": summary.get("mode"),
                    "phase": summary.get("phase"),
                    "ongoing_count": summary.get("ongoing_count"),
                    "new_dispatches_blocked": summary.get("new_dispatches_blocked"),
                    "banner_text": summary.get("banner_text"),
                    "control": summary,
                    "control_path": sanitize_control_text(str(ctrl_path)),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if subcmd == "orchestration-stop" or (subcmd == "orchestration-pause" and bool(getattr(args, "hard", False))):
        res = hard_stop_orchestration(
            state_dir=state_override,
            reason=str(getattr(args, "reason", "") or ""),
            source=str(getattr(args, "source", "cli") or "cli"),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    if subcmd == "orchestration-pause":
        res = pause_orchestration(
            state_dir=state_override,
            reason=str(getattr(args, "reason", "") or ""),
            source=str(getattr(args, "source", "cli") or "cli"),
        )
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0

    # orchestration-resume
    no_wake = bool(getattr(args, "no_wake", False))
    dry_run = bool(getattr(args, "dry_run", False))
    res = resume_orchestration(
        state_dir=state_override,
        source=str(getattr(args, "source", "cli") or "cli"),
        request_wake=not no_wake,
    )
    wake_info: dict[str, Any] = {"status": "skipped_no_wake" if no_wake else "pending_watchdog", "woke": False}
    if not no_wake:
        try:
            import dev_queue_doctor as _doc

            procs = _doc.inspect_live_processes()
            if procs.get("wait_for_unlock_running"):
                wake_info = {
                    "status": "delegated_to_wait_for_unlock",
                    "woke": False,
                    "wait_for_unlock_pids": list(procs.get("wait_for_unlock_pids") or []),
                }
            else:
                diag = _doc.diagnose_orchestrator(state_dir=state_override, sync_git=False)
                wake_info = _doc.trigger_orchestrator_wake(
                    diag,
                    state_dir=state_override,
                    force=True,
                    dry_run=dry_run,
                )
                if (wake_info.get("woke") or wake_info.get("status") == "skipped_wake_in_flight") and not dry_run:
                    mark_resume_handled(
                        state_dir=state_override,
                        resume_nonce=str(res.get("resume_nonce") or ""),
                        handled_by="cli_resume",
                    )
        except Exception as exc:
            wake_info = {
                "status": "error",
                "woke": False,
                "error": sanitize_control_text(f"{exc.__class__.__name__}: {exc}"),
            }
    res["wake"] = wake_info
    print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0
