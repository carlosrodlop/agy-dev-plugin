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
import time
from typing import Any

_SIDECAR_DIR = Path(__file__).resolve().parent
_PLUGIN_ROOT = _SIDECAR_DIR.parents[1]
_INSTALLED_PLUGIN_ROOT = Path.home() / ".gemini" / "config" / "plugins" / "agy-dev-plugin"

for _cand_root in (_INSTALLED_PLUGIN_ROOT, _PLUGIN_ROOT):
    for _rel in (Path("clients"), Path("skills") / "agy-code-remediation-pr" / "scripts"):
        _cand = _cand_root / _rel
        if _cand.is_dir():
            _cand_str = str(_cand)
            if _cand_str in sys.path:
                sys.path.remove(_cand_str)
            sys.path.insert(0, _cand_str)

import dev_common  # noqa: E402
import dev_queue_doctor  # noqa: E402
import lock_store  # noqa: E402
import merge_watcher  # noqa: E402
import orchestration_control as _orch_ctrl  # noqa: E402
import review_gate  # noqa: E402

DEFAULT_WATCHDOG_INTERVAL_SECONDS = 300
MIN_WATCHDOG_INTERVAL_SECONDS = 30
DEFAULT_WATCHDOG_COOLDOWN_SECONDS = 900
DEFAULT_CONTROL_SLICE_SECONDS = 3.0
DEFAULT_AUTO_CLEANUP_INTERVAL_SECONDS = 3600
AUTO_CLEANUP_STATE_FILENAME = "agy_auto_cleanup_state.json"
AUTO_CLEANUP_LOCK_FILENAME = "agy_auto_cleanup_state.lock"
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


def resolve_auto_cleanup_interval(
    interval_seconds: int | float | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve hourly auto-cleanup interval in seconds (`AGY_AUTO_CLEANUP_INTERVAL_SECONDS`, default `3600`, `0` disables)."""
    eff_env = os.environ if env is None else env
    if interval_seconds is not None:
        with contextlib.suppress(TypeError, ValueError):
            val = int(round(float(interval_seconds)))
            return 0 if val <= 0 else val
    raw_env = str(eff_env.get("AGY_AUTO_CLEANUP_INTERVAL_SECONDS") or "").strip()
    if raw_env:
        with contextlib.suppress(TypeError, ValueError):
            val = int(round(float(raw_env)))
            return 0 if val <= 0 else val
    return DEFAULT_AUTO_CLEANUP_INTERVAL_SECONDS


def _maybe_run_hourly_auto_cleanup(
    *,
    state_dir: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    repos_dir: Path | str | None = None,
    home_dir: Path | None = None,
    dry_run: bool = False,
    now_ts: float | None = None,
    env: Mapping[str, str] | None = None,
    git_client: Any | None = None,
    auto_cleanup_interval_seconds: int | float | None = None,
    ps_output: str | None = None,
    ps_runner: Callable[[], str] | None = None,
    conversation_client: Any | None = None,
    adhoc_days: float | None = None,
) -> dict[str, Any]:
    """Run cooldown-gated 0-LLM conversation/sidecar/orphan cleanup and auto-archive once per `AGY_AUTO_CLEANUP_INTERVAL_SECONDS`."""
    eff_env = os.environ if env is None else env
    interval = resolve_auto_cleanup_interval(auto_cleanup_interval_seconds, env=eff_env)
    if interval <= 0:
        return {"status": "disabled", "ran": False, "interval_seconds": 0}

    has_env_agy = bool(
        str(eff_env.get("AGY_ANTIGRAVITY_DIR") or "").strip()
        or str(eff_env.get("AGY_ANTIGRAVITY_CLI_DIR") or "").strip()
    )
    if (
        antigravity_dirs is None
        and brain_dir is None
        and home_dir is None
        and not has_env_agy
        and ("unittest" in sys.modules or "pytest" in sys.modules)
    ):
        return {"status": "skipped", "ran": False, "reason": "hermetic_test_guard"}

    cur_now = float(now_ts if now_ts is not None else time.time())
    st_dir = dev_common.resolve_state_dir(portfolio_dir=state_dir)
    st_dir.mkdir(parents=True, exist_ok=True)
    state_path = st_dir / AUTO_CLEANUP_STATE_FILENAME
    lock_path = st_dir / AUTO_CLEANUP_LOCK_FILENAME

    eff_agy_dirs: Sequence[Path | str] | None = antigravity_dirs
    eff_brain_dir: Path | str | None = None
    if eff_agy_dirs is None:
        if isinstance(brain_dir, Sequence) and not isinstance(brain_dir, (str, bytes)):
            derived = [Path(str(b)).expanduser().parent for b in brain_dir if str(b).strip()]
            eff_agy_dirs = derived or None
        elif brain_dir is not None:
            eff_brain_dir = brain_dir
        elif home_dir is not None:
            eff_agy_dirs = [
                Path(home_dir) / ".gemini" / "antigravity",
                Path(home_dir) / ".gemini" / "antigravity-cli",
            ]

    try:
        with dev_common.file_lock(lock_path):
            prev = dev_common.atomic_read_json(state_path, default={})
            last_ep_raw = prev.get("last_cleanup_epoch")
            if last_ep_raw is not None:
                with contextlib.suppress(TypeError, ValueError):
                    last_ep = float(last_ep_raw)
                    elapsed = cur_now - last_ep
                    if 0.0 <= elapsed < float(interval):
                        return {
                            "status": "cooldown",
                            "ran": False,
                            "interval_seconds": interval,
                            "next_run_in_seconds": round(float(interval) - elapsed, 1),
                            "last_cleanup_at": str(prev.get("last_cleanup_at") or ""),
                        }

            clean_res = merge_watcher.cleanup_merged_conversations(
                all_merged=True,
                include_noop_actions=True,
                include_scheduled_actions=True,
                include_sidecar_events=True,
                include_orphans=True,
                dry_run=dry_run,
                portfolio_dir=st_dir,
                brain_dir=eff_brain_dir,
                antigravity_dirs=eff_agy_dirs,
                repos_dir=repos_dir,
                git_client=git_client,
                now_ts=cur_now,
                env=eff_env,
                ps_output=ps_output,
                ps_runner=ps_runner,
                conversation_client=conversation_client,
            )
            try:
                archive_res = merge_watcher.auto_archive_stale_conversations(
                    adhoc_days=adhoc_days,
                    dry_run=dry_run,
                    portfolio_dir=st_dir,
                    brain_dir=eff_brain_dir,
                    antigravity_dirs=eff_agy_dirs,
                    now_ts=cur_now,
                    env=eff_env,
                    conversation_client=conversation_client,
                )
            except Exception as arch_exc:
                archive_res = {
                    "status": "error",
                    "archived_count": 0,
                    "archived": [],
                    "error": _sanitize_path(f"{arch_exc.__class__.__name__}: {arch_exc}"),
                }

            archived_count = int(archive_res.get("archived_count") or 0)
            deferred_desktop_count = int(clean_res.get("deferred_desktop_count") or 0)
            if not dry_run and clean_res.get("status") == "ok":
                dev_common.atomic_write_json(
                    state_path,
                    {
                        "schema_version": 1,
                        "last_cleanup_epoch": cur_now,
                        "last_cleanup_at": _utc_now_iso(),
                        "deleted_count": int(clean_res.get("deleted_count") or 0),
                        "archived_count": archived_count,
                        "deferred_desktop_count": deferred_desktop_count,
                        "sidecar_events_pruned_count": int(clean_res.get("sidecar_events_pruned_count") or 0),
                        "orphan_artifacts_pruned_count": int(clean_res.get("orphan_artifacts_pruned_count") or 0),
                        "freed_bytes": int(clean_res.get("freed_bytes") or 0),
                    },
                )
            return {
                "status": str(clean_res.get("status") or "ok"),
                "ran": True,
                "dry_run": bool(dry_run),
                "interval_seconds": interval,
                "deleted_count": int(clean_res.get("deleted_count") or 0),
                "archived_count": archived_count,
                "deferred_desktop_count": deferred_desktop_count,
                "native_delete_count": int(clean_res.get("native_delete_count") or 0),
                "sidecar_events_pruned_count": int(clean_res.get("sidecar_events_pruned_count") or 0),
                "orphan_artifacts_pruned_count": int(clean_res.get("orphan_artifacts_pruned_count") or 0),
                "freed_bytes": int(clean_res.get("freed_bytes") or 0),
                "freed_mb": float(clean_res.get("freed_mb") or 0.0),
                "auto_archive": archive_res,
            }
    except Exception as exc:
        return {
            "status": "error",
            "ran": False,
            "error": _sanitize_path(f"{exc.__class__.__name__}: {exc}"),
        }


def _run_zero_llm_rebase_reverify_pass(
    *,
    state_dir: Path | str | None = None,
    sync_git: bool = True,
    dry_run: bool = False,
    git_client: Any | None = None,
) -> dict[str, Any]:
    """Run 0-LLM `rebase-reverify` directly on `NEEDS_REBASE_REVERIFY` / stale follower locks without waking LLM."""
    resolved_state = dev_common.resolve_state_dir(portfolio_dir=state_dir)
    all_locks = lock_store.get_all_dev_pr_locks(portfolio_dir=resolved_state)
    is_offline = os.environ.get("AGY_OFFLINE_ONLY") == "1"
    actions: list[dict[str, Any]] = []
    agent_intervention_slots: list[dict[str, Any]] = []
    auto_promoted_count = 0
    applied_count = 0

    for slug, lk in sorted(all_locks.items()):
        if not isinstance(lk, dict) or not bool(lk.get("active")):
            continue
        for slot_lk in dev_common.iter_active_lock_slots(lk):
            st_u = str(slot_lk.get("status") or "").strip().upper()
            slot_num = int(slot_lk.get("slot_id") or 1)
            br = str(slot_lk.get("branch_name") or "").strip()
            wt_raw = str(slot_lk.get("worktree_path") or slot_lk.get("target_repo_dir") or "").strip()
            wt_expanded = wt_raw.replace("$HOME", str(Path.home())) if wt_raw else ""
            if st_u in ("NEEDS_AGENT_CONFLICT_RESOLUTION", "NEEDS_AGENT_SEMANTIC_FIX"):
                agent_intervention_slots.append(
                    {
                        "repo_slug": slug,
                        "slot_id": slot_num,
                        "branch_name": br,
                        "status": st_u,
                    }
                )
                continue
            needs_rb = bool(
                st_u == "NEEDS_REBASE_REVERIFY"
                or slot_lk.get("needs_base_rebase")
                or slot_lk.get("base_stale")
            )
            if not needs_rb:
                continue
            if dry_run:
                actions.append(
                    {
                        "action": "rebase_and_reverify_stale_follower",
                        "repo_slug": slug,
                        "slot_id": slot_num,
                        "branch_name": br,
                        "status": "dry_run",
                        "outcome": "DRY_RUN",
                        "promoted_to_human_pr": False,
                        "requires_wake": False,
                    }
                )
                continue
            try:
                rb_res = review_gate.run_slot_rebase_reverify(
                    repo_slug=slug,
                    workdir=wt_expanded or None,
                    branch_name=br,
                    fetch=bool(sync_git and not is_offline),
                    portfolio_dir=resolved_state,
                    git_client=git_client,
                )
                outcome = str((rb_res or {}).get("outcome") or (rb_res or {}).get("status") or "")
                promoted = bool((rb_res or {}).get("promoted_to_human_pr"))
                auto_promoted = outcome == "AUTO_PROMOTED_ZERO_LLM" or promoted
                needs_wake = outcome in ("NEEDS_AGENT_CONFLICT_RESOLUTION", "NEEDS_AGENT_SEMANTIC_FIX") or (
                    (rb_res or {}).get("status") != "ok"
                )
                if (rb_res or {}).get("status") == "ok":
                    applied_count += 1
                if auto_promoted:
                    auto_promoted_count += 1
                if needs_wake:
                    agent_intervention_slots.append(
                        {
                            "repo_slug": slug,
                            "slot_id": slot_num,
                            "branch_name": br,
                            "status": outcome or "ERROR",
                        }
                    )
                actions.append(
                    {
                        "action": "rebase_and_reverify_stale_follower",
                        "repo_slug": slug,
                        "slot_id": slot_num,
                        "branch_name": br,
                        "status": "applied" if (rb_res or {}).get("status") == "ok" else "error",
                        "outcome": outcome,
                        "promoted_to_human_pr": promoted,
                        "auto_promoted_zero_llm": auto_promoted,
                        "requires_wake": needs_wake,
                    }
                )
            except Exception as exc:
                agent_intervention_slots.append(
                    {
                        "repo_slug": slug,
                        "slot_id": slot_num,
                        "branch_name": br,
                        "status": "ERROR",
                    }
                )
                actions.append(
                    {
                        "action": "rebase_and_reverify_stale_follower",
                        "repo_slug": slug,
                        "slot_id": slot_num,
                        "branch_name": br,
                        "status": "error",
                        "error": _sanitize_path(str(exc)),
                        "requires_wake": True,
                    }
                )

    return {
        "actions": actions,
        "applied_count": applied_count,
        "auto_promoted_count": auto_promoted_count,
        "agent_intervention_slots": agent_intervention_slots,
        "requires_wake": bool(agent_intervention_slots),
    }


def run_watchdog_cycle(
    *,
    state_dir: Path | str | None = None,
    brain_dir: Path | str | Sequence[Path | str] | None = None,
    antigravity_dirs: Sequence[Path | str] | None = None,
    repos_dir: Path | str | None = None,
    sync_git: bool = True,
    cooldown_seconds: int = DEFAULT_WATCHDOG_COOLDOWN_SECONDS,
    auto_cleanup_interval_seconds: int | float | None = None,
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
    """Execute one 0-LLM watchdog cycle: honor Pause (Graceful Drain), fast-wake explicit Resume, or heal anomalies."""
    orch_ctrl = _orch_ctrl.load_orchestration_control(state_dir=state_dir, home_dir=home_dir)
    if bool(orch_ctrl.get("paused")):
        return {
            "status": "paused",
            "overall_status": "paused",
            "paused": True,
            "orchestration_control": orch_ctrl,
            "action_required": False,
            "healed": False,
            "woke": False,
            "checked_at": _utc_now_iso(),
            "health_score": 100,
            "anomalies": [],
            "queue_summary": {},
        }

    if _orch_ctrl.has_unhandled_resume_request(orch_ctrl):
        resume_nonce = str(orch_ctrl.get("resume_nonce") or "").strip()
        procs = dev_queue_doctor.inspect_live_processes(ps_output=ps_output, ps_runner=ps_runner)
        if procs.get("wait_for_unlock_running"):
            return {
                "status": "resume_delegated_to_waiter",
                "overall_status": "healthy",
                "paused": False,
                "orchestration_control": orch_ctrl,
                "action_required": False,
                "healed": False,
                "woke": False,
                "checked_at": _utc_now_iso(),
                "health_score": 100,
                "anomalies": [],
                "queue_summary": {},
                "wake": {
                    "status": "delegated_to_wait_for_unlock",
                    "woke": False,
                    "wait_for_unlock_pids": list(procs.get("wait_for_unlock_pids") or []),
                },
            }
        doc_res = dev_queue_doctor.run_queue_doctor(
            state_dir=state_dir,
            repos_dir=repos_dir,
            brain_dir=brain_dir,
            sync_git=sync_git,
            fix=True,
            wake=True,
            force=True,
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
        wake_st = str(wake_info.get("status") or "")
        if not dry_run and (woke or wake_st in ("skipped_wake_in_flight", "skipped_healthy")):
            _orch_ctrl.mark_resume_handled(
                state_dir=state_dir,
                home_dir=home_dir,
                resume_nonce=resume_nonce,
                handled_by="watchdog",
                now_ts=now_ts,
            )
        rem_actions = [r for r in (doc_res.get("remediation_actions") or []) if isinstance(r, dict)]
        healed = bool(not dry_run and any(r.get("status") == "applied" for r in rem_actions))
        return {
            "status": "dry_run" if dry_run else ("resumed_woke" if woke else "resumed"),
            "overall_status": str(doc_res.get("overall_status") or "healthy"),
            "paused": False,
            "orchestration_control": _orch_ctrl.load_orchestration_control(state_dir=state_dir, home_dir=home_dir),
            "action_required": True,
            "needs_fix": True,
            "needs_wake": True,
            "healed": healed,
            "woke": woke,
            "checked_at": str(doc_res.get("generated_at") or _utc_now_iso()),
            "health_score": int(doc_res.get("health_score") or 100),
            "anomalies": list(doc_res.get("anomalies") or []),
            "remediation_actions": rem_actions,
            "wake": wake_info,
        }

    rebase_pass = _run_zero_llm_rebase_reverify_pass(
        state_dir=state_dir,
        sync_git=sync_git,
        dry_run=dry_run,
        git_client=git_client,
    )

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
        auto_cleanup = _maybe_run_hourly_auto_cleanup(
            state_dir=state_dir,
            brain_dir=brain_dir,
            antigravity_dirs=antigravity_dirs,
            repos_dir=repos_dir,
            home_dir=home_dir,
            dry_run=dry_run,
            now_ts=now_ts,
            env=env,
            git_client=git_client,
            auto_cleanup_interval_seconds=auto_cleanup_interval_seconds,
            ps_output=ps_output,
            ps_runner=ps_runner,
        )
        if rebase_pass["applied_count"] > 0 or rebase_pass["auto_promoted_count"] > 0:
            return {
                "status": "healed",
                "overall_status": str(diag.get("overall_status") or "healthy"),
                "action_required": False,
                "needs_fix": False,
                "needs_wake": False,
                "healed": True,
                "woke": False,
                "auto_promoted_zero_llm": rebase_pass["auto_promoted_count"] > 0,
                "rebase_actions": rebase_pass["actions"],
                "remediation_actions": rebase_pass["actions"],
                "checked_at": str(diag.get("generated_at") or _utc_now_iso()),
                "health_score": int(diag.get("health_score") or 100),
                "anomalies": [],
                "queue_summary": dict(diag.get("queue_summary") or {}),
                "auto_cleanup": auto_cleanup,
            }
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
            "auto_cleanup": auto_cleanup,
        }

    needs_fix = bool(diag.get("needs_fix"))
    needs_wake = bool(diag.get("needs_wake") or rebase_pass.get("requires_wake"))
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
    rem_actions = [
        *rebase_pass["actions"],
        *[r for r in (doc_res.get("remediation_actions") or []) if isinstance(r, dict)],
    ]
    healed = bool(
        not dry_run
        and (
            rebase_pass["applied_count"] > 0
            or (needs_fix and any(r.get("status") == "applied" for r in rem_actions))
        )
    )

    wake_status = str(wake_info.get("status") or "")
    if dry_run:
        cycle_status = "dry_run"
    elif woke:
        cycle_status = "woke"
    elif healed:
        cycle_status = "healed"
    elif wake_status == "skipped_all_models_exhausted":
        cycle_status = "quota_exhausted"
    elif wake_status in ("skipped_cooldown", "skipped_wake_in_flight"):
        cycle_status = "cooldown"
    else:
        cycle_status = str(doc_res.get("overall_status") or "degraded")

    auto_cleanup = _maybe_run_hourly_auto_cleanup(
        state_dir=state_dir,
        brain_dir=brain_dir,
        antigravity_dirs=antigravity_dirs,
        repos_dir=repos_dir,
        home_dir=home_dir,
        dry_run=dry_run,
        now_ts=now_ts,
        env=env,
        git_client=git_client,
        auto_cleanup_interval_seconds=auto_cleanup_interval_seconds,
        ps_output=ps_output,
        ps_runner=ps_runner,
    )

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
        "auto_cleanup": auto_cleanup,
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
    sel_model = str(wake_info.get("selected_wake_model") or "")
    if sel_model:
        extra += f" model={sel_model}"
    next_reset = str(wake_info.get("next_reset_at") or "")
    if wake_info.get("status") == "skipped_all_models_exhausted" and next_reset:
        extra += f" next_reset={next_reset}"
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

            # Slice the wait interval into <=3s control checks (<0.1ms, 0 git/LLM) so clicking
            # Continue/Resume in the Dev Queue panel wakes the orchestrator within ~3s.
            deadline = time.monotonic() + float(interval)
            stopped_early = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                slice_sec = min(remaining, DEFAULT_CONTROL_SLICE_SECONDS)
                if ev.wait(slice_sec):
                    stopped_early = True
                    break
                with contextlib.suppress(Exception):
                    if _orch_ctrl.has_unhandled_resume_request(
                        _orch_ctrl.load_orchestration_control(state_dir=state_override)
                    ):
                        break
            if stopped_early:
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
