#!/usr/bin/env python3
"""Compact zero-hardcode Dev Queue TUI and live Antigravity HITL Board Artifact generator.

Renders a single-table multi-repo status dashboard backed by `dev_queue.json` and
per-repository `dev_pr_locks` (`actions_state.json` / `dev_pr_lock.json`), with:
  - Dynamic zero-hardcode state and repository discovery (`dev_common` + `git_client`)
  - Strict `pr_gate_ready` and `HUMAN_READY_LOCK_STATUSES` gating (never leaking unreviewed PRs)
  - Multi-repo PR-ready progress clarity (`PR 1/2 listo` yellow / `PR listo` green; never averaging
    a human-ready PR with unstarted downstream repos) and exact `★` repo attribution + unlock footer
  - Richest PR URL ranking (`/pull/` > verified `?title=&body=` > bare `?expand=1`) with full
    query preservation across OSC-8 hyperlinks, browser hotkeys, and `dev_queue_board.md`
  - GUI-first Antigravity Desktop integration (`conversation_summaries.db` lookup, clipboard copy +
    app focus, and atomic `dev_queue_board.md` Markdown artifact sync with `conversation://` links)
  - Flicker-free alternate-screen `--watch` mode with `/dev/tty` cbreak hotkeys and offline-safe
    local Git ref validation (`AGY_OFFLINE_ONLY=1`)
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime
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
import time
import tty
from typing import Any, TextIO
from urllib.parse import parse_qs, unquote, urlsplit
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
    import git_client as _git_client  # type: ignore[import-not-found]
except Exception:
    _git_client = None

try:
    import dev_flow_optimizer as _flow_opt  # type: ignore[import-not-found]
except Exception:
    _flow_opt = None

ANSI_RE = re.compile(r"\x1b\]8;;[^\x1b\x07]*(?:\x1b\\|\x07)|\x1b\[[0-9;?]*[A-Za-z]")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_CREATED_PR_PATH_RE = re.compile(r"/(?:pull|pulls|pull-requests|merge_requests)/\d+(?:/|$)")

ALT_SCREEN_ENTER = "\033[?1049h\033[?25l\033[H\033[2J"
ALT_SCREEN_EXIT = "\033[?25h\033[?1049l"

STATUS_ORDER: dict[str, int] = {
    "pr_open": 0,
    "in_progress": 1,
    "pending": 2,
    "hitl_blocked": 3,
    "standby": 4,
    "merged": 5,
    "cleaned": 6,
    "cancelled": 7,
    "rejected": 8,
}

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

UNREADY_PR_GATE_LOCK_STATUSES: frozenset[str] = getattr(
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


def _resolve_brain_dir(override: Path | str | None = None) -> Path:
    """Resolve the Antigravity brain artifact directory."""
    if override is not None and str(override).strip():
        return Path(str(override)).expanduser()
    env_val = str(os.environ.get("AGY_BRAIN_DIR") or "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return Path.home() / ".gemini" / "antigravity" / "brain"


def _color(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def _pad_visible(text: str, width: int) -> str:
    pad = max(0, width - _visible_len(text))
    return text + (" " * pad)


def _trunc(text: str, width: int) -> str:
    if width <= 3:
        return text[: max(width, 0)]
    return text if len(text) <= width else text[: width - 1] + "…"


def _supports_osc8(stream: TextIO | None = None) -> bool:
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


def _load_json(path: Path) -> dict[str, Any]:
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
) -> dict[str, str]:
    """Read exact Antigravity Desktop sidebar conversation titles from `conversation_summaries.db`."""
    clean_ids = [c for c in dict.fromkeys(cids) if _is_uuid(c)]
    resolved_db = _resolve_conv_summaries_db(db_path)
    if not clean_ids or not resolved_db.is_file():
        return {}
    titles: dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{resolved_db}?mode=ro", uri=True, timeout=0.5)
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
    except Exception:
        pass
    return titles


def _load_locks(state_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Load active and historical per-repository `dev_pr_locks` from state files."""
    active: dict[str, dict[str, Any]] = {}
    all_locks: dict[str, dict[str, Any]] = {}
    for candidate in (state_dir / "dev_pr_lock.json", state_dir / "actions_state.json"):
        data = _load_json(candidate)
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
                    all_locks[slug_key] = info
                    if info.get("active"):
                        active[slug_key] = info
                    else:
                        active.pop(slug_key, None)
    return (active, all_locks)


def is_lock_human_ready(lk: dict[str, Any]) -> bool:
    """Return True only when a lock has passed review and satisfied the human PR validation gate."""
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
    if _git_client is not None and hasattr(_git_client, "detect_vcs_provider_from_remote"):
        try:
            provider, web_base, slug = _git_client.detect_vcs_provider_from_remote(raw)
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


def _run_live_sync(
    state_dir: Path,
    repo_local_dirs: dict[str, Path],
    *,
    offline: bool | None = None,
) -> str:
    """Synchronize remote refs (unless offline) and delegate reconciliation to `pr_remediator.py queue-sync`."""
    is_offline = bool(offline) if offline is not None else (os.environ.get("AGY_OFFLINE_ONLY") == "1")
    sub_env = {**os.environ, "AGY_DEV_STATE_DIR": str(state_dir)}
    if is_offline:
        sub_env["AGY_OFFLINE_ONLY"] = "1"

    fetched = 0
    if not is_offline:
        for _slug, rdir in sorted(repo_local_dirs.items()):
            try:
                proc = subprocess.run(
                    ["git", "-C", str(rdir), "fetch", "--prune", "origin"],
                    capture_output=True,
                    text=True,
                    timeout=6.0,
                    check=False,
                )
                if proc.returncode == 0:
                    fetched += 1
            except Exception:
                pass

    reconciled = 0
    pr_rem = _resolve_pr_remediator_script()
    if pr_rem.is_file():
        lock_cmd = [sys.executable, str(pr_rem), "lock-status", "--all"]
        if not is_offline:
            lock_cmd.append("--sync-git")
        try:
            proc_lock = subprocess.run(
                lock_cmd,
                env=sub_env,
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            if proc_lock.returncode == 0:
                reconciled += _extract_reconciled_count(proc_lock.stdout)
        except Exception:
            pass

        queue_cmd = [sys.executable, str(pr_rem), "queue-sync"]
        if not is_offline:
            queue_cmd.append("--fetch")
        try:
            proc_q = subprocess.run(
                queue_cmd,
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

    mode_lbl = "Offline sync" if is_offline else "Sync"
    return f"↻ {mode_lbl} OK ({fetched} repos fetched, {reconciled} PRs reconciled)"


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
    brain_dir: Path | str | None = None,
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
        "threshold_seconds": 900,
        "last_activity_ts": 0.0,
        "worker_cid": str(lk.get("worker_cid") or "").strip(),
    }


def _compute_lock_progress(
    lk: dict[str, Any],
    *,
    now_ts: float | None = None,
    threshold_seconds: int | float | None = None,
    brain_dir: Path | str | None = None,
) -> dict[str, object]:
    """Compute per-lock progress percentage, compact tag, stalled state, and PR readiness flags."""
    st_u = str(lk.get("status") or "DEVELOPING").upper()
    branch_pushed = bool(lk.get("branch_pushed"))
    has_pr = bool(lk.get("pr_url"))

    is_human_ready = is_lock_human_ready(lk)
    is_escalated = st_u == "ESCALATED_MAX_REVIEW_ITERATIONS"
    is_iterating = st_u == "NEEDS_REVISION_ITERATING"
    is_agent_review = (not is_human_ready and not is_escalated) and (
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
    idle_label = str(stall_info.get("idle_label") or "")

    explicit_pct = lk.get("progress_pct")
    if is_stalled:
        pct = 0
        tag = f"STALL {idle_label}".strip()
    elif isinstance(explicit_pct, (int, float)):
        pct = max(0, min(100, int(round(explicit_pct))))
        eta_mins = lk.get("eta_minutes")
        tag = f"~{int(eta_mins)}m" if isinstance(eta_mins, (int, float)) else str(lk.get("stage_label") or "")[:5]
    elif is_human_ready:
        pct, tag = 90, "PR"
    elif is_escalated:
        pct, tag = 0, "esc"
    elif is_iterating:
        pct, tag = 65, "iter"
    elif is_agent_review:
        pct, tag = 75, "rev"
    else:
        has_dirty, commits_ahead = _inspect_worktree_progress(str(lk.get("worktree_path") or ""))
        if commits_ahead > 0 and not has_dirty:
            pct, tag = 60, "~3m"
        elif commits_ahead > 0 and has_dirty:
            pct, tag = 50, "~5m"
        elif has_dirty:
            pct, tag = 35, "~6m"
        else:
            pct, tag = 15, "~10m"

    return {
        "pct": pct,
        "tag": tag,
        "is_human_ready": is_human_ready,
        "is_agent_review": is_agent_review,
        "is_escalated": is_escalated,
        "is_stalled": is_stalled,
        "idle_seconds": idle_seconds,
        "idle_label": idle_label,
        "worker_cid": str(stall_info.get("worker_cid") or ""),
    }


def _compute_item_progress(
    it: dict[str, Any],
    locks_by_cid: dict[str, list[tuple[str, dict[str, Any]]]],
    lock_progress_cache: dict[str, dict[str, object]],
    link_state: str = "",
    is_human_pr: bool = False,
) -> dict[str, object] | None:
    """Compute item progress, rendering full PR-ready bars without averaging unstarted downstream repos."""
    st = str(it.get("status") or "pending").lower()
    repo_slugs = [str(r) for r in (it.get("repo_slugs") or []) if r]
    merged_set = {str(r) for r in (it.get("merged_repos") or []) if r}
    cid = str(it.get("conversation_id") or "")
    linked_locks = locks_by_cid.get(cid, [])

    if st in INACTIVE_STATUSES:
        return None
    if st not in ("in_progress", "pr_open") and not linked_locks and not merged_set:
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
    stalled_tag = ""
    stalled_idle_label = ""

    for slug, lk in linked_locks:
        lp = lock_progress_cache.get(slug)
        if lp is None:
            lp = _compute_lock_progress(lk)
            lock_progress_cache[slug] = lp
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

    if is_human_pr or any_lock_human_ready:
        active_idx = min(total_repos, done_repos + 1)
        is_final_repo = (done_repos + 1) >= total_repos
        return {
            "pct": 100,
            "tag": f"PR {active_idx}/{total_repos}" if total_repos > 1 else "PR listo",
            "mode": "pr_final" if is_final_repo else "pr_part",
            "part_idx": active_idx,
            "total_repos": total_repos,
        }

    if any_lock_stalled:
        return {
            "pct": 0,
            "tag": stalled_tag or f"STALL {stalled_idle_label}".strip(),
            "mode": "stalled",
            "is_stalled": True,
            "idle_label": stalled_idle_label,
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
            tags.append("~10m")

    avg_pct = int(round(sum(per_repo_pct.values()) / max(1, len(per_repo_pct)))) if per_repo_pct else 0
    base_tag = tags[0] if tags else ""
    if total_repos > 1:
        active_idx = min(total_repos, done_repos + (1 if linked_locks or st in ("in_progress", "pr_open") else 0))
        if not linked_locks and done_repos > 0 and st == "pending":
            tag = f"{done_repos}/{total_repos}"
        elif base_tag:
            tag = f"{active_idx}/{total_repos}·{base_tag}"
        else:
            tag = f"{active_idx}/{total_repos}"
    else:
        tag = base_tag

    return {"pct": max(0, min(100, avg_pct)), "tag": tag, "mode": "dev"}


def _render_inline_progress(progress: dict[str, object] | None, col_w: int, color_on: bool) -> str:
    """Render an inline progress bar cell using full yellow/green bars for PR-ready and alert style for stalled locks."""
    if progress is None:
        return _pad_visible(_color("—", "2", color_on), col_w)
    pct = max(0, min(100, int(progress.get("pct") or 0)))  # type: ignore[arg-type]
    tag = str(progress.get("tag") or "").strip()
    mode = str(progress.get("mode") or "")
    bar_cells = 6 if col_w < 21 else 8

    if mode == "stalled":
        lbl = tag or "STALL"
        stall_cells = 4 if (col_w - bar_cells - 1) < len(lbl) and col_w >= 12 else bar_cells
        bar = _color("▓" * stall_cells, "1;31", color_on)
        max_lbl = max(4, col_w - stall_cells - 1)
        lbl_clean = _trunc(lbl, max_lbl)
        return _pad_visible(f"{bar} {_color(lbl_clean, '1;35', color_on)}", col_w)

    if mode in ("pr_part", "pr_final"):
        bar_code = "1;32" if mode == "pr_final" else "1;33"
        bar = _color("█" * bar_cells, bar_code, color_on)
        max_lbl = max(3, col_w - bar_cells - 1)
        lbl = tag or "PR listo"
        if "listo" not in lbl and len(f"{lbl} listo") <= max_lbl:
            lbl = f"{lbl} listo"
        lbl_clean = _trunc(lbl, max_lbl)
        return _pad_visible(f"{bar} {_color(lbl_clean, bar_code, color_on)}", col_w)

    filled = int(round((pct / 100.0) * bar_cells))
    empty = max(0, bar_cells - filled)
    if tag in ("stale", "sync!"):
        bar_code = "1;35"
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
    tag_txt = f" {_color(tag_clean, '1;35' if tag in ('stale', 'sync!') else '2', color_on)}" if tag_clean else ""
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
    """Format compact repository list, highlighting merged (`✓`) and PR-ready (`★`) repos first on truncation."""

    def _fmt(slug: str) -> str:
        name = label_map.get(slug, slug.split("/")[-1])
        if slug in merged_repos:
            return f"{name}✓"
        if pr_repo_slug and slug == pr_repo_slug:
            return f"{name}★"
        return name

    ordered = [str(s) for s in (repo_slugs or []) if s]
    parts = [_fmt(s) for s in ordered]
    if not parts:
        return "-"
    full = ", ".join(parts)
    if len(full) <= max_w:
        return full
    if len(parts) > 1:
        suffix = f" +{len(parts) - 1}"
        primary = parts[0]
        if pr_repo_slug and pr_repo_slug in ordered:
            primary = _fmt(pr_repo_slug)
        first = _trunc(primary, max(6, max_w - len(suffix)))
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
    st = str(it.get("status") or "pending").lower()
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
            if _is_created_pr_url(u):
                fallback_slug = next((r for r in unmerged if r not in unready_locked_slugs), "")
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


def _status_badge(status: str, color_on: bool) -> str:
    styles = {
        "pr_open": ("1;33", "PR_OPEN"),
        "in_progress": ("1;36", "IN_PROGRESS"),
        "pending": ("0;37", "PENDING"),
        "hitl_blocked": ("1;35", "HITL_BLOCK"),
        "standby": ("2;37", "STANDBY"),
        "merged": ("0;32", "MERGED"),
        "cleaned": ("2;32", "CLEANED"),
        "cancelled": ("2;31", "CANCELLED"),
        "rejected": ("0;31", "REJECTED"),
    }
    code, label = styles.get(status, ("0", status.upper()))
    return _color(f"{label:<11}", code, color_on)


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
) -> bool:
    """Atomically write `dev_queue_board.md` with native `conversation://` links and full escaped PR links."""
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
                lp = lock_progress_cache.get(slug) or {}
                is_pr = bool(lp.get("is_human_ready"))
                is_stalled = bool(lp.get("is_stalled"))
                if is_pr:
                    tag = "PR listo"
                elif is_stalled:
                    tag = str(lp.get("tag") or "STALL")
                elif lp.get("is_escalated"):
                    tag = "escalated"
                elif lp.get("is_agent_review"):
                    tag = "agent review"
                else:
                    tag = f"{int(lp.get('pct') or 0)}%"  # type: ignore[arg-type]
                _, u, lock_has_pr, _ = _resolve_lock_human_pr_link(
                    slug,
                    lk,
                    repo_web_bases,
                    repo_local_dirs,
                    extra_urls=_collect_lock_extra_urls(lk, item_urls_by_cid),
                )
                u_md = _escape_md_url(u) if (is_pr and lock_has_pr) else (_clean_clickable_url(u) or u)
                link_part = (
                    f" · [Abrir PR]({u_md})"
                    if (is_pr and lock_has_pr and u_md)
                    else (f" · [Pulls]({u_md})" if u_md else "")
                )
                icon = "🔴" if is_stalled else "🟡"
                md.append(f"- {icon} **`{lbl}`** — `{tag}`{link_part}")
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

        tmp = board_path.with_name(f".{board_path.name}.tmp.{os.getpid()}")
        tmp.write_text("\n".join(md) + "\n", encoding="utf-8")
        os.replace(tmp, board_path)
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
    """Synthesize live table items for active `dev_pr_locks` not represented among active `dev_queue.json` rows."""
    active_queue_cids = [
        str(it.get("conversation_id") or "").strip()
        for it in items
        if str(it.get("status") or "pending").strip().lower() not in INACTIVE_STATUSES
        and str(it.get("conversation_id") or "").strip()
    ]
    grouped: dict[str, dict[str, Any]] = {}
    for slug, lk in sorted(locks.items()):
        if not isinstance(lk, dict) or not bool(lk.get("active", True)):
            continue
        primary_cid = str(lk.get("conversation_id") or "").strip()
        sibs = [str(s).strip() for s in (lk.get("sibling_conversation_ids") or []) if str(s).strip()]
        lock_cids = [c for c in [primary_cid, *sibs] if c]
        if any(_cids_match(lcid, qcid) for lcid in lock_cids for qcid in active_queue_cids):
            continue

        synth_cid = primary_cid or f"lock-{slug.split('/')[-1]}"
        is_ready = is_lock_human_ready(lk)
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
                "priority": 10,
                "status": "pr_open" if is_ready else "in_progress",
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

        if synth_cid not in locks_by_cid or all(s != slug for s, _ in locks_by_cid[synth_cid]):
            locks_by_cid.setdefault(synth_cid, []).append((slug, lk))

    return list(grouped.values())


def render(
    args: argparse.Namespace,
    *,
    conv_db_path: Path | str | None = None,
    brain_dir: Path | str | None = None,
    is_tty_override: bool | None = None,
    term_width_override: int | None = None,
    now_ts_override: float | None = None,
    stall_threshold_seconds: int | float | None = None,
) -> tuple[
    str,
    dict[int, tuple[str, str]],
    dict[int, tuple[str, str, str]],
    tuple[str, str] | None,
    Path,
    dict[str, Path],
]:
    """Render the single-table Dev Queue dashboard and optionally sync the live Antigravity board."""
    is_tty = sys.stdout.isatty() if is_tty_override is None else bool(is_tty_override)
    color_on = is_tty and not bool(args.no_color)
    use_osc8 = (is_tty if is_tty_override is not None else _supports_osc8()) and not bool(args.plain_urls)
    term_w = max(78, term_width_override if term_width_override is not None else shutil.get_terminal_size((124, 40)).columns)
    bar_w = min(term_w, 140)

    state_dir = _resolve_state_dir(args.state_dir)
    queue_data = _load_json(state_dir / "dev_queue.json")
    items = queue_data.get("items") or []
    locks, all_locks = _load_locks(state_dir)
    repo_web_bases, repo_local_dirs = _discover_repos(all_locks, items, args.repos_dir)

    lock_progress_cache: dict[str, dict[str, object]] = {
        slug: _compute_lock_progress(
            lk,
            now_ts=now_ts_override,
            threshold_seconds=stall_threshold_seconds,
            brain_dir=brain_dir,
        )
        for slug, lk in locks.items()
    }
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
    for it in items:
        icid = str(it.get("conversation_id") or "")
        if icid:
            urls = [str(u).strip() for u in (it.get("pr_urls") or []) if str(u or "").strip()]
            if urls:
                item_urls_by_cid.setdefault(icid, []).extend(urls)
        for s in it.get("repo_slugs") or []:
            if s:
                all_managed_slugs.add(str(s))
    label_map = _build_repo_labels(all_managed_slugs)

    locks_by_cid: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    seen_lock_cid_pairs: set[tuple[str, str]] = set()
    for slug, lk in locks.items():
        lock_cids: list[str] = []
        primary_cid = str(lk.get("conversation_id") or "").strip()
        if primary_cid:
            lock_cids.append(primary_cid)
        raw_siblings = lk.get("sibling_conversation_ids")
        if isinstance(raw_siblings, list):
            for sib in raw_siblings:
                sib_clean = str(sib or "").strip()
                if sib_clean:
                    lock_cids.append(sib_clean)
        for lcid in lock_cids:
            pair = (lcid, slug)
            if pair not in seen_lock_cid_pairs:
                seen_lock_cid_pairs.add(pair)
                locks_by_cid.setdefault(lcid, []).append((slug, lk))

    synthetic_lock_items = _synthesize_untracked_lock_items(items, locks, locks_by_cid)
    effective_items = [*synthetic_lock_items, *items]

    counts: dict[str, int] = {}
    for it in effective_items:
        st = str(it.get("status") or "pending")
        counts[st] = counts.get(st, 0) + 1

    filtered: list[dict[str, Any]] = []
    repo_filter = (args.repo or "").strip().lower()
    status_filter = (args.status or "").strip().lower()
    for it in effective_items:
        st = str(it.get("status") or "pending")
        if not args.all and not status_filter and st in INACTIVE_STATUSES:
            continue
        if status_filter and st.lower() != status_filter:
            continue
        repos_raw = [str(r) for r in (it.get("repo_slugs") or [])]
        if repo_filter and not any(repo_filter in r.lower() for r in repos_raw):
            continue
        filtered.append(it)

    filtered.sort(
        key=lambda x: (
            STATUS_ORDER.get(str(x.get("status") or "pending"), 99),
            int(x.get("priority") if x.get("priority") is not None else 999),
            str(x.get("added_at") or ""),
        )
    )
    display_ids = _build_display_ids(filtered)
    all_filtered_cids = [str(it.get("conversation_id") or "") for it in filtered]
    ag_titles = _load_conversation_titles(all_filtered_cids, db_path=conv_db_path)
    managed_list = [s for s in sorted(all_managed_slugs) if not repo_filter or repo_filter in s.lower()]

    if args.json:
        enriched_items: list[dict[str, Any]] = []
        for it in filtered:
            entry = dict(it)
            cid_full = str(it.get("conversation_id") or "")
            if cid_full in ag_titles:
                entry["sidebar_title"] = ag_titles[cid_full]
            enriched_items.append(entry)
        active_locks_payload = {
            slug: {**lk, "progress": lock_progress_cache.get(slug, {})}
            for slug, lk in locks.items()
            if not repo_filter or repo_filter in slug.lower()
        }
        stalled_locks_payload = {
            slug: lock_progress_cache[slug]
            for slug in active_locks_payload
            if bool((lock_progress_cache.get(slug) or {}).get("is_stalled"))
        }
        payload = json.dumps(
            {
                "state_dir": str(state_dir),
                "updated_at": queue_data.get("updated_at", ""),
                "orchestrator_conversation_id": queue_data.get("orchestrator_conversation_id", ""),
                "counts": counts,
                "managed_repos": managed_list,
                "locks": active_locks_payload,
                "active_locks": active_locks_payload,
                "stalled_locks": stalled_locks_payload,
                "opt_hint": opt_hint,
                "items": enriched_items,
            },
            indent=2,
            ensure_ascii=False,
        )
        return (payload, {}, {}, None, state_dir, repo_local_dirs)

    now_local = datetime.now().strftime("%H:%M:%S")
    now_full = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    orch_full = str(queue_data.get("orchestrator_conversation_id") or "-")
    orch_id = orch_full[:8]
    summary_bits = [
        _color(f"pr:{counts.get('pr_open', 0)}", "1;33", color_on),
        _color(f"run:{counts.get('in_progress', 0)}", "1;36", color_on),
        _color(f"pending:{counts.get('pending', 0)}", "1;37", color_on),
        _color(f"hitl:{counts.get('hitl_blocked', 0)}", "1;35", color_on),
        _color(f"standby:{counts.get('standby', 0)}", "2;37", color_on),
        _color(f"merged:{counts.get('merged', 0)}", "0;32", color_on),
    ]

    lines: list[str] = []
    lines.append(_color("═" * bar_w, "2;36", color_on))
    header_left = (
        f"{_color('ANTIGRAVITY DEV QUEUE', '1;36', color_on)}  "
        f"{_color(now_local, '2', color_on)}  "
        f"orch:{_color(orch_id, '1', color_on)}"
    )
    summary_str = "  ".join(summary_bits)
    if bar_w >= 104:
        lines.append(f"{header_left}  |  {summary_str}")
    else:
        lines.append(header_left)
        lines.append(summary_str)

    stalled_alerts: list[tuple[str, str, str]] = []
    for slug in managed_list:
        lk = locks.get(slug)
        lp = lock_progress_cache.get(slug) or {}
        if lk and bool(lp.get("is_stalled")):
            lbl = label_map.get(slug, slug)
            stalled_alerts.append(
                (lbl, str(lp.get("idle_label") or ""), str(lk.get("branch_name") or ""))
            )

    if managed_list:
        repo_pills: list[str] = []
        for slug in managed_list:
            lbl = label_map.get(slug, slug)
            lk = locks.get(slug)
            if lk:
                lp = lock_progress_cache.get(slug) or {}
                is_human_pr = bool(lp.get("is_human_ready"))
                is_stalled = bool(lp.get("is_stalled"))
                is_rev = bool(lp.get("is_agent_review"))
                is_esc = bool(lp.get("is_escalated"))
                if is_human_pr:
                    tag = "PR"
                elif is_stalled:
                    tag = str(lp.get("tag") or "STALL")
                elif is_esc:
                    tag = "esc"
                elif is_rev:
                    tag = str(lp.get("tag") or "rev")
                else:
                    tag = f"{int(lp.get('pct') or 0)}%"  # type: ignore[arg-type]
                dot_code = "1;33" if is_human_pr else ("1;31" if is_stalled else ("1;35" if is_esc else "1;36"))
                dot = _color("●", dot_code, color_on)
                _, repo_u, _, _ = _resolve_lock_human_pr_link(
                    slug,
                    lk,
                    repo_web_bases,
                    repo_local_dirs,
                    extra_urls=_collect_lock_extra_urls(lk, item_urls_by_cid),
                )
                styled_name = _color(lbl, "1;33" if is_human_pr else ("1;31" if is_stalled else "1"), color_on)
                tag_code = "1;33" if is_human_pr else ("1;35" if is_stalled else "2")
                repo_pills.append(
                    f"{dot} {_osc8_link(styled_name, repo_u, use_osc8)} "
                    f"{_color(f'({tag})', tag_code, color_on)}"
                )
            else:
                dot = _color("○", "0;32", color_on)
                pu = _repo_pulls_url(slug, repo_web_bases)
                styled_name = _color(lbl, "0;37", color_on)
                repo_pills.append(f"{dot} {_osc8_link(styled_name, pu, use_osc8)} {_color('(FREE)', '0;32', color_on)}")
        lines.append(f"{_color('REPOS:', '1;33', color_on)}  " + "   ".join(repo_pills))

    lines.append(_color("─" * bar_w, "2", color_on))

    if not filtered:
        lines.append(_color("No matching items in dev_queue.json.", "2", color_on))
        if stalled_alerts:
            lines.append(_color("─" * bar_w, "2", color_on))
            summary_st = ", ".join(f"{r} idle {idle}" for r, idle, _ in stalled_alerts)
            lines.append(
                _color(_trunc(f"⚠ STALLED WORKER: {summary_st} — inspect worktree or re-dispatch", bar_w), "1;31", color_on)
            )
        if opt_hint:
            lines.append(_color(_trunc(opt_hint, bar_w), "1;35", color_on))
        lines.append(_color("═" * bar_w, "2;36", color_on))
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
            )
        return ("\n".join(lines), {}, {}, None, state_dir, repo_local_dirs)

    prog_col = 20 if bar_w >= 112 else 15
    repo_col = 19 if bar_w >= 112 else 14
    link_col = 9
    fixed = 2 + 1 + 4 + 2 + 11 + 2 + prog_col + 2 + repo_col + 2 + 8 + 2 + 2 + link_col
    title_col = max(14, bar_w - fixed)

    hdr = (
        f"{'#':>2} {'Prio':>4}  {'STATUS':<11}  {'PROGRESS':<{prog_col}}  "
        f"{'REPOS':<{repo_col}}  {'ID':<8}  {'TITLE':<{title_col}}  {'ACTION':<{link_col}}"
    )
    lines.append(_color(hdr, "1;4", color_on))

    row_links: dict[int, tuple[str, str]] = {}
    hitl_convs: dict[int, tuple[str, str, str]] = {}
    first_pr_link: tuple[str, str] | None = None
    ready_pr_footer: list[tuple[int, str, str, str, str, str, bool]] = []
    hitl_footer: list[tuple[int, str, str]] = []
    board_rows: list[dict[str, str]] = []
    hitl_board_rows: list[dict[str, str]] = []
    hitl_idx = 0
    resolved_brain_root = _resolve_brain_dir(brain_dir)

    for idx, it in enumerate(filtered, 1):
        prio = int(it.get("priority") if it.get("priority") is not None else 99)
        st = str(it.get("status") or "pending")
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
        )
        prog_cell = _render_inline_progress(prog, prog_col, color_on)
        if prog and str(prog.get("mode") or "") in ("pr_part", "pr_final"):
            total_r = int(prog.get("total_repos") or 1)  # type: ignore[arg-type]
            part_i = int(prog.get("part_idx") or 1)  # type: ignore[arg-type]
            prog_md = f"PR {part_i}/{total_r} listo" if total_r > 1 else "PR listo"
        elif prog and str(prog.get("mode") or "") == "stalled":
            prog_md = str(prog.get("tag") or "STALL")
        else:
            prog_md = f"{int(prog['pct'])}% {prog.get('tag', '')}".strip() if prog else "—"  # type: ignore[arg-type]
        action_md = "—"

        if is_human_pr and link_url:
            clean_u = _clean_clickable_url(link_url) or link_url
            full_u = _sanitize_full_url(link_url) or link_url
            has_body = _url_has_pr_body(full_u)
            row_links[idx] = (f"{cid_disp} (PR)", full_u)
            if first_pr_link is None:
                first_pr_link = (f"{cid_disp} (PR)", full_u)
            repo_lbl = label_map.get(target_slug, target_slug.split("/")[-1]) if target_slug else repos_str
            part_meta = ""
            if len(repo_slugs_list) > 1:
                part_idx = min(len(repo_slugs_list), len(merged_set) + 1)
                remaining_lbls = [
                    label_map.get(r, r.split("/")[-1])
                    for r in repo_slugs_list
                    if r not in merged_set and r != target_slug
                ]
                unlock_txt = f" · desbloquea {', '.join(remaining_lbls)}" if remaining_lbls else ""
                part_meta = f" · parte {part_idx}/{len(repo_slugs_list)}{unlock_txt}"
            ready_pr_footer.append((idx, repo_lbl, cid_disp, clean_u, full_u, part_meta, has_body))
            pill_raw = f"[{idx}] PR" if idx <= 9 else "[↗ PR]"
            pill_styled = _color(pill_raw, "1;33", color_on)
            action_cell = _pad_visible(_osc8_link(pill_styled, full_u, use_osc8), link_col)
            md_pr_u = _escape_md_url(full_u) or clean_u
            action_md = f"[Abrir PR]({md_pr_u})"
            if cid_full in ag_titles:
                action_md += f" · [Chat](conversation://{cid_full})"
        elif link_kind in ("Sync", "Stale"):
            pill_styled = _color(f"[↻ {link_kind}]", "1;35", color_on)
            action_cell = _pad_visible(pill_styled, link_col)
            action_md = f"`{link_kind}`"
        elif st == "hitl_blocked" and cid_full:
            hitl_idx += 1
            ag_t = ag_titles.get(cid_full) or title
            hitl_convs[hitl_idx] = (cid_disp, cid_full, ag_t)
            hitl_footer.append((hitl_idx, cid_disp, ag_t))
            pill_raw = f"[h{hitl_idx}] Agy"
            pill_styled = _color(pill_raw, "1;35", color_on)
            action_cell = _pad_visible(pill_styled, link_col)
            action_md = f"[Abrir chat](conversation://{cid_full})" if _is_uuid(cid_full) else "—"
            cid_brain_dir = resolved_brain_root / cid_full
            prop_mds = sorted(cid_brain_dir.glob("*.md")) if cid_brain_dir.is_dir() else []
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
            if st == "in_progress":
                unmerged = [r for r in repo_slugs_list if r not in merged_set]
                if unmerged:
                    pu = _repo_pulls_url(str(unmerged[0]), repo_web_bases)
                    if pu:
                        row_links[idx] = (f"{cid_disp} (Repo)", pu)
            action_cell = _pad_visible(_color("—", "2", color_on), link_col)
            if cid_full in ag_titles:
                action_md = f"[Chat](conversation://{cid_full})"

        lines.append(
            f"{idx:>2} {prio:>4}  {_status_badge(st, color_on)}  {prog_cell}  "
            f"{repos_str:<{repo_col}}  {_color(f'{cid_disp:<8}', '36', color_on)}  "
            f"{_trunc(' '.join(title.split()), title_col):<{title_col}}  {action_cell}"
        )
        board_rows.append(
            {
                "idx": str(idx),
                "prio": str(prio),
                "status": st.upper(),
                "progress": prog_md,
                "repos": repos_md,
                "title": _trunc(_sanitize_md_table_cell(title), 72),
                "action_md": action_md,
            }
        )

    if ready_pr_footer:
        lines.append(_color("─" * bar_w, "2", color_on))
        lines.append(
            _color(
                "PRs READY FOR MERGE (press number/[o] to open with title + verifications · or Cmd+Click):",
                "1;33",
                color_on,
            )
        )
        for idx, repo_lbl, cid_disp, clean_u, full_u, part_meta, has_body in ready_pr_footer:
            verif_badge = (
                _color(" [+verificaciones]", "0;32", color_on)
                if has_body
                else _color(" [commit fallback]", "2", color_on)
            )
            lines.append(
                f"  {_color(f'[{idx}]', '1;33', color_on)} {_color(repo_lbl, '1', color_on)} "
                f"({cid_disp}{part_meta}){verif_badge}:"
            )
            lines.append(_osc8_link(clean_u, full_u, use_osc8))

    if hitl_footer:
        lines.append(_color("─" * bar_w, "2", color_on))
        lines.append(
            f"{_color('HITL — ANTIGRAVITY APP VALIDATION', '1;35', color_on)} "
            f"{_color('(press h + number: copy title & focus Antigravity · or run /hitl-review / open dev_queue_board.md):', '2', color_on)}"
        )
        for h_i, cid_disp, ag_t in hitl_footer:
            lines.append(
                f"  {_color(f'[h{h_i}]', '1;35', color_on)} "
                f"{_color(cid_disp, '36', color_on)}  "
                f"{_trunc(ag_t, max(20, bar_w - 20))}"
            )

    if stalled_alerts:
        lines.append(_color("─" * bar_w, "2", color_on))
        st_bits = [
            f"{repo_lbl} idle {idle_lbl}" + (f" ({br})" if br else "")
            for repo_lbl, idle_lbl, br in stalled_alerts
        ]
        lines.append(
            _color(
                _trunc(f"⚠ STALLED WORKER: {'; '.join(st_bits)} — inspect worktree or re-dispatch", bar_w),
                "1;31",
                color_on,
            )
        )

    if opt_hint:
        if not stalled_alerts and not ready_pr_footer and not hitl_footer:
            lines.append(_color("─" * bar_w, "2", color_on))
        lines.append(_color(_trunc(opt_hint, bar_w), "1;35", color_on))

    lines.append(_color("═" * bar_w, "2;36", color_on))

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
        )

    return ("\n".join(lines), row_links, hitl_convs, first_pr_link, state_dir, repo_local_dirs)


def _clamp_lines_to_viewport(all_lines: list[str], term_w: int, term_h: int, footer: str) -> list[str]:
    """Clamp rendered lines (accounting for soft wrapping of body and footer) to fit inside the terminal height."""
    safe_w = max(1, term_w)
    footer_rows = max(1, math.ceil(_visible_len(footer) / safe_w))
    budget = max(1, term_h - footer_rows)
    kept: list[str] = []
    used_rows = 0
    for idx, line in enumerate(all_lines):
        vlen = _visible_len(line)
        rows = max(1, math.ceil(vlen / safe_w))
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


def _handle_board_hotkey(args: argparse.Namespace, state_dir: Path) -> str:
    """Sync the configured board artifact (if any) and focus Antigravity Desktop cleanly."""
    board_path = _resolve_board_artifact_path(state_dir, getattr(args, "write_board", ""))
    if board_path is None:
        return "No board path configured (pass --write-board PATH or set dev_queue_board_path.txt)"
    render(args)
    _focus_antigravity_app()
    return f"✓ Board synced ({board_path.name}) · Antigravity focused"


def _build_watch_frame(body_lines: list[str], term_w: int, term_h: int, footer: str) -> str:
    """Build a single flicker-free ANSI synchronized-update watch frame."""
    display_lines = _clamp_lines_to_viewport(body_lines, term_w, term_h, footer)
    return (
        "\033[?2026h\033[H"
        + "\n".join(f"{line}\033[K" for line in display_lines)
        + "\033[J\033[?2026l"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agy-queue",
        description="Compact zero-hardcode TUI for Antigravity multi-repo dev_queue.json and dev_pr_locks.",
    )
    parser.add_argument(
        "-w",
        "--watch",
        nargs="?",
        const=3.0,
        type=float,
        metavar="SECS",
        help="Auto-refresh dashboard in place every N seconds (default: 3).",
    )
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Include merged, cleaned, cancelled, and rejected items.",
    )
    parser.add_argument("-r", "--repo", default="", help="Filter by repository substring.")
    parser.add_argument("-s", "--status", default="", help="Filter by exact status.")
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Fetch origin refs across managed repos and reconcile merged PRs (offline-safe when AGY_OFFLINE_ONLY=1).",
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
    parser.add_argument("--json", action="store_true", help="Output raw JSON.")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI color sequences.")
    return parser


def run_watch_loop(
    args: argparse.Namespace,
    *,
    stream: TextIO | None = None,
    use_tty_override: bool | None = None,
    open_tty: bool = True,
    max_iterations: int | None = None,
    term_size_override: tuple[int, int] | None = None,
) -> int:
    """Run flicker-free alternate-screen watch mode with clean cursor/termios restoration."""
    out_stream = stream if stream is not None else sys.stdout
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

    interval = max(0.5, float(args.watch))
    use_tty = bool(getattr(out_stream, "isatty", lambda: False)()) if use_tty_override is None else bool(use_tty_override)
    tty_fd: int | None = None
    orig_termios: Any = None

    if use_tty and open_tty:
        try:
            tty_fd = os.open("/dev/tty", os.O_RDONLY | os.O_NONBLOCK)
            if os.isatty(tty_fd):
                orig_termios = termios.tcgetattr(tty_fd)
                tty.setcbreak(tty_fd)
            else:
                os.close(tty_fd)
                tty_fd = None
        except Exception:
            if tty_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(tty_fd)
                tty_fd = None

    status_msg = ""
    status_until = 0.0
    awaiting_hitl_digit = False
    iterations = 0

    try:
        if use_tty:
            out_stream.write(ALT_SCREEN_ENTER)
            out_stream.flush()

        while not stop:
            width_arg = term_size_override[0] if term_size_override is not None else None
            out, row_links, hitl_convs, first_pr, st_dir, local_dirs = render(
                args,
                is_tty_override=use_tty,
                term_width_override=width_arg,
            )
            now_mono = time.monotonic()
            if awaiting_hitl_digit:
                footer = "Press HITL number [1-9] -> copy exact title & focus Antigravity App (Esc cancels)…"
            elif status_msg and now_mono < status_until:
                footer = f"{status_msg}   |   [o] PR · [1-9] open · [h+#] Agy · [b] board · [s] sync · [q] quit"
            else:
                status_msg = ""
                footer = (
                    f"[o] PR · [1-9] open · [h+#] Agy · [b] board · [s] sync · "
                    f"[a] merged · [q] quit ({interval:g}s)"
                )

            body_lines = out.splitlines()
            if use_tty:
                if term_size_override is not None:
                    cols, rows = term_size_override
                else:
                    tsize = shutil.get_terminal_size((124, 40))
                    cols, rows = tsize.columns, tsize.lines
                out_stream.write(_build_watch_frame(body_lines, cols, rows, footer))
            else:
                out_stream.write("\n".join([*body_lines, footer]) + "\n")
            out_stream.flush()

            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break

            deadline = time.monotonic() + interval
            while not stop and time.monotonic() < deadline:
                remaining = max(0.05, min(0.15, deadline - time.monotonic()))
                if tty_fd is None:
                    time.sleep(remaining)
                    continue
                try:
                    ready, _, _ = select.select([tty_fd], [], [], remaining)
                except (InterruptedError, OSError):
                    break
                if not ready:
                    continue
                try:
                    ch = os.read(tty_fd, 8).decode("utf-8", errors="ignore")
                except OSError:
                    continue
                if not ch:
                    continue
                key = ch[0].lower()
                if awaiting_hitl_digit:
                    awaiting_hitl_digit = False
                    if key.isdigit() and key != "0":
                        entry = hitl_convs.get(int(key))
                        if entry:
                            cid_disp, _cid_full, ag_title = entry
                            copied = _copy_to_clipboard(ag_title)
                            _focus_antigravity_app()
                            status_msg = (
                                f"✓ Antigravity focused · copied '{_trunc(ag_title, 34)}' ({cid_disp})"
                                if copied
                                else f"✓ Antigravity focused ({cid_disp})"
                            )
                        else:
                            status_msg = f"No [h{key}] item"
                        status_until = time.monotonic() + 4.0
                        break
                    status_msg = "Cancelled"
                    status_until = time.monotonic() + 1.5
                    break
                if key == "q":
                    stop = True
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
                    break
                if key == "r":
                    status_msg = "↻ Refreshed"
                    status_until = time.monotonic() + 2.0
                    break
                if key == "s":
                    status_msg = _run_live_sync(st_dir, local_dirs)
                    status_until = time.monotonic() + 4.0
                    break
                if key == "a":
                    args.all = not args.all
                    status_msg = f"Merged view: {'ON' if args.all else 'OFF'}"
                    status_until = time.monotonic() + 2.5
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
        if sigterm_installed and prev_sigterm is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGTERM, prev_sigterm)
        if tty_fd is not None:
            if orig_termios is not None:
                with contextlib.suppress(Exception):
                    termios.tcsetattr(tty_fd, termios.TCSADRAIN, orig_termios)
            with contextlib.suppress(OSError):
                os.close(tty_fd)
        if use_tty:
            out_stream.write(ALT_SCREEN_EXIT)
            out_stream.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `dev_queue_tui.py` / `agy-queue`."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.open_hitl:
        _, _, hitl_convs, _, _, _ = render(args)
        key_clean = args.open_hitl.strip().lower().lstrip("h")
        entry = hitl_convs.get(int(key_clean)) if key_clean.isdigit() else None
        if not entry:
            print(f"No HITL item found for '{args.open_hitl}'.", file=sys.stderr)
            return 1
        cid_disp, _cid_full, ag_title = entry
        _copy_to_clipboard(ag_title)
        _focus_antigravity_app()
        print(
            f"✓ Copied '{ag_title}' ({cid_disp}) & focused Antigravity Desktop "
            "(paste in sidebar search or open dev_queue_board.md)"
        )
        return 0

    if args.sync:
        st_dir = _resolve_state_dir(args.state_dir)
        q_data = _load_json(st_dir / "dev_queue.json")
        _, all_locks = _load_locks(st_dir)
        _, local_dirs = _discover_repos(all_locks, q_data.get("items") or [], args.repos_dir)
        msg = _run_live_sync(st_dir, local_dirs)
        if args.watch is None and not args.json:
            print(msg)

    if args.watch is None:
        out, _, _, _, _, _ = render(args)
        print(out)
        return 0

    return run_watch_loop(args)


if __name__ == "__main__":
    raise SystemExit(main())
