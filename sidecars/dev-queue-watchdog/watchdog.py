#!/usr/bin/env python3
"""Zero-LLM Background Watchdog Sidecar for `agy-dev-plugin` (`watchdog.py`).

Pure Python 3 stdlib daemon (Santa Lockdown compliant, Unix portable, Zero-PII) that:
  1. Polls `dev_queue_doctor.diagnose_orchestrator()` every 5 minutes (default `300s`,
     clamped `>= 30s`) with **0 LLM tokens** when the queue and locks are healthy.
  2. Runs safe runtime reconciliations (`--fix`) and cooldown/live-PID-gated hybrid
     `agy-dev-orchestrator` recovery (`--wake` via `agentapi` when `ANTIGRAVITY_LS_ADDRESS`
     is set, or headless `agy` CLI fallback) exclusively when actionable anomalies exist.
  3. Shuts down cleanly on `SIGTERM` / `SIGINT` via `threading.Event().wait(interval)`
     and isolates per-cycle exceptions so transient filesystem/Git glitches never crash the sidecar.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
from typing import Any

_SIDECAR_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SIDECAR_DIR.parents[1]
_INSTALLED_PLUGIN_ROOT = Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin"

for _cand_root in (_PLUGIN_ROOT, _INSTALLED_PLUGIN_ROOT):
    for _rel in (Path("clients"), Path("skills") / "agy-code-remediation-pr" / "scripts"):
        _cand = _cand_root / _rel
        if _cand.is_dir():
            _cand_str = str(_cand)
            if _cand_str not in sys.path:
                sys.path.insert(0, _cand_str)

import dev_queue_doctor  # noqa: E402

DEFAULT_WATCHDOG_INTERVAL_SECONDS = 300
MIN_WATCHDOG_INTERVAL_SECONDS = 30
DEFAULT_WATCHDOG_COOLDOWN_SECONDS = 900
_LOCAL_HOME_PATH_RE = re.compile(r"/(?:Users|home)/(?!testuser\b|test\b|Shared\b)[A-Za-z0-9._-]+")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sanitize_path(text: str) -> str:
    """Replace local user home paths with `$HOME` to preserve Zero-PII logs and JSON output."""
    if not text:
        return ""
    cleaned = str(text)
    with contextlib.suppress(OSError):
        home_str = str(Path.home().resolve())
        if home_str and home_str != "/":
            cleaned = cleaned.replace(home_str, "$HOME")
    return _LOCAL_HOME_PATH_RE.sub("$HOME", cleaned)


def resolve_watchdog_interval(
    interval_seconds: int | float | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve watchdog poll interval in seconds (default `AGY_WATCHDOG_INTERVAL_SECONDS` or `300`, clamped `>= 30`)."""
    eff_env = os.environ if env is None else env
    if interval_seconds is not None:
        with contextlib.suppress(TypeError, ValueError):
            return max(MIN_WATCHDOG_INTERVAL_SECONDS, int(round(float(interval_seconds))))
    raw_env = str(eff_env.get("AGY_WATCHDOG_INTERVAL_SECONDS") or "").strip()
    if raw_env:
        with contextlib.suppress(TypeError, ValueError):
            return max(MIN_WATCHDOG_INTERVAL_SECONDS, int(round(float(raw_env))))
    return DEFAULT_WATCHDOG_INTERVAL_SECONDS


def run_watchdog_cycle(
    *,
    state_dir: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    repos_dir: Path | str | None = None,
    sync_git: bool = True,
    cooldown_seconds: int = DEFAULT_WATCHDOG_COOLDOWN_SECONDS,
    dry_run: bool = False,
    now_ts: float | None = None,
    home_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    popen_fn: Callable[..., Any] | None = None,
    pid_alive_fn: Callable[[int], bool] | None = None,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    git_client: Any | None = None,
) -> dict[str, Any]:
    """Execute one 0-LLM watchdog cycle: fast read-only diagnosis first, then targeted fix/wake only if required."""
    diag = dev_queue_doctor.diagnose_orchestrator(
        state_dir=state_dir,
        repos_dir=repos_dir,
        brain_dir=brain_dir,
        home_dir=home_dir,
        sync_git=False,
        now_ts=now_ts,
        ps_output=ps_output,
        ps_runner=ps_runner,
        git_client=git_client,
    )

    if not bool(diag.get("action_required")):
        return {
            "status": "healthy",
            "overall_status": str(diag.get("overall_status") or "healthy"),
            "action_required": False,
            "healed": False,
            "woke": False,
            "checked_at": str(diag.get("generated_at") or _utc_now_iso()),
            "health_score": int(diag.get("health_score") or 100),
            "anomalies": [],
            "queue_summary": dict(diag.get("queue_summary") or {}),
        }

    needs_fix = bool(diag.get("needs_fix"))
    needs_wake = bool(diag.get("needs_wake"))
    doc_res = dev_queue_doctor.run_queue_doctor(
        state_dir=state_dir,
        repos_dir=repos_dir,
        brain_dir=brain_dir,
        sync_git=sync_git,
        fix=needs_fix,
        wake=needs_wake,
        cooldown_seconds=cooldown_seconds,
        dry_run=dry_run,
        now_ts=now_ts,
        ps_output=ps_output,
        ps_runner=ps_runner,
        git_client=git_client,
        home_dir=home_dir,
        env=env,
        which_fn=which_fn,
        popen_fn=popen_fn,
        pid_alive_fn=pid_alive_fn,
    )

    wake_info = doc_res.get("wake") if isinstance(doc_res.get("wake"), dict) else {}
    woke = bool(wake_info.get("woke"))
    rem_actions = [r for r in (doc_res.get("remediation_actions") or []) if isinstance(r, dict)]
    healed = bool(needs_fix and not dry_run and any(r.get("status") == "applied" for r in rem_actions))

    wake_status = str(wake_info.get("status") or "")
    if dry_run:
        cycle_status = "dry_run"
    elif woke:
        cycle_status = "woke"
    elif healed:
        cycle_status = "healed"
    elif wake_status in ("skipped_cooldown", "skipped_wake_in_flight"):
        cycle_status = "cooldown"
    else:
        cycle_status = str(doc_res.get("overall_status") or "degraded")

    return {
        "status": cycle_status,
        "overall_status": str(doc_res.get("overall_status") or diag.get("overall_status") or "degraded"),
        "initial_overall_status": str(diag.get("overall_status") or "degraded"),
        "action_required": True,
        "needs_fix": needs_fix,
        "needs_wake": needs_wake,
        "healed": healed,
        "woke": woke,
        "checked_at": str(doc_res.get("generated_at") or diag.get("generated_at") or _utc_now_iso()),
        "health_score": int(doc_res.get("health_score") or 100),
        "initial_anomalies": list(diag.get("anomalies") or []),
        "anomalies": list(doc_res.get("anomalies") or []),
        "remediation_actions": rem_actions,
        "wake": wake_info,
    }


def _format_cycle_log_line(result: dict[str, Any]) -> str:
    """Format a single-line Zero-PII summary for sidecar stdout."""
    ts = str(result.get("checked_at") or _utc_now_iso())
    st = str(result.get("status") or "unknown")
    healed = bool(result.get("healed"))
    woke = bool(result.get("woke"))
    wake_info = result.get("wake") if isinstance(result.get("wake"), dict) else {}
    transport = str(wake_info.get("transport") or "")
    extra = f" transport={transport}" if transport else ""
    if result.get("error"):
        extra += f" error={result['error']}"
    return f"[{ts}] [dev-queue-watchdog] status={st} healed={healed} woke={woke}{extra}"


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser for `watchdog.py`."""
    parser = argparse.ArgumentParser(
        prog="watchdog",
        description="Zero-LLM background watchdog sidecar for dev_queue.json and dev_pr_locks.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"Poll interval in seconds (default: AGY_WATCHDOG_INTERVAL_SECONDS or {DEFAULT_WATCHDOG_INTERVAL_SECONDS}, clamped >= {MIN_WATCHDOG_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--cooldown-seconds",
        dest="cooldown_seconds",
        type=int,
        default=DEFAULT_WATCHDOG_COOLDOWN_SECONDS,
        help=f"Minimum seconds between orchestrator wake-ups (default: {DEFAULT_WATCHDOG_COOLDOWN_SECONDS}).",
    )
    parser.add_argument("--state-dir", dest="state_dir", default="", help="Optional state directory override.")
    parser.add_argument("--brain-dir", dest="brain_dir", default=None, help="Optional Antigravity brain directory override.")
    parser.add_argument("--repos-dir", dest="repos_dir", default="", help="Optional parent directory of managed git repositories.")
    parser.add_argument("--once", action="store_true", help="Run a single watchdog cycle and exit.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Preview remediation/wake actions without mutating state or spawning.")
    parser.add_argument("--no-sync-git", dest="no_sync_git", action="store_true", help="Disable git lock/queue sync during remediation.")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Emit JSON cycle result(s) to stdout.")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    stop_event: threading.Event | None = None,
) -> int:
    """CLI and sidecar daemon entrypoint for `watchdog.py`."""
    args = build_parser().parse_args(argv)
    interval = resolve_watchdog_interval(args.interval)
    ev = stop_event if stop_event is not None else threading.Event()

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

    state_override: Path | str | None = args.state_dir.strip() or None
    repos_override: Path | str | None = args.repos_dir.strip() or None
    sync_git = not bool(args.no_sync_git)

    try:
        while True:
            had_error = False
            try:
                res = run_watchdog_cycle(
                    state_dir=state_override,
                    brain_dir=args.brain_dir,
                    repos_dir=repos_override,
                    sync_git=sync_git,
                    cooldown_seconds=int(args.cooldown_seconds),
                    dry_run=bool(args.dry_run),
                )
            except Exception as exc:
                had_error = True
                res = {
                    "status": "error",
                    "action_required": False,
                    "healed": False,
                    "woke": False,
                    "checked_at": _utc_now_iso(),
                    "error": _sanitize_path(f"{exc.__class__.__name__}: {exc}"),
                }

            if args.as_json:
                print(json.dumps(res, indent=2 if args.once else None, ensure_ascii=False), flush=True)
            elif args.once or bool(res.get("action_required")) or had_error:
                print(_format_cycle_log_line(res), flush=True)

            if args.once:
                return 1 if had_error else 0

            if ev.wait(interval):
                break
        return 0
    finally:
        if installed_signals:
            with contextlib.suppress(ValueError, OSError):
                if prev_sigterm is not None:
                    signal.signal(signal.SIGTERM, prev_sigterm)
                if prev_sigint is not None:
                    signal.signal(signal.SIGINT, prev_sigint)


if __name__ == "__main__":
    raise SystemExit(main())
