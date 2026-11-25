"""Unicode cell-width layout, dashboard renderer, priority viewport composer, and TUI overlays (`tui_render.py`)."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import contextlib
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any, TextIO
import unicodedata
from urllib.parse import urlsplit

from tui_board import _escape_md_url, _sanitize_md_table_cell, _write_live_antigravity_board
from tui_models import (
    _STATUS_STYLES,
    ANSI_RE,
    IDLE_AMBER_THRESHOLD_SECONDS,
    INACTIVE_STATUSES,
    JSON_SCHEMA_VERSION,
    STATUS_ORDER,
    DashboardSnapshot,
    QueueRowView,
    RenderResult,
    RepoPillView,
    _dev_common,
    _facade_attr,
    _flow_opt,
)
from tui_progress import (
    _build_display_ids,
    _build_repo_labels,
    _clean_clickable_url,
    _collect_lock_extra_urls,
    _compute_item_progress,
    _compute_lock_progress,
    _extract_lock_telemetry_fields,
    _repo_pulls_url,
    _resolve_item_human_pr_link,
    _resolve_lock_human_pr_link,
    _sanitize_full_url,
    _slot_progress_cache_key,
    _url_has_pr_body,
)
from tui_state import (
    _cids_match,
    _discover_repos_cached,
    _extract_repo_slug_from_url,
    _format_compact_age,
    _is_uuid,
    _iter_active_lock_slots,
    _load_conversation_titles,
    _load_json,
    _load_locks,
    _load_orchestration_phase,
    _lock_slot_occupancy,
    _resolve_board_artifact_path,
    _resolve_brain_dirs,
    _resolve_effective_item_status,
    _resolve_stall_threshold,
    _resolve_state_dir,
    _secondary_slot_progress,
    _slot_label,
    _synthesize_untracked_lock_items,
    load_waiting_user_conversations,
)


def _debug_log(enabled: bool, msg: str) -> None:
    """Emit timestamped debug diagnostics to `stderr` when `--debug` is enabled."""
    if enabled:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
        with contextlib.suppress(OSError):
            sys.stderr.write(f"[agy-queue:debug {ts}Z] {msg}\n")


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


def _color(text: str, code: str, enabled: bool) -> str:
    """Wrap `text` in ANSI SGR color `code` when `enabled` is True."""
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
    """Pad `text` on the right to at least `width` visible terminal columns."""
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
    """Return True when the target TTY supports OSC-8 hyperlinks."""
    if _is_dumb_terminal():
        return False
    term_prog = (os.environ.get("TERM_PROGRAM") or "").lower()
    if term_prog == "apple_terminal":
        return False
    target = stream if stream is not None else sys.stdout
    return bool(getattr(target, "isatty", lambda: False)())


def _osc8_link(label: str, url: str, enabled: bool) -> str:
    """Wrap `label` in an OSC-8 hyperlink targeting the full unstripped URL."""
    if not enabled or not url:
        return label
    target = _sanitize_full_url(url) or url
    return f"\033]8;;{target}\007{label}\033]8;;\007"


def _compact_osc8_pr_label(url: str, repo_lbl: str = "", *, max_width: int = 84) -> str:
    """Build a compact single-line OSC-8 PR label that never overflows 123-col terminals."""
    from tui_progress import _extract_compare_branch_from_url

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


def _status_badge(status: str, color_on: bool, width: int = 11) -> str:
    """Format a fixed-width styled status badge."""
    norm_key = str(status or "").strip().lower()
    code, label = _STATUS_STYLES.get(norm_key, ("0", str(status or "").strip().upper()))
    eff_w = max(11, int(width))
    return _color(f"{label:<{eff_w}}", code, color_on)


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


def _compute_lightweight_opt_hint(
    *,
    items: list[dict[str, Any]],
    locks: dict[str, dict[str, Any]],
    all_locks: dict[str, dict[str, Any]],
    repo_local_dirs: dict[str, Path],
    bar_w: int,
    now_ts: float | None = None,
    stall_threshold_seconds: int | float | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
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
    except (OSError, ValueError, TypeError, RuntimeError):
        return ""


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


def _build_repo_pill_views(
    *,
    managed_list: list[str],
    label_map: dict[str, str],
    locks: dict[str, dict[str, Any]],
    lock_progress_cache: dict[str, dict[str, object]],
    repo_web_bases: dict[str, tuple[str, str]],
    repo_local_dirs: dict[str, Path],
    item_urls_by_cid: dict[str, list[str]],
) -> list[RepoPillView]:
    """Construct typed `RepoPillView` models for every managed repository."""
    lock_link_fn = _facade_attr("_resolve_lock_human_pr_link", _resolve_lock_human_pr_link)
    pills: list[RepoPillView] = []
    for slug in managed_list:
        lbl = label_map.get(slug, slug)
        lk = locks.get(slug)
        if not lk:
            pills.append(
                RepoPillView(
                    slug=slug,
                    label=lbl,
                    locked=False,
                    link_url=_repo_pulls_url(slug, repo_web_bases),
                )
            )
            continue

        occ = _lock_slot_occupancy(lk)
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

        secondary_lps = _secondary_slot_progress(slug, lock_progress_cache)
        secondary_slots: list[tuple[str, bool, str]] = []
        any_sn_amber = False
        slots_progress: dict[str, dict[str, object]] = {"1": dict(lp)}
        s2_lp: dict[str, Any] | None = None
        s2_suffix_plain = ""
        s2_is_amber = False
        s2_idle_lbl = ""
        s2_short = ""
        for sn_id, sn_lp in secondary_lps:
            slots_progress[str(sn_id)] = dict(sn_lp)
            if sn_lp.get("is_stalled"):
                sn_short = str(sn_lp.get("tag") or "STALL")
            elif sn_lp.get("is_awaiting_base"):
                sn_short = "await"
            elif sn_lp.get("is_needs_rebase"):
                sn_short = "rebase"
            elif sn_lp.get("is_agent_review"):
                sn_short = "rev"
            else:
                sn_short = f"{int(sn_lp.get('pct') or 0)}%"  # type: ignore[arg-type]
            sn_idle_s = int(sn_lp.get("idle_seconds") or 0)
            sn_idle_lbl = str(sn_lp.get("idle_label") or "")
            sn_is_amber = (
                not bool(sn_lp.get("is_stalled"))
                and not bool(sn_lp.get("is_awaiting_base"))
                and not bool(sn_lp.get("is_needs_rebase"))
                and sn_idle_s >= IDLE_AMBER_THRESHOLD_SECONDS
            )
            sn_suffix = f"·{_slot_label(sn_id)}:{sn_short}"
            any_sn_amber = any_sn_amber or sn_is_amber
            secondary_slots.append((sn_suffix, sn_is_amber, sn_idle_lbl))
            if sn_id == 2:
                s2_lp = sn_lp
                s2_suffix_plain = sn_suffix
                s2_is_amber = sn_is_amber
                s2_idle_lbl = sn_idle_lbl
                s2_short = sn_short

        _, repo_u, lock_has_pr, _ = lock_link_fn(
            slug,
            lk,
            repo_web_bases,
            repo_local_dirs,
            extra_urls=_collect_lock_extra_urls(lk, item_urls_by_cid),
        )
        pills.append(
            RepoPillView(
                slug=slug,
                label=lbl,
                locked=True,
                occupancy=occ,
                base_tag=base_tag,
                is_human_ready=is_human_pr,
                is_stalled=is_stalled,
                is_needs_rebase=is_needs_rebase,
                is_awaiting_base=is_awaiting_base,
                is_agent_review=is_rev,
                is_escalated=is_esc,
                is_amber=is_amber_pill,
                idle_seconds=lp_idle_secs,
                idle_label=lp_idle_lbl,
                s2_suffix_plain=s2_suffix_plain,
                s2_is_amber=s2_is_amber,
                s2_idle_label=s2_idle_lbl,
                s2_tag=s2_short,
                secondary_slots=secondary_slots,
                any_secondary_amber=any_sn_amber,
                link_url=repo_u,
                lock_has_pr=lock_has_pr,
                lock_progress=dict(lp),
                slot2_progress=dict(s2_lp) if isinstance(s2_lp, dict) else None,
                slots_progress=slots_progress,
            )
        )
    return pills


def _format_repo_pill(pill: RepoPillView, *, color_on: bool, use_osc8: bool) -> str:
    """Render a single `RepoPillView` for the terminal `REPOS:` header line."""
    if not pill.locked:
        dot = _color("○", "0;32", color_on)
        styled_name = _color(pill.label, "0;37", color_on)
        return f"{dot} {_osc8_link(styled_name, pill.link_url, use_osc8)} {_color('(FREE)', '0;32', color_on)}"

    dot_code = (
        "1;33"
        if (pill.is_human_ready or pill.is_amber or pill.any_secondary_amber or pill.s2_is_amber)
        else ("1;31" if pill.is_stalled else ("1;35" if (pill.is_escalated or pill.is_needs_rebase) else "1;36"))
    )
    dot = _color("●", dot_code, color_on)
    styled_name = _color(pill.label, "1;33" if pill.is_human_ready else ("1;31" if pill.is_stalled else "1"), color_on)
    occ_styled = _color(f"[{pill.occupancy}]", "1;36" if not pill.occupancy.startswith(("0/", "1/")) else "2", color_on)
    tag_code = "1;33" if pill.is_human_ready else ("1;35" if (pill.is_stalled or pill.is_needs_rebase) else "2")
    pill_inner = _color(f"({pill.base_tag}", tag_code, color_on)
    if pill.is_amber and pill.idle_label:
        pill_inner += _color(f" · idle {pill.idle_label}", "1;33", color_on)
    sec_list = pill.secondary_slots or (
        [(pill.s2_suffix_plain, pill.s2_is_amber, pill.s2_idle_label)] if pill.s2_suffix_plain else []
    )
    for sn_suffix_plain, sn_is_amber, sn_idle_lbl in sec_list:
        pill_inner += _color(sn_suffix_plain, tag_code, color_on)
        if sn_is_amber and sn_idle_lbl:
            pill_inner += _color(f" · idle {sn_idle_lbl}", "1;33", color_on)
    pill_inner += _color(")", tag_code, color_on)
    return f"{dot} {_osc8_link(styled_name, pill.link_url, use_osc8)} {occ_styled} {pill_inner}"


def _build_dashboard_snapshot(
    args: argparse.Namespace,
    *,
    bar_w: int,
    conv_db_path: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    now_ts_override: float | None = None,
    stall_threshold_seconds: int | float | None = None,
    repo_cache: dict[str, Any] | None = None,
    strict_io: bool = False,
) -> DashboardSnapshot:
    """Load queue, locks, repos, progress, and telemetry into a typed `DashboardSnapshot`."""
    debug_on = bool(getattr(args, "debug", False))
    state_dir = _facade_attr("_resolve_state_dir", _resolve_state_dir)(getattr(args, "state_dir", ""))
    queue_data = _facade_attr("_load_json", _load_json)(state_dir / "dev_queue.json", strict=strict_io)
    items = queue_data.get("items") or []
    locks, all_locks = _facade_attr("_load_locks", _load_locks)(state_dir, strict=strict_io)
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

    lock_prog_fn = _facade_attr("_compute_lock_progress", _compute_lock_progress)
    lock_progress_cache: dict[str, dict[str, object]] = {}
    for slug, repo_lk in locks.items():
        for slot_entry in _iter_active_lock_slots(repo_lk):
            p_key = _slot_progress_cache_key(slug, slot_entry)
            lock_progress_cache[p_key] = lock_prog_fn(
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
    row_eff_statuses = [_resolve_effective_item_status(it, locks_by_cid) for it in filtered]
    display_ids = _build_display_ids(filtered)
    all_filtered_cids = [str(it.get("conversation_id") or "") for it in filtered]
    ag_titles = _facade_attr("_load_conversation_titles", _load_conversation_titles)(
        all_filtered_cids,
        db_path=conv_db_path,
        strict=strict_io,
    )
    managed_list = [s for s in sorted(all_managed_slugs) if not repo_filter or repo_filter in s.lower()]

    orch_full = str(queue_data.get("orchestrator_conversation_id") or "-")
    orch_live_txt, orch_live_code = _resolve_orchestrator_liveness(
        orch_full,
        brain_dir=brain_dir,
        counts=counts,
        now_ts=now_ts_override,
        stall_threshold_seconds=stall_threshold_seconds,
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

    repo_pills = _build_repo_pill_views(
        managed_list=managed_list,
        label_map=label_map,
        locks=locks,
        lock_progress_cache=lock_progress_cache,
        repo_web_bases=repo_web_bases,
        repo_local_dirs=repo_local_dirs,
        item_urls_by_cid=item_urls_by_cid,
    )

    orch_phase = _facade_attr("_load_orchestration_phase", _load_orchestration_phase)(state_dir, locks)
    waiting_convs = _facade_attr("load_waiting_user_conversations", load_waiting_user_conversations)(
        db_path=conv_db_path,
        brain_dir=brain_dir,
        strict=strict_io,
    )

    return DashboardSnapshot(
        state_dir=state_dir,
        queue_data=queue_data,
        items=items,
        locks=locks,
        all_locks=all_locks,
        repo_web_bases=repo_web_bases,
        repo_local_dirs=repo_local_dirs,
        effective_items=effective_items,
        filtered=filtered,
        row_eff_statuses=row_eff_statuses,
        locks_by_cid=locks_by_cid,
        lock_progress_cache=lock_progress_cache,
        counts=counts,
        all_managed_slugs=all_managed_slugs,
        managed_list=managed_list,
        label_map=label_map,
        display_ids=display_ids,
        ag_titles=ag_titles,
        item_urls_by_cid=item_urls_by_cid,
        orch_full=orch_full,
        orch_live_txt=orch_live_txt,
        orch_live_code=orch_live_code,
        opt_hint=opt_hint,
        has_active_work=bool(locks or counts.get("pr_open", 0) > 0),
        orch_phase=orch_phase,
        repo_pills=repo_pills,
        stalled_alerts=stalled_alerts,
        waiting_convs=waiting_convs,
    )


def _format_waiting_hitl_detail(w: Mapping[str, Any]) -> str:
    """Format a compact single-line badge/preview label for a live waiting conversation."""
    kind = str(w.get("waiting_kind") or "").strip().lower()
    tool = str(w.get("waiting_tool") or "").strip()
    q_cnt = int(w.get("question_count") or 0)
    preview = str(w.get("question_preview") or "").strip()
    if kind == "ask_question":
        badge = f"QUESTION ({q_cnt})" if q_cnt > 0 else "QUESTION"
        more = f" (+{q_cnt - 1} more)" if q_cnt > 1 else ""
        return f"{badge}: {preview}{more}" if preview else badge
    return f"APPROVAL ({tool})" if tool else "APPROVAL"


def _collect_brain_proposal_links(cid: str, brain_roots: Sequence[Path]) -> str:
    """Return compact Markdown links for `.md` artifacts under `<brain_root>/<cid>/`."""
    prop_mds: list[Path] = []
    seen_names: set[str] = set()
    for b_root in brain_roots:
        cid_brain_dir = b_root / cid
        if cid_brain_dir.is_dir():
            for md_cand in sorted(cid_brain_dir.glob("*.md")):
                if md_cand.name not in seen_names:
                    seen_names.add(md_cand.name)
                    prop_mds.append(md_cand)
    return " · ".join(f"[{p.name}](file://{p})" for p in prop_mds[:2]) if prop_mds else "—"


def _compute_unified_hitl_total(snap: DashboardSnapshot) -> int:
    """Count queue `hitl_blocked` items plus deduplicated live waiting conversations."""
    queue_hitl_cids = [
        str(it.get("conversation_id") or "").strip()
        for it in snap.effective_items
        if _resolve_effective_item_status(it, snap.locks_by_cid).lower() == "hitl_blocked"
        and str(it.get("conversation_id") or "").strip()
    ]
    extra_live = [
        w
        for w in snap.waiting_convs
        if not any(_cids_match(str(w.get("conversation_id") or ""), qcid) for qcid in queue_hitl_cids)
    ]
    return snap.counts.get("hitl_blocked", 0) + len(extra_live)


def _append_live_waiting_hitl_entries(
    *,
    args: argparse.Namespace,
    snap: DashboardSnapshot,
    resolved_brain_roots: Sequence[Path],
    hitl_convs: dict[int, tuple[str, str, str]],
    hitl_footer: list[tuple[int, str, str]],
    hitl_board_rows: list[dict[str, str]],
    hitl_idx: int,
) -> int:
    """Append deduplicated live waiting conversations to unified HITL hotkeys, footer, and board rows."""
    status_filter = (getattr(args, "status", "") or "").strip().lower()
    repo_filter = (getattr(args, "repo", "") or "").strip().lower()
    if status_filter and status_filter != "hitl_blocked":
        return hitl_idx

    existing_cids = [entry[1] for entry in hitl_convs.values() if entry[1]]
    items_by_cid = {
        str(it.get("conversation_id") or "").strip(): it
        for it in snap.effective_items
        if str(it.get("conversation_id") or "").strip()
    }
    for w in snap.waiting_convs:
        w_cid = str(w.get("conversation_id") or "").strip()
        if not w_cid or any(_cids_match(w_cid, cur) for cur in existing_cids):
            continue
        matched_it = items_by_cid.get(w_cid)
        if matched_it is None:
            for icid, it_obj in items_by_cid.items():
                if _cids_match(w_cid, icid):
                    matched_it = it_obj
                    break
        if repo_filter:
            it_repos = [str(r) for r in ((matched_it or {}).get("repo_slugs") or []) if r]
            if not any(repo_filter in r.lower() for r in it_repos):
                continue

        hitl_idx += 1
        existing_cids.append(w_cid)
        w_disp = str(w.get("display_id") or w_cid[:8])
        w_title = str(w.get("sidebar_title") or w.get("title") or w_disp)
        detail = _format_waiting_hitl_detail(w)
        footer_label = f"{w_title} · {detail}" if detail and w_title != w_disp else (f"{w_title} · {detail}" if w_title else detail)
        hitl_convs[hitl_idx] = (w_disp, w_cid, w_title)
        hitl_footer.append((hitl_idx, w_disp, footer_label))
        hitl_board_rows.append(
            {
                "idx": str(hitl_idx),
                "prio": "LIVE",
                "cid": w_cid,
                "ag_title": _sanitize_md_table_cell(w_title),
                "repos": "live",
                "notes": _trunc(_sanitize_md_table_cell(detail), 180),
                "artifact_link": _collect_brain_proposal_links(w_cid, resolved_brain_roots),
            }
        )
    return hitl_idx


def _render_hitl_footer_lines(
    hitl_footer: list[tuple[int, str, str]],
    *,
    bar_w: int,
    color_on: bool,
) -> list[str]:
    """Render the unified `HITL — ANTIGRAVITY APP VALIDATION` footer section."""
    if not hitl_footer:
        return []
    title_part = "HITL — ANTIGRAVITY APP VALIDATION"
    full_hint = "(press h + number: jump via Dev Queue Panel or copy title & focus Antigravity · /hitl-review):"
    short_hint = "(press h+# to jump · /hitl-review):"
    if len(title_part) + 1 + len(full_hint) <= bar_w:
        header_line = f"{_color(title_part, '1;35', color_on)} {_color(full_hint, '2', color_on)}"
    elif len(title_part) + 1 + len(short_hint) <= bar_w:
        header_line = f"{_color(title_part, '1;35', color_on)} {_color(short_hint, '2', color_on)}"
    else:
        header_line = _color(_trunc(f"{title_part} {short_hint}", bar_w), "1;35", color_on)
    lines: list[str] = [
        _color("─" * bar_w, "2", color_on),
        header_line,
    ]
    for h_i, cid_disp, ag_t in hitl_footer:
        lines.append(
            f"  {_color(f'[h{h_i}]', '1;35', color_on)} "
            f"{_color(cid_disp, '36', color_on)}  "
            f"{_trunc(ag_t, max(20, bar_w - 20))}"
        )
    return lines


def _render_json_payload(
    args: argparse.Namespace,
    snap: DashboardSnapshot,
    *,
    now_ts_override: float | None,
    ndjson_compact: bool,
    ndjson_seq: int | None,
) -> str:
    """Serialize `DashboardSnapshot` into `--json` / `--watch --json` NDJSON output."""
    repo_filter = (args.repo or "").strip().lower()
    item_link_fn = _facade_attr("_resolve_item_human_pr_link", _resolve_item_human_pr_link)
    item_prog_fn = _facade_attr("_compute_item_progress", _compute_item_progress)

    enriched_items: list[dict[str, Any]] = []
    for it, eff_st in zip(snap.filtered, snap.row_eff_statuses):
        entry = dict(it)
        cid_full = str(it.get("conversation_id") or "")
        entry["effective_status"] = eff_st
        if cid_full in snap.ag_titles:
            entry["sidebar_title"] = snap.ag_titles[cid_full]
        _, _, item_is_pr, item_git_state, _ = item_link_fn(
            it, snap.locks_by_cid, snap.repo_web_bases, snap.repo_local_dirs
        )
        item_prog = item_prog_fn(
            it,
            snap.locks_by_cid,
            snap.lock_progress_cache,
            item_git_state,
            is_human_pr=item_is_pr,
            now_ts=now_ts_override,
        )
        if item_prog is not None:
            entry["progress"] = item_prog
        linked_lks = snap.locks_by_cid.get(cid_full) or []
        if linked_lks:
            first_slug, first_lk = linked_lks[0]
            first_lp = snap.lock_progress_cache.get(_slot_progress_cache_key(first_slug, first_lk)) or {}
            telem = _extract_lock_telemetry_fields(first_lp, first_lk)
            entry["lock_telemetry"] = telem
            for tk, tv in telem.items():
                entry.setdefault(tk, tv)
        enriched_items.append(entry)

    active_locks_payload: dict[str, dict[str, Any]] = {}
    for slug, lk in snap.locks.items():
        if repo_filter and repo_filter not in slug.lower():
            continue
        lp_primary = snap.lock_progress_cache.get(slug, {})
        telem_primary = _extract_lock_telemetry_fields(lp_primary, lk)
        secondary_lps = _secondary_slot_progress(slug, snap.lock_progress_cache)
        active_locks_payload[slug] = {
            **lk,
            **telem_primary,
            "progress": lp_primary,
            "slots_progress": {
                "1": lp_primary,
                **{str(sn_id): sn_lp for sn_id, sn_lp in secondary_lps},
            },
            **(
                {"slot2_progress": snap.lock_progress_cache[f"{slug}#slot2"]}
                if f"{slug}#slot2" in snap.lock_progress_cache
                else {}
            ),
        }

    stalled_locks_payload = {
        p_key: lp_val
        for p_key, lp_val in snap.lock_progress_cache.items()
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
        "state_dir": str(snap.state_dir),
        "updated_at": snap.queue_data.get("updated_at", ""),
        "orchestrator_conversation_id": snap.queue_data.get("orchestrator_conversation_id", ""),
        "orchestration": snap.orch_phase,
        "counts": snap.counts,
        "unified_hitl_count": _compute_unified_hitl_total(snap),
        "waiting_user_conversations": list(snap.waiting_convs),
        "managed_repos": snap.managed_list,
        "locks": active_locks_payload,
        "active_locks": active_locks_payload,
        "stalled_locks": stalled_locks_payload,
        "opt_hint": snap.opt_hint,
        "items": enriched_items,
    }
    return (
        json.dumps(json_obj, separators=(",", ":"), ensure_ascii=False)
        if ndjson_compact
        else json.dumps(json_obj, indent=2, ensure_ascii=False)
    )


def render_dashboard(
    args: argparse.Namespace,
    *,
    conv_db_path: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    is_tty_override: bool | None = None,
    term_width_override: int | None = None,
    now_ts_override: float | None = None,
    stall_threshold_seconds: int | float | None = None,
    repo_cache: dict[str, Any] | None = None,
    sync_status_badge: str = "",
    force_board_write: bool = False,
    strict_io: bool = False,
    ndjson_compact: bool = False,
    ndjson_seq: int | None = None,
) -> RenderResult:
    """Render the Dev Queue dashboard into a typed `RenderResult` with `QueueRowView` and `RepoPillView` models."""
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

    snap = _build_dashboard_snapshot(
        args,
        bar_w=bar_w,
        conv_db_path=conv_db_path,
        brain_dir=brain_dir,
        now_ts_override=now_ts_override,
        stall_threshold_seconds=stall_threshold_seconds,
        repo_cache=repo_cache,
        strict_io=strict_io,
    )

    if getattr(args, "json", False):
        payload = _render_json_payload(
            args,
            snap,
            now_ts_override=now_ts_override,
            ndjson_compact=ndjson_compact,
            ndjson_seq=ndjson_seq,
        )
        return RenderResult(
            text=payload,
            row_links={},
            hitl_convs={},
            first_pr_link=None,
            state_dir=snap.state_dir,
            repo_local_dirs=snap.repo_local_dirs,
            active_locks=dict(snap.locks),
            all_locks=dict(snap.all_locks),
            items_snapshot=list(snap.effective_items),
            color_on=color_on,
            use_osc8=use_osc8,
            bar_w=bar_w,
        )

    now_local = datetime.now().strftime("%H:%M:%S")
    now_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    orch_id = snap.orch_full[:8]
    in_flight_run = (
        snap.counts.get("in_progress", 0)
        + snap.counts.get("ready_awaiting_base_merge", 0)
        + snap.counts.get("needs_rebase_reverify", 0)
        + snap.counts.get("needs_agent_semantic_fix", 0)
        + snap.counts.get("needs_agent_conflict_resolution", 0)
    )
    unified_hitl_total = _compute_unified_hitl_total(snap)
    summary_bits = [
        _color(f"pr:{snap.counts.get('pr_open', 0)}", "1;33", color_on),
        _color(f"run:{in_flight_run}", "1;36", color_on),
        _color(f"pending:{snap.counts.get('pending', 0)}", "1;37", color_on),
        _color(f"hitl:{unified_hitl_total}", "1;35", color_on),
        _color(f"standby:{snap.counts.get('standby', 0)}", "2;37", color_on),
        _color(f"merged:{snap.counts.get('merged', 0)}", "0;32", color_on),
    ]

    header_lines: list[str] = [_color("═" * bar_w, "2;36", color_on)]
    orch_segment = f"orch:{_color(orch_id, '1', color_on)}"
    if snap.orch_live_txt:
        orch_segment = f"{orch_segment} {_color(snap.orch_live_txt, snap.orch_live_code, color_on)}"
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
    elif bar_w >= 104 and not snap.orch_live_txt and not sync_status_badge:
        header_lines.append(f"{header_left}  |  {summary_str}")
    else:
        header_lines.append(header_left)
        header_lines.append(summary_str)

    orch_phase = snap.orch_phase or {}
    if bool(orch_phase.get("paused")):
        pause_banner = str(orch_phase.get("banner_text") or "PAUSED — 0 new dispatches · Watchdog silent")
        pause_reason = str(orch_phase.get("reason") or "").strip()
        if pause_reason:
            pause_banner = f"{pause_banner} ({pause_reason})"
        header_lines.append(
            _color(_trunc(f"⏸ [[P] Continue] {pause_banner}", bar_w), "1;33", color_on)
        )

    if snap.repo_pills:
        rendered_pills = [_format_repo_pill(p, color_on=color_on, use_osc8=use_osc8) for p in snap.repo_pills]
        header_lines.append(f"{_color('REPOS:', '1;33', color_on)}  " + "   ".join(rendered_pills))

    header_lines.append(_color("─" * bar_w, "2", color_on))
    board_writer_fn = _facade_attr("_write_live_antigravity_board", _write_live_antigravity_board)

    if not snap.filtered:
        resolved_brain_roots_empty = _resolve_brain_dirs(brain_dir)
        empty_hitl_convs: dict[int, tuple[str, str, str]] = {}
        empty_hitl_footer: list[tuple[int, str, str]] = []
        empty_hitl_board_rows: list[dict[str, str]] = []
        _append_live_waiting_hitl_entries(
            args=args,
            snap=snap,
            resolved_brain_roots=resolved_brain_roots_empty,
            hitl_convs=empty_hitl_convs,
            hitl_footer=empty_hitl_footer,
            hitl_board_rows=empty_hitl_board_rows,
            hitl_idx=0,
        )
        empty_msg = _color("No matching items in dev_queue.json.", "2", color_on)
        sticky_empty: list[str] = [empty_msg]
        if empty_hitl_footer:
            sticky_empty.extend(_render_hitl_footer_lines(empty_hitl_footer, bar_w=bar_w, color_on=color_on))
        if snap.stalled_alerts:
            sticky_empty.append(_color("─" * bar_w, "2", color_on))
            summary_st = ", ".join(f"{r} idle {idle}" for r, idle, _ in snap.stalled_alerts)
            sticky_empty.append(
                _color(_trunc(f"⚠ STALLED WORKER: {summary_st} — inspect worktree or re-dispatch", bar_w), "1;31", color_on)
            )
        if snap.opt_hint:
            sticky_empty.append(_color(_trunc(snap.opt_hint, bar_w), "1;35", color_on))
        sticky_empty.append(_color("═" * bar_w, "2;36", color_on))
        lines = [*header_lines, *sticky_empty]
        board_path = _resolve_board_artifact_path(snap.state_dir, getattr(args, "write_board", ""))
        if board_path is not None:
            board_writer_fn(
                board_path,
                now_str=now_full,
                orch_id=snap.orch_full,
                counts=snap.counts,
                managed_list=snap.managed_list,
                label_map=snap.label_map,
                locks=snap.locks,
                lock_progress_cache=snap.lock_progress_cache,
                repo_web_bases=snap.repo_web_bases,
                repo_local_dirs=snap.repo_local_dirs,
                item_urls_by_cid=snap.item_urls_by_cid,
                board_rows=[],
                hitl_rows=empty_hitl_board_rows,
                force_write=force_board_write,
            )
        return RenderResult(
            text="\n".join(lines),
            row_links={},
            hitl_convs=empty_hitl_convs,
            first_pr_link=None,
            state_dir=snap.state_dir,
            repo_local_dirs=snap.repo_local_dirs,
            header_lines=header_lines,
            sticky_footer_lines=sticky_empty,
            has_active_work=snap.has_active_work,
            orchestration_paused=bool(orch_phase.get("paused")),
            orchestration_phase=str(orch_phase.get("phase") or "active"),
            active_locks=dict(snap.locks),
            all_locks=dict(snap.all_locks),
            items_snapshot=list(snap.effective_items),
            color_on=color_on,
            use_osc8=use_osc8,
            bar_w=bar_w,
            waiting_convs=list(snap.waiting_convs),
        )

    status_col = max(
        11,
        *(len(_STATUS_STYLES.get(es, ("0", es.upper()))[1]) for es in snap.row_eff_statuses),
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
    active_rows: list[QueueRowView] = []
    middle_rows: list[QueueRowView] = []
    row_views: list[QueueRowView] = []
    row_records: list[dict[str, Any]] = []
    sticky_footer_lines: list[str] = []
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

    item_link_fn = _facade_attr("_resolve_item_human_pr_link", _resolve_item_human_pr_link)
    item_prog_fn = _facade_attr("_compute_item_progress", _compute_item_progress)

    for idx, (it, eff_st) in enumerate(zip(snap.filtered, snap.row_eff_statuses), 1):
        prio = int(it.get("priority") if it.get("priority") is not None else 99)
        st = eff_st
        st_lower = eff_st.lower()
        raw_item_st = str(it.get("status") or "pending").strip().lower()
        repo_slugs_list = [str(r) for r in (it.get("repo_slugs") or []) if r]
        merged_set = {str(r) for r in (it.get("merged_repos") or []) if r}
        cid_full = str(it.get("conversation_id") or "")
        cid_disp = snap.display_ids.get(cid_full, cid_full[:8])
        title = str(it.get("title") or "").strip()
        deps = [str(d)[:8] for d in (it.get("depends_on") or []) if str(d).strip()]
        if deps:
            title = f"{title} [deps:{','.join(deps)}]"

        link_kind, link_url, is_human_pr, git_state, target_slug = item_link_fn(
            it, snap.locks_by_cid, snap.repo_web_bases, snap.repo_local_dirs
        )
        pr_repo_slug = target_slug if is_human_pr else ""
        repos_str = _compact_repos(repo_slugs_list, snap.label_map, merged_set, repo_col, pr_repo_slug=pr_repo_slug)
        repos_md = _compact_repos(repo_slugs_list, snap.label_map, merged_set, 40, pr_repo_slug=pr_repo_slug)
        prog = item_prog_fn(
            it,
            snap.locks_by_cid,
            snap.lock_progress_cache,
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
            repo_lbl = snap.label_map.get(target_slug, target_slug.split("/")[-1]) if target_slug else repos_str
            part_meta = ""
            if len(repo_slugs_list) > 1:
                part_idx = min(len(repo_slugs_list), len(merged_set) + 1)
                remaining_lbls = [
                    snap.label_map.get(r, r.split("/")[-1])
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
            if cid_full in snap.ag_titles:
                action_md += f" · [Chat](conversation://{cid_full})"
        elif link_kind in ("Sync", "Stale"):
            pill_styled = _color(f"[↻ {link_kind}]", "1;35", color_on)
            action_cell = _pad_visible(pill_styled, link_col)
            action_md = f"`{link_kind}`"
        elif st_lower == "hitl_blocked" and cid_full:
            hitl_idx += 1
            ag_t = snap.ag_titles.get(cid_full) or title
            matched_w = next(
                (w for w in snap.waiting_convs if _cids_match(cid_full, str(w.get("conversation_id") or ""))),
                None,
            )
            live_detail = _format_waiting_hitl_detail(matched_w) if matched_w is not None else ""
            hitl_convs[hitl_idx] = (cid_disp, cid_full, ag_t)
            hitl_footer.append((hitl_idx, cid_disp, f"{ag_t} · {live_detail}" if live_detail else ag_t))
            pill_raw = f"[h{hitl_idx}] Agy"
            pill_styled = _color(pill_raw, "1;35", color_on)
            action_cell = _pad_visible(pill_styled, link_col)
            action_md = f"[Open chat](conversation://{cid_full})" if _is_uuid(cid_full) else "—"
            art_link = _collect_brain_proposal_links(cid_full, resolved_brain_roots)
            notes_raw = str(it.get("notes") or title)
            if live_detail and live_detail not in notes_raw:
                notes_raw = f"{live_detail} · {notes_raw}" if notes_raw else live_detail
            hitl_board_rows.append(
                {
                    "idx": str(hitl_idx),
                    "prio": str(prio),
                    "cid": cid_full,
                    "ag_title": _sanitize_md_table_cell(ag_t),
                    "repos": repos_md,
                    "notes": _trunc(_sanitize_md_table_cell(notes_raw), 180),
                    "artifact_link": art_link,
                }
            )
        else:
            if raw_item_st == "in_progress" and st_lower == "in_progress":
                unmerged = [r for r in repo_slugs_list if r not in merged_set]
                if unmerged:
                    pu = _repo_pulls_url(str(unmerged[0]), snap.repo_web_bases)
                    if pu:
                        row_links[idx] = (f"{cid_disp} (Repo)", pu)
            action_cell = _pad_visible(_color("—", "2", color_on), link_col)
            if cid_full in snap.ag_titles:
                action_md = f"[Chat](conversation://{cid_full})"

        clean_title_flat = " ".join(title.split())
        row_line = (
            f"{idx:>2} {prio:>4}  {_status_badge(st, color_on, width=status_col)}  {prog_cell}  "
            f"{_pad_visible(repos_str, repo_col)}  {_color(_pad_visible(cid_disp, 8), '36', color_on)}  "
            f"{_pad_visible(_trunc(clean_title_flat, title_col), title_col)}  {action_cell}"
        )
        is_active_band = (
            st_lower in ("pr_open", "in_progress", "ready_awaiting_base_merge", "needs_rebase_reverify")
            or bool(snap.locks_by_cid.get(cid_full))
        )
        band = "active" if is_active_band else "middle"

        item_lock_records: list[dict[str, Any]] = []
        for l_slug, l_dict in snap.locks_by_cid.get(cid_full, []):
            l_prog = snap.lock_progress_cache.get(_slot_progress_cache_key(l_slug, l_dict)) or {}
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
                snap.ag_titles.get(cid_full, ""),
                " ".join(repo_slugs_list),
                " ".join(str(b) for b in (it.get("branch_names") or [])),
                str(it.get("notes") or ""),
            ]
        ).lower()

        row_view = QueueRowView(
            idx=idx,
            priority=prio,
            status=st,
            raw_status=raw_item_st,
            band=band,
            display_id=cid_disp,
            conversation_id=cid_full,
            sidebar_title=snap.ag_titles.get(cid_full, ""),
            title=clean_title_flat,
            repos_str=repos_str,
            repos_md=repos_md,
            repo_slugs=repo_slugs_list,
            merged_repos=merged_set,
            progress=prog,
            linked_locks=item_lock_records,
            pr_url=row_links.get(idx, ("", ""))[1] if is_human_pr else "",
            is_human_pr=is_human_pr,
            link_kind=link_kind,
            git_state=git_state,
            target_slug=target_slug,
            raw_line=row_line,
            search_blob=search_blob,
            item=dict(it),
        )
        row_views.append(row_view)
        row_records.append(row_view.to_record_dict())
        if is_active_band:
            active_rows.append(row_view)
            active_row_lines.append(row_line)
        else:
            middle_rows.append(row_view)
            middle_row_lines.append(row_line)

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

    hitl_idx = _append_live_waiting_hitl_entries(
        args=args,
        snap=snap,
        resolved_brain_roots=resolved_brain_roots,
        hitl_convs=hitl_convs,
        hitl_footer=hitl_footer,
        hitl_board_rows=hitl_board_rows,
        hitl_idx=hitl_idx,
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
        sticky_footer_lines.extend(_render_hitl_footer_lines(hitl_footer, bar_w=bar_w, color_on=color_on))

    if snap.stalled_alerts:
        sticky_footer_lines.append(_color("─" * bar_w, "2", color_on))
        st_bits = [
            f"{repo_lbl} idle {idle_lbl}" + (f" ({br})" if br else "")
            for repo_lbl, idle_lbl, br in snap.stalled_alerts
        ]
        sticky_footer_lines.append(
            _color(
                _trunc(f"⚠ STALLED WORKER: {'; '.join(st_bits)} — inspect worktree or re-dispatch", bar_w),
                "1;31",
                color_on,
            )
        )

    if snap.opt_hint:
        if not snap.stalled_alerts and not ready_pr_footer and not hitl_footer:
            sticky_footer_lines.append(_color("─" * bar_w, "2", color_on))
        sticky_footer_lines.append(_color(_trunc(snap.opt_hint, bar_w), "1;35", color_on))

    sticky_footer_lines.append(_color("═" * bar_w, "2;36", color_on))
    lines = [*header_lines, *active_row_lines, *middle_row_lines, *sticky_footer_lines]

    board_path = _resolve_board_artifact_path(snap.state_dir, getattr(args, "write_board", ""))
    if board_path is not None:
        board_writer_fn(
            board_path,
            now_str=now_full,
            orch_id=snap.orch_full,
            counts=snap.counts,
            managed_list=snap.managed_list,
            label_map=snap.label_map,
            locks=snap.locks,
            lock_progress_cache=snap.lock_progress_cache,
            repo_web_bases=snap.repo_web_bases,
            repo_local_dirs=snap.repo_local_dirs,
            item_urls_by_cid=snap.item_urls_by_cid,
            board_rows=board_rows,
            hitl_rows=hitl_board_rows,
            force_write=force_board_write,
        )

    _debug_log(debug_on, f"render completed in {(time.perf_counter() - t_start) * 1000.0:.2f}ms")
    return RenderResult(
        text="\n".join(lines),
        row_links=row_links,
        hitl_convs=hitl_convs,
        first_pr_link=first_pr_link,
        state_dir=snap.state_dir,
        repo_local_dirs=snap.repo_local_dirs,
        header_lines=header_lines,
        active_row_lines=active_row_lines,
        middle_row_lines=middle_row_lines,
        active_rows=active_rows,
        middle_rows=middle_rows,
        sticky_footer_lines=sticky_footer_lines,
        row_records=row_records,
        row_views=row_views,
        all_pr_links=all_pr_links,
        has_active_work=snap.has_active_work,
        orchestration_paused=bool(orch_phase.get("paused")),
        orchestration_phase=str(orch_phase.get("phase") or "active"),
        active_locks=dict(snap.locks),
        all_locks=dict(snap.all_locks),
        items_snapshot=list(snap.effective_items),
        color_on=color_on,
        use_osc8=use_osc8,
        bar_w=bar_w,
        waiting_convs=list(snap.waiting_convs),
    )


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
    res = render_dashboard(
        args,
        conv_db_path=conv_db_path,
        brain_dir=brain_dir,
        is_tty_override=is_tty_override,
        term_width_override=term_width_override,
        now_ts_override=now_ts_override,
        stall_threshold_seconds=stall_threshold_seconds,
        repo_cache=repo_cache,
        sync_status_badge=sync_status_badge,
        force_board_write=force_board_write,
        strict_io=strict_io,
        ndjson_compact=ndjson_compact,
        ndjson_seq=ndjson_seq,
    )
    if layout_out is not None:
        layout_out.update(res.to_layout_dict())
    return res.to_legacy_tuple()


def _wrapped_row_count(line: str, safe_w: int) -> int:
    """Compute wrapped terminal line count for `line` at width `safe_w`."""
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


def _filter_compact_middle_rows(
    middle_items: Sequence[QueueRowView | str],
    max_pending: int = 3,
    *,
    middle_rows: Sequence[QueueRowView] | None = None,
) -> list[str]:
    """Keep at most `max_pending` `pending` rows while preserving all `hitl_blocked` and non-pending rows using `QueueRowView.status` (zero ANSI scraping)."""
    if middle_rows is not None and len(middle_rows) == len(middle_items):
        out: list[str] = []
        kept_pending = 0
        for item, row_view in zip(middle_items, middle_rows):
            line = item.raw_line if isinstance(item, QueueRowView) else str(item)
            st = row_view.status.strip().lower()
            if st == "hitl_blocked":
                out.append(line)
            elif st == "pending":
                if kept_pending < max_pending:
                    out.append(line)
                    kept_pending += 1
            else:
                out.append(line)
        return out

    if all(isinstance(it, QueueRowView) for it in middle_items):
        out_views: list[str] = []
        kept_pending = 0
        for row_view in middle_items:
            assert isinstance(row_view, QueueRowView)
            st = row_view.status.strip().lower()
            if st == "hitl_blocked":
                out_views.append(row_view.raw_line)
            elif st == "pending":
                if kept_pending < max_pending:
                    out_views.append(row_view.raw_line)
                    kept_pending += 1
            else:
                out_views.append(row_view.raw_line)
        return out_views

    return [str(ln) for ln in list(middle_items)[:max_pending]]


def _compose_priority_viewport_with_meta(
    header_lines: list[str],
    active_row_lines: list[str],
    middle_row_lines: Sequence[QueueRowView | str],
    sticky_footer_lines: list[str],
    term_w: int,
    term_h: int,
    footer: str,
    *,
    scroll_offset: int = 0,
    compact_pending: bool = False,
    middle_rows: Sequence[QueueRowView] | None = None,
) -> tuple[list[str], int, int]:
    """Compose wrap-aware viewport keeping header, active rows, and PR/HITL/OPT sticky footers visible."""
    safe_w = max(1, term_w)
    footer_rows = _wrapped_row_count(footer, safe_w)
    body_budget = max(1, term_h - footer_rows)
    color_on = any("\033[" in ln for ln in header_lines)

    middle_str_lines = [
        item.raw_line if isinstance(item, QueueRowView) else str(item)
        for item in middle_row_lines
    ]

    header_rows = sum(_wrapped_row_count(ln, safe_w) for ln in header_lines)
    active_rows_h = sum(_wrapped_row_count(ln, safe_w) for ln in active_row_lines)
    sticky_rows = sum(_wrapped_row_count(ln, safe_w) for ln in sticky_footer_lines)

    if not active_row_lines and not middle_str_lines:
        combined = [*header_lines, *sticky_footer_lines]
        if sum(_wrapped_row_count(ln, safe_w) for ln in combined) <= body_budget:
            return ([*combined, footer], 0, 0)
        return (_clamp_lines_to_viewport(combined, term_w, term_h, footer), 0, 0)

    if header_rows + sticky_rows > body_budget:
        all_lines = [*header_lines, *active_row_lines, *middle_str_lines, *sticky_footer_lines]
        return (_clamp_lines_to_viewport(all_lines, term_w, term_h, footer), 0, 0)

    if header_rows + active_rows_h + sticky_rows > body_budget:
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
        hidden_cnt = (len(active_row_lines) - len(kept_active)) + len(middle_str_lines)
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

    middle_budget = body_budget - (header_rows + active_rows_h + sticky_rows)
    candidate_middle = (
        _filter_compact_middle_rows(middle_row_lines, middle_rows=middle_rows)
        if compact_pending
        else list(middle_str_lines)
    )
    compact_hidden = len(middle_str_lines) - len(candidate_middle)
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

    total_orig = len(middle_str_lines)
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
    middle_row_lines: Sequence[QueueRowView | str],
    sticky_footer_lines: list[str],
    term_w: int,
    term_h: int,
    footer: str,
    scroll_offset: int = 0,
    compact_pending: bool = False,
    *,
    middle_rows: Sequence[QueueRowView] | None = None,
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
        middle_rows=middle_rows,
    )
    return lines


def _mark_cursor_on_row_line(line: str, color_on: bool) -> str:
    """Inject a zero-width-delta `▸` cursor marker right after the 2-char `#` index column."""
    if len(line) >= 3 and line[2] == " ":
        marker = _color("▸", "1;36", color_on)
        return f"{line[:2]}{marker}{line[3:]}"
    return line


def _row_attr(rec: QueueRowView | dict[str, Any], key: str, default: Any = "") -> Any:
    """Read a field uniformly from either a `QueueRowView` or legacy record dict."""
    if isinstance(rec, QueueRowView):
        if key == "effective_status":
            return rec.status
        return getattr(rec, key, default)
    return rec.get(key, default)


def _apply_live_filter_and_cursor(
    row_records: Sequence[QueueRowView | dict[str, Any]],
    *,
    live_filter: str,
    cursor_idx: int,
    compact_pending: bool,
    color_on: bool,
) -> tuple[list[str], list[str], list[Any], int]:
    """Filter captured `QueueRowView`/record items in `<1ms` pure memory and mark `cursor_idx` without disk/Git I/O."""
    q = (live_filter or "").strip().lower()
    matched = [r for r in row_records if (not q or q in str(_row_attr(r, "search_blob", "") or ""))]
    active_recs = [r for r in matched if _row_attr(r, "band", "") == "active"]
    middle_recs = [r for r in matched if _row_attr(r, "band", "") != "active"]
    if compact_pending:
        kept_mid: list[QueueRowView | dict[str, Any]] = []
        pend_seen = 0
        for mr in middle_recs:
            eff_l = str(_row_attr(mr, "effective_status", "") or "").strip().lower()
            if eff_l == "hitl_blocked":
                kept_mid.append(mr)
            elif eff_l == "pending":
                if pend_seen < 3:
                    kept_mid.append(mr)
                    pend_seen += 1
            else:
                kept_mid.append(mr)
        nav_middle_recs = kept_mid
    else:
        nav_middle_recs = middle_recs

    nav_records: list[Any] = [*active_recs, *nav_middle_recs]
    if not nav_records:
        return ([], [], [], 0)

    eff_cursor = max(0, min(int(cursor_idx), len(nav_records) - 1))
    selected_obj_id = id(nav_records[eff_cursor])

    act_lines = [
        _mark_cursor_on_row_line(str(_row_attr(r, "raw_line", "")), color_on)
        if id(r) == selected_obj_id
        else str(_row_attr(r, "raw_line", ""))
        for r in active_recs
    ]
    mid_lines = [
        _mark_cursor_on_row_line(str(_row_attr(r, "raw_line", "")), color_on)
        if id(r) == selected_obj_id
        else str(_row_attr(r, "raw_line", ""))
        for r in middle_recs
    ]
    return (act_lines, mid_lines, nav_records, eff_cursor)


def _render_detail_inspector_lines(
    rec: QueueRowView | dict[str, Any] | None,
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

    rec_dict = rec.to_record_dict() if isinstance(rec, QueueRowView) else rec
    it = rec_dict.get("item") if isinstance(rec_dict.get("item"), dict) else {}
    cid = str(it.get("conversation_id") or "")
    eff_st = str(rec_dict.get("effective_status") or it.get("status") or "pending").upper()
    raw_st = str(it.get("status") or "pending").upper()
    prio = it.get("priority", 99)
    eff_prio = it.get("effective_priority", prio)
    inh_from = str(it.get("inherited_priority_from") or "").strip()
    prio_str = f"P{prio}" + (f" (effective P{eff_prio} via {inh_from[:8]})" if eff_prio != prio or inh_from else "")

    lines.append(f"  {_color('ID:', '1', color_on)}           {cid}")
    if rec_dict.get("sidebar_title"):
        lines.append(f"  {_color('Sidebar:', '1', color_on)}      {rec_dict['sidebar_title']}")
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

    linked_locks = rec_dict.get("linked_locks") if isinstance(rec_dict.get("linked_locks"), list) else []
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

    pr_u = str(rec_dict.get("pr_url") or "").strip()
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
            if str(f.get("code") or "") == "pr_compaction_candidates":
                ev = f.get("evidence") if isinstance(f.get("evidence"), dict) else {}
                for plan in (ev.get("plans") or [])[:5]:
                    if not isinstance(plan, dict):
                        continue
                    t_lbl = f"T{plan.get('tier', 2)}"
                    a8 = str(plan.get("absorber_id") or "")[:8]
                    b8 = str(plan.get("absorbed_id") or "")[:8]
                    r_sh = str(plan.get("target_repo") or "").split("/")[-1]
                    pat = str(plan.get("pattern") or "")
                    fcnt = int(plan.get("combined_file_count") or 0)
                    if pat == "split_phase_multi_repo":
                        s_sh = str(plan.get("split_repo") or "").split("/")[-1]
                        p_txt = f"[{t_lbl}] {a8} <- {b8} ({r_sh} split from {s_sh} · {fcnt}f)"
                    else:
                        p_txt = f"[{t_lbl}] {a8} <- {b8} ({r_sh} · {fcnt}f)"
                    lines.append(f"     {_color('• ' + _trunc(p_txt, max(40, bar_w - 8)), '0;36', color_on)}")
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


def _plan_progress_bar(pct: int, width: int = 10) -> str:
    """Render an ASCII/Unicode progress bar for component completion percentage."""
    clamped = max(0, min(100, int(pct)))
    filled = int(round((clamped / 100.0) * width))
    return f"[{'█' * filled}{'░' * (width - filled)}] {clamped:3d}%"


def _render_plan_lines(plan: dict[str, Any], term_w: int, color_on: bool) -> list[str]:
    """Render deterministic `Repo -> Sub-Component` planning view for terminal output."""
    bar_w = max(72, min(term_w, 160))
    totals = plan.get("totals") or {}
    by_wave = totals.get("by_wave") or {}
    by_imp = totals.get("by_importance") or {}
    lines: list[str] = [
        _color("═" * bar_w, "2;36", color_on),
        _color(
            f"AGY-QUEUE COMPONENT PLAN  open:{totals.get('open_count', 0)}  "
            f"repos:{totals.get('repos_count', 0)}  components:{totals.get('components_count', 0)}  "
            f"done:{totals.get('total_done_count', 0)}",
            "1;36",
            color_on,
        ),
        (
            f"  Waves: active={by_wave.get('active', 0)} · partial_wip={by_wave.get('partial_wip', 0)} · "
            f"ready={by_wave.get('ready', 0)} · blocked={by_wave.get('blocked', 0)} · "
            f"standby={by_wave.get('standby', 0)}"
        ),
        (
            f"  Importance: critical={by_imp.get('critical', 0)} · high={by_imp.get('high', 0)} · "
            f"medium={by_imp.get('medium', 0)} · low={by_imp.get('low', 0)}"
        ),
        _color("─" * bar_w, "2;36", color_on),
    ]

    wave_colors = {
        "active": "1;36",
        "partial_wip": "1;33",
        "ready": "1;32",
        "blocked": "1;35",
        "standby": "2;37",
    }
    imp_colors = {
        "critical": "1;31",
        "high": "1;33",
        "medium": "0;36",
        "low": "2;37",
    }

    repos = plan.get("repos") or []
    if not repos:
        lines.append("  No open items match the active planning filters.")
    for repo in repos:
        r_slug = str(repo.get("repo_slug") or "")
        r_label = str(repo.get("repo_label") or r_slug)
        r_bar = _plan_progress_bar(int(repo.get("completion_pct") or 0), width=10)
        repo_head_lbl = f"▸ {r_label}"
        lines.append(
            f"{_color(repo_head_lbl, '1;37', color_on)} ({r_slug})  {r_bar}  "
            f"open:{repo.get('open_count', 0)}  done:{repo.get('done_count', 0)}"
        )
        for comp in repo.get("components") or []:
            c_bar = _plan_progress_bar(int(comp.get("completion_pct") or 0), width=8)
            comp_name_lbl = f"▪ {comp.get('name', 'general')}"
            lines.append(
                f"  {_color(comp_name_lbl, '1;34', color_on)}  {c_bar}  "
                f"({comp.get('done_count', 0)}/{comp.get('total_count', 0)})  "
                f"top:{str(comp.get('highest_importance') or 'medium').upper()}"
            )
            for item in comp.get("items") or []:
                wave = str(item.get("wave") or "ready")
                imp = str(item.get("importance") or "medium")
                prio = int(item.get("priority") or 50)
                eff_p = int(item.get("effective_priority") or prio)
                p_lbl = f"P{eff_p}<-P{prio}" if eff_p != prio else f"P{prio}"
                title = str(item.get("sidebar_title") or item.get("title") or item.get("display_id") or "")
                extras: list[str] = []
                if item.get("unblocks_label"):
                    extras.append(_color(str(item["unblocks_label"]), "1;33", color_on))
                if item.get("blocked_by_label"):
                    extras.append(_color(str(item["blocked_by_label"]), "1;35", color_on))
                if item.get("pr_url"):
                    extras.append(_color(f"PR:{item['pr_url']}", "1;32", color_on))
                extra_txt = f"  [{'; '.join(extras)}]" if extras else ""
                lines.append(
                    f"    • {_color(f'[{wave.upper()}]', wave_colors.get(wave, '0'), color_on)} "
                    f"{_color(f'[{imp.upper()}]', imp_colors.get(imp, '0'), color_on)} "
                    f"{p_lbl}  {item.get('display_id', '')}  {title}{extra_txt}"
                )

    recent = plan.get("recently_integrated") or {}
    recent_items = recent.get("items") or []
    total_done = int(recent.get("total_done_count") or 0)
    if recent_items or total_done > 0:
        lines.append(_color("─" * bar_w, "2;36", color_on))
        lines.append(
            _color(f"Recently Integrated (showing {len(recent_items)} of {total_done}):", "1;32", color_on)
        )
        for d_it in recent_items:
            d_repos = ",".join(d_it.get("repo_slugs") or []) or "-"
            d_title = str(d_it.get("sidebar_title") or d_it.get("title") or d_it.get("display_id") or "")
            lines.append(
                f"  ✓ {d_it.get('display_id', '')} [{d_it.get('status', 'merged')}] "
                f"{d_repos} / {d_it.get('component', 'general')}  {d_title}"
            )
    lines.append(_color("═" * bar_w, "2;36", color_on))
    return lines
