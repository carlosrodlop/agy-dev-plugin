#!/usr/bin/env python3
"""Hermetic unit tests for compact/summary/Markdown CLI output on `lock-status` and `queue-list`."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
for _p in (str(REPO_ROOT / "clients"), str(SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import lock_store  # noqa: E402
import pr_remediator as prs  # noqa: E402
import queue_store  # noqa: E402


def _sample_lock_status_payload() -> dict[str, object]:
    """Build a synthetic verbose `lock-status --all` payload mirroring real multi-slot state."""
    repo_a_slot1 = {
        "slot_id": 1,
        "active": True,
        "lock_id": "lock-1111aaaa-20261125-170000",
        "conversation_id": "1111aaaa-0000-4000-8000-000000000001",
        "action_id": "alpha-feature",
        "status": "DEVELOPING",
        "stage_label": "coding",
        "branch_name": "fix/20261125-170000-alpha-1111aaaa",
        "pr_gate_ready": False,
        "branch_pushed": False,
        "manual_pr_url": "",
        "worker_cid": "aaaa1111-worker",
        "overlap_tier": "",
        "needs_base_rebase": False,
        "target_files": ["clients/alpha.py", "tests/test_alpha.py"],
        "summary": "Implement alpha parser | with pipe",
        "verification_results": {"huge_blob": "x" * 5000},
        "review_markdown": "y" * 5000,
    }
    repo_a_slot2 = {
        "slot_id": 2,
        "active": True,
        "lock_id": "lock-2222bbbb-20261125-170500-s2",
        "conversation_id": "2222bbbb-0000-4000-8000-000000000002",
        "action_id": "alpha-follower",
        "status": "READY_AWAITING_BASE_MERGE",
        "stage_label": "reviewed",
        "branch_name": "fix/20261125-170500-beta-2222bbbb",
        "pr_gate_ready": False,
        "branch_pushed": False,
        "manual_pr_url": "",
        "worker_cid": "bbbb2222-worker",
        "overlap_tier": "TRANSITIVE",
        "needs_base_rebase": True,
        "target_files": ["docs/guide.md"],
        "summary": "Add guide docs",
    }
    repo_b_slot1 = {
        "slot_id": 1,
        "active": True,
        "lock_id": "lock-3333cccc-20261125-163000",
        "conversation_id": "3333cccc-0000-4000-8000-000000000003",
        "action_id": "beta-ui",
        "status": "PENDING_HUMAN_PR_VALIDATION",
        "stage_label": "pr_ready",
        "branch_name": "fix/20261125-163000-ui-3333cccc",
        "pr_gate_ready": True,
        "branch_pushed": True,
        "manual_pr_url": "https://github.com/acme/repo-beta/compare/main...fix/20261125-163000-ui-3333cccc?expand=1",
        "worker_cid": "cccc3333-worker",
        "target_files": ["web/app.js"],
        "summary": "Polish beta UI",
    }
    repo_a = {
        **repo_a_slot1,
        "repo_slug": "acme/repo-alpha",
        "max_slots": 3,
        "active_slots_count": 2,
        "slot_occupancy": "2/3",
        "slots": {"2": repo_a_slot2, "1": repo_a_slot1},
    }
    repo_b = {
        **repo_b_slot1,
        "repo_slug": "acme/repo-beta",
        "max_slots": 3,
        "active_slots_count": 1,
        "slot_occupancy": "1/3",
        "slots": {"1": repo_b_slot1},
    }
    return {
        "status": "ok",
        "active_count": 2,
        "lock": repo_a,
        "locks": {
            "acme/repo-beta": repo_b,
            "acme/repo-alpha": repo_a,
        },
        "queue_reconciliation": {
            "status": "ok",
            "checked_count": 2,
            "reconciled_count": 0,
            "reconciled_items": [],
        },
    }


def _sample_queue_payload() -> dict[str, object]:
    """Build a synthetic `load_dev_queue()` payload containing active and terminal items."""
    return {
        "items": [
            {
                "conversation_id": "1111aaaa-0000-4000-8000-000000000001",
                "title": "Active alpha fix | urgent",
                "action_id": "alpha-fix",
                "priority": 19,
                "effective_priority": 11,
                "status": "in_progress",
                "repo_slugs": ["acme/repo-alpha"],
                "depends_on": [],
                "target_files": ["clients/alpha.py", "tests/test_alpha.py"],
                "rca_summary": "x" * 3000,
                "proposed_fix": "y" * 3000,
                "importance": "high",
                "component": "alpha-core",
            },
            {
                "conversation_id": "2222bbbb-0000-4000-8000-000000000002",
                "title": "Pending beta enhancement",
                "action_id": "beta-feat",
                "priority": 12,
                "effective_priority": 12,
                "status": "pending",
                "repo_slugs": ["acme/repo-alpha", "acme/repo-beta"],
                "merged_repos": ["acme/repo-alpha"],
                "depends_on": ["1111aaaa-0000-4000-8000-000000000001"],
                "target_files": ["web/app.js"],
                "pr_urls": ["https://github.com/acme/repo-beta/pull/42"],
            },
            {
                "conversation_id": "3333cccc-0000-4000-8000-000000000003",
                "title": "Standby research item",
                "action_id": "research",
                "priority": 40,
                "effective_priority": 40,
                "status": "standby",
                "repo_slugs": ["acme/repo-beta"],
                "depends_on": [],
                "target_files": [],
            },
            {
                "conversation_id": "4444dddd-0000-4000-8000-000000000004",
                "title": "Old cleaned item",
                "action_id": "old-task",
                "priority": 20,
                "effective_priority": 20,
                "status": "cleaned",
                "repo_slugs": ["acme/repo-alpha"],
                "depends_on": [],
                "target_files": ["README.md"],
            },
            {
                "conversation_id": "5555eeee-0000-4000-8000-000000000005",
                "title": "Cancelled item",
                "action_id": "cancelled-task",
                "priority": 25,
                "effective_priority": 25,
                "status": "cancelled",
                "repo_slugs": ["acme/repo-beta"],
                "depends_on": [],
                "target_files": [],
            },
        ],
        "summary": {
            "pending": 1,
            "in_progress": 1,
            "pr_open": 0,
            "merged": 0,
            "standby": 1,
            "hitl_blocked": 0,
            "cleaned": 1,
            "cancelled": 1,
            "rejected": 0,
            "total": 5,
        },
        "queue_path": "/tmp/dev_queue.json",
    }


class TestPrRemediatorCompactCli(unittest.TestCase):
    """Verify compact JSON, Markdown table, and backward-compatible JSON outputs for lock-status and queue-list."""

    def test_build_compact_lock_status_normalizes_slots_and_strips_blobs(self) -> None:
        raw = _sample_lock_status_payload()
        compact = lock_store.build_compact_lock_status_payload(raw)

        self.assertTrue(compact["compact"])
        self.assertEqual(compact["active_count"], 2)
        self.assertEqual([r["repo_slug"] for r in compact["active_locks"]], ["acme/repo-alpha", "acme/repo-beta"])
        self.assertIn("acme/repo-alpha", compact["locks"])
        alpha_slots = compact["locks"]["acme/repo-alpha"]["slots"]
        self.assertIsInstance(alpha_slots, list)
        self.assertEqual([s["slot_id"] for s in alpha_slots], [1, 2])
        self.assertNotIn("verification_results", alpha_slots[0])
        self.assertNotIn("review_markdown", alpha_slots[0])
        self.assertEqual(alpha_slots[0]["target_files_count"], 2)
        self.assertTrue(alpha_slots[1]["needs_base_rebase"])
        self.assertEqual(compact["queue_reconciliation"]["checked_count"], 2)

    def test_render_lock_status_markdown_renders_table_and_empty_state(self) -> None:
        md = lock_store.render_lock_status_markdown(_sample_lock_status_payload())
        self.assertIn("### Per-Repository Dev PR Locks (`2` active repo(s))", md)
        self.assertIn("| Repo | Slot | Occupancy | Status | Conversation ID | Branch | PR Gate | Files |", md)
        self.assertIn("`acme/repo-alpha` | `#1` | `2/3` | `DEVELOPING`", md)
        self.assertIn("`acme/repo-alpha` | `#2` | `2/3` | `READY_AWAITING_BASE_MERGE`", md)
        self.assertIn("`acme/repo-beta` | `#1` | `1/3` | `PENDING_HUMAN_PR_VALIDATION`", md)
        self.assertIn("| `ready` |", md)

        empty_md = lock_store.render_lock_status_markdown({"status": "ok", "active_count": 0, "locks": {}})
        self.assertIn("_No active repository locks._", empty_md)

    def test_lock_status_cli_dispatch_supports_json_summary_compact_and_md(self) -> None:
        raw = _sample_lock_status_payload()
        locks_map = raw["locks"]
        first_lock = raw["lock"]

        with (
            mock.patch.object(prs, "get_all_dev_pr_locks", return_value=locks_map),
            mock.patch.object(prs, "get_dev_pr_lock", return_value=first_lock),
        ):
            # Default json remains backward-compatible with raw nested slots dict
            buf_json = io.StringIO()
            with redirect_stdout(buf_json):
                rc_json = prs.main(["lock-status", "--all"])
            self.assertEqual(rc_json, 0)
            parsed_json = json.loads(buf_json.getvalue())
            self.assertIsInstance(parsed_json["locks"]["acme/repo-alpha"]["slots"], dict)
            self.assertIn("verification_results", parsed_json["locks"]["acme/repo-alpha"])

            # --summary triggers compact JSON projection
            buf_sum = io.StringIO()
            with redirect_stdout(buf_sum):
                rc_sum = prs.main(["lock-status", "--all", "--summary"])
            self.assertEqual(rc_sum, 0)
            parsed_sum = json.loads(buf_sum.getvalue())
            self.assertTrue(parsed_sum["compact"])
            self.assertIsInstance(parsed_sum["locks"]["acme/repo-alpha"]["slots"], list)
            self.assertLess(len(buf_sum.getvalue()), len(buf_json.getvalue()))

            # --format md emits readable Markdown table
            buf_md = io.StringIO()
            with redirect_stdout(buf_md):
                rc_md = prs.main(["lock-status", "--all", "--format", "md"])
            self.assertEqual(rc_md, 0)
            self.assertIn("### Per-Repository Dev PR Locks", buf_md.getvalue())
            self.assertIn("`acme/repo-alpha`", buf_md.getvalue())

    def test_queue_list_compact_active_only_repo_limit_and_markdown(self) -> None:
        raw_q = _sample_queue_payload()

        compact = queue_store.build_compact_queue_list_payload(raw_q, active_only=True)
        self.assertTrue(compact["compact"])
        self.assertTrue(compact["active_only"])
        self.assertEqual(compact["total_matched"], 3)
        self.assertEqual(compact["returned_count"], 3)
        statuses = {item["status"] for item in compact["items"]}
        self.assertEqual(statuses, {"in_progress", "pending", "standby"})
        self.assertNotIn("rca_summary", compact["items"][0])
        self.assertNotIn("proposed_fix", compact["items"][0])

        filtered_repo = queue_store.build_compact_queue_list_payload(
            raw_q,
            active_only=True,
            repo_slug="repo-beta",
            limit=1,
        )
        self.assertEqual(filtered_repo["total_matched"], 2)
        self.assertEqual(filtered_repo["returned_count"], 1)
        self.assertEqual(filtered_repo["items"][0]["conversation_id"], "2222bbbb-0000-4000-8000-000000000002")

        md = queue_store.render_queue_list_markdown(raw_q, active_only=True)
        self.assertIn("### Dev Queue (3/3 shown · active-only", md)
        self.assertIn("`P19→11`", md)
        self.assertIn("Active alpha fix \\| urgent", md)
        self.assertIn("`1111aaaa`", md)
        self.assertNotIn("Old cleaned item", md)

    def test_queue_list_cli_dispatch_modes_and_backward_compat(self) -> None:
        with mock.patch.object(prs, "load_dev_queue", side_effect=lambda: _sample_queue_payload()):
            # Default json includes all 5 items and full fields
            buf_raw = io.StringIO()
            with redirect_stdout(buf_raw):
                self.assertEqual(prs.main(["queue-list"]), 0)
            parsed_raw = json.loads(buf_raw.getvalue())
            self.assertEqual(len(parsed_raw["items"]), 5)
            self.assertIn("rca_summary", parsed_raw["items"][0])

            # --active-only on default json excludes terminal items
            buf_active_json = io.StringIO()
            with redirect_stdout(buf_active_json):
                self.assertEqual(prs.main(["queue-list", "--active-only"]), 0)
            parsed_active = json.loads(buf_active_json.getvalue())
            self.assertEqual(len(parsed_active["items"]), 3)

            # --summary implies compact projection + active-only when --status is unset
            buf_sum = io.StringIO()
            with redirect_stdout(buf_sum):
                self.assertEqual(prs.main(["queue-list", "--summary"]), 0)
            parsed_sum = json.loads(buf_sum.getvalue())
            self.assertTrue(parsed_sum["compact"])
            self.assertTrue(parsed_sum["active_only"])
            self.assertEqual(parsed_sum["returned_count"], 3)
            self.assertLess(len(buf_sum.getvalue()), len(buf_raw.getvalue()))

            # --summary with explicit --status cleaned still returns cleaned items
            buf_cleaned = io.StringIO()
            with redirect_stdout(buf_cleaned):
                self.assertEqual(prs.main(["queue-list", "--summary", "--status", "cleaned"]), 0)
            parsed_cleaned = json.loads(buf_cleaned.getvalue())
            self.assertEqual(parsed_cleaned["returned_count"], 1)
            self.assertEqual(parsed_cleaned["items"][0]["status"], "cleaned")

            # --active-only --format md renders Markdown table
            buf_md = io.StringIO()
            with redirect_stdout(buf_md):
                self.assertEqual(prs.main(["queue-list", "--active-only", "--format", "md", "--repo", "repo-alpha"]), 0)
            md_out = buf_md.getvalue()
            self.assertIn("### Dev Queue (2/2 shown · active-only · repo=repo-alpha", md_out)
            self.assertIn("`P19→11`", md_out)
            self.assertNotIn("Standby research item", md_out)

    def test_docs_reference_compact_cli_flags_and_respect_24kb_limit(self) -> None:
        co_design = REPO_ROOT / "rules" / "co-design-and-discovery.md"
        pr_skill = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "SKILL.md"
        orch_skill = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "SKILL.md"

        for doc_path in (co_design, pr_skill, orch_skill):
            raw = doc_path.read_text(encoding="utf-8")
            self.assertLess(len(raw.encode("utf-8")), 24000, msg=f"{doc_path.name} exceeded 24KB limit")

        co_text = co_design.read_text(encoding="utf-8")
        self.assertIn("lock-status --all --format md", co_text)
        self.assertIn("queue-list --active-only --format md", co_text)

        pr_text = pr_skill.read_text(encoding="utf-8")
        self.assertIn("lock-status --all --sync-git --format md", pr_text)
        self.assertIn("--active-only --format md", pr_text)

        orch_text = orch_skill.read_text(encoding="utf-8")
        self.assertIn("--format compact|md|json", orch_text)
        self.assertIn("--active-only", orch_text)


if __name__ == "__main__":
    unittest.main()
