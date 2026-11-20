#!/usr/bin/env python3
"""Layer 1 Shared Runtime Utilities & SDLC Lifecycle Guardrails (`dev_common.py`).

Provides zero-dependency stdlib primitives for `agy-dev-plugin` scripts:
  - State & plugin directory resolution (`resolve_plugin_root`, `resolve_state_dir`)
  - Safe `.env` / `~/.env` loading and masking (`load_dotenv_dict`, `load_env_into_os`)
  - Atomic JSON I/O (`0600`) and POSIX `fcntl.flock` concurrency locks (`file_lock`)
  - Minimal `BaseIntegrationClient` & `HealthProbeResultDTO` base classes for `git_client.py`
  - Optional soft bridges to companion Antigravity plugins/services when present on disk
  - Deterministic SDLC lifecycle guardrails (`DevLifecycleGuardrailEvaluator`) for
    `PreToolUse` (`direct-main-push`, `proprietary-vcs-cli`, `santa-lockdown-binary`,
    `dotenv-write-protection`) and `SessionStart` (`dev-pr-lock-context`)
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import contextlib
from dataclasses import asdict, dataclass, field
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from typing import Any, Iterator
import urllib.parse

PLUGIN_NAME = "agy-dev-plugin"
DEFAULT_DEV_STATE_DIRNAME = "agy-dev-plugin"
COMPAT_PORTFOLIO_STATE_DIRNAME = "wiz-customer-portfolio"

DIRECT_MAIN_PUSH_RE = re.compile(
    r"\bgit\s+(?:-[A-Za-z]+\s+\S+\s+)*push\b(?![^\n;]*--delete\b)[^\n;|&]*\b(?:origin\s+)?(?:HEAD:)?(?:refs/heads/)?(?:main|master)\b"
)
PROPRIETARY_VCS_CLI_RE = re.compile(
    r"(?:^|[;&|]\s*|\$\(\s*)(?:gh\s+(?:pr|issue|repo|release|api)\b|glab\s+(?:mr|issue|repo|api)\b)"
)
SANTA_UNAPPROVED_BINARY_PIPE_RE = re.compile(
    r"(?:\b(?:curl|wget)\b[^;\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b|(?:^|[;&|]\s*)mise\s+(?:install|use)\b)"
)

DETERMINISTIC_DEV_PRETOOL_CHECKS: tuple[dict[str, str], ...] = (
    {
        "check_id": "pretool:direct-main-push",
        "event": "PreToolUse",
        "title": "Protected main/master Branch Push Blocker",
        "target": "run_command (DIRECT_MAIN_PUSH_RE)",
        "decision": "deny",
        "description": "Blocks direct 'git push' to main or master; enforces timestamped fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8> branches.",
    },
    {
        "check_id": "pretool:proprietary-vcs-cli",
        "event": "PreToolUse",
        "title": "Pure Native Git Mandate (gh/glab Blocker)",
        "target": "run_command (PROPRIETARY_VCS_CLI_RE)",
        "decision": "deny",
        "description": "Blocks 'gh' and 'glab' CLI invocations; enforces 100% VCS-agnostic native /usr/bin/git and /usr/bin/ssh.",
    },
    {
        "check_id": "pretool:santa-lockdown-binary",
        "event": "PreToolUse",
        "title": "Santa Lockdown Binary Pipe Blocker",
        "target": "run_command (SANTA_UNAPPROVED_BINARY_PIPE_RE)",
        "decision": "deny",
        "description": "Blocks curl|sh, wget|sh, and 'mise install/use'; enforces declarative asdf (.tool-versions) and pyproject.toml.",
    },
    {
        "check_id": "pretool:dotenv-write-protection",
        "event": "PreToolUse",
        "title": "Live ~/.env Write & Edit Blocker",
        "target": "write_file | edit_file (~/.env)",
        "decision": "deny",
        "description": "Blocks direct agent writes or edits to live '~/.env' credentials; mock placeholders belong in .env.example.",
    },
    {
        "check_id": "pretool:unvalidated-branch-push",
        "event": "PreToolUse",
        "title": "Unvalidated Feature Branch Push Blocker (PR Readiness Gate)",
        "target": "run_command (git push targeting active locked branch with pr_gate_ready=False)",
        "decision": "deny",
        "description": "Blocks direct 'git push' commands targeting an active locked branch before agy-code-pr-reviewer passes (>90/100) and sets pr_gate_ready=True.",
    },
)

UNREADY_PR_GATE_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "AWAITING_AGENT_REVIEW",
        "NEEDS_REVISION_ITERATING",
        "ESCALATED_MAX_REVIEW_ITERATIONS",
    }
)

DETERMINISTIC_DEV_SESSION_CHECKS: tuple[dict[str, str], ...] = (
    {
        "check_id": "session:dev-pr-lock-context",
        "event": "SessionStart",
        "title": "Per-Repository Cross-Conversation dev_pr_locks Context Injector",
        "target": "SessionStart (dev_pr_locks / dev_pr_lock.json / actions_state.json)",
        "decision": "inject_context",
        "description": "Inspects active per-repository dev_pr_locks state at session start, scopes concurrency per repo, and warns never to surface PR links while a lock is awaiting review or iterating.",
    },
)


def resolve_plugin_root() -> Path:
    """Return the active `agy-dev-plugin` root directory (source checkout or `$HOME` plugin mirror)."""
    return Path(__file__).resolve().parent.parent


def load_dotenv_dict(
    env_file: Path | str | None = None,
    *,
    home_dir: Path | None = None,
) -> dict[str, str]:
    """Parse `KEY=VALUE` pairs from `env_file` (or `~/.env`) without mutating process env."""
    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    target = Path(env_file).expanduser() if env_file is not None else (base_home / ".env")
    parsed: dict[str, str] = {}
    if not target.is_file():
        return parsed
    try:
        raw_text = target.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return parsed

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip()
        if len(val) >= 2 and ((val[0] == '"' and val[-1] == '"') or (val[0] == "'" and val[-1] == "'")):
            val = val[1:-1]
        if key:
            parsed[key] = val
    return parsed


def load_env_into_os(
    env_file: Path | str | None = None,
    *,
    home_dir: Path | None = None,
) -> dict[str, str]:
    """Load `env_file` (or `~/.env`) keys into `os.environ` using `setdefault`."""
    loaded = load_dotenv_dict(env_file=env_file, home_dir=home_dir)
    for k, v in loaded.items():
        if v and k not in os.environ:
            os.environ[k] = v
    return loaded


def resolve_state_dir(
    state_dir: Path | str | None = None,
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
    env_file: Path | str | None = None,
) -> Path:
    """Resolve persistent local state directory for `dev_pr_lock.json`, `dev_queue.json`, and watchers.

    Precedence:
      1. Explicit `state_dir` or `portfolio_dir` argument
      2. `AGY_DEV_STATE_DIR` in `os.environ` or `~/.env`
      3. `WIZ_CUSTOMER_PORTFOLIO_DIR` in `os.environ` or `~/.env` (when companion workspace is configured)
      4. Existing companion `<home>/.local/share/wiz-customer-portfolio` if populated while `agy-dev-plugin` dir is absent
      5. Default `<home>/.local/share/agy-dev-plugin`
    """
    explicit = state_dir if state_dir is not None else portfolio_dir
    if explicit is not None and str(explicit).strip():
        return Path(str(explicit)).expanduser().resolve()

    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    dotenv_vars = load_dotenv_dict(env_file=env_file, home_dir=base_home)

    for key in ("AGY_DEV_STATE_DIR", "WIZ_CUSTOMER_PORTFOLIO_DIR"):
        raw = str(
            (os.environ.get(key) if home_dir is None else "") or dotenv_vars.get(key) or ""
        ).strip()
        if raw and not raw.startswith("<"):
            if raw.startswith("~/"):
                return (base_home / raw[2:]).resolve()
            return Path(raw).expanduser().resolve()

    dev_default = (base_home / ".local" / "share" / DEFAULT_DEV_STATE_DIRNAME).resolve()
    compat_dir = (base_home / ".local" / "share" / COMPAT_PORTFOLIO_STATE_DIRNAME).resolve()
    if not dev_default.exists() and (compat_dir / "actions_state.json").is_file():
        return compat_dir
    return dev_default


def atomic_read_json(path: Path | str, default: Any = None) -> Any:
    """Safely read JSON from `path`, returning `default` on missing or corrupt files."""
    target = Path(path).expanduser()
    if not target.is_file():
        return default
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(path: Path | str, data: Any, *, mode: int = 0o600) -> Path:
    """Atomically write JSON to `path` via temp file + `os.replace` with strict file permissions."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp_path, mode)
    os.replace(tmp_path, target)
    return target


@contextmanager
def file_lock(lock_path: Path | str, timeout_seconds: float = 15.0) -> Iterator[None]:
    """Acquire an exclusive non-blocking POSIX `fcntl.flock` lock with bounded retry timeout."""
    lpath = Path(lock_path).expanduser()
    lpath.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lpath), os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + max(0.5, float(timeout_seconds))
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Timed out waiting for lock {lpath}") from exc
                time.sleep(0.05)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


@dataclass
class HealthProbeResultDTO:
    """Normalized Layer 1 integration health probe result."""

    system_id: str
    display_name: str
    status: str = "ok"
    authenticated: bool = True
    account: str = ""
    latency_ms: int = 0
    details: str = ""
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize DTO into a clean dictionary."""
        out = asdict(self)
        extra = out.pop("extra", {}) or {}
        out.update(extra)
        return out


class BaseIntegrationClient:
    """Minimal standalone Layer 1 integration base class compatible with `git_client.py`."""

    system_id: str = "generic_client"
    display_name: str = "Generic Client"
    category: str = "adhoc_client"

    def __init__(
        self,
        env_file: Path | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> None:
        self._env_file = Path(env_file).expanduser() if env_file else None
        self._env_overrides = dict(env_overrides or {})
        self._dotenv_cache = load_dotenv_dict(env_file=self._env_file)

    def get_config(self, key: str, default: str = "") -> str:
        """Resolve config key across `env_overrides`, `os.environ`, and parsed `.env`."""
        if key in self._env_overrides and str(self._env_overrides[key]).strip():
            return str(self._env_overrides[key]).strip()
        env_val = str(os.environ.get(key) or "").strip()
        if env_val:
            return env_val
        file_val = str(self._dotenv_cache.get(key) or "").strip()
        return file_val or default


def _load_optional_module(module_path: Path, mod_name: str) -> Any | None:
    """Dynamically load a companion module when available on disk, returning None if absent."""
    if not module_path.is_file():
        return None
    mod_dir = str(module_path.parent)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    try:
        spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _resolve_companion_clients_dir(home_dir: Path | None = None) -> Path | None:
    """Locate companion `~/.gemini/config/plugins/wiz-tam-plugin/clients` or `~/.gemini/config/clients` if present."""
    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    candidates = [
        base_home / ".gemini" / "config" / "plugins" / "wiz-tam-plugin" / "clients",
        base_home / ".gemini" / "config" / "clients",
        resolve_plugin_root().parent / "agy-tam-plugin" / "clients",
    ]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def _resolve_companion_control_tower_dir(home_dir: Path | None = None) -> Path | None:
    """Locate sibling `g-control-tower` repository directory if installed alongside plugins."""
    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    candidates = [
        resolve_plugin_root().parent / "g-control-tower",
        base_home / "Code" / "Github" / "carlosrodlop" / "g-control-tower",
    ]
    for cand in candidates:
        if (cand / "api" / "server.py").is_file():
            return cand
    return None


def try_restart_control_tower_api(
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> bool:
    """Optionally restart companion `g-control-tower` API server after `$HOME` deployment if present."""
    comp_clients = _resolve_companion_clients_dir(home_dir=home_dir)
    if comp_clients is not None:
        wc_mod = _load_optional_module(comp_clients / "wiz_common.py", "_agy_dev_optional_wiz_common")
        if wc_mod is not None and hasattr(wc_mod, "restart_pro_api_server"):
            try:
                return bool(wc_mod.restart_pro_api_server(portfolio_dir=portfolio_dir))
            except Exception:
                pass
    ct_dir = _resolve_companion_control_tower_dir(home_dir=home_dir)
    if ct_dir is None:
        return False
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "api.server", "restart"],
            cwd=str(ct_dir),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return proc.returncode == 0
    except Exception:
        return False


def try_stop_dev_api_server(
    *,
    portfolio_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Optionally stop companion preview server when Control Tower companion exists."""
    comp_clients = _resolve_companion_clients_dir(home_dir=home_dir)
    if comp_clients is not None:
        wc_mod = _load_optional_module(comp_clients / "wiz_common.py", "_agy_dev_optional_wiz_common")
        if wc_mod is not None and hasattr(wc_mod, "stop_dev_api_server"):
            try:
                res = wc_mod.stop_dev_api_server(portfolio_dir=portfolio_dir)
                if isinstance(res, dict):
                    return res
            except Exception:
                pass
    return {"status": "skipped", "stopped": False}


def try_build_control_tower_ui_url(
    page: str = "health",
    *,
    portfolio_dir: Path | str | None = None,
    ensure_server: bool = False,
    home_dir: Path | None = None,
) -> str:
    """Return companion dashboard URL if configured/available, else local fallback URL."""
    comp_clients = _resolve_companion_clients_dir(home_dir=home_dir)
    if comp_clients is not None:
        wc_mod = _load_optional_module(comp_clients / "wiz_common.py", "_agy_dev_optional_wiz_common")
        if wc_mod is not None and hasattr(wc_mod, "build_pro_dashboard_ui_url"):
            try:
                return str(
                    wc_mod.build_pro_dashboard_ui_url(
                        page,
                        portfolio_dir=portfolio_dir,
                        ensure_server=ensure_server,
                    )
                )
            except Exception:
                pass
    host = str(os.environ.get("AGY_PRO_HOST") or "agy.localhost").strip()
    port = str(os.environ.get("AGY_PRO_PORT") or "8787").strip()
    clean_page = page.strip().lstrip("#")
    return f"http://{host}:{port}/ui#{clean_page}" if clean_page else f"http://{host}:{port}/ui"


def try_inspect_antigravity_conversation(
    conversation_id: str,
    *,
    brain_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Inspect a local Antigravity conversation transcript via companion analyzer or stdlib fallback."""
    comp_clients = _resolve_companion_clients_dir(home_dir=home_dir)
    if comp_clients is not None and brain_dir is None:
        wc_mod = _load_optional_module(comp_clients / "wiz_common.py", "_agy_dev_optional_wiz_common")
        if wc_mod is not None and hasattr(wc_mod, "inspect_antigravity_conversation"):
            try:
                res = wc_mod.inspect_antigravity_conversation(conversation_id)
                if isinstance(res, dict):
                    return res
            except Exception:
                pass

    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    resolved_brain = Path(brain_dir).expanduser() if brain_dir is not None else (base_home / ".gemini" / "antigravity" / "brain")
    tpath = resolved_brain / conversation_id.strip() / ".system_generated" / "logs" / "transcript.jsonl"
    if not tpath.is_file():
        return {"status": "not_found", "conversation_id": conversation_id}

    details = ""
    try:
        for raw_line in tpath.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not raw_line.strip():
                continue
            step = json.loads(raw_line)
            if isinstance(step, dict) and step.get("type") == "USER_INPUT":
                details = str(step.get("content") or "").strip()
                break
    except (OSError, json.JSONDecodeError):
        details = ""
    return {
        "status": "ok",
        "conversation_id": conversation_id,
        "trigger": "prompt",
        "details": details[:400],
        "touched_files": [],
    }


def try_sync_actions_ledger_verdicts(
    verdict_updates: dict[str, dict[str, Any]] | None = None,
    *,
    state_dir: Path | str | None = None,
    legacy_audited: dict[str, Any] | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Optionally sync PR review/resolution verdicts to companion `actions_ledger.db` if present."""
    p_dir = resolve_state_dir(state_dir, home_dir=home_dir)
    ledger_db = p_dir / "actions_ledger.db"
    if not ledger_db.is_file():
        return dict(verdict_updates or {})
    comp_clients = _resolve_companion_clients_dir(home_dir=home_dir)
    if comp_clients is None:
        return dict(verdict_updates or {})
    wc_mod = _load_optional_module(comp_clients / "wiz_common.py", "_agy_dev_optional_wiz_common")
    if wc_mod is None or not hasattr(wc_mod, "ActionsLedgerStore"):
        return dict(verdict_updates or {})
    try:
        store = wc_mod.ActionsLedgerStore(portfolio_dir=p_dir)
        out = store.sync_audit_verdicts(legacy_audited or {})
        if verdict_updates:
            out = store.sync_audit_verdicts(verdict_updates)
        return out if isinstance(out, dict) else dict(verdict_updates or {})
    except Exception:
        return dict(verdict_updates or {})


def get_active_dev_pr_lock_protections(
    *,
    state_dir: Path | str | None = None,
    home_dir: Path | None = None,
) -> tuple[set[str], set[str]]:
    """Return `(active_branches, active_worktree_paths)` held by any active `dev_pr_locks` or legacy `dev_pr_lock` entry."""
    p_dir = resolve_state_dir(state_dir, home_dir=home_dir)
    state_obj = atomic_read_json(p_dir / "actions_state.json", default={})
    if not isinstance(state_obj, dict):
        return set(), set()
    audit_state = state_obj.get("actions_log_audit_state")
    if not isinstance(audit_state, dict):
        return set(), set()

    entries: list[dict[str, Any]] = []
    raw_locks = audit_state.get("dev_pr_locks")
    if isinstance(raw_locks, dict):
        entries.extend(v for v in raw_locks.values() if isinstance(v, dict))
    legacy = audit_state.get("dev_pr_lock")
    if isinstance(legacy, dict):
        entries.append(legacy)

    base_home = str((Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()))
    active_branches: set[str] = set()
    active_worktrees: set[str] = set()
    for entry in entries:
        if not bool(entry.get("active")):
            continue
        main_br = str(entry.get("branch_name") or "").strip().removeprefix("refs/heads/")
        if main_br:
            active_branches.add(main_br)
        raw_wt = str(entry.get("worktree_path") or "").strip()
        if raw_wt:
            with contextlib.suppress(OSError):
                active_worktrees.add(str(Path(raw_wt.replace("$HOME", base_home)).expanduser().resolve()))
        rb_map = entry.get("repo_branches")
        if isinstance(rb_map, dict):
            for rb_item in rb_map.values():
                if not isinstance(rb_item, dict):
                    continue
                rb_br = str(rb_item.get("branch_name") or "").strip().removeprefix("refs/heads/")
                if rb_br:
                    active_branches.add(rb_br)
                rb_wt = str(rb_item.get("worktree_path") or "").strip()
                if rb_wt:
                    with contextlib.suppress(OSError):
                        active_worktrees.add(str(Path(rb_wt.replace("$HOME", base_home)).expanduser().resolve()))
    return active_branches, active_worktrees


def _sanitize_http_target_url(target_url: str) -> str:
    """Validate and strip an optional HTTP(S) target URL for notification dispatch."""
    cleaned = str(target_url or "").strip()
    if not cleaned or any(ch in cleaned for ch in ("\r", "\n")):
        return ""
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return ""
    return cleaned


def try_send_branch_review_notification(
    *,
    status: str,
    subject: str,
    body: str,
    conversation_id: str,
    validator: str,
    branch_name: str,
    target_url: str = "",
    errors_only_override: bool | None = None,
    home_dir: Path | None = None,
) -> dict[str, Any]:
    """Optionally dispatch a branch-review notification using `AGY_DEV_NOTIFY_CMD` or companion `gmail_draft_client.py`."""
    clean_target_url = _sanitize_http_target_url(target_url)
    notify_cmd = str(os.environ.get("AGY_DEV_NOTIFY_CMD") or "").strip()
    if notify_cmd:
        try:
            notify_args = [
                *notify_cmd.split(),
                "--status",
                status,
                "--subject",
                subject,
                "--body",
                body,
                "--conversation-id",
                conversation_id,
                "--branch-name",
                branch_name,
            ]
            if clean_target_url:
                notify_args.extend(["--target-url", clean_target_url])
            proc = subprocess.run(
                notify_args,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return {"sent": proc.returncode == 0, "transport": "AGY_DEV_NOTIFY_CMD"}
        except Exception as exc:
            return {"sent": False, "transport": "AGY_DEV_NOTIFY_CMD", "error": str(exc)}

    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    gmail_client = next(
        (
            cand
            for cand in (
                base_home / ".gemini" / "config" / "clients" / "gmail_draft_client.py",
                base_home / ".gemini" / "config" / "plugins" / "wiz-tam-plugin" / "clients" / "gmail_draft_client.py",
            )
            if cand.is_file()
        ),
        None,
    )
    if gmail_client is None:
        return {"sent": False, "transport": "none", "skipped_reason": "no_notification_transport"}

    errors_only = True if errors_only_override is None else errors_only_override
    if errors_only_override is None:
        gdc_mod = _load_optional_module(gmail_client, "_agy_dev_optional_gmail_draft_client")
        if gdc_mod is not None and hasattr(gdc_mod, "is_error_only_email_policy_enabled"):
            with contextlib.suppress(Exception):
                errors_only = bool(gdc_mod.is_error_only_email_policy_enabled())

    if status == "OK" and errors_only:
        return {"sent": False, "transport": "gmail_draft_client", "skipped_reason": "errors_only_policy"}

    base_cmd = [
        sys.executable,
        str(gmail_client),
        "--send-notification",
        "--status",
        status,
        "--source",
        "Ad-Hoc",
        "--subject",
        subject,
        "--body",
        body,
        "--conversation-id",
        conversation_id,
        "--validator",
        validator,
        "--branch-name",
        branch_name,
    ]
    if errors_only_override is False:
        base_cmd.append("--force-notify-ok")

    cmd = [*base_cmd]
    if clean_target_url:
        cmd.extend(["--target-url", clean_target_url])

    try:
        res_em = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        fallback_without_target_url = False
        if (
            res_em.returncode != 0
            and clean_target_url
            and "unrecognized arguments: --target-url" in (res_em.stderr or "")
        ):
            fallback_without_target_url = True
            res_em = subprocess.run(
                base_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        result: dict[str, Any] = {"sent": res_em.returncode == 0, "transport": "gmail_draft_client"}
        if fallback_without_target_url:
            result["fallback_without_target_url"] = True
        return result
    except Exception as exc:
        return {"sent": False, "transport": "gmail_draft_client", "error": str(exc)}


def normalize_repo_slug(raw_value: str) -> str:
    """Normalize a Git remote URL, SSH path, or `owner/repo` identifier into lowercase `owner/repo`."""
    raw = str(raw_value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    if raw.endswith(".git"):
        raw = raw[:-4]
    raw = raw.rstrip("/")
    m_scp = re.match(r"^(?:ssh://)?[^@:/]+@[^:/]+[:/](.+)$", raw)
    if m_scp:
        raw = m_scp.group(1)
    elif "://" in raw:
        raw = raw.split("://", 1)[1]
        if "/" in raw:
            raw = raw.split("/", 1)[1]
    parts = [p for p in raw.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}".lower()
    return raw.lower()


def _resolve_git_config_from_root(repo_root: Path) -> Path | None:
    """Resolve `.git/config` for a normal Git checkout or `.git` gitdir worktree pointer."""
    dot_git = repo_root / ".git"
    if dot_git.is_dir():
        cfg = dot_git / "config"
        return cfg if cfg.is_file() else None
    if dot_git.is_file():
        with contextlib.suppress(OSError):
            first_line = dot_git.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
            if first_line.lower().startswith("gitdir:"):
                raw_gitdir = first_line.split(":", 1)[1].strip()
                gitdir = Path(raw_gitdir)
                if not gitdir.is_absolute():
                    gitdir = (repo_root / gitdir).resolve()
                commondir_file = gitdir / "commondir"
                if commondir_file.is_file():
                    rel_common = commondir_file.read_text(encoding="utf-8", errors="ignore").strip()
                    common_dir = Path(rel_common)
                    if not common_dir.is_absolute():
                        common_dir = (gitdir / common_dir).resolve()
                    cfg = common_dir / "config"
                    if cfg.is_file():
                        return cfg
                cfg_local = gitdir / "config"
                if cfg_local.is_file():
                    return cfg_local
    return None


def resolve_repo_slug_from_path(candidate: Path | str) -> str:
    """Resolve `owner/repo` (or directory fallback name) by walking up from `candidate` to its `.git` root."""
    raw_str = str(candidate or "").strip()
    if not raw_str:
        return ""
    try:
        cur = Path(raw_str).expanduser().resolve()
    except OSError:
        cur = Path(raw_str).expanduser()
    if cur.is_file() or not cur.exists():
        cur = cur.parent
    for cand in (cur, *cur.parents):
        if (cand / ".git").exists():
            cfg = _resolve_git_config_from_root(cand)
            if cfg is not None:
                with contextlib.suppress(OSError):
                    cfg_text = cfg.read_text(encoding="utf-8", errors="ignore")
                    m_sec = re.search(
                        r'\[remote "origin"\]\s*\n((?:[ \t]+[^\n]*\n?)*)',
                        cfg_text,
                    )
                    if m_sec:
                        m_url = re.search(r"^\s*url\s*=\s*(.+)$", m_sec.group(1), re.MULTILINE)
                        if m_url and m_url.group(1).strip():
                            slug = normalize_repo_slug(m_url.group(1).strip())
                            if slug:
                                return slug
            return cand.name.lower()
    return ""


class DevLifecycleGuardrailEvaluator:
    """Deterministic evaluator for SDLC `PreToolUse` and `SessionStart` lifecycle guardrails."""

    @classmethod
    def get_deterministic_checks(cls, event_name: str = "") -> list[dict[str, str]]:
        """Return the catalog of SDLC deterministic checks enforced for `event_name`."""
        clean = (event_name or "").strip()
        if clean == "PreToolUse":
            return [dict(item) for item in DETERMINISTIC_DEV_PRETOOL_CHECKS]
        if clean == "SessionStart":
            return [dict(item) for item in DETERMINISTIC_DEV_SESSION_CHECKS]
        if not clean:
            return [dict(item) for item in (*DETERMINISTIC_DEV_PRETOOL_CHECKS, *DETERMINISTIC_DEV_SESSION_CHECKS)]
        return []

    def __init__(
        self,
        home_dir: Path | None = None,
        state_dir: Path | None = None,
        *,
        portfolio_dir: Path | None = None,
    ) -> None:
        self.home_dir = (home_dir or Path.home()).expanduser().resolve()
        explicit_state = state_dir if state_dir is not None else portfolio_dir
        self.state_dir = resolve_state_dir(explicit_state, home_dir=self.home_dir)

    def evaluate_pre_tool_use(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a `PreToolUse` hook payload and return `{\"decision\": \"allow\"|\"deny\", ...}`."""
        tool_call = payload.get("toolCall")
        if isinstance(tool_call, dict):
            tool_name = str(tool_call.get("name") or tool_call.get("toolName") or "").strip()
            raw_args = tool_call.get("args") if "args" in tool_call else tool_call.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
        else:
            tool_name = str(payload.get("tool_name") or payload.get("toolName") or "").strip()
            raw_input = payload.get("tool_input") or payload.get("args") or payload.get("arguments") or {}
            args = raw_input if isinstance(raw_input, dict) else {}

        if tool_name == "run_command":
            cmd_line = str(args.get("CommandLine") or args.get("command") or "").strip()
            if DIRECT_MAIN_PUSH_RE.search(cmd_line):
                return {
                    "decision": "deny",
                    "reason": (
                        "Guardrail violation (Protected main Branch - Dev Pillar): direct 'git push' to main/master "
                        "is blocked. Create and push a timestamped branch fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8> "
                        "via pr_remediator.py or git_client.py."
                    ),
                }

            if PROPRIETARY_VCS_CLI_RE.search(cmd_line):
                return {
                    "decision": "deny",
                    "reason": (
                        "Guardrail violation (Pure Native Git): proprietary CLI wrappers ('gh' / 'glab') "
                        "are forbidden. Use 'git_client.py' or 'pr_remediator.py' with native /usr/bin/git and /usr/bin/ssh."
                    ),
                }

            if SANTA_UNAPPROVED_BINARY_PIPE_RE.search(cmd_line):
                return {
                    "decision": "deny",
                    "reason": (
                        "Guardrail violation (Santa Lockdown): piping remote scripts to shell or invoking "
                        "'mise' is blocked. Manage runtimes declaratively via 'asdf' (.tool-versions) and pyproject.toml."
                    ),
                }

            unvalidated_push_deny = self._check_unvalidated_branch_push(cmd_line, payload, args)
            if unvalidated_push_deny is not None:
                return unvalidated_push_deny

        elif tool_name in ("write_file", "edit_file", "replace_file_content", "multi_replace_file_content"):
            raw_target = str(args.get("Path") or args.get("TargetFile") or args.get("path") or "").strip()
            if raw_target:
                expanded = Path(raw_target).expanduser()
                try:
                    resolved = expanded.resolve()
                except OSError:
                    resolved = expanded
                env_file = self.home_dir / ".env"
                if resolved == env_file or raw_target in ("~/.env", "$HOME/.env"):
                    return {
                        "decision": "deny",
                        "reason": (
                            "Guardrail violation (Zero-Secrets): direct agent writes to live '~/.env' "
                            "are blocked. Parameterize mock placeholders in '.env.example' instead."
                        ),
                    }

        return {"decision": "allow"}

    @staticmethod
    def _read_git_head_branch_and_sha(target_dir: Path | None, branch_name: str = "") -> tuple[str, str]:
        """Return `(checked_out_branch, head_sha_for_branch)` for a git checkout or worktree without network calls."""
        if target_dir is None or not target_dir.exists() or not (target_dir / ".git").exists():
            return "", ""
        branch_out = ""
        sha_out = ""
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc_br = subprocess.run(
                ["git", "-C", str(target_dir), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if proc_br.returncode == 0:
                branch_out = (proc_br.stdout or "").strip()
            ref_target = branch_name.strip() or "HEAD"
            proc_sha = subprocess.run(
                ["git", "-C", str(target_dir), "rev-parse", "--verify", ref_target],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if proc_sha.returncode == 0:
                sha_out = (proc_sha.stdout or "").strip()
        return branch_out, sha_out

    def _is_lock_pr_gate_ready(self, lock_data: dict[str, Any]) -> bool:
        """Return True iff `lock_data` passed review (`pr_gate_ready=True`) and HEAD SHA matches `reviewed_head_sha`."""
        if not bool(lock_data.get("pr_gate_ready")):
            return False
        reviewed_sha = str(lock_data.get("reviewed_head_sha") or "").strip()
        if not reviewed_sha:
            return True
        lock_head = str(lock_data.get("head_sha") or "").strip()
        if lock_head and lock_head != reviewed_sha:
            return False
        branch_name = str(lock_data.get("branch_name") or "").strip()
        for pkey in ("worktree_path", "target_repo_dir"):
            raw_p = str(lock_data.get(pkey) or "").strip()
            if not raw_p:
                continue
            cand = Path(raw_p.replace("$HOME", str(self.home_dir))).expanduser()
            if cand.exists() and (cand / ".git").exists():
                _, live_sha = self._read_git_head_branch_and_sha(cand, branch_name=branch_name)
                if live_sha and live_sha != reviewed_sha:
                    return False
                break
        return True

    def _check_unvalidated_branch_push(
        self,
        cmd_line: str,
        payload: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Deny direct `git push` commands targeting an active locked branch before `pr_gate_ready=True`."""
        if "git" not in cmd_line or "push" not in cmd_line:
            return None
        active_map = self._read_active_locks_map()
        if not active_map:
            return None

        unvalidated_locks: list[tuple[str, dict[str, Any]]] = [
            (slug, ldata)
            for slug, ldata in active_map.items()
            if str(ldata.get("branch_name") or "").strip() and not self._is_lock_pr_gate_ready(ldata)
        ]
        if not unvalidated_locks:
            return None

        raw_cwd = str(args.get("Cwd") or args.get("cwd") or payload.get("cwd") or "").strip()
        current_dir: Path | None = Path(raw_cwd.replace("$HOME", str(self.home_dir))).expanduser() if raw_cwd else None

        segments = re.split(r"(?:&&|\|\||[;|\n])", cmd_line)
        for seg in segments:
            seg_clean = seg.strip()
            if not seg_clean:
                continue
            try:
                tokens = shlex.split(seg_clean)
            except ValueError:
                tokens = seg_clean.split()
            if not tokens:
                continue

            # Track `cd <dir>` across chained shell segments
            if tokens[0] == "cd" and len(tokens) >= 2:
                cd_raw = tokens[1].replace("$HOME", str(self.home_dir))
                cd_path = Path(cd_raw).expanduser()
                current_dir = cd_path if cd_path.is_absolute() or current_dir is None else (current_dir / cd_path)
                continue

            # Skip leading env assignments or `env` wrapper
            idx = 0
            if idx < len(tokens) and tokens[idx] == "env":
                idx += 1
            while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
                idx += 1
            if idx >= len(tokens):
                continue
            exe_name = Path(tokens[idx]).name
            if exe_name != "git":
                continue

            idx += 1
            git_c_dir: Path | None = None
            while idx < len(tokens) and tokens[idx].startswith("-"):
                opt = tokens[idx]
                if opt in ("-C", "--git-dir", "--work-tree", "-c") and idx + 1 < len(tokens):
                    if opt == "-C":
                        c_raw = tokens[idx + 1].replace("$HOME", str(self.home_dir))
                        c_path = Path(c_raw).expanduser()
                        git_c_dir = c_path if c_path.is_absolute() or current_dir is None else (current_dir / c_path)
                    idx += 2
                elif opt.startswith("--git-dir=") or opt.startswith("--work-tree="):
                    idx += 1
                else:
                    idx += 1
            if idx >= len(tokens) or tokens[idx] != "push":
                continue

            push_tokens = tokens[idx + 1 :]
            if any(t in ("--delete", "-d") for t in push_tokens):
                continue

            pos_args: list[str] = []
            p_idx = 0
            while p_idx < len(push_tokens):
                tok = push_tokens[p_idx]
                if tok in ("-o", "--push-option", "--repo", "--receive-pack", "--exec") and p_idx + 1 < len(push_tokens):
                    p_idx += 2
                    continue
                if tok.startswith("-"):
                    p_idx += 1
                    continue
                pos_args.append(tok)
                p_idx += 1

            refspecs = pos_args[1:] if len(pos_args) >= 2 else []
            explicit_branches: set[str] = set()
            targets_head_or_implicit = len(refspecs) == 0
            for spec in refspecs:
                clean_spec = spec.lstrip("+")
                if clean_spec.startswith(":"):
                    # Delete refspec (`git push origin :branch`) is always allowed
                    continue
                if ":" in clean_spec:
                    src_part, dst_part = clean_spec.split(":", 1)
                    for part in (src_part, dst_part):
                        norm_p = part.strip().removeprefix("refs/heads/")
                        if norm_p == "HEAD":
                            targets_head_or_implicit = True
                        elif norm_p:
                            explicit_branches.add(norm_p)
                else:
                    norm_s = clean_spec.strip().removeprefix("refs/heads/")
                    if norm_s == "HEAD":
                        targets_head_or_implicit = True
                    elif norm_s:
                        explicit_branches.add(norm_s)

            eff_dir = git_c_dir or current_dir
            eff_dir_resolved: Path | None = None
            if eff_dir is not None:
                with contextlib.suppress(OSError):
                    eff_dir_resolved = eff_dir.resolve()
            eff_branch, _ = self._read_git_head_branch_and_sha(eff_dir_resolved)
            eff_slug = resolve_repo_slug_from_path(eff_dir_resolved) if eff_dir_resolved is not None else ""

            for slug, lock_data in unvalidated_locks:
                lock_branch = str(lock_data.get("branch_name") or "").strip()
                lock_status = str(lock_data.get("status") or "DEVELOPING").strip()
                matched = lock_branch in explicit_branches
                if not matched and targets_head_or_implicit:
                    if eff_branch and eff_branch == lock_branch:
                        matched = True
                    elif eff_dir_resolved is not None:
                        for pkey in ("worktree_path", "target_repo_dir"):
                            raw_lp = str(lock_data.get(pkey) or "").strip()
                            if not raw_lp:
                                continue
                            lp = Path(raw_lp.replace("$HOME", str(self.home_dir))).expanduser()
                            with contextlib.suppress(OSError):
                                lp_res = lp.resolve()
                                if eff_dir_resolved == lp_res or lp_res in eff_dir_resolved.parents:
                                    matched = True
                                    break
                        if not matched and eff_slug and slug and eff_slug == slug:
                            matched = True
                if matched:
                    return {
                        "decision": "deny",
                        "check_id": "pretool:unvalidated-branch-push",
                        "reason": (
                            f"Guardrail violation (PR Readiness Gate — pretool:unvalidated-branch-push): "
                            f"direct 'git push' targeting locked branch '{lock_branch}' (repo '{slug}') "
                            f"is blocked while pr_gate_ready=False (status={lock_status}). "
                            f"Complete independent agy-code-pr-reviewer evaluation (PASS > 90/100) via "
                            f"pr_remediator.py record-review to publish the branch."
                        ),
                    }
        return None

    @staticmethod
    def _extract_repo_slug_from_lock(lock_data: dict[str, Any], default_key: str = "") -> str:
        """Infer normalized `owner/repo` slug from a lock dictionary or fallback key."""
        for key in ("repo_slug", "repo"):
            val = normalize_repo_slug(str(lock_data.get(key) or ""))
            if val:
                return val
        for path_key in ("target_repo_dir", "worktree_path", "worktree_dir"):
            raw_p = str(lock_data.get(path_key) or "").strip()
            if raw_p:
                resolved = resolve_repo_slug_from_path(raw_p.replace("$HOME", str(Path.home())))
                if resolved:
                    return resolved
        clean_def = normalize_repo_slug(default_key)
        return clean_def or "default"

    def _read_active_locks_map(self) -> dict[str, dict[str, Any]]:
        """Inspect `dev_pr_locks`, `dev_pr_lock.json`, and `actions_state.json` across state dirs, keyed by `repo_slug`."""
        search_dirs = [self.state_dir]
        compat_dir = self.home_dir / ".local" / "share" / COMPAT_PORTFOLIO_STATE_DIRNAME
        if compat_dir.resolve() != self.state_dir.resolve():
            search_dirs.append(compat_dir)

        active_map: dict[str, dict[str, Any]] = {}
        for candidate_dir in search_dirs:
            lock_file_data = atomic_read_json(candidate_dir / "dev_pr_lock.json", default={})
            if isinstance(lock_file_data, dict):
                raw_nested = lock_file_data.get("dev_pr_locks") or lock_file_data.get("locks")
                if isinstance(raw_nested, dict):
                    for k, v in raw_nested.items():
                        if isinstance(v, dict) and v.get("active"):
                            slug = self._extract_repo_slug_from_lock(v, k)
                            active_map.setdefault(slug, v)
                elif lock_file_data.get("active"):
                    slug = self._extract_repo_slug_from_lock(lock_file_data)
                    active_map.setdefault(slug, lock_file_data)

            st_raw = atomic_read_json(candidate_dir / "actions_state.json", default={})
            if isinstance(st_raw, dict):
                audit_st = st_raw.get("actions_log_audit_state") or {}
                if isinstance(audit_st, dict):
                    raw_locks = audit_st.get("dev_pr_locks")
                    if isinstance(raw_locks, dict):
                        for k, v in raw_locks.items():
                            if isinstance(v, dict) and v.get("active"):
                                slug = self._extract_repo_slug_from_lock(v, k)
                                active_map.setdefault(slug, v)
                    cand_lock = audit_st.get("dev_pr_lock")
                    if isinstance(cand_lock, dict) and cand_lock.get("active"):
                        slug = self._extract_repo_slug_from_lock(cand_lock)
                        active_map.setdefault(slug, cand_lock)
        return active_map

    def _read_active_lock_data(self, repo_slug: str = "") -> dict[str, Any]:
        """Inspect active locks and return either the lock matching `repo_slug` or the first active lock."""
        active_map = self._read_active_locks_map()
        if not active_map:
            return {}
        clean_slug = normalize_repo_slug(repo_slug)
        if clean_slug:
            return active_map.get(clean_slug, {})
        first_key = next(iter(active_map))
        return active_map[first_key]

    def evaluate_session_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a `SessionStart` hook payload and inject a per-repository lock status notice."""
        active_map = self._read_active_locks_map()
        if not active_map:
            return {"decision": "allow"}

        current_cid = str(payload.get("conversationId") or payload.get("conversation_id") or "").strip()
        workspace_repos: list[str] = []
        raw_paths = payload.get("workspacePaths")
        candidates: list[str] = list(raw_paths) if isinstance(raw_paths, list) else []
        cwd_val = str(payload.get("cwd") or "").strip()
        if cwd_val:
            candidates.append(cwd_val)
        for item in candidates:
            slug = resolve_repo_slug_from_path(str(item))
            if slug and slug not in workspace_repos:
                workspace_repos.append(slug)

        messages: list[str] = []
        other_locked: list[str] = []
        unready_warnings: list[str] = []

        def _append_unready_warning(slug_key: str, ldata: dict[str, Any]) -> None:
            lock_status = str(ldata.get("status") or "").strip().upper()
            br_name = str(ldata.get("branch_name") or "").strip()
            if br_name and lock_status in UNREADY_PR_GATE_LOCK_STATUSES:
                warn_msg = (
                    f"[Antigravity PR Readiness Gate] Repo '{slug_key}' branch '{br_name}' is in status "
                    f"'{lock_status}' (pr_gate_ready=False). NEVER surface Compare/PR links or claim the PR "
                    f"is ready until agy-code-pr-reviewer passes (>90/100) and pr_remediator.py publishes the branch."
                )
                if warn_msg not in unready_warnings:
                    unready_warnings.append(warn_msg)

        if workspace_repos:
            matched_repos = [r for r in workspace_repos if r in active_map]
            free_repos = [r for r in workspace_repos if r not in active_map]
            for r in matched_repos:
                lock_data = active_map[r]
                branch_name = str(lock_data.get("branch_name") or "").strip()
                owner_cid = str(lock_data.get("conversation_id") or "").strip()
                if not branch_name:
                    continue
                if current_cid and current_cid == owner_cid:
                    messages.append(
                        f"[Antigravity Dev Guardrail] Active PR Development Lock (`dev_pr_lock`) for repo '{r}' "
                        f"owned by this conversation ({owner_cid[:8]}) on branch '{branch_name}'. "
                        f"Reuse this branch (--reuse-active --workdir ...) for follow-up commits."
                    )
                else:
                    messages.append(
                        f"[Antigravity Dev Guardrail] Active PR Development Lock (`dev_pr_lock`) for repo '{r}' "
                        f"held by conversation '{owner_cid[:8]}' on branch '{branch_name}'. Do not start a new branch "
                        f"or modify tracked repository files in '{r}' from another conversation until that branch is merged into main or evaluated."
                    )
                _append_unready_warning(r, lock_data)
            for slug, lock_data in active_map.items():
                if slug not in workspace_repos and lock_data.get("branch_name"):
                    other_locked.append(
                        f"{slug}: '{lock_data.get('branch_name')}' (cid {str(lock_data.get('conversation_id') or '')[:8]})"
                    )
                    _append_unready_warning(slug, lock_data)
            if free_repos and other_locked:
                messages.append(
                    f"[Antigravity Dev Guardrail] Active workspace repository {', '.join(free_repos)} is UNLOCKED "
                    f"(safe to branch cleanly). Concurrent locks exist on sibling repositories: {'; '.join(other_locked)}."
                )
        else:
            for slug, lock_data in active_map.items():
                branch_name = str(lock_data.get("branch_name") or "").strip()
                owner_cid = str(lock_data.get("conversation_id") or "").strip()
                if not branch_name:
                    continue
                if current_cid and current_cid == owner_cid:
                    messages.append(
                        f"[Antigravity Dev Guardrail] Active PR Development Lock (`dev_pr_lock`) for '{slug}' "
                        f"owned by this conversation ({owner_cid[:8]}) on branch '{branch_name}'. "
                        f"Reuse this branch (--reuse-active) for follow-up commits."
                    )
                else:
                    messages.append(
                        f"[Antigravity Dev Guardrail] Active PR Development Lock (`dev_pr_lock`) for '{slug}' "
                        f"held by conversation '{owner_cid[:8]}' on branch '{branch_name}'. Do not start a new branch "
                        f"or modify tracked repository files in '{slug}' from another conversation until that branch is merged into main or evaluated."
                    )
                _append_unready_warning(slug, lock_data)

        messages.extend(unready_warnings)

        if not messages:
            return {"decision": "allow"}
        msg = "\n".join(messages)
        return {
            "decision": "allow",
            "additionalContext": msg,
            "injectSteps": [
                {
                    "systemMessage": {
                        "systemMessage": msg,
                    }
                }
            ],
        }

    def evaluate(self, event_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch lifecycle hook evaluation by `event_name`."""
        clean_event = (event_name or "").strip()
        if clean_event == "PreToolUse":
            return self.evaluate_pre_tool_use(payload)
        if clean_event == "SessionStart":
            return self.evaluate_session_start(payload)
        if clean_event == "PostToolUse":
            return {}
        return {"decision": "allow"}


def get_deterministic_dev_checks(event_name: str = "") -> list[dict[str, str]]:
    """Return the catalog of SDLC deterministic checks enforced for `event_name`."""
    return DevLifecycleGuardrailEvaluator.get_deterministic_checks(event_name=event_name)


def evaluate_pre_tool_use(
    payload: dict[str, Any],
    *,
    home_dir: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Pure functional evaluator for an SDLC `PreToolUse` hook payload."""
    return DevLifecycleGuardrailEvaluator(
        home_dir=home_dir,
        state_dir=state_dir,
    ).evaluate_pre_tool_use(payload)


def evaluate_session_start(
    payload: dict[str, Any],
    *,
    home_dir: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Pure functional evaluator for an SDLC `SessionStart` hook payload."""
    return DevLifecycleGuardrailEvaluator(
        home_dir=home_dir,
        state_dir=state_dir,
    ).evaluate_session_start(payload)


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for `dev_common.py`."""
    parser = argparse.ArgumentParser(
        prog="dev_common",
        description="Shared runtime helpers and SDLC lifecycle guardrail evaluator for agy-dev-plugin.",
    )
    sub = parser.add_subparsers(dest="subcommand")

    p_gh = sub.add_parser("guardrail-hook", help="Evaluate Antigravity lifecycle hook payload from stdin.")
    p_gh.add_argument("--event", required=True, choices=("PreToolUse", "SessionStart", "PostToolUse"))

    p_chk = sub.add_parser("checks", help="List deterministic SDLC lifecycle checks.")
    p_chk.add_argument("--event", default="", help="Optional event filter (PreToolUse | SessionStart).")
    p_chk.add_argument("--format", choices=("json", "text"), default="json")
    return parser


def run_dev_queue_tui_cli(argv: list[str] | None = None) -> int:
    """Console entrypoint (`agy-queue`) delegating to `dev_queue_tui.py`."""
    candidates = (
        resolve_plugin_root() / "skills" / "agy-dev-queue-orchestrator" / "scripts" / "dev_queue_tui.py",
        Path.home()
        / ".gemini"
        / "config"
        / "plugins"
        / PLUGIN_NAME
        / "skills"
        / "agy-dev-queue-orchestrator"
        / "scripts"
        / "dev_queue_tui.py",
    )
    for script_path in candidates:
        mod = _load_optional_module(script_path, "_agy_dev_queue_tui_cli")
        if mod is not None and hasattr(mod, "main"):
            return int(mod.main(argv))
    sys.stderr.write("[agy-queue] Error: dev_queue_tui.py not found. Run ./install.sh to deploy agy-dev-plugin.\n")
    return 1


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `dev_common.py`."""
    args = build_parser().parse_args(argv)
    if args.subcommand == "guardrail-hook":
        raw_stdin = ""
        try:
            if not sys.stdin.isatty():
                raw_stdin = sys.stdin.read()
        except Exception:
            raw_stdin = ""
        try:
            payload = json.loads(raw_stdin) if raw_stdin.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        result = DevLifecycleGuardrailEvaluator().evaluate(args.event, payload)
        print(json.dumps(result, ensure_ascii=False))
        return 0

    if args.subcommand == "checks":
        checks = get_deterministic_dev_checks(event_name=args.event)
        if args.format == "text":
            for c in checks:
                print(f"- [{c['event']}] {c['check_id']}: {c['title']} ({c['decision']})")
        else:
            print(json.dumps({"checks": checks, "count": len(checks)}, indent=2, ensure_ascii=False))
        return 0

    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
