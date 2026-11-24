"""Lock/item progress computation, PR URL richness ranking, and link resolution (`tui_progress.py`)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import re
import subprocess
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from tui_models import (
    _CREATED_PR_PATH_RE,
    AGENT_REVIEW_LOCK_STATUSES,
    IDLE_AMBER_THRESHOLD_SECONDS,
    INACTIVE_STATUSES,
    _facade_attr,
    _flow_opt,
)
from tui_state import (
    _check_local_remote_branch_state,
    _extract_repo_slug_from_url,
    _find_live_unmerged_remote_branch_for_cid,
    _format_compact_age,
    _is_lock_base_stale,
    is_lock_human_ready,
)


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
    except (OSError, subprocess.SubprocessError):
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
        except (OSError, subprocess.SubprocessError):
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
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
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

    is_human_ready = _facade_attr("is_lock_human_ready", is_lock_human_ready)(lk)
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
            tag = stage_label[:8] if stage_label else "ok·0s"
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

    lock_prog_fn = _facade_attr("_compute_lock_progress", _compute_lock_progress)
    for slug, lk in linked_locks:
        p_key = _slot_progress_cache_key(slug, lk)
        lp = lock_progress_cache.get(p_key)
        if lp is None:
            lp = lock_prog_fn(lk, now_ts=now_ts)
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


def _sanitize_full_url(url: str) -> str:
    """Validate and clean a full clickable HTTP(S) URL while preserving `?title=...&body=...` params."""
    raw = re.sub(r"[\x00-\x1f\x7f]", "", (url or "").strip())
    if not raw or not raw.startswith(("http://", "https://")):
        return ""
    return raw


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
    """Build the Pull/Merge Requests listing URL for `slug`."""
    if slug in repo_web_bases:
        provider, web_base = repo_web_bases[slug]
        if provider == "gitlab":
            return f"{web_base}/-/merge_requests"
        if provider == "bitbucket":
            return f"{web_base}/pull-requests"
        return f"{web_base}/pulls"
    return ""


def _repo_compare_url(slug: str, branch: str, repo_web_bases: dict[str, tuple[str, str]]) -> str:
    """Build a VCS compare/create-PR URL for `branch` on `slug`."""
    if not branch or slug not in repo_web_bases:
        return ""
    provider, web_base = repo_web_bases[slug]
    if provider == "gitlab":
        return f"{web_base}/-/merge_requests/new?merge_request[source_branch]={branch}"
    if provider == "bitbucket":
        return f"{web_base}/pull-requests/new?source={branch}"
    return f"{web_base}/compare/main...{branch}?expand=1"


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


def _resolve_lock_human_pr_link(
    slug: str,
    lk: dict[str, Any],
    repo_web_bases: dict[str, tuple[str, str]],
    repo_local_dirs: dict[str, Path],
    extra_urls: list[str] | None = None,
) -> tuple[str, str, bool, str]:
    """Resolve the richest validated human PR URL for an active lock, honoring `pr_gate_ready`."""
    if not _facade_attr("is_lock_human_ready", is_lock_human_ready)(lk):
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
    branch_state_fn = _facade_attr("_check_local_remote_branch_state", _check_local_remote_branch_state)
    gstate = branch_state_fn(slug, br, repo_local_dirs) if br else "unknown"
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

    human_ready_fn = _facade_attr("is_lock_human_ready", is_lock_human_ready)
    lock_link_fn = _facade_attr("_resolve_lock_human_pr_link", _resolve_lock_human_pr_link)
    branch_state_fn = _facade_attr("_check_local_remote_branch_state", _check_local_remote_branch_state)
    live_branch_fn = _facade_attr("_find_live_unmerged_remote_branch_for_cid", _find_live_unmerged_remote_branch_for_cid)

    unready_locked_slugs: set[str] = set()
    for slug, lk in locks_by_cid.get(cid, []):
        if slug in merged_set:
            continue
        if not human_ready_fn(lk):
            unready_locked_slugs.add(slug)
            continue
        kind, u, is_pr, gstate = lock_link_fn(
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
                gstate = branch_state_fn(slug, br, repo_local_dirs)
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
            live_br = live_branch_fn(slug, cid8, repo_local_dirs)
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
