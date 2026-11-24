"""State loading, repository discovery, Git branch probing, and in-process sync (`tui_state.py`)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

from tui_models import (
    _INSTALLED_PLUGIN_ROOT,
    _SCRIPT_PLUGIN_ROOT,
    DEFAULT_MAX_REPO_SLOTS,
    DEFAULT_NAV_INTENT_ACK_WAIT_SECONDS,
    DEFAULT_NAV_INTENT_TTL_SECONDS,
    INACTIVE_STATUSES,
    MAX_REPO_SLOTS_CAP,
    NAV_INTENT_FILENAME,
    NAV_INTENT_LOCK_FILENAME,
    _UUID_RE,
    _dev_common,
    _facade_attr,
    _flow_opt,
    _get_git_client,
    _get_lock_store,
    _get_orch_control,
    _get_queue_store,
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


def _resolve_state_dir(override: str = "") -> Path:
    """Resolve persistent local state directory dynamically via `dev_common.resolve_state_dir()`."""
    if override and str(override).strip():
        return Path(str(override)).expanduser().resolve()
    if _dev_common is not None and hasattr(_dev_common, "resolve_state_dir"):
        try:
            return Path(_dev_common.resolve_state_dir()).expanduser().resolve()
        except (OSError, ValueError, TypeError):
            pass
    env_state = str(os.environ.get("AGY_DEV_STATE_DIR") or "").strip()
    if env_state:
        return Path(env_state).expanduser().resolve()
    return (Path.home() / ".local" / "share" / "agy-dev-plugin").resolve()


def _load_json(path: Path, *, strict: bool = False) -> dict[str, Any]:
    """Read a JSON object from `path`, delegating to `dev_common.load_json_dict`."""
    if _dev_common is not None and hasattr(_dev_common, "load_json_dict"):
        return dict(_dev_common.load_json_dict(path, strict=strict))
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def _is_uuid(val: str) -> bool:
    """Return True when `val` matches UUID format."""
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
        except OSError:
            break
    return titles


def _is_lock_base_stale(lk: dict[str, Any]) -> bool:
    """Return True when a lock or slot has `base_stale=True`, `needs_base_rebase=True`, or rebase-required status."""
    if _dev_common is not None and hasattr(_dev_common, "is_lock_base_stale"):
        return bool(_dev_common.is_lock_base_stale(lk))
    if bool(lk.get("base_stale")) or bool(lk.get("needs_base_rebase")):
        return True
    status_u = str(lk.get("status") or "").strip().upper()
    return status_u in {
        "NEEDS_REBASE_REVERIFY",
        "NEEDS_AGENT_SEMANTIC_FIX",
        "NEEDS_AGENT_CONFLICT_RESOLUTION",
    }


def _iter_active_lock_slots(lk: dict[str, Any]) -> list[dict[str, Any]]:
    """Return ordered active slot dictionaries (`slot_id` 1..N) for a repository lock."""
    if _dev_common is not None and hasattr(_dev_common, "iter_active_lock_slots"):
        return list(_dev_common.iter_active_lock_slots(lk))
    return []


def _resolve_max_repo_slots(explicit_max_slots: Any = None) -> int:
    """Resolve per-repository slot capacity clamped to `[1..MAX_REPO_SLOTS_CAP]` (default `DEFAULT_MAX_REPO_SLOTS`)."""
    if _dev_common is not None and hasattr(_dev_common, "resolve_max_repo_slots"):
        with contextlib.suppress(TypeError, ValueError, AttributeError):
            return int(_dev_common.resolve_max_repo_slots(explicit_max_slots))
    for raw in (explicit_max_slots, str(os.environ.get("AGY_DEV_MAX_SLOTS_PER_REPO") or "").strip()):
        if raw is None or raw == "" or isinstance(raw, bool):
            continue
        try:
            val = int(raw)
        except (TypeError, ValueError):
            continue
        if val >= 1:
            return min(val, MAX_REPO_SLOTS_CAP)
    return DEFAULT_MAX_REPO_SLOTS


def _slot_label(slot_id: Any) -> str:
    """Return the compact slot label (`S1`..`S4`) for a slot identifier."""
    try:
        return f"S{int(slot_id)}"
    except (TypeError, ValueError):
        return "S1"


def _lock_slot_occupancy(lk: dict[str, Any]) -> str:
    """Return `active/max` slot occupancy for a repository lock (default capacity `DEFAULT_MAX_REPO_SLOTS`)."""
    if not isinstance(lk, dict):
        return f"0/{DEFAULT_MAX_REPO_SLOTS}"
    occ = str(lk.get("slot_occupancy") or "").strip()
    if occ:
        return occ
    try:
        active_n = int(lk.get("active_slots_count") or len(_iter_active_lock_slots(lk)) or 1)
    except (TypeError, ValueError):
        active_n = 1
    max_n = max(_resolve_max_repo_slots(lk.get("max_slots")), active_n)
    return f"{active_n}/{max_n}"


def _secondary_slot_progress(slug: str, lock_progress_cache: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """Return `(slot_id, progress)` pairs for Slots #2..#N of `slug` from a slot-keyed progress cache, sorted by slot."""
    prefix = f"{slug}#slot"
    out: list[tuple[int, dict[str, Any]]] = []
    for p_key, lp_val in lock_progress_cache.items():
        if not isinstance(p_key, str) or not p_key.startswith(prefix) or not isinstance(lp_val, dict):
            continue
        try:
            s_num = int(p_key[len(prefix) :])
        except ValueError:
            continue
        if s_num >= 2:
            out.append((s_num, lp_val))
    out.sort(key=lambda pair: pair[0])
    return out


def _normalize_repo_lock_slots(slug: str, info: dict[str, Any]) -> dict[str, Any]:
    """Normalize an active repository lock with N-slot occupancy (`1/3`, `3/4`) and `base_stale` flags."""
    if _dev_common is not None and hasattr(_dev_common, "normalize_repo_lock_slots"):
        return dict(_dev_common.normalize_repo_lock_slots(slug, info))
    return dict(info or {})


def _load_orchestration_phase(state_dir: Path, locks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Load Pause / Draining / Active orchestration phase from `orchestration_control` safely."""
    oc = _get_orch_control()
    if oc is not None:
        with contextlib.suppress(OSError, ValueError, TypeError, RuntimeError, AttributeError):
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


def _load_locks(
    state_dir: Path,
    *,
    strict: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load active and historical per-repository `dev_pr_locks` (including `slots`) from state files."""
    if _dev_common is not None and hasattr(_dev_common, "load_dev_locks_from_dir"):
        return _dev_common.load_dev_locks_from_dir(state_dir, strict=strict, normalize_slots=True)
    return ({}, {})


def _cids_match(cid_a: str, cid_b: str) -> bool:
    """Return True when two conversation IDs match exactly or share an 8-char / >=8-char prefix."""
    if _dev_common is not None and hasattr(_dev_common, "cids_match"):
        return bool(_dev_common.cids_match(cid_a, cid_b))
    a = str(cid_a or "").strip().lower()
    b = str(cid_b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 8 and len(b) >= 8 and a[:8] == b[:8]:
        return a.startswith(b) or b.startswith(a)
    return False


def is_lock_human_ready(lk: dict[str, Any]) -> bool:
    """Return True iff a Slot #1 lock has `base_stale == False`, `pr_gate_ready == True`, and passed review."""
    from tui_models import HUMAN_READY_LOCK_STATUSES, UNREADY_PR_GATE_LOCK_STATUSES

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
        except (ValueError, TypeError, AttributeError):
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
        except (OSError, subprocess.SubprocessError):
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
    discover_fn = _facade_attr("_discover_repos", _discover_repos)
    if repo_cache is None:
        return discover_fn(all_locks, items, repos_dir_override)
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
    fresh = discover_fn(all_locks, items, repos_dir_override)
    repo_cache["key"] = key
    repo_cache["ts"] = now_mono
    repo_cache["ttl"] = eff_ttl
    repo_cache["result"] = fresh
    return fresh


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
        state_dir / "dev_orchestration_control.json",
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
        with contextlib.suppress(TypeError, ValueError, AttributeError):
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
        with contextlib.suppress(TypeError, ValueError, AttributeError):
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
    except (OSError, subprocess.SubprocessError):
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
            if _facade_attr("_check_local_remote_branch_state", _check_local_remote_branch_state)(
                repo_slug, br, repo_local_dirs
            ) == "open":
                return br
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _extract_reconciled_count(payload: str | dict[str, Any]) -> int:
    """Parse `reconciled_count` / `reconciled_items` from `pr_remediator` JSON or in-process dict."""
    if isinstance(payload, dict):
        parsed: Any = payload
    else:
        raw = (payload or "").strip()
        if not raw:
            return 0
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return 0
    if not isinstance(parsed, dict):
        return 0
    for obj in (parsed, parsed.get("queue_reconciliation")):
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
    """Synchronize remote refs without duplicate fetches and reconcile in-process via `lock_store` & `queue_store`."""
    is_offline = bool(offline) if offline is not None else (os.environ.get("AGY_OFFLINE_ONLY") == "1")

    fetched = 0
    reconciled = 0
    ls_mod = _get_lock_store()
    qs_mod = _get_queue_store()
    active_locks, _ = _facade_attr("_load_locks", _load_locks)(state_dir)
    q_items = _facade_attr("_load_json", _load_json)(state_dir / "dev_queue.json").get("items") or []
    locked_slugs = set(active_locks.keys())
    unlocked_pr_slugs = _collect_unlocked_pr_repo_slugs(q_items, locked_slugs)

    if is_offline:
        if qs_mod is not None and hasattr(qs_mod, "reconcile_dev_queue_with_git"):
            try:
                res_q = qs_mod.reconcile_dev_queue_with_git(portfolio_dir=state_dir, fetch=False)
                if isinstance(res_q, dict) and res_q.get("status") in ("ok", "skipped"):
                    reconciled += _extract_reconciled_count(res_q)
            except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
                pass
    elif ls_mod is not None and qs_mod is not None:
        if locked_slugs:
            try:
                res_lock = ls_mod.sync_dev_pr_lock_with_git(portfolio_dir=state_dir)
                if isinstance(res_lock, dict) and res_lock.get("status") == "ok":
                    fetched += len(locked_slugs)
                    reconciled += _extract_reconciled_count(res_lock)
            except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
                pass
            for u_slug in sorted(unlocked_pr_slugs):
                try:
                    res_u = qs_mod.reconcile_dev_queue_with_git(
                        portfolio_dir=state_dir,
                        repo_slug=u_slug,
                        fetch=True,
                    )
                    if isinstance(res_u, dict) and res_u.get("status") == "ok":
                        fetched += 1
                        reconciled += _extract_reconciled_count(res_u)
                except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
                    pass
        else:
            try:
                res_q = qs_mod.reconcile_dev_queue_with_git(portfolio_dir=state_dir, fetch=True)
                if isinstance(res_q, dict) and res_q.get("status") == "ok":
                    fetched += len(unlocked_pr_slugs)
                    reconciled += _extract_reconciled_count(res_q)
            except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError):
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
                    except (OSError, subprocess.SubprocessError):
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
            except (OSError, subprocess.SubprocessError):
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
            fn = self._sync_fn if self._sync_fn is not None else _facade_attr("_run_live_sync", _run_live_sync)
            msg = fn(state_dir, repo_local_dirs)
        except Exception as exc:
            msg = f"⚠ Sync error: {exc.__class__.__name__}"
        with self._lock:
            self._in_flight = False
            self._last_completed_mono = time.monotonic()
            self._last_result_msg = msg
            self._has_unconsumed = True


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
                    raw_u = re.sub(r"[\x00-\x1f\x7f]", "", str(lk.get(k) or "").strip())
                    if raw_u.startswith(("http://", "https://")):
                        pr_candidates.append(raw_u)
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
