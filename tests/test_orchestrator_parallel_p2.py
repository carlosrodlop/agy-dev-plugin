#!/usr/bin/env python3
"""Hermetic Zero-PII unit tests for Orchestrator Parallelism Phase 2 (Puntos 4+5+6).

Covers:
  1. Release Train `bundle-ready-slots` (2-3 PASS>90 disjoint READY_AWAITING_BASE_MERGE slots -> 1 verified
     integration PR with `test-local --mode ci-parity` gate + atomic rollback on conflict or CI failure,
     preserving max-1-open-PR-per-repo).
  2. Speculative pre-rebase over frozen Slot #1 HEAD (`speculative_pre_rebase_slot`, `run_speculative_pre_rebase`,
     and fast-path cache reuse inside `rebase_and_reverify` on fast-forward & squash merges).
  3. Isolated Feature Test Policy (`audit_isolated_feature_test_policy`), pure-stdlib AST import-graph
     auto-discovery (`build_python_ast_import_graph`, `discover_dependent_test_modules`), and Santa-safe
     `ProcessPoolExecutor` parallel unittest execution with sequential fallback (`run_unittest_targets`).
  4. 100-110 step proactive orchestrator auto-handoff rotation (`dev_flow_optimizer`, `dev_queue_doctor`,
     `orchestrator-tick`), lean 1-call `branch-diff --review-bundle`, and `< 24,000` byte markdown ceilings.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CLIENTS_DIR = REPO_ROOT / "clients"
PR_REM_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
TESTS_SCRIPTS_DIR = REPO_ROOT / "tests" / "scripts"
for _p in (CLIENTS_DIR, PR_REM_DIR, TESTS_SCRIPTS_DIR):
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

import dev_flow_optimizer as dfo  # noqa: E402
import dev_queue_doctor as doc  # noqa: E402
import git_client  # noqa: E402
import pr_remediator as prs  # noqa: E402
import rebase_gate as rgate  # noqa: E402
import review_gate as rvgate  # noqa: E402
import selective_test_runner as srunner  # noqa: E402
import worktree_ops  # noqa: E402


def _git(repo_dir: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run native `git -C <repo_dir>` with clean environment."""
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init_org_bare_and_clone(
    root: pathlib.Path,
    repo_name: str = "acme-plugin",
    org_name: str = "acme-org",
) -> tuple[pathlib.Path, pathlib.Path]:
    """Create a hermetic `file://` bare remote `<org_name>/<repo_name>.git` and local clone on `main`."""
    bare = root / "remotes" / org_name / f"{repo_name}.git"
    clone = root / "repos" / repo_name
    bare.parent.mkdir(parents=True, exist_ok=True)
    clone.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", f"file://{bare}", str(clone)], check=True, capture_output=True, text=True)
    _git(clone, "config", "user.name", "Acme Bot")
    _git(clone, "config", "user.email", "acme-bot@example.com")
    (clone / "README.md").write_text(f"# {repo_name}\n", encoding="utf-8")
    (clone / "calc.py").write_text(
        "def multiply(x: int, factor: int = 2) -> int:\n    return x * factor\n",
        encoding="utf-8",
    )
    (clone / "tests").mkdir(parents=True, exist_ok=True)
    (clone / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (clone / "tests" / "test_calc.py").write_text(
        "import unittest\nimport calc\n\n"
        "class TestCalc(unittest.TestCase):\n"
        "    def test_multiply(self) -> None:\n"
        "        self.assertEqual(calc.multiply(3), 6)\n",
        encoding="utf-8",
    )
    _git(clone, "add", "README.md", "calc.py", "tests/__init__.py", "tests/test_calc.py")
    _git(clone, "commit", "-m", "chore: initial commit")
    _git(clone, "push", "-u", "origin", "main")
    return bare, clone


def _create_branch_with_files(
    clone: pathlib.Path,
    branch_name: str,
    files: dict[str, str],
    *,
    base_ref: str = "main",
    message: str = "feat: branch change",
) -> str:
    """Create `branch_name` from `base_ref`, commit `files`, return HEAD SHA, and switch back to `main`."""
    _git(clone, "checkout", "-b", branch_name, base_ref)
    for rel_path, content in files.items():
        fpath = clone / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")
        _git(clone, "add", rel_path)
    _git(clone, "commit", "-m", message)
    sha = _git(clone, "rev-parse", "HEAD").stdout.strip()
    _git(clone, "checkout", "main")
    return sha


def _write_transcript(brain_dir: pathlib.Path, cid: str, steps: list[dict[str, object]]) -> pathlib.Path:
    tpath = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
    tpath.parent.mkdir(parents=True, exist_ok=True)
    tpath.write_text("\n".join(json.dumps(s) for s in steps) + "\n", encoding="utf-8")
    return tpath


def _verify_ok(_train_wt: pathlib.Path) -> dict[str, object]:
    """Stand-in ci-parity gate that always passes."""
    return {"passed": True, "verification_results": {"overall_passed": True}}


def _acquire_slot_lock(
    pdir: pathlib.Path,
    slug: str,
    clone: pathlib.Path,
    base_sha: str,
    *,
    slot_id: int,
    cid: str,
    branch: str,
    target_file: str,
) -> None:
    """Acquire Slot #1 (or a follower slot via `allow_slot2`) for `branch` on a 3-slot repo lock."""
    prs.acquire_dev_pr_lock(
        conversation_id=cid,
        action_id=f"slot{slot_id}",
        branch_name=branch,
        target_files=[target_file],
        target_repo_dir=str(clone),
        repo_slug=slug,
        base_sha=base_sha,
        allow_slot2=slot_id > 1,
        max_slots=3,
        portfolio_dir=pdir,
    )


def _mark_slot_ready(
    pdir: pathlib.Path,
    slug: str,
    *,
    slot_id: int,
    head_sha: str,
    score: int = 95,
    extra: dict[str, object] | None = None,
) -> None:
    """Mark a slot `READY_AWAITING_BASE_MERGE` with a PASS review bound to `head_sha`."""
    prs.update_dev_pr_lock(
        {
            "status": prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE,
            "head_sha": head_sha,
            "reviewed_head_sha": head_sha,
            "review_approved": True,
            "reviewer_score": score,
            "review_history": [{"verdict": "PASS", "score": score}],
            **(extra or {}),
        },
        portfolio_dir=pdir,
        repo_slug=slug,
        slot_id=slot_id,
    )


class TestReleaseTrainBundleReadySlots(unittest.TestCase):
    """Verify 0-LLM Release Train `bundle-ready-slots` bundling, ci-parity gate, rollback, and PR invariants."""

    def test_bundle_ready_slots_git_combines_disjoint_slots_and_passes_ci_parity_when_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _, clone = _init_org_bare_and_clone(root)
            base_sha = _git(clone, "rev-parse", "main").stdout.strip()

            br2 = "fix/20261128-090100-slot2-aaaa1111"
            br3 = "fix/20261128-090200-slot3-bbbb2222"
            br4 = "fix/20261128-090300-slot4-cccc3333"
            _create_branch_with_files(clone, br2, {"mod_a.py": "VAL_A = 1\n"}, message="feat: mod_a")
            _create_branch_with_files(clone, br3, {"mod_b.py": "VAL_B = 2\n"}, message="feat: mod_b")
            _create_branch_with_files(clone, br4, {"mod_c.py": "VAL_C = 3\n"}, message="feat: mod_c")

            entries = [
                {"slot_id": 2, "conversation_id": "aaaa1111", "branch_name": br2, "base_sha": base_sha},
                {"slot_id": 3, "conversation_id": "bbbb2222", "branch_name": br3, "base_sha": base_sha},
                {"slot_id": 4, "conversation_id": "cccc3333", "branch_name": br4, "base_sha": base_sha},
            ]

            # 1. Dry-run succeeds with zero git mutation
            dry = rgate.bundle_ready_slots_git(clone, entries, dry_run=True)
            self.assertEqual(dry.status, "ok")
            self.assertEqual(dry.outcome, rgate.OUTCOME_TRAIN_DRY_RUN_READY)
            self.assertFalse(dry.bundled)
            self.assertEqual(dry.union_files, ("mod_a.py", "mod_b.py", "mod_c.py"))
            self.assertNotIn(dry.train_branch, _git(clone, "branch", "--list").stdout)

            # 2. Real bundle applies all 3 slots onto train worktree and runs ci-parity verification
            seen_files_in_train: list[str] = []

            def _fake_ci_parity(train_wt: pathlib.Path) -> dict[str, object]:
                for fname in ("mod_a.py", "mod_b.py", "mod_c.py"):
                    if (train_wt / fname).is_file():
                        seen_files_in_train.append(fname)
                return {
                    "passed": True,
                    "verification_results": {
                        "overall_passed": True,
                        "qa_static_audit": {"status": "pass", "passed": True, "errors": 0, "warnings": 0},
                        "unittest": {"status": "pass", "passed": True, "passed_count": 3, "total_count": 3},
                    },
                }

            res = rgate.bundle_ready_slots_git(
                clone,
                entries,
                worktree_base_dir=root / "wt-trains",
                ci_parity_verify_fn=_fake_ci_parity,
            )
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.outcome, rgate.OUTCOME_TRAIN_BUNDLED)
            self.assertTrue(res.bundled)
            self.assertFalse(res.rolled_back)
            self.assertTrue(res.verification_passed)
            self.assertEqual(seen_files_in_train, ["mod_a.py", "mod_b.py", "mod_c.py"])
            self.assertEqual(res.included_branches, (br2, br3, br4))
            self.assertEqual(res.included_slot_ids, (2, 3, 4))
            self.assertTrue(res.train_head_sha)
            train_wt = pathlib.Path(res.train_worktree.replace("$HOME", str(pathlib.Path.home())))
            self.assertTrue(train_wt.is_dir())
            rgate._remove_worktree_cleanly(clone, train_wt)

    def test_bundle_ready_slots_git_rolls_back_cleanly_when_ci_parity_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _, clone = _init_org_bare_and_clone(root)
            base_sha = _git(clone, "rev-parse", "main").stdout.strip()
            br2 = "fix/20261128-090400-s2-dddd4444"
            br3 = "fix/20261128-090500-s3-eeee5555"
            sha2 = _create_branch_with_files(clone, br2, {"f2.py": "X = 2\n"})
            sha3 = _create_branch_with_files(clone, br3, {"f3.py": "Y = 3\n"})

            train_wt = root / "train-fail-wt"
            train_br = "fix/20261128-090600-release-train-fail"
            res = rgate.bundle_ready_slots_git(
                clone,
                [
                    {"slot_id": 2, "conversation_id": "dddd4444", "branch_name": br2, "base_sha": base_sha},
                    {"slot_id": 3, "conversation_id": "eeee5555", "branch_name": br3, "base_sha": base_sha},
                ],
                train_branch=train_br,
                train_worktree_dir=train_wt,
                ci_parity_verify_fn=lambda _wt: {
                    "passed": False,
                    "verification_results": {"overall_passed": False},
                    "failure_details": "AssertionError: coverage below threshold",
                },
            )
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.outcome, rgate.OUTCOME_TRAIN_ROLLED_BACK_VERIFICATION)
            self.assertFalse(res.bundled)
            self.assertTrue(res.rolled_back)
            self.assertTrue(res.worktree_pristine)
            self.assertIn("coverage below threshold", res.failure_details)
            self.assertFalse(train_wt.exists())
            self.assertNotIn(train_br, _git(clone, "branch", "--list").stdout)
            # Original slot branches remain 100% untouched
            self.assertEqual(_git(clone, "rev-parse", br2).stdout.strip(), sha2)
            self.assertEqual(_git(clone, "rev-parse", br3).stdout.strip(), sha3)

    def test_bundle_ready_slots_git_rolls_back_cleanly_when_slots_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _, clone = _init_org_bare_and_clone(root)
            base_sha = _git(clone, "rev-parse", "main").stdout.strip()
            br2 = "fix/20261128-090700-conf-a"
            br3 = "fix/20261128-090800-conf-b"
            _create_branch_with_files(clone, br2, {"calc.py": "def multiply(x: int) -> int:\n    return x * 10\n"})
            _create_branch_with_files(clone, br3, {"calc.py": "def multiply(x: int) -> int:\n    return x * 99\n"})

            res = rgate.bundle_ready_slots_git(
                clone,
                [
                    {"slot_id": 2, "conversation_id": "1111aaaa", "branch_name": br2, "base_sha": base_sha},
                    {"slot_id": 3, "conversation_id": "2222bbbb", "branch_name": br3, "base_sha": base_sha},
                ],
                train_branch="fix/20261128-090900-train-conflict",
                train_worktree_dir=root / "wt-conflict",
            )
            self.assertEqual(res.status, "ok")
            self.assertEqual(res.outcome, rgate.OUTCOME_TRAIN_ROLLED_BACK_CONFLICT)
            self.assertFalse(res.bundled)
            self.assertTrue(res.rolled_back)
            self.assertTrue(res.worktree_pristine)
            self.assertIn("calc.py", res.conflicted_files)
            self.assertFalse((root / "wt-conflict").exists())
            self.assertNotIn("fix/20261128-090900-train-conflict", _git(clone, "branch", "--list").stdout)

    def test_run_bundle_ready_slots_enforces_review_gate_disjointness_and_max_one_open_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pdir = root / "state"
            pdir.mkdir(parents=True, exist_ok=True)
            _, clone = _init_org_bare_and_clone(root)
            base_sha = _git(clone, "rev-parse", "main").stdout.strip()
            slug = "acme-org/acme-plugin"

            br1 = "fix/20261128-091000-slot1-11111111"
            br2 = "fix/20261128-091100-slot2-22222222"
            br3 = "fix/20261128-091200-slot3-33333333"
            sha1 = _create_branch_with_files(clone, br1, {"slot1_only.py": "S1 = 1\n"})
            sha2 = _create_branch_with_files(clone, br2, {"slot2_only.py": "S2 = 2\n"})
            sha3 = _create_branch_with_files(clone, br3, {"slot3_only.py": "S3 = 3\n"})

            # Seed Slot #1 as OPEN pushed PR + Slot #2 & #3 as PASS>90 disjoint READY_AWAITING_BASE_MERGE
            prs.acquire_dev_pr_lock(
                conversation_id="11111111-aaaa",
                action_id="slot1",
                branch_name=br1,
                target_files=["slot1_only.py"],
                target_repo_dir=str(clone),
                repo_slug=slug,
                base_sha=base_sha,
                portfolio_dir=pdir,
                max_slots=3,
            )
            prs.update_dev_pr_lock(
                {
                    "status": "PENDING_HUMAN_PR_VALIDATION",
                    "branch_pushed": True,
                    "pr_gate_ready": True,
                    "head_sha": sha1,
                    "review_approved": True,
                    "reviewer_score": 96,
                    "manual_pr_url": "https://github.com/acme-org/acme-plugin/compare/main...slot1",
                },
                portfolio_dir=pdir,
                repo_slug=slug,
                slot_id=1,
            )
            for sid, cid, br, sha, tf, score in (
                (2, "22222222-bbbb", br2, sha2, "slot2_only.py", 95),
                (3, "33333333-cccc", br3, sha3, "slot3_only.py", 94),
            ):
                prs.acquire_dev_pr_lock(
                    conversation_id=cid,
                    action_id=f"slot{sid}",
                    branch_name=br,
                    target_files=[tf],
                    target_repo_dir=str(clone),
                    repo_slug=slug,
                    base_sha=base_sha,
                    allow_slot2=True,
                    max_slots=3,
                    portfolio_dir=pdir,
                )
                prs.update_dev_pr_lock(
                    {
                        "status": prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE,
                        "head_sha": sha,
                        "review_approved": True,
                        "reviewer_score": score,
                        "review_history": [{"verdict": "PASS", "score": score}],
                    },
                    portfolio_dir=pdir,
                    repo_slug=slug,
                    slot_id=sid,
                )

            # 1. Fail closed when Slot #3 score drops <= 90
            prs.update_dev_pr_lock(
                {"review_approved": False, "reviewer_score": 85, "review_history": [{"verdict": "WARN", "score": 85}]},
                portfolio_dir=pdir,
                repo_slug=slug,
                slot_id=3,
            )
            not_elig = prs.run_bundle_ready_slots(repo_slug=slug, workdir=clone, portfolio_dir=pdir)
            self.assertEqual(not_elig["outcome"], rgate.OUTCOME_TRAIN_NOT_ELIGIBLE)
            self.assertFalse(not_elig["bundled"])

            # Restore Slot #3 PASS 94 -> bundle while Slot #1 has an open PR MUST NOT push (max-1-open-PR invariant)
            prs.update_dev_pr_lock(
                {"review_approved": True, "reviewer_score": 94, "review_history": [{"verdict": "PASS", "score": 94}]},
                portfolio_dir=pdir,
                repo_slug=slug,
                slot_id=3,
            )
            ok_local = prs.run_bundle_ready_slots(
                repo_slug=slug,
                workdir=clone,
                publish=True,
                worktree_base_dir=root / "trains",
                portfolio_dir=pdir,
                ci_parity_verify_fn=lambda _wt: {"passed": True, "verification_results": {"overall_passed": True}},
            )
            self.assertEqual(ok_local["outcome"], rgate.OUTCOME_TRAIN_BUNDLED)
            self.assertTrue(ok_local["bundled"])
            self.assertTrue(ok_local["slot1_open_pr_blocking_publish"])
            self.assertFalse(ok_local["branch_pushed"])
            self.assertEqual(ok_local["manual_pr_url"], "")
            rgate._remove_worktree_cleanly(
                clone,
                pathlib.Path(ok_local["train_worktree"].replace("$HOME", str(pathlib.Path.home()))),
            )

            # 2. When Slot #1 has NO pushed open PR (READY_AWAITING_BASE_MERGE), publish=True pushes single Release Train PR
            prs.update_dev_pr_lock(
                {
                    "status": prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE,
                    "branch_pushed": False,
                    "pr_gate_ready": False,
                    "manual_pr_url": "",
                    "review_history": [{"verdict": "PASS", "score": 96}],
                },
                portfolio_dir=pdir,
                repo_slug=slug,
                slot_id=1,
            )
            vcs = git_client.GitVcsIntegrationClient()
            with mock.patch.object(prs, "spawn_pr_merge_watcher_daemon", return_value={"status": "spawned"}):
                ok_pub = prs.run_bundle_ready_slots(
                    repo_slug=slug,
                    workdir=clone,
                    slot_ids=[1, 2],
                    publish=True,
                    worktree_base_dir=root / "trains-pub",
                    portfolio_dir=pdir,
                    git_client=vcs,
                    ci_parity_verify_fn=lambda _wt: {"passed": True, "verification_results": {"overall_passed": True}},
                )
            self.assertEqual(ok_pub["outcome"], rgate.OUTCOME_TRAIN_BUNDLED)
            self.assertTrue(ok_pub["branch_pushed"])
            self.assertTrue(ok_pub["ready_for_human_pr"])
            self.assertIn(ok_pub["train_branch"], ok_pub["manual_pr_url"])
            updated_lock = prs.get_dev_pr_lock(portfolio_dir=pdir, repo_slug=slug)
            self.assertEqual(updated_lock["status"], "PENDING_HUMAN_PR_VALIDATION")
            self.assertTrue(updated_lock.get("release_train"))
            rgate._remove_worktree_cleanly(
                clone,
                pathlib.Path(ok_pub["train_worktree"].replace("$HOME", str(pathlib.Path.home()))),
            )

    def test_run_bundle_ready_slots_refuses_publish_when_slot1_is_outside_the_train(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pdir = root / "state"
            pdir.mkdir(parents=True, exist_ok=True)
            _, clone = _init_org_bare_and_clone(root)
            base_sha = _git(clone, "rev-parse", "main").stdout.strip()
            slug = "acme-org/acme-plugin"

            br1 = "fix/20261128-101000-slot1-aaaa0001"
            br2 = "fix/20261128-101100-slot2-bbbb0002"
            br3 = "fix/20261128-101200-slot3-cccc0003"
            _create_branch_with_files(clone, br1, {"s1_wip.py": "S1 = 1\n"})
            sha2 = _create_branch_with_files(clone, br2, {"s2_done.py": "S2 = 2\n"})
            sha3 = _create_branch_with_files(clone, br3, {"s3_done.py": "S3 = 3\n"})
            for sid, cid, br, tf in (
                (1, "aaaa0001-1111", br1, "s1_wip.py"),
                (2, "bbbb0002-2222", br2, "s2_done.py"),
                (3, "cccc0003-3333", br3, "s3_done.py"),
            ):
                _acquire_slot_lock(pdir, slug, clone, base_sha, slot_id=sid, cid=cid, branch=br, target_file=tf)
            # Slot #1 is still under agent review (never pushed); only Slots #2 and #3 are approved and ready
            prs.update_dev_pr_lock({"status": "AWAITING_AGENT_REVIEW"}, portfolio_dir=pdir, repo_slug=slug, slot_id=1)
            _mark_slot_ready(pdir, slug, slot_id=2, head_sha=sha2, score=95)
            _mark_slot_ready(pdir, slug, slot_id=3, head_sha=sha3, score=94)

            vcs = mock.MagicMock()
            res = prs.run_bundle_ready_slots(
                repo_slug=slug,
                workdir=clone,
                publish=True,
                worktree_base_dir=root / "trains",
                portfolio_dir=pdir,
                git_client=vcs,
                ci_parity_verify_fn=_verify_ok,
            )
            # The train is verified and staged locally, but publishing it would open a PR that is not Slot #1's
            self.assertEqual(res["outcome"], rgate.OUTCOME_TRAIN_BUNDLED)
            self.assertTrue(res["bundled"])
            self.assertTrue(res["publish_requested"])
            self.assertTrue(res["publish_blocked"])
            self.assertIn("Slot #1", res["publish_blocked_reason"])
            self.assertFalse(res["slot1_open_pr_blocking_publish"])
            self.assertFalse(res["branch_pushed"])
            self.assertFalse(res["ready_for_human_pr"])
            self.assertEqual(res["manual_pr_url"], "")
            vcs.push_branch.assert_not_called()
            self.assertEqual(_git(clone, "ls-remote", "--heads", "origin", res["train_branch"]).stdout.strip(), "")

            # Lock state is untouched: Slot #1 stays active/unpushed and no follower slot becomes the open PR
            top_lock = prs.get_dev_pr_lock(portfolio_dir=pdir, repo_slug=slug)
            self.assertEqual(top_lock["status"], "AWAITING_AGENT_REVIEW")
            self.assertFalse(top_lock.get("branch_pushed"))
            staged_slot = prs.get_dev_pr_lock(portfolio_dir=pdir, repo_slug=slug, slot_id=res["included_slot_ids"][0])
            self.assertEqual(staged_slot["status"], prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE)
            self.assertFalse(staged_slot.get("branch_pushed"))
            self.assertEqual(staged_slot.get("manual_pr_url") or "", "")

            # The staged (non-publish) path keeps the verification gate bound to the amended train HEAD
            live_train_head = _git(clone, "rev-parse", res["train_branch"]).stdout.strip()
            self.assertEqual(res["train_head_sha"], live_train_head)
            self.assertEqual(res["verification_results"]["head_sha"], live_train_head)
            self.assertEqual(staged_slot["release_train_bundle"]["train_head_sha"], live_train_head)
            self.assertIn("Slot #1", staged_slot["release_train_bundle"]["publish_blocked_reason"])
            rgate._remove_worktree_cleanly(
                clone,
                pathlib.Path(res["train_worktree"].replace("$HOME", str(pathlib.Path.home()))),
            )

    def test_run_bundle_ready_slots_rejects_stale_approval_and_commits_added_after_review(self) -> None:
        # The latest verdict is authoritative even when a stale `review_approved=True` survives on the lock
        stale = {"review_approved": True, "reviewer_score": 95}
        self.assertFalse(
            rvgate._slot_passed_independent_review(
                {**stale, "review_history": [{"verdict": "PASS", "score": 95}, {"verdict": "NEEDS_REVISION", "score": 80}]}
            )
        )
        self.assertFalse(rvgate._slot_passed_independent_review({**stale, "review_history": [{"verdict": "WARN", "score": 95}]}))
        self.assertTrue(rvgate._slot_passed_independent_review({**stale, "review_history": [{"verdict": "PASS", "score": 95}]}))
        self.assertTrue(rvgate._slot_passed_independent_review(stale))

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pdir = root / "state"
            pdir.mkdir(parents=True, exist_ok=True)
            _, clone = _init_org_bare_and_clone(root)
            base_sha = _git(clone, "rev-parse", "main").stdout.strip()
            slug = "acme-org/acme-plugin"

            br1 = "fix/20261128-102000-slot1-dddd0001"
            br2 = "fix/20261128-102100-slot2-eeee0002"
            br3 = "fix/20261128-102200-slot3-ffff0003"
            sha1 = _create_branch_with_files(clone, br1, {"g1.py": "G1 = 1\n"})
            sha2 = _create_branch_with_files(clone, br2, {"g2.py": "G2 = 2\n"})
            sha3 = _create_branch_with_files(clone, br3, {"g3.py": "G3 = 3\n"})
            for sid, cid, br, tf in (
                (1, "dddd0001-1111", br1, "g1.py"),
                (2, "eeee0002-2222", br2, "g2.py"),
                (3, "ffff0003-3333", br3, "g3.py"),
            ):
                _acquire_slot_lock(pdir, slug, clone, base_sha, slot_id=sid, cid=cid, branch=br, target_file=tf)
            prs.update_dev_pr_lock(
                {"status": "PENDING_HUMAN_PR_VALIDATION", "branch_pushed": True, "pr_gate_ready": True, "head_sha": sha1},
                portfolio_dir=pdir,
                repo_slug=slug,
                slot_id=1,
            )
            _mark_slot_ready(pdir, slug, slot_id=2, head_sha=sha2, score=95)
            _mark_slot_ready(pdir, slug, slot_id=3, head_sha=sha3, score=94)

            # 1. Slot #3 received NEEDS_REVISION after an earlier PASS, but `review_approved=True` was never cleared
            prs.update_dev_pr_lock(
                {"review_history": [{"verdict": "PASS", "score": 94}, {"verdict": "NEEDS_REVISION", "score": 80}]},
                portfolio_dir=pdir,
                repo_slug=slug,
                slot_id=3,
            )
            stale_res = prs.run_bundle_ready_slots(
                repo_slug=slug, workdir=clone, portfolio_dir=pdir, ci_parity_verify_fn=_verify_ok
            )
            self.assertEqual(stale_res["outcome"], rgate.OUTCOME_TRAIN_NOT_ELIGIBLE)
            self.assertFalse(stale_res["bundled"])
            stale_reasons = {r["slot_id"]: r["reason"] for r in stale_res["ineligible_reasons"]}
            self.assertIn("verdict=NEEDS_REVISION", stale_reasons[3])

            # 2. Slot #3 passes review again, then a commit lands on its branch after the reviewed HEAD
            _mark_slot_ready(pdir, slug, slot_id=3, head_sha=sha3, score=94)
            _git(clone, "checkout", br3)
            (clone / "g3_unreviewed.py").write_text("SNEAKED_IN = True\n", encoding="utf-8")
            _git(clone, "add", "g3_unreviewed.py")
            _git(clone, "commit", "-m", "feat: change added after review")
            _git(clone, "checkout", "main")
            moved_res = prs.run_bundle_ready_slots(
                repo_slug=slug, workdir=clone, portfolio_dir=pdir, ci_parity_verify_fn=_verify_ok
            )
            self.assertEqual(moved_res["outcome"], rgate.OUTCOME_TRAIN_NOT_ELIGIBLE)
            self.assertFalse(moved_res["bundled"])
            moved_reasons = {r["slot_id"]: r["reason"] for r in moved_res["ineligible_reasons"]}
            self.assertIn("reviewed HEAD parity failed", moved_reasons[3])
            self.assertNotIn(2, moved_reasons)
            self.assertNotIn("release-train", _git(clone, "branch", "--list").stdout)

            # 3. The git layer pins the reviewed HEAD as well (closes the check-then-bundle window)
            pinned = rgate.bundle_ready_slots_git(
                clone,
                [
                    {"slot_id": 2, "branch_name": br2, "expected_head_sha": sha2},
                    {"slot_id": 3, "branch_name": br3, "expected_head_sha": sha3},
                ],
                dry_run=True,
            )
            self.assertEqual(pinned.outcome, rgate.OUTCOME_TRAIN_NOT_ELIGIBLE)
            self.assertIn("moved since review", pinned.error)

    def test_run_bundle_ready_slots_publish_discards_train_on_push_failure_and_unions_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pdir = root / "state"
            pdir.mkdir(parents=True, exist_ok=True)
            _, clone = _init_org_bare_and_clone(root)
            base_sha = _git(clone, "rev-parse", "main").stdout.strip()
            slug = "acme-org/acme-plugin"

            br1 = "fix/20261128-103000-slot1-abab0001"
            br2 = "fix/20261128-103100-slot2-cdcd0002"
            sha1 = _create_branch_with_files(clone, br1, {"p1.py": "P1 = 1\n"})
            sha2 = _create_branch_with_files(clone, br2, {"p2.py": "P2 = 2\n"})
            _acquire_slot_lock(pdir, slug, clone, base_sha, slot_id=1, cid="abab0001-1111", branch=br1, target_file="p1.py")
            _acquire_slot_lock(pdir, slug, clone, base_sha, slot_id=2, cid="cdcd0002-2222", branch=br2, target_file="p2.py")
            _mark_slot_ready(
                pdir, slug, slot_id=1, head_sha=sha1, score=96, extra={"sibling_conversation_ids": ["s1helper-0001"]}
            )
            _mark_slot_ready(
                pdir, slug, slot_id=2, head_sha=sha2, score=95, extra={"sibling_conversation_ids": ["s2helper-0002"]}
            )

            # 1. A failed push must not leave the unpublished train worktree/branch behind or touch the locks
            failing_vcs = mock.MagicMock()
            failing_vcs.push_branch.return_value = {"status": "error", "error": "remote rejected (hook declined)"}
            fail_branch = "fix/20261128-103500-release-train-pushfail"
            fail_wt = root / "train-pushfail"
            fail_res = prs.run_bundle_ready_slots(
                repo_slug=slug,
                workdir=clone,
                publish=True,
                train_branch=fail_branch,
                train_worktree_dir=fail_wt,
                portfolio_dir=pdir,
                git_client=failing_vcs,
                ci_parity_verify_fn=_verify_ok,
            )
            self.assertEqual(fail_res["status"], "error")
            self.assertEqual(fail_res["step"], "push_release_train")
            self.assertIn("remote rejected", fail_res["error"])
            self.assertTrue(fail_res["rolled_back"])
            self.assertFalse(fail_res["bundled"])
            self.assertFalse(fail_res["branch_pushed"])
            failing_vcs.push_branch.assert_called_once()
            self.assertFalse(fail_wt.exists())
            self.assertNotIn(fail_branch, _git(clone, "branch", "--list").stdout)
            self.assertEqual(
                prs.get_dev_pr_lock(portfolio_dir=pdir, repo_slug=slug)["status"],
                prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE,
            )
            self.assertEqual(_git(clone, "rev-parse", br1).stdout.strip(), sha1)
            self.assertEqual(_git(clone, "rev-parse", br2).stdout.strip(), sha2)

            # 2. A train led by Slot #1 publishes, keeping siblings already tracked by both slots
            with mock.patch.object(prs, "spawn_pr_merge_watcher_daemon", return_value={"status": "spawned"}):
                ok_pub = prs.run_bundle_ready_slots(
                    repo_slug=slug,
                    workdir=clone,
                    publish=True,
                    worktree_base_dir=root / "trains-pub",
                    portfolio_dir=pdir,
                    git_client=git_client.GitVcsIntegrationClient(),
                    ci_parity_verify_fn=_verify_ok,
                )
            self.assertEqual(ok_pub["outcome"], rgate.OUTCOME_TRAIN_BUNDLED)
            self.assertTrue(ok_pub["branch_pushed"])
            self.assertFalse(ok_pub["publish_blocked"])
            self.assertEqual(ok_pub["verification_results"]["head_sha"], ok_pub["train_head_sha"])
            published = prs.get_dev_pr_lock(portfolio_dir=pdir, repo_slug=slug)
            self.assertEqual(published["status"], "PENDING_HUMAN_PR_VALIDATION")
            self.assertEqual(published["conversation_id"], "abab0001-1111")
            for sibling in ("s1helper-0001", "cdcd0002-2222", "s2helper-0002"):
                self.assertIn(sibling, published["sibling_conversation_ids"])
            self.assertNotIn("abab0001-1111", published["sibling_conversation_ids"])
            rgate._remove_worktree_cleanly(
                clone,
                pathlib.Path(ok_pub["train_worktree"].replace("$HOME", str(pathlib.Path.home()))),
            )

    def test_bundle_ready_slots_git_uses_live_merge_base_for_rebased_and_recorded_base_for_stacked_slots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _, clone = _init_org_bare_and_clone(root)
            base_v1 = _git(clone, "rev-parse", "main").stdout.strip()

            br_rebased = "fix/20261128-104000-rebased-slot"
            _create_branch_with_files(clone, br_rebased, {"mod_r.py": "R = 1\n"}, message="feat: mod_r")
            # `main` advances to v2 and the slot is rebased onto it, but its lock still records `base_sha=v1`
            (clone / "main_v2.py").write_text("V2 = True\n", encoding="utf-8")
            _git(clone, "add", "main_v2.py")
            _git(clone, "commit", "-m", "feat: main v2")
            _git(clone, "push", "origin", "main")
            base_v2 = _git(clone, "rev-parse", "main").stdout.strip()
            _git(clone, "rebase", "main", br_rebased)
            _git(clone, "checkout", "main")
            br_fresh = "fix/20261128-104100-fresh-slot"
            _create_branch_with_files(clone, br_fresh, {"mod_s.py": "S = 2\n"}, message="feat: mod_s")

            rebased_res = rgate.bundle_ready_slots_git(
                clone,
                [
                    {"slot_id": 2, "conversation_id": "aaaa2222", "branch_name": br_rebased, "base_sha": base_v1},
                    {"slot_id": 3, "conversation_id": "bbbb3333", "branch_name": br_fresh, "base_sha": base_v2},
                ],
                train_branch="fix/20261128-104200-release-train-rebased",
                worktree_base_dir=root / "trains",
                ci_parity_verify_fn=_verify_ok,
            )
            # A stale recorded base must not replay `main v2` (empty cherry-pick -> rollback) onto the train
            self.assertEqual(rebased_res.outcome, rgate.OUTCOME_TRAIN_BUNDLED)
            self.assertTrue(rebased_res.bundled)
            self.assertEqual(set(rebased_res.union_files), {"mod_r.py", "mod_s.py"})
            self.assertEqual(
                _git(clone, "rev-list", "--count", f"{base_v2}..{rebased_res.train_head_sha}").stdout.strip(), "2"
            )
            rgate.discard_release_train(
                clone, train_branch=rebased_res.train_branch, train_worktree=rebased_res.train_worktree
            )
            self.assertNotIn("release-train-rebased", _git(clone, "branch", "--list").stdout)

            # A stacked slot keeps its recorded base (its parent's HEAD) so the parent's commit is not replayed twice
            parent_head = _git(clone, "rev-parse", br_rebased).stdout.strip()
            br_stacked = "fix/20261128-104300-stacked-slot"
            _create_branch_with_files(clone, br_stacked, {"mod_t.py": "T = 3\n"}, base_ref=br_rebased, message="feat: mod_t")
            stacked_res = rgate.bundle_ready_slots_git(
                clone,
                [
                    {"slot_id": 2, "conversation_id": "aaaa2222", "branch_name": br_rebased, "base_sha": base_v1},
                    {"slot_id": 3, "conversation_id": "cccc4444", "branch_name": br_stacked, "base_sha": parent_head},
                ],
                train_branch="fix/20261128-104400-release-train-stacked",
                worktree_base_dir=root / "trains",
                ci_parity_verify_fn=_verify_ok,
            )
            self.assertEqual(stacked_res.outcome, rgate.OUTCOME_TRAIN_BUNDLED)
            self.assertEqual(set(stacked_res.union_files), {"mod_r.py", "mod_t.py"})
            self.assertEqual(
                _git(clone, "rev-list", "--count", f"{base_v2}..{stacked_res.train_head_sha}").stdout.strip(), "2"
            )
            rgate.discard_release_train(
                clone, train_branch=stacked_res.train_branch, train_worktree=stacked_res.train_worktree
            )

    def test_train_ci_parity_gate_fails_closed_when_the_gate_itself_crashes(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(worktree_ops, "run_local_validation_suite", side_effect=RuntimeError("gate exploded")),
            mock.patch.object(rgate, "_run_default_union_verification") as union_spy,
        ):
            crashed = rgate._run_train_ci_parity_verification(pathlib.Path(tmp))
        # A crashing ci-parity gate must fail the train, never downgrade to the weaker union verification
        self.assertFalse(crashed["passed"])
        self.assertFalse(crashed["verification_results"]["overall_passed"])
        self.assertIn("gate exploded", crashed["failure_details"])
        union_spy.assert_not_called()

    def test_phase2_cli_parsers_and_standalone_rebase_gate_cli(self) -> None:
        self.assertTrue(prs.parse_args(["branch-diff", "--review-bundle"]).review_bundle)
        self.assertEqual(prs.parse_args(["test-local", "--mode", "ci-parity"]).test_mode, "ci-parity")
        self.assertTrue(prs.parse_args(["rebase-reverify", "--speculative"]).speculative)
        self.assertEqual(prs.parse_args(["speculative-rebase", "--branch", "fix/demo"]).subcommand, "speculative-rebase")
        bun_args = prs.parse_args(["bundle-ready-slots", "--repo", "acme-org/acme-plugin", "--dry-run", "--speculative"])
        self.assertEqual(bun_args.subcommand, "bundle-ready-slots")
        self.assertTrue(bun_args.dry_run)
        self.assertTrue(bun_args.speculative)

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _, clone = _init_org_bare_and_clone(root)
            br2 = "fix/20261128-092000-cli-s2"
            br3 = "fix/20261128-092100-cli-s3"
            _create_branch_with_files(clone, br2, {"cli_a.py": "A = 1\n"})
            _create_branch_with_files(clone, br3, {"cli_b.py": "B = 2\n"})
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = rgate.main(["bundle-ready-slots", "--workdir", str(clone), "--branches", br2, br3, "--dry-run"])
            self.assertEqual(rc, 0)
            payload = json.loads(buf.getvalue())
            self.assertEqual(payload["outcome"], rgate.OUTCOME_TRAIN_DRY_RUN_READY)


class TestSpeculativePreRebase(unittest.TestCase):
    """Verify speculative pre-rebase over frozen Slot #1 HEAD and promotion cache reuse."""

    def test_speculative_pre_rebase_reuses_verified_cache_on_fast_forward_and_squash_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bare, clone = _init_org_bare_and_clone(root)
            base_v1 = _git(clone, "rev-parse", "main").stdout.strip()

            br1 = "fix/20261128-093000-slot1-aaaa1111"
            br2 = "fix/20261128-093100-slot2-bbbb2222"
            br3 = "fix/20261128-093200-slot3-cccc3333"
            sha1 = _create_branch_with_files(clone, br1, {"slot1_feat.py": "S1 = 10\n"})
            sha2 = _create_branch_with_files(clone, br2, {"slot2_feat.py": "S2 = 20\n"})
            sha3 = _create_branch_with_files(clone, br3, {"slot3_feat.py": "S3 = 30\n"})

            spec2 = rgate.speculative_pre_rebase_slot(
                clone,
                follower_branch=br2,
                slot1_head_sha=sha1,
                verify=True,
                verify_fn=lambda _wt, _files: {"passed": True, "verification_results": {"overall_passed": True}},
            )
            self.assertEqual(spec2.status, "ok")
            self.assertEqual(spec2.outcome, rgate.OUTCOME_SPECULATIVE_READY)
            self.assertTrue(spec2.verification_passed)
            self.assertTrue(spec2.speculative_ref.startswith(rgate.SPECULATIVE_REF_PREFIX))
            # Original follower branch HEAD MUST remain untouched before Slot #1 merges
            self.assertEqual(_git(clone, "rev-parse", br2).stdout.strip(), sha2)

            spec3 = rgate.speculative_pre_rebase_slot(
                clone,
                follower_branch=br3,
                slot1_head_sha=sha1,
                verify=True,
                verify_fn=lambda _wt, _files: {"passed": True, "verification_results": {"overall_passed": True}},
            )
            self.assertEqual(spec3.outcome, rgate.OUTCOME_SPECULATIVE_READY)

            # Fast-path 1: Slot #1 fast-forwards onto origin/main -> rebase_and_reverify hits cache (0 verify calls)
            _git(clone, "checkout", "main")
            _git(clone, "merge", "--ff-only", br1)
            _git(clone, "push", "origin", "main")
            spy_verify_ff = mock.MagicMock(return_value={"passed": True, "verification_results": {}})
            promo_ff = rgate.rebase_and_reverify(
                clone,
                branch_name=br2,
                recorded_base_sha=base_v1,
                fetch=True,
                verify_fn=spy_verify_ff,
                speculative_cache=spec2.to_dict(),
            )
            self.assertEqual(promo_ff.outcome, rgate.OUTCOME_AUTO_PROMOTED)
            self.assertTrue(promo_ff.speculative_cache_hit)
            self.assertEqual(promo_ff.new_head_sha, spec2.speculative_head_sha)
            spy_verify_ff.assert_not_called()

            # Fast-path 2: Squash/amended Slot #1 commit on origin/main with DIFFERENT SHA but IDENTICAL tree
            _git(clone, "checkout", "main")
            _git(clone, "commit", "--amend", "-m", "feat: squashed slot1 merge")
            _git(clone, "push", "--force", "origin", "main")
            self.assertNotEqual(_git(clone, "rev-parse", "origin/main").stdout.strip(), sha1)
            self.assertEqual(_git(clone, "rev-parse", br3).stdout.strip(), sha3)

            spy_verify_sq = mock.MagicMock(return_value={"passed": True, "verification_results": {}})
            promo_sq = rgate.rebase_and_reverify(
                clone,
                branch_name=br3,
                recorded_base_sha=base_v1,
                fetch=True,
                verify_fn=spy_verify_sq,
                speculative_cache=spec3.to_dict(),
            )
            self.assertEqual(promo_sq.outcome, rgate.OUTCOME_AUTO_PROMOTED)
            self.assertTrue(promo_sq.speculative_cache_hit)
            spy_verify_sq.assert_not_called()

    def test_speculative_pre_rebase_detects_conflict_and_semantic_failure_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _, clone = _init_org_bare_and_clone(root)

            br1 = "fix/20261128-093500-s1-conflict"
            br2 = "fix/20261128-093600-s2-conflict"
            br3 = "fix/20261128-093700-s3-semantic"
            sha1 = _create_branch_with_files(clone, br1, {"calc.py": "def multiply(x: int, factor: int = 10) -> int:\n    return x * factor\n"})
            sha2 = _create_branch_with_files(clone, br2, {"calc.py": "def multiply(x: int, factor: int = 99) -> int:\n    return x * factor\n"})
            sha3 = _create_branch_with_files(
                clone,
                br3,
                {
                    "consumer.py": "import calc\ndef six() -> int:\n    return calc.multiply(3)\n",
                    "tests/test_consumer.py": (
                        "import unittest\nimport consumer\n"
                        "class T(unittest.TestCase):\n"
                        "    def test_six(self) -> None:\n"
                        "        self.assertEqual(consumer.six(), 6)\n"
                    ),
                },
            )

            # 1. Textual conflict detected without mutating br2
            conf = rgate.speculative_pre_rebase_slot(clone, follower_branch=br2, slot1_head_sha=sha1)
            self.assertEqual(conf.outcome, rgate.OUTCOME_SPECULATIVE_CONFLICT)
            self.assertFalse(conf.clean)
            self.assertTrue(conf.aborted_cleanly)
            self.assertIn("calc.py", conf.conflicted_files)
            self.assertEqual(_git(clone, "rev-parse", br2).stdout.strip(), sha2)

            # 2. Semantic break detected (clean rebase, failing test) -> no speculative ref persisted
            sem = rgate.speculative_pre_rebase_slot(clone, follower_branch=br3, slot1_head_sha=sha1, verify=True)
            self.assertEqual(sem.outcome, rgate.OUTCOME_SPECULATIVE_SEMANTIC_FAILURE)
            self.assertFalse(sem.verification_passed)
            self.assertEqual(sem.speculative_ref, "")
            self.assertEqual(_git(clone, "rev-parse", br3).stdout.strip(), sha3)

    def test_run_speculative_pre_rebase_persists_cache_on_follower_slot_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pdir = root / "state"
            pdir.mkdir(parents=True, exist_ok=True)
            _, clone = _init_org_bare_and_clone(root)
            base_sha = _git(clone, "rev-parse", "main").stdout.strip()
            slug = "acme-org/acme-plugin"

            br1 = "fix/20261128-094000-s1-frozen"
            br2 = "fix/20261128-094100-s2-ready"
            sha1 = _create_branch_with_files(clone, br1, {"alpha.py": "ALPHA = 1\n"})
            sha2 = _create_branch_with_files(clone, br2, {"beta.py": "BETA = 2\n"})

            prs.acquire_dev_pr_lock(
                conversation_id="1111aaaa-0001",
                branch_name=br1,
                target_files=["alpha.py"],
                target_repo_dir=str(clone),
                repo_slug=slug,
                base_sha=base_sha,
                portfolio_dir=pdir,
                max_slots=3,
            )
            prs.update_dev_pr_lock(
                {"status": "PENDING_HUMAN_PR_VALIDATION", "head_sha": sha1, "pr_gate_ready": True, "branch_pushed": True},
                portfolio_dir=pdir,
                repo_slug=slug,
                slot_id=1,
            )
            prs.acquire_dev_pr_lock(
                conversation_id="2222bbbb-0002",
                branch_name=br2,
                target_files=["beta.py"],
                target_repo_dir=str(clone),
                repo_slug=slug,
                base_sha=base_sha,
                allow_slot2=True,
                portfolio_dir=pdir,
                max_slots=3,
            )
            prs.update_dev_pr_lock(
                {"status": prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE, "head_sha": sha2, "review_approved": True, "reviewer_score": 95},
                portfolio_dir=pdir,
                repo_slug=slug,
                slot_id=2,
            )

            res = prs.run_speculative_pre_rebase(
                repo_slug=slug,
                workdir=clone,
                portfolio_dir=pdir,
                verify_fn=lambda _wt, _files: {"passed": True, "verification_results": {"overall_passed": True}},
            )
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["outcome"], rgate.OUTCOME_SPECULATIVE_READY)
            self.assertTrue(res["all_ready"])
            slot2_lk = prs.get_dev_pr_lock(portfolio_dir=pdir, repo_slug=slug, slot_id=2)
            cache = slot2_lk.get("speculative_pre_rebase") or {}
            self.assertEqual(cache.get("slot1_head_sha"), sha1)
            self.assertEqual(cache.get("follower_head_sha"), sha2)
            self.assertTrue(cache.get("verification_passed"))

    def test_unverified_or_restamped_speculative_results_never_satisfy_the_promotion_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _, clone = _init_org_bare_and_clone(root)
            base_v1 = _git(clone, "rev-parse", "main").stdout.strip()

            br1 = "fix/20261128-095100-slot1-1a1a1a1a"
            br2 = "fix/20261128-095200-slot2-2b2b2b2b"
            br3 = "fix/20261128-095300-slot3-3c3c3c3c"
            br_on_slot1 = "fix/20261128-095400-stacked-4d4d4d4d"
            sha1 = _create_branch_with_files(clone, br1, {"slot1_feat.py": "S1 = 10\n"})
            _create_branch_with_files(clone, br2, {"slot2_feat.py": "S2 = 20\n"})
            _create_branch_with_files(clone, br3, {"slot3_feat.py": "S3 = 30\n"})
            _create_branch_with_files(clone, br_on_slot1, {"stacked_feat.py": "ST = 40\n"}, base_ref=br1)

            # 1. `verify=False` reports a clean rebase but never claims a passed verification
            unverified = rgate.speculative_pre_rebase_slot(clone, follower_branch=br2, slot1_head_sha=sha1, verify=False)
            self.assertEqual(unverified.outcome, rgate.OUTCOME_SPECULATIVE_READY)
            self.assertTrue(unverified.rebased)
            self.assertFalse(unverified.verification_passed)

            # 2. `SPECULATIVE_SKIPPED` (follower already contains Slot #1 HEAD) verified nothing either
            skipped = rgate.speculative_pre_rebase_slot(clone, follower_branch=br_on_slot1, slot1_head_sha=sha1)
            self.assertEqual(skipped.outcome, rgate.OUTCOME_SPECULATIVE_SKIPPED)
            self.assertFalse(skipped.verification_passed)

            verified = rgate.speculative_pre_rebase_slot(
                clone,
                follower_branch=br3,
                slot1_head_sha=sha1,
                verify=True,
                verify_fn=lambda _wt, _files: {"passed": True, "verification_results": {"overall_passed": True}},
            )
            self.assertTrue(verified.verification_passed)

            _git(clone, "checkout", "main")
            _git(clone, "merge", "--ff-only", br1)
            _git(clone, "push", "origin", "main")

            # 3. Promotion with the unverified cache runs the real verification instead of trusting the cache
            spy_unverified = mock.MagicMock(return_value={"passed": True, "verification_results": {}})
            promo_unverified = rgate.rebase_and_reverify(
                clone,
                branch_name=br2,
                recorded_base_sha=base_v1,
                fetch=True,
                verify_fn=spy_unverified,
                speculative_cache=unverified.to_dict(),
            )
            self.assertEqual(promo_unverified.outcome, rgate.OUTCOME_AUTO_PROMOTED)
            self.assertFalse(promo_unverified.speculative_cache_hit)
            spy_unverified.assert_called_once()

            # 4. A `restamp_fn` that amends the pre-verified commit invalidates the fast-path cache hit
            def _restamp_manifest(wt_dir: pathlib.Path, _files: list[str]) -> None:
                (wt_dir / "VERSION").write_text("restamped\n", encoding="utf-8")
                _git(wt_dir, "add", "VERSION")
                _git(wt_dir, "commit", "--amend", "--no-edit")

            spy_restamped = mock.MagicMock(return_value={"passed": True, "verification_results": {}})
            promo_restamped = rgate.rebase_and_reverify(
                clone,
                branch_name=br3,
                recorded_base_sha=base_v1,
                fetch=True,
                verify_fn=spy_restamped,
                restamp_fn=_restamp_manifest,
                speculative_cache=verified.to_dict(),
            )
            self.assertEqual(promo_restamped.outcome, rgate.OUTCOME_AUTO_PROMOTED)
            self.assertFalse(promo_restamped.speculative_cache_hit)
            spy_restamped.assert_called_once()
            self.assertNotEqual(promo_restamped.new_head_sha, verified.speculative_head_sha)
            self.assertTrue((clone / "VERSION").is_file())

            # 5. Promoted followers consumed their pinned speculative commits: no stale refs remain
            self.assertEqual(_git(clone, "for-each-ref", rgate.SPECULATIVE_REF_PREFIX).stdout.strip(), "")


class TestIsolatedFeatureTestPolicyAndAstParallelRunner(unittest.TestCase):
    """Verify isolated feature test policy, AST import-graph auto-discovery, and ProcessPoolExecutor parallel runner."""

    def test_isolated_feature_test_policy_warns_on_monolith_only_edits_and_passes_on_isolated_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / "tests").mkdir(parents=True)
            (repo / "tests" / "test_monolith.py").write_text("# line\n" * 1600, encoding="utf-8")
            (repo / "tests" / "test_feature_abcd1234.py").write_text("# isolated\n" * 40, encoding="utf-8")

            warn_res = prs.audit_isolated_feature_test_policy(
                ["skills/core/engine.py", "tests/test_monolith.py"],
                repo_dir=repo,
                slot_id=2,
                conversation_id="abcd1234-5555",
                action_id="engine-upgrade",
            )
            self.assertFalse(warn_res["compliant"])
            self.assertTrue(warn_res["advisory_triggered"])
            self.assertEqual(warn_res["suggested_test_file"], "tests/test_engine_upgrade_abcd1234.py")
            self.assertTrue(warn_res["warnings"])

            ok_res = prs.audit_isolated_feature_test_policy(
                ["skills/core/engine.py", "tests/test_feature_abcd1234.py"],
                repo_dir=repo,
                slot_id=2,
                conversation_id="abcd1234-5555",
                action_id="engine-upgrade",
            )
            self.assertTrue(ok_res["compliant"])
            self.assertFalse(ok_res["advisory_triggered"])
            self.assertEqual(ok_res["warnings"], [])

    def test_ast_import_graph_discovers_direct_and_transitive_dependent_tests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / "pkg").mkdir(parents=True)
            (repo / "tests").mkdir(parents=True)
            (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (repo / "pkg" / "core_math.py").write_text("def double(x: int) -> int:\n    return x * 2\n", encoding="utf-8")
            (repo / "pkg" / "service_layer.py").write_text(
                "from pkg.core_math import double\ndef run(x: int) -> int:\n    return double(x) + 1\n",
                encoding="utf-8",
            )
            (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (repo / "tests" / "test_service_a1b2c3d4.py").write_text(
                "import unittest\nimport service_layer\n"
                "class T(unittest.TestCase):\n    def test_run(self) -> None:\n        self.assertEqual(service_layer.run(2), 5)\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_unrelated_99999999.py").write_text(
                "import unittest\nclass U(unittest.TestCase):\n    def test_ok(self) -> None:\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )

            discovered = srunner.discover_dependent_test_modules(["pkg/core_math.py"], repo_root=repo)
            self.assertIn("tests.test_service_a1b2c3d4", discovered)
            self.assertNotIn("tests.test_unrelated_99999999", discovered)

            # Isolated new test file changed alone returns only itself without pulling monolith
            iso_selected = srunner.select_test_modules(
                ["tests/test_service_a1b2c3d4.py"],
                repo_root=repo,
                use_ast_discovery=True,
            )
            self.assertEqual(iso_selected, ["tests.test_service_a1b2c3d4"])

            # Real repo router integrity & AST discovery on rebase_gate.py
            self.assertEqual(srunner.audit_router_integrity(REPO_ROOT), [])
            real_selected = srunner.select_test_modules(["skills/agy-code-remediation-pr/scripts/rebase_gate.py"])
            self.assertIn("tests.test_orchestrator_parallel_p2", real_selected)
            self.assertIn("tests.test_parallel_subagent_orchestration", real_selected)

    def test_process_pool_parallel_unittest_runs_modules_and_falls_back_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            (repo / "tests").mkdir(parents=True)
            (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (repo / "tests" / "test_alpha.py").write_text(
                "import unittest\nclass TestAlpha(unittest.TestCase):\n"
                "    def test_one(self) -> None:\n        self.assertEqual(1 + 1, 2)\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_beta.py").write_text(
                "import unittest\nclass TestBeta(unittest.TestCase):\n"
                "    def test_two(self) -> None:\n        self.assertEqual(2 + 2, 4)\n",
                encoding="utf-8",
            )
            (repo / "tests" / "test_gamma_fail.py").write_text(
                "import unittest\nclass TestGammaFail(unittest.TestCase):\n"
                "    def test_boom(self) -> None:\n        self.assertEqual(1, 2)\n",
                encoding="utf-8",
            )

            # This module itself runs inside a pool worker (nested-worker marker set) under the parallel selective
            # runner: start every scenario below from a top-level environment so each one controls the marker.
            top_level_env = mock.patch.dict(os.environ)
            top_level_env.start()
            self.addCleanup(top_level_env.stop)
            os.environ.pop(srunner._ENV_IN_PARALLEL_WORKER, None)

            # 0. Mocked `subprocess.run` stays in-process and sequential (pool workers would bypass the mock)
            mocked_notice = io.StringIO()
            with (
                mock.patch.object(srunner.subprocess, "run", wraps=subprocess.run) as run_mock,
                contextlib.redirect_stderr(mocked_notice),
            ):
                mocked_res = srunner.run_unittest_targets(
                    ["tests.test_alpha", "tests.test_beta"],
                    repo_dir=repo,
                    workers=2,
                )
            self.assertEqual(mocked_res["returncode"], 0)
            self.assertEqual(mocked_res["mode"], "sequential")
            self.assertEqual(mocked_res["fallback_reason"], "subprocess_run_mocked")
            self.assertIn("subprocess_run_mocked", mocked_notice.getvalue())
            run_mock.assert_called_once()
            self.assertGreater(run_mock.call_args.kwargs["timeout"], 0)

            # 0a. A non-mock passthrough wrapper (e.g. a hermetic guard) also stays sequential, but never silently
            real_run = subprocess.run

            def _passthrough_guard(*args: object, **kwargs: object) -> object:
                return real_run(*args, **kwargs)  # type: ignore[call-overload]

            with (
                mock.patch.object(srunner.subprocess, "run", _passthrough_guard),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                wrapped_res = srunner.run_unittest_targets(
                    ["tests.test_alpha", "tests.test_beta"],
                    repo_dir=repo,
                    workers=2,
                )
            self.assertEqual(wrapped_res["returncode"], 0)
            self.assertEqual(wrapped_res["mode"], "sequential")
            self.assertTrue(wrapped_res["fallback_reason"].startswith("subprocess_run_wrapped:"))
            self.assertIn("_passthrough_guard", wrapped_res["fallback_reason"])

            # Earlier test modules (or an installed companion plugin's hermetic guard) can leave a passthrough
            # wrapper on `subprocess.run` in this process. Pin the patch-detection predicate so the pool path
            # below is exercised deterministically regardless of suite ordering.
            unpatched_run = mock.patch.object(srunner, "_subprocess_run_patch_reason", return_value="")
            unpatched_run.start()
            self.addCleanup(unpatched_run.stop)

            # 0b. Nested invocation inside a pool worker stays sequential (no recursive process pools)
            with (
                mock.patch.dict(os.environ, {srunner._ENV_IN_PARALLEL_WORKER: "1"}),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                nested_res = srunner.run_unittest_targets(
                    ["tests.test_alpha", "tests.test_beta"],
                    repo_dir=repo,
                    workers=2,
                )
            self.assertEqual(nested_res["returncode"], 0)
            self.assertEqual(nested_res["mode"], "sequential")
            self.assertEqual(nested_res["fallback_reason"], "nested_parallel_worker")

            # 1. Parallel execution via stdlib ProcessPoolExecutor
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                par_res = srunner.run_unittest_targets(
                    ["tests.test_alpha", "tests.test_beta"],
                    repo_dir=repo,
                    workers=2,
                )
            self.assertEqual(par_res["returncode"], 0)
            self.assertEqual(par_res["mode"], "parallel")
            self.assertEqual(par_res["workers_used"], 2)
            self.assertEqual(par_res["total_tests"], 2)

            # 2. Explicit `--workers 1` runs sequentially
            seq_res = srunner.run_unittest_targets(
                ["tests.test_alpha", "tests.test_beta"],
                repo_dir=repo,
                workers=1,
            )
            self.assertEqual(seq_res["returncode"], 0)
            self.assertEqual(seq_res["mode"], "sequential")
            self.assertEqual(seq_res["workers_used"], 1)
            self.assertEqual(seq_res["fallback_reason"], "")

            # 3. Restricted sandbox (`PermissionError` on ProcessPoolExecutor) falls back cleanly to sequential
            with mock.patch.object(srunner, "ProcessPoolExecutor", side_effect=PermissionError("sem_open blocked")):
                fb_res = srunner.run_unittest_targets(
                    ["tests.test_alpha", "tests.test_beta"],
                    repo_dir=repo,
                    workers=2,
                )
            self.assertEqual(fb_res["returncode"], 0)
            self.assertEqual(fb_res["mode"], "sequential")
            self.assertIn("PermissionError", fb_res["fallback_reason"])

            # 4. Failing test module surfaces non-zero exit code under parallel execution
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                fail_res = srunner.run_unittest_targets(
                    ["tests.test_alpha", "tests.test_gamma_fail"],
                    repo_dir=repo,
                    workers=2,
                )
            self.assertNotEqual(fail_res["returncode"], 0)
            self.assertEqual(fail_res["mode"], "parallel")

    def test_unittest_subprocesses_are_bounded_by_timeout_and_report_exit_124(self) -> None:
        with mock.patch.dict(os.environ, {srunner.ENV_TEST_WORKER_TIMEOUT: "not-a-number"}):
            self.assertEqual(
                srunner.resolve_unittest_timeout_seconds(), float(srunner.DEFAULT_UNITTEST_TIMEOUT_SECONDS)
            )

        expired = subprocess.TimeoutExpired(cmd=["python3"], timeout=7.0, output=b"partial out", stderr=None)
        with (
            mock.patch.dict(os.environ, {srunner.ENV_TEST_WORKER_TIMEOUT: "7"}),
            mock.patch.object(srunner.subprocess, "run", side_effect=expired) as run_mock,
            contextlib.redirect_stderr(io.StringIO()) as seq_stderr,
        ):
            self.assertEqual(srunner.resolve_unittest_timeout_seconds(), 7.0)
            self.assertEqual(srunner.resolve_unittest_timeout_seconds(3), 21.0)
            # A hung module in a pool worker is terminated and surfaces as a failed module (exit 124)
            worker_res = srunner._run_unittest_target_worker(("tests.test_hang", ".", {}))
            # The sequential path is bounded too (per-module budget x number of targets)
            seq_res = srunner.run_unittest_targets(
                ["tests.test_hang_a", "tests.test_hang_b"],
                repo_dir=pathlib.Path("."),
                env={},
                workers=2,
            )

        self.assertEqual(worker_res["returncode"], srunner.UNITTEST_TIMEOUT_RETURNCODE)
        self.assertEqual(worker_res["stdout"], "partial out")
        self.assertIn("TIMEOUT: `tests.test_hang` exceeded 7s", worker_res["stderr"])
        self.assertEqual(run_mock.call_args_list[0].kwargs["timeout"], 7.0)
        self.assertEqual(seq_res["returncode"], srunner.UNITTEST_TIMEOUT_RETURNCODE)
        self.assertEqual(seq_res["mode"], "sequential")
        self.assertEqual(run_mock.call_args_list[1].kwargs["timeout"], 14.0)
        self.assertIn("TIMEOUT: sequential unittest run exceeded 14s", seq_stderr.getvalue())

    def test_build_test_plan_parses_the_import_graph_at_most_once(self) -> None:
        changed = ["skills/agy-code-remediation-pr/scripts/rebase_gate.py"]
        expected_ast = srunner.discover_dependent_test_modules(changed, repo_root=REPO_ROOT)
        with mock.patch.object(
            srunner, "build_python_ast_import_graph", wraps=srunner.build_python_ast_import_graph
        ) as graph_spy:
            plan = srunner.build_test_plan(REPO_ROOT, changed_files=changed, no_bats=True)
            self.assertEqual(graph_spy.call_count, 1)
            # Sharing one graph must not change what either consumer selects
            self.assertEqual(plan["ast_discovered_modules"], expected_ast)
            self.assertIn("tests.test_orchestrator_parallel_p2", plan["unittest_modules"])
            for ast_mod in expected_ast:
                self.assertIn(ast_mod, plan["unittest_modules"])

            graph_spy.reset_mock()
            docs_plan = srunner.build_test_plan(REPO_ROOT, changed_files=["README.md"], no_bats=True)
            graph_spy.assert_not_called()
            self.assertEqual(docs_plan["ast_discovered_modules"], [])


class TestRotationThresholdAndLeanReviewBundle(unittest.TestCase):
    """Verify 100-110 step rotation threshold, 1-call `branch-diff --review-bundle`, and <24KB markdown ceilings."""

    def test_proactive_orchestrator_rotation_triggers_at_100_and_escalates_at_110_steps(self) -> None:
        self.assertEqual(dfo.DEFAULT_MAX_ORCHESTRATOR_STEPS, 100)
        self.assertEqual(dfo.DEFAULT_HARD_ORCHESTRATOR_STEPS, 110)

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pdir = root / "state"
            brain = root / "brain"
            pdir.mkdir(parents=True)
            orch_cid = "aaaaaaaa-1111-4222-8333-444444444444"
            (pdir / "dev_queue.json").write_text(
                json.dumps({"orchestrator_conversation_id": orch_cid, "items": []}),
                encoding="utf-8",
            )

            # 99 steps -> below threshold
            _write_transcript(brain, orch_cid, [{"type": "STEP", "status": "DONE"} for _ in range(99)])
            rep_99 = dfo.analyze_dev_flow_optimizations(portfolio_dir=pdir, brain_dir=brain, orchestrator_cid=orch_cid)
            self.assertNotIn("bloated_orchestrator_session", {f["code"] for f in rep_99["findings"]})
            diag_99 = doc.diagnose_orchestrator(state_dir=pdir, brain_dir=brain, orchestrator_cid=orch_cid, ps_output="")
            self.assertFalse(diag_99["orchestrator"]["rotation_recommended"])
            self.assertEqual(diag_99["orchestrator"]["rotation_threshold"], 100)
            self.assertEqual(diag_99["orchestrator"]["hard_rotation_threshold"], 110)
            # Below the threshold there is no optimizer finding, yet the tick must still report the real step count
            tick_99 = doc.run_orchestrator_tick(
                orchestrator_id=orch_cid,
                portfolio_dir=pdir,
                brain_dir=brain,
                sync_git=False,
                auto_heal=False,
                compact=False,
            )
            self.assertFalse(tick_99["orchestrator_rotation"]["recommended"])
            self.assertEqual(tick_99["orchestrator_rotation"]["step_count"], 99)
            self.assertEqual(tick_99["orchestrator_rotation"]["urgency"], "none")

            # 102 steps -> proactive rotation recommended (`medium`)
            _write_transcript(brain, orch_cid, [{"type": "STEP", "status": "DONE"} for _ in range(102)])
            rep_102 = dfo.analyze_dev_flow_optimizations(portfolio_dir=pdir, brain_dir=brain, orchestrator_cid=orch_cid)
            by_code_102 = {f["code"]: f for f in rep_102["findings"]}
            self.assertIn("bloated_orchestrator_session", by_code_102)
            self.assertEqual(by_code_102["bloated_orchestrator_session"]["severity"], "medium")
            self.assertEqual(by_code_102["bloated_orchestrator_session"]["evidence"]["rotation_urgency"], "proactive")
            diag_102 = doc.diagnose_orchestrator(state_dir=pdir, brain_dir=brain, orchestrator_cid=orch_cid, ps_output="")
            self.assertTrue(diag_102["orchestrator"]["rotation_recommended"])
            self.assertEqual(diag_102["orchestrator"]["rotation_urgency"], "proactive")

            tick_102 = doc.run_orchestrator_tick(
                orchestrator_id=orch_cid,
                portfolio_dir=pdir,
                brain_dir=brain,
                sync_git=False,
                auto_heal=False,
                compact=False,
            )
            self.assertTrue(tick_102["orchestrator_rotation"]["recommended"])
            self.assertTrue(tick_102["summary"]["rotation_recommended"])
            self.assertEqual(tick_102["orchestrator_rotation"]["step_count"], 102)

            # 112 steps -> escalated urgent rotation (`high`)
            _write_transcript(brain, orch_cid, [{"type": "STEP", "status": "DONE"} for _ in range(112)])
            rep_112 = dfo.analyze_dev_flow_optimizations(portfolio_dir=pdir, brain_dir=brain, orchestrator_cid=orch_cid)
            by_code_112 = {f["code"]: f for f in rep_112["findings"]}
            self.assertEqual(by_code_112["bloated_orchestrator_session"]["severity"], "high")
            self.assertEqual(by_code_112["bloated_orchestrator_session"]["evidence"]["rotation_urgency"], "urgent")
            diag_112 = doc.diagnose_orchestrator(state_dir=pdir, brain_dir=brain, orchestrator_cid=orch_cid, ps_output="")
            self.assertEqual(diag_112["orchestrator"]["rotation_urgency"], "urgent")

    def test_branch_diff_review_bundle_returns_diff_verification_gate_and_static_audit_in_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            pdir = root / "state"
            pdir.mkdir(parents=True)
            _, clone = _init_org_bare_and_clone(root)
            slug = "acme-org/acme-plugin"

            br = "fix/20261128-095000-lean-bundle-abcd1234"
            sha = _create_branch_with_files(
                clone,
                br,
                {
                    "feature_x.py": "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                    "tests/test_feature_x_abcd1234.py": (
                        "import unittest\nimport feature_x\n"
                        "class TestFeatureX(unittest.TestCase):\n"
                        "    def test_greet(self) -> None:\n"
                        "        self.assertEqual(feature_x.greet('Acme'), 'Hello, Acme!')\n"
                    ),
                },
                message="feat: add feature_x",
            )
            _git(clone, "checkout", br)
            snap = prs._snapshot_worktree_state(clone, br)

            prs.acquire_dev_pr_lock(
                conversation_id="abcd1234-9999",
                action_id="lean-bundle",
                branch_name=br,
                target_files=["feature_x.py", "tests/test_feature_x_abcd1234.py"],
                target_repo_dir=str(clone),
                worktree_path=str(clone),
                repo_slug=slug,
                summary="Add greeting helper",
                proposed_fix="Implement feature_x.greet with isolated unit test",
                portfolio_dir=pdir,
            )
            prs.update_dev_pr_lock(
                {
                    "status": "AWAITING_AGENT_REVIEW",
                    "head_sha": sha,
                    "staged_head_sha": sha,
                    "verification_results": {
                        "head_sha": sha,
                        "tree_sha": snap["tree_sha"],
                        "dirty": False,
                        "content_fingerprint": snap["content_fingerprint"],
                        "overall_passed": True,
                        "pre_commit_status": "PASSED",
                        "qa_static_audit": {"status": "pass", "passed": True, "errors": 0, "warnings": 0},
                        "unittest": {"status": "pass", "passed": True, "passed_count": 2, "total_count": 2},
                        "bats": {"status": "skipped", "passed": True},
                        "pre_commit": {"status": "pass", "passed": True},
                    },
                },
                portfolio_dir=pdir,
                repo_slug=slug,
            )

            bundle = prs.get_branch_diff_with_verification_gate(
                branch=br,
                workdir=clone,
                repo_slug=slug,
                portfolio_dir=pdir,
                review_bundle=True,
            )
            self.assertEqual(bundle["status"], "ok")
            self.assertTrue(bundle["review_bundle"])
            self.assertTrue(bundle["fast_path_ready"])
            self.assertTrue(bundle["pre_review_gate_passed"])
            self.assertTrue(bundle["verification_gate"]["skip_redundant_test_rerun"])
            self.assertTrue(bundle["static_audit_summary"]["passed"])
            self.assertTrue(bundle["static_audit_summary"]["isolated_test_policy"]["compliant"])
            self.assertTrue(bundle["static_audit_summary"]["markdown_byte_audit"]["passed"])
            self.assertEqual(bundle["task_context"]["conversation_id"], "abcd1234-9999")
            self.assertIn("def greet", bundle["diff"])
            self.assertIn("record-review", bundle["recommended_next_command"])
            self.assertFalse(bundle["diff_truncated"])
            # Totals come from `get_branch_diff()` `additions`/`deletions` (2 + 5 added lines; were always 0)
            self.assertEqual(bundle["diff_summary"]["files_count"], 2)
            self.assertEqual(bundle["diff_summary"]["total_additions"], 7)
            self.assertEqual(bundle["diff_summary"]["total_deletions"], 0)
            self.assertEqual(bundle["repo_slug"], slug)

            # Verify bounded diff truncation & >24KB markdown ceiling failure detection
            (clone / "agents").mkdir(parents=True, exist_ok=True)
            (clone / "agents" / "oversized.md").write_text("x" * 24500 + "\n", encoding="utf-8")
            _git(clone, "add", "agents/oversized.md")
            _git(clone, "commit", "-m", "docs: oversized agent doc")
            trunc_bundle = prs.get_branch_diff_with_verification_gate(
                branch=br,
                workdir=clone,
                repo_slug=slug,
                portfolio_dir=pdir,
                review_bundle=True,
                max_diff_chars=2500,
            )
            self.assertTrue(trunc_bundle["diff_truncated"])
            self.assertGreater(trunc_bundle["diff_total_chars"], 2500)
            self.assertFalse(trunc_bundle["static_audit_summary"]["markdown_byte_audit"]["passed"])
            self.assertFalse(trunc_bundle["static_audit_summary"]["passed"])

    def test_all_agent_rule_and_skill_markdown_files_stay_under_24000_bytes_and_codify_p2_contracts(self) -> None:
        md_paths = sorted(
            [
                *(REPO_ROOT / "agents").glob("*.md"),
                *(REPO_ROOT / "rules").glob("*.md"),
                *(REPO_ROOT / "skills").glob("*/SKILL.md"),
            ]
        )
        self.assertGreaterEqual(len(md_paths), 10)
        for md_path in md_paths:
            size_b = md_path.stat().st_size
            self.assertLess(size_b, 24000, msg=f"{md_path} exceeded 24,000 byte ceiling: {size_b} bytes")

        orch_text = (REPO_ROOT / "agents" / "agy-dev-orchestrator.md").read_text(encoding="utf-8")
        for term in ("100-110", "bundle-ready-slots", "speculative-rebase", "branch-diff --review-bundle"):
            self.assertIn(term, orch_text, msg=f"agy-dev-orchestrator.md missing Phase 2 term: {term}")

        rev_text = (REPO_ROOT / "agents" / "agy-code-pr-reviewer.md").read_text(encoding="utf-8")
        for term in ("branch-diff", "--review-bundle", "static_audit_summary"):
            self.assertIn(term, rev_text, msg=f"agy-code-pr-reviewer.md missing Phase 2 term: {term}")

        dev_text = (REPO_ROOT / "agents" / "agy-code-developer.md").read_text(encoding="utf-8")
        for term in ("Isolated Per-Feature Test Policy", "tests/test_<component>_<cid8>.py", "ProcessPoolExecutor"):
            self.assertIn(term, dev_text, msg=f"agy-code-developer.md missing Phase 2 term: {term}")


if __name__ == "__main__":
    unittest.main()
