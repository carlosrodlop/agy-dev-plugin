#!/usr/bin/env python3
"""Hermetic Zero-PII unit tests for orchestration WIP controls, priority inheritance, TUI clarity, and wake hardening."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
REM_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
TUI_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts"
TEST_SCRIPTS_DIR = REPO_ROOT / "tests" / "scripts"

for _p in (CLIENTS_DIR, REM_SCRIPTS_DIR, TUI_SCRIPTS_DIR, TEST_SCRIPTS_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import dev_common  # noqa: E402
import dev_flow_optimizer as flow_opt  # noqa: E402
import dev_queue_doctor as doctor  # noqa: E402
import dev_queue_tui as tui  # noqa: E402
import pr_remediator as prs  # noqa: E402
import selective_test_runner as runner  # noqa: E402


def _write_state(
    state_dir: Path,
    *,
    items: list[dict[str, object]] | None = None,
    locks: dict[str, dict[str, object]] | None = None,
    orchestrator_id: str = "11111111-1111-4111-8111-111111111111",
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    dev_common.atomic_write_json(
        state_dir / "dev_queue.json",
        {
            "schema_version": 1,
            "orchestrator_conversation_id": orchestrator_id,
            "updated_at": "2026-11-22T22:00:00Z",
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


class TestAgentApiWakeHardening(unittest.TestCase):
    """Verify `resolve_agentapi_cmd` precedence, `language_server` argv injection, startup probe, and crash log anomaly."""

    def test_resolve_agentapi_cmd_precedence_and_language_server_argv_injection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fake_home = root / "home"
            home_bin = fake_home / ".gemini" / "antigravity" / "bin" / "agentapi"
            path_bin = root / "path_bin" / "agentapi"
            ls_bin = root / "Antigravity.app" / "bin" / "language_server_macos_arm"

            for p in (home_bin, path_bin, ls_bin):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                os.chmod(p, 0o755)

            env_with_ls = {
                "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:50123",
                "ANTIGRAVITY_AGENTAPI_EXE": str(ls_bin),
            }

            # 1. ~/.gemini/antigravity/bin/agentapi wins over PATH and ANTIGRAVITY_AGENTAPI_EXE
            self.assertEqual(
                doctor.resolve_agentapi_cmd(
                    env=env_with_ls,
                    home_dir=fake_home,
                    which_fn=lambda name: str(path_bin) if name == "agentapi" else None,
                ),
                [str(home_bin)],
            )

            # 2. shutil.which("agentapi") wins over ANTIGRAVITY_AGENTAPI_EXE when home agentapi is absent
            home_bin.unlink()
            self.assertEqual(
                doctor.resolve_agentapi_cmd(
                    env=env_with_ls,
                    home_dir=fake_home,
                    which_fn=lambda name: str(path_bin) if name == "agentapi" else None,
                ),
                [str(path_bin)],
            )

            # 3. Fallback to ANTIGRAVITY_AGENTAPI_EXE pointing to language_server* injects "agentapi" at argv[1]
            self.assertEqual(
                doctor.resolve_agentapi_cmd(
                    env=env_with_ls,
                    home_dir=fake_home,
                    which_fn=lambda _name: None,
                ),
                [str(ls_bin), "agentapi"],
            )

            # Verify dry-run wake builds [language_server, "agentapi", "new-conversation", ...]
            state_dir = root / "state"
            diag = {
                "overall_status": "degraded",
                "needs_wake": True,
                "anomalies": [{"code": "idle_orchestrator_with_runnable_work", "severity": "high", "requires_wake": True}],
                "queue_summary": {},
                "orchestrator": {"conversation_id": ""},
            }
            dry_res = doctor.trigger_orchestrator_wake(
                diag,
                state_dir=state_dir,
                dry_run=True,
                home_dir=fake_home,
                env=env_with_ls,
                which_fn=lambda _name: None,
            )
            self.assertEqual(dry_res["status"], "dry_run")
            self.assertEqual(dry_res["transport"], "agentapi")
            self.assertEqual(
                dry_res["command"][:4],
                [str(ls_bin), "agentapi", "new-conversation", "--title=[agy-doctor] Orchestrator Recovery"],
            )

    def test_startup_probe_catches_immediate_sigabrt_without_poisoning_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            fake_home = root / "home"
            ls_bin = root / "language_server_macos_arm"
            ls_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(ls_bin, 0o755)

            diag = {
                "overall_status": "degraded",
                "needs_wake": True,
                "anomalies": [{"code": "idle_orchestrator_with_runnable_work", "severity": "high", "requires_wake": True}],
                "queue_summary": {},
                "orchestrator": {"conversation_id": ""},
            }
            now_ts = 1_800_500_000.0
            env_ls = {
                "ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:50123",
                "ANTIGRAVITY_AGENTAPI_EXE": str(ls_bin),
            }

            # Simulate wake process starting briefly and aborting with SIGABRT (-6) on second poll
            crashing_proc = mock.MagicMock()
            crashing_proc.pid = 77777
            crashing_proc.returncode = None
            crashing_proc.poll.side_effect = [None, -int(signal.SIGABRT)]

            res_fail = doctor.trigger_orchestrator_wake(
                diag,
                state_dir=state_dir,
                cooldown_seconds=900,
                startup_probe_seconds=0.05,
                now_ts=now_ts,
                home_dir=fake_home,
                env=env_ls,
                which_fn=lambda _n: None,
                popen_fn=mock.MagicMock(return_value=crashing_proc),
                pid_alive_fn=lambda _p: False,
                sleep_fn=lambda _s: None,
            )
            self.assertEqual(res_fail["status"], "error")
            self.assertFalse(res_fail["woke"])
            self.assertEqual(res_fail["reason"], "wake_startup_crashed")
            self.assertEqual(res_fail["signal"], "SIGABRT")
            self.assertFalse(res_fail["cooldown_persisted"])
            self.assertFalse((state_dir / doctor.DOCTOR_STATE_FILENAME).exists())

            # Because cooldown was NOT poisoned, an immediate retry succeeds without --force!
            healthy_proc = mock.MagicMock()
            healthy_proc.pid = 77778
            healthy_proc.returncode = None
            healthy_proc.poll.return_value = None
            res_ok = doctor.trigger_orchestrator_wake(
                diag,
                state_dir=state_dir,
                cooldown_seconds=900,
                startup_probe_seconds=0.02,
                now_ts=now_ts + 5.0,
                home_dir=fake_home,
                env=env_ls,
                which_fn=lambda _n: None,
                popen_fn=mock.MagicMock(return_value=healthy_proc),
                pid_alive_fn=lambda _p: False,
                sleep_fn=lambda _s: None,
            )
            self.assertEqual(res_ok["status"], "woke")
            self.assertTrue(res_ok["woke"])
            self.assertTrue((state_dir / doctor.DOCTOR_STATE_FILENAME).is_file())

    def test_diagnose_orchestrator_surfaces_wake_log_sigabrt_and_clears_after_healthy_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            state_dir = root / "state"
            brain_dir = root / "brain"
            logs_dir = state_dir / "logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            _write_state(state_dir, items=[], locks={})

            crash_log = logs_dir / "agy_doctor_wake_20261122-210000.log"
            crash_log.write_text(
                "F1122 21:00:00.123456 main.go:88] Failed to read initial metadata from stdin: EOF\n"
                "SIGABRT: abort\n",
                encoding="utf-8",
            )
            os.utime(crash_log, (1_800_600_000.0, 1_800_600_000.0))

            diag_crash = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                ps_output="",
                now_ts=1_800_600_010.0,
            )
            by_code = {a["code"]: a for a in diag_crash["anomalies"]}
            self.assertIn("wake_process_crash", by_code)
            self.assertTrue(diag_crash["needs_wake"])
            ev = by_code["wake_process_crash"]["evidence"]
            self.assertIn("Failed to read initial metadata from stdin", ev["signatures"])
            self.assertIn("SIGABRT", ev["signatures"])

            # Newer clean wake log clears the active crash anomaly
            clean_log = logs_dir / "agy_doctor_wake_20261122-210500.log"
            clean_log.write_text("Created conversation 99999999-aaaa-4999-8999-999999999999\n", encoding="utf-8")
            os.utime(clean_log, (1_800_600_300.0, 1_800_600_300.0))

            diag_clean = doctor.diagnose_orchestrator(
                state_dir=state_dir,
                brain_dir=brain_dir,
                ps_output="",
                now_ts=1_800_600_310.0,
            )
            self.assertNotIn("wake_process_crash", {a["code"] for a in diag_clean["anomalies"]})


class TestPriorityInheritanceAndWipHardening(unittest.TestCase):
    """Verify cycle-safe transitive `effective_priority`, 50% WIP-finish boost, `P<=17` precedence, and partial-WIP cap."""

    def test_cycle_safe_transitive_effective_priority_inheritance_and_queue_ordering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            cid_base = "aaaa1111-1111-4111-8111-111111111111"
            cid_mid = "bbbb2222-2222-4222-8222-222222222222"
            cid_urgent_dep = "cccc3333-3333-4333-8333-333333333333"
            cid_other = "dddd4444-4444-4444-8444-444444444444"
            cid_cyc_a = "eeee5555-5555-4555-8555-555555555555"
            cid_cyc_b = "ffff6666-6666-4666-8666-666666666666"

            # Chain: P16 (cid_urgent_dep) -> depends_on P24 (cid_mid) -> depends_on P30 (cid_base)
            # Plus mutual cycle: cid_cyc_a (P28) <-> cid_cyc_b (P22)
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid_other,
                        "title": "Unrelated P20 task",
                        "priority": 20,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "target_files": ["src/other.py"],
                    },
                    {
                        "conversation_id": cid_base,
                        "title": "Foundational schema prerequisite",
                        "priority": 30,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-a"],
                        "target_files": ["src/schema.py"],
                    },
                    {
                        "conversation_id": cid_mid,
                        "title": "Middle adapter task",
                        "priority": 24,
                        "status": "pending",
                        "depends_on": [cid_base[:8]],
                        "repo_slugs": ["acme/repo-a"],
                        "target_files": ["src/adapter.py"],
                    },
                    {
                        "conversation_id": cid_urgent_dep,
                        "title": "Urgent P16 endpoint depending on chain",
                        "priority": 16,
                        "status": "pending",
                        "depends_on": [cid_mid],
                        "repo_slugs": ["acme/repo-a"],
                        "target_files": ["src/endpoint.py"],
                    },
                    {
                        "conversation_id": cid_cyc_a,
                        "title": "Cycle node A",
                        "priority": 28,
                        "status": "pending",
                        "depends_on": [cid_cyc_b],
                        "repo_slugs": ["acme/repo-b"],
                        "target_files": ["src/cyc_a.py"],
                    },
                    {
                        "conversation_id": cid_cyc_b,
                        "title": "Cycle node B",
                        "priority": 22,
                        "status": "pending",
                        "depends_on": [cid_cyc_a],
                        "repo_slugs": ["acme/repo-b"],
                        "target_files": ["src/cyc_b.py"],
                    },
                ],
            )

            q_loaded = prs.load_dev_queue(portfolio_dir=state_dir)
            by_cid = {it["conversation_id"]: it for it in q_loaded["items"]}

            # Raw priority preserved while effective_priority & inherited_priority_from propagate transitively
            self.assertEqual(by_cid[cid_base]["priority"], 30)
            self.assertEqual(by_cid[cid_base]["effective_priority"], 16)
            self.assertEqual(by_cid[cid_base]["inherited_priority_from"], cid_urgent_dep)
            self.assertEqual(by_cid[cid_mid]["priority"], 24)
            self.assertEqual(by_cid[cid_mid]["effective_priority"], 16)
            self.assertEqual(by_cid[cid_mid]["inherited_priority_from"], cid_urgent_dep)

            # Cycle terminates cleanly without RecursionError
            self.assertEqual(by_cid[cid_cyc_a]["priority"], 28)
            self.assertEqual(by_cid[cid_cyc_a]["effective_priority"], 22)
            self.assertEqual(by_cid[cid_cyc_a]["inherited_priority_from"], cid_cyc_b)
            self.assertEqual(by_cid[cid_cyc_b]["effective_priority"], 22)
            self.assertEqual(by_cid[cid_cyc_b]["inherited_priority_from"], "")

            # `queue-next` selects cid_base (effective P16) ahead of cid_other (P20)!
            nxt = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
            self.assertIsNotNone(nxt["next_unlocked_item"])
            self.assertEqual(nxt["next_unlocked_item"]["conversation_id"], cid_base)
            self.assertEqual(nxt["next_unlocked_item"]["priority"], 30)
            self.assertEqual(nxt["next_unlocked_item"]["effective_priority"], 16)

    def test_partial_wip_finish_boost_and_p17_infra_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            cid_zero_p19 = "1111aaaa-1111-4111-8111-aaaaaaaaaaaa"
            cid_half_p25 = "2222bbbb-2222-4222-8222-bbbbbbbbbbbb"
            cid_infra_p15 = "3333cccc-3333-4333-8333-cccccccccccc"

            # 1. P25 50%-merged WIP beats P19 0%-merged item on the same unlocked repo by default
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid_zero_p19,
                        "title": "Fresh P19 feature",
                        "priority": 19,
                        "status": "pending",
                        "repo_slugs": ["acme/service-tower"],
                        "merged_repos": [],
                        "target_files": ["api/new.py"],
                    },
                    {
                        "conversation_id": cid_half_p25,
                        "title": "Half-merged P25 cross-repo task",
                        "priority": 25,
                        "status": "pending",
                        "repo_slugs": ["acme/plugin-core", "acme/service-tower"],
                        "merged_repos": ["acme/plugin-core"],
                        "target_files": ["api/finish.py"],
                    },
                ],
            )
            with mock.patch.dict(os.environ, {"AGY_DEV_PRIORITIZE_PARTIAL_WIP": "1"}, clear=False):
                res_boost = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
            self.assertEqual(res_boost["next_unlocked_item"]["conversation_id"], cid_half_p25)
            self.assertTrue(res_boost["next_unlocked_item"]["wip_finish_boost"])
            self.assertEqual(res_boost["runnable_by_repo"]["acme/service-tower"]["conversation_id"], cid_half_p25)

            # Disabling AGY_DEV_PRIORITIZE_PARTIAL_WIP=0 falls back to numeric priority (P19 beats P25)
            with mock.patch.dict(os.environ, {"AGY_DEV_PRIORITIZE_PARTIAL_WIP": "0"}, clear=False):
                res_no_boost = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
            self.assertEqual(res_no_boost["next_unlocked_item"]["conversation_id"], cid_zero_p19)

            # 2. P<=17 infrastructure task (P15) strictly preempts P18+ partial WIP (P25)!
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": cid_half_p25,
                        "title": "Half-merged P25 cross-repo task",
                        "priority": 25,
                        "status": "pending",
                        "repo_slugs": ["acme/plugin-core", "acme/service-tower"],
                        "merged_repos": ["acme/plugin-core"],
                        "target_files": ["api/finish.py"],
                    },
                    {
                        "conversation_id": cid_infra_p15,
                        "title": "Urgent P15 infra hardening",
                        "priority": 15,
                        "status": "pending",
                        "repo_slugs": ["acme/service-tower"],
                        "merged_repos": [],
                        "target_files": ["api/infra.py"],
                    },
                ],
            )
            with mock.patch.dict(os.environ, {"AGY_DEV_PRIORITIZE_PARTIAL_WIP": "1"}, clear=False):
                res_infra = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
            self.assertEqual(res_infra["next_unlocked_item"]["conversation_id"], cid_infra_p15)
            self.assertEqual(res_infra["runnable_by_repo"]["acme/service-tower"]["conversation_id"], cid_infra_p15)

    def test_partial_wip_cap_gates_new_zero_pct_partial_dispatch_leaving_single_repo_unrestricted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            cid_lock_holder = "00001111-1111-4111-8111-000000000001"
            half_cids = [
                f"5050000{i}-1111-4111-8111-00000000000{i}"
                for i in (1, 2, 3)
            ]
            cid_new_partial = "88881111-1111-4111-8111-888888888881"
            cid_single_free = "99991111-1111-4111-8111-999999999991"
            cid_existing_partial = "77771111-1111-4111-8111-777777777771"

            items: list[dict[str, object]] = [
                {
                    "conversation_id": cid,
                    "title": f"Half-merged item {idx}",
                    "priority": 20 + idx,
                    "status": "pending",
                    "repo_slugs": ["acme/plugin-core", "acme/service-tower"],
                    "merged_repos": ["acme/plugin-core"],
                    "target_files": [f"api/part_{idx}.py"],
                }
                for idx, cid in enumerate(half_cids, 1)
            ]
            # New 0% multi-repo task whose downstream repo (acme/service-tower) is locked with 3 partial WIP items
            items.append(
                {
                    "conversation_id": cid_new_partial,
                    "title": "New 0% multi-repo task that would add a 4th half-merged WIP",
                    "priority": 18,
                    "status": "pending",
                    "repo_slugs": ["acme/plugin-core", "acme/service-tower"],
                    "merged_repos": [],
                    "target_files": ["skills/new_multi.py"],
                }
            )
            # Single-repo task on acme/plugin-core MUST stay 100% unrestricted!
            items.append(
                {
                    "conversation_id": cid_single_free,
                    "title": "Single-repo plugin fix",
                    "priority": 28,
                    "status": "pending",
                    "repo_slugs": ["acme/plugin-core"],
                    "merged_repos": [],
                    "target_files": ["skills/single_only.py"],
                }
            )
            # Existing partial-WIP item advancing an intermediate unlocked repo MUST stay unrestricted!
            items.append(
                {
                    "conversation_id": cid_existing_partial,
                    "title": "Existing 1/3 partial WIP advancing repo-mid",
                    "priority": 30,
                    "status": "pending",
                    "repo_slugs": ["acme/dev-kit", "acme/repo-mid", "acme/service-tower"],
                    "merged_repos": ["acme/dev-kit"],
                    "target_files": ["mid/handler.py"],
                }
            )

            _write_state(
                state_dir,
                items=items,
                locks={
                    "acme/service-tower": {
                        "active": True,
                        "repo_slug": "acme/service-tower",
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "pr_gate_ready": True,
                        "max_slots": 1,
                        "conversation_id": cid_lock_holder,
                        "branch_name": "fix/20261122-210000-tower-00001111",
                    }
                },
            )

            with mock.patch.dict(os.environ, {"AGY_DEV_MAX_PARTIAL_WIP": "3"}, clear=False):
                picked = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)

            # cid_new_partial is gated by partial-WIP cap (3 half-merged waiting on acme/service-tower >= 3)
            capped_ids = [c["conversation_id"] for c in picked["blocked_by_partial_wip_cap"]]
            self.assertIn(cid_new_partial, capped_ids)
            self.assertNotEqual(picked["runnable_by_repo"]["acme/plugin-core"]["conversation_id"], cid_new_partial)
            # Single-repo task on acme/plugin-core runs unrestricted!
            self.assertEqual(picked["runnable_by_repo"]["acme/plugin-core"]["conversation_id"], cid_single_free)
            # Existing partial-WIP item on acme/repo-mid ALSO runs unrestricted!
            self.assertEqual(picked["runnable_by_repo"]["acme/repo-mid"]["conversation_id"], cid_existing_partial)


class TestOptimizerTuiAndDocsContracts(unittest.TestCase):
    """Verify `dev_flow_optimizer` findings, `dev_queue_tui._compact_repos` 50% clarity, docs <24KB, and router wiring."""

    def test_optimizer_priority_inversion_and_partial_wip_accumulation_findings(self) -> None:
        items = [
            {
                "conversation_id": "aaaa1111-1111-4111-8111-111111111111",
                "title": "Low-priority P26 prerequisite",
                "priority": 26,
                "status": "pending",
                "repo_slugs": ["acme/repo-a"],
                "merged_repos": [],
                "target_files": ["src/prereq.py"],
            },
            {
                "conversation_id": "bbbb2222-2222-4222-8222-222222222222",
                "title": "Higher-priority P20 dependent",
                "priority": 20,
                "status": "pending",
                "depends_on": ["aaaa1111-1111-4111-8111-111111111111"],
                "repo_slugs": ["acme/repo-a"],
                "merged_repos": [],
                "target_files": ["src/dep.py"],
            },
            *[
                {
                    "conversation_id": f"cccc333{i}-3333-4333-8333-33333333333{i}",
                    "title": f"Half-merged item {i}",
                    "priority": 22 + i,
                    "status": "pending",
                    "repo_slugs": ["acme/plugin-core", "acme/service-tower"],
                    "merged_repos": ["acme/plugin-core"],
                    "target_files": [f"api/mod_{i}.py"],
                }
                for i in (1, 2, 3)
            ],
        ]
        with mock.patch.dict(os.environ, {"AGY_DEV_MAX_PARTIAL_WIP": "3"}, clear=False):
            report = flow_opt.analyze_dev_flow_optimizations(
                lightweight=True,
                preloaded_items=items,
                preloaded_active_locks={},
                preloaded_all_locks={},
                preloaded_repo_dirs={},
            )
        by_code = {f["code"]: f for f in report["findings"]}
        self.assertIn("priority_inversion", by_code)
        self.assertEqual(by_code["priority_inversion"]["severity"], "high")
        self.assertEqual(by_code["priority_inversion"]["evidence"]["count"], 1)
        self.assertEqual(by_code["priority_inversion"]["evidence"]["inversions"][0]["priority_gap"], 6)

        self.assertIn("partial_wip_accumulation", by_code)
        self.assertEqual(by_code["partial_wip_accumulation"]["severity"], "high")
        self.assertEqual(by_code["partial_wip_accumulation"]["evidence"]["top_repo_slug"], "acme/service-tower")
        self.assertEqual(by_code["partial_wip_accumulation"]["evidence"]["top_count"], 3)
        self.assertIn("OPT:", report["top_hint"])

    def test_tui_compact_repos_50_pct_clarity_and_board_rendering(self) -> None:
        slugs_2 = ["acme/plugin-core-service", "acme/control-gateway"]
        slugs_3 = ["acme/dev-kit-core", "acme/plugin-core-service", "acme/control-gateway"]
        label_map = tui._build_repo_labels(set(slugs_3))

        # Wide column shows exact remaining repo + (1/2✓)
        wide_half = tui._compact_repos(slugs_2, label_map, {"acme/plugin-core-service"}, max_w=26)
        self.assertEqual(wide_half, "control-gateway (1/2✓)")

        # Standard 19-char TUI column preserves both control-gateway prefix and (1/2✓)
        std_half = tui._compact_repos(slugs_2, label_map, {"acme/plugin-core-service"}, max_w=19)
        self.assertLessEqual(len(std_half), 19)
        self.assertIn("control-gat", std_half)
        self.assertTrue(std_half.endswith("(1/2✓)"), msg=f"Unexpected 19-col output: {std_half}")

        # Narrow 14-char column still shows remaining repo prefix and (1/2✓)
        narrow_half = tui._compact_repos(slugs_2, label_map, {"acme/plugin-core-service"}, max_w=14)
        self.assertLessEqual(len(narrow_half), 14)
        self.assertTrue(narrow_half.startswith("contr"), msg=f"Unexpected 14-col output: {narrow_half}")
        self.assertTrue(narrow_half.endswith("(1/2✓)"), msg=f"Unexpected 14-col output: {narrow_half}")

        # 3-repo partial WIP highlights remaining pending repos + (1/3✓) (both full and compact +1)
        wide_third = tui._compact_repos(slugs_3, label_map, {"acme/dev-kit-core"}, max_w=46)
        self.assertEqual(wide_third, "plugin-core-service, control-gateway (1/3✓)")
        compact_third = tui._compact_repos(slugs_3, label_map, {"acme/dev-kit-core"}, max_w=32)
        self.assertEqual(compact_third, "plugin-core-service +1 (1/3✓)")

        # Active PR star attribution remains unchanged when pr_repo_slug is supplied
        pr_ready = tui._compact_repos(
            slugs_2,
            label_map,
            {"acme/plugin-core-service"},
            max_w=19,
            pr_repo_slug="acme/control-gateway",
        )
        self.assertTrue(pr_ready.startswith("control-gateway★"), msg=f"Unexpected PR repo output: {pr_ready}")

        # Full TUI render check for a 50% PENDING row
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            _write_state(
                state_dir,
                items=[
                    {
                        "conversation_id": "5555aaaa-1111-4555-8555-555555555555",
                        "title": "Half-merged telemetry sync",
                        "priority": 20,
                        "status": "pending",
                        "repo_slugs": slugs_2,
                        "merged_repos": ["acme/plugin-core-service"],
                        "target_files": ["api/sync.py"],
                    }
                ],
            )
            args = argparse.Namespace(
                state_dir=str(state_dir),
                repos_dir="",
                repo="",
                status="",
                all=False,
                json=False,
                no_color=True,
                plain_urls=True,
                no_opt=True,
                write_board="",
            )
            rendered, _, _, _, _, _ = tui.render(args, is_tty_override=False, term_width_override=124)
            self.assertIn("(1/2✓)", rendered)
            self.assertIn("50%", rendered)

    def test_orchestrator_docs_under_24kb_and_selective_runner_wiring(self) -> None:
        orch_md = REPO_ROOT / "agents" / "agy-dev-orchestrator.md"
        skill_md = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "SKILL.md"
        for doc_path in (orch_md, skill_md):
            raw_bytes = doc_path.read_bytes()
            self.assertLess(len(raw_bytes), 24_000, msg=f"{doc_path.name} exceeded 24,000 bytes: {len(raw_bytes)}")
            text = raw_bytes.decode("utf-8")
            self.assertIn("AGY_DEV_PRIORITIZE_PARTIAL_WIP", text)
            self.assertIn("AGY_DEV_MAX_PARTIAL_WIP", text)
            self.assertIn("effective_priority", text)
            self.assertIn("100", text)
            self.assertIn("AGY_DEV_MAX_ORCHESTRATOR_STEPS", text)

        self.assertEqual(runner.audit_router_integrity(REPO_ROOT), [])
        for modified in (
            "skills/agy-code-remediation-pr/scripts/dev_queue_doctor.py",
            "skills/agy-code-remediation-pr/scripts/pr_remediator.py",
            "skills/agy-code-remediation-pr/scripts/dev_flow_optimizer.py",
            "skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py",
        ):
            selected = runner.select_test_modules([modified])
            self.assertIn(
                "tests.test_orchestration_wip_and_wake_fixes",
                selected,
                msg=f"{modified} did not select tests.test_orchestration_wip_and_wake_fixes",
            )


if __name__ == "__main__":
    unittest.main()
