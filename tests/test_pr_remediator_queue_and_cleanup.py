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


if __name__ == "__main__":
    unittest.main()
