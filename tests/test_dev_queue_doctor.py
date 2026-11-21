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

for _p in (CLIENTS_DIR, REM_SCRIPTS_DIR, TUI_SCRIPTS_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import dev_common  # noqa: E402
import dev_queue_doctor as doctor  # noqa: E402
import dev_queue_tui as tui  # noqa: E402
import pr_remediator as prs  # noqa: E402


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
            res_dry = doctor.run_queue_doctor(
                state_dir=state_dir,
                brain_dir=brain_dir,
                wake=True,
                dry_run=True,
                now_ts=now_ts,
                ps_output="",
                home_dir=home_dir,
                which_fn=lambda _name: None,
                popen_fn=mock_popen,
            )
            self.assertEqual(res_dry["wake"]["status"], "dry_run")
            self.assertEqual(
                res_dry["wake"]["command"][:4],
                [str(agy_bin_path), "--agent", "agy-dev-orchestrator", "--mode"],
            )
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
                which_fn=lambda _name: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _pid: False,
            )
            self.assertEqual(res_first["wake"]["status"], "woke")
            self.assertTrue(res_first["wake"]["woke"])
            self.assertEqual(res_first["wake"]["pid"], 43210)
            self.assertEqual(mock_popen.call_count, 1)
            called_cmd = mock_popen.call_args[0][0]
            self.assertEqual(
                called_cmd[:5],
                [str(agy_bin_path), "--agent", "agy-dev-orchestrator", "--mode", "auto"],
            )
            self.assertEqual(called_cmd[5], "-p")
            self.assertTrue(mock_popen.call_args[1]["start_new_session"])

            st_file = state_dir / doctor.DOCTOR_STATE_FILENAME
            self.assertTrue(st_file.is_file())
            self.assertEqual(st_file.stat().st_mode & 0o777, 0o600)
            saved_state = json.loads(st_file.read_text(encoding="utf-8"))
            self.assertEqual(saved_state["last_wake_pid"], 43210)
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


if __name__ == "__main__":
    unittest.main()
