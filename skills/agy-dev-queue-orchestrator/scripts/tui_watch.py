"""Interactive watch loop, TTY key handling, doctor overlay, and AST-guarded auto-reload (`tui_watch.py`)."""

from __future__ import annotations

import argparse
import ast
from collections.abc import Callable
import contextlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import termios
import time
import tty
from typing import Any, TextIO

from tui_board import _handle_board_hotkey, _open_url_in_browser, _trigger_hitl_navigation
from tui_models import (
    _INSTALLED_PLUGIN_ROOT,
    _RELOAD_ENV_FLAG,
    _SCRIPT_PLUGIN_ROOT,
    ALT_SCREEN_ENTER,
    ALT_SCREEN_EXIT,
    JSON_SCHEMA_VERSION,
    VALID_QUEUE_STATUSES,
    QueueRowView,
    WatchState,
    _dev_common,
    _facade_attr,
    _flow_opt,
    _get_git_client,
    _get_lock_store,
    _get_orch_control,
    _get_queue_doctor,
    _get_queue_planner,
    _get_queue_store,
)
from tui_progress import _build_repo_labels, _compute_lock_progress, _extract_lock_telemetry_fields
from tui_render import (
    _apply_live_filter_and_cursor,
    _build_watch_footer,
    _clamp_lines_to_viewport,
    _color,
    _compose_priority_viewport_with_meta,
    _debug_log,
    _is_dumb_terminal,
    _render_detail_inspector_lines,
    _render_help_overlay_lines,
    _render_optimizer_overlay_lines,
    _render_plan_lines,
    _row_attr,
    _should_enable_color,
    _supports_osc8,
    _trunc,
    render,
)
from tui_state import (
    _AsyncSyncController,
    _discover_repos,
    _iter_active_lock_slots,
    _load_conversation_titles,
    _load_json,
    _load_locks,
    _load_orchestration_phase,
    _lock_slot_occupancy,
    _resolve_pr_remediator_script,
    _resolve_state_dir,
    _run_live_sync,
    _snapshot_state_fingerprint,
)

_TUI_MODULE_FILENAMES: tuple[str, ...] = (
    "dev_queue_tui.py",
    "tui_models.py",
    "tui_state.py",
    "tui_progress.py",
    "tui_render.py",
    "tui_board.py",
    "tui_watch.py",
)

_TTY_PUSHBACK_BYTES: dict[int, bytearray] = {}


def _silence_broken_pipe_stdout() -> int:
    """Redirect stdout fd to `/dev/null` on `BrokenPipeError` so pipe consumers (`head`) exit cleanly."""
    with contextlib.suppress(OSError, ValueError):
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        finally:
            os.close(devnull_fd)
    return 0


def _collect_watched_script_paths() -> list[Path]:
    """Collect resolved Python module paths (entrypoint first, plus all `tui_*.py` siblings) for `--watch` auto-reload."""
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

    facade_file = _facade_attr("__file__", None)
    if facade_file:
        _add(facade_file)
    _add(Path(__file__).with_name("dev_queue_tui.py"))

    script_dir = Path(__file__).resolve().parent
    for fname in _TUI_MODULE_FILENAMES:
        _add(script_dir / fname)

    for root in (_SCRIPT_PLUGIN_ROOT, _INSTALLED_PLUGIN_ROOT):
        tui_dir = root / "skills" / "agy-dev-queue-orchestrator" / "scripts"
        for fname in _TUI_MODULE_FILENAMES:
            _add(tui_dir / fname)

    for mod in (_flow_opt, _get_queue_doctor(), _dev_common, _get_git_client()):
        mod_file = getattr(mod, "__file__", None) if mod is not None else None
        if mod_file:
            _add(mod_file)
    for root in (_SCRIPT_PLUGIN_ROOT, _INSTALLED_PLUGIN_ROOT):
        _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / "dev_flow_optimizer.py")
        _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / "queue_compactor.py")
        _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / "dev_queue_doctor.py")
        _add(root / "clients" / "dev_common.py")
        _add(root / "clients" / "git_client.py")
    return collected


def _snapshot_file_fingerprints(
    paths: list[Path] | tuple[Path, ...],
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


def _validate_python_syntax(paths: list[Path] | tuple[Path, ...]) -> tuple[bool, str]:
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
    raw_argv: list[str] | None = None,
) -> list[str]:
    """Build `[sys.executable, resolved_script, *cli_args]` for `os.execv` across shell and console entrypoints."""
    default_entry = Path(__file__).with_name("dev_queue_tui.py")
    resolved_script = str((script_path or default_entry).resolve())
    preserved = list(sys.argv[1:] if raw_argv is None else raw_argv)
    return [sys.executable, resolved_script, *preserved]


def _invoke_queue_doctor(
    state_dir: Path,
    *,
    repos_dir: str = "",
    fix: bool = False,
    wake: bool = False,
    sync_git: bool = False,
) -> dict[str, Any]:
    """Execute `queue-doctor` in-process (or via `pr_remediator.py` subprocess fallback)."""
    qd_mod = _get_queue_doctor()
    if qd_mod is not None and hasattr(qd_mod, "run_queue_doctor"):
        return dict(
            qd_mod.run_queue_doctor(
                state_dir=state_dir,
                repos_dir=repos_dir or None,
                sync_git=sync_git,
                fix=fix,
                wake=wake,
            )
        )
    pr_rem = _resolve_pr_remediator_script()
    cmd = [sys.executable, str(pr_rem), "queue-doctor", "--json", "--state-dir", str(state_dir)]
    if repos_dir:
        cmd.extend(["--repos-dir", repos_dir])
    if sync_git:
        cmd.append("--sync-git")
    if fix:
        cmd.append("--fix")
    if wake:
        cmd.append("--wake")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20.0, check=False)
    with contextlib.suppress(json.JSONDecodeError):
        parsed = json.loads(proc.stdout or "{}")
        if isinstance(parsed, dict):
            return parsed
    return {"status": "error", "overall_status": "degraded", "anomalies": [], "improvements": []}


def _format_doctor_report_text(report: dict[str, Any], *, color: bool) -> str:
    """Format `queue-doctor` report dictionary for terminal display."""
    qd_mod = _get_queue_doctor()
    if qd_mod is not None and hasattr(qd_mod, "render_doctor_report"):
        return str(qd_mod.render_doctor_report(report, color=color))
    return json.dumps(report, indent=2, ensure_ascii=False)


def _handle_doctor_hotkey(
    args: argparse.Namespace,
    state_dir: Path,
    *,
    out_stream: TextIO,
    use_tty: bool,
    tty_fd: int | None,
    orig_termios: Any = None,
    doctor_fn: Callable[..., dict[str, Any]] | None = None,
    prompt_timeout_seconds: float = 300.0,
) -> str:
    """Suspend alt-screen/cbreak, render full `agy-doctor` report, offer `[w] wake / [f] fix / [Enter]`, and restore TUI."""
    runner = doctor_fn if doctor_fn is not None else _facade_attr("_invoke_queue_doctor", _invoke_queue_doctor)
    color_on = (not bool(getattr(args, "no_color", False))) and not bool(os.environ.get("NO_COLOR"))
    repos_dir = str(getattr(args, "repos_dir", "") or "")

    try:
        if tty_fd is not None and orig_termios is not None:
            with contextlib.suppress(OSError, termios.error):
                termios.tcsetattr(tty_fd, termios.TCSADRAIN, orig_termios)
        if use_tty:
            out_stream.write(ALT_SCREEN_EXIT)
            out_stream.flush()

        report = runner(state_dir, repos_dir=repos_dir, fix=False, wake=False, sync_git=False)
        rendered = _format_doctor_report_text(report, color=color_on)
        prompt_line = _color(
            "\n[agy-doctor] Press [w] wake · [f] fix · [Enter] return to TUI: ",
            "1;36",
            color_on,
        )
        out_stream.write(f"\n{rendered}\n{prompt_line}")
        out_stream.flush()

        choice = ""
        if tty_fd is not None:
            if orig_termios is not None:
                with contextlib.suppress(OSError, termios.error):
                    tty.setcbreak(tty_fd)
            try:
                ready, _, _ = select.select([tty_fd], [], [], max(0.05, float(prompt_timeout_seconds)))
                if ready:
                    choice = _read_watch_key(tty_fd)
            except (InterruptedError, OSError):
                choice = ""
            finally:
                if orig_termios is not None:
                    with contextlib.suppress(OSError, termios.error):
                        termios.tcsetattr(tty_fd, termios.TCSADRAIN, orig_termios)

        out_stream.write("\n")
        out_stream.flush()

        if choice == "w":
            wake_rep = runner(state_dir, repos_dir=repos_dir, fix=False, wake=True, sync_git=False)
            w_info = wake_rep.get("wake") if isinstance(wake_rep.get("wake"), dict) else {}
            w_st = str(w_info.get("status") or "not_requested")
            if w_st == "woke":
                w_tr = str(w_info.get("transport") or "").strip()
                via_lbl = f" via {w_tr}" if w_tr else ""
                return f"✓ Doctor: woke orchestrator{via_lbl} (pid {w_info.get('pid')})"
            if w_st == "skipped_cooldown":
                return f"⚠ Doctor: wake cooldown ({w_info.get('remaining_seconds', 0)}s left)"
            if w_st == "skipped_wake_in_flight":
                return f"⚠ Doctor: wake already running (pid {w_info.get('pid')})"
            if w_st == "skipped_healthy":
                return "✓ Doctor: healthy (wake skipped, 0 tokens)"
            return f"⚠ Doctor wake: {w_st}"

        if choice == "f":
            is_offline = os.environ.get("AGY_OFFLINE_ONLY") == "1"
            fix_rep = runner(
                state_dir,
                repos_dir=repos_dir,
                fix=True,
                wake=False,
                sync_git=not is_offline,
            )
            rem_acts = fix_rep.get("remediation_actions") or []
            rec_cnt = sum(
                int(a.get("reconciled_count") or 0)
                for a in rem_acts
                if isinstance(a, dict)
            )
            return f"✓ Doctor: safe fixes applied ({rec_cnt} reconciled)"

        overall = str(report.get("overall_status") or "healthy")
        anom_cnt = len(report.get("anomalies") or [])
        if overall == "healthy":
            return f"✓ Doctor: healthy ({report.get('health_score', 100)}/100)"
        return f"⚠ Doctor: {overall} ({anom_cnt} anomaly)"
    finally:
        if tty_fd is not None and orig_termios is not None:
            with contextlib.suppress(OSError, termios.error):
                tty.setcbreak(tty_fd)
        if use_tty:
            out_stream.write(ALT_SCREEN_ENTER)
            out_stream.flush()


def _build_watch_frame(
    body_lines: list[str],
    term_w: int,
    term_h: int,
    footer: str,
    *,
    preclamped_lines: list[str] | None = None,
) -> str:
    """Build a single flicker-free ANSI synchronized-update watch frame."""
    display_lines = (
        preclamped_lines
        if preclamped_lines is not None
        else _clamp_lines_to_viewport(body_lines, term_w, term_h, footer)
    )
    return (
        "\033[?2026h\033[H"
        + "\n".join(f"{line}\033[K" for line in display_lines)
        + "\033[J\033[?2026l"
    )


def _parse_watch_key_sequence(raw: str, *, raw_mode: bool = False) -> str:
    """Normalize raw cbreak key bytes, recognizing arrows, PgUp/PgDn, Home/End, Enter, Backspace, and Ctrl+L."""
    if not raw:
        return ""
    if raw in ("\r", "\n"):
        return "ENTER"
    if raw in ("\x7f", "\x08"):
        return "BACKSPACE"
    if raw == "\x0c":
        return "CTRL_L"
    if raw.startswith(("\x1b[A", "\x1bOA")):
        return "UP"
    if raw.startswith(("\x1b[B", "\x1bOB")):
        return "DOWN"
    if raw.startswith("\x1b[5~"):
        return "PGUP"
    if raw.startswith("\x1b[6~"):
        return "PGDN"
    if raw.startswith(("\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~")):
        return "HOME"
    if raw.startswith(("\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~")):
        return "END"
    if raw.startswith("\x1b"):
        return "ESC"
    first = raw[0]
    if raw_mode:
        return first
    if first in ("O", "P", "R", "G"):
        return first
    return first.lower()


def _has_tty_pushback(tty_fd: int | None) -> bool:
    """Return True when unread pushback bytes exist for `tty_fd`."""
    return bool(tty_fd is not None and _TTY_PUSHBACK_BYTES.get(tty_fd))


def _read_tty_byte(tty_fd: int) -> bytes:
    """Read a single byte from pushback buffer or `os.read(tty_fd, 1)`."""
    buf = _TTY_PUSHBACK_BYTES.get(tty_fd)
    if buf:
        b = bytes([buf.pop(0)])
        if not buf:
            _TTY_PUSHBACK_BYTES.pop(tty_fd, None)
        return b
    return os.read(tty_fd, 1)


def _unread_tty_byte(tty_fd: int, b: bytes) -> None:
    """Push back a single peeked byte for `tty_fd`."""
    if b:
        _TTY_PUSHBACK_BYTES.setdefault(tty_fd, bytearray()).extend(b)


def _read_watch_key(tty_fd: int, *, raw_mode: bool = False) -> str:
    """Read a single logical keystroke or CSI escape sequence from `tty_fd` without eating typeahead."""
    raw_first = _read_tty_byte(tty_fd)
    if not raw_first:
        return ""
    if raw_first == b"\x1b":
        seq = bytearray(raw_first)
        with contextlib.suppress(InterruptedError, OSError):
            has_next = _has_tty_pushback(tty_fd) or bool(select.select([tty_fd], [], [], 0.015)[0])
            if has_next:
                b2 = _read_tty_byte(tty_fd)
                if b2 in (b"[", b"O"):
                    seq.extend(b2)
                    for _ in range(6):
                        has_csi = _has_tty_pushback(tty_fd) or bool(select.select([tty_fd], [], [], 0.01)[0])
                        if not has_csi:
                            break
                        ch = _read_tty_byte(tty_fd)
                        if not ch:
                            break
                        seq.extend(ch)
                        if 0x40 <= ch[0] <= 0x7E:
                            break
                elif b2:
                    _unread_tty_byte(tty_fd, b2)
        return _parse_watch_key_sequence(seq.decode("utf-8", errors="ignore"), raw_mode=raw_mode)
    if raw_first[0] >= 0xC0:
        utf_seq = bytearray(raw_first)
        needed = 1 if raw_first[0] < 0xE0 else (2 if raw_first[0] < 0xF0 else 3)
        for _ in range(needed):
            with contextlib.suppress(InterruptedError, OSError):
                if _has_tty_pushback(tty_fd) or select.select([tty_fd], [], [], 0.01)[0]:
                    utf_seq.extend(_read_tty_byte(tty_fd))
        return _parse_watch_key_sequence(utf_seq.decode("utf-8", errors="ignore"), raw_mode=raw_mode)
    return _parse_watch_key_sequence(raw_first.decode("utf-8", errors="ignore"), raw_mode=raw_mode)


def _run_watch_json_ndjson_loop(
    args: argparse.Namespace,
    *,
    stream: TextIO | None = None,
    max_iterations: int | None = None,
) -> int:
    """Emit newline-delimited JSON (`NDJSON`) snapshots with `schema_version` for `--watch --json`."""
    out_stream = stream if stream is not None else sys.stdout
    interval = max(0.05, float(args.watch if args.watch is not None else 3.0))
    repo_cache: dict[str, Any] = {"ttl": 60.0}
    stop = False

    def _handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    prev_sigterm: Any = None
    sigterm_installed = False
    with contextlib.suppress(ValueError, OSError):
        prev_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _handle_stop)
        sigterm_installed = True

    render_fn = _facade_attr("render", render)
    load_locks_fn = _facade_attr("_load_locks", _load_locks)
    fp_fn = _facade_attr("_snapshot_state_fingerprint", _snapshot_state_fingerprint)

    seq = 0
    try:
        while not stop:
            seq += 1
            line_json, _, _, _, st_dir, _ = render_fn(
                args,
                is_tty_override=False,
                repo_cache=repo_cache,
                ndjson_compact=True,
                ndjson_seq=seq,
            )
            out_stream.write(line_json + "\n")
            out_stream.flush()
            if max_iterations is not None and seq >= max_iterations:
                break
            locks_cur, _ = load_locks_fn(st_dir)
            fp_base = fp_fn(st_dir, locks_cur)
            deadline = time.monotonic() + interval
            while not stop and time.monotonic() < deadline:
                if fp_fn(st_dir, locks_cur) != fp_base:
                    break
                time.sleep(max(0.02, min(0.15, deadline - time.monotonic())))
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return _facade_attr("_silence_broken_pipe_stdout", _silence_broken_pipe_stdout)()
    finally:
        if sigterm_installed and prev_sigterm is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, prev_sigterm)
    return 0


def run_watch_loop(
    args: argparse.Namespace,
    *,
    stream: TextIO | None = None,
    use_tty_override: bool | None = None,
    open_tty: bool = True,
    tty_fd_override: int | None = None,
    max_iterations: int | None = None,
    term_size_override: tuple[int, int] | None = None,
    exec_fn: Callable[[str, list[str]], Any] | None = None,
    watched_paths_override: list[Path] | None = None,
    reload_debounce_seconds: float = 0.3,
    sync_fn: Callable[[Path, dict[str, Path]], str] | None = None,
    raw_argv: list[str] | None = None,
) -> int:
    """Run flicker-free watch mode separating snapshot I/O from `<1ms` pure viewport repaints & overlays."""
    if getattr(args, "json", False):
        return _run_watch_json_ndjson_loop(args, stream=stream, max_iterations=max_iterations)

    out_stream = stream if stream is not None else sys.stdout
    stop = False
    sigwinch_received = False

    def _handle_stop(_signum: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    def _handle_sigwinch(_signum: int, _frame: object) -> None:
        nonlocal sigwinch_received
        sigwinch_received = True

    prev_sigterm: Any = None
    sigterm_installed = False
    with contextlib.suppress(ValueError, OSError):
        prev_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _handle_stop)
        sigterm_installed = True

    prev_sigwinch: Any = None
    sigwinch_installed = False
    sigwinch_num = getattr(signal, "SIGWINCH", None)
    if sigwinch_num is not None:
        with contextlib.suppress(ValueError, OSError):
            prev_sigwinch = signal.getsignal(sigwinch_num)
            signal.signal(sigwinch_num, _handle_sigwinch)
            sigwinch_installed = True

    interval = max(0.05, float(args.watch if args.watch is not None else 3.0))
    sync_interval = float(getattr(args, "sync_interval", 30.0))
    no_auto_sync = bool(getattr(args, "no_auto_sync", False))
    use_tty = (
        (bool(getattr(out_stream, "isatty", lambda: False)()) and not _is_dumb_terminal())
        if use_tty_override is None
        else bool(use_tty_override)
    )
    tty_fd: int | None = None
    owns_tty_fd = False
    orig_termios: Any = None

    if tty_fd_override is not None:
        tty_fd = tty_fd_override
    elif use_tty and open_tty:
        try:
            tty_fd = os.open("/dev/tty", os.O_RDONLY | os.O_NONBLOCK)
            owns_tty_fd = True
            if os.isatty(tty_fd):
                orig_termios = termios.tcgetattr(tty_fd)
                tty.setcbreak(tty_fd)
            else:
                os.close(tty_fd)
                tty_fd = None
                owns_tty_fd = False
        except (OSError, termios.error, ValueError):
            if tty_fd is not None and owns_tty_fd:
                with contextlib.suppress(OSError):
                    os.close(tty_fd)
            tty_fd = None
            owns_tty_fd = False

    collect_paths_fn = _facade_attr("_collect_watched_script_paths", _collect_watched_script_paths)
    watched_paths = (
        list(watched_paths_override)
        if watched_paths_override is not None
        else collect_paths_fn()
    )
    baseline_script_fp = _snapshot_file_fingerprints(watched_paths)
    reload_pending_deadline: float | None = None
    effective_exec_fn = exec_fn if exec_fn is not None else os.execv
    preserved_argv = list(raw_argv) if raw_argv is not None else getattr(args, "_raw_argv", None)

    sync_ctrl = _facade_attr("_AsyncSyncController", _AsyncSyncController)(sync_fn=sync_fn)
    repo_cache: dict[str, Any] = {"ttl": 60.0}

    ws = WatchState(
        state_dir=_facade_attr("_resolve_state_dir", _resolve_state_dir)(getattr(args, "state_dir", "")),
        last_auto_sync_mono=time.monotonic() if bool(getattr(args, "sync", False)) else 0.0,
    )
    if os.environ.pop(_RELOAD_ENV_FLAG, "") == "1":
        ws.status_msg = "↻ Auto-reloaded after plugin update"
        ws.status_until = time.monotonic() + 4.0

    render_fn = _facade_attr("render", render)
    fp_fn = _facade_attr("_snapshot_state_fingerprint", _snapshot_state_fingerprint)
    footer_fn = _facade_attr("_build_watch_footer", _build_watch_footer)
    frame_fn = _facade_attr("_build_watch_frame", _build_watch_frame)
    filter_fn = _facade_attr("_apply_live_filter_and_cursor", _apply_live_filter_and_cursor)
    compose_fn = _facade_attr("_compose_priority_viewport_with_meta", _compose_priority_viewport_with_meta)
    open_url_fn = _facade_attr("_open_url_in_browser", _open_url_in_browser)
    board_hotkey_fn = _facade_attr("_handle_board_hotkey", _handle_board_hotkey)
    doctor_hotkey_fn = _facade_attr("_handle_doctor_hotkey", _handle_doctor_hotkey)
    hitl_nav_fn = _facade_attr("_trigger_hitl_navigation", _trigger_hitl_navigation)

    def _cleanup_watch_terminal() -> None:
        nonlocal tty_fd
        if ws.terminal_cleaned:
            return
        ws.terminal_cleaned = True
        if sigterm_installed and prev_sigterm is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, prev_sigterm)
        if sigwinch_installed and sigwinch_num is not None and prev_sigwinch is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sigwinch_num, prev_sigwinch)
        if tty_fd is not None:
            _TTY_PUSHBACK_BYTES.pop(tty_fd, None)
        if tty_fd is not None and owns_tty_fd:
            if orig_termios is not None:
                with contextlib.suppress(OSError, termios.error):
                    termios.tcsetattr(tty_fd, termios.TCSADRAIN, orig_termios)
            with contextlib.suppress(OSError):
                os.close(tty_fd)
            tty_fd = None
        if use_tty:
            out_stream.write(ALT_SCREEN_EXIT)
            out_stream.flush()

    def _attempt_safe_reload() -> tuple[bool, str]:
        syntax_ok, syntax_err = _validate_python_syntax(watched_paths)
        if not syntax_ok:
            return (False, syntax_err)
        script_target = watched_paths[0] if watched_paths else Path(__file__).with_name("dev_queue_tui.py").resolve()
        exec_argv = _build_reload_exec_argv(script_target, preserved_argv)
        _cleanup_watch_terminal()
        os.environ[_RELOAD_ENV_FLAG] = "1"
        effective_exec_fn(sys.executable, exec_argv)
        return (True, "")

    try:
        if use_tty:
            out_stream.write(ALT_SCREEN_ENTER)
            out_stream.flush()

        while not stop:
            if ws.data_dirty:
                completed_sync_msg = sync_ctrl.consume_result()
                if completed_sync_msg:
                    ws.status_msg = completed_sync_msg
                    ws.status_until = time.monotonic() + 4.0

                width_arg = term_size_override[0] if term_size_override is not None else None
                fresh_layout: dict[str, Any] = {}
                sync_badge = sync_ctrl.format_badge()
                pre_render_core_fp = fp_fn(ws.state_dir, {})
                try:
                    out, ws.row_links, ws.hitl_convs, ws.first_pr, ws.state_dir, ws.local_dirs = render_fn(
                        args,
                        is_tty_override=use_tty,
                        term_width_override=width_arg,
                        layout_out=fresh_layout,
                        repo_cache=repo_cache,
                        sync_status_badge=sync_badge,
                        strict_io=True,
                    )
                    ws.body_lines = out.splitlines()
                    ws.last_good_body_lines = ws.body_lines
                    if fresh_layout:
                        ws.layout_data = fresh_layout
                        ws.last_good_layout = fresh_layout
                        ws.all_pr_links = list(fresh_layout.get("all_pr_links") or [])
                        ws.has_active_work = bool(fresh_layout.get("has_active_work"))
                        ws.active_locks_map = dict(fresh_layout.get("active_locks") or {})
                        ws.cached_opt_analysis = None
                except (OSError, json.JSONDecodeError, sqlite3.Error) as io_err:
                    err_banner = f"⚠ Transient read error ({io_err.__class__.__name__}) — retrying…"
                    ws.status_msg = err_banner
                    ws.status_until = time.monotonic() + 3.0
                    ws.layout_data = dict(ws.last_good_layout) if ws.last_good_layout else {}
                    ws.body_lines = (
                        list(ws.last_good_body_lines)
                        if ws.last_good_body_lines
                        else [err_banner, f"  {io_err}"]
                    )
                if fp_fn(ws.state_dir, {}) != pre_render_core_fp:
                    ws.last_state_fp = pre_render_core_fp
                else:
                    ws.last_state_fp = fp_fn(ws.state_dir, ws.active_locks_map)
                ws.deadline = time.monotonic() + interval
                ws.data_dirty = False

            t_paint = time.perf_counter()
            now_mono = time.monotonic()
            if ws.status_msg and now_mono >= ws.status_until:
                ws.status_msg = ""

            if term_size_override is not None:
                cols, rows = term_size_override
            else:
                tsize = shutil.get_terminal_size((124, 40))
                cols, rows = tsize.columns, tsize.lines

            hitl_cnt = int(ws.layout_data.get("hitl_count") or len(ws.hitl_convs))
            footer = footer_fn(
                interval=interval,
                hitl_count=hitl_cnt,
                compact_pending=ws.compact_pending,
                show_all=bool(getattr(args, "all", False)),
                live_filter=ws.live_filter,
                filter_input_active=ws.filter_input_active,
                overlay_mode=ws.overlay_mode,
                awaiting_hitl_digit=ws.awaiting_hitl_digit,
                sync_in_flight=sync_ctrl.is_in_flight(),
                status_msg=ws.status_msg,
                orchestration_paused=bool(ws.layout_data.get("orchestration_paused", False)),
                orchestration_phase=str(ws.layout_data.get("orchestration_phase") or ""),
            )

            color_on = bool(ws.layout_data.get("color_on", False))
            use_osc8 = bool(ws.layout_data.get("use_osc8", False))
            raw_recs = list(ws.layout_data.get("row_views") or ws.layout_data.get("row_records") or [])
            act_lines, mid_lines, nav_records, ws.cursor_idx = filter_fn(
                raw_recs,
                live_filter=ws.live_filter,
                cursor_idx=ws.cursor_idx,
                compact_pending=ws.compact_pending,
                color_on=color_on,
            )
            nav_total = len(nav_records)

            if use_tty:
                clear_prefix = "\033[2J\033[H" if ws.force_clear_screen else ""
                ws.force_clear_screen = False
                if ws.overlay_mode in ("detail", "optimizer", "help"):
                    if ws.overlay_mode == "detail":
                        sel_rec = nav_records[ws.cursor_idx] if (0 <= ws.cursor_idx < nav_total) else None
                        ov_lines = _render_detail_inspector_lines(sel_rec, cols, color_on, use_osc8)
                    elif ws.overlay_mode == "optimizer":
                        if ws.cached_opt_analysis is None:
                            if _flow_opt is not None and hasattr(_flow_opt, "analyze_dev_flow_optimizations"):
                                ws.cached_opt_analysis = dict(
                                    _flow_opt.analyze_dev_flow_optimizations(
                                        lightweight=True,
                                        preloaded_items=list(ws.layout_data.get("items_snapshot") or []),
                                        preloaded_active_locks=dict(ws.layout_data.get("active_locks") or {}),
                                        preloaded_all_locks=dict(ws.layout_data.get("all_locks") or {}),
                                        preloaded_repo_dirs=dict(ws.layout_data.get("repo_local_dirs") or {}),
                                    )
                                )
                            else:
                                ws.cached_opt_analysis = {"health_score": 100, "findings": []}
                        ov_lines = _render_optimizer_overlay_lines(ws.cached_opt_analysis, cols, color_on)
                    else:
                        ov_lines = _render_help_overlay_lines(cols, color_on)
                    max_ov_scroll = max(0, len(ov_lines) - max(3, rows - 2))
                    ws.overlay_scroll = max(0, min(ws.overlay_scroll, max_ov_scroll))
                    sliced_ov = ov_lines[ws.overlay_scroll:]
                    out_stream.write(clear_prefix + frame_fn(sliced_ov, cols, rows, footer))
                elif ws.layout_data:
                    if ws.live_filter and not act_lines and not mid_lines:
                        mid_lines = [_color(f"  No rows matching filter {ws.live_filter!r} (press Esc to clear).", "2", color_on)]
                        mid_views: list[QueueRowView] | None = None
                    else:
                        q_f = ws.live_filter.strip().lower()
                        matched_mid = [
                            r
                            for r in raw_recs
                            if isinstance(r, QueueRowView)
                            and r.band != "active"
                            and (not q_f or q_f in r.search_blob)
                        ]
                        mid_views = matched_mid if len(matched_mid) == len(mid_lines) else None

                    vp_lines, ws.scroll_offset, ws.max_scroll_offset = compose_fn(
                        ws.layout_data.get("header_lines") or [],
                        act_lines,
                        mid_lines,
                        ws.layout_data.get("sticky_footer_lines") or [],
                        cols,
                        rows,
                        footer,
                        scroll_offset=ws.scroll_offset,
                        compact_pending=ws.compact_pending,
                        middle_rows=mid_views,
                    )
                    out_stream.write(
                        clear_prefix + frame_fn(ws.body_lines, cols, rows, footer, preclamped_lines=vp_lines)
                    )
                else:
                    out_stream.write(clear_prefix + frame_fn(ws.body_lines, cols, rows, footer))
            else:
                out_stream.write("\n".join([*ws.body_lines, footer]) + "\n")
            out_stream.flush()
            _debug_log(
                bool(getattr(args, "debug", False)),
                f"viewport repaint in {(time.perf_counter() - t_paint) * 1000.0:.3f}ms",
            )

            ws.iterations += 1
            if max_iterations is not None and ws.iterations >= max_iterations:
                break

            while not stop:
                cur_mono = time.monotonic()
                if cur_mono >= ws.deadline:
                    ws.data_dirty = True
                    break

                if sigwinch_received:
                    sigwinch_received = False
                    ws.data_dirty = True
                    break

                if sync_ctrl.has_unconsumed_result():
                    ws.data_dirty = True
                    break

                cur_script_fp = _snapshot_file_fingerprints(watched_paths)
                if cur_script_fp != baseline_script_fp:
                    if reload_pending_deadline is None:
                        reload_pending_deadline = cur_mono + max(0.0, reload_debounce_seconds)
                    if time.monotonic() >= reload_pending_deadline:
                        settled_fp = _snapshot_file_fingerprints(watched_paths)
                        reloaded, err_txt = _attempt_safe_reload()
                        if reloaded:
                            return 0
                        baseline_script_fp = settled_fp
                        reload_pending_deadline = None
                        ws.status_msg = f"⚠ {err_txt}"
                        ws.status_until = time.monotonic() + 5.0
                        break
                else:
                    reload_pending_deadline = None

                auto_sync_allowed = (
                    not no_auto_sync
                    and sync_interval > 0.0
                    and os.environ.get("AGY_OFFLINE_ONLY") != "1"
                    and use_tty
                    and ws.has_active_work
                )
                if (
                    auto_sync_allowed
                    and not sync_ctrl.is_in_flight()
                    and (ws.last_auto_sync_mono == 0.0 or (cur_mono - ws.last_auto_sync_mono) >= sync_interval)
                ):
                    ws.last_auto_sync_mono = cur_mono
                    if sync_ctrl.trigger(ws.state_dir, ws.local_dirs):
                        break

                if fp_fn(ws.state_dir, ws.active_locks_map) != ws.last_state_fp:
                    ws.data_dirty = True
                    break

                remaining = max(0.02, min(0.15, ws.deadline - time.monotonic()))
                if tty_fd is None:
                    time.sleep(remaining)
                    continue
                if not _has_tty_pushback(tty_fd):
                    try:
                        ready, _, _ = select.select([tty_fd], [], [], remaining)
                    except (InterruptedError, OSError):
                        break
                    if not ready:
                        continue
                try:
                    key = _read_watch_key(tty_fd, raw_mode=ws.filter_input_active)
                except OSError:
                    continue
                if not key:
                    if tty_fd_override is not None:
                        tty_fd = None
                    continue

                if ws.filter_input_active:
                    if key == "ENTER":
                        ws.filter_input_active = False
                        ws.cursor_idx = 0
                        ws.scroll_offset = 0
                    elif key == "ESC":
                        ws.filter_input_active = False
                        ws.live_filter = ""
                        ws.cursor_idx = 0
                        ws.scroll_offset = 0
                    elif key == "BACKSPACE":
                        ws.live_filter = ws.live_filter[:-1]
                        ws.cursor_idx = 0
                        ws.scroll_offset = 0
                    elif key == "CTRL_L":
                        ws.force_clear_screen = True
                    elif len(key) == 1 and key.isprintable():
                        ws.live_filter += key
                        ws.cursor_idx = 0
                        ws.scroll_offset = 0
                    break

                if ws.awaiting_hitl_digit:
                    ws.awaiting_hitl_digit = False
                    if key.isdigit() and key != "0":
                        h_idx = int(key)
                        entry = ws.hitl_convs.get(h_idx)
                        if entry:
                            cid_disp, cid_full, ag_title = entry
                            nav_res = hitl_nav_fn(
                                ws.state_dir,
                                index=h_idx,
                                cid_disp=cid_disp,
                                cid_full=cid_full,
                                ag_title=ag_title,
                            )
                            if nav_res.get("acknowledged"):
                                ws.status_msg = f"✓ Dev Queue Panel switched to '{_trunc(ag_title, 34)}' ({cid_disp})"
                            elif nav_res.get("copied"):
                                ws.status_msg = (
                                    f"✓ nav_intent + Antigravity focused · copied '{_trunc(ag_title, 30)}' ({cid_disp})"
                                )
                            else:
                                ws.status_msg = f"✓ nav_intent + Antigravity focused ({cid_disp})"
                        else:
                            ws.status_msg = f"No [h{key}] item"
                        ws.status_until = time.monotonic() + 4.0
                        break
                    ws.status_msg = "Cancelled"
                    ws.status_until = time.monotonic() + 1.5
                    break

                if ws.overlay_mode:
                    page_step = max(3, rows - 6)
                    if key in ("ESC", "ENTER", "q") or (ws.overlay_mode == "optimizer" and key == "x") or (ws.overlay_mode == "help" and key == "?"):
                        ws.overlay_mode = ""
                        ws.overlay_scroll = 0
                    elif key in ("j", "DOWN"):
                        ws.overlay_scroll += 1
                    elif key in ("k", "UP"):
                        ws.overlay_scroll = max(0, ws.overlay_scroll - 1)
                    elif key == "PGDN":
                        ws.overlay_scroll += page_step
                    elif key == "PGUP":
                        ws.overlay_scroll = max(0, ws.overlay_scroll - page_step)
                    elif key in ("g", "HOME"):
                        ws.overlay_scroll = 0
                    elif key in ("G", "END"):
                        ws.overlay_scroll = 9999
                    elif key == "CTRL_L":
                        ws.force_clear_screen = True
                    elif ws.overlay_mode == "detail" and key == "o":
                        sel_rec = nav_records[ws.cursor_idx] if (0 <= ws.cursor_idx < nav_total) else None
                        pr_target = (
                            str(_row_attr(sel_rec, "pr_url", "") or "") if sel_rec is not None else ""
                        ) or (ws.first_pr[1] if ws.first_pr else "")
                        if pr_target:
                            ok = open_url_fn(pr_target)
                            ws.status_msg = "✓ Opening PR" if ok else "✗ Failed opening PR"
                        else:
                            ws.status_msg = "No human-ready PR on selected item"
                        ws.status_until = time.monotonic() + 3.0
                        ws.overlay_mode = ""
                    break

                if key == "q":
                    stop = True
                    break
                if key == "ESC":
                    if ws.live_filter:
                        ws.live_filter = ""
                        ws.cursor_idx = 0
                        ws.scroll_offset = 0
                        ws.status_msg = "Filter cleared"
                        ws.status_until = time.monotonic() + 2.0
                    break
                if key == "CTRL_L":
                    ws.force_clear_screen = True
                    break
                if key == "/":
                    ws.filter_input_active = True
                    break
                if key == "ENTER":
                    ws.overlay_mode = "detail"
                    ws.overlay_scroll = 0
                    break
                if key == "x":
                    ws.overlay_mode = "optimizer"
                    ws.overlay_scroll = 0
                    break
                if key == "?":
                    ws.overlay_mode = "help"
                    ws.overlay_scroll = 0
                    break
                if key == "h":
                    if not ws.hitl_convs:
                        ws.status_msg = "No pending HITL conversations"
                        ws.status_until = time.monotonic() + 2.5
                    else:
                        ws.awaiting_hitl_digit = True
                    break
                if key == "b":
                    ws.status_msg = board_hotkey_fn(args, ws.state_dir)
                    ws.status_until = time.monotonic() + 3.5
                    ws.data_dirty = True
                    break
                if key == "d":
                    ws.status_msg = doctor_hotkey_fn(
                        args,
                        ws.state_dir,
                        out_stream=out_stream,
                        use_tty=use_tty,
                        tty_fd=tty_fd,
                        orig_termios=orig_termios,
                    )
                    ws.status_until = time.monotonic() + 4.5
                    ws.data_dirty = True
                    break
                if key == "r":
                    ws.status_msg = "↻ Refreshed"
                    ws.status_until = time.monotonic() + 2.0
                    ws.data_dirty = True
                    break
                if key == "R":
                    reloaded, err_txt = _attempt_safe_reload()
                    if reloaded:
                        return 0
                    ws.status_msg = f"⚠ {err_txt}"
                    ws.status_until = time.monotonic() + 5.0
                    break
                if key == "s":
                    ws.last_auto_sync_mono = time.monotonic()
                    if sync_ctrl.trigger(ws.state_dir, ws.local_dirs):
                        ws.status_msg = "↻ Syncing…"
                        ws.status_until = time.monotonic() + 30.0
                    else:
                        ws.status_msg = "↻ Sync already in progress…"
                        ws.status_until = time.monotonic() + 2.5
                    break
                if key == "a":
                    args.all = not args.all
                    ws.cursor_idx = 0
                    ws.scroll_offset = 0
                    ws.status_msg = f"Merged view: {'ON' if args.all else 'OFF'}"
                    ws.status_until = time.monotonic() + 2.5
                    ws.data_dirty = True
                    break
                if key == "p":
                    ws.compact_pending = not ws.compact_pending
                    ws.cursor_idx = 0
                    ws.scroll_offset = 0
                    ws.status_msg = f"Pending view: {'COMPACT' if ws.compact_pending else 'FULL'}"
                    ws.status_until = time.monotonic() + 2.5
                    break
                if key == "S":
                    oc_mod = _facade_attr("_get_orch_control", _get_orch_control)()
                    if oc_mod is None:
                        ws.status_msg = "⚠ orchestration_control module unavailable"
                    else:
                        cur_locks, _ = _load_locks(ws.state_dir)
                        res_s = oc_mod.hard_stop_orchestration(
                            state_dir=ws.state_dir,
                            source="tui",
                            active_locks=cur_locks,
                        )
                        phase_info = oc_mod.summarize_orchestration_phase(res_s.get("control"), cur_locks)
                        ws.status_msg = f"⏹ {phase_info.get('banner_text') or 'HARD STOPPED — Workers frozen'}"
                    ws.status_until = time.monotonic() + 5.0
                    ws.data_dirty = True
                    break
                if key == "P":
                    oc_mod = _facade_attr("_get_orch_control", _get_orch_control)()
                    if oc_mod is None:
                        ws.status_msg = "⚠ orchestration_control module unavailable"
                    else:
                        cur_paused = bool(oc_mod.is_orchestration_paused(state_dir=ws.state_dir))
                        if cur_paused:
                            oc_mod.resume_orchestration(state_dir=ws.state_dir, source="tui", request_wake=True)
                            ws.status_msg = "▶ Orchestration resumed — waking orchestrator…"
                        else:
                            res_p = oc_mod.pause_orchestration(state_dir=ws.state_dir, source="tui")
                            cur_locks, _ = _load_locks(ws.state_dir)
                            phase_info = oc_mod.summarize_orchestration_phase(res_p.get("control"), cur_locks)
                            ws.status_msg = f"⏸ {phase_info.get('banner_text') or 'PAUSED — 0 new dispatches'}"
                    ws.status_until = time.monotonic() + 4.5
                    ws.data_dirty = True
                    break
                if key in ("j", "DOWN"):
                    if nav_total > 0:
                        ws.cursor_idx = min(nav_total - 1, ws.cursor_idx + 1)
                    ws.scroll_offset = (
                        min(ws.max_scroll_offset, ws.scroll_offset + 1)
                        if ws.cursor_idx >= len(act_lines)
                        else ws.scroll_offset
                    )
                    break
                if key in ("k", "UP"):
                    if nav_total > 0:
                        ws.cursor_idx = max(0, ws.cursor_idx - 1)
                    if ws.cursor_idx < len(act_lines):
                        ws.scroll_offset = 0
                    else:
                        ws.scroll_offset = max(0, ws.scroll_offset - 1)
                    break
                if key == "PGDN":
                    p_step = max(3, rows // 2)
                    if nav_total > 0:
                        ws.cursor_idx = min(nav_total - 1, ws.cursor_idx + p_step)
                    ws.scroll_offset = min(ws.max_scroll_offset, ws.scroll_offset + p_step)
                    break
                if key == "PGUP":
                    p_step = max(3, rows // 2)
                    if nav_total > 0:
                        ws.cursor_idx = max(0, ws.cursor_idx - p_step)
                    ws.scroll_offset = max(0, ws.scroll_offset - p_step)
                    break
                if key in ("g", "HOME"):
                    ws.cursor_idx = 0
                    ws.scroll_offset = 0
                    break
                if key in ("G", "END"):
                    if nav_total > 0:
                        ws.cursor_idx = nav_total - 1
                    ws.scroll_offset = ws.max_scroll_offset
                    break
                if key == "o":
                    if ws.first_pr:
                        lbl, url = ws.first_pr
                        ok = open_url_fn(url)
                        ws.status_msg = f"✓ Opening {lbl}" if ok else f"✗ Failed opening {lbl}"
                    else:
                        ws.status_msg = "No human-ready PR available"
                    ws.status_until = time.monotonic() + 3.0
                    break
                if key == "O":
                    if ws.all_pr_links:
                        opened = sum(1 for _, u in ws.all_pr_links if open_url_fn(u))
                        ws.status_msg = (
                            f"✓ Opening {opened} ready PR(s)"
                            if opened == len(ws.all_pr_links)
                            else f"⚠ Opened {opened}/{len(ws.all_pr_links)} PR(s)"
                        )
                    else:
                        ws.status_msg = "No human-ready PR available"
                    ws.status_until = time.monotonic() + 3.0
                    break
                if key.isdigit() and key != "0":
                    row_num = int(key)
                    target = ws.row_links.get(row_num)
                    if target:
                        lbl, url = target
                        ok = open_url_fn(url)
                        ws.status_msg = f"✓ Opening #{row_num} {lbl}" if ok else f"✗ Failed opening #{row_num}"
                    else:
                        ws.status_msg = f"Row #{row_num} has no active link"
                    ws.status_until = time.monotonic() + 3.0
                    break
    except KeyboardInterrupt:
        return 0
    finally:
        _cleanup_watch_terminal()
    return 0


def _run_subcommand_optimize(argv: list[str]) -> int:
    """Execute `agy-queue optimize` subcommand."""
    parser = argparse.ArgumentParser(prog="agy-queue optimize", description="Run 0-LLM dev flow bottleneck analyzer.")
    parser.add_argument("--state-dir", default="", help="Override state directory.")
    parser.add_argument("--repos-dir", default="", help="Optional parent directory for managed git repos.")
    parser.add_argument("--orchestrator-id", default="", help="Optional orchestrator conversation ID.")
    parser.add_argument("--stall-seconds", type=int, default=None, help="Override stalled-worker threshold in seconds.")
    parser.add_argument("--json", action="store_true", help="Output structured JSON.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    parser.add_argument("--debug", action="store_true", help="Emit debug diagnostics to stderr.")
    args = parser.parse_args(argv)

    st_dir = _resolve_state_dir(args.state_dir)
    q_data = _load_json(st_dir / "dev_queue.json")
    locks, all_locks = _load_locks(st_dir)
    _, local_dirs = _facade_attr("_discover_repos", _discover_repos)(all_locks, q_data.get("items") or [], args.repos_dir)
    orch_cid = args.orchestrator_id or str(q_data.get("orchestrator_conversation_id") or "")

    if _flow_opt is not None and hasattr(_flow_opt, "analyze_dev_flow_optimizations"):
        report = dict(
            _flow_opt.analyze_dev_flow_optimizations(
                repos_dir=args.repos_dir or None,
                orchestrator_cid=orch_cid,
                stall_threshold_seconds=args.stall_seconds,
                preloaded_items=q_data.get("items") or [],
                preloaded_active_locks=locks,
                preloaded_all_locks=all_locks,
                preloaded_repo_dirs=local_dirs,
            )
        )
    else:
        pr_rem = _resolve_pr_remediator_script()
        cmd = [sys.executable, str(pr_rem), "queue-optimize"]
        if args.repos_dir:
            cmd.extend(["--repos-dir", args.repos_dir])
        if orch_cid:
            cmd.extend(["--orchestrator-id", orch_cid])
        if args.stall_seconds is not None:
            cmd.extend(["--stall-seconds", str(args.stall_seconds)])
        proc = subprocess.run(
            cmd,
            env={**os.environ, "AGY_DEV_STATE_DIR": str(st_dir)},
            capture_output=True,
            text=True,
            check=False,
        )
        report = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {"status": "error", "findings": []}

    report.setdefault("schema_version", JSON_SCHEMA_VERSION)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    color_on = _should_enable_color(args.no_color, sys.stdout.isatty())
    term_w = shutil.get_terminal_size((124, 40)).columns
    print("\n".join(_render_optimizer_overlay_lines(report, term_w, color_on)))
    return 0


def _get_queue_compactor() -> Any:
    """Import sibling `queue_compactor` module lazily without mutating global import state."""
    for root in (_SCRIPT_PLUGIN_ROOT, _INSTALLED_PLUGIN_ROOT):
        cand_dir = root / "skills" / "agy-code-remediation-pr" / "scripts"
        if (cand_dir / "queue_compactor.py").is_file():
            cand_s = str(cand_dir)
            if cand_s not in sys.path:
                sys.path.insert(0, cand_s)
            try:
                import queue_compactor as _qc  # type: ignore[import-not-found]

                return _qc
            except Exception:
                return None
    return None


def _run_subcommand_compact(argv: list[str]) -> int:
    """Execute `agy-queue compact [--apply] [--tier 1|all] [--repo SLUG] [--json]`."""
    parser = argparse.ArgumentParser(
        prog="agy-queue compact",
        description="0-LLM deterministic PR compaction detector & atomic executor (defaults to --dry-run).",
    )
    parser.add_argument("--apply", action="store_true", help="Atomically apply detected compactions under file_lock (default: dry-run).")
    parser.add_argument("--tier", choices=("1", "all"), default="all", help="Compaction tier: '1' or 'all' (default: all).")
    parser.add_argument("--repo", default="", help="Filter compaction targets by repository slug/substring.")
    parser.add_argument("--ids", nargs="*", default=[], help="Optional explicit conversation ID pair/prefixes.")
    parser.add_argument("--max-files", dest="max_files", type=int, default=None, help="Override combined file ceiling (default: 16).")
    parser.add_argument("--respect-pause", dest="respect_pause", action="store_true", help="Skip when orchestration is PAUSED.")
    parser.add_argument("--state-dir", default="", help="Override state directory.")
    parser.add_argument("--repos-dir", default="", help="Optional parent directory for managed git repos.")
    parser.add_argument("--orchestrator-id", default="", help="Optional active orchestrator conversation ID.")
    parser.add_argument("--json", action="store_true", help="Output structured JSON.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    args = parser.parse_args(argv)

    st_dir = _resolve_state_dir(args.state_dir)
    qc_mod = _get_queue_compactor()
    if qc_mod is not None and hasattr(qc_mod, "compact_dev_queue"):
        res = dict(
            qc_mod.compact_dev_queue(
                portfolio_dir=st_dir,
                apply=bool(args.apply),
                tier=args.tier,
                repo_slug=args.repo,
                item_ids=args.ids or None,
                repos_dir=args.repos_dir or None,
                max_combined_files=args.max_files,
                orchestrator_id=args.orchestrator_id,
                respect_pause=bool(args.respect_pause),
            )
        )
    else:
        pr_rem = _resolve_pr_remediator_script()
        cmd = [sys.executable, str(pr_rem), "queue-compact", "--tier", args.tier]
        if args.apply:
            cmd.append("--apply")
        if args.repo:
            cmd.extend(["--repo", args.repo])
        if args.ids:
            cmd.extend(["--ids", *args.ids])
        if args.max_files is not None:
            cmd.extend(["--max-files", str(args.max_files)])
        if args.repos_dir:
            cmd.extend(["--repos-dir", args.repos_dir])
        if args.orchestrator_id:
            cmd.extend(["--orchestrator-id", args.orchestrator_id])
        if args.respect_pause:
            cmd.append("--respect-pause")
        proc = subprocess.run(
            cmd,
            env={**os.environ, "AGY_DEV_STATE_DIR": str(st_dir)},
            capture_output=True,
            text=True,
            check=False,
        )
        res = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {"status": "error", "error": (proc.stderr or proc.stdout).strip()}

    res.setdefault("schema_version", JSON_SCHEMA_VERSION)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif qc_mod is not None and hasattr(qc_mod, "render_compaction_report"):
        color_on = _should_enable_color(args.no_color, sys.stdout.isatty())
        print(qc_mod.render_compaction_report(res, color=color_on))
    else:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    return 0 if res.get("status") == "ok" else 1


def _run_subcommand_sync(argv: list[str]) -> int:
    """Execute `agy-queue sync` subcommand."""
    parser = argparse.ArgumentParser(prog="agy-queue sync", description="Fetch origin refs and reconcile merged PRs.")
    parser.add_argument("--state-dir", default="", help="Override state directory.")
    parser.add_argument("--repos-dir", default="", help="Optional parent directory for managed git repos.")
    parser.add_argument("--offline", action="store_true", help="Reconcile using local Git refs without network fetch.")
    parser.add_argument("--json", action="store_true", help="Output structured JSON.")
    parser.add_argument("--debug", action="store_true", help="Emit debug diagnostics to stderr.")
    args = parser.parse_args(argv)

    st_dir = _resolve_state_dir(args.state_dir)
    q_data = _load_json(st_dir / "dev_queue.json")
    _, all_locks = _load_locks(st_dir)
    _, local_dirs = _facade_attr("_discover_repos", _discover_repos)(all_locks, q_data.get("items") or [], args.repos_dir)
    offline_opt = True if args.offline else None
    msg = _facade_attr("_run_live_sync", _run_live_sync)(st_dir, local_dirs, offline=offline_opt)
    if args.json:
        print(
            json.dumps(
                {"schema_version": JSON_SCHEMA_VERSION, "status": "ok", "state_dir": str(st_dir), "message": msg},
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print(msg)
    return 0


def _run_subcommand_show(argv: list[str]) -> int:
    """Execute `agy-queue show <ID>` subcommand."""
    parser = argparse.ArgumentParser(prog="agy-queue show", description="Inspect a single queue item and live worker telemetry.")
    parser.add_argument("item_id", help="Conversation ID, short ID prefix, or 1-based row number.")
    parser.add_argument("--state-dir", default="", help="Override state directory.")
    parser.add_argument("--repos-dir", default="", help="Optional parent directory for managed git repos.")
    parser.add_argument("--json", action="store_true", help="Output structured JSON.")
    parser.add_argument("--plain-urls", action="store_true", help="Disable OSC-8 hyperlinks.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    parser.add_argument("--debug", action="store_true", help="Emit debug diagnostics to stderr.")
    args = parser.parse_args(argv)

    render_ns = argparse.Namespace(
        watch=None,
        all=True,
        repo="",
        status="",
        sync=False,
        write_board="",
        open_hitl="",
        state_dir=args.state_dir,
        repos_dir=args.repos_dir,
        plain_urls=args.plain_urls,
        no_opt=True,
        json=False,
        no_color=args.no_color,
        debug=args.debug,
    )
    layout: dict[str, Any] = {}
    _facade_attr("render", render)(render_ns, layout_out=layout)
    records = list(layout.get("row_records") or [])
    needle = args.item_id.strip().lower()

    matched_rec: dict[str, Any] | None = None
    if needle.isdigit():
        idx_num = int(needle)
        matched_rec = next((r for r in records if int(r.get("idx") or 0) == idx_num), None)
    if matched_rec is None:
        exact = [
            r
            for r in records
            if str((r.get("item") or {}).get("conversation_id") or "").lower() == needle
            or str(r.get("display_id") or "").lower() == needle
        ]
        if exact:
            matched_rec = exact[0]
        else:
            pref = [
                r
                for r in records
                if str((r.get("item") or {}).get("conversation_id") or "").lower().startswith(needle)
            ]
            if len(pref) == 1:
                matched_rec = pref[0]
            elif len(pref) > 1:
                print(f"[agy-queue show] Ambiguous ID prefix '{args.item_id}' ({len(pref)} matches).", file=sys.stderr)
                return 1

    if matched_rec is None:
        print(f"[agy-queue show] No queue item found matching '{args.item_id}'.", file=sys.stderr)
        return 1

    if args.json:
        item_dict = dict(matched_rec.get("item") or {})
        linked = list(matched_rec.get("linked_locks") or [])
        telem = _extract_lock_telemetry_fields(linked[0] if linked else None, linked[0] if linked else None)
        payload = {
            "schema_version": JSON_SCHEMA_VERSION,
            "status": "ok",
            "effective_status": matched_rec.get("effective_status", ""),
            "sidebar_title": matched_rec.get("sidebar_title", ""),
            "progress": matched_rec.get("progress"),
            "lock_telemetry": telem,
            **telem,
            "linked_locks": linked,
            "item": item_dict,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    color_on = _should_enable_color(args.no_color, sys.stdout.isatty())
    use_osc8 = _supports_osc8() and not args.plain_urls
    term_w = shutil.get_terminal_size((124, 40)).columns
    print("\n".join(_render_detail_inspector_lines(matched_rec, term_w, color_on, use_osc8)))
    return 0


def _run_subcommand_locks(argv: list[str]) -> int:
    """Execute `agy-queue locks` subcommand."""
    parser = argparse.ArgumentParser(prog="agy-queue locks", description="Inspect active per-repository dev_pr_locks.")
    parser.add_argument("-r", "--repo", default="", help="Filter by repository substring.")
    parser.add_argument("--sync-git", action="store_true", help="Reconcile lock state against origin before displaying.")
    parser.add_argument("--state-dir", default="", help="Override state directory.")
    parser.add_argument("--repos-dir", default="", help="Optional parent directory for managed git repos.")
    parser.add_argument("--stall-seconds", type=int, default=None, help="Override stall threshold in seconds.")
    parser.add_argument("--json", action="store_true", help="Output structured JSON.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    parser.add_argument("--debug", action="store_true", help="Emit debug diagnostics to stderr.")
    args = parser.parse_args(argv)

    st_dir = _resolve_state_dir(args.state_dir)
    if args.sync_git:
        ls_mod = _get_lock_store()
        if ls_mod is not None and hasattr(ls_mod, "sync_dev_pr_lock_with_git"):
            ls_mod.sync_dev_pr_lock_with_git(portfolio_dir=st_dir)
        else:
            pr_rem = _resolve_pr_remediator_script()
            if pr_rem.is_file():
                subprocess.run(
                    [sys.executable, str(pr_rem), "lock-status", "--all", "--sync-git"],
                    env={**os.environ, "AGY_DEV_STATE_DIR": str(st_dir)},
                    capture_output=True,
                    text=True,
                    timeout=20.0,
                    check=False,
                )

    locks, _ = _load_locks(st_dir)
    repo_f = args.repo.strip().lower()
    out_locks: dict[str, dict[str, Any]] = {}
    for slug, repo_lk in sorted(locks.items()):
        if repo_f and repo_f not in slug.lower():
            continue
        slots_out: list[dict[str, Any]] = []
        for slot_entry in _iter_active_lock_slots(repo_lk):
            lp = _compute_lock_progress(slot_entry, threshold_seconds=args.stall_seconds)
            telem = _extract_lock_telemetry_fields(lp, slot_entry)
            slots_out.append({**slot_entry, **telem, "progress": lp})
        primary_telem = _extract_lock_telemetry_fields(
            slots_out[0]["progress"] if slots_out else None,
            slots_out[0] if slots_out else repo_lk,
        )
        out_locks[slug] = {**repo_lk, **primary_telem, "active_slots": slots_out}

    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": JSON_SCHEMA_VERSION,
                    "status": "ok",
                    "state_dir": str(st_dir),
                    "active_count": len(out_locks),
                    "locks": out_locks,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    color_on = _should_enable_color(args.no_color, sys.stdout.isatty())
    if not out_locks:
        print(_color("No active repository locks.", "0;32", color_on))
        return 0

    bar_w = min(shutil.get_terminal_size((124, 40)).columns, 220)
    lines = [_color("═" * bar_w, "2;36", color_on), _color(f"ACTIVE REPOSITORY LOCKS ({len(out_locks)})", "1;36", color_on)]
    for slug, info in out_locks.items():
        occ = _lock_slot_occupancy(info)
        lines.append(_color("─" * bar_w, "2", color_on))
        lines.append(f"  {_color(slug, '1;33', color_on)}  [{occ}]")
        for s_item in info.get("active_slots") or []:
            st_s = str(s_item.get("status") or "DEVELOPING")
            br_s = str(s_item.get("branch_name") or "-")
            cid_s = str(s_item.get("conversation_id") or "-")[:8]
            idle_l = str(s_item.get("idle_label") or "0s")
            elap_l = str(s_item.get("elapsed_label") or "0s")
            src_s = str(s_item.get("last_activity_source") or "none")
            idle_st = str(s_item.get("idle_state") or "")
            sem_c = "1;31" if s_item.get("is_stalled") else ("1;33" if idle_st == "amber" else "0;32")
            sem_t = (
                f"STALL {idle_l}"
                if s_item.get("is_stalled")
                else (f"idle {idle_l}" if idle_st == "amber" else f"ok·{idle_l}")
            )
            lines.append(
                f"    Slot #{s_item.get('slot_id', 1)}  {st_s}  cid:{cid_s}  "
                f"{_color(sem_t, sem_c, color_on)}  elapsed:{elap_l}  src:{src_s}  branch:{br_s}"
            )
    lines.append(_color("═" * bar_w, "2;36", color_on))
    print("\n".join(lines))
    return 0


def _run_subcommand_set_status(argv: list[str]) -> int:
    """Execute `agy-queue set-status` subcommand in-process via `queue_store`."""
    parser = argparse.ArgumentParser(prog="agy-queue set-status", description="Transition a queue item status cleanly.")
    parser.add_argument("pos_id", nargs="?", default="", help="Conversation ID or unambiguous prefix.")
    parser.add_argument("pos_status", nargs="?", default="", help="Target queue status.")
    parser.add_argument("--id", dest="flag_id", default="", help="Conversation ID (alternative to positional).")
    parser.add_argument("--status", dest="flag_status", default="", help="Target status (alternative to positional).")
    parser.add_argument("--notes", default="", help="Optional transition notes.")
    parser.add_argument("--branches", nargs="*", default=[], help="Optional branch names.")
    parser.add_argument("--pr-urls", nargs="*", default=[], help="Optional PR URLs.")
    parser.add_argument("--orchestrator-id", default="", help="Optional orchestrator conversation ID.")
    parser.add_argument("--state-dir", default="", help="Override state directory.")
    parser.add_argument("--json", action="store_true", help="Output structured JSON.")
    args = parser.parse_args(argv)

    raw_id = (args.flag_id or args.pos_id).strip()
    raw_status = (args.flag_status or args.pos_status).strip().lower()
    if not raw_id or not raw_status:
        parser.error("both <id> and <status> are required (positional or via --id / --status).")
    if raw_status not in VALID_QUEUE_STATUSES:
        parser.error(f"invalid status {raw_status!r} (choose from: {', '.join(VALID_QUEUE_STATUSES)}).")

    st_dir = _resolve_state_dir(args.state_dir)
    q_items = _load_json(st_dir / "dev_queue.json").get("items") or []
    resolved_id = raw_id
    exact_matches = [str(it.get("conversation_id") or "") for it in q_items if str(it.get("conversation_id") or "").lower() == raw_id.lower()]
    if exact_matches:
        resolved_id = exact_matches[0]
    else:
        pref_matches = [str(it.get("conversation_id") or "") for it in q_items if str(it.get("conversation_id") or "").lower().startswith(raw_id.lower())]
        if len(pref_matches) == 1:
            resolved_id = pref_matches[0]
        elif len(pref_matches) > 1:
            print(f"[agy-queue set-status] Ambiguous ID prefix '{raw_id}' ({len(pref_matches)} matches).", file=sys.stderr)
            return 1

    qs_mod = _get_queue_store()
    if qs_mod is not None and hasattr(qs_mod, "update_dev_queue_item_status"):
        q_res = qs_mod.update_dev_queue_item_status(
            conversation_id=resolved_id,
            status=raw_status,
            branch_names=args.branches or None,
            pr_urls=args.pr_urls or None,
            notes=args.notes or None,
            orchestrator_conversation_id=args.orchestrator_id,
            portfolio_dir=st_dir,
        )
        if q_res.get("status") != "ok":
            sys.stderr.write(str(q_res.get("error") or "[agy-queue set-status] Failed updating item.") + "\n")
            return 1
        out_payload = json.dumps(q_res, indent=2, ensure_ascii=False)
    else:
        pr_rem = _resolve_pr_remediator_script()
        cmd = [sys.executable, str(pr_rem), "queue-set-status", "--id", resolved_id, "--status", raw_status]
        if args.notes:
            cmd.extend(["--notes", args.notes])
        if args.branches:
            cmd.extend(["--branches", *args.branches])
        if args.pr_urls:
            cmd.extend(["--pr-urls", *args.pr_urls])
        if args.orchestrator_id:
            cmd.extend(["--orchestrator-id", args.orchestrator_id])
        proc = subprocess.run(
            cmd,
            env={**os.environ, "AGY_DEV_STATE_DIR": str(st_dir)},
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr or proc.stdout or "[agy-queue set-status] Failed updating item.\n")
            return int(proc.returncode)
        out_payload = (proc.stdout or "{}").strip()

    if args.json:
        print(out_payload)
    else:
        print(f"✓ Updated {resolved_id[:8]} -> {raw_status}")
    return 0


def _run_subcommand_pause(argv: list[str]) -> int:
    """Handle `agy-queue pause [--hard] [--reason ...] [--state-dir DIR] [--json]`."""
    parser = argparse.ArgumentParser(
        prog="agy-queue pause",
        description="Pause orchestration (Graceful Drain by default, or non-destructive Hard Freeze with --hard).",
    )
    parser.add_argument("--reason", default="", help="Optional short Zero-PII reason (e.g. 'Customer focus').")
    parser.add_argument(
        "--hard",
        dest="hard",
        action="store_true",
        help="Activate Hard Stop (Hard Freeze: cut off active workers & kill worktree processes while preserving locks).",
    )
    parser.add_argument("--state-dir", default="", help="Optional state directory override.")
    parser.add_argument("--json", action="store_true", help="Output JSON payload.")
    ns = parser.parse_args(argv)
    st_dir = _resolve_state_dir(ns.state_dir)
    oc_mod = _facade_attr("_get_orch_control", _get_orch_control)()
    if oc_mod is None:
        print("Error: orchestration_control module not found.", file=sys.stderr)
        return 1
    locks, _ = _load_locks(st_dir)
    if ns.hard:
        res = oc_mod.hard_stop_orchestration(
            state_dir=st_dir,
            reason=ns.reason,
            source="cli",
            active_locks=locks,
        )
    else:
        res = oc_mod.pause_orchestration(state_dir=st_dir, reason=ns.reason, source="cli")
    summary = oc_mod.summarize_orchestration_phase(res.get("control"), locks)
    out_payload: dict[str, Any] = {
        "status": "ok",
        "paused": True,
        "hard_stopped": bool(summary.get("hard_stopped")),
        "changed": bool(res.get("changed")),
        "orchestration": summary,
    }
    if ns.hard:
        out_payload["worktree_processes"] = res.get("worktree_processes") or {}
    if ns.json:
        print(json.dumps(out_payload, indent=2, ensure_ascii=False))
    else:
        icon = "⏹" if summary.get("hard_stopped") else "⏸"
        print(f"{icon} {summary.get('banner_text') or 'PAUSED — 0 new dispatches · Watchdog silent'}")
    return 0


def _run_subcommand_stop(argv: list[str]) -> int:
    """Handle `agy-queue stop [--reason ...] [--state-dir DIR] [--json]` (non-destructive Hard Freeze)."""
    parser = argparse.ArgumentParser(
        prog="agy-queue stop",
        description="Hard Stop orchestration (Hard Freeze: freeze active workers, kill worktree subprocesses, preserve locks & worktrees).",
    )
    parser.add_argument("--reason", default="", help="Optional short Zero-PII reason (e.g. 'Live demo').")
    parser.add_argument("--state-dir", default="", help="Optional state directory override.")
    parser.add_argument("--json", action="store_true", help="Output JSON payload.")
    ns = parser.parse_args(argv)
    st_dir = _resolve_state_dir(ns.state_dir)
    oc_mod = _facade_attr("_get_orch_control", _get_orch_control)()
    if oc_mod is None:
        print("Error: orchestration_control module not found.", file=sys.stderr)
        return 1
    locks, _ = _load_locks(st_dir)
    res = oc_mod.hard_stop_orchestration(
        state_dir=st_dir,
        reason=ns.reason,
        source="cli",
        active_locks=locks,
    )
    summary = oc_mod.summarize_orchestration_phase(res.get("control"), locks)
    out_payload = {
        "status": "ok",
        "paused": True,
        "hard_stopped": True,
        "changed": bool(res.get("changed")),
        "chrome_devtools": res.get("chrome_devtools") or {},
        "worktree_processes": res.get("worktree_processes") or {},
        "orchestration": summary,
    }
    if ns.json:
        print(json.dumps(out_payload, indent=2, ensure_ascii=False))
    else:
        print(f"⏹ {summary.get('banner_text') or 'HARD STOPPED — Workers frozen'}")
    return 0


def _run_subcommand_resume(argv: list[str]) -> int:
    """Handle `agy-queue resume [--no-wake] [--dry-run] [--state-dir DIR] [--json]`."""
    parser = argparse.ArgumentParser(
        prog="agy-queue resume",
        description="Continue orchestration and automatically wake the orchestrator.",
    )
    parser.add_argument("--no-wake", dest="no_wake", action="store_true", help="Unpause passively without auto-wake.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Preview wake command without spawning.")
    parser.add_argument("--state-dir", default="", help="Optional state directory override.")
    parser.add_argument("--json", action="store_true", help="Output JSON payload.")
    ns = parser.parse_args(argv)
    st_dir = _resolve_state_dir(ns.state_dir)
    pr_rem = _resolve_pr_remediator_script()
    cmd = [sys.executable, str(pr_rem), "orchestration-resume", "--source", "cli", "--portfolio-dir", str(st_dir)]
    if ns.no_wake:
        cmd.append("--no-wake")
    if ns.dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err_out = (proc.stderr or proc.stdout or "").strip()
        if err_out:
            print(err_out, file=sys.stderr)
        return int(proc.returncode)
    if ns.json:
        print((proc.stdout or "{}").strip())
    else:
        wake_st = ""
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(proc.stdout or "{}")
            wake_st = str((parsed.get("wake") or {}).get("status") or "")
        extra = f" ({wake_st})" if wake_st else ""
        print(f"▶ Orchestration resumed{extra}")
    return 0


def _run_subcommand_status(argv: list[str]) -> int:
    """Handle `agy-queue status [--state-dir DIR] [--json]`."""
    parser = argparse.ArgumentParser(
        prog="agy-queue status",
        description="Show Pause / Draining / Active orchestration status.",
    )
    parser.add_argument("--state-dir", default="", help="Optional state directory override.")
    parser.add_argument("--json", action="store_true", help="Output JSON payload.")
    ns = parser.parse_args(argv)
    st_dir = _resolve_state_dir(ns.state_dir)
    locks, _ = _load_locks(st_dir)
    summary = _load_orchestration_phase(st_dir, locks)
    if ns.json:
        print(json.dumps({"status": "ok", "orchestration": summary}, indent=2, ensure_ascii=False))
    else:
        if summary.get("hard_stopped"):
            print(f"⏹ {summary.get('banner_text') or 'HARD STOPPED — Workers frozen'}")
        elif summary.get("paused"):
            print(f"⏸ {summary.get('banner_text') or 'PAUSED — 0 new dispatches · Watchdog silent'}")
        else:
            ongoing = int(summary.get("ongoing_count") or 0)
            print(f"▶ ACTIVE — {ongoing} ongoing task(s) · new dispatches enabled")
    return 0


def _run_subcommand_plan(argv: list[str]) -> int:
    """Handle `agy-queue plan [--repo SLUG] [--importance TIER] [--include-done N] [--json]`."""
    parser = argparse.ArgumentParser(
        prog="agy-queue plan",
        description="Render 0-LLM Repo -> Sub-Component planning view (Importance x Wave & Recently Integrated).",
    )
    parser.add_argument("--repo", default="", help="Filter by repository slug or substring.")
    parser.add_argument(
        "--importance",
        choices=("critical", "high", "medium", "low"),
        default="",
        help="Filter open items by importance tier.",
    )
    parser.add_argument(
        "--include-done",
        dest="include_done",
        type=int,
        default=10,
        help="Maximum recently integrated (merged/cleaned) items to include (default: 10).",
    )
    parser.add_argument("--state-dir", default="", help="Override state directory.")
    parser.add_argument("--repos-dir", default="", help="Optional parent directory for managed git repos.")
    parser.add_argument("--json", action="store_true", help="Output structured JSON.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    parser.add_argument("--debug", action="store_true", help="Emit debug diagnostics to stderr.")
    args = parser.parse_args(argv)

    st_dir = _resolve_state_dir(args.state_dir)
    q_data = _load_json(st_dir / "dev_queue.json")
    raw_items = [it for it in (q_data.get("items") or []) if isinstance(it, dict)]
    locks, all_locks = _load_locks(st_dir)
    repo_web_bases, _ = _facade_attr("_discover_repos", _discover_repos)(all_locks, raw_items, args.repos_dir)
    all_slugs: set[str] = set(locks.keys()) | set(repo_web_bases.keys())
    for it in raw_items:
        for s in it.get("repo_slugs") or []:
            if s:
                all_slugs.add(str(s))
    label_map = _build_repo_labels(all_slugs)
    cids = [str(it.get("conversation_id") or "").strip() for it in raw_items if it.get("conversation_id")]
    ag_titles = _load_conversation_titles(cids)

    qp = _facade_attr("_get_queue_planner", _get_queue_planner)()
    if qp is not None and hasattr(qp, "build_component_planning_snapshot"):
        plan = dict(
            qp.build_component_planning_snapshot(
                raw_items,
                locks=locks,
                repo_labels=label_map,
                repo_filter=args.repo,
                importance_filter=args.importance,
                include_done=args.include_done,
                ag_titles=ag_titles,
            )
        )
    else:
        pr_rem = _resolve_pr_remediator_script()
        cmd = [sys.executable, str(pr_rem), "queue-plan", "--include-done", str(args.include_done)]
        if args.repo:
            cmd.extend(["--repo", args.repo])
        if args.importance:
            cmd.extend(["--importance", args.importance])
        proc = subprocess.run(
            cmd,
            env={**os.environ, "AGY_DEV_STATE_DIR": str(st_dir)},
            capture_output=True,
            text=True,
            check=False,
        )
        plan = json.loads(proc.stdout or "{}") if proc.returncode == 0 else {"status": "error", "repos": []}

    plan.setdefault("schema_version", JSON_SCHEMA_VERSION)
    if args.json:
        print(json.dumps({"status": "ok", **plan}, indent=2, ensure_ascii=False))
        return 0
    color_on = _should_enable_color(args.no_color, sys.stdout.isatty())
    term_w = shutil.get_terminal_size((124, 40)).columns
    print("\n".join(_render_plan_lines(plan, term_w, color_on)))
    return 0
