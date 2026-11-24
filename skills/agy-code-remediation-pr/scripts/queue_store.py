#!/usr/bin/env python3
"""Persistent orchestrator dev queue (`dev_queue.json`), priority ranking & git reconciliation (`queue_store.py`).

Extracted from `pr_remediator.py` to separate `dev_queue.json` CRUD, dependency-aware
priority scoring, `queue-next` unlocked lookahead, and readonly git merge reconciliation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
import urllib.parse

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
from git_client import GitVcsIntegrationClient  # noqa: E402
from lock_store import (  # noqa: E402
    _branch_found_in_base_git_log,
    _entry_matches_branch,
    _facade_attr,
    _inspect_local_branch_merged_readonly,
    _is_same_conversation_id,
    _resolve_lock_target_repo_dir,
    _resolve_main_git_repo_root,
    _resolve_max_repo_slots,
    _resolve_repo_dir_for_slug,
    _resolve_repo_slug_and_dir,
    _sanitize_home_paths,
    _utc_now_iso,
    get_all_dev_pr_locks,
)
from dev_queue_planner import (  # noqa: E402
    IMPORTANCE_TIERS,
    build_component_planning_snapshot,
    normalize_component_name,
    normalize_importance_tier,
)
from parallel_scope import analyze_candidate_against_slots  # noqa: E402
import orchestration_control as _orch_ctrl  # noqa: E402

DEV_QUEUE_FILENAME = "dev_queue.json"
DEV_QUEUE_STATUSES: tuple[str, ...] = (
    "pending",
    "in_progress",
    "pr_open",
    "merged",
    "standby",
    "hitl_blocked",
    "cleaned",
    "cancelled",
    "rejected",
)
DEV_QUEUE_DONE_STATUSES: tuple[str, ...] = ("merged", "cleaned", "rejected", "cancelled")
DEV_QUEUE_HOLD_STATUSES: tuple[str, ...] = ("standby", "hitl_blocked")
DEV_QUEUE_OPEN_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "pr_open", "hitl_blocked"})
INFRA_PRIORITY_PRECEDENCE_MAX = 17
DEFAULT_MAX_PARTIAL_WIP = 3
DEV_QUEUE_STATUS_RANK: dict[str, int] = {
    "in_progress": 0,
    "pr_open": 1,
    "pending": 2,
    "hitl_blocked": 3,
    "standby": 4,
    "merged": 5,
    "cleaned": 6,
    "cancelled": 7,
    "rejected": 7,
}
_CONVERSATION_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


def _atomic_write_json_0600(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write JSON with strict 0600 permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, path)


def _normalize_str_list(values: list[str] | tuple[str, ...] | None) -> list[str]:
    """Deduplicate non-empty strings while preserving order and sanitizing home paths."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in values or ():
        clean = _sanitize_home_paths(str(raw or "").strip())
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _matches_cid(cand: str, ref: str) -> bool:
    """Return True if two conversation IDs match exactly or one is an 8-char hex short prefix of the other."""
    return _is_same_conversation_id(cand, ref)


def _lock_max_slots(lock_obj: Mapping[str, Any]) -> int:
    """Resolve a repository lock's slot capacity (`max_slots`, clamped `[1..4]`, default `3`)."""
    resolver = getattr(dev_common, "resolve_max_repo_slots", None)
    raw_max = lock_obj.get("max_slots") if isinstance(lock_obj, Mapping) else None
    if callable(resolver):
        return int(resolver(raw_max))
    return int(_resolve_max_repo_slots(raw_max))


def _active_lock_slot_specs(lock_obj: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return ordered active slot dicts (`slot_id` 1..N); Slot #1 is merged over the top-level lock mirror."""
    if not isinstance(lock_obj, Mapping) or not bool(lock_obj.get("active")):
        return []
    lock_dict = dict(lock_obj)
    iter_fn = getattr(dev_common, "iter_active_lock_slots", None)
    slots_out: list[dict[str, Any]] = []
    if callable(iter_fn):
        slots_out = [dict(s) for s in iter_fn(lock_dict) if isinstance(s, dict)]
    else:
        raw_slots = lock_dict.get("slots") if isinstance(lock_dict.get("slots"), dict) else {}
        for raw_k, raw_v in raw_slots.items():
            if isinstance(raw_v, dict) and bool(raw_v.get("active", True)):
                with contextlib.suppress(TypeError, ValueError):
                    slots_out.append({**raw_v, "slot_id": int(raw_v.get("slot_id") or raw_k)})
    top_level = {k: v for k, v in lock_dict.items() if k != "slots"}
    by_id: dict[int, dict[str, Any]] = {}
    for slot_obj in slots_out:
        with contextlib.suppress(TypeError, ValueError):
            by_id[int(slot_obj.get("slot_id") or 1)] = slot_obj
    # Slot #1 always mirrors the top-level lock (preserving any additive per-slot keys)
    by_id[1] = {**by_id.get(1, {}), **{k: v for k, v in top_level.items() if v not in (None, "", [], {})}}
    by_id[1]["slot_id"] = 1
    target_dir = str(lock_dict.get("target_repo_dir") or "")
    for s_id, slot_obj in by_id.items():
        slot_obj["slot_id"] = s_id
        slot_obj.setdefault("target_repo_dir", target_dir)
    return [by_id[k] for k in sorted(by_id)]


def _next_free_slot_id(active_slot_ids: Sequence[int], max_slots: int, reserved: Sequence[int] = ()) -> int:
    """Return the lowest free parallel slot id in `2..max_slots` (0 when the repository is at capacity)."""
    taken = {int(s) for s in active_slot_ids} | {int(s) for s in reserved}
    for cand_id in range(2, int(max_slots) + 1):
        if cand_id not in taken:
            return cand_id
    return 0


def get_dev_queue_path(portfolio_dir: Path | str | None = None) -> Path:
    """Return the path to `dev_queue.json` under the portfolio data directory."""
    return dev_common.resolve_state_dir(portfolio_dir=portfolio_dir) / DEV_QUEUE_FILENAME


def _empty_dev_queue(now_iso: str = "") -> dict[str, Any]:
    """Return a clean empty `dev_queue.json` document."""
    return {
        "schema_version": 1,
        "orchestrator_conversation_id": "",
        "updated_at": now_iso or _utc_now_iso(),
        "items": [],
    }


def _find_queue_item_index(items: list[dict[str, Any]], conversation_id: str) -> int:
    """Find the index of `conversation_id` in queue items via exact or unique >=8-char prefix match."""
    cid_clean = str(conversation_id or "").strip()
    if not cid_clean:
        return -1
    for idx, item in enumerate(items):
        if str(item.get("conversation_id") or "").strip().lower() == cid_clean.lower():
            return idx
    if len(cid_clean) >= 8:
        prefix_matches = [
            idx
            for idx, item in enumerate(items)
            if _matches_cid(str(item.get("conversation_id") or ""), cid_clean)
        ]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
    return -1


def _raw_queue_priority(item: Mapping[str, Any]) -> int:
    """Extract raw numeric priority from a queue item (default `50`)."""
    try:
        val = item.get("priority", 50)
        return int(val) if val is not None else 50
    except (TypeError, ValueError):
        return 50


def _resolve_prioritize_partial_wip() -> bool:
    """Return True when `AGY_DEV_PRIORITIZE_PARTIAL_WIP` enables 50% WIP-finish boost (default `1`)."""
    raw = str(os.environ.get("AGY_DEV_PRIORITIZE_PARTIAL_WIP", "1")).strip().lower()
    return raw not in ("0", "false", "no", "off")


def _resolve_max_partial_wip() -> int:
    """Resolve `AGY_DEV_MAX_PARTIAL_WIP` cap for half-merged downstream WIP (default `3`)."""
    raw = str(os.environ.get("AGY_DEV_MAX_PARTIAL_WIP") or "").strip()
    if raw:
        with contextlib.suppress(TypeError, ValueError):
            parsed = int(raw)
            if parsed >= 0:
                return parsed
    return DEFAULT_MAX_PARTIAL_WIP


def compute_effective_priorities(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Compute cycle-safe transitive `effective_priority` across open queue items.

    For every open item, `effective_priority` is the minimum of its own `priority` and all
    transitive open dependents' priorities (`depends_on` reverse reachability), breaking any
    dependency cycle via explicit per-source `visited` sets without recursion. Raw `priority`
    is strictly preserved while `effective_priority` and `inherited_priority_from` are stamped.
    """
    copied: list[dict[str, Any]] = [dict(it) for it in items]
    open_by_cid: dict[str, dict[str, Any]] = {}
    for it in copied:
        cid = str(it.get("conversation_id") or "").strip()
        st = str(it.get("status") or "pending").strip().lower()
        if cid and st in DEV_QUEUE_OPEN_STATUSES:
            open_by_cid[cid] = it

    # Build reverse dependency adjacency: prerequisite_cid -> [dependent_cid, ...]
    dependents_map: dict[str, list[str]] = {cid: [] for cid in open_by_cid}
    open_cids = list(open_by_cid.keys())
    for dep_cid, dep_item in open_by_cid.items():
        for raw_ref in dep_item.get("depends_on") or []:
            ref_clean = str(raw_ref or "").strip()
            if not ref_clean:
                continue
            matched_targets = [
                cand for cand in open_cids if cand.lower() == ref_clean.lower() and cand != dep_cid
            ]
            if not matched_targets:
                matched_targets = [
                    cand for cand in open_cids if _matches_cid(cand, ref_clean) and cand != dep_cid
                ]
            for target_cid in matched_targets:
                if dep_cid not in dependents_map[target_cid]:
                    dependents_map[target_cid].append(dep_cid)

    for it in copied:
        raw_p = _raw_queue_priority(it)
        cid = str(it.get("conversation_id") or "").strip()
        if cid not in open_by_cid:
            it["effective_priority"] = raw_p
            it["inherited_priority_from"] = ""
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
            cur_p = _raw_queue_priority(cur_item)
            cand_key = (cur_p, str(cur_item.get("added_at") or ""), cur)
            if best_dep_key is None or cand_key < best_dep_key:
                best_dep_key = cand_key
            for nxt in dependents_map.get(cur, ()):
                if nxt not in visited:
                    frontier.append(nxt)

        if best_dep_key is not None and best_dep_key[0] < raw_p:
            it["effective_priority"] = best_dep_key[0]
            it["inherited_priority_from"] = best_dep_key[2]
        else:
            it["effective_priority"] = raw_p
            it["inherited_priority_from"] = ""

    return copied


def _sort_dev_queue_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort queue items deterministically by lifecycle bucket, transitive `effective_priority`, raw `priority`, `added_at`, and CID."""
    enriched = compute_effective_priorities(items)
    open_cids_lower = {
        str(it.get("conversation_id") or "").strip(): str(it.get("status") or "pending").strip().lower()
        for it in enriched
        if str(it.get("conversation_id") or "").strip()
    }

    def _has_open_unmet_dep(it: dict[str, Any]) -> int:
        for raw_ref in it.get("depends_on") or []:
            ref_clean = str(raw_ref or "").strip()
            if not ref_clean:
                continue
            for cand_cid, cand_st in open_cids_lower.items():
                if _matches_cid(cand_cid, ref_clean) and cand_st not in ("merged", "cleaned"):
                    return 1
        return 0

    return sorted(
        enriched,
        key=lambda it: (
            DEV_QUEUE_STATUS_RANK.get(str(it.get("status") or "pending").lower(), 9),
            int(it.get("effective_priority", _raw_queue_priority(it))),
            _has_open_unmet_dep(it),
            _raw_queue_priority(it),
            str(it.get("added_at") or ""),
            str(it.get("conversation_id") or ""),
        ),
    )


def get_dev_queue_summary(items: list[dict[str, Any]]) -> dict[str, int]:
    """Count items in `dev_queue.json` by status."""
    counts: dict[str, int] = {st: 0 for st in DEV_QUEUE_STATUSES}
    counts["total"] = len(items)
    for it in items:
        st = str(it.get("status") or "pending").lower()
        counts[st] = counts.get(st, 0) + 1
    return counts


def load_dev_queue(portfolio_dir: Path | str | None = None) -> dict[str, Any]:
    """Load `dev_queue.json` safely with normalized schema and sorted items."""
    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    raw = dev_common.atomic_read_json(q_path, default={})
    if not isinstance(raw, dict) or not raw:
        data = _empty_dev_queue()
    else:
        items = [dict(it) for it in (raw.get("items") or []) if isinstance(it, dict) and str(it.get("conversation_id") or "").strip()]
        data = {
            "schema_version": int(raw.get("schema_version") or 1),
            "orchestrator_conversation_id": str(raw.get("orchestrator_conversation_id") or "").strip(),
            "updated_at": str(raw.get("updated_at") or _utc_now_iso()),
            "items": _sort_dev_queue_items(items),
        }
    data["summary"] = get_dev_queue_summary(data["items"])
    data["queue_path"] = str(q_path)
    return data


def _read_dev_queue_locked(q_path: Path) -> dict[str, Any]:
    """Read raw `dev_queue.json` while holding its file lock."""
    if not q_path.is_file():
        return _empty_dev_queue()
    try:
        raw = json.loads(q_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_dev_queue()
    if not isinstance(raw, dict):
        return _empty_dev_queue()
    items = [dict(it) for it in (raw.get("items") or []) if isinstance(it, dict) and str(it.get("conversation_id") or "").strip()]
    return {
        "schema_version": int(raw.get("schema_version") or 1),
        "orchestrator_conversation_id": str(raw.get("orchestrator_conversation_id") or "").strip(),
        "updated_at": str(raw.get("updated_at") or _utc_now_iso()),
        "items": items,
    }


def set_dev_queue_orchestrator(
    orchestrator_conversation_id: str,
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Persist the active coordinator conversation ID in `dev_queue.json` for restart recovery and cleanup protection."""
    orch_clean = str(orchestrator_conversation_id or "").strip()
    if not orch_clean:
        return {"status": "error", "error": "orchestrator_conversation_id is required"}
    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    with dev_common.file_lock(q_path.with_suffix(".json.lock")):
        data = _read_dev_queue_locked(q_path)
        data["orchestrator_conversation_id"] = orch_clean
        data["updated_at"] = _utc_now_iso()
        data["items"] = _sort_dev_queue_items(data["items"])
        _atomic_write_json_0600(q_path, data)
    return {
        "status": "ok",
        "queue_path": str(q_path),
        "orchestrator_conversation_id": orch_clean,
        "summary": get_dev_queue_summary(data["items"]),
    }


def _resolve_depends_on_update(
    existing: Sequence[str],
    merge_values: Sequence[str] | None,
    *,
    replace_depends_on: Sequence[str] | None = None,
    clear_depends_on: bool = False,
) -> list[str]:
    """Resolve the new `depends_on` list with precedence clear > replace > merge (default)."""
    if clear_depends_on:
        return []
    if replace_depends_on is not None:
        return _normalize_str_list(list(replace_depends_on))
    return _normalize_str_list([*(existing or []), *(merge_values or [])])


def enqueue_dev_queue_item(
    conversation_id: str,
    title: str = "",
    action_id: str = "",
    priority: int = 50,
    repo_slugs: list[str] | None = None,
    status: str = "pending",
    depends_on: list[str] | None = None,
    rca_summary: str = "",
    proposed_fix: str = "",
    target_files: list[str] | None = None,
    branch_names: list[str] | None = None,
    pr_urls: list[str] | None = None,
    subagent_conversation_ids: list[str] | None = None,
    notes: str = "",
    orchestrator_conversation_id: str = "",
    source: str = "manual",
    blocked_by_lock_id: str = "",
    portfolio_dir: Path | str | None = None,
    *,
    section_anchors: list[str] | None = None,
    replace_depends_on: list[str] | None = None,
    clear_depends_on: bool = False,
    importance: str = "",
    component: str = "",
) -> dict[str, Any]:
    """Upsert a development task entry into persistent `dev_queue.json` (`0600` + `file_lock`).

    `depends_on` merges into the existing list by default. `replace_depends_on` (any list, even empty) replaces it
    and `clear_depends_on=True` empties it; precedence is clear > replace > merge.
    """
    cid_clean = str(conversation_id or "").strip()
    if not cid_clean:
        return {"status": "error", "error": "conversation_id is required"}
    norm_status = str(status or "pending").strip().lower()
    if norm_status not in DEV_QUEUE_STATUSES:
        return {
            "status": "error",
            "error": f"Invalid queue status '{status}'. Expected one of {DEV_QUEUE_STATUSES}.",
        }
    raw_imp = str(importance or "").strip()
    norm_importance = normalize_importance_tier(raw_imp)
    if raw_imp and not norm_importance:
        return {
            "status": "error",
            "error": f"Invalid queue importance '{importance}'. Expected one of {IMPORTANCE_TIERS}.",
        }
    norm_component = normalize_component_name(component)
    try:
        norm_priority = int(priority)
    except (TypeError, ValueError):
        norm_priority = 50

    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    now_iso = _utc_now_iso()
    with dev_common.file_lock(q_path.with_suffix(".json.lock")):
        data = _read_dev_queue_locked(q_path)
        items: list[dict[str, Any]] = list(data["items"])
        if orchestrator_conversation_id.strip():
            data["orchestrator_conversation_id"] = orchestrator_conversation_id.strip()

        idx = _find_queue_item_index(items, cid_clean)
        created = idx < 0
        existing = dict(items[idx]) if idx >= 0 else {}
        existing_status = str(existing.get("status") or "").lower()

        if created:
            resolved_status = norm_status
            resolved_prio = norm_priority
        elif source == "ad_hoc_lock":
            resolved_status = existing_status if existing_status in ("merged", "cleaned") else norm_status
            resolved_prio = int(existing.get("priority", norm_priority))
        elif source == "auto_lock" and existing_status in (*DEV_QUEUE_HOLD_STATUSES, "in_progress", "pr_open"):
            resolved_status = existing_status
            resolved_prio = int(existing.get("priority", norm_priority))
        elif source == "auto_lock":
            resolved_status = existing_status or norm_status
            resolved_prio = min(int(existing.get("priority", norm_priority)), norm_priority)
        else:
            resolved_status = norm_status
            resolved_prio = norm_priority

        existing_title = str(existing.get("title") or "").strip()
        if source == "ad_hoc_lock" and existing_title:
            resolved_title = existing_title
        else:
            resolved_title = title.strip() or existing_title or action_id.strip() or cid_clean[:8]

        item: dict[str, Any] = {
            "conversation_id": str(existing.get("conversation_id") or cid_clean),
            "title": resolved_title,
            "action_id": action_id.strip() or str(existing.get("action_id") or ""),
            "priority": resolved_prio,
            "status": resolved_status,
            "repo_slugs": _normalize_str_list([*(existing.get("repo_slugs") or []), *(repo_slugs or [])]),
            "depends_on": _resolve_depends_on_update(
                existing.get("depends_on") or [],
                depends_on,
                replace_depends_on=replace_depends_on,
                clear_depends_on=clear_depends_on,
            ),
            "rca_summary": _sanitize_home_paths(rca_summary.strip()) or str(existing.get("rca_summary") or ""),
            "proposed_fix": _sanitize_home_paths(proposed_fix.strip()) or str(existing.get("proposed_fix") or ""),
            "target_files": _normalize_str_list([*(existing.get("target_files") or []), *(target_files or [])]),
            "branch_names": _normalize_str_list([*(existing.get("branch_names") or []), *(branch_names or [])]),
            "pr_urls": _dedupe_richest_pr_urls([*(existing.get("pr_urls") or []), *(pr_urls or [])]),
            "subagent_conversation_ids": _normalize_str_list(
                [*(existing.get("subagent_conversation_ids") or []), *(subagent_conversation_ids or [])]
            ),
            "notes": _sanitize_home_paths(notes.strip()) or str(existing.get("notes") or ""),
            "source": str(existing.get("source") or source or "manual"),
            "blocked_by_lock_id": blocked_by_lock_id.strip() or str(existing.get("blocked_by_lock_id") or ""),
            "added_at": str(existing.get("added_at") or now_iso),
            "updated_at": now_iso,
        }
        merged_anchors = _normalize_str_list([*(existing.get("section_anchors") or []), *(section_anchors or [])])
        if merged_anchors:
            item["section_anchors"] = merged_anchors
        resolved_importance = norm_importance or normalize_importance_tier(existing.get("importance"))
        if resolved_importance:
            item["importance"] = resolved_importance
        resolved_component = norm_component or normalize_component_name(existing.get("component"))
        if resolved_component:
            item["component"] = resolved_component
        if existing.get("merged_repos"):
            item["merged_repos"] = _normalize_str_list(
                [dev_common.normalize_repo_slug(str(r)) for r in existing.get("merged_repos") or [] if str(r).strip()]
            )
        if idx >= 0:
            items[idx] = item
        else:
            items.append(item)
        data["items"] = _sort_dev_queue_items(items)
        data["updated_at"] = now_iso
        _atomic_write_json_0600(q_path, data)

    return {
        "status": "ok",
        "created": created,
        "queue_path": str(q_path),
        "item": item,
        "summary": get_dev_queue_summary(data["items"]),
    }


def _sync_active_lock_to_dev_queue(
    *,
    conversation_id: str,
    status: str = "in_progress",
    title: str = "",
    action_id: str = "",
    repo_slug: str = "",
    branch_name: str = "",
    pr_urls: list[str] | None = None,
    subagent_conversation_ids: list[str] | None = None,
    rca_summary: str = "",
    proposed_fix: str = "",
    target_files: list[str] | None = None,
    section_anchors: list[str] | None = None,
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Ensure every active `dev_pr_lock` (including ad-hoc runs) is tracked in `dev_queue.json`."""
    cid_clean = str(conversation_id or "").strip()
    if not cid_clean:
        return {"status": "skipped", "reason": "missing_conversation_id"}
    norm_slug = dev_common.normalize_repo_slug(repo_slug)
    repo_list = [norm_slug] if (norm_slug and norm_slug != "default") else []
    clean_title = str(title or "").strip() or str(action_id or "").strip() or str(rca_summary or "").strip()[:96]
    return enqueue_dev_queue_item(
        conversation_id=cid_clean,
        title=clean_title,
        action_id=action_id,
        priority=15,
        repo_slugs=repo_list,
        status=status,
        rca_summary=rca_summary,
        proposed_fix=proposed_fix,
        target_files=target_files,
        section_anchors=section_anchors,
        branch_names=[branch_name] if str(branch_name or "").strip() else None,
        pr_urls=pr_urls,
        subagent_conversation_ids=subagent_conversation_ids,
        source="ad_hoc_lock",
        portfolio_dir=portfolio_dir,
    )


def update_dev_queue_item_status(
    conversation_id: str,
    status: str,
    branch_names: list[str] | None = None,
    pr_urls: list[str] | None = None,
    subagent_conversation_ids: list[str] | None = None,
    notes: str | None = None,
    orchestrator_conversation_id: str = "",
    portfolio_dir: Path | str | None = None,
    *,
    strip_pr_urls_for_branches: list[str] | None = None,
    strip_repo_slug: str = "",
    importance: str | None = None,
    component: str | None = None,
) -> dict[str, Any]:
    """Transition an existing `dev_queue.json` entry to `status`, append artifacts, and deduplicate `pr_urls` keeping the richest verified URL per `(repo_slug, branch_name)`."""
    cid_clean = str(conversation_id or "").strip()
    norm_status = str(status or "").strip().lower()
    if norm_status not in DEV_QUEUE_STATUSES:
        return {
            "status": "error",
            "updated": False,
            "error": f"Invalid queue status '{status}'. Expected one of {DEV_QUEUE_STATUSES}.",
        }
    norm_importance: str | None = None
    if importance is not None:
        raw_imp = str(importance).strip()
        if raw_imp:
            norm_importance = normalize_importance_tier(raw_imp)
            if not norm_importance:
                return {
                    "status": "error",
                    "updated": False,
                    "error": f"Invalid queue importance '{importance}'. Expected one of {IMPORTANCE_TIERS}.",
                }
        else:
            norm_importance = ""
    norm_component: str | None = normalize_component_name(component) if component is not None else None
    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    now_iso = _utc_now_iso()
    with dev_common.file_lock(q_path.with_suffix(".json.lock")):
        data = _read_dev_queue_locked(q_path)
        items = list(data["items"])
        if orchestrator_conversation_id.strip():
            data["orchestrator_conversation_id"] = orchestrator_conversation_id.strip()
        idx = _find_queue_item_index(items, cid_clean)
        if idx < 0:
            return {
                "status": "error",
                "updated": False,
                "error": f"Queue item `{cid_clean}` not found in `{q_path}`.",
            }
        item = dict(items[idx])
        item["status"] = norm_status
        item["branch_names"] = _normalize_str_list([*(item.get("branch_names") or []), *(branch_names or [])])
        item["pr_urls"] = _dedupe_richest_pr_urls(
            [*(item.get("pr_urls") or []), *(pr_urls or [])],
            strip_branches=strip_pr_urls_for_branches,
            strip_repo_slug=strip_repo_slug,
        )
        item["subagent_conversation_ids"] = _normalize_str_list(
            [*(item.get("subagent_conversation_ids") or []), *(subagent_conversation_ids or [])]
        )
        if notes is not None and notes.strip():
            item["notes"] = _sanitize_home_paths(notes.strip())
        if norm_importance is not None:
            if norm_importance:
                item["importance"] = norm_importance
            else:
                item.pop("importance", None)
        if norm_component is not None:
            if norm_component:
                item["component"] = norm_component
            else:
                item.pop("component", None)
        item["updated_at"] = now_iso
        items[idx] = item
        data["items"] = _sort_dev_queue_items(items)
        data["updated_at"] = now_iso
        _atomic_write_json_0600(q_path, data)
    return {
        "status": "ok",
        "updated": True,
        "queue_path": str(q_path),
        "item": item,
        "summary": get_dev_queue_summary(data["items"]),
    }


_STALE_PR_COMPARE_MARKERS = (
    "/compare/",
    "/-/merge_requests/new",
    "/pull-requests/new",
)
_CREATED_PR_URL_RE = re.compile(
    r"^(.*?)/(?:pull|pull-requests|-/merge_requests)/(\d+)(?:/|$)",
    re.IGNORECASE,
)
_MERGED_LOCK_RELEASE_REASONS = ("BRANCH_MERGED_INTO_MAIN", "HUMAN_VALIDATED_MANUAL_PR")


def _extract_created_pr_repo_and_number(raw_url: str) -> tuple[str, str]:
    """Return `(normalized_repo_slug, pr_number)` if `raw_url` is a created PR/MR URL, else `('', '')`."""
    url_str = str(raw_url or "").strip()
    if not url_str:
        return "", ""
    parsed = urllib.parse.urlsplit(url_str)
    path = parsed.path or ""
    m = _CREATED_PR_URL_RE.match(path)
    if not m:
        return "", ""
    parts = [p for p in m.group(1).strip("/").split("/") if p]
    slug = dev_common.normalize_repo_slug(f"{parts[-2]}/{parts[-1]}") if len(parts) >= 2 else ""
    return slug, m.group(2)


def _score_pr_url_richness(raw_url: str) -> tuple[int, int]:
    """Rank PR/Compare URL richness: created `/pull/` (400) > verified PASS compare with title+body (300) > compare with title+body (200) > partial compare (150) > bare compare (100)."""
    url_str = str(raw_url or "").strip()
    if not url_str:
        return (0, 0)
    pr_slug, pr_num = _extract_created_pr_repo_and_number(url_str)
    if pr_num:
        return (400, len(url_str))
    parsed = urllib.parse.urlsplit(url_str)
    path_lower = (parsed.path or "").lower()
    if any(marker in path_lower for marker in _STALE_PR_COMPARE_MARKERS):
        qs = urllib.parse.parse_qs(parsed.query)
        has_title = bool(qs.get("title") or qs.get("merge_request[title]"))
        body_vals = qs.get("body") or qs.get("merge_request[description]") or qs.get("description") or []
        body_text = str(body_vals[0]) if body_vals else ""
        has_body = bool(body_text.strip())
        has_verified_pass = has_body and (
            "PASS" in body_text
            and (
                "Independent PR Review" in body_text
                or "Verdict" in body_text
                or "Automated Verification Checks" in body_text
            )
        )
        if has_title and has_verified_pass:
            return (300, len(url_str))
        if has_title and has_body:
            return (200, len(url_str))
        if has_title or has_body:
            return (150, len(url_str))
        return (100, len(url_str))
    _ = pr_slug
    return (50, len(url_str))


def _dedupe_richest_pr_urls(
    pr_urls: list[str] | tuple[str, ...] | None,
    *,
    strip_branches: list[str] | None = None,
    strip_repo_slug: str = "",
) -> list[str]:
    """Deduplicate `pr_urls` per `(repo_slug, branch_name)` keeping the richest verified URL (and letting created `/pull/` supersede compare links for that repo)."""
    norm_urls = _normalize_str_list(pr_urls)
    if not norm_urls:
        return []

    strip_branch_set = {
        str(b).strip().removeprefix("refs/heads/")
        for b in (strip_branches or ())
        if str(b).strip()
    }
    norm_strip_slug = dev_common.normalize_repo_slug(strip_repo_slug).lower()

    # Track repos that already have a created /pull/<n> URL so compare URLs for those repos are superseded
    created_pr_repos: set[str] = set()
    for url in norm_urls:
        c_slug, c_num = _extract_created_pr_repo_and_number(url)
        if c_num and c_slug:
            created_pr_repos.add(c_slug.lower())

    best_by_key: dict[tuple[str, ...], tuple[int, tuple[int, int], str]] = {}
    for idx, url in enumerate(norm_urls):
        c_slug, c_num = _extract_created_pr_repo_and_number(url)
        score = _score_pr_url_richness(url)
        if c_num:
            key: tuple[str, ...] = ("created_pr", c_slug.lower(), c_num)
        else:
            cmp_slug, cmp_branch = _extract_repo_slug_and_branch_from_pr_url(url)
            if cmp_branch:
                if cmp_branch in strip_branch_set and (
                    not norm_strip_slug or not cmp_slug or cmp_slug.lower() == norm_strip_slug
                ):
                    continue
                if cmp_slug and cmp_slug.lower() in created_pr_repos:
                    continue
                key = ("compare", cmp_slug.lower(), cmp_branch)
            else:
                key = ("raw", url)

        existing = best_by_key.get(key)
        if existing is None or score > existing[1]:
            first_idx = existing[0] if existing is not None else idx
            best_by_key[key] = (first_idx, score, url)

    ordered = sorted(best_by_key.values(), key=lambda entry: entry[0])
    return [entry[2] for entry in ordered]


def _extract_repo_slug_and_branch_from_pr_url(raw_url: str) -> tuple[str, str]:
    """Extract `(normalized_repo_slug, branch_name)` from a VCS compare/new-PR URL when recognizable."""
    url_str = str(raw_url or "").strip()
    if not url_str:
        return "", ""
    parsed = urllib.parse.urlsplit(url_str)
    path = parsed.path or ""
    path_lower = path.lower()

    def _slug_from_prefix(prefix_path: str) -> str:
        parts = [p for p in prefix_path.strip("/").split("/") if p]
        if len(parts) >= 2:
            return dev_common.normalize_repo_slug(f"{parts[-2]}/{parts[-1]}")
        return ""

    compare_idx = path_lower.find("/compare/")
    if compare_idx >= 0:
        slug = _slug_from_prefix(path[:compare_idx])
        tail = path[compare_idx + len("/compare/") :].strip("/")
        if "..." in tail:
            branch_raw = tail.split("...", 1)[1]
        elif ".." in tail:
            branch_raw = tail.split("..", 1)[1]
        else:
            branch_raw = tail
        branch = urllib.parse.unquote(branch_raw).strip().removeprefix("refs/heads/")
        return slug, branch

    gl_idx = path_lower.find("/-/merge_requests/new")
    if gl_idx >= 0:
        slug = _slug_from_prefix(path[:gl_idx])
        qs = urllib.parse.parse_qs(parsed.query)
        br_list = qs.get("merge_request[source_branch]") or []
        branch = str(br_list[0]).strip().removeprefix("refs/heads/") if br_list else ""
        return slug, branch

    bb_idx = path_lower.find("/pull-requests/new")
    if bb_idx >= 0:
        slug = _slug_from_prefix(path[:bb_idx])
        qs = urllib.parse.parse_qs(parsed.query)
        br_list = qs.get("source") or []
        branch = str(br_list[0]).strip().removeprefix("refs/heads/") if br_list else ""
        return slug, branch

    return "", ""


def _prune_merged_repo_pr_urls(
    pr_urls: list[str] | None,
    released_repo_slug: str = "",
    *,
    all_merged: bool = False,
    merged_repos: list[str] | None = None,
) -> list[str]:
    """Remove stale VCS compare/new-PR URLs for merged repos while preserving open-repo and archival URLs."""
    pruned_slugs: set[str] = set()
    for raw_slug in [released_repo_slug, *(merged_repos or [])]:
        norm = dev_common.normalize_repo_slug(str(raw_slug or ""))
        if norm and norm != "default":
            pruned_slugs.add(norm.lower())

    kept: list[str] = []
    for raw_url in _normalize_str_list(pr_urls):
        path_lower = urllib.parse.urlsplit(raw_url).path.lower()
        is_compare_url = any(marker in path_lower for marker in _STALE_PR_COMPARE_MARKERS)
        if not is_compare_url:
            kept.append(raw_url)
            continue
        if all_merged:
            continue
        if any(
            f"/{slug}{marker}" in path_lower
            for slug in pruned_slugs
            for marker in _STALE_PR_COMPARE_MARKERS
        ):
            continue
        kept.append(raw_url)
    return kept


def _mark_queue_item_repo_merged(
    *,
    conversation_id: str,
    released_repo_slug: str,
    branch_names: list[str] | None = None,
    subagent_conversation_ids: list[str] | None = None,
    remaining_active_locks: dict[str, dict[str, Any]] | None = None,
    portfolio_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Record a merged repository on a queue item, prune stale compare `pr_urls`, and promote to `merged` once all target repos finish."""
    cid_clean = str(conversation_id or "").strip()
    clean_slug = dev_common.normalize_repo_slug(released_repo_slug)
    q_path = get_dev_queue_path(portfolio_dir=portfolio_dir)
    now_iso = _utc_now_iso()
    with dev_common.file_lock(q_path.with_suffix(".json.lock")):
        data = _read_dev_queue_locked(q_path)
        items = list(data["items"])
        idx = _find_queue_item_index(items, cid_clean)
        if idx < 0:
            return {"status": "error", "updated": False, "error": f"Queue item `{cid_clean}` not found."}
        item = dict(items[idx])
        target_repos = [
            norm
            for r in (item.get("repo_slugs") or [])
            for norm in [dev_common.normalize_repo_slug(str(r))]
            if norm
        ]
        if target_repos and clean_slug and clean_slug != "default" and clean_slug not in target_repos:
            return {
                "status": "skipped",
                "updated": False,
                "reason": "released_repo_not_in_item_target_repos",
                "item": item,
            }
        merged_repos = _normalize_str_list(
            [
                *[dev_common.normalize_repo_slug(str(r)) for r in (item.get("merged_repos") or [])],
                *([clean_slug] if clean_slug and clean_slug != "default" else []),
            ]
        )
        still_locked_for_item = False
        still_locked_human_ready = False
        _human_ready_statuses = {
            "PENDING_HUMAN_PR_VALIDATION",
            "PR_OPEN",
            "REVIEW_PASSED",
            "REVIEW_APPROVED",
            "NEEDS_HUMAN_REVIEW",
        }
        for act_lock in (remaining_active_locks or {}).values():
            act_cid = str(act_lock.get("conversation_id") or "").strip()
            act_sibs = [str(s).strip() for s in (act_lock.get("sibling_conversation_ids") or [])]
            if _matches_cid(act_cid, cid_clean) or any(_matches_cid(s, cid_clean) for s in act_sibs):
                still_locked_for_item = True
                act_st_u = str(act_lock.get("status") or "").strip().upper()
                if act_st_u in _human_ready_statuses and bool(act_lock.get("pr_gate_ready", True)):
                    still_locked_human_ready = True
        all_target_repos_merged = (
            (not target_repos and not still_locked_for_item)
            or (clean_slug in ("", "default") and len(target_repos) <= 1 and not still_locked_for_item)
            or (set(target_repos).issubset(set(merged_repos)) and not still_locked_for_item)
        )

        item["merged_repos"] = merged_repos
        item["branch_names"] = _normalize_str_list([*(item.get("branch_names") or []), *(branch_names or [])])
        item["pr_urls"] = _prune_merged_repo_pr_urls(
            item.get("pr_urls"),
            released_repo_slug=clean_slug,
            all_merged=all_target_repos_merged,
            merged_repos=merged_repos,
        )
        item["subagent_conversation_ids"] = _normalize_str_list(
            [*(item.get("subagent_conversation_ids") or []), *(subagent_conversation_ids or [])]
        )
        if all_target_repos_merged:
            item["status"] = "merged"
        elif still_locked_human_ready or bool(item["pr_urls"]):
            item["status"] = "pr_open"
        elif still_locked_for_item:
            item["status"] = "in_progress"
        else:
            item["status"] = "pending"
        item["updated_at"] = now_iso
        items[idx] = item
        data["items"] = _sort_dev_queue_items(items)
        data["updated_at"] = now_iso
        _atomic_write_json_0600(q_path, data)
    return {"status": "ok", "updated": True, "item": item}




def _collect_queue_item_repo_branches(
    item: dict[str, Any],
    target_slug: str,
) -> tuple[list[str], list[str]]:
    """Return `(candidate_branches, repo_specific_branches)` for `(item, target_slug)`."""
    clean_target = dev_common.normalize_repo_slug(target_slug)
    target_name = clean_target.rsplit("/", 1)[-1] if clean_target else ""
    url_owners: dict[str, set[str]] = {}
    repo_specific: list[str] = []

    for raw_url in item.get("pr_urls") or []:
        u_slug, u_br = _extract_repo_slug_and_branch_from_pr_url(str(raw_url))
        if u_slug and u_br:
            url_owners.setdefault(u_br, set()).add(u_slug)
            if u_slug == clean_target and u_br not in repo_specific:
                repo_specific.append(u_br)

    candidates: list[str] = list(repo_specific)
    for raw_br in item.get("branch_names") or []:
        br_str = str(raw_br or "").strip()
        if not br_str:
            continue
        if "::" in br_str:
            hint_raw, br_only = br_str.split("::", 1)
            clean_br = br_only.strip().removeprefix("refs/heads/")
            if not clean_br:
                continue
            hint_norm = dev_common.normalize_repo_slug(hint_raw)
            hint_name = hint_raw.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
            if hint_norm == clean_target or (target_name and hint_name == target_name.lower()):
                if clean_br not in repo_specific:
                    repo_specific.append(clean_br)
                if clean_br not in candidates:
                    candidates.append(clean_br)
            continue
        clean_br = br_str.removeprefix("refs/heads/")
        if not clean_br:
            continue
        owners = url_owners.get(clean_br)
        if owners and clean_target not in owners:
            continue
        if clean_br not in candidates:
            candidates.append(clean_br)
    return candidates, repo_specific


def _verify_queue_repo_merged_readonly(
    *,
    item: dict[str, Any],
    target_slug: str,
    all_locks: dict[str, dict[str, Any]],
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    is_scoped: bool = False,
    scoped_slug: str = "",
    scoped_repo_dir: Path | None = None,
    fetch: bool = False,
    fetched_dirs: set[str] | None = None,
) -> tuple[bool, str, list[str], list[str]]:
    """Verify via read-only lock history or Git history whether `(item, target_slug)` is genuinely merged."""
    item_cid = str(item.get("conversation_id") or "").strip()
    candidate_branches, repo_specific_branches = _collect_queue_item_repo_branches(item, target_slug)

    # Evidence (a): Inactive released lock history for `target_slug`
    inactive_lock = all_locks.get(target_slug)
    if isinstance(inactive_lock, dict) and not bool(inactive_lock.get("active")):
        rel_reason = str(inactive_lock.get("last_release_reason") or "").strip()
        if rel_reason in _MERGED_LOCK_RELEASE_REASONS:
            last_cid = str(inactive_lock.get("last_conversation_id") or "").strip()
            last_br = str(inactive_lock.get("last_branch_name") or "").strip()
            branch_matched = any(
                _entry_matches_branch(inactive_lock, br, include_inactive=True)
                for br in candidate_branches
            )
            cid_matched = _matches_cid(last_cid, item_cid)
            conflicting_specific_branch = bool(
                repo_specific_branches
                and last_br
                and not any(
                    _entry_matches_branch(inactive_lock, br, include_inactive=True)
                    for br in repo_specific_branches
                )
            )
            if branch_matched or (cid_matched and not conflicting_specific_branch):
                ev_branches: list[str] = []
                if last_br:
                    ev_branches.append(last_br)
                last_rb = inactive_lock.get("last_repo_branches")
                if isinstance(last_rb, dict):
                    rb_entry = last_rb.get(target_slug)
                    if isinstance(rb_entry, dict) and rb_entry.get("branch_name"):
                        rb_br = str(rb_entry["branch_name"]).strip()
                        if rb_br and rb_br not in ev_branches:
                            ev_branches.append(rb_br)
                ev_subagents = [
                    str(s).strip()
                    for s in (inactive_lock.get("last_subagent_conversation_ids") or [])
                    if str(s).strip()
                ]
                match_kind = "branch" if branch_matched else "conversation_id"
                return True, f"inactive_lock:{rel_reason}:{match_kind}", ev_branches, ev_subagents

    # Evidence (b): Strictly read-only native Git verification
    if not candidate_branches:
        return False, "no_candidate_branches", [], []

    vcs: Any = None
    repo_dir: Path | None = None
    use_injected_client = False
    if git_client is not None:
        if is_scoped and target_slug == scoped_slug:
            use_injected_client = True
        elif not is_scoped and hasattr(git_client, "resolve_repo"):
            with contextlib.suppress(Exception):
                injected_slug = dev_common.normalize_repo_slug(str(git_client.resolve_repo() or ""))
                use_injected_client = injected_slug == target_slug

    if use_injected_client and git_client is not None:
        vcs = git_client
        if is_scoped and target_slug == scoped_slug and scoped_repo_dir is not None:
            repo_dir = scoped_repo_dir
        elif hasattr(git_client, "resolve_repo_dir"):
            with contextlib.suppress(Exception):
                cand_d = git_client.resolve_repo_dir()
                if isinstance(cand_d, Path) and (cand_d / ".git").exists():
                    repo_dir = _resolve_main_git_repo_root(cand_d)
    else:
        if is_scoped and target_slug == scoped_slug and scoped_repo_dir is not None:
            repo_dir = scoped_repo_dir
        else:
            repo_dir = _resolve_repo_dir_for_slug(
                target_slug,
                portfolio_dir=portfolio_dir,
                git_client=git_client if (git_client is not None and hasattr(git_client, "resolve_repo_dir")) else None,
            )
        if repo_dir is None or not (repo_dir / ".git").exists():
            return False, "repo_dir_not_found", [], []
        vcs = GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_dir)})

    if not hasattr(vcs, "inspect_remote_branch_state"):
        return False, "vcs_missing_inspect_remote_branch_state", [], []

    for br_name in candidate_branches:
        eff_fetch = fetch
        dir_key = str(repo_dir) if repo_dir is not None else ""
        if eff_fetch and fetched_dirs is not None and dir_key and not use_injected_client:
            if dir_key in fetched_dirs:
                eff_fetch = False
            else:
                fetched_dirs.add(dir_key)

        if (
            not eff_fetch
            and isinstance(vcs, GitVcsIntegrationClient)
            and repo_dir is not None
            and (repo_dir / ".git").exists()
        ):
            loc_merged, loc_reason = _inspect_local_branch_merged_readonly(repo_dir, br_name, base_branch="main")
            if loc_merged:
                return True, loc_reason, [br_name], []
            continue

        try:
            st = vcs.inspect_remote_branch_state(
                branch_name=br_name,
                base_branch="main",
                repo_dir=repo_dir,
                fetch=eff_fetch,
            )
        except TypeError:
            st = vcs.inspect_remote_branch_state(branch_name=br_name)

        if isinstance(st, dict) and st.get("status") == "ok":
            if bool(st.get("merged_into_base")):
                return True, f"git_merged_into_base:{br_name}", [br_name], []
            remote_exists = bool(st.get("remote_exists") or st.get("exists_on_remote"))
            local_exists = False
            if repo_dir is not None and (repo_dir / ".git").exists():
                loc_proc = subprocess.run(
                    ["git", "-C", str(repo_dir), "rev-parse", "--verify", f"refs/heads/{br_name}"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                local_exists = loc_proc.returncode == 0
            if remote_exists or local_exists:
                continue
            if repo_dir is not None and _branch_found_in_base_git_log(repo_dir, br_name, base_branch="main"):
                return True, f"git_log_merge_commit:{br_name}", [br_name], []
        elif repo_dir is not None and (repo_dir / ".git").exists():
            loc_merged, loc_reason = _inspect_local_branch_merged_readonly(repo_dir, br_name, base_branch="main")
            if loc_merged:
                return True, loc_reason, [br_name], []

    return False, "not_merged", [], []


def reconcile_dev_queue_with_git(
    portfolio_dir: Path | str | None = None,
    git_client: GitVcsIntegrationClient | None = None,
    *,
    repo_slug: str = "",
    workdir: Path | str | None = None,
    fetch: bool = False,
) -> dict[str, Any]:
    """Reconcile stale `pr_open` / branched `in_progress` items in `dev_queue.json` using read-only lock & Git evidence."""
    all_locks = get_all_dev_pr_locks(portfolio_dir=portfolio_dir, include_inactive=True)
    active_locks = {k: v for k, v in all_locks.items() if isinstance(v, dict) and bool(v.get("active"))}

    is_scoped = bool(str(repo_slug or "").strip() or workdir)
    scoped_slug = ""
    scoped_repo_dir: Path | None = None
    if is_scoped:
        scoped_slug, scoped_repo_dir = _resolve_repo_slug_and_dir(
            repo_slug=repo_slug,
            workdir=workdir,
            git_client=git_client if (git_client is not None and hasattr(git_client, "resolve_repo")) else None,
            prefer_cwd=False,
            portfolio_dir=portfolio_dir,
            locks_map=all_locks,
        )
        if scoped_repo_dir is not None and (scoped_repo_dir / ".git").exists():
            scoped_repo_dir = _resolve_main_git_repo_root(scoped_repo_dir)
        if not scoped_slug:
            return {
                "status": "skipped",
                "reason": "workdir_not_a_git_repo",
                "repo_slug": "",
                "checked_count": 0,
                "reconciled_count": 0,
                "reconciled_items": [],
            }

    q_data = load_dev_queue(portfolio_dir=portfolio_dir)
    items = list(q_data.get("items") or [])
    checked_count = 0
    reconciled_items: list[dict[str, Any]] = []
    fetched_dirs: set[str] = set()

    for raw_item in items:
        item = dict(raw_item)
        item_cid = str(item.get("conversation_id") or "").strip()
        item_status = str(item.get("status") or "").strip().lower()
        if not item_cid:
            continue
        has_branch_or_pr = bool(item.get("branch_names")) or bool(item.get("pr_urls"))
        if item_status != "pr_open" and not (item_status == "in_progress" and has_branch_or_pr):
            continue

        target_repos = _normalize_str_list(
            [dev_common.normalize_repo_slug(str(r)) for r in (item.get("repo_slugs") or []) if str(r).strip()]
        )
        if not target_repos:
            inferred_repos: list[str] = []
            for raw_url in item.get("pr_urls") or []:
                u_slug, _ = _extract_repo_slug_and_branch_from_pr_url(str(raw_url))
                if u_slug and u_slug not in inferred_repos:
                    inferred_repos.append(u_slug)
            if not inferred_repos:
                for l_slug, l_entry in all_locks.items():
                    if not isinstance(l_entry, dict):
                        continue
                    l_cid = str(l_entry.get("conversation_id") or l_entry.get("last_conversation_id") or "").strip()
                    if _matches_cid(l_cid, item_cid) or any(
                        _entry_matches_branch(l_entry, str(br).strip(), include_inactive=True)
                        for br in (item.get("branch_names") or [])
                    ):
                        norm_l = dev_common.normalize_repo_slug(l_slug)
                        if norm_l and norm_l not in inferred_repos:
                            inferred_repos.append(norm_l)
            target_repos = inferred_repos

        if not target_repos:
            continue

        already_merged = {
            dev_common.normalize_repo_slug(str(r))
            for r in (item.get("merged_repos") or [])
            if str(r).strip()
        }

        for cand_slug in target_repos:
            if is_scoped and cand_slug != scoped_slug:
                continue
            checked_count += 1
            if cand_slug in active_locks:
                continue

            if cand_slug in already_merged:
                item_still_locked = any(
                    _matches_cid(str(al.get("conversation_id") or ""), item_cid)
                    or any(_matches_cid(str(s), item_cid) for s in (al.get("sibling_conversation_ids") or []))
                    for al in active_locks.values()
                )
                all_done = set(target_repos).issubset(already_merged) and not item_still_locked
                pruned_urls = _prune_merged_repo_pr_urls(
                    item.get("pr_urls"),
                    released_repo_slug=cand_slug,
                    all_merged=all_done,
                    merged_repos=list(already_merged),
                )
                needs_cleanup = (
                    all_done and str(item.get("status") or "").lower() != "merged"
                ) or (pruned_urls != _normalize_str_list(item.get("pr_urls")))
                if needs_cleanup:
                    upd = _mark_queue_item_repo_merged(
                        conversation_id=item_cid,
                        released_repo_slug=cand_slug,
                        remaining_active_locks=active_locks,
                        portfolio_dir=portfolio_dir,
                    )
                    if upd.get("updated"):
                        item = dict(upd.get("item") or item)
                        reconciled_items.append(
                            {
                                "conversation_id": item_cid,
                                "repo_slug": cand_slug,
                                "evidence": "already_merged_repo_cleanup",
                                "status": str(item.get("status") or ""),
                                "merged_repos": list(item.get("merged_repos") or []),
                                "pr_urls_remaining": list(item.get("pr_urls") or []),
                            }
                        )
                continue

            is_merged, evidence, ev_branches, ev_subagents = _verify_queue_repo_merged_readonly(
                item=item,
                target_slug=cand_slug,
                all_locks=all_locks,
                portfolio_dir=portfolio_dir,
                git_client=git_client,
                is_scoped=is_scoped,
                scoped_slug=scoped_slug,
                scoped_repo_dir=scoped_repo_dir,
                fetch=fetch,
                fetched_dirs=fetched_dirs,
            )
            if not is_merged:
                continue

            upd = _mark_queue_item_repo_merged(
                conversation_id=item_cid,
                released_repo_slug=cand_slug,
                branch_names=ev_branches or None,
                subagent_conversation_ids=ev_subagents or None,
                remaining_active_locks=active_locks,
                portfolio_dir=portfolio_dir,
            )
            if upd.get("updated"):
                item = dict(upd.get("item") or item)
                already_merged.add(cand_slug)
                reconciled_items.append(
                    {
                        "conversation_id": item_cid,
                        "repo_slug": cand_slug,
                        "evidence": evidence,
                        "status": str(item.get("status") or ""),
                        "merged_repos": list(item.get("merged_repos") or []),
                        "pr_urls_remaining": list(item.get("pr_urls") or []),
                    }
                )

        # Self-heal phantom `pr_open` items left with zero active locks and zero open PR URLs
        cur_item_status = str(item.get("status") or "").strip().lower()
        remaining_unmerged = [r for r in target_repos if r not in already_merged]
        if cur_item_status == "pr_open" and remaining_unmerged and not _normalize_str_list(item.get("pr_urls")):
            item_still_locked = any(
                _matches_cid(str(al.get("conversation_id") or ""), item_cid)
                or any(_matches_cid(str(s), item_cid) for s in (al.get("sibling_conversation_ids") or []))
                for al in active_locks.values()
            )
            if not item_still_locked and (not is_scoped or scoped_slug in remaining_unmerged or scoped_slug in already_merged):
                upd_ph = update_dev_queue_item_status(
                    conversation_id=item_cid,
                    status="pending",
                    portfolio_dir=portfolio_dir,
                )
                if upd_ph.get("updated"):
                    item = dict(upd_ph.get("item") or item)
                    reconciled_items.append(
                        {
                            "conversation_id": item_cid,
                            "repo_slug": remaining_unmerged[0],
                            "evidence": "phantom_pr_open_reverted_to_pending",
                            "status": str(item.get("status") or "pending"),
                            "merged_repos": list(item.get("merged_repos") or []),
                            "pr_urls_remaining": list(item.get("pr_urls") or []),
                        }
                    )

    result: dict[str, Any] = {
        "status": "ok",
        "checked_count": checked_count,
        "reconciled_count": len(reconciled_items),
        "reconciled_items": reconciled_items,
    }
    if scoped_slug:
        result["repo_slug"] = scoped_slug
    return result


def _auto_enqueue_blocked_task(
    conversation_id: str,
    action_id: str = "",
    rca_summary: str = "",
    proposed_fix: str = "",
    target_files: list[str] | None = None,
    repo_slugs: list[str] | None = None,
    blocked_by_lock_id: str = "",
    portfolio_dir: Path | str | None = None,
    *,
    section_anchors: list[str] | None = None,
) -> dict[str, Any]:
    """Best-effort deterministic enqueue when a repository lock blocks a concurrent remediation attempt."""
    try:
        return enqueue_dev_queue_item(
            conversation_id=conversation_id,
            action_id=action_id,
            priority=40,
            repo_slugs=repo_slugs,
            status="pending",
            rca_summary=rca_summary,
            proposed_fix=proposed_fix,
            target_files=target_files,
            section_anchors=section_anchors,
            source="auto_lock",
            blocked_by_lock_id=blocked_by_lock_id,
            portfolio_dir=portfolio_dir,
        )
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def get_next_dev_queue_item(
    orchestrator_conversation_id: str = "",
    portfolio_dir: Path | str | None = None,
    *,
    skip_locked_repos: bool = False,
) -> dict[str, Any]:
    """Return the highest-priority runnable item from `dev_queue.json`, including safe Slot #2..#N lookahead candidates.

    `multi_slot_candidates` / `multi_slot_by_repo` list pending items that are parallel-safe pairwise against every
    active slot of a locked repo with free capacity (`len(active slots) < max_slots`); each carries `free_slot_id`
    / `next_slot_id`. Legacy `slot2_candidates` / `slot2_by_repo` mirror them identically.
    """
    if orchestrator_conversation_id.strip():
        set_dev_queue_orchestrator(orchestrator_conversation_id.strip(), portfolio_dir=portfolio_dir)
    data = load_dev_queue(portfolio_dir=portfolio_dir)
    items: list[dict[str, Any]] = list(data.get("items") or [])
    status_by_cid = {str(it.get("conversation_id") or ""): str(it.get("status") or "").lower() for it in items}
    active_locks = get_all_dev_pr_locks(portfolio_dir=portfolio_dir)
    locked_repos = sorted(active_locks.keys())
    orch_ctrl = _orch_ctrl.load_orchestration_control(portfolio_dir=portfolio_dir)
    if bool(orch_ctrl.get("paused")):
        return {
            "status": "ok",
            "paused": True,
            "orchestration_control": orch_ctrl,
            "queue_path": data.get("queue_path", ""),
            "orchestrator_conversation_id": data.get("orchestrator_conversation_id", ""),
            "has_next": False,
            "next_item": None,
            "next_pending_item": None,
            "next_unlocked_item": None,
            "runnable_by_repo": {},
            "multi_slot_candidates": [],
            "multi_slot_by_repo": {},
            "slot2_candidates": [],
            "slot2_by_repo": {},
            "locked_repos": locked_repos,
            "partial_wip_by_repo": {},
            "blocked_by_dependencies": [],
            "blocked_by_repo_locks": [],
            "blocked_by_partial_wip_cap": [],
            "summary": data.get("summary", {}),
        }

    def _deps_unmet(item: dict[str, Any]) -> list[str]:
        unmet: list[str] = []
        for dep in item.get("depends_on") or []:
            dep_clean = str(dep or "").strip()
            if not dep_clean:
                continue
            matched_st = ""
            for cand_cid, cand_st in status_by_cid.items():
                if _matches_cid(cand_cid, dep_clean):
                    matched_st = cand_st
                    break
            if matched_st and matched_st not in ("merged", "cleaned"):
                unmet.append(f"{dep_clean}({matched_st})")
        return unmet

    def _lock_owns_cid(lock_obj: dict[str, Any], cid_val: str) -> bool:
        act_cid = str(lock_obj.get("conversation_id") or "").strip()
        act_sibs = [str(s).strip() for s in (lock_obj.get("sibling_conversation_ids") or [])]
        if _matches_cid(act_cid, cid_val) or any(_matches_cid(s, cid_val) for s in act_sibs):
            return True
        for slot_obj in _active_lock_slot_specs(lock_obj):
            s_cid = str(slot_obj.get("conversation_id") or "").strip()
            s_sibs = [str(s).strip() for s in (slot_obj.get("sibling_conversation_ids") or [])]
            if _matches_cid(s_cid, cid_val) or any(_matches_cid(s, cid_val) for s in s_sibs):
                return True
        return False

    def _item_repo_schedule(item: dict[str, Any]) -> tuple[list[str], list[str], list[str], list[str]]:
        """Compute `(remaining_repos, dispatch_repos, foreign_locked, self_locked)` using contract-first ordering and subtracting `merged_repos`."""
        item_repos = _normalize_str_list(
            [dev_common.normalize_repo_slug(str(r)) for r in (item.get("repo_slugs") or []) if str(r).strip()]
        )
        merged_set = {
            dev_common.normalize_repo_slug(str(r))
            for r in (item.get("merged_repos") or [])
            if str(r).strip()
        }
        remaining_repos = [r for r in item_repos if r not in merged_set]
        item_cid = str(item.get("conversation_id") or "").strip()
        foreign_locked: list[str] = []
        self_locked: list[str] = []
        for rslug in remaining_repos:
            act = active_locks.get(rslug)
            if not act:
                continue
            if _lock_owns_cid(act, item_cid):
                self_locked.append(rslug)
            else:
                foreign_locked.append(rslug)

        if not remaining_repos:
            return ([], [], [], [])
        next_contract_repo = remaining_repos[0]
        if next_contract_repo in foreign_locked or next_contract_repo in self_locked:
            return (remaining_repos, [], foreign_locked, self_locked)
        if not foreign_locked:
            dispatch_repos = [r for r in remaining_repos if r not in self_locked]
        else:
            # Contract-first partial multi-repo dispatch: advance unlocked prefix repo(s) ahead of downstream locked repo(s)
            dispatch_repos = []
            for r in remaining_repos:
                if r in foreign_locked or r in self_locked:
                    break
                dispatch_repos.append(r)
        return (remaining_repos, dispatch_repos, foreign_locked, self_locked)

    reserved_by_repo: dict[str, list[tuple[int, dict[str, Any]]]] = {}

    def _probe_parallel_slot_lookahead(item: dict[str, Any], target_repo: str) -> tuple[bool, dict[str, Any], str]:
        """Return `(eligible, slot_annotation, blocked_reason)` for a pending item on a locked repo with a free Slot #2..#N.

        Overlap is validated pairwise against EVERY active slot plus candidates already reserved for this repo
        in the same `queue-next` pass, so two lookahead picks can never collide with each other either.
        """
        act = active_locks.get(target_repo)
        if not isinstance(act, dict) or not bool(act.get("active")):
            return (False, {}, "repo_not_locked")
        max_slots = _lock_max_slots(act)
        active_slots = _active_lock_slot_specs(act)
        active_ids = [int(s.get("slot_id") or 1) for s in active_slots]
        reserved = reserved_by_repo.get(target_repo, [])
        free_slot_id = _next_free_slot_id(active_ids, max_slots, [r_id for r_id, _ in reserved])
        if max_slots < 2 or not free_slot_id:
            return (False, {"max_slots": max_slots, "active_slot_ids": active_ids}, "max_slots_occupied")
        if str(item.get("status") or "pending").lower() != "pending":
            return (False, {}, "item_not_pending")
        if not (item.get("target_files") or item.get("section_anchors")):
            return (False, {}, "candidate_unanchored")
        for slot_obj in active_slots:
            if not (
                slot_obj.get("target_files")
                or slot_obj.get("section_anchors")
                or slot_obj.get("branch_name")
                or slot_obj.get("worktree_path")
            ):
                return (False, {}, f"slot{int(slot_obj.get('slot_id') or 1)}_unanchored")

        r_dir = _resolve_lock_target_repo_dir(act) or _resolve_repo_dir_for_slug(target_repo, portfolio_dir=portfolio_dir)
        peers: list[dict[str, Any]] = [dict(s) for s in active_slots]
        peers.extend({**r_item, "slot_id": r_id} for r_id, r_item in reserved)
        ov_res = analyze_candidate_against_slots(peers, item, repo_dir=r_dir, repo_slug=target_repo)
        reserved_ids = {r_id for r_id, _ in reserved}
        pairwise = [
            {
                "peer": "candidate" if int(p.get("slot_id") or 0) in reserved_ids else "slot",
                "slot_id": int(p.get("slot_id") or 0),
                "conversation_id": str(p.get("task_a_id") or ""),
                "overlap_tier": str(p.get("overlap_tier") or ""),
                "parallel_safe": bool(p.get("parallel_safe")),
            }
            for p in (ov_res.get("pairwise") or [])
        ]
        if not ov_res.get("parallel_safe"):
            blocking_ids = [int(x) for x in (ov_res.get("blocking_slot_ids") or [])]
            first_block = next((p for p in pairwise if not p["parallel_safe"]), {})
            block_tier = str(first_block.get("overlap_tier") or ov_res.get("overlap_tier") or "DEEP_SHARED")
            return (
                False,
                {
                    "overlap_tier": block_tier,
                    "overlap_with_slot": blocking_ids[0] if blocking_ids else 0,
                    "overlap_with": str(first_block.get("peer") or "slot"),
                    "blocking_slot_ids": blocking_ids,
                    "pairwise_overlap": pairwise,
                },
                f"overlap_{block_tier.lower()}",
            )
        raw_pairs = list(ov_res.get("pairwise") or [])
        monolith_disjoint = any(bool(p.get("monolith_disjoint")) for p in raw_pairs)
        requires_reverify = any(bool(p.get("requires_rebase_reverify")) for p in raw_pairs)
        worst_tier = str(ov_res.get("overlap_tier") or "NONE")
        landing_order = [str(c) for c in (ov_res.get("landing_order") or []) if str(c).strip()]

        annotated = dict(item)
        annotated["target_slot"] = free_slot_id
        annotated["free_slot_id"] = free_slot_id
        annotated["next_slot_id"] = free_slot_id
        annotated["slot_allocation"] = f"slot_{free_slot_id}_lookahead"
        annotated["max_slots"] = max_slots
        annotated["active_slot_ids"] = active_ids
        annotated["dispatch_repos"] = [target_repo]
        annotated["overlap_tier"] = worst_tier
        annotated["pairwise_overlap"] = pairwise
        annotated["monolith_disjoint"] = monolith_disjoint
        annotated["requires_rebase_reverify"] = requires_reverify
        annotated["landing_order"] = landing_order
        annotated["slot1_lock_id"] = str(act.get("lock_id") or "")
        annotated["slot1_branch_name"] = str(act.get("branch_name") or "")
        annotated["slot1_status"] = str(act.get("status") or "")
        return (True, annotated, "")

    prioritize_partial_wip = _resolve_prioritize_partial_wip()
    max_partial_wip = _resolve_max_partial_wip()

    # Count open half-merged (`merged_repos` non-empty) items waiting on each remaining downstream repo
    partial_wip_by_repo: dict[str, int] = {}
    for open_it in items:
        open_st = str(open_it.get("status") or "pending").strip().lower()
        if open_st not in DEV_QUEUE_OPEN_STATUSES:
            continue
        open_merged = {
            dev_common.normalize_repo_slug(str(r))
            for r in (open_it.get("merged_repos") or [])
            if str(r).strip()
        }
        if not open_merged:
            continue
        open_repos = _normalize_str_list(
            [dev_common.normalize_repo_slug(str(r)) for r in (open_it.get("repo_slugs") or []) if str(r).strip()]
        )
        for rem_slug in open_repos:
            if rem_slug and rem_slug not in open_merged:
                partial_wip_by_repo[rem_slug] = partial_wip_by_repo.get(rem_slug, 0) + 1

    def _has_partial_wip(item: Mapping[str, Any]) -> bool:
        return any(str(r).strip() for r in (item.get("merged_repos") or []))

    def _annotate_dispatch_hints(item: dict[str, Any]) -> dict[str, Any]:
        """Attach 0-LLM `skip_planner_subagent` fast-path & 3-tier `risk_classification` hints."""
        tfiles = [str(f).strip() for f in (item.get("target_files") or []) if str(f).strip()]
        sanchors = [str(a).strip() for a in (item.get("section_anchors") or []) if str(a).strip()]
        pfix = str(item.get("proposed_fix") or "").strip()
        item["skip_planner_subagent"] = bool(tfiles and (pfix or sanchors))
        item["risk_classification"] = dev_common.classify_change_risk_tier(
            tfiles,
            title=str(item.get("title") or ""),
            proposed_fix=pfix,
        )
        return item

    def _dispatch_order_key(item: Mapping[str, Any]) -> tuple[int, int, int, int, int, str, str]:
        """Order queue-next candidates: preserve P<=17 infra precedence, then boost partial-WIP closure over 0% items."""
        st_rank = DEV_QUEUE_STATUS_RANK.get(str(item.get("status") or "pending").lower(), 9)
        eff_p = int(item.get("effective_priority", _raw_queue_priority(item)))
        raw_p = _raw_queue_priority(item)
        wip_rank = 0 if (prioritize_partial_wip and _has_partial_wip(item)) else 1
        if eff_p <= INFRA_PRIORITY_PRECEDENCE_MAX:
            band_a, band_b, band_c = (0, eff_p, wip_rank)
        else:
            band_a, band_b, band_c = (1, wip_rank, eff_p)
        return (
            st_rank,
            band_a,
            band_b,
            band_c,
            raw_p,
            str(item.get("added_at") or ""),
            str(item.get("conversation_id") or ""),
        )

    next_item: dict[str, Any] | None = None
    next_pending_item: dict[str, Any] | None = None
    next_unlocked_item: dict[str, Any] | None = None
    runnable_by_repo: dict[str, dict[str, Any]] = {}
    multi_slot_candidates: list[dict[str, Any]] = []
    multi_slot_by_repo: dict[str, dict[str, Any]] = {}
    blocked_deps: list[dict[str, Any]] = []
    blocked_repos: list[dict[str, Any]] = []
    blocked_partial_wip: list[dict[str, Any]] = []

    for it in sorted(items, key=_dispatch_order_key):
        st = str(it.get("status") or "pending").lower()
        if st not in ("in_progress", "pr_open", "pending"):
            continue
        unmet = _deps_unmet(it)
        if unmet:
            blocked_deps.append(
                {
                    "conversation_id": it.get("conversation_id"),
                    "title": it.get("title"),
                    "unmet_dependencies": unmet,
                }
            )
            continue
        remaining_repos, dispatch_repos, foreign_locked, self_locked = _item_repo_schedule(it)
        is_self_waiting = bool(remaining_repos and remaining_repos[0] in self_locked)
        is_contract_blocked = bool(remaining_repos and not dispatch_repos and foreign_locked)

        if is_contract_blocked:
            first_repo = remaining_repos[0]
            s2_ok, s2_annotated, s2_reason = _probe_parallel_slot_lookahead(it, first_repo)
            if s2_ok:
                s2_annotated["wip_finish_boost"] = bool(prioritize_partial_wip and _has_partial_wip(it))
                _annotate_dispatch_hints(s2_annotated)
                multi_slot_candidates.append(s2_annotated)
                multi_slot_by_repo.setdefault(first_repo, s2_annotated)
                reserved_by_repo.setdefault(first_repo, []).append((int(s2_annotated["free_slot_id"]), s2_annotated))
                if first_repo not in runnable_by_repo:
                    runnable_by_repo[first_repo] = s2_annotated
                if next_unlocked_item is None:
                    next_unlocked_item = s2_annotated
                if next_item is None:
                    next_item = s2_annotated
                if st == "pending" and next_pending_item is None:
                    next_pending_item = s2_annotated
                continue

            blk_entry: dict[str, Any] = {
                "conversation_id": it.get("conversation_id"),
                "title": it.get("title"),
                "locked_repos": foreign_locked,
            }
            if s2_annotated.get("overlap_tier"):
                blk_entry["overlap_tier"] = s2_annotated["overlap_tier"]
            if s2_annotated.get("blocking_slot_ids"):
                blk_entry["blocking_slot_ids"] = list(s2_annotated["blocking_slot_ids"])
            if s2_reason:
                blk_entry["slot2_blocked_reason"] = s2_reason
                blk_entry["parallel_slot_blocked_reason"] = s2_reason
            blocked_repos.append(blk_entry)
            if skip_locked_repos:
                continue
        elif is_self_waiting:
            # Already actively developing or awaiting PR merge on its contract-first repo; do not re-dispatch as unlocked
            if skip_locked_repos:
                continue
        else:
            is_new_zero_pct_partial = bool(foreign_locked) and not _has_partial_wip(it)
            capped_downstream = (
                [r for r in foreign_locked if partial_wip_by_repo.get(r, 0) >= max_partial_wip]
                if is_new_zero_pct_partial
                else []
            )
            if capped_downstream:
                cap_entry: dict[str, Any] = {
                    "conversation_id": it.get("conversation_id"),
                    "title": it.get("title"),
                    "capped_downstream_repos": capped_downstream,
                    "partial_wip_counts": {r: partial_wip_by_repo[r] for r in capped_downstream},
                    "max_partial_wip": max_partial_wip,
                }
                blocked_partial_wip.append(cap_entry)
                blocked_repos.append(
                    {
                        "conversation_id": it.get("conversation_id"),
                        "title": it.get("title"),
                        "locked_repos": foreign_locked,
                        "partial_wip_capped": True,
                        "capped_downstream_repos": capped_downstream,
                    }
                )
                continue

            annotated = dict(it)
            annotated["wip_finish_boost"] = bool(prioritize_partial_wip and _has_partial_wip(it))
            _annotate_dispatch_hints(annotated)
            if remaining_repos:
                annotated["target_slot"] = 1
                annotated["dispatch_repos"] = list(dispatch_repos)
                annotated["partial_multi_repo_dispatch"] = bool(foreign_locked)
                annotated["waiting_locked_repos"] = list(foreign_locked)
            if next_unlocked_item is None:
                next_unlocked_item = annotated
            for free_repo in dispatch_repos:
                if free_repo not in runnable_by_repo:
                    runnable_by_repo[free_repo] = annotated
            it_out = annotated
            if next_item is None:
                next_item = it_out
            if st == "pending" and next_pending_item is None:
                next_pending_item = it_out
            continue

        if next_item is None:
            next_item = it
        if st == "pending" and next_pending_item is None:
            next_pending_item = it

    return {
        "status": "ok",
        "paused": False,
        "orchestration_control": orch_ctrl,
        "queue_path": data.get("queue_path", ""),
        "orchestrator_conversation_id": data.get("orchestrator_conversation_id", ""),
        "has_next": next_item is not None,
        "next_item": next_item,
        "next_pending_item": next_pending_item,
        "next_unlocked_item": next_unlocked_item,
        "runnable_by_repo": runnable_by_repo,
        "multi_slot_candidates": multi_slot_candidates,
        "multi_slot_by_repo": multi_slot_by_repo,
        # Legacy aliases (pre-N-slot clients): populated identically to `multi_slot_*`
        "slot2_candidates": list(multi_slot_candidates),
        "slot2_by_repo": dict(multi_slot_by_repo),
        "locked_repos": locked_repos,
        "partial_wip_by_repo": partial_wip_by_repo,
        "blocked_by_dependencies": blocked_deps,
        "blocked_by_repo_locks": blocked_repos,
        "blocked_by_partial_wip_cap": blocked_partial_wip,
        "summary": data.get("summary", {}),
    }


def register_queue_cli(sub: Any) -> None:
    """Register `dev_queue.json`, optimizer, and doctor CLI subcommands."""
    from dev_queue_doctor import DEFAULT_WAKE_COOLDOWN_SECONDS

    p_qlist = sub.add_parser("queue-list", help="Inspect persistent orchestrator development queue (`dev_queue.json`).")
    p_qlist.add_argument("--status", default="", help="Optional status filter.")

    p_qadd = sub.add_parser("queue-add", help="Add or update a task in `dev_queue.json`.")
    p_qadd.add_argument("--id", "--conversation-id", dest="conversation_id", required=True, help="Task Conversation ID.")
    p_qadd.add_argument("--title", default="", help="Short descriptive title.")
    p_qadd.add_argument("--action-id", default="", help="Action or prompt scope ID.")
    p_qadd.add_argument("--priority", type=int, default=50, help="Numeric priority (lower executes first).")
    p_qadd.add_argument(
        "--importance",
        choices=IMPORTANCE_TIERS,
        default="",
        help="Optional explicit business importance tier (critical|high|medium|low).",
    )
    p_qadd.add_argument(
        "--component",
        default="",
        help="Optional explicit sub-component label (e.g. dev-queue-panel, auth).",
    )
    p_qadd.add_argument("--repos", nargs="*", default=[], help="Target repository slugs.")
    p_qadd.add_argument("--status", choices=DEV_QUEUE_STATUSES, default="pending", help="Queue item status.")
    p_qadd.add_argument("--depends-on", nargs="*", default=[], help="Dependency conversation IDs (merged into existing `depends_on`).")
    p_qadd.add_argument(
        "--replace-depends-on",
        dest="replace_depends_on",
        nargs="*",
        default=None,
        help="Replace the item's `depends_on` list with these IDs instead of merging (no IDs = clear).",
    )
    p_qadd.add_argument(
        "--clear-depends-on",
        dest="clear_depends_on",
        action="store_true",
        help="Empty the item's `depends_on` list (takes precedence over --replace-depends-on / --depends-on).",
    )
    p_qadd.add_argument("--rca-summary", default="", help="RCA summary.")
    p_qadd.add_argument("--proposed-fix", default="", help="Proposed fix summary.")
    p_qadd.add_argument("--target-files", nargs="*", default=[], help="Target file paths.")
    p_qadd.add_argument("--section-anchors", nargs="*", default=[], help="Optional section/symbol/line anchors.")
    p_qadd.add_argument("--branches", nargs="*", default=[], help="Associated branch names.")
    p_qadd.add_argument("--pr-urls", nargs="*", default=[], help="Associated PR or Compare URLs.")
    p_qadd.add_argument("--notes", default="", help="Orchestrator notes.")
    p_qadd.add_argument("--orchestrator-id", default="", help="Optional active orchestrator conversation ID.")

    p_qset = sub.add_parser("queue-set-status", help="Update the status or links of an item in `dev_queue.json`.")
    p_qset.add_argument("--id", "--conversation-id", dest="conversation_id", required=True, help="Task Conversation ID.")
    p_qset.add_argument("--status", choices=DEV_QUEUE_STATUSES, required=True, help="New status.")
    p_qset.add_argument("--branches", nargs="*", default=[], help="Branch names to attach.")
    p_qset.add_argument("--pr-urls", nargs="*", default=[], help="PR/Compare URLs to attach.")
    p_qset.add_argument("--notes", default="", help="Optional updated notes.")
    p_qset.add_argument(
        "--importance",
        choices=IMPORTANCE_TIERS,
        default=None,
        help="Optional updated importance tier (critical|high|medium|low).",
    )
    p_qset.add_argument(
        "--component",
        default=None,
        help="Optional updated sub-component label.",
    )
    p_qset.add_argument("--orchestrator-id", default="", help="Optional active orchestrator conversation ID.")

    p_qplan = sub.add_parser(
        "queue-plan",
        help="Emit deterministic 0-LLM Repo -> Sub-Component planning view (Importance x Wave & Recently Integrated).",
    )
    p_qplan.add_argument("--repo", dest="repo_slug", default="", help="Optional repository slug/substring filter.")
    p_qplan.add_argument(
        "--importance",
        choices=IMPORTANCE_TIERS,
        default="",
        help="Optional importance tier filter.",
    )
    p_qplan.add_argument(
        "--include-done",
        dest="include_done",
        type=int,
        default=10,
        help="Maximum recently integrated (merged/cleaned) items to include (default: 10).",
    )
    p_qplan.add_argument("--json", dest="as_json", action="store_true", help="Emit structured JSON snapshot (default).")

    p_qnext = sub.add_parser("queue-next", help="Select the highest-priority runnable item from `dev_queue.json`.")
    p_qnext.add_argument("--orchestrator-id", default="", help="Optional active orchestrator conversation ID to record.")
    p_qnext.add_argument("--unlocked", action="store_true", help="Skip queue items whose target repo currently holds an active lock.")

    p_qsync = sub.add_parser(
        "queue-sync",
        help="Reconcile stale `pr_open` / branched `in_progress` items in `dev_queue.json` against lock history & native Git merge state, pruning stale compare `pr_urls`.",
    )
    p_qsync.add_argument("--repo", dest="repo_slug", default="", help="Optional owner/repo slug to scope reconciliation.")
    p_qsync.add_argument("--workdir", default="", help="Optional target repository directory or worktree path to scope reconciliation.")
    p_qsync.add_argument("--fetch", action="store_true", help="Fetch origin refs before checking Git merge state.")

    p_qopt = sub.add_parser(
        "queue-optimize",
        help="Run deterministic 0-LLM self-diagnosing workflow optimization audit across locks, queue, managed repos, and bounded transcripts.",
    )
    p_qopt.add_argument("--json", dest="as_json", action="store_true", help="Emit structured JSON report (default output format).")
    p_qopt.add_argument("--orchestrator-id", dest="orchestrator_id", default="", help="Active orchestrator conversation ID for bloat diagnostics.")
    p_qopt.add_argument("--repos-dir", dest="repos_dir", default="", help="Optional parent directory of managed local git repositories.")
    p_qopt.add_argument("--brain-dir", dest="brain_dir", default=None, help="Optional Antigravity brain directory override for read-only transcript telemetry.")
    p_qopt.add_argument("--stall-seconds", dest="stall_seconds", type=int, default=None, help="Override stalled-worker idle threshold in seconds (default: 900).")

    p_qdoc = sub.add_parser(
        "queue-doctor",
        help="0-LLM orchestrator diagnosis, safe runtime auto-heal (--fix), cooldown+PID-gated wake-up (--wake), and PR-only improvement advisor.",
    )
    p_qdoc.add_argument("--json", dest="as_json", action="store_true", help="Emit structured JSON diagnosis report.")
    p_qdoc.add_argument("--sync-git", dest="sync_git", action="store_true", help="Sync per-repo locks and local main with git before diagnosing.")
    p_qdoc.add_argument("--fix", action="store_true", help="Apply safe runtime-only reconciliations (sync locks & phantom pr_open items; never mutates tracked code).")
    p_qdoc.add_argument("--wake", action="store_true", help="Launch detached hybrid `agentapi` (Desktop/sidecar) or `agy --agent agy-dev-orchestrator` (terminal) recovery when needed (cooldown + live-PID gated).")
    p_qdoc.add_argument("--heal", action="store_true", help="Shorthand for `--sync-git --fix --wake`.")
    p_qdoc.add_argument("--check-only", dest="check_only", action="store_true", help="0-LLM sidecar/cron health gate: exit 0 when healthy, exit 2 when anomaly requires wake or fix.")
    p_qdoc.add_argument("--dry-run", dest="dry_run", action="store_true", help="Preview safe fixes and exact wake command/prompt without spawning processes or mutating state.")
    p_qdoc.add_argument("--force", action="store_true", help="Override wake cooldown or healthy skip check when `--wake` is passed.")
    p_qdoc.add_argument("--cooldown-seconds", dest="cooldown_seconds", type=int, default=DEFAULT_WAKE_COOLDOWN_SECONDS, help=f"Minimum seconds between orchestrator wake-ups (default: {DEFAULT_WAKE_COOLDOWN_SECONDS}).")
    p_qdoc.add_argument("--stall-seconds", dest="stall_seconds", type=int, default=None, help="Override stalled-worker/orchestrator idle threshold in seconds (default: 900).")
    p_qdoc.add_argument("--repos-dir", dest="repos_dir", default="", help="Optional parent directory of managed local git repositories.")
    p_qdoc.add_argument("--state-dir", dest="state_dir", default="", help="Optional state directory override.")
    p_qdoc.add_argument("--brain-dir", dest="brain_dir", default=None, help="Optional Antigravity brain directory override.")
    p_qdoc.add_argument("--orchestrator-id", dest="orchestrator_id", default="", help="Optional orchestrator conversation ID override.")
    p_qdoc.add_argument("--print-prompt", dest="print_prompt", action="store_true", help="Print the pre-computed wake-up prompt to stdout.")
    p_qdoc.add_argument("--no-color", dest="no_color", action="store_true", help="Disable ANSI color codes in terminal report.")


def dispatch_queue_cli(args: Any) -> int | None:
    """Handle queue, optimizer, and doctor CLI subcommands; return exit code or None if unhandled."""
    if args.subcommand == "queue-list":
        q_res = _facade_attr("load_dev_queue", load_dev_queue)()
        st_filter = str(getattr(args, "status", "") or "").strip().lower()
        if st_filter:
            q_res["items"] = [it for it in q_res.get("items") or [] if str(it.get("status") or "").lower() == st_filter]
        print(json.dumps({"status": "ok", **q_res}, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "queue-add":
        q_res = _facade_attr("enqueue_dev_queue_item", enqueue_dev_queue_item)(
            conversation_id=args.conversation_id,
            title=args.title,
            action_id=args.action_id,
            priority=args.priority,
            repo_slugs=args.repos,
            status=args.status,
            depends_on=args.depends_on,
            rca_summary=args.rca_summary,
            proposed_fix=args.proposed_fix,
            target_files=args.target_files,
            section_anchors=getattr(args, "section_anchors", []) or None,
            replace_depends_on=getattr(args, "replace_depends_on", None),
            clear_depends_on=bool(getattr(args, "clear_depends_on", False)),
            branch_names=args.branches,
            pr_urls=args.pr_urls,
            notes=args.notes,
            orchestrator_conversation_id=args.orchestrator_id,
            importance=getattr(args, "importance", "") or "",
            component=getattr(args, "component", "") or "",
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") == "ok" else 1

    if args.subcommand == "queue-set-status":
        q_res = _facade_attr("update_dev_queue_item_status", update_dev_queue_item_status)(
            conversation_id=args.conversation_id,
            status=args.status,
            branch_names=args.branches or None,
            pr_urls=args.pr_urls or None,
            notes=args.notes or None,
            orchestrator_conversation_id=args.orchestrator_id,
            importance=getattr(args, "importance", None),
            component=getattr(args, "component", None),
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") == "ok" else 1

    if args.subcommand == "queue-plan":
        q_res = _facade_attr("load_dev_queue", load_dev_queue)()
        active_locks = _facade_attr("get_all_dev_pr_locks", get_all_dev_pr_locks)()
        plan = build_component_planning_snapshot(
            q_res.get("items") or [],
            locks=active_locks,
            repo_filter=str(getattr(args, "repo_slug", "") or ""),
            importance_filter=str(getattr(args, "importance", "") or ""),
            include_done=int(getattr(args, "include_done", 10)),
        )
        print(json.dumps({"status": "ok", "queue_path": q_res.get("queue_path", ""), **plan}, indent=2, ensure_ascii=False))
        return 0

    if args.subcommand == "queue-next":
        q_res = _facade_attr("get_next_dev_queue_item", get_next_dev_queue_item)(
            orchestrator_conversation_id=args.orchestrator_id,
            skip_locked_repos=bool(getattr(args, "unlocked", False)),
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") == "ok" else 1

    if args.subcommand == "queue-sync":
        q_res = _facade_attr("reconcile_dev_queue_with_git", reconcile_dev_queue_with_git)(
            repo_slug=getattr(args, "repo_slug", ""),
            workdir=getattr(args, "workdir", "") or None,
            fetch=bool(getattr(args, "fetch", False)),
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") in ("ok", "skipped") else 1

    if args.subcommand == "queue-optimize":
        from dev_flow_optimizer import analyze_dev_flow_optimizations

        q_res = _facade_attr("analyze_dev_flow_optimizations", analyze_dev_flow_optimizations)(
            repos_dir=getattr(args, "repos_dir", "") or None,
            brain_dir=getattr(args, "brain_dir", None),
            orchestrator_cid=getattr(args, "orchestrator_id", ""),
            stall_threshold_seconds=getattr(args, "stall_seconds", None),
        )
        print(json.dumps(q_res, indent=2, ensure_ascii=False))
        return 0 if q_res.get("status") == "ok" else 1

    if args.subcommand == "queue-doctor":
        from dev_queue_doctor import (
            DEFAULT_WAKE_COOLDOWN_SECONDS,
            render_doctor_report,
            run_queue_doctor,
        )

        doc_res = _facade_attr("run_queue_doctor", run_queue_doctor)(
            state_dir=getattr(args, "state_dir", "") or None,
            repos_dir=getattr(args, "repos_dir", "") or None,
            brain_dir=getattr(args, "brain_dir", None),
            orchestrator_cid=getattr(args, "orchestrator_id", ""),
            sync_git=bool(getattr(args, "sync_git", False)),
            fix=bool(getattr(args, "fix", False)),
            wake=bool(getattr(args, "wake", False)),
            heal=bool(getattr(args, "heal", False)),
            check_only=bool(getattr(args, "check_only", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
            force=bool(getattr(args, "force", False)),
            cooldown_seconds=int(getattr(args, "cooldown_seconds", DEFAULT_WAKE_COOLDOWN_SECONDS)),
            stall_threshold_seconds=getattr(args, "stall_seconds", None),
        )
        if bool(getattr(args, "as_json", False)):
            print(json.dumps(doc_res, indent=2, ensure_ascii=False))
        elif bool(getattr(args, "print_prompt", False)):
            print(str(doc_res.get("wake_prompt") or ""))
        else:
            color_on = (not bool(getattr(args, "no_color", False))) and bool(
                getattr(sys.stdout, "isatty", lambda: False)()
            ) and not bool(os.environ.get("NO_COLOR"))
            print(_facade_attr("render_doctor_report", render_doctor_report)(doc_res, color=color_on))
        return int(doc_res.get("exit_code", 0))

    return None
