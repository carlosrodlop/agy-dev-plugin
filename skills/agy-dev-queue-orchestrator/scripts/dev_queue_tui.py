#!/usr/bin/env python3
"""Compact zero-hardcode Dev Queue TUI CLI entrypoint & backward-compatible facade (`dev_queue_tui.py`).

Thin CLI entrypoint and re-export facade delegating domain logic to cohesive `tui_*` modules:
  - `tui_models.py`: `@dataclass` view-models (`QueueRowView`, `RepoPillView`, `DashboardSnapshot`, `RenderResult`, `WatchState`)
  - `tui_state.py`: JSON/SQLite/Git snapshot loading, repo discovery, in-process sync, and HITL `nav_intent`
  - `tui_progress.py`: lock/item progress calculation and PR URL richness ranking
  - `tui_render.py`: Unicode cell-width layout, `QueueRowView.status` viewport composer, and TUI overlays
  - `tui_board.py`: atomic SHA-256 `dev_queue_board.md` writer and desktop HITL navigation
  - `tui_watch.py`: `termios`/`select` watch loop, doctor hotkey, subcommands, and AST-guarded auto-reload
"""

from __future__ import annotations

import argparse
import contextlib
import os  # noqa: F401
from pathlib import Path
import select  # noqa: F401
import signal
import subprocess
import sys
import termios  # noqa: F401
import traceback
import tty  # noqa: F401
from typing import Any

_SCRIPT_DIR_STR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR_STR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR_STR)

if __name__ != "__main__":
    _current_mod = sys.modules.get(__name__)
    if _current_mod is not None:
        sys.modules.setdefault("dev_queue_tui", _current_mod)
        sys.modules["dev_queue_tui"] = _current_mod

from tui_board import (  # noqa: F401, E402
    _BOARD_DIGEST_CACHE,
    _board_semantic_digest,
    _copy_to_clipboard,
    _escape_md_url,
    _focus_antigravity_app,
    _handle_board_hotkey,
    _open_url_in_browser,
    _sanitize_md_table_cell,
    _trigger_hitl_navigation,
    _write_live_antigravity_board,
)
from tui_models import (  # noqa: F401, E402
    _BOARD_UPDATED_LINE_RE,
    _CREATED_PR_PATH_RE,
    _INSTALLED_PLUGIN_ROOT,
    _RELOAD_ENV_FLAG,
    _SCRIPT_PLUGIN_ROOT,
    _STATUS_STYLES,
    _UUID_RE,
    AGENT_REVIEW_LOCK_STATUSES,
    AGY_QUEUE_SUBCOMMANDS,
    ALT_SCREEN_ENTER,
    ALT_SCREEN_EXIT,
    ANSI_RE,
    DEFAULT_MAX_REPO_SLOTS,
    DEFAULT_NAV_INTENT_ACK_WAIT_SECONDS,
    DEFAULT_NAV_INTENT_TTL_SECONDS,
    HUMAN_READY_LOCK_STATUSES,
    IDLE_AMBER_THRESHOLD_SECONDS,
    INACTIVE_STATUSES,
    JSON_SCHEMA_VERSION,
    MAX_REPO_SLOTS_CAP,
    NAV_INTENT_FILENAME,
    NAV_INTENT_LOCK_FILENAME,
    REBASE_OR_AWAIT_LOCK_STATUSES,
    STATUS_ORDER,
    UNREADY_PR_GATE_LOCK_STATUSES,
    VALID_QUEUE_STATUSES,
    DashboardSnapshot,
    QueueRowView,
    RenderResult,
    RepoPillView,
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
    _register_facade_module,
    _register_facade_namespace,
)
from tui_progress import (  # noqa: F401, E402
    _build_display_ids,
    _build_repo_labels,
    _clean_clickable_url,
    _collect_lock_extra_urls,
    _compute_item_progress,
    _compute_lock_progress,
    _detect_stalled_lock_info,
    _extract_compare_branch_from_url,
    _extract_lock_telemetry_fields,
    _inspect_worktree_progress,
    _is_created_pr_url,
    _repo_compare_url,
    _repo_pulls_url,
    _resolve_item_human_pr_link,
    _resolve_lock_human_pr_link,
    _sanitize_full_url,
    _score_pr_url_richness,
    _slot_progress_cache_key,
    _url_has_pr_body,
)
from tui_render import (  # noqa: F401, E402
    _apply_live_filter_and_cursor,
    _build_watch_footer,
    _char_display_width,
    _clamp_lines_to_viewport,
    _color,
    _compact_osc8_pr_label,
    _compact_repos,
    _compose_priority_viewport,
    _compose_priority_viewport_with_meta,
    _compute_lightweight_opt_hint,
    _debug_log,
    _filter_compact_middle_rows,
    _is_dumb_terminal,
    _is_no_color_env,
    _mark_cursor_on_row_line,
    _osc8_link,
    _pad_visible,
    _plan_progress_bar,
    _render_detail_inspector_lines,
    _render_help_overlay_lines,
    _render_inline_progress,
    _render_optimizer_overlay_lines,
    _render_plan_lines,
    _resolve_orchestrator_liveness,
    _should_enable_color,
    _status_badge,
    _supports_osc8,
    _trunc,
    _visible_len,
    _wrapped_row_count,
    render,
    render_dashboard,
)
from tui_state import (  # noqa: F401, E402
    _AsyncSyncController,
    _atomic_write_json_0600,
    _build_repo_discovery_cache_key,
    _check_local_remote_branch_state,
    _cids_match,
    _collect_unlocked_pr_repo_slugs,
    _discover_repos,
    _discover_repos_cached,
    _extract_reconciled_count,
    _extract_repo_slug_from_url,
    _find_live_unmerged_remote_branch_for_cid,
    _format_compact_age,
    _is_lock_base_stale,
    _is_network_remote,
    _is_uuid,
    _iter_active_lock_slots,
    _load_conversation_titles,
    _load_json,
    _load_locks,
    _load_orchestration_phase,
    _lock_slot_occupancy,
    _nav_intent_lock,
    _normalize_repo_lock_slots,
    _parse_remote_to_web_info,
    _parse_waiting_step_from_raw_summary,
    _resolve_board_artifact_path,
    _resolve_brain_dirs,
    _resolve_cli_version,
    _resolve_conv_summaries_db,
    _resolve_effective_item_status,
    _resolve_max_repo_slots,
    _resolve_nav_intent_ack_wait_seconds,
    _resolve_nav_intent_path,
    _resolve_pr_remediator_script,
    _resolve_stall_threshold,
    _resolve_state_dir,
    _resolve_worktree_gitdir,
    _run_live_sync,
    _secondary_slot_progress,
    _slot_label,
    _snapshot_state_fingerprint,
    _synthesize_untracked_lock_items,
    _wait_for_nav_intent_ack,
    acknowledge_nav_intent,
    is_lock_human_ready,
    load_waiting_user_conversations,
    read_active_nav_intent,
    write_nav_intent,
)
from tui_watch import (  # noqa: F401, E402
    _TTY_PUSHBACK_BYTES,
    _build_reload_exec_argv,
    _build_watch_frame,
    _collect_watched_script_paths,
    _format_doctor_report_text,
    _handle_doctor_hotkey,
    _has_tty_pushback,
    _invoke_queue_doctor,
    _parse_watch_key_sequence,
    _read_tty_byte,
    _read_watch_key,
    _run_subcommand_compact,
    _run_subcommand_locks,
    _run_subcommand_optimize,
    _run_subcommand_pause,
    _run_subcommand_plan,
    _run_subcommand_resume,
    _run_subcommand_set_status,
    _run_subcommand_show,
    _run_subcommand_status,
    _run_subcommand_sync,
    _run_watch_json_ndjson_loop,
    _silence_broken_pipe_stdout,
    _snapshot_file_fingerprints,
    _unread_tty_byte,
    _validate_python_syntax,
    run_watch_loop,
)

_git_client: Any = _get_git_client()
_queue_doctor: Any = _get_queue_doctor()

if __name__ != "__main__":
    _register_facade_module(sys.modules.get(__name__))
    _register_facade_namespace(globals())


def _install_sigpipe_hygiene() -> None:
    """Ensure `SIGPIPE` raises `BrokenPipeError` cleanly without signal tracebacks."""
    sigpipe = getattr(signal, "SIGPIPE", None)
    if sigpipe is not None:
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sigpipe, signal.SIG_IGN)


def _normalize_status_arg(raw: str) -> str:
    """Validate and normalize `--status` CLI flag values."""
    val = str(raw or "").strip().lower()
    if not val:
        return ""
    if val not in VALID_QUEUE_STATUSES:
        valid_str = ", ".join(VALID_QUEUE_STATUSES)
        raise argparse.ArgumentTypeError(f"invalid status {raw!r} (choose from: {valid_str})")
    return val


def _build_parser() -> argparse.ArgumentParser:
    """Build the `agy-queue` CLI argument parser."""
    epilog_txt = (
        "Unified subcommands:\n"
        "  agy-queue                     Render live multi-repo queue dashboard\n"
        "  agy-queue watch [SECS]        Flicker-free interactive TUI (--watch)\n"
        "  agy-queue doctor [FLAGS]      Run 0-LLM orchestrator doctor (--fix/--wake/--heal)\n"
        "  agy-queue optimize [FLAGS]    Run 0-LLM dev flow bottleneck analyzer\n"
        "  agy-queue compact [FLAGS]     Detect or atomically compact overlapping PRs (--apply)\n"
        "  agy-queue plan [FLAGS]        Show Repo -> Sub-Component planning view (Importance x Wave)\n"
        "  agy-queue sync [FLAGS]        Fetch origin refs & reconcile merged PRs\n"
        "  agy-queue list [FLAGS]        List filtered queue items (--status/--repo/--json)\n"
        "  agy-queue show <ID> [FLAGS]   Inspect single item + live worker/reviewer telemetry\n"
        "  agy-queue locks [FLAGS]       Inspect per-repo dev_pr_locks & stall semaphore\n"
        "  agy-queue set-status <ID> <S> Transition queue item status cleanly\n"
        "  agy-queue pause [FLAGS]       Pause new dispatches (Graceful Drain: finish ongoing)\n"
        "  agy-queue resume [FLAGS]      Continue orchestration & auto-wake orchestrator\n"
        "  agy-queue status [FLAGS]      Show Pause / Draining / Active orchestration status\n"
    )
    parser = argparse.ArgumentParser(
        prog="agy-queue",
        description="Compact zero-hardcode CLI & TUI for Antigravity multi-repo dev_queue.json and dev_pr_locks.",
        epilog=epilog_txt,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {_resolve_cli_version()}")
    parser.add_argument(
        "-w",
        "--watch",
        nargs="?",
        const=3.0,
        type=float,
        metavar="SECS",
        help="Auto-refresh dashboard in place every N seconds (default: 3; emits NDJSON with --json).",
    )
    parser.add_argument("-a", "--all", action="store_true", help="Include merged, cleaned, cancelled, and rejected items.")
    parser.add_argument("-r", "--repo", default="", help="Filter by repository substring.")
    parser.add_argument(
        "-s",
        "--status",
        default="",
        type=_normalize_status_arg,
        metavar="STATUS",
        help=f"Filter by exact status ({', '.join(VALID_QUEUE_STATUSES)}).",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Fetch origin refs across managed repos and reconcile merged PRs (status written to stderr).",
    )
    parser.add_argument(
        "--sync-interval",
        type=float,
        default=30.0,
        metavar="SECS",
        help="Seconds between automatic background Git syncs in --watch while locks or open PRs exist (<=0 disables; default: 30).",
    )
    parser.add_argument(
        "--no-auto-sync",
        dest="no_auto_sync",
        action="store_true",
        help="Disable periodic background Git auto-sync in --watch mode.",
    )
    parser.add_argument(
        "--write-board",
        default="",
        metavar="PATH",
        help="Write/sync live Antigravity Markdown Artifact board with native conversation:// links.",
    )
    parser.add_argument(
        "--open-hitl",
        default="",
        metavar="H#",
        help="Copy HITL conversation sidebar title and focus Antigravity Desktop App (e.g. h1).",
    )
    parser.add_argument("--state-dir", default="", help="Override state directory containing dev_queue.json.")
    parser.add_argument("--repos-dir", default="", help="Optional local parent directory to scan for managed git repos.")
    parser.add_argument("--plain-urls", action="store_true", help="Disable OSC-8 hyperlink escape sequences.")
    parser.add_argument("--no-opt", dest="no_opt", action="store_true", help="Hide compact OPT: flow-optimizer footer hint.")
    parser.add_argument("--json", action="store_true", help="Output structured JSON (NDJSON stream when combined with --watch).")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color sequences (also honors NO_COLOR and TERM=dumb).")
    parser.add_argument("--debug", action="store_true", help="Emit diagnostic timing and tracebacks to stderr.")
    return parser


def _dispatch_subcommand(subcmd: str, rest: list[str], raw_cli_argv: list[str]) -> int:
    """Dispatch unified `agy-queue` subcommands while keeping full legacy flag compatibility."""
    if subcmd == "doctor":
        if _dev_common is not None and hasattr(_dev_common, "run_dev_queue_doctor_cli"):
            return int(_dev_common.run_dev_queue_doctor_cli(rest))
        pr_rem = _resolve_pr_remediator_script()
        proc = subprocess.run([sys.executable, str(pr_rem), "queue-doctor", *rest], check=False)
        return int(proc.returncode)
    if subcmd == "watch":
        if rest and not rest[0].startswith("-"):
            with contextlib.suppress(ValueError):
                float(rest[0])
                return _main_dashboard(["--watch", rest[0], *rest[1:]], raw_cli_argv)
        return _main_dashboard(["--watch", *rest], raw_cli_argv)
    if subcmd == "list":
        return _main_dashboard(rest, raw_cli_argv)
    if subcmd == "optimize":
        return _run_subcommand_optimize(rest)
    if subcmd == "compact":
        return _run_subcommand_compact(rest)
    if subcmd == "plan":
        return _run_subcommand_plan(rest)
    if subcmd == "sync":
        return _run_subcommand_sync(rest)
    if subcmd == "show":
        return _run_subcommand_show(rest)
    if subcmd == "locks":
        return _run_subcommand_locks(rest)
    if subcmd == "set-status":
        return _run_subcommand_set_status(rest)
    if subcmd == "pause":
        return _run_subcommand_pause(rest)
    if subcmd == "resume":
        return _run_subcommand_resume(rest)
    if subcmd == "status":
        return _run_subcommand_status(rest)
    return 1


def _main_dashboard(argv: list[str], raw_cli_argv: list[str]) -> int:
    """Run default dashboard render, `--open-hitl`, `--sync`, or `--watch` loop."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    args._raw_argv = raw_cli_argv

    try:
        if args.open_hitl:
            _, _, hitl_convs, _, open_state_dir, _ = render(args)
            key_clean = args.open_hitl.strip().lower().lstrip("h")
            h_idx = int(key_clean) if key_clean.isdigit() else 0
            entry = hitl_convs.get(h_idx) if h_idx > 0 else None
            if not entry:
                print(f"No HITL item found for '{args.open_hitl}'.", file=sys.stderr)
                return 1
            cid_disp, cid_full, ag_title = entry
            nav_res = _trigger_hitl_navigation(
                open_state_dir,
                index=h_idx,
                cid_disp=cid_disp,
                cid_full=cid_full,
                ag_title=ag_title,
            )
            if nav_res.get("acknowledged"):
                print(f"✓ Dev Queue Panel opened '{ag_title}' ({cid_disp}) in Antigravity Desktop")
            else:
                print(
                    f"✓ Wrote nav_intent, copied '{ag_title}' ({cid_disp}) & focused Antigravity Desktop "
                    "(Dev Queue Panel auto-switches when open, or paste in sidebar search)"
                )
            return 0

        if args.sync:
            st_dir = _resolve_state_dir(args.state_dir)
            q_data = _load_json(st_dir / "dev_queue.json")
            _, all_locks = _load_locks(st_dir)
            _, local_dirs = _discover_repos(all_locks, q_data.get("items") or [], args.repos_dir)
            msg = _run_live_sync(st_dir, local_dirs)
            if args.watch is None:
                print(msg, file=sys.stderr)

        if args.watch is None:
            out, _, _, _, _, _ = render(args)
            print(out)
            return 0

        return run_watch_loop(args, raw_argv=raw_cli_argv)
    except Exception:
        if getattr(args, "debug", False):
            traceback.print_exc(file=sys.stderr)
        raise


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `dev_queue_tui.py` / `agy-queue`."""
    _install_sigpipe_hygiene()
    raw_cli_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if raw_cli_argv and raw_cli_argv[0] in AGY_QUEUE_SUBCOMMANDS:
            return _dispatch_subcommand(raw_cli_argv[0], raw_cli_argv[1:], raw_cli_argv)
        return _main_dashboard(raw_cli_argv, raw_cli_argv)
    except BrokenPipeError:
        return _silence_broken_pipe_stdout()


if __name__ == "__main__":
    raise SystemExit(main())
