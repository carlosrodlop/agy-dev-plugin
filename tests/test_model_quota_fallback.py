"""Hermetic Zero-PII unit tests for model routing, preventive low-quota Eco, 429 cooldown fallback, 150-step rotation & agy-doctor blind spots."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
WATCHDOG_PATH = REPO_ROOT / "sidecars" / "dev-queue-watchdog" / "watchdog.py"

for _p in (str(CLIENTS_DIR), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dev_common  # noqa: E402
import dev_flow_optimizer as dfo  # noqa: E402
import dev_queue_doctor as doctor  # noqa: E402
import merge_watcher as mw  # noqa: E402
import model_quota as mq  # noqa: E402
import pr_remediator as prs  # noqa: E402


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


watchdog = _load_module("watchdog_quota_under_test", WATCHDOG_PATH)


def _write_transcript(brain_dir: Path, cid: str, lines: list[dict[str, object]], *, mtime: float | None = None) -> Path:
    tpath = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
    tpath.parent.mkdir(parents=True, exist_ok=True)
    tpath.write_text("\n".join(json.dumps(obj) for obj in lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(tpath, (mtime, mtime))
    return tpath


def _write_state(
    state_dir: Path,
    *,
    orchestrator_id: str = "",
    items: list[dict[str, object]] | None = None,
    locks: dict[str, dict[str, object]] | None = None,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "dev_queue.json").write_text(
        json.dumps(
            {
                "updated_at": "2026-11-24T12:00:00Z",
                "orchestrator_conversation_id": orchestrator_id,
                "items": list(items or []),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (state_dir / "actions_state.json").write_text(
        json.dumps({"actions_log_audit_state": {"dev_pr_locks": dict(locks or {})}}, indent=2),
        encoding="utf-8",
    )


class TestModelQuotaCoreAndEcoFallback(unittest.TestCase):
    """Verify unlimited-first routing, preventive <=25% low-quota Eco, 429 cooldowns & 0600 persistence."""

    def test_parsers_and_cooldown_pruning_with_0600_permissions(self) -> None:
        self.assertEqual(mq.parse_model_list("auto, hydrogen-l-tf-high , auto"), ["auto", "hydrogen-l-tf-high"])
        self.assertEqual(mq.parse_quota_percent("43%"), (43.0, False))
        self.assertEqual(mq.parse_quota_percent("unlimited"), (100.0, True))
        self.assertEqual(mq.parse_reset_duration_seconds("Resets in 5h 49m 32s"), 5 * 3600 + 49 * 60 + 32)

        with tempfile.TemporaryDirectory() as tmp:
            st_dir = Path(tmp) / "state"
            now_ts = 1_800_000_000.0
            rec = mq.record_model_quota_cooldown(
                "gemini-4-carbon-high",
                resets_in_seconds=600,
                state_dir=st_dir,
                now_ts=now_ts,
            )
            self.assertEqual(rec["status"], "ok")
            q_file = st_dir / mq.MODEL_QUOTA_COOLDOWNS_FILENAME
            self.assertTrue(q_file.is_file())
            self.assertEqual(stat.S_IMODE(q_file.stat().st_mode), 0o600)

            loaded_active = mq.load_model_quota_state(state_dir=st_dir, now_ts=now_ts + 120)
            self.assertIn("gemini-4-carbon-high", loaded_active["cooldowns"])
            self.assertIn("pro", loaded_active["cooldowns"])

            loaded_expired = mq.load_model_quota_state(state_dir=st_dir, now_ts=now_ts + 700)
            self.assertEqual(loaded_expired["cooldowns"], {})

    def test_unlimited_first_preventive_low_quota_eco_and_all_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st_dir = Path(tmp) / "state"
            now_ts = 1_800_000_000.0

            # 1. Unlimited model always wins #1
            sel_unl = mq.select_next_optimal_model(
                role="reviewer",
                state_dir=st_dir,
                now_ts=now_ts,
                env={"AGY_DEV_UNLIMITED_MODEL_IDS": "acme-unlimited-high"},
            )
            self.assertEqual(sel_unl["selected_model"], "acme-unlimited-high")
            self.assertEqual(sel_unl["mode"], "unlimited")
            self.assertTrue(sel_unl["requires_swarm_dispatch"])

            # 2. Preventive low-quota Eco (pro pool at 20% <= 25% threshold -> skips pro to healthy auto at 93%)
            live_low_pro = {
                "quotas": {
                    "gemini-4-carbon-high": {"quota_remaining": "20%", "quota_resets": "2026-11-24T19:00:00Z"},
                    "auto": {"quota_remaining": "93%", "quota_resets": "2026-11-25T03:00:00Z"},
                    "gemini-flash-next": {"quota_remaining": "100%", "quota_resets": "2026-11-25T07:00:00Z"},
                }
            }
            sel_eco_rev = mq.select_next_optimal_model(
                role="reviewer",
                live_quotas=live_low_pro,
                state_dir=st_dir,
                now_ts=now_ts,
                env={"AGY_DEV_LOW_QUOTA_THRESHOLD_PCT": "25"},
            )
            self.assertEqual(sel_eco_rev["selected_model"], "auto")
            self.assertEqual(sel_eco_rev["mode"], "eco_low_quota")
            self.assertTrue(sel_eco_rev["low_quota_triggered"])

            # 3. Developer Round 1 uses flash normally when flash is healthy (100%)
            sel_dev_r1 = mq.select_next_optimal_model(
                role="developer",
                live_quotas=live_low_pro,
                state_dir=st_dir,
                review_iteration=1,
                now_ts=now_ts,
                env={},
            )
            self.assertEqual(sel_dev_r1["selected_model"], "flash")
            self.assertEqual(sel_dev_r1["subagent_model"], "flash")
            self.assertEqual(sel_dev_r1["mode"], "normal")

            # 4. Snapshot persistence records 0% cooldowns and clears recovered ones
            snap_res = mq.record_swarm_quota_snapshot(
                {
                    "quotas": {
                        "gemini-4-carbon-high": {"quota_remaining": "0%", "quota_resets": "2026-11-24T23:00:00Z"},
                        "auto": {"quota_remaining": "0%", "quota_resets": "2026-11-24T22:00:00Z"},
                    }
                },
                state_dir=st_dir,
                now_ts=now_ts,
            )
            self.assertEqual(snap_res["status"], "ok")
            self.assertIn("gemini-4-carbon-high", snap_res["zero_quota_models"])

            # 5. All candidates exhausted -> all_exhausted=True with earliest next_reset_at
            sel_all_zero = mq.select_next_optimal_model(
                role="wake",
                state_dir=st_dir,
                now_ts=now_ts,
                env={"AGY_DEV_MODEL_FALLBACK_ORDER": "auto,gemini-4-carbon-high"},
            )
            self.assertTrue(sel_all_zero["all_exhausted"])
            self.assertEqual(sel_all_zero["selected_model"], "")
            self.assertEqual(sel_all_zero["mode"], "all_exhausted")
            self.assertTrue(sel_all_zero["next_reset_at"])

    def test_transcript_429_detection_and_recovery_clearing(self) -> None:
        now_ts = 1_800_000_000.0
        lines_429 = [
            json.dumps({"type": "PLANNER_RESPONSE", "status": "DONE", "content": "starting"}),
            json.dumps(
                {
                    "type": "ERROR",
                    "status": "ERROR",
                    "content": "HTTP 429 RESOURCE_EXHAUSTED on gemini-4-carbon-high: Individual quota reached. Resets in 2h 30m.",
                }
            ),
        ]
        hit = mq.detect_quota_exhaustion_in_lines(lines_429, default_model="pro", now_ts=now_ts, recovery_line_index=-1)
        self.assertTrue(hit["quota_exhausted"])
        self.assertEqual(hit["exhausted_model"], "gemini-4-carbon-high")
        self.assertEqual(hit["resets_in_seconds"], 2 * 3600 + 30 * 60)
        self.assertTrue(hit["resets_at"])

        # Subsequent recovery step clears the 429 flag
        cleared = mq.detect_quota_exhaustion_in_lines(lines_429, default_model="pro", now_ts=now_ts, recovery_line_index=5)
        self.assertFalse(cleared["quota_exhausted"])


class TestReviewGateFlashFirstAndEscalation(unittest.TestCase):
    """Verify flash-first Round 1 dev default, Round 2+ pro escalation, reviewer fallback & quota-status CLI."""

    def test_developer_defaults_to_flash_and_escalates_to_pro_on_round_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st_dir = Path(tmp) / "state"
            r1 = prs.build_code_developer_subagent_plan(
                conversation_id="11111111-2222-4333-8444-555555555555",
                action_id="acme-feature",
                portfolio_dir=st_dir,
            )
            self.assertEqual(r1["subagent_invocation"]["Model"], "flash")
            self.assertFalse(r1["model_routing"]["escalated_from_flash"])

            r2 = prs.build_code_developer_subagent_plan(
                conversation_id="11111111-2222-4333-8444-555555555555",
                action_id="acme-feature",
                review_iteration=2,
                portfolio_dir=st_dir,
            )
            self.assertEqual(r2["subagent_invocation"]["Model"], "pro")
            self.assertTrue(r2["model_routing"]["escalated_from_flash"])

            rem_plan = prs._build_developer_remediation_plan(
                conversation_id="11111111-2222-4333-8444-555555555555",
                action_id="acme-feature",
                repo_slug="acme/repo",
                branch_name="fix/20261124-120000-acme-11111111",
                workdir=str(Path(tmp) / "wt"),
                review_iteration=1,
                verdict="NEEDS_REVISION",
                score=82,
                reviewer_feedback="Add edge-case test.",
                rca_summary="Bug",
                proposed_fix="Fix",
                portfolio_dir=st_dir,
            )
            self.assertEqual(rem_plan["next_dev_round"], 2)
            self.assertEqual(rem_plan["developer_subagent_invocation"]["Model"], "pro")
            self.assertTrue(rem_plan["developer_model_routing"]["escalated_from_flash"])

    def test_reviewer_plan_fallback_and_quota_status_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st_dir = Path(tmp) / "state"
            swarm_payload = {
                "quotas": {
                    "gemini-4-carbon-high": {"quota_remaining": "15%", "quota_resets": "2026-11-24T20:00:00Z"},
                    "hydrogen-l-tf-high": {"quota_remaining": "92%", "quota_resets": "2026-11-24T21:00:00Z"},
                    "auto": {"quota_remaining": "10%", "quota_resets": "2026-11-25T03:00:00Z"},
                }
            }
            buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(st_dir), "AGY_DEV_LOW_QUOTA_THRESHOLD_PCT": "25"}),
                contextlib.redirect_stdout(buf),
            ):
                rc = prs.main(
                    [
                        "quota-status",
                        "--role",
                        "reviewer",
                        "--record-swarm-json",
                        json.dumps(swarm_payload),
                    ]
                )
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["status"], "ok")
            self.assertEqual(out["routing"]["selected_model"], "hydrogen-l-tf-high")
            self.assertEqual(out["routing"]["mode"], "eco_low_quota")
            self.assertTrue(out["state"]["snapshot_fresh"])

            rev_plan = prs.build_pr_reviewer_subagent_plan(
                conversation_id="11111111-2222-4333-8444-555555555555",
                branch_name="fix/20261124-120000-acme-11111111",
                portfolio_dir=st_dir,
            )
            self.assertEqual(rev_plan["model_routing"]["selected_model"], "hydrogen-l-tf-high")
            self.assertIn('--reviewer-model "hydrogen-l-tf-high"', rev_plan["subagent_invocation"]["Prompt"])


class TestDoctorBlindSpotsWatcherAndRotation(unittest.TestCase):
    """Verify all 4 agy-doctor/merge_watcher blind spots, quota wakes, watchdog suppression & 150-step rotation."""

    def test_early_orchestrator_rotation_threshold_150_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "state"
            brain_dir = root / "brain"
            orch_cid = "aaaaaaaa-1111-4222-8333-444444444444"
            _write_state(pdir, orchestrator_id=orch_cid, items=[], locks={})
            _write_transcript(
                brain_dir,
                orch_cid,
                [{"type": "PLANNER_RESPONSE", "status": "DONE"} for _ in range(155)],
            )
            rep_default = dfo.analyze_dev_flow_optimizations(
                portfolio_dir=pdir,
                brain_dir=brain_dir,
                orchestrator_cid=orch_cid,
            )
            by_code = {f["code"]: f for f in rep_default["findings"]}
            self.assertIn("bloated_orchestrator_session", by_code)
            self.assertEqual(by_code["bloated_orchestrator_session"]["evidence"]["threshold"], 150)

            with mock.patch.dict(os.environ, {"AGY_DEV_MAX_ORCHESTRATOR_STEPS": "200"}):
                rep_override = dfo.analyze_dev_flow_optimizations(
                    portfolio_dir=pdir,
                    brain_dir=brain_dir,
                    orchestrator_cid=orch_cid,
                )
            self.assertNotIn("bloated_orchestrator_session", {f["code"] for f in rep_override["findings"]})

    def test_doctor_detects_awaiting_review_429_and_slot2_stalls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st_dir = root / "state"
            brain_dir = root / "brain"
            now_ts = 1_800_200_000.0

            rev_cid = "bbbbbbbb-1111-4222-8333-444444444444"
            s2_worker_cid = "cccccccc-1111-4222-8333-444444444444"
            _write_transcript(
                brain_dir,
                rev_cid,
                [
                    {
                        "type": "ERROR",
                        "status": "ERROR",
                        "content": "429 RESOURCE_EXHAUSTED on gemini-4-neon-high. Resets in 3h 15m.",
                    }
                ],
                mtime=now_ts - 1800.0,
            )
            _write_transcript(
                brain_dir,
                s2_worker_cid,
                [{"type": "PLANNER_RESPONSE", "status": "DONE", "content": "stuck"}],
                mtime=now_ts - 2400.0,
            )
            _write_state(
                st_dir,
                items=[],
                locks={
                    "acme/repo-a": {
                        "active": True,
                        "slot_id": 1,
                        "status": "AWAITING_AGENT_REVIEW",
                        "pr_gate_ready": False,
                        "branch_name": "fix/20261124-100000-rev-11111111",
                        "conversation_id": "11111111-1111-4111-8111-111111111111",
                        "reviewer_subagent_id": rev_cid,
                        "acquired_at": "2026-11-24T09:00:00Z",
                        "last_activity_at": "2026-11-24T09:30:00Z",
                        "slots": {
                            "1": {
                                "slot_id": 1,
                                "active": True,
                                "status": "AWAITING_AGENT_REVIEW",
                                "pr_gate_ready": False,
                                "branch_name": "fix/20261124-100000-rev-11111111",
                                "conversation_id": "11111111-1111-4111-8111-111111111111",
                                "reviewer_subagent_id": rev_cid,
                                "acquired_at": "2026-11-24T09:00:00Z",
                                "last_activity_at": "2026-11-24T09:30:00Z",
                            },
                            "2": {
                                "slot_id": 2,
                                "active": True,
                                "status": "DEVELOPING",
                                "pr_gate_ready": False,
                                "branch_name": "fix/20261124-101000-s2-22222222",
                                "conversation_id": "22222222-2222-4222-8222-222222222222",
                                "worker_cid": s2_worker_cid,
                                "acquired_at": "2026-11-24T09:05:00Z",
                                "last_activity_at": "2026-11-24T09:10:00Z",
                            },
                        },
                    }
                },
            )

            diag = doctor.diagnose_orchestrator(
                state_dir=st_dir,
                brain_dir=brain_dir,
                now_ts=now_ts,
                ps_output="",
            )
            codes = [a["code"] for a in diag["anomalies"]]
            self.assertIn("stalled_worker", codes)
            self.assertIn("quota_exhausted", codes)
            stalled_slots = {
                a["evidence"]["slot_id"]
                for a in diag["anomalies"]
                if a["code"] == "stalled_worker"
            }
            self.assertEqual(stalled_slots, {1, 2})
            self.assertEqual(len(diag["detected_quota_cooldowns"]), 1)
            self.assertEqual(diag["detected_quota_cooldowns"][0]["model"], "gemini-4-neon-high")

    def test_stale_follower_auto_fix_and_wait_for_unlock_stall_clamping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st_dir = root / "state"
            now_ts = 1_800_300_000.0
            _write_state(
                st_dir,
                items=[],
                locks={
                    "acme/repo-b": {
                        "active": True,
                        "slot_id": 1,
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "needs_base_rebase": True,
                        "branch_name": "fix/20261124-100000-stale-33333333",
                        "conversation_id": "33333333-3333-4333-8333-333333333333",
                        "worktree_path": str(root / "wt"),
                    }
                },
            )

            diag = doctor.diagnose_orchestrator(state_dir=st_dir, brain_dir=root / "brain", now_ts=now_ts, ps_output="")
            stale_anoms = [a for a in diag["anomalies"] if a["code"] == "stale_follower_needs_rebase"]
            self.assertEqual(len(stale_anoms), 1)
            self.assertTrue(stale_anoms[0]["auto_fixable"])
            self.assertFalse(stale_anoms[0]["requires_wake"])

            # apply_safe_fixes invokes run_slot_rebase_reverify with 0 LLM tokens
            live_pr = doctor._load_pr_remediator()
            with mock.patch.object(
                live_pr,
                "run_slot_rebase_reverify",
                return_value={
                    "status": "ok",
                    "outcome": "AUTO_PROMOTED_ZERO_LLM",
                    "promoted_to_human_pr": True,
                },
            ) as mock_rebase:
                fix_res = doctor.apply_safe_fixes(state_dir=st_dir, sync_git=False)
            mock_rebase.assert_called_once()
            rb_acts = [a for a in fix_res["remediation_actions"] if a["action"] == "rebase_and_reverify_stale_follower"]
            self.assertEqual(len(rb_acts), 1)
            self.assertEqual(rb_acts[0]["outcome"], "AUTO_PROMOTED_ZERO_LLM")
            self.assertFalse(rb_acts[0]["requires_wake"])

            # merge_watcher detects needs_base_rebase AND clamps excessive --stall-seconds=36000
            warn_buf = io.StringIO()
            with mock.patch.object(
                live_pr,
                "sync_dev_pr_lock_with_git",
                return_value={"status": "ok", "released": False, "locks": {}},
            ):
                wait_res = mw.wait_for_unlock_or_actionable_event(
                    poll_interval_seconds=0.01,
                    max_duration_seconds=5.0,
                    max_iterations=1,
                    stall_threshold_seconds=36000,
                    portfolio_dir=st_dir,
                    warn_stream=warn_buf,
                )
            self.assertEqual(wait_res["event"], "slot2_needs_rebase_reverify")
            self.assertEqual(len(wait_res["rebase_reverify_locks"]), 1)
            self.assertTrue(wait_res.get("stall_threshold_clamped"))
            self.assertEqual(wait_res.get("effective_stall_seconds"), mw.MAX_SAFE_STALL_THRESHOLD_SECONDS)
            self.assertIn("clamping", warn_buf.getvalue().lower())

    def test_wake_model_flag_and_all_exhausted_watchdog_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st_dir = root / "state"
            brain_dir = root / "brain"
            home_dir = root / "home"
            agentapi_path = home_dir / ".gemini" / "antigravity" / "bin" / "agentapi"
            agentapi_path.parent.mkdir(parents=True)
            agentapi_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(agentapi_path, 0o755)
            now_ts = 1_800_400_000.0

            _write_state(
                st_dir,
                items=[
                    {
                        "conversation_id": "44444444-4444-4444-8444-444444444444",
                        "priority": 10,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-c"],
                        "title": "Wake with fallback model",
                    }
                ],
                locks={},
            )
            # Exhaust auto -> wake should pin --model=hydrogen-l-tf-high
            mq.record_model_quota_cooldown("auto", resets_in_seconds=3600, state_dir=st_dir, now_ts=now_ts)
            mock_popen = mock.MagicMock()
            mock_proc = mock.MagicMock()
            mock_proc.pid = 77777
            mock_popen.return_value = mock_proc
            res_wake = doctor.run_queue_doctor(
                state_dir=st_dir,
                brain_dir=brain_dir,
                wake=True,
                now_ts=now_ts,
                ps_output="",
                home_dir=home_dir,
                env={
                    "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:51234",
                    "AGY_DEV_MODEL_FALLBACK_ORDER": "auto,hydrogen-l-tf-high",
                },
                which_fn=lambda _n: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _p: False,
            )
            self.assertEqual(res_wake["wake"]["status"], "woke")
            self.assertEqual(res_wake["wake"]["selected_wake_model"], "hydrogen-l-tf-high")
            spawned_cmd = mock_popen.call_args[0][0]
            self.assertIn("--model=hydrogen-l-tf-high", spawned_cmd)

            # Now exhaust hydrogen-l-tf-high too -> watchdog suppresses wake cleanly with 0 burn
            mq.record_model_quota_cooldown("hydrogen-l-tf-high", resets_in_seconds=3600, state_dir=st_dir, now_ts=now_ts + 10)
            cycle = watchdog.run_watchdog_cycle(
                state_dir=st_dir,
                brain_dir=brain_dir,
                sync_git=False,
                now_ts=now_ts + 1000.0,
                home_dir=home_dir,
                env={
                    "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:51234",
                    "AGY_DEV_MODEL_FALLBACK_ORDER": "auto,hydrogen-l-tf-high",
                },
                which_fn=lambda _n: None,
                popen_fn=mock_popen,
                pid_alive_fn=lambda _p: False,
                ps_output="",
            )
            self.assertEqual(cycle["status"], "quota_exhausted")
            self.assertFalse(cycle["woke"])
            self.assertEqual(cycle["wake"]["status"], "skipped_all_models_exhausted")
            self.assertTrue(cycle["wake"]["next_reset_at"])

    def test_docs_and_agents_under_24kb_and_reference_playbook_exists(self) -> None:
        ref_doc = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "references" / "model-quota-fallback.md"
        self.assertTrue(ref_doc.is_file())
        ref_text = ref_doc.read_text(encoding="utf-8")
        for token in (
            "PR_DEV_DEFAULT_MODEL",
            "AGY_DEV_LOW_QUOTA_THRESHOLD_PCT",
            "AGY_DEV_MAX_ORCHESTRATOR_STEPS",
            "AGY_DEV_MODEL_ROUTING_STRATEGY",
            "AGY_DEV_ORCHESTRATOR_MODEL",
            "classify_change_risk_tier",
            "skip_planner_subagent",
            "skipped_all_models_exhausted",
        ):
            self.assertIn(token, ref_text)

        for rel in (
            "agents/agy-dev-orchestrator.md",
            "agents/agy-dev-executor.md",
            "agents/agy-code-developer.md",
            "agents/agy-code-pr-reviewer.md",
            "skills/agy-dev-queue-orchestrator/SKILL.md",
            "skills/agy-code-remediation-pr/SKILL.md",
        ):
            p = REPO_ROOT / rel
            self.assertLess(p.stat().st_size, 24000, msg=f"{rel} exceeds 24,000-byte Antigravity limit")


class TestTokenOptimalCrossFamilyAndFastPaths(unittest.TestCase):
    """Verify Highest-Quota Cross-Family Balancing, 3-tier risk classifier, Flash orchestrator, compact CLI & planner fast-path."""

    def test_model_family_and_3_tier_risk_classifier(self) -> None:
        self.assertEqual(mq.get_model_family("opus-5.5-high"), "claude_opus")
        self.assertEqual(mq.get_model_broad_family("opus-5.5-high"), "claude")
        self.assertEqual(mq.get_model_family("fable-5.2-medium"), "claude_fable")
        self.assertEqual(mq.get_model_broad_family("fable-5.2-medium"), "claude")
        self.assertEqual(mq.get_model_family("gemini-4-carbon-high"), "gemini_4")
        self.assertEqual(mq.get_model_broad_family("gemini-4-carbon-high"), "gemini")
        self.assertEqual(mq.get_model_family("hydrogen-l-tf-medium"), "gemini_hydrogen")
        self.assertEqual(mq.get_model_family("auto"), "gemini_pro_next")
        self.assertEqual(mq.get_model_family("gemini-flash-next"), "gemini_flash")
        self.assertEqual(mq.get_model_family("gemini-3.5-flash-lite"), "gemini_flash_lite")

        micro = mq.classify_change_risk_tier(["README.md", "web/styles.css"], lines_changed=24)
        self.assertEqual(micro["risk_tier"], "micro")
        self.assertEqual(micro["dev_effort"], "flash_lite")
        self.assertEqual(micro["review_effort"], "low")

        standard = mq.classify_change_risk_tier(["clients/helper.py", "tests/test_helper.py"], lines_changed=95)
        self.assertEqual(standard["risk_tier"], "standard")
        self.assertEqual(standard["dev_effort"], "flash")
        self.assertEqual(standard["review_effort"], "medium")

        crit_path = mq.classify_change_risk_tier(["hooks.json"], lines_changed=10)
        self.assertEqual(crit_path["risk_tier"], "critical")
        self.assertIn("hooks.json", crit_path["critical_files"])

        crit_loc = mq.classify_change_risk_tier(["clients/helper.py"], lines_changed=450)
        self.assertEqual(crit_loc["risk_tier"], "critical")

        crit_files = mq.classify_change_risk_tier([f"clients/m{i}.py" for i in range(9)], lines_changed=90)
        self.assertEqual(crit_files["risk_tier"], "critical")

        crit_kw = mq.classify_change_risk_tier(["clients/helper.py"], title="OAuth token migration")
        self.assertEqual(crit_kw["risk_tier"], "critical")

    def test_highest_quota_cross_family_balancing_and_sequential_override(self) -> None:
        live_pools = {
            "quotas": {
                "gemini-4-carbon-low": {"quota_remaining": "43%"},
                "gemini-4-carbon-medium": {"quota_remaining": "43%"},
                "gemini-4-carbon-high": {"quota_remaining": "43%"},
                "hydrogen-l-tf-medium": {"quota_remaining": "90%"},
                "hydrogen-l-tf-high": {"quota_remaining": "90%"},
                "auto": {"quota_remaining": "93%"},
                "fable-5.2-low": {"quota_remaining": "98%"},
                "fable-5.2-medium": {"quota_remaining": "98%"},
                "fable-5.2-high": {"quota_remaining": "98%"},
                "opus-5.5-low": {"quota_remaining": "98%"},
                "opus-5.5-medium": {"quota_remaining": "98%"},
                "opus-5.5-high": {"quota_remaining": "98%"},
                "gemini-flash-next": {"quota_remaining": "100%"},
                "gemini-3.5-flash-lite": {"quota_remaining": "96%"},
            }
        }
        with tempfile.TemporaryDirectory() as tmp:
            st_dir = Path(tmp) / "state"
            now_ts = 1_800_500_000.0

            # Tier 1 micro: flash_lite dev + low-effort Cross-Family Claude review
            dev_micro = mq.select_next_optimal_model(
                role="developer",
                risk_tier="micro",
                live_quotas=live_pools,
                state_dir=st_dir,
                now_ts=now_ts,
                env={},
            )
            self.assertEqual(dev_micro["selected_model"], "flash_lite")
            self.assertEqual(dev_micro["subagent_model"], "flash_lite")

            rev_micro = mq.select_next_optimal_model(
                role="reviewer",
                risk_tier="micro",
                dev_model="flash_lite",
                live_quotas=live_pools,
                state_dir=st_dir,
                now_ts=now_ts,
                env={},
            )
            self.assertEqual(rev_micro["selected_model"], "fable-5.2-low")
            self.assertEqual(rev_micro["broad_family"], "claude")
            self.assertTrue(rev_micro["cross_family_matched"])
            self.assertEqual(rev_micro["subagent_model"], "flash")

            # Tier 2 standard: Gemini Flash dev -> medium-effort Cross-Family Claude review (fallback subagent tier stays 'pro')
            rev_std = mq.select_next_optimal_model(
                role="reviewer",
                risk_tier="standard",
                dev_model="gemini-flash-next",
                live_quotas=live_pools,
                state_dir=st_dir,
                now_ts=now_ts,
                env={},
            )
            self.assertEqual(rev_std["selected_model"], "fable-5.2-medium")
            self.assertEqual(rev_std["mode"], "balanced_cross_family")
            self.assertTrue(rev_std["cross_family_matched"])
            self.assertEqual(rev_std["subagent_model"], "pro")

            # Tier 3 critical: Dev picks highest-quota high pool (opus-5.5-high 98%), Review picks Cross-Family Gemini (auto 93%)
            dev_crit = mq.select_next_optimal_model(
                role="developer",
                risk_tier="critical",
                live_quotas=live_pools,
                state_dir=st_dir,
                now_ts=now_ts,
                env={},
            )
            self.assertEqual(dev_crit["selected_model"], "opus-5.5-high")
            self.assertEqual(dev_crit["subagent_model"], "pro")

            rev_crit = mq.select_next_optimal_model(
                role="reviewer",
                risk_tier="critical",
                dev_model="opus-5.5-high",
                live_quotas=live_pools,
                state_dir=st_dir,
                now_ts=now_ts,
                env={},
            )
            self.assertEqual(rev_crit["selected_model"], "auto")
            self.assertEqual(rev_crit["broad_family"], "gemini")
            self.assertTrue(rev_crit["cross_family_matched"])

            # Sequential strategy override restores static preferred order (pro @ 43% > 25%)
            rev_seq = mq.select_next_optimal_model(
                role="reviewer",
                live_quotas=live_pools,
                state_dir=st_dir,
                now_ts=now_ts,
                env={"AGY_DEV_MODEL_ROUTING_STRATEGY": "sequential"},
            )
            self.assertEqual(rev_seq["selected_model"], "pro")
            self.assertEqual(rev_seq["routing_strategy"], "sequential")
            self.assertEqual(rev_seq["mode"], "normal")

    def test_orchestrator_defaults_to_flash_and_honors_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            st_dir = Path(tmp) / "state"
            orch_default = mq.select_next_optimal_model(role="orchestrator", state_dir=st_dir, env={})
            self.assertEqual(orch_default["selected_model"], "gemini-flash-next")
            self.assertEqual(orch_default["subagent_model"], "flash")

            wake_override = mq.select_next_optimal_model(
                role="wake",
                state_dir=st_dir,
                env={"AGY_DEV_MODEL_FALLBACK_ORDER": "auto,hydrogen-l-tf-high"},
            )
            self.assertEqual(wake_override["selected_model"], "auto")

            orch_explicit = mq.select_next_optimal_model(
                role="orchestrator",
                state_dir=st_dir,
                env={"AGY_DEV_ORCHESTRATOR_MODEL": "fable-5.2-medium"},
            )
            self.assertEqual(orch_explicit["selected_model"], "fable-5.2-medium")

    def test_dynamic_created_by_label_and_compact_vs_verbose_cli(self) -> None:
        import subprocess
        from git_client import GitVcsIntegrationClient

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "state"
            bare = root / "origin.git"
            repo = root / "acme-repo"
            subprocess.run(["git", "init", "--bare", str(bare)], check=True, capture_output=True)
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "dev@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Acme Dev"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(bare)], check=True)
            (repo / "README.md").write_text("# Acme\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "push", "-u", "origin", "main"], check=True, capture_output=True)

            cid = "55555555-1111-4222-8333-444444444444"
            # 1. plan-developer compact default vs --verbose
            buf_c = io.StringIO()
            buf_v = io.StringIO()
            with mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(pdir)}):
                with contextlib.redirect_stdout(buf_c):
                    rc_c = prs.main(["plan-developer", "--id", cid, "--target-files", "README.md"])
                with contextlib.redirect_stdout(buf_v):
                    rc_v = prs.main(["plan-developer", "--id", cid, "--target-files", "README.md", "--verbose"])
            self.assertEqual(rc_c, 0)
            self.assertEqual(rc_v, 0)
            out_c = json.loads(buf_c.getvalue())
            out_v = json.loads(buf_v.getvalue())
            self.assertTrue(out_c.get("compact"))
            self.assertNotIn("subagent_definition", out_c)
            self.assertIn("subagent_definition", out_v)
            self.assertEqual(out_c["risk_classification"]["risk_tier"], "micro")
            self.assertEqual(out_c["subagent_invocation"]["Model"], "flash_lite")
            self.assertLess(len(buf_c.getvalue()), len(buf_v.getvalue()))

            # 2. start-remediation compact default persists --dev-model & omits proposal_markdown
            buf_start = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(pdir)}),
                contextlib.redirect_stdout(buf_start),
            ):
                rc_s = prs.main(
                    [
                        "start-remediation",
                        "--id",
                        cid,
                        "--action-id",
                        "micro-doc",
                        "--workdir",
                        str(repo),
                        "--dev-model",
                        "gemini-3.5-flash-lite",
                        "--target-files",
                        "README.md",
                        "--rca-summary",
                        "Update docs",
                        "--proposed-fix",
                        "Clarify quickstart",
                        "--worktree",
                    ]
                )
            self.assertEqual(rc_s, 0)
            start_out = json.loads(buf_start.getvalue())
            self.assertTrue(start_out.get("compact"))
            self.assertNotIn("proposal_markdown", start_out)
            self.assertEqual(start_out["lock"]["dev_model"], "gemini-3.5-flash-lite")

            wt_path = Path(start_out["worktree_path"])
            (wt_path / "README.md").write_text("# Acme\n\nUpdated quickstart.\n", encoding="utf-8")

            # 3. prepare-review compact default returns micro risk & cross-family Claude reviewer, strips SystemPrompt
            buf_prep = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(pdir)}),
                contextlib.redirect_stdout(buf_prep),
            ):
                rc_p = prs.main(
                    [
                        "prepare-review",
                        "--workdir",
                        str(wt_path),
                        "--title",
                        "docs(readme): clarify quickstart",
                        "--executive-summary",
                        "Clarify quickstart instructions in README.md.",
                    ]
                )
            self.assertEqual(rc_p, 0)
            prep_out = json.loads(buf_prep.getvalue())
            self.assertTrue(prep_out.get("compact"))
            self.assertNotIn("reviewer_subagent_definition", prep_out)
            self.assertEqual(prep_out["risk_classification"]["risk_tier"], "micro")
            self.assertEqual(prep_out["reviewer_model_routing"]["selected_model"], "fable-5.2-low")
            self.assertTrue(prep_out["reviewer_model_routing"]["cross_family_matched"])

            # 4. record-review stamps dynamic created_by using actual persisted dev_model & reviewer_model
            vcs = GitVcsIntegrationClient()
            rev = prs.record_pr_review_evaluation(
                branch_name=start_out["branch_name"],
                verdict="PASS",
                score=96,
                summary="Clean micro doc update.",
                reviewer_model="fable-5.2-low",
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(rev["status"], "ok")
            self.assertTrue(rev["ready_for_human_pr"])
            self.assertEqual(
                rev["created_by"],
                "agy-code-developer (gemini-3.5-flash-lite) · agy-code-pr-reviewer (fable-5.2-low)",
            )
            self.assertEqual(rev["review_history"][-1]["dev_model"], "gemini-3.5-flash-lite")

            # Also verify Round 2+ dev_model escalation attribution when no explicit dev_model is stored on lock
            rev_r2 = prs.record_pr_review_evaluation(
                branch_name="fix/20261124-120000-r2-99999999",
                verdict="NEEDS_REVISION",
                score=82,
                summary="Needs extra test",
                reviewer_model="opus-5.5-medium",
                dev_model="pro",
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(
                rev_r2["created_by"],
                "agy-code-developer (pro) · agy-code-pr-reviewer (opus-5.5-medium)",
            )

            # 5. quota-status CLI with --files & --dev-model classifies critical risk & enforces cross-family
            buf_q = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(pdir)}),
                contextlib.redirect_stdout(buf_q),
            ):
                rc_q = prs.main(
                    [
                        "quota-status",
                        "--role",
                        "reviewer",
                        "--files",
                        "hooks.json",
                        "--dev-model",
                        "gemini-flash-next",
                    ]
                )
            self.assertEqual(rc_q, 0)
            q_out = json.loads(buf_q.getvalue())
            self.assertEqual(q_out["risk_classification"]["risk_tier"], "critical")
            self.assertTrue(q_out["routing"]["cross_family_matched"])
            self.assertEqual(q_out["routing"]["broad_family"], "claude")

    def test_queue_next_skip_planner_subagent_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pdir = Path(tmp) / "state"
            cid_anchored = "66666666-1111-4222-8333-444444444441"
            cid_unanchored = "66666666-1111-4222-8333-444444444442"
            prs.enqueue_dev_queue_item(
                conversation_id=cid_anchored,
                title="Polish README badge",
                priority=10,
                repo_slugs=["acme/repo-a"],
                target_files=["README.md"],
                proposed_fix="Update badge URL in README.md",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_unanchored,
                title="Investigate auth latency",
                priority=20,
                repo_slugs=["acme/repo-b"],
                portfolio_dir=pdir,
            )

            nxt = prs.get_next_dev_queue_item(skip_locked_repos=True, portfolio_dir=pdir)
            self.assertEqual(nxt["status"], "ok")
            item_a = nxt["runnable_by_repo"]["acme/repo-a"]
            item_b = nxt["runnable_by_repo"]["acme/repo-b"]
            self.assertTrue(item_a["skip_planner_subagent"])
            self.assertEqual(item_a["risk_classification"]["risk_tier"], "micro")
            self.assertFalse(item_b["skip_planner_subagent"])
            self.assertEqual(item_b["risk_classification"]["risk_tier"], "critical")


if __name__ == "__main__":
    unittest.main()
