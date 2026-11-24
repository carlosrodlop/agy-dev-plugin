#!/usr/bin/env python3
"""Antigravity 2.0 Dev Queue Sidecar UI Panel HTTP Server (`server.py`).

Pure Python 3 stdlib HTTP server (Santa Lockdown compliant, Unix portable, Zero-PII) that:
  1. Binds strictly to loopback `127.0.0.1:<ANTIGRAVITY_SIDECAR_WEB_PORT>` (refusing non-loopback hosts).
  2. Validates `ANTIGRAVITY_SIDECAR_UI_TOKEN` (fail-closed `401` on all `/api/*` and `/_sidecar/*`
     routes when missing, empty, or mismatched via constant-time `hmac.compare_digest`).
  3. Serves Vanilla-First Zero-Build assets (`index.html`, `styles.css`, `app.js`, and host/fallback
     `/preload.js`) with strict CSP and `no-store` caching.
  4. Exposes read-only queue, per-repository multi-slot lock, inactivity semaphore, allowlisted `.md`
     artifact URI resolution (`fileView`), and `dev_queue_nav_intent.json` polling + acknowledgment
     while strictly forbidding mutating queue or `agentapi` actions (`send-message`, `new-conversation`).
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import contextlib
from datetime import datetime, timezone
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

_SIDECAR_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SIDECAR_DIR.parents[1]
_INSTALLED_PLUGIN_ROOT = Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin"

for _cand_root in (_INSTALLED_PLUGIN_ROOT, _PLUGIN_ROOT):
    for _rel in (
        Path("skills") / "agy-code-remediation-pr" / "scripts",
        Path("skills") / "agy-dev-queue-orchestrator" / "scripts",
        Path("clients"),
    ):
        _cand = _cand_root / _rel
        if _cand.is_dir():
            _cand_str = str(_cand)
            if _cand_str in sys.path:
                sys.path.remove(_cand_str)
            sys.path.insert(0, _cand_str)

import dev_queue_tui as _tui  # noqa: E402
import merge_watcher as _merge_watcher  # noqa: E402
import orchestration_control as _orch_ctrl  # noqa: E402

BIND_HOST = "127.0.0.1"
MAX_POST_BODY_BYTES = 8192
MAX_ARTIFACTS_PER_CONVERSATION = 4
PANEL_SCHEMA_VERSION = 1

_SAFE_MD_BASENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")
_LOCAL_HOME_PATH_RE = re.compile(r"/(?:Users|home)/(?!testuser\b|test\b|Shared\b)[A-Za-z0-9._-]+")

CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors *; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "object-src 'none'"
)

STATIC_ASSETS: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}

FALLBACK_PRELOAD_JS = """\
(function () {
  "use strict";
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token") || "";
  const conversationId = params.get("conversationId") || "";

  window.addEventListener("message", (event) => {
    if (event.data && event.data.type === "theme-change" && event.data.payload && event.data.payload.cssVariables) {
      for (const [key, value] of Object.entries(event.data.payload.cssVariables)) {
        document.documentElement.style.setProperty(key, String(value));
      }
    }
  });

  function secureFetch(url, options) {
    const opts = options || {};
    const headers = new Headers(opts.headers || {});
    if (!headers.has("Content-Type") && opts.body) {
      headers.set("Content-Type", "application/json");
    }
    if (token) {
      headers.set("X-Sidecar-Token", token);
    }
    return fetch(url, Object.assign({}, opts, { headers }));
  }

  window.sidecar = {
    conversationId,
    fetch: secureFetch,
    async getWorkspaceUris() {
      return [];
    },
    onWorkspaceChange() {
      return function () {};
    },
    ui: {
      toggleAuxPane(request) {
        if (window.parent && window.parent !== window) {
          window.parent.postMessage({ type: "toggle-aux-pane", payload: request }, "*");
        }
      },
      toggleConversation(convId, options) {
        const payload = options && typeof options === "object"
          ? Object.assign({ conversationId: convId }, options)
          : { conversationId: convId };
        if (window.parent && window.parent !== window) {
          window.parent.postMessage({ type: "toggle-conversation", payload }, "*");
        }
      }
    }
  };
})();
"""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_panel_max_slots(explicit_max_slots: Any = None) -> int:
    """Resolve per-repository slot capacity (`[1..4]`, default `3`) via the TUI resolver with a local clamp fallback."""
    resolver = getattr(_tui, "_resolve_max_repo_slots", None)
    if callable(resolver):
        try:
            return int(resolver(explicit_max_slots))
        except Exception:
            pass
    val = 3
    if explicit_max_slots not in (None, "") and not isinstance(explicit_max_slots, bool):
        try:
            val = int(explicit_max_slots)
        except (TypeError, ValueError):
            val = 3
    return min(max(val, 1), 4)


def sanitize_zero_pii_text(text: str, *, home_dir: Path | None = None) -> str:
    """Replace local `/Users/<user>` or `/home/<user>` segments with `$HOME`."""
    if not text:
        return ""
    cleaned = str(text)
    home_variants: list[str] = []
    for cand_home in ([home_dir] if home_dir is not None else []) + [Path.home()]:
        if cand_home is None:
            continue
        with contextlib.suppress(OSError):
            expanded = cand_home.expanduser()
            for variant in (str(expanded.resolve()), str(expanded)):
                if variant and variant != "/" and variant not in home_variants:
                    home_variants.append(variant)
    for variant in sorted(home_variants, key=len, reverse=True):
        cleaned = cleaned.replace(variant, "$HOME")
    return _LOCAL_HOME_PATH_RE.sub("$HOME", cleaned)


def sanitize_zero_pii_payload(value: Any, *, home_dir: Path | None = None) -> Any:
    """Recursively scrub local filesystem usernames from JSON-serializable payloads."""
    if isinstance(value, str):
        return sanitize_zero_pii_text(value, home_dir=home_dir)
    if isinstance(value, dict):
        return {str(k): sanitize_zero_pii_payload(v, home_dir=home_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_zero_pii_payload(item, home_dir=home_dir) for item in value]
    if isinstance(value, tuple):
        return [sanitize_zero_pii_payload(item, home_dir=home_dir) for item in value]
    return value


def validate_bind_host(host: str) -> str:
    """Enforce strict loopback `127.0.0.1` binding for Sidecar UI HTTP servers."""
    cleaned = str(host or "").strip()
    if cleaned != BIND_HOST:
        raise ValueError(f"Sidecar web server must bind strictly to {BIND_HOST!r}, got {host!r}.")
    return BIND_HOST


def resolve_web_port(
    port_override: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve `ANTIGRAVITY_SIDECAR_WEB_PORT` (or explicit CLI override) into a valid TCP port."""
    if port_override is not None:
        port_num = int(port_override)
    else:
        eff_env = os.environ if env is None else env
        raw_port = str(eff_env.get("ANTIGRAVITY_SIDECAR_WEB_PORT") or "").strip()
        if not raw_port:
            raise ValueError("Missing required ANTIGRAVITY_SIDECAR_WEB_PORT environment variable.")
        try:
            port_num = int(raw_port)
        except ValueError as exc:
            raise ValueError(f"Invalid ANTIGRAVITY_SIDECAR_WEB_PORT={raw_port!r}.") from exc
    if port_num < 0 or port_num > 65535:
        raise ValueError(f"Port out of range (0..65535): {port_num}")
    return port_num


def resolve_ui_token(
    token_override: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resolve expected `ANTIGRAVITY_SIDECAR_UI_TOKEN` (empty string triggers fail-closed `401`)."""
    if token_override is not None:
        return str(token_override).strip()
    eff_env = os.environ if env is None else env
    return str(eff_env.get("ANTIGRAVITY_SIDECAR_UI_TOKEN") or "").strip()


def validate_sidecar_token(
    expected_token: str,
    *,
    header_token: str = "",
    auth_header: str = "",
    query_token: str = "",
) -> bool:
    """Validate `ANTIGRAVITY_SIDECAR_UI_TOKEN` fail-closed using constant-time `hmac.compare_digest`."""
    expected = str(expected_token or "").strip()
    if not expected:
        return False

    candidate = str(header_token or "").strip()
    if not candidate and auth_header:
        auth_clean = str(auth_header).strip()
        if auth_clean.lower().startswith("bearer "):
            candidate = auth_clean[7:].strip()
    if not candidate and query_token:
        candidate = str(query_token).strip()
    if not candidate:
        return False

    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def resolve_preload_js(
    *,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> str:
    """Load Antigravity's native `sidecar_sdk/preload.js` when installed, or return safe pure-JS fallback."""
    eff_env = os.environ if env is None else env
    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    candidates: list[Path] = []

    exec_data_dir = str(eff_env.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR") or "").strip()
    if exec_data_dir:
        cur = Path(exec_data_dir).expanduser()
        while cur != cur.parent:
            if cur.name == "sidecar_data":
                app_data = cur.parent
                candidates.append(app_data / "lib" / "node_modules" / "sidecar_sdk" / "preload.js")
                break
            cur = cur.parent

    candidates.extend(
        [
            base_home / ".gemini" / "antigravity" / "lib" / "node_modules" / "sidecar_sdk" / "preload.js",
            base_home / ".gemini" / "config" / "node_modules" / "sidecar_sdk" / "preload.js",
        ]
    )
    for cand in candidates:
        if cand.is_file():
            with contextlib.suppress(OSError):
                content = cand.read_text(encoding="utf-8")
                if content.strip():
                    return content
    return FALLBACK_PRELOAD_JS


def resolve_sidecar_brain_dirs(
    brain_dir_override: Path | str | Sequence[Path | str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> list[Path]:
    """Resolve Antigravity `brain/` roots from explicit override, `ANTIGRAVITY_EXECUTABLE_DATA_DIR`, or `$HOME`."""
    if brain_dir_override is not None:
        return _tui._resolve_brain_dirs(brain_dir_override, home_dir=home_dir)
    eff_env = os.environ if env is None else env
    exec_data_dir = str(eff_env.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR") or "").strip()
    if exec_data_dir:
        cur = Path(exec_data_dir).expanduser()
        while cur != cur.parent:
            if cur.name == "sidecar_data":
                cand_brain = cur.parent / "brain"
                if cand_brain.is_dir():
                    return [cand_brain]
                break
            cur = cur.parent
    return _tui._resolve_brain_dirs(None, home_dir=home_dir)


def list_conversation_artifacts(
    conversation_id: str,
    brain_roots: Sequence[Path],
    *,
    limit: int = MAX_ARTIFACTS_PER_CONVERSATION,
) -> list[dict[str, str]]:
    """Return allowlisted `.md` artifact basenames inside `<brain_root>/<conversation_id>/`."""
    cid = str(conversation_id or "").strip()
    if not _tui._is_uuid(cid):
        return []
    found: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for b_root in brain_roots:
        with contextlib.suppress(OSError):
            root_real = b_root.expanduser().resolve()
            conv_dir = (root_real / cid).resolve()
            if not conv_dir.is_dir() or conv_dir.parent != root_real:
                continue
            for cand in sorted(conv_dir.glob("*.md")):
                name = cand.name
                if name in seen_names or not _SAFE_MD_BASENAME_RE.match(name):
                    continue
                cand_real = cand.resolve()
                if cand_real.parent != conv_dir or not cand_real.is_file():
                    continue
                seen_names.add(name)
                found.append({"filename": name, "label": name})
                if len(found) >= limit:
                    return found
    return found


def resolve_allowed_artifact_uri(
    request_data: Mapping[str, Any],
    *,
    state_dir: Path,
    brain_roots: Sequence[Path],
    board_override: str = "",
) -> dict[str, Any] | None:
    """Resolve an allowlisted Markdown artifact to a `file://` URI strictly under `brain/` or `board_path`."""
    kind = str(request_data.get("kind") or "artifact").strip().lower()
    if kind == "board":
        board_path = _tui._resolve_board_artifact_path(state_dir, board_override)
        if board_path is None:
            return None
        with contextlib.suppress(OSError):
            real_board = board_path.expanduser().resolve()
            if real_board.is_file() and real_board.suffix.lower() == ".md":
                return {
                    "ok": True,
                    "kind": "board",
                    "label": real_board.name,
                    "uri": real_board.as_uri(),
                }
        return None

    cid = str(request_data.get("conversation_id") or "").strip()
    filename = str(request_data.get("filename") or "").strip()
    if not _tui._is_uuid(cid) or not _SAFE_MD_BASENAME_RE.match(filename) or ".." in filename:
        return None

    for b_root in brain_roots:
        with contextlib.suppress(OSError):
            root_real = b_root.expanduser().resolve()
            conv_real = (root_real / cid).resolve()
            if not conv_real.is_dir() or conv_real.parent != root_real:
                continue
            target_real = (conv_real / filename).resolve()
            if target_real.parent == conv_real and target_real.is_file():
                return {
                    "ok": True,
                    "kind": "artifact",
                    "conversation_id": cid,
                    "label": filename,
                    "uri": target_real.as_uri(),
                }
    return None


def build_panel_state(
    *,
    state_dir: Path | str | None = None,
    repos_dir: str = "",
    conv_db_path: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    board_override: str = "",
    now_ts: float | None = None,
    stall_threshold_seconds: int | float | None = None,
    repo_cache: dict[str, Any] | None = None,
    client_etag: str = "",
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Build Zero-PII read-only snapshot for the Antigravity 2.0 Dev Queue Sidecar UI Panel."""
    st_dir = _tui._resolve_state_dir(str(state_dir) if state_dir is not None else "")
    brain_roots = resolve_sidecar_brain_dirs(brain_dir, env=env, home_dir=home_dir)

    queue_data = _tui._load_json(st_dir / "dev_queue.json")
    raw_items = [it for it in (queue_data.get("items") or []) if isinstance(it, dict)]
    locks, all_locks = _tui._load_locks(st_dir)
    repo_web_bases, repo_local_dirs = _tui._discover_repos_cached(
        all_locks,
        raw_items,
        repos_dir,
        repo_cache,
    )

    lock_progress_cache: dict[str, dict[str, object]] = {}
    for slug, repo_lk in locks.items():
        for slot_entry in _tui._iter_active_lock_slots(repo_lk):
            p_key = _tui._slot_progress_cache_key(slug, slot_entry)
            lock_progress_cache[p_key] = _tui._compute_lock_progress(
                slot_entry,
                now_ts=now_ts,
                threshold_seconds=stall_threshold_seconds,
                brain_dir=brain_roots,
            )

    opt_hint = _tui._compute_lightweight_opt_hint(
        items=raw_items,
        locks=locks,
        all_locks=all_locks,
        repo_local_dirs=repo_local_dirs,
        bar_w=140,
        now_ts=now_ts,
        stall_threshold_seconds=stall_threshold_seconds,
        brain_dir=brain_roots,
        enabled=True,
    )

    all_managed_slugs: set[str] = set(locks.keys()) | set(repo_web_bases.keys())
    item_urls_by_cid: dict[str, list[str]] = {}
    known_item_cids: list[str] = []
    for it in raw_items:
        icid = str(it.get("conversation_id") or "").strip()
        if icid:
            if icid not in known_item_cids:
                known_item_cids.append(icid)
            urls = [str(u).strip() for u in (it.get("pr_urls") or []) if str(u or "").strip()]
            if urls:
                item_urls_by_cid.setdefault(icid, []).extend(urls)
        for s in it.get("repo_slugs") or []:
            if s:
                all_managed_slugs.add(str(s))
    label_map = _tui._build_repo_labels(all_managed_slugs)

    locks_by_cid: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    seen_pairs: set[tuple[str, str, int]] = set()
    for slug, repo_lk in locks.items():
        for slot_lk in _tui._iter_active_lock_slots(repo_lk):
            slot_num = int(slot_lk.get("slot_id") or 1)
            lock_cids: list[str] = []
            primary_cid = str(slot_lk.get("conversation_id") or "").strip()
            if primary_cid:
                lock_cids.append(primary_cid)
            raw_sibs = slot_lk.get("sibling_conversation_ids")
            if isinstance(raw_sibs, list):
                for sib in raw_sibs:
                    sib_clean = str(sib or "").strip()
                    if sib_clean:
                        lock_cids.append(sib_clean)
            matched_q_cids = [
                qcid
                for qcid in known_item_cids
                if any(_tui._cids_match(lcid, qcid) for lcid in lock_cids)
            ]
            for lcid in dict.fromkeys([*lock_cids, *matched_q_cids]):
                pair = (lcid, slug, slot_num)
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    locks_by_cid.setdefault(lcid, []).append((slug, slot_lk))

    synthetic_items = _tui._synthesize_untracked_lock_items(raw_items, locks, locks_by_cid)
    effective_items = [*synthetic_items, *raw_items]

    counts: dict[str, int] = {}
    active_items: list[dict[str, Any]] = []
    for it in effective_items:
        eff_st = _tui._resolve_effective_item_status(it, locks_by_cid)
        counts[eff_st] = counts.get(eff_st, 0) + 1
        if eff_st not in _tui.INACTIVE_STATUSES:
            active_items.append(it)

    active_items.sort(
        key=lambda x: (
            _tui.STATUS_ORDER.get(_tui._resolve_effective_item_status(x, locks_by_cid), 99),
            int(
                x.get("effective_priority")
                if x.get("effective_priority") is not None
                else (x.get("priority") if x.get("priority") is not None else 999)
            ),
            int(x.get("priority") if x.get("priority") is not None else 999),
            str(x.get("added_at") or ""),
        )
    )

    orch_full = str(queue_data.get("orchestrator_conversation_id") or "").strip()
    all_lookup_cids = [str(it.get("conversation_id") or "") for it in active_items]
    if orch_full:
        all_lookup_cids.append(orch_full)
    for repo_lk in locks.values():
        for slot_lk in _tui._iter_active_lock_slots(repo_lk):
            for key_cid in ("conversation_id", "worker_cid", "reviewer_subagent_id"):
                val_cid = str(slot_lk.get(key_cid) or "").strip()
                if val_cid:
                    all_lookup_cids.append(val_cid)

    ag_titles = _tui._load_conversation_titles(all_lookup_cids, db_path=conv_db_path)
    display_ids = _tui._build_display_ids(active_items)

    orch_live_txt, _ = _tui._resolve_orchestrator_liveness(
        orch_full or "-",
        brain_dir=brain_roots,
        counts=counts,
        now_ts=now_ts,
        stall_threshold_seconds=stall_threshold_seconds,
    )
    orchestrator_info = {
        "conversation_id": orch_full,
        "display_id": orch_full[:8] if orch_full else "-",
        "is_uuid": _tui._is_uuid(orch_full),
        "sidebar_title": ag_titles.get(orch_full, ""),
        "liveness": orch_live_txt,
    }

    managed_slugs = sorted(all_managed_slugs)
    repos_cards: list[dict[str, Any]] = []
    for slug in managed_slugs:
        lbl = label_map.get(slug, slug)
        pulls_u = _tui._repo_pulls_url(slug, repo_web_bases)
        lk = locks.get(slug)
        if not lk:
            free_max = _resolve_panel_max_slots(None)
            repos_cards.append(
                {
                    "slug": slug,
                    "label": lbl,
                    "locked": False,
                    "slot_occupancy": f"0/{free_max}",
                    "max_slots": free_max,
                    "active_slots_count": 0,
                    "state_tag": "FREE",
                    "is_human_ready": False,
                    "is_stalled": False,
                    "idle_label": "",
                    "pr_url": "",
                    "pulls_url": pulls_u,
                    "slots": [],
                }
            )
            continue

        active_slot_entries = _tui._iter_active_lock_slots(lk)
        max_slots = max(_resolve_panel_max_slots(lk.get("max_slots")), len(active_slot_entries))
        occ = str(lk.get("slot_occupancy") or f"{len(active_slot_entries) or 1}/{max_slots}")
        lp_primary = lock_progress_cache.get(slug) or {}
        is_human_ready = bool(lp_primary.get("is_human_ready"))
        _, lock_pr_url, _, _ = _tui._resolve_lock_human_pr_link(
            slug,
            lk,
            repo_web_bases,
            repo_local_dirs,
            extra_urls=_tui._collect_lock_extra_urls(lk, item_urls_by_cid),
        )
        safe_pr_url = _tui._sanitize_full_url(lock_pr_url) if is_human_ready else ""
        slots_out: list[dict[str, Any]] = []
        any_stalled = False
        for slot_entry in active_slot_entries:
            s_num = int(slot_entry.get("slot_id") or 1)
            s_lp = lock_progress_cache.get(_tui._slot_progress_cache_key(slug, slot_entry)) or {}
            s_stalled = bool(s_lp.get("is_stalled"))
            any_stalled = any_stalled or s_stalled
            s_cid = str(slot_entry.get("conversation_id") or "").strip()
            s_worker = str(slot_entry.get("worker_cid") or "").strip()
            s_landing = slot_entry.get("landing_order")
            s_landing_num = s_landing if isinstance(s_landing, int) and not isinstance(s_landing, bool) else s_num
            slots_out.append(
                {
                    "slot_id": s_num,
                    "slot_label": f"S{s_num}",
                    "status": str(slot_entry.get("status") or ""),
                    "branch_name": str(slot_entry.get("branch_name") or ""),
                    "conversation_id": s_cid,
                    "is_uuid": _tui._is_uuid(s_cid),
                    "display_id": s_cid[:8] if s_cid else "",
                    "worker_cid": s_worker,
                    "worker_is_uuid": _tui._is_uuid(s_worker),
                    "pct": int(s_lp.get("pct") or 0),  # type: ignore[arg-type]
                    "tag": str(s_lp.get("tag") or ""),
                    "idle_label": str(s_lp.get("idle_label") or ""),
                    "is_stalled": s_stalled,
                    "is_human_ready": bool(s_lp.get("is_human_ready")) and s_num == 1,
                    "is_awaiting_base": bool(s_lp.get("is_awaiting_base")),
                    "landing_order": s_landing_num,
                    "overlap_tier": str(slot_entry.get("overlap_tier") or ""),
                }
            )
        state_tag = (
            "PR READY"
            if is_human_ready
            else ("STALL" if any_stalled else str(lp_primary.get("tag") or lk.get("status") or "LOCKED"))
        )
        repos_cards.append(
            {
                "slug": slug,
                "label": lbl,
                "locked": True,
                "slot_occupancy": occ,
                "max_slots": max_slots,
                "active_slots_count": len(slots_out),
                "state_tag": state_tag,
                "is_human_ready": is_human_ready,
                "is_stalled": any_stalled,
                "idle_label": str(lp_primary.get("idle_label") or ""),
                "pr_url": safe_pr_url,
                "pulls_url": pulls_u,
                "slots": slots_out,
            }
        )

    sec_hitl: list[dict[str, Any]] = []
    sec_pr_open: list[dict[str, Any]] = []
    sec_in_progress: list[dict[str, Any]] = []
    sec_pending: list[dict[str, Any]] = []
    hitl_idx = 0

    for row_num, it in enumerate(active_items, 1):
        eff_st = _tui._resolve_effective_item_status(it, locks_by_cid)
        eff_lower = eff_st.lower()
        cid_full = str(it.get("conversation_id") or "").strip()
        cid_disp = display_ids.get(cid_full, cid_full[:8])
        prio = int(it.get("priority") if it.get("priority") is not None else 99)
        eff_prio = int(it.get("effective_priority") if it.get("effective_priority") is not None else prio)
        title = " ".join(str(it.get("title") or "").split())
        sidebar_title = ag_titles.get(cid_full, "")
        repo_slugs_list = [str(r) for r in (it.get("repo_slugs") or []) if r]
        merged_set = {str(r) for r in (it.get("merged_repos") or []) if r}

        _, link_url, is_human_pr, git_state, target_slug = _tui._resolve_item_human_pr_link(
            it, locks_by_cid, repo_web_bases, repo_local_dirs
        )
        pr_repo_slug = target_slug if is_human_pr else ""
        repos_compact = _tui._compact_repos(repo_slugs_list, label_map, merged_set, 44, pr_repo_slug=pr_repo_slug)
        prog = _tui._compute_item_progress(
            it,
            locks_by_cid,
            lock_progress_cache,
            git_state,
            is_human_pr=is_human_pr,
            now_ts=now_ts,
        )

        prog_pct = int(prog.get("pct") or 0) if prog else 0  # type: ignore[arg-type]
        prog_mode = str(prog.get("mode") or "") if prog else ""
        if prog_mode in ("pr_part", "pr_final") and prog:
            total_r = int(prog.get("total_repos") or 1)  # type: ignore[arg-type]
            part_i = int(prog.get("part_idx") or 1)  # type: ignore[arg-type]
            prog_label = f"PR {part_i}/{total_r} ready" if total_r > 1 else "PR ready"
        elif prog_mode == "stalled" and prog:
            prog_label = str(prog.get("tag") or "STALL")
        elif prog_mode == "partial_pending" and prog:
            prog_label = f"{prog.get('tag', '1/2 merged')} ({prog_pct}%)"
        elif prog:
            prog_label = f"{prog_pct}% {prog.get('tag', '')}".strip()
        else:
            prog_label = ""

        linked_locks = locks_by_cid.get(cid_full) or []
        worker_cid = ""
        reviewer_cid = ""
        stage_label = ""
        idle_label = ""
        is_stalled = prog_mode == "stalled"
        if linked_locks:
            l_slug, l_dict = linked_locks[0]
            l_prog = lock_progress_cache.get(_tui._slot_progress_cache_key(l_slug, l_dict)) or {}
            telem = _tui._extract_lock_telemetry_fields(l_prog, l_dict)
            worker_cid = str(telem.get("worker_cid") or "").strip()
            reviewer_cid = str(telem.get("reviewer_subagent_id") or "").strip()
            stage_label = str(telem.get("stage_label") or "").strip()
            idle_label = str(l_prog.get("idle_label") or "")
            is_stalled = is_stalled or bool(l_prog.get("is_stalled"))

        safe_pr_url = _tui._sanitize_full_url(link_url) if (is_human_pr and link_url) else ""
        pr_label = (
            _tui._compact_osc8_pr_label(
                safe_pr_url,
                label_map.get(target_slug, target_slug.split("/")[-1]) if target_slug else repos_compact,
                max_width=64,
            )
            if safe_pr_url
            else ""
        )
        artifacts = list_conversation_artifacts(cid_full, brain_roots)

        card: dict[str, Any] = {
            "row": row_num,
            "conversation_id": cid_full,
            "is_uuid": _tui._is_uuid(cid_full),
            "display_id": cid_disp,
            "priority": prio,
            "effective_priority": eff_prio,
            "effective_status": eff_lower,
            "title": title,
            "sidebar_title": sidebar_title,
            "repos_label": repos_compact,
            "repo_slugs": repo_slugs_list,
            "merged_repos": sorted(merged_set),
            "depends_on": [str(d)[:8] for d in (it.get("depends_on") or []) if str(d).strip()],
            "notes": " ".join(str(it.get("notes") or "").split())[:220],
            "progress_pct": prog_pct,
            "progress_label": prog_label,
            "idle_label": idle_label,
            "is_stalled": is_stalled,
            "stage_label": stage_label,
            "worker_cid": worker_cid,
            "worker_is_uuid": _tui._is_uuid(worker_cid),
            "reviewer_cid": reviewer_cid,
            "reviewer_is_uuid": _tui._is_uuid(reviewer_cid),
            "pr_url": safe_pr_url,
            "pr_label": pr_label,
            "artifacts": artifacts,
        }

        if eff_lower == "hitl_blocked":
            hitl_idx += 1
            card["hitl_index"] = hitl_idx
            card["hotkey"] = f"h{hitl_idx}"
            sec_hitl.append(card)
        elif is_human_pr and safe_pr_url:
            sec_pr_open.append(card)
        elif eff_lower in (
            "in_progress",
            "pr_open",
            "ready_awaiting_base_merge",
            "needs_rebase_reverify",
            "needs_agent_semantic_fix",
            "needs_agent_conflict_resolution",
        ) or bool(linked_locks):
            card["pr_url"] = ""
            card["pr_label"] = ""
            sec_in_progress.append(card)
        else:
            sec_pending.append(card)

    board_file = _tui._resolve_board_artifact_path(st_dir, board_override)
    board_available = bool(board_file is not None and board_file.is_file() and board_file.suffix.lower() == ".md")
    board_info = {
        "available": board_available,
        "label": board_file.name if board_file is not None else "dev_queue_board.md",
    }
    orch_ctrl = _orch_ctrl.load_orchestration_control(state_dir=st_dir, home_dir=home_dir)
    orchestration_state = _orch_ctrl.summarize_orchestration_phase(orch_ctrl, locks)
    ghost_kwargs = _resolve_sidecar_ghost_kwargs(
        state_dir=st_dir,
        conv_db_path=conv_db_path,
        brain_dir=brain_dir,
        home_dir=home_dir,
        env=env,
    )
    ghost_purge_state = _get_cached_ghost_purge_status(
        ghost_kwargs,
        repo_cache=repo_cache,
        now_ts=now_ts,
    )

    semantic_core = sanitize_zero_pii_payload(
        {
            "orchestrator": orchestrator_info,
            "orchestration": orchestration_state,
            "counts": counts,
            "repos": repos_cards,
            "sections": {
                "hitl_blocked": sec_hitl,
                "pr_open": sec_pr_open,
                "in_progress": sec_in_progress,
                "pending": sec_pending,
            },
            "board": board_info,
            "opt_hint": opt_hint,
            "ghost_purge": ghost_purge_state,
        },
        home_dir=home_dir,
    )
    etag = hashlib.sha256(
        json.dumps(semantic_core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]

    raw_nav_intent = _tui.read_active_nav_intent(st_dir, now_ts=now_ts)
    safe_nav_intent = sanitize_zero_pii_payload(raw_nav_intent, home_dir=home_dir) if raw_nav_intent else None

    if client_etag and client_etag == etag:
        return {
            "ok": True,
            "schema_version": PANEL_SCHEMA_VERSION,
            "unchanged": True,
            "etag": etag,
            "generated_at": _utc_now_iso(),
            "nav_intent": safe_nav_intent,
        }

    return {
        "ok": True,
        "schema_version": PANEL_SCHEMA_VERSION,
        "unchanged": False,
        "etag": etag,
        "generated_at": _utc_now_iso(),
        **semantic_core,
        "nav_intent": safe_nav_intent,
    }


def _resolve_sidecar_agy_dirs(
    *,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    env: Mapping[str, str] | None = None,
    home_dir: Path | None = None,
) -> list[Path] | None:
    """Resolve Antigravity runtime root directories from explicit brain/env/home overrides."""
    if brain_dir is not None:
        brain_roots = resolve_sidecar_brain_dirs(brain_dir, env=env, home_dir=home_dir)
        if brain_roots:
            return [Path(b).expanduser().parent for b in brain_roots]
    elif home_dir is not None:
        return [
            Path(home_dir).expanduser() / ".gemini" / "antigravity",
            Path(home_dir).expanduser() / ".gemini" / "antigravity-cli",
        ]
    elif env and str(env.get("ANTIGRAVITY_EXECUTABLE_DATA_DIR") or "").strip():
        brain_roots = resolve_sidecar_brain_dirs(None, env=env, home_dir=home_dir)
        if brain_roots:
            return [Path(b).expanduser().parent for b in brain_roots]
    return None


GHOST_PURGE_CACHE_TTL_SECONDS = 45.0


def _stat_path_signature(path: Path) -> tuple[str, int, int]:
    """Return `(path_str, st_mtime_ns, st_size)` without following symlinks or failing on missing paths."""
    try:
        st = path.stat()
        return (str(path), int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        return (str(path), 0, -1)


def _compute_ghost_purge_cache_signature(ghost_kwargs: Mapping[str, Any]) -> tuple[Any, ...]:
    """Build a fast `os.stat`-only cache key covering DB, `app_storage.json`, and state/lock files."""
    st_dir = Path(ghost_kwargs["portfolio_dir"])
    agy_dirs = ghost_kwargs.get("antigravity_dirs")
    summaries_db = ghost_kwargs.get("summaries_db")
    home_dir = ghost_kwargs.get("home_dir")
    env = ghost_kwargs.get("env")

    eff_agy_dirs = agy_dirs
    if eff_agy_dirs is None:
        eff_agy_dirs = _merge_watcher._infer_antigravity_dirs_from_summaries_db(
            brain_dir=None,
            conversations_dir=None,
            cli_dir=None,
            antigravity_dirs=agy_dirs,
            summaries_db=summaries_db,
        )
    roots = _merge_watcher._resolve_antigravity_roots(antigravity_dirs=eff_agy_dirs)
    dbs = _merge_watcher._resolve_summaries_db_candidates(roots, summaries_db=summaries_db)
    app_paths = _merge_watcher._resolve_app_storage_paths(
        roots,
        has_explicit_root_override=eff_agy_dirs is not None,
        home_dir=home_dir,
        env=env,
    )

    watched_files: list[Path] = [
        st_dir / _merge_watcher.GHOST_PURGE_ARMED_FILENAME,
        st_dir / _merge_watcher.GHOST_PURGE_RESULT_FILENAME,
        st_dir / "actions_state.json",
        st_dir / "dev_queue.json",
    ]
    for db_p in dbs:
        watched_files.append(db_p)
        watched_files.append(Path(f"{db_p}-wal"))
    watched_files.extend(app_paths)
    for r in roots:
        for dir_key in ("brain_dir", "conversations_dir"):
            if r.get(dir_key):
                watched_files.append(Path(r[dir_key]))

    return (
        str(st_dir),
        str(summaries_db or ""),
        str(home_dir or ""),
        tuple(_stat_path_signature(p) for p in watched_files),
    )


def _get_cached_ghost_purge_status(
    ghost_kwargs: Mapping[str, Any],
    *,
    repo_cache: dict[str, Any] | None = None,
    now_ts: float | None = None,
    ttl_seconds: float = GHOST_PURGE_CACHE_TTL_SECONDS,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return mtime + TTL cached `get_ghost_purge_status()` so 1.5s `/api/state` polls run in <5ms."""
    cur_ts = float(now_ts if now_ts is not None else time.time())
    sig = _compute_ghost_purge_cache_signature(ghost_kwargs)
    if repo_cache is not None and not force_refresh:
        cached = repo_cache.get("_ghost_purge_cache")
        if isinstance(cached, Mapping) and cached.get("signature") == sig:
            age = cur_ts - float(cached.get("cached_at") or 0.0)
            exp_ts = cached.get("armed_expires_at_ts")
            arm_unexpired = not isinstance(exp_ts, (int, float)) or cur_ts < float(exp_ts)
            if 0.0 <= age < float(ttl_seconds) and arm_unexpired:
                return dict(cached["value"])

    fresh = _merge_watcher.get_ghost_purge_status(**ghost_kwargs, now_ts=now_ts)
    if repo_cache is not None:
        st_dir = Path(ghost_kwargs["portfolio_dir"])
        arm_doc = _merge_watcher._read_unexpired_ghost_purge_arm(st_dir, now_ts=cur_ts)
        repo_cache["_ghost_purge_cache"] = {
            "signature": sig,
            "cached_at": cur_ts,
            "armed_expires_at_ts": arm_doc.get("expires_at_ts") if isinstance(arm_doc, Mapping) else None,
            "value": fresh,
        }
    return fresh


def _resolve_sidecar_ghost_kwargs(
    *,
    state_dir: Path | str | None = None,
    conv_db_path: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build hermetic ghost-purge status/guard/arm arguments derived strictly from panel config."""
    st_dir = _tui._resolve_state_dir(str(state_dir) if state_dir is not None else "")
    agy_dirs = _resolve_sidecar_agy_dirs(brain_dir=brain_dir, env=env, home_dir=home_dir)
    return {
        "portfolio_dir": st_dir,
        "antigravity_dirs": agy_dirs,
        "summaries_db": conv_db_path,
        "home_dir": home_dir,
        "env": env,
    }


def _resolve_sidecar_cleanup_kwargs(cfg: DevQueuePanelConfig) -> dict[str, Any]:
    """Build hermetic `cleanup_merged_conversations` arguments derived strictly from server config."""
    st_dir = _tui._resolve_state_dir(str(cfg.state_dir) if cfg.state_dir is not None else "")
    agy_dirs = _resolve_sidecar_agy_dirs(brain_dir=cfg.brain_dir, env=cfg.env, home_dir=cfg.home_dir)

    return {
        "all_merged": True,
        "include_noop_actions": True,
        "include_scheduled_actions": True,
        "include_sidecar_events": True,
        "include_orphans": True,
        "portfolio_dir": st_dir,
        "antigravity_dirs": agy_dirs,
        "summaries_db": cfg.conv_db_path,
        "repos_dir": cfg.repos_dir or None,
        "env": cfg.env,
    }


class DevQueuePanelConfig:
    """Immutable runtime configuration for `DevQueuePanelHTTPServer`."""

    def __init__(
        self,
        *,
        ui_token: str,
        state_dir: Path | str | None = None,
        repos_dir: str = "",
        conv_db_path: Path | str | None = None,
        brain_dir: Path | str | Sequence[Path | str] | None = None,
        board_override: str = "",
        home_dir: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.ui_token = str(ui_token or "").strip()
        self.state_dir = state_dir
        self.repos_dir = str(repos_dir or "")
        self.conv_db_path = conv_db_path
        self.brain_dir = brain_dir
        self.board_override = str(board_override or "")
        self.home_dir = home_dir
        self.env = dict(env) if env is not None else None
        self.repo_cache: dict[str, Any] = {}
        self.preload_js = resolve_preload_js(env=self.env, home_dir=self.home_dir)


class DevQueuePanelHTTPServer(ThreadingHTTPServer):
    """Loopback-only `ThreadingHTTPServer` carrying `DevQueuePanelConfig`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int, panel_config: DevQueuePanelConfig) -> None:
        safe_host = validate_bind_host(host)
        self.panel_config = panel_config
        super().__init__((safe_host, int(port)), DevQueuePanelRequestHandler)


class DevQueuePanelRequestHandler(BaseHTTPRequestHandler):
    """Read-only HTTP request handler enforcing `ANTIGRAVITY_SIDECAR_UI_TOKEN` on all API routes."""

    server: DevQueuePanelHTTPServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        head_only: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP_HEADER)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Mapping[str, Any], *, head_only: bool = False) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, encoded, "application/json; charset=utf-8", head_only=head_only)

    def _check_api_token(self, query_params: Mapping[str, list[str]]) -> bool:
        cfg = self.server.panel_config
        q_tokens = query_params.get("token") or []
        return validate_sidecar_token(
            cfg.ui_token,
            header_token=str(self.headers.get("X-Sidecar-Token") or ""),
            auth_header=str(self.headers.get("Authorization") or ""),
            query_token=str(q_tokens[0] if q_tokens else ""),
        )

    def _read_json_body(self) -> dict[str, Any] | None:
        raw_len = str(self.headers.get("Content-Length") or "0").strip()
        try:
            length = int(raw_len)
        except ValueError:
            return None
        if length < 0 or length > MAX_POST_BODY_BYTES:
            return None
        if length == 0:
            return {}
        raw_bytes = self.rfile.read(length)
        try:
            parsed = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _handle_get_or_head(self, *, head_only: bool) -> None:
        parsed = urlsplit(self.path)
        route = parsed.path or "/"
        query = parse_qs(parsed.query)
        cfg = self.server.panel_config

        if route == "/preload.js":
            self._send_bytes(
                HTTPStatus.OK,
                cfg.preload_js.encode("utf-8"),
                "application/javascript; charset=utf-8",
                head_only=head_only,
            )
            return

        if route in STATIC_ASSETS:
            rel_file, ctype = STATIC_ASSETS[route]
            asset_path = (_SIDECAR_DIR / rel_file).resolve()
            if asset_path.parent != _SIDECAR_DIR.resolve() or not asset_path.is_file():
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"}, head_only=head_only)
                return
            self._send_bytes(HTTPStatus.OK, asset_path.read_bytes(), ctype, head_only=head_only)
            return

        if route.startswith(("/api/", "/_sidecar/")):
            if not self._check_api_token(query):
                self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"}, head_only=head_only)
                return

            if route == "/api/state":
                etag_param = str((query.get("etag") or [""])[0] or "").strip()
                try:
                    snap = build_panel_state(
                        state_dir=cfg.state_dir,
                        repos_dir=cfg.repos_dir,
                        conv_db_path=cfg.conv_db_path,
                        brain_dir=cfg.brain_dir,
                        board_override=cfg.board_override,
                        repo_cache=cfg.repo_cache,
                        client_etag=etag_param,
                        env=cfg.env,
                        home_dir=cfg.home_dir,
                    )
                    self._send_json(HTTPStatus.OK, snap, head_only=head_only)
                except Exception as exc:
                    self._send_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {
                            "ok": False,
                            "error": sanitize_zero_pii_text(f"{exc.__class__.__name__}: {exc}", home_dir=cfg.home_dir),
                        },
                        head_only=head_only,
                    )
                return

            if route == "/api/nav-intent":
                st_dir = _tui._resolve_state_dir(str(cfg.state_dir) if cfg.state_dir is not None else "")
                intent = _tui.read_active_nav_intent(st_dir)
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "nav_intent": sanitize_zero_pii_payload(intent, home_dir=cfg.home_dir) if intent else None,
                    },
                    head_only=head_only,
                )
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"}, head_only=head_only)
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"}, head_only=head_only)

    def do_GET(self) -> None:  # noqa: N802
        self._handle_get_or_head(head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_get_or_head(head_only=True)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        route = parsed.path or "/"
        query = parse_qs(parsed.query)
        cfg = self.server.panel_config

        if not route.startswith(("/api/", "/_sidecar/")):
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "method_not_allowed"})
            return

        if not self._check_api_token(query):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return

        if route in ("/_sidecar/send-message", "/_sidecar/new-conversation"):
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "read_only_sidecar"})
            return

        body = self._read_json_body()
        if body is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json_body"})
            return

        if route == "/_sidecar/get-conversation-metadata":
            self._send_json(HTTPStatus.OK, {"response": {}})
            return

        if route == "/api/nav-intent/ack":
            intent_id = str(body.get("intent_id") or "").strip()
            if not intent_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "missing_intent_id"})
                return
            st_dir = _tui._resolve_state_dir(str(cfg.state_dir) if cfg.state_dir is not None else "")
            ack_res = _tui.acknowledge_nav_intent(st_dir, intent_id)
            status_code = HTTPStatus.OK if ack_res.get("ok") else HTTPStatus.CONFLICT
            self._send_json(status_code, ack_res)
            return

        if route == "/api/orchestration/pause":
            st_dir = _tui._resolve_state_dir(str(cfg.state_dir) if cfg.state_dir is not None else "")
            reason = str(body.get("reason") or "")
            res = _orch_ctrl.pause_orchestration(
                state_dir=st_dir,
                home_dir=cfg.home_dir,
                reason=reason,
                source="sidecar_panel",
            )
            locks, _ = _tui._load_locks(st_dir)
            orch_state = _orch_ctrl.summarize_orchestration_phase(res.get("control"), locks)
            self._send_json(
                HTTPStatus.OK,
                sanitize_zero_pii_payload(
                    {
                        "ok": True,
                        "paused": True,
                        "changed": bool(res.get("changed")),
                        "chrome_devtools": res.get("chrome_devtools") or {},
                        "orchestration": orch_state,
                    },
                    home_dir=cfg.home_dir,
                ),
            )
            return

        if route == "/api/orchestration/resume":
            st_dir = _tui._resolve_state_dir(str(cfg.state_dir) if cfg.state_dir is not None else "")
            res = _orch_ctrl.resume_orchestration(
                state_dir=st_dir,
                home_dir=cfg.home_dir,
                source="sidecar_panel",
                request_wake=True,
            )
            locks, _ = _tui._load_locks(st_dir)
            orch_state = _orch_ctrl.summarize_orchestration_phase(res.get("control"), locks)
            self._send_json(
                HTTPStatus.OK,
                sanitize_zero_pii_payload(
                    {
                        "ok": True,
                        "paused": False,
                        "changed": bool(res.get("changed")),
                        "resume_nonce": str(res.get("resume_nonce") or ""),
                        "orchestration": orch_state,
                    },
                    home_dir=cfg.home_dir,
                ),
            )
            return

        if route == "/api/artifact-uri":
            st_dir = _tui._resolve_state_dir(str(cfg.state_dir) if cfg.state_dir is not None else "")
            brain_roots = resolve_sidecar_brain_dirs(cfg.brain_dir, env=cfg.env, home_dir=cfg.home_dir)
            resolved = resolve_allowed_artifact_uri(
                body,
                state_dir=st_dir,
                brain_roots=brain_roots,
                board_override=cfg.board_override,
            )
            if resolved is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "artifact_not_found"})
                return
            self._send_json(HTTPStatus.OK, resolved)
            return

        if route in ("/api/cleanup/preview", "/api/cleanup/execute"):
            is_dry_run = route == "/api/cleanup/preview"
            try:
                cleanup_kwargs = _resolve_sidecar_cleanup_kwargs(cfg)
                cleanup_kwargs["dry_run"] = is_dry_run
                if "include_unaudited_check" in body:
                    cleanup_kwargs["include_unaudited_check"] = bool(body.get("include_unaudited_check"))
                clean_res = _merge_watcher.cleanup_merged_conversations(**cleanup_kwargs)
                ok = str(clean_res.get("status") or "") == "ok"
                status_code = HTTPStatus.OK if ok else HTTPStatus.CONFLICT
                extra_ghost: dict[str, Any] = {}
                if not is_dry_run:
                    cfg.repo_cache.pop("_ghost_purge_cache", None)
                else:
                    ghost_kwargs = _resolve_sidecar_ghost_kwargs(
                        state_dir=cfg.state_dir,
                        conv_db_path=cfg.conv_db_path,
                        brain_dir=cfg.brain_dir,
                        home_dir=cfg.home_dir,
                        env=cfg.env,
                    )
                    extra_ghost["ghost_purge"] = _get_cached_ghost_purge_status(
                        ghost_kwargs,
                        repo_cache=cfg.repo_cache,
                        force_refresh=True,
                    )
                payload = sanitize_zero_pii_payload(
                    {"ok": ok, **clean_res, **extra_ghost},
                    home_dir=cfg.home_dir,
                )
                self._send_json(status_code, payload)
            except Exception as exc:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "error": sanitize_zero_pii_text(f"{exc.__class__.__name__}: {exc}", home_dir=cfg.home_dir),
                    },
                )
            return

        if route in (
            "/api/cleanup/ghosts/arm",
            "/api/cleanup/ghosts/cancel",
            "/api/cleanup/ghosts/ack-result",
        ):
            try:
                ghost_kwargs = _resolve_sidecar_ghost_kwargs(
                    state_dir=cfg.state_dir,
                    conv_db_path=cfg.conv_db_path,
                    brain_dir=cfg.brain_dir,
                    home_dir=cfg.home_dir,
                    env=cfg.env,
                )
                if route == "/api/cleanup/ghosts/arm":
                    raw_mode = str(body.get("mode") or ("restart_now" if body.get("restart_now") else "on_quit")).strip().lower()
                    ttl_raw = body.get("ttl_seconds")
                    ttl_val: float | None = None
                    if ttl_raw is not None and not isinstance(ttl_raw, bool):
                        try:
                            ttl_val = float(ttl_raw)
                        except (TypeError, ValueError):
                            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_ttl_seconds"})
                            return
                    if raw_mode in ("restart_now", "restart_and_purge"):
                        action_res = _merge_watcher.restart_and_purge(
                            ttl_seconds=ttl_val,
                            **ghost_kwargs,
                        )
                    else:
                        action_res = _merge_watcher.arm_ghost_purge(
                            mode="on_quit",
                            ttl_seconds=ttl_val,
                            portfolio_dir=ghost_kwargs["portfolio_dir"],
                            home_dir=ghost_kwargs["home_dir"],
                            env=ghost_kwargs["env"],
                            summaries_db=ghost_kwargs["summaries_db"],
                        )
                    ok = str(action_res.get("status") or "") == "ok"
                    status_code = HTTPStatus.OK if ok else HTTPStatus.CONFLICT
                elif route == "/api/cleanup/ghosts/cancel":
                    action_res = _merge_watcher.cancel_ghost_purge(
                        portfolio_dir=ghost_kwargs["portfolio_dir"],
                        home_dir=ghost_kwargs["home_dir"],
                    )
                    ok = str(action_res.get("status") or "") == "ok"
                    status_code = HTTPStatus.OK if ok else HTTPStatus.CONFLICT
                else:
                    action_res = _merge_watcher.ack_ghost_purge_result(
                        portfolio_dir=ghost_kwargs["portfolio_dir"],
                        home_dir=ghost_kwargs["home_dir"],
                    )
                    ok = str(action_res.get("status") or "") == "ok"
                    status_code = HTTPStatus.OK if ok else HTTPStatus.CONFLICT

                cfg.repo_cache.pop("_ghost_purge_cache", None)
                ghost_state = _get_cached_ghost_purge_status(
                    ghost_kwargs,
                    repo_cache=cfg.repo_cache,
                    force_refresh=True,
                )
                payload = sanitize_zero_pii_payload(
                    {"ok": ok, **action_res, "ghost_purge": ghost_state},
                    home_dir=cfg.home_dir,
                )
                self._send_json(status_code, payload)
            except Exception as exc:
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {
                        "ok": False,
                        "error": sanitize_zero_pii_text(f"{exc.__class__.__name__}: {exc}", home_dir=cfg.home_dir),
                    },
                )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})


def create_panel_server(
    *,
    host: str = BIND_HOST,
    port: int | None = None,
    ui_token: str | None = None,
    state_dir: Path | str | None = None,
    repos_dir: str = "",
    conv_db_path: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    board_override: str = "",
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> DevQueuePanelHTTPServer:
    """Create a loopback-bound `DevQueuePanelHTTPServer` instance."""
    resolved_port = resolve_web_port(port, env=env)
    resolved_token = resolve_ui_token(ui_token, env=env)
    cfg = DevQueuePanelConfig(
        ui_token=resolved_token,
        state_dir=state_dir,
        repos_dir=repos_dir,
        conv_db_path=conv_db_path,
        brain_dir=brain_dir,
        board_override=board_override,
        home_dir=home_dir,
        env=env,
    )
    return DevQueuePanelHTTPServer(host, resolved_port, cfg)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for `server.py`."""
    parser = argparse.ArgumentParser(
        prog="dev-queue-panel",
        description="Read-only Antigravity 2.0 Dev Queue Sidecar UI Panel HTTP server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Loopback TCP port override (defaults to ANTIGRAVITY_SIDECAR_WEB_PORT).",
    )
    parser.add_argument("--state-dir", dest="state_dir", default="", help="Optional state directory override.")
    parser.add_argument("--repos-dir", dest="repos_dir", default="", help="Optional repositories parent directory.")
    parser.add_argument("--brain-dir", dest="brain_dir", default=None, help="Optional Antigravity brain directory.")
    parser.add_argument("--conv-db", dest="conv_db", default="", help="Optional conversation_summaries.db path.")
    parser.add_argument(
        "--dump-state",
        dest="dump_state",
        action="store_true",
        help="Print one Zero-PII JSON snapshot to stdout and exit.",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stop_event: threading.Event | None = None,
) -> int:
    """CLI and Antigravity sidecar entrypoint for `server.py`."""
    args = build_parser().parse_args(argv)
    state_override: Path | str | None = args.state_dir.strip() or None
    conv_db_override: Path | str | None = args.conv_db.strip() or None

    if args.dump_state:
        snap = build_panel_state(
            state_dir=state_override,
            repos_dir=args.repos_dir,
            conv_db_path=conv_db_override,
            brain_dir=args.brain_dir,
        )
        print(json.dumps(snap, indent=2, ensure_ascii=False), flush=True)
        return 0

    try:
        httpd = create_panel_server(
            host=BIND_HOST,
            port=args.port,
            state_dir=state_override,
            repos_dir=args.repos_dir,
            conv_db_path=conv_db_override,
            brain_dir=args.brain_dir,
        )
    except ValueError as exc:
        print(f"[dev-queue-panel] ERROR: {sanitize_zero_pii_text(str(exc))}", file=sys.stderr, flush=True)
        return 2

    actual_port = httpd.server_address[1]
    print(f"[{_utc_now_iso()}] [dev-queue-panel] listening on {BIND_HOST}:{actual_port}", flush=True)

    ev = stop_event if stop_event is not None else threading.Event()
    serve_thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    serve_thread.start()

    def _handle_stop(_signum: int, _frame: object) -> None:
        ev.set()

    prev_sigterm: Any = None
    prev_sigint: Any = None
    installed_signals = False
    if stop_event is None and threading.current_thread() is threading.main_thread():
        with contextlib.suppress(ValueError, OSError):
            prev_sigterm = signal.getsignal(signal.SIGTERM)
            prev_sigint = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGTERM, _handle_stop)
            signal.signal(signal.SIGINT, _handle_stop)
            installed_signals = True

    try:
        while not ev.wait(0.25):
            if not serve_thread.is_alive():
                break
        return 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        serve_thread.join(timeout=2.0)
        if installed_signals:
            with contextlib.suppress(ValueError, OSError):
                if prev_sigterm is not None:
                    signal.signal(signal.SIGTERM, prev_sigterm)
                if prev_sigint is not None:
                    signal.signal(signal.SIGINT, prev_sigint)


if __name__ == "__main__":
    raise SystemExit(main())
