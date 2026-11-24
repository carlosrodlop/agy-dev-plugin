#!/usr/bin/env python3
"""Compact zero-hardcode Dev Queue TUI and live Antigravity HITL Board Artifact generator.

Renders a single-table multi-repo status dashboard backed by `dev_queue.json` and
per-repository `dev_pr_locks` (`actions_state.json` / `dev_pr_lock.json`), with:
  - Dynamic zero-hardcode state and repository discovery (`dev_common` + `git_client`)
  - Strict `pr_gate_ready` and `HUMAN_READY_LOCK_STATUSES` gating (never leaking unreviewed PRs)
  - Live inactivity semaphore inside `PROGRESS` (`<3m` `ok·<idle>`, `3-14m` amber `idle <idle>`,
    `>=15m` red `STALL <idle>`) including `AWAITING_AGENT_REVIEW` + `reviewer_subagent_id`
  - Multi-repo PR-ready progress clarity (`PR 1/2 ready` yellow / `PR ready` green; never averaging
    a human-ready PR with unstarted downstream repos) and exact `★` repo attribution + unlock footer
  - Richest PR URL ranking (`/pull/` > verified `?title=&body=` > bare `?expand=1`) with compact
    OSC-8 hyperlink labels and full query preservation across hotkeys and `dev_queue_board.md`
  - Unified `agy-queue` subcommands (`watch`, `doctor`, `optimize`, `sync`, `list`, `show`,
    `locks`, `set-status`) + `<1ms` pure viewport repaint and interactive TUI overlays
"""

from __future__ import annotations

import argparse
import ast
from collections.abc import Callable, Sequence
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import termios
import threading
import time
import traceback
import tty
from typing import Any, TextIO
import unicodedata
from urllib.parse import parse_qs, unquote, urlsplit
import uuid
import webbrowser

_SCRIPT_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_INSTALLED_PLUGIN_ROOT = Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin"

for _cand_clients in (_SCRIPT_PLUGIN_ROOT / "clients", _INSTALLED_PLUGIN_ROOT / "clients"):
    if _cand_clients.is_dir():
        _clients_str = str(_cand_clients)
        if _clients_str not in sys.path:
            sys.path.insert(0, _clients_str)
        break

for _cand_opt in (
    _SCRIPT_PLUGIN_ROOT / "skills" / "agy-code-remediation-pr" / "scripts",
    _INSTALLED_PLUGIN_ROOT / "skills" / "agy-code-remediation-pr" / "scripts",
):
    if (_cand_opt / "dev_flow_optimizer.py").is_file():
        _opt_str = str(_cand_opt)
        if _opt_str not in sys.path:
            sys.path.insert(0, _opt_str)
        break

try:
    import dev_common as _dev_common  # type: ignore[import-not-found]
except Exception:
    _dev_common = None

try:
    import dev_flow_optimizer as _flow_opt  # type: ignore[import-not-found]
except Exception:
    _flow_opt = None

_git_client: Any = None
_git_client_loaded = False
_queue_doctor: Any = None
_queue_doctor_loaded = False
_orch_ctrl_mod: Any = None
_orch_ctrl_loaded = False


def _get_orch_control() -> Any:
    global _orch_ctrl_mod, _orch_ctrl_loaded
    if not _orch_ctrl_loaded:
        _orch_ctrl_loaded = True
        with contextlib.suppress(Exception):
            import orchestration_control as _oc  # type: ignore[import-not-found]

            _orch_ctrl_mod = _oc
    return _orch_ctrl_mod


def _load_orchestration_phase(state_dir: Path, locks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    oc = _get_orch_control()
    if oc is not None:
        with contextlib.suppress(Exception):
            ctrl = oc.load_orchestration_control(state_dir=state_dir)
            return dict(oc.summarize_orchestration_phase(ctrl, locks))
    return {
        "paused": False,
        "phase": "active",
        "ongoing_count": 0,
        "new_dispatches_blocked": False,
        "banner_text": "",
        "reason": "",
        "paused_at": "",
        "resumed_at": "",
    }


def _get_git_client() -> Any:
    global _git_client, _git_client_loaded
    if not _git_client_loaded:
        _git_client_loaded = True
        with contextlib.suppress(Exception):
            import git_client as _gc  # type: ignore[import-not-found]

            _git_client = _gc
    return _git_client


def _get_queue_doctor() -> Any:
    global _queue_doctor, _queue_doctor_loaded
    if not _queue_doctor_loaded:
        _queue_doctor_loaded = True
        with contextlib.suppress(Exception):
            import dev_queue_doctor as _qd  # type: ignore[import-not-found]

            _queue_doctor = _qd
    return _queue_doctor

ANSI_RE = re.compile(r"\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07)|\x1b\[[0-9;?]*[A-Za-z]")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_CREATED_PR_PATH_RE = re.compile(r"/(?:pull|pulls|pull-requests|merge_requests)/\d+(?:/|$)")
_BOARD_UPDATED_LINE_RE = re.compile(r"^> \*\*Updated\*\*: `[^`]*`", re.MULTILINE)
_BOARD_DIGEST_CACHE: dict[str, tuple[int, int, str]] = {}
_RELOAD_ENV_FLAG = "_AGY_QUEUE_RELOADED"

ALT_SCREEN_ENTER = "\033[?1049h\033[?25l\033[H\033[2J"
ALT_SCREEN_EXIT = "\033[?25h\033[?1049l"

JSON_SCHEMA_VERSION = 1
IDLE_AMBER_THRESHOLD_SECONDS = 180

STATUS_ORDER: dict[str, int] = {
    "pr_open": 0,
    "needs_rebase_reverify": 1,
    "ready_awaiting_base_merge": 1,
    "needs_agent_semantic_fix": 1,
    "needs_agent_conflict_resolution": 1,
    "in_progress": 1,
    "pending": 2,
    "hitl_blocked": 3,
    "standby": 4,
    "merged": 5,
    "cleaned": 6,
    "cancelled": 7,
    "rejected": 8,
}

VALID_QUEUE_STATUSES: tuple[str, ...] = tuple(STATUS_ORDER.keys())
AGY_QUEUE_SUBCOMMANDS: tuple[str, ...] = (
    "watch",
    "doctor",
    "optimize",
    "sync",
    "list",
    "show",
    "locks",
    "set-status",
    "pause",
    "resume",
    "status",
)

INACTIVE_STATUSES: frozenset[str] = frozenset({"merged", "cleaned", "cancelled", "rejected"})

HUMAN_READY_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "PENDING_HUMAN_PR_VALIDATION",
        "PR_OPEN",
        "REVIEW_PASSED",
        "REVIEW_APPROVED",
        "NEEDS_HUMAN_REVIEW",
    }
)

AGENT_REVIEW_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "AWAITING_AGENT_REVIEW",
        "NEEDS_REVISION_ITERATING",
        "BRANCH_PUSHED_AWAITING_AGENT_REVIEW",
        "BRANCH_PUSHED",
        "READY_FOR_PR",
    }
)

REBASE_OR_AWAIT_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "READY_AWAITING_BASE_MERGE",
        "NEEDS_REBASE_REVERIFY",
        "NEEDS_AGENT_SEMANTIC_FIX",
        "NEEDS_AGENT_CONFLICT_RESOLUTION",
    }
)

UNREADY_PR_GATE_LOCK_STATUSES: frozenset[str] = frozenset(
    set(
        getattr(
            _dev_common,
            "UNREADY_PR_GATE_LOCK_STATUSES",
            frozenset(
                {
                    "AWAITING_AGENT_REVIEW",
                    "NEEDS_REVISION_ITERATING",
                    "ESCALATED_MAX_REVIEW_ITERATIONS",
                }
            ),
        )
    )
    | set(REBASE_OR_AWAIT_LOCK_STATUSES)
)


def _resolve_pr_remediator_script() -> Path:
    """Locate `pr_remediator.py` relative to this plugin checkout or the installed plugin mirror."""
    for root in (_SCRIPT_PLUGIN_ROOT, _INSTALLED_PLUGIN_ROOT):
        cand = root / "skills" / "agy-code-remediation-pr" / "scripts" / "pr_remediator.py"
        if cand.is_file():
            return cand
    return _INSTALLED_PLUGIN_ROOT / "skills" / "agy-code-remediation-pr" / "scripts" / "pr_remediator.py"


def _resolve_conv_summaries_db(override: Path | str | None = None) -> Path:
    """Resolve the Antigravity `conversation_summaries.db` SQLite path."""
    if override is not None and str(override).strip():
        return Path(str(override)).expanduser()
    env_val = str(os.environ.get("AGY_CONVERSATION_SUMMARIES_DB") or "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return Path.home() / ".gemini" / "antigravity" / "conversation_summaries.db"


def _resolve_brain_dirs(
    override: Path | str | Sequence[Path | str] | None = None,
    *,
    home_dir: Path | None = None,
) -> list[Path]:
    """Resolve Antigravity Desktop + CLI `brain/` directories via `dev_common`."""
    if _dev_common is not None and hasattr(_dev_common, "resolve_antigravity_brain_dirs"):
        return list(_dev_common.resolve_antigravity_brain_dirs(override, home_dir=home_dir))
    if override is not None:
        if isinstance(override, Sequence) and not isinstance(override, (str, bytes)):
            return [Path(str(p).strip()).expanduser() for p in override if str(p).strip()]
        s = str(override).strip()
        return [Path(s).expanduser()] if s else []
    env_val = str(os.environ.get("AGY_BRAIN_DIR") or "").strip()
    if env_val:
        return [Path(env_val).expanduser()]
    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    return [
        base_home / ".gemini" / "antigravity" / "brain",
        base_home / ".gemini" / "antigravity-cli" / "brain",
    ]


def _is_dumb_terminal() -> bool:
    """Return True when `TERM=dumb` is set in the environment."""
    return (os.environ.get("TERM") or "").strip().lower() == "dumb"


def _is_no_color_env() -> bool:
    """Return True when non-empty `NO_COLOR` is set (`https://no-color.org`)."""
    return bool((os.environ.get("NO_COLOR") or "").strip())


def _should_enable_color(no_color_flag: bool, is_tty: bool, *, honor_dumb_term: bool = True) -> bool:
    """Determine whether ANSI SGR colors should be emitted."""
    if not is_tty or bool(no_color_flag) or _is_no_color_env():
        return False
    if honor_dumb_term and _is_dumb_terminal():
        return False
    return True


def _resolve_cli_version() -> str:
    """Resolve `agy-queue` CLI version from `plugin.json` or `pyproject.toml`."""
    for root in (_SCRIPT_PLUGIN_ROOT, _INSTALLED_PLUGIN_ROOT):
        pjson = root / "plugin.json"
        if pjson.is_file():
            with contextlib.suppress(OSError, json.JSONDecodeError):
                ver = str((json.loads(pjson.read_text(encoding="utf-8")) or {}).get("version") or "").strip()
                if ver:
                    return ver
        pyproj = root / "pyproject.toml"
        if pyproj.is_file():
            with contextlib.suppress(OSError):
                m = re.search(r'^version\s*=\s*"([^"]+)"', pyproj.read_text(encoding="utf-8"), re.MULTILINE)
                if m and m.group(1).strip():
                    return m.group(1).strip()
    return "1.0.0"


def _debug_log(enabled: bool, msg: str) -> None:
    """Emit timestamped debug diagnostics to `stderr` when `--debug` is enabled."""
    if enabled:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        with contextlib.suppress(OSError):
            sys.stderr.write(f"[agy-queue:debug {ts}Z] {msg}\n")


def _silence_broken_pipe_stdout() -> int:
    """Redirect stdout fd to `/dev/null` on `BrokenPipeError` so pipe consumers (`head`) exit cleanly."""
    with contextlib.suppress(OSError, ValueError):
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        finally:
            os.close(devnull_fd)
    return 0


def _install_sigpipe_hygiene() -> None:
    """Ensure `SIGPIPE` raises `BrokenPipeError` cleanly without signal tracebacks."""
    sigpipe = getattr(signal, "SIGPIPE", None)
    if sigpipe is not None:
        with contextlib.suppress(OSError, ValueError):
            signal.signal(sigpipe, signal.SIG_IGN)


def _color(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _char_display_width(ch: str) -> int:
    """Compute terminal column width for a single Unicode codepoint (`0`, `1`, or `2`)."""
    cp = ord(ch)
    if cp < 32 or cp == 127:
        return 0
    if unicodedata.combining(ch):
        return 0
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def _visible_len(text: str) -> int:
    """Compute visible terminal display width ignoring ANSI/OSC-8 escapes and accounting for wide/combining chars."""
    plain = ANSI_RE.sub("", text or "")
    return sum(_char_display_width(ch) for ch in plain)


def _pad_visible(text: str, width: int) -> str:
    pad = max(0, width - _visible_len(text))
    return text + (" " * pad)


def _trunc(text: str, width: int) -> str:
    """Truncate `text` to at most `width` terminal display columns using `unicodedata` cell widths."""
    if width <= 0:
        return ""
    plain = ANSI_RE.sub("", str(text or ""))
    if _visible_len(plain) <= width:
        return plain
    if width <= 1:
        out_min: list[str] = []
        used_min = 0
        for ch in plain:
            cw = _char_display_width(ch)
            if used_min + cw > width:
                break
            out_min.append(ch)
            used_min += cw
        return "".join(out_min)

    limit = width - 1
    out_chars: list[str] = []
    used = 0
    for ch in plain:
        cw = _char_display_width(ch)
        if cw == 0:
            if out_chars:
                out_chars.append(ch)
            continue
        if used + cw > limit:
            break
        out_chars.append(ch)
        used += cw
    return "".join(out_chars) + "…"


def _supports_osc8(stream: TextIO | None = None) -> bool:
    if _is_dumb_terminal():
        return False
    term_prog = (os.environ.get("TERM_PROGRAM") or "").lower()
    if term_prog == "apple_terminal":
        return False
    target = stream if stream is not None else sys.stdout
    return bool(getattr(target, "isatty", lambda: False)())


def _sanitize_full_url(url: str) -> str:
    """Validate and clean a full clickable HTTP(S) URL while preserving `?title=...&body=...` params."""
    raw = re.sub(r"[\x00-\x1f\x7f]", "", (url or "").strip())
    if not raw or not raw.startswith(("http://", "https://")):
        return ""
    return raw


def _escape_md_url(url: str) -> str:
    """Escape whitespace, parentheses, and pipes so rich PR URLs never break Markdown tables or links."""
    clean = _sanitize_full_url(url)
    if not clean:
        return ""
    return clean.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("|", "%7C")


def _sanitize_md_table_cell(text: str) -> str:
    """Collapse newlines/control whitespace and replace pipes so Markdown table rows never break."""
    collapsed = " ".join(str(text or "").split())
    return collapsed.replace("|", "/")


def _clean_clickable_url(url: str) -> str:
    """Build a short single-line display URL strictly for compact visible labels (never for link targets)."""
    raw = _sanitize_full_url(url)
    if not raw:
        return ""
    parsed = urlsplit(raw)
    base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    if "/compare/" in parsed.path:
        return f"{base}?expand=1"
    return base


def _osc8_link(label: str, url: str, enabled: bool) -> str:
    """Wrap `label` in an OSC-8 hyperlink targeting the full unstripped URL."""
    if not enabled or not url:
        return label
    target = _sanitize_full_url(url) or url
    return f"\033]8;;{target}\007{label}\033]8;;\007"


def _compact_osc8_pr_label(url: str, repo_lbl: str = "", *, max_width: int = 84) -> str:
    """Build a compact single-line OSC-8 PR label that never overflows 123-col terminals."""
    clean = _sanitize_full_url(url)
    if not clean:
        return ""
    parsed = urlsplit(clean)
    slug = _extract_repo_slug_from_url(clean)
    disp_repo = repo_lbl or (slug.split("/")[-1] if slug else parsed.netloc)
    m_num = re.search(r"/(?:pull|pulls|pull-requests|merge_requests)/(\d+)(?:/|$)", parsed.path or "")
    if m_num:
        return _trunc(f"↗ Open PR #{m_num.group(1)} ({disp_repo})", max_width)
    br = _extract_compare_branch_from_url(clean)
    if br:
        return _trunc(f"↗ Open PR · {disp_repo} (main...{br})", max_width)
    return _trunc(f"↗ Open PR · {_clean_clickable_url(clean)}", max_width)


def _copy_to_clipboard(text: str) -> bool:
    """Copy `text` to the OS clipboard cleanly on macOS and Linux."""
    if not text:
        return False
    try:
        sys_name = platform.system()
        if sys_name == "Darwin" and shutil.which("pbcopy"):
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True, timeout=1.5)
            return True
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            if shutil.which(cmd[0]):
                subprocess.run(cmd, input=text.encode("utf-8"), check=True, timeout=1.5)
                return True
    except Exception:
        pass
    return False


def _focus_antigravity_app() -> bool:
    """Focus the Antigravity Desktop GUI application without ever spawning `agy --conversation`."""
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(
                ["open", "-a", "Antigravity"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        if shutil.which("xdg-open"):
            subprocess.Popen(
                ["xdg-open", "antigravity://"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
    except Exception:
        pass
    return False


def _open_url_in_browser(url: str) -> bool:
    """Open a full URL (preserving `?title=...&body=...`) in the default desktop browser."""
    clean_url = _sanitize_full_url(url)
    if not clean_url:
        return False
    try:
        sys_name = platform.system()
        if sys_name == "Darwin":
            subprocess.Popen(
                ["open", clean_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        if sys_name == "Linux" and shutil.which("xdg-open"):
            subprocess.Popen(
                ["xdg-open", clean_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        return bool(webbrowser.open(clean_url))
    except Exception:
        return False


def _resolve_state_dir(override: str = "") -> Path:
    """Resolve persistent local state directory dynamically via `dev_common.resolve_state_dir()`."""
    if override and str(override).strip():
        return Path(str(override)).expanduser().resolve()
    if _dev_common is not None and hasattr(_dev_common, "resolve_state_dir"):
        try:
            return Path(_dev_common.resolve_state_dir()).expanduser().resolve()
        except Exception:
            pass
    env_state = str(os.environ.get("AGY_DEV_STATE_DIR") or "").strip()
    if env_state:
        return Path(env_state).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "agy-dev-plugin").resolve()


def _load_json(path: Path, *, strict: bool = False) -> dict[str, Any]:
    if strict:
        try:
            raw_text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        if not raw_text.strip():
            raise json.JSONDecodeError("Empty JSON state file", raw_text, 0)
        data = json.loads(raw_text)
        return data if isinstance(data, dict) else {}
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _is_uuid(val: str) -> bool:
    return bool(_UUID_RE.match((val or "").strip()))


def _load_conversation_titles(
    cids: list[str],
    *,
    db_path: Path | str | None = None,
    strict: bool = False,
) -> dict[str, str]:
    """Read exact Antigravity Desktop sidebar conversation titles from `conversation_summaries.db`."""
    clean_ids = [c for c in dict.fromkeys(cids) if _is_uuid(c)]
    explicit_db = bool(
        (db_path is not None and str(db_path).strip())
        or str(os.environ.get("AGY_CONVERSATION_SUMMARIES_DB") or "").strip()
    )
    resolved_db = _resolve_conv_summaries_db(db_path)
    if not clean_ids or not resolved_db.is_file():
        return {}
    titles: dict[str, str] = {}
    for uri in (f"file:{resolved_db}?mode=ro", f"file:{resolved_db}?mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.5)
            try:
                placeholders = ",".join("?" for _ in clean_ids)
                for cid, title in conn.execute(
                    f"SELECT conversation_id, title FROM conversation_summaries WHERE conversation_id IN ({placeholders})",
                    clean_ids,
                ).fetchall():
                    if title:
                        titles[str(cid)] = str(title).strip()
            finally:
                conn.close()
            return titles
        except sqlite3.OperationalError:
            if uri.endswith("immutable=1") and strict and explicit_db:
                raise
            continue
        except sqlite3.Error:
            if strict and explicit_db:
                raise
            break
        except Exception:
            break
    return titles


def _is_lock_base_stale(lk: dict[str, Any]) -> bool:
    """Return True when a lock or slot has `base_stale=True`, `needs_base_rebase=True`, or rebase-required status."""
    if bool(lk.get("base_stale")) or bool(lk.get("needs_base_rebase")):
        return True
    status_u = str(lk.get("status") or "").strip().upper()
    return status_u in {
        "NEEDS_REBASE_REVERIFY",
        "NEEDS_AGENT_SEMANTIC_FIX",
        "NEEDS_AGENT_CONFLICT_RESOLUTION",
    }


def _iter_active_lock_slots(lk: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ordered active slot dictionaries (`slot_id` 1, 2, ...) for a repository lock."""
    if not isinstance(lk, dict) or not bool(lk.get("active", True)):
        return []
    raw_slots = lk.get("slots")
    collected: dict[int, dict[str, Any]] = {}
    if isinstance(raw_slots, dict):
        for raw_k, raw_v in raw_slots.items():
            if not isinstance(raw_v, dict) or not bool(raw_v.get("active", True)):
                continue
            try:
                s_id = int(raw_v.get("slot_id") or raw_k)
            except (TypeError, ValueError):
                continue
            if s_id < 1:
                continue
            s_entry = dict(raw_v)
            s_entry["slot_id"] = s_id
            s_entry["active"] = True
            s_entry["base_stale"] = _is_lock_base_stale(s_entry)
            s_entry.setdefault("target_repo_dir", str(lk.get("target_repo_dir") or ""))
            collected[s_id] = s_entry

    if 1 not in collected and (
        not isinstance(raw_slots, dict)
        or not raw_slots
        or str(lk.get("branch_name") or "").strip()
        or str(lk.get("conversation_id") or "").strip()
        or str(lk.get("status") or "").strip()
    ):
        top_slot = {k: v for k, v in lk.items() if k != "slots"}
        try:
            top_sid = int(lk.get("slot_id") or 1)
        except (TypeError, ValueError):
            top_sid = 1
        top_slot["slot_id"] = top_sid
        top_slot["active"] = True
        top_slot["base_stale"] = _is_lock_base_stale(top_slot)
        collected.setdefault(top_sid, top_slot)

    return [collected[k] for k in sorted(collected)]


def _normalize_repo_lock_slots(slug: str, info: dict[str, Any]) -> dict[str, Any]:
    """Normalize an active repository lock with multi-slot occupancy (`1/2`, `2/2`) and `base_stale` flags."""
    if not isinstance(info, dict) or not bool(info.get("active")):
        return dict(info or {})
    norm = dict(info)
    active_slots = _iter_active_lock_slots(norm)
    try:
        raw_max = int(norm.get("max_slots") or 2)
    except (TypeError, ValueError):
        raw_max = 2
    max_slots = max(len(active_slots), raw_max, 2)
    slots_map = {str(s["slot_id"]): s for s in active_slots}
    norm["slots"] = slots_map
    norm["max_slots"] = max_slots
    norm["active_slots_count"] = len(active_slots)
    norm["slot_occupancy"] = f"{len(active_slots)}/{max_slots}"
    primary = slots_map.get("1") or (active_slots[0] if active_slots else norm)
    norm["base_stale"] = _is_lock_base_stale(primary)
    if slug and not norm.get("repo_slug"):
        norm["repo_slug"] = slug
    return norm


def _load_locks(
    state_dir: Path,
    *,
    strict: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load active and historical per-repository `dev_pr_locks` (including `slots`) from state files."""
    active: dict[str, dict[str, Any]] = {}
    all_locks: dict[str, dict[str, Any]] = {}
    for candidate in (state_dir / "dev_pr_lock.json", state_dir / "actions_state.json"):
        data = _load_json(candidate, strict=strict)
        raw_map: Any = None
        if candidate.name == "dev_pr_lock.json":
            raw_map = data.get("dev_pr_locks") or data.get("locks")
        else:
            audit_st = data.get("actions_log_audit_state")
            if isinstance(audit_st, dict):
                raw_map = audit_st.get("dev_pr_locks")
                if not raw_map and isinstance(audit_st.get("dev_pr_lock"), dict):
                    legacy = audit_st["dev_pr_lock"]
                    slug = str(legacy.get("repo_slug") or legacy.get("repo") or "default")
                    raw_map = {slug: legacy}
        if isinstance(raw_map, dict):
            for slug, info in raw_map.items():
                if isinstance(info, dict):
                    slug_key = str(slug)
                    if info.get("active"):
                        norm_info = _normalize_repo_lock_slots(slug_key, info)
                        all_locks[slug_key] = norm_info
                        active[slug_key] = norm_info
                    else:
                        all_locks[slug_key] = dict(info)
                        active.pop(slug_key, None)
    return (active, all_locks)


def is_lock_human_ready(lk: dict[str, Any]) -> bool:
    """Return True iff a Slot #1 lock has `base_stale == False`, `pr_gate_ready == True`, and passed review."""
    if not isinstance(lk, dict):
        return False
    try:
        slot_num = int(lk.get("slot_id") or 1)
    except (TypeError, ValueError):
        slot_num = 1
    if slot_num != 1:
        return False
    if _is_lock_base_stale(lk):
        return False
    status_u = str(lk.get("status") or "").upper()
    if status_u in UNREADY_PR_GATE_LOCK_STATUSES:
        return False
    if status_u not in HUMAN_READY_LOCK_STATUSES:
        return False
    if "pr_gate_ready" in lk and not bool(lk.get("pr_gate_ready")):
        return False
    return True


def _is_network_remote(remote_url: str) -> bool:
    """Return True when `remote_url` is an SSH/HTTPS/Git network remote (not a local path)."""
    cleaned = (remote_url or "").strip()
    if not cleaned or cleaned.startswith(("/", "~", "./", "../")) or cleaned.lower().startswith("file://"):
        return False
    if re.match(r"^[A-Za-z]:[/\\]", cleaned):
        return False
    has_scheme = bool(re.match(r"^(?:https?|ssh|git)://", cleaned, re.IGNORECASE))
    has_scp = bool(re.match(r"^[^@:/\s]+@[^:/\s]+:.+$", cleaned))
    return has_scheme or has_scp


def _parse_remote_to_web_info(remote_url: str) -> tuple[str, str, str]:
    """Parse any Git SSH or HTTPS remote URL into `(provider, web_base, repo_slug)`."""
    raw = (remote_url or "").strip()
    if not _is_network_remote(raw):
        return ("", "", "")
    gc_mod = _get_git_client()
    if gc_mod is not None and hasattr(gc_mod, "detect_vcs_provider_from_remote"):
        try:
            provider, web_base, slug = gc_mod.detect_vcs_provider_from_remote(raw)
            if web_base and slug:
                return (str(provider), str(web_base).rstrip("/"), str(slug))
        except Exception:
            pass
    m_scp = re.match(r"^(?:ssh://)?(?:[^@]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", raw)
    if raw.startswith(("http://", "https://")):
        parsed = urlsplit(raw)
        host = (parsed.hostname or parsed.netloc).lower()
        path_slug = parsed.path.strip("/").removesuffix(".git")
        web_base = f"{parsed.scheme}://{parsed.netloc}/{path_slug}" if path_slug else ""
    elif m_scp:
        host, path_slug = m_scp.group(1).lower(), m_scp.group(2).strip("/")
        web_base = f"https://{host}/{path_slug}"
    else:
        return ("", "", "")
    if "gitlab" in host:
        provider = "gitlab"
    elif "bitbucket" in host:
        provider = "bitbucket"
    elif "gitea" in host or "forgejo" in host:
        provider = "gitea"
    else:
        provider = "github"
    return (provider, web_base.rstrip("/"), path_slug)


def _extract_repo_slug_from_url(url: str) -> str:
    """Extract `owner/repo` (or GitLab group path before `/-/`) from an HTTP(S) VCS URL."""
    parsed = urlsplit((url or "").strip())
    if "/-/" in parsed.path:
        prefix = parsed.path.split("/-/", 1)[0].strip("/")
        if "/" in prefix:
            return prefix
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return ""


def _discover_repos(
    all_locks: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
    repos_dir_override: str = "",
    *,
    home_dir: Path | None = None,
) -> tuple[dict[str, tuple[str, str]], dict[str, Path]]:
    """Dynamically discover managed repositories and local Git checkouts with zero hardcoded names."""
    repo_meta: dict[str, tuple[str, str]] = {}
    repo_dirs: dict[str, Path] = {}
    home_str = str((home_dir or Path.home()).expanduser())

    url_candidates: list[str] = []
    local_dir_candidates: list[Path] = []
    slug_dir_hints: list[tuple[str, Path]] = []

    for lock_slug, lk in all_locks.items():
        for key in ("pr_url", "manual_pr_url", "issue_url"):
            if lk.get(key):
                url_candidates.append(str(lk[key]))
        for rb_key in ("repo_branches", "last_repo_branches"):
            rb = lk.get(rb_key)
            if isinstance(rb, dict):
                for rb_slug, info in rb.items():
                    if isinstance(info, dict):
                        if info.get("manual_pr_url"):
                            url_candidates.append(str(info["manual_pr_url"]))
                        td = str(info.get("target_repo_dir") or "").replace("$HOME", home_str).strip()
                        if td:
                            cand_path = Path(td).expanduser()
                            local_dir_candidates.append(cand_path)
                            if rb_slug:
                                slug_dir_hints.append((str(rb_slug), cand_path))
        tdir = str(lk.get("target_repo_dir") or "").replace("$HOME", home_str).strip()
        if tdir:
            cand_path = Path(tdir).expanduser()
            local_dir_candidates.append(cand_path)
            if lock_slug:
                slug_dir_hints.append((str(lock_slug), cand_path))

    for it in items:
        for raw_u in it.get("pr_urls") or []:
            if raw_u:
                url_candidates.append(str(raw_u))

    for raw_u in url_candidates:
        if not raw_u.startswith(("http://", "https://")):
            continue
        parsed = urlsplit(raw_u)
        slug = _extract_repo_slug_from_url(raw_u)
        if slug:
            host = (parsed.hostname or parsed.netloc).lower()
            provider = "gitlab" if "gitlab" in host else ("bitbucket" if "bitbucket" in host else "github")
            repo_meta.setdefault(slug, (provider, f"{parsed.scheme}://{parsed.netloc}/{slug}"))

    for hinted_slug, hinted_dir in slug_dir_hints:
        if (hinted_dir / ".git").exists():
            with contextlib.suppress(OSError):
                repo_dirs.setdefault(hinted_slug, hinted_dir.resolve())

    scan_parents: set[Path] = set()
    for d in list(local_dir_candidates):
        if d.is_dir():
            scan_parents.add(d.parent)
    scan_root = repos_dir_override or os.environ.get("AGY_DEV_REPOS_DIR", "").strip()
    if scan_root:
        rp = Path(scan_root).expanduser()
        if rp.is_dir():
            scan_parents.add(rp)
    for parent in scan_parents:
        try:
            for child in parent.iterdir():
                if (child / ".git").exists():
                    local_dir_candidates.append(child)
        except OSError:
            pass

    seen_dirs: set[Path] = set()
    for d in local_dir_candidates:
        if d in seen_dirs or not (d / ".git").exists():
            continue
        seen_dirs.add(d)
        try:
            proc = subprocess.run(
                ["git", "-C", str(d), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                provider, web_base, slug = _parse_remote_to_web_info(proc.stdout.strip())
                if slug and web_base:
                    repo_meta[slug] = (provider, web_base)
                    repo_dirs[slug] = d.resolve()
        except Exception:
            pass

    return (repo_meta, repo_dirs)


def _build_repo_discovery_cache_key(
    all_locks: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
    repos_dir_override: str = "",
) -> tuple[Any, ...]:
    """Build a deterministic invalidation key from lock/queue repo slugs, URLs, and checkout hints."""
    lock_entries: list[tuple[str, str, str, str]] = []
    for slug in sorted(all_locks):
        lk = all_locks.get(slug)
        if not isinstance(lk, dict):
            continue
        rb = lk.get("repo_branches")
        rb_dirs = (
            ",".join(
                f"{k}:{v.get('target_repo_dir', '')}:{v.get('manual_pr_url', '')}"
                for k, v in sorted(rb.items())
                if isinstance(v, dict)
            )
            if isinstance(rb, dict)
            else ""
        )
        lock_entries.append(
            (
                str(slug),
                str(lk.get("target_repo_dir") or ""),
                str(lk.get("pr_url") or lk.get("manual_pr_url") or ""),
                rb_dirs,
            )
        )
    item_slugs = tuple(
        sorted(
            {
                str(s)
                for it in items
                for s in (it.get("repo_slugs") or [])
                if s
            }
        )
    )
    item_urls = tuple(
        sorted(
            {
                str(u)
                for it in items
                for u in (it.get("pr_urls") or [])
                if u
            }
        )
    )
    return (
        repos_dir_override.strip(),
        os.environ.get("AGY_DEV_REPOS_DIR", "").strip(),
        tuple(lock_entries),
        item_slugs,
        item_urls,
    )


def _discover_repos_cached(
    all_locks: dict[str, dict[str, Any]],
    items: list[dict[str, Any]],
    repos_dir_override: str = "",
    repo_cache: dict[str, Any] | None = None,
    *,
    ttl_seconds: float = 60.0,
) -> tuple[dict[str, tuple[str, str]], dict[str, Path]]:
    """Return cached `_discover_repos()` results within `ttl_seconds` unless repo hints changed."""
    if repo_cache is None:
        return _discover_repos(all_locks, items, repos_dir_override)
    key = _build_repo_discovery_cache_key(all_locks, items, repos_dir_override)
    eff_ttl = float(repo_cache.get("ttl", ttl_seconds))
    now_mono = time.monotonic()
    cached_res = repo_cache.get("result")
    if (
        cached_res is not None
        and repo_cache.get("key") == key
        and (now_mono - float(repo_cache.get("ts", 0.0))) < eff_ttl
    ):
        return cached_res  # type: ignore[return-value]
    fresh = _discover_repos(all_locks, items, repos_dir_override)
    repo_cache["key"] = key
    repo_cache["ts"] = now_mono
    repo_cache["ttl"] = eff_ttl
    repo_cache["result"] = fresh
    return fresh


def _collect_watched_script_paths() -> list[Path]:
    """Collect resolved Python module paths whose updates should auto-reload `--watch`."""
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

    _add(Path(__file__))
    for mod in (_flow_opt, _queue_doctor, _dev_common, _git_client):
        mod_file = getattr(mod, "__file__", None) if mod is not None else None
        if mod_file:
            _add(mod_file)
    for root in (_SCRIPT_PLUGIN_ROOT, _INSTALLED_PLUGIN_ROOT):
        _add(root / "skills" / "agy-code-remediation-pr" / "scripts" / "dev_flow_optimizer.py")
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
    resolved_script = str((script_path or Path(__file__)).resolve())
    preserved = list(sys.argv[1:] if raw_argv is None else raw_argv)
    return [sys.executable, resolved_script, *preserved]


def _resolve_worktree_gitdir(wt_dir: Path) -> Path | None:
    """Resolve the underlying Git metadata directory for a checkout or `git worktree` pointer."""
    dot_git = wt_dir / ".git"
    with contextlib.suppress(OSError):
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            first_line = dot_git.read_text(encoding="utf-8", errors="ignore").splitlines()
            if first_line and first_line[0].startswith("gitdir:"):
                raw_ptr = first_line[0].split(":", 1)[1].strip()
                if raw_ptr:
                    ptr_path = Path(raw_ptr)
                    return ptr_path if ptr_path.is_absolute() else (wt_dir / ptr_path).resolve()
    return None


def _snapshot_state_fingerprint(
    state_dir: Path,
    locks: dict[str, dict[str, Any]] | None = None,
) -> tuple[tuple[str, int, int, int], ...]:
    """Snapshot `stat()` tuples across queue/lock JSON state and active worktree Git refs/index."""
    candidates: list[Path] = [
        state_dir / "dev_queue.json",
        state_dir / "actions_state.json",
        state_dir / "dev_pr_lock.json",
        state_dir / "dev_queue_board_path.txt",
    ]
    home_str = str(Path.home())
    for _slug, lk in sorted((locks or {}).items()):
        if not isinstance(lk, dict):
            continue
        for slot_entry in _iter_active_lock_slots(lk):
            wt_raw = str(slot_entry.get("worktree_path") or "").replace("$HOME", home_str).strip()
            if not wt_raw:
                continue
            wt_dir = Path(wt_raw).expanduser()
            candidates.append(wt_dir)
            candidates.append(wt_dir / ".git")
            gitdir = _resolve_worktree_gitdir(wt_dir)
            if gitdir is not None:
                candidates.append(gitdir / "HEAD")
                candidates.append(gitdir / "index")

    entries: list[tuple[str, int, int, int]] = []
    for p in candidates:
        try:
            st = p.stat()
            entries.append((str(p), int(st.st_ino), int(st.st_mtime_ns), int(st.st_size)))
        except OSError:
            entries.append((str(p), 0, 0, -1))
    return tuple(entries)


def _format_compact_age(seconds: int) -> str:
    """Format elapsed seconds into a compact human label (`12s`, `4m`, `3h18m`)."""
    if _flow_opt is not None and hasattr(_flow_opt, "format_idle_duration"):
        with contextlib.suppress(Exception):
            return str(_flow_opt.format_idle_duration(max(0, int(seconds))))
    secs = max(0, int(seconds))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    rem_m = mins % 60
    return f"{hours}h{rem_m:02d}m" if rem_m else f"{hours}h"


def _resolve_stall_threshold(threshold_seconds: int | float | None = None) -> int:
    """Resolve stalled-worker/orchestrator threshold in seconds (default `900`)."""
    if _flow_opt is not None and hasattr(_flow_opt, "resolve_stall_threshold_seconds"):
        with contextlib.suppress(Exception):
            return int(_flow_opt.resolve_stall_threshold_seconds(threshold_seconds))
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
    return 900


def _extract_compare_branch_from_url(url: str) -> str:
    """Extract the source branch name from GitHub, GitLab, or Bitbucket compare/create-PR URLs."""
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        return ""
    parsed = urlsplit(raw)
    if "/compare/" in parsed.path:
        tail = parsed.path.split("/compare/", 1)[1].strip("/")
        if "..." in tail:
            return unquote(tail.split("...", 1)[1].strip("/"))
        return unquote(tail)
    qmap = parse_qs(parsed.query)
    for key in ("merge_request[source_branch]", "source"):
        vals = qmap.get(key)
        if vals and vals[0].strip():
            return vals[0].strip()
    return ""


def _check_local_remote_branch_state(
    repo_slug: str,
    branch_name: str,
    repo_local_dirs: dict[str, Path],
) -> str:
    """Validate whether `refs/remotes/origin/<branch>` is open, merged, or missing using local refs only."""
    repo_dir = repo_local_dirs.get(repo_slug)
    br = (branch_name or "").strip()
    if repo_dir is None or not br or not (repo_dir / ".git").exists():
        return "unknown"
    try:
        ref = f"refs/remotes/origin/{br}"
        p_exists = subprocess.run(
            ["git", "-C", str(repo_dir), "show-ref", "--verify", "--quiet", ref],
            capture_output=True,
            timeout=0.5,
            check=False,
        )
        if p_exists.returncode != 0:
            return "missing"

        for base_ref in (
            "refs/remotes/origin/main",
            "refs/remotes/origin/master",
            "refs/heads/main",
            "refs/heads/master",
        ):
            p_base = subprocess.run(
                ["git", "-C", str(repo_dir), "show-ref", "--verify", "--quiet", base_ref],
                capture_output=True,
                timeout=0.5,
                check=False,
            )
            if p_base.returncode != 0:
                continue
            p_anc = subprocess.run(
                ["git", "-C", str(repo_dir), "merge-base", "--is-ancestor", ref, base_ref],
                capture_output=True,
                timeout=0.5,
                check=False,
            )
            if p_anc.returncode == 0:
                return "merged"
            return "open"
        return "open"
    except Exception:
        return "unknown"


def _find_live_unmerged_remote_branch_for_cid(
    repo_slug: str,
    cid8: str,
    repo_local_dirs: dict[str, Path],
) -> str:
    """Find the newest local `origin/fix/*` branch matching `cid8` that is still unmerged."""
    repo_dir = repo_local_dirs.get(repo_slug)
    if repo_dir is None or not cid8 or not (repo_dir / ".git").exists():
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/fix"],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
        if proc.returncode != 0:
            return ""
        matches = [
            ln.strip().removeprefix("origin/")
            for ln in proc.stdout.splitlines()
            if cid8.lower() in ln.lower()
        ]
        for br in reversed(matches):
            if _check_local_remote_branch_state(repo_slug, br, repo_local_dirs) == "open":
                return br
    except Exception:
        pass
    return ""


def _extract_reconciled_count(stdout_text: str) -> int:
    """Parse `reconciled_count` / `reconciled_items` from `pr_remediator.py` JSON stdout."""
    raw = (stdout_text or "").strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return 0
    if not isinstance(payload, dict):
        return 0
    for obj in (payload, payload.get("queue_reconciliation")):
        if not isinstance(obj, dict):
            continue
        raw_cnt = obj.get("reconciled_count")
        if isinstance(raw_cnt, int):
            return raw_cnt
        raw_items = obj.get("reconciled_items")
        if isinstance(raw_items, list):
            return len(raw_items)
    return 0


def _collect_unlocked_pr_repo_slugs(
    items: list[dict[str, Any]],
    locked_slugs: set[str],
) -> set[str]:
    """Identify unmerged repo slugs on `pr_open` or branched `in_progress` items not covered by `locked_slugs`."""
    unlocked: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        st = str(it.get("status") or "").strip().lower()
        has_br_or_pr = bool(it.get("branch_names")) or bool(it.get("pr_urls"))
        if st != "pr_open" and not (st == "in_progress" and has_br_or_pr):
            continue
        merged = {str(r).strip() for r in (it.get("merged_repos") or []) if str(r).strip()}
        candidates = {str(r).strip() for r in (it.get("repo_slugs") or []) if str(r).strip()}
        for raw_u in it.get("pr_urls") or []:
            u_slug = _extract_repo_slug_from_url(str(raw_u or ""))
            if u_slug:
                candidates.add(u_slug)
        for slug in candidates:
            if slug not in merged and slug not in locked_slugs:
                unlocked.add(slug)
    return unlocked


def _run_live_sync(
    state_dir: Path,
    repo_local_dirs: dict[str, Path],
    *,
    offline: bool | None = None,
) -> str:
    """Synchronize remote refs without duplicate fetches and reconcile via `pr_remediator.py`."""
    is_offline = bool(offline) if offline is not None else (os.environ.get("AGY_OFFLINE_ONLY") == "1")
    sub_env = {**os.environ, "AGY_DEV_STATE_DIR": str(state_dir)}
    if is_offline:
        sub_env["AGY_OFFLINE_ONLY"] = "1"

    fetched = 0
    reconciled = 0
    pr_rem = _resolve_pr_remediator_script()
    active_locks, _ = _load_locks(state_dir)
    q_items = _load_json(state_dir / "dev_queue.json").get("items") or []
    locked_slugs = set(active_locks.keys())
    unlocked_pr_slugs = _collect_unlocked_pr_repo_slugs(q_items, locked_slugs)

    if is_offline:
        if pr_rem.is_file():
            try:
                proc_q = subprocess.run(
                    [sys.executable, str(pr_rem), "queue-sync"],
                    env=sub_env,
                    capture_output=True,
                    text=True,
                    timeout=15.0,
                    check=False,
                )
                if proc_q.returncode == 0:
                    reconciled += _extract_reconciled_count(proc_q.stdout)
            except Exception:
                pass
    elif pr_rem.is_file():
        if locked_slugs:
            try:
                proc_lock = subprocess.run(
                    [sys.executable, str(pr_rem), "lock-status", "--all", "--sync-git"],
                    env=sub_env,
                    capture_output=True,
                    text=True,
                    timeout=15.0,
                    check=False,
                )
                if proc_lock.returncode == 0:
                    fetched += len(locked_slugs)
                    reconciled += _extract_reconciled_count(proc_lock.stdout)
            except Exception:
                pass
            for u_slug in sorted(unlocked_pr_slugs):
                try:
                    proc_u = subprocess.run(
                        [sys.executable, str(pr_rem), "queue-sync", "--repo", u_slug, "--fetch"],
                        env=sub_env,
                        capture_output=True,
                        text=True,
                        timeout=15.0,
                        check=False,
                    )
                    if proc_u.returncode == 0:
                        fetched += 1
                        reconciled += _extract_reconciled_count(proc_u.stdout)
                except Exception:
                    pass
        else:
            try:
                proc_q = subprocess.run(
                    [sys.executable, str(pr_rem), "queue-sync", "--fetch"],
                    env=sub_env,
                    capture_output=True,
                    text=True,
                    timeout=15.0,
                    check=False,
                )
                if proc_q.returncode == 0:
                    fetched += len(unlocked_pr_slugs)
                    reconciled += _extract_reconciled_count(proc_q.stdout)
            except Exception:
                pass
            if not unlocked_pr_slugs:
                for _slug, rdir in sorted(repo_local_dirs.items()):
                    try:
                        proc_f = subprocess.run(
                            ["git", "-C", str(rdir), "fetch", "--prune", "origin"],
                            capture_output=True,
                            text=True,
                            timeout=6.0,
                            check=False,
                        )
                        if proc_f.returncode == 0:
                            fetched += 1
                    except Exception:
                        pass
    else:
        for _slug, rdir in sorted(repo_local_dirs.items()):
            try:
                proc_f = subprocess.run(
                    ["git", "-C", str(rdir), "fetch", "--prune", "origin"],
                    capture_output=True,
                    text=True,
                    timeout=6.0,
                    check=False,
                )
                if proc_f.returncode == 0:
                    fetched += 1
            except Exception:
                pass

    mode_lbl = "Offline sync" if is_offline else "Sync"
    return f"↻ {mode_lbl} OK ({fetched} repos fetched, {reconciled} PRs reconciled)"


class _AsyncSyncController:
    """Thread-safe single-flight background Git sync controller for `run_watch_loop()`."""

    def __init__(self, sync_fn: Callable[[Path, dict[str, Path]], str] | None = None) -> None:
        self._lock = threading.Lock()
        self._sync_fn = sync_fn
        self._in_flight = False
        self._last_started_mono = 0.0
        self._last_completed_mono = 0.0
        self._last_result_msg = ""
        self._has_unconsumed = False

    def is_in_flight(self) -> bool:
        with self._lock:
            return self._in_flight

    def has_unconsumed_result(self) -> bool:
        with self._lock:
            return self._has_unconsumed

    def last_started_mono(self) -> float:
        with self._lock:
            return self._last_started_mono

    def last_completed_mono(self) -> float:
        with self._lock:
            return self._last_completed_mono

    def consume_result(self) -> str | None:
        with self._lock:
            if not self._has_unconsumed:
                return None
            self._has_unconsumed = False
            return self._last_result_msg

    def format_badge(self, now_mono: float | None = None) -> str:
        with self._lock:
            if self._in_flight:
                return "sync:syncing…"
            if self._last_completed_mono > 0.0:
                cur = now_mono if now_mono is not None else time.monotonic()
                return f"sync:{_format_compact_age(max(0, int(cur - self._last_completed_mono)))}"
            return ""

    def trigger(self, state_dir: Path, repo_local_dirs: dict[str, Path]) -> bool:
        with self._lock:
            if self._in_flight:
                return False
            self._in_flight = True
            self._last_started_mono = time.monotonic()
        worker = threading.Thread(
            target=self._run_worker,
            args=(state_dir, dict(repo_local_dirs)),
            daemon=True,
        )
        worker.start()
        return True

    def _run_worker(self, state_dir: Path, repo_local_dirs: dict[str, Path]) -> None:
        try:
            fn = self._sync_fn if self._sync_fn is not None else _run_live_sync
            msg = fn(state_dir, repo_local_dirs)
        except Exception as exc:
            msg = f"⚠ Sync error: {exc.__class__.__name__}"
        with self._lock:
            self._in_flight = False
            self._last_completed_mono = time.monotonic()
            self._last_result_msg = msg
            self._has_unconsumed = True


def _inspect_worktree_progress(worktree_raw: str) -> tuple[bool, int]:
    """Inspect a local worktree for uncommitted changes and commits ahead of base branch."""
    expanded = (worktree_raw or "").replace("$HOME", str(Path.home())).strip()
    if not expanded:
        return (False, 0)
    wt = Path(expanded).expanduser()
    if not wt.is_dir():
        return (False, 0)
    has_dirty = False
    commits_ahead = 0
    try:
        p1 = subprocess.run(
            ["git", "-C", str(wt), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
        if p1.returncode == 0 and p1.stdout.strip():
            has_dirty = True
    except Exception:
        pass
    for base_ref in ("origin/main", "main", "origin/master", "master"):
        try:
            p2 = subprocess.run(
                ["git", "-C", str(wt), "rev-list", "--count", f"{base_ref}..HEAD"],
                capture_output=True,
                text=True,
                timeout=0.5,
                check=False,
            )
            if p2.returncode == 0 and p2.stdout.strip().isdigit():
                commits_ahead = int(p2.stdout.strip())
                break
        except Exception:
            pass
    return (has_dirty, commits_ahead)


def _detect_stalled_lock_info(
    lk: dict[str, Any],
    *,
    now_ts: float | None = None,
    threshold_seconds: int | float | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
) -> dict[str, Any]:
    """Delegate stalled-worker inspection to `dev_flow_optimizer.detect_stalled_lock` safely."""
    if _flow_opt is not None and hasattr(_flow_opt, "detect_stalled_lock"):
        try:
            return dict(
                _flow_opt.detect_stalled_lock(
                    lk,
                    now_ts=now_ts,
                    threshold_seconds=threshold_seconds,
                    brain_dir=brain_dir,
                )
            )
        except Exception:
            pass
    return {
        "stalled": False,
        "idle_seconds": 0,
        "idle_label": "0s",
        "elapsed_seconds": 0,
        "elapsed_label": "0s",
        "threshold_seconds": 900,
        "last_activity_ts": 0.0,
        "last_activity_source": "",
        "worker_cid": str(lk.get("worker_cid") or "").strip(),
        "reviewer_subagent_id": str(lk.get("reviewer_subagent_id") or "").strip(),
    }


def _slot_progress_cache_key(slug: str, lk: dict[str, Any]) -> str:
    """Build a slot-isolated cache key so Slot #1 and Slot #2 never share progress entries."""
    try:
        s_num = int(lk.get("slot_id") or 1)
    except (TypeError, ValueError):
        s_num = 1
    return f"{slug}#slot{s_num}" if s_num > 1 else slug


def _compute_lock_progress(
    lk: dict[str, Any],
    *,
    now_ts: float | None = None,
    threshold_seconds: int | float | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
) -> dict[str, object]:
    """Compute per-lock progress percentage, live inactivity semaphore tag, and PR readiness flags."""
    st_u = str(lk.get("status") or "DEVELOPING").upper()
    branch_pushed = bool(lk.get("branch_pushed"))
    has_pr = bool(lk.get("pr_url"))

    is_human_ready = is_lock_human_ready(lk)
    is_awaiting_base = st_u == "READY_AWAITING_BASE_MERGE"
    is_needs_rebase = st_u == "NEEDS_REBASE_REVERIFY" or _is_lock_base_stale(lk)
    is_escalated = st_u == "ESCALATED_MAX_REVIEW_ITERATIONS"
    is_iterating = st_u in ("NEEDS_REVISION_ITERATING", "NEEDS_AGENT_SEMANTIC_FIX", "NEEDS_AGENT_CONFLICT_RESOLUTION")
    is_agent_review = (not is_human_ready and not is_escalated and not is_awaiting_base and not is_needs_rebase) and (
        st_u in AGENT_REVIEW_LOCK_STATUSES or branch_pushed or has_pr
    )

    stall_info = _detect_stalled_lock_info(
        lk,
        now_ts=now_ts,
        threshold_seconds=threshold_seconds,
        brain_dir=brain_dir,
    )
    is_stalled = bool(stall_info.get("stalled"))
    idle_seconds = int(stall_info.get("idle_seconds") or 0)
    idle_label = str(stall_info.get("idle_label") or "0s")
    elapsed_seconds = int(stall_info.get("elapsed_seconds") or 0)
    elapsed_label = str(stall_info.get("elapsed_label") or "0s")
    last_activity_ts = float(stall_info.get("last_activity_ts") or 0.0)
    last_activity_source = str(stall_info.get("last_activity_source") or "")
    stage_label = str(lk.get("stage_label") or "").strip()
    worker_cid = str(stall_info.get("worker_cid") or lk.get("worker_cid") or "").strip()
    reviewer_subagent_id = str(stall_info.get("reviewer_subagent_id") or lk.get("reviewer_subagent_id") or "").strip()

    explicit_pct = lk.get("progress_pct")
    idle_state = ""
    if is_stalled:
        pct = 0
        tag = f"STALL {idle_label}".strip()
        idle_state = "stall"
    elif is_human_ready:
        pct, tag = 90, "PR"
    elif is_awaiting_base:
        pct, tag = 85, "await"
    elif is_needs_rebase:
        pct, tag = 70, "rebase"
    elif is_escalated:
        pct, tag = 0, "esc"
    else:
        if isinstance(explicit_pct, (int, float)):
            pct = max(0, min(100, int(round(explicit_pct))))
        elif is_agent_review:
            pct = 75
        elif is_iterating:
            pct = 65
        else:
            has_dirty, commits_ahead = _inspect_worktree_progress(str(lk.get("worktree_path") or ""))
            if commits_ahead > 0 and not has_dirty:
                pct = 60
            elif commits_ahead > 0 and has_dirty:
                pct = 50
            elif has_dirty:
                pct = 35
            else:
                pct = 15

        if idle_seconds >= IDLE_AMBER_THRESHOLD_SECONDS:
            tag = f"idle {idle_label}"
            idle_state = "amber"
        elif last_activity_ts > 0.0:
            tag = f"ok·{idle_label or '0s'}"
            idle_state = "ok"
        elif is_agent_review:
            tag = "rev"
            idle_state = "ok"
        elif is_iterating:
            tag = "iter"
            idle_state = "ok"
        else:
            tag = (stage_label[:8] if stage_label else "ok·0s")
            idle_state = "ok"

    return {
        "pct": pct,
        "tag": tag,
        "idle_state": idle_state,
        "is_human_ready": is_human_ready,
        "is_awaiting_base": is_awaiting_base,
        "is_needs_rebase": is_needs_rebase,
        "is_agent_review": is_agent_review,
        "is_escalated": is_escalated,
        "is_stalled": is_stalled,
        "idle_seconds": idle_seconds,
        "idle_label": idle_label,
        "elapsed_seconds": elapsed_seconds,
        "elapsed_label": elapsed_label,
        "last_activity_source": last_activity_source,
        "stage_label": stage_label,
        "worker_cid": worker_cid,
        "reviewer_subagent_id": reviewer_subagent_id,
    }


def _compute_item_progress(
    it: dict[str, Any],
    locks_by_cid: dict[str, list[tuple[str, dict[str, Any]]]],
    lock_progress_cache: dict[str, dict[str, object]],
    link_state: str = "",
    is_human_pr: bool = False,
    *,
    now_ts: float | None = None,
) -> dict[str, object] | None:
    """Compute item progress, rendering full PR-ready bars, dim `1/2 merged` partial WIP, and live idle semaphore."""
    st = str(it.get("status") or "pending").lower()
    repo_slugs = [str(r) for r in (it.get("repo_slugs") or []) if r]
    merged_set = {str(r) for r in (it.get("merged_repos") or []) if r}
    cid = str(it.get("conversation_id") or "")
    linked_locks = locks_by_cid.get(cid, [])

    if st in INACTIVE_STATUSES:
        return None
    if (
        st not in ("in_progress", "pr_open", "ready_awaiting_base_merge", "needs_rebase_reverify")
        and not linked_locks
        and not merged_set
    ):
        return None
    if link_state in ("merged", "stale"):
        return {
            "pct": 100 if link_state == "merged" else 0,
            "tag": "sync!" if link_state == "merged" else "stale",
            "mode": "stale",
        }

    total_repos = max(1, len(repo_slugs))
    done_repos = len(merged_set)
    per_repo_pct: dict[str, int] = {slug: (100 if slug in merged_set else 0) for slug in repo_slugs}
    tags: list[str] = []
    any_lock_human_ready = False
    any_lock_stalled = False
    any_lock_amber = False
    any_lock_ok = False
    stalled_tag = ""
    stalled_idle_label = ""

    for slug, lk in linked_locks:
        p_key = _slot_progress_cache_key(slug, lk)
        lp = lock_progress_cache.get(p_key)
        if lp is None:
            lp = _compute_lock_progress(lk, now_ts=now_ts)
            lock_progress_cache[p_key] = lp
        if slug not in merged_set:
            per_repo_pct[slug] = int(lp["pct"])  # type: ignore[arg-type]
            if lp.get("tag"):
                tags.append(str(lp["tag"]))
            if lp.get("is_human_ready"):
                any_lock_human_ready = True
            if lp.get("is_stalled"):
                any_lock_stalled = True
                stalled_tag = str(lp.get("tag") or "STALL")
                stalled_idle_label = str(lp.get("idle_label") or "")
            elif lp.get("idle_state") == "amber":
                any_lock_amber = True
            elif lp.get("idle_state") == "ok":
                any_lock_ok = True

    if is_human_pr or any_lock_human_ready:
        active_idx = min(total_repos, done_repos + 1)
        is_final_repo = (done_repos + 1) >= total_repos
        return {
            "pct": 100,
            "tag": f"PR {active_idx}/{total_repos}" if total_repos > 1 else "PR ready",
            "mode": "pr_final" if is_final_repo else "pr_part",
            "part_idx": active_idx,
            "total_repos": total_repos,
        }

    if any_lock_stalled:
        return {
            "pct": 0,
            "tag": stalled_tag or f"STALL {stalled_idle_label}".strip(),
            "mode": "stalled",
            "idle_state": "stall",
            "is_stalled": True,
            "idle_label": stalled_idle_label,
        }

    avg_pct = int(round(sum(per_repo_pct.values()) / max(1, len(per_repo_pct)))) if per_repo_pct else 0
    if not linked_locks and 0 < done_repos < total_repos and st not in ("in_progress", "pr_open"):
        return {
            "pct": max(0, min(100, avg_pct)),
            "tag": f"{done_repos}/{total_repos} merged",
            "mode": "partial_pending",
            "done_repos": done_repos,
            "total_repos": total_repos,
        }

    if not linked_locks:
        if st == "pr_open":
            for slug in repo_slugs:
                if slug not in merged_set:
                    per_repo_pct[slug] = 90
            tags.append("PR")
        elif st == "in_progress":
            for slug in repo_slugs:
                if slug not in merged_set:
                    per_repo_pct[slug] = 15
            item_ts = 0.0
            if _flow_opt is not None and hasattr(_flow_opt, "parse_iso_timestamp"):
                item_ts = float(
                    _flow_opt.parse_iso_timestamp(it.get("updated_at") or it.get("added_at"))
                )
            if item_ts > 0.0:
                cur_t = float(now_ts if now_ts is not None else time.time())
                i_secs = max(0, int(cur_t - item_ts))
                i_lbl = _format_compact_age(i_secs)
                if i_secs >= IDLE_AMBER_THRESHOLD_SECONDS:
                    tags.append(f"idle {i_lbl}")
                    any_lock_amber = True
                else:
                    tags.append(f"ok·{i_lbl}")
                    any_lock_ok = True
            else:
                tags.append("ok·0s")
                any_lock_ok = True
        avg_pct = int(round(sum(per_repo_pct.values()) / max(1, len(per_repo_pct)))) if per_repo_pct else 0

    base_tag = tags[0] if tags else ""
    if total_repos > 1:
        active_idx = min(total_repos, done_repos + (1 if linked_locks or st in ("in_progress", "pr_open") else 0))
        if not linked_locks and done_repos > 0 and st == "pending":
            tag = f"{done_repos}/{total_repos} merged"
        elif base_tag:
            tag = f"{active_idx}/{total_repos}·{base_tag}"
        else:
            tag = f"{active_idx}/{total_repos}"
    else:
        tag = base_tag

    eff_idle_state = "amber" if any_lock_amber else ("ok" if any_lock_ok else "")
    return {"pct": max(0, min(100, avg_pct)), "tag": tag, "mode": "dev", "idle_state": eff_idle_state}


def _render_inline_progress(progress: dict[str, object] | None, col_w: int, color_on: bool) -> str:
    """Render an inline progress bar cell with PR-ready bars, dim partial WIP, and live idle/stall semaphore."""
    if progress is None:
        return _pad_visible(_color("—", "2", color_on), col_w)
    pct = max(0, min(100, int(progress.get("pct") or 0)))  # type: ignore[arg-type]
    tag = str(progress.get("tag") or "").strip()
    mode = str(progress.get("mode") or "")
    idle_state = str(progress.get("idle_state") or "")
    bar_cells_default = 6 if col_w < 22 else 8

    if mode == "stalled":
        lbl = tag or "STALL"
        stall_cells = 4 if (col_w - bar_cells_default - 1) < _visible_len(lbl) and col_w >= 12 else bar_cells_default
        bar = _color("▓" * stall_cells, "1;31", color_on)
        max_lbl = max(4, col_w - stall_cells - 1)
        lbl_clean = _trunc(lbl, max_lbl)
        return _pad_visible(f"{bar} {_color(lbl_clean, '1;35', color_on)}", col_w)

    if mode in ("pr_part", "pr_final"):
        bar_code = "1;32" if mode == "pr_final" else "1;33"
        lbl = tag or "PR ready"
        if "ready" not in lbl:
            cand_ready = f"{lbl} ready"
            pr_cells = 6 if (col_w - bar_cells_default - 1) < _visible_len(cand_ready) else bar_cells_default
            if _visible_len(cand_ready) <= max(3, col_w - pr_cells - 1):
                lbl = cand_ready
                bar_cells_pr = pr_cells
            else:
                bar_cells_pr = bar_cells_default
        else:
            bar_cells_pr = 6 if (col_w - bar_cells_default - 1) < _visible_len(lbl) else bar_cells_default
        bar = _color("█" * bar_cells_pr, bar_code, color_on)
        max_lbl = max(3, col_w - bar_cells_pr - 1)
        lbl_clean = _trunc(lbl, max_lbl)
        return _pad_visible(f"{bar} {_color(lbl_clean, bar_code, color_on)}", col_w)

    if mode == "partial_pending":
        base_lbl = tag or "1/2 merged"
        with_pct = f"{base_lbl} ({pct}%)"
        lbl_clean = with_pct if _visible_len(with_pct) <= col_w else _trunc(base_lbl, col_w)
        return _pad_visible(_color(lbl_clean, "2", color_on), col_w)

    is_amber_idle = idle_state == "amber" or "idle " in tag
    is_ok_idle = idle_state == "ok" or tag.startswith("ok·") or "·ok·" in tag
    tag_vis = _visible_len(tag)
    if tag_vis > 0:
        bar_cells = max(3, min(bar_cells_default, col_w - 6 - tag_vis))
    else:
        bar_cells = bar_cells_default

    filled = int(round((pct / 100.0) * bar_cells))
    empty = max(0, bar_cells - filled)
    if tag in ("stale", "sync!"):
        bar_code = "1;35"
    elif is_amber_idle:
        bar_code = "1;33"
    elif pct >= 85:
        bar_code = "1;32"
    elif pct >= 50:
        bar_code = "1;33"
    else:
        bar_code = "1;36"
    bar = _color("█" * filled, bar_code, color_on) + _color("░" * empty, "2", color_on)
    pct_txt = _color(f"{pct:>3}%", bar_code, color_on)
    max_tag = max(0, col_w - bar_cells - 6)
    tag_clean = _trunc(tag, max_tag) if max_tag >= 3 else ""
    if tag_clean:
        if tag in ("stale", "sync!"):
            tag_code = "1;35"
        elif is_amber_idle:
            tag_code = "1;33"
        elif is_ok_idle:
            tag_code = "0;32"
        else:
            tag_code = "2"
        tag_txt = f" {_color(tag_clean, tag_code, color_on)}"
    else:
        tag_txt = ""
    return _pad_visible(f"{bar} {pct_txt}{tag_txt}", col_w)


def _build_repo_labels(all_slugs: set[str]) -> dict[str, str]:
    """Generate short unambiguous display labels for repository slugs."""
    by_base: dict[str, list[str]] = {}
    for slug in sorted(all_slugs):
        base = slug.split("/")[-1]
        by_base.setdefault(base, []).append(slug)
    labels: dict[str, str] = {}
    for base, slugs in by_base.items():
        if len(slugs) == 1:
            labels[slugs[0]] = base
        else:
            for s in slugs:
                labels[s] = s
    return labels


def _compact_repos(
    repo_slugs: list[str],
    label_map: dict[str, str],
    merged_repos: set[str],
    max_w: int,
    pr_repo_slug: str = "",
) -> str:
    """Format compact repository list, highlighting PR-ready (`★`) or remaining partial-WIP (`(k/n✓)`) repos."""

    def _fmt(slug: str) -> str:
        name = label_map.get(slug, slug.split("/")[-1])
        if slug in merged_repos:
            return f"{name}✓"
        if pr_repo_slug and slug == pr_repo_slug:
            return f"{name}★"
        return name

    ordered = [str(s) for s in (repo_slugs or []) if s]
    if not ordered:
        return "-"

    merged_norm = {str(m).strip().lower() for m in (merged_repos or set()) if str(m).strip()}
    done_slugs = [s for s in ordered if s in merged_repos or s.lower() in merged_norm]
    pending_slugs = [s for s in ordered if s not in merged_repos and s.lower() not in merged_norm]

    # Partially-merged rows without an active human PR explicitly highlight remaining pending repo(s) + (done/total✓)
    if not (pr_repo_slug and pr_repo_slug in ordered) and 0 < len(done_slugs) < len(ordered):
        badge = f"({len(done_slugs)}/{len(ordered)}✓)"
        rem_names = [label_map.get(s, s.split("/")[-1]) for s in pending_slugs]
        full_partial = f"{', '.join(rem_names)} {badge}"
        if _visible_len(full_partial) <= max_w:
            return full_partial
        if len(rem_names) > 1:
            multi_suffix = f" +{len(rem_names) - 1} {badge}"
            if max_w - _visible_len(multi_suffix) >= 5:
                return f"{_trunc(rem_names[0], max_w - _visible_len(multi_suffix))}{multi_suffix}"
        suffix = f" {badge}"
        if max_w - _visible_len(suffix) >= 4:
            return f"{_trunc(rem_names[0], max_w - _visible_len(suffix))}{suffix}"
        return _trunc(full_partial, max_w)

    parts = [_fmt(s) for s in ordered]
    full = ", ".join(parts)
    if _visible_len(full) <= max_w:
        return full
    if len(parts) > 1:
        suffix = f" +{len(parts) - 1}"
        primary = parts[0]
        if pr_repo_slug and pr_repo_slug in ordered:
            primary = _fmt(pr_repo_slug)
        first = _trunc(primary, max(6, max_w - _visible_len(suffix)))
        return f"{first}{suffix}"
    return _trunc(parts[0], max_w)


def _build_display_ids(items: list[dict[str, Any]]) -> dict[str, str]:
    """Generate collision-safe short display IDs for queue rows."""
    prefix_counts: dict[str, int] = {}
    for it in items:
        cid = str(it.get("conversation_id") or "")
        p8 = cid[:8]
        prefix_counts[p8] = prefix_counts.get(p8, 0) + 1
    out: dict[str, str] = {}
    used: set[str] = set()
    for it in items:
        cid = str(it.get("conversation_id") or "")
        p8 = cid[:8]
        if prefix_counts.get(p8, 0) <= 1:
            cand = p8
        else:
            tail_parts = [
                t for t in cid.split("-")[1:] if t and not re.fullmatch(r"[0-9a-f]{4,12}", t, re.IGNORECASE)
            ]
            if tail_parts:
                cand = f"{cid[:4]}·{tail_parts[-1][:3]}"
                if cand in used:
                    cand = f"{cid[:4]}·{tail_parts[0][:3]}"
            else:
                cand = p8
        used.add(cand)
        out[cid] = cand
    return out


def _url_has_pr_body(url: str) -> bool:
    """Return True when a PR/MR compare URL embeds a pre-populated body or description parameter."""
    parsed = urlsplit((url or "").strip())
    q = parsed.query
    return bool(
        q
        and (
            "body=" in q
            or "description=" in q
            or "description%5D=" in q
            or "description]=" in q
        )
    )


def _is_created_pr_url(url: str) -> bool:
    """Return True only for already-created PR/MR URLs (e.g. `/pull/42`, `/-/merge_requests/17`)."""
    parsed = urlsplit((url or "").strip())
    return bool(_CREATED_PR_PATH_RE.search(parsed.path or ""))


def _score_pr_url_richness(url: str, order_idx: int = 0) -> tuple[int, int, int, int, int]:
    """Rank PR URLs so created `/pull/<n>` and verified `?title=...&body=...` URLs beat bare `?expand=1`."""
    clean = _sanitize_full_url(url)
    if not clean:
        return (-1, -1, -1, -1, -1)
    parsed = urlsplit(clean)
    is_created_pr = 1 if _is_created_pr_url(clean) else 0
    q = parsed.query or ""
    has_body = 1 if _url_has_pr_body(clean) else 0
    has_title = 1 if ("title=" in q or "title%5D=" in q or "title]=" in q) else 0
    verdict_rank = 0
    if has_body:
        decoded_q = unquote(q)
        if "Pending independent evaluation" in decoded_q:
            verdict_rank = 1
        elif any(
            marker in decoded_q
            for marker in ("PASS", "Verdict", "Automated Verification", "Independent PR Review")
        ):
            verdict_rank = 3
        else:
            verdict_rank = 2
    return (is_created_pr, verdict_rank, has_body, has_title, order_idx)


def _repo_pulls_url(slug: str, repo_web_bases: dict[str, tuple[str, str]]) -> str:
    if slug in repo_web_bases:
        provider, web_base = repo_web_bases[slug]
        if provider == "gitlab":
            return f"{web_base}/-/merge_requests"
        if provider == "bitbucket":
            return f"{web_base}/pull-requests"
        return f"{web_base}/pulls"
    return ""


def _repo_compare_url(slug: str, branch: str, repo_web_bases: dict[str, tuple[str, str]]) -> str:
    if not branch or slug not in repo_web_bases:
        return ""
    provider, web_base = repo_web_bases[slug]
    if provider == "gitlab":
        return f"{web_base}/-/merge_requests/new?merge_request[source_branch]={branch}"
    if provider == "bitbucket":
        return f"{web_base}/pull-requests/new?source={branch}"
    return f"{web_base}/compare/main...{branch}?expand=1"


def _resolve_lock_human_pr_link(
    slug: str,
    lk: dict[str, Any],
    repo_web_bases: dict[str, tuple[str, str]],
    repo_local_dirs: dict[str, Path],
    extra_urls: list[str] | None = None,
) -> tuple[str, str, bool, str]:
    """Resolve the richest validated human PR URL for an active lock, honoring `pr_gate_ready`."""
    if not is_lock_human_ready(lk):
        return ("", _repo_pulls_url(slug, repo_web_bases), False, "open")

    raw_candidates: list[str] = []
    for key in ("pr_url", "manual_pr_url", "issue_url"):
        val = _sanitize_full_url(str(lk.get(key) or ""))
        if val:
            raw_candidates.append(val)
    rb = lk.get("repo_branches")
    if isinstance(rb, dict) and isinstance(rb.get(slug), dict):
        rb_u = _sanitize_full_url(str(rb[slug].get("manual_pr_url") or ""))
        if rb_u:
            raw_candidates.append(rb_u)

    lock_br = str(lk.get("branch_name") or "").strip()
    for eu in extra_urls or []:
        clean_eu = _sanitize_full_url(str(eu or ""))
        if not clean_eu:
            continue
        eu_slug = _extract_repo_slug_from_url(clean_eu)
        if eu_slug and eu_slug != slug:
            continue
        eu_br = _extract_compare_branch_from_url(clean_eu)
        if lock_br and eu_br and eu_br != lock_br:
            continue
        raw_candidates.append(clean_eu)

    created_prs = [u for u in raw_candidates if _is_created_pr_url(u)]
    if created_prs:
        best_pr = max(enumerate(created_prs), key=lambda pair: _score_pr_url_richness(pair[1], pair[0]))[1]
        return ("PR", best_pr, True, "open")

    best_manual = ""
    if raw_candidates:
        best_manual = max(enumerate(raw_candidates), key=lambda pair: _score_pr_url_richness(pair[1], pair[0]))[1]

    br = lock_br or _extract_compare_branch_from_url(best_manual)
    gstate = _check_local_remote_branch_state(slug, br, repo_local_dirs) if br else "unknown"
    if gstate in ("open", "unknown"):
        if best_manual:
            return ("PR", best_manual, True, gstate)
        cmp_u = _repo_compare_url(slug, br, repo_web_bases)
        if cmp_u:
            return ("PR", cmp_u, True, gstate)
    if gstate == "merged":
        return ("Sync", _repo_pulls_url(slug, repo_web_bases), False, "merged")
    return ("Stale", _repo_pulls_url(slug, repo_web_bases), False, "stale")


def _resolve_item_human_pr_link(
    it: dict[str, Any],
    locks_by_cid: dict[str, list[tuple[str, dict[str, Any]]]],
    repo_web_bases: dict[str, tuple[str, str]],
    repo_local_dirs: dict[str, Path],
) -> tuple[str, str, bool, str, str]:
    """Resolve the best validated human-ready PR URL and owning repo slug for a queue item."""
    if _is_lock_base_stale(it) or ("pr_gate_ready" in it and not bool(it.get("pr_gate_ready"))):
        return ("", "", False, "", "")

    st = str(it.get("status") or "pending").lower()
    if st in ("ready_awaiting_base_merge", "needs_rebase_reverify", "needs_agent_semantic_fix", "needs_agent_conflict_resolution"):
        return ("", "", False, "", "")

    merged_set = {str(r) for r in (it.get("merged_repos") or []) if r}
    repo_slugs = [str(r) for r in (it.get("repo_slugs") or []) if r]
    unmerged = [r for r in repo_slugs if r not in merged_set]
    cid = str(it.get("conversation_id") or "")
    cid8 = cid[:8]
    item_pr_urls = [str(raw_u or "").strip() for raw_u in (it.get("pr_urls") or []) if str(raw_u or "").strip()]

    unready_locked_slugs: set[str] = set()
    for slug, lk in locks_by_cid.get(cid, []):
        if slug in merged_set:
            continue
        if not is_lock_human_ready(lk):
            unready_locked_slugs.add(slug)
            continue
        kind, u, is_pr, gstate = _resolve_lock_human_pr_link(
            slug,
            lk,
            repo_web_bases,
            repo_local_dirs,
            extra_urls=item_pr_urls,
        )
        if is_pr and u:
            return (kind, u, True, gstate, slug)
        if gstate in ("merged", "stale") and kind:
            return (kind, u, False, gstate, slug)

    if st == "pr_open":
        saw_merged = False
        saw_missing = False
        open_candidates: list[tuple[tuple[int, int, int, int, int], str, str, str]] = []
        for idx_u, raw_u in enumerate(item_pr_urls):
            u = _sanitize_full_url(raw_u)
            if not u:
                continue
            slug = _extract_repo_slug_from_url(u)
            if slug and (slug in merged_set or slug in unready_locked_slugs):
                continue
            if not slug and unmerged and all(r in unready_locked_slugs for r in unmerged):
                continue
            if _is_created_pr_url(u):
                fallback_slug = next((r for r in unmerged if r not in unready_locked_slugs), "")
                if not slug and not fallback_slug and unready_locked_slugs:
                    continue
                open_candidates.append((_score_pr_url_richness(u, idx_u), u, "open", slug or fallback_slug))
                continue
            br = _extract_compare_branch_from_url(u)
            if slug and br:
                gstate = _check_local_remote_branch_state(slug, br, repo_local_dirs)
                if gstate in ("open", "unknown"):
                    open_candidates.append((_score_pr_url_richness(u, idx_u), u, gstate, slug))
                elif gstate == "merged":
                    saw_merged = True
                elif gstate == "missing":
                    saw_missing = True

        if open_candidates:
            open_candidates.sort(key=lambda entry: entry[0], reverse=True)
            _, best_u, best_gstate, best_slug = open_candidates[0]
            return ("PR", best_u, True, best_gstate, best_slug)

        eligible_unmerged = [r for r in unmerged if r not in unready_locked_slugs]
        for slug in eligible_unmerged:
            live_br = _find_live_unmerged_remote_branch_for_cid(slug, cid8, repo_local_dirs)
            if live_br:
                cmp_u = _repo_compare_url(slug, live_br, repo_web_bases)
                if cmp_u:
                    return ("PR", cmp_u, True, "open", slug)

        target_slug = eligible_unmerged[0] if eligible_unmerged else ""
        pu = _repo_pulls_url(target_slug, repo_web_bases) if target_slug else ""
        if saw_merged:
            return ("Sync", pu, False, "merged", target_slug)
        if saw_missing or pu:
            return ("Stale", pu, False, "stale", target_slug)

    return ("", "", False, "", "")


_STATUS_STYLES: dict[str, tuple[str, str]] = {
    "pr_open": ("1;33", "PR_OPEN"),
    "ready_awaiting_base_merge": ("1;34", "READY_AWAITING_BASE_MERGE"),
    "needs_rebase_reverify": ("1;35", "NEEDS_REBASE_REVERIFY"),
    "needs_agent_semantic_fix": ("1;31", "NEEDS_AGENT_SEMANTIC_FIX"),
    "needs_agent_conflict_resolution": ("1;31", "NEEDS_AGENT_CONFLICT_RESOLUTION"),
    "in_progress": ("1;36", "IN_PROGRESS"),
    "pending": ("0;37", "PENDING"),
    "hitl_blocked": ("1;35", "HITL_BLOCK"),
    "standby": ("2;37", "STANDBY"),
    "merged": ("0;32", "MERGED"),
    "cleaned": ("2;32", "CLEANED"),
    "cancelled": ("2;31", "CANCELLED"),
    "rejected": ("0;31", "REJECTED"),
}


def _status_badge(status: str, color_on: bool, width: int = 11) -> str:
    norm_key = str(status or "").strip().lower()
    code, label = _STATUS_STYLES.get(norm_key, ("0", str(status or "").strip().upper()))
    eff_w = max(11, int(width))
    return _color(f"{label:<{eff_w}}", code, color_on)


def _resolve_effective_item_status(
    it: dict[str, Any],
    locks_by_cid: dict[str, list[tuple[str, dict[str, Any]]]],
) -> str:
    """Resolve effective display status for a queue row, surfacing `READY_AWAITING_BASE_MERGE` & `NEEDS_REBASE_REVERIFY`."""
    raw_st = str(it.get("status") or "pending").strip().lower()
    if raw_st in INACTIVE_STATUSES:
        return raw_st
    if raw_st in (
        "ready_awaiting_base_merge",
        "needs_rebase_reverify",
        "needs_agent_semantic_fix",
        "needs_agent_conflict_resolution",
    ):
        return raw_st
    if _is_lock_base_stale(it):
        return "needs_rebase_reverify"

    cid = str(it.get("conversation_id") or "")
    merged_set = {str(r) for r in (it.get("merged_repos") or []) if r}
    unmerged_locks = [(s, lk) for s, lk in locks_by_cid.get(cid, []) if s not in merged_set]
    if unmerged_locks:
        for _slug, lk in unmerged_locks:
            lk_st = str(lk.get("status") or "").strip().upper()
            if lk_st == "READY_AWAITING_BASE_MERGE":
                return "ready_awaiting_base_merge"
            if lk_st == "NEEDS_REBASE_REVERIFY" or _is_lock_base_stale(lk):
                return "needs_rebase_reverify"
            if lk_st in ("NEEDS_AGENT_SEMANTIC_FIX", "NEEDS_AGENT_CONFLICT_RESOLUTION"):
                return lk_st.lower()
        if raw_st == "pr_open" and not any(is_lock_human_ready(lk) for _, lk in unmerged_locks):
            return raw_st
    return raw_st


def _resolve_board_artifact_path(state_dir: Path, explicit: str = "") -> Path | None:
    """Resolve the Markdown board artifact path from `--write-board` or `dev_queue_board_path.txt`."""
    if explicit and str(explicit).strip():
        return Path(str(explicit)).expanduser().resolve()
    ptr = state_dir / "dev_queue_board_path.txt"
    if ptr.is_file():
        raw = ptr.read_text(encoding="utf-8").strip()
        if raw:
            p = Path(raw).expanduser()
            if p.parent.is_dir():
                return p
    return None


NAV_INTENT_FILENAME = "dev_queue_nav_intent.json"
NAV_INTENT_LOCK_FILENAME = ".dev_queue_nav_intent.lock"
DEFAULT_NAV_INTENT_TTL_SECONDS = 60
DEFAULT_NAV_INTENT_ACK_WAIT_SECONDS = 0.12


def _resolve_nav_intent_path(state_dir: Path) -> Path:
    """Return `<state_dir>/dev_queue_nav_intent.json` path."""
    return Path(state_dir).expanduser().resolve() / NAV_INTENT_FILENAME


@contextlib.contextmanager
def _nav_intent_lock(state_dir: Path) -> Any:
    """Acquire exclusive file lock for `dev_queue_nav_intent.json` mutations when available."""
    lock_file = Path(state_dir).expanduser().resolve() / NAV_INTENT_LOCK_FILENAME
    if _dev_common is not None and hasattr(_dev_common, "file_lock"):
        with _dev_common.file_lock(lock_file, timeout_seconds=3.0):
            yield
        return
    yield


def _atomic_write_json_0600(target: Path, payload: dict[str, Any]) -> Path:
    """Atomically write `payload` to `target` with strict `0600` POSIX permissions."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, target)
    with contextlib.suppress(OSError):
        os.chmod(target, 0o600)
    return target


def write_nav_intent(
    state_dir: Path | str,
    *,
    conversation_id: str,
    display_id: str = "",
    sidebar_title: str = "",
    hotkey: str = "",
    action: str = "toggle_conversation",
    ttl_seconds: int = DEFAULT_NAV_INTENT_TTL_SECONDS,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Atomically write `0600` `dev_queue_nav_intent.json` for Sidecar UI Panel navigation."""
    st_dir = Path(state_dir).expanduser().resolve()
    nav_path = _resolve_nav_intent_path(st_dir)
    epoch = float(now_ts if now_ts is not None else time.time())
    created_iso = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    clean_cid = str(conversation_id or "").strip()
    clean_disp = str(display_id or clean_cid[:8]).strip()
    clean_title = " ".join(str(sidebar_title or "").split())
    payload: dict[str, Any] = {
        "schema_version": 1,
        "intent_id": str(uuid.uuid4()),
        "action": str(action or "toggle_conversation").strip(),
        "conversation_id": clean_cid,
        "display_id": clean_disp,
        "sidebar_title": clean_title,
        "hotkey": str(hotkey or "").strip(),
        "source": "dev_queue_tui",
        "created_at": created_iso,
        "created_at_epoch": round(epoch, 3),
        "ttl_seconds": max(5, int(ttl_seconds)),
        "acknowledged": False,
        "acknowledged_at": "",
    }
    with _nav_intent_lock(st_dir):
        _atomic_write_json_0600(nav_path, payload)
    return payload


def read_active_nav_intent(
    state_dir: Path | str,
    *,
    now_ts: float | None = None,
    include_acknowledged: bool = False,
) -> dict[str, Any] | None:
    """Read `dev_queue_nav_intent.json` and return it when unacknowledged and within TTL."""
    st_dir = Path(state_dir).expanduser().resolve()
    nav_path = _resolve_nav_intent_path(st_dir)
    data = _load_json(nav_path)
    if not data or not isinstance(data, dict):
        return None
    intent_id = str(data.get("intent_id") or "").strip()
    conv_id = str(data.get("conversation_id") or "").strip()
    if not intent_id or not conv_id:
        return None
    acked = bool(data.get("acknowledged"))
    if acked and not include_acknowledged:
        return None
    try:
        created_epoch = float(data.get("created_at_epoch") or 0.0)
        ttl_s = max(5, int(data.get("ttl_seconds") or DEFAULT_NAV_INTENT_TTL_SECONDS))
    except (TypeError, ValueError):
        return None
    epoch = float(now_ts if now_ts is not None else time.time())
    if created_epoch <= 0.0 or (epoch - created_epoch) > float(ttl_s):
        return None
    return dict(data)


def acknowledge_nav_intent(
    state_dir: Path | str,
    intent_id: str,
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Atomically mark matching `intent_id` in `dev_queue_nav_intent.json` as acknowledged (`0600`)."""
    clean_id = str(intent_id or "").strip()
    if not clean_id:
        return {"ok": False, "acknowledged": False, "error": "missing_intent_id"}
    st_dir = Path(state_dir).expanduser().resolve()
    nav_path = _resolve_nav_intent_path(st_dir)
    epoch = float(now_ts if now_ts is not None else time.time())
    ack_iso = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _nav_intent_lock(st_dir):
        cur = _load_json(nav_path)
        if not cur or str(cur.get("intent_id") or "").strip() != clean_id:
            return {"ok": False, "acknowledged": False, "intent_id": clean_id, "error": "intent_mismatch"}
        updated = dict(cur)
        updated["acknowledged"] = True
        updated["acknowledged_at"] = ack_iso
        _atomic_write_json_0600(nav_path, updated)
    return {"ok": True, "acknowledged": True, "intent_id": clean_id, "acknowledged_at": ack_iso}


def _resolve_nav_intent_ack_wait_seconds(explicit: float | None = None) -> float:
    """Resolve optional short ack poll window before running clipboard fallback."""
    if explicit is not None:
        return max(0.0, float(explicit))
    raw_env = str(os.environ.get("AGY_NAV_INTENT_ACK_WAIT_SECONDS") or "").strip()
    if raw_env:
        with contextlib.suppress(TypeError, ValueError):
            return max(0.0, min(2.0, float(raw_env)))
    if os.environ.get("PRE_COMMIT_HOOK") == "1" or os.environ.get("AGY_OFFLINE_ONLY") == "1":
        return 0.0
    return DEFAULT_NAV_INTENT_ACK_WAIT_SECONDS


def _wait_for_nav_intent_ack(state_dir: Path, intent_id: str, timeout_seconds: float) -> bool:
    """Poll `dev_queue_nav_intent.json` up to `timeout_seconds` for Sidecar UI Panel acknowledgment."""
    nav_path = _resolve_nav_intent_path(state_dir)
    cur = _load_json(nav_path)
    if cur and str(cur.get("intent_id") or "") == intent_id and bool(cur.get("acknowledged")):
        return True
    if timeout_seconds <= 0.0:
        return False
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.025, max(0.005, deadline - time.monotonic())))
        cur = _load_json(nav_path)
        if cur and str(cur.get("intent_id") or "") == intent_id and bool(cur.get("acknowledged")):
            return True
    return False


def _trigger_hitl_navigation(
    state_dir: Path,
    *,
    index: int,
    cid_disp: str,
    cid_full: str,
    ag_title: str,
    ack_wait_seconds: float | None = None,
) -> dict[str, Any]:
    """Write `0600` `dev_queue_nav_intent.json` for the Sidecar UI Panel and preserve clipboard+focus fallback."""
    intent_written = False
    intent_id = ""
    if cid_full:
        try:
            intent_obj = write_nav_intent(
                state_dir,
                conversation_id=cid_full,
                display_id=cid_disp,
                sidebar_title=ag_title,
                hotkey=f"h{index}",
            )
            intent_written = True
            intent_id = str(intent_obj.get("intent_id") or "")
        except OSError:
            intent_written = False

    focused = _focus_antigravity_app()
    wait_s = _resolve_nav_intent_ack_wait_seconds(ack_wait_seconds)
    acknowledged = bool(intent_written and intent_id and _wait_for_nav_intent_ack(state_dir, intent_id, wait_s))
    copied = False
    if not acknowledged:
        copied = _copy_to_clipboard(ag_title)
    return {
        "intent_written": intent_written,
        "intent_id": intent_id,
        "acknowledged": acknowledged,
        "copied": copied,
        "focused": focused,
    }


def _collect_lock_extra_urls(
    lk: dict[str, Any],
    item_urls_by_cid: dict[str, list[str]],
) -> list[str]:
    """Gather candidate PR URLs across a lock's primary and sibling conversation IDs."""
    collected: list[str] = []
    primary = str(lk.get("conversation_id") or "").strip()
    if primary:
        collected.extend(item_urls_by_cid.get(primary, []))
    raw_sibs = lk.get("sibling_conversation_ids")
    if isinstance(raw_sibs, list):
        for sib in raw_sibs:
            sib_clean = str(sib or "").strip()
            if sib_clean:
                collected.extend(item_urls_by_cid.get(sib_clean, []))
    return collected


def _board_semantic_digest(md_text: str) -> str:
    """Compute SHA-256 digest of board Markdown ignoring only the `> **Updated**:` timestamp."""
    normalized = _BOARD_UPDATED_LINE_RE.sub("> **Updated**: `<TS>`", md_text, count=1)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _write_live_antigravity_board(
    board_path: Path,
    *,
    now_str: str,
    orch_id: str,
    counts: dict[str, int],
    managed_list: list[str],
    label_map: dict[str, str],
    locks: dict[str, dict[str, Any]],
    lock_progress_cache: dict[str, dict[str, object]],
    repo_web_bases: dict[str, tuple[str, str]],
    repo_local_dirs: dict[str, Path],
    item_urls_by_cid: dict[str, list[str]],
    board_rows: list[dict[str, str]],
    hitl_rows: list[dict[str, str]],
    force_write: bool = False,
) -> bool:
    """Atomically write `dev_queue_board.md`, skipping redundant writes when semantic content is unchanged."""
    try:
        board_path.parent.mkdir(parents=True, exist_ok=True)
        orch_md = f"[{orch_id[:8]}](conversation://{orch_id})" if _is_uuid(orch_id) else f"`{orch_id[:8]}`"
        md: list[str] = [
            "# Antigravity Dev Queue & HITL Board",
            "",
            f"> **Updated**: `{now_str}` · **Orchestrator**: {orch_md} · "
            f"`PRs: {counts.get('pr_open', 0)}` · `In progress: {counts.get('in_progress', 0)}` · "
            f"`Pending: {counts.get('pending', 0)}` · `HITL: {counts.get('hitl_blocked', 0)}` · "
            f"`Merged: {counts.get('merged', 0)}`",
            "",
            "## 1. Conversations awaiting validation (`HITL_BLOCK` — 1-click in Antigravity)",
            "",
        ]
        if hitl_rows:
            md.append("| Tag | Prio | Antigravity Conversation (1-click) | Repos | Pending Decision | Proposal |")
            md.append("| :---: | :---: | :--- | :--- | :--- | :--- |")
            for hr in hitl_rows:
                conv_cell = (
                    f"[{hr['ag_title']}](conversation://{hr['cid']})"
                    if _is_uuid(hr["cid"])
                    else f"`{hr['ag_title']}`"
                )
                md.append(
                    f"| **`h{hr['idx']}`** | `{hr['prio']}` | {conv_cell} | "
                    f"`{hr['repos']}` | {hr['notes']} | {hr['artifact_link']} |"
                )
        else:
            md.append("_No `HITL_BLOCK` conversations pending validation right now._")

        md.extend(["", "## 2. Managed Repositories", ""])
        for slug in managed_list:
            lbl = label_map.get(slug, slug)
            lk = locks.get(slug)
            if lk:
                occ = str(lk.get("slot_occupancy") or f"{lk.get('active_slots_count', 1)}/{lk.get('max_slots', 2)}")
                lp = lock_progress_cache.get(slug) or {}
                is_pr = bool(lp.get("is_human_ready"))
                is_stalled = bool(lp.get("is_stalled"))
                if is_pr:
                    tag = "PR ready"
                elif is_stalled:
                    tag = str(lp.get("tag") or "STALL")
                elif lp.get("is_needs_rebase"):
                    tag = "NEEDS_REBASE_REVERIFY"
                elif lp.get("is_awaiting_base"):
                    tag = "READY_AWAITING_BASE_MERGE"
                elif lp.get("is_escalated"):
                    tag = "escalated"
                elif lp.get("is_agent_review"):
                    tag = "agent review"
                else:
                    tag = f"{int(lp.get('pct') or 0)}%"  # type: ignore[arg-type]
                if not is_pr and not is_stalled and int(lp.get("idle_seconds") or 0) >= IDLE_AMBER_THRESHOLD_SECONDS:
                    tag = f"{tag} · idle {lp.get('idle_label') or ''}".strip()
                s2_lp = lock_progress_cache.get(f"{slug}#slot2")
                if isinstance(s2_lp, dict):
                    s2_tag = (
                        "READY_AWAITING_BASE_MERGE"
                        if s2_lp.get("is_awaiting_base")
                        else ("NEEDS_REBASE_REVERIFY" if s2_lp.get("is_needs_rebase") else str(s2_lp.get("tag") or f"{int(s2_lp.get('pct') or 0)}%"))
                    )
                    tag = f"{tag} · S2:{s2_tag}"
                _, u, lock_has_pr, _ = _resolve_lock_human_pr_link(
                    slug,
                    lk,
                    repo_web_bases,
                    repo_local_dirs,
                    extra_urls=_collect_lock_extra_urls(lk, item_urls_by_cid),
                )
                u_md = _escape_md_url(u) if (is_pr and lock_has_pr) else (_clean_clickable_url(u) or u)
                link_part = (
                    f" · [Open PR]({u_md})"
                    if (is_pr and lock_has_pr and u_md)
                    else (f" · [Pulls]({u_md})" if u_md else "")
                )
                icon = "🔴" if is_stalled else "🟡"
                md.append(f"- {icon} **`{lbl}`** `[{occ}]` — `{tag}`{link_part}")
            else:
                pu = _repo_pulls_url(slug, repo_web_bases)
                link_part = f" · [Pulls]({pu})" if pu else ""
                md.append(f"- 🟢 **`{lbl}`** — `FREE`{link_part}")

        md.extend(["", "## 3. Active Queue (`dev_queue.json`)", ""])
        md.append("| # | Prio | Status | Progress | Repos | Task | Action |")
        md.append("| :---: | :---: | :--- | :--- | :--- | :--- | :--- |")
        for br in board_rows:
            md.append(
                f"| {br['idx']} | `{br['prio']}` | `{br['status']}` | `{br['progress']}` | "
                f"`{br['repos']}` | {br['title']} | {br['action_md']} |"
            )

        rendered_md = "\n".join(md) + "\n"
        new_digest = _board_semantic_digest(rendered_md)
        cache_key = str(board_path)
        if not force_write and board_path.is_file():
            st_cur = board_path.stat()
            cached = _BOARD_DIGEST_CACHE.get(cache_key)
            if cached is not None and cached[0] == int(st_cur.st_mtime_ns) and cached[1] == int(st_cur.st_size):
                if cached[2] == new_digest:
                    return False
            else:
                existing_md = board_path.read_text(encoding="utf-8")
                existing_digest = _board_semantic_digest(existing_md)
                if existing_digest == new_digest:
                    _BOARD_DIGEST_CACHE[cache_key] = (int(st_cur.st_mtime_ns), int(st_cur.st_size), new_digest)
                    return False

        tmp = board_path.with_name(f".{board_path.name}.tmp.{os.getpid()}")
        tmp.write_text(rendered_md, encoding="utf-8")
        os.replace(tmp, board_path)
        with contextlib.suppress(OSError):
            st_new = board_path.stat()
            _BOARD_DIGEST_CACHE[cache_key] = (int(st_new.st_mtime_ns), int(st_new.st_size), new_digest)
        return True
    except Exception:
        return False


def _compute_lightweight_opt_hint(
    *,
    items: list[dict[str, Any]],
    locks: dict[str, dict[str, Any]],
    all_locks: dict[str, dict[str, Any]],
    repo_local_dirs: dict[str, Path],
    bar_w: int,
    now_ts: float | None = None,
    stall_threshold_seconds: int | float | None = None,
    brain_dir: Path | str | None = None,
    enabled: bool = True,
) -> str:
    """Compute a fast, compact 1-line `OPT:` footer hint using lightweight 0-LLM analysis."""
    if not enabled or _flow_opt is None or not hasattr(_flow_opt, "analyze_dev_flow_optimizations"):
        return ""
    try:
        analysis = _flow_opt.analyze_dev_flow_optimizations(
            lightweight=True,
            preloaded_items=items,
            preloaded_active_locks=locks,
            preloaded_all_locks=all_locks,
            preloaded_repo_dirs=repo_local_dirs,
            now_ts=now_ts,
            stall_threshold_seconds=stall_threshold_seconds,
            brain_dir=brain_dir if brain_dir is not None else "",
        )
        return str(_flow_opt.format_compact_opt_hint(analysis, max_width=bar_w) or "")
    except Exception:
        return ""


def _cids_match(cid_a: str, cid_b: str) -> bool:
    """Return True when two conversation IDs match exactly or share an 8-char prefix."""
    a = str(cid_a or "").strip().lower()
    b = str(cid_b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 8 and len(b) >= 8 and a[:8] == b[:8]:
        return a.startswith(b) or b.startswith(a)
    return False


def _synthesize_untracked_lock_items(
    items: list[dict[str, Any]],
    locks: dict[str, dict[str, Any]],
    locks_by_cid: dict[str, list[tuple[str, dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Synthesize live table items for active `dev_pr_locks` (across all `slots`) not represented in `dev_queue.json`."""
    active_queue_cids = [
        str(it.get("conversation_id") or "").strip()
        for it in items
        if str(it.get("status") or "pending").strip().lower() not in INACTIVE_STATUSES
        and str(it.get("conversation_id") or "").strip()
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for slug, repo_lk in sorted(locks.items()):
        if not isinstance(repo_lk, dict) or not bool(repo_lk.get("active", True)):
            continue
        for lk in _iter_active_lock_slots(repo_lk):
            slot_num = int(lk.get("slot_id") or 1)
            primary_cid = str(lk.get("conversation_id") or "").strip()
            sibs = [str(s).strip() for s in (lk.get("sibling_conversation_ids") or []) if str(s).strip()]
            lock_cids = [c for c in [primary_cid, *sibs] if c]
            if any(_cids_match(lcid, qcid) for lcid in lock_cids for qcid in active_queue_cids):
                continue

            synth_cid = primary_cid or (
                f"lock-{slug.split('/')[-1]}-s{slot_num}" if slot_num > 1 else f"lock-{slug.split('/')[-1]}"
            )
            is_ready = is_lock_human_ready(lk)
            lk_st_u = str(lk.get("status") or "").strip().upper()
            if is_ready:
                synth_status = "pr_open"
            elif lk_st_u == "READY_AWAITING_BASE_MERGE":
                synth_status = "ready_awaiting_base_merge"
            elif lk_st_u == "NEEDS_REBASE_REVERIFY" or _is_lock_base_stale(lk):
                synth_status = "needs_rebase_reverify"
            else:
                synth_status = "in_progress"

            raw_title = (
                str(lk.get("proposal_title") or "").strip()
                or str(lk.get("action_id") or "").strip()
                or str(lk.get("summary") or "").strip()
                or str(lk.get("branch_name") or "").strip()
                or slug.split("/")[-1]
            )
            pr_candidates: list[str] = []
            if is_ready:
                for k in ("pr_url", "manual_pr_url"):
                    u = _sanitize_full_url(str(lk.get(k) or ""))
                    if u:
                        pr_candidates.append(u)
                        break

            entry = grouped.get(synth_cid)
            if entry is None:
                grouped[synth_cid] = {
                    "conversation_id": synth_cid,
                    "priority": 10 if slot_num == 1 else 12,
                    "status": synth_status,
                    "repo_slugs": [slug],
                    "merged_repos": [],
                    "branch_names": [str(lk.get("branch_name") or "").strip()] if lk.get("branch_name") else [],
                    "pr_urls": pr_candidates,
                    "title": f"[ad-hoc] {raw_title}",
                    "source": "ad_hoc_lock",
                    "synthetic_live_lock": True,
                    "added_at": str(lk.get("acquired_at") or ""),
                }
            else:
                if slug not in entry["repo_slugs"]:
                    entry["repo_slugs"].append(slug)
                if is_ready:
                    entry["status"] = "pr_open"
                    for u in pr_candidates:
                        if u not in entry["pr_urls"]:
                            entry["pr_urls"].append(u)
                br_n = str(lk.get("branch_name") or "").strip()
                if br_n and br_n not in entry["branch_names"]:
                    entry["branch_names"].append(br_n)

            if synth_cid not in locks_by_cid or all(
                not (s == slug and int(cur_lk.get("slot_id") or 1) == slot_num)
                for s, cur_lk in locks_by_cid[synth_cid]
            ):
                locks_by_cid.setdefault(synth_cid, []).append((slug, lk))

    return list(grouped.values())


def _resolve_orchestrator_liveness(
    orch_full: str,
    *,
    brain_dir: Path | str | Sequence[Path | str] | None,
    counts: dict[str, int],
    now_ts: float | None = None,
    stall_threshold_seconds: int | float | None = None,
    home_dir: Path | None = None,
) -> tuple[str, str]:
    """Read orchestrator transcript `mtime` safely across Desktop + CLI `brain/` dirs and return `(badge_text, ansi_code)`."""
    clean_orch = (orch_full or "").strip()
    if not _is_uuid(clean_orch):
        return ("", "")
    tpath: Path | None = None
    if _dev_common is not None and hasattr(_dev_common, "resolve_transcript_path"):
        tpath = _dev_common.resolve_transcript_path(clean_orch, brain_dir, home_dir=home_dir)
    else:
        for b_root in _resolve_brain_dirs(brain_dir, home_dir=home_dir):
            cand = b_root / clean_orch / ".system_generated" / "logs" / "transcript.jsonl"
            if cand.is_file():
                tpath = cand
                break
    if tpath is None:
        return ("", "")
    try:
        mtime = float(tpath.stat().st_mtime)
    except OSError:
        return ("", "")

    cur_ts = float(now_ts if now_ts is not None else time.time())
    idle_secs = max(0, int(cur_ts - mtime))
    idle_lbl = _format_compact_age(idle_secs)
    thresh = _resolve_stall_threshold(stall_threshold_seconds)
    has_open_work = any(counts.get(k, 0) > 0 for k in ("pending", "pr_open", "in_progress"))
    if idle_secs >= thresh and has_open_work:
        return (f"(idle {idle_lbl})", "1;33")
    if idle_secs < 120:
        return (f"(active {idle_lbl})", "0;32")
    return (f"(idle {idle_lbl})", "2")


def _extract_lock_telemetry_fields(lp: dict[str, object] | None, lk: dict[str, Any] | None = None) -> dict[str, Any]:
    """Extract canonical live idle/elapsed/source/worker/reviewer telemetry fields."""
    prog = lp if isinstance(lp, dict) else {}
    lock_d = lk if isinstance(lk, dict) else {}
    return {
        "idle_seconds": int(prog.get("idle_seconds") or 0),
        "idle_label": str(prog.get("idle_label") or "0s"),
        "elapsed_seconds": int(prog.get("elapsed_seconds") or 0),
        "elapsed_label": str(prog.get("elapsed_label") or "0s"),
        "last_activity_source": str(prog.get("last_activity_source") or ""),
        "stage_label": str(prog.get("stage_label") or lock_d.get("stage_label") or ""),
        "worker_cid": str(prog.get("worker_cid") or lock_d.get("worker_cid") or ""),
        "reviewer_subagent_id": str(prog.get("reviewer_subagent_id") or lock_d.get("reviewer_subagent_id") or ""),
        "idle_state": str(prog.get("idle_state") or ""),
        "is_stalled": bool(prog.get("is_stalled")),
    }


def render(
    args: argparse.Namespace,
    *,
    conv_db_path: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    is_tty_override: bool | None = None,
    term_width_override: int | None = None,
    now_ts_override: float | None = None,
    stall_threshold_seconds: int | float | None = None,
    layout_out: dict[str, Any] | None = None,
    repo_cache: dict[str, Any] | None = None,
    sync_status_badge: str = "",
    force_board_write: bool = False,
    strict_io: bool = False,
    ndjson_compact: bool = False,
    ndjson_seq: int | None = None,
) -> tuple[
    str,
    dict[int, tuple[str, str]],
    dict[int, tuple[str, str, str]],
    tuple[str, str] | None,
    Path,
    dict[str, Path],
]:
    """Render the single-table Dev Queue dashboard and optionally sync the live Antigravity board."""
    debug_on = bool(getattr(args, "debug", False))
    t_start = time.perf_counter()
    honor_dumb = is_tty_override is None
    is_tty = (
        (bool(getattr(sys.stdout, "isatty", lambda: False)()) and not _is_dumb_terminal())
        if is_tty_override is None
        else bool(is_tty_override)
    )
    color_on = _should_enable_color(bool(getattr(args, "no_color", False)), is_tty, honor_dumb_term=honor_dumb)
    use_osc8 = (
        (is_tty if is_tty_override is not None else _supports_osc8())
        and not bool(getattr(args, "plain_urls", False))
        and not (honor_dumb and _is_dumb_terminal())
    )
    term_w = max(78, term_width_override if term_width_override is not None else shutil.get_terminal_size((124, 40)).columns)
    bar_w = min(term_w, 220)

    state_dir = _resolve_state_dir(getattr(args, "state_dir", ""))
    queue_data = _load_json(state_dir / "dev_queue.json", strict=strict_io)
    items = queue_data.get("items") or []
    locks, all_locks = _load_locks(state_dir, strict=strict_io)
    repo_web_bases, repo_local_dirs = _discover_repos_cached(
        all_locks,
        items,
        getattr(args, "repos_dir", ""),
        repo_cache,
    )
    _debug_log(
        debug_on,
        f"snapshot loaded state_dir={state_dir} items={len(items)} active_locks={len(locks)} repos={len(repo_local_dirs)}",
    )

    lock_progress_cache: dict[str, dict[str, object]] = {}
    for slug, repo_lk in locks.items():
        for slot_entry in _iter_active_lock_slots(repo_lk):
            p_key = _slot_progress_cache_key(slug, slot_entry)
            lock_progress_cache[p_key] = _compute_lock_progress(
                slot_entry,
                now_ts=now_ts_override,
                threshold_seconds=stall_threshold_seconds,
                brain_dir=brain_dir,
            )
    opt_enabled = not bool(getattr(args, "no_opt", False))
    opt_hint = _compute_lightweight_opt_hint(
        items=items,
        locks=locks,
        all_locks=all_locks,
        repo_local_dirs=repo_local_dirs,
        bar_w=bar_w,
        now_ts=now_ts_override,
        stall_threshold_seconds=stall_threshold_seconds,
        brain_dir=brain_dir,
        enabled=opt_enabled,
    )

    all_managed_slugs: set[str] = set(locks.keys()) | set(repo_web_bases.keys())
    item_urls_by_cid: dict[str, list[str]] = {}
    known_item_cids: list[str] = []
    for it in items:
        icid = str(it.get("conversation_id") or "")
        if icid:
            if icid not in known_item_cids:
                known_item_cids.append(icid)
            urls = [str(u).strip() for u in (it.get("pr_urls") or []) if str(u or "").strip()]
            if urls:
                item_urls_by_cid.setdefault(icid, []).extend(urls)
        for s in it.get("repo_slugs") or []:
            if s:
                all_managed_slugs.add(str(s))
    label_map = _build_repo_labels(all_managed_slugs)

    locks_by_cid: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    seen_lock_cid_pairs: set[tuple[str, str, int]] = set()
    for slug, repo_lk in locks.items():
        for slot_lk in _iter_active_lock_slots(repo_lk):
            slot_num = int(slot_lk.get("slot_id") or 1)
            lock_cids: list[str] = []
            primary_cid = str(slot_lk.get("conversation_id") or "").strip()
            if primary_cid:
                lock_cids.append(primary_cid)
            raw_siblings = slot_lk.get("sibling_conversation_ids")
            if isinstance(raw_siblings, list):
                for sib in raw_siblings:
                    sib_clean = str(sib or "").strip()
                    if sib_clean:
                        lock_cids.append(sib_clean)
            matched_item_cids = [
                qcid
                for qcid in known_item_cids
                if any(_cids_match(lcid, qcid) for lcid in lock_cids)
            ]
            for lcid in dict.fromkeys([*lock_cids, *matched_item_cids]):
                pair = (lcid, slug, slot_num)
                if pair not in seen_lock_cid_pairs:
                    seen_lock_cid_pairs.add(pair)
                    locks_by_cid.setdefault(lcid, []).append((slug, slot_lk))

    synthetic_lock_items = _synthesize_untracked_lock_items(items, locks, locks_by_cid)
    effective_items = [*synthetic_lock_items, *items]

    counts: dict[str, int] = {}
    for it in effective_items:
        st = _resolve_effective_item_status(it, locks_by_cid)
        counts[st] = counts.get(st, 0) + 1

    filtered: list[dict[str, Any]] = []
    repo_filter = (args.repo or "").strip().lower()
    status_filter = (args.status or "").strip().lower()
    for it in effective_items:
        eff_st = _resolve_effective_item_status(it, locks_by_cid)
        raw_st = str(it.get("status") or "pending").strip().lower()
        if not args.all and not status_filter and eff_st in INACTIVE_STATUSES:
            continue
        if status_filter and eff_st != status_filter and raw_st != status_filter:
            continue
        repos_raw = [str(r) for r in (it.get("repo_slugs") or [])]
        if repo_filter and not any(repo_filter in r.lower() for r in repos_raw):
            continue
        filtered.append(it)

    filtered.sort(
        key=lambda x: (
            STATUS_ORDER.get(_resolve_effective_item_status(x, locks_by_cid), 99),
            int(
                x.get("effective_priority")
                if x.get("effective_priority") is not None
                else (x.get("priority") if x.get("priority") is not None else 999)
            ),
            int(x.get("priority") if x.get("priority") is not None else 999),
            str(x.get("added_at") or ""),
        )
    )
    display_ids = _build_display_ids(filtered)
    all_filtered_cids = [str(it.get("conversation_id") or "") for it in filtered]
    ag_titles = _load_conversation_titles(all_filtered_cids, db_path=conv_db_path, strict=strict_io)
    managed_list = [s for s in sorted(all_managed_slugs) if not repo_filter or repo_filter in s.lower()]

    if getattr(args, "json", False):
        enriched_items: list[dict[str, Any]] = []
        for it in filtered:
            entry = dict(it)
            cid_full = str(it.get("conversation_id") or "")
            entry["effective_status"] = _resolve_effective_item_status(it, locks_by_cid)
            if cid_full in ag_titles:
                entry["sidebar_title"] = ag_titles[cid_full]
            _, _, item_is_pr, item_git_state, _ = _resolve_item_human_pr_link(
                it, locks_by_cid, repo_web_bases, repo_local_dirs
            )
            item_prog = _compute_item_progress(
                it,
                locks_by_cid,
                lock_progress_cache,
                item_git_state,
                is_human_pr=item_is_pr,
                now_ts=now_ts_override,
            )
            if item_prog is not None:
                entry["progress"] = item_prog
            linked_lks = locks_by_cid.get(cid_full) or []
            if linked_lks:
                first_slug, first_lk = linked_lks[0]
                first_lp = lock_progress_cache.get(_slot_progress_cache_key(first_slug, first_lk)) or {}
                telem = _extract_lock_telemetry_fields(first_lp, first_lk)
                entry["lock_telemetry"] = telem
                for tk, tv in telem.items():
                    entry.setdefault(tk, tv)
            enriched_items.append(entry)
        active_locks_payload: dict[str, dict[str, Any]] = {}
        for slug, lk in locks.items():
            if repo_filter and repo_filter not in slug.lower():
                continue
            lp_primary = lock_progress_cache.get(slug, {})
            telem_primary = _extract_lock_telemetry_fields(lp_primary, lk)
            active_locks_payload[slug] = {
                **lk,
                **telem_primary,
                "progress": lp_primary,
                **(
                    {"slot2_progress": lock_progress_cache[f"{slug}#slot2"]}
                    if f"{slug}#slot2" in lock_progress_cache
                    else {}
                ),
            }
        stalled_locks_payload = {
            p_key: lp_val
            for p_key, lp_val in lock_progress_cache.items()
            if bool((lp_val or {}).get("is_stalled"))
            and (not repo_filter or repo_filter in p_key.lower())
        }
        json_obj: dict[str, Any] = {
            "schema_version": JSON_SCHEMA_VERSION,
            **(
                {
                    "seq": ndjson_seq,
                    "emitted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                if ndjson_seq is not None
                else {}
            ),
            "state_dir": str(state_dir),
            "updated_at": queue_data.get("updated_at", ""),
            "orchestrator_conversation_id": queue_data.get("orchestrator_conversation_id", ""),
            "orchestration": _load_orchestration_phase(state_dir, locks),
            "counts": counts,
            "managed_repos": managed_list,
            "locks": active_locks_payload,
            "active_locks": active_locks_payload,
            "stalled_locks": stalled_locks_payload,
            "opt_hint": opt_hint,
            "items": enriched_items,
        }
        payload = (
            json.dumps(json_obj, separators=(",", ":"), ensure_ascii=False)
            if ndjson_compact
            else json.dumps(json_obj, indent=2, ensure_ascii=False)
        )
        return (payload, {}, {}, None, state_dir, repo_local_dirs)

    now_local = datetime.now().strftime("%H:%M:%S")
    now_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    orch_full = str(queue_data.get("orchestrator_conversation_id") or "-")
    orch_id = orch_full[:8]
    orch_live_txt, orch_live_code = _resolve_orchestrator_liveness(
        orch_full,
        brain_dir=brain_dir,
        counts=counts,
        now_ts=now_ts_override,
        stall_threshold_seconds=stall_threshold_seconds,
    )
    in_flight_run = (
        counts.get("in_progress", 0)
        + counts.get("ready_awaiting_base_merge", 0)
        + counts.get("needs_rebase_reverify", 0)
        + counts.get("needs_agent_semantic_fix", 0)
        + counts.get("needs_agent_conflict_resolution", 0)
    )
    summary_bits = [
        _color(f"pr:{counts.get('pr_open', 0)}", "1;33", color_on),
        _color(f"run:{in_flight_run}", "1;36", color_on),
        _color(f"pending:{counts.get('pending', 0)}", "1;37", color_on),
        _color(f"hitl:{counts.get('hitl_blocked', 0)}", "1;35", color_on),
        _color(f"standby:{counts.get('standby', 0)}", "2;37", color_on),
        _color(f"merged:{counts.get('merged', 0)}", "0;32", color_on),
    ]

    header_lines: list[str] = []
    header_lines.append(_color("═" * bar_w, "2;36", color_on))
    orch_segment = f"orch:{_color(orch_id, '1', color_on)}"
    if orch_live_txt:
        orch_segment = f"{orch_segment} {_color(orch_live_txt, orch_live_code, color_on)}"
    header_left = (
        f"{_color('ANTIGRAVITY DEV QUEUE', '1;36', color_on)}  "
        f"{_color(now_local, '2', color_on)}  "
        f"{orch_segment}"
    )
    if sync_status_badge:
        sync_code = "1;36" if "…" in sync_status_badge else "2"
        header_left = f"{header_left}  {_color(sync_status_badge, sync_code, color_on)}"
    summary_str = "  ".join(summary_bits)
    if bar_w >= 104 and (_visible_len(header_left) + 5 + _visible_len(summary_str)) <= bar_w:
        header_lines.append(f"{header_left}  |  {summary_str}")
    elif bar_w >= 104 and not orch_live_txt and not sync_status_badge:
        header_lines.append(f"{header_left}  |  {summary_str}")
    else:
        header_lines.append(header_left)
        header_lines.append(summary_str)

    orch_phase = _load_orchestration_phase(state_dir, locks)
    if bool(orch_phase.get("paused")):
        pause_banner = str(orch_phase.get("banner_text") or "PAUSED — 0 new dispatches · Watchdog silent")
        pause_reason = str(orch_phase.get("reason") or "").strip()
        if pause_reason:
            pause_banner = f"{pause_banner} ({pause_reason})"
        header_lines.append(
            _color(_trunc(f"⏸ [[P] Continue] {pause_banner}", bar_w), "1;33", color_on)
        )

    stalled_alerts: list[tuple[str, str, str]] = []
    for slug in managed_list:
        repo_lk = locks.get(slug)
        if not repo_lk:
            continue
        for slot_entry in _iter_active_lock_slots(repo_lk):
            lp = lock_progress_cache.get(_slot_progress_cache_key(slug, slot_entry)) or {}
            if bool(lp.get("is_stalled")):
                lbl = label_map.get(slug, slug)
                stalled_alerts.append(
                    (lbl, str(lp.get("idle_label") or ""), str(slot_entry.get("branch_name") or ""))
                )

    if managed_list:
        repo_pills: list[str] = []
        for slug in managed_list:
            lbl = label_map.get(slug, slug)
            lk = locks.get(slug)
            if lk:
                occ = str(lk.get("slot_occupancy") or f"{lk.get('active_slots_count', 1)}/{lk.get('max_slots', 2)}")
                lp = lock_progress_cache.get(slug) or {}
                is_human_pr = bool(lp.get("is_human_ready"))
                is_stalled = bool(lp.get("is_stalled"))
                is_needs_rebase = bool(lp.get("is_needs_rebase"))
                is_awaiting_base = bool(lp.get("is_awaiting_base"))
                is_rev = bool(lp.get("is_agent_review"))
                is_esc = bool(lp.get("is_escalated"))
                if is_human_pr:
                    base_tag = "PR"
                elif is_stalled:
                    base_tag = str(lp.get("tag") or "STALL")
                elif is_needs_rebase:
                    base_tag = "rebase"
                elif is_awaiting_base:
                    base_tag = "await"
                elif is_esc:
                    base_tag = "esc"
                elif is_rev:
                    base_tag = "rev"
                else:
                    base_tag = f"{int(lp.get('pct') or 0)}%"  # type: ignore[arg-type]

                lp_idle_secs = int(lp.get("idle_seconds") or 0)
                lp_idle_lbl = str(lp.get("idle_label") or "")
                is_amber_pill = (
                    not is_human_pr
                    and not is_stalled
                    and not is_awaiting_base
                    and not is_needs_rebase
                    and not is_esc
                    and lp_idle_secs >= IDLE_AMBER_THRESHOLD_SECONDS
                )

                s2_lp = lock_progress_cache.get(f"{slug}#slot2")
                s2_suffix_plain = ""
                s2_is_amber = False
                s2_idle_lbl = ""
                if isinstance(s2_lp, dict):
                    if s2_lp.get("is_stalled"):
                        s2_short = str(s2_lp.get("tag") or "STALL")
                    elif s2_lp.get("is_awaiting_base"):
                        s2_short = "await"
                    elif s2_lp.get("is_needs_rebase"):
                        s2_short = "rebase"
                    elif s2_lp.get("is_agent_review"):
                        s2_short = "rev"
                    else:
                        s2_short = f"{int(s2_lp.get('pct') or 0)}%"  # type: ignore[arg-type]
                    s2_idle_s = int(s2_lp.get("idle_seconds") or 0)
                    s2_idle_lbl = str(s2_lp.get("idle_label") or "")
                    s2_is_amber = (
                        not bool(s2_lp.get("is_stalled"))
                        and not bool(s2_lp.get("is_awaiting_base"))
                        and not bool(s2_lp.get("is_needs_rebase"))
                        and s2_idle_s >= IDLE_AMBER_THRESHOLD_SECONDS
                    )
                    s2_suffix_plain = f"·S2:{s2_short}"

                dot_code = (
                    "1;33"
                    if (is_human_pr or is_amber_pill or s2_is_amber)
                    else ("1;31" if is_stalled else ("1;35" if (is_esc or is_needs_rebase) else "1;36"))
                )
                dot = _color("●", dot_code, color_on)
                _, repo_u, _, _ = _resolve_lock_human_pr_link(
                    slug,
                    lk,
                    repo_web_bases,
                    repo_local_dirs,
                    extra_urls=_collect_lock_extra_urls(lk, item_urls_by_cid),
                )
                styled_name = _color(lbl, "1;33" if is_human_pr else ("1;31" if is_stalled else "1"), color_on)
                occ_styled = _color(f"[{occ}]", "1;36" if occ.startswith("2/") else "2", color_on)
                tag_code = "1;33" if is_human_pr else ("1;35" if (is_stalled or is_needs_rebase) else "2")
                pill_inner = _color(f"({base_tag}", tag_code, color_on)
                if is_amber_pill and lp_idle_lbl:
                    pill_inner += _color(f" · idle {lp_idle_lbl}", "1;33", color_on)
                if s2_suffix_plain:
                    pill_inner += _color(s2_suffix_plain, tag_code, color_on)
                    if s2_is_amber and s2_idle_lbl:
                        pill_inner += _color(f" · idle {s2_idle_lbl}", "1;33", color_on)
                pill_inner += _color(")", tag_code, color_on)
                repo_pills.append(
                    f"{dot} {_osc8_link(styled_name, repo_u, use_osc8)} "
                    f"{occ_styled} {pill_inner}"
                )
            else:
                dot = _color("○", "0;32", color_on)
                pu = _repo_pulls_url(slug, repo_web_bases)
                styled_name = _color(lbl, "0;37", color_on)
                repo_pills.append(f"{dot} {_osc8_link(styled_name, pu, use_osc8)} {_color('(FREE)', '0;32', color_on)}")
        header_lines.append(f"{_color('REPOS:', '1;33', color_on)}  " + "   ".join(repo_pills))

    header_lines.append(_color("─" * bar_w, "2", color_on))
    has_active_work = bool(locks or counts.get("pr_open", 0) > 0)

    if not filtered:
        empty_msg = _color("No matching items in dev_queue.json.", "2", color_on)
        sticky_empty: list[str] = [empty_msg]
        if stalled_alerts:
            sticky_empty.append(_color("─" * bar_w, "2", color_on))
            summary_st = ", ".join(f"{r} idle {idle}" for r, idle, _ in stalled_alerts)
            sticky_empty.append(
                _color(_trunc(f"⚠ STALLED WORKER: {summary_st} — inspect worktree or re-dispatch", bar_w), "1;31", color_on)
            )
        if opt_hint:
            sticky_empty.append(_color(_trunc(opt_hint, bar_w), "1;35", color_on))
        sticky_empty.append(_color("═" * bar_w, "2;36", color_on))
        lines = [*header_lines, *sticky_empty]
        if layout_out is not None:
            layout_out.update(
                {
                    "header_lines": header_lines,
                    "active_row_lines": [],
                    "middle_row_lines": [],
                    "sticky_footer_lines": sticky_empty,
                    "row_records": [],
                    "hitl_count": 0,
                    "all_pr_links": [],
                    "has_active_work": has_active_work,
                    "orchestration_paused": bool(orch_phase.get("paused")),
                    "orchestration_phase": str(orch_phase.get("phase") or "active"),
                    "active_locks": dict(locks),
                    "all_locks": dict(all_locks),
                    "items_snapshot": list(effective_items),
                    "repo_local_dirs": dict(repo_local_dirs),
                    "color_on": color_on,
                    "use_osc8": use_osc8,
                    "bar_w": bar_w,
                }
            )
        board_path = _resolve_board_artifact_path(state_dir, getattr(args, "write_board", ""))
        if board_path is not None:
            _write_live_antigravity_board(
                board_path,
                now_str=now_full,
                orch_id=orch_full,
                counts=counts,
                managed_list=managed_list,
                label_map=label_map,
                locks=locks,
                lock_progress_cache=lock_progress_cache,
                repo_web_bases=repo_web_bases,
                repo_local_dirs=repo_local_dirs,
                item_urls_by_cid=item_urls_by_cid,
                board_rows=[],
                hitl_rows=[],
                force_write=force_board_write,
            )
        return ("\n".join(lines), {}, {}, None, state_dir, repo_local_dirs)

    row_eff_statuses = [_resolve_effective_item_status(it, locks_by_cid) for it in filtered]
    status_col = max(
        11,
        *(len(_STATUS_STYLES.get(es, ("0", es.upper()))[1]) for es in row_eff_statuses),
    )
    prog_col = 21 if bar_w >= 116 else (20 if bar_w >= 108 else 16)
    repo_col = 24 if bar_w >= 160 else (19 if bar_w >= 112 else 14)
    link_col = 9
    fixed = 2 + 1 + 4 + 2 + status_col + 2 + prog_col + 2 + repo_col + 2 + 8 + 2 + 2 + link_col
    title_col = max(14, bar_w - fixed)

    hdr = (
        f"{'#':>2} {'Prio':>4}  {'STATUS':<{status_col}}  {'PROGRESS':<{prog_col}}  "
        f"{'REPOS':<{repo_col}}  {'ID':<8}  {'TITLE':<{title_col}}  {'ACTION':<{link_col}}"
    )
    header_lines.append(_color(hdr, "1;4", color_on))

    active_row_lines: list[str] = []
    middle_row_lines: list[str] = []
    sticky_footer_lines: list[str] = []
    row_records: list[dict[str, Any]] = []
    row_links: dict[int, tuple[str, str]] = {}
    hitl_convs: dict[int, tuple[str, str, str]] = {}
    first_pr_link: tuple[str, str] | None = None
    all_pr_links: list[tuple[str, str]] = []
    seen_pr_urls: set[str] = set()
    ready_pr_footer: list[tuple[int, str, str, str, str, str, bool]] = []
    hitl_footer: list[tuple[int, str, str]] = []
    board_rows: list[dict[str, str]] = []
    hitl_board_rows: list[dict[str, str]] = []
    hitl_idx = 0
    resolved_brain_roots = _resolve_brain_dirs(brain_dir)

    for idx, (it, eff_st) in enumerate(zip(filtered, row_eff_statuses), 1):
        prio = int(it.get("priority") if it.get("priority") is not None else 99)
        st = eff_st
        st_lower = eff_st.lower()
        repo_slugs_list = [str(r) for r in (it.get("repo_slugs") or []) if r]
        merged_set = {str(r) for r in (it.get("merged_repos") or []) if r}
        cid_full = str(it.get("conversation_id") or "")
        cid_disp = display_ids.get(cid_full, cid_full[:8])
        title = str(it.get("title") or "").strip()
        deps = [str(d)[:8] for d in (it.get("depends_on") or []) if str(d).strip()]
        if deps:
            title = f"{title} [deps:{','.join(deps)}]"

        link_kind, link_url, is_human_pr, git_state, target_slug = _resolve_item_human_pr_link(
            it, locks_by_cid, repo_web_bases, repo_local_dirs
        )
        pr_repo_slug = target_slug if is_human_pr else ""
        repos_str = _compact_repos(repo_slugs_list, label_map, merged_set, repo_col, pr_repo_slug=pr_repo_slug)
        repos_md = _compact_repos(repo_slugs_list, label_map, merged_set, 40, pr_repo_slug=pr_repo_slug)
        prog = _compute_item_progress(
            it,
            locks_by_cid,
            lock_progress_cache,
            git_state,
            is_human_pr=is_human_pr,
            now_ts=now_ts_override,
        )
        prog_cell = _render_inline_progress(prog, prog_col, color_on)
        if prog and str(prog.get("mode") or "") in ("pr_part", "pr_final"):
            total_r = int(prog.get("total_repos") or 1)  # type: ignore[arg-type]
            part_i = int(prog.get("part_idx") or 1)  # type: ignore[arg-type]
            prog_md = f"PR {part_i}/{total_r} ready" if total_r > 1 else "PR ready"
        elif prog and str(prog.get("mode") or "") == "stalled":
            prog_md = str(prog.get("tag") or "STALL")
        elif prog and str(prog.get("mode") or "") == "partial_pending":
            prog_md = f"{prog.get('tag', '1/2 merged')} ({int(prog.get('pct') or 0)}%)"  # type: ignore[arg-type]
        else:
            prog_md = f"{int(prog['pct'])}% {prog.get('tag', '')}".strip() if prog else "—"  # type: ignore[arg-type]
        action_md = "—"

        if is_human_pr and link_url:
            clean_u = _clean_clickable_url(link_url) or link_url
            full_u = _sanitize_full_url(link_url) or link_url
            has_body = _url_has_pr_body(full_u)
            pr_pair = (f"{cid_disp} (PR)", full_u)
            row_links[idx] = pr_pair
            if first_pr_link is None:
                first_pr_link = pr_pair
            if full_u not in seen_pr_urls:
                seen_pr_urls.add(full_u)
                all_pr_links.append(pr_pair)
            repo_lbl = label_map.get(target_slug, target_slug.split("/")[-1]) if target_slug else repos_str
            part_meta = ""
            if len(repo_slugs_list) > 1:
                part_idx = min(len(repo_slugs_list), len(merged_set) + 1)
                remaining_lbls = [
                    label_map.get(r, r.split("/")[-1])
                    for r in repo_slugs_list
                    if r not in merged_set and r != target_slug
                ]
                unlock_txt = f" · unlocks {', '.join(remaining_lbls)}" if remaining_lbls else ""
                part_meta = f" · part {part_idx}/{len(repo_slugs_list)}{unlock_txt}"
            ready_pr_footer.append((idx, repo_lbl, cid_disp, clean_u, full_u, part_meta, has_body))
            pill_raw = f"[{idx}] PR" if idx <= 9 else "[↗ PR]"
            pill_styled = _color(pill_raw, "1;33", color_on)
            action_cell = _pad_visible(_osc8_link(pill_styled, full_u, use_osc8), link_col)
            md_pr_u = _escape_md_url(full_u) or clean_u
            action_md = f"[Open PR]({md_pr_u})"
            if cid_full in ag_titles:
                action_md += f" · [Chat](conversation://{cid_full})"
        elif link_kind in ("Sync", "Stale"):
            pill_styled = _color(f"[↻ {link_kind}]", "1;35", color_on)
            action_cell = _pad_visible(pill_styled, link_col)
            action_md = f"`{link_kind}`"
        elif st_lower == "hitl_blocked" and cid_full:
            hitl_idx += 1
            ag_t = ag_titles.get(cid_full) or title
            hitl_convs[hitl_idx] = (cid_disp, cid_full, ag_t)
            hitl_footer.append((hitl_idx, cid_disp, ag_t))
            pill_raw = f"[h{hitl_idx}] Agy"
            pill_styled = _color(pill_raw, "1;35", color_on)
            action_cell = _pad_visible(pill_styled, link_col)
            action_md = f"[Open chat](conversation://{cid_full})" if _is_uuid(cid_full) else "—"
            prop_mds: list[Path] = []
            seen_prop_names: set[str] = set()
            for b_root in resolved_brain_roots:
                cid_brain_dir = b_root / cid_full
                if cid_brain_dir.is_dir():
                    for md_cand in sorted(cid_brain_dir.glob("*.md")):
                        if md_cand.name not in seen_prop_names:
                            seen_prop_names.add(md_cand.name)
                            prop_mds.append(md_cand)
            art_link = " · ".join(f"[{p.name}](file://{p})" for p in prop_mds[:2]) if prop_mds else "—"
            hitl_board_rows.append(
                {
                    "idx": str(hitl_idx),
                    "prio": str(prio),
                    "cid": cid_full,
                    "ag_title": _sanitize_md_table_cell(ag_t),
                    "repos": repos_md,
                    "notes": _trunc(_sanitize_md_table_cell(str(it.get("notes") or title)), 180),
                    "artifact_link": art_link,
                }
            )
        else:
            raw_item_st = str(it.get("status") or "pending").strip().lower()
            if raw_item_st == "in_progress" and st_lower == "in_progress":
                unmerged = [r for r in repo_slugs_list if r not in merged_set]
                if unmerged:
                    pu = _repo_pulls_url(str(unmerged[0]), repo_web_bases)
                    if pu:
                        row_links[idx] = (f"{cid_disp} (Repo)", pu)
            action_cell = _pad_visible(_color("—", "2", color_on), link_col)
            if cid_full in ag_titles:
                action_md = f"[Chat](conversation://{cid_full})"

        clean_title_flat = " ".join(title.split())
        row_line = (
            f"{idx:>2} {prio:>4}  {_status_badge(st, color_on, width=status_col)}  {prog_cell}  "
            f"{_pad_visible(repos_str, repo_col)}  {_color(_pad_visible(cid_disp, 8), '36', color_on)}  "
            f"{_pad_visible(_trunc(clean_title_flat, title_col), title_col)}  {action_cell}"
        )
        is_active_band = (
            st_lower in ("pr_open", "in_progress", "ready_awaiting_base_merge", "needs_rebase_reverify")
            or bool(locks_by_cid.get(cid_full))
        )
        if is_active_band:
            active_row_lines.append(row_line)
        else:
            middle_row_lines.append(row_line)

        item_lock_records: list[dict[str, Any]] = []
        for l_slug, l_dict in locks_by_cid.get(cid_full, []):
            l_prog = lock_progress_cache.get(_slot_progress_cache_key(l_slug, l_dict)) or {}
            item_lock_records.append(
                {
                    "repo_slug": l_slug,
                    "slot_id": int(l_dict.get("slot_id") or 1),
                    "status": str(l_dict.get("status") or ""),
                    "branch_name": str(l_dict.get("branch_name") or ""),
                    "worktree_path": str(l_dict.get("worktree_path") or ""),
                    **_extract_lock_telemetry_fields(l_prog, l_dict),
                }
            )
        search_blob = " ".join(
            [
                cid_full,
                cid_disp,
                st_lower,
                clean_title_flat,
                ag_titles.get(cid_full, ""),
                " ".join(repo_slugs_list),
                " ".join(str(b) for b in (it.get("branch_names") or [])),
                str(it.get("notes") or ""),
            ]
        ).lower()
        row_records.append(
            {
                "idx": idx,
                "band": "active" if is_active_band else "middle",
                "raw_line": row_line,
                "item": dict(it),
                "effective_status": st,
                "display_id": cid_disp,
                "sidebar_title": ag_titles.get(cid_full, ""),
                "repos_str": repos_str,
                "progress": prog,
                "linked_locks": item_lock_records,
                "pr_url": row_links.get(idx, ("", ""))[1] if is_human_pr else "",
                "search_blob": search_blob,
            }
        )

        board_rows.append(
            {
                "idx": str(idx),
                "prio": str(prio),
                "status": _STATUS_STYLES.get(st_lower, ("0", st.upper()))[1],
                "progress": prog_md,
                "repos": repos_md,
                "title": _trunc(_sanitize_md_table_cell(title), 72),
                "action_md": action_md,
            }
        )

    if ready_pr_footer:
        sticky_footer_lines.append(_color("─" * bar_w, "2", color_on))
        sticky_footer_lines.append(
            _color(
                "PRs READY FOR MERGE (press number/[o]/[O] to open with title + verifications · or Cmd+Click):",
                "1;33",
                color_on,
            )
        )
        for idx, repo_lbl, cid_disp, clean_u, full_u, part_meta, has_body in ready_pr_footer:
            verif_badge = (
                _color(" [+verified]", "0;32", color_on)
                if has_body
                else _color(" [commit fallback]", "2", color_on)
            )
            sticky_footer_lines.append(
                f"  {_color(f'[{idx}]', '1;33', color_on)} {_color(repo_lbl, '1', color_on)} "
                f"({cid_disp}{part_meta}){verif_badge}:"
            )
            if use_osc8:
                compact_lbl = _compact_osc8_pr_label(full_u, repo_lbl, max_width=max(32, bar_w - 6))
                styled_lbl = _color(compact_lbl, "4;36", color_on)
                sticky_footer_lines.append(f"    {_osc8_link(styled_lbl, full_u, True)}")
            else:
                sticky_footer_lines.append(clean_u)

    if hitl_footer:
        sticky_footer_lines.append(_color("─" * bar_w, "2", color_on))
        sticky_footer_lines.append(
            f"{_color('HITL — ANTIGRAVITY APP VALIDATION', '1;35', color_on)} "
            f"{_color('(press h + number: jump via Dev Queue Panel or copy title & focus Antigravity · /hitl-review):', '2', color_on)}"
        )
        for h_i, cid_disp, ag_t in hitl_footer:
            sticky_footer_lines.append(
                f"  {_color(f'[h{h_i}]', '1;35', color_on)} "
                f"{_color(cid_disp, '36', color_on)}  "
                f"{_trunc(ag_t, max(20, bar_w - 20))}"
            )

    if stalled_alerts:
        sticky_footer_lines.append(_color("─" * bar_w, "2", color_on))
        st_bits = [
            f"{repo_lbl} idle {idle_lbl}" + (f" ({br})" if br else "")
            for repo_lbl, idle_lbl, br in stalled_alerts
        ]
        sticky_footer_lines.append(
            _color(
                _trunc(f"⚠ STALLED WORKER: {'; '.join(st_bits)} — inspect worktree or re-dispatch", bar_w),
                "1;31",
                color_on,
            )
        )

    if opt_hint:
        if not stalled_alerts and not ready_pr_footer and not hitl_footer:
            sticky_footer_lines.append(_color("─" * bar_w, "2", color_on))
        sticky_footer_lines.append(_color(_trunc(opt_hint, bar_w), "1;35", color_on))

    sticky_footer_lines.append(_color("═" * bar_w, "2;36", color_on))
    lines = [*header_lines, *active_row_lines, *middle_row_lines, *sticky_footer_lines]

    if layout_out is not None:
        layout_out.update(
            {
                "header_lines": header_lines,
                "active_row_lines": active_row_lines,
                "middle_row_lines": middle_row_lines,
                "sticky_footer_lines": sticky_footer_lines,
                "row_records": row_records,
                "hitl_count": len(hitl_convs),
                "all_pr_links": all_pr_links,
                "has_active_work": has_active_work,
                "orchestration_paused": bool(orch_phase.get("paused")),
                "orchestration_phase": str(orch_phase.get("phase") or "active"),
                "active_locks": dict(locks),
                "all_locks": dict(all_locks),
                "items_snapshot": list(effective_items),
                "repo_local_dirs": dict(repo_local_dirs),
                "color_on": color_on,
                "use_osc8": use_osc8,
                "bar_w": bar_w,
            }
        )

    board_path = _resolve_board_artifact_path(state_dir, getattr(args, "write_board", ""))
    if board_path is not None:
        _write_live_antigravity_board(
            board_path,
            now_str=now_full,
            orch_id=orch_full,
            counts=counts,
            managed_list=managed_list,
            label_map=label_map,
            locks=locks,
            lock_progress_cache=lock_progress_cache,
            repo_web_bases=repo_web_bases,
            repo_local_dirs=repo_local_dirs,
            item_urls_by_cid=item_urls_by_cid,
            board_rows=board_rows,
            hitl_rows=hitl_board_rows,
            force_write=force_board_write,
        )

    _debug_log(debug_on, f"render completed in {(time.perf_counter() - t_start) * 1000.0:.2f}ms")
    return ("\n".join(lines), row_links, hitl_convs, first_pr_link, state_dir, repo_local_dirs)


def _wrapped_row_count(line: str, safe_w: int) -> int:
    return max(1, math.ceil(_visible_len(line) / safe_w))


def _clamp_lines_to_viewport(all_lines: list[str], term_w: int, term_h: int, footer: str) -> list[str]:
    """Clamp rendered lines (accounting for soft wrapping of body and footer) to fit inside the terminal height."""
    safe_w = max(1, term_w)
    footer_rows = _wrapped_row_count(footer, safe_w)
    budget = max(1, term_h - footer_rows)
    kept: list[str] = []
    used_rows = 0
    for idx, line in enumerate(all_lines):
        rows = _wrapped_row_count(line, safe_w)
        remaining_items = len(all_lines) - idx
        if used_rows + rows > budget:
            if used_rows + 1 <= budget:
                kept.append(
                    _trunc(f"… ({remaining_items} more lines — enlarge terminal or filter with -r/-s)", safe_w)
                )
            elif kept:
                kept[-1] = _trunc(
                    f"… ({remaining_items + 1} more lines — enlarge terminal or filter with -r/-s)",
                    safe_w,
                )
            break
        kept.append(line)
        used_rows += rows
    kept.append(footer)
    return kept


def _filter_compact_middle_rows(middle_row_lines: list[str], max_pending: int = 3) -> list[str]:
    """Keep at most `max_pending` `PENDING` rows while preserving all `HITL_BLOCK` and non-pending rows."""
    plain_rows = [ANSI_RE.sub("", ln) for ln in middle_row_lines]
    has_status_tokens = any(
        any(tok in p.upper() for tok in ("PENDING", "HITL_BLOCK", "STANDBY", "MERGED", "CLEANED", "CANCELLED", "REJECTED"))
        for p in plain_rows
    )
    out: list[str] = []
    kept_pending = 0
    for raw_ln, plain in zip(middle_row_lines, plain_rows):
        upper = plain.upper()
        if "HITL_BLOCK" in upper:
            out.append(raw_ln)
        elif "PENDING" in upper or not has_status_tokens:
            if kept_pending < max_pending:
                out.append(raw_ln)
                kept_pending += 1
        else:
            out.append(raw_ln)
    return out


def _compose_priority_viewport_with_meta(
    header_lines: list[str],
    active_row_lines: list[str],
    middle_row_lines: list[str],
    sticky_footer_lines: list[str],
    term_w: int,
    term_h: int,
    footer: str,
    *,
    scroll_offset: int = 0,
    compact_pending: bool = False,
) -> tuple[list[str], int, int]:
    """Compose wrap-aware viewport keeping header, active rows, and PR/HITL/OPT sticky footers visible."""
    safe_w = max(1, term_w)
    footer_rows = _wrapped_row_count(footer, safe_w)
    body_budget = max(1, term_h - footer_rows)
    color_on = any("\033[" in ln for ln in header_lines)

    header_rows = sum(_wrapped_row_count(ln, safe_w) for ln in header_lines)
    active_rows = sum(_wrapped_row_count(ln, safe_w) for ln in active_row_lines)
    sticky_rows = sum(_wrapped_row_count(ln, safe_w) for ln in sticky_footer_lines)

    if not active_row_lines and not middle_row_lines:
        combined = [*header_lines, *sticky_footer_lines]
        if sum(_wrapped_row_count(ln, safe_w) for ln in combined) <= body_budget:
            return ([*combined, footer], 0, 0)
        return (_clamp_lines_to_viewport(combined, term_w, term_h, footer), 0, 0)

    if header_rows + sticky_rows > body_budget:
        all_lines = [*header_lines, *active_row_lines, *middle_row_lines, *sticky_footer_lines]
        return (_clamp_lines_to_viewport(all_lines, term_w, term_h, footer), 0, 0)

    if header_rows + active_rows + sticky_rows > body_budget:
        avail_active = max(0, body_budget - header_rows - sticky_rows)
        kept_active: list[str] = []
        used_a = 0
        reserve_ind = 1 if avail_active >= 1 else 0
        for ln in active_row_lines:
            r = _wrapped_row_count(ln, safe_w)
            if used_a + r > max(0, avail_active - reserve_ind):
                break
            kept_active.append(ln)
            used_a += r
        hidden_cnt = (len(active_row_lines) - len(kept_active)) + len(middle_row_lines)
        ind: list[str] = []
        if reserve_ind and hidden_cnt > 0:
            ind.append(
                _color(
                    _trunc(f"  … (+{hidden_cnt} rows hidden to keep PR/HITL footers visible)", safe_w),
                    "2;36",
                    color_on,
                )
            )
        return ([*header_lines, *kept_active, *ind, *sticky_footer_lines, footer], 0, 0)

    middle_budget = body_budget - (header_rows + active_rows + sticky_rows)
    candidate_middle = (
        _filter_compact_middle_rows(middle_row_lines) if compact_pending else list(middle_row_lines)
    )
    compact_hidden = len(middle_row_lines) - len(candidate_middle)
    cand_total_rows = sum(_wrapped_row_count(ln, safe_w) for ln in candidate_middle)

    if cand_total_rows <= middle_budget and compact_hidden == 0 and scroll_offset <= 0:
        return ([*header_lines, *active_row_lines, *candidate_middle, *sticky_footer_lines, footer], 0, 0)

    if middle_budget <= 0:
        return ([*header_lines, *active_row_lines, *sticky_footer_lines, footer], 0, 0)

    avail_items = max(0, middle_budget - 1)
    total_cand = len(candidate_middle)
    if avail_items <= 0 or total_cand == 0:
        max_offset = 0
        eff_offset = 0
        visible_slice: list[str] = []
    else:
        max_offset = total_cand - 1
        acc = 0
        for idx_back in range(total_cand - 1, -1, -1):
            r = _wrapped_row_count(candidate_middle[idx_back], safe_w)
            if acc + r > avail_items:
                break
            acc += r
            max_offset = idx_back
        eff_offset = max(0, min(int(scroll_offset), max_offset))
        visible_slice = []
        used_m = 0
        for ln in candidate_middle[eff_offset:]:
            r = _wrapped_row_count(ln, safe_w)
            if used_m + r > avail_items:
                break
            visible_slice.append(ln)
            used_m += r

    total_orig = len(middle_row_lines)
    hidden_total = max(0, total_orig - len(visible_slice))
    mode_hint = "[p] full" if compact_pending else "[p] compact"
    if visible_slice:
        start_1 = eff_offset + 1
        end_1 = eff_offset + len(visible_slice)
        ind_text = (
            f"  … (+{hidden_total} pending · rows {start_1}-{end_1}/{total_orig} · [j/k] scroll · {mode_hint})"
        )
    else:
        ind_text = f"  … (+{hidden_total} pending · [j/k] scroll · {mode_hint} · enlarge terminal)"
    indicator_line = _color(_trunc(ind_text, safe_w), "2;36", color_on)
    return (
        [*header_lines, *active_row_lines, *visible_slice, indicator_line, *sticky_footer_lines, footer],
        eff_offset,
        max_offset,
    )


def _compose_priority_viewport(
    header_lines: list[str],
    active_row_lines: list[str],
    middle_row_lines: list[str],
    sticky_footer_lines: list[str],
    term_w: int,
    term_h: int,
    footer: str,
    scroll_offset: int = 0,
    compact_pending: bool = False,
) -> list[str]:
    """Public priority-aware viewport composer keeping sticky headers and PR/HITL/OPT footers visible."""
    lines, _, _ = _compose_priority_viewport_with_meta(
        header_lines,
        active_row_lines,
        middle_row_lines,
        sticky_footer_lines,
        term_w,
        term_h,
        footer,
        scroll_offset=scroll_offset,
        compact_pending=compact_pending,
    )
    return lines


def _handle_board_hotkey(args: argparse.Namespace, state_dir: Path) -> str:
    """Sync the configured board artifact (forcing a fresh write) and focus Antigravity Desktop cleanly."""
    board_path = _resolve_board_artifact_path(state_dir, getattr(args, "write_board", ""))
    if board_path is None:
        return "No board path configured (pass --write-board PATH or set dev_queue_board_path.txt)"
    render(args, force_board_write=True)
    _focus_antigravity_app()
    return f"✓ Board synced ({board_path.name}) · Antigravity focused"


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
    runner = doctor_fn if doctor_fn is not None else _invoke_queue_doctor
    color_on = (not bool(getattr(args, "no_color", False))) and not bool(os.environ.get("NO_COLOR"))
    repos_dir = str(getattr(args, "repos_dir", "") or "")

    try:
        if tty_fd is not None and orig_termios is not None:
            with contextlib.suppress(Exception):
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
                with contextlib.suppress(Exception):
                    tty.setcbreak(tty_fd)
            try:
                ready, _, _ = select.select([tty_fd], [], [], max(0.05, float(prompt_timeout_seconds)))
                if ready:
                    choice = _read_watch_key(tty_fd)
            except (InterruptedError, OSError):
                choice = ""
            finally:
                if orig_termios is not None:
                    with contextlib.suppress(Exception):
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
            with contextlib.suppress(Exception):
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
    if first in ("O", "R", "G"):
        return first
    return first.lower()


_TTY_PUSHBACK_BYTES: dict[int, bytearray] = {}


def _has_tty_pushback(tty_fd: int | None) -> bool:
    return bool(tty_fd is not None and _TTY_PUSHBACK_BYTES.get(tty_fd))


def _read_tty_byte(tty_fd: int) -> bytes:
    buf = _TTY_PUSHBACK_BYTES.get(tty_fd)
    if buf:
        b = bytes([buf.pop(0)])
        if not buf:
            _TTY_PUSHBACK_BYTES.pop(tty_fd, None)
        return b
    return os.read(tty_fd, 1)


def _unread_tty_byte(tty_fd: int, b: bytes) -> None:
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


def _mark_cursor_on_row_line(line: str, color_on: bool) -> str:
    """Inject a zero-width-delta `▸` cursor marker right after the 2-char `#` index column."""
    if len(line) >= 3 and line[2] == " ":
        marker = _color("▸", "1;36", color_on)
        return f"{line[:2]}{marker}{line[3:]}"
    return line


def _apply_live_filter_and_cursor(
    row_records: list[dict[str, Any]],
    *,
    live_filter: str,
    cursor_idx: int,
    compact_pending: bool,
    color_on: bool,
) -> tuple[list[str], list[str], list[dict[str, Any]], int]:
    """Filter captured `row_records` in `<1ms` pure memory and mark `cursor_idx` without disk/Git I/O."""
    q = (live_filter or "").strip().lower()
    matched = [r for r in row_records if (not q or q in str(r.get("search_blob") or ""))]
    active_recs = [r for r in matched if r.get("band") == "active"]
    middle_recs = [r for r in matched if r.get("band") != "active"]
    if compact_pending:
        kept_mid: list[dict[str, Any]] = []
        pend_seen = 0
        for mr in middle_recs:
            eff_u = str(mr.get("effective_status") or "").upper()
            if eff_u == "HITL_BLOCK":
                kept_mid.append(mr)
            elif eff_u == "PENDING":
                if pend_seen < 3:
                    kept_mid.append(mr)
                    pend_seen += 1
            else:
                kept_mid.append(mr)
        nav_middle_recs = kept_mid
    else:
        nav_middle_recs = middle_recs

    nav_records = [*active_recs, *nav_middle_recs]
    if not nav_records:
        return ([], [], [], 0)

    eff_cursor = max(0, min(int(cursor_idx), len(nav_records) - 1))
    selected_obj_id = id(nav_records[eff_cursor])

    act_lines = [
        _mark_cursor_on_row_line(str(r["raw_line"]), color_on) if id(r) == selected_obj_id else str(r["raw_line"])
        for r in active_recs
    ]
    mid_lines = [
        _mark_cursor_on_row_line(str(r["raw_line"]), color_on) if id(r) == selected_obj_id else str(r["raw_line"])
        for r in middle_recs
    ]
    return (act_lines, mid_lines, nav_records, eff_cursor)


def _render_detail_inspector_lines(
    rec: dict[str, Any] | None,
    term_w: int,
    color_on: bool,
    use_osc8: bool,
) -> list[str]:
    """Render pure in-memory `[Enter]` Detail Inspector panel exposing item & live worker/reviewer telemetry."""
    bar_w = min(max(78, term_w), 220)
    lines: list[str] = [
        _color("═" * bar_w, "2;36", color_on),
        f"{_color('ITEM DETAIL INSPECTOR', '1;36', color_on)}  "
        f"{_color('([Enter]/[Esc] close · [j/k/PgUp/PgDn] scroll · [o] open PR)', '2', color_on)}",
        _color("─" * bar_w, "2", color_on),
    ]
    if not rec:
        lines.append(_color("  No queue item selected.", "2", color_on))
        lines.append(_color("═" * bar_w, "2;36", color_on))
        return lines

    it = rec.get("item") if isinstance(rec.get("item"), dict) else {}
    cid = str(it.get("conversation_id") or "")
    eff_st = str(rec.get("effective_status") or it.get("status") or "pending").upper()
    raw_st = str(it.get("status") or "pending").upper()
    prio = it.get("priority", 99)
    eff_prio = it.get("effective_priority", prio)
    inh_from = str(it.get("inherited_priority_from") or "").strip()
    prio_str = f"P{prio}" + (f" (effective P{eff_prio} via {inh_from[:8]})" if eff_prio != prio or inh_from else "")

    lines.append(f"  {_color('ID:', '1', color_on)}           {cid}")
    if rec.get("sidebar_title"):
        lines.append(f"  {_color('Sidebar:', '1', color_on)}      {rec['sidebar_title']}")
    lines.append(f"  {_color('Title:', '1', color_on)}        {it.get('title') or '-'}")
    st_txt = eff_st if eff_st == raw_st else f"{eff_st} (queue: {raw_st})"
    lines.append(f"  {_color('Status:', '1', color_on)}       {st_txt}  ·  {_color('Priority:', '1', color_on)} {prio_str}")

    repos = [str(r) for r in (it.get("repo_slugs") or []) if r]
    merged = [str(r) for r in (it.get("merged_repos") or []) if r]
    lines.append(f"  {_color('Target Repos:', '1', color_on)} {', '.join(repos) if repos else '-'}")
    lines.append(f"  {_color('Merged Repos:', '1', color_on)} {', '.join(merged) if merged else 'none'}")

    deps = [str(d) for d in (it.get("depends_on") or []) if str(d).strip()]
    if deps:
        lines.append(f"  {_color('Depends On:', '1', color_on)}   {', '.join(deps)}")
    branches = [str(b) for b in (it.get("branch_names") or []) if str(b).strip()]
    if branches:
        lines.append(f"  {_color('Branches:', '1', color_on)}     {', '.join(branches)}")

    linked_locks = rec.get("linked_locks") if isinstance(rec.get("linked_locks"), list) else []
    lines.append(_color("─" * bar_w, "2", color_on))
    lines.append(_color("  LIVE LOCK & WORKER TELEMETRY:", "1;33", color_on))
    if linked_locks:
        for lk_rec in linked_locks:
            idle_s = int(lk_rec.get("idle_seconds") or 0)
            idle_l = str(lk_rec.get("idle_label") or "0s")
            elap_s = int(lk_rec.get("elapsed_seconds") or 0)
            elap_l = str(lk_rec.get("elapsed_label") or "0s")
            src = str(lk_rec.get("last_activity_source") or "none")
            stg = str(lk_rec.get("stage_label") or "-")
            w_cid = str(lk_rec.get("worker_cid") or "-")
            r_cid = str(lk_rec.get("reviewer_subagent_id") or "-")
            idle_state = str(lk_rec.get("idle_state") or "")
            sem_code = "1;31" if lk_rec.get("is_stalled") else ("1;33" if idle_state == "amber" else "0;32")
            sem_lbl = (
                f"STALL {idle_l} ({idle_s}s)"
                if lk_rec.get("is_stalled")
                else (f"idle {idle_l} ({idle_s}s)" if idle_state == "amber" else f"ok·{idle_l} ({idle_s}s)")
            )
            lines.append(
                f"    • {_color(str(lk_rec.get('repo_slug') or ''), '1', color_on)} "
                f"[Slot #{lk_rec.get('slot_id', 1)}] status={lk_rec.get('status') or '-'}"
            )
            lines.append(
                f"      inactivity={_color(sem_lbl, sem_code, color_on)}  ·  "
                f"elapsed={elap_l} ({elap_s}s)  ·  source={src}  ·  stage={stg}"
            )
            lines.append(f"      worker_cid={w_cid}  ·  reviewer_subagent_id={r_cid}")
            if lk_rec.get("worktree_path"):
                lines.append(f"      worktree={lk_rec['worktree_path']}")
    else:
        lines.append(_color("    No active repository lock linked to this item.", "2", color_on))

    tfiles = [str(f) for f in (it.get("target_files") or []) if str(f).strip()]
    if tfiles:
        lines.append(_color("─" * bar_w, "2", color_on))
        lines.append(_color(f"  TARGET FILES ({len(tfiles)}):", "1", color_on))
        for tf in tfiles[:12]:
            lines.append(f"    - {tf}")
        if len(tfiles) > 12:
            lines.append(f"    … (+{len(tfiles) - 12} more)")

    for label_hdr, field_key in (("RCA / RATIONALE", "rca_summary"), ("PROPOSED FIX", "proposed_fix"), ("NOTES", "notes")):
        val = str(it.get(field_key) or "").strip()
        if val:
            lines.append(_color("─" * bar_w, "2", color_on))
            lines.append(_color(f"  {label_hdr}:", "1", color_on))
            lines.append(f"    {_trunc(' '.join(val.split()), max(40, bar_w - 6))}")

    pr_u = str(rec.get("pr_url") or "").strip()
    if pr_u:
        lines.append(_color("─" * bar_w, "2", color_on))
        lines.append(_color("  VERIFIED PR LINK:", "1;32", color_on))
        disp_u = _compact_osc8_pr_label(pr_u, max_width=max(40, bar_w - 6)) if use_osc8 else (_clean_clickable_url(pr_u) or pr_u)
        lines.append(f"    {_osc8_link(disp_u, pr_u, use_osc8)}")

    lines.append(_color("═" * bar_w, "2;36", color_on))
    return lines


def _render_optimizer_overlay_lines(
    analysis: dict[str, Any],
    term_w: int,
    color_on: bool,
) -> list[str]:
    """Render human-readable `queue-optimize` findings for `[x]` overlay and `agy-queue optimize`."""
    bar_w = min(max(78, term_w), 220)
    score = int(analysis.get("health_score") if analysis.get("health_score") is not None else 100)
    findings = analysis.get("findings") if isinstance(analysis.get("findings"), list) else []
    score_code = "1;32" if score >= 90 else ("1;33" if score >= 70 else "1;31")
    lines: list[str] = [
        _color("═" * bar_w, "2;36", color_on),
        f"{_color('DEV FLOW OPTIMIZER', '1;36', color_on)}  "
        f"health={_color(f'{score}/100', score_code, color_on)}  "
        f"findings={len(findings)}",
        _color("─" * bar_w, "2", color_on),
    ]
    if not findings:
        lines.append(_color("  ✓ Zero workflow bottlenecks detected — queue flow is healthy.", "0;32", color_on))
    else:
        sev_colors = {"critical": "1;31", "high": "1;33", "medium": "1;36", "low": "2"}
        for idx, f in enumerate(findings, 1):
            if not isinstance(f, dict):
                continue
            sev = str(f.get("severity") or "low").lower()
            sev_badge = _color(f"[{sev.upper()}]", sev_colors.get(sev, "0"), color_on)
            title = str(f.get("title") or f.get("code") or "")
            sav = int(f.get("estimated_savings_min") or 0)
            sav_txt = f" (~{sav}m saved)" if sav > 0 and f"~{sav}m" not in title else ""
            lines.append(f"  {idx}. {sev_badge} {_color(title + sav_txt, '1', color_on)}")
            rec_txt = str(f.get("recommendation") or "").strip()
            if rec_txt:
                lines.append(f"     {_trunc(rec_txt, max(40, bar_w - 6))}")
            hint = str(f.get("auto_action_hint") or "").strip()
            if hint:
                lines.append(f"     {_color('→ ' + _trunc(hint, max(40, bar_w - 8)), '2;36', color_on)}")
    lines.append(_color("═" * bar_w, "2;36", color_on))
    return lines


def _render_help_overlay_lines(term_w: int, color_on: bool) -> list[str]:
    """Render `[?]` hotkey & inactivity semaphore reference overlay."""
    bar_w = min(max(78, term_w), 220)
    return [
        _color("═" * bar_w, "2;36", color_on),
        f"{_color('AGY-QUEUE INTERACTIVE HELP & SEMAPHORE LEGEND', '1;36', color_on)}  "
        f"{_color('([?]/[Esc]/[Enter] close)', '2', color_on)}",
        _color("─" * bar_w, "2", color_on),
        _color("  NAVIGATION & INSPECTOR (<1ms zero-I/O viewport repaint):", "1;33", color_on),
        "    j / ↓ · k / ↑     Move row cursor down / up (auto-scrolls pending queue)",
        "    PgDn / PgUp       Page down / up across long queues or overlays",
        "    g / Home · G / End Jump to first / last queue row",
        "    Enter             Open Detail Inspector (worker/reviewer IDs, idle/elapsed/source, files, RCA)",
        "    /                 Live substring filter across ID, repo, status, branch, and title (Esc clears)",
        "    x                 Toggle 0-LLM Dev Flow Optimizer view",
        "    Ctrl+L            Force full terminal clear and viewport redraw",
        _color("─" * bar_w, "2", color_on),
        _color("  ACTIONS & TOGGLES:", "1;33", color_on),
        "    o / O / 1-9       Open first ready PR / all ready PRs / row #N link in default browser",
        "    h + 1-9           Jump via Dev Queue Sidecar Panel (nav_intent) + clipboard/focus fallback",
        "    d                 Run interactive agy-doctor check ([w] wake · [f] safe fix)",
        "    P                 Toggle Pause / Continue orchestration (Graceful Drain: finish ongoing, 0 new)",
        "    p / a             Toggle [compact] pending rows (<=3) / [all] inactive items",
        "    s / b             Trigger async background Git fetch+reconcile / force-sync board.md",
        "    r / R / q         Refresh snapshot / safe syntax-checked reload / quit",
        _color("─" * bar_w, "2", color_on),
        _color("  LIVE INACTIVITY SEMAPHORE (DEVELOPING · NEEDS_REVISION_ITERATING · AWAITING_AGENT_REVIEW):", "1;33", color_on),
        f"    {_color('ok·<idle>', '0;32', color_on)}       Active (<3m since newest worker/reviewer transcript, worktree, or heartbeat)",
        f"    {_color('idle <idle>', '1;33', color_on)}     Early inactivity warning (3m–14m; highlighted in row + REPOS pill)",
        f"    {_color('STALL <idle>', '1;35', color_on)}    Stalled (>=15m default AGY_DEV_STALL_THRESHOLD_SECONDS; triggers alert)",
        _color("═" * bar_w, "2;36", color_on),
    ]


def _build_watch_footer(
    *,
    interval: float,
    hitl_count: int,
    compact_pending: bool,
    show_all: bool,
    live_filter: str,
    filter_input_active: bool,
    overlay_mode: str,
    awaiting_hitl_digit: bool,
    sync_in_flight: bool,
    status_msg: str,
    orchestration_paused: bool = False,
) -> str:
    """Compose compact 1-line watch footer with conditional `[h+#]` and persistent toggle badges."""
    if filter_input_active:
        return f"Filter: {live_filter}█  ([Enter] apply · [Esc] clear · [Backspace] edit)"
    if awaiting_hitl_digit:
        return "Press HITL number [1-9] -> copy exact title & focus Antigravity App (Esc cancels)…"
    if overlay_mode in ("detail", "optimizer", "help"):
        return f"[{overlay_mode.upper()}]  [j/k/PgUp/PgDn/g/G] scroll · [Esc/Enter] back to queue · [q] close"

    toggles: list[str] = []
    if orchestration_paused:
        toggles.append("[PAUSED]")
    if compact_pending:
        toggles.append("[compact]")
    if show_all:
        toggles.append("[all]")
    if live_filter:
        toggles.append(f"[filter:{_trunc(live_filter, 16)}]")
    toggle_prefix = (" ".join(toggles) + "  ") if toggles else ""

    hitl_seg = " · [h+#] Agy" if hitl_count > 0 else ""
    pause_hint = "[P] resume" if orchestration_paused else "[P] pause"
    core_keys = (
        f"[Enter] inspect · [o/O] PR · [1-9] open{hitl_seg} · [/] filter · {pause_hint} · [d] doctor · "
        f"[x] opt · [j/k] scroll · [p] compact · [a] all · [s] sync · [?] help · [R] reload · [q] quit"
    )
    if sync_in_flight and (not status_msg or status_msg == "↻ Syncing…"):
        return f"↻ Syncing…  |  {toggle_prefix}{core_keys}"
    if status_msg:
        return f"{status_msg}  |  {toggle_prefix}{core_keys}"
    return f"{toggle_prefix}{core_keys} ({interval:g}s)"


def _normalize_status_arg(raw: str) -> str:
    val = str(raw or "").strip().lower()
    if not val:
        return ""
    if val not in VALID_QUEUE_STATUSES:
        valid_str = ", ".join(VALID_QUEUE_STATUSES)
        raise argparse.ArgumentTypeError(f"invalid status {raw!r} (choose from: {valid_str})")
    return val


def _build_parser() -> argparse.ArgumentParser:
    epilog_txt = (
        "Unified subcommands:\n"
        "  agy-queue                     Render live multi-repo queue dashboard\n"
        "  agy-queue watch [SECS]        Flicker-free interactive TUI (--watch)\n"
        "  agy-queue doctor [FLAGS]      Run 0-LLM orchestrator doctor (--fix/--wake/--heal)\n"
        "  agy-queue optimize [FLAGS]    Run 0-LLM dev flow bottleneck analyzer\n"
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
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {_resolve_cli_version()}",
    )
    parser.add_argument(
        "-w",
        "--watch",
        nargs="?",
        const=3.0,
        type=float,
        metavar="SECS",
        help="Auto-refresh dashboard in place every N seconds (default: 3; emits NDJSON with --json).",
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Include merged, cleaned, cancelled, and rejected items.",
    )
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

    seq = 0
    try:
        while not stop:
            seq += 1
            line_json, _, _, _, st_dir, _ = render(
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
            locks_cur, _ = _load_locks(st_dir)
            fp_base = _snapshot_state_fingerprint(st_dir, locks_cur)
            deadline = time.monotonic() + interval
            while not stop and time.monotonic() < deadline:
                if _snapshot_state_fingerprint(st_dir, locks_cur) != fp_base:
                    break
                time.sleep(max(0.02, min(0.15, deadline - time.monotonic())))
    except KeyboardInterrupt:
        return 0
    except BrokenPipeError:
        return _silence_broken_pipe_stdout()
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
        except Exception:
            if tty_fd is not None and owns_tty_fd:
                with contextlib.suppress(OSError):
                    os.close(tty_fd)
            tty_fd = None
            owns_tty_fd = False

    watched_paths = (
        list(watched_paths_override)
        if watched_paths_override is not None
        else _collect_watched_script_paths()
    )
    baseline_script_fp = _snapshot_file_fingerprints(watched_paths)
    reload_pending_deadline: float | None = None
    effective_exec_fn = exec_fn if exec_fn is not None else os.execv
    preserved_argv = list(raw_argv) if raw_argv is not None else getattr(args, "_raw_argv", None)

    sync_ctrl = _AsyncSyncController(sync_fn=sync_fn)
    last_auto_sync_mono = time.monotonic() if bool(getattr(args, "sync", False)) else 0.0
    repo_cache: dict[str, Any] = {"ttl": 60.0}

    status_msg = ""
    status_until = 0.0
    if os.environ.pop(_RELOAD_ENV_FLAG, "") == "1":
        status_msg = "↻ Auto-reloaded after plugin update"
        status_until = time.monotonic() + 4.0

    awaiting_hitl_digit = False
    filter_input_active = False
    live_filter = ""
    overlay_mode = ""  # "" | "detail" | "optimizer" | "help"
    overlay_scroll = 0
    cached_opt_analysis: dict[str, Any] | None = None
    force_clear_screen = False
    cursor_idx = 0
    scroll_offset = 0
    max_scroll_offset = 0
    compact_pending = False
    iterations = 0
    terminal_cleaned = False
    data_dirty = True

    st_dir = _resolve_state_dir(getattr(args, "state_dir", ""))
    local_dirs: dict[str, Path] = {}
    row_links: dict[int, tuple[str, str]] = {}
    hitl_convs: dict[int, tuple[str, str, str]] = {}
    first_pr: tuple[str, str] | None = None
    all_pr_links: list[tuple[str, str]] = []
    has_active_work = False
    active_locks_map: dict[str, dict[str, Any]] = {}
    layout_data: dict[str, Any] = {}
    body_lines: list[str] = []
    last_good_layout: dict[str, Any] = {}
    last_good_body_lines: list[str] = []
    last_state_fp: tuple[tuple[str, int, int, int], ...] = ()
    deadline = 0.0

    def _cleanup_watch_terminal() -> None:
        nonlocal terminal_cleaned, tty_fd
        if terminal_cleaned:
            return
        terminal_cleaned = True
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
                with contextlib.suppress(Exception):
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
        script_target = watched_paths[0] if watched_paths else Path(__file__).resolve()
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
            if data_dirty:
                completed_sync_msg = sync_ctrl.consume_result()
                if completed_sync_msg:
                    status_msg = completed_sync_msg
                    status_until = time.monotonic() + 4.0

                width_arg = term_size_override[0] if term_size_override is not None else None
                fresh_layout: dict[str, Any] = {}
                sync_badge = sync_ctrl.format_badge()
                pre_render_core_fp = _snapshot_state_fingerprint(st_dir, {})
                try:
                    out, row_links, hitl_convs, first_pr, st_dir, local_dirs = render(
                        args,
                        is_tty_override=use_tty,
                        term_width_override=width_arg,
                        layout_out=fresh_layout,
                        repo_cache=repo_cache,
                        sync_status_badge=sync_badge,
                        strict_io=True,
                    )
                    body_lines = out.splitlines()
                    last_good_body_lines = body_lines
                    if fresh_layout:
                        layout_data = fresh_layout
                        last_good_layout = fresh_layout
                        all_pr_links = list(fresh_layout.get("all_pr_links") or [])
                        has_active_work = bool(fresh_layout.get("has_active_work"))
                        active_locks_map = dict(fresh_layout.get("active_locks") or {})
                        cached_opt_analysis = None
                except (OSError, json.JSONDecodeError, sqlite3.Error) as io_err:
                    err_banner = f"⚠ Transient read error ({io_err.__class__.__name__}) — retrying…"
                    status_msg = err_banner
                    status_until = time.monotonic() + 3.0
                    layout_data = dict(last_good_layout) if last_good_layout else {}
                    body_lines = (
                        list(last_good_body_lines)
                        if last_good_body_lines
                        else [err_banner, f"  {io_err}"]
                    )
                if _snapshot_state_fingerprint(st_dir, {}) != pre_render_core_fp:
                    # State file mutated while render() was in flight -> force immediate next-tick wakeup
                    last_state_fp = pre_render_core_fp
                else:
                    last_state_fp = _snapshot_state_fingerprint(st_dir, active_locks_map)
                deadline = time.monotonic() + interval
                data_dirty = False

            t_paint = time.perf_counter()
            now_mono = time.monotonic()
            if status_msg and now_mono >= status_until:
                status_msg = ""

            if term_size_override is not None:
                cols, rows = term_size_override
            else:
                tsize = shutil.get_terminal_size((124, 40))
                cols, rows = tsize.columns, tsize.lines

            hitl_cnt = int(layout_data.get("hitl_count") or len(hitl_convs))
            footer = _build_watch_footer(
                interval=interval,
                hitl_count=hitl_cnt,
                compact_pending=compact_pending,
                show_all=bool(getattr(args, "all", False)),
                live_filter=live_filter,
                filter_input_active=filter_input_active,
                overlay_mode=overlay_mode,
                awaiting_hitl_digit=awaiting_hitl_digit,
                sync_in_flight=sync_ctrl.is_in_flight(),
                status_msg=status_msg,
                orchestration_paused=bool(layout_data.get("orchestration_paused", False)),
            )

            color_on = bool(layout_data.get("color_on", False))
            use_osc8 = bool(layout_data.get("use_osc8", False))
            raw_recs = list(layout_data.get("row_records") or [])
            act_lines, mid_lines, nav_records, cursor_idx = _apply_live_filter_and_cursor(
                raw_recs,
                live_filter=live_filter,
                cursor_idx=cursor_idx,
                compact_pending=compact_pending,
                color_on=color_on,
            )
            nav_total = len(nav_records)

            if use_tty:
                clear_prefix = "\033[2J\033[H" if force_clear_screen else ""
                force_clear_screen = False
                if overlay_mode in ("detail", "optimizer", "help"):
                    if overlay_mode == "detail":
                        sel_rec = nav_records[cursor_idx] if (0 <= cursor_idx < nav_total) else None
                        ov_lines = _render_detail_inspector_lines(sel_rec, cols, color_on, use_osc8)
                    elif overlay_mode == "optimizer":
                        if cached_opt_analysis is None:
                            if _flow_opt is not None and hasattr(_flow_opt, "analyze_dev_flow_optimizations"):
                                cached_opt_analysis = dict(
                                    _flow_opt.analyze_dev_flow_optimizations(
                                        lightweight=True,
                                        preloaded_items=list(layout_data.get("items_snapshot") or []),
                                        preloaded_active_locks=dict(layout_data.get("active_locks") or {}),
                                        preloaded_all_locks=dict(layout_data.get("all_locks") or {}),
                                        preloaded_repo_dirs=dict(layout_data.get("repo_local_dirs") or {}),
                                    )
                                )
                            else:
                                cached_opt_analysis = {"health_score": 100, "findings": []}
                        ov_lines = _render_optimizer_overlay_lines(cached_opt_analysis, cols, color_on)
                    else:
                        ov_lines = _render_help_overlay_lines(cols, color_on)
                    max_ov_scroll = max(0, len(ov_lines) - max(3, rows - 2))
                    overlay_scroll = max(0, min(overlay_scroll, max_ov_scroll))
                    sliced_ov = ov_lines[overlay_scroll:]
                    out_stream.write(clear_prefix + _build_watch_frame(sliced_ov, cols, rows, footer))
                elif layout_data:
                    if live_filter and not act_lines and not mid_lines:
                        mid_lines = [_color(f"  No rows matching filter {live_filter!r} (press Esc to clear).", "2", color_on)]
                    vp_lines, scroll_offset, max_scroll_offset = _compose_priority_viewport_with_meta(
                        layout_data.get("header_lines") or [],
                        act_lines,
                        mid_lines,
                        layout_data.get("sticky_footer_lines") or [],
                        cols,
                        rows,
                        footer,
                        scroll_offset=scroll_offset,
                        compact_pending=compact_pending,
                    )
                    out_stream.write(
                        clear_prefix + _build_watch_frame(body_lines, cols, rows, footer, preclamped_lines=vp_lines)
                    )
                else:
                    out_stream.write(clear_prefix + _build_watch_frame(body_lines, cols, rows, footer))
            else:
                out_stream.write("\n".join([*body_lines, footer]) + "\n")
            out_stream.flush()
            _debug_log(
                bool(getattr(args, "debug", False)),
                f"viewport repaint in {(time.perf_counter() - t_paint) * 1000.0:.3f}ms",
            )

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break

            while not stop:
                cur_mono = time.monotonic()
                if cur_mono >= deadline:
                    data_dirty = True
                    break

                if sigwinch_received:
                    sigwinch_received = False
                    data_dirty = True
                    break

                if sync_ctrl.has_unconsumed_result():
                    data_dirty = True
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
                        status_msg = f"⚠ {err_txt}"
                        status_until = time.monotonic() + 5.0
                        break
                else:
                    reload_pending_deadline = None

                auto_sync_allowed = (
                    not no_auto_sync
                    and sync_interval > 0.0
                    and os.environ.get("AGY_OFFLINE_ONLY") != "1"
                    and use_tty
                    and has_active_work
                )
                if (
                    auto_sync_allowed
                    and not sync_ctrl.is_in_flight()
                    and (last_auto_sync_mono == 0.0 or (cur_mono - last_auto_sync_mono) >= sync_interval)
                ):
                    last_auto_sync_mono = cur_mono
                    if sync_ctrl.trigger(st_dir, local_dirs):
                        break

                if _snapshot_state_fingerprint(st_dir, active_locks_map) != last_state_fp:
                    data_dirty = True
                    break

                remaining = max(0.02, min(0.15, deadline - time.monotonic()))
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
                    key = _read_watch_key(tty_fd, raw_mode=filter_input_active)
                except OSError:
                    continue
                if not key:
                    if tty_fd_override is not None:
                        tty_fd = None
                    continue

                if filter_input_active:
                    if key == "ENTER":
                        filter_input_active = False
                        cursor_idx = 0
                        scroll_offset = 0
                    elif key == "ESC":
                        filter_input_active = False
                        live_filter = ""
                        cursor_idx = 0
                        scroll_offset = 0
                    elif key == "BACKSPACE":
                        live_filter = live_filter[:-1]
                        cursor_idx = 0
                        scroll_offset = 0
                    elif key == "CTRL_L":
                        force_clear_screen = True
                    elif len(key) == 1 and key.isprintable():
                        live_filter += key
                        cursor_idx = 0
                        scroll_offset = 0
                    break

                if awaiting_hitl_digit:
                    awaiting_hitl_digit = False
                    if key.isdigit() and key != "0":
                        h_idx = int(key)
                        entry = hitl_convs.get(h_idx)
                        if entry:
                            cid_disp, cid_full, ag_title = entry
                            nav_res = _trigger_hitl_navigation(
                                st_dir,
                                index=h_idx,
                                cid_disp=cid_disp,
                                cid_full=cid_full,
                                ag_title=ag_title,
                            )
                            if nav_res.get("acknowledged"):
                                status_msg = f"✓ Dev Queue Panel switched to '{_trunc(ag_title, 34)}' ({cid_disp})"
                            elif nav_res.get("copied"):
                                status_msg = (
                                    f"✓ nav_intent + Antigravity focused · copied '{_trunc(ag_title, 30)}' ({cid_disp})"
                                )
                            else:
                                status_msg = f"✓ nav_intent + Antigravity focused ({cid_disp})"
                        else:
                            status_msg = f"No [h{key}] item"
                        status_until = time.monotonic() + 4.0
                        break
                    status_msg = "Cancelled"
                    status_until = time.monotonic() + 1.5
                    break

                if overlay_mode:
                    page_step = max(3, rows - 6)
                    if key in ("ESC", "ENTER", "q") or (overlay_mode == "optimizer" and key == "x") or (overlay_mode == "help" and key == "?"):
                        overlay_mode = ""
                        overlay_scroll = 0
                    elif key in ("j", "DOWN"):
                        overlay_scroll += 1
                    elif key in ("k", "UP"):
                        overlay_scroll = max(0, overlay_scroll - 1)
                    elif key == "PGDN":
                        overlay_scroll += page_step
                    elif key == "PGUP":
                        overlay_scroll = max(0, overlay_scroll - page_step)
                    elif key in ("g", "HOME"):
                        overlay_scroll = 0
                    elif key in ("G", "END"):
                        overlay_scroll = 9999
                    elif key == "CTRL_L":
                        force_clear_screen = True
                    elif overlay_mode == "detail" and key == "o":
                        sel_rec = nav_records[cursor_idx] if (0 <= cursor_idx < nav_total) else None
                        pr_target = str((sel_rec or {}).get("pr_url") or "") or (first_pr[1] if first_pr else "")
                        if pr_target:
                            ok = _open_url_in_browser(pr_target)
                            status_msg = "✓ Opening PR" if ok else "✗ Failed opening PR"
                        else:
                            status_msg = "No human-ready PR on selected item"
                        status_until = time.monotonic() + 3.0
                        overlay_mode = ""
                    break

                if key == "q":
                    stop = True
                    break
                if key == "ESC":
                    if live_filter:
                        live_filter = ""
                        cursor_idx = 0
                        scroll_offset = 0
                        status_msg = "Filter cleared"
                        status_until = time.monotonic() + 2.0
                    break
                if key == "CTRL_L":
                    force_clear_screen = True
                    break
                if key == "/":
                    filter_input_active = True
                    break
                if key == "ENTER":
                    overlay_mode = "detail"
                    overlay_scroll = 0
                    break
                if key == "x":
                    overlay_mode = "optimizer"
                    overlay_scroll = 0
                    break
                if key == "?":
                    overlay_mode = "help"
                    overlay_scroll = 0
                    break
                if key == "h":
                    if not hitl_convs:
                        status_msg = "No pending HITL conversations"
                        status_until = time.monotonic() + 2.5
                    else:
                        awaiting_hitl_digit = True
                    break
                if key == "b":
                    status_msg = _handle_board_hotkey(args, st_dir)
                    status_until = time.monotonic() + 3.5
                    data_dirty = True
                    break
                if key == "d":
                    status_msg = _handle_doctor_hotkey(
                        args,
                        st_dir,
                        out_stream=out_stream,
                        use_tty=use_tty,
                        tty_fd=tty_fd,
                        orig_termios=orig_termios,
                    )
                    status_until = time.monotonic() + 4.5
                    data_dirty = True
                    break
                if key == "r":
                    status_msg = "↻ Refreshed"
                    status_until = time.monotonic() + 2.0
                    data_dirty = True
                    break
                if key == "R":
                    reloaded, err_txt = _attempt_safe_reload()
                    if reloaded:
                        return 0
                    status_msg = f"⚠ {err_txt}"
                    status_until = time.monotonic() + 5.0
                    break
                if key == "s":
                    last_auto_sync_mono = time.monotonic()
                    if sync_ctrl.trigger(st_dir, local_dirs):
                        status_msg = "↻ Syncing…"
                        status_until = time.monotonic() + 30.0
                    else:
                        status_msg = "↻ Sync already in progress…"
                        status_until = time.monotonic() + 2.5
                    break
                if key == "a":
                    args.all = not args.all
                    cursor_idx = 0
                    scroll_offset = 0
                    status_msg = f"Merged view: {'ON' if args.all else 'OFF'}"
                    status_until = time.monotonic() + 2.5
                    data_dirty = True
                    break
                if key == "p":
                    compact_pending = not compact_pending
                    cursor_idx = 0
                    scroll_offset = 0
                    status_msg = f"Pending view: {'COMPACT' if compact_pending else 'FULL'}"
                    status_until = time.monotonic() + 2.5
                    break
                if key == "P":
                    oc_mod = _get_orch_control()
                    if oc_mod is None:
                        status_msg = "⚠ orchestration_control module unavailable"
                    else:
                        cur_paused = bool(oc_mod.is_orchestration_paused(state_dir=st_dir))
                        if cur_paused:
                            oc_mod.resume_orchestration(state_dir=st_dir, source="tui", request_wake=True)
                            status_msg = "▶ Orchestration resumed — waking orchestrator…"
                        else:
                            res_p = oc_mod.pause_orchestration(state_dir=st_dir, source="tui")
                            cur_locks, _ = _load_locks(st_dir)
                            phase_info = oc_mod.summarize_orchestration_phase(res_p.get("control"), cur_locks)
                            status_msg = f"⏸ {phase_info.get('banner_text') or 'PAUSED — 0 new dispatches'}"
                    status_until = time.monotonic() + 4.5
                    data_dirty = True
                    break
                if key in ("j", "DOWN"):
                    if nav_total > 0:
                        cursor_idx = min(nav_total - 1, cursor_idx + 1)
                    scroll_offset = min(max_scroll_offset, scroll_offset + 1) if cursor_idx >= len(act_lines) else scroll_offset
                    break
                if key in ("k", "UP"):
                    if nav_total > 0:
                        cursor_idx = max(0, cursor_idx - 1)
                    if cursor_idx < len(act_lines):
                        scroll_offset = 0
                    else:
                        scroll_offset = max(0, scroll_offset - 1)
                    break
                if key == "PGDN":
                    p_step = max(3, rows // 2)
                    if nav_total > 0:
                        cursor_idx = min(nav_total - 1, cursor_idx + p_step)
                    scroll_offset = min(max_scroll_offset, scroll_offset + p_step)
                    break
                if key == "PGUP":
                    p_step = max(3, rows // 2)
                    if nav_total > 0:
                        cursor_idx = max(0, cursor_idx - p_step)
                    scroll_offset = max(0, scroll_offset - p_step)
                    break
                if key in ("g", "HOME"):
                    cursor_idx = 0
                    scroll_offset = 0
                    break
                if key in ("G", "END"):
                    if nav_total > 0:
                        cursor_idx = nav_total - 1
                    scroll_offset = max_scroll_offset
                    break
                if key == "o":
                    if first_pr:
                        lbl, url = first_pr
                        ok = _open_url_in_browser(url)
                        status_msg = f"✓ Opening {lbl}" if ok else f"✗ Failed opening {lbl}"
                    else:
                        status_msg = "No human-ready PR available"
                    status_until = time.monotonic() + 3.0
                    break
                if key == "O":
                    if all_pr_links:
                        opened = sum(1 for _, u in all_pr_links if _open_url_in_browser(u))
                        status_msg = (
                            f"✓ Opening {opened} ready PR(s)"
                            if opened == len(all_pr_links)
                            else f"⚠ Opened {opened}/{len(all_pr_links)} PR(s)"
                        )
                    else:
                        status_msg = "No human-ready PR available"
                    status_until = time.monotonic() + 3.0
                    break
                if key.isdigit() and key != "0":
                    row_num = int(key)
                    target = row_links.get(row_num)
                    if target:
                        lbl, url = target
                        ok = _open_url_in_browser(url)
                        status_msg = f"✓ Opening #{row_num} {lbl}" if ok else f"✗ Failed opening #{row_num}"
                    else:
                        status_msg = f"Row #{row_num} has no active link"
                    status_until = time.monotonic() + 3.0
                    break
    except KeyboardInterrupt:
        return 0
    finally:
        _cleanup_watch_terminal()
    return 0


def _run_subcommand_optimize(argv: list[str]) -> int:
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
    _, local_dirs = _discover_repos(all_locks, q_data.get("items") or [], args.repos_dir)
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


def _run_subcommand_sync(argv: list[str]) -> int:
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
    _, local_dirs = _discover_repos(all_locks, q_data.get("items") or [], args.repos_dir)
    offline_opt = True if args.offline else None
    msg = _run_live_sync(st_dir, local_dirs, offline=offline_opt)
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
    render(render_ns, layout_out=layout)
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
        occ = str(info.get("slot_occupancy") or "1/2")
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
    if args.json:
        print((proc.stdout or "{}").strip())
    else:
        print(f"✓ Updated {resolved_id[:8]} -> {raw_status}")
    return 0


def _run_subcommand_pause(argv: list[str]) -> int:
    """Handle `agy-queue pause [--reason ...] [--state-dir DIR] [--json]`."""
    parser = argparse.ArgumentParser(
        prog="agy-queue pause",
        description="Pause orchestration (Graceful Drain: finish ongoing active-lock tasks, dispatch zero new tasks).",
    )
    parser.add_argument("--reason", default="", help="Optional short Zero-PII reason (e.g. 'Customer focus').")
    parser.add_argument("--state-dir", default="", help="Optional state directory override.")
    parser.add_argument("--json", action="store_true", help="Output JSON payload.")
    ns = parser.parse_args(argv)
    st_dir = _resolve_state_dir(ns.state_dir)
    oc_mod = _get_orch_control()
    if oc_mod is None:
        print("Error: orchestration_control module not found.", file=sys.stderr)
        return 1
    res = oc_mod.pause_orchestration(state_dir=st_dir, reason=ns.reason, source="cli")
    locks, _ = _load_locks(st_dir)
    summary = oc_mod.summarize_orchestration_phase(res.get("control"), locks)
    out_payload = {
        "status": "ok",
        "paused": True,
        "changed": bool(res.get("changed")),
        "orchestration": summary,
    }
    if ns.json:
        print(json.dumps(out_payload, indent=2, ensure_ascii=False))
    else:
        print(f"⏸ {summary.get('banner_text') or 'PAUSED — 0 new dispatches · Watchdog silent'}")
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
        if summary.get("paused"):
            print(f"⏸ {summary.get('banner_text') or 'PAUSED — 0 new dispatches · Watchdog silent'}")
        else:
            ongoing = int(summary.get("ongoing_count") or 0)
            print(f"▶ ACTIVE — {ongoing} ongoing task(s) · new dispatches enabled")
    return 0


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
                print(
                    f"✓ Dev Queue Panel opened '{ag_title}' ({cid_disp}) in Antigravity Desktop"
                )
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
