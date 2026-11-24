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
    `dotenv-write-protection`, `unvalidated-branch-push`, `parallel-file-lease`,
    `parallel-worker-deploy-home`) and multi-slot `SessionStart` (`dev-pr-lock-context`)
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
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
from typing import Any
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
    r"(?:"
    r"\b(?:curl|wget)\b[^;\n|]*\|\s*(?:sudo\s+)?(?:(?:ba|z|da|k)?sh|python3?|perl|ruby)\b"
    r"|(?:^|[;&|]\s*)(?:mise\s+(?:install|use)|uv\s+(?:tool|python)\s+install|cargo\s+binstall)\b"
    r"|\b(?:curl|wget)\b[^\n]*(?:releases/download/|(?:-o|-O)\s+\S+)[^\n]*(?:&&|;)[^\n]*\bchmod\s+(?:[a-z]*\+x|[0-7]*[1357][0-7]{0,2})\b"
    r")"
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
        "title": "Santa Lockdown Binary Download & Pipe Blocker",
        "target": "run_command (SANTA_UNAPPROVED_BINARY_PIPE_RE)",
        "decision": "deny",
        "description": "Blocks curl|sh, curl|python*, releases/download + chmod +x, mise, uv tool/python install, and cargo binstall; enforces declarative asdf (.tool-versions) and pure-Python pyproject.toml.",
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
    {
        "check_id": "pretool:parallel-file-lease",
        "event": "PreToolUse",
        "title": "Parallel Multi-Slot File Lease Write Blocker",
        "target": "write_file | edit_file | run_command (target_files leased by sibling slot)",
        "decision": "deny",
        "description": "Blocks writes to non-monolith files currently leased (target_files) by a sibling active slot in the same repository while allowing own-slot and disjoint file edits.",
    },
    {
        "check_id": "pretool:parallel-worker-deploy-home",
        "event": "PreToolUse",
        "title": "Parallel Worktree $HOME Deployment Blocker",
        "target": "run_command (install.sh | deploy-home inside pr-worktree-*)",
        "decision": "deny",
        "description": "Blocks './install.sh' and 'git_client.py deploy-home' execution inside isolated 'pr-worktree-*' directories so parallel workers never race $HOME deployment mid-flight.",
    },
    {
        "check_id": "pretool:paused-chrome-devtools",
        "event": "PreToolUse",
        "title": "Paused Dev Queue Chrome DevTools Blocker",
        "target": "call_mcp_tool | mcp_*chrome*devtools* | run_command (chrome-devtools-mcp)",
        "decision": "deny",
        "description": "Turns off Chrome DevTools MCP tool calls and chrome-devtools-mcp CLI launches whenever Dev Queue orchestration is paused (dev_orchestration_control.json).",
    },
)

_CHROME_DEVTOOLS_TOOL_OR_SERVER_RE = re.compile(r"chrome[-_]?devtools", re.IGNORECASE)
_CHROME_DEVTOOLS_CMD_RE = re.compile(r"(?:chrome[-_]devtools[-_]mcp|CHROME_DEVTOOLS_MCP_JS)", re.IGNORECASE)

PARALLEL_MONOLITH_MIN_LINES = 250
PARALLEL_KNOWN_MONOLITH_BASENAMES: frozenset[str] = frozenset(
    {
        "pr_remediator.py",
        "dashboard.js",
        "portfolio.py",
        "styles.css",
        "index.html",
        "wiz_common.py",
        "dev_common.py",
        "git_client.py",
    }
)
_LEASE_LINE_RANGE_RE = re.compile(r"^L?\d+(?:\s*-\s*L?\d+)?$", re.IGNORECASE)
_LEASE_REPO_PREFIX_RE = re.compile(r"^[A-Za-z0-9_.-]{2,}(?:/[A-Za-z0-9_.-]+)?$")
_LEASE_SINGLE_COLON_RE = re.compile(r"(?<!:):(?!:)")
_SHELL_WRITE_REDIRECT_RE = re.compile(r"(?:^|[^0-9&])>>?\|?\s*(?!&|\s*$)(?:'([^']+)'|\"([^\"]+)\"|([^\s;|&>]+))")
_SHELL_TEE_TARGET_RE = re.compile(r"\btee\s+(?:-[a-zA-Z]+\s+)*(?:'([^']+)'|\"([^\"]+)\"|([^\s;|&>-][^\s;|&>]*))")

UNREADY_PR_GATE_LOCK_STATUSES: frozenset[str] = frozenset(
    {
        "AWAITING_AGENT_REVIEW",
        "NEEDS_REVISION_ITERATING",
        "ESCALATED_MAX_REVIEW_ITERATIONS",
        "READY_AWAITING_BASE_MERGE",
        "NEEDS_REBASE_REVERIFY",
        "NEEDS_AGENT_SEMANTIC_FIX",
        "NEEDS_AGENT_CONFLICT_RESOLUTION",
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
    previous = sys.modules.get(mod_name)
    try:
        spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        if previous is None:
            sys.modules.pop(mod_name, None)
        else:
            sys.modules[mod_name] = previous
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


def resolve_antigravity_brain_dirs(
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    *,
    home_dir: Path | None = None,
    explicit_state_dir: bool = False,
) -> list[Path]:
    """Resolve ordered Antigravity `brain/` candidate directories across Desktop and CLI runtimes.

    Precedence:
      1. Explicit `brain_dir`: non-empty path(s) -> `[Path(...).expanduser()]`; empty string/sequence -> `[]`
      2. `AGY_BRAIN_DIR` env var when non-empty -> `[Path(env_b).expanduser()]`
      3. `explicit_state_dir=True` -> `[]` (preserves 100% hermetic custom-state unit tests)
      4. Default -> `[<home>/.gemini/antigravity/brain, <home>/.gemini/antigravity-cli/brain]`
    """
    if brain_dir is not None:
        if isinstance(brain_dir, Sequence) and not isinstance(brain_dir, (str, bytes)):
            out: list[Path] = []
            for item in brain_dir:
                s = str(item or "").strip()
                if s:
                    out.append(Path(s).expanduser())
            return out
        b_str = str(brain_dir).strip()
        return [Path(b_str).expanduser()] if b_str else []

    env_b = str(os.environ.get("AGY_BRAIN_DIR") or "").strip()
    if env_b:
        return [Path(env_b).expanduser()]
    if explicit_state_dir:
        return []

    base_home = Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()
    return [
        base_home / ".gemini" / "antigravity" / "brain",
        base_home / ".gemini" / "antigravity-cli" / "brain",
    ]


def resolve_transcript_path(
    conversation_id: str,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    *,
    home_dir: Path | None = None,
    explicit_state_dir: bool = False,
) -> Path | None:
    """Return the first existing `transcript.jsonl` path for `conversation_id` across candidate `brain/` dirs."""
    cid = str(conversation_id or "").strip()
    if not cid:
        return None
    for b_root in resolve_antigravity_brain_dirs(
        brain_dir,
        home_dir=home_dir,
        explicit_state_dir=explicit_state_dir,
    ):
        cand = b_root / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if cand.is_file():
            return cand
    return None


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
                if isinstance(res, dict) and res.get("status") == "ok":
                    return res
            except Exception:
                pass

    tpath = resolve_transcript_path(conversation_id, brain_dir=brain_dir, home_dir=home_dir)
    if tpath is None:
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
    """Return `(active_branches, active_worktree_paths)` held by any active `dev_pr_locks` (including all `slots`) or legacy `dev_pr_lock`."""
    p_dir = resolve_state_dir(state_dir, home_dir=home_dir)
    entries: list[dict[str, Any]] = []

    lock_file_obj = atomic_read_json(p_dir / "dev_pr_lock.json", default={})
    if isinstance(lock_file_obj, dict):
        raw_nested = lock_file_obj.get("dev_pr_locks") or lock_file_obj.get("locks")
        if isinstance(raw_nested, dict):
            entries.extend(v for v in raw_nested.values() if isinstance(v, dict))
        elif lock_file_obj.get("active"):
            entries.append(lock_file_obj)

    state_obj = atomic_read_json(p_dir / "actions_state.json", default={})
    if isinstance(state_obj, dict):
        audit_state = state_obj.get("actions_log_audit_state")
        if isinstance(audit_state, dict):
            raw_locks = audit_state.get("dev_pr_locks")
            if isinstance(raw_locks, dict):
                entries.extend(v for v in raw_locks.values() if isinstance(v, dict))
            legacy = audit_state.get("dev_pr_lock")
            if isinstance(legacy, dict):
                entries.append(legacy)

    if not entries:
        return set(), set()

    base_home = str((Path(home_dir).expanduser().resolve() if home_dir is not None else Path.home()))
    active_branches: set[str] = set()
    active_worktrees: set[str] = set()

    def _record_branch_and_wt(item: dict[str, Any]) -> None:
        br = str(item.get("branch_name") or "").strip().removeprefix("refs/heads/")
        if br:
            active_branches.add(br)
        raw_wt = str(item.get("worktree_path") or "").strip()
        if raw_wt:
            with contextlib.suppress(OSError):
                active_worktrees.add(str(Path(raw_wt.replace("$HOME", base_home)).expanduser().resolve()))

    for entry in entries:
        if not bool(entry.get("active")):
            continue
        _record_branch_and_wt(entry)
        slots_map = entry.get("slots")
        if isinstance(slots_map, dict):
            for slot_item in slots_map.values():
                if isinstance(slot_item, dict) and bool(slot_item.get("active", True)):
                    _record_branch_and_wt(slot_item)
        rb_map = entry.get("repo_branches")
        if isinstance(rb_map, dict):
            for rb_item in rb_map.values():
                if isinstance(rb_item, dict):
                    _record_branch_and_wt(rb_item)
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

        paused_devtools_deny = self._check_paused_chrome_devtools(tool_name, args)
        if paused_devtools_deny is not None:
            return paused_devtools_deny

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
                        "Guardrail violation (Santa Lockdown): piping remote scripts to shell/interpreters, "
                        "downloading standalone executable binaries (releases/download + chmod +x), or invoking "
                        "unapproved binary managers ('mise', 'uv tool/python install', 'cargo binstall') is blocked. "
                        "Manage runtimes declaratively via 'asdf' (.tool-versions), pure-Python pyproject.toml, "
                        "and OS-signed utilities."
                    ),
                }

            unvalidated_push_deny = self._check_unvalidated_branch_push(cmd_line, payload, args)
            if unvalidated_push_deny is not None:
                return unvalidated_push_deny

            deploy_home_deny = self._check_parallel_worker_deploy_home(cmd_line, payload, args)
            if deploy_home_deny is not None:
                return deploy_home_deny

            shell_lease_deny = self._check_parallel_shell_redirect_leases(cmd_line, payload, args)
            if shell_lease_deny is not None:
                return shell_lease_deny

        elif tool_name in ("write_file", "edit_file", "replace_file_content", "multi_replace_file_content"):
            raw_target = str(args.get("Path") or args.get("TargetFile") or args.get("path") or "").strip()
            if raw_target:
                expanded = Path(raw_target.replace("$HOME", str(self.home_dir))).expanduser()
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
                lease_deny = self._check_parallel_file_lease_path(raw_target, payload, args)
                if lease_deny is not None:
                    return lease_deny

        return {"decision": "allow"}

    def _is_orchestration_paused(self) -> bool:
        """Read `<state_dir>/dev_orchestration_control.json` in <0.1ms to check Pause state."""
        ctrl_path = self.state_dir / "dev_orchestration_control.json"
        if not ctrl_path.is_file():
            return False
        raw = atomic_read_json(ctrl_path, default={})
        return isinstance(raw, dict) and bool(raw.get("paused"))

    def _check_paused_chrome_devtools(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """Deny Chrome DevTools MCP calls and `chrome-devtools-mcp` commands while Dev Queue is paused."""
        if not self._is_orchestration_paused():
            return None
        deny_payload = {
            "decision": "deny",
            "reason": (
                "Guardrail (Dev Queue Paused): Chrome DevTools automation is turned off while Dev Queue "
                "orchestration is paused. Click 'Continue Dev' in the Dev Queue sidebar/panel or run "
                "'agy-queue resume' to re-enable Chrome DevTools."
            ),
        }
        if _CHROME_DEVTOOLS_TOOL_OR_SERVER_RE.search(tool_name):
            return deny_payload
        if tool_name == "call_mcp_tool":
            server_name = str(args.get("ServerName") or args.get("server_name") or args.get("serverName") or "")
            if _CHROME_DEVTOOLS_TOOL_OR_SERVER_RE.search(server_name):
                return deny_payload
        if tool_name == "run_command":
            cmd_line = str(args.get("CommandLine") or args.get("command") or "")
            if _CHROME_DEVTOOLS_CMD_RE.search(cmd_line):
                return deny_payload
        return None

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

    @staticmethod
    def _extract_active_slots_from_lock(lock_data: dict[str, Any]) -> list[dict[str, Any]]:
        """Return ordered active slot dicts (`slot_id` 1, 2, ...) from multi-slot or legacy flat `lock_data`."""
        if not isinstance(lock_data, dict) or not bool(lock_data.get("active")):
            return []
        raw_slots = lock_data.get("slots")
        collected: dict[int, dict[str, Any]] = {}
        if isinstance(raw_slots, dict):
            for raw_k, raw_v in raw_slots.items():
                if not isinstance(raw_v, dict) or not bool(raw_v.get("active", True)):
                    continue
                try:
                    s_id = int(raw_v.get("slot_id") or raw_k)
                except (TypeError, ValueError):
                    continue
                if s_id < 1:
                    continue
                merged = dict(raw_v)
                merged["slot_id"] = s_id
                merged.setdefault("target_repo_dir", str(lock_data.get("target_repo_dir") or ""))
                collected[s_id] = merged
        if 1 not in collected and (
            not isinstance(raw_slots, dict)
            or not raw_slots
            or str(lock_data.get("branch_name") or "").strip()
            or str(lock_data.get("conversation_id") or "").strip()
        ):
            top_slot = {k: v for k, v in lock_data.items() if k != "slots"}
            top_slot["slot_id"] = int(lock_data.get("slot_id") or 1)
            collected.setdefault(top_slot["slot_id"], top_slot)
        return [collected[k] for k in sorted(collected)]

    @staticmethod
    def _cid_matches_slot(cid: str, slot_data: dict[str, Any]) -> bool:
        """Return True when `cid` matches a slot's `conversation_id`, `worker_cid`, or `sibling_conversation_ids`."""
        clean = str(cid or "").strip().lower()
        if not clean:
            return False
        cands = [
            str(slot_data.get("conversation_id") or "").strip().lower(),
            str(slot_data.get("worker_cid") or "").strip().lower(),
            *(str(s or "").strip().lower() for s in (slot_data.get("sibling_conversation_ids") or [])),
        ]
        for cand in cands:
            if not cand:
                continue
            if clean == cand:
                return True
            if len(clean) >= 8 and len(cand) >= 8 and clean[:8] == cand[:8] and (
                clean.startswith(cand) or cand.startswith(clean)
            ):
                return True
        return False

    def _resolve_expanded_path(self, raw_path: str, base_dir: Path | None = None) -> Path | None:
        """Expand `$HOME` / `~` and resolve `raw_path` (relative to `base_dir` if relative)."""
        cleaned = str(raw_path or "").strip()
        if not cleaned:
            return None
        expanded = Path(cleaned.replace("$HOME", str(self.home_dir))).expanduser()
        if not expanded.is_absolute():
            if base_dir is None:
                return None
            expanded = base_dir / expanded
        with contextlib.suppress(OSError):
            return expanded.resolve()
        return expanded

    @staticmethod
    def _path_is_within(child: Path, parent: Path) -> bool:
        """Return True when `child` equals `parent` or resides inside `parent`."""
        return child == parent or parent in child.parents

    def _is_parallel_worktree_dir(self, cand: Path | None, active_worktrees: set[Path] | None = None) -> bool:
        """Return True when `cand` is inside an isolated `pr-worktree-*` or active lock `worktree_path`."""
        if cand is None:
            return False
        resolved = cand
        with contextlib.suppress(OSError):
            resolved = cand.resolve()
        if any(part.startswith("pr-worktree-") for part in resolved.parts):
            return True
        if active_worktrees:
            for wt in active_worktrees:
                if self._path_is_within(resolved, wt):
                    return True
        return False

    def _check_parallel_worker_deploy_home(
        self,
        cmd_line: str,
        payload: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Block `install.sh` / `deploy-home` inside `pr-worktree-*` directories (`pretool:parallel-worker-deploy-home`)."""
        if (
            "install.sh" not in cmd_line
            and "deploy-home" not in cmd_line
            and "deploy_repo_to_home" not in cmd_line
        ):
            return None

        active_worktrees: set[Path] = set()
        active_map = self._read_active_locks_map()
        for ldata in active_map.values():
            main_td = self._resolve_expanded_path(str(ldata.get("target_repo_dir") or ""))
            for slot in self._extract_active_slots_from_lock(ldata):
                wt_p = self._resolve_expanded_path(str(slot.get("worktree_path") or ""))
                if wt_p is not None and (main_td is None or wt_p != main_td):
                    active_worktrees.add(wt_p)

        raw_cwd = str(args.get("Cwd") or args.get("cwd") or payload.get("cwd") or "").strip()
        if not raw_cwd:
            ws_paths = payload.get("workspacePaths")
            if isinstance(ws_paths, list) and len(ws_paths) == 1 and str(ws_paths[0] or "").strip():
                raw_cwd = str(ws_paths[0]).strip()
        current_dir = self._resolve_expanded_path(raw_cwd) if raw_cwd else None

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

            if tokens[0] == "cd" and len(tokens) >= 2:
                next_dir = self._resolve_expanded_path(tokens[1], base_dir=current_dir)
                if next_dir is not None:
                    current_dir = next_dir
                continue

            idx = 0
            if idx < len(tokens) and tokens[idx] == "env":
                idx += 1
            while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
                idx += 1
            if idx >= len(tokens):
                continue

            cmd_tokens = tokens[idx:]
            exe_raw = cmd_tokens[0]
            exe_base = Path(exe_raw).name
            is_deploy_invocation = False
            explicit_target_dirs: list[Path] = []

            if exe_base == "install.sh":
                is_deploy_invocation = True
                script_p = self._resolve_expanded_path(exe_raw, base_dir=current_dir)
                if script_p is not None:
                    explicit_target_dirs.append(script_p.parent)
            elif exe_base in ("sh", "bash", "zsh", "dash", "ksh"):
                for tok in cmd_tokens[1:]:
                    if tok.startswith("-"):
                        continue
                    if Path(tok).name == "install.sh":
                        is_deploy_invocation = True
                        script_p = self._resolve_expanded_path(tok, base_dir=current_dir)
                        if script_p is not None:
                            explicit_target_dirs.append(script_p.parent)
                    break
            elif exe_base.startswith("python") or exe_base == "git_client.py":
                has_git_client = exe_base == "git_client.py" or any(
                    Path(t).name == "git_client.py" for t in cmd_tokens[1:]
                )
                if has_git_client and "deploy-home" in cmd_tokens[1:]:
                    is_deploy_invocation = True
                elif "-c" in cmd_tokens[1:] and "deploy_repo_to_home" in seg_clean:
                    is_deploy_invocation = True

            if not is_deploy_invocation:
                continue

            for t_idx, tok in enumerate(cmd_tokens):
                if tok in ("--workdir", "--repo-dir", "-C") and t_idx + 1 < len(cmd_tokens):
                    opt_dir = self._resolve_expanded_path(cmd_tokens[t_idx + 1], base_dir=current_dir)
                    if opt_dir is not None:
                        explicit_target_dirs.append(opt_dir)
                elif tok.startswith(("--workdir=", "--repo-dir=")):
                    opt_val = tok.split("=", 1)[1]
                    opt_dir = self._resolve_expanded_path(opt_val, base_dir=current_dir)
                    if opt_dir is not None:
                        explicit_target_dirs.append(opt_dir)

            dirs_to_check = [*explicit_target_dirs]
            if current_dir is not None:
                dirs_to_check.append(current_dir)
            for cand_dir in dirs_to_check:
                if self._is_parallel_worktree_dir(cand_dir, active_worktrees):
                    return {
                        "decision": "deny",
                        "check_id": "pretool:parallel-worker-deploy-home",
                        "reason": (
                            f"Guardrail violation (Parallel Worktree Deploy Protection — pretool:parallel-worker-deploy-home): "
                            f"running './install.sh' or 'deploy-home' from parallel worktree '{cand_dir.name}' is blocked "
                            f"so concurrent workers never race live $HOME plugin deployment. Deploy to $HOME exclusively "
                            f"from the primary main checkout after PR merge."
                        ),
                    }
        return None

    @staticmethod
    def _normalize_leased_rel_path(
        raw_spec: str,
        repo_slug: str = "",
        repo_roots: Sequence[Path] = (),
        home_dir: Path | None = None,
    ) -> str:
        """Normalize a slot `target_files` entry into a clean repo-relative POSIX path (empty if foreign repo)."""
        text = str(raw_spec or "").strip()
        if not text or text.startswith("#"):
            return ""
        if not text.startswith((".", "/", "~", "$")):
            m_col = _LEASE_SINGLE_COLON_RE.search(text)
            if m_col:
                pref = text[: m_col.start()].strip()
                rest = text[m_col.end() :].strip()
                if (
                    pref
                    and rest
                    and not rest.startswith("//")
                    and not _LEASE_LINE_RANGE_RE.match(rest)
                    and _LEASE_REPO_PREFIX_RE.match(pref)
                    and not any(pref.lower().endswith(ext) for ext in (".py", ".js", ".html", ".css", ".sh", ".md", ".json", ".yaml", ".yml", ".toml", ".bats"))
                ):
                    if repo_slug:
                        p_low = pref.strip("/").removesuffix(".git").lower()
                        s_low = repo_slug.strip("/").removesuffix(".git").lower()
                        match_ok = (p_low == s_low) if ("/" in p_low and "/" in s_low) else (p_low.rsplit("/", 1)[-1] == s_low.rsplit("/", 1)[-1])
                        if not match_ok:
                            return ""
                    text = rest
        if "::" in text:
            text = text.split("::", 1)[0]
        elif "#" in text:
            text = text.split("#", 1)[0]
        elif ":" in text:
            left, right = text.rsplit(":", 1)
            if left.strip() and _LEASE_LINE_RANGE_RE.match(right.strip()):
                text = left
        cleaned = text.strip().replace("\\", "/")
        if not cleaned:
            return ""
        if cleaned.startswith(("/", "~", "$HOME")):
            base_h = str(home_dir) if home_dir is not None else str(Path.home())
            cand_p = Path(cleaned.replace("$HOME", base_h)).expanduser()
            with contextlib.suppress(OSError):
                cand_p = cand_p.resolve()
            for root in repo_roots:
                with contextlib.suppress(ValueError):
                    return cand_p.relative_to(root).as_posix()
            return ""
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned.strip("/")

    @staticmethod
    def _is_monolith_target_file(rel_path: str, candidate_roots: Sequence[Path]) -> bool:
        """Return True (`<0.5ms`) when `rel_path` is a known monolith basename or `>= 250` lines on disk."""
        if not rel_path:
            return False
        if Path(rel_path).name in PARALLEL_KNOWN_MONOLITH_BASENAMES:
            return True
        for root in candidate_roots:
            fpath = root / rel_path
            if fpath.is_file():
                with contextlib.suppress(OSError):
                    data = fpath.read_bytes()
                    line_count = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
                    return line_count >= PARALLEL_MONOLITH_MIN_LINES
        return False

    def _check_parallel_file_lease_path(
        self,
        raw_target: str,
        payload: dict[str, Any],
        args: dict[str, Any],
        *,
        effective_cwd: Path | None = None,
    ) -> dict[str, Any] | None:
        """Block writes to non-monolith files leased (`target_files`) by a sibling active slot (`pretool:parallel-file-lease`)."""
        clean_raw = str(raw_target or "").strip()
        if not clean_raw or clean_raw.startswith("/dev/"):
            return None

        active_map = self._read_active_locks_map()
        if not active_map:
            return None

        base_cwd = effective_cwd
        if base_cwd is None:
            raw_cwd = str(args.get("Cwd") or args.get("cwd") or payload.get("cwd") or "").strip()
            if raw_cwd:
                base_cwd = self._resolve_expanded_path(raw_cwd)
        target_abs = self._resolve_expanded_path(clean_raw, base_dir=base_cwd)
        if target_abs is None:
            return None

        caller_cid = str(
            payload.get("conversationId")
            or payload.get("conversation_id")
            or payload.get("worker_cid")
            or args.get("conversation_id")
            or ""
        ).strip()

        for slug, lock_data in active_map.items():
            active_slots = self._extract_active_slots_from_lock(lock_data)
            if not active_slots:
                continue

            slot_wts: dict[int, Path] = {}
            repo_roots: list[Path] = []
            for slot in active_slots:
                s_id = int(slot.get("slot_id") or 1)
                wt_p = self._resolve_expanded_path(str(slot.get("worktree_path") or ""))
                if wt_p is not None:
                    slot_wts[s_id] = wt_p
                    if wt_p not in repo_roots:
                        repo_roots.append(wt_p)
            for td_raw in (
                str(lock_data.get("target_repo_dir") or ""),
                *(str(s.get("target_repo_dir") or "") for s in active_slots),
            ):
                td_p = self._resolve_expanded_path(td_raw)
                if td_p is not None and td_p not in repo_roots:
                    repo_roots.append(td_p)

            rel_path = ""
            target_owner_slot: int | None = None
            for s_id, wt_p in slot_wts.items():
                if self._path_is_within(target_abs, wt_p):
                    with contextlib.suppress(ValueError):
                        rel_path = target_abs.relative_to(wt_p).as_posix()
                        target_owner_slot = s_id
                    break
            if not rel_path:
                for root_p in repo_roots:
                    if self._path_is_within(target_abs, root_p):
                        with contextlib.suppress(ValueError):
                            rel_path = target_abs.relative_to(root_p).as_posix()
                        break
            if not rel_path:
                continue

            caller_slot_id: int | None = None
            if caller_cid:
                for slot in active_slots:
                    if self._cid_matches_slot(caller_cid, slot):
                        caller_slot_id = int(slot["slot_id"])
                        break
            if caller_slot_id is None and base_cwd is not None:
                for s_id, wt_p in slot_wts.items():
                    if self._path_is_within(base_cwd, wt_p):
                        caller_slot_id = s_id
                        break
            if caller_slot_id is None and target_owner_slot is not None:
                caller_slot_id = target_owner_slot

            if caller_slot_id is None and len(active_slots) < 2 and not caller_cid:
                continue

            for sib_slot in active_slots:
                sib_id = int(sib_slot.get("slot_id") or 1)
                if caller_slot_id is not None and sib_id == caller_slot_id:
                    continue
                if caller_slot_id is None and caller_cid and self._cid_matches_slot(caller_cid, sib_slot):
                    continue
                sib_targets = sib_slot.get("target_files") or []
                if not isinstance(sib_targets, (list, tuple, set)) or not sib_targets:
                    continue
                leased_paths = {
                    norm_p
                    for raw_t in sib_targets
                    for norm_p in [
                        self._normalize_leased_rel_path(
                            str(raw_t),
                            repo_slug=slug,
                            repo_roots=repo_roots,
                            home_dir=self.home_dir,
                        )
                    ]
                    if norm_p
                }
                if rel_path not in leased_paths:
                    continue
                if self._is_monolith_target_file(rel_path, repo_roots):
                    continue
                sib_br = str(sib_slot.get("branch_name") or "").strip()
                sib_cid = str(sib_slot.get("conversation_id") or "").strip()
                return {
                    "decision": "deny",
                    "check_id": "pretool:parallel-file-lease",
                    "reason": (
                        f"Guardrail violation (Parallel Multi-Slot File Lease — pretool:parallel-file-lease): "
                        f"write to non-monolith file '{rel_path}' in repo '{slug}' is blocked because it is "
                        f"currently leased in target_files by sibling Slot #{sib_id} "
                        f"(branch '{sib_br}', cid '{sib_cid[:8]}'). Restrict edits to files leased by your own "
                        f"slot or disjoint non-overlapping files."
                    ),
                }
        return None

    def _check_parallel_shell_redirect_leases(
        self,
        cmd_line: str,
        payload: dict[str, Any],
        args: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Check shell output redirects (`>`, `>>`, `>|`, `tee`) against sibling slot file leases."""
        if ">" not in cmd_line and "tee " not in cmd_line:
            return None
        raw_cwd = str(args.get("Cwd") or args.get("cwd") or payload.get("cwd") or "").strip()
        current_dir = self._resolve_expanded_path(raw_cwd) if raw_cwd else None
        for seg in re.split(r"(?:&&|\|\||[;|\n])", cmd_line):
            seg_clean = seg.strip()
            if not seg_clean:
                continue
            if seg_clean.startswith("cd "):
                with contextlib.suppress(ValueError):
                    toks = shlex.split(seg_clean)
                    if len(toks) >= 2:
                        nxt = self._resolve_expanded_path(toks[1], base_dir=current_dir)
                        if nxt is not None:
                            current_dir = nxt
                continue
            targets: list[str] = []
            for m_red in _SHELL_WRITE_REDIRECT_RE.finditer(seg_clean):
                cand = m_red.group(1) or m_red.group(2) or m_red.group(3) or ""
                if cand and not cand.startswith("/dev/"):
                    targets.append(cand)
            for m_tee in _SHELL_TEE_TARGET_RE.finditer(seg_clean):
                cand = m_tee.group(1) or m_tee.group(2) or m_tee.group(3) or ""
                if cand and not cand.startswith("/dev/"):
                    targets.append(cand)
            for t_raw in targets:
                deny = self._check_parallel_file_lease_path(t_raw, payload, args, effective_cwd=current_dir)
                if deny is not None:
                    return deny
        return None

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

        unvalidated_locks: list[tuple[str, dict[str, Any]]] = []
        for slug, ldata in active_map.items():
            for slot_entry in self._extract_active_slots_from_lock(ldata):
                if str(slot_entry.get("branch_name") or "").strip() and not self._is_lock_pr_gate_ready(slot_entry):
                    unvalidated_locks.append((slug, slot_entry))
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
                    elif not eff_branch and eff_dir_resolved is not None:
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

        def _format_repo_lock_notice(slug_key: str, lock_data: dict[str, Any], *, with_workdir_flag: bool) -> str:
            active_slots = self._extract_active_slots_from_lock(lock_data)
            if not active_slots:
                return ""
            try:
                raw_max = int(lock_data.get("max_slots") or 2)
            except (TypeError, ValueError):
                raw_max = 2
            max_slots = max(len(active_slots), raw_max, 2)
            occ_txt = f"{len(active_slots)}/{max_slots} slots active"

            owned_slot = next((s for s in active_slots if current_cid and self._cid_matches_slot(current_cid, s)), None)
            for s_entry in active_slots:
                _append_unready_warning(slug_key, s_entry)

            repo_lbl = f"repo '{slug_key}'" if with_workdir_flag else f"'{slug_key}'"
            reuse_hint = "--reuse-active --workdir ..." if with_workdir_flag else "--reuse-active"
            if owned_slot is not None:
                o_br = str(owned_slot.get("branch_name") or "").strip()
                o_cid = str(owned_slot.get("conversation_id") or current_cid).strip()
                o_sid = int(owned_slot.get("slot_id") or 1)
                if not o_br:
                    return ""
                sib_bits = [
                    f"Slot #{int(s.get('slot_id') or 1)} '{s.get('branch_name')}' (cid {str(s.get('conversation_id') or '')[:8]})"
                    for s in active_slots
                    if int(s.get("slot_id") or 1) != o_sid and str(s.get("branch_name") or "").strip()
                ]
                sib_suffix = f" Concurrent sibling {', '.join(sib_bits)}." if sib_bits else ""
                return (
                    f"[Antigravity Dev Guardrail] Active PR Development Lock (`dev_pr_lock`, {occ_txt}, Slot #{o_sid}) "
                    f"for {repo_lbl} owned by this conversation ({o_cid[:8]}) on branch '{o_br}'. "
                    f"Reuse this branch ({reuse_hint}) for follow-up commits.{sib_suffix}"
                )

            primary_slot = active_slots[0]
            branch_name = str(primary_slot.get("branch_name") or lock_data.get("branch_name") or "").strip()
            owner_cid = str(primary_slot.get("conversation_id") or lock_data.get("conversation_id") or "").strip()
            if not branch_name:
                return ""
            extra_slots = [
                f"Slot #{int(s.get('slot_id') or 2)} '{s.get('branch_name')}' (cid {str(s.get('conversation_id') or '')[:8]})"
                for s in active_slots[1:]
                if str(s.get("branch_name") or "").strip()
            ]
            extra_txt = f" (+ {', '.join(extra_slots)})" if extra_slots else ""
            slot2_hint = (
                " (parallel Slot #2 available via --allow-slot2 when blast-radius overlap is NONE/TRANSITIVE)"
                if len(active_slots) < max_slots
                else " (all parallel slots occupied)"
            )
            return (
                f"[Antigravity Dev Guardrail] Active PR Development Lock (`dev_pr_lock`, {occ_txt}) for {repo_lbl} "
                f"held by conversation '{owner_cid[:8]}' on branch '{branch_name}'{extra_txt}{slot2_hint}. "
                f"Do not start a new branch or modify tracked repository files in '{slug_key}' from another conversation "
                f"until that branch is merged into main or evaluated."
            )

        if workspace_repos:
            matched_repos = [r for r in workspace_repos if r in active_map]
            free_repos = [r for r in workspace_repos if r not in active_map]
            for r in matched_repos:
                notice = _format_repo_lock_notice(r, active_map[r], with_workdir_flag=True)
                if notice:
                    messages.append(notice)
            for slug, lock_data in active_map.items():
                if slug not in workspace_repos:
                    active_slots = self._extract_active_slots_from_lock(lock_data)
                    max_slots = max(len(active_slots), int(lock_data.get("max_slots") or 2), 2)
                    if active_slots and str(active_slots[0].get("branch_name") or lock_data.get("branch_name") or "").strip():
                        br0 = str(active_slots[0].get("branch_name") or lock_data.get("branch_name") or "")
                        cid0 = str(active_slots[0].get("conversation_id") or lock_data.get("conversation_id") or "")[:8]
                        other_locked.append(f"{slug} [{len(active_slots)}/{max_slots}]: '{br0}' (cid {cid0})")
                    for s_entry in active_slots:
                        _append_unready_warning(slug, s_entry)
            if free_repos and other_locked:
                messages.append(
                    f"[Antigravity Dev Guardrail] Active workspace repository {', '.join(free_repos)} is UNLOCKED "
                    f"(safe to branch cleanly). Concurrent locks exist on sibling repositories: {'; '.join(other_locked)}."
                )
        else:
            for slug, lock_data in active_map.items():
                notice = _format_repo_lock_notice(slug, lock_data, with_workdir_flag=False)
                if notice:
                    messages.append(notice)

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


def _load_model_quota_module() -> Any:
    """Import sibling `model_quota.py` reliably whether `dev_common` was loaded via `sys.path` or `spec_from_file_location`."""
    clients_dir = str(Path(__file__).resolve().parent)
    if clients_dir not in sys.path:
        sys.path.insert(0, clients_dir)
    existing = sys.modules.get("model_quota")
    if existing is not None:
        return existing
    import model_quota as _mq  # noqa: PLC0415

    return _mq


_mq = _load_model_quota_module()
MODEL_QUOTA_COOLDOWNS_FILENAME = _mq.MODEL_QUOTA_COOLDOWNS_FILENAME
DEFAULT_DEV_MODEL = _mq.DEFAULT_DEV_MODEL
DEFAULT_REVIEWER_MODEL = _mq.DEFAULT_REVIEWER_MODEL
DEFAULT_ORCHESTRATOR_MODEL = _mq.DEFAULT_ORCHESTRATOR_MODEL
DEFAULT_ROUTING_STRATEGY = _mq.DEFAULT_ROUTING_STRATEGY
DEFAULT_LOW_QUOTA_THRESHOLD_PCT = _mq.DEFAULT_LOW_QUOTA_THRESHOLD_PCT
DEFAULT_QUOTA_SNAPSHOT_TTL_SECONDS = _mq.DEFAULT_QUOTA_SNAPSHOT_TTL_SECONDS
DEFAULT_MODEL_FALLBACK_ORDER = _mq.DEFAULT_MODEL_FALLBACK_ORDER
DEFAULT_DEV_ECO_FALLBACK_ORDER = _mq.DEFAULT_DEV_ECO_FALLBACK_ORDER
DEFAULT_REVIEWER_MODEL_FALLBACK_ORDER = _mq.DEFAULT_REVIEWER_MODEL_FALLBACK_ORDER
parse_model_list = _mq.parse_model_list
parse_quota_percent = _mq.parse_quota_percent
get_model_family = _mq.get_model_family
get_model_broad_family = _mq.get_model_broad_family
classify_change_risk_tier = _mq.classify_change_risk_tier
resolve_low_quota_threshold_pct = _mq.resolve_low_quota_threshold_pct
resolve_model_fallback_config = _mq.resolve_model_fallback_config
load_model_quota_state = _mq.load_model_quota_state
record_model_quota_cooldown = _mq.record_model_quota_cooldown
record_swarm_quota_snapshot = _mq.record_swarm_quota_snapshot
map_model_to_subagent_tier = _mq.map_model_to_subagent_tier
select_next_optimal_model = _mq.select_next_optimal_model
parse_reset_duration_seconds = _mq.parse_reset_duration_seconds
detect_quota_exhaustion_in_lines = _mq.detect_quota_exhaustion_in_lines


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


AGY_QUEUE_SUBCOMMANDS: tuple[str, ...] = (
    "watch",
    "doctor",
    "optimize",
    "sync",
    "list",
    "show",
    "locks",
    "set-status",
)


def _silence_broken_pipe() -> int:
    """Redirect stdout fd to `/dev/null` when a downstream pipe reader closes early (`SIGPIPE` / `BrokenPipeError`)."""
    with contextlib.suppress(OSError, ValueError):
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull_fd, sys.stdout.fileno())
        finally:
            os.close(devnull_fd)
    return 0


def run_dev_queue_tui_cli(argv: list[str] | None = None) -> int:
    """Console entrypoint (`agy-queue`) delegating unified subcommands and dashboard flags to `dev_queue_tui.py`."""
    forwarded = list(sys.argv[1:] if argv is None else argv)
    if forwarded and forwarded[0] == "doctor":
        return run_dev_queue_doctor_cli(forwarded[1:])
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
            try:
                return int(mod.main(forwarded))
            except BrokenPipeError:
                return _silence_broken_pipe()
    sys.stderr.write("[agy-queue] Error: dev_queue_tui.py not found. Run ./install.sh to deploy agy-dev-plugin.\n")
    return 1


def run_dev_queue_doctor_cli(argv: list[str] | None = None) -> int:
    """Console entrypoint (`agy-doctor`) forwarding to `pr_remediator.py queue-doctor`."""
    forwarded = list(sys.argv[1:] if argv is None else argv)
    candidates = (
        resolve_plugin_root() / "skills" / "agy-code-remediation-pr" / "scripts" / "pr_remediator.py",
        Path.home()
        / ".gemini"
        / "config"
        / "plugins"
        / PLUGIN_NAME
        / "skills"
        / "agy-code-remediation-pr"
        / "scripts"
        / "pr_remediator.py",
    )
    for script_path in candidates:
        mod = _load_optional_module(script_path, "pr_remediator")
        if mod is not None and hasattr(mod, "main"):
            try:
                return int(mod.main(["queue-doctor", *forwarded]))
            except BrokenPipeError:
                return _silence_broken_pipe()
    sys.stderr.write("[agy-doctor] Error: pr_remediator.py not found. Run ./install.sh to deploy agy-dev-plugin.\n")
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
