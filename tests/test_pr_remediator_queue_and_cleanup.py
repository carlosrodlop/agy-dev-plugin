#!/usr/bin/env python3
"""Zero-credential hermetic unit tests for `dev_queue.json` and safe post-merge conversation cleanup."""

from __future__ import annotations

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
            _, clone = _init_bare_and_clone_repo(troot)
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

            # Verify partial multi-repo merge progression (`merged_repos`)
            prs.update_dev_queue_item_status(cid_high_locked, status="pr_open", portfolio_dir=pdir)
            partial = prs._mark_queue_item_repo_merged(
                conversation_id=cid_high_locked,
                released_repo_slug="acme-org/acme-plugin",
                branch_names=["fix/plugin"],
                portfolio_dir=pdir,
            )["item"]
            self.assertEqual(partial["merged_repos"], ["acme-org/acme-plugin"])
            self.assertEqual(partial["status"], "pr_open")

            completed = prs._mark_queue_item_repo_merged(
                conversation_id=cid_high_locked,
                released_repo_slug="acme-org/acme-tower",
                branch_names=["fix/tower"],
                portfolio_dir=pdir,
            )["item"]
            self.assertEqual(completed["merged_repos"], ["acme-org/acme-plugin", "acme-org/acme-tower"])
            self.assertEqual(completed["status"], "merged")


if __name__ == "__main__":
    unittest.main()
