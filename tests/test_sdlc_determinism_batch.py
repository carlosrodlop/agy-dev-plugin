#!/usr/bin/env python3
"""Hermetic unit tests for SDLC Determinism Batch (C1+C2+C3+C4)."""

from __future__ import annotations

import contextlib
import importlib.util
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
PR_REM_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
WATCHDOG_DIR = REPO_ROOT / "sidecars" / "dev-queue-watchdog"
DESIGN_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-design-refactor" / "scripts"

for _path in (CLIENTS_DIR, PR_REM_DIR, WATCHDOG_DIR, DESIGN_SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import dev_common  # noqa: E402
import dev_queue_doctor as doctor  # noqa: E402
import git_client  # noqa: E402
import orchestration_control as orch_ctrl  # noqa: E402
import pr_remediator as prs  # noqa: E402
import review_gate  # noqa: E402
import watchdog  # noqa: E402


def _load_complexity_audit():
    spec = importlib.util.spec_from_file_location(
        "agy_complexity_audit_test_mod",
        str(DESIGN_SCRIPTS_DIR / "complexity_audit.py"),
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo_with_tests(root: Path, repo_name: str = "acme-core") -> tuple[Path, Path]:
    bare = root / "remotes" / "acme" / f"{repo_name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, text=True, check=True)

    clone = root / "work" / repo_name
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], capture_output=True, text=True, check=True)
    _git(clone, "config", "user.name", "Test Bot")
    _git(clone, "config", "user.email", "testbot@example.com")
    _git(clone, "checkout", "-B", "main")

    (clone / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
    (clone / "README.md").write_text("# Acme Core\n", encoding="utf-8")
    (clone / "src").mkdir(parents=True, exist_ok=True)
    (clone / "tests").mkdir(parents=True, exist_ok=True)
    (clone / "src" / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (clone / "tests" / "test_app.py").write_text(
        "import pathlib, sys, unittest\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
        "import app\n\n"
        "class TestApp(unittest.TestCase):\n"
        "    def test_add(self) -> None:\n"
        "        self.assertEqual(app.add(1, 2), 3)\n",
        encoding="utf-8",
    )
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "feat: initial seed")
    _git(clone, "push", "-u", "origin", "main")
    return bare, clone


def _write_state_files(
    state_dir: Path,
    *,
    items: list[dict] | None = None,
    locks: dict | None = None,
    orchestrator_id: str = "orch-11111111",
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    dev_common.atomic_write_json(
        state_dir / "dev_queue.json",
        {
            "schema_version": 1,
            "orchestrator_conversation_id": orchestrator_id,
            "items": list(items or []),
        },
        mode=0o600,
    )
    dev_common.atomic_write_json(
        state_dir / "actions_state.json",
        {"actions_log_audit_state": {"dev_pr_locks": dict(locks or {})}},
        mode=0o600,
    )


class TestOrchestratorTickAndWatchdogRebase(unittest.TestCase):
    """(C1) Unified 0-LLM `orchestrator-tick` and Watchdog 0-LLM `rebase-reverify`."""

    def test_orchestrator_tick_unifies_heal_compact_optimize_and_next_and_respects_pause(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            _write_state_files(
                state_dir,
                items=[
                    {
                        "conversation_id": "aaaa1111-1111-4111-8111-111111111111",
                        "priority": 10,
                        "status": "pr_open",
                        "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                        "merged_repos": ["acme/repo-a"],
                        "pr_urls": [],
                        "target_files": ["src/mod_a.py"],
                        "title": "Phantom pr_open partial multi-repo",
                    },
                    {
                        "conversation_id": "bbbb2222-2222-4222-8222-222222222222",
                        "priority": 20,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-c"],
                        "target_files": ["src/alpha.py"],
                        "proposed_fix": "Update alpha helper",
                        "title": "Alpha task",
                    },
                    {
                        "conversation_id": "cccc3333-3333-4333-8333-333333333333",
                        "priority": 25,
                        "status": "pending",
                        "repo_slugs": ["acme/repo-c"],
                        "depends_on": ["bbbb2222-2222-4222-8222-222222222222"],
                        "target_files": ["src/alpha.py"],
                        "proposed_fix": "Tighten alpha edge case",
                        "title": "Alpha follow-up",
                    },
                ],
                locks={},
            )

            with mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(state_dir)}):
                tick = prs.run_orchestrator_tick(
                    orchestrator_id="orch-11111111",
                    portfolio_dir=state_dir,
                    brain_dir=brain_dir,
                    sync_git=False,
                )

            self.assertEqual(tick["status"], "ok")
            self.assertEqual(tick["mode"], "orchestrator_tick")
            self.assertFalse(tick["paused"])
            self.assertGreaterEqual(tick["auto_heals"]["reconciled_count"], 1)
            self.assertEqual(tick["compaction"]["mode"], "applied")
            self.assertEqual(tick["compaction"]["applied_count"], 1)
            self.assertIn("acme/repo-b", tick["queue_next"]["runnable_by_repo"])
            self.assertIn("acme/repo-c", tick["queue_next"]["runnable_by_repo"])
            self.assertTrue(tick["summary"]["needs_llm_action"])

            # Verify CLI entrypoint works and emits JSON
            buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(state_dir)}),
                contextlib.redirect_stdout(buf),
            ):
                rc = prs.main(
                    [
                        "orchestrator-tick",
                        "--orchestrator-id",
                        "orch-11111111",
                        "--state-dir",
                        str(state_dir),
                        "--no-sync-git",
                    ]
                )
            self.assertEqual(rc, 0)
            cli_payload = json.loads(buf.getvalue())
            self.assertEqual(cli_payload["mode"], "orchestrator_tick")

            # Pause orchestration -> tick skips mutations cleanly
            orch_ctrl.pause_orchestration(state_dir=state_dir, reason="maintenance")
            paused_tick = prs.run_orchestrator_tick(
                orchestrator_id="orch-11111111",
                portfolio_dir=state_dir,
                brain_dir=brain_dir,
                sync_git=True,
            )
            self.assertTrue(paused_tick["paused"])
            self.assertFalse(paused_tick["auto_heals"]["ran"])
            self.assertEqual(paused_tick["compaction"]["mode"], "skipped_paused")
            self.assertFalse(paused_tick["summary"]["needs_llm_action"])

    def test_watchdog_runs_zero_llm_rebase_reverify_and_wakes_only_on_agent_intervention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            brain_dir = root / "brain"
            home_dir = root / "home"
            agentapi_bin = home_dir / ".gemini" / "antigravity" / "bin" / "agentapi"
            agentapi_bin.parent.mkdir(parents=True)
            agentapi_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            os.chmod(agentapi_bin, 0o755)
            now_ts = 1_800_500_000.0

            _write_state_files(
                state_dir,
                items=[],
                locks={
                    "acme/repo-b": {
                        "active": True,
                        "slot_id": 1,
                        "status": "NEEDS_REBASE_REVERIFY",
                        "needs_base_rebase": True,
                        "branch_name": "fix/20261125-100000-slot2-33333333",
                        "conversation_id": "33333333-3333-4333-8333-333333333333",
                        "worktree_path": str(root / "wt"),
                    }
                },
            )

            def _fake_auto_promote(**kwargs):
                prs.update_dev_pr_lock(
                    {
                        "status": "PENDING_HUMAN_PR_VALIDATION",
                        "needs_base_rebase": False,
                        "base_stale": False,
                        "pr_gate_ready": True,
                    },
                    portfolio_dir=kwargs["portfolio_dir"],
                    repo_slug=kwargs["repo_slug"],
                    branch_name=kwargs["branch_name"],
                    slot_id=1,
                )
                return {
                    "status": "ok",
                    "outcome": "AUTO_PROMOTED_ZERO_LLM",
                    "promoted_to_human_pr": True,
                }

            mock_popen = mock.MagicMock()
            with mock.patch.object(review_gate, "run_slot_rebase_reverify", side_effect=_fake_auto_promote) as spy_rb:
                res_ok = watchdog.run_watchdog_cycle(
                    state_dir=state_dir,
                    brain_dir=brain_dir,
                    home_dir=home_dir,
                    sync_git=False,
                    now_ts=now_ts,
                    env={"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:51234"},
                    popen_fn=mock_popen,
                    ps_output="",
                )
            spy_rb.assert_called_once()
            mock_popen.assert_not_called()
            self.assertEqual(res_ok["status"], "healed")
            self.assertTrue(res_ok["healed"])
            self.assertFalse(res_ok["woke"])
            self.assertTrue(res_ok["auto_promoted_zero_llm"])

            # Now simulate rebase conflict -> watchdog wakes LLM orchestrator
            _write_state_files(
                state_dir,
                items=[],
                locks={
                    "acme/repo-b": {
                        "active": True,
                        "slot_id": 1,
                        "status": "NEEDS_REBASE_REVERIFY",
                        "needs_base_rebase": True,
                        "branch_name": "fix/20261125-100500-conflict-44444444",
                        "conversation_id": "44444444-4444-4444-8444-444444444444",
                        "worktree_path": str(root / "wt2"),
                    }
                },
            )

            def _fake_conflict(**kwargs):
                prs.update_dev_pr_lock(
                    {
                        "status": "NEEDS_AGENT_CONFLICT_RESOLUTION",
                        "needs_base_rebase": False,
                        "base_stale": False,
                    },
                    portfolio_dir=kwargs["portfolio_dir"],
                    repo_slug=kwargs["repo_slug"],
                    branch_name=kwargs["branch_name"],
                    slot_id=1,
                )
                return {
                    "status": "ok",
                    "outcome": "NEEDS_AGENT_CONFLICT_RESOLUTION",
                    "promoted_to_human_pr": False,
                }

            mock_proc = mock.MagicMock()
            mock_proc.pid = 91234
            mock_popen.return_value = mock_proc
            with mock.patch.object(review_gate, "run_slot_rebase_reverify", side_effect=_fake_conflict):
                res_wake = watchdog.run_watchdog_cycle(
                    state_dir=state_dir,
                    brain_dir=brain_dir,
                    home_dir=home_dir,
                    sync_git=False,
                    now_ts=now_ts + 100.0,
                    env={"ANTIGRAVITY_LS_ADDRESS": "127.0.0.1:51234"},
                    which_fn=lambda _n: None,
                    popen_fn=mock_popen,
                    pid_alive_fn=lambda _p: False,
                    ps_output="",
                )
            self.assertEqual(res_wake["status"], "woke")
            self.assertTrue(res_wake["woke"])
            mock_popen.assert_called_once()


class TestPreReviewFailFastGate(unittest.TestCase):
    """(C2) 0-LLM Pre-Review Fail-Fast Gate in `review_gate.py`."""

    def test_blocks_missing_test_coverage_audit_errors_and_sha_mismatch_while_allowing_doc_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdir = root / "state"
            pdir.mkdir(parents=True)
            _, clone = _init_repo_with_tests(root)
            vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(clone)})

            # 1. Non-doc code change in repo with `tests/` and NO diff tests / NO verified test-local -> BLOCKED
            s1 = prs.start_remediation_branch_and_lock(
                conversation_id="11111111-1111-4111-8111-111111111111",
                action_id="code-no-test",
                rca_summary="Add sub helper",
                proposed_fix="Add sub()",
                create_worktree=True,
                branch_timestamp="20261125-110000",
                workdir=clone,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            wt1 = Path(s1["worktree_path"])
            (wt1 / "src" / "app.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\n"
                "def sub(a: int, b: int) -> int:\n    return a - b\n",
                encoding="utf-8",
            )
            blocked_cov = prs.push_remediation_branch_and_build_reviewer_plan(
                title="feat(app): add sub",
                workdir=wt1,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(blocked_cov["status"], "error")
            self.assertEqual(blocked_cov["step"], "pre_review_gate")
            self.assertFalse(blocked_cov["pre_review_gate"]["diff_test_coverage"]["passed"])

            # 2. Add Santa / bare-except violation -> BLOCKED by `project_audit --strict`
            (wt1 / "tests" / "test_app.py").write_text(
                "import pathlib, sys, unittest\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
                "import app\n\n"
                "class TestApp(unittest.TestCase):\n"
                "    def test_sub(self) -> None:\n"
                "        try:\n"
                "            self.assertEqual(app.sub(5, 2), 3)\n"
                "        except:\n"
                "            pass\n",
                encoding="utf-8",
            )
            blocked_audit = prs.push_remediation_branch_and_build_reviewer_plan(
                title="feat(app): add sub with test",
                workdir=wt1,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(blocked_audit["status"], "error")
            self.assertEqual(blocked_audit["step"], "pre_review_gate")
            self.assertFalse(blocked_audit["pre_review_gate"]["project_audit"]["passed"])

            # 3. Fix bare-except + run `test-local` -> PASSES!
            (wt1 / "tests" / "test_app.py").write_text(
                "import pathlib, sys, unittest\n"
                "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
                "import app\n\n"
                "class TestApp(unittest.TestCase):\n"
                "    def test_sub(self) -> None:\n"
                "        self.assertEqual(app.sub(5, 2), 3)\n",
                encoding="utf-8",
            )
            _git(wt1, "add", ".")
            _git(wt1, "commit", "-m", "feat: add sub and test")
            val_ok = prs.run_local_validation_suite(
                workdir=wt1,
                portfolio_dir=pdir,
                pre_commit_status="pass",
                run_bats=False,
            )
            self.assertTrue(val_ok["passed"])
            staged_ok = prs.push_remediation_branch_and_build_reviewer_plan(
                title="feat(app): add sub with test",
                workdir=wt1,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(staged_ok["status"], "ok")
            self.assertTrue(staged_ok["pre_review_gate"]["passed"])

            # 4. Edit code AFTER `test-local` without re-running -> BLOCKED by SHA parity!
            (wt1 / "src" / "app.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\n"
                "def sub(a: int, b: int) -> int:\n    return a - b + 0\n",
                encoding="utf-8",
            )
            blocked_sha = prs.push_remediation_branch_and_build_reviewer_plan(
                title="feat(app): stale verification",
                workdir=wt1,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(blocked_sha["status"], "error")
            self.assertEqual(blocked_sha["step"], "pre_review_gate")
            self.assertFalse(blocked_sha["pre_review_gate"]["sha_parity"]["passed"])

            # `branch-diff` surfaces `pre_review_gate` cleanly alongside `verification_gate`
            bdiff = prs.get_branch_diff_with_verification_gate(
                branch=s1["branch_name"],
                workdir=wt1,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(bdiff["status"], "ok")
            self.assertIn("pre_review_gate", bdiff)
            self.assertFalse(bdiff["pre_review_gate"]["sha_parity"]["passed"])

            # 5. Doc-only flow passes without test changes!
            prs.release_dev_pr_lock(
                reason="test_cleanup",
                repo_slug=s1["repo_slug"],
                branch_name=s1["branch_name"],
                portfolio_dir=pdir,
            )
            s2 = prs.start_remediation_branch_and_lock(
                conversation_id="22222222-2222-4222-8222-222222222222",
                action_id="docs-only",
                rca_summary="Docs clarification",
                proposed_fix="Update README.md",
                create_worktree=True,
                branch_timestamp="20261125-111000",
                workdir=clone,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            wt2 = Path(s2["worktree_path"])
            (wt2 / "README.md").write_text("# Acme Core\n\nUpdated docs.\n", encoding="utf-8")
            staged_doc = prs.push_remediation_branch_and_build_reviewer_plan(
                title="docs(readme): clarify usage",
                workdir=wt2,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(staged_doc["status"], "ok")
            self.assertTrue(staged_doc["pre_review_gate"]["doc_only"])
            self.assertTrue(staged_doc["pre_review_gate"]["passed"])

    def test_project_audit_errors_are_diff_scoped_and_warnings_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)

            # (a) Untouched-file errors do NOT block when changed_files is non-empty
            res_untouched = review_gate.evaluate_pre_review_fail_fast_gate(
                repo_dir,
                changed_files=["src/app.py", "tests/test_app.py"],
                project_audit_fn=lambda _r, _s: {
                    "passed": False,
                    "errors": [
                        {
                            "severity": "error",
                            "rule_id": "pii:home-path",
                            "file": "tests/legacy_fixture.py",
                            "line": 12,
                            "message": "pre-existing fixture",
                        }
                    ],
                    "warnings": [],
                },
            )
            self.assertTrue(res_untouched["passed"])
            self.assertTrue(res_untouched["project_audit"]["passed"])
            self.assertFalse(res_untouched["project_audit"]["repo_passed"])
            self.assertEqual(res_untouched["project_audit"]["errors_count"], 1)
            self.assertEqual(res_untouched["project_audit"]["diff_errors_count"], 0)
            self.assertEqual(res_untouched["project_audit"]["untouched_errors_count"], 1)
            self.assertEqual(res_untouched["warnings_count"], 1)
            self.assertEqual(res_untouched["warnings"][0]["code"], "project_audit_untouched:pii:home-path")

            # (b) Errors inside changed_files STILL fail fast
            res_in_diff = review_gate.evaluate_pre_review_fail_fast_gate(
                repo_dir,
                changed_files=["src/app.py", "tests/test_app.py"],
                project_audit_fn=lambda _r, _s: {
                    "passed": False,
                    "errors": [
                        {
                            "severity": "error",
                            "rule_id": "pii:home-path",
                            "file": "src/app.py",
                            "line": 4,
                            "message": "hardcoded path in diff",
                        },
                        {
                            "severity": "error",
                            "rule_id": "santa:curl-pipe",
                            "file": "tests/legacy_fixture.py",
                            "line": 22,
                            "message": "untouched error",
                        },
                    ],
                    "warnings": [],
                },
            )
            self.assertFalse(res_in_diff["passed"])
            self.assertFalse(res_in_diff["project_audit"]["passed"])
            self.assertEqual(res_in_diff["project_audit"]["errors_count"], 2)
            self.assertEqual(res_in_diff["project_audit"]["diff_errors_count"], 1)
            self.assertEqual(res_in_diff["project_audit"]["untouched_errors_count"], 1)
            self.assertEqual([e["code"] for e in res_in_diff["errors"]], ["project_audit:pii:home-path"])

            # (c) Returned warnings/warning_messages lists are bounded (<= 50) while preserving warnings_count
            res_capped = review_gate.evaluate_pre_review_fail_fast_gate(
                repo_dir,
                changed_files=["README.md"],
                project_audit_fn=lambda _r, _s: {
                    "passed": False,
                    "errors": [
                        {
                            "severity": "error",
                            "rule_id": "santa:bin",
                            "file": f"tests/untouched_{idx}.py",
                            "line": idx + 1,
                            "message": f"untouched err {idx}",
                        }
                        for idx in range(2)
                    ],
                    "warnings": [
                        {
                            "severity": "warning",
                            "rule_id": "html:inline-style",
                            "file": f"assets/exam_{idx}.html",
                            "line": idx + 1,
                            "message": f"untouched warn {idx}",
                        }
                        for idx in range(120)
                    ],
                },
            )
            self.assertTrue(res_capped["passed"])
            self.assertTrue(res_capped["project_audit"]["passed"])
            self.assertEqual(len(res_capped["warnings"]), 50)
            self.assertEqual(len(res_capped["warning_messages"]), 50)
            self.assertEqual(res_capped["warnings_count"], 122)
            self.assertEqual(res_capped["warnings"][0]["code"], "project_audit_untouched:santa:bin")


class TestComplexityAuditAndDocsContracts(unittest.TestCase):
    """(C3 & C4) Pure-stdlib AST complexity auditor, SKILL/agent contracts, and router integrity."""

    def test_ast_complexity_metrics_elif_nesting_and_god_file_detection(self) -> None:
        ca = _load_complexity_audit()
        sample_src = "\n".join(
            [
                "def classify(x: int, y: int) -> str:",
                "    if x < 0:",
                "        return 'neg'",
                "    elif x == 0:",
                "        return 'zero'",
                "    elif x > 10 and y > 10:",
                "        for i in range(x):",
                "            if i % 2 == 0:",
                "                return 'even-high'",
                "    return 'pos'",
                "",
                "def outer(items: list[int]) -> list[int]:",
                "    def inner(v: int) -> int:",
                "        return v * 2",
                "    return [inner(v) for v in items if v > 0]",
            ]
        )
        fm = ca.analyze_python_source(
            sample_src,
            rel_path="sample.py",
            thresholds=ca.ComplexityThresholds(max_file_loc=10, max_func_loc=8, max_complexity=4, max_nesting=2),
        )
        self.assertTrue(fm.is_god_file)
        by_name = {f.name: f for f in fm.functions}
        self.assertEqual(by_name["classify"].max_nesting_depth, 3)
        # 1 base + if + elif + elif + boolOp(and) + for + if = 7
        self.assertEqual(by_name["classify"].cyclomatic_complexity, 7)
        # `outer` must not inherit `inner`'s body metrics; has comprehension(+1) + 1 if filter(+1) = 3
        self.assertEqual(by_name["outer"].cyclomatic_complexity, 3)
        self.assertEqual(by_name["outer.inner"].cyclomatic_complexity, 1)

        rule_ids = {v.rule_id for v in fm.violations}
        self.assertIn("complexity:god-file-loc", rule_ids)
        self.assertIn("complexity:function-loc", rule_ids)
        self.assertIn("complexity:cyclomatic", rule_ids)
        self.assertIn("complexity:nesting-depth", rule_ids)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "mod.py"
            target.write_text(sample_src + "\n", encoding="utf-8")
            buf_fail = io.StringIO()
            with contextlib.redirect_stdout(buf_fail):
                rc_fail = ca.main([str(target), "--max-file-loc", "10", "--json"])
            self.assertEqual(rc_fail, 1)
            payload = json.loads(buf_fail.getvalue())
            self.assertFalse(payload["passed"])
            self.assertEqual(payload["god_files"][0]["path"], "mod.py")

            buf_warn = io.StringIO()
            with contextlib.redirect_stdout(buf_warn):
                rc_warn = ca.main([str(target), "--max-file-loc", "10", "--warn-only", "--json"])
            self.assertEqual(rc_warn, 0)

            buf_missing = io.StringIO()
            with contextlib.redirect_stdout(buf_missing):
                rc_missing = ca.main([str(Path(tmp) / "missing.py"), "--json"])
            self.assertEqual(rc_missing, 2)

    def test_agent_skill_size_limits_and_selective_runner_registration(self) -> None:
        orch_path = REPO_ROOT / "agents" / "agy-dev-orchestrator.md"
        refactor_skill_path = REPO_ROOT / "skills" / "agy-code-design-refactor" / "SKILL.md"
        self.assertLess(orch_path.stat().st_size, 24_000)
        self.assertLess(refactor_skill_path.stat().st_size, 24_000)
        self.assertIn("orchestrator-tick", orch_path.read_text(encoding="utf-8"))
        self.assertIn("complexity_audit.py", refactor_skill_path.read_text(encoding="utf-8"))

        sys.path.insert(0, str(REPO_ROOT / "tests" / "scripts"))
        import selective_test_runner as str_mod  # noqa: E402

        self.assertEqual(str_mod.audit_router_integrity(REPO_ROOT), [])
        self.assertIn("tests.test_sdlc_determinism_batch", str_mod.ALL_TEST_MODULES)
        self.assertIn(
            "tests.test_sdlc_determinism_batch",
            str_mod.select_test_modules(["skills/agy-code-design-refactor/scripts/complexity_audit.py"]),
        )


if __name__ == "__main__":
    unittest.main()
