#!/usr/bin/env python3
"""Hermetic Zero-PII unit tests for Parallel Subagents Phase 2 (`test_parallel_file_leases_and_tui.py`).

Covers:
  1. `clients/dev_common.py`:
     - `get_active_dev_pr_lock_protections` across multi-slot `dev_pr_locks[repo]["slots"]` & legacy flat locks
     - `PreToolUse` `pretool:parallel-file-lease` (`<5ms` fast path, blocking sibling-leased non-monolith writes/redirects,
       allowing own-slot, disjoint, and monolith edits)
     - `PreToolUse` `pretool:parallel-worker-deploy-home` (blocking `install.sh` / `deploy-home` inside `pr-worktree-*`,
       allowing on main repository checkouts)
     - Multi-slot `SessionStart` notices (`1/2 slots active`, `2/2 slots active`, Slot #1/#2 ownership, unready gate warnings)
  2. `skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py`:
     - `_load_locks`, `is_lock_human_ready`, `_status_badge`, and `render` 2-slot occupancy (`1/2`, `2/2`)
     - `READY_AWAITING_BASE_MERGE` and `NEEDS_REBASE_REVERIFY` badges (table + `dev_queue_board.md` + JSON)
     - Strict PR/Compare link suppression when `base_stale=True`, `needs_base_rebase=True`, `pr_gate_ready=False`, or `slot_id=2`
     - Preservation of `_compact_repos()` `(k/n✓)` clarity and `effective_priority` ordering
  3. `skills/agy-code-remediation-pr/scripts/dev_flow_optimizer.py`:
     - `parallel_lookahead_opportunity` and `stale_follower_needs_rebase` findings + compact `OPT:` hints
     - Preservation of `priority_inversion` and `partial_wip_accumulation` findings
  4. `< 24,000`-byte contract & content checks across all 5 updated agent/skill markdown specs + `selective_test_runner.py` integrity.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CLIENTS_DIR = REPO_ROOT / "clients"
PR_REM_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
TUI_DIR = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts"
TESTS_SCRIPTS_DIR = REPO_ROOT / "tests" / "scripts"
for _p in (CLIENTS_DIR, PR_REM_DIR, TUI_DIR, TESTS_SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

for _git_hook_var in (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
):
    os.environ.pop(_git_hook_var, None)

import dev_common  # noqa: E402
import dev_flow_optimizer as flow_opt  # noqa: E402
import dev_queue_tui as tui  # noqa: E402
import selective_test_runner as srunner  # noqa: E402


def _make_tui_args(
    state_dir: pathlib.Path,
    *,
    repos_dir: str = "",
    write_board: str = "",
    as_json: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        state_dir=str(state_dir),
        repos_dir=repos_dir,
        repo="",
        status="",
        all=False,
        json=as_json,
        no_color=True,
        plain_urls=True,
        no_opt=False,
        write_board=write_board,
    )


class TestParallelFileLeasesAndWorkerDeployGuardrails(unittest.TestCase):
    """Test `dev_common.py` multi-slot protections, `pretool:parallel-file-lease`, `pretool:parallel-worker-deploy-home`, and `SessionStart`."""

    def _seed_two_slot_fixture(
        self,
        root: pathlib.Path,
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
        home_dir = root / "home"
        state_dir = root / "state"
        main_repo = root / "repos" / "acme-service"
        wt1 = root / "worktrees" / "pr-worktree-acme-service-fix-20261122-100000-alpha-11111111"
        wt2 = root / "worktrees" / "pr-worktree-acme-service-fix-20261122-100500-beta-22222222"
        for d in (home_dir, state_dir, main_repo / "src", main_repo / "web", wt1 / "src", wt2 / "src", wt1 / "web", wt2 / "web"):
            d.mkdir(parents=True, exist_ok=True)

        # Initialize lightweight git metadata on `main_repo` so `resolve_repo_slug_from_path` works hermetically
        subprocess.run(["git", "init", "-b", "main", str(main_repo)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(main_repo), "remote", "add", "origin", "git@github.com:acme-org/acme-service.git"],
            check=True,
            capture_output=True,
        )

        # Seed non-monolith files (`< 250` lines) + monolith files (`>= 250` lines and `dashboard.js` basename)
        for base in (main_repo, wt1, wt2):
            (base / "src" / "alpha.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")
            (base / "src" / "beta.py").write_text("def beta() -> int:\n    return 2\n", encoding="utf-8")
            (base / "src" / "gamma.py").write_text("def gamma() -> int:\n    return 3\n", encoding="utf-8")
            (base / "src" / "big_monolith.py").write_text(
                "\n".join(f"LINE_{i} = {i}" for i in range(260)) + "\n",
                encoding="utf-8",
            )
            (base / "web" / "dashboard.js").write_text("export const VIEW = 'ok';\n", encoding="utf-8")
            (base / "install.sh").write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")

        dev_common.atomic_write_json(
            state_dir / "actions_state.json",
            {
                "actions_log_audit_state": {
                    "dev_pr_locks": {
                        "acme-org/acme-service": {
                            "active": True,
                            "repo_slug": "acme-org/acme-service",
                            "max_slots": 2,
                            "active_slots_count": 2,
                            "conversation_id": "11111111-1111-4111-8111-111111111111",
                            "branch_name": "fix/20261122-100000-alpha-11111111",
                            "status": "PENDING_HUMAN_PR_VALIDATION",
                            "pr_gate_ready": True,
                            "target_repo_dir": str(main_repo),
                            "worktree_path": str(wt1),
                            "target_files": [
                                "acme-service:src/alpha.py",
                                "src/big_monolith.py#L1-L40",
                                "web/dashboard.js#Overview",
                            ],
                            "slots": {
                                "1": {
                                    "slot_id": 1,
                                    "active": True,
                                    "conversation_id": "11111111-1111-4111-8111-111111111111",
                                    "worker_cid": "worker-slot1-aaaa1111",
                                    "branch_name": "fix/20261122-100000-alpha-11111111",
                                    "status": "PENDING_HUMAN_PR_VALIDATION",
                                    "pr_gate_ready": True,
                                    "target_repo_dir": str(main_repo),
                                    "worktree_path": str(wt1),
                                    "target_files": [
                                        "acme-service:src/alpha.py",
                                        "src/big_monolith.py#L1-L40",
                                        "web/dashboard.js#Overview",
                                    ],
                                },
                                "2": {
                                    "slot_id": 2,
                                    "active": True,
                                    "conversation_id": "22222222-2222-4222-8222-222222222222",
                                    "worker_cid": "worker-slot2-bbbb2222",
                                    "branch_name": "fix/20261122-100500-beta-22222222",
                                    "status": "READY_AWAITING_BASE_MERGE",
                                    "pr_gate_ready": False,
                                    "target_repo_dir": str(main_repo),
                                    "worktree_path": str(wt2),
                                    "target_files": [
                                        "acme-org/acme-service:src/beta.py::beta",
                                        "src/big_monolith.py#L180-L240",
                                        "web/dashboard.js#Drawer",
                                    ],
                                },
                            },
                        }
                    },
                    "dev_pr_lock": {
                        "active": True,
                        "repo_slug": "acme-org/legacy-repo",
                        "branch_name": "fix/20261122-090000-legacy-99999999",
                        "worktree_path": str(root / "worktrees" / "pr-worktree-legacy-repo-fix-99999999"),
                    },
                }
            },
        )
        return home_dir, state_dir, main_repo, wt1, wt2

    def test_get_active_dev_pr_lock_protections_reads_all_slots_and_legacy_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home_dir, state_dir, _main_repo, wt1, wt2 = self._seed_two_slot_fixture(root)
            branches, worktrees = dev_common.get_active_dev_pr_lock_protections(
                state_dir=state_dir,
                home_dir=home_dir,
            )
            self.assertIn("fix/20261122-100000-alpha-11111111", branches)
            self.assertIn("fix/20261122-100500-beta-22222222", branches)
            self.assertIn("fix/20261122-090000-legacy-99999999", branches)
            self.assertIn(str(wt1.resolve()), worktrees)
            self.assertIn(str(wt2.resolve()), worktrees)

    def test_pretool_parallel_file_lease_blocks_sibling_allows_own_disjoint_monolith_under_5ms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home_dir, state_dir, _main_repo, wt1, wt2 = self._seed_two_slot_fixture(root)
            evaluator = dev_common.DevLifecycleGuardrailEvaluator(home_dir=home_dir, state_dir=state_dir)

            # 1. Slot #2 writing to Slot #1's non-monolith leased file (`src/alpha.py`) -> DENIED
            deny_s2_alpha = evaluator.evaluate_pre_tool_use(
                {
                    "toolName": "write_file",
                    "arguments": {"Path": str(wt2 / "src" / "alpha.py"), "Content": "x = 9\n"},
                }
            )
            self.assertEqual(deny_s2_alpha["decision"], "deny")
            self.assertEqual(deny_s2_alpha.get("check_id"), "pretool:parallel-file-lease")
            self.assertIn("Slot #1", deny_s2_alpha["reason"])

            # 2. Slot #1 editing Slot #2's non-monolith leased file (`src/beta.py`) -> DENIED
            deny_s1_beta = evaluator.evaluate_pre_tool_use(
                {
                    "toolCall": {
                        "name": "edit_file",
                        "args": {"Path": str(wt1 / "src" / "beta.py"), "Target": "2", "Replacement": "3"},
                    }
                }
            )
            self.assertEqual(deny_s1_beta["decision"], "deny")
            self.assertEqual(deny_s1_beta.get("check_id"), "pretool:parallel-file-lease")
            self.assertIn("Slot #2", deny_s1_beta["reason"])

            # 3. Shell redirect (`>` and `tee`) in Slot #2 targeting Slot #1's leased `src/alpha.py` -> DENIED
            deny_redirect = evaluator.evaluate_pre_tool_use(
                {
                    "toolName": "run_command",
                    "cwd": str(wt2),
                    "arguments": {"CommandLine": "echo 'bad = 1' > src/alpha.py"},
                }
            )
            self.assertEqual(deny_redirect["decision"], "deny")
            self.assertEqual(deny_redirect.get("check_id"), "pretool:parallel-file-lease")

            deny_tee = evaluator.evaluate_pre_tool_use(
                {
                    "toolName": "run_command",
                    "cwd": str(wt2),
                    "arguments": {"CommandLine": "printf 'bad' | tee -a src/alpha.py"},
                }
            )
            self.assertEqual(deny_tee["decision"], "deny")
            self.assertEqual(deny_tee.get("check_id"), "pretool:parallel-file-lease")

            # 4. Own-slot leased files (`wt1/src/alpha.py`, `wt2/src/beta.py`) -> ALLOWED
            self.assertEqual(
                evaluator.evaluate_pre_tool_use(
                    {"toolName": "edit_file", "arguments": {"Path": str(wt1 / "src" / "alpha.py")}}
                )["decision"],
                "allow",
            )
            self.assertEqual(
                evaluator.evaluate_pre_tool_use(
                    {"toolName": "edit_file", "arguments": {"Path": str(wt2 / "src" / "beta.py")}}
                )["decision"],
                "allow",
            )

            # 5. Disjoint unleased file (`wt2/src/gamma.py`, `wt2/tests/test_new_feature.py`) -> ALLOWED
            self.assertEqual(
                evaluator.evaluate_pre_tool_use(
                    {"toolName": "write_file", "arguments": {"Path": str(wt2 / "src" / "gamma.py")}}
                )["decision"],
                "allow",
            )
            self.assertEqual(
                evaluator.evaluate_pre_tool_use(
                    {"toolName": "write_file", "arguments": {"Path": str(wt2 / "tests" / "test_beta_extra.py")}}
                )["decision"],
                "allow",
            )

            # 6. Monolith files (`>= 250` lines `src/big_monolith.py` and known basename `web/dashboard.js`) -> ALLOWED
            self.assertEqual(
                evaluator.evaluate_pre_tool_use(
                    {"toolName": "edit_file", "arguments": {"Path": str(wt2 / "src" / "big_monolith.py")}}
                )["decision"],
                "allow",
            )
            self.assertEqual(
                evaluator.evaluate_pre_tool_use(
                    {"toolName": "edit_file", "arguments": {"Path": str(wt2 / "web" / "dashboard.js")}}
                )["decision"],
                "allow",
            )

            # 7. Performance budget: average `< 5ms` per `PreToolUse` evaluation
            iterations = 40
            t0 = time.perf_counter()
            for _ in range(iterations):
                evaluator.evaluate_pre_tool_use(
                    {"toolName": "edit_file", "arguments": {"Path": str(wt2 / "src" / "beta.py")}}
                )
            avg_ms = ((time.perf_counter() - t0) * 1000.0) / iterations
            self.assertLess(avg_ms, 5.0, msg=f"Expected <5ms fast path, got {avg_ms:.2f}ms")

    def test_pretool_parallel_worker_deploy_home_blocks_in_worktree_allows_main_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home_dir, state_dir, main_repo, wt1, wt2 = self._seed_two_slot_fixture(root)
            evaluator = dev_common.DevLifecycleGuardrailEvaluator(home_dir=home_dir, state_dir=state_dir)

            # Blocked inside `pr-worktree-*` (both via `cwd` and explicit script / `cd` / `--workdir`)
            for blocked_payload in (
                {"toolName": "run_command", "cwd": str(wt1), "arguments": {"CommandLine": "./install.sh"}},
                {"toolName": "run_command", "cwd": str(wt2), "arguments": {"CommandLine": "bash ./install.sh --dry-run"}},
                {
                    "toolName": "run_command",
                    "cwd": str(wt2),
                    "arguments": {"CommandLine": "python3 clients/git_client.py deploy-home"},
                },
                {
                    "toolName": "run_command",
                    "cwd": str(main_repo),
                    "arguments": {"CommandLine": f"cd {wt2} && ./install.sh"},
                },
                {
                    "toolName": "run_command",
                    "cwd": str(main_repo),
                    "arguments": {"CommandLine": f"python3 clients/git_client.py deploy-home --workdir {wt1}"},
                },
            ):
                res = evaluator.evaluate_pre_tool_use(blocked_payload)
                self.assertEqual(res["decision"], "deny", msg=f"Expected deny for {blocked_payload}")
                self.assertEqual(res.get("check_id"), "pretool:parallel-worker-deploy-home")

            # Allowed on primary `main` checkout and for non-deploy commands inside worktrees
            for allowed_payload in (
                {"toolName": "run_command", "cwd": str(main_repo), "arguments": {"CommandLine": "./install.sh"}},
                {
                    "toolName": "run_command",
                    "cwd": str(main_repo),
                    "arguments": {"CommandLine": "python3 clients/git_client.py deploy-home"},
                },
                {
                    "toolName": "run_command",
                    "cwd": str(wt2),
                    "arguments": {"CommandLine": 'git commit -m "feat: block install.sh and deploy-home in worktree"'},
                },
                {
                    "toolName": "run_command",
                    "cwd": str(wt2),
                    "arguments": {"CommandLine": "python3 -m unittest tests.test_parallel_file_leases_and_tui"},
                },
            ):
                res_ok = evaluator.evaluate_pre_tool_use(allowed_payload)
                self.assertEqual(res_ok["decision"], "allow", msg=f"Expected allow for {allowed_payload}")

    def test_multi_slot_session_start_notices_surface_1_of_2_and_2_of_2_occupancy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            home_dir, state_dir, main_repo, _wt1, _wt2 = self._seed_two_slot_fixture(root)
            evaluator = dev_common.DevLifecycleGuardrailEvaluator(home_dir=home_dir, state_dir=state_dir)

            # 2/2 slots active: Slot #2 owner sees own Slot #2 branch, `2/2 slots active`, sibling Slot #1, and unready warning
            res_s2 = evaluator.evaluate_session_start(
                {"conversationId": "22222222-2222-4222-8222-222222222222", "cwd": str(main_repo)}
            )
            ctx_s2 = res_s2["additionalContext"]
            self.assertIn("2/2 slots active", ctx_s2)
            self.assertIn("Slot #2", ctx_s2)
            self.assertIn("fix/20261122-100500-beta-22222222", ctx_s2)
            self.assertIn("Reuse this branch", ctx_s2)
            self.assertIn("READY_AWAITING_BASE_MERGE", ctx_s2)

            # Foreign conversation sees `2/2 slots active` and both slots
            res_other_2 = evaluator.evaluate_session_start(
                {"conversationId": "33333333-3333-4333-8333-333333333333", "cwd": str(main_repo)}
            )
            self.assertIn("2/2 slots active", res_other_2["additionalContext"])
            self.assertIn("all parallel slots occupied", res_other_2["additionalContext"])

            # Drop Slot #2 -> `1/2 slots active` with `--allow-slot2` hint
            st_data = dev_common.atomic_read_json(state_dir / "actions_state.json")
            del st_data["actions_log_audit_state"]["dev_pr_locks"]["acme-org/acme-service"]["slots"]["2"]
            st_data["actions_log_audit_state"]["dev_pr_locks"]["acme-org/acme-service"]["active_slots_count"] = 1
            dev_common.atomic_write_json(state_dir / "actions_state.json", st_data)

            res_other_1 = evaluator.evaluate_session_start(
                {"conversationId": "33333333-3333-4333-8333-333333333333", "cwd": str(main_repo)}
            )
            self.assertIn("1/2 slots active", res_other_1["additionalContext"])
            self.assertIn("--allow-slot2", res_other_1["additionalContext"])


class TestMultiSlotTuiBadgesAndStrictLinkSuppression(unittest.TestCase):
    """Test `dev_queue_tui.py` 2-slot occupancy, `READY_AWAITING_BASE_MERGE` / `NEEDS_REBASE_REVERIFY` badges, and link suppression."""

    def test_is_lock_human_ready_and_status_badges_enforce_slot1_base_fresh_and_pr_gate(self) -> None:
        # Human-ready ONLY when Slot #1 + base_stale=False + pr_gate_ready=True + human-ready status
        self.assertTrue(
            tui.is_lock_human_ready(
                {
                    "slot_id": 1,
                    "status": "PENDING_HUMAN_PR_VALIDATION",
                    "pr_gate_ready": True,
                    "base_stale": False,
                }
            )
        )
        # Blocked when base_stale=True, needs_base_rebase=True, pr_gate_ready=False, slot_id=2, or parallel statuses
        for unready_lock in (
            {"slot_id": 1, "status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True, "base_stale": True},
            {"slot_id": 1, "status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True, "needs_base_rebase": True},
            {"slot_id": 1, "status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": False, "base_stale": False},
            {"slot_id": 2, "status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True, "base_stale": False},
            {"slot_id": 2, "status": "READY_AWAITING_BASE_MERGE", "pr_gate_ready": True, "base_stale": False},
            {"slot_id": 1, "status": "NEEDS_REBASE_REVERIFY", "pr_gate_ready": True, "base_stale": False},
        ):
            self.assertFalse(tui.is_lock_human_ready(unready_lock), msg=f"Must reject unready lock: {unready_lock}")

        badge_await = tui._status_badge("READY_AWAITING_BASE_MERGE", True)
        badge_rebase = tui._status_badge("NEEDS_REBASE_REVERIFY", True)
        self.assertIn("READY_AWAITING_BASE_MERGE", badge_await)
        self.assertIn("NEEDS_REBASE_REVERIFY", badge_rebase)
        self.assertNotEqual(badge_await, badge_rebase)

    def test_render_2slot_occupancy_badges_partial_wip_effective_priority_and_strict_link_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True)
            board_path = root / "dev_queue_board.md"

            cid_slot1_ready = "1111aaaa-1111-4111-8111-111111111111"
            cid_slot2_await = "2222bbbb-2222-4222-8222-222222222222"
            cid_stale_rebase = "3333cccc-3333-4333-8333-333333333333"
            cid_partial_high = "4444dddd-4444-4444-8444-444444444444"
            cid_pending_low = "5555eeee-5555-4555-8555-555555555555"

            valid_slot1_url = (
                "https://github.com/acme/repo-two-slot/compare/main...fix/20261122-slot1-1111aaaa"
                "?expand=1&title=feat%3A+slot1&body=Verdict%3A+PASS+97%2F100"
            )
            suppressed_slot2_url = "https://github.com/acme/repo-two-slot/compare/main...fix/20261122-slot2-2222bbbb?expand=1"
            suppressed_stale_url = "https://github.com/acme/repo-stale/pull/88"

            (state_dir / "dev_queue.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "orchestrator_conversation_id": "99990000-0000-4000-8000-999900000000",
                        "items": [
                            {
                                "conversation_id": cid_slot1_ready,
                                "priority": 15,
                                "status": "pr_open",
                                "repo_slugs": ["acme/repo-two-slot"],
                                "merged_repos": [],
                                "pr_urls": [valid_slot1_url],
                                "title": "Slot 1 validated PR",
                            },
                            {
                                "conversation_id": cid_slot2_await,
                                "priority": 20,
                                "status": "in_progress",
                                "repo_slugs": ["acme/repo-two-slot"],
                                "merged_repos": [],
                                "pr_urls": [suppressed_slot2_url],
                                "title": "Slot 2 follower awaiting base merge",
                            },
                            {
                                "conversation_id": cid_stale_rebase,
                                "priority": 18,
                                "status": "pr_open",
                                "repo_slugs": ["acme/repo-stale"],
                                "merged_repos": [],
                                "pr_urls": [suppressed_stale_url],
                                "title": "Promoted follower with base_stale=True",
                            },
                            {
                                "conversation_id": cid_pending_low,
                                "priority": 25,
                                "effective_priority": 25,
                                "status": "pending",
                                "repo_slugs": ["acme/repo-free"],
                                "merged_repos": [],
                                "title": "Standard pending task P25",
                            },
                            {
                                "conversation_id": cid_partial_high,
                                "priority": 40,
                                "effective_priority": 12,
                                "status": "pending",
                                "repo_slugs": ["acme/repo-two-slot", "acme/repo-free"],
                                "merged_repos": ["acme/repo-two-slot"],
                                "title": "Inherited P12 partial multi-repo task",
                            },
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (state_dir / "actions_state.json").write_text(
                json.dumps(
                    {
                        "actions_log_audit_state": {
                            "dev_pr_locks": {
                                "acme/repo-two-slot": {
                                    "active": True,
                                    "repo_slug": "acme/repo-two-slot",
                                    "max_slots": 2,
                                    "conversation_id": cid_slot1_ready,
                                    "branch_name": "fix/20261122-slot1-1111aaaa",
                                    "status": "PENDING_HUMAN_PR_VALIDATION",
                                    "pr_gate_ready": True,
                                    "base_stale": False,
                                    "manual_pr_url": valid_slot1_url,
                                    "slots": {
                                        "1": {
                                            "slot_id": 1,
                                            "active": True,
                                            "conversation_id": cid_slot1_ready,
                                            "branch_name": "fix/20261122-slot1-1111aaaa",
                                            "status": "PENDING_HUMAN_PR_VALIDATION",
                                            "pr_gate_ready": True,
                                            "base_stale": False,
                                            "manual_pr_url": valid_slot1_url,
                                        },
                                        "2": {
                                            "slot_id": 2,
                                            "active": True,
                                            "conversation_id": cid_slot2_await,
                                            "branch_name": "fix/20261122-slot2-2222bbbb",
                                            "status": "READY_AWAITING_BASE_MERGE",
                                            "pr_gate_ready": False,
                                            "base_stale": False,
                                            "manual_pr_url": suppressed_slot2_url,
                                        },
                                    },
                                },
                                "acme/repo-stale": {
                                    "active": True,
                                    "repo_slug": "acme/repo-stale",
                                    "max_slots": 2,
                                    "slot_id": 1,
                                    "conversation_id": cid_stale_rebase,
                                    "branch_name": "fix/20261122-stale-3333cccc",
                                    "status": "PENDING_HUMAN_PR_VALIDATION",
                                    "pr_gate_ready": True,
                                    "base_stale": True,
                                    "pr_url": suppressed_stale_url,
                                },
                            }
                        }
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            active_locks, _ = tui._load_locks(state_dir)
            self.assertEqual(active_locks["acme/repo-two-slot"]["slot_occupancy"], "2/2")
            self.assertEqual(active_locks["acme/repo-stale"]["slot_occupancy"], "1/2")
            self.assertTrue(active_locks["acme/repo-stale"]["base_stale"])

            out, row_links, _, first_pr, _, _ = tui.render(
                _make_tui_args(state_dir, write_board=str(board_path)),
                is_tty_override=False,
                term_width_override=140,
            )

            # 2-slot occupancy badges `[2/2]` and `[1/2]` rendered cleanly
            self.assertIn("[2/2]", out)
            self.assertIn("[1/2]", out)
            # Distinct `READY_AWAITING_BASE_MERGE` and `NEEDS_REBASE_REVERIFY` status badges rendered
            self.assertIn("READY_AWAITING_BASE_MERGE", out)
            self.assertIn("NEEDS_REBASE_REVERIFY", out)
            # Partial-WIP `(1/2✓)` clarity and `effective_priority` ordering (`4444dddd` P12 before `5555eeee` P25) preserved
            self.assertIn("repo-free (1/2✓)", out)
            self.assertLess(out.index("4444dddd"), out.index("5555eeee"))

            # Strict PR/Compare link suppression: ONLY `valid_slot1_url` surfaces; Slot #2 and `base_stale=True` URLs never leak!
            self.assertIsNotNone(first_pr)
            assert first_pr is not None
            self.assertEqual(first_pr[1], valid_slot1_url)
            self.assertEqual(list(row_links.values()), [("1111aaaa (PR)", valid_slot1_url)])
            self.assertNotIn("2222bbbb", "\n".join(u for _, u in row_links.values()))
            self.assertNotIn(suppressed_slot2_url, out)
            self.assertNotIn(suppressed_stale_url, out)

            board_md = board_path.read_text(encoding="utf-8")
            self.assertIn("`[2/2]`", board_md)
            self.assertIn("`[1/2]`", board_md)
            self.assertIn("`READY_AWAITING_BASE_MERGE`", board_md)
            self.assertIn("`NEEDS_REBASE_REVERIFY`", board_md)
            self.assertNotIn(suppressed_slot2_url, board_md)
            self.assertNotIn(suppressed_stale_url, board_md)

            # JSON output exposes `slot_occupancy`, `slot2_progress`, and `effective_status`
            json_raw, _, _, _, _, _ = tui.render(
                _make_tui_args(state_dir, as_json=True),
                is_tty_override=False,
                term_width_override=140,
            )
            json_payload = json.loads(json_raw)
            self.assertEqual(json_payload["active_locks"]["acme/repo-two-slot"]["slot_occupancy"], "2/2")
            self.assertIn("slot2_progress", json_payload["active_locks"]["acme/repo-two-slot"])
            eff_by_cid = {it["conversation_id"]: it["effective_status"] for it in json_payload["items"]}
            self.assertEqual(eff_by_cid[cid_slot2_await], "ready_awaiting_base_merge")
            self.assertEqual(eff_by_cid[cid_stale_rebase], "needs_rebase_reverify")


class TestOptimizerLookaheadStaleFollowerAndSpecContracts(unittest.TestCase):
    """Test `dev_flow_optimizer.py` Phase 2 findings, `< 24,000`-byte markdown contracts, and `selective_test_runner.py` integrity."""

    def test_parallel_lookahead_opportunity_and_stale_follower_needs_rebase_preserving_p14(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = pathlib.Path(tmp) / "acme-repo"
            (repo_dir / "src").mkdir(parents=True)
            (repo_dir / "src" / "alpha.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")
            (repo_dir / "src" / "beta.py").write_text("def beta() -> int:\n    return 2\n", encoding="utf-8")

            items = [
                {
                    "conversation_id": "1111aaaa-0000-4000-8000-000000000001",
                    "title": "Slot 1 open PR on alpha",
                    "priority": 10,
                    "status": "pr_open",
                    "repo_slugs": ["acme/repo"],
                    "target_files": ["src/alpha.py"],
                },
                # Colliding pending task (`src/alpha.py`) -> NOT eligible for Slot #2
                {
                    "conversation_id": "2222bbbb-0000-4000-8000-000000000002",
                    "title": "Colliding alpha follow-up",
                    "priority": 12,
                    "status": "pending",
                    "repo_slugs": ["acme/repo"],
                    "target_files": ["src/alpha.py"],
                },
                # Disjoint pending task (`src/beta.py`) + priority inversion (`P15` depends on `P45`)
                {
                    "conversation_id": "3333cccc-0000-4000-8000-000000000003",
                    "title": "Disjoint beta feature",
                    "priority": 15,
                    "status": "pending",
                    "repo_slugs": ["acme/repo"],
                    "target_files": ["src/beta.py"],
                },
                {
                    "conversation_id": "4444dddd-0000-4000-8000-000000000004",
                    "title": "Urgent dependent task",
                    "priority": 14,
                    "status": "pending",
                    "depends_on": ["5555eeee-0000-4000-8000-000000000005"],
                    "repo_slugs": ["acme/other"],
                    "target_files": ["src/other.py"],
                },
                {
                    "conversation_id": "5555eeee-0000-4000-8000-000000000005",
                    "title": "Lower-priority prerequisite",
                    "priority": 45,
                    "status": "pending",
                    "repo_slugs": ["acme/other"],
                    "target_files": ["src/prereq.py"],
                },
                *[
                    {
                        "conversation_id": f"666{i}ffff-0000-4000-8000-00000000000{i}",
                        "title": f"Half-merged item {i}",
                        "priority": 30 + i,
                        "status": "pending",
                        "repo_slugs": ["acme/upstream", "acme/downstream"],
                        "merged_repos": ["acme/upstream"],
                        "target_files": [f"src/mod_{i}.py"],
                    }
                    for i in (1, 2, 3)
                ],
            ]
            active_locks = {
                "acme/repo": {
                    "active": True,
                    "repo_slug": "acme/repo",
                    "max_slots": 2,
                    "conversation_id": "1111aaaa-0000-4000-8000-000000000001",
                    "branch_name": "fix/20261122-alpha-1111aaaa",
                    "status": "PENDING_HUMAN_PR_VALIDATION",
                    "pr_gate_ready": True,
                    "target_files": ["src/alpha.py"],
                },
                "acme/follower-repo": {
                    "active": True,
                    "repo_slug": "acme/follower-repo",
                    "max_slots": 2,
                    "promoted_from_slot": 2,
                    "conversation_id": "7777aaaa-0000-4000-8000-000000000007",
                    "branch_name": "fix/20261122-follower-7777aaaa",
                    "status": "NEEDS_REBASE_REVERIFY",
                    "pr_gate_ready": False,
                    "base_stale": True,
                    "target_files": ["src/follower.py"],
                },
            }

            report = flow_opt.analyze_dev_flow_optimizations(
                lightweight=True,
                preloaded_items=items,
                preloaded_active_locks=active_locks,
                preloaded_all_locks=active_locks,
                preloaded_repo_dirs={"acme/repo": repo_dir},
            )
            by_code = {f["code"]: f for f in report["findings"]}

            # Verify Phase 2 findings + P14 findings coexist cleanly
            self.assertIn("parallel_lookahead_opportunity", by_code)
            self.assertIn("stale_follower_needs_rebase", by_code)
            self.assertIn("priority_inversion", by_code)
            self.assertIn("partial_wip_accumulation", by_code)

            lookahead_ev = by_code["parallel_lookahead_opportunity"]["evidence"]
            self.assertEqual(lookahead_ev["top_repo_slug"], "acme/repo")
            self.assertEqual(lookahead_ev["top_candidate_id"], "3333cccc-0000-4000-8000-000000000003")
            self.assertEqual(lookahead_ev["top_overlap_tier"], "NONE")

            rebase_ev = by_code["stale_follower_needs_rebase"]["evidence"]
            self.assertEqual(rebase_ev["top_repo_slug"], "acme/follower-repo")
            self.assertEqual(rebase_ev["branch_name"], "fix/20261122-follower-7777aaaa")

            hint_rebase = flow_opt.format_compact_opt_hint(
                {"findings": [by_code["stale_follower_needs_rebase"], by_code["parallel_lookahead_opportunity"]]}
            )
            self.assertIn("rebase-reverify follower-repo", hint_rebase)
            self.assertIn("slot2 lookahead repo", hint_rebase)

    def test_parallel_lookahead_opportunity_git_fallback_when_slot1_targets_empty(self) -> None:
        """Regression test: `_detect_parallel_lookahead_opportunity` must invoke `collect_git_changed_files` with valid kwargs."""
        import subprocess

        def _git(cwd: pathlib.Path, *args: str) -> str:
            proc = subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                check=True,
            )
            return (proc.stdout or "").strip()

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            repo_dir = root / "acme-repo"
            repo_dir.mkdir(parents=True)
            _git(repo_dir, "init", "-b", "main")
            _git(repo_dir, "config", "user.email", "ci@example.com")
            _git(repo_dir, "config", "user.name", "CI Bot")
            (repo_dir / "src").mkdir(parents=True)
            (repo_dir / "src" / "alpha.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")
            (repo_dir / "src" / "beta.py").write_text("def beta() -> int:\n    return 2\n", encoding="utf-8")
            _git(repo_dir, "add", "src")
            _git(repo_dir, "commit", "-m", "feat: seed repo")
            base_sha = _git(repo_dir, "rev-parse", "HEAD")

            wt_dir = root / "pr-worktree-acme-repo-alpha"
            _git(repo_dir, "worktree", "add", "-b", "fix/20261123-alpha-1111aaaa", str(wt_dir), "main")
            (wt_dir / "src" / "alpha.py").write_text("def alpha() -> int:\n    return 42\n", encoding="utf-8")
            _git(wt_dir, "add", "src/alpha.py")
            _git(wt_dir, "commit", "-m", "fix: modify alpha only")

            items = [
                {
                    "conversation_id": "2222bbbb-0000-4000-8000-000000000002",
                    "title": "Colliding alpha task",
                    "priority": 10,
                    "status": "pending",
                    "repo_slugs": ["acme/repo"],
                    "target_files": ["src/alpha.py"],
                },
                {
                    "conversation_id": "3333cccc-0000-4000-8000-000000000003",
                    "title": "Disjoint beta task",
                    "priority": 15,
                    "status": "pending",
                    "repo_slugs": ["acme/repo"],
                    "target_files": ["src/beta.py"],
                },
            ]
            active_locks = {
                "acme/repo": {
                    "active": True,
                    "repo_slug": "acme/repo",
                    "max_slots": 2,
                    "conversation_id": "1111aaaa-0000-4000-8000-000000000001",
                    "branch_name": "fix/20261123-alpha-1111aaaa",
                    "base_sha": base_sha,
                    "worktree_path": str(wt_dir),
                    "status": "PENDING_HUMAN_PR_VALIDATION",
                    "pr_gate_ready": True,
                    "target_files": [],
                    "section_anchors": [],
                }
            }

            report = flow_opt.analyze_dev_flow_optimizations(
                lightweight=True,
                preloaded_items=items,
                preloaded_active_locks=active_locks,
                preloaded_all_locks=active_locks,
                preloaded_repo_dirs={"acme/repo": repo_dir},
            )
            by_code = {f["code"]: f for f in report["findings"]}
            self.assertIn("parallel_lookahead_opportunity", by_code)
            ev = by_code["parallel_lookahead_opportunity"]["evidence"]
            self.assertEqual(ev["top_candidate_id"], "3333cccc-0000-4000-8000-000000000003")
            self.assertEqual(ev["top_overlap_tier"], "NONE")
            self.assertEqual(ev["opportunities"][0]["candidate_count"], 1)

    def test_agent_and_skill_specs_strictly_under_24kb_and_selective_runner_integrity(self) -> None:
        spec_contracts: dict[pathlib.Path, tuple[str, ...]] = {
            REPO_ROOT / "agents" / "agy-dev-orchestrator.md": (
                "--allow-slot2",
                "analyze-overlap",
                "rebase-reverify",
                "git rebase --onto",
                "workspace='share'",
                "DEEP_SHARED",
                "selective_test_runner.py",
                "parallel_lookahead_opportunity",
                "stale_follower_needs_rebase",
            ),
            REPO_ROOT / "agents" / "agy-dev-planner.md": (
                "--allow-slot2",
                "analyze-overlap",
                "rebase-reverify",
                "git rebase --onto",
                "workspace='share'",
                "DEEP_SHARED",
                "selective_test_runner.py",
            ),
            REPO_ROOT / "agents" / "agy-dev-executor.md": (
                "--allow-slot2",
                "analyze-overlap",
                "rebase-reverify",
                "git rebase --onto",
                "workspace='share'",
                "DEEP_SHARED",
                "pretool:parallel-file-lease",
                "pretool:parallel-worker-deploy-home",
                "selective_test_runner.py",
            ),
            REPO_ROOT / "skills" / "agy-code-remediation-pr" / "SKILL.md": (
                "--allow-slot2",
                "analyze-overlap",
                "rebase-reverify",
                "git rebase --onto",
                "workspace='share'",
                "DEEP_SHARED",
                "pretool:parallel-file-lease",
                "pretool:parallel-worker-deploy-home",
            ),
            REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "SKILL.md": (
                "--allow-slot2",
                "analyze-overlap",
                "rebase-reverify",
                "git rebase --onto",
                "workspace='share'",
                "DEEP_SHARED",
                "READY_AWAITING_BASE_MERGE",
                "NEEDS_REBASE_REVERIFY",
                "parallel_lookahead_opportunity",
                "stale_follower_needs_rebase",
            ),
        }
        for md_path, required_terms in spec_contracts.items():
            self.assertTrue(md_path.is_file(), msg=f"Missing spec file: {md_path}")
            size_bytes = md_path.stat().st_size
            self.assertLess(
                size_bytes,
                24000,
                msg=f"{md_path.name} is {size_bytes} bytes (must remain strictly < 24,000 bytes!)",
            )
            content = md_path.read_text(encoding="utf-8")
            for term in required_terms:
                self.assertIn(term, content, msg=f"{md_path.name} missing required Phase 2 contract term: {term}")

        # Verify `selective_test_runner.py --audit-integrity` and routing
        self.assertEqual(srunner.audit_router_integrity(REPO_ROOT), [])
        self.assertIn("tests.test_parallel_file_leases_and_tui", srunner.ALL_TEST_MODULES)
        for changed in (
            ["clients/dev_common.py"],
            ["skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py"],
            ["skills/agy-code-remediation-pr/scripts/dev_flow_optimizer.py"],
            ["tests/test_parallel_file_leases_and_tui.py"],
        ):
            self.assertIn("tests.test_parallel_file_leases_and_tui", srunner.select_test_modules(changed))


if __name__ == "__main__":
    unittest.main()
