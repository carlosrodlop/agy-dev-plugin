"""Atomic SHA-256 `dev_queue_board.md` writer and desktop HITL navigation helpers (`tui_board.py`)."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
from pathlib import Path
import platform
import shutil
import subprocess
from typing import Any
import webbrowser

from tui_models import (
    _BOARD_UPDATED_LINE_RE,
    IDLE_AMBER_THRESHOLD_SECONDS,
    _facade_attr,
)
from tui_progress import (
    _clean_clickable_url,
    _collect_lock_extra_urls,
    _repo_pulls_url,
    _resolve_lock_human_pr_link,
    _sanitize_full_url,
)
from tui_state import (
    _is_uuid,
    _lock_slot_occupancy,
    _resolve_board_artifact_path,
    _resolve_nav_intent_ack_wait_seconds,
    _secondary_slot_progress,
    _slot_label,
    _wait_for_nav_intent_ack,
    write_nav_intent,
)

_BOARD_DIGEST_CACHE: dict[str, tuple[int, int, str]] = {}


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
    except (OSError, subprocess.SubprocessError):
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
    except OSError:
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
    except (OSError, webbrowser.Error):
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

    focused = _facade_attr("_focus_antigravity_app", _focus_antigravity_app)()
    wait_s = _resolve_nav_intent_ack_wait_seconds(ack_wait_seconds)
    acknowledged = bool(intent_written and intent_id and _wait_for_nav_intent_ack(state_dir, intent_id, wait_s))
    copied = False
    if not acknowledged:
        copied = _facade_attr("_copy_to_clipboard", _copy_to_clipboard)(ag_title)
    return {
        "intent_written": intent_written,
        "intent_id": intent_id,
        "acknowledged": acknowledged,
        "copied": copied,
        "focused": focused,
    }


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
            f"`Pending: {counts.get('pending', 0)}` · `HITL: {max(counts.get('hitl_blocked', 0), len(hitl_rows))}` · "
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
        lock_link_fn = _facade_attr("_resolve_lock_human_pr_link", _resolve_lock_human_pr_link)
        for slug in managed_list:
            lbl = label_map.get(slug, slug)
            lk = locks.get(slug)
            if lk:
                occ = _lock_slot_occupancy(lk)
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
                for sn_id, sn_lp in _secondary_slot_progress(slug, lock_progress_cache):
                    if sn_lp.get("is_awaiting_base"):
                        sn_tag = "READY_AWAITING_BASE_MERGE"
                    elif sn_lp.get("is_needs_rebase"):
                        sn_tag = "NEEDS_REBASE_REVERIFY"
                    else:
                        sn_tag = str(sn_lp.get("tag") or f"{int(sn_lp.get('pct') or 0)}%")  # type: ignore[arg-type]
                    tag = f"{tag} · {_slot_label(sn_id)}:{sn_tag}"
                _, u, lock_has_pr, _ = lock_link_fn(
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
    except (OSError, TypeError, ValueError):
        return False


def _handle_board_hotkey(args: argparse.Namespace, state_dir: Path) -> str:
    """Sync the configured board artifact (forcing a fresh write) and focus Antigravity Desktop cleanly."""
    board_path = _resolve_board_artifact_path(state_dir, getattr(args, "write_board", ""))
    if board_path is None:
        return "No board path configured (pass --write-board PATH or set dev_queue_board_path.txt)"
    render_fn = _facade_attr("render", None)
    if render_fn is not None:
        render_fn(args, force_board_write=True)
    _facade_attr("_focus_antigravity_app", _focus_antigravity_app)()
    return f"✓ Board synced ({board_path.name}) · Antigravity focused"
