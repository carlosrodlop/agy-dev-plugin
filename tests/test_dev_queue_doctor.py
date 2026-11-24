#!/usr/bin/env python3
"""Hermetic Zero-PII unit tests for `dev_queue_doctor.py`, CLI entrypoints, and TUI `[d]` hotkey."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
REM_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
TUI_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts"
WATCHDOG_DIR = REPO_ROOT / "sidecars" / "dev-queue-watchdog"

for _p in (CLIENTS_DIR, REM_SCRIPTS_DIR, TUI_SCRIPTS_DIR, WATCHDOG_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import dev_common  # noqa: E402
import dev_flow_optimizer as flow_opt  # noqa: E402
import dev_queue_doctor as doctor  # noqa: E402
import dev_queue_tui as tui  # noqa: E402
import pr_remediator as prs  # noqa: E402
import watchdog  # noqa: E402


def _write_transcript(
    brain_dir: Path,
    cid: str,
    steps: list[dict[str, object]],
    *,
    mtime: float | None = None,
) -> Path:
    tpath = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
    tpath.parent.mkdir(parents=True, exist_ok=True)
    tpath.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(tpath, (mtime, mtime))
    return tpath


def _write_state_files(
    state_dir: Path,
    *,
    orchestrator_id: str = "",
    items: list[dict[str, object]] | None = None,
    locks: dict[str, dict[str, object]] | None = None,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    dev_common.atomic_write_json(
        state_dir / "dev_queue.json",
        {
            "schema_version": 1,
            "orchestrator_conversation_id": orchestrator_id,
            "items": list(items or []),
        },
    )
    dev_common.atomic_write_json(
        state_dir / "actions_state.json",
        {
            "actions_log_audit_state": {
                "dev_pr_locks": dict(locks or {}),
            }
        },
    )


class TestDevQueueDoctor(unittest.TestCase):
    """Comprehensive hermetic offline tests for `agy-doctor` / `queue-doctor`."""

    def test_healthy_queue_diagnosis_check_only_and_wake_skip(self) -> None:
        """Healthy queue reports `healthy`, `needs_wake=False`, `--check-only` exit `0`, and `--wake` `skipped_healthy`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            _write_state_files(
                state_dir,
                orchestrator_id="11111111-aaaa-4111-8111-111111111111",
                items=[
                    {
                        "conversation_id": "22222222-bbbb-4222-8222-222222222222",
                        "priority": 10,
                        "status": "merged",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": ["acme/repo-a"],
                        "title": "Completed task",
                    }
                ],
                locks={},
            )

            diag = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                ps_output="  PID ARGS\n  100 /sbin/launchd\n",
            )
            self.assertEqual(diag["overall_status"], "healthy")
            self.assertFalse(diag["needs_wake"])
            self.assertFalse(diag["needs_fix"])
            self.assertEqual(diag["anomalies"], [])

            buf_chk = io.StringIO()
            with contextlib.redirect_stdout(buf_chk):
                rc_chk = prs.main(
                    [
                        "queue-doctor",
                        "--state-dir",
                        str(state_dir),
                        "--brain-dir",
                        str(brain_dir),
                        "--check-only",
                        "--json",
                    ]
                )
            self.assertEqual(rc_chk, 0)
            chk_json = json.loads(buf_chk.getvalue())
            self.assertEqual(chk_json["overall_status"], "healthy")

            mock_popen = mock.MagicMock()
            res_wake = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                wake=True,
                ps_output="",
                popen_fn=mock_popen,
            )
            self.assertEqual(res_wake["wake"]["status"], "skipped_healthy")
            self.assertFalse(res_wake["wake"]["woke"])
            mock_popen.assert_not_called()

    def test_dormant_orchestrator_with_runnable_work_and_check_only_exit_2(self) -> None:
        """Idle or missing orchestrator with runnable free-repo tasks flags `idle_orchestrator_with_runnable_work` and exits `2`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            orch_cid = "33333333-cccc-4333-8333-333333333333"
            now_ts = 1_800_000_000.0
            _write_transcript(
                brain_dir,
                orch_cid,
                [{"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE"}],
                mtime=now_ts - 2400.0,
            )
            _write_state_files(
                state_dir,
                orchestrator_id=orch_cid,
                items=[
                    {
                        "conversation_id": "44444444-dddd-4444-8444-444444444444",
                        "priority": 15,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "target_files": ["src/module.py"],
                        "title": "Implement synthetic feature",
                    }
                ],
                locks={},
            )

            with mock.patch.object(
                doctor,
                "inspect_live_processes",
                return_value={
                    "wait_for_unlock_running": False,
                    "wait_for_unlock_pids": [],
                    "watch_pr_merge_running": False,
                    "watch_pr_merge_pids": [],
                    "orchestrator_cli_running": False,
                    "orchestrator_cli_pids": [],
                    "probe_error": "",
                },
            ):
                diag = doctor.diagnose_orchestrator(
                    state_dir=state_dir,
                    brain_dir=brain_dir,
                    now_ts=now_ts,
                )
                codes = [a["code"] for a in diag["anomalies"]]
                self.assertIn("idle_orchestrator_with_runnable_work", codes)
                self.assertTrue(diag["needs_wake"])
                self.assertEqual(diag["overall_status"], "degraded")

                buf_chk = io.StringIO()
                with mock.patch.object(doctor.time, "time", return_value=now_ts), contextlib.redirect_stdout(buf_chk):
                    rc = dev_common.run_dev_queue_doctor_cli(
                        [
                            "--state-dir",
                            str(state_dir),
                            "--brain-dir",
                            str(brain_dir),
                            "--check-only",
                            "--no-color",
                        ]
                    )
                self.assertEqual(rc, 2)
                self.assertIn("idle_orchestrator_with_runnable_work", buf_chk.getvalue())

    def test_server_restart_transcript_detection_and_recovery_clearing(self) -> None:
        """Detects unrecovered Antigravity server restart notice and clears once subsequent tool recovery runs."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            orch_cid = "55555555-eeee-4555-8555-555555555555"
            now_ts = 1_800_010_000.0

            _write_transcript(
                brain_dir,
                orch_cid,
                [
                    {"step_index": 1, "type": "USER_INPUT", "content": "Execute dev queue"},
                    {
                        "step_index": 2,
                        "type": "RUN_COMMAND",
                        "status": "DONE",
                        "arguments": {"CommandLine": "pr_remediator.py wait-for-unlock"},
                    },
                    {
                        "step_index": 3,
                        "type": "SYSTEM_MESSAGE",
                        "content": "All your subagents and background tasks have been stopped due to server restart.",
                    },
                ],
                mtime=now_ts - 120.0,
            )
            _write_state_files(
                state_dir,
                orchestrator_id=orch_cid,
                items=[
                    {
                        "conversation_id": "66666666-ffff-4666-8666-666666666666",
                        "priority": 10,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "target_files": ["clients/helper.py"],
                        "title": "Pending after server restart",
                    }
                ],
                locks={},
            )

            diag = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                now_ts=now_ts,
                ps_output="",
            )
            codes = [a["code"] for a in diag["anomalies"]]
            self.assertIn("server_restart_interrupted", codes)
            self.assertEqual(diag["overall_status"], "blocked")
            self.assertTrue(diag["needs_wake"])

            # Now append a recovery tool dispatch after the restart notice
            _write_transcript(
                brain_dir,
                orch_cid,
                [
                    {"step_index": 1, "type": "USER_INPUT", "content": "Execute dev queue"},
                    {
                        "step_index": 2,
                        "type": "SYSTEM_MESSAGE",
                        "content": "All your subagents and background tasks have been stopped due to server restart.",
                    },
                    {
                        "step_index": 3,
                        "type": "RUN_COMMAND",
                        "status": "RUNNING",
                        "arguments": {"CommandLine": "pr_remediator.py queue-next --unlocked"},
                    },
                ],
                mtime=now_ts - 5.0,
            )
            diag_recovered = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                now_ts=now_ts,
                ps_output="",
            )
            recovered_codes = [a["code"] for a in diag_recovered["anomalies"]]
            self.assertNotIn("server_restart_interrupted", recovered_codes)
            self.assertEqual(diag_recovered["overall_status"], "healthy")

    def test_stalled_worker_and_missing_wait_for_unlock_detection(self) -> None:
        """Detects `stalled_worker` on idle DEVELOPING locks and `missing_wait_for_unlock` when all repos are locked."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            now_ts = 1_800_020_000.0

            # Case A: All repos locked in human-ready PR state + pending item waiting + zero wait-for-unlock processes
            _write_state_files(
                state_dir,
                orchestrator_id="77771111-aaaa-4777-8777-111111111111",
                items=[
                    {
                        "conversation_id": "77772222-bbbb-4777-8777-222222222222",
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "pr_urls": ["https://github.com/acme/repo-a/pull/1"],
                        "target_files": ["a.py"],
                        "title": "Open PR item",
                    },
                    {
                        "conversation_id": "77773333-cccc-4777-8777-333333333333",
                        "priority": 20,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "target_files": ["b.py"],
                        "title": "Queued behind PR",
                    },
                ],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": "77772222-bbbb-4777-8777-222222222222",
                        "branch_name": "fix/20261121-100000-open-77772222",
                    }
                },
            )
            diag_missing_wait = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                now_ts=now_ts,
                ps_output="  PID ARGS\n  222 python3 pr_remediator.py watch-pr-merge\n",
            )
            self.assertIn("missing_wait_for_unlock", [a["code"] for a in diag_missing_wait["anomalies"]])
            self.assertTrue(diag_missing_wait["processes"]["watch_pr_merge_running"])
            self.assertFalse(diag_missing_wait["processes"]["wait_for_unlock_running"])

            # With wait-for-unlock running in ps output -> healthy!
            diag_with_wait = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                now_ts=now_ts,
                ps_output="  PID ARGS\n  223 python3 /opt/pr_remediator.py wait-for-unlock --orchestrator-id 77771111\n",
            )
            self.assertEqual(diag_with_wait["overall_status"], "healthy")
            self.assertTrue(diag_with_wait["processes"]["wait_for_unlock_running"])

            # Case B: Stalled DEVELOPING worker lock
            _write_state_files(
                state_dir,
                orchestrator_id="77771111-aaaa-4777-8777-111111111111",
                items=[
                    {
                        "conversation_id": "77774444-dddd-4777-8777-444444444444",
                        "priority": 10,
                        "status": "in_progress",
                        "repo_slugs": ["acme/repo-b"],
                        "merged_repos": [],
                        "target_files": ["c.py"],
                        "title": "Stalled worker item",
                    }
                ],
                locks={
                    "acme/repo-b": {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "conversation_id": "77774444-dddd-4777-8777-444444444444",
                        "worker_cid": "77775555-eeee-4777-8777-555555555555",
                        "branch_name": "fix/20261121-110000-stall-77774444",
                        "last_activity_at": "2026-11-20T00:00:00Z",
                        "updated_at": "2026-11-20T00:00:00Z",
                        "acquired_at": "2026-11-20T00:00:00Z",
                    }
                },
            )
            diag_stalled = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                now_ts=now_ts,
                ps_output="  PID ARGS\n  223 python3 pr_remediator.py wait-for-unlock\n",
            )
            self.assertIn("stalled_worker", [a["code"] for a in diag_stalled["anomalies"]])
            self.assertEqual(diag_stalled["overall_status"], "blocked")
            prompt_txt = doctor.build_wake_prompt(diag_stalled)
            self.assertIn("send_message", prompt_txt)
            self.assertIn("wait-for-unlock", prompt_txt)

    def test_cooldown_and_pid_anti_loop_wake_guardrails(self) -> None:
        """Verifies `--dry-run`, `0600` state file, `skipped_cooldown`, `skipped_wake_in_flight`, and `--force`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            brain_dir = root / "brain"
            home_dir = root / "fake_home"
            agy_bin_path = home_dir / ".local" / "bin" / "agy"
            agy_bin_path.parent.mkdir(parents=True)
            agy_bin_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(agy_bin_path, 0o755)

            now_ts = 1_800_050_000.0
            _write_state_files(
                state_dir,
                orchestrator_id="",
                items=[
                    {
                        "conversation_id": "88881111-aaaa-4888-8888-111111111111",
                        "priority": 10,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "target_files": ["x.py"],
                        "title": "Wake candidate task",
                    }
                ],
                locks={},
            )

            mock_popen = mock.MagicMock()
            mock_proc = mock.MagicMock()
            mock_proc.pid = 43210
            mock_popen.return_value = mock_proc

            # 1. Dry-run never spawns and does not write state file
            term_env = {"ANTIGRAVITY_LS_ADDRESS": ""}
            res_dry = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                wake=True,
                dry_run=True,
                now_ts=now_ts,
                ps_output="",
                home_dir=home_dir,
                env=term_env,
                which_fn=lambda _name: None,
                popen_fn=mock_popen,
            )
            self.assertEqual(res_dry["wake"]["status"], "dry_run")
            self.assertEqual(res_dry["wake"]["transport"], "agy_cli")
            self.assertEqual(
                res_dry["wake"]["command"][:5],
                [str(agy_bin_path), "--agent", "agy-dev-orchestrator", "--dangerously-skip-permissions", "-p"],
            )
            self.assertNotIn("--mode", res_dry["wake"]["command"])
            mock_popen.assert_not_called()
            self.assertFalse((state_dir / doctor.DOCTOR_STATE_FILENAME).exists())

            # 2. First real wake launches detached agy and writes 0600 agy_doctor_state.json
            res_first = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                wake=True,
                cooldown_seconds=900,
                now_ts=now_ts,
                ps_output="",
                home_dir=home_dir,
                env=term_env,
                which_fn=lambda _name: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _pid: False,
            )
            self.assertEqual(res_first["wake"]["status"], "woke")
            self.assertTrue(res_first["wake"]["woke"])
            self.assertEqual(res_first["wake"]["transport"], "agy_cli")
            self.assertEqual(res_first["wake"]["pid"], 43210)
            self.assertEqual(mock_popen.call_count, 1)
            called_cmd = mock_popen.call_args[0][0]
            self.assertEqual(
                called_cmd[:5],
                [str(agy_bin_path), "--agent", "agy-dev-orchestrator", "--dangerously-skip-permissions", "-p"],
            )
            self.assertNotIn("--mode", called_cmd)
            self.assertTrue(mock_popen.call_args[1]["start_new_session"])

            st_file = state_dir / doctor.DOCTOR_STATE_FILENAME
            self.assertTrue(st_file.is_file())
            self.assertEqual(st_file.stat().st_mode & 0o777, 0o600)
            saved_state = json.loads(st_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_state["last_wake_pid"], 43210)
            self.assertEqual(saved_state["transport"], "agy_cli")
            self.assertEqual(saved_state["last_command"][-1], "<prompt>")
            self.assertTrue(saved_state["last_wake_at"])

            # 3. Second wake within cooldown (PID exited) -> skipped_cooldown
            res_cool = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                wake=True,
                cooldown_seconds=900,
                now_ts=now_ts + 120.0,
                ps_output="",
                home_dir=home_dir,
                env=term_env,
                which_fn=lambda _name: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _pid: False,
            )
            self.assertEqual(res_cool["wake"]["status"], "skipped_cooldown")
            self.assertEqual(res_cool["wake"]["remaining_seconds"], 780)
            self.assertEqual(mock_popen.call_count, 1)

            # 4. Alive PID -> skipped_wake_in_flight (even if cooldown elapsed or force passed)
            res_inflight = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                wake=True,
                force=True,
                cooldown_seconds=900,
                now_ts=now_ts + 2000.0,
                ps_output="",
                home_dir=home_dir,
                env=term_env,
                which_fn=lambda _name: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda pid: pid == 43210,
            )
            self.assertEqual(res_inflight["wake"]["status"], "skipped_wake_in_flight")
            self.assertEqual(res_inflight["wake"]["pid"], 43210)
            self.assertEqual(mock_popen.call_count, 1)

            # 5. Force overrides cooldown once previous PID has exited
            mock_proc2 = mock.MagicMock()
            mock_proc2.pid = 54321
            mock_popen.return_value = mock_proc2
            res_forced = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                wake=True,
                force=True,
                cooldown_seconds=900,
                now_ts=now_ts + 180.0,
                ps_output="",
                home_dir=home_dir,
                env=term_env,
                which_fn=lambda _name: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _pid: False,
            )
            self.assertEqual(res_forced["wake"]["status"], "woke")
            self.assertEqual(res_forced["wake"]["pid"], 54321)
            self.assertEqual(mock_popen.call_count, 2)

    def test_fix_reconciles_phantom_pr_open_without_mutating_tracked_repo_files(self) -> None:
        """`--fix` reverts `phantom_pr_open` to `pending` (preserving `merged_repos`) without touching tracked git files."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            repo_dir = root / "repos" / "repo-b"
            repo_dir.mkdir(parents=True)
            git_env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith("GIT_")
            }
            subprocess.run(["git", "-C", str(repo_dir), "init", "-b", "main"], env=git_env, check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "config", "user.name", "Test Bot"], env=git_env, check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "config", "user.email", "bot@example.com"], env=git_env, check=True, capture_output=True)
            tracked_file = repo_dir / "app.py"
            tracked_file.write_text("VALUE = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_dir), "add", "app.py"], env=git_env, check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "init"], env=git_env, check=True, capture_output=True)
            head_before = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                env=git_env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            phantom_cid = "99991111-aaaa-4999-8999-111111111111"
            _write_state_files(
                state_dir,
                orchestrator_id="99990000-aaaa-4999-8999-000000000000",
                items=[
                    {
                        "conversation_id": phantom_cid,
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                        "merged_repos": ["acme/repo-a"],
                        "pr_urls": [],
                        "target_files": ["app.py"],
                        "title": "Multi-repo phantom PR item",
                    }
                ],
                locks={},
            )

            diag_before = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                repos_dir=repo_dir.parent,
                ps_output="",
            )
            self.assertIn("phantom_pr_open", [a["code"] for a in diag_before["anomalies"]])
            self.assertTrue(diag_before["needs_fix"])

            with mock.patch.dict(os.environ, {"AGY_OFFLINE_ONLY": "1"}):
                healed = doctor.run_queue_doctor(
                    state_dir=state_dir,
                    brain_dir=brain_dir,
                    repos_dir=repo_dir.parent,
                    fix=True,
                    ps_output="",
                )

            self.assertNotIn("phantom_pr_open", [a["code"] for a in healed["anomalies"]])
            q_after = json.loads((state_dir / "dev_queue.json").read_text(encoding="utf-8"))
            self.assertEqual(q_after["items"][0]["status"], "pending")
            self.assertEqual(q_after["items"][0]["merged_repos"], ["acme/repo-a"])

            # Verify tracked git repository is 100% untouched
            head_after = subprocess.run(
                ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
                env=git_env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            porcelain = subprocess.run(
                ["git", "-C", str(repo_dir), "status", "--porcelain"],
                env=git_env,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(head_before, head_after)
            self.assertEqual(porcelain, "")
            self.assertEqual(tracked_file.read_text(encoding="utf-8"), "VALUE = 1\n")

    def test_improvements_rendered_cleanly_from_dev_flow_optimizer(self) -> None:
        """`improvements` surfaces prioritized `dev_flow_optimizer` recommendations as PR-only proposals."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            orch_cid = "aaaa1111-bbbb-4111-8111-111111111111"
            now_ts = 1_800_080_000.0
            _write_transcript(
                brain_dir,
                orch_cid,
                [{"step_index": 1, "type": "RUN_COMMAND", "status": "RUNNING"}],
                mtime=now_ts - 10.0,
            )
            _write_state_files(
                state_dir,
                orchestrator_id=orch_cid,
                items=[
                    {
                        "conversation_id": "bbbb1111-cccc-4111-8111-111111111111",
                        "priority": 20,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "target_files": ["web/style.css"],
                        "title": "Minor UI badge tweak",
                    },
                    {
                        "conversation_id": "bbbb2222-cccc-4222-8222-222222222222",
                        "priority": 22,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "target_files": [],
                        "title": "Minor UI tooltip copy polish",
                    },
                ],
                locks={},
            )

            rep = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                now_ts=now_ts,
                ps_output="",
            )
            imp_codes = [i["code"] for i in rep["improvements"]]
            self.assertIn("micro_pr_batch_candidates", imp_codes)
            self.assertIn("unanchored_pending_tasks", imp_codes)
            for imp in rep["improvements"]:
                self.assertEqual(imp["delivery_mode"], "pr_proposal_only")
                self.assertFalse(imp["auto_mutated"])

            rendered = doctor.render_doctor_report(rep, color=False)
            self.assertIn("1. Diagnosis (What Happened)", rendered)
            self.assertIn("2. Safe Runtime Actions & Wake Status", rendered)
            self.assertIn("3. Orchestrator Improvements (PR Proposals — Never Auto-Mutated)", rendered)
            self.assertIn("micro_pr_batch_candidates", rendered)

    def test_tui_watch_legend_and_doctor_hotkey_delegation(self) -> None:
        """`dev_queue_tui.py` watch mode shows `[d] doctor` and `[d]` suspends/restores alt-screen cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            _write_state_files(state_dir, orchestrator_id="", items=[], locks={})

            args = tui._build_parser().parse_args(
                ["--state-dir", str(state_dir), "--watch", "0.2", "--no-auto-sync", "--no-color"]
            )

            r_fd, w_fd = os.pipe()
            try:
                # Press 'd' to open doctor, '\n' to return to TUI, then 'q' to quit watch mode
                os.write(w_fd, b"d\nq")
                os.close(w_fd)
                w_fd = -1

                buf = io.StringIO()
                with mock.patch.object(
                    tui,
                    "_invoke_queue_doctor",
                    return_value={
                        "status": "ok",
                        "overall_status": "healthy",
                        "health_score": 100,
                        "anomalies": [],
                        "improvements": [],
                        "remediation_actions": [],
                        "wake": {"status": "not_requested", "woke": False},
                    },
                ) as spy_doc:
                    rc = tui.run_watch_loop(
                        args,
                        stream=buf,
                        use_tty_override=True,
                        open_tty=False,
                        tty_fd_override=r_fd,
                        max_iterations=3,
                        term_size_override=(140, 24),
                    )
                self.assertEqual(rc, 0)
                self.assertEqual(spy_doc.call_count, 1)
                output = buf.getvalue()
                self.assertIn("[d] doctor", output)
                self.assertIn("1. Diagnosis (What Happened)", output)
                self.assertIn("✓ Doctor: healthy (100/100)", output)
                self.assertGreaterEqual(output.count(tui.ALT_SCREEN_ENTER), 2)
                self.assertGreaterEqual(output.count(tui.ALT_SCREEN_EXIT), 2)
            finally:
                os.close(r_fd)
                if w_fd >= 0:
                    os.close(w_fd)

            # Also verify `[w]` wake choice inside `_handle_doctor_hotkey`
            r2, w2 = os.pipe()
            try:
                os.write(w2, b"w")
                os.close(w2)
                w2 = -1
                calls: list[tuple[bool, bool]] = []

                def _fake_runner(
                    _st: Path,
                    *,
                    repos_dir: str = "",
                    fix: bool = False,
                    wake: bool = False,
                    sync_git: bool = False,
                ) -> dict[str, object]:
                    _ = (repos_dir, sync_git)
                    calls.append((fix, wake))
                    return {
                        "status": "ok",
                        "overall_status": "degraded",
                        "health_score": 85,
                        "anomalies": [{"code": "idle_orchestrator_with_runnable_work", "severity": "high", "title": "Idle"}],
                        "improvements": [],
                        "remediation_actions": [],
                        "wake": {"status": "woke" if wake else "not_requested", "woke": wake, "pid": 77777},
                    }

                msg = tui._handle_doctor_hotkey(
                    args,
                    state_dir,
                    out_stream=io.StringIO(),
                    use_tty=True,
                    tty_fd=r2,
                    doctor_fn=_fake_runner,
                )
                self.assertEqual(calls, [(False, False), (False, True)])
                self.assertEqual(msg, "✓ Doctor: woke orchestrator (pid 77777)")
            finally:
                os.close(r2)
                if w2 >= 0:
                    os.close(w2)

            # Verify `agy-queue doctor --help` forwarding through `dev_queue_tui.main`
            help_buf = io.StringIO()
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(help_buf):
                tui.main(["doctor", "--help"])
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("--check-only", help_buf.getvalue())

            # Verify `--print-prompt` CLI flag and `is_pid_alive` via `os.kill(pid, 0)`
            prompt_buf = io.StringIO()
            with contextlib.redirect_stdout(prompt_buf):
                rc_prompt = prs.main(
                    [
                        "queue-doctor",
                        "--state-dir",
                        str(state_dir),
                        "--print-prompt",
                    ]
                )
            self.assertEqual(rc_prompt, 0)
            self.assertIn("wait-for-unlock", prompt_buf.getvalue())

            with mock.patch.object(doctor.os, "kill", return_value=None):
                self.assertTrue(doctor.is_pid_alive(12345))
            with mock.patch.object(doctor.os, "kill", side_effect=ProcessLookupError()):
                self.assertFalse(doctor.is_pid_alive(12345))

    def test_inspect_live_processes_strips_prompts_and_avoids_wait_for_unlock_false_positive(self) -> None:
        """`agy -p "... wait-for-unlock ..."` and `agentapi` wakes never trigger `wait_for_unlock_running=True`."""
        ps_false_pos = "\n".join(
            [
                "  PID ARGS",
                '  401 /opt/bin/agy --agent agy-dev-orchestrator --dangerously-skip-permissions -p "[agy-doctor] run pr_remediator.py wait-for-unlock"',
                "  402 /opt/bin/agy --agent agy-dev-orchestrator --dangerously-skip-permissions -p [agy-doctor] unquoted ps output pr_remediator.py wait-for-unlock",
                '  403 agy -p "please inspect pr_remediator.py wait-for-unlock and watch-pr-merge"',
                '  404 agy -p "unclosed truncated prompt mentioning pr_remediator.py wait-for-unlock',
                "  405 /bin/zsh -c 'agy --prompt \"run pr_remediator.py wait-for-unlock\"'",
                '  406 /opt/antigravity/bin/agentapi new-conversation "--title=[agy-doctor] Orchestrator Recovery" "arm pr_remediator.py wait-for-unlock"',
            ]
        )
        res_clean = doctor.inspect_live_processes(ps_output=ps_false_pos, self_pid=99999)
        self.assertFalse(res_clean["wait_for_unlock_running"])
        self.assertEqual(res_clean["wait_for_unlock_pids"], [])
        self.assertFalse(res_clean["watch_pr_merge_running"])
        self.assertEqual(res_clean["watch_pr_merge_pids"], [])
        self.assertTrue(res_clean["orchestrator_cli_running"])
        self.assertEqual(res_clean["orchestrator_cli_pids"], [401, 402, 406])

        # Genuine wait-for-unlock and watch-pr-merge daemons are detected accurately alongside wakes
        ps_genuine = "\n".join(
            [
                "  PID ARGS",
                "  501 python3 /opt/agy/pr_remediator.py wait-for-unlock --orchestrator-id 11112222 --interval 15",
                "  502 /usr/bin/python3 /opt/agy/pr_remediator.py watch-pr-merge --interval 15",
                "  503 /bin/zsh -c 'python3 /opt/agy/pr_remediator.py wait-for-unlock --max-duration 3600'",
            ]
        )
        res_genuine = doctor.inspect_live_processes(ps_output=ps_genuine, self_pid=99999)
        self.assertTrue(res_genuine["wait_for_unlock_running"])
        self.assertEqual(res_genuine["wait_for_unlock_pids"], [501, 503])
        self.assertTrue(res_genuine["watch_pr_merge_running"])
        self.assertEqual(res_genuine["watch_pr_merge_pids"], [502])
        self.assertFalse(res_genuine["orchestrator_cli_running"])

    def test_hybrid_wake_agentapi_desktop_vs_agy_cli_terminal(self) -> None:
        """Selects `agentapi new-conversation` when `ANTIGRAVITY_LS_ADDRESS` is set, else `agy --dangerously-skip-permissions`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            brain_dir = root / "brain"
            home_dir = root / "fake_home"
            agentapi_path = home_dir / ".gemini" / "antigravity" / "bin" / "agentapi"
            agentapi_path.parent.mkdir(parents=True)
            agentapi_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(agentapi_path, 0o755)

            agy_path = home_dir / ".local" / "bin" / "agy"
            agy_path.parent.mkdir(parents=True)
            agy_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(agy_path, 0o755)

            now_ts = 1_800_100_000.0
            _write_state_files(
                state_dir,
                orchestrator_id="",
                items=[
                    {
                        "conversation_id": "aaaa2222-bbbb-4222-8222-222222222222",
                        "priority": 10,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "target_files": ["mod.py"],
                        "title": "Hybrid wake task",
                    }
                ],
                locks={},
            )

            # Path A: ANTIGRAVITY_LS_ADDRESS set + agentapi present -> transport == "agentapi"
            mock_popen = mock.MagicMock()
            mock_proc = mock.MagicMock()
            mock_proc.pid = 65432
            mock_popen.return_value = mock_proc

            desktop_env = {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:51234"}
            res_desktop = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                wake=True,
                now_ts=now_ts,
                ps_output="",
                home_dir=home_dir,
                env=desktop_env,
                which_fn=lambda _name: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _pid: False,
            )
            self.assertEqual(res_desktop["wake"]["status"], "woke")
            self.assertEqual(res_desktop["wake"]["transport"], "agentapi")
            cmd_a = mock_popen.call_args[0][0]
            self.assertEqual(
                cmd_a[:3],
                [str(agentapi_path), "new-conversation", "--title=[agy-doctor] Orchestrator Recovery"],
            )
            self.assertIn("agy-dev-orchestrator.md", cmd_a[3])
            saved = json.loads((state_dir / doctor.DOCTOR_STATE_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(saved["transport"], "agentapi")
            self.assertEqual(saved["last_command"][-1], "<prompt>")
            rendered_a = doctor.render_doctor_report(res_desktop, color=False)
            self.assertIn("(agentapi)", rendered_a)

            # Standalone ~/.gemini/antigravity/bin/agentapi takes precedence over ANTIGRAVITY_AGENTAPI_EXE,
            # while ANTIGRAVITY_AGENTAPI_EXE serves as fallback once standalone agentapi is absent
            custom_agentapi = root / "custom_agentapi"
            custom_agentapi.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            self.assertEqual(
                doctor.resolve_agentapi_cli(
                    env={"ANTIGRAVITY_AGENTAPI_EXE": str(custom_agentapi)},
                    home_dir=home_dir,
                    which_fn=lambda _n: None,
                ),
                str(agentapi_path),
            )

            # Path B fallback: ANTIGRAVITY_LS_ADDRESS set, standalone agentapi removed -> ANTIGRAVITY_AGENTAPI_EXE fallback works,
            # and when neither exists -> falls back to agy_cli
            agentapi_path.unlink()
            self.assertEqual(
                doctor.resolve_agentapi_cli(
                    env={"ANTIGRAVITY_AGENTAPI_EXE": str(custom_agentapi)},
                    home_dir=home_dir,
                    which_fn=lambda _n: None,
                ),
                str(custom_agentapi),
            )
            res_fallback = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                wake=True,
                dry_run=True,
                force=True,
                now_ts=now_ts + 1000.0,
                ps_output="",
                home_dir=home_dir,
                env=desktop_env,
                which_fn=lambda _name: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _pid: False,
            )
            self.assertEqual(res_fallback["wake"]["transport"], "agy_cli")
            self.assertEqual(
                res_fallback["wake"]["command"][:5],
                [str(agy_path), "--agent", "agy-dev-orchestrator", "--dangerously-skip-permissions", "-p"],
            )
            self.assertNotIn("--mode", res_fallback["wake"]["command"])

    def test_dual_desktop_and_cli_brain_transcript_lookup_and_hermetic_isolation(self) -> None:
        """`dev_common` resolves transcripts across both `antigravity/brain` and `antigravity-cli/brain` while staying hermetic."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fake_home = root / "home"
            desktop_brain = fake_home / ".gemini" / "antigravity" / "brain"
            cli_brain = fake_home / ".gemini" / "antigravity-cli" / "brain"
            state_dir = root / "state"
            now_ts = 1_800_200_000.0

            with mock.patch.dict(os.environ, {"AGY_BRAIN_DIR": ""}, clear=False):
                os.environ.pop("AGY_BRAIN_DIR", None)
                # Empty string and explicit_state_dir=True return [] for 100% hermetic isolation
                self.assertEqual(dev_common.resolve_antigravity_brain_dirs("", home_dir=fake_home), [])
                self.assertEqual(
                    dev_common.resolve_antigravity_brain_dirs(None, home_dir=fake_home, explicit_state_dir=True),
                    [],
                )
                # Default returns [desktop_brain, cli_brain]
                self.assertEqual(
                    dev_common.resolve_antigravity_brain_dirs(None, home_dir=fake_home),
                    [desktop_brain, cli_brain],
                )

                cli_orch_cid = "cccc1111-dddd-4111-8111-111111111111"
                _write_transcript(
                    cli_brain,
                    cli_orch_cid,
                    [
                        {"step_index": 1, "type": "USER_INPUT", "content": "CLI orchestrator wake"},
                        {"step_index": 2, "type": "RUN_COMMAND", "status": "RUNNING"},
                    ],
                    mtime=now_ts - 15.0,
                )

                # Resolved via dev_common helpers even though desktop_brain does not have this CID
                resolved_tp = dev_common.resolve_transcript_path(cli_orch_cid, home_dir=fake_home)
                self.assertIsNotNone(resolved_tp)
                self.assertEqual(resolved_tp, cli_brain / cli_orch_cid / ".system_generated" / "logs" / "transcript.jsonl")

                inspected = dev_common.try_inspect_antigravity_conversation(cli_orch_cid, home_dir=fake_home)
                self.assertEqual(inspected["status"], "ok")
                self.assertIn("CLI orchestrator wake", inspected["details"])

                tail = doctor.inspect_transcript_tail(cli_orch_cid, [desktop_brain, cli_brain], now_ts=now_ts)
                self.assertTrue(tail["exists"])
                self.assertEqual(tail["idle_seconds"], 15)

                # Active CLI-spawned orchestrator prevents false `idle_orchestrator_with_runnable_work`
                _write_state_files(
                    state_dir,
                    orchestrator_id=cli_orch_cid,
                    items=[
                        {
                            "conversation_id": "dddd2222-eeee-4222-8222-222222222222",
                            "priority": 10,
                            "status": "pending",
                            "repo_slugs": ["acme/repo-a"],
                            "merged_repos": [],
                            "target_files": ["a.py"],
                            "title": "Handled by active CLI orchestrator",
                        }
                    ],
                    locks={},
                )
                diag = doctor.diagnose_orchestrator(
                    state_dir=state_dir,
                    brain_dir=[desktop_brain, cli_brain],
                    now_ts=now_ts,
                    ps_output="",
                )
                self.assertEqual(diag["overall_status"], "healthy")

                # Active CLI worker transcript mtime prevents false `stalled_worker` in detect_stalled_lock
                cli_worker_cid = "eeee3333-ffff-4333-8333-333333333333"
                _write_transcript(
                    cli_brain,
                    cli_worker_cid,
                    [{"step_index": 1, "type": "RUN_COMMAND", "status": "RUNNING"}],
                    mtime=now_ts - 20.0,
                )
                st_check = flow_opt.detect_stalled_lock(
                    {
                        "active": True,
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "worker_cid": cli_worker_cid,
                        "acquired_at": "2026-11-01T00:00:00Z",
                    },
                    now_ts=now_ts,
                    home_dir=fake_home,
                )
                self.assertFalse(st_check["stalled"])
                self.assertEqual(st_check["idle_seconds"], 20)

    def test_watchdog_sidecar_schema_cycle_and_cli_modes(self) -> None:
        """Validates `sidecar.json` schema and `watchdog.py` healthy 0-LLM no-op, degraded heal, wake, and cooldown."""
        sidecar_manifest = json.loads((WATCHDOG_DIR / "sidecar.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar_manifest["command"], "python3")
        self.assertEqual(sidecar_manifest["args"], ["watchdog.py"])
        self.assertEqual(sidecar_manifest["restart_policy"], "always")
        self.assertIs(sidecar_manifest["has_web_ui"], False)
        self.assertTrue(sidecar_manifest["display_name"])
        self.assertTrue(sidecar_manifest["description"])

        self.assertEqual(watchdog.resolve_watchdog_interval(None, env={}), 300)
        self.assertEqual(watchdog.resolve_watchdog_interval(None, env={"AGY_WATCHDOG_INTERVAL_SECONDS": "120"}), 120)
        self.assertEqual(watchdog.resolve_watchdog_interval(5, env={}), 30)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            brain_dir = root / "brain"
            home_dir = root / "home"
            agentapi_bin = home_dir / ".gemini" / "antigravity" / "bin" / "agentapi"
            agentapi_bin.parent.mkdir(parents=True)
            agentapi_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(agentapi_bin, 0o755)
            now_ts = 1_800_300_000.0

            # 1. Healthy queue: 0-LLM fast path, run_queue_doctor is never called
            _write_state_files(state_dir, orchestrator_id="", items=[], locks={})
            with mock.patch.object(doctor, "run_queue_doctor") as spy_run_doc:
                healthy_res = watchdog.run_watchdog_cycle(
                    state_dir=state_dir,
                    brain_dir=brain_dir,
                    sync_git=False,
                    now_ts=now_ts,
                    ps_output="",
                )
            self.assertEqual(healthy_res["status"], "healthy")
            self.assertFalse(healthy_res["healed"])
            self.assertFalse(healthy_res["woke"])
            spy_run_doc.assert_not_called()

            # Verify CLI `--once --json --no-sync-git`
            cli_buf = io.StringIO()
            with contextlib.redirect_stdout(cli_buf):
                rc_once = watchdog.main(
                    [
                        "--once",
                        "--json",
                        "--no-sync-git",
                        "--state-dir",
                        str(state_dir),
                        "--brain-dir",
                        str(brain_dir),
                    ]
                )
            self.assertEqual(rc_once, 0)
            self.assertEqual(json.loads(cli_buf.getvalue())["status"], "healthy")

            # 2. Degraded phantom_pr_open -> safe heal applied (`healed=True`, `woke=False`)
            _write_state_files(
                state_dir,
                orchestrator_id="",
                items=[
                    {
                        "conversation_id": "ffff1111-aaaa-4111-8111-111111111111",
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                        "merged_repos": ["acme/repo-a"],
                        "pr_urls": [],
                        "title": "Phantom multi-repo item",
                    }
                ],
                locks={
                    "acme/repo-b": {
                        "active": True,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "conversation_id": "ffff2222-bbbb-4222-8222-222222222222",
                        "branch_name": "fix/20261122-070000-other-ffff2222",
                    }
                },
            )
            with mock.patch.dict(os.environ, {"AGY_OFFLINE_ONLY": "1"}):
                healed_res = watchdog.run_watchdog_cycle(
                    state_dir=state_dir,
                    brain_dir=brain_dir,
                    sync_git=False,
                    now_ts=now_ts,
                    ps_output="  PID ARGS\n  901 python3 pr_remediator.py wait-for-unlock\n",
                )
            self.assertEqual(healed_res["status"], "healed")
            self.assertTrue(healed_res["healed"])
            self.assertFalse(healed_res["woke"])

            # 3. Degraded runnable work -> wakes via agentapi on first cycle, then cooldown-gated on second cycle
            _write_state_files(
                state_dir,
                orchestrator_id="",
                items=[
                    {
                        "conversation_id": "ffff3333-cccc-4333-8333-333333333333",
                        "priority": 10,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "merged_repos": [],
                        "target_files": ["main.py"],
                        "title": "Runnable pending item",
                    }
                ],
                locks={},
            )
            mock_popen = mock.MagicMock()
            mock_proc = mock.MagicMock()
            mock_proc.pid = 88888
            mock_popen.return_value = mock_proc
            sidecar_env = {"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:59999"}

            wake_cycle = watchdog.run_watchdog_cycle(
                state_dir=state_dir,
                brain_dir=brain_dir,
                sync_git=False,
                cooldown_seconds=900,
                now_ts=now_ts,
                home_dir=home_dir,
                env=sidecar_env,
                which_fn=lambda _n: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _p: False,
                ps_output="",
            )
            self.assertEqual(wake_cycle["status"], "woke")
            self.assertTrue(wake_cycle["woke"])
            self.assertEqual(wake_cycle["wake"]["transport"], "agentapi")
            self.assertEqual(mock_popen.call_count, 1)

            cooldown_cycle = watchdog.run_watchdog_cycle(
                state_dir=state_dir,
                brain_dir=brain_dir,
                sync_git=False,
                cooldown_seconds=900,
                now_ts=now_ts + 300.0,
                home_dir=home_dir,
                env=sidecar_env,
                which_fn=lambda _n: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _p: False,
                ps_output="",
            )
            self.assertEqual(cooldown_cycle["status"], "cooldown")
            self.assertFalse(cooldown_cycle["woke"])
            self.assertEqual(mock_popen.call_count, 1)

            # 4. Continuous daemon loop survives transient exceptions and exits cleanly on stop_event
            stop_ev = watchdog.threading.Event()
            stop_ev.set()
            err_buf = io.StringIO()
            with (
                mock.patch.object(watchdog, "run_watchdog_cycle", side_effect=RuntimeError("transient fs glitch")),
                contextlib.redirect_stdout(err_buf),
            ):
                rc_daemon = watchdog.main(
                    ["--interval", "30", "--no-sync-git", "--state-dir", str(state_dir)],
                    stop_event=stop_ev,
                )
            self.assertEqual(rc_daemon, 0)
            self.assertIn("status=error", err_buf.getvalue())
            self.assertIn("RuntimeError: transient fs glitch", err_buf.getvalue())

    def test_watchdog_hourly_auto_cleanup_cooldown_pause_and_zero_disable(self) -> None:
        """Verify (2b) cooldown-gated hourly 0-LLM cleanup in `watchdog.run_watchdog_cycle`, pause guard, and `0` disable."""
        self.assertEqual(watchdog.resolve_auto_cleanup_interval(None, env={}), 3600)
        self.assertEqual(watchdog.resolve_auto_cleanup_interval(None, env={"AGY_AUTO_CLEANUP_INTERVAL_SECONDS": "1800"}), 1800)
        self.assertEqual(watchdog.resolve_auto_cleanup_interval(None, env={"AGY_AUTO_CLEANUP_INTERVAL_SECONDS": "0"}), 0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            agy_dir = root / ".gemini" / "antigravity"
            brain_dir = agy_dir / "brain"
            convs_dir = agy_dir / "conversations"
            convs_dir.mkdir(parents=True, exist_ok=True)
            brain_dir.mkdir(parents=True, exist_ok=True)

            now_ts = 1_800_300_000.0
            merged_cid = "71111111-1111-4111-8111-111111111111"
            merged_db = convs_dir / f"{merged_cid}.db"
            merged_db.write_bytes(b"sqlite-stub")
            os.utime(merged_db, (now_ts - 7200.0, now_ts - 7200.0))

            ev_old = agy_dir / "sidecar_data" / "demo-sidecar" / "events" / "old.json"
            ev_old.parent.mkdir(parents=True, exist_ok=True)
            ev_old.write_text("{}\n", encoding="utf-8")
            os.utime(ev_old, (now_ts - 30 * 3600.0, now_ts - 30 * 3600.0))

            _write_state_files(
                state_dir,
                orchestrator_id="",
                items=[{"conversation_id": merged_cid, "priority": 50, "status": "merged", "title": "Merged item"}],
                locks={},
            )

            # 1. Disabled when AGY_AUTO_CLEANUP_INTERVAL_SECONDS=0
            disabled_res = watchdog.run_watchdog_cycle(
                state_dir=state_dir,
                antigravity_dirs=[agy_dir],
                sync_git=False,
                now_ts=now_ts,
                ps_output="",
                env={"AGY_AUTO_CLEANUP_INTERVAL_SECONDS": "0"},
            )
            self.assertEqual(disabled_res["auto_cleanup"]["status"], "disabled")
            self.assertFalse(disabled_res["auto_cleanup"]["ran"])
            self.assertTrue(merged_db.is_file())

            # 2. Disabled when paused
            watchdog._orch_ctrl.pause_orchestration(state_dir=state_dir, reason="test")
            paused_res = watchdog.run_watchdog_cycle(
                state_dir=state_dir,
                antigravity_dirs=[agy_dir],
                sync_git=False,
                now_ts=now_ts,
                ps_output="",
            )
            self.assertEqual(paused_res["status"], "paused")
            self.assertNotIn("auto_cleanup", paused_res)
            self.assertTrue(merged_db.is_file())
            watchdog._orch_ctrl.resume_orchestration(state_dir=state_dir, request_wake=False)

            # 3. Active & interval elapsed -> runs cleanup, then cooldown-gates within 3600s
            ran_res = watchdog.run_watchdog_cycle(
                state_dir=state_dir,
                antigravity_dirs=[agy_dir],
                sync_git=False,
                now_ts=now_ts,
                ps_output="",
                env={"AGY_AUTO_CLEANUP_INTERVAL_SECONDS": "3600"},
            )
            self.assertEqual(ran_res["status"], "healthy")
            self.assertTrue(ran_res["auto_cleanup"]["ran"])
            self.assertEqual(ran_res["auto_cleanup"]["deleted_count"], 1)
            self.assertEqual(ran_res["auto_cleanup"]["sidecar_events_pruned_count"], 1)
            self.assertFalse(merged_db.exists())
            self.assertFalse(ev_old.exists())

            cd_res = watchdog.run_watchdog_cycle(
                state_dir=state_dir,
                antigravity_dirs=[agy_dir],
                sync_git=False,
                now_ts=now_ts + 1200.0,
                ps_output="",
                env={"AGY_AUTO_CLEANUP_INTERVAL_SECONDS": "3600"},
            )
            self.assertEqual(cd_res["auto_cleanup"]["status"], "cooldown")
            self.assertFalse(cd_res["auto_cleanup"]["ran"])

            # 4. After 3600s -> runs again
            ev_old_2 = agy_dir / "sidecar_data" / "demo-sidecar" / "events" / "old2.json"
            ev_old_2.write_text("{}\n", encoding="utf-8")
            os.utime(ev_old_2, (now_ts - 30 * 3600.0, now_ts - 30 * 3600.0))
            next_res = watchdog.run_watchdog_cycle(
                state_dir=state_dir,
                antigravity_dirs=[agy_dir],
                sync_git=False,
                now_ts=now_ts + 3601.0,
                ps_output="",
                env={"AGY_AUTO_CLEANUP_INTERVAL_SECONDS": "3600"},
            )
            self.assertTrue(next_res["auto_cleanup"]["ran"])
            self.assertEqual(next_res["auto_cleanup"]["sidecar_events_pruned_count"], 1)
            self.assertFalse(ev_old_2.exists())


class TestStandbyDependencyDeadlockAndNSlotLookahead(unittest.TestCase):
    """Hermetic tests for `standby_dependency_deadlock` (optimizer + doctor) and N-slot optimizer lookahead."""

    CID_STANDBY = "aaaa0001-0000-4000-8000-000000000001"
    CID_CANCELLED = "aaaa0002-0000-4000-8000-000000000002"
    CID_FREE = "bbbb0001-0000-4000-8000-000000000001"
    CID_LOCKED = "bbbb0002-0000-4000-8000-000000000002"
    CID_TRANSITIVE = "bbbb0003-0000-4000-8000-000000000003"
    CID_MIXED = "bbbb0004-0000-4000-8000-000000000004"
    CID_MERGED = "cccc0001-0000-4000-8000-000000000001"

    def _deadlock_items(self) -> list[dict[str, object]]:
        return [
            {"conversation_id": self.CID_STANDBY, "title": "Parked prerequisite", "priority": 20, "status": "standby", "repo_slugs": ["owner/repo-a"]},
            {"conversation_id": self.CID_CANCELLED, "title": "Dropped prerequisite", "priority": 20, "status": "cancelled", "repo_slugs": ["owner/repo-b"]},
            {"conversation_id": self.CID_MERGED, "title": "Done prerequisite", "priority": 20, "status": "merged", "repo_slugs": ["owner/repo-a"]},
            # Free repo (no active lock) -> high severity
            {"conversation_id": self.CID_FREE, "title": "Free repo dependent", "priority": 10, "status": "pending", "repo_slugs": ["owner/repo-free"], "target_files": ["src/a.py"], "depends_on": [self.CID_STANDBY[:8]]},
            # Repo with an active lock
            {"conversation_id": self.CID_LOCKED, "title": "Locked repo dependent", "priority": 12, "status": "pending", "repo_slugs": ["owner/repo-locked"], "target_files": ["src/b.py"], "depends_on": [self.CID_CANCELLED]},
            # Transitive: waits on a deadlocked pending item
            {"conversation_id": self.CID_TRANSITIVE, "title": "Transitive dependent", "priority": 30, "status": "pending", "repo_slugs": ["owner/repo-free"], "target_files": ["src/c.py"], "depends_on": [self.CID_FREE]},
            # Mixed: merged dep kept, cancelled dep dropped via --replace-depends-on
            {"conversation_id": self.CID_MIXED, "title": "Mixed deps", "priority": 40, "status": "pending", "repo_slugs": ["owner/repo-free2"], "target_files": ["src/d.py"], "depends_on": [self.CID_MERGED, self.CID_CANCELLED]},
        ]

    def _locked_repo_lock(self) -> dict[str, dict[str, object]]:
        return {
            "owner/repo-locked": {
                "active": True,
                "repo_slug": "owner/repo-locked",
                "conversation_id": "dddd0001-0000-4000-8000-000000000001",
                "branch_name": "fix/20261124-locked-dddd0001",
                "status": "DEVELOPING",
                "target_files": ["src/other.py"],
            }
        }

    def test_optimizer_detects_standby_dependency_deadlock_with_hints(self) -> None:
        report = flow_opt.analyze_dev_flow_optimizations(
            lightweight=True,
            preloaded_items=self._deadlock_items(),
            preloaded_active_locks=self._locked_repo_lock(),
            preloaded_repo_dirs={},
        )
        by_code = {f["code"]: f for f in report["findings"]}
        self.assertIn("standby_dependency_deadlock", by_code)
        finding = by_code["standby_dependency_deadlock"]
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["category"], "throughput")
        ev = finding["evidence"]
        self.assertEqual(ev["count"], 4)
        self.assertEqual(ev["free_repo_count"], 3)
        self.assertEqual(set(ev["conversation_ids"]), {self.CID_FREE, self.CID_LOCKED, self.CID_TRANSITIVE, self.CID_MIXED})
        self.assertEqual(ev["blocking_dependency_statuses"][self.CID_STANDBY], "standby")
        self.assertEqual(ev["blocking_dependency_statuses"][self.CID_CANCELLED], "cancelled")
        self.assertNotIn(self.CID_MERGED, ev["blocking_dependency_ids"])

        items_by_cid = {o["conversation_id"]: o for o in ev["items"]}
        # Free-repo offenders rank first; locked-repo offender is flagged as not free
        self.assertTrue(items_by_cid[self.CID_FREE]["on_free_repo"])
        self.assertFalse(items_by_cid[self.CID_LOCKED]["on_free_repo"])
        self.assertEqual(ev["items"][0]["conversation_id"], self.CID_FREE)
        self.assertEqual(items_by_cid[self.CID_TRANSITIVE]["blocking_dependencies"], [])
        self.assertEqual(items_by_cid[self.CID_TRANSITIVE]["transitive_via"], [self.CID_FREE])
        self.assertEqual(items_by_cid[self.CID_TRANSITIVE]["root_blockers"][0]["conversation_id"], self.CID_STANDBY)

        hints = ev["remediation_hints"]
        self.assertIn(f"pr_remediator.py queue-add --id {self.CID_FREE} --clear-depends-on", hints)
        self.assertIn(f"pr_remediator.py queue-set-status --id {self.CID_STANDBY} --status pending", hints)
        self.assertIn(f"pr_remediator.py queue-add --id {self.CID_MIXED} --replace-depends-on {self.CID_MERGED}", hints)
        self.assertEqual(finding["auto_action_hint"], f"pr_remediator.py queue-add --id {self.CID_FREE} --clear-depends-on")
        self.assertIn("dep deadlock (4, 3 free repo)", flow_opt.format_compact_opt_hint({"findings": [finding]}))

    def test_optimizer_deadlock_medium_when_only_locked_repos_and_absent_when_healthy(self) -> None:
        items = [it for it in self._deadlock_items() if it["conversation_id"] in (self.CID_CANCELLED, self.CID_LOCKED)]
        report = flow_opt.analyze_dev_flow_optimizations(
            lightweight=True,
            preloaded_items=items,
            preloaded_active_locks=self._locked_repo_lock(),
            preloaded_repo_dirs={},
        )
        by_code = {f["code"]: f for f in report["findings"]}
        self.assertEqual(by_code["standby_dependency_deadlock"]["severity"], "medium")
        self.assertEqual(by_code["standby_dependency_deadlock"]["evidence"]["free_repo_count"], 0)

        healthy_items = [
            {"conversation_id": self.CID_MERGED, "status": "merged", "repo_slugs": ["owner/repo-a"]},
            {"conversation_id": self.CID_FREE, "status": "pending", "repo_slugs": ["owner/repo-a"], "target_files": ["src/a.py"], "depends_on": [self.CID_MERGED]},
            {"conversation_id": self.CID_STANDBY, "status": "standby", "repo_slugs": ["owner/repo-a"]},
        ]
        healthy = flow_opt.analyze_dev_flow_optimizations(
            lightweight=True,
            preloaded_items=healthy_items,
            preloaded_active_locks={},
            preloaded_repo_dirs={},
        )
        self.assertNotIn("standby_dependency_deadlock", {f["code"] for f in healthy["findings"]})

    def test_doctor_surfaces_standby_dependency_deadlock_anomaly_without_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            items = [it for it in self._deadlock_items() if it["conversation_id"] in (self.CID_STANDBY, self.CID_FREE)]
            _write_state_files(state_dir, items=items, locks={})

            diag = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                ps_output="  PID ARGS\n  100 /sbin/launchd\n",
            )
            by_code = {a["code"]: a for a in diag["anomalies"]}
            self.assertIn("standby_dependency_deadlock", by_code)
            anomaly = by_code["standby_dependency_deadlock"]
            self.assertEqual(anomaly["severity"], "high")
            self.assertFalse(anomaly["requires_wake"])
            self.assertFalse(anomaly["auto_fixable"])
            self.assertEqual(anomaly["safe_fix_action"], f"pr_remediator.py queue-add --id {self.CID_FREE} --clear-depends-on")
            self.assertIn(f"{self.CID_STANDBY[:8]}(standby)", anomaly["cause"])
            self.assertIn(
                f"pr_remediator.py queue-set-status --id {self.CID_STANDBY} --status pending",
                anomaly["evidence"]["remediation_hints"],
            )
            self.assertEqual(diag["overall_status"], "degraded")
            self.assertFalse(diag["needs_wake"])
            # Owned by the doctor -> not duplicated as a PR-only improvement
            self.assertNotIn("standby_dependency_deadlock", {i["code"] for i in diag["improvements"]})
            self.assertIn("standby_dependency_deadlock", doctor.render_doctor_report(diag, color=False))

    def _three_slot_repo(self, root: Path) -> Path:
        repo_dir = root / "owner-repo"
        (repo_dir / "src").mkdir(parents=True)
        for name in ("alpha", "beta", "gamma", "delta"):
            (repo_dir / "src" / f"{name}.py").write_text(f"def {name}() -> int:\n    return 1\n", encoding="utf-8")
        return repo_dir

    def _three_slot_lock(self, *, max_slots: int, with_slot3: bool = False) -> dict[str, dict[str, object]]:
        slots: dict[str, dict[str, object]] = {
            "1": {"slot_id": 1, "active": True, "conversation_id": "eeee0001-0000-4000-8000-000000000001", "status": "PENDING_HUMAN_PR_VALIDATION", "branch_name": "fix/20261124-alpha-eeee0001", "target_files": ["src/alpha.py"]},
            "2": {"slot_id": 2, "active": True, "conversation_id": "eeee0002-0000-4000-8000-000000000002", "status": "READY_AWAITING_BASE_MERGE", "branch_name": "fix/20261124-beta-eeee0002", "target_files": ["src/beta.py"]},
        }
        if with_slot3:
            slots["3"] = {"slot_id": 3, "active": True, "conversation_id": "eeee0003-0000-4000-8000-000000000003", "status": "DEVELOPING", "branch_name": "fix/20261124-gamma-eeee0003", "target_files": ["src/gamma.py"]}
        return {
            "owner/repo": {
                "active": True,
                "repo_slug": "owner/repo",
                "max_slots": max_slots,
                "conversation_id": "eeee0001-0000-4000-8000-000000000001",
                "branch_name": "fix/20261124-alpha-eeee0001",
                "status": "PENDING_HUMAN_PR_VALIDATION",
                "pr_gate_ready": True,
                "target_files": ["src/alpha.py"],
                "slots": slots,
            }
        }

    def test_optimizer_parallel_lookahead_targets_free_slot3_with_pairwise_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = self._three_slot_repo(Path(tmp))
            items = [
                # Collides with Slot #2 (`src/beta.py`) -> must be rejected even though it is disjoint from Slot #1
                {"conversation_id": "ffff0001-0000-4000-8000-000000000001", "title": "Beta collision", "priority": 5, "status": "pending", "repo_slugs": ["owner/repo"], "target_files": ["src/beta.py"]},
                {"conversation_id": "ffff0002-0000-4000-8000-000000000002", "title": "Delta feature", "priority": 9, "status": "pending", "repo_slugs": ["owner/repo"], "target_files": ["src/delta.py"]},
            ]
            report = flow_opt.analyze_dev_flow_optimizations(
                lightweight=True,
                preloaded_items=items,
                preloaded_active_locks=self._three_slot_lock(max_slots=3),
                preloaded_repo_dirs={"owner/repo": repo_dir},
            )
            by_code = {f["code"]: f for f in report["findings"]}
            self.assertIn("parallel_lookahead_opportunity", by_code)
            finding = by_code["parallel_lookahead_opportunity"]
            ev = finding["evidence"]
            self.assertEqual(ev["top_candidate_id"], "ffff0002-0000-4000-8000-000000000002")
            self.assertEqual(ev["free_slot_id"], 3)
            self.assertEqual(ev["next_slot_id"], 3)
            op = ev["opportunities"][0]
            self.assertEqual(op["candidate_count"], 1)
            self.assertEqual(op["active_slot_ids"], [1, 2])
            self.assertEqual(op["max_slots"], 3)
            self.assertIn("Slot #3", finding["title"])
            self.assertIn("--allow-slot2", finding["recommendation"])
            self.assertIn("--parallel-slot", finding["recommendation"])
            self.assertIn("slot3 lookahead repo", flow_opt.format_compact_opt_hint({"findings": [finding]}))

            # Repo at capacity (3/3 active) -> no opportunity
            full = flow_opt.analyze_dev_flow_optimizations(
                lightweight=True,
                preloaded_items=items,
                preloaded_active_locks=self._three_slot_lock(max_slots=3, with_slot3=True),
                preloaded_repo_dirs={"owner/repo": repo_dir},
            )
            self.assertNotIn("parallel_lookahead_opportunity", {f["code"] for f in full["findings"]})

            # 4-slot repo with 3 active: candidate colliding with Slot #3 is rejected, disjoint one targets Slot #4
            items_four = [
                {"conversation_id": "ffff0003-0000-4000-8000-000000000003", "title": "Gamma collision", "priority": 5, "status": "pending", "repo_slugs": ["owner/repo"], "target_files": ["src/gamma.py"]},
                items[1],
            ]
            four = flow_opt.analyze_dev_flow_optimizations(
                lightweight=True,
                preloaded_items=items_four,
                preloaded_active_locks=self._three_slot_lock(max_slots=4, with_slot3=True),
                preloaded_repo_dirs={"owner/repo": repo_dir},
            )
            four_ev = {f["code"]: f for f in four["findings"]}["parallel_lookahead_opportunity"]["evidence"]
            self.assertEqual(four_ev["free_slot_id"], 4)
            self.assertEqual(four_ev["top_candidate_id"], "ffff0002-0000-4000-8000-000000000002")
            self.assertEqual(four_ev["opportunities"][0]["candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
