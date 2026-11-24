#!/usr/bin/env python3
"""Pure-Python 0-LLM component planning view for `dev_queue.json` (`dev_queue_planner.py`).

Groups queue items hierarchically as `Repo -> Sub-Component`, separates business
importance (`critical|high|medium|low`) from execution wave
(`active -> partial_wip -> ready -> blocked -> standby`), surfaces reverse-dependency
`Unblocks N` / `Blocked by` links, and preserves a bounded `Recently Integrated`
history of `merged`/`cleaned` items with `total_done_count`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
import re
import sys
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

IMPORTANCE_TIERS: tuple[str, ...] = ("critical", "high", "medium", "low")
IMPORTANCE_RANK: dict[str, int] = {tier: idx for idx, tier in enumerate(IMPORTANCE_TIERS)}

EXECUTION_WAVES: tuple[str, ...] = ("active", "partial_wip", "ready", "blocked", "standby")
WAVE_RANK: dict[str, int] = {wave: idx for idx, wave in enumerate(EXECUTION_WAVES)}

DONE_STATUSES: frozenset[str] = frozenset({"merged", "cleaned", "cancelled", "rejected"})
INTEGRATED_STATUSES: frozenset[str] = frozenset({"merged", "cleaned"})
OPEN_PRIORITY_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "pr_open", "hitl_blocked"})
ACTIVE_EXECUTION_STATUSES: frozenset[str] = frozenset(
    {
        "in_progress",
        "pr_open",
        "ready_awaiting_base_merge",
        "needs_rebase_reverify",
        "needs_agent_semantic_fix",
        "needs_agent_conflict_resolution",
    }
)

_LOCAL_HOME_PATH_RE = re.compile(r"/(?:Users|home)/(?!testuser\b|test\b|Shared\b)[A-Za-z0-9._-]+")
_ANCHOR_TAG_RE = re.compile(r"(?:^|[\s\[(\{:])#([A-Za-z][A-Za-z0-9_/-]{1,40})")
_CONTAINER_DIRS: frozenset[str] = frozenset(
    {
        "src",
        "lib",
        "libs",
        "pkg",
        "pkgs",
        "packages",
        "apps",
        "services",
        "modules",
        "internal",
        "cmd",
        "skills",
        "sidecars",
        "plugins",
        "components",
    }
)
_TEST_ROOT_DIRS: frozenset[str] = frozenset({"tests", "test", "__tests__", "spec"})


def _sanitize_text(text: str) -> str:
    """Scrub local home directory usernames from text for Zero-PII safety."""
    raw = str(text or "")
    if not raw:
        return ""
    home_str = str(Path.home())
    if home_str and home_str != "/":
        raw = raw.replace(home_str, "$HOME")
    return _LOCAL_HOME_PATH_RE.sub("$HOME", raw)


def _matches_cid(cand: str, ref: str) -> bool:
    """Return True when two conversation IDs match case-insensitively or by 8-char hex prefix."""
    c_clean = str(cand or "").strip().lower()
    r_clean = str(ref or "").strip().lower()
    if not c_clean or not r_clean:
        return False
    if c_clean == r_clean:
        return True
    if len(r_clean) >= 8 and c_clean.startswith(r_clean):
        return True
    if len(c_clean) >= 8 and r_clean.startswith(c_clean):
        return True
    return False


def _raw_priority(item: Mapping[str, Any]) -> int:
    """Return integer priority for `item` (default 50)."""
    raw = item.get("priority", 50)
    try:
        return int(raw) if raw is not None else 50
    except (TypeError, ValueError):
        return 50


def normalize_importance_tier(raw_value: Any) -> str:
    """Normalize an explicit importance string to `critical|high|medium|low` or `''`."""
    cleaned = str(raw_value or "").strip().lower()
    return cleaned if cleaned in IMPORTANCE_RANK else ""


def normalize_component_name(raw_value: Any) -> str:
    """Normalize and Zero-PII sanitize an explicit sub-component label."""
    cleaned = " ".join(_sanitize_text(str(raw_value or "")).split()).strip()
    return cleaned[:64]


def _normalize_repo_list(raw_repos: Any) -> list[str]:
    """Normalize repo slugs while preserving order and uniqueness."""
    out: list[str] = []
    seen: set[str] = set()
    for r in raw_repos or ():
        norm = dev_common.normalize_repo_slug(str(r or ""))
        if norm and norm != "default" and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def _is_partial_wip_item(item: Mapping[str, Any]) -> bool:
    """Return True when a multi-repo item has merged at least one repo and has remaining repos."""
    repos = _normalize_repo_list(item.get("repo_slugs"))
    merged = set(_normalize_repo_list(item.get("merged_repos")))
    return bool(merged) and (not repos or len(merged & set(repos)) < len(repos))


def _compute_effective_priority_map(items: Sequence[Mapping[str, Any]]) -> dict[str, tuple[int, str]]:
    """Compute cycle-safe transitive `(effective_priority, inherited_from)` for open items."""
    open_by_cid: dict[str, Mapping[str, Any]] = {}
    for it in items:
        cid = str(it.get("conversation_id") or "").strip()
        st = str(it.get("status") or "pending").strip().lower()
        if cid and st in OPEN_PRIORITY_STATUSES:
            open_by_cid[cid] = it

    dependents_map: dict[str, list[str]] = {cid: [] for cid in open_by_cid}
    open_cids = list(open_by_cid.keys())
    for dep_cid, dep_item in open_by_cid.items():
        for raw_ref in dep_item.get("depends_on") or ():
            ref_clean = str(raw_ref or "").strip()
            if not ref_clean:
                continue
            matched = [c for c in open_cids if c.lower() == ref_clean.lower() and c != dep_cid]
            if not matched:
                matched = [c for c in open_cids if _matches_cid(c, ref_clean) and c != dep_cid]
            for target_cid in matched:
                if dep_cid not in dependents_map[target_cid]:
                    dependents_map[target_cid].append(dep_cid)

    result: dict[str, tuple[int, str]] = {}
    for it in items:
        cid = str(it.get("conversation_id") or "").strip()
        raw_p = _raw_priority(it)
        if not cid or cid not in open_by_cid:
            if cid:
                result[cid] = (raw_p, "")
            continue
        best_dep_key: tuple[int, str, str] | None = None
        visited: set[str] = {cid}
        frontier: list[str] = list(dependents_map.get(cid, ()))
        while frontier:
            cur = frontier.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            cur_item = open_by_cid.get(cur)
            if cur_item is None:
                continue
            cur_p = _raw_priority(cur_item)
            cand_key = (cur_p, str(cur_item.get("added_at") or ""), cur)
            if best_dep_key is None or cand_key < best_dep_key:
                best_dep_key = cand_key
            for nxt in dependents_map.get(cur, ()):
                if nxt not in visited:
                    frontier.append(nxt)
        if best_dep_key is not None and best_dep_key[0] < raw_p:
            result[cid] = (best_dep_key[0], best_dep_key[2])
        else:
            result[cid] = (raw_p, "")
    return result


def infer_importance(
    item: Mapping[str, Any],
    *,
    blocks_count: int = 0,
    effective_status: str = "",
    effective_priority: int | None = None,
) -> dict[str, Any]:
    """Infer deterministic business importance (`critical|high|medium|low`) with explicit reasons.

    Explicit `item['importance']` takes precedence over deterministic fallback rules.
    """
    explicit = normalize_importance_tier(item.get("importance"))
    if explicit:
        return {
            "importance": explicit,
            "source": "explicit",
            "reasons": [f"explicit:{explicit}"],
        }

    eff_st = str(effective_status or item.get("status") or "pending").strip().lower()
    if effective_priority is not None:
        eff_prio = int(effective_priority)
    elif item.get("effective_priority") is not None:
        try:
            eff_prio = int(item["effective_priority"])
        except (TypeError, ValueError):
            eff_prio = _raw_priority(item)
    else:
        eff_prio = _raw_priority(item)

    partial_wip = _is_partial_wip_item(item)
    reasons: list[str] = []

    if eff_prio <= 17:
        reasons.append(f"priority_band:P{eff_prio}<=17")
    if blocks_count >= 2:
        reasons.append(f"blocks_count:{blocks_count}>=2")
    elif blocks_count == 1:
        reasons.append("blocks_count:1")
    if partial_wip:
        reasons.append("partial_wip")
    if eff_st == "hitl_blocked":
        reasons.append("status:hitl_blocked")
    elif eff_st in ACTIVE_EXECUTION_STATUSES:
        reasons.append(f"status:{eff_st}")

    if eff_prio <= 17 or blocks_count >= 2 or (eff_st == "hitl_blocked" and eff_prio <= 25):
        tier = "critical"
    elif blocks_count >= 1 or partial_wip or eff_st in ACTIVE_EXECUTION_STATUSES or eff_st == "hitl_blocked" or eff_prio <= 39:
        tier = "high"
        if eff_prio <= 39:
            reasons.append(f"priority_band:P{eff_prio}<=39")
    elif eff_prio <= 69:
        tier = "medium"
        reasons.append(f"priority_band:P{eff_prio}<=69")
    else:
        tier = "low"
        reasons.append(f"priority_band:P{eff_prio}>=70")

    if not reasons:
        reasons.append(f"priority_band:P{eff_prio}")

    return {
        "importance": tier,
        "source": "inferred",
        "reasons": reasons,
    }


def _extract_component_from_path(raw_path: str) -> tuple[str, bool]:
    """Return `(component_name, is_test_only)` from a file path with Zero-PII scrubbing."""
    clean = _sanitize_text(str(raw_path or "")).strip().replace("\\", "/")
    if not clean:
        return "", False
    # Strip file path prefixes like `$HOME/...` or `/abs/...` down to repo-relative segment where possible
    if "::" in clean:
        clean = clean.split("::", 1)[0]
    clean = clean.lstrip("./")
    parts = [p for p in clean.split("/") if p and p not in (".", "..")]
    if not parts:
        return "", False

    # Drop leading `$HOME` or absolute workspace path segments up to known root markers
    if parts[0] == "$HOME":
        parts = parts[1:]
        # Also skip common workspace prefixes like Code/Github/<owner>/<repo>/...
        if len(parts) >= 4 and parts[0].lower() in ("code", "repos", "workspace", "projects"):
            for idx in range(1, len(parts) - 1):
                if parts[idx].lower() in _CONTAINER_DIRS or parts[idx].lower() in _TEST_ROOT_DIRS:
                    parts = parts[idx:]
                    break
            else:
                if len(parts) > 3:
                    parts = parts[3:]
    if not parts:
        return "", False

    first = parts[0]
    first_lower = first.lower()
    is_test = first_lower in _TEST_ROOT_DIRS

    if is_test:
        # Derive feature hint from test filename if no non-test files exist
        stem = Path(parts[-1]).stem.lower()
        for prefix in ("test_", "spec_"):
            if stem.startswith(prefix):
                stem = stem[len(prefix) :]
        return (stem or "tests"), True

    if len(parts) >= 2 and first_lower in _CONTAINER_DIRS:
        second = parts[1]
        # Avoid returning raw filename if second is a file and len == 2, keep stem if needed
        if len(parts) == 2 and "." in second:
            return first, False
        return second, False

    if len(parts) == 1:
        return "core", False

    return first, False


def infer_subcomponent(item: Mapping[str, Any]) -> dict[str, str]:
    """Infer deterministic Zero-PII sub-component from explicit `component`, `#anchor` titles, or `target_files`."""
    explicit = normalize_component_name(item.get("component"))
    if explicit:
        return {"component": explicit, "source": "explicit"}

    title = _sanitize_text(str(item.get("title") or ""))
    anchor_match = _ANCHOR_TAG_RE.search(title)
    if anchor_match:
        anchor = normalize_component_name(anchor_match.group(1).strip("/"))
        if anchor:
            return {"component": anchor, "source": "anchor"}

    raw_paths: list[str] = [str(p) for p in (item.get("target_files") or ()) if str(p or "").strip()]
    if not raw_paths:
        raw_paths = [str(a) for a in (item.get("section_anchors") or ()) if "::" in str(a or "") or "/" in str(a or "")]

    non_test_counts: Counter[str] = Counter()
    test_counts: Counter[str] = Counter()
    for p in raw_paths:
        comp, is_test = _extract_component_from_path(p)
        if not comp:
            continue
        if is_test:
            test_counts[comp] += 1
        else:
            non_test_counts[comp] += 1

    if non_test_counts:
        best_comp = sorted(non_test_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[0][0]
        return {"component": normalize_component_name(best_comp) or "general", "source": "path"}
    if test_counts:
        best_comp = sorted(test_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[0][0]
        return {"component": normalize_component_name(best_comp) or "tests", "source": "path"}

    return {"component": "general", "source": "default"}


def classify_execution_wave(
    item: Mapping[str, Any],
    *,
    effective_status: str = "",
    open_unmet_deps: Sequence[Any] = (),
    has_active_lock: bool = False,
) -> str:
    """Classify an open queue item into `active -> partial_wip -> ready -> blocked -> standby`."""
    eff_st = str(effective_status or item.get("status") or "pending").strip().lower()

    if eff_st == "standby":
        return "standby"
    if eff_st == "hitl_blocked" or len(open_unmet_deps) > 0:
        return "blocked"
    if eff_st in ACTIVE_EXECUTION_STATUSES or bool(has_active_lock):
        return "active"
    if _is_partial_wip_item(item):
        return "partial_wip"
    return "ready"


def _empty_wave_counts() -> dict[str, int]:
    return {w: 0 for w in EXECUTION_WAVES}


def _empty_importance_counts() -> dict[str, int]:
    return {t: 0 for t in IMPORTANCE_TIERS}


def _select_safe_pr_url(item: Mapping[str, Any], *, is_done: bool, is_human_pr: bool = False) -> str:
    """Return a safe PR URL for done items or human-ready PR items; never leak unreviewed compare URLs."""
    explicit_pr = str(item.get("pr_url") or "").strip()
    if explicit_pr and (is_done or is_human_pr):
        return explicit_pr
    urls = [str(u).strip() for u in (item.get("pr_urls") or ()) if str(u or "").strip()]
    if not urls:
        return ""
    if is_done:
        for u in urls:
            if "/pull/" in u or "/pull-requests/" in u or "/merge_requests/" in u:
                return u
        return urls[0]
    if is_human_pr:
        return urls[0]
    return ""


def build_component_planning_snapshot(
    items: Sequence[Mapping[str, Any]],
    *,
    locks: Mapping[str, Mapping[str, Any]] | None = None,
    locks_by_cid: Mapping[str, Sequence[tuple[str, Mapping[str, Any]]]] | None = None,
    repo_labels: Mapping[str, str] | None = None,
    repo_filter: str = "",
    importance_filter: str = "",
    include_done: int = 10,
    ag_titles: Mapping[str, str] | None = None,
    effective_statuses: Mapping[str, str] | None = None,
    human_pr_urls: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build hierarchical `Repo -> Sub-Component` planning view with 0-LLM deterministic metrics."""
    raw_items = [dict(it) for it in items if isinstance(it, Mapping) and str(it.get("conversation_id") or "").strip()]
    eff_prio_map = _compute_effective_priority_map(raw_items)

    # Index all items by CID for dependency / reverse-dependency resolution across the full queue
    by_cid: dict[str, dict[str, Any]] = {}
    status_by_cid: dict[str, str] = {}
    for it in raw_items:
        cid = str(it.get("conversation_id") or "").strip()
        by_cid[cid] = it
        eff_st = str((effective_statuses or {}).get(cid) or it.get("status") or "pending").strip().lower()
        status_by_cid[cid] = eff_st

    def _resolve_cid(ref: str) -> str:
        ref_clean = str(ref or "").strip()
        if not ref_clean:
            return ""
        for cand in by_cid:
            if cand.lower() == ref_clean.lower():
                return cand
        for cand in by_cid:
            if _matches_cid(cand, ref_clean):
                return cand
        return ""

    # Build open dependency links (full graph before filtering)
    blocked_by_map: dict[str, list[dict[str, str]]] = {cid: [] for cid in by_cid}
    unblocks_map: dict[str, list[dict[str, str]]] = {cid: [] for cid in by_cid}
    for cid, it in by_cid.items():
        if status_by_cid[cid] in DONE_STATUSES:
            continue
        seen_deps: set[str] = set()
        for raw_ref in it.get("depends_on") or ():
            target_cid = _resolve_cid(str(raw_ref or ""))
            if not target_cid or target_cid == cid or target_cid in seen_deps:
                continue
            target_st = status_by_cid.get(target_cid, "")
            if target_st in INTEGRATED_STATUSES:
                continue
            seen_deps.add(target_cid)
            target_item = by_cid[target_cid]
            target_title = _sanitize_text(
                str((ag_titles or {}).get(target_cid) or target_item.get("title") or target_cid[:8])
            )
            blocked_by_map[cid].append(
                {
                    "conversation_id": target_cid,
                    "display_id": target_cid[:8],
                    "title": target_title,
                    "status": target_st,
                }
            )
            src_title = _sanitize_text(str((ag_titles or {}).get(cid) or it.get("title") or cid[:8]))
            unblocks_map[target_cid].append(
                {
                    "conversation_id": cid,
                    "display_id": cid[:8],
                    "title": src_title,
                    "status": status_by_cid[cid],
                }
            )

    norm_repo_filter = str(repo_filter or "").strip().lower()
    norm_imp_filter = normalize_importance_tier(importance_filter)
    try:
        done_limit = max(0, int(include_done))
    except (TypeError, ValueError):
        done_limit = 10

    def _item_matches_repo_filter(item_repos: list[str], assigned_repo: str) -> bool:
        if not norm_repo_filter:
            return True
        if norm_repo_filter in assigned_repo.lower():
            return True
        return any(norm_repo_filter in r.lower() for r in item_repos)

    # Active locks lookup helper when locks_by_cid isn't supplied directly
    lock_cids_map: dict[str, list[str]] = {}
    if locks and not locks_by_cid:
        for slug, lk in locks.items():
            if not isinstance(lk, Mapping) or not bool(lk.get("active", True)):
                continue
            cands: list[str] = []
            if lk.get("conversation_id"):
                cands.append(str(lk["conversation_id"]).strip())
            for sib in lk.get("sibling_conversation_ids") or ():
                if str(sib or "").strip():
                    cands.append(str(sib).strip())
            slots = lk.get("slots") if isinstance(lk.get("slots"), Mapping) else {}
            for s_obj in slots.values():
                if isinstance(s_obj, Mapping) and bool(s_obj.get("active", True)):
                    if s_obj.get("conversation_id"):
                        cands.append(str(s_obj["conversation_id"]).strip())
                    for sib in s_obj.get("sibling_conversation_ids") or ():
                        if str(sib or "").strip():
                            cands.append(str(sib).strip())
            for lcid in cands:
                for qcid in by_cid:
                    if _matches_cid(lcid, qcid):
                        lock_cids_map.setdefault(qcid, []).append(slug)

    # Group open items and integrated (merged/cleaned) items
    repo_comp_open: dict[str, dict[str, list[dict[str, Any]]]] = {}
    repo_comp_done_counts: dict[str, dict[str, int]] = {}
    repo_done_counts: dict[str, int] = {}
    done_records: list[dict[str, Any]] = []
    totals_wave = _empty_wave_counts()
    totals_imp = _empty_importance_counts()
    open_total = 0

    for it in raw_items:
        cid = str(it.get("conversation_id") or "").strip()
        eff_st = status_by_cid[cid]
        item_repos = _normalize_repo_list(it.get("repo_slugs"))
        merged_repos = _normalize_repo_list(it.get("merged_repos"))
        merged_set = set(merged_repos)
        remaining_repos = [r for r in item_repos if r not in merged_set]

        comp_info = infer_subcomponent(it)
        comp_name = comp_info["component"]
        raw_p = _raw_priority(it)
        eff_p, inherited_from = eff_prio_map.get(cid, (raw_p, ""))
        if it.get("effective_priority") is not None:
            with_err = False
            try:
                eff_p = int(it["effective_priority"])
            except (TypeError, ValueError):
                with_err = True
            if not with_err and not inherited_from:
                inherited_from = str(it.get("inherited_priority_from") or "")

        unblocks_list = unblocks_map.get(cid, [])
        blocked_by_list = blocked_by_map.get(cid, [])
        blocks_count = len(unblocks_list)
        imp_info = infer_importance(
            it,
            blocks_count=blocks_count,
            effective_status=eff_st,
            effective_priority=eff_p,
        )
        imp_tier = imp_info["importance"]

        if eff_st in INTEGRATED_STATUSES:
            if norm_repo_filter and not any(norm_repo_filter in r.lower() for r in (item_repos or ["unassigned"])):
                continue
            target_repos = item_repos or ["unassigned"]
            for rslug in target_repos:
                if norm_repo_filter and norm_repo_filter not in rslug.lower():
                    continue
                repo_done_counts[rslug] = repo_done_counts.get(rslug, 0) + 1
                comp_map = repo_comp_done_counts.setdefault(rslug, {})
                comp_map[comp_name] = comp_map.get(comp_name, 0) + 1

            done_records.append(
                {
                    "conversation_id": cid,
                    "display_id": cid[:8],
                    "title": _sanitize_text(" ".join(str(it.get("title") or "").split())),
                    "sidebar_title": _sanitize_text(str((ag_titles or {}).get(cid) or "")),
                    "status": eff_st,
                    "repo_slugs": item_repos,
                    "component": comp_name,
                    "importance": imp_tier,
                    "pr_url": _select_safe_pr_url(it, is_done=True),
                    "updated_at": str(it.get("updated_at") or it.get("added_at") or ""),
                }
            )
            continue

        if eff_st in DONE_STATUSES:
            continue

        # Open / hold item: choose primary remaining repo (or matching filtered repo)
        if norm_repo_filter:
            matching_repos = [r for r in (remaining_repos or item_repos) if norm_repo_filter in r.lower()]
            if not matching_repos:
                continue
            primary_repo = matching_repos[0]
        else:
            primary_repo = remaining_repos[0] if remaining_repos else (item_repos[0] if item_repos else "unassigned")

        if not _item_matches_repo_filter(item_repos, primary_repo):
            continue
        if norm_imp_filter and imp_tier != norm_imp_filter:
            continue

        has_lock = bool((locks_by_cid or {}).get(cid)) or bool(lock_cids_map.get(cid))
        wave = classify_execution_wave(
            it,
            effective_status=eff_st,
            open_unmet_deps=blocked_by_list,
            has_active_lock=has_lock,
        )

        safe_human_pr = str((human_pr_urls or {}).get(cid) or "").strip()
        is_human_pr = bool(safe_human_pr)
        pr_url_val = safe_human_pr if is_human_pr else _select_safe_pr_url(it, is_done=False, is_human_pr=False)

        unblocks_label = f"Unblocks {blocks_count}" if blocks_count > 0 else ""
        blocked_by_label = (
            "Blocked by " + ", ".join(b["display_id"] for b in blocked_by_list[:3])
            if blocked_by_list
            else ""
        )
        if len(blocked_by_list) > 3:
            blocked_by_label += f" +{len(blocked_by_list) - 3}"

        item_entry: dict[str, Any] = {
            "conversation_id": cid,
            "display_id": cid[:8],
            "title": _sanitize_text(" ".join(str(it.get("title") or "").split())),
            "sidebar_title": _sanitize_text(str((ag_titles or {}).get(cid) or "")),
            "status": str(it.get("status") or "pending").strip().lower(),
            "effective_status": eff_st,
            "priority": raw_p,
            "effective_priority": eff_p,
            "inherited_priority_from": inherited_from,
            "importance": imp_tier,
            "importance_source": imp_info["source"],
            "importance_reasons": list(imp_info["reasons"]),
            "component": comp_name,
            "component_source": comp_info["source"],
            "wave": wave,
            "repo_slugs": item_repos,
            "merged_repos": merged_repos,
            "depends_on": [str(d).strip()[:8] for d in (it.get("depends_on") or ()) if str(d).strip()],
            "blocked_by": blocked_by_list,
            "unblocks": unblocks_list,
            "blocks_count": blocks_count,
            "unblocks_label": unblocks_label,
            "blocked_by_label": blocked_by_label,
            "target_files": [_sanitize_text(str(p)) for p in (it.get("target_files") or ()) if str(p or "").strip()],
            "pr_url": pr_url_val,
            "updated_at": str(it.get("updated_at") or it.get("added_at") or ""),
            "_added_at": str(it.get("added_at") or ""),
        }

        repo_comp_open.setdefault(primary_repo, {}).setdefault(comp_name, []).append(item_entry)
        totals_wave[wave] = totals_wave.get(wave, 0) + 1
        totals_imp[imp_tier] = totals_imp.get(imp_tier, 0) + 1
        open_total += 1

    # Assemble sorted Repo -> Sub-Component hierarchy
    repos_out: list[dict[str, Any]] = []
    components_total = 0

    for rslug, comp_buckets in repo_comp_open.items():
        comp_list: list[dict[str, Any]] = []
        r_wave = _empty_wave_counts()
        r_imp = _empty_importance_counts()
        r_open = 0

        for cname, c_items in comp_buckets.items():
            c_items_sorted = sorted(
                c_items,
                key=lambda x: (
                    WAVE_RANK.get(x["wave"], 9),
                    IMPORTANCE_RANK.get(x["importance"], 9),
                    int(x["effective_priority"]),
                    int(x["priority"]),
                    str(x.get("_added_at") or ""),
                    str(x["conversation_id"]),
                ),
            )
            for ci in c_items_sorted:
                ci.pop("_added_at", None)

            c_wave = _empty_wave_counts()
            c_imp = _empty_importance_counts()
            for ci in c_items_sorted:
                c_wave[ci["wave"]] += 1
                c_imp[ci["importance"]] += 1
                r_wave[ci["wave"]] += 1
                r_imp[ci["importance"]] += 1

            c_open = len(c_items_sorted)
            c_done = int(repo_comp_done_counts.get(rslug, {}).get(cname, 0))
            c_tot = c_open + c_done
            c_pct = int(round((c_done / c_tot) * 100)) if c_tot > 0 else 0
            best_imp = min(
                (IMPORTANCE_RANK.get(ci["importance"], 9) for ci in c_items_sorted),
                default=9,
            )
            highest_imp = IMPORTANCE_TIERS[best_imp] if best_imp < len(IMPORTANCE_TIERS) else "medium"
            best_wave = min((WAVE_RANK.get(ci["wave"], 9) for ci in c_items_sorted), default=9)
            best_prio = min((int(ci["effective_priority"]) for ci in c_items_sorted), default=99)

            comp_list.append(
                {
                    "name": cname,
                    "open_count": c_open,
                    "done_count": c_done,
                    "total_count": c_tot,
                    "completion_pct": c_pct,
                    "by_wave": c_wave,
                    "by_importance": c_imp,
                    "highest_importance": highest_imp,
                    "items": c_items_sorted,
                    "_sort_key": (best_wave, best_imp, best_prio, -c_open, cname.lower()),
                }
            )
            r_open += c_open
            components_total += 1

        comp_list.sort(key=lambda c: c["_sort_key"])
        r_best_key = comp_list[0]["_sort_key"] if comp_list else (9, 9, 99, 0, "")
        for c in comp_list:
            c.pop("_sort_key", None)

        r_done = int(repo_done_counts.get(rslug, 0))
        r_tot = r_open + r_done
        r_pct = int(round((r_done / r_tot) * 100)) if r_tot > 0 else 0
        r_label = str((repo_labels or {}).get(rslug) or (rslug.split("/")[-1] if "/" in rslug else rslug))

        repos_out.append(
            {
                "repo_slug": rslug,
                "repo_label": _sanitize_text(r_label),
                "open_count": r_open,
                "done_count": r_done,
                "total_count": r_tot,
                "completion_pct": r_pct,
                "by_wave": r_wave,
                "by_importance": r_imp,
                "components": comp_list,
                "_sort_key": (r_best_key[0], r_best_key[1], r_best_key[2], -r_open, rslug.lower()),
            }
        )

    repos_out.sort(key=lambda r: r["_sort_key"])
    for r in repos_out:
        r.pop("_sort_key", None)

    done_records_sorted = sorted(
        done_records,
        key=lambda d: (str(d.get("updated_at") or ""), str(d.get("conversation_id") or "")),
        reverse=True,
    )
    total_done_count = len(done_records_sorted)

    return {
        "schema_version": 1,
        "totals": {
            "open_count": open_total,
            "done_count": total_done_count,
            "total_done_count": total_done_count,
            "repos_count": len(repos_out),
            "components_count": components_total,
            "by_wave": totals_wave,
            "by_importance": totals_imp,
        },
        "filters": {
            "repo": norm_repo_filter,
            "importance": norm_imp_filter,
            "include_done": done_limit,
        },
        "repos": repos_out,
        "recently_integrated": {
            "total_done_count": total_done_count,
            "items": done_records_sorted[:done_limit] if done_limit > 0 else [],
        },
    }
