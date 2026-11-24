#!/usr/bin/env python3
"""Deterministic 0-LLM PR Queue Compaction Engine (`queue_compactor.py`).

Detects and atomically compacts high-overlap, dependency-chained, partial-WIP, and
split-phase multi-repo pending items in `dev_queue.json` under strict fail-closed
safety guardrails (zero LLM calls, zero live-lock mutations, zero cycles/deadlocks).

Supported compaction patterns:
  1. `same_remaining_repo` (Tier 1 & Tier 2): merges two `pending` items sharing the
     exact same single remaining repository (including partial-WIP items) when they
     have direct `depends_on` linkage, high non-hub file subset overlap, or same
     non-generic `component` with shared domain files.
  2. `split_phase_multi_repo` (Tier 1 & Tier 2): splits a 2-repo `pending` item
     (`merged_repos == []`) cleanly when 100% of its `target_files` partition
     unambiguously across both repositories — leaving the auxiliary repo phase as an
     independent parallel item while consolidating the bottleneck repo phase into an
     overlapping single-remaining-repo item.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import sys
from typing import Any

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

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
import dev_queue_planner  # noqa: E402
import lock_store  # noqa: E402
import orchestration_control  # noqa: E402
import queue_store  # noqa: E402

DEFAULT_MAX_COMBINED_FILES = 16
DEFAULT_BATCH_GRACE_FILES = 2
DEFAULT_MAX_COMBINED_FIX_CHARS = 12000
DEFAULT_EPIC_FILE_THRESHOLD = 18
DEFAULT_MAX_PRIORITY_SPREAD = 4
DEFAULT_DEPENDS_PRIORITY_SPREAD = 8
DEFAULT_SUBSET_RATIO_MIN = 0.70
DEFAULT_MIN_SHARED_DOMAIN_FILES = 2
DEFAULT_COMPACT_SAVINGS_MIN = 18

MAX_FILES_ENV = "AGY_DEV_COMPACT_MAX_FILES"
MAX_FIX_CHARS_ENV = "AGY_DEV_COMPACT_MAX_FIX_CHARS"

# Global monolith / shell / manifest / hub files that many unrelated tasks touch.
# They still count toward the combined file ceiling, but NEVER trigger domain cohesion alone.
GLOBAL_HUB_FILES: frozenset[str] = frozenset(
    {
        "web/index.html",
        "web/dashboard.js",
        "web/styles.css",
        "index.html",
        "styles.css",
        "readme.md",
        "changelog.md",
        "pyproject.toml",
        "install.sh",
        "plugin.json",
        "tests/__init__.py",
        "tests/scripts/selective_test_runner.py",
        "tests/test_control_tower_architecture_and_api.py",
        "tests/test_ct_architecture_and_routes.py",
        "docs/architecture_and_workflows.md",
    }
)

GLOBAL_HUB_BASENAMES: frozenset[str] = frozenset(
    {
        "index.html",
        "__init__.py",
        "selective_test_runner.py",
        "readme.md",
        "changelog.md",
        "pyproject.toml",
    }
)

SENSITIVE_SCOPE_RE = re.compile(
    r"\b(?:security|guardrail|guardrails|global[-_\s]?reader|read[-_\s]?only|"
    r"encrypted|oauth|pkce|rbac|credential|credentials|secret|secrets|"
    r"session[-_\s]?manager|cirt|incident[-_\s]?response|pretool)\b",
    re.IGNORECASE,
)

SENSITIVE_FILE_RE = re.compile(
    r"(?:^|/)(?:hooks\.json|lifecycle_guardrails\.py|.*auth.*\.py|.*security.*\.py|"
    r".*verify_tenant.*|.*session_client\.py)$",
    re.IGNORECASE,
)

NO_COMPACT_RE = re.compile(r"\[no[-_\s]?compact\]", re.IGNORECASE)
EXISTING_BATCH_RE = re.compile(r"\b(?:polish\s+batch|ui\s+batch|batch\s*\()\b", re.IGNORECASE)

ACTIVE_QUEUE_STATUSES: frozenset[str] = frozenset({"pending", "in_progress", "pr_open"})
DEAD_DEP_STATUSES: frozenset[str] = frozenset({"cancelled", "rejected", "standby"})

_IMPORTANCE_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_path(raw: str) -> str:
    cleaned = str(raw or "").strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _dedupe_strs(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _resolve_int_env(env_name: str, default: int, *, low: int, high: int) -> int:
    raw = str(os.environ.get(env_name) or "").strip()
    if raw:
        try:
            return max(low, min(high, int(raw)))
        except ValueError:
            pass
    return max(low, min(high, int(default)))


def _resolve_max_combined_files(override: int | None = None) -> int:
    if override is not None:
        return max(6, min(24, int(override)))
    return _resolve_int_env(MAX_FILES_ENV, DEFAULT_MAX_COMBINED_FILES, low=6, high=24)


def _resolve_max_combined_fix_chars(override: int | None = None) -> int:
    if override is not None:
        return max(4000, min(24000, int(override)))
    return _resolve_int_env(MAX_FIX_CHARS_ENV, DEFAULT_MAX_COMBINED_FIX_CHARS, low=4000, high=24000)


def _item_priority(item: Mapping[str, Any]) -> int:
    try:
        return int(item.get("priority") if item.get("priority") is not None else 50)
    except (TypeError, ValueError):
        return 50


def _item_component(item: Mapping[str, Any]) -> str:
    raw = str(item.get("component") or "").strip().lower()
    if raw:
        return dev_queue_planner.normalize_component_name(raw)
    return dev_queue_planner.normalize_component_name(dev_queue_planner.infer_subcomponent(dict(item)))


def _highest_importance(imp_a: Any, imp_b: Any) -> str:
    norm_a = dev_queue_planner.normalize_importance_tier(imp_a) or ""
    norm_b = dev_queue_planner.normalize_importance_tier(imp_b) or ""
    if not norm_a:
        return norm_b
    if not norm_b:
        return norm_a
    return norm_a if _IMPORTANCE_ORDER.get(norm_a, 9) <= _IMPORTANCE_ORDER.get(norm_b, 9) else norm_b


def is_hub_file(path: str) -> bool:
    """Return True when `path` is a global hub/boilerplate file that must not trigger overlap alone."""
    norm = _normalize_path(path).lower()
    if not norm:
        return True
    if norm in GLOBAL_HUB_FILES:
        return True
    base = norm.rsplit("/", 1)[-1]
    return base in GLOBAL_HUB_BASENAMES


def get_remaining_repos(item: Mapping[str, Any]) -> list[str]:
    """Return normalized repository slugs not yet merged for `item`."""
    repos = [
        dev_common.normalize_repo_slug(str(r))
        for r in (item.get("repo_slugs") or [])
        if str(r).strip()
    ]
    merged = {
        dev_common.normalize_repo_slug(str(r))
        for r in (item.get("merged_repos") or [])
        if str(r).strip()
    }
    return _dedupe_strs([r for r in repos if r and r not in merged])


def _collect_locked_conversation_ids(active_locks: Mapping[str, Any] | None) -> set[str]:
    """Extract every conversation_id currently holding any active slot in `dev_pr_locks`."""
    locked: set[str] = set()
    if not isinstance(active_locks, Mapping):
        return locked
    for lock_entry in active_locks.values():
        if not isinstance(lock_entry, Mapping):
            continue
        top_cid = str(lock_entry.get("conversation_id") or "").strip()
        if top_cid and lock_entry.get("active", True):
            locked.add(top_cid)
        slots = lock_entry.get("slots")
        if isinstance(slots, Mapping):
            for sdata in slots.values():
                if not isinstance(sdata, Mapping) or not sdata.get("active", True):
                    continue
                s_cid = str(sdata.get("conversation_id") or "").strip()
                if s_cid:
                    locked.add(s_cid)
                for sib in sdata.get("sibling_conversation_ids") or []:
                    sib_s = str(sib or "").strip()
                    if sib_s:
                        locked.add(sib_s)
    return locked


def _cid_is_locked(cid: str, locked_cids: set[str]) -> bool:
    if not cid or not locked_cids:
        return False
    cid_l = cid.lower()
    return any(cid_l == lc.lower() or cid_l[:8] == lc.lower()[:8] for lc in locked_cids if lc)


def is_compaction_excluded(
    item: Mapping[str, Any],
    *,
    remaining_files: Sequence[str] | None = None,
    locked_cids: set[str] | None = None,
    epic_threshold: int = DEFAULT_EPIC_FILE_THRESHOLD,
) -> tuple[bool, str]:
    """Evaluate fail-closed hard guardrails determining whether `item` is ineligible for compaction."""
    cid = str(item.get("conversation_id") or "").strip()
    status = str(item.get("status") or "pending").strip().lower()
    if status != "pending":
        return (True, f"status:{status}")
    if locked_cids and _cid_is_locked(cid, locked_cids):
        return (True, "active_lock")
    if bool(item.get("no_compact")):
        return (True, "opt_out_flag")
    notes = str(item.get("notes") or "")
    if NO_COMPACT_RE.search(notes):
        return (True, "opt_out_marker")

    files = [_normalize_path(f) for f in (remaining_files if remaining_files is not None else (item.get("target_files") or [])) if _normalize_path(f)]
    if not files:
        return (True, "unanchored_no_target_files")
    if len(files) >= epic_threshold:
        return (True, f"epic_size:{len(files)}>={epic_threshold}")

    scope_blob = " ".join(
        str(item.get(k) or "")
        for k in ("title", "action_id", "component")
    )
    if SENSITIVE_SCOPE_RE.search(scope_blob):
        return (True, "sensitive_security_scope")
    for fpath in files:
        if SENSITIVE_FILE_RE.search(fpath):
            return (True, f"sensitive_file:{fpath}")

    return (False, "")


def _extract_anchor_repo_hint(anchor: str, known_repos: Mapping[str, str]) -> tuple[str, str]:
    """Parse optional `<repo_slug_or_short>:<rel_path>[:symbol]` prefix from a `section_anchors` entry."""
    raw = str(anchor or "").strip()
    if ":" not in raw:
        return ("", "")
    prefix, rest = raw.split(":", 1)
    pref_l = prefix.strip().lower()
    repo = known_repos.get(pref_l, "")
    if not repo:
        return ("", "")
    # Extract file path segment before optional `:symbol` or `#anchor`
    path_part = rest.split("#", 1)[0].split(":", 1)[0].strip()
    return (repo, _normalize_path(path_part))


def infer_repo_path_owners(
    items: Sequence[Mapping[str, Any]],
    *,
    repo_dirs: Mapping[str, Path] | None = None,
) -> dict[str, Any]:
    """Dynamically infer zero-hardcode file & directory ownership across repos from queue history & anchors."""
    all_repos: set[str] = set()
    for it in items:
        for r in it.get("repo_slugs") or []:
            nr = dev_common.normalize_repo_slug(str(r))
            if nr:
                all_repos.add(nr)
    if repo_dirs:
        for r in repo_dirs:
            nr = dev_common.normalize_repo_slug(str(r))
            if nr:
                all_repos.add(nr)

    short_to_slug: dict[str, str] = {}
    short_counts: dict[str, int] = {}
    for slug in sorted(all_repos):
        short = slug.split("/")[-1].lower()
        short_counts[short] = short_counts.get(short, 0) + 1
    for slug in sorted(all_repos):
        short_to_slug[slug.lower()] = slug
        short = slug.split("/")[-1].lower()
        if short_counts.get(short, 0) == 1:
            short_to_slug[short] = slug

    exact_file_owners: dict[str, set[str]] = {}
    top_dir_owners: dict[str, set[str]] = {}
    sub_dir_owners: dict[str, set[str]] = {}

    for it in items:
        repos = _dedupe_strs(
            [dev_common.normalize_repo_slug(str(r)) for r in (it.get("repo_slugs") or []) if str(r).strip()]
        )
        # 1. Explicit repo-prefixed section_anchors provide exact ownership even on multi-repo items
        for anc in it.get("section_anchors") or []:
            r_slug, fpath = _extract_anchor_repo_hint(str(anc), short_to_slug)
            if r_slug and fpath and "/" in fpath:
                exact_file_owners.setdefault(fpath, set()).add(r_slug)
                top = fpath.split("/", 1)[0]
                if top != "tests":
                    top_dir_owners.setdefault(top, set()).add(r_slug)

        # 2. Single-repo items teach exact file and non-`tests/` directory ownership
        if len(repos) == 1:
            r_slug = repos[0]
            for raw_f in it.get("target_files") or []:
                fpath = _normalize_path(str(raw_f))
                if not fpath:
                    continue
                exact_file_owners.setdefault(fpath, set()).add(r_slug)
                parts = fpath.split("/")
                if len(parts) >= 2 and parts[0] != "tests":
                    top_dir_owners.setdefault(parts[0], set()).add(r_slug)
                if len(parts) >= 3:
                    sub_dir_owners.setdefault(f"{parts[0]}/{parts[1]}", set()).add(r_slug)

    return {
        "short_to_slug": short_to_slug,
        "exact_file_owners": exact_file_owners,
        "top_dir_owners": top_dir_owners,
        "sub_dir_owners": sub_dir_owners,
    }


def partition_item_files_by_repo(
    item: Mapping[str, Any],
    repos: Sequence[str],
    *,
    owner_index: Mapping[str, Any],
    repo_dirs: Mapping[str, Path] | None = None,
) -> tuple[dict[str, list[str]], list[str]]:
    """Partition `item.target_files` across `repos` unambiguously; ambiguous files are returned separately."""
    norm_repos = _dedupe_strs([dev_common.normalize_repo_slug(str(r)) for r in repos if str(r).strip()])
    repo_set = set(norm_repos)
    partitioned: dict[str, list[str]] = {r: [] for r in norm_repos}
    ambiguous: list[str] = []

    short_to_slug: Mapping[str, str] = owner_index.get("short_to_slug") or {}
    exact_owners: Mapping[str, set[str]] = owner_index.get("exact_file_owners") or {}
    top_owners: Mapping[str, set[str]] = owner_index.get("top_dir_owners") or {}
    sub_owners: Mapping[str, set[str]] = owner_index.get("sub_dir_owners") or {}

    # Item-local explicit repo-prefixed anchors have highest priority
    item_anchor_owner: dict[str, set[str]] = {}
    for anc in item.get("section_anchors") or []:
        r_slug, fpath = _extract_anchor_repo_hint(str(anc), short_to_slug)
        if r_slug in repo_set and fpath:
            item_anchor_owner.setdefault(fpath, set()).add(r_slug)

    norm_repo_dirs: dict[str, Path] = {}
    if repo_dirs:
        for k, v in repo_dirs.items():
            nk = dev_common.normalize_repo_slug(str(k))
            if nk in repo_set and isinstance(v, Path) and v.is_dir():
                norm_repo_dirs[nk] = v

    for raw_f in item.get("target_files") or []:
        fpath = _normalize_path(str(raw_f))
        if not fpath:
            continue

        resolved: str = ""
        # 1. Item-local anchor prefix
        local_hits = item_anchor_owner.get(fpath) or set()
        if len(local_hits) == 1:
            resolved = next(iter(local_hits))

        # 2. Local git repo disk presence (exact file first, then unique non-tests parent directory)
        if not resolved and len(norm_repo_dirs) == len(norm_repos) and norm_repo_dirs:
            exact_disk = [r for r in norm_repos if (norm_repo_dirs[r] / fpath).exists()]
            if len(exact_disk) == 1:
                resolved = exact_disk[0]
            elif len(exact_disk) == 0 and "/" in fpath:
                parent_rel = str(Path(fpath).parent)
                if parent_rel not in (".", "tests"):
                    dir_disk = [r for r in norm_repos if (norm_repo_dirs[r] / parent_rel).is_dir()]
                    if len(dir_disk) == 1:
                        resolved = dir_disk[0]

        # 3. Exact file owner learned from single-repo items / anchors
        if not resolved:
            ex_hits = (exact_owners.get(fpath) or set()) & repo_set
            if len(ex_hits) == 1:
                resolved = next(iter(ex_hits))

        # 4. Two-level subdir prefix owner (e.g., `tests/js`, `skills/foo`)
        if not resolved:
            parts = fpath.split("/")
            if len(parts) >= 3:
                sub_hits = (sub_owners.get(f"{parts[0]}/{parts[1]}") or set()) & repo_set
                if len(sub_hits) == 1:
                    resolved = next(iter(sub_hits))

        # 5. Top-level directory owner (excluding shared `tests/`)
        if not resolved and "/" in fpath:
            top = fpath.split("/", 1)[0]
            if top != "tests":
                top_hits = (top_owners.get(top) or set()) & repo_set
                if len(top_hits) == 1:
                    resolved = next(iter(top_hits))

        if resolved and resolved in partitioned:
            if fpath not in partitioned[resolved]:
                partitioned[resolved].append(fpath)
        else:
            ambiguous.append(fpath)

    return (partitioned, ambiguous)


def _remaining_files_for_single_repo(
    item: Mapping[str, Any],
    target_repo: str,
    *,
    owner_index: Mapping[str, Any],
    repo_dirs: Mapping[str, Path] | None = None,
) -> list[str]:
    """Return `target_files` for a single-remaining-repo item, stripping files owned solely by `merged_repos`."""
    all_files = _dedupe_strs([_normalize_path(str(f)) for f in (item.get("target_files") or []) if _normalize_path(str(f))])
    merged_repos = [
        dev_common.normalize_repo_slug(str(r))
        for r in (item.get("merged_repos") or [])
        if str(r).strip()
    ]
    if not merged_repos:
        return all_files
    part, ambiguous = partition_item_files_by_repo(
        item,
        [target_repo, *merged_repos],
        owner_index=owner_index,
        repo_dirs=repo_dirs,
    )
    # Keep target_repo files + any ambiguous files conservatively (never undercount)
    kept = _dedupe_strs([*(part.get(target_repo) or []), *ambiguous])
    return kept or all_files


def _build_active_dep_graph(items: Sequence[Mapping[str, Any]]) -> dict[str, set[str]]:
    """Build adjacency map `cid -> set[active_dep_cids]` for active items."""
    by_id = {str(it.get("conversation_id") or "").strip(): it for it in items if it.get("conversation_id")}
    active_ids = {
        cid
        for cid, it in by_id.items()
        if str(it.get("status") or "pending").strip().lower() in ACTIVE_QUEUE_STATUSES
    }
    graph: dict[str, set[str]] = {cid: set() for cid in active_ids}
    for cid in active_ids:
        for raw_dep in by_id[cid].get("depends_on") or []:
            dep = str(raw_dep or "").strip()
            if dep in active_ids and dep != cid:
                graph[cid].add(dep)
    return graph


def _find_cycles(graph: Mapping[str, set[str]]) -> list[list[str]]:
    """Return any dependency cycles in `graph` via deterministic DFS."""
    state: dict[str, int] = {}  # 0=unvisited, 1=visiting, 2=done
    cycles: list[list[str]] = []

    def _dfs(node: str, stack: list[str]) -> None:
        state[node] = 1
        for nxt in sorted(graph.get(node) or ()):
            st = state.get(nxt, 0)
            if st == 1:
                idx = stack.index(nxt) if nxt in stack else 0
                cycles.append([*stack[idx:], nxt])
            elif st == 0:
                _dfs(nxt, [*stack, nxt])
        state[node] = 2

    for node in sorted(graph.keys()):
        if state.get(node, 0) == 0:
            _dfs(node, [node])
    return cycles


def _can_reach(graph: Mapping[str, set[str]], start: str, target: str) -> bool:
    """Return True when `target` is reachable from `start` in `graph`."""
    if start == target:
        return True
    seen: set[str] = {start}
    queue = [start]
    while queue:
        cur = queue.pop(0)
        for nxt in graph.get(cur) or ():
            if nxt == target:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def validate_queue_invariants(
    items: Sequence[Mapping[str, Any]],
    *,
    active_locks: Mapping[str, Any] | None = None,
    before_items: Sequence[Mapping[str, Any]] | None = None,
) -> list[str]:
    """Verify hard post-compaction invariants: 0 cycles, 0 deadlocks, 0 live-item mutations."""
    issues: list[str] = []
    by_id = {str(it.get("conversation_id") or "").strip(): it for it in items if it.get("conversation_id")}

    # 1. Zero deadlocks: no active item may depend on cancelled/rejected/standby
    for cid, it in by_id.items():
        st = str(it.get("status") or "pending").strip().lower()
        if st not in ACTIVE_QUEUE_STATUSES:
            continue
        for raw_dep in it.get("depends_on") or []:
            dep_cid = str(raw_dep or "").strip()
            dep_item = by_id.get(dep_cid)
            if dep_item is None:
                continue
            dep_st = str(dep_item.get("status") or "").strip().lower()
            if dep_st in DEAD_DEP_STATUSES:
                issues.append(f"deadlock:{cid[:8]}->{dep_cid[:8]}({dep_st})")

    # 2. Zero dependency cycles across active items
    graph = _build_active_dep_graph(items)
    for cyc in _find_cycles(graph):
        issues.append("cycle:" + "->".join(c[:8] for c in cyc))

    # 3. Zero mutations to any item that was non-pending or actively locked
    if before_items is not None:
        locked_cids = _collect_locked_conversation_ids(active_locks)
        before_by_id = {str(it.get("conversation_id") or "").strip(): it for it in before_items if it.get("conversation_id")}
        for cid, prev in before_by_id.items():
            prev_st = str(prev.get("status") or "pending").strip().lower()
            if prev_st in ("in_progress", "pr_open") or _cid_is_locked(cid, locked_cids):
                curr = by_id.get(cid)
                if curr is None:
                    issues.append(f"live_item_deleted:{cid[:8]}")
                    continue
                for protected_key in ("status", "repo_slugs", "target_files", "proposed_fix"):
                    if curr.get(protected_key) != prev.get(protected_key):
                        issues.append(f"live_item_mutated:{cid[:8]}:{protected_key}")

    return issues


def _select_same_repo_absorber(
    item_a: Mapping[str, Any],
    item_b: Mapping[str, Any],
    *,
    reverse_deps: Mapping[str, set[str]],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Choose `(absorber, absorbed)` maximizing partial-WIP preservation and minimizing dependent rewires."""
    cid_a = str(item_a.get("conversation_id") or "")
    cid_b = str(item_b.get("conversation_id") or "")

    def _rank(it: Mapping[str, Any], cid: str, other_cid: str) -> tuple[int, int, int, int, str]:
        is_partial = 0 if bool(it.get("merged_repos")) else 1
        # External dependents (excluding the partner itself)
        ext_deps = len((reverse_deps.get(cid) or set()) - {other_cid})
        # If one item already consolidates an existing polish batch, prefer keeping its ID
        is_batch = 0 if EXISTING_BATCH_RE.search(str(it.get("title") or "")) else 1
        return (is_partial, -ext_deps, is_batch, _item_priority(it), cid)

    if _rank(item_a, cid_a, cid_b) <= _rank(item_b, cid_b, cid_a):
        return (item_a, item_b)
    return (item_b, item_a)


def _evaluate_same_repo_pair(
    item_a: Mapping[str, Any],
    item_b: Mapping[str, Any],
    *,
    target_repo: str,
    files_a: list[str],
    files_b: list[str],
    dep_graph: Mapping[str, set[str]],
    reverse_deps: Mapping[str, set[str]],
    max_combined_files: int,
    max_combined_fix_chars: int,
    allow_explicit_override: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """Score a `same_remaining_repo` candidate pair and return `(plan, skip_reason)`."""
    cid_a = str(item_a.get("conversation_id") or "")
    cid_b = str(item_b.get("conversation_id") or "")

    # Check direct dependency link
    a_deps_b = cid_b in (dep_graph.get(cid_a) or set())
    b_deps_a = cid_a in (dep_graph.get(cid_b) or set())
    direct_dep = a_deps_b or b_deps_a

    # Combined file cap (allow +2 grace if one item is already an explicit UI polish batch)
    combined_files = _dedupe_strs([*files_a, *files_b])
    has_existing_batch = bool(
        EXISTING_BATCH_RE.search(str(item_a.get("title") or ""))
        or EXISTING_BATCH_RE.search(str(item_b.get("title") or ""))
    )
    eff_max_files = max_combined_files + (DEFAULT_BATCH_GRACE_FILES if has_existing_batch else 0)
    if len(combined_files) > eff_max_files:
        return (None, f"exceeds_max_files:{len(combined_files)}>{eff_max_files}")

    # Combined spec size cap (guards against mega-PR review Round 2/3 bloat)
    combined_fix_chars = len(str(item_a.get("proposed_fix") or "")) + len(str(item_b.get("proposed_fix") or ""))
    if combined_fix_chars > max_combined_fix_chars:
        return (None, f"exceeds_max_fix_chars:{combined_fix_chars}>{max_combined_fix_chars}")

    # Priority spread check
    prio_a, prio_b = _item_priority(item_a), _item_priority(item_b)
    prio_spread = abs(prio_a - prio_b)
    max_spread = DEFAULT_DEPENDS_PRIORITY_SPREAD if direct_dep else DEFAULT_MAX_PRIORITY_SPREAD
    if not allow_explicit_override and prio_spread > max_spread:
        return (None, f"priority_spread:{prio_spread}>{max_spread}")

    # Domain (non-hub) file overlap analysis
    domain_a = {f for f in files_a if not is_hub_file(f)}
    domain_b = {f for f in files_b if not is_hub_file(f)}
    shared_domain = sorted(domain_a & domain_b)
    min_domain = min(len(domain_a), len(domain_b)) if (domain_a and domain_b) else 0
    subset_ratio = (len(shared_domain) / min_domain) if min_domain > 0 else 0.0

    comp_a = _item_component(item_a)
    comp_b = _item_component(item_b)
    same_comp = bool(comp_a and comp_a == comp_b and comp_a != "general")
    both_partial = bool(item_a.get("merged_repos")) and bool(item_b.get("merged_repos"))

    reasons: list[str] = []
    tier = 0
    if direct_dep and (len(shared_domain) >= 1 or same_comp):
        tier = 1
        reasons.append("direct_depends_on")
        if shared_domain:
            reasons.append(f"shared_domain_files:{len(shared_domain)}")
        if same_comp:
            reasons.append(f"same_component:{comp_a}")
    elif subset_ratio >= DEFAULT_SUBSET_RATIO_MIN and len(shared_domain) >= 2:
        tier = 1
        reasons.append(f"domain_file_subset:{int(round(subset_ratio * 100))}%")
        if same_comp:
            reasons.append(f"same_component:{comp_a}")
    elif same_comp and len(shared_domain) >= DEFAULT_MIN_SHARED_DOMAIN_FILES:
        tier = 2
        reasons.append(f"same_component:{comp_a}")
        reasons.append(f"shared_domain_files:{len(shared_domain)}")
    elif both_partial and same_comp and len(shared_domain) >= 1:
        tier = 2
        reasons.append("partial_wip_drain")
        reasons.append(f"same_component:{comp_a}")
        reasons.append(f"shared_domain_files:{len(shared_domain)}")
    elif allow_explicit_override and (len(shared_domain) >= 1 or same_comp):
        tier = 2
        reasons.append("explicit_ids_match")
        if shared_domain:
            reasons.append(f"shared_domain_files:{len(shared_domain)}")
    else:
        return (None, "insufficient_non_hub_cohesion")

    # Verify merging A & B does not create an indirect dependency cycle through a 3rd item
    # (If A -> X -> B and we collapse A+B into one node, X would become A -> X -> A!)
    sim_graph: dict[str, set[str]] = {k: set(v) for k, v in dep_graph.items()}
    absorber, absorbed = _select_same_repo_absorber(item_a, item_b, reverse_deps=reverse_deps)
    abs_cid = str(absorber.get("conversation_id") or "")
    sub_cid = str(absorbed.get("conversation_id") or "")
    # Merge sub_cid into abs_cid inside sim_graph
    merged_deps = (sim_graph.get(abs_cid, set()) | sim_graph.get(sub_cid, set())) - {abs_cid, sub_cid}
    sim_graph[abs_cid] = merged_deps
    sim_graph.pop(sub_cid, None)
    for node_id, deps in sim_graph.items():
        if sub_cid in deps:
            deps.discard(sub_cid)
            if node_id != abs_cid:
                deps.add(abs_cid)
    if _find_cycles(sim_graph):
        return (None, "would_create_dependency_cycle")

    partial_wip_reduced = 1 if (bool(item_a.get("merged_repos")) and bool(item_b.get("merged_repos"))) else 0
    if partial_wip_reduced and "partial_wip_drain" not in reasons:
        reasons.append("partial_wip_drain")

    rewired_deps = sorted((reverse_deps.get(sub_cid) or set()) - {abs_cid})
    repo_short = target_repo.split("/")[-1]
    plan: dict[str, Any] = {
        "plan_id": f"compact-t{tier}-{repo_short}-{abs_cid[:8]}-{sub_cid[:8]}",
        "tier": tier,
        "pattern": "same_remaining_repo",
        "target_repo": target_repo,
        "split_repo": "",
        "absorber_id": abs_cid,
        "absorbed_id": sub_cid,
        "absorber_title": str(absorber.get("title") or ""),
        "absorbed_title": str(absorbed.get("title") or ""),
        "absorber_priority": _item_priority(absorber),
        "absorbed_priority": _item_priority(absorbed),
        "resulting_priority": min(prio_a, prio_b),
        "component": comp_a if same_comp else (_item_component(absorber) or _item_component(absorbed)),
        "reasons": reasons,
        "shared_domain_files": shared_domain,
        "combined_target_files": combined_files,
        "combined_file_count": len(combined_files),
        "split_repo_files": [],
        "prs_saved": 1,
        "partial_wip_reduced": partial_wip_reduced,
        "estimated_savings_min": DEFAULT_COMPACT_SAVINGS_MIN,
        "rewired_dependents": rewired_deps,
    }
    return (plan, "")


def _evaluate_split_phase_pair(
    single_item: Mapping[str, Any],
    multi_item: Mapping[str, Any],
    *,
    target_repo: str,
    split_repo: str,
    single_files: list[str],
    multi_hot_files: list[str],
    multi_aux_files: list[str],
    dep_graph: Mapping[str, set[str]],
    max_combined_files: int,
    max_combined_fix_chars: int,
    allow_explicit_override: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """Evaluate Pattern 2 (`split_phase_multi_repo`): split `multi_item` into `split_repo` + absorb `target_repo` into `single_item`."""
    cid_s = str(single_item.get("conversation_id") or "")
    cid_m = str(multi_item.get("conversation_id") or "")

    # Check direct dependency link
    s_deps_m = cid_m in (dep_graph.get(cid_s) or set())
    m_deps_s = cid_s in (dep_graph.get(cid_m) or set())
    direct_dep = s_deps_m or m_deps_s

    # If multi_item depends on single_item, we can ONLY drop that edge when single_item has no
    # unmerged work in split_repo (i.e., single_item either never targeted split_repo or already merged split_repo).
    single_rem = get_remaining_repos(single_item)
    if m_deps_s and split_repo in single_rem:
        return (None, "split_repo_still_unmerged_in_absorber")

    combined_files = _dedupe_strs([*single_files, *multi_hot_files])
    if len(combined_files) > max_combined_files:
        return (None, f"exceeds_max_files:{len(combined_files)}>{max_combined_files}")

    combined_fix_chars = len(str(single_item.get("proposed_fix") or "")) + len(str(multi_item.get("proposed_fix") or ""))
    if combined_fix_chars > max_combined_fix_chars:
        return (None, f"exceeds_max_fix_chars:{combined_fix_chars}>{max_combined_fix_chars}")

    prio_s, prio_m = _item_priority(single_item), _item_priority(multi_item)
    prio_spread = abs(prio_s - prio_m)
    max_spread = DEFAULT_DEPENDS_PRIORITY_SPREAD if direct_dep else DEFAULT_MAX_PRIORITY_SPREAD
    if not allow_explicit_override and prio_spread > max_spread:
        return (None, f"priority_spread:{prio_spread}>{max_spread}")

    domain_s = {f for f in single_files if not is_hub_file(f)}
    domain_m = {f for f in multi_hot_files if not is_hub_file(f)}
    shared_domain = sorted(domain_s & domain_m)
    min_domain = min(len(domain_s), len(domain_m)) if (domain_s and domain_m) else 0
    subset_ratio = (len(shared_domain) / min_domain) if min_domain > 0 else 0.0

    comp_s = _item_component(single_item)
    comp_m = _item_component(multi_item)
    same_comp = bool(comp_s and comp_s == comp_m and comp_s != "general")

    reasons: list[str] = ["split_phase_multi_repo"]
    tier = 0
    if direct_dep and (len(shared_domain) >= 1 or same_comp):
        tier = 1
        reasons.append("direct_depends_on")
        if shared_domain:
            reasons.append(f"shared_domain_files:{len(shared_domain)}")
    elif subset_ratio >= DEFAULT_SUBSET_RATIO_MIN and len(shared_domain) >= 2:
        tier = 1
        reasons.append(f"domain_file_subset:{int(round(subset_ratio * 100))}%")
    elif same_comp and len(shared_domain) >= DEFAULT_MIN_SHARED_DOMAIN_FILES:
        tier = 2
        reasons.append(f"same_component:{comp_s}")
        reasons.append(f"shared_domain_files:{len(shared_domain)}")
    elif allow_explicit_override and (len(shared_domain) >= 1 or same_comp):
        tier = 2
        reasons.append("explicit_ids_match")
    else:
        return (None, "insufficient_non_hub_cohesion")

    # Simulate resulting graph: single_item adds depends_on multi_item; multi_item drops single_item
    sim_graph: dict[str, set[str]] = {k: set(v) for k, v in dep_graph.items()}
    sim_graph.setdefault(cid_m, set()).discard(cid_s)
    sim_graph.setdefault(cid_s, set()).add(cid_m)
    if _can_reach(sim_graph, cid_m, cid_s) or _find_cycles(sim_graph):
        return (None, "would_create_dependency_cycle")

    repo_short = target_repo.split("/")[-1]
    plan: dict[str, Any] = {
        "plan_id": f"compact-t{tier}-split-{repo_short}-{cid_s[:8]}-{cid_m[:8]}",
        "tier": tier,
        "pattern": "split_phase_multi_repo",
        "target_repo": target_repo,
        "split_repo": split_repo,
        "absorber_id": cid_s,
        "absorbed_id": cid_m,
        "absorber_title": str(single_item.get("title") or ""),
        "absorbed_title": str(multi_item.get("title") or ""),
        "absorber_priority": prio_s,
        "absorbed_priority": prio_m,
        "resulting_priority": min(prio_s, prio_m),
        "component": comp_s if same_comp else (comp_s or comp_m),
        "reasons": reasons,
        "shared_domain_files": shared_domain,
        "combined_target_files": combined_files,
        "combined_file_count": len(combined_files),
        "split_repo_files": list(multi_aux_files),
        "prs_saved": 1,
        "partial_wip_reduced": 0,
        "estimated_savings_min": DEFAULT_COMPACT_SAVINGS_MIN,
        "rewired_dependents": [],
    }
    return (plan, "")


def detect_pr_compaction_candidates(
    items: Sequence[Mapping[str, Any]],
    *,
    active_locks: Mapping[str, Any] | None = None,
    repo_dirs: Mapping[str, Path] | None = None,
    tier: str = "all",
    repo_filter: str = "",
    max_combined_files: int | None = None,
    max_combined_fix_chars: int | None = None,
    explicit_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Deterministically detect disjoint Tier 1 / Tier 2 PR compaction plans with zero LLM calls."""
    norm_tier = str(tier or "all").strip().lower()
    if norm_tier not in ("1", "all"):
        norm_tier = "all"
    norm_repo_filter = str(repo_filter or "").strip().lower()
    max_files = _resolve_max_combined_files(max_combined_files)
    max_fix_chars = _resolve_max_combined_fix_chars(max_combined_fix_chars)

    locked_cids = _collect_locked_conversation_ids(active_locks)
    owner_index = infer_repo_path_owners(items, repo_dirs=repo_dirs)
    dep_graph = _build_active_dep_graph(items)
    reverse_deps: dict[str, set[str]] = {}
    for cid, deps in dep_graph.items():
        for dep in deps:
            reverse_deps.setdefault(dep, set()).add(cid)

    explicit_set: set[str] = set()
    if explicit_ids:
        for raw_id in explicit_ids:
            tok = str(raw_id or "").strip().lower()
            if tok:
                explicit_set.add(tok)

    def _matches_explicit(cid: str) -> bool:
        if not explicit_set:
            return True
        cl = cid.lower()
        return any(cl == tok or cl.startswith(tok) for tok in explicit_set)

    # Classify eligible pending items
    single_by_repo: dict[str, list[tuple[Mapping[str, Any], list[str]]]] = {}
    multi_two_repo: list[tuple[Mapping[str, Any], list[str], dict[str, list[str]]]] = []
    excluded_items: list[dict[str, str]] = []

    for it in items:
        cid = str(it.get("conversation_id") or "").strip()
        if not cid:
            continue
        rem_repos = get_remaining_repos(it)
        if not rem_repos:
            continue
        if len(rem_repos) == 1:
            r_slug = rem_repos[0]
            rem_files = _remaining_files_for_single_repo(it, r_slug, owner_index=owner_index, repo_dirs=repo_dirs)
            excl, reason = is_compaction_excluded(it, remaining_files=rem_files, locked_cids=locked_cids)
            if excl:
                if str(it.get("status") or "").lower() == "pending":
                    excluded_items.append({"conversation_id": cid, "reason": reason})
                continue
            single_by_repo.setdefault(r_slug, []).append((it, rem_files))
        elif len(rem_repos) == 2 and not (it.get("merged_repos") or []):
            excl, reason = is_compaction_excluded(it, locked_cids=locked_cids)
            if excl:
                if str(it.get("status") or "").lower() == "pending":
                    excluded_items.append({"conversation_id": cid, "reason": reason})
                continue
            partitioned, ambiguous = partition_item_files_by_repo(
                it,
                rem_repos,
                owner_index=owner_index,
                repo_dirs=repo_dirs,
            )
            if ambiguous:
                excluded_items.append(
                    {"conversation_id": cid, "reason": f"ambiguous_split_files:{len(ambiguous)}"}
                )
                continue
            if all(len(partitioned.get(r) or []) >= 1 for r in rem_repos):
                multi_two_repo.append((it, rem_repos, partitioned))

    candidate_plans: list[dict[str, Any]] = []
    allow_explicit = bool(explicit_set)

    # Pattern 1: Same-Remaining-Repo pairs
    for repo_slug, repo_entries in sorted(single_by_repo.items()):
        if norm_repo_filter and norm_repo_filter not in repo_slug.lower():
            continue
        for i in range(len(repo_entries)):
            it_a, files_a = repo_entries[i]
            cid_a = str(it_a.get("conversation_id") or "")
            for j in range(i + 1, len(repo_entries)):
                it_b, files_b = repo_entries[j]
                cid_b = str(it_b.get("conversation_id") or "")
                if explicit_set and not (_matches_explicit(cid_a) and _matches_explicit(cid_b)):
                    continue
                plan, _ = _evaluate_same_repo_pair(
                    it_a,
                    it_b,
                    target_repo=repo_slug,
                    files_a=files_a,
                    files_b=files_b,
                    dep_graph=dep_graph,
                    reverse_deps=reverse_deps,
                    max_combined_files=max_files,
                    max_combined_fix_chars=max_fix_chars,
                    allow_explicit_override=allow_explicit,
                )
                if plan is not None:
                    candidate_plans.append(plan)

    # Pattern 2: Split-Phase Multi-Repo pairs
    for multi_it, rem_repos, partitioned in multi_two_repo:
        cid_m = str(multi_it.get("conversation_id") or "")
        for idx_hot, hot_repo in enumerate(rem_repos):
            aux_repo = rem_repos[1 - idx_hot]
            if norm_repo_filter and norm_repo_filter not in hot_repo.lower():
                continue
            hot_entries = single_by_repo.get(hot_repo) or []
            for single_it, single_files in hot_entries:
                cid_s = str(single_it.get("conversation_id") or "")
                if explicit_set and not (_matches_explicit(cid_s) and _matches_explicit(cid_m)):
                    continue
                plan, _ = _evaluate_split_phase_pair(
                    single_it,
                    multi_it,
                    target_repo=hot_repo,
                    split_repo=aux_repo,
                    single_files=single_files,
                    multi_hot_files=partitioned.get(hot_repo) or [],
                    multi_aux_files=partitioned.get(aux_repo) or [],
                    dep_graph=dep_graph,
                    max_combined_files=max_files,
                    max_combined_fix_chars=max_fix_chars,
                    allow_explicit_override=allow_explicit,
                )
                if plan is not None:
                    candidate_plans.append(plan)

    # Filter by requested tier
    if norm_tier == "1":
        candidate_plans = [p for p in candidate_plans if int(p.get("tier") or 2) == 1]

    # Rank deterministically: Tier 1 first -> partial-WIP drain -> direct_depends_on -> shared domain count -> priority
    def _plan_sort_key(p: Mapping[str, Any]) -> tuple[int, int, int, int, int, str]:
        reasons = set(p.get("reasons") or [])
        return (
            int(p.get("tier") or 2),
            0 if "direct_depends_on" in reasons else 1,
            -int(p.get("partial_wip_reduced") or 0),
            -len(p.get("shared_domain_files") or []),
            int(p.get("resulting_priority") or 50),
            str(p.get("plan_id") or ""),
        )

    candidate_plans.sort(key=_plan_sort_key)

    # Greedy disjoint selection (each item participates in at most one compaction per pass)
    selected_plans: list[dict[str, Any]] = []
    used_cids: set[str] = set()
    for p in candidate_plans:
        a_id = str(p.get("absorber_id") or "")
        b_id = str(p.get("absorbed_id") or "")
        if a_id in used_cids or b_id in used_cids:
            continue
        selected_plans.append(p)
        used_cids.add(a_id)
        used_cids.add(b_id)

    tier1_count = sum(1 for p in selected_plans if int(p.get("tier") or 2) == 1)
    tier2_count = sum(1 for p in selected_plans if int(p.get("tier") or 2) == 2)
    total_prs_saved = sum(int(p.get("prs_saved") or 1) for p in selected_plans)
    total_partial_wip_reduced = sum(int(p.get("partial_wip_reduced") or 0) for p in selected_plans)
    total_savings_min = sum(int(p.get("estimated_savings_min") or DEFAULT_COMPACT_SAVINGS_MIN) for p in selected_plans)

    return {
        "status": "ok",
        "tier": norm_tier,
        "plan_count": len(selected_plans),
        "tier1_count": tier1_count,
        "tier2_count": tier2_count,
        "total_prs_saved": total_prs_saved,
        "total_partial_wip_reduced": total_partial_wip_reduced,
        "estimated_savings_min": total_savings_min,
        "max_combined_files": max_files,
        "plans": selected_plans,
        "excluded_count": len(excluded_items),
        "excluded_items": excluded_items,
    }


def _filter_anchors_for_repo(
    anchors: Sequence[str],
    *,
    keep_repo: str,
    drop_repo: str,
    keep_files: Sequence[str],
    short_to_slug: Mapping[str, str],
) -> list[str]:
    """Filter `section_anchors` belonging to `keep_repo` cleanly during Split-Phase compaction."""
    keep_set = {_normalize_path(f) for f in keep_files}
    out: list[str] = []
    for raw in anchors:
        anc = str(raw or "").strip()
        if not anc:
            continue
        r_slug, fpath = _extract_anchor_repo_hint(anc, short_to_slug)
        if r_slug == keep_repo:
            out.append(anc)
        elif r_slug == drop_repo:
            continue
        else:
            # Unprefixed anchor: check if leading path matches keep_set
            lead = _normalize_path(anc.split("#", 1)[0].split(":", 1)[0])
            if lead in keep_set:
                out.append(anc)
    return _dedupe_strs(out)


def _apply_plan_in_memory(
    items: list[dict[str, Any]],
    plan: Mapping[str, Any],
    *,
    owner_index: Mapping[str, Any],
    now_iso: str,
) -> tuple[bool, str]:
    """Mutate `items` in memory according to a single validated `plan`; return `(applied, reason)`."""
    by_id = {str(it.get("conversation_id") or ""): it for it in items if it.get("conversation_id")}
    abs_id = str(plan.get("absorber_id") or "")
    sub_id = str(plan.get("absorbed_id") or "")
    absorber = by_id.get(abs_id)
    absorbed = by_id.get(sub_id)
    if absorber is None or absorbed is None:
        return (False, "item_missing")
    if str(absorber.get("status") or "").lower() != "pending" or str(absorbed.get("status") or "").lower() != "pending":
        return (False, "status_changed_in_flight")

    pattern = str(plan.get("pattern") or "same_remaining_repo")
    target_repo = str(plan.get("target_repo") or "")
    tier = int(plan.get("tier") or 2)
    plan_id = str(plan.get("plan_id") or "")
    short_to_slug = owner_index.get("short_to_slug") or {}

    if pattern == "same_remaining_repo":
        abs_title = str(absorber.get("title") or abs_id[:8]).strip()
        sub_title = str(absorbed.get("title") or sub_id[:8]).strip()
        if sub_id[:8] not in abs_title and sub_title[:30].lower() not in abs_title.lower():
            absorber["title"] = f"{abs_title} + {sub_title}"[:220]

        absorber["priority"] = min(_item_priority(absorber), _item_priority(absorbed))
        merged_imp = _highest_importance(absorber.get("importance"), absorbed.get("importance"))
        if merged_imp:
            absorber["importance"] = merged_imp
        if not absorber.get("component") and absorbed.get("component"):
            absorber["component"] = absorbed["component"]

        merged_deps = _dedupe_strs(
            [
                *(absorber.get("depends_on") or []),
                *(absorbed.get("depends_on") or []),
            ]
        )
        absorber["depends_on"] = [d for d in merged_deps if d not in (abs_id, sub_id)]

        absorber["rca_summary"] = (
            f"[Part 1/2 {abs_id[:8]}] {str(absorber.get('rca_summary') or '').strip()}\n"
            f"[Part 2/2 {sub_id[:8]}] {str(absorbed.get('rca_summary') or '').strip()}"
        ).strip()
        absorber["proposed_fix"] = (
            f"Auto-Compacted ({plan_id}, Tier {tier}, {target_repo}):\n\n"
            f"[Part 1/2 — {abs_id[:8]}]: {str(absorber.get('proposed_fix') or '').strip()}\n\n"
            f"[Part 2/2 — {sub_id[:8]}]: {str(absorbed.get('proposed_fix') or '').strip()}"
        ).strip()
        absorber["target_files"] = list(plan.get("combined_target_files") or [])
        combined_anchors = _dedupe_strs(
            [*(absorber.get("section_anchors") or []), *(absorbed.get("section_anchors") or [])]
        )
        if combined_anchors:
            absorber["section_anchors"] = combined_anchors
        absorber["notes"] = (
            f"{str(absorber.get('notes') or '').strip()}\n\n"
            f"Auto-Compacted ({now_iso}, {plan_id}): absorbed {sub_id} into {abs_id} on {target_repo}.\n"
            f"[{sub_id[:8]} Notes]: {str(absorbed.get('notes') or '').strip()}"
        ).strip()
        absorber["updated_at"] = now_iso

        # Cancel absorbed item with explicit superseded marker
        absorbed["status"] = "cancelled"
        absorbed["notes"] = (
            f"Superseded / consolidated into {abs_id} on {now_iso} ({plan_id}).\n"
            f"{str(absorbed.get('notes') or '').strip()}"
        ).strip()
        absorbed["updated_at"] = now_iso

        # Rewire EVERY downstream active item pointing at `sub_id` -> `abs_id`
        for it in items:
            it_cid = str(it.get("conversation_id") or "")
            if it_cid in (abs_id, sub_id):
                continue
            if str(it.get("status") or "").lower() not in ACTIVE_QUEUE_STATUSES:
                continue
            deps = list(it.get("depends_on") or [])
            if sub_id in deps:
                it["depends_on"] = _dedupe_strs([abs_id if d == sub_id else d for d in deps if d != it_cid])
                it["updated_at"] = now_iso
        return (True, "applied")

    if pattern == "split_phase_multi_repo":
        split_repo = str(plan.get("split_repo") or "")
        split_files = list(plan.get("split_repo_files") or [])
        combined_files = list(plan.get("combined_target_files") or [])
        hot_files = [f for f in combined_files if f not in set(split_files)]

        orig_sub_title = str(absorbed.get("title") or sub_id[:8]).strip()
        orig_sub_fix = str(absorbed.get("proposed_fix") or "").strip()
        orig_sub_notes = str(absorbed.get("notes") or "").strip()
        orig_sub_anchors = list(absorbed.get("section_anchors") or [])

        # 1. Scope `absorbed` strictly to `split_repo` (runs independently in parallel!)
        split_short = split_repo.split("/")[-1]
        hot_short = target_repo.split("/")[-1]
        absorbed["title"] = f"{orig_sub_title} (Phase 1 {split_short}; {hot_short} moved to {abs_id[:8]})"[:220]
        absorbed["priority"] = min(_item_priority(absorbed), _item_priority(absorber))
        merged_imp = _highest_importance(absorber.get("importance"), absorbed.get("importance"))
        if merged_imp:
            absorbed["importance"] = merged_imp
        absorbed["repo_slugs"] = [split_repo]
        absorbed["depends_on"] = [d for d in (absorbed.get("depends_on") or []) if d != abs_id]
        absorbed["target_files"] = split_files
        aux_anchors = _filter_anchors_for_repo(
            orig_sub_anchors,
            keep_repo=split_repo,
            drop_repo=target_repo,
            keep_files=split_files,
            short_to_slug=short_to_slug,
        )
        if aux_anchors:
            absorbed["section_anchors"] = aux_anchors
        else:
            absorbed.pop("section_anchors", None)
        absorbed["proposed_fix"] = (
            f"Phase 1 ONLY ({split_repo}; {target_repo} phase consolidated into {abs_id} via {plan_id}):\n"
            f"{orig_sub_fix}"
        ).strip()
        absorbed["notes"] = (
            f"Auto-Compacted Split-Phase ({now_iso}, {plan_id}): scoped strictly to {split_repo} ({len(split_files)} files); "
            f"{target_repo} phase consolidated into {abs_id}.\n{orig_sub_notes}"
        ).strip()
        absorbed["updated_at"] = now_iso

        # 2. Upgrade `absorber` to absorb `absorbed`'s `target_repo` phase & depend on `absorbed`
        abs_title = str(absorber.get("title") or abs_id[:8]).strip()
        if sub_id[:8] not in abs_title:
            absorber["title"] = f"{abs_title} + {hot_short} phase from {sub_id[:8]}"[:220]
        absorber["priority"] = min(_item_priority(absorber), _item_priority(absorbed))
        if merged_imp:
            absorber["importance"] = merged_imp
        absorber["depends_on"] = _dedupe_strs([*(absorber.get("depends_on") or []), sub_id])
        absorber["rca_summary"] = (
            f"[Part 1/2 {abs_id[:8]}] {str(absorber.get('rca_summary') or '').strip()}\n"
            f"[Part 2/2 {sub_id[:8]} ({hot_short})] {str(absorbed.get('rca_summary') or '').strip()}"
        ).strip()
        absorber["proposed_fix"] = (
            f"Auto-Compacted Split-Phase ({plan_id}, Tier {tier}, {target_repo}; depends on {sub_id} {split_repo} phase):\n\n"
            f"[Part 1/2 — {abs_id[:8]}]: {str(absorber.get('proposed_fix') or '').strip()}\n\n"
            f"[Part 2/2 — {sub_id[:8]} ({target_repo} phase ONLY)]: {orig_sub_fix}"
        ).strip()
        absorber["target_files"] = combined_files
        hot_anchors = _filter_anchors_for_repo(
            orig_sub_anchors,
            keep_repo=target_repo,
            drop_repo=split_repo,
            keep_files=hot_files,
            short_to_slug=short_to_slug,
        )
        merged_anchors = _dedupe_strs([*(absorber.get("section_anchors") or []), *hot_anchors])
        if merged_anchors:
            absorber["section_anchors"] = merged_anchors
        absorber["notes"] = (
            f"{str(absorber.get('notes') or '').strip()}\n\n"
            f"Auto-Compacted Split-Phase ({now_iso}, {plan_id}): absorbed {target_repo} phase of {sub_id}.\n"
            f"[{sub_id[:8]} Notes]: {orig_sub_notes}"
        ).strip()
        absorber["updated_at"] = now_iso
        return (True, "applied")

    return (False, f"unsupported_pattern:{pattern}")


def _best_effort_refresh_board(state_dir: Path) -> str:
    """Refresh registered `dev_queue_board.md` best-effort after queue compaction."""
    ptr = state_dir / "dev_queue_board_path.txt"
    if not ptr.is_file():
        return ""
    board_str = ptr.read_text(encoding="utf-8", errors="ignore").strip()
    if not board_str:
        return ""
    try:
        tui_dir = Path(__file__).resolve().parents[2] / "agy-dev-queue-orchestrator" / "scripts"
        if tui_dir.is_dir() and str(tui_dir) not in sys.path:
            sys.path.insert(0, str(tui_dir))
        import dev_queue_tui  # type: ignore[import-not-found]

        ns = dev_queue_tui._build_parser().parse_args(
            ["--state-dir", str(state_dir), "--write-board", board_str]
        )
        dev_queue_tui.render(ns)
        return board_str
    except Exception:
        return ""


def compact_dev_queue(
    *,
    portfolio_dir: Path | str | None = None,
    apply: bool = False,
    tier: str = "all",
    repo_slug: str = "",
    item_ids: Sequence[str] | None = None,
    repos_dir: Path | str | None = None,
    max_combined_files: int | None = None,
    max_combined_fix_chars: int | None = None,
    orchestrator_id: str = "",
    respect_pause: bool = False,
    refresh_board: bool = True,
) -> dict[str, Any]:
    """Preview (`apply=False`) or atomically execute (`apply=True`) deterministic PR compaction."""
    state_dir = dev_common.resolve_state_dir(portfolio_dir=portfolio_dir)
    q_path = queue_store.get_dev_queue_path(portfolio_dir=state_dir)

    if respect_pause and orchestration_control.is_orchestration_paused(state_dir=state_dir):
        return {
            "status": "ok",
            "mode": "skipped_paused",
            "paused": True,
            "applied_count": 0,
            "total_prs_saved": 0,
            "plans": [],
            "applied_plans": [],
            "skipped_plans": [],
            "queue_path": str(q_path),
        }

    # Resolve optional local repo dirs for unambiguous Split-Phase disk detection
    repo_dirs: dict[str, Path] = {}
    if repos_dir:
        base = Path(str(repos_dir)).expanduser()
        if base.is_dir():
            for child in sorted(base.iterdir()):
                if child.is_dir() and (child / ".git").exists():
                    repo_dirs[child.name] = child

    lock_path = q_path.with_suffix(".json.lock")
    now_iso = _utc_now_iso()
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    with dev_common.file_lock(lock_path):
        data = queue_store._read_dev_queue_locked(q_path)
        items_orig = [dict(it) for it in (data.get("items") or []) if isinstance(it, dict)]
        active_locks = lock_store.get_all_dev_pr_locks(portfolio_dir=state_dir)

        # Also map slugs to local dirs from active lock metadata when available
        for slug, ldata in active_locks.items():
            if not isinstance(ldata, Mapping):
                continue
            raw_dir = str(ldata.get("target_repo_dir") or "").replace("$HOME", str(Path.home())).strip()
            if raw_dir:
                p_dir = Path(raw_dir).expanduser()
                if p_dir.is_dir():
                    repo_dirs[dev_common.normalize_repo_slug(slug)] = p_dir

        detection = detect_pr_compaction_candidates(
            items_orig,
            active_locks=active_locks,
            repo_dirs=repo_dirs or None,
            tier=tier,
            repo_filter=repo_slug,
            max_combined_files=max_combined_files,
            max_combined_fix_chars=max_combined_fix_chars,
            explicit_ids=item_ids,
        )
        plans: list[dict[str, Any]] = list(detection.get("plans") or [])

        if not apply:
            return {
                "status": "ok",
                "mode": "dry_run",
                "paused": False,
                "queue_path": str(q_path),
                "applied_count": 0,
                "applied_plans": [],
                "skipped_plans": [],
                "apply_command": (
                    f"pr_remediator.py queue-compact --apply --tier {detection['tier']}"
                    + (f" --repo {repo_slug}" if repo_slug else "")
                ),
                **detection,
            }

        if not plans:
            return {
                "status": "ok",
                "mode": "apply_noop",
                "paused": False,
                "queue_path": str(q_path),
                "applied_count": 0,
                "applied_plans": [],
                "skipped_plans": [],
                **detection,
            }

        # Deep-copy items so any invariant failure aborts cleanly without partial writes
        working_items = [
            {
                k: (list(v) if isinstance(v, list) else (dict(v) if isinstance(v, dict) else v))
                for k, v in it.items()
            }
            for it in items_orig
        ]
        owner_index = infer_repo_path_owners(working_items, repo_dirs=repo_dirs or None)
        locked_cids = _collect_locked_conversation_ids(active_locks)

        applied_plans: list[dict[str, Any]] = []
        skipped_plans: list[dict[str, Any]] = []

        for plan in plans:
            abs_id = str(plan.get("absorber_id") or "")
            sub_id = str(plan.get("absorbed_id") or "")
            if _cid_is_locked(abs_id, locked_cids) or _cid_is_locked(sub_id, locked_cids):
                skipped_plans.append({"plan_id": plan["plan_id"], "reason": "active_lock_in_flight"})
                continue
            ok, reason = _apply_plan_in_memory(working_items, plan, owner_index=owner_index, now_iso=now_iso)
            if ok:
                applied_plans.append(plan)
            else:
                skipped_plans.append({"plan_id": plan["plan_id"], "reason": reason})

        if not applied_plans:
            return {
                "status": "ok",
                "mode": "apply_noop",
                "paused": False,
                "queue_path": str(q_path),
                "applied_count": 0,
                "applied_plans": [],
                "skipped_plans": skipped_plans,
                **detection,
            }

        # Hard pre-write invariant check: 0 cycles, 0 deadlocks, 0 live-item mutations
        invariant_issues = validate_queue_invariants(
            working_items,
            active_locks=active_locks,
            before_items=items_orig,
        )
        if invariant_issues:
            return {
                "status": "error",
                "mode": "aborted_invariant_violation",
                "error": f"Compaction aborted by pre-write invariant guard: {', '.join(invariant_issues)}",
                "invariant_issues": invariant_issues,
                "applied_count": 0,
                "applied_plans": [],
                "skipped_plans": skipped_plans,
                "queue_path": str(q_path),
            }

        # Create 0600 backup before writing
        backup_path = ""
        if q_path.is_file():
            bak = q_path.with_name(f"dev_queue.json.bak-{ts_tag}-pre-compact")
            shutil.copy2(q_path, bak)
            os.chmod(bak, 0o600)
            backup_path = str(bak)

        if orchestrator_id.strip():
            data["orchestrator_conversation_id"] = orchestrator_id.strip()
        data["items"] = queue_store._sort_dev_queue_items(working_items)
        data["updated_at"] = now_iso
        queue_store._atomic_write_json_0600(q_path, data)

    board_refreshed = _best_effort_refresh_board(state_dir) if refresh_board else ""
    prs_saved = sum(int(p.get("prs_saved") or 1) for p in applied_plans)
    pwip_reduced = sum(int(p.get("partial_wip_reduced") or 0) for p in applied_plans)

    return {
        "status": "ok",
        "mode": "applied",
        "paused": False,
        "queue_path": str(q_path),
        "backup_path": backup_path,
        "board_refreshed": board_refreshed,
        "tier": detection["tier"],
        "plan_count": len(plans),
        "applied_count": len(applied_plans),
        "total_prs_saved": prs_saved,
        "total_partial_wip_reduced": pwip_reduced,
        "estimated_savings_min": prs_saved * DEFAULT_COMPACT_SAVINGS_MIN,
        "plans": plans,
        "applied_plans": applied_plans,
        "skipped_plans": skipped_plans,
        "summary": queue_store.get_dev_queue_summary(data["items"]),
    }


def build_compaction_optimizer_finding(detection: Mapping[str, Any]) -> dict[str, Any] | None:
    """Convert a non-empty `detect_pr_compaction_candidates()` result into a `queue-optimize` finding."""
    plans = list(detection.get("plans") or [])
    if not plans:
        return None
    total_saved = int(detection.get("total_prs_saved") or len(plans))
    t1 = int(detection.get("tier1_count") or 0)
    t2 = int(detection.get("tier2_count") or 0)
    pwip = int(detection.get("total_partial_wip_reduced") or 0)
    savings = int(detection.get("estimated_savings_min") or (total_saved * DEFAULT_COMPACT_SAVINGS_MIN))
    severity = "high" if (t1 >= 1 or total_saved >= 2) else "medium"
    repos = _dedupe_strs([str(p.get("target_repo") or "") for p in plans if p.get("target_repo")])
    repo_lbl = repos[0].split("/")[-1] if len(repos) == 1 else f"{len(repos)} repos"

    return {
        "code": "pr_compaction_candidates",
        "severity": severity,
        "category": "throughput",
        "title": (
            f"Compact {len(plans)} PR group(s) on {repo_lbl} "
            f"(-{total_saved} PRs · T1:{t1} T2:{t2} · ~{savings}m saved)"
        ),
        "evidence": {
            "plan_count": len(plans),
            "tier1_count": t1,
            "tier2_count": t2,
            "total_prs_saved": total_saved,
            "total_partial_wip_reduced": pwip,
            "repos": repos,
            "plans": [
                {
                    "plan_id": p["plan_id"],
                    "tier": p["tier"],
                    "pattern": p["pattern"],
                    "target_repo": p["target_repo"],
                    "split_repo": p.get("split_repo", ""),
                    "absorber_id": p["absorber_id"],
                    "absorbed_id": p["absorbed_id"],
                    "combined_file_count": p["combined_file_count"],
                    "reasons": p["reasons"],
                }
                for p in plans
            ],
        },
        "recommendation": (
            "Run deterministic `pr_remediator.py queue-compact --apply --tier all --respect-pause` "
            "(or `agy-queue compact --apply`) to eliminate redundant PR cycles, rebase churn, and partial-WIP bloat safely."
        ),
        "auto_action_hint": "pr_remediator.py queue-compact --apply --tier all --respect-pause",
        "estimated_savings_min": savings,
    }


def render_compaction_report(result: Mapping[str, Any], *, color: bool = False) -> str:
    """Render concise human-readable terminal output for `agy-queue compact`."""
    def _c(text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text

    mode = str(result.get("mode") or "dry_run")
    plans = list(result.get("applied_plans") if mode == "applied" else (result.get("plans") or []))
    saved = int(result.get("total_prs_saved") or 0)
    est_min = int(result.get("estimated_savings_min") or 0)
    bar = "═" * 78

    header_lbl = (
        f"AGY-QUEUE COMPACT [{mode.upper()}]  groups={len(plans)}  PRs saved=-{saved} (~{est_min}m)"
    )
    lines = [_c(bar, "2;36"), _c(header_lbl, "1;36"), _c("─" * 78, "2")]

    if mode == "skipped_paused":
        lines.append(_c("  ⏸ Skipped: orchestration is PAUSED (--respect-pause active).", "1;33"))
    elif not plans:
        lines.append(_c("  ✓ No safe PR compaction candidates found under current guardrails.", "0;32"))
    else:
        for idx, p in enumerate(plans, 1):
            t_badge = _c(f"[T{p.get('tier', 2)}]", "1;32" if p.get("tier") == 1 else "1;33")
            pat = str(p.get("pattern") or "")
            r_short = str(p.get("target_repo") or "").split("/")[-1]
            a8 = str(p.get("absorber_id") or "")[:8]
            b8 = str(p.get("absorbed_id") or "")[:8]
            f_cnt = int(p.get("combined_file_count") or 0)
            reasons_s = ", ".join(p.get("reasons") or [])
            if pat == "split_phase_multi_repo":
                s_short = str(p.get("split_repo") or "").split("/")[-1]
                action_s = f"{a8} absorbs {r_short} phase of {b8} ({b8} -> {s_short}-only)"
            else:
                action_s = f"{a8} absorbs {b8} on {r_short} ({b8} -> cancelled)"
            lines.append(f"  {idx}. {t_badge} {_c(action_s, '1')}  [{f_cnt} files · P{p.get('resulting_priority')}]")
            lines.append(f"     Reasons: {reasons_s}")
        if mode == "dry_run":
            lines.append(_c("─" * 78, "2"))
            lines.append(_c("  → Apply atomically: agy-queue compact --apply", "0;36"))
        elif mode == "applied" and result.get("backup_path"):
            lines.append(_c("─" * 78, "2"))
            lines.append(f"  ✓ Applied {len(plans)} compaction(s) · backup: {result['backup_path']}")
    lines.append(_c(bar, "2;36"))
    return "\n".join(lines)
