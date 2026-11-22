#!/usr/bin/env python3
"""Deterministic 0-LLM Orchestrator Doctor, Safe Runtime Auto-Heal & Cooldown-Gated Wake-Up.

Pure Python 3 stdlib module (Santa Lockdown compliant, Unix portable, Zero-PII) providing:
  1. `diagnose_orchestrator`: 0-LLM full diagnosis across `dev_pr_locks`, `dev_queue.json`,
     runnable repos (`queue-next --unlocked`), `dev_flow_optimizer.analyze_dev_flow()`,
     live OS processes (`wait-for-unlock`, `watch-pr-merge`), and tail-bounded transcripts
     across Desktop + CLI `brain/` directories (`server_restart_interrupted`,
     `idle_orchestrator_with_runnable_work`, `stalled_worker`, `missing_wait_for_unlock`,
     `phantom_pr_open`).
  2. `apply_safe_fixes`: safe runtime-only reconciliations (`sync_dev_pr_lock_with_git` and
     `reconcile_dev_queue_with_git`) that NEVER mutate tracked source files or push to `main`.
  3. `trigger_orchestrator_wake`: cooldown- and live-PID-gated detached hybrid wake-up
     (`agentapi new-conversation` inside Antigravity Desktop/sidecars when `ANTIGRAVITY_LS_ADDRESS`
     is set; `agy --agent agy-dev-orchestrator --dangerously-skip-permissions -p` from external
     terminals; `agy_doctor_state.json` `0600` + `file_lock`) with `--dry-run` & `--force`.
  4. `render_doctor_report`: crisp 3-part terminal report for CLI and `dev_queue_tui.py` `[d]`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
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
import dev_flow_optimizer  # noqa: E402
from dev_flow_optimizer import (  # noqa: E402
    DEFAULT_STALL_THRESHOLD_SECONDS,
    OPEN_QUEUE_STATUSES,
    SEVERITY_RANK,
    detect_stalled_lock,
    format_idle_duration,
    parse_iso_timestamp,
    resolve_stall_threshold_seconds,
)

DEFAULT_WAKE_COOLDOWN_SECONDS = 900
DOCTOR_STATE_FILENAME = "agy_doctor_state.json"
DOCTOR_LOCK_FILENAME = "agy_doctor_state.lock"
TRANSCRIPT_TAIL_MAX_BYTES = 262_144
SERVER_RESTART_NOTICE = "All your subagents and background tasks have been stopped due to server restart"

_LOCAL_HOME_PATH_RE = re.compile(r"/(?:Users|home)/(?!testuser\b|test\b|Shared\b)[A-Za-z0-9._-]+")
_TERMINAL_FAILURE_STATUSES: frozenset[str] = frozenset(
    {"STOPPED", "CANCELLED", "CANCELED", "INTERRUPTED", "ERROR", "FAILED", "ABORTED"}
)
_ACTIVE_STEP_STATUSES: frozenset[str] = frozenset(
    {"RUNNING", "PENDING", "EXECUTING", "IN_PROGRESS", "QUEUED"}
)
_ANOMALY_CODES_OWNED_BY_DOCTOR: frozenset[str] = frozenset(
    {
        "server_restart_interrupted",
        "idle_orchestrator_with_runnable_work",
        "stalled_worker",
        "missing_wait_for_unlock",
        "phantom_pr_open",
    }
)


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
    return dt.strftime("%Y%m%d-%H%M%S")


def _sanitize_path(text: str) -> str:
    """Replace local user home paths with `$HOME` to preserve Zero-PII output."""
    if not text:
        return ""
    cleaned = str(text)
    with contextlib.suppress(OSError):
        home_str = str(Path.home().resolve())
        if home_str and home_str != "/":
            cleaned = cleaned.replace(home_str, "$HOME")
    return _LOCAL_HOME_PATH_RE.sub("$HOME", cleaned)


def _color(text: str, ansi_code: str, enabled: bool) -> str:
    return f"\033[{ansi_code}m{text}\033[0m" if enabled else text


def _load_pr_remediator() -> Any:
    """Import sibling `pr_remediator` module without duplicating module state."""
    existing = sys.modules.get("pr_remediator")
    if existing is not None and hasattr(existing, "get_next_dev_queue_item"):
        return existing
    import pr_remediator as _pr_rem  # noqa: E402

    return _pr_rem


def is_pid_alive(pid: int) -> bool:
    """Return True when OS process `pid` (> 1) is alive via `os.kill(pid, 0)`."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


_PROMPT_FLAG_NAMES: frozenset[str] = frozenset({"-p", "--prompt", "-i", "--prompt-interactive"})
_PROMPT_FLAG_PREFIXES: tuple[str, ...] = ("--prompt=", "--prompt-interactive=", "-p=", "-i=")
_SHELL_INTERPRETER_NAMES: frozenset[str] = frozenset({"sh", "bash", "zsh", "dash", "ksh"})


def resolve_agy_cli(*, home_dir: Path | None = None, which_fn: Callable[[str], str | None] | None = None) -> str:
    """Resolve native `agy` CLI binary path (`shutil.which("agy")` or `$HOME/.local/bin/agy`)."""
    finder = which_fn if which_fn is not None else shutil.which
    found = finder("agy")
    if found:
        return str(found)
    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    local_bin_agy = base_home / ".local" / "bin" / "agy"
    if local_bin_agy.is_file():
        return str(local_bin_agy)
    return "agy"


def resolve_agentapi_cli(
    *,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
    which_fn: Callable[[str], str | None] | None = None,
) -> str:
    """Resolve Antigravity Desktop `agentapi` binary path, returning `""` when unavailable."""
    eff_env = os.environ if env is None else env
    env_exe = str(eff_env.get("ANTIGRAVITY_AGENTAPI_EXE") or "").strip()
    if env_exe:
        env_path = Path(env_exe).expanduser()
        if env_path.is_file():
            return str(env_path)

    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    default_bin = base_home / ".gemini" / "antigravity" / "bin" / "agentapi"
    if default_bin.is_file():
        return str(default_bin)

    finder = which_fn if which_fn is not None else shutil.which
    found = finder("agentapi")
    if found:
        return str(found)
    return ""


def _strip_prompt_tokens(cmd: str, *, _depth: int = 0) -> tuple[str, list[str]]:
    """Tokenize a `ps` command string and strip prompt arguments after `-p`/`--prompt`/`-i`/`--prompt-interactive`."""
    shlex_ok = True
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        shlex_ok = False
        tokens = cmd.split()

    if not tokens:
        return ("", [])

    exe_idx = 0
    if exe_idx < len(tokens) and Path(tokens[exe_idx]).name.lower() == "env":
        exe_idx += 1
    while exe_idx < len(tokens) and "=" in tokens[exe_idx] and not tokens[exe_idx].startswith("-"):
        exe_idx += 1
    exe_base = Path(tokens[exe_idx]).name.lower() if exe_idx < len(tokens) else ""

    has_quoted_prompt = bool(re.search(r"""(?:^|\s)(?:-p|--prompt|-i|--prompt-interactive)(?:=|\s+)["']""", cmd))

    cleaned: list[str] = []
    idx = 0
    while idx < len(tokens):
        tok = tokens[idx]
        tok_l = tok.lower()
        if tok_l in _PROMPT_FLAG_NAMES:
            if not shlex_ok:
                break
            if exe_base == "agy" and not has_quoted_prompt:
                # `ps -eo pid,args` flattens argv without shell quotes: consume unquoted prompt words until next `--*` flag
                idx += 1
                while idx < len(tokens) and not tokens[idx].startswith("--"):
                    idx += 1
                continue
            idx += 2
            continue
        if tok_l.startswith(_PROMPT_FLAG_PREFIXES):
            if not shlex_ok:
                break
            idx += 1
            continue
        if (
            _depth < 2
            and exe_base in _SHELL_INTERPRETER_NAMES
            and tok == "-c"
            and idx + 1 < len(tokens)
        ):
            cleaned.append(tok)
            _, sub_tokens = _strip_prompt_tokens(tokens[idx + 1], _depth=_depth + 1)
            cleaned.extend(sub_tokens)
            idx += 2
            continue
        cleaned.append(tok)
        idx += 1

    return (exe_base, cleaned)


def _is_orchestrator_wake_process(exe_base: str, clean_tokens: list[str], raw_cmd_lower: str) -> bool:
    """Return True when process tokens represent an `agy-dev-orchestrator` CLI or `[agy-doctor]` `agentapi` wake."""
    tokens_l = [t.lower() for t in clean_tokens]
    for idx, tok_l in enumerate(tokens_l):
        if tok_l == "--agent" and idx + 1 < len(tokens_l) and tokens_l[idx + 1] == "agy-dev-orchestrator":
            return True
        if tok_l == "--agent=agy-dev-orchestrator":
            return True
    if ("--agent" in raw_cmd_lower) and ("agy-dev-orchestrator" in raw_cmd_lower):
        if exe_base == "agy" or any(Path(t).name.lower() == "agy" for t in clean_tokens):
            return True
    has_agentapi = exe_base == "agentapi" or any(Path(t).name.lower() == "agentapi" for t in clean_tokens)
    if has_agentapi and "new-conversation" in tokens_l:
        if "[agy-doctor]" in raw_cmd_lower or "agy-dev-orchestrator" in raw_cmd_lower:
            return True
    return False


def inspect_live_processes(
    *,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    self_pid: int | None = None,
) -> dict[str, Any]:
    """Inspect live OS processes (`ps -eo pid,args`) for orchestrator wakes, `wait-for-unlock`, and `watch-pr-merge`."""
    raw_text = ""
    probe_error = ""
    if ps_output is not None:
        raw_text = str(ps_output)
    elif ps_runner is not None:
        try:
            raw_text = str(ps_runner() or "")
        except Exception as exc:
            probe_error = f"{exc.__class__.__name__}: {exc}"
    else:
        try:
            proc = subprocess.run(
                ["ps", "-eo", "pid,args"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if proc.returncode == 0:
                raw_text = proc.stdout or ""
            else:
                probe_error = f"ps_exit_{proc.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            probe_error = f"{exc.__class__.__name__}: {exc}"

    cur_pid = os.getpid() if self_pid is None else int(self_pid)
    wait_pids: list[int] = []
    watch_pids: list[int] = []
    wake_pids: list[int] = []

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2 or not parts[0].isdigit():
            continue
        pid = int(parts[0])
        if pid == cur_pid:
            continue
        cmd = parts[1].strip()
        cmd_l = cmd.lower()
        if "ps -eo" in cmd_l:
            continue

        exe_base, clean_tokens = _strip_prompt_tokens(cmd)
        if _is_orchestrator_wake_process(exe_base, clean_tokens, cmd_l):
            wake_pids.append(pid)
            continue

        if exe_base in ("agy", "agentapi"):
            continue

        clean_tokens_l = [t.lower() for t in clean_tokens]
        has_remediator = any("pr_remediator" in t for t in clean_tokens_l)
        if has_remediator and "wait-for-unlock" in clean_tokens_l:
            wait_pids.append(pid)
        elif has_remediator and "watch-pr-merge" in clean_tokens_l:
            watch_pids.append(pid)

    return {
        "wait_for_unlock_running": len(wait_pids) > 0,
        "wait_for_unlock_pids": wait_pids,
        "watch_pr_merge_running": len(watch_pids) > 0,
        "watch_pr_merge_pids": watch_pids,
        "orchestrator_cli_running": len(wake_pids) > 0,
        "orchestrator_cli_pids": wake_pids,
        "probe_error": probe_error,
    }


def _read_tail_lines(path: Path, max_bytes: int = TRANSCRIPT_TAIL_MAX_BYTES) -> list[str]:
    """Read tail lines from `path` bounded by `max_bytes` for sub-millisecond inspection."""
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        total_size = fh.tell()
        offset = max(0, total_size - max(4096, int(max_bytes)))
        fh.seek(offset, os.SEEK_SET)
        chunk = fh.read()
    text = chunk.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    if offset > 0 and len(lines) > 1:
        # Drop partial leading line when seeking mid-file
        lines = lines[1:]
    return [ln for ln in lines if ln.strip()]


def _step_has_recovery_dispatch(step_obj: dict[str, Any], raw_line: str) -> bool:
    """Return True when a transcript step represents active tool/subagent recovery after a server restart."""
    if SERVER_RESTART_NOTICE.lower() in raw_line.lower():
        return False

    found_tool_dispatch = False

    def _walk(node: Any) -> None:
        nonlocal found_tool_dispatch
        if found_tool_dispatch:
            return
        if isinstance(node, dict):
            name_l = str(node.get("name") or node.get("tool") or node.get("toolName") or "").strip().lower()
            step_type_u = str(node.get("type") or "").strip().upper()
            if name_l in ("run_command", "invoke_subagent", "send_message", "schedule", "edit_file", "write_file"):
                found_tool_dispatch = True
                return
            if step_type_u in ("RUN_COMMAND", "INVOKE_SUBAGENT", "SEND_MESSAGE", "TOOL_CALL"):
                found_tool_dispatch = True
                return
            if isinstance(node.get("spec"), dict) and ("typeName" in node["spec"] or "TypeName" in node["spec"]):
                found_tool_dispatch = True
                return
            for key in ("tool_calls", "calls", "toolCall", "content", "arguments", "args"):
                val = node.get(key)
                if isinstance(val, (dict, list)):
                    _walk(val)
        elif isinstance(node, list):
            for elem in node:
                _walk(elem)

    _walk(step_obj)
    return found_tool_dispatch


def inspect_transcript_tail(
    conversation_id: str,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    *,
    now_ts: float | None = None,
    max_bytes: int = TRANSCRIPT_TAIL_MAX_BYTES,
    home_dir: Path | None = None,
    explicit_state_dir: bool = False,
) -> dict[str, Any]:
    """Inspect a conversation's `transcript.jsonl` tail (read-only, Zero-PII) for restart & liveness state."""
    cid = str(conversation_id or "").strip()
    brain_dirs = dev_common.resolve_antigravity_brain_dirs(
        brain_dir,
        home_dir=home_dir,
        explicit_state_dir=explicit_state_dir,
    )
    if not cid or not brain_dirs:
        return {
            "conversation_id": cid,
            "exists": False,
            "idle_seconds": 0,
            "idle_label": "n/a",
            "last_step_status": "",
            "last_step_type": "",
            "restart_notice_seen": False,
            "restart_interrupted": False,
            "tail_lines_scanned": 0,
        }

    tpath = dev_common.resolve_transcript_path(cid, brain_dirs)
    if tpath is None:
        return {
            "conversation_id": cid,
            "exists": False,
            "idle_seconds": 0,
            "idle_label": "missing",
            "last_step_status": "",
            "last_step_type": "",
            "restart_notice_seen": False,
            "restart_interrupted": False,
            "tail_lines_scanned": 0,
        }

    cur_ts = float(now_ts if now_ts is not None else time.time())
    try:
        mtime = float(tpath.stat().st_mtime)
        lines = _read_tail_lines(tpath, max_bytes=max_bytes)
    except OSError:
        return {
            "conversation_id": cid,
            "exists": False,
            "idle_seconds": 0,
            "idle_label": "unreadable",
            "last_step_status": "",
            "last_step_type": "",
            "restart_notice_seen": False,
            "restart_interrupted": False,
            "tail_lines_scanned": 0,
        }

    idle_seconds = max(0, int(cur_ts - mtime))
    last_restart_idx = -1
    last_recovery_idx = -1
    last_step_status = ""
    last_step_type = ""
    notice_lower = SERVER_RESTART_NOTICE.lower()

    for idx, raw_line in enumerate(lines):
        has_notice = notice_lower in raw_line.lower()
        if has_notice:
            last_restart_idx = idx
        parsed: Any = None
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(raw_line)
        if isinstance(parsed, dict):
            st_val = str(parsed.get("status") or "").strip().upper()
            if st_val:
                last_step_status = st_val
            tp_val = str(parsed.get("type") or parsed.get("name") or "").strip()
            if tp_val:
                last_step_type = tp_val
            if not has_notice and _step_has_recovery_dispatch(parsed, raw_line):
                last_recovery_idx = idx

    restart_notice_seen = last_restart_idx >= 0
    restart_interrupted = bool(restart_notice_seen and last_recovery_idx < last_restart_idx)

    return {
        "conversation_id": cid,
        "exists": True,
        "idle_seconds": idle_seconds,
        "idle_label": format_idle_duration(idle_seconds),
        "last_step_status": last_step_status,
        "last_step_type": last_step_type,
        "restart_notice_seen": restart_notice_seen,
        "restart_interrupted": restart_interrupted,
        "tail_lines_scanned": len(lines),
    }


def _make_anomaly(
    *,
    code: str,
    severity: str,
    title: str,
    cause: str,
    evidence: dict[str, Any],
    safe_fix_action: str,
    requires_wake: bool,
    auto_fixable: bool = False,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "cause": cause,
        "evidence": evidence,
        "safe_fix_action": safe_fix_action,
        "requires_wake": bool(requires_wake),
        "auto_fixable": bool(auto_fixable),
    }


def diagnose_orchestrator(
    *,
    state_dir: Path | str | None = None,
    portfolio_dir: Path | str | None = None,
    repos_dir: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    home_dir: Path | None = None,
    orchestrator_cid: str = "",
    sync_git: bool = False,
    stall_threshold_seconds: int | float | None = None,
    now_ts: float | None = None,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    git_client: Any | None = None,
) -> dict[str, Any]:
    """Perform 0-LLM deterministic diagnosis across locks, queue, live OS processes, and transcripts."""
    pr_rem = _load_pr_remediator()
    raw_explicit_state = state_dir if state_dir is not None else portfolio_dir
    has_explicit_state = raw_explicit_state is not None and bool(str(raw_explicit_state).strip())
    resolved_state = dev_common.resolve_state_dir(raw_explicit_state, home_dir=home_dir)
    resolved_brain_dirs = dev_common.resolve_antigravity_brain_dirs(
        brain_dir,
        home_dir=home_dir,
        explicit_state_dir=has_explicit_state,
    )
    stall_thresh = resolve_stall_threshold_seconds(stall_threshold_seconds)
    cur_ts = float(now_ts if now_ts is not None else time.time())

    git_sync_summary: dict[str, Any] = {"performed": False}
    if sync_git:
        is_offline = os.environ.get("AGY_OFFLINE_ONLY") == "1"
        sync_res = pr_rem.sync_dev_pr_lock_with_git(
            portfolio_dir=resolved_state,
            git_client=git_client,
        )
        recon_res = pr_rem.reconcile_dev_queue_with_git(
            portfolio_dir=resolved_state,
            git_client=git_client,
            fetch=not is_offline,
        )
        git_sync_summary = {
            "performed": True,
            "offline": is_offline,
            "locks_released": bool(sync_res.get("released")),
            "reconciled_count": int(recon_res.get("reconciled_count") or 0),
        }

    q_data = pr_rem.load_dev_queue(portfolio_dir=resolved_state)
    items: list[dict[str, Any]] = [
        dict(it) for it in (q_data.get("items") or []) if isinstance(it, dict)
    ]
    eff_orch_cid = str(orchestrator_cid or "").strip() or str(q_data.get("orchestrator_conversation_id") or "").strip()

    all_locks = pr_rem.get_all_dev_pr_locks(portfolio_dir=resolved_state, include_inactive=True)
    active_locks = {
        slug: lk
        for slug, lk in all_locks.items()
        if isinstance(lk, dict) and bool(lk.get("active"))
    }

    q_next = pr_rem.get_next_dev_queue_item(
        portfolio_dir=resolved_state,
        skip_locked_repos=True,
    )
    runnable_by_repo: dict[str, dict[str, Any]] = dict(q_next.get("runnable_by_repo") or {})
    next_unlocked_item = q_next.get("next_unlocked_item")

    flow_report = dev_flow_optimizer.analyze_dev_flow(
        portfolio_dir=resolved_state,
        repos_dir=repos_dir,
        brain_dir=resolved_brain_dirs,
        orchestrator_cid=eff_orch_cid,
        stall_threshold_seconds=stall_thresh,
        now_ts=cur_ts,
    )

    proc_info = inspect_live_processes(ps_output=ps_output, ps_runner=ps_runner)
    orch_transcript = inspect_transcript_tail(
        eff_orch_cid,
        resolved_brain_dirs,
        now_ts=cur_ts,
    )

    worker_transcripts: dict[str, dict[str, Any]] = {}
    for _slug, lk in sorted(active_locks.items()):
        w_cid = str(lk.get("worker_cid") or "").strip()
        if w_cid and w_cid not in worker_transcripts:
            worker_transcripts[w_cid] = inspect_transcript_tail(
                w_cid,
                resolved_brain_dirs,
                now_ts=cur_ts,
            )

    open_items = [
        it for it in items if str(it.get("status") or "pending").strip().lower() in OPEN_QUEUE_STATUSES
    ]
    pending_items = [
        it for it in items if str(it.get("status") or "pending").strip().lower() == "pending"
    ]
    in_prog_items = [
        it for it in items if str(it.get("status") or "").strip().lower() == "in_progress"
    ]
    pr_open_items = [
        it for it in items if str(it.get("status") or "").strip().lower() == "pr_open"
    ]
    has_actionable_queue_work = bool(pending_items or in_prog_items or pr_open_items or active_locks)

    anomalies: list[dict[str, Any]] = []

    # 1. Server restart interruption (orchestrator or active worker transcripts)
    interrupted_workers: list[dict[str, str]] = []
    for slug, lk in sorted(active_locks.items()):
        st_u = str(lk.get("status") or "").strip().upper()
        if st_u not in ("DEVELOPING", "NEEDS_REVISION_ITERATING", "AWAITING_AGENT_REVIEW"):
            continue
        w_cid = str(lk.get("worker_cid") or "").strip()
        wt_info = worker_transcripts.get(w_cid) or {}
        if wt_info.get("restart_interrupted"):
            interrupted_workers.append(
                {
                    "repo_slug": slug,
                    "worker_cid": w_cid,
                    "branch_name": str(lk.get("branch_name") or ""),
                }
            )

    if orch_transcript.get("restart_interrupted") or interrupted_workers:
        anomalies.append(
            _make_anomaly(
                code="server_restart_interrupted",
                severity="critical" if has_actionable_queue_work else "medium",
                title="Antigravity server restart stopped subagents/background tasks without recovery",
                cause=(
                    f"Transcript recorded '{SERVER_RESTART_NOTICE}' with no subsequent tool or subagent recovery."
                ),
                evidence={
                    "orchestrator_cid": eff_orch_cid,
                    "orchestrator_interrupted": bool(orch_transcript.get("restart_interrupted")),
                    "interrupted_workers": interrupted_workers,
                    "open_queue_items": len(open_items),
                },
                safe_fix_action="agy-doctor --wake",
                requires_wake=has_actionable_queue_work or bool(orch_transcript.get("restart_interrupted")),
                auto_fixable=False,
            )
        )

    # 2. Stalled workers (idle >= threshold OR stopped/failed worker transcript)
    for slug, lk in sorted(active_locks.items()):
        st_u = str(lk.get("status") or "").strip().upper()
        if st_u not in ("DEVELOPING", "NEEDS_REVISION_ITERATING") or bool(lk.get("pr_gate_ready")):
            continue
        st_info = detect_stalled_lock(
            lk,
            now_ts=cur_ts,
            threshold_seconds=stall_thresh,
            brain_dir=resolved_brain_dirs,
            home_dir=home_dir,
        )
        w_cid = str(lk.get("worker_cid") or "").strip()
        wt_info = worker_transcripts.get(w_cid) or {}
        wt_last_status = str(wt_info.get("last_step_status") or "").upper()
        worker_stopped = bool(
            wt_info.get("restart_interrupted") or wt_last_status in _TERMINAL_FAILURE_STATUSES
        )
        if not st_info.get("stalled") and not worker_stopped:
            continue

        idle_lbl = str(st_info.get("idle_label") or wt_info.get("idle_label") or "0s")
        wt_clean = _sanitize_path(str(lk.get("worktree_path") or lk.get("target_repo_dir") or ""))
        reason_detail = (
            f"worker transcript interrupted/stopped ({wt_last_status or 'server_restart'})"
            if worker_stopped and not st_info.get("stalled")
            else f"no lock/worktree/transcript activity for {idle_lbl} (threshold {stall_thresh}s)"
        )
        anomalies.append(
            _make_anomaly(
                code="stalled_worker",
                severity="critical",
                title=f"Stalled worker on {slug} ({reason_detail})",
                cause=f"Active {st_u} lock on {slug} has stalled ({reason_detail}).",
                evidence={
                    "repo_slug": slug,
                    "branch_name": str(lk.get("branch_name") or ""),
                    "worktree_path": wt_clean,
                    "status": st_u,
                    "conversation_id": str(lk.get("conversation_id") or ""),
                    "worker_cid": w_cid,
                    "idle_seconds": int(st_info.get("idle_seconds") or 0),
                    "idle_label": idle_lbl,
                    "worker_last_step_status": wt_last_status,
                },
                safe_fix_action="agy-doctor --wake",
                requires_wake=True,
                auto_fixable=False,
            )
        )

    # 3. Idle or missing orchestrator while unlocked repos have runnable tasks
    has_runnable_unlocked = bool(runnable_by_repo or next_unlocked_item is not None)
    if has_runnable_unlocked:
        orch_exists = bool(orch_transcript.get("exists"))
        orch_idle_secs = int(orch_transcript.get("idle_seconds") or 0)
        orch_last_st = str(orch_transcript.get("last_step_status") or "").upper()
        orch_actively_running = bool(
            eff_orch_cid
            and orch_exists
            and not orch_transcript.get("restart_interrupted")
            and orch_last_st not in _TERMINAL_FAILURE_STATUSES
            and (orch_idle_secs < stall_thresh or orch_last_st in _ACTIVE_STEP_STATUSES)
        )
        if not orch_actively_running and not proc_info.get("orchestrator_cli_running"):
            if not eff_orch_cid:
                dormant_reason = "no orchestrator_conversation_id registered in dev_queue.json"
            elif not orch_exists:
                dormant_reason = f"orchestrator {eff_orch_cid[:8]} transcript not found on disk"
            elif orch_transcript.get("restart_interrupted"):
                dormant_reason = f"orchestrator {eff_orch_cid[:8]} stopped by server restart"
            else:
                dormant_reason = (
                    f"orchestrator {eff_orch_cid[:8]} idle for {orch_transcript.get('idle_label', '0s')}"
                )
            free_repos = sorted(runnable_by_repo.keys())
            if not free_repos and isinstance(next_unlocked_item, dict):
                free_repos = [str(r) for r in (next_unlocked_item.get("dispatch_repos") or []) if str(r).strip()]
            runnable_cids: list[str] = []
            for r_item in runnable_by_repo.values():
                cid_v = str(r_item.get("conversation_id") or "").strip()
                if cid_v and cid_v not in runnable_cids:
                    runnable_cids.append(cid_v)
            if not runnable_cids and isinstance(next_unlocked_item, dict):
                cid_v = str(next_unlocked_item.get("conversation_id") or "").strip()
                if cid_v:
                    runnable_cids.append(cid_v)

            anomalies.append(
                _make_anomaly(
                    code="idle_orchestrator_with_runnable_work",
                    severity="high",
                    title=(
                        f"Runnable work waiting on {max(1, len(free_repos))} unlocked repo(s) while {dormant_reason}"
                    ),
                    cause=(
                        f"Unlocked repo(s) ({', '.join(free_repos) or 'unscoped'}) have runnable tasks, "
                        f"but {dormant_reason}."
                    ),
                    evidence={
                        "orchestrator_cid": eff_orch_cid,
                        "orchestrator_exists": orch_exists,
                        "idle_seconds": orch_idle_secs,
                        "idle_label": str(orch_transcript.get("idle_label") or ""),
                        "free_target_repos": free_repos,
                        "runnable_conversation_ids": runnable_cids,
                        "runnable_count": len(runnable_cids),
                    },
                    safe_fix_action="agy-doctor --wake",
                    requires_wake=True,
                    auto_fixable=False,
                )
            )

    # 4. Missing `wait-for-unlock` when all target repos are locked with pending/downstream tasks remaining
    has_downstream_multi_repo = False
    for it in (*pr_open_items, *in_prog_items):
        repos_all = [str(r).strip() for r in (it.get("repo_slugs") or []) if str(r).strip()]
        merged_set = {str(r).strip().lower() for r in (it.get("merged_repos") or []) if str(r).strip()}
        rem = [r for r in repos_all if r.lower() not in merged_set]
        if len(rem) >= 2:
            has_downstream_multi_repo = True
            break

    if (
        active_locks
        and not has_runnable_unlocked
        and (len(pending_items) > 0 or has_downstream_multi_repo)
        and not proc_info.get("wait_for_unlock_running")
    ):
        anomalies.append(
            _make_anomaly(
                code="missing_wait_for_unlock",
                severity="high",
                title=(
                    f"All target repos locked ({len(active_locks)} lock(s)) with queued work remaining, "
                    "but zero wait-for-unlock processes are running"
                ),
                cause=(
                    "When a PR merges or a lock releases, no 0-LLM `wait-for-unlock` background task is armed "
                    "to wake the orchestrator automatically."
                ),
                evidence={
                    "active_lock_repos": sorted(active_locks.keys()),
                    "pending_count": len(pending_items),
                    "downstream_multi_repo_waiting": has_downstream_multi_repo,
                    "wait_for_unlock_pids": [],
                },
                safe_fix_action=(
                    f"pr_remediator.py wait-for-unlock --orchestrator-id {eff_orch_cid or '<ORCH_CID>'} "
                    "--interval 15 --max-duration 3600"
                ),
                requires_wake=True,
                auto_fixable=False,
            )
        )

    # 5. Phantom `pr_open` items (stuck in `pr_open` with zero active locks and zero `pr_urls`)
    for f in flow_report.get("findings") or []:
        if not isinstance(f, dict) or str(f.get("code") or "") != "phantom_pr_open":
            continue
        ev = dict(f.get("evidence") or {})
        anomalies.append(
            _make_anomaly(
                code="phantom_pr_open",
                severity="high",
                title=str(f.get("title") or "Phantom pr_open item(s) without active locks or PR URLs"),
                cause=(
                    "Queue item(s) remain marked `pr_open` despite zero active locks and zero open PR URLs "
                    "(typically after partial multi-repo merges)."
                ),
                evidence=ev,
                safe_fix_action="agy-doctor --fix",
                requires_wake=False,
                auto_fixable=True,
            )
        )

    anomalies.sort(
        key=lambda a: (
            SEVERITY_RANK.get(str(a.get("severity") or "low").lower(), 9),
            str(a.get("code") or ""),
        )
    )

    improvements: list[dict[str, Any]] = []
    for f in flow_report.get("findings") or []:
        if not isinstance(f, dict):
            continue
        f_code = str(f.get("code") or "")
        if f_code in _ANOMALY_CODES_OWNED_BY_DOCTOR:
            continue
        improvements.append(
            {
                "code": f_code,
                "severity": str(f.get("severity") or "low"),
                "category": str(f.get("category") or "optimization"),
                "title": str(f.get("title") or f_code),
                "recommendation": str(f.get("recommendation") or ""),
                "next_step": str(f.get("auto_action_hint") or ""),
                "estimated_savings_min": int(f.get("estimated_savings_min") or 0),
                "delivery_mode": "pr_proposal_only",
                "auto_mutated": False,
            }
        )

    needs_wake = any(bool(a.get("requires_wake")) for a in anomalies)
    needs_fix = any(bool(a.get("auto_fixable")) for a in anomalies)

    if not anomalies:
        overall_status = "healthy"
    elif any(str(a.get("severity") or "").lower() == "critical" for a in anomalies):
        overall_status = "blocked"
    else:
        overall_status = "degraded"

    runnable_summary: list[dict[str, Any]] = []
    for r_slug, r_item in sorted(runnable_by_repo.items()):
        runnable_summary.append(
            {
                "repo_slug": r_slug,
                "conversation_id": str(r_item.get("conversation_id") or ""),
                "priority": int(r_item.get("priority") if r_item.get("priority") is not None else 50),
                "title": str(r_item.get("title") or ""),
                "dispatch_repos": list(r_item.get("dispatch_repos") or [r_slug]),
            }
        )
    if not runnable_summary and isinstance(next_unlocked_item, dict):
        runnable_summary.append(
            {
                "repo_slug": ",".join(next_unlocked_item.get("dispatch_repos") or []) or "unscoped",
                "conversation_id": str(next_unlocked_item.get("conversation_id") or ""),
                "priority": int(
                    next_unlocked_item.get("priority")
                    if next_unlocked_item.get("priority") is not None
                    else 50
                ),
                "title": str(next_unlocked_item.get("title") or ""),
                "dispatch_repos": list(next_unlocked_item.get("dispatch_repos") or []),
            }
        )

    return {
        "status": "ok",
        "generated_at": _utc_now_iso(cur_ts),
        "overall_status": overall_status,
        "needs_wake": needs_wake,
        "needs_fix": needs_fix,
        "action_required": bool(needs_wake or needs_fix),
        "health_score": int(flow_report.get("health_score") or 100),
        "state_dir": _sanitize_path(str(resolved_state)),
        "orchestrator": {
            "conversation_id": eff_orch_cid,
            "transcript": orch_transcript,
        },
        "processes": proc_info,
        "queue_summary": {
            "total_items": len(items),
            "open_items": len(open_items),
            "pending": len(pending_items),
            "in_progress": len(in_prog_items),
            "pr_open": len(pr_open_items),
            "active_locks": len(active_locks),
            "active_lock_repos": sorted(active_locks.keys()),
            "runnable_unlocked_repos": sorted(runnable_by_repo.keys()),
            "runnable_items": runnable_summary,
        },
        "git_sync": git_sync_summary,
        "anomalies": anomalies,
        "improvements": improvements,
    }


def apply_safe_fixes(
    *,
    state_dir: Path | str | None = None,
    portfolio_dir: Path | str | None = None,
    sync_git: bool = False,
    git_client: Any | None = None,
) -> dict[str, Any]:
    """Apply strictly safe runtime-state reconciliations (never editing tracked source files or pushing to `main`)."""
    pr_rem = _load_pr_remediator()
    resolved_state = dev_common.resolve_state_dir(state_dir if state_dir is not None else portfolio_dir)
    is_offline = os.environ.get("AGY_OFFLINE_ONLY") == "1"
    actions: list[dict[str, Any]] = []

    if sync_git:
        if is_offline:
            actions.append(
                {
                    "action": "sync_dev_pr_lock_with_git",
                    "status": "skipped",
                    "reason": "AGY_OFFLINE_ONLY=1",
                }
            )
        else:
            sync_res = pr_rem.sync_dev_pr_lock_with_git(
                portfolio_dir=resolved_state,
                git_client=git_client,
            )
            actions.append(
                {
                    "action": "sync_dev_pr_lock_with_git",
                    "status": "applied",
                    "released": bool(sync_res.get("released")),
                    "active_locks_remaining": len(sync_res.get("locks") or {}),
                }
            )
    else:
        actions.append(
            {
                "action": "sync_dev_pr_lock_with_git",
                "status": "skipped",
                "reason": "sync_git_not_requested",
            }
        )

    recon_res = pr_rem.reconcile_dev_queue_with_git(
        portfolio_dir=resolved_state,
        git_client=git_client,
        fetch=bool(sync_git and not is_offline),
    )
    reconciled_items = list(recon_res.get("reconciled_items") or [])
    actions.append(
        {
            "action": "reconcile_dev_queue_with_git",
            "status": "applied",
            "checked_count": int(recon_res.get("checked_count") or 0),
            "reconciled_count": len(reconciled_items),
            "reconciled_items": reconciled_items,
        }
    )

    return {
        "status": "ok",
        "remediation_actions": actions,
        "reconciled_count": len(reconciled_items),
    }


def build_wake_prompt(diagnosis: dict[str, Any]) -> str:
    """Build a concise, Zero-PII pre-computed recovery prompt for `agy-dev-orchestrator`."""
    anomalies = [a for a in (diagnosis.get("anomalies") or []) if isinstance(a, dict)]
    q_sum = diagnosis.get("queue_summary") if isinstance(diagnosis.get("queue_summary"), dict) else {}
    orch_info = diagnosis.get("orchestrator") if isinstance(diagnosis.get("orchestrator"), dict) else {}
    prev_orch_cid = str(orch_info.get("conversation_id") or "").strip()

    anomaly_lines: list[str] = []
    stalled_lines: list[str] = []
    for a in anomalies:
        code = str(a.get("code") or "anomaly")
        title = _sanitize_path(str(a.get("title") or code))
        anomaly_lines.append(f"- [{str(a.get('severity') or 'high').upper()}] `{code}`: {title}")
        if code == "stalled_worker":
            ev = a.get("evidence") if isinstance(a.get("evidence"), dict) else {}
            stalled_lines.append(
                f"- Repo `{ev.get('repo_slug', '')}` branch `{ev.get('branch_name', '')}` "
                f"(worker `{ev.get('worker_cid', '') or 'unknown'}`, worktree `{ev.get('worktree_path', '')}`): "
                "inspect worktree status, revive worker via `send_message` or re-dispatch `agy-dev-executor` "
                "with `--reuse-active`, and arm a 1200s worker watchdog timer."
            )

    runnable_lines: list[str] = []
    for r_entry in q_sum.get("runnable_items") or []:
        if not isinstance(r_entry, dict):
            continue
        runnable_lines.append(
            f"- P{r_entry.get('priority', 50)} `{r_entry.get('conversation_id', '')}` "
            f"on `{r_entry.get('repo_slug', '')}`: {_sanitize_path(str(r_entry.get('title') or ''))}"
        )

    parts: list[str] = [
        "[agy-doctor Auto-Wake Recovery] Act as `agy-dev-orchestrator` and read/follow "
        "`$HOME/.gemini/config/plugins/agy-dev-plugin/agents/agy-dev-orchestrator.md`.",
        "Pre-computed 0-LLM orchestrator diagnosis:",
        f"- Overall Status: `{diagnosis.get('overall_status', 'degraded')}` "
        f"(Previous Orchestrator CID: `{prev_orch_cid or 'none'}`)",
        "- Detected Anomalies:",
        *(anomaly_lines or ["- Manual `--force` wake requested."]),
    ]
    if runnable_lines:
        parts.extend(["- Runnable Tasks on FREE Repositories:", *runnable_lines])
    if stalled_lines:
        parts.extend(["- Stalled Workers Requiring Immediate Recovery:", *stalled_lines])

    parts.extend(
        [
            "",
            "Action Plan:",
            "0. Adopt the `agy-dev-orchestrator` persona and follow the Core Orchestration Protocol in "
            "`$HOME/.gemini/config/plugins/agy-dev-plugin/agents/agy-dev-orchestrator.md`.",
            "1. Register your active conversation ID and verify state via "
            "`pr_remediator.py lock-status --all --sync-git` and "
            "`pr_remediator.py queue-next --unlocked --orchestrator-id <YOUR_CONVERSATION_ID>`.",
            "2. Recover any stalled workers (`send_message` or `--reuse-active`) and dispatch runnable tasks "
            "across unlocked repositories in parallel (scheduling 1200s worker watchdog timers).",
            "3. Whenever all remaining target repositories hold active locks/`pr_open` with queued work left, "
            "ALWAYS arm `pr_remediator.py wait-for-unlock --orchestrator-id <YOUR_CONVERSATION_ID> "
            "--interval 15 --max-duration 3600` in the background (+ 65m safety timer) before going idle.",
        ]
    )
    return "\n".join(parts)


def trigger_orchestrator_wake(
    diagnosis: dict[str, Any],
    *,
    state_dir: Path | str | None = None,
    portfolio_dir: Path | str | None = None,
    cooldown_seconds: int = DEFAULT_WAKE_COOLDOWN_SECONDS,
    dry_run: bool = False,
    force: bool = False,
    now_ts: float | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    popen_fn: Callable[..., Any] | None = None,
    pid_alive_fn: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Launch detached hybrid orchestrator wake (`agentapi` in Desktop/sidecar; `agy` CLI in terminal)."""
    resolved_state = dev_common.resolve_state_dir(
        state_dir if state_dir is not None else portfolio_dir,
        home_dir=home_dir,
    )
    eff_env = os.environ if env is None else env
    eff_cooldown = max(0, int(cooldown_seconds))
    cur_ts = float(now_ts if now_ts is not None else time.time())
    needs_wake = bool(diagnosis.get("needs_wake"))
    anomalies = [a for a in (diagnosis.get("anomalies") or []) if isinstance(a, dict)]
    wake_reason = (
        ",".join(str(a.get("code") or "") for a in anomalies if a.get("requires_wake"))
        or ("forced_wake" if force else "healthy")
    )

    if not needs_wake and not force:
        return {
            "status": "skipped_healthy",
            "woke": False,
            "reason": "no_anomalies_requiring_wake",
            "cooldown_seconds": eff_cooldown,
        }

    state_file = resolved_state / DOCTOR_STATE_FILENAME
    lock_file = resolved_state / DOCTOR_LOCK_FILENAME
    check_alive = pid_alive_fn if pid_alive_fn is not None else is_pid_alive
    launcher = popen_fn if popen_fn is not None else subprocess.Popen

    with dev_common.file_lock(lock_file):
        prev_state = dev_common.atomic_read_json(state_file, default={})
        if not isinstance(prev_state, dict):
            prev_state = {}

        raw_prev_pid = prev_state.get("last_wake_pid")
        prev_pid = int(raw_prev_pid) if isinstance(raw_prev_pid, int) and not isinstance(raw_prev_pid, bool) else 0
        if prev_pid > 1 and check_alive(prev_pid):
            return {
                "status": "skipped_wake_in_flight",
                "woke": False,
                "pid": prev_pid,
                "transport": str(prev_state.get("transport") or ""),
                "last_wake_at": str(prev_state.get("last_wake_at") or ""),
                "last_wake_reason": str(prev_state.get("last_wake_reason") or ""),
            }

        prev_wake_ts = parse_iso_timestamp(prev_state.get("last_wake_at"))
        if not force and prev_wake_ts > 0.0 and eff_cooldown > 0:
            elapsed = cur_ts - prev_wake_ts
            if elapsed < eff_cooldown:
                remaining = max(1, int(round(eff_cooldown - elapsed)))
                return {
                    "status": "skipped_cooldown",
                    "woke": False,
                    "cooldown_seconds": eff_cooldown,
                    "remaining_seconds": remaining,
                    "transport": str(prev_state.get("transport") or ""),
                    "last_wake_at": str(prev_state.get("last_wake_at") or ""),
                    "last_wake_pid": prev_pid,
                }

        prompt = build_wake_prompt(diagnosis)
        ls_addr = str(eff_env.get("ANTIGRAVITY_LS_ADDRESS") or "").strip()
        agentapi_bin = (
            resolve_agentapi_cli(env=eff_env, home_dir=home_dir, which_fn=which_fn)
            if ls_addr
            else ""
        )
        if ls_addr and agentapi_bin:
            transport = "agentapi"
            cmd = [agentapi_bin, "new-conversation", "--title=[agy-doctor] Orchestrator Recovery", prompt]
        else:
            transport = "agy_cli"
            agy_bin = resolve_agy_cli(home_dir=home_dir, which_fn=which_fn)
            cmd = [agy_bin, "--agent", "agy-dev-orchestrator", "--dangerously-skip-permissions", "-p", prompt]

        if dry_run:
            return {
                "status": "dry_run",
                "woke": False,
                "dry_run": True,
                "transport": transport,
                "reason": wake_reason,
                "command": cmd,
                "prompt": prompt,
            }

        logs_dir = resolved_state / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / f"agy_doctor_wake_{_utc_compact_stamp(cur_ts)}.log"
        log_fd = os.open(str(log_path), os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
        log_fp = os.fdopen(log_fd, "a", encoding="utf-8")
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": log_fp,
            "stderr": subprocess.STDOUT,
            "start_new_session": True,
            "close_fds": True,
        }
        if env is not None:
            popen_kwargs["env"] = dict(eff_env)
        try:
            proc = launcher(cmd, **popen_kwargs)
        except OSError as exc:
            return {
                "status": "error",
                "woke": False,
                "transport": transport,
                "reason": "agentapi_spawn_failed" if transport == "agentapi" else "agy_spawn_failed",
                "error": _sanitize_path(str(exc)),
                "command": cmd,
            }
        finally:
            with contextlib.suppress(OSError):
                log_fp.close()

        raw_new_pid = getattr(proc, "pid", 0)
        new_pid = int(raw_new_pid) if isinstance(raw_new_pid, int) and not isinstance(raw_new_pid, bool) else 0
        wake_iso = _utc_now_iso(cur_ts)
        sanitized_cmd = [*(_sanitize_path(c) for c in cmd[:-1]), "<prompt>"] if cmd else []
        persisted_state = {
            "last_wake_at": wake_iso,
            "last_wake_pid": new_pid,
            "last_wake_reason": wake_reason,
            "last_wake_log": _sanitize_path(str(log_path)),
            "transport": transport,
            "last_command": sanitized_cmd,
        }
        dev_common.atomic_write_json(state_file, persisted_state, mode=0o600)

        return {
            "status": "woke",
            "woke": True,
            "transport": transport,
            "pid": new_pid,
            "woke_at": wake_iso,
            "reason": wake_reason,
            "log_path": _sanitize_path(str(log_path)),
            "state_file": _sanitize_path(str(state_file)),
            "command": cmd,
            "prompt": prompt,
        }


def run_queue_doctor(
    *,
    state_dir: Path | str | None = None,
    portfolio_dir: Path | str | None = None,
    repos_dir: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    orchestrator_cid: str = "",
    sync_git: bool = False,
    fix: bool = False,
    wake: bool = False,
    heal: bool = False,
    check_only: bool = False,
    dry_run: bool = False,
    force: bool = False,
    cooldown_seconds: int = DEFAULT_WAKE_COOLDOWN_SECONDS,
    stall_threshold_seconds: int | float | None = None,
    now_ts: float | None = None,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    git_client: Any | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    popen_fn: Callable[..., Any] | None = None,
    pid_alive_fn: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Execute full `queue-doctor` pipeline: diagnose -> optional `--fix` -> optional `--wake`."""
    eff_sync_git = bool(sync_git or heal)
    eff_fix = bool(fix or heal)
    eff_wake = bool(wake or heal)
    target_state = state_dir if state_dir is not None else portfolio_dir

    remediation_actions: list[dict[str, Any]] = []
    if eff_fix and not check_only:
        if dry_run:
            remediation_actions.append(
                {
                    "action": "apply_safe_fixes",
                    "status": "dry_run",
                    "would_sync_git": eff_sync_git,
                    "would_reconcile_queue": True,
                }
            )
            diag = diagnose_orchestrator(
                state_dir=target_state,
                repos_dir=repos_dir,
                brain_dir=brain_dir,
                home_dir=home_dir,
                orchestrator_cid=orchestrator_cid,
                sync_git=False,
                stall_threshold_seconds=stall_threshold_seconds,
                now_ts=now_ts,
                ps_output=ps_output,
                ps_runner=ps_runner,
                git_client=git_client,
            )
        else:
            fix_res = apply_safe_fixes(
                state_dir=target_state,
                sync_git=eff_sync_git,
                git_client=git_client,
            )
            remediation_actions.extend(fix_res.get("remediation_actions") or [])
            diag = diagnose_orchestrator(
                state_dir=target_state,
                repos_dir=repos_dir,
                brain_dir=brain_dir,
                home_dir=home_dir,
                orchestrator_cid=orchestrator_cid,
                sync_git=False,
                stall_threshold_seconds=stall_threshold_seconds,
                now_ts=now_ts,
                ps_output=ps_output,
                ps_runner=ps_runner,
                git_client=git_client,
            )
    else:
        diag = diagnose_orchestrator(
            state_dir=target_state,
            repos_dir=repos_dir,
            brain_dir=brain_dir,
            home_dir=home_dir,
            orchestrator_cid=orchestrator_cid,
            sync_git=bool(eff_sync_git and not dry_run and not check_only),
            stall_threshold_seconds=stall_threshold_seconds,
            now_ts=now_ts,
            ps_output=ps_output,
            ps_runner=ps_runner,
            git_client=git_client,
        )

    wake_prompt = build_wake_prompt(diag)
    if eff_wake and not check_only:
        wake_result = trigger_orchestrator_wake(
            diag,
            state_dir=target_state,
            cooldown_seconds=cooldown_seconds,
            dry_run=dry_run,
            force=force,
            now_ts=now_ts,
            home_dir=home_dir,
            env=env,
            which_fn=which_fn,
            popen_fn=popen_fn,
            pid_alive_fn=pid_alive_fn,
        )
    else:
        wake_result = {
            "status": "not_requested",
            "woke": False,
        }

    exit_code = 0
    if check_only:
        exit_code = 0 if (diag.get("overall_status") == "healthy" and not diag.get("action_required")) else 2
    elif wake_result.get("status") == "error":
        exit_code = 1

    return {
        **diag,
        "remediation_actions": remediation_actions,
        "wake": wake_result,
        "wake_prompt": wake_prompt,
        "check_only": bool(check_only),
        "exit_code": exit_code,
    }


def render_doctor_report(report: dict[str, Any], *, color: bool = True) -> str:
    """Render crisp 3-part terminal report: Diagnosis, Safe Runtime Actions & Wake, Improvements."""
    overall = str(report.get("overall_status") or "healthy").lower()
    if overall == "healthy":
        badge = _color("[HEALTHY]", "1;32", color)
    elif overall == "blocked":
        badge = _color("[BLOCKED]", "1;31", color)
    else:
        badge = _color("[DEGRADED]", "1;33", color)

    score = int(report.get("health_score") or 100)
    orch = report.get("orchestrator") if isinstance(report.get("orchestrator"), dict) else {}
    orch_cid = str(orch.get("conversation_id") or "")
    orch_tr = orch.get("transcript") if isinstance(orch.get("transcript"), dict) else {}
    orch_lbl = (
        f"{orch_cid[:8]} ({'restart-stopped' if orch_tr.get('restart_interrupted') else 'idle ' + str(orch_tr.get('idle_label') or 'n/a')})"
        if orch_cid
        else "none"
    )

    procs = report.get("processes") if isinstance(report.get("processes"), dict) else {}
    wait_pids = procs.get("wait_for_unlock_pids") or []
    watch_pids = procs.get("watch_pr_merge_pids") or []
    q_sum = report.get("queue_summary") if isinstance(report.get("queue_summary"), dict) else {}

    lines: list[str] = [
        f"{_color('AGY DOCTOR', '1;36', color)} {badge} · score {score}/100 · orch:{orch_lbl}",
        (
            f"  Queue: open={q_sum.get('open_items', 0)} (pending={q_sum.get('pending', 0)}, "
            f"in_prog={q_sum.get('in_progress', 0)}, pr_open={q_sum.get('pr_open', 0)}) · "
            f"locks={q_sum.get('active_locks', 0)} · "
            f"free_runnable_repos={len(q_sum.get('runnable_unlocked_repos') or [])}"
        ),
        (
            f"  Processes: wait-for-unlock={'running (' + ','.join(str(p) for p in wait_pids) + ')' if wait_pids else 'none'} · "
            f"watch-pr-merge={'running (' + ','.join(str(p) for p in watch_pids) + ')' if watch_pids else 'none'}"
        ),
        "",
        _color("1. Diagnosis (What Happened)", "1", color),
    ]

    anomalies = [a for a in (report.get("anomalies") or []) if isinstance(a, dict)]
    if not anomalies:
        lines.append(f"  {_color('✓', '1;32', color)} Queue, locks, and orchestrator state are healthy (0 anomalies).")
    else:
        for a in anomalies:
            sev = str(a.get("severity") or "high").upper()
            sev_col = "1;31" if sev == "CRITICAL" else ("1;33" if sev == "HIGH" else "1;36")
            lines.append(
                f"  • {_color(f'[{sev}]', sev_col, color)} {a.get('code')}: {a.get('title')}"
            )
            if a.get("cause"):
                lines.append(f"      Cause: {a['cause']}")
            if a.get("safe_fix_action"):
                lines.append(f"      Action: `{a['safe_fix_action']}`")

    lines.extend(["", _color("2. Safe Runtime Actions & Wake Status", "1", color)])
    rem_actions = [r for r in (report.get("remediation_actions") or []) if isinstance(r, dict)]
    if rem_actions:
        for act in rem_actions:
            lines.append(
                f"  • {act.get('action')}: {act.get('status')}"
                + (
                    f" (reconciled={act['reconciled_count']})"
                    if "reconciled_count" in act
                    else (f" ({act['reason']})" if act.get("reason") else "")
                )
            )
    elif report.get("needs_fix"):
        lines.append("  • Runtime fixes pending — run `agy-doctor --fix` (or `agy-doctor --heal`) to reconcile.")
    else:
        lines.append("  • Runtime fixes: none needed.")

    wake_info = report.get("wake") if isinstance(report.get("wake"), dict) else {}
    w_status = str(wake_info.get("status") or "not_requested")
    w_transport = str(wake_info.get("transport") or "agy_cli")
    if w_status == "woke":
        lines.append(
            f"  • Wake: {_color('LAUNCHED', '1;32', color)} ({w_transport}) pid={wake_info.get('pid')} "
            f"(log: {wake_info.get('log_path', '')})"
        )
    elif w_status == "dry_run":
        dry_cmd_lbl = (
            "`agentapi new-conversation \"--title=[agy-doctor] Orchestrator Recovery\" <prompt>`"
            if w_transport == "agentapi"
            else "`agy --agent agy-dev-orchestrator --dangerously-skip-permissions -p <prompt>`"
        )
        lines.append(
            f"  • Wake: {_color('DRY-RUN', '1;36', color)} ({w_transport}) would spawn {dry_cmd_lbl}"
        )
    elif w_status == "skipped_healthy":
        lines.append("  • Wake: skipped (queue & orchestrator healthy — 0 LLM tokens used).")
    elif w_status == "skipped_wake_in_flight":
        lines.append(
            f"  • Wake: skipped (previous wake pid={wake_info.get('pid')} is still running)."
        )
    elif w_status == "skipped_cooldown":
        lines.append(
            f"  • Wake: skipped (cooldown active — {wake_info.get('remaining_seconds', 0)}s remaining; "
            "pass `--force` to override)."
        )
    elif w_status == "error":
        lines.append(f"  • Wake: {_color('ERROR', '1;31', color)} {wake_info.get('error', '')}")
    elif report.get("needs_wake"):
        lines.append("  • Wake: recommended — run `agy-doctor --wake` (or `agy-doctor --heal`).")
    else:
        lines.append("  • Wake: not required.")

    lines.extend(
        [
            "",
            _color("3. Orchestrator Improvements (PR Proposals — Never Auto-Mutated)", "1", color),
        ]
    )
    improvements = [imp for imp in (report.get("improvements") or []) if isinstance(imp, dict)]
    if not improvements:
        lines.append("  • No workflow optimization bottlenecks detected.")
    else:
        for imp in improvements[:6]:
            sev = str(imp.get("severity") or "low").upper()
            sav = int(imp.get("estimated_savings_min") or 0)
            sav_str = f" (~{sav}m saved)" if sav > 0 else ""
            lines.append(f"  • [{sev}] {imp.get('code')}{sav_str}: {imp.get('title')}")
            if imp.get("next_step"):
                lines.append(f"      Next step (PR/Planner): {imp['next_step']}")

    return "\n".join(lines)
