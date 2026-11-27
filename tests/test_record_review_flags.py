"""Hermetic Zero-PII unit tests for `--reviewer-subagent-id` CLI parity, Slot #1 PASS flags, and `rebase-reverify` TOCTOU/escalation guards."""

from __future__ import annotations

from contextlib import redirect_stdout
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
SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
CLIENTS_DIR = REPO_ROOT / "clients"
RUNNER_DIR = REPO_ROOT / "tests" / "scripts"
for _p in (str(SCRIPTS_DIR), str(CLIENTS_DIR), str(RUNNER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import git_client  # noqa: E402
import lock_store  # noqa: E402
import pr_remediator as prs  # noqa: E402
import review_gate  # noqa: E402
import selective_test_runner  # noqa: E402

_GIT_ENV_STRIP = {
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
}


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_STRIP}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=_git_env(),
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    return (proc.stdout or "").strip()


def _init_hermetic_repo(root: Path, slug_name: str = "repo-alpha") -> tuple[Path, Path, git_client.GitVcsIntegrationClient]:
    """Create a local bare `origin` (`acme/<slug_name>.git`) and cloned working repository."""
    bare = root / "remotes" / "acme" / f"{slug_name}.git"
    bare.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(bare)], env=_git_env(), check=True, capture_output=True)

    clone = root / "checkouts" / slug_name
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", f"file://{bare}", str(clone)], env=_git_env(), check=True, capture_output=True)
    _git(clone, "config", "user.name", "Acme Test Bot")
    _git(clone, "config", "user.email", "bot@example.invalid")

    (clone / "src").mkdir(parents=True, exist_ok=True)
    (clone / "src" / "alpha.py").write_text("def alpha() -> int:\n    return 1\n", encoding="utf-8")
    (clone / "src" / "beta.py").write_text("def beta() -> int:\n    return 2\n", encoding="utf-8")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "chore: initial fixture")
    _git(clone, "push", "-u", "origin", "main")

    vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(clone)})
    return bare, clone, vcs


def _push_upstream_commit(bare: Path, root: Path, rel_path: str, content: str, msg: str = "feat: upstream update") -> str:
    """Push an upstream commit directly to `origin/main` via a scratch clone and return the new `main` SHA."""
    pusher = root / f"pusher-{abs(hash((rel_path, content, msg))) % 100000}"
    subprocess.run(["git", "clone", f"file://{bare}", str(pusher)], env=_git_env(), check=True, capture_output=True)
    _git(pusher, "config", "user.name", "Upstream Bot")
    _git(pusher, "config", "user.email", "upstream@example.invalid")
    target = pusher / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(pusher, "add", "-A")
    _git(pusher, "commit", "-m", msg)
    _git(pusher, "push", "origin", "main")
    return _git(pusher, "rev-parse", "HEAD")


class TestRecordReviewAndHeartbeatFlags(unittest.TestCase):
    """Verify `--reviewer-subagent-id` / `--reviewer-cid` CLI parity, docs contracts, Slot #1 PASS flags, and TOCTOU guards."""

    def test_record_review_cli_aliases_and_reviewer_docs_contract(self) -> None:
        """Verify `record-review` accepts `--subagent-id`, `--reviewer-subagent-id`, and `--reviewer-cid` with updated prompts/docs."""
        expected_cid = "11112222-3333-4444-8555-666677778888"
        for flag in ("--subagent-id", "--reviewer-subagent-id", "--reviewer-cid"):
            parsed = prs.parse_args(
                [
                    "record-review",
                    "--branch",
                    "fix/20261127-210000-acme-11112222",
                    flag,
                    expected_cid,
                    "--verdict",
                    "PASS",
                    "--score",
                    "96",
                    "--summary",
                    "All checks passed.",
                ]
            )
            self.assertEqual(parsed.subcommand, "record-review")
            self.assertEqual(parsed.subagent_id, expected_cid)

            buf = io.StringIO()
            with (
                mock.patch.object(
                    prs,
                    "record_pr_review_evaluation",
                    return_value={"status": "ok", "verdict": "PASS"},
                ) as spy_rec,
                redirect_stdout(buf),
            ):
                rc = review_gate.dispatch_review_cli(parsed)
            self.assertEqual(rc, 0)
            self.assertEqual(spy_rec.call_args.kwargs["reviewer_subagent_id"], expected_cid)

        with tempfile.TemporaryDirectory() as tmp:
            plan = prs.build_pr_reviewer_subagent_plan(
                conversation_id="aaaabbbb-cccc-4ddd-8eee-ffff00001111",
                branch_name="fix/20261127-210000-acme-aaaabbbb",
                portfolio_dir=Path(tmp),
            )
        self.assertIn("--reviewer-subagent-id <YOUR_CID>", plan["subagent_definition"]["SystemPrompt"])
        self.assertIn("--reviewer-subagent-id <YOUR_CID>", plan["subagent_invocation"]["Prompt"])

        reviewer_md_path = REPO_ROOT / "agents" / "agy-code-pr-reviewer.md"
        reviewer_md = reviewer_md_path.read_text(encoding="utf-8")
        self.assertIn("--reviewer-subagent-id <YOUR_CID>", reviewer_md)
        self.assertLess(len(reviewer_md.encode("utf-8")), 24_000)

    def test_lock_heartbeat_reviewer_subagent_id_cli_and_deduplicated_subagents(self) -> None:
        """Verify `lock-heartbeat` `--reviewer-subagent-id` / `--reviewer-cid` updates lock and deduplicates `subagent_conversation_ids`."""
        rev_cid_1 = "aaaa1111-2222-4333-8444-555566667777"
        rev_cid_2 = "bbbb1111-2222-4333-8444-555566667777"
        dev_cid = "cccc1111-2222-4333-8444-555566667777"

        for flag in ("--reviewer-subagent-id", "--reviewer-cid"):
            parsed = prs.parse_args(
                [
                    "lock-heartbeat",
                    "--repo",
                    "acme/repo-alpha",
                    flag,
                    rev_cid_1,
                    "--stage",
                    "reviewing",
                ]
            )
            self.assertEqual(parsed.subcommand, "lock-heartbeat")
            self.assertEqual(parsed.reviewer_subagent_id, rev_cid_1)

        with tempfile.TemporaryDirectory() as tmp:
            troot = Path(tmp)
            pdir = troot / "state"
            pdir.mkdir(parents=True)
            _, clone, vcs = _init_hermetic_repo(troot)

            s1 = prs.start_remediation_branch_and_lock(
                conversation_id="10101010-1111-4222-8333-404040404040",
                action_id="hb-test",
                target_files=["src/alpha.py"],
                create_worktree=True,
                branch_timestamp="20261127-210500",
                workdir=clone,
                portfolio_dir=pdir,
                git_client=vcs,
                worker_cid=dev_cid,
            )
            wt1 = Path(s1["worktree_path"])
            prs.update_dev_pr_lock(
                {"subagent_conversation_ids": [dev_cid]},
                portfolio_dir=pdir,
                workdir=wt1,
            )

            # Empty reviewer_subagent_id leaves existing subagent_conversation_ids untouched
            hb_empty = prs.record_dev_pr_lock_heartbeat(
                workdir=wt1,
                stage_label="coding",
                reviewer_subagent_id="   ",
                portfolio_dir=pdir,
            )
            self.assertEqual(hb_empty["status"], "ok")
            self.assertEqual(hb_empty["lock"]["subagent_conversation_ids"], [dev_cid])

            # Non-empty reviewer_subagent_id appends deduplicated preserving order
            hb_rev1 = prs.record_dev_pr_lock_heartbeat(
                workdir=wt1,
                reviewer_subagent_id=rev_cid_1,
                stage_label="reviewing",
                portfolio_dir=pdir,
            )
            self.assertEqual(hb_rev1["status"], "ok")
            self.assertEqual(hb_rev1["lock"]["reviewer_subagent_id"], rev_cid_1)
            self.assertEqual(hb_rev1["lock"]["subagent_conversation_ids"], [dev_cid, rev_cid_1])

            # Repeated heartbeat with same reviewer CID stays deduplicated; second reviewer CID appends cleanly
            prs.record_dev_pr_lock_heartbeat(
                workdir=wt1,
                reviewer_subagent_id=rev_cid_1,
                portfolio_dir=pdir,
            )
            hb_rev2 = prs.record_dev_pr_lock_heartbeat(
                workdir=wt1,
                reviewer_subagent_id=rev_cid_2,
                portfolio_dir=pdir,
            )
            self.assertEqual(
                hb_rev2["lock"]["subagent_conversation_ids"],
                [dev_cid, rev_cid_1, rev_cid_2],
            )

            # Dispatch via CLI parser with `--reviewer-cid`
            cli_args = prs.parse_args(
                [
                    "lock-heartbeat",
                    "--workdir",
                    str(wt1),
                    "--reviewer-cid",
                    rev_cid_2,
                    "--stage",
                    "verdict",
                ]
            )
            out_buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"WIZ_CUSTOMER_PORTFOLIO_DIR": str(pdir)}),
                redirect_stdout(out_buf),
            ):
                rc = lock_store.dispatch_lock_cli(cli_args)
            self.assertEqual(rc, 0)
            payload = json.loads(out_buf.getvalue())
            self.assertEqual(payload["lock"]["reviewer_subagent_id"], rev_cid_2)
            self.assertEqual(
                payload["lock"]["subagent_conversation_ids"],
                [dev_cid, rev_cid_1, rev_cid_2],
            )

            prs.cleanup_worktree(wt1, repo_dir=clone)

    def test_slot1_pass_sets_review_approved_and_clears_needs_base_rebase_only_when_up_to_date(self) -> None:
        """Verify Slot #1 PASS sets `review_approved=True` and clears `needs_base_rebase=False` iff merge-base == `origin/main`."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(prs, "spawn_pr_merge_watcher_daemon", return_value={"status": "mocked", "spawned": False}):
            troot = Path(tmp)
            pdir = troot / "state"
            pdir.mkdir(parents=True)
            bare, clone, vcs = _init_hermetic_repo(troot)

            # Case 1: Branch merge-base matches `origin/main` -> `needs_base_rebase` cleared to False
            s1 = prs.start_remediation_branch_and_lock(
                conversation_id="20202020-1111-4222-8333-505050505050",
                action_id="slot1-up-to-date",
                target_files=["src/alpha.py"],
                create_worktree=True,
                branch_timestamp="20261127-211000",
                workdir=clone,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            wt1 = Path(s1["worktree_path"])
            br1 = s1["branch_name"]
            (wt1 / "src" / "alpha.py").write_text("def alpha() -> int:\n    return 11\n", encoding="utf-8")
            _git(wt1, "add", "-A")
            _git(wt1, "commit", "-m", "fix(alpha): return 11")
            head1 = _git(wt1, "rev-parse", "HEAD")

            prs.update_dev_pr_lock(
                {
                    "status": "AWAITING_AGENT_REVIEW",
                    "needs_base_rebase": True,
                    "base_stale": True,
                    "review_approved": False,
                    "staged_head_sha": head1,
                    "head_sha": head1,
                    "verification_results": {"passed": True, "head_sha": head1, "dirty": False},
                },
                portfolio_dir=pdir,
                workdir=wt1,
                branch_name=br1,
            )

            rev_ok = prs.record_pr_review_evaluation(
                verdict="PASS",
                score=95,
                summary="Clean implementation and tests.",
                branch_name=br1,
                reviewer_subagent_id="rev-slot1-aaaa",
                portfolio_dir=pdir,
                git_client=vcs,
                workdir=wt1,
            )
            self.assertEqual(rev_ok["status"], "ok")
            self.assertTrue(rev_ok["ready_for_human_pr"])
            lk1 = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt1, branch_name=br1)
            self.assertEqual(lk1["status"], "PENDING_HUMAN_PR_VALIDATION")
            self.assertTrue(lk1["review_approved"])
            self.assertFalse(lk1["needs_base_rebase"])
            self.assertFalse(lk1.get("base_stale"))
            self.assertIn("rev-slot1-aaaa", lk1["subagent_conversation_ids"])

            prs.release_dev_pr_lock(reason="TEST_DONE", portfolio_dir=pdir, workdir=wt1, branch_name=br1)
            prs.cleanup_worktree(wt1, repo_dir=clone)

            # Case 2: `origin/main` advances in worktree's fetched refs -> merge-base != `origin/main` -> `needs_base_rebase` stays True
            s2 = prs.start_remediation_branch_and_lock(
                conversation_id="30303030-1111-4222-8333-606060606060",
                action_id="slot1-behind",
                target_files=["src/beta.py"],
                create_worktree=True,
                branch_timestamp="20261127-211500",
                workdir=clone,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            wt2 = Path(s2["worktree_path"])
            br2 = s2["branch_name"]
            (wt2 / "src" / "beta.py").write_text("def beta() -> int:\n    return 22\n", encoding="utf-8")
            _git(wt2, "add", "-A")
            _git(wt2, "commit", "-m", "fix(beta): return 22")
            head2 = _git(wt2, "rev-parse", "HEAD")

            _push_upstream_commit(bare, troot, "src/other.py", "X = 1\n", "feat: upstream drift")
            _git(wt2, "fetch", "origin", "--quiet")

            prs.update_dev_pr_lock(
                {
                    "status": "AWAITING_AGENT_REVIEW",
                    "needs_base_rebase": True,
                    "review_approved": False,
                    "staged_head_sha": head2,
                    "head_sha": head2,
                    "verification_results": {"passed": True, "head_sha": head2, "dirty": False},
                },
                portfolio_dir=pdir,
                workdir=wt2,
                branch_name=br2,
            )

            rev_behind = prs.record_pr_review_evaluation(
                verdict="PASS",
                score=94,
                summary="Approved while behind origin/main.",
                branch_name=br2,
                portfolio_dir=pdir,
                git_client=vcs,
                workdir=wt2,
            )
            self.assertEqual(rev_behind["status"], "ok")
            lk2 = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt2, branch_name=br2)
            self.assertTrue(lk2["review_approved"])
            self.assertTrue(lk2["needs_base_rebase"])

            prs.release_dev_pr_lock(reason="TEST_DONE", portfolio_dir=pdir, workdir=wt2, branch_name=br2)
            prs.cleanup_worktree(wt2, repo_dir=clone)

    def test_rebase_reverify_toctou_fresh_reread_never_downgrades_concurrent_review(self) -> None:
        """Verify `run_slot_rebase_reverify` re-reads fresh lock state and NEVER downgrades concurrent PASS/escalated states."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(prs, "spawn_pr_merge_watcher_daemon", return_value={"status": "mocked", "spawned": False}):
            troot = Path(tmp)
            pdir = troot / "state"
            pdir.mkdir(parents=True)
            _, clone, vcs = _init_hermetic_repo(troot)

            # 1. Concurrent Slot #1 `record-review` PASS (`PENDING_HUMAN_PR_VALIDATION`) while `rebase-reverify` runs
            s1 = prs.start_remediation_branch_and_lock(
                conversation_id="40404040-1111-4222-8333-707070707070",
                action_id="toctou-slot1",
                target_files=["src/alpha.py"],
                create_worktree=True,
                branch_timestamp="20261127-212000",
                workdir=clone,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            wt1 = Path(s1["worktree_path"])
            br1 = s1["branch_name"]
            (wt1 / "src" / "alpha.py").write_text("def alpha() -> int:\n    return 42\n", encoding="utf-8")
            _git(wt1, "add", "-A")
            _git(wt1, "commit", "-m", "fix(alpha): answer 42")
            head1 = _git(wt1, "rev-parse", "HEAD")

            prs.update_dev_pr_lock(
                {
                    "status": "AWAITING_AGENT_REVIEW",
                    "needs_base_rebase": True,
                    "review_approved": False,
                    "staged_head_sha": head1,
                    "head_sha": head1,
                    "verification_results": {"passed": True, "head_sha": head1, "dirty": False},
                },
                portfolio_dir=pdir,
                workdir=wt1,
                branch_name=br1,
            )

            def _concurrent_slot1_pass_during_verify(_wd: Path, _files: list[str]) -> dict[str, object]:
                rec = prs.record_pr_review_evaluation(
                    verdict="PASS",
                    score=96,
                    summary="Concurrent reviewer PASS finished during rebase-reverify.",
                    branch_name=br1,
                    reviewer_subagent_id="concurrent-rev-1",
                    portfolio_dir=pdir,
                    git_client=vcs,
                    workdir=wt1,
                )
                self.assertEqual(rec["status"], "ok")
                return {"passed": True, "summary": "union checks ok"}

            rbr1 = prs.run_slot_rebase_reverify(
                workdir=wt1,
                branch_name=br1,
                fetch=True,
                portfolio_dir=pdir,
                git_client=vcs,
                verify_fn=_concurrent_slot1_pass_during_verify,
            )
            self.assertEqual(rbr1["status"], "ok")
            self.assertEqual(rbr1["lock_status"], "PENDING_HUMAN_PR_VALIDATION")
            self.assertTrue(rbr1["ready_for_human_pr"])
            self.assertTrue(rbr1["pr_gate_ready"])
            self.assertTrue(rbr1["branch_pushed"])
            self.assertIn(br1, rbr1["manual_pr_url"])

            after_s1 = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt1, branch_name=br1)
            self.assertEqual(after_s1["status"], "PENDING_HUMAN_PR_VALIDATION")
            self.assertTrue(after_s1["pr_gate_ready"])
            self.assertTrue(after_s1["branch_pushed"])
            self.assertFalse(after_s1["needs_base_rebase"])

            # 2. Concurrent follower Slot #2 `READY_AWAITING_BASE_MERGE` while `rebase-reverify` runs
            s2 = prs.start_remediation_branch_and_lock(
                conversation_id="50505050-1111-4222-8333-808080808080",
                action_id="toctou-slot2",
                target_files=["src/beta.py"],
                create_worktree=True,
                allow_slot2=True,
                branch_timestamp="20261127-212100",
                workdir=clone,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(s2["slot_id"], 2)
            wt2 = Path(s2["worktree_path"])
            br2 = s2["branch_name"]
            (wt2 / "src" / "beta.py").write_text("def beta() -> int:\n    return 99\n", encoding="utf-8")
            _git(wt2, "add", "-A")
            _git(wt2, "commit", "-m", "fix(beta): answer 99")
            head2 = _git(wt2, "rev-parse", "HEAD")

            prs.update_dev_pr_lock(
                {
                    "status": "AWAITING_AGENT_REVIEW",
                    "review_approved": False,
                    "staged_head_sha": head2,
                    "head_sha": head2,
                },
                portfolio_dir=pdir,
                workdir=wt2,
                branch_name=br2,
                slot_id=2,
            )

            def _concurrent_slot2_pass_during_verify(_wd: Path, _files: list[str]) -> dict[str, object]:
                prs.update_dev_pr_lock(
                    {
                        "status": prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE,
                        "review_approved": True,
                        "reviewer_verdict": "PASS",
                        "reviewer_score": 95,
                    },
                    portfolio_dir=pdir,
                    workdir=wt2,
                    branch_name=br2,
                    slot_id=2,
                )
                return {"passed": True, "summary": "slot2 checks ok"}

            rbr2 = prs.run_slot_rebase_reverify(
                workdir=wt2,
                branch_name=br2,
                fetch=True,
                portfolio_dir=pdir,
                git_client=vcs,
                verify_fn=_concurrent_slot2_pass_during_verify,
            )
            self.assertEqual(rbr2["status"], "ok")
            self.assertEqual(rbr2["lock_status"], prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE)
            after_s2 = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt2, branch_name=br2)
            self.assertEqual(after_s2["status"], prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE)

            prs.release_dev_pr_lock(reason="TEST_DONE", portfolio_dir=pdir, release_all=True)
            prs.cleanup_worktree(wt1, repo_dir=clone)
            prs.cleanup_worktree(wt2, repo_dir=clone)

    def test_rebase_reverify_never_auto_promotes_escalated_max_review_iterations(self) -> None:
        """Verify `ESCALATED_MAX_REVIEW_ITERATIONS` (`review_iteration >= MAX` with non-PASS verdict) never auto-promotes."""
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(prs, "spawn_pr_merge_watcher_daemon", return_value={"status": "mocked", "spawned": False}):
            troot = Path(tmp)
            pdir = troot / "state"
            pdir.mkdir(parents=True)
            bare, clone, vcs = _init_hermetic_repo(troot)

            s1 = prs.start_remediation_branch_and_lock(
                conversation_id="60606060-1111-4222-8333-909090909090",
                action_id="escalated-guard",
                target_files=["src/alpha.py"],
                create_worktree=True,
                branch_timestamp="20261127-212500",
                workdir=clone,
                portfolio_dir=pdir,
                git_client=vcs,
            )
            wt1 = Path(s1["worktree_path"])
            br1 = s1["branch_name"]
            (wt1 / "src" / "alpha.py").write_text("def alpha() -> int:\n    return 77\n", encoding="utf-8")
            _git(wt1, "add", "-A")
            _git(wt1, "commit", "-m", "fix(alpha): escalated candidate")

            _push_upstream_commit(bare, troot, "src/beta.py", "def beta() -> int:\n    return 200\n")

            # Case A: Explicit ESCALATED_MAX_REVIEW_ITERATIONS status even with stale `review_approved=True`
            prs.update_dev_pr_lock(
                {
                    "status": "ESCALATED_MAX_REVIEW_ITERATIONS",
                    "review_approved": True,
                    "review_iteration": prs.MAX_PR_REVIEW_ITERATIONS,
                    "reviewer_verdict": "NEEDS_REVISION",
                    "reviewer_score": 72,
                    "review_history": [
                        {"iteration": 1, "verdict": "WARN", "score": 80},
                        {"iteration": 2, "verdict": "NEEDS_REVISION", "score": 75},
                        {"iteration": 3, "verdict": "NEEDS_REVISION", "score": 72},
                    ],
                },
                portfolio_dir=pdir,
                workdir=wt1,
                branch_name=br1,
            )

            rbr_esc = prs.run_slot_rebase_reverify(
                workdir=wt1,
                branch_name=br1,
                fetch=True,
                portfolio_dir=pdir,
                git_client=vcs,
                verify_fn=lambda _wd, _files: {"passed": True, "summary": "ok"},
            )
            self.assertEqual(rbr_esc["status"], "ok")
            self.assertFalse(rbr_esc["promoted_to_human_pr"])
            self.assertFalse(rbr_esc["ready_for_human_pr"])
            self.assertFalse(rbr_esc["pr_gate_ready"])
            self.assertFalse(rbr_esc["branch_pushed"])
            self.assertEqual(rbr_esc["lock_status"], "ESCALATED_MAX_REVIEW_ITERATIONS")

            lk_esc = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt1, branch_name=br1)
            self.assertEqual(lk_esc["status"], "ESCALATED_MAX_REVIEW_ITERATIONS")
            self.assertFalse(lk_esc["review_approved"])
            self.assertFalse(lk_esc["pr_gate_ready"])

            # Case B: `NEEDS_REBASE_REVERIFY` with `review_iteration >= MAX_PR_REVIEW_ITERATIONS` and last verdict != PASS
            prs.update_dev_pr_lock(
                {
                    "status": prs.SLOT_STATUS_NEEDS_REBASE_REVERIFY,
                    "review_approved": True,
                    "review_iteration": prs.MAX_PR_REVIEW_ITERATIONS,
                    "reviewer_verdict": "WARN",
                    "reviewer_score": 85,
                },
                portfolio_dir=pdir,
                workdir=wt1,
                branch_name=br1,
            )
            rbr_iter = prs.run_slot_rebase_reverify(
                workdir=wt1,
                branch_name=br1,
                fetch=False,
                portfolio_dir=pdir,
                git_client=vcs,
                verify_fn=lambda _wd, _files: {"passed": True, "summary": "ok"},
            )
            self.assertFalse(rbr_iter["promoted_to_human_pr"])
            self.assertFalse(rbr_iter["ready_for_human_pr"])
            self.assertEqual(rbr_iter["lock_status"], "ESCALATED_MAX_REVIEW_ITERATIONS")

            prs.release_dev_pr_lock(reason="TEST_DONE", portfolio_dir=pdir, release_all=True)
            prs.cleanup_worktree(wt1, repo_dir=clone)

    def test_selective_test_runner_registers_test_record_review_flags(self) -> None:
        """Verify `tests.test_record_review_flags` is registered and routed in `selective_test_runner.py`."""
        self.assertEqual(selective_test_runner.audit_router_integrity(REPO_ROOT), [])
        for rel in (
            "skills/agy-code-remediation-pr/scripts/review_gate.py",
            "skills/agy-code-remediation-pr/scripts/lock_store.py",
            "agents/agy-code-pr-reviewer.md",
            "tests/test_record_review_flags.py",
        ):
            mods = selective_test_runner.select_test_modules([rel])
            self.assertIn("tests.test_record_review_flags", mods)


if __name__ == "__main__":
    unittest.main()
