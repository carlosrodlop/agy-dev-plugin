#!/usr/bin/env python3
"""Zero-credential hermetic unit tests for `dev_queue.json` and safe post-merge conversation cleanup."""

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
import urllib.parse

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CLIENTS_DIR = REPO_ROOT / "clients"
PR_REM_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
for _p in (CLIENTS_DIR, PR_REM_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Prevent inherited git hook GIT_* variables from hijacking temp-repo subprocesses during commits.
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

import git_client  # noqa: E402
import pr_remediator as prs  # noqa: E402


def _init_bare_and_clone_repo(base: pathlib.Path, name: str = "acme-plugin") -> tuple[pathlib.Path, pathlib.Path]:
    """Create a hermetic bare origin remote and local clone with an initial commit on `main`."""
    bare = base / f"{name}.git"
    clone = base / name
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Acme Bot"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "acme-bot@example.com"], check=True, capture_output=True)
    (clone / "README.md").write_text("# Acme Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "chore: initial acme commit"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return bare, clone


def _git_rev_parse(repo_path: pathlib.Path, ref: str = "main") -> str:
    """Return the resolved commit SHA for `ref` in `repo_path`."""
    return subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_org_bare_and_clone_repo(
    remotes_org: pathlib.Path,
    checkouts_dir: pathlib.Path,
    name: str,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Create a hermetic `file://` bare remote under `<org>/<name>.git` and local clone with an initial `main` commit."""
    bare = remotes_org / f"{name}.git"
    clone = checkouts_dir / name
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", f"file://{bare}", str(clone)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Acme Bot"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "acme-bot@example.com"], check=True, capture_output=True)
    (clone / "README.md").write_text(f"# {name}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "chore: initial"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "push", "-u", "origin", "main"], check=True, capture_output=True)
    return bare, clone


def _push_upstream_commit(bare_path: pathlib.Path, scratch_root: pathlib.Path, fname: str, body: str) -> str:
    """Push a new commit directly to `bare_path`'s `main` branch and return its SHA."""
    pusher = scratch_root / f"push-{fname.replace('.', '-')}"
    subprocess.run(["git", "clone", f"file://{bare_path}", str(pusher)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(pusher), "config", "user.name", "Acme Bot"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(pusher), "config", "user.email", "acme-bot@example.com"], check=True, capture_output=True)
    (pusher / fname).write_text(body, encoding="utf-8")
    subprocess.run(["git", "-C", str(pusher), "add", fname], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(pusher), "commit", "-m", f"feat: {fname}"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(pusher), "push", "origin", "main"], check=True, capture_output=True)
    return _git_rev_parse(pusher, "HEAD")


def _write_fake_conversation(
    brain_dir: pathlib.Path,
    convs_dir: pathlib.Path,
    cid: str,
    steps: list[dict] | None = None,
    mtime: float | None = None,
) -> None:
    """Create a synthetic Antigravity `brain/<cid>` transcript and `<cid>.pb` stub."""
    logs_dir = brain_dir / cid / ".system_generated" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    tpath = logs_dir / "transcript.jsonl"
    content_lines = [json.dumps(s) for s in (steps or [{"step_index": 1, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "done"}])]
    tpath.write_text("\n".join(content_lines) + "\n", encoding="utf-8")
    pb_path = convs_dir / f"{cid}.pb"
    pb_path.parent.mkdir(parents=True, exist_ok=True)
    pb_path.write_bytes(b"acme-pb-stub-" + cid.encode("utf-8"))
    if mtime is not None:
        os.utime(tpath, (mtime, mtime))
        os.utime(pb_path, (mtime, mtime))


class TestDevQueueAndMergedConversationCleanup(unittest.TestCase):
    """Verify persistent dev queue orchestration, lock auto-enqueue, and protected conversation deletion."""

    def test_dev_queue_crud_priority_depends_on_and_lock_integration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme-org"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)
            _, clone = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "acme-plugin")
            vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(clone)})

            orch_cid = "11111111-1111-4111-8111-111111111111"
            cid_standby = "22222222-2222-4222-8222-222222222222"
            cid_p1 = "33333333-3333-4333-8333-333333333333"
            cid_p2_dep = "44444444-4444-4444-8444-444444444444"
            cid_blocked = "55555555-5555-4555-8555-555555555555"

            prs.set_dev_queue_orchestrator(orch_cid, portfolio_dir=pdir)
            prs.enqueue_dev_queue_item(
                conversation_id=cid_standby,
                title="Acme standby task",
                priority=5,
                status="standby",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_p2_dep,
                title="Acme dependent P2 task",
                priority=15,
                depends_on=[cid_p1],
                status="pending",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_p1,
                title="Acme P1 task",
                action_id="acme-p1",
                priority=20,
                repo_slugs=["acme-org/acme-plugin"],
                status="pending",
                portfolio_dir=pdir,
            )

            # Next item skips standby (even priority=5) and unmet dependency cid_p2_dep (priority=15)
            nxt = prs.get_next_dev_queue_item(portfolio_dir=pdir)
            self.assertTrue(nxt["has_next"])
            self.assertEqual(nxt["next_item"]["conversation_id"], cid_p1)
            self.assertEqual(len(nxt["blocked_by_dependencies"]), 1)

            # Lock cid_p1 and verify another concurrent CID is auto-enqueued on status="locked"
            start_ok = prs.start_remediation_branch_and_lock(
                conversation_id=cid_p1,
                action_id="acme-p1",
                rca_summary="Acme parser bug",
                proposed_fix="Fix Acme parser",
                checkout_branch=True,
                branch_timestamp="20261119-100000",
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(start_ok["status"], "ok")
            prs.update_dev_queue_item_status(cid_p1, "in_progress", portfolio_dir=pdir)

            blocked_res = prs.start_remediation_branch_and_lock(
                conversation_id=cid_blocked,
                action_id="acme-blocked",
                rca_summary="Queued behind active lock",
                proposed_fix="Run after P1 merges",
                portfolio_dir=pdir,
                git_client=vcs,
            )
            self.assertEqual(blocked_res["status"], "locked")
            self.assertTrue(blocked_res.get("queued"))
            self.assertEqual(blocked_res["queue_entry"]["conversation_id"], cid_blocked)

            # Auto-lock call for existing standby task must preserve standby status
            prs.start_remediation_branch_and_lock(
                conversation_id=cid_standby,
                action_id="standby-should-hold",
                portfolio_dir=pdir,
                git_client=vcs,
            )
            q_after = prs.load_dev_queue(portfolio_dir=pdir)
            by_cid = {it["conversation_id"]: it for it in q_after["items"]}
            self.assertEqual(by_cid[cid_standby]["status"], "standby")
            self.assertEqual(by_cid[cid_blocked]["status"], "pending")

            # Releasing lock on human merge advances cid_p1 -> merged, unlocking cid_p2_dep
            prs.release_dev_pr_lock(reason="HUMAN_VALIDATED_MANUAL_PR", portfolio_dir=pdir)
            nxt2 = prs.get_next_dev_queue_item(portfolio_dir=pdir)
            self.assertTrue(nxt2["has_next"])
            self.assertEqual(nxt2["next_item"]["conversation_id"], cid_p2_dep)

    def test_cleanup_merged_conversations_protections_subagents_and_git_merge_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            brain_dir = troot / "antigravity" / "brain"
            convs_dir = troot / "antigravity" / "conversations"
            pdir.mkdir(parents=True, exist_ok=True)
            brain_dir.mkdir(parents=True, exist_ok=True)
            convs_dir.mkdir(parents=True, exist_ok=True)

            _, clone = _init_bare_and_clone_repo(troot)
            vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(clone)})

            old_mtime = 1700000000.0
            orch_cid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            merged_root = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            spawned_dev = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            spawned_rev = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            pending_prose_ref = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
            active_step_cid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
            tier2_cid = "99999999-9999-4999-8999-999999999999"

            prs.set_dev_queue_orchestrator(orch_cid, portfolio_dir=pdir)
            prs.enqueue_dev_queue_item(
                conversation_id=pending_prose_ref,
                title="Pending item mentioned in prose",
                status="pending",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=merged_root,
                title="Acme merged fix",
                status="merged",
                branch_names=["fix/20261119-110000-acme-cleanup-bbbbbbbb"],
                portfolio_dir=pdir,
            )

            # Seed actions_state.json with a pending Tier 2 proposal
            (pdir / "actions_state.json").write_text(
                json.dumps(
                    {
                        "actions_log_audit_state": {
                            "pending_tier2_proposals": {
                                tier2_cid: {"conversation_id": tier2_cid, "resolution": "STAGE_PROPOSED"}
                            }
                        }
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            _write_fake_conversation(
                brain_dir,
                convs_dir,
                merged_root,
                steps=[
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "content": f"Pending backlog item {pending_prose_ref} and {tier2_cid} stay untouched.",
                        "tool_calls": [
                            {
                                "name": "invoke_subagent",
                                "result": {"conversationId": spawned_dev},
                            }
                        ],
                    },
                    {
                        "step_index": 2,
                        "type": "GENERIC",
                        "status": "DONE",
                        "content": json.dumps(
                            {
                                "spec": {"typeName": "agy-code-pr-reviewer"},
                                "result": {"conversationId": spawned_rev},
                            }
                        ),
                    },
                ],
                mtime=old_mtime,
            )
            _write_fake_conversation(brain_dir, convs_dir, spawned_dev, mtime=old_mtime)
            _write_fake_conversation(brain_dir, convs_dir, spawned_rev, mtime=old_mtime)
            _write_fake_conversation(brain_dir, convs_dir, pending_prose_ref, mtime=old_mtime)
            _write_fake_conversation(brain_dir, convs_dir, tier2_cid, mtime=old_mtime)
            _write_fake_conversation(brain_dir, convs_dir, orch_cid, mtime=old_mtime)
            _write_fake_conversation(
                brain_dir,
                convs_dir,
                active_step_cid,
                steps=[{"step_index": 1, "type": "PLANNER_RESPONSE", "status": "RUNNING", "content": "working"}],
                mtime=old_mtime,
            )

            # Create an UNMERGED feature branch on origin and confirm deletion is BLOCKED
            br_name = "fix/20261119-110000-acme-cleanup-bbbbbbbb"
            subprocess.run(["git", "-C", str(clone), "checkout", "-b", br_name], check=True, capture_output=True)
            (clone / "feature.py").write_text('"""Acme module."""\nVAL = 1\n', encoding="utf-8")
            subprocess.run(["git", "-C", str(clone), "add", "feature.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(clone), "commit", "-m", "feat: acme cleanup"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(clone), "push", "-u", "origin", br_name], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(clone), "checkout", "main"], check=True, capture_output=True)

            blocked = prs.cleanup_merged_conversations(
                conversation_ids=[merged_root],
                branch_specs=[f"{clone}::{br_name}"],
                confirm_merged=True,
                include_unaudited_check=False,
                portfolio_dir=pdir,
                brain_dir=brain_dir,
                conversations_dir=convs_dir,
                git_client=vcs,
            )
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["deleted_count"], 0)
            self.assertTrue((brain_dir / merged_root).is_dir())

            # Merge branch into origin/main
            subprocess.run(["git", "-C", str(clone), "merge", "--no-ff", br_name, "-m", f"Merge {br_name}"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(clone), "push", "origin", "main"], check=True, capture_output=True)

            # Dry-run leaves everything on disk
            dry = prs.cleanup_merged_conversations(
                conversation_ids=[merged_root],
                branch_specs=[f"{clone}::{br_name}"],
                dry_run=True,
                include_unaudited_check=False,
                portfolio_dir=pdir,
                brain_dir=brain_dir,
                conversations_dir=convs_dir,
                git_client=vcs,
            )
            self.assertEqual(dry["status"], "ok")
            self.assertEqual(set(dry["deleted"]), {merged_root, spawned_dev, spawned_rev})
            self.assertTrue((brain_dir / merged_root).is_dir())

            # Protected pending / tier2 / orchestrator / running CIDs must be skipped even if passed
            cleaned = prs.cleanup_merged_conversations(
                conversation_ids=[merged_root, orch_cid, pending_prose_ref, tier2_cid, active_step_cid],
                branch_specs=[f"{clone}::{br_name}"],
                include_unaudited_check=False,
                portfolio_dir=pdir,
                brain_dir=brain_dir,
                conversations_dir=convs_dir,
                git_client=vcs,
            )
            self.assertEqual(cleaned["status"], "ok")
            self.assertEqual(set(cleaned["deleted"]), {merged_root, spawned_dev, spawned_rev})
            self.assertFalse((brain_dir / merged_root).exists())
            self.assertFalse((convs_dir / f"{spawned_dev}.pb").exists())
            self.assertTrue((brain_dir / orch_cid).is_dir())
            self.assertTrue((brain_dir / pending_prose_ref).is_dir())
            self.assertTrue((brain_dir / tier2_cid).is_dir())
            self.assertTrue((brain_dir / active_step_cid).is_dir())

            # Verify queue item moved from merged -> cleaned
            q_final = prs.load_dev_queue(portfolio_dir=pdir)
            by_cid_final = {it["conversation_id"]: it for it in q_final["items"]}
            self.assertEqual(by_cid_final[merged_root]["status"], "cleaned")

            # Verify build_code_developer_subagent_plan supports optional model tiering & anchored context while preserving pro default
            default_plan = prs.build_code_developer_subagent_plan(conversation_id="abcd1234-0000-0000-0000-000000000000", action_id="acme")
            self.assertEqual(default_plan["subagent_invocation"]["Model"], "pro")
            flash_plan = prs.build_code_developer_subagent_plan(
                conversation_id="abcd1234-0000-0000-0000-000000000000",
                action_id="acme",
                model="flash",
                anchored_context="clients/foo.py:10-40",
            )
            self.assertEqual(flash_plan["subagent_invocation"]["Model"], "flash")
            self.assertIn("clients/foo.py:10-40", flash_plan["subagent_invocation"]["Prompt"])
            with mock.patch("sys.argv", ["pr_remediator.py", "--help"]):
                self.assertEqual(prs.parse_args(["queue-list"]).subcommand, "queue-list")

    def test_multi_repo_verify_branches_dedups_scoped_repo_and_blocks_partial_merge(self) -> None:
        """Ensure multi-repo `last_repo_branches` + `dev_queue.branch_names` never creates wrong-repo duplicate targets."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            _, repo_plugin = _init_bare_and_clone_repo(troot, name="acme-plugin")
            _, repo_tower = _init_bare_and_clone_repo(troot, name="acme-tower")

            multi_cid = "77777777-7777-4777-8777-777777777777"
            br_plugin = "fix/20261119-120000-acme-plugin-77777777"
            br_tower = "fix/20261119-120000-acme-tower-77777777"

            # Create and merge branch in repo_plugin
            subprocess.run(["git", "-C", str(repo_plugin), "checkout", "-b", br_plugin], check=True, capture_output=True)
            (repo_plugin / "p.txt").write_text("plugin change\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_plugin), "add", "p.txt"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_plugin), "commit", "-m", "feat: acme plugin"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_plugin), "push", "-u", "origin", br_plugin], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_plugin), "checkout", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_plugin), "merge", "--no-ff", br_plugin, "-m", f"Merge {br_plugin}"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_plugin), "push", "origin", "main"], check=True, capture_output=True)

            # Create branch in repo_tower (NOT yet merged)
            subprocess.run(["git", "-C", str(repo_tower), "checkout", "-b", br_tower], check=True, capture_output=True)
            (repo_tower / "t.txt").write_text("tower change\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_tower), "add", "t.txt"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_tower), "commit", "-m", "feat: acme tower"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_tower), "push", "-u", "origin", br_tower], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_tower), "checkout", "main"], check=True, capture_output=True)

            # Record both branches in released last_repo_branches AND dev_queue.json branch_names
            (pdir / "actions_state.json").write_text(
                json.dumps(
                    {
                        "actions_log_audit_state": {
                            "dev_pr_lock": {
                                "active": False,
                                "last_release_reason": "HUMAN_VALIDATED_MANUAL_PR",
                                "last_conversation_id": multi_cid,
                                "last_branch_name": br_tower,
                                "last_repo_branches": {
                                    "acme-org/acme-plugin": {"target_repo_dir": str(repo_plugin), "branch_name": br_plugin},
                                    "acme-org/acme-tower": {"target_repo_dir": str(repo_tower), "branch_name": br_tower},
                                },
                            }
                        }
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            prs.enqueue_dev_queue_item(
                conversation_id=multi_cid,
                title="Multi-repo Acme task",
                repo_slugs=["acme-org/acme-plugin", "acme-org/acme-tower"],
                branch_names=[br_plugin, br_tower],
                status="merged",
                portfolio_dir=pdir,
            )

            vcs_plugin_default = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_plugin)})
            partial_check = prs.verify_branches_merged_into_base(
                conversation_ids=[multi_cid],
                confirm_merged=False,
                portfolio_dir=pdir,
                git_client=vcs_plugin_default,
            )
            self.assertEqual(partial_check["checked_count"], 2)
            self.assertFalse(partial_check["all_merged"])
            self.assertEqual(len(partial_check["unmerged"]), 1)
            self.assertEqual(partial_check["unmerged"][0]["branch_name"], br_tower)

            # Merge repo_tower too and verify exact 2-repo check passes with zero wrong-repo duplicate
            subprocess.run(["git", "-C", str(repo_tower), "merge", "--no-ff", br_tower, "-m", f"Merge {br_tower}"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_tower), "push", "origin", "main"], check=True, capture_output=True)

            full_check = prs.verify_branches_merged_into_base(
                conversation_ids=[multi_cid],
                confirm_merged=False,
                portfolio_dir=pdir,
                git_client=vcs_plugin_default,
            )
            self.assertEqual(full_check["checked_count"], 2)
            self.assertTrue(full_check["all_merged"])
            by_branch = {row["branch_name"]: pathlib.Path(row["repo_dir"]).name for row in full_check["branches"]}
            self.assertEqual(by_branch, {br_plugin: "acme-plugin", br_tower: "acme-tower"})

    def test_three_concurrent_repo_locks_preserve_foreign_legacy_lock_and_isolate_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            _, repo_plugin = _init_bare_and_clone_repo(troot, name="acme-plugin")
            _, repo_cli = _init_bare_and_clone_repo(troot, name="acme-cli")
            _, repo_tower = _init_bare_and_clone_repo(troot, name="acme-tower")
            subprocess.run(["git", "-C", str(repo_plugin), "remote", "set-url", "origin", f"git@github.com:acme-org/acme-plugin.git"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_cli), "remote", "set-url", "origin", f"git@github.com:acme-org/acme-cli.git"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_tower), "remote", "set-url", "origin", f"git@github.com:acme-org/acme-tower.git"], check=True, capture_output=True)

            legacy_cid = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
            plugin_cid = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
            cli_cid = "cccccccc-3333-4333-8333-cccccccccccc"
            other_plugin_cid = "dddddddd-4444-4444-8444-dddddddddddd"

            # Seed foreign active legacy `dev_pr_lock` for acme-org/acme-tower
            (pdir / "actions_state.json").write_text(
                json.dumps(
                    {
                        "actions_log_audit_state": {
                            "dev_pr_lock": {
                                "active": True,
                                "conversation_id": legacy_cid,
                                "action_id": "legacy-tower",
                                "branch_name": "fix/20261120-010000-legacy-tower-aaaaaaaa",
                                "target_repo_dir": str(repo_tower),
                                "repo_slug": "acme-org/acme-tower",
                            }
                        }
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            vcs_plugin = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_plugin)})
            res_plugin = prs.start_remediation_branch_and_lock(
                conversation_id=plugin_cid,
                action_id="acme-fix",
                rca_summary="Plugin fix",
                proposed_fix="Patch plugin",
                create_worktree=True,
                branch_timestamp="20261120-092000",
                portfolio_dir=pdir,
                workdir=repo_plugin,
                git_client=vcs_plugin,
            )
            self.assertEqual(res_plugin["status"], "ok")
            self.assertEqual(res_plugin["repo_slug"], "acme-org/acme-plugin")
            self.assertIn("acme-plugin", pathlib.Path(res_plugin["worktree_path"]).name)

            vcs_cli = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_cli)})
            res_cli = prs.start_remediation_branch_and_lock(
                conversation_id=cli_cid,
                action_id="acme-fix",
                rca_summary="CLI fix",
                proposed_fix="Patch cli",
                create_worktree=True,
                branch_timestamp="20261120-092000",
                portfolio_dir=pdir,
                workdir=repo_cli,
                git_client=vcs_cli,
            )
            self.assertEqual(res_cli["status"], "ok")
            self.assertEqual(res_cli["repo_slug"], "acme-org/acme-cli")
            self.assertIn("acme-cli", pathlib.Path(res_cli["worktree_path"]).name)
            self.assertNotEqual(res_plugin["worktree_path"], res_cli["worktree_path"])

            all_locks = prs.get_all_dev_pr_locks(portfolio_dir=pdir)
            self.assertEqual(set(all_locks.keys()), {"acme-org/acme-tower", "acme-org/acme-plugin", "acme-org/acme-cli"})

            # Competing conversation targeting acme-plugin is blocked & auto-enqueued on that repo only
            blocked = prs.start_remediation_branch_and_lock(
                conversation_id=other_plugin_cid,
                action_id="competing",
                rca_summary="Should block",
                proposed_fix="Wait",
                portfolio_dir=pdir,
                workdir=repo_plugin,
                git_client=vcs_plugin,
            )
            self.assertEqual(blocked["status"], "locked")
            self.assertFalse(blocked["acquired"])
            self.assertTrue(blocked["queued"])
            q_items = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
            self.assertEqual(q_items[other_plugin_cid]["repo_slugs"], ["acme-org/acme-plugin"])

            # Verify foreign legacy lock remained untouched in raw state JSON
            raw_state = json.loads((pdir / "actions_state.json").read_text(encoding="utf-8"))
            self.assertEqual(raw_state["actions_log_audit_state"]["dev_pr_lock"]["conversation_id"], legacy_cid)
            self.assertTrue(raw_state["actions_log_audit_state"]["dev_pr_lock"]["active"])

            # Releasing only acme-org/acme-plugin keeps cli & legacy tower locks intact
            prs.cleanup_worktree(res_plugin["worktree_path"], repo_dir=repo_plugin)
            prs.cleanup_worktree(res_cli["worktree_path"], repo_dir=repo_cli)
            prs.release_dev_pr_lock(
                reason="TEST_RELEASE",
                portfolio_dir=pdir,
                repo_slug="acme-org/acme-plugin",
            )
            remaining = prs.get_all_dev_pr_locks(portfolio_dir=pdir)
            self.assertEqual(set(remaining.keys()), {"acme-org/acme-tower", "acme-org/acme-cli"})

    def test_queue_next_unlocked_and_progressive_multi_repo_merged_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            _, repo_plugin = _init_bare_and_clone_repo(troot, name="acme-plugin")
            _, repo_tower = _init_bare_and_clone_repo(troot, name="acme-tower")

            orch_cid = "11111111-aaaa-4111-8111-111111111111"
            locked_holder = "22222222-aaaa-4222-8222-222222222222"
            cid_high_locked = "33333333-aaaa-4333-8333-333333333333"
            cid_low_unlocked = "44444444-aaaa-4444-8444-444444444444"

            prs.acquire_dev_pr_lock(
                conversation_id=locked_holder,
                action_id="hold-plugin",
                branch_name="fix/20261120-080000-hold-22222222",
                summary="Busy",
                proposed_fix="Hold",
                target_repo_dir=str(repo_plugin),
                repo_slug="acme-org/acme-plugin",
                portfolio_dir=pdir,
            )
            prs.set_dev_queue_orchestrator(orch_cid, portfolio_dir=pdir)
            prs.enqueue_dev_queue_item(
                conversation_id=cid_high_locked,
                title="High priority blocked on acme-plugin",
                priority=10,
                repo_slugs=["acme-org/acme-plugin", "acme-org/acme-tower"],
                status="pending",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_low_unlocked,
                title="Lower priority on unlocked acme-cli",
                priority=50,
                repo_slugs=["acme-org/acme-cli"],
                status="pending",
                portfolio_dir=pdir,
            )

            picked = prs.get_next_dev_queue_item(orch_cid, portfolio_dir=pdir, skip_locked_repos=True)
            self.assertEqual(picked["next_unlocked_item"]["conversation_id"], cid_low_unlocked)
            self.assertEqual(picked["next_item"]["conversation_id"], cid_low_unlocked)
            self.assertTrue(picked["blocked_by_repo_locks"])

            # Verify partial multi-repo merge progression (`merged_repos`):
            # without any open 2nd PR URL or active lock on acme-tower, item cleanly reverts to `pending` (never phantom `pr_open`)
            prs.update_dev_queue_item_status(cid_high_locked, status="pr_open", portfolio_dir=pdir)
            partial = prs._mark_queue_item_repo_merged(
                conversation_id=cid_high_locked,
                released_repo_slug="acme-org/acme-plugin",
                branch_names=["fix/plugin"],
                portfolio_dir=pdir,
            )["item"]
            self.assertEqual(partial["merged_repos"], ["acme-org/acme-plugin"])
            self.assertEqual(partial["status"], "pending")

            completed = prs._mark_queue_item_repo_merged(
                conversation_id=cid_high_locked,
                released_repo_slug="acme-org/acme-tower",
                branch_names=["fix/tower"],
                portfolio_dir=pdir,
            )["item"]
            self.assertEqual(completed["merged_repos"], ["acme-org/acme-plugin", "acme-org/acme-tower"])
            self.assertEqual(completed["status"], "merged")

    def test_sync_dev_pr_lock_with_git_honors_workdir_when_unlocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme-org"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)
            scratch = troot / "scratch"
            scratch.mkdir(parents=True, exist_ok=True)

            bare_default, repo_default = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "acme-default")
            bare_other, repo_other = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "acme-other")
            initial_default_sha = _git_rev_parse(repo_default)

            # Advance both upstream remotes so syncing the wrong repo is immediately observable
            _push_upstream_commit(bare_default, scratch, "default-ahead.txt", "default upstream\n")
            upstream_other_sha_1 = _push_upstream_commit(bare_other, scratch, "other-v1.txt", "other upstream 1\n")

            with (
                mock.patch.dict(os.environ, {"AGY_CONF_REPO_DIR": str(repo_default)}),
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
            ):
                # 1. Empty active_map: sync_dev_pr_lock_with_git(workdir=repo_other) must sync ONLY repo_other
                res_empty = prs.sync_dev_pr_lock_with_git(
                    portfolio_dir=pdir,
                    workdir=repo_other,
                )
                self.assertEqual(res_empty["status"], "ok")
                self.assertFalse(res_empty["active"])
                self.assertEqual(res_empty["repo_slug"], "acme-org/acme-other")
                self.assertEqual(res_empty["lock"]["repo_slug"], "acme-org/acme-other")
                self.assertFalse(res_empty["lock"]["active"])
                self.assertTrue(res_empty["repo_sync"]["synced"])
                self.assertEqual(res_empty["repo_sync"]["local_base_sha"], upstream_other_sha_1)
                self.assertEqual(_git_rev_parse(repo_other), upstream_other_sha_1)
                self.assertEqual(_git_rev_parse(repo_default), initial_default_sha)

                # 2. Non-empty active_map holding a lock on repo_default: workdir=repo_other must still sync ONLY repo_other
                prs.acquire_dev_pr_lock(
                    conversation_id="99999999-1111-4111-8111-999999999999",
                    action_id="hold-default",
                    branch_name="fix/20261120-100000-hold-default-99999999",
                    summary="Active lock on default repo",
                    proposed_fix="Keep default locked",
                    target_repo_dir=str(repo_default),
                    repo_slug="acme-org/acme-default",
                    portfolio_dir=pdir,
                )
                upstream_other_sha_2 = _push_upstream_commit(bare_other, scratch, "other-v2.txt", "other upstream 2\n")

                res_sibling_locked = prs.sync_dev_pr_lock_with_git(
                    portfolio_dir=pdir,
                    workdir=repo_other,
                )
                self.assertEqual(res_sibling_locked["status"], "ok")
                self.assertTrue(res_sibling_locked["active"])
                self.assertFalse(res_sibling_locked["repo_active"])
                self.assertEqual(res_sibling_locked["repo_slug"], "acme-org/acme-other")
                self.assertEqual(res_sibling_locked["lock"]["repo_slug"], "acme-org/acme-other")
                self.assertFalse(res_sibling_locked["lock"]["active"])
                self.assertTrue(res_sibling_locked["repo_sync"]["synced"])
                self.assertEqual(res_sibling_locked["repo_sync"]["local_base_sha"], upstream_other_sha_2)
                self.assertEqual(_git_rev_parse(repo_other), upstream_other_sha_2)
                self.assertEqual(_git_rev_parse(repo_default), initial_default_sha)
                self.assertIn("acme-org/acme-default", prs.get_all_dev_pr_locks(portfolio_dir=pdir))

    def test_sync_dev_pr_lock_with_git_repo_slug_injected_client_and_unscoped_compat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme-org"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)
            scratch = troot / "scratch"
            scratch.mkdir(parents=True, exist_ok=True)

            bare_default, repo_default = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "acme-default")
            bare_other, repo_other = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "acme-other")
            initial_default_sha = _git_rev_parse(repo_default)
            upstream_default_sha = _push_upstream_commit(bare_default, scratch, "default-next.txt", "default next\n")
            upstream_other_sha = _push_upstream_commit(bare_other, scratch, "other-slug.txt", "other slug\n")

            with (
                mock.patch.dict(os.environ, {"AGY_CONF_REPO_DIR": str(repo_default)}),
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
            ):
                # 1. Explicit --repo slug without workdir resolves sibling checkout and never syncs default repo
                res_slug = prs.sync_dev_pr_lock_with_git(
                    portfolio_dir=pdir,
                    repo_slug="acme-org/acme-other",
                )
                self.assertEqual(res_slug["repo_slug"], "acme-org/acme-other")
                self.assertTrue(res_slug["repo_sync"]["synced"])
                self.assertEqual(_git_rev_parse(repo_other), upstream_other_sha)
                self.assertEqual(_git_rev_parse(repo_default), initial_default_sha)

                # 2. Unknown --repo slug without local checkout skips sync instead of falling back to default repo
                res_unknown = prs.sync_dev_pr_lock_with_git(
                    portfolio_dir=pdir,
                    repo_slug="acme-org/missing-repo",
                )
                self.assertEqual(res_unknown["repo_sync"]["status"], "skipped")
                self.assertEqual(res_unknown["repo_sync"]["reason"], "repo_dir_not_found_for_slug")
                self.assertEqual(_git_rev_parse(repo_default), initial_default_sha)

                # 3. Injected git_client is used as-is without mutation
                class _StubVcs:
                    def __init__(self) -> None:
                        self.calls: list[str] = []

                    def sync_and_clean_local_repo(self, base_branch: str = "main") -> dict[str, str]:
                        self.calls.append(base_branch)
                        return {"status": "ok", "stub_branch": base_branch}

                stub = _StubVcs()
                res_stub = prs.sync_dev_pr_lock_with_git(
                    portfolio_dir=pdir,
                    git_client=stub,  # type: ignore[arg-type]
                    workdir=repo_other,
                )
                self.assertEqual(stub.calls, ["main"])
                self.assertEqual(res_stub["repo_sync"]["stub_branch"], "main")
                self.assertEqual(set(stub.__dict__.keys()), {"calls"})

                # 4. Unscoped call (neither repo_slug nor workdir) preserves default-repo sync backward compatibility
                res_unscoped = prs.sync_dev_pr_lock_with_git(portfolio_dir=pdir)
                self.assertTrue(res_unscoped["repo_sync"]["synced"])
                self.assertEqual(_git_rev_parse(repo_default), upstream_default_sha)

    def test_sync_and_clean_local_repo_protects_active_dev_pr_lock_branch_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme-org"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo_main = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "acme-guarded")

            active_branch = "fix/20261120-110127-active-dev-11111111"
            orphan_branch = "fix/20261120-090000-stale-orphan-22222222"
            active_wt = troot / "wt-active"
            orphan_wt = troot / "wt-orphan"

            # Create two unpushed local fix/* branches + worktrees (neither exists on origin)
            subprocess.run(
                ["git", "-C", str(repo_main), "worktree", "add", "-b", active_branch, str(active_wt), "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_main), "worktree", "add", "-b", orphan_branch, str(orphan_wt), "main"],
                check=True,
                capture_output=True,
                text=True,
            )

            # Record ONLY active_branch as an active DEVELOPING (unpushed) lock in actions_state.json
            prs.acquire_dev_pr_lock(
                conversation_id="11111111-aaaa-4111-8111-111111111111",
                action_id="active-dev",
                branch_name=active_branch,
                worktree_path=str(active_wt),
                summary="In-progress unpushed worktree",
                proposed_fix="Must never be pruned",
                target_repo_dir=str(repo_main),
                repo_slug="acme-org/acme-guarded",
                portfolio_dir=pdir,
            )

            vcs = git_client.GitVcsIntegrationClient(
                env_overrides={"AGY_CONF_REPO_DIR": str(repo_main), "AGY_DEV_STATE_DIR": str(pdir)}
            )
            # Call sync_and_clean_local_repo WITHOUT passing protected_branches explicitly
            sync_res = vcs.sync_and_clean_local_repo(base_branch="main")
            self.assertEqual(sync_res["status"], "ok")
            self.assertTrue(sync_res["synced"])
            # Active DEVELOPING unpushed branch and worktree MUST survive intact
            self.assertTrue(active_wt.is_dir())
            self.assertNotIn(active_branch, sync_res["deleted_local_branches"])
            self.assertNotIn(str(active_wt.resolve()), [str(pathlib.Path(p).resolve()) for p in sync_res["deleted_worktrees"]])
            local_branches = subprocess.run(
                ["git", "-C", str(repo_main), "branch", "--format=%(refname:short)"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertIn(active_branch, local_branches)
            # Unrelated orphaned fix/* branch and worktree ARE pruned normally
            self.assertIn(orphan_branch, sync_res["deleted_local_branches"])
            self.assertFalse(orphan_wt.exists())

            # Explicitly passing force_clean_branches (post-merge cleanup before lock release) STILL removes it
            clean_res = vcs.sync_and_clean_local_repo(
                base_branch="main",
                force_clean_branches=[active_branch],
            )
            self.assertEqual(clean_res["status"], "ok")
            self.assertIn(active_branch, clean_res["deleted_local_branches"])
            self.assertFalse(active_wt.exists())

    def test_sync_dev_pr_lock_with_git_missing_or_cleaned_workdir_never_syncs_default_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme-org"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)
            scratch = troot / "scratch"
            scratch.mkdir(parents=True, exist_ok=True)

            bare_default, repo_default = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "acme-default")
            initial_default_sha = _git_rev_parse(repo_default)
            _push_upstream_commit(bare_default, scratch, "default-upstream.txt", "ahead commit\n")

            missing_wt = troot / "tmp" / "pr-worktree-acme-other-fix-20261120-100000-demo-12345678"
            plain_non_git = troot / "plain-non-git-dir"
            plain_non_git.mkdir(parents=True, exist_ok=True)

            with (
                mock.patch.dict(os.environ, {"AGY_CONF_REPO_DIR": str(repo_default)}),
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
            ):
                # 1. Empty active_map: missing cleaned worktree must skip sync and never touch repo_default
                res_missing_empty = prs.sync_dev_pr_lock_with_git(
                    portfolio_dir=pdir,
                    workdir=missing_wt,
                )
                self.assertEqual(res_missing_empty["status"], "ok")
                self.assertFalse(res_missing_empty["active"])
                self.assertEqual(res_missing_empty["repo_sync"]["status"], "skipped")
                self.assertFalse(res_missing_empty["repo_sync"]["synced"])
                self.assertEqual(res_missing_empty["repo_sync"]["reason"], "workdir_not_a_git_repo")
                self.assertEqual(_git_rev_parse(repo_default), initial_default_sha)

                # 2. Empty active_map: existing non-git directory must also skip sync and never touch repo_default
                res_nongit_empty = prs.sync_dev_pr_lock_with_git(
                    portfolio_dir=pdir,
                    workdir=plain_non_git,
                )
                self.assertEqual(res_nongit_empty["repo_sync"]["status"], "skipped")
                self.assertFalse(res_nongit_empty["repo_sync"]["synced"])
                self.assertEqual(res_nongit_empty["repo_sync"]["reason"], "workdir_not_a_git_repo")
                self.assertEqual(_git_rev_parse(repo_default), initial_default_sha)

                # 3. Non-empty active_map holding a lock on repo_default: missing/cleaned workdir skips without touching repo_default
                prs.acquire_dev_pr_lock(
                    conversation_id="66666666-1111-4111-8111-666666666666",
                    action_id="hold-default",
                    branch_name="fix/20261120-100000-hold-default-66666666",
                    summary="Active lock on default repo",
                    proposed_fix="Stay locked",
                    target_repo_dir=str(repo_default),
                    repo_slug="acme-org/acme-default",
                    portfolio_dir=pdir,
                )
                res_missing_locked = prs.sync_dev_pr_lock_with_git(
                    portfolio_dir=pdir,
                    workdir=missing_wt,
                )
                self.assertEqual(res_missing_locked["status"], "ok")
                self.assertTrue(res_missing_locked["active"])
                self.assertFalse(res_missing_locked["repo_active"])
                self.assertEqual(res_missing_locked["repo_sync"]["status"], "skipped")
                self.assertFalse(res_missing_locked["repo_sync"]["synced"])
                self.assertEqual(res_missing_locked["repo_sync"]["reason"], "workdir_not_a_git_repo")
                self.assertEqual(_git_rev_parse(repo_default), initial_default_sha)

    def test_record_review_on_merged_repo_with_deleted_worktree_never_corrupts_sibling_active_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme-org"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "acme-a")
            _, repo_b = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "acme-b")

            cid_a = "aaaaaaaa-9999-4999-8999-aaaaaaaaaaaa"
            cid_b = "bbbbbbbb-9999-4999-8999-bbbbbbbbbbbb"
            branch_a = "fix/20261120-110121-repo-a-aaaaaaaa"
            branch_b = "fix/20261120-110127-repo-b-bbbbbbbb"
            wt_a = troot / "tmp" / f"pr-worktree-acme-a-{branch_a.replace('/', '-')}"
            wt_b = troot / "tmp" / f"pr-worktree-acme-b-{branch_b.replace('/', '-')}"
            wt_a.parent.mkdir(parents=True, exist_ok=True)

            # Create branch_a + worktree on repo_a, commit with real title, push, merge into main, then release lock (deleting wt_a)
            subprocess.run(
                ["git", "-C", str(repo_a), "worktree", "add", "-b", branch_a, str(wt_a), "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            (wt_a / "feature_a.txt").write_text("feature a\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(wt_a), "add", "feature_a.txt"], check=True, capture_output=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(wt_a),
                    "commit",
                    "-m",
                    "feat(a): real repo-a feature\n\n## 📋 Executive Summary\n\n- **Summary of Changes**: Real repo-a summary.\n",
                ],
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "-C", str(wt_a), "push", "-u", "origin", branch_a], check=True, capture_output=True)
            branch_a_sha = _git_rev_parse(wt_a, "HEAD")

            prs.acquire_dev_pr_lock(
                conversation_id=cid_a,
                action_id="repo-a",
                branch_name=branch_a,
                worktree_path=str(wt_a),
                summary="Repo A change",
                proposed_fix="Ship repo A",
                target_repo_dir=str(repo_a),
                repo_slug="acme-org/acme-a",
                portfolio_dir=pdir,
            )
            subprocess.run(["git", "-C", str(repo_a), "merge", "--no-ff", branch_a, "-m", f"Merge {branch_a}"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "origin", "main"], check=True, capture_output=True)
            with (
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
                mock.patch.object(prs, "restart_pro_api_server", return_value=False),
            ):
                prs.release_dev_pr_lock(
                    reason="BRANCH_MERGED_INTO_MAIN",
                    portfolio_dir=pdir,
                    repo_slug="acme-org/acme-a",
                    branch_name=branch_a,
                )
            self.assertFalse(wt_a.exists())

            # Create active DEVELOPING unpushed branch_b + worktree wt_b on repo_b (also set as default AGY_CONF_REPO_DIR)
            subprocess.run(
                ["git", "-C", str(repo_b), "worktree", "add", "-b", branch_b, str(wt_b), "main"],
                check=True,
                capture_output=True,
                text=True,
            )
            prs.acquire_dev_pr_lock(
                conversation_id=cid_b,
                action_id="repo-b",
                branch_name=branch_b,
                worktree_path=str(wt_b),
                summary="Active repo B work",
                proposed_fix="Must stay isolated",
                target_repo_dir=str(repo_b),
                repo_slug="acme-org/acme-b",
                portfolio_dir=pdir,
            )
            self.assertTrue(wt_b.is_dir())

            with (
                mock.patch.dict(os.environ, {"AGY_CONF_REPO_DIR": str(repo_b)}),
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
                mock.patch.object(prs, "spawn_pr_merge_watcher_daemon", return_value={"status": "skipped", "spawned": False}),
            ):
                # Late record_pr_review_evaluation on merged repo_a with deleted wt_a must NOT touch repo_b's lock or overwrite branch_a commit
                rev_late = prs.record_pr_review_evaluation(
                    branch_name=branch_a,
                    workdir=wt_a,
                    verdict="PASS",
                    score=96,
                    summary="Late review after merge",
                    send_email=False,
                    portfolio_dir=pdir,
                )
                self.assertEqual(rev_late["status"], "ok")
                # Existing commit subject/SHA/summary on repo_a preserved (never amended or routed to repo_b)
                self.assertEqual(_git_rev_parse(repo_a, branch_a), branch_a_sha)
                unquoted_url = urllib.parse.unquote(rev_late["manual_pr_url"])
                self.assertIn("acme-org/acme-a", unquoted_url)
                self.assertIn("feat(a): real repo-a feature", unquoted_url)
                self.assertIn("Real repo-a summary.", rev_late["review_markdown"])

                # repo_b's active lock MUST remain completely uncorrupted
                active_locks = prs.get_all_dev_pr_locks(portfolio_dir=pdir)
                self.assertEqual(set(active_locks.keys()), {"acme-org/acme-b"})
                lock_b = active_locks["acme-org/acme-b"]
                self.assertTrue(lock_b["active"])
                self.assertEqual(lock_b["branch_name"], branch_b)
                self.assertEqual(lock_b["conversation_id"], cid_b)

                # Direct cross-branch update_dev_pr_lock on repo_b with branch_a must also be refused
                bad_upd = prs.update_dev_pr_lock(
                    {"branch_name": branch_a, "status": "PENDING_HUMAN_PR_VALIDATION"},
                    portfolio_dir=pdir,
                    repo_slug="acme-org/acme-b",
                )
                self.assertEqual(bad_upd["status"], "error")
                self.assertEqual(prs.get_all_dev_pr_locks(portfolio_dir=pdir)["acme-org/acme-b"]["branch_name"], branch_b)

                # Subsequent sync_dev_pr_lock_with_git must preserve repo_b's active worktree and branch intact
                sync_after = prs.sync_dev_pr_lock_with_git(portfolio_dir=pdir)
                self.assertEqual(sync_after["status"], "ok")
                self.assertTrue(sync_after["active"])
                self.assertTrue(wt_b.is_dir())
                self.assertIn("acme-org/acme-b", prs.get_all_dev_pr_locks(portfolio_dir=pdir))

    def test_single_repo_stale_pr_open_reconciled_via_sync_and_queue_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            bare_a, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-a")

            cid_git = "11111111-1234-4123-8123-111111111111"
            cid_inprog_branched = "22222222-1234-4123-8123-222222222222"
            cid_inprog_plain = "33333333-1234-4123-8123-333333333333"
            cid_inactive_lock = "44444444-1234-4123-8123-444444444444"
            br_git = "fix/20261120-120000-single-a-11111111"
            br_inprog = "fix/20261120-120500-inprog-a-22222222"
            br_lock = "fix/20261120-121000-lock-a-44444444"

            # Create and merge br_git into origin/main on repo-a
            subprocess.run(["git", "-C", str(repo_a), "checkout", "-b", br_git], check=True, capture_output=True)
            (repo_a / "single_a.txt").write_text("single a fix\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_a), "add", "single_a.txt"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "commit", "-m", "fix: single a"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "-u", "origin", br_git], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "checkout", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "merge", "--no-ff", br_git, "-m", f"Merge {br_git}"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "origin", "main"], check=True, capture_output=True)

            compare_url_a = f"https://github.com/acme/repo-a/compare/main...{br_git}?expand=1"
            archival_pr_url = "https://github.com/acme/repo-a/pull/42"

            prs.enqueue_dev_queue_item(
                conversation_id=cid_git,
                title="Stale single-repo pr_open item",
                repo_slugs=["acme/repo-a"],
                branch_names=[br_git],
                pr_urls=[compare_url_a, archival_pr_url],
                status="pr_open",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_inprog_plain,
                title="In-progress item with no branch or PR yet",
                repo_slugs=["acme/repo-a"],
                status="in_progress",
                portfolio_dir=pdir,
            )

            with (
                mock.patch.dict(os.environ, {"AGY_CONF_REPO_DIR": str(repo_a), "AGY_DEV_STATE_DIR": str(pdir)}),
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
            ):
                # 1. sync_dev_pr_lock_with_git automatically reconciles stale pr_open item & prunes compare URL
                sync_res = prs.sync_dev_pr_lock_with_git(portfolio_dir=pdir, workdir=repo_a)
                self.assertEqual(sync_res["status"], "ok")
                self.assertFalse(sync_res["active"])
                self.assertIn("queue_reconciliation", sync_res)
                self.assertEqual(sync_res["queue_reconciliation"]["status"], "ok")
                self.assertEqual(sync_res["queue_reconciliation"]["reconciled_count"], 1)

                q_by_cid = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
                self.assertEqual(q_by_cid[cid_git]["status"], "merged")
                self.assertEqual(q_by_cid[cid_git]["merged_repos"], ["acme/repo-a"])
                self.assertEqual(q_by_cid[cid_git]["pr_urls"], [archival_pr_url])
                self.assertEqual(q_by_cid[cid_inprog_plain]["status"], "in_progress")

                # Re-running sync is idempotent
                sync_again = prs.sync_dev_pr_lock_with_git(portfolio_dir=pdir, workdir=repo_a)
                self.assertEqual(sync_again["queue_reconciliation"]["reconciled_count"], 0)

                # 2. Merge br_inprog on bare_a via a separate clone so local repo_a needs --fetch via queue-sync CLI
                pusher = troot / "pusher-inprog"
                subprocess.run(["git", "clone", f"file://{bare_a}", str(pusher)], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(pusher), "config", "user.name", "Acme Bot"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(pusher), "config", "user.email", "acme-bot@example.com"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(pusher), "checkout", "-b", br_inprog], check=True, capture_output=True)
                (pusher / "inprog.txt").write_text("inprog fix\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(pusher), "add", "inprog.txt"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(pusher), "commit", "-m", "fix: inprog"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(pusher), "push", "-u", "origin", br_inprog], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(pusher), "checkout", "main"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(pusher), "merge", "--no-ff", br_inprog, "-m", f"Merge {br_inprog}"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(pusher), "push", "origin", "main"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(pusher), "push", "origin", "--delete", br_inprog], check=True, capture_output=True)

                prs.enqueue_dev_queue_item(
                    conversation_id=cid_inprog_branched,
                    title="Branched in_progress item merged upstream",
                    repo_slugs=["acme/repo-a"],
                    branch_names=[br_inprog],
                    pr_urls=[f"https://github.com/acme/repo-a/compare/main...{br_inprog}?expand=1"],
                    status="in_progress",
                    portfolio_dir=pdir,
                )

                # Without --fetch, local repo_a origin/main does not yet contain the remote-only merge
                no_fetch_res = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir, workdir=repo_a, fetch=False)
                self.assertEqual(no_fetch_res["reconciled_count"], 0)

                # CLI queue-sync --workdir <repo_a> --fetch discovers the merged & pruned branch via git log
                parsed = prs.parse_args(["queue-sync", "--workdir", str(repo_a), "--fetch"])
                self.assertEqual(parsed.subcommand, "queue-sync")
                self.assertTrue(parsed.fetch)
                with mock.patch("builtins.print"):
                    exit_code = prs.main(["queue-sync", "--workdir", str(repo_a), "--fetch"])
                self.assertEqual(exit_code, 0)

                q_after_cli = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
                self.assertEqual(q_after_cli[cid_inprog_branched]["status"], "merged")
                self.assertEqual(q_after_cli[cid_inprog_branched]["merged_repos"], ["acme/repo-a"])
                self.assertEqual(q_after_cli[cid_inprog_branched]["pr_urls"], [])

                # 3. Evidence (a): Inactive lock history with BRANCH_MERGED_INTO_MAIN reconciles even if CID differed and branch matched
                (pdir / "actions_state.json").write_text(
                    json.dumps(
                        {
                            "actions_log_audit_state": {
                                "dev_pr_locks": {
                                    "acme/repo-a": {
                                        "active": False,
                                        "repo_slug": "acme/repo-a",
                                        "last_release_reason": "BRANCH_MERGED_INTO_MAIN",
                                        "last_conversation_id": "99999999-aaaa-4aaa-8aaa-999999999999",
                                        "last_branch_name": br_lock,
                                    }
                                }
                            }
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                prs.enqueue_dev_queue_item(
                    conversation_id=cid_inactive_lock,
                    title="Reconciled via inactive lock branch history",
                    repo_slugs=["acme/repo-a"],
                    branch_names=[br_lock],
                    pr_urls=[f"https://github.com/acme/repo-a/compare/main...{br_lock}?expand=1"],
                    status="pr_open",
                    portfolio_dir=pdir,
                )
                lock_hist_res = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir, repo_slug="acme/repo-a")
                self.assertEqual(lock_hist_res["reconciled_count"], 1)
                self.assertIn("inactive_lock:BRANCH_MERGED_INTO_MAIN", lock_hist_res["reconciled_items"][0]["evidence"])
                q_final = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
                self.assertEqual(q_final[cid_inactive_lock]["status"], "merged")
                self.assertEqual(q_final[cid_inactive_lock]["pr_urls"], [])

    def test_multi_repo_progressive_reconciliation_and_pr_url_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-a")
            _, repo_b = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-b")

            multi_cid = "55555555-aaaa-4555-8555-555555555555"
            br_a = "fix/20261120-130000-multi-a-55555555"
            br_b = "fix/20261120-130000-multi-b-55555555"
            url_a = f"https://github.com/acme/repo-a/compare/main...{br_a}?expand=1"
            url_b = f"https://github.com/acme/repo-b/compare/main...{br_b}?expand=1"

            # Direct unit check of _prune_merged_repo_pr_urls across VCS providers & substring safety
            url_ab = "https://github.com/acme/repo-ab/compare/main...fix/other?expand=1"
            url_gl_a = "https://gitlab.com/acme/repo-a/-/merge_requests/new?merge_request%5Bsource_branch%5D=fix%2Fa"
            url_bb_a = "https://bitbucket.org/acme/repo-a/pull-requests/new?source=fix%2Fa&dest=main"
            url_archival = "https://github.com/acme/repo-a/pull/99"
            pruned_partial = prs._prune_merged_repo_pr_urls(
                [url_a, url_b, url_ab, url_gl_a, url_bb_a, url_archival],
                "acme/repo-a",
                all_merged=False,
            )
            self.assertEqual(pruned_partial, [url_b, url_ab, url_archival])
            pruned_all = prs._prune_merged_repo_pr_urls(
                [url_a, url_b, url_archival],
                "acme/repo-b",
                all_merged=True,
            )
            self.assertEqual(pruned_all, [url_archival])

            # Repo A: create, push, and merge br_a into origin/main
            subprocess.run(["git", "-C", str(repo_a), "checkout", "-b", br_a], check=True, capture_output=True)
            (repo_a / "a.py").write_text("A = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_a), "add", "a.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "commit", "-m", "feat: repo a"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "-u", "origin", br_a], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "checkout", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "merge", "--no-ff", br_a, "-m", f"Merge {br_a}"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "origin", "main"], check=True, capture_output=True)

            # Repo B: create and push br_b, keep UNMERGED
            subprocess.run(["git", "-C", str(repo_b), "checkout", "-b", br_b], check=True, capture_output=True)
            (repo_b / "b.py").write_text("B = 2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_b), "add", "b.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_b), "commit", "-m", "feat: repo b"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_b), "push", "-u", "origin", br_b], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_b), "checkout", "main"], check=True, capture_output=True)

            prs.enqueue_dev_queue_item(
                conversation_id=multi_cid,
                title="Multi-repo progressive merge",
                repo_slugs=["acme/repo-a", "acme/repo-b"],
                branch_names=[br_a, br_b],
                pr_urls=[url_a, url_b],
                status="pr_open",
                portfolio_dir=pdir,
            )

            with mock.patch.dict(os.environ, {"AGY_CONF_REPO_DIR": str(repo_a)}):
                # Step 1: Repo A merged -> merged_repos=["acme/repo-a"], url_a pruned, url_b kept, status stays pr_open
                step1 = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir)
                self.assertEqual(step1["status"], "ok")
                self.assertEqual(step1["reconciled_count"], 1)
                item1 = prs.load_dev_queue(portfolio_dir=pdir)["items"][0]
                self.assertEqual(item1["status"], "pr_open")
                self.assertEqual(item1["merged_repos"], ["acme/repo-a"])
                self.assertEqual(item1["pr_urls"], [url_b])

                # Idempotent re-run before Repo B merges
                step1_repeat = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir)
                self.assertEqual(step1_repeat["reconciled_count"], 0)

                # Step 2: Merge Repo B into origin/main and delete branch refs -> item promotes to merged & prunes url_b
                subprocess.run(["git", "-C", str(repo_b), "merge", "--no-ff", br_b, "-m", f"Merge {br_b}"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(repo_b), "push", "origin", "main"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(repo_b), "push", "origin", "--delete", br_b], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(repo_b), "branch", "-D", br_b], check=True, capture_output=True)

                step2 = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir)
                self.assertEqual(step2["status"], "ok")
                self.assertEqual(step2["reconciled_count"], 1)
                item2 = prs.load_dev_queue(portfolio_dir=pdir)["items"][0]
                self.assertEqual(item2["status"], "merged")
                self.assertEqual(item2["merged_repos"], ["acme/repo-a", "acme/repo-b"])
                self.assertEqual(item2["pr_urls"], [])

    def test_reconcile_safety_guard_active_locked_and_unmerged_pr_open_never_merged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo_locked = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-locked")
            _, repo_unmerged = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-unmerged")
            _, repo_deleted = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-deleted")

            cid_locked = "66666666-bbbb-4666-8666-666666666666"
            cid_unmerged = "77777777-bbbb-4777-8777-777777777777"
            cid_deleted = "88888888-bbbb-4888-8888-888888888888"
            br_locked = "fix/20261120-131000-locked-66666666"
            br_unmerged = "fix/20261120-131000-unmerged-77777777"
            br_deleted = "fix/20261120-131000-deleted-88888888"

            # repo_locked: merged in git, BUT STILL holds an active dev_pr_lock!
            subprocess.run(["git", "-C", str(repo_locked), "checkout", "-b", br_locked], check=True, capture_output=True)
            (repo_locked / "l.txt").write_text("locked\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_locked), "add", "l.txt"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_locked), "commit", "-m", "fix: locked"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_locked), "push", "-u", "origin", br_locked], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_locked), "checkout", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_locked), "merge", "--no-ff", br_locked, "-m", f"Merge {br_locked}"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_locked), "push", "origin", "main"], check=True, capture_output=True)
            prs.acquire_dev_pr_lock(
                conversation_id=cid_locked,
                action_id="locked",
                branch_name=br_locked,
                summary="Still active",
                proposed_fix="Hold lock",
                target_repo_dir=str(repo_locked),
                repo_slug="acme/repo-locked",
                portfolio_dir=pdir,
            )

            # repo_unmerged: unlocked, pushed commit NOT merged into main
            subprocess.run(["git", "-C", str(repo_unmerged), "checkout", "-b", br_unmerged], check=True, capture_output=True)
            (repo_unmerged / "u.txt").write_text("unmerged\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_unmerged), "add", "u.txt"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_unmerged), "commit", "-m", "fix: unmerged"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_unmerged), "push", "-u", "origin", br_unmerged], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_unmerged), "checkout", "main"], check=True, capture_output=True)

            # repo_deleted: unlocked, branch deleted without merging + non-merge release reason
            subprocess.run(["git", "-C", str(repo_deleted), "checkout", "-b", br_deleted], check=True, capture_output=True)
            (repo_deleted / "d.txt").write_text("rejected\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_deleted), "add", "d.txt"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_deleted), "commit", "-m", "fix: rejected"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_deleted), "checkout", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_deleted), "branch", "-D", br_deleted], check=True, capture_output=True)
            prs.acquire_dev_pr_lock(
                conversation_id=cid_deleted,
                action_id="deleted",
                branch_name=br_deleted,
                summary="Rejected",
                proposed_fix="Deleted",
                target_repo_dir=str(repo_deleted),
                repo_slug="acme/repo-deleted",
                portfolio_dir=pdir,
            )
            with (
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
                mock.patch.object(prs, "restart_pro_api_server", return_value=False),
            ):
                prs.release_dev_pr_lock(
                    reason="REMOTE_BRANCH_DELETED_AFTER_EVALUATION",
                    portfolio_dir=pdir,
                    repo_slug="acme/repo-deleted",
                )

            url_locked = f"https://github.com/acme/repo-locked/compare/main...{br_locked}?expand=1"
            url_unmerged = f"https://github.com/acme/repo-unmerged/compare/main...{br_unmerged}?expand=1"
            url_deleted = f"https://github.com/acme/repo-deleted/compare/main...{br_deleted}?expand=1"

            prs.enqueue_dev_queue_item(
                conversation_id=cid_locked,
                title="Active locked pr_open",
                repo_slugs=["acme/repo-locked"],
                branch_names=[br_locked],
                pr_urls=[url_locked],
                status="pr_open",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_unmerged,
                title="Unlocked unmerged pr_open",
                repo_slugs=["acme/repo-unmerged"],
                branch_names=[br_unmerged],
                pr_urls=[url_unmerged],
                status="pr_open",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_deleted,
                title="Rejected deleted branch pr_open",
                repo_slugs=["acme/repo-deleted"],
                branch_names=[br_deleted],
                pr_urls=[url_deleted],
                status="pr_open",
                portfolio_dir=pdir,
            )

            with mock.patch.dict(os.environ, {"AGY_CONF_REPO_DIR": str(repo_unmerged)}):
                res = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir, fetch=True)
                self.assertEqual(res["status"], "ok")
                self.assertEqual(res["reconciled_count"], 0)
                self.assertEqual(res["reconciled_items"], [])

                by_cid = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
                for cid, expected_url in (
                    (cid_locked, url_locked),
                    (cid_unmerged, url_unmerged),
                    (cid_deleted, url_deleted),
                ):
                    self.assertEqual(by_cid[cid]["status"], "pr_open")
                    self.assertEqual(by_cid[cid]["pr_urls"], [expected_url])
                    self.assertEqual(by_cid[cid].get("merged_repos") or [], [])

    def test_queue_sync_strict_cross_repo_isolation_and_missing_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo_default = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-default")
            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-a")
            _, repo_b = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-b")

            missing_dir = troot / "tmp" / "pr-worktree-acme-missing-fix-20261120-132000-none-12345678"
            plain_non_git = troot / "plain-non-git"
            plain_non_git.mkdir(parents=True, exist_ok=True)

            items_spec = [
                ("aaaaaaaa-cccc-4aaa-8aaa-aaaaaaaaaaaa", "acme/repo-default", repo_default, "fix/20261120-132000-def-aaaaaaaa"),
                ("bbbbbbbb-cccc-4bbb-8bbb-bbbbbbbbbbbb", "acme/repo-a", repo_a, "fix/20261120-132000-a-bbbbbbbb"),
                ("cccccccc-cccc-4ccc-8ccc-cccccccccccc", "acme/repo-b", repo_b, "fix/20261120-132000-b-cccccccc"),
            ]
            urls_by_cid: dict[str, str] = {}
            for cid, slug, rpath, br in items_spec:
                subprocess.run(["git", "-C", str(rpath), "checkout", "-b", br], check=True, capture_output=True)
                (rpath / f"{rpath.name}.txt").write_text("merged\n", encoding="utf-8")
                subprocess.run(["git", "-C", str(rpath), "add", f"{rpath.name}.txt"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(rpath), "commit", "-m", f"feat: {rpath.name}"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(rpath), "push", "-u", "origin", br], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(rpath), "checkout", "main"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(rpath), "merge", "--no-ff", br, "-m", f"Merge {br}"], check=True, capture_output=True)
                subprocess.run(["git", "-C", str(rpath), "push", "origin", "main"], check=True, capture_output=True)
                cmp_url = f"https://github.com/{slug}/compare/main...{br}?expand=1"
                urls_by_cid[cid] = cmp_url
                prs.enqueue_dev_queue_item(
                    conversation_id=cid,
                    title=f"Merged {slug}",
                    repo_slugs=[slug],
                    branch_names=[br],
                    pr_urls=[cmp_url],
                    status="pr_open",
                    portfolio_dir=pdir,
                )

            cid_def, cid_a, cid_b = items_spec[0][0], items_spec[1][0], items_spec[2][0]

            with mock.patch.dict(os.environ, {"AGY_CONF_REPO_DIR": str(repo_default), "AGY_DEV_STATE_DIR": str(pdir)}):
                # 1. Missing or non-git --workdir MUST skip and NEVER fall back to repo-default or touch repo-a/repo-b
                res_missing = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir, workdir=missing_dir)
                self.assertEqual(res_missing["status"], "skipped")
                self.assertEqual(res_missing["reason"], "workdir_not_a_git_repo")
                self.assertEqual(res_missing["reconciled_count"], 0)

                res_nongit = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir, workdir=plain_non_git)
                self.assertEqual(res_nongit["status"], "skipped")
                self.assertEqual(res_nongit["reason"], "workdir_not_a_git_repo")
                self.assertEqual(res_nongit["reconciled_count"], 0)

                by_cid_0 = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
                for cid in (cid_def, cid_a, cid_b):
                    self.assertEqual(by_cid_0[cid]["status"], "pr_open")
                    self.assertEqual(by_cid_0[cid]["pr_urls"], [urls_by_cid[cid]])

                # 2. Scoped `queue-sync --repo acme/repo-a` MUST reconcile ONLY acme/repo-a, leaving repo-b & repo-default untouched
                with mock.patch("builtins.print"):
                    rc = prs.main(["queue-sync", "--repo", "acme/repo-a"])
                self.assertEqual(rc, 0)

                by_cid_1 = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
                self.assertEqual(by_cid_1[cid_a]["status"], "merged")
                self.assertEqual(by_cid_1[cid_a]["merged_repos"], ["acme/repo-a"])
                self.assertEqual(by_cid_1[cid_a]["pr_urls"], [])

                self.assertEqual(by_cid_1[cid_b]["status"], "pr_open")
                self.assertEqual(by_cid_1[cid_b]["pr_urls"], [urls_by_cid[cid_b]])
                self.assertEqual(by_cid_1[cid_def]["status"], "pr_open")
                self.assertEqual(by_cid_1[cid_def]["pr_urls"], [urls_by_cid[cid_def]])

                # 3. Unknown --repo slug never touches default or repo-b
                res_unknown = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir, repo_slug="acme/nonexistent-repo")
                self.assertEqual(res_unknown["status"], "ok")
                self.assertEqual(res_unknown["reconciled_count"], 0)
                by_cid_2 = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
                self.assertEqual(by_cid_2[cid_b]["status"], "pr_open")
                self.assertEqual(by_cid_2[cid_def]["status"], "pr_open")

    def test_reconcile_fetch_false_zero_network_calls_offline_git_guard_and_single_repo_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-a")
            _, repo_ssh = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-ssh")
            subprocess.run(
                ["git", "-C", str(repo_ssh), "remote", "set-url", "origin", "git@github.com:acme/repo-ssh.git"],
                check=True,
                capture_output=True,
            )

            # 1. Verify _is_local_git_remote distinguishes local/file:// remotes from network remotes
            vcs_a = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_a)})
            vcs_ssh = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_ssh)})
            self.assertTrue(vcs_a._is_local_git_remote(repo_a))
            self.assertFalse(vcs_ssh._is_local_git_remote(repo_ssh))

            # 2. Prepare a merged branch whose local refs/heads/<br> is deleted while refs/remotes/origin/<br> remains
            cid_remote_ref = "dddddddd-1111-4ddd-8ddd-dddddddddddd"
            cid_mismatch = "eeeeeeee-1111-4eee-8eee-eeeeeeeeeeee"
            br_remote_ref = "fix/20261120-140000-remote-ref-dddddddd"
            url_remote_ref = f"https://github.com/acme/repo-a/compare/main...{br_remote_ref}?expand=1"
            url_mismatch = "https://github.com/acme/repo-b/compare/main...fix/20261120-140000-b-eeeeeeee?expand=1"

            subprocess.run(["git", "-C", str(repo_a), "checkout", "-b", br_remote_ref], check=True, capture_output=True)
            (repo_a / "remref.txt").write_text("remote tracking ref merged\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_a), "add", "remref.txt"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "commit", "-m", "fix: remote tracking ref"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "-u", "origin", br_remote_ref], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "checkout", "main"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "merge", "--no-ff", br_remote_ref, "-m", f"Merge {br_remote_ref}"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "origin", "main"], check=True, capture_output=True)
            # Delete local branch only; refs/remotes/origin/<br_remote_ref> still exists locally
            subprocess.run(["git", "-C", str(repo_a), "branch", "-D", br_remote_ref], check=True, capture_output=True)

            prs.enqueue_dev_queue_item(
                conversation_id=cid_remote_ref,
                title="Merged branch tracked via origin/<branch>",
                repo_slugs=["acme/repo-a"],
                branch_names=[br_remote_ref],
                pr_urls=[url_remote_ref],
                status="pr_open",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_mismatch,
                title="Single-repo item for acme/repo-b",
                repo_slugs=["acme/repo-b"],
                branch_names=["fix/20261120-140000-b-eeeeeeee"],
                pr_urls=[url_mismatch],
                status="pr_open",
                portfolio_dir=pdir,
            )

            # 3. Spy on subprocess.run and prove fetch=False runs ZERO `git ls-remote` or `git fetch` commands
            real_run = subprocess.run
            recorded_cmds: list[list[str]] = []

            def _spy_run(cmd: list[str] | str, *args: object, **kwargs: object) -> subprocess.CompletedProcess:
                if isinstance(cmd, (list, tuple)):
                    recorded_cmds.append([str(c) for c in cmd])
                return real_run(cmd, *args, **kwargs)

            with (
                mock.patch.dict(os.environ, {"AGY_CONF_REPO_DIR": str(repo_a), "AGY_OFFLINE_ONLY": "1"}),
                mock.patch.object(prs.subprocess, "run", side_effect=_spy_run),
                mock.patch.object(git_client.subprocess, "run", side_effect=_spy_run),
            ):
                recon_res = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir, workdir=repo_a, fetch=False)
                self.assertEqual(recon_res["status"], "ok")
                self.assertEqual(recon_res["reconciled_count"], 1)

                # Also verify AGY_OFFLINE_ONLY=1 skips network fetch/ls-remote on SSH remote without hanging
                ssh_sync = vcs_ssh.sync_and_clean_local_repo(base_branch="main", state_dir=pdir)
                self.assertEqual(ssh_sync["status"], "ok")
                ssh_state = vcs_ssh.inspect_remote_branch_state(branch_name="fix/nonexistent", fetch=True)
                self.assertEqual(ssh_state["status"], "ok")
                self.assertFalse(ssh_state["remote_exists"])

            network_git_cmds = [
                cmd for cmd in recorded_cmds if "ls-remote" in cmd or "fetch" in cmd
            ]
            self.assertEqual(network_git_cmds, [], f"Expected zero network git commands, saw: {network_git_cmds}")

            by_cid = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
            self.assertEqual(by_cid[cid_remote_ref]["status"], "merged")
            self.assertEqual(by_cid[cid_remote_ref]["merged_repos"], ["acme/repo-a"])
            self.assertEqual(by_cid[cid_remote_ref]["pr_urls"], [])

            # 4. Single-repo mismatch guard: releasing acme/repo-a must NEVER mark acme/repo-b merged or prune its URLs
            mismatch_res = prs._mark_queue_item_repo_merged(
                conversation_id=cid_mismatch,
                released_repo_slug="acme/repo-a",
                portfolio_dir=pdir,
            )
            self.assertFalse(mismatch_res["updated"])
            self.assertEqual(mismatch_res["status"], "skipped")
            by_cid_after = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
            self.assertEqual(by_cid_after[cid_mismatch]["status"], "pr_open")
            self.assertEqual(by_cid_after[cid_mismatch]["pr_urls"], [url_mismatch])
            self.assertEqual(by_cid_after[cid_mismatch].get("merged_repos") or [], [])

    def test_local_first_review_hard_gate_iteration_loop_verification_and_worktree_publish(self) -> None:
        """Verify Local-First Review staging, deterministic verification persistence, PASS>90 hard gate, iteration loop, worktree-aware publish, and subsequent commit invalidation."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            bare_a, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-a")
            # Seed tiny deterministic tests + mock qa_static_audit inside repo-a so test-local runs hermetically
            (repo_a / "tests" / "scripts").mkdir(parents=True, exist_ok=True)
            (repo_a / "tests" / "scripts" / "qa_static_audit.py").write_text(
                "#!/usr/bin/env python3\nprint('[qa_static_audit] PASS: 3 files scanned (0 errors, 0 warnings)')\n",
                encoding="utf-8",
            )
            (repo_a / "tests" / "test_acme_unit.py").write_text(
                "import unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok_one(self): self.assertEqual(1 + 1, 2)\n"
                "    def test_ok_two(self): self.assertTrue(True)\n"
                "if __name__ == '__main__': unittest.main()\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo_a), "add", "tests"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "commit", "-m", "test: seed hermetic suite"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "origin", "main"], check=True, capture_output=True)

            vcs_a = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_a)})
            cid = "10101010-2020-4030-8040-101010101010"

            # Verify CLI `prepare-review` alias parses cleanly and `--allow-pre-review-push` does NOT exist
            parsed_prep = prs.parse_args(["prepare-review", "--title", "feat(gate): sample", "--workdir", str(repo_a)])
            self.assertEqual(parsed_prep.subcommand, "prepare-review")
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                prs.parse_args(["push-branch", "--title", "x", "--allow-pre-review-push"])

            # Step 1: Start remediation in isolated worktree & enqueue item with a stale bare compare URL
            start_res = prs.start_remediation_branch_and_lock(
                conversation_id=cid,
                action_id="pr-gate",
                rca_summary="Unreviewed branches leaked compare URLs",
                proposed_fix="Enforce PASS>90 local-first PR readiness gate",
                create_worktree=True,
                branch_timestamp="20261120-151000",
                workdir=repo_a,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertEqual(start_res["status"], "ok")
            self.assertEqual(start_res["manual_pr_url"], "")
            self.assertFalse(start_res["pr_gate_ready"])
            wt_path = pathlib.Path(start_res["worktree_path"])
            branch_name = start_res["branch_name"]

            stale_compare_url = f"https://github.com/acme/repo-a/compare/main...{branch_name}?expand=1"
            prs.enqueue_dev_queue_item(
                conversation_id=cid,
                title="Enforce hard PR readiness gate",
                repo_slugs=["acme/repo-a"],
                branch_names=[branch_name],
                pr_urls=[stale_compare_url],
                status="in_progress",
                portfolio_dir=pdir,
            )

            # Step 2: Edit file in worktree, commit, and run deterministic `test-local`
            (wt_path / "gate_impl.py").write_text("GATE_VERSION = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(wt_path), "add", "gate_impl.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(wt_path), "commit", "-m", "feat: gate v1"], check=True, capture_output=True)
            sha_v1 = _git_rev_parse(wt_path, "HEAD")

            val_res = prs.run_local_validation_suite(
                workdir=wt_path,
                portfolio_dir=pdir,
                pre_commit_status="pass",
                run_bats=False,
            )
            self.assertTrue(val_res["passed"])
            self.assertEqual(val_res["head_sha"], sha_v1)
            vr = val_res["verification_results"]
            self.assertEqual(vr["head_sha"], sha_v1)
            self.assertEqual(vr["qa_static_audit"]["status"], "pass")
            self.assertEqual(vr["qa_static_audit"]["errors"], 0)
            self.assertEqual(vr["unittest"]["passed_count"], 2)
            self.assertEqual(vr["unittest"]["total_count"], 2)
            self.assertEqual(vr["pre_commit"]["status"], "pass")

            # Verify verification_results persisted inside dev_pr_lock
            lock_after_test = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt_path)
            self.assertEqual(lock_after_test["verification_results"]["head_sha"], sha_v1)

            # Step 3: Local-First Review (`push-branch` / `prepare-review`) MUST commit locally WITHOUT pushing to bare origin
            staged = prs.push_remediation_branch_and_build_reviewer_plan(
                title="feat(pr-gate): enforce local-first review",
                executive_summary="Enforce local-first review and hard PASS>90 PR gate.",
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertEqual(staged["status"], "ok")
            self.assertFalse(staged["branch_pushed"])
            self.assertFalse(staged["pr_gate_ready"])
            self.assertTrue(staged["awaiting_agent_review"])
            self.assertEqual(staged["manual_pr_url"], "")
            self.assertEqual(staged["pr_url"], "")
            self.assertTrue(staged["staged_head_sha"])
            self.assertEqual(staged["lock"]["status"], "AWAITING_AGENT_REVIEW")

            # Confirm bare remote has ZERO refs for feature branch before review PASS!
            remote_heads_pre = subprocess.run(
                ["git", "--git-dir", str(bare_a), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(remote_heads_pre, ["main"])

            # Confirm local commit body deterministically includes Automated Verification Checks above Independent PR Review
            local_msg = subprocess.run(
                ["git", "-C", str(wt_path), "log", "-1", "--format=%B"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("## 📋 Executive Summary", local_msg)
            self.assertIn("## 🧪 Automated Verification Checks", local_msg)
            self.assertIn("## 🤖 Independent PR Review", local_msg)
            self.assertLess(local_msg.index("## 🧪 Automated Verification Checks"), local_msg.index("## 🤖 Independent PR Review"))
            self.assertIn("2/2 tests passed", local_msg)

            # Re-seed a stale compare URL on queue item to prove NEEDS_REVISION strips it cleanly
            prs.update_dev_queue_item_status(cid, "in_progress", pr_urls=[stale_compare_url], portfolio_dir=pdir)

            # Step 4: Round 1 NEEDS_REVISION (82) -> NOT pushed, suppresses URL & email, preserves real Executive Summary
            with mock.patch.object(prs.dev_common, "try_send_branch_review_notification") as mock_notify:
                rev1 = prs.record_pr_review_evaluation(
                    branch_name=branch_name,
                    verdict="NEEDS_REVISION",
                    score=82,
                    summary="Reviewer critique: handle edge case when score equals 90.",
                    send_email=True,
                    workdir=wt_path,
                    portfolio_dir=pdir,
                    git_client=vcs_a,
                )
            self.assertEqual(rev1["status"], "ok")
            self.assertFalse(rev1["ready_for_human_pr"])
            self.assertTrue(rev1["needs_reiteration"])
            self.assertFalse(rev1["circuit_breaker_tripped"])
            self.assertFalse(rev1["branch_pushed"])
            self.assertFalse(rev1["pr_gate_ready"])
            self.assertEqual(rev1["manual_pr_url"], "")
            self.assertEqual(rev1["pr_url"], "")
            self.assertEqual(rev1["review_iteration"], 1)
            self.assertFalse(rev1["notification_email_sent"])
            mock_notify.assert_not_called()
            self.assertIn("developer_remediation_plan", rev1)
            self.assertIn("handle edge case", rev1["developer_remediation_plan"]["reviewer_feedback"])

            lock_r1 = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt_path)
            self.assertEqual(lock_r1["status"], "NEEDS_REVISION_ITERATING")
            self.assertEqual(lock_r1["review_iteration"], 1)
            self.assertEqual(lock_r1["summary"], "Unreviewed branches leaked compare URLs")
            self.assertEqual(lock_r1["proposed_fix"], "Enforce PASS>90 local-first PR readiness gate")
            self.assertEqual(lock_r1["executive_summary"], "Enforce local-first review and hard PASS>90 PR gate.")
            q_r1 = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}[cid]
            self.assertEqual(q_r1["status"], "in_progress")
            self.assertEqual(q_r1["pr_urls"], [])

            # Step 5: Boundary check Round 2 PASS with score == 90 STILL fails hard gate (requires > 90, i.e. >= 91)
            with mock.patch.object(prs.dev_common, "try_send_branch_review_notification") as mock_notify_90:
                rev2_boundary = prs.record_pr_review_evaluation(
                    branch_name=branch_name,
                    verdict="PASS",
                    score=90,
                    summary="Borderline score 90 must still iterate.",
                    send_email=True,
                    workdir=wt_path,
                    portfolio_dir=pdir,
                    git_client=vcs_a,
                )
            self.assertFalse(rev2_boundary["ready_for_human_pr"])
            self.assertTrue(rev2_boundary["needs_reiteration"])
            self.assertEqual(rev2_boundary["review_iteration"], 2)
            self.assertEqual(rev2_boundary["manual_pr_url"], "")
            mock_notify_90.assert_not_called()

            # Remote bare repo STILL does not have the branch!
            remote_heads_mid = subprocess.run(
                ["git", "--git-dir", str(bare_a), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(remote_heads_mid, ["main"])

            # Step 6: Developer fixes in worktree, re-runs test-local & prepare-review, then Round 3 PASS (96)
            # Deliberately pass PRIMARY repo dir `repo_a` (checked out on main!) to prove `git worktree list --porcelain` resolution works!
            (wt_path / "gate_impl.py").write_text("GATE_VERSION = 2\nMIN_SCORE = 91\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(wt_path), "add", "gate_impl.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(wt_path), "commit", "-m", "fix: address round 1-2 findings"], check=True, capture_output=True)
            val_res2 = prs.run_local_validation_suite(
                workdir=wt_path,
                portfolio_dir=pdir,
                pre_commit_status="pass",
                run_bats=False,
            )
            self.assertTrue(val_res2["passed"])
            staged2 = prs.push_remediation_branch_and_build_reviewer_plan(
                title="feat(pr-gate): enforce local-first review",
                executive_summary="Enforce local-first review and hard PASS>90 PR gate.",
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertEqual(staged2["status"], "ok")
            self.assertFalse(staged2["branch_pushed"])

            with mock.patch.object(
                prs.dev_common,
                "try_send_branch_review_notification",
                return_value={"sent": True, "transport": "mock"},
            ) as mock_notify_pass:
                rev_pass = prs.record_pr_review_evaluation(
                    branch_name=branch_name,
                    verdict="PASS",
                    score=96,
                    summary="All findings resolved with deterministic tests.",
                    send_email=True,
                    workdir=repo_a,  # Primary repo dir, NOT wt_path -> tests worktree resolution!
                    portfolio_dir=pdir,
                    git_client=vcs_a,
                )
            self.assertEqual(rev_pass["status"], "ok")
            self.assertTrue(rev_pass["ready_for_human_pr"])
            self.assertFalse(rev_pass["needs_reiteration"])
            self.assertTrue(rev_pass["pr_gate_ready"])
            self.assertTrue(rev_pass["branch_pushed"])
            self.assertEqual(rev_pass["review_iteration"], 3)
            self.assertTrue(rev_pass["notification_email_sent"])
            mock_notify_pass.assert_called_once()
            self.assertEqual(mock_notify_pass.call_args.kwargs.get("status"), "OK")
            expected_clean_target_url = f"https://github.com/acme/repo-a/compare/main...{branch_name}?expand=1"
            self.assertEqual(
                mock_notify_pass.call_args.kwargs.get("subject"),
                "PR Ready for Validation [repo-a]: feat(pr-gate): enforce local-first review (PASS 96/100)",
            )
            self.assertEqual(mock_notify_pass.call_args.kwargs.get("target_url"), expected_clean_target_url)
            self.assertEqual(rev_pass["notification_target_url"], expected_clean_target_url)
            self.assertNotIn("title=", rev_pass["notification_target_url"])
            self.assertNotIn("body=", rev_pass["notification_target_url"])

            # Confirm bare remote NOW HAS the branch and its commit carries real Executive Summary + Verification Checks + Round 3/3 Review
            remote_heads_post = subprocess.run(
                ["git", "--git-dir", str(bare_a), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertIn(branch_name, remote_heads_post)
            remote_sha = subprocess.run(
                ["git", "--git-dir", str(bare_a), "rev-parse", f"refs/heads/{branch_name}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(rev_pass["reviewed_head_sha"], remote_sha)
            remote_msg = subprocess.run(
                ["git", "--git-dir", str(bare_a), "log", "-1", "--format=%B", f"refs/heads/{branch_name}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("Enforce local-first review and hard PASS>90 PR gate.", remote_msg)
            self.assertNotIn("Reviewer critique: handle edge case", remote_msg)
            self.assertIn("## 🧪 Automated Verification Checks", remote_msg)
            self.assertIn("2/2 tests passed", remote_msg)
            self.assertIn("## 🤖 Independent PR Review", remote_msg)
            self.assertIn("`3/3`", remote_msg)

            # Confirm lock & queue transitioned to PENDING_HUMAN_PR_VALIDATION / pr_open with rich manual_pr_url
            lock_pass = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt_path)
            self.assertEqual(lock_pass["status"], "PENDING_HUMAN_PR_VALIDATION")
            self.assertTrue(lock_pass["pr_gate_ready"])
            self.assertEqual(lock_pass["reviewed_head_sha"], remote_sha)
            self.assertIn("title=", rev_pass["manual_pr_url"])
            self.assertIn("body=", rev_pass["manual_pr_url"])
            q_pass = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}[cid]
            self.assertEqual(q_pass["status"], "pr_open")
            self.assertEqual(q_pass["pr_urls"], [rev_pass["manual_pr_url"]])

            # Step 7: Subsequent commit + prepare-review invalidates reviewed_head_sha, pr_gate_ready, and manual_pr_url
            (wt_path / "gate_impl.py").write_text("GATE_VERSION = 3\n", encoding="utf-8")
            staged3 = prs.push_remediation_branch_and_build_reviewer_plan(
                title="feat(pr-gate): follow-up tweak",
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertEqual(staged3["status"], "ok")
            self.assertFalse(staged3["pr_gate_ready"])
            self.assertEqual(staged3["manual_pr_url"], "")
            lock_invalidated = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt_path)
            self.assertFalse(lock_invalidated["pr_gate_ready"])
            self.assertEqual(lock_invalidated["reviewed_head_sha"], "")
            self.assertEqual(lock_invalidated["manual_pr_url"], "")

            prs.cleanup_worktree(wt_path, repo_dir=repo_a)

    def test_review_circuit_breaker_round_3_escalates_to_hitl_blocked(self) -> None:
        """Verify 3 consecutive failing review rounds trip the circuit breaker to ESCALATED_MAX_REVIEW_ITERATIONS + hitl_blocked + [🟥] escalation alert without pushing."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            bare_a, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-cb")
            vcs_a = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_a)})
            cid = "20202020-3030-4040-8050-202020202020"

            start_res = prs.start_remediation_branch_and_lock(
                conversation_id=cid,
                action_id="circuit-breaker",
                rca_summary="Flaky parser",
                proposed_fix="Stabilize parser",
                create_worktree=True,
                branch_timestamp="20261120-152000",
                workdir=repo_a,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            wt_path = pathlib.Path(start_res["worktree_path"])
            branch_name = start_res["branch_name"]
            prs.enqueue_dev_queue_item(
                conversation_id=cid,
                title="Circuit breaker test",
                repo_slugs=["acme/repo-cb"],
                branch_names=[branch_name],
                status="in_progress",
                portfolio_dir=pdir,
            )
            (wt_path / "parser.py").write_text("X = 1\n", encoding="utf-8")
            prs.push_remediation_branch_and_build_reviewer_plan(
                title="fix(cb): stabilize parser",
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )

            with mock.patch.object(
                prs.dev_common,
                "try_send_branch_review_notification",
                return_value={"sent": True, "transport": "mock"},
            ) as mock_notify:
                r1 = prs.record_pr_review_evaluation(
                    branch_name=branch_name, verdict="NEEDS_REVISION", score=75, summary="Round 1 fail",
                    send_email=True, workdir=wt_path, portfolio_dir=pdir, git_client=vcs_a,
                )
                r2 = prs.record_pr_review_evaluation(
                    branch_name=branch_name, verdict="WARN", score=88, summary="Round 2 warn",
                    send_email=True, workdir=wt_path, portfolio_dir=pdir, git_client=vcs_a,
                )
                self.assertEqual(mock_notify.call_count, 0)
                self.assertTrue(r1["needs_reiteration"])
                self.assertTrue(r2["needs_reiteration"])

                r3 = prs.record_pr_review_evaluation(
                    branch_name=branch_name, verdict="PASS", score=90, summary="Round 3 still <= 90",
                    send_email=True, workdir=wt_path, portfolio_dir=pdir, git_client=vcs_a,
                )

            self.assertEqual(r3["status"], "ok")
            self.assertFalse(r3["ready_for_human_pr"])
            self.assertFalse(r3["needs_reiteration"])
            self.assertTrue(r3["circuit_breaker_tripped"])
            self.assertFalse(r3["pr_gate_ready"])
            self.assertFalse(r3["branch_pushed"])
            self.assertEqual(r3["manual_pr_url"], "")
            self.assertEqual(r3["review_iteration"], 3)
            self.assertEqual(len(r3["review_history"]), 3)
            self.assertTrue(r3["notification_email_sent"])
            mock_notify.assert_called_once()
            self.assertEqual(mock_notify.call_args.kwargs.get("status"), "KO")
            self.assertEqual(mock_notify.call_args.kwargs.get("target_url"), "")

            lock_cb = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt_path)
            self.assertEqual(lock_cb["status"], "ESCALATED_MAX_REVIEW_ITERATIONS")
            self.assertFalse(lock_cb["pr_gate_ready"])
            self.assertFalse(lock_cb["branch_pushed"])
            self.assertEqual(lock_cb["manual_pr_url"], "")
            q_cb = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}[cid]
            self.assertEqual(q_cb["status"], "hitl_blocked")
            self.assertEqual(q_cb["pr_urls"], [])

            remote_heads = subprocess.run(
                ["git", "--git-dir", str(bare_a), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(remote_heads, ["main"])
            prs.cleanup_worktree(wt_path, repo_dir=repo_a)

    def test_record_review_non_silent_amend_and_push_failure(self) -> None:
        """Verify PASS review fails loudly with status='error' when amend or push fails instead of silently exposing PR URLs."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-fail")
            vcs_a = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_a)})
            cid = "30303030-4040-4050-8060-303030303030"

            start_res = prs.start_remediation_branch_and_lock(
                conversation_id=cid,
                action_id="fail-loud",
                rca_summary="Silent amend/push failure",
                proposed_fix="Fail loudly on amend or push error",
                create_worktree=True,
                branch_timestamp="20261120-153000",
                workdir=repo_a,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            wt_path = pathlib.Path(start_res["worktree_path"])
            branch_name = start_res["branch_name"]

            # 1. With 0 commits ahead of main, amend MUST fail loudly
            with mock.patch.object(prs.dev_common, "try_send_branch_review_notification") as mock_notify_fail:
                amend_fail = prs.record_pr_review_evaluation(
                    branch_name=branch_name,
                    verdict="PASS",
                    score=97,
                    summary="Should fail because nothing to amend",
                    send_email=True,
                    workdir=wt_path,
                    portfolio_dir=pdir,
                    git_client=vcs_a,
                )
            self.assertEqual(amend_fail["status"], "error")
            self.assertEqual(amend_fail["step"], "amend_commit_message")
            self.assertFalse(amend_fail["ready_for_human_pr"])
            self.assertFalse(amend_fail["pr_gate_ready"])
            self.assertEqual(amend_fail["manual_pr_url"], "")
            mock_notify_fail.assert_not_called()

            # 2. With a real commit, if vcs.push_branch fails, record-review MUST fail loudly without unlocking or sending email
            (wt_path / "change.py").write_text("OK = True\n", encoding="utf-8")
            prs.push_remediation_branch_and_build_reviewer_plan(
                title="fix(fail-loud): commit before push failure",
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            with (
                mock.patch.object(
                    vcs_a,
                    "push_branch",
                    return_value={"status": "error", "error": "remote rejected non-fast-forward"},
                ),
                mock.patch.object(prs.dev_common, "try_send_branch_review_notification") as mock_notify_push_fail,
            ):
                push_fail = prs.record_pr_review_evaluation(
                    branch_name=branch_name,
                    verdict="PASS",
                    score=97,
                    summary="Push failure must be surfaced loudly",
                    send_email=True,
                    workdir=wt_path,
                    portfolio_dir=pdir,
                    git_client=vcs_a,
                )
            self.assertEqual(push_fail["status"], "error")
            self.assertEqual(push_fail["step"], "push_branch")
            self.assertFalse(push_fail["ready_for_human_pr"])
            self.assertFalse(push_fail["pr_gate_ready"])
            self.assertFalse(push_fail["branch_pushed"])
            self.assertEqual(push_fail["manual_pr_url"], "")
            mock_notify_push_fail.assert_not_called()
            lock_after = prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt_path)
            self.assertEqual(lock_after["status"], "AWAITING_AGENT_REVIEW")
            self.assertFalse(lock_after["pr_gate_ready"])
            prs.cleanup_worktree(wt_path, repo_dir=repo_a)

    def test_rich_pr_urls_deduplication_and_evaluate_branch_approve_guard(self) -> None:
        """Verify rich pr_urls deduplication priority (/pull/ > verified PASS compare > bare ?expand=1) and evaluate-branch approve guard + --force."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True, exist_ok=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True, exist_ok=True)

            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-dedup")
            vcs_a = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_a)})

            cid_q = "40404040-5050-4060-8070-404040404040"
            br_a = "fix/20261120-154000-dedup-40404040"
            br_b = "fix/20261120-154000-dedup-b-40404040"
            bare_a = f"https://github.com/acme/repo-dedup/compare/main...{br_a}?expand=1"
            plain_tb_a = f"https://github.com/acme/repo-dedup/compare/main...{br_a}?expand=1&title=fix&body=Summary"
            rich_pass_a = (
                f"https://github.com/acme/repo-dedup/compare/main...{br_a}"
                "?expand=1&title=fix%28dedup%29&body=%23%23+Independent+PR+Review+PASS+96%2F100"
            )
            pull_a = "https://github.com/acme/repo-dedup/pull/77"
            rich_b = (
                f"https://github.com/acme/repo-other/compare/main...{br_b}"
                "?expand=1&title=fix%28b%29&body=Verdict+PASS"
            )

            # 1. Enqueue with bare + plain + rich PASS compare URL -> keeps ONLY richest verified PASS URL
            prs.enqueue_dev_queue_item(
                conversation_id=cid_q,
                title="Rich PR URL dedup test",
                repo_slugs=["acme/repo-dedup", "acme/repo-other"],
                branch_names=[br_a, br_b],
                pr_urls=[bare_a, plain_tb_a, rich_pass_a, rich_b],
                status="pr_open",
                portfolio_dir=pdir,
            )
            it1 = prs.load_dev_queue(portfolio_dir=pdir)["items"][0]
            self.assertEqual(it1["pr_urls"], [rich_pass_a, rich_b])

            # 2. Trailing queue-set-status with bare ?expand=1 MUST NOT bury or duplicate the rich PASS compare URL
            upd_bare = prs.update_dev_queue_item_status(
                conversation_id=cid_q,
                status="pr_open",
                pr_urls=[bare_a],
                portfolio_dir=pdir,
            )["item"]
            self.assertEqual(upd_bare["pr_urls"], [rich_pass_a, rich_b])

            # 3. Adding created /pull/77 supersedes compare URL for acme/repo-dedup while preserving acme/repo-other
            upd_pull = prs.update_dev_queue_item_status(
                conversation_id=cid_q,
                status="pr_open",
                pr_urls=[pull_a],
                portfolio_dir=pdir,
            )["item"]
            self.assertEqual(upd_pull["pr_urls"], [rich_b, pull_a])

            # 4. evaluate-branch --decision approve guard & --force escape hatch
            start_res = prs.start_remediation_branch_and_lock(
                conversation_id=cid_q,
                action_id="approve-guard",
                rca_summary="Unreviewed approve bypass",
                proposed_fix="Enforce pr_gate_ready and reviewed_head_sha",
                create_worktree=True,
                branch_timestamp="20261120-154500",
                workdir=repo_a,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            wt_path = pathlib.Path(start_res["worktree_path"])
            guard_br = start_res["branch_name"]
            (wt_path / "guard.py").write_text("SAFE = 1\n", encoding="utf-8")
            prs.push_remediation_branch_and_build_reviewer_plan(
                title="fix(guard): enforce approve gate",
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )

            # 4a. Approve blocked while pr_gate_ready == False
            blocked_unready = prs.evaluate_branch_and_release_lock(
                decision="approve",
                comment="Attempt premature approve",
                branch_name=guard_br,
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertEqual(blocked_unready["status"], "error")
            self.assertEqual(blocked_unready["decision"], "blocked_pr_gate_not_ready")
            self.assertFalse(blocked_unready["lock_released"])
            self.assertTrue(prs.get_dev_pr_lock(portfolio_dir=pdir, workdir=wt_path)["active"])

            # 4b. Pass review -> pr_gate_ready == True, then add an unreviewed commit -> approve blocked by SHA mismatch!
            rev_ok = prs.record_pr_review_evaluation(
                branch_name=guard_br,
                verdict="PASS",
                score=96,
                summary="Looks great",
                send_email=False,
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertTrue(rev_ok["ready_for_human_pr"])
            (wt_path / "guard.py").write_text("SAFE = 2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(wt_path), "add", "guard.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(wt_path), "commit", "-m", "chore: unreviewed follow-up"], check=True, capture_output=True)

            blocked_sha = prs.evaluate_branch_and_release_lock(
                decision="approve",
                comment="Approve after unreviewed commit",
                branch_name=guard_br,
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertEqual(blocked_sha["status"], "error")
            self.assertEqual(blocked_sha["decision"], "blocked_unreviewed_commits")
            self.assertFalse(blocked_sha["lock_released"])

            # 4c. CLI `--force` flag parses and overrides approve guard when explicitly requested
            parsed_force = prs.parse_args(
                ["evaluate-branch", "--branch", guard_br, "--decision", "approve", "--comment", "override", "--force"]
            )
            self.assertTrue(parsed_force.force)
            with (
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
                mock.patch.object(prs, "restart_pro_api_server", return_value=False),
            ):
                forced_ok = prs.evaluate_branch_and_release_lock(
                    decision="approve",
                    comment="Emergency HITL override",
                    branch_name=guard_br,
                    workdir=wt_path,
                    portfolio_dir=pdir,
                    git_client=vcs_a,
                    force=True,
                )
            self.assertEqual(forced_ok["status"], "ok")
            self.assertTrue(forced_ok["lock_released"])

            # 4d. Reject ALWAYS works even when pr_gate_ready == False
            start_rej = prs.start_remediation_branch_and_lock(
                conversation_id="50505050-6060-4070-8080-505050505050",
                action_id="reject-unready",
                rca_summary="Discard",
                proposed_fix="Discard",
                create_worktree=True,
                branch_timestamp="20261120-155000",
                workdir=repo_a,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            with (
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
                mock.patch.object(prs, "restart_pro_api_server", return_value=False),
            ):
                rej_ok = prs.evaluate_branch_and_release_lock(
                    decision="reject",
                    comment="Reject unreviewed branch",
                    branch_name=start_rej["branch_name"],
                    workdir=start_rej["worktree_path"],
                    portfolio_dir=pdir,
                    git_client=vcs_a,
                )
            self.assertEqual(rej_ok["status"], "ok")
            self.assertTrue(rej_ok["lock_released"])

    def test_detect_stalled_lock_and_lock_heartbeat_cli(self) -> None:
        """Verify stalled-worker detection across timestamps/worktree/transcript and `lock-heartbeat` CLI."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True)
            brain_dir = troot / "brain"
            convs_dir = troot / "convs"
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True)

            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-a")
            vcs_a = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_a)})

            cid = "71717171-1111-4111-8111-717171717171"
            worker_cid = "82828282-2222-4222-8222-828282828282"
            start_res = prs.start_remediation_branch_and_lock(
                conversation_id=cid,
                action_id="stall-check",
                rca_summary="Test stall detection",
                proposed_fix="Track worker activity",
                create_worktree=True,
                branch_timestamp="20261121-080000",
                workdir=repo_a,
                portfolio_dir=pdir,
                git_client=vcs_a,
                worker_cid=worker_cid,
            )
            self.assertEqual(start_res["status"], "ok")
            wt_path = pathlib.Path(start_res["worktree_path"])
            lock_obj = start_res["lock"]
            self.assertEqual(lock_obj["worker_cid"], worker_cid)
            self.assertTrue(lock_obj["last_activity_at"])

            # Anchor deterministic clock and backdate worktree + lock timestamps by 1h42m (6120s)
            stale_iso = "2027-01-15T06:18:00Z"  # exact 6120s before 2027-01-15T08:00:00Z
            base_now = prs._flow_opt.parse_iso_timestamp("2027-01-15T08:00:00Z")
            stale_ts = base_now - 6120.0
            for root_d, dirnames, filenames in os.walk(wt_path):
                dirnames[:] = [d for d in dirnames if d != ".git"]
                for fn in filenames:
                    os.utime(pathlib.Path(root_d) / fn, (stale_ts, stale_ts))

            stale_lock = {
                **lock_obj,
                "status": "DEVELOPING",
                "pr_gate_ready": False,
                "acquired_at": stale_iso,
                "updated_at": stale_iso,
                "last_activity_at": stale_iso,
            }
            det_stale = prs.detect_stalled_lock(stale_lock, now_ts=base_now, brain_dir=brain_dir)
            self.assertTrue(det_stale["stalled"])
            self.assertEqual(det_stale["idle_seconds"], 6120)
            self.assertEqual(det_stale["idle_label"], "1h42m")
            self.assertEqual(det_stale["threshold_seconds"], 900)
            self.assertEqual(det_stale["worker_cid"], worker_cid)

            # NEEDS_REVISION_ITERATING and AWAITING_AGENT_REVIEW are also eligible for stall detection
            for eligible_status in ("NEEDS_REVISION_ITERATING", "AWAITING_AGENT_REVIEW"):
                det_iter = prs.detect_stalled_lock(
                    {**stale_lock, "status": eligible_status},
                    now_ts=base_now,
                    brain_dir=brain_dir,
                )
                self.assertTrue(det_iter["stalled"], msg=f"Expected {eligible_status} to be stall-eligible")

            # Human-ready or non-developing/non-review locks are NEVER flagged as stalled
            for exempt in (
                {**stale_lock, "status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True},
                {**stale_lock, "status": "READY_AWAITING_BASE_MERGE", "pr_gate_ready": False},
                {**stale_lock, "status": "DEVELOPING", "pr_gate_ready": True},
                {**stale_lock, "active": False},
            ):
                self.assertFalse(prs.detect_stalled_lock(exempt, now_ts=base_now, brain_dir=brain_dir)["stalled"])

            # Fresh worker transcript mtime clears stall even when lock/worktree timestamps are stale
            _write_fake_conversation(brain_dir, convs_dir, worker_cid, mtime=base_now - 120.0)
            det_transcript_ok = prs.detect_stalled_lock(stale_lock, now_ts=base_now, brain_dir=brain_dir)
            self.assertFalse(det_transcript_ok["stalled"])
            self.assertEqual(det_transcript_ok["idle_label"], "2m")

            # Backdate transcript too, then touch a worktree file -> worktree activity clears stall
            _write_fake_conversation(brain_dir, convs_dir, worker_cid, mtime=stale_ts)
            self.assertTrue(prs.detect_stalled_lock(stale_lock, now_ts=base_now, brain_dir=brain_dir)["stalled"])
            (wt_path / "active_edit.py").write_text("X = 1\n", encoding="utf-8")
            os.utime(wt_path / "active_edit.py", (base_now - 60.0, base_now - 60.0))
            self.assertFalse(prs.detect_stalled_lock(stale_lock, now_ts=base_now, brain_dir=brain_dir)["stalled"])

            # Env override AGY_DEV_STALL_THRESHOLD_SECONDS respected
            with mock.patch.dict(os.environ, {"AGY_DEV_STALL_THRESHOLD_SECONDS": "30"}):
                det_env = prs.detect_stalled_lock(stale_lock, now_ts=base_now, brain_dir=brain_dir)
                self.assertTrue(det_env["stalled"])
                self.assertEqual(det_env["threshold_seconds"], 30)

            # CLI `lock-heartbeat` & `start-remediation --worker-cid` argument parsing + heartbeat stamping
            parsed_start = prs.parse_args(
                ["start-remediation", "--id", cid, "--worker-cid", worker_cid, "--worktree"]
            )
            self.assertEqual(parsed_start.worker_cid, worker_cid)
            parsed_hb = prs.parse_args(
                ["lock-heartbeat", "--workdir", str(wt_path), "--worker-cid", worker_cid, "--stage", "testing"]
            )
            self.assertEqual(parsed_hb.subcommand, "lock-heartbeat")
            self.assertEqual(parsed_hb.stage_label, "testing")

            hb_res = prs.record_dev_pr_lock_heartbeat(
                workdir=wt_path,
                worker_cid="93939393-3333-4333-8333-939393939393",
                stage_label="testing",
                portfolio_dir=pdir,
            )
            self.assertEqual(hb_res["status"], "ok")
            self.assertEqual(hb_res["lock"]["worker_cid"], "93939393-3333-4333-8333-939393939393")
            self.assertEqual(hb_res["lock"]["stage_label"], "testing")
            self.assertTrue(hb_res["last_activity_at"])

    def test_branch_diff_verification_gate_and_single_pass_test_local_with_pre_commit(self) -> None:
        """Verify `test-local --with-pre-commit` single-pass SKIP & `branch-diff` embedded `verification_gate`."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True)

            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-a")
            (repo_a / ".pre-commit-config.yaml").write_text(
                "repos:\n  - repo: local\n    hooks:\n      - id: unit-tests\n        entry: python3 -m unittest\n",
                encoding="utf-8",
            )
            (repo_a / "tests" / "scripts").mkdir(parents=True)
            (repo_a / "tests" / "scripts" / "qa_static_audit.py").write_text(
                "#!/usr/bin/env python3\nprint('[qa_static_audit] PASS: 2 files scanned (0 errors, 0 warnings)')\n",
                encoding="utf-8",
            )
            (repo_a / "tests" / "test_acme_fast.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(2, 2)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo_a), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "commit", "-m", "chore: seed suite"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "origin", "main"], check=True, capture_output=True)

            vcs_a = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_a)})
            cid = "74747474-4444-4444-8444-747474747474"
            start_res = prs.start_remediation_branch_and_lock(
                conversation_id=cid,
                action_id="single-pass",
                rca_summary="Duplicate test runs",
                proposed_fix="Single-pass test-local --with-pre-commit",
                create_worktree=True,
                branch_timestamp="20261121-081500",
                workdir=repo_a,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            wt_path = pathlib.Path(start_res["worktree_path"])
            branch_name = start_res["branch_name"]
            (wt_path / "feature.py").write_text("ENABLED = True\n", encoding="utf-8")

            # CLI flags `--with-pre-commit`, `--run-pre-commit`, and uppercase `--pre-commit-status PASSED`
            self.assertTrue(prs.parse_args(["test-local", "--with-pre-commit"]).run_pre_commit)
            self.assertTrue(prs.parse_args(["test-local", "--run-pre-commit"]).run_pre_commit)
            self.assertEqual(prs.parse_args(["test-local", "--pre-commit-status", "PASSED"]).pre_commit_status, "pass")

            # Single-pass run executes pre-commit FIRST with SKIP=unit-tests,qa-static-audit, then QA + unittest once
            call_log: list[tuple[list[str], str]] = []
            real_subproc_run = subprocess.run

            def _intercept_run(cmd, *args, **kwargs):
                cmd_list = [str(c) for c in cmd]
                env_used = kwargs.get("env") or {}
                call_log.append((cmd_list, str(env_used.get("SKIP") or "")))
                if cmd_list and cmd_list[0].endswith("pre-commit"):
                    return subprocess.CompletedProcess(cmd_list, 0, stdout="check-ast...Passed\n", stderr="")
                return real_subproc_run(cmd, *args, **kwargs)

            with (
                mock.patch.object(prs.shutil, "which", side_effect=lambda b: "/usr/local/bin/pre-commit" if b == "pre-commit" else None),
                mock.patch.object(prs.subprocess, "run", side_effect=_intercept_run),
            ):
                val_res = prs.run_local_validation_suite(
                    workdir=wt_path,
                    portfolio_dir=pdir,
                    run_pre_commit=True,
                    run_bats=False,
                )

            self.assertTrue(val_res["passed"])
            self.assertEqual(val_res["pre_commit_status"], "PASSED")
            self.assertIn("check-ast...Passed", val_res["pre_commit_output"])
            self.assertEqual(val_res["verification_results"]["pre_commit"]["status"], "pass")
            self.assertEqual(val_res["verification_results"]["pre_commit_status"], "PASSED")

            pc_calls = [(c, sk) for c, sk in call_log if c and c[0].endswith("pre-commit")]
            qa_calls = [c for c, _ in call_log if any("qa_static_audit.py" in tok for tok in c)]
            ut_calls = [c for c, _ in call_log if "-m" in c and "unittest" in c]
            self.assertEqual(len(pc_calls), 1)
            self.assertEqual(pc_calls[0][0], ["/usr/local/bin/pre-commit", "run", "--files", "feature.py"])
            self.assertEqual(pc_calls[0][1], "unit-tests,unit-tests-fast,qa-static-audit,bats-suite")
            self.assertEqual(len(qa_calls), 1)
            self.assertIn("--incremental", qa_calls[0])
            self.assertEqual(len(ut_calls), 1)
            # Pre-commit ran before qa_static_audit and unittest
            pc_idx = next(i for i, (c, _) in enumerate(call_log) if c and c[0].endswith("pre-commit"))
            qa_idx = next(i for i, (c, _) in enumerate(call_log) if any("qa_static_audit.py" in tok for tok in c))
            self.assertLess(pc_idx, qa_idx)

            # Stage locally via prepare-review so verification_results binds cleanly to staged commit SHA
            staged = prs.push_remediation_branch_and_build_reviewer_plan(
                title="feat(single-pass): add feature",
                executive_summary="Add feature and verify single-pass gate.",
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertEqual(staged["status"], "ok")
            staged_sha = staged["staged_head_sha"]

            # `branch-diff` embeds `verification_gate` with `skip_redundant_test_rerun == True`
            diff_res = prs.get_branch_diff_with_verification_gate(
                branch=branch_name,
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertEqual(diff_res["status"], "ok")
            vgate = diff_res["verification_gate"]
            self.assertEqual(vgate["head_sha"], staged_sha)
            self.assertEqual(vgate["verified_head_sha"], staged_sha)
            self.assertTrue(vgate["matches_head"])
            self.assertTrue(vgate["passed"])
            self.assertEqual(vgate["pre_commit_status"], "PASSED")
            self.assertTrue(vgate["qa_passed"])
            self.assertTrue(vgate["tests_passed"])
            self.assertTrue(vgate["bats_passed"])
            self.assertTrue(vgate["skip_redundant_test_rerun"])

            # A subsequent unverified commit invalidates `matches_head` and `skip_redundant_test_rerun`
            (wt_path / "feature.py").write_text("ENABLED = False\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(wt_path), "add", "feature.py"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(wt_path), "commit", "-m", "chore: unverified edit"], check=True, capture_output=True)
            diff_stale = prs.get_branch_diff_with_verification_gate(
                branch=branch_name,
                workdir=wt_path,
                portfolio_dir=pdir,
                git_client=vcs_a,
            )
            self.assertFalse(diff_stale["verification_gate"]["matches_head"])
            self.assertFalse(diff_stale["verification_gate"]["skip_redundant_test_rerun"])

            # Failed pre-commit marks `pre_commit_status == "FAILED"` and `passed == False`
            def _intercept_fail(cmd, *args, **kwargs):
                cmd_list = [str(c) for c in cmd]
                if cmd_list and cmd_list[0].endswith("pre-commit"):
                    return subprocess.CompletedProcess(cmd_list, 1, stdout="trailing-whitespace...Failed\n", stderr="")
                return real_subproc_run(cmd, *args, **kwargs)

            with (
                mock.patch.object(prs.shutil, "which", side_effect=lambda b: "/usr/local/bin/pre-commit" if b == "pre-commit" else None),
                mock.patch.object(prs.subprocess, "run", side_effect=_intercept_fail),
            ):
                fail_res = prs.run_local_validation_suite(
                    workdir=wt_path,
                    portfolio_dir=pdir,
                    run_pre_commit=True,
                    run_bats=False,
                )
            self.assertFalse(fail_res["passed"])
            self.assertEqual(fail_res["pre_commit_status"], "FAILED")
            self.assertIn("trailing-whitespace...Failed", fail_res["pre_commit_output"])

    def test_test_local_pre_commit_indexes_untracked_files_and_snapshot_tracks_exec_bit(self) -> None:
        """Verify `test-local --with-pre-commit` temporarily indexes untracked files and `_snapshot_worktree_state` hashes exec bits."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True)

            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-untracked")
            (repo_a / ".pre-commit-config.yaml").write_text(
                "repos:\n  - repo: local\n    hooks:\n      - id: unit-tests\n        entry: python3 -m unittest\n",
                encoding="utf-8",
            )
            (repo_a / "tests").mkdir(exist_ok=True)
            (repo_a / "tests" / "test_smoke.py").write_text(
                "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo_a), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "commit", "-m", "chore: init"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "origin", "main"], check=True, capture_output=True)

            # Pre-existing staged file must remain staged after cleanup
            staged_keep = repo_a / "staged_keep.py"
            staged_keep.write_text("KEEP = True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo_a), "add", "staged_keep.py"], check=True, capture_output=True)

            # Untracked script with shebang starts non-executable (0644)
            new_script = repo_a / "new_tool.py"
            new_script.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
            os.chmod(new_script, 0o644)

            snap_644 = prs._snapshot_worktree_state(repo_a)
            os.chmod(new_script, 0o755)
            snap_755 = prs._snapshot_worktree_state(repo_a)
            self.assertNotEqual(
                snap_644["content_fingerprint"],
                snap_755["content_fingerprint"],
                "_snapshot_worktree_state must include executable mode bits for untracked files",
            )
            os.chmod(new_script, 0o644)

            observed_stage_lines: list[str] = []
            real_subproc_run = subprocess.run

            def _intercept_check_stage(cmd, *args, **kwargs):
                cmd_list = [str(c) for c in cmd]
                if cmd_list and cmd_list[0].endswith("pre-commit"):
                    stage = real_subproc_run(
                        ["git", "-C", str(repo_a), "ls-files", "--stage", "--", "new_tool.py"],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    stage_out = stage.stdout.strip()
                    observed_stage_lines.append(stage_out)
                    is_exec = stage_out.startswith("100755 ")
                    return subprocess.CompletedProcess(
                        cmd_list,
                        0 if is_exec else 1,
                        stdout="check-shebang...Passed\n" if is_exec else "new_tool.py: not executable\n",
                        stderr="",
                    )
                return real_subproc_run(cmd, *args, **kwargs)

            with (
                mock.patch.object(prs.shutil, "which", side_effect=lambda b: "/usr/local/bin/pre-commit" if b == "pre-commit" else None),
                mock.patch.object(prs.subprocess, "run", side_effect=_intercept_check_stage),
            ):
                res_nonexec = prs.run_local_validation_suite(
                    workdir=repo_a,
                    portfolio_dir=pdir,
                    run_pre_commit=True,
                    run_bats=False,
                )
            self.assertFalse(res_nonexec["passed"])
            self.assertEqual(res_nonexec["pre_commit_status"], "FAILED")
            self.assertTrue(observed_stage_lines and observed_stage_lines[-1].startswith("100644 "))

            # Untracked file must be unstaged after failure while staged_keep.py stays staged and on disk
            porcelain_after_fail = subprocess.run(
                ["git", "-C", str(repo_a), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertIn("A  staged_keep.py", porcelain_after_fail)
            self.assertIn("?? new_tool.py", porcelain_after_fail)
            self.assertTrue(new_script.exists())

            # After chmod +x, re-running sees 100755 in git index and passes, still cleaning up index
            os.chmod(new_script, 0o755)
            with (
                mock.patch.object(prs.shutil, "which", side_effect=lambda b: "/usr/local/bin/pre-commit" if b == "pre-commit" else None),
                mock.patch.object(prs.subprocess, "run", side_effect=_intercept_check_stage),
            ):
                res_exec = prs.run_local_validation_suite(
                    workdir=repo_a,
                    portfolio_dir=pdir,
                    run_pre_commit=True,
                    run_bats=False,
                )
            self.assertTrue(res_exec["passed"])
            self.assertEqual(res_exec["pre_commit_status"], "PASSED")
            self.assertTrue(observed_stage_lines[-1].startswith("100755 "))
            porcelain_after_pass = subprocess.run(
                ["git", "-C", str(repo_a), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertIn("A  staged_keep.py", porcelain_after_pass)
            self.assertIn("?? new_tool.py", porcelain_after_pass)

    def test_analyze_dev_flow_optimizations_all_seven_detectors_and_cli(self) -> None:
        """Verify all 7 deterministic 0-LLM detectors in `analyze_dev_flow_optimizations` and `queue-optimize` CLI."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True)
            repos_root = troot / "repos"
            repos_root.mkdir(parents=True)
            brain_dir = troot / "brain"
            convs_dir = troot / "convs"

            # 0. Clean empty state -> health_score == 100, zero findings, missing brain_dir gracefully skipped
            clean_rep = prs.analyze_dev_flow_optimizations(
                portfolio_dir=pdir,
                repos_dir=repos_root,
                brain_dir=troot / "nonexistent-brain",
            )
            self.assertEqual(clean_rep["status"], "ok")
            self.assertEqual(clean_rep["health_score"], 100)
            self.assertEqual(clean_rep["findings"], [])
            self.assertEqual(clean_rep["summary"]["scanned_transcripts"], 0)
            self.assertEqual(clean_rep["top_hint"], "")

            # Seed synthetic managed repo-a (monolithic god files + missing selective test runner)
            repo_a = repos_root / "repo-a"
            (repo_a / ".git").mkdir(parents=True)
            (repo_a / ".pre-commit-config.yaml").write_text(
                "repos:\n"
                "  - repo: local\n"
                "    hooks:\n"
                "      - id: qa-static-audit\n"
                "        entry: python3 ./tests/scripts/qa_static_audit.py --strict\n"
                "      - id: unit-tests\n"
                "        entry: python3 -m unittest discover -s tests -p 'test_*.py'\n",
                encoding="utf-8",
            )
            (repo_a / "web").mkdir(parents=True)
            (repo_a / "web" / "big_ui.js").write_text("const x = 1;\n" * 5100, encoding="utf-8")
            (repo_a / "core_monolith.py").write_text("VALUE = 1\n" * 5050, encoding="utf-8")

            # Seed stale worktree + active DEVELOPING lock (Detector 1: stalled_worker)
            now_ts = prs._flow_opt.parse_iso_timestamp("2027-01-15T10:00:00Z")
            stale_iso = "2027-01-15T08:18:00Z"  # 1h42m idle
            stale_wt = troot / "wt-stale"
            stale_wt.mkdir(parents=True)
            (stale_wt / "patch.py").write_text("A = 1\n", encoding="utf-8")
            os.utime(stale_wt / "patch.py", (now_ts - 6120.0, now_ts - 6120.0))

            (pdir / "actions_state.json").write_text(
                json.dumps(
                    {
                        "actions_log_audit_state": {
                            "dev_pr_locks": {
                                "acme/repo-a": {
                                    "active": True,
                                    "repo_slug": "acme/repo-a",
                                    "status": "DEVELOPING",
                                    "pr_gate_ready": False,
                                    "conversation_id": "aaaa1111-1111-4111-8111-111111111111",
                                    "branch_name": "fix/20270115-081800-stall-aaaa1111",
                                    "worktree_path": str(stale_wt),
                                    "target_repo_dir": str(repo_a),
                                    "acquired_at": stale_iso,
                                    "updated_at": stale_iso,
                                    "last_activity_at": stale_iso,
                                }
                            }
                        }
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            # Seed dev_queue.json triggering Detectors 2 (micro_pr_batch_candidates), 3 (repo_contention_hotspot), 4 (unanchored_pending_tasks)
            orch_cid = "99990000-1111-4111-8111-999900000001"
            (pdir / "dev_queue.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "orchestrator_conversation_id": orch_cid,
                        "updated_at": stale_iso,
                        "items": [
                            {
                                "conversation_id": "aaaa1111-1111-4111-8111-111111111111",
                                "title": "Active stalled work",
                                "priority": 10,
                                "status": "in_progress",
                                "repo_slugs": ["acme/repo-a"],
                                "target_files": ["web/big_ui.js"],
                            },
                            {
                                "conversation_id": "bbbb2222-1111-4111-8111-111111111111",
                                "title": "UI badge alignment polish",
                                "priority": 30,
                                "status": "pending",
                                "repo_slugs": ["acme/repo-a"],
                                "target_files": ["web/big_ui.js"],
                            },
                            {
                                "conversation_id": "cccc3333-1111-4111-8111-111111111111",
                                "title": "Tooltip copy tweak",
                                "priority": 32,
                                "status": "pending",
                                "repo_slugs": ["acme/repo-a"],
                                "target_files": ["web/tooltip.css"],
                            },
                            {
                                "conversation_id": "dddd4444-1111-4111-8111-111111111111",
                                "title": "Minor icon spacing fix",
                                "priority": 34,
                                "status": "pending",
                                "repo_slugs": ["acme/repo-a"],
                                "target_files": ["web/icons.css"],
                            },
                            {
                                "conversation_id": "eeee5555-1111-4111-8111-111111111111",
                                "title": "Unanchored architecture task",
                                "priority": 60,
                                "status": "pending",
                                "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                                "target_files": [],
                            },
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            # Seed Detector 7 telemetry transcripts (bloated orchestrator + heavy reviewer + 0% flash routing)
            orch_steps: list[dict] = []
            for idx in range(6):
                orch_steps.append(
                    {
                        "step_index": idx + 1,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "tool_calls": [
                            {
                                "name": "invoke_subagent",
                                "arguments": {"TypeName": "agy-code-developer", "Model": "pro"},
                            }
                        ],
                    }
                )
            for idx in range(6, 305):
                orch_steps.append({"step_index": idx + 1, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "tick"})
            _write_fake_conversation(brain_dir, convs_dir, orch_cid, steps=orch_steps)

            rev_cid = "88880000-1111-4111-8111-888800000002"
            rev_steps: list[dict] = [
                {
                    "step_index": 1,
                    "type": "RUN_COMMAND",
                    "tool_calls": [
                        {"name": "run_command", "arguments": {"CommandLine": "python3 pr_remediator.py branch-diff --branch fix/x"}}
                    ],
                },
                {
                    "step_index": 2,
                    "type": "RUN_COMMAND",
                    "tool_calls": [
                        {"name": "run_command", "arguments": {"CommandLine": "pre-commit run --all-files"}}
                    ],
                },
            ]
            for idx in range(3, 12):
                rev_steps.append(
                    {
                        "step_index": idx,
                        "type": "RUN_COMMAND",
                        "tool_calls": [{"name": "run_command", "arguments": {"CommandLine": "git status"}}],
                    }
                )
            rev_steps.append(
                {
                    "step_index": 12,
                    "type": "RUN_COMMAND",
                    "tool_calls": [
                        {"name": "run_command", "arguments": {"CommandLine": "python3 pr_remediator.py record-review --verdict PASS --score 96"}}
                    ],
                }
            )
            _write_fake_conversation(brain_dir, convs_dir, rev_cid, steps=rev_steps)

            report = prs.analyze_dev_flow_optimizations(
                portfolio_dir=pdir,
                repos_dir=repos_root,
                brain_dir=brain_dir,
                orchestrator_cid=orch_cid,
                now_ts=now_ts,
            )
            self.assertEqual(report["status"], "ok")
            self.assertGreaterEqual(report["health_score"], 0)
            self.assertLess(report["health_score"], 100)
            by_code = {f["code"]: f for f in report["findings"]}

            expected_codes = {
                "stalled_worker",
                "micro_pr_batch_candidates",
                "repo_contention_hotspot",
                "unanchored_pending_tasks",
                "monolithic_god_files",
                "missing_selective_test_runner",
                "bloated_orchestrator_session",
                "heavy_reviewer_overhead",
                "missing_flash_routing",
            }
            self.assertEqual(set(by_code.keys()), expected_codes)

            required_keys = {
                "code",
                "severity",
                "category",
                "title",
                "evidence",
                "recommendation",
                "auto_action_hint",
                "estimated_savings_min",
            }
            for finding in report["findings"]:
                self.assertTrue(required_keys.issubset(finding.keys()))

            self.assertEqual(by_code["stalled_worker"]["severity"], "critical")
            self.assertEqual(by_code["stalled_worker"]["evidence"]["idle_label"], "1h42m")
            self.assertEqual(by_code["micro_pr_batch_candidates"]["severity"], "high")
            self.assertEqual(by_code["micro_pr_batch_candidates"]["estimated_savings_min"], 36)
            self.assertEqual(by_code["micro_pr_batch_candidates"]["evidence"]["count"], 3)
            self.assertEqual(by_code["repo_contention_hotspot"]["severity"], "high")
            self.assertEqual(by_code["unanchored_pending_tasks"]["evidence"]["count"], 1)
            god_files = by_code["monolithic_god_files"]["evidence"]["files"]
            self.assertEqual(god_files[0]["path"], "web/big_ui.js")
            self.assertTrue(god_files[0]["queued_reference"])
            self.assertEqual(god_files[0]["suggested_skill"], "agy-web-ui-architecture")
            self.assertEqual(god_files[1]["suggested_skill"], "agy-code-design-refactor")
            self.assertEqual(by_code["missing_selective_test_runner"]["severity"], "high")
            self.assertTrue(by_code["missing_selective_test_runner"]["evidence"]["repos"][0]["missing_selective_runner"])
            self.assertTrue(by_code["missing_selective_test_runner"]["evidence"]["repos"][0]["non_incremental_qa_audit"])
            self.assertEqual(by_code["bloated_orchestrator_session"]["evidence"]["step_count"], 305)
            self.assertEqual(by_code["heavy_reviewer_overhead"]["evidence"]["redundant_test_reruns"], 1)
            self.assertEqual(by_code["missing_flash_routing"]["evidence"]["developer_invocations"], 6)
            self.assertTrue(report["top_hint"].startswith("OPT:"))

            # Verify `queue-optimize` CLI subcommand parses & outputs valid JSON
            parsed_opt = prs.parse_args(
                [
                    "queue-optimize",
                    "--json",
                    "--orchestrator-id",
                    orch_cid,
                    "--repos-dir",
                    str(repos_root),
                    "--brain-dir",
                    str(brain_dir),
                    "--stall-seconds",
                    "900",
                ]
            )
            self.assertEqual(parsed_opt.subcommand, "queue-optimize")
            self.assertTrue(parsed_opt.as_json)
            self.assertEqual(parsed_opt.stall_seconds, 900)

            buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(pdir)}),
                contextlib.redirect_stdout(buf),
            ):
                rc = prs.main(
                    [
                        "queue-optimize",
                        "--json",
                        "--orchestrator-id",
                        orch_cid,
                        "--repos-dir",
                        str(repos_root),
                        "--brain-dir",
                        str(brain_dir),
                    ]
                )
            self.assertEqual(rc, 0)
            cli_payload = json.loads(buf.getvalue())
            self.assertEqual(cli_payload["status"], "ok")
            self.assertGreaterEqual(cli_payload["summary"]["total_findings"], 8)

    def test_contract_first_partial_multi_repo_scheduling_and_merged_repos_subtraction(self) -> None:
        """Verify `queue-next --unlocked` subtracts `merged_repos`, schedules contract-first unlocked prefixes, and populates `runnable_by_repo`."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            _, repo_a = _init_bare_and_clone_repo(troot, name="acme-a")
            _, repo_b = _init_bare_and_clone_repo(troot, name="acme-b")

            orch_cid = "aaaa1111-aaaa-4aaa-8aaa-111111111111"
            holder_b = "bbbb2222-bbbb-4bbb-8bbb-222222222222"
            cid_multi_ab = "cccc3333-cccc-4ccc-8ccc-333333333333"
            cid_single_a = "dddd4444-dddd-4ddd-8ddd-444444444444"
            cid_single_c = "eeee5555-eeee-4eee-8eee-555555555555"

            # Lock downstream repo-b; repo-a and repo-c are FREE
            prs.acquire_dev_pr_lock(
                conversation_id=holder_b,
                action_id="hold-b",
                branch_name="fix/20261121-080000-holdb-bbbb2222",
                summary="PR waiting on repo-b",
                proposed_fix="Hold",
                target_repo_dir=str(repo_b),
                repo_slug="acme/acme-b",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=holder_b,
                title="Active PR on repo-b",
                priority=10,
                repo_slugs=["acme/acme-b"],
                status="pr_open",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_multi_ab,
                title="P18 contract-first multi-repo A -> B",
                priority=18,
                repo_slugs=["acme/acme-a", "acme/acme-b"],
                status="pending",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_single_a,
                title="P25 single-repo A",
                priority=25,
                repo_slugs=["acme/acme-a"],
                status="pending",
                portfolio_dir=pdir,
            )
            prs.enqueue_dev_queue_item(
                conversation_id=cid_single_c,
                title="P30 single-repo C",
                priority=30,
                repo_slugs=["acme/acme-c"],
                status="pending",
                portfolio_dir=pdir,
            )

            picked = prs.get_next_dev_queue_item(orch_cid, portfolio_dir=pdir, skip_locked_repos=True)
            # Self-locked holder_b on repo-b must NOT be returned as next_unlocked_item;
            # P18 contract-first multi-repo item MUST win for unlocked repo-a ahead of P25 single-repo A
            self.assertIsNotNone(picked["next_unlocked_item"])
            self.assertEqual(picked["next_unlocked_item"]["conversation_id"], cid_multi_ab)
            self.assertEqual(picked["next_unlocked_item"]["dispatch_repos"], ["acme/acme-a"])
            self.assertTrue(picked["next_unlocked_item"]["partial_multi_repo_dispatch"])
            self.assertEqual(picked["next_unlocked_item"]["waiting_locked_repos"], ["acme/acme-b"])
            # runnable_by_repo maps BOTH free repos simultaneously
            self.assertEqual(picked["runnable_by_repo"]["acme/acme-a"]["conversation_id"], cid_multi_ab)
            self.assertEqual(picked["runnable_by_repo"]["acme/acme-c"]["conversation_id"], cid_single_c)

            # Now simulate repo-a merged on cid_multi_ab, repo-a locked by another task, and repo-b FREE:
            # merged_repos subtraction ensures cid_multi_ab is NO LONGER falsely blocked by repo-a!
            prs._mark_queue_item_repo_merged(
                conversation_id=cid_multi_ab,
                released_repo_slug="acme/acme-a",
                branch_names=["fix/20261121-081000-multi-cccc3333"],
                portfolio_dir=pdir,
            )
            with (
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
                mock.patch.object(prs, "restart_pro_api_server", return_value=False),
            ):
                prs.release_dev_pr_lock(reason="HUMAN_VALIDATED_MANUAL_PR", repo_slug="acme/acme-b", portfolio_dir=pdir)
            prs.acquire_dev_pr_lock(
                conversation_id="ffff6666-ffff-4fff-8fff-666666666666",
                action_id="hold-a",
                branch_name="fix/20261121-082000-holda-ffff6666",
                summary="Busy on repo-a",
                proposed_fix="Hold",
                target_repo_dir=str(repo_a),
                repo_slug="acme/acme-a",
                portfolio_dir=pdir,
            )
            picked2 = prs.get_next_dev_queue_item(orch_cid, portfolio_dir=pdir, skip_locked_repos=True)
            self.assertEqual(picked2["next_unlocked_item"]["conversation_id"], cid_multi_ab)
            self.assertEqual(picked2["next_unlocked_item"]["dispatch_repos"], ["acme/acme-b"])
            self.assertFalse(picked2["next_unlocked_item"]["partial_multi_repo_dispatch"])

    def test_ad_hoc_lock_auto_upsert_reject_and_phantom_pr_open_self_heal(self) -> None:
        """Verify `start-remediation` auto-tracks ad-hoc locks in `dev_queue.json`, rejects cleanly, and `queue-sync` self-heals phantom `pr_open`."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            _, repo_a = _init_bare_and_clone_repo(troot, name="acme-adhoc")
            vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(repo_a)})

            adhoc_cid = "1234abcd-1111-4222-8333-444444444444"
            with (
                mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
                mock.patch.object(prs, "restart_pro_api_server", return_value=False),
            ):
                st_res = prs.start_remediation_branch_and_lock(
                    conversation_id=adhoc_cid,
                    action_id="adhoc-fix",
                    rca_summary="Ad-hoc bug",
                    proposed_fix="Fix ad-hoc bug",
                    checkout_branch=True,
                    branch_timestamp="20261121-090000",
                    workdir=repo_a,
                    portfolio_dir=pdir,
                    git_client=vcs,
                )
                self.assertEqual(st_res["status"], "ok")
                q_map = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
                self.assertIn(adhoc_cid, q_map)
                self.assertEqual(q_map[adhoc_cid]["status"], "in_progress")
                self.assertEqual(q_map[adhoc_cid]["source"], "ad_hoc_lock")

                # Rejecting an ad-hoc lock transitions its dev_queue entry to rejected
                prs.release_dev_pr_lock(
                    reason="HUMAN_REJECTED_BRANCH",
                    repo_slug=st_res["repo_slug"],
                    portfolio_dir=pdir,
                )
                q_after_rej = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
                self.assertEqual(q_after_rej[adhoc_cid]["status"], "rejected")

            # Seed a phantom `pr_open` item (0 active locks, 0 pr_urls, 1/2 repos already merged)
            phantom_cid = "5678efab-1111-4222-8333-555555555555"
            prs.enqueue_dev_queue_item(
                conversation_id=phantom_cid,
                title="Phantom PR after part 1 merge",
                priority=18,
                repo_slugs=["acme/repo-one", "acme/repo-two"],
                status="pr_open",
                branch_names=["fix/20261121-060000-part1-5678efab"],
                pr_urls=[],
                portfolio_dir=pdir,
            )
            # Mark repo-one already merged while leaving status=pr_open on disk directly
            q_path = prs.get_dev_queue_path(portfolio_dir=pdir)
            q_raw = json.loads(q_path.read_text(encoding="utf-8"))
            for it in q_raw["items"]:
                if it["conversation_id"] == phantom_cid:
                    it["merged_repos"] = ["acme/repo-one"]
                    it["status"] = "pr_open"
            q_path.write_text(json.dumps(q_raw, indent=2), encoding="utf-8")

            recon = prs.reconcile_dev_queue_with_git(portfolio_dir=pdir, fetch=False)
            self.assertEqual(recon["status"], "ok")
            self.assertTrue(
                any(
                    r["conversation_id"] == phantom_cid and r["evidence"] == "phantom_pr_open_reverted_to_pending"
                    for r in recon["reconciled_items"]
                )
            )
            q_healed = {it["conversation_id"]: it for it in prs.load_dev_queue(portfolio_dir=pdir)["items"]}
            self.assertEqual(q_healed[phantom_cid]["status"], "pending")
            self.assertEqual(q_healed[phantom_cid]["merged_repos"], ["acme/repo-one"])

    def test_wait_for_unlock_reactive_events_and_cli(self) -> None:
        """Verify 0-LLM `wait-for-unlock` exits cleanly on runnable_work_available, repo_unlocked, stalled_worker, queue_drained, and max_iterations."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            orch_cid = "77770000-aaaa-4111-8111-000000000001"

            # 1. Empty queue + zero locks -> queue_drained immediately
            res_drained = prs.wait_for_unlock_or_actionable_event(
                poll_interval_seconds=0.05,
                max_iterations=2,
                orchestrator_conversation_id=orch_cid,
                portfolio_dir=pdir,
            )
            self.assertEqual(res_drained["event"], "queue_drained")
            self.assertFalse(res_drained["rearm_recommended"])

            # 2. Unlocked runnable work -> runnable_work_available immediately
            prs.enqueue_dev_queue_item(
                conversation_id="77770000-aaaa-4111-8111-000000000002",
                title="Ready task on free repo",
                priority=20,
                repo_slugs=["acme/free-repo"],
                status="pending",
                portfolio_dir=pdir,
            )
            res_ready = prs.wait_for_unlock_or_actionable_event(
                poll_interval_seconds=0.05,
                max_iterations=2,
                orchestrator_conversation_id=orch_cid,
                portfolio_dir=pdir,
            )
            self.assertEqual(res_ready["event"], "runnable_work_available")
            self.assertEqual(res_ready["next_unlocked_item"]["conversation_id"], "77770000-aaaa-4111-8111-000000000002")
            self.assertIn("acme/free-repo", res_ready["runnable_by_repo"])

            # 3. Lock acme/free-repo with fresh lock -> max_iterations with rearm_recommended=True
            _, repo_free = _init_bare_and_clone_repo(troot, name="free-repo")
            prs.acquire_dev_pr_lock(
                conversation_id="77770000-aaaa-4111-8111-000000000003",
                action_id="hold-free",
                branch_name="fix/20261121-090000-hold-77770000",
                summary="Waiting human PR",
                proposed_fix="Hold",
                target_repo_dir=str(repo_free),
                repo_slug="acme/free-repo",
                portfolio_dir=pdir,
            )
            prs.update_dev_pr_lock(
                {"status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True},
                repo_slug="acme/free-repo",
                portfolio_dir=pdir,
            )
            with mock.patch.object(prs, "sync_dev_pr_lock_with_git", return_value={"status": "ok", "active": True}):
                res_max = prs.wait_for_unlock_or_actionable_event(
                    poll_interval_seconds=0.05,
                    max_iterations=2,
                    orchestrator_conversation_id=orch_cid,
                    portfolio_dir=pdir,
                )
                self.assertEqual(res_max["event"], "max_iterations")
                self.assertTrue(res_max["rearm_recommended"])
                self.assertEqual(res_max["iterations"], 2)

            # 4. Simulated merge releasing acme/free-repo on tick 2 -> repo_unlocked!
            calls = {"n": 0}

            def _fake_sync(**_kw: object) -> dict[str, object]:
                calls["n"] += 1
                if calls["n"] >= 2:
                    with (
                        mock.patch.object(prs, "stop_dev_api_server", return_value={"status": "skipped", "stopped": False}),
                        mock.patch.object(prs, "restart_pro_api_server", return_value=False),
                    ):
                        prs.release_dev_pr_lock(
                            reason="BRANCH_MERGED_INTO_MAIN",
                            repo_slug="acme/free-repo",
                            portfolio_dir=pdir,
                        )
                    return {"status": "ok", "active": False, "released": True, "release_result": {"released_repos": ["acme/free-repo"]}}
                return {"status": "ok", "active": True, "released": False}

            with mock.patch.object(prs, "sync_dev_pr_lock_with_git", side_effect=_fake_sync):
                res_unlocked = prs.wait_for_unlock_or_actionable_event(
                    poll_interval_seconds=0.05,
                    max_iterations=4,
                    orchestrator_conversation_id=orch_cid,
                    portfolio_dir=pdir,
                )
            self.assertEqual(res_unlocked["event"], "repo_unlocked")
            self.assertEqual(res_unlocked["released_repos"], ["acme/free-repo"])
            self.assertEqual(res_unlocked["iterations"], 2)
            self.assertEqual(res_unlocked["next_unlocked_item"]["conversation_id"], "77770000-aaaa-4111-8111-000000000002")

            # 5. Stalled DEVELOPING lock -> stalled_worker event (backdate lock timestamps in actions_state.json)
            prs.acquire_dev_pr_lock(
                conversation_id="77770000-aaaa-4111-8111-000000000004",
                action_id="stall-free",
                branch_name="fix/20261121-070000-stall-77770000",
                summary="Stalled",
                proposed_fix="Stalled",
                target_repo_dir=str(repo_free),
                repo_slug="acme/free-repo",
                portfolio_dir=pdir,
            )
            st_path = pdir / "actions_state.json"
            st_raw = json.loads(st_path.read_text(encoding="utf-8"))
            lk_raw = st_raw["actions_log_audit_state"]["dev_pr_locks"]["acme/free-repo"]
            lk_raw["status"] = "DEVELOPING"
            lk_raw["acquired_at"] = "2020-01-01T00:00:00Z"
            lk_raw["updated_at"] = "2020-01-01T00:00:00Z"
            lk_raw["last_activity_at"] = "2020-01-01T00:00:00Z"
            st_path.write_text(json.dumps(st_raw, indent=2), encoding="utf-8")
            with mock.patch.object(prs, "sync_dev_pr_lock_with_git", return_value={"status": "ok", "active": True}):
                res_stall = prs.wait_for_unlock_or_actionable_event(
                    poll_interval_seconds=0.05,
                    max_iterations=2,
                    stall_threshold_seconds=60,
                    orchestrator_conversation_id=orch_cid,
                    portfolio_dir=pdir,
                )
            self.assertEqual(res_stall["event"], "stalled_worker")
            self.assertEqual(len(res_stall["stalled_locks"]), 1)
            self.assertEqual(res_stall["stalled_locks"][0]["repo_slug"], "acme/free-repo")

            # 6. CLI `wait-for-unlock` parses & runs cleanly
            parsed = prs.parse_args(
                ["wait-for-unlock", "--interval", "1", "--max-duration", "10", "--max-iterations", "1", "--orchestrator-id", orch_cid]
            )
            self.assertEqual(parsed.subcommand, "wait-for-unlock")
            buf = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(pdir)}),
                mock.patch.object(prs, "sync_dev_pr_lock_with_git", return_value={"status": "ok", "active": True}),
                contextlib.redirect_stdout(buf),
            ):
                rc = prs.main(["wait-for-unlock", "--interval", "0.05", "--max-iterations", "1", "--stall-seconds", "60", "--orchestrator-id", orch_cid])
            self.assertEqual(rc, 0)
            cli_out = json.loads(buf.getvalue())
            self.assertEqual(cli_out["event"], "stalled_worker")

    def test_flow_optimizer_phantom_pr_untracked_lock_and_idle_orchestrator_detectors(self) -> None:
        """Verify 0-LLM optimizer detects phantom_pr_open, untracked_active_lock, and idle_orchestrator_with_runnable_work."""
        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            brain_dir = troot / "brain"
            orch_cid = "88881111-aaaa-4111-8111-111111111111"
            tpath = brain_dir / orch_cid / ".system_generated" / "logs" / "transcript.jsonl"
            tpath.parent.mkdir(parents=True, exist_ok=True)
            tpath.write_text('{"step_index": 1, "type": "PLANNER_RESPONSE"}\n', encoding="utf-8")

            now_ts = prs._flow_opt.parse_iso_timestamp("2027-02-01T12:00:00Z")
            idle_ts = now_ts - 2400.0  # 40m idle
            os.utime(tpath, (idle_ts, idle_ts))

            (pdir / "dev_queue.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "orchestrator_conversation_id": orch_cid,
                        "items": [
                            {
                                "conversation_id": "88882222-aaaa-4222-8222-222222222222",
                                "title": "Phantom PR item",
                                "priority": 18,
                                "status": "pr_open",
                                "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                                "merged_repos": ["acme/repo-a"],
                                "pr_urls": [],
                                "target_files": ["a.py"],
                            },
                            {
                                "conversation_id": "88883333-aaaa-4333-8333-333333333333",
                                "title": "Runnable task on free repo",
                                "priority": 20,
                                "status": "pending",
                                "repo_slugs": ["acme/repo-free"],
                                "merged_repos": [],
                                "target_files": ["b.py"],
                            },
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            (pdir / "actions_state.json").write_text(
                json.dumps(
                    {
                        "actions_log_audit_state": {
                            "dev_pr_locks": {
                                "acme/repo-adhoc": {
                                    "active": True,
                                    "conversation_id": "88884444-aaaa-4444-8444-444444444444",
                                    "status": "PENDING_HUMAN_PR_VALIDATION",
                                    "pr_gate_ready": True,
                                    "branch_name": "fix/20270201-110000-adhoc-88884444",
                                }
                            }
                        }
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            report = prs.analyze_dev_flow_optimizations(
                portfolio_dir=pdir,
                brain_dir=brain_dir,
                orchestrator_cid=orch_cid,
                now_ts=now_ts,
                stall_threshold_seconds=900,
            )
            by_code = {f["code"]: f for f in report["findings"]}
            self.assertIn("phantom_pr_open", by_code)
            self.assertIn("untracked_active_lock", by_code)
            self.assertIn("idle_orchestrator_with_runnable_work", by_code)
            self.assertEqual(by_code["idle_orchestrator_with_runnable_work"]["evidence"]["runnable_count"], 1)
            self.assertIn("acme/repo-free", by_code["idle_orchestrator_with_runnable_work"]["evidence"]["free_target_repos"])
            hint = prs._flow_opt.format_compact_opt_hint(report)
            self.assertTrue(hint.startswith("OPT:"))
            self.assertTrue(any(tok in hint for tok in ("phantom PR", "untracked lock", "orch idle")))

    def test_test_local_incremental_pre_commit_fallbacks_and_selective_bats_routing(self) -> None:
        """Verify incremental `pre-commit run --files`, `--all-files` fallbacks, `bats-suite` in SKIP, and `should_run_bats` routing."""
        self.assertTrue(prs.parse_args(["test-local", "--all"]).run_all)
        self.assertTrue(prs.parse_args(["test-local", "--all-files"]).run_all)
        self.assertFalse(prs.parse_args(["test-local"]).run_all)

        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True)
            remotes_org = troot / "remotes" / "acme"
            remotes_org.mkdir(parents=True)
            checkouts_dir = troot / "checkouts"
            checkouts_dir.mkdir(parents=True)

            _, repo_a = _init_org_bare_and_clone_repo(remotes_org, checkouts_dir, "repo-p0-fast")
            (repo_a / ".pre-commit-config.yaml").write_text(
                "repos:\n  - repo: local\n    hooks:\n      - id: unit-tests-fast\n        entry: python3 -m unittest\n",
                encoding="utf-8",
            )
            (repo_a / "obsolete.py").write_text("OLD = 1\n", encoding="utf-8")
            (repo_a / "install.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho ok\n", encoding="utf-8")
            (repo_a / "tests" / "scripts").mkdir(parents=True)
            (repo_a / "tests" / "bats").mkdir(parents=True)
            (repo_a / "tests" / "bats" / "smoke.bats").write_text("#!/usr/bin/env bats\n@test 'ok' { [ 1 -eq 1 ]; }\n", encoding="utf-8")
            (repo_a / "tests" / "scripts" / "selective_test_runner.py").write_text(
                "#!/usr/bin/env python3\n"
                "def should_run_bats(changed_files, *, run_all=False):\n"
                "    if run_all:\n"
                "        return True\n"
                "    return any(str(f).endswith(('.sh', '.bats')) for f in changed_files)\n"
                "if __name__ == '__main__':\n"
                "    print('Ran 1 test in 0.001s\\n\\nOK')\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "-C", str(repo_a), "add", "."], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "commit", "-m", "chore: seed repo"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo_a), "push", "origin", "main"], check=True, capture_output=True)

            pc_seen: list[tuple[list[str], str]] = []
            bats_seen: list[list[str]] = []
            real_run = subprocess.run

            def _fake_which(bin_name: str) -> str | None:
                if bin_name == "pre-commit":
                    return "/usr/local/bin/pre-commit"
                if bin_name == "bats":
                    return "/usr/local/bin/bats"
                return None

            def _intercept(cmd, *args, **kwargs):
                cmd_list = [str(c) for c in cmd]
                env_used = kwargs.get("env") or {}
                if cmd_list and cmd_list[0].endswith("pre-commit"):
                    pc_seen.append((cmd_list, str(env_used.get("SKIP") or "")))
                    return subprocess.CompletedProcess(cmd_list, 0, stdout="Passed\n", stderr="")
                if cmd_list and cmd_list[0].endswith("bats"):
                    bats_seen.append(cmd_list)
                    return subprocess.CompletedProcess(cmd_list, 0, stdout="1..1\nok 1 smoke\n", stderr="")
                return real_run(cmd, *args, **kwargs)

            with (
                mock.patch.object(prs.shutil, "which", side_effect=_fake_which),
                mock.patch.object(prs.subprocess, "run", side_effect=_intercept),
            ):
                # 1. Clean working tree (empty changed-files list) -> falls back to --all-files; should_run_bats=False skips bats
                res_clean = prs.run_local_validation_suite(
                    workdir=repo_a,
                    portfolio_dir=pdir,
                    run_pre_commit=True,
                    run_bats=True,
                )
                self.assertTrue(res_clean["passed"])
                self.assertEqual(pc_seen[-1][0], ["/usr/local/bin/pre-commit", "run", "--all-files"])
                self.assertEqual(pc_seen[-1][1], "unit-tests,unit-tests-fast,qa-static-audit,bats-suite")
                self.assertEqual(len(bats_seen), 0)
                self.assertEqual(res_clean["verification_results"]["bats"]["status"], "skipped")

                # 2. Docs/Python edit + deleted file -> incremental --files omits deleted file; should_run_bats=False skips bats
                subprocess.run(["git", "-C", str(repo_a), "rm", "-q", "obsolete.py"], check=True, capture_output=True)
                (repo_a / "README.md").write_text("# Updated docs\n", encoding="utf-8")
                res_inc = prs.run_local_validation_suite(
                    workdir=repo_a,
                    portfolio_dir=pdir,
                    run_pre_commit=True,
                    run_bats=True,
                )
                self.assertTrue(res_inc["passed"])
                self.assertEqual(pc_seen[-1][0], ["/usr/local/bin/pre-commit", "run", "--files", "README.md"])
                self.assertEqual(res_inc["verification_results"]["pre_commit"]["mode"], "incremental")
                self.assertEqual(len(bats_seen), 0)
                self.assertEqual(res_inc["verification_results"]["bats"]["status"], "skipped")

                # 3. Explicit --all (run_all=True) -> forces --all-files on pre-commit and runs BATS
                res_all = prs.run_local_validation_suite(
                    workdir=repo_a,
                    portfolio_dir=pdir,
                    run_pre_commit=True,
                    run_bats=True,
                    run_all=True,
                )
                self.assertTrue(res_all["passed"])
                self.assertEqual(pc_seen[-1][0], ["/usr/local/bin/pre-commit", "run", "--all-files"])
                self.assertEqual(len(bats_seen), 1)
                self.assertEqual(res_all["verification_results"]["bats"]["status"], "pass")

                # 4. Shell edit -> incremental --files includes install.sh AND should_run_bats=True runs BATS
                (repo_a / "install.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho v2\n", encoding="utf-8")
                res_sh = prs.run_local_validation_suite(
                    workdir=repo_a,
                    portfolio_dir=pdir,
                    run_pre_commit=True,
                    run_bats=True,
                )
                self.assertTrue(res_sh["passed"])
                self.assertEqual(pc_seen[-1][0][:3], ["/usr/local/bin/pre-commit", "run", "--files"])
                self.assertIn("install.sh", pc_seen[-1][0][3:])
                self.assertNotIn("obsolete.py", pc_seen[-1][0][3:])
                self.assertEqual(len(bats_seen), 2)
                self.assertEqual(res_sh["verification_results"]["bats"]["status"], "pass")

                # 5. Editing .pre-commit-config.yaml -> forces --all-files fallback and runs BATS even without --all
                subprocess.run(["git", "-C", str(repo_a), "checkout", "--", "install.sh"], check=True, capture_output=True)
                (repo_a / ".pre-commit-config.yaml").write_text(
                    "repos:\n  - repo: local\n    hooks:\n      - id: unit-tests-fast\n        entry: python3 -m unittest -v\n",
                    encoding="utf-8",
                )
                res_cfg = prs.run_local_validation_suite(
                    workdir=repo_a,
                    portfolio_dir=pdir,
                    run_pre_commit=True,
                    run_bats=True,
                )
                self.assertTrue(res_cfg["passed"])
                self.assertEqual(pc_seen[-1][0], ["/usr/local/bin/pre-commit", "run", "--all-files"])
                self.assertEqual(len(bats_seen), 3)
                self.assertEqual(res_cfg["verification_results"]["bats"]["status"], "pass")

    def test_watch_pr_merge_and_wait_for_unlock_ast_execv_auto_reload_and_syntax_guard(self) -> None:
        """Verify `watch-pr-merge` & `wait-for-unlock` watch companion modules, guard `ast.parse` syntax, release `flock`, and `os.execv` cleanly."""
        import fcntl

        watched_real = prs._collect_watched_script_paths()
        self.assertGreaterEqual(len(watched_real), 4)
        self.assertEqual(watched_real[0], pathlib.Path(prs.__file__).resolve())
        watched_names = {p.name for p in watched_real}
        self.assertTrue(
            {"pr_remediator.py", "git_client.py", "parallel_scope.py", "dev_flow_optimizer.py"}.issubset(watched_names)
        )

        with tempfile.TemporaryDirectory() as tmp:
            troot = pathlib.Path(tmp)
            pdir = troot / "portfolio"
            pdir.mkdir(parents=True, exist_ok=True)
            fake_entry = troot / "pr_remediator.py"
            fake_companion = troot / "git_client.py"
            fake_entry.write_text("VALUE = 1\n", encoding="utf-8")
            fake_companion.write_text("COMPANION = 1\n", encoding="utf-8")

            # 1. `watch-pr-merge`: broken syntax logs non-fatal warning and continues without crashing; valid companion edit releases lock & execs
            watch_calls = {"n": 0}
            watch_execs: list[tuple[str, list[str], bool]] = []
            warn_buf_watch = io.StringIO()
            watcher_lock_path = pdir / "tmp" / "pr_merge_watcher.lock"

            def _fake_watch_sync(**_kw: object) -> dict[str, object]:
                watch_calls["n"] += 1
                if watch_calls["n"] == 1:
                    # Broken syntax on companion module -> must warn, NOT exec, and NOT crash
                    fake_companion.write_text("def broken(:\n", encoding="utf-8")
                elif watch_calls["n"] == 2:
                    # Valid syntax fix on companion module -> must release flock and execv
                    fake_companion.write_text("COMPANION = 2\n", encoding="utf-8")
                return {"status": "ok", "active": True, "released": False}

            def _fake_watch_exec(exe: str, argv: list[str]) -> None:
                # Verify pr_merge_watcher.lock is already unlocked before os.execv
                can_lock = False
                with open(watcher_lock_path, "a+", encoding="utf-8") as probe_fd:
                    try:
                        fcntl.flock(probe_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        can_lock = True
                        fcntl.flock(probe_fd.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        can_lock = False
                watch_execs.append((exe, list(argv), can_lock))

            with mock.patch.object(prs, "sync_dev_pr_lock_with_git", side_effect=_fake_watch_sync):
                res_watch = prs.run_pr_merge_watcher_loop(
                    poll_interval_seconds=0.05,
                    max_iterations=4,
                    portfolio_dir=pdir,
                    exec_fn=_fake_watch_exec,
                    watched_paths_override=[fake_entry, fake_companion],
                    raw_argv=["watch-pr-merge", "--interval", "15"],
                    warn_stream=warn_buf_watch,
                )
            self.assertEqual(res_watch["status"], "ok")
            self.assertTrue(res_watch["reloaded"])
            self.assertEqual(res_watch["reason"], "auto_reloaded")
            self.assertIn("Reload skipped (git_client.py: SyntaxError)", warn_buf_watch.getvalue())
            self.assertEqual(len(watch_execs), 1)
            self.assertEqual(
                watch_execs[0][:2],
                (sys.executable, [sys.executable, str(fake_entry.resolve()), "watch-pr-merge", "--interval", "15"]),
            )
            self.assertTrue(watch_execs[0][2])

            # 2. `wait-for-unlock`: broken syntax logs non-fatal warning and keeps waiting; valid update triggers `auto_reloaded`
            _, repo_hold = _init_bare_and_clone_repo(troot, name="hold-repo")
            prs.acquire_dev_pr_lock(
                conversation_id="88880000-aaaa-4111-8111-000000000001",
                action_id="hold-reload",
                branch_name="fix/20261123-070000-hold-88880000",
                summary="Hold lock",
                proposed_fix="Hold lock",
                target_repo_dir=str(repo_hold),
                repo_slug="acme/hold-repo",
                portfolio_dir=pdir,
            )
            prs.update_dev_pr_lock(
                {"status": "PENDING_HUMAN_PR_VALIDATION", "pr_gate_ready": True},
                repo_slug="acme/hold-repo",
                portfolio_dir=pdir,
            )

            wait_calls = {"n": 0}
            wait_execs: list[tuple[str, list[str]]] = []
            warn_buf_wait = io.StringIO()

            def _fake_wait_sync(**_kw: object) -> dict[str, object]:
                wait_calls["n"] += 1
                if wait_calls["n"] == 1:
                    fake_entry.write_text("if True print('oops')\n", encoding="utf-8")
                elif wait_calls["n"] == 2:
                    fake_entry.write_text("VALUE = 2\n", encoding="utf-8")
                return {"status": "ok", "active": True, "released": False}

            def _fake_wait_exec(exe: str, argv: list[str]) -> None:
                wait_execs.append((exe, list(argv)))

            with mock.patch.object(prs, "sync_dev_pr_lock_with_git", side_effect=_fake_wait_sync):
                res_wait = prs.wait_for_unlock_or_actionable_event(
                    poll_interval_seconds=0.05,
                    max_iterations=4,
                    portfolio_dir=pdir,
                    exec_fn=_fake_wait_exec,
                    watched_paths_override=[fake_entry, fake_companion],
                    raw_argv=["wait-for-unlock", "--interval", "15"],
                    warn_stream=warn_buf_wait,
                )
            self.assertEqual(res_wait["status"], "ok")
            self.assertEqual(res_wait["event"], "auto_reloaded")
            self.assertTrue(res_wait["reloaded"])
            self.assertIn("Reload skipped (pr_remediator.py: SyntaxError)", warn_buf_wait.getvalue())
            self.assertEqual(len(wait_execs), 1)
            self.assertEqual(
                wait_execs[0],
                (sys.executable, [sys.executable, str(fake_entry.resolve()), "wait-for-unlock", "--interval", "15"]),
            )


if __name__ == "__main__":
    unittest.main()
