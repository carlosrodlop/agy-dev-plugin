#!/usr/bin/env python3
"""Hermetic Zero-PII unit tests for deterministic 0-LLM PR Queue Compaction (`queue_compactor.py`)."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
TUI_DIR = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts"
CLIENTS_DIR = REPO_ROOT / "clients"

for _p in (SCRIPTS_DIR, TUI_DIR, CLIENTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import dev_flow_optimizer as flow_opt  # noqa: E402
import dev_queue_tui as tui  # noqa: E402
import orchestration_control as orch_ctrl  # noqa: E402
import pr_remediator as prs  # noqa: E402
import queue_compactor as qc  # noqa: E402


def _write_queue_and_locks(
    state_dir: Path,
    items: list[dict],
    locks: dict | None = None,
) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    q_path = state_dir / "dev_queue.json"
    q_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "orchestrator_conversation_id": "orch-0000-0000-0000-000000000001",
                "updated_at": "2026-11-25T00:00:00Z",
                "items": items,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.chmod(q_path, 0o600)
    if locks is not None:
        l_path = state_dir / "actions_state.json"
        l_path.write_text(
            json.dumps({"dev_pr_locks": locks}, indent=2),
            encoding="utf-8",
        )
        os.chmod(l_path, 0o600)
    return q_path


class TestQueueCompactor(unittest.TestCase):
    """Hermetic coverage for Tier 1/2 detection, Split-Phase, hard guardrails, CLI, and TUI."""

    def test_tier1_same_repo_chain_absorbs_rewires_dependents_and_writes_0600_backup(self) -> None:
        """Direct `depends_on` chain on same remaining repo (incl. partial-WIP) compacts atomically."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            items = [
                # Single-repo seed items to teach path ownership dynamically (zero hardcode)
                {
                    "conversation_id": "seed-a001-0000-0000-000000000001",
                    "title": "Merged repo-a helper",
                    "priority": 10,
                    "status": "merged",
                    "repo_slugs": ["acme/repo-a"],
                    "target_files": ["clients/common.py", "docs/arch.md"],
                },
                {
                    "conversation_id": "seed-b001-0000-0000-000000000002",
                    "title": "Merged repo-b view",
                    "priority": 10,
                    "status": "merged",
                    "repo_slugs": ["acme/repo-b"],
                    "target_files": ["api/radars.py", "web/js/views/radars.mjs", "web/css/views/radars.css"],
                },
                # Partial-WIP item (repo-a already merged; only repo-b remains)
                {
                    "conversation_id": "aaaa1111-0000-4000-8000-000000000001",
                    "title": "Radars Global Feed (#global-feed)",
                    "priority": 13,
                    "importance": "high",
                    "component": "radars",
                    "status": "pending",
                    "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                    "merged_repos": ["acme/repo-a"],
                    "depends_on": [],
                    "target_files": [
                        "clients/common.py",
                        "api/global_feed.py",
                        "api/routers/general.py",
                        "web/index.html",
                        "web/js/views/radars.mjs",
                        "web/css/views/radars.css",
                        "tests/test_global_feed.py",
                    ],
                    "rca_summary": "Missing global feed.",
                    "proposed_fix": "Implement global feed with 2.5h poller.",
                    "notes": "Ratified Contract A.",
                },
                # Dependent single-repo item on repo-b
                {
                    "conversation_id": "bbbb2222-0000-4000-8000-000000000002",
                    "title": "Radars unified 3h poller + Last/Next badges",
                    "priority": 13,
                    "importance": "medium",
                    "component": "radars",
                    "status": "pending",
                    "repo_slugs": ["acme/repo-b"],
                    "depends_on": ["aaaa1111-0000-4000-8000-000000000001"],
                    "target_files": [
                        "api/radar_poller.py",
                        "api/global_feed.py",
                        "api/routers/general.py",
                        "web/index.html",
                        "web/js/views/radars.mjs",
                        "web/css/views/radars.css",
                        "tests/test_global_feed.py",
                    ],
                    "rca_summary": "Poller cadence duplicated.",
                    "proposed_fix": "Unify poller to 3h across all radars.",
                    "notes": "Ratified Contract B.",
                },
                # Downstream item depending on BOTH aaaa1111 and bbbb2222
                {
                    "conversation_id": "cccc3333-0000-4000-8000-000000000003",
                    "title": "Community Events Radar",
                    "priority": 14,
                    "importance": "medium",
                    "component": "radars",
                    "status": "pending",
                    "repo_slugs": ["acme/repo-b"],
                    "depends_on": [
                        "aaaa1111-0000-4000-8000-000000000001",
                        "bbbb2222-0000-4000-8000-000000000002",
                    ],
                    "target_files": [f"api/community_{i}.py" for i in range(18)],  # Epic -> excluded
                    "rca_summary": "Epic.",
                    "proposed_fix": "Epic spec.",
                },
            ]
            q_path = _write_queue_and_locks(state_dir, items, locks={})

            # 1. Dry-run detects Tier 1 plan without mutating disk
            dry = qc.compact_dev_queue(portfolio_dir=state_dir, apply=False, tier="1", refresh_board=False)
            self.assertEqual(dry["status"], "ok")
            self.assertEqual(dry["mode"], "dry_run")
            self.assertEqual(dry["plan_count"], 1)
            plan = dry["plans"][0]
            self.assertEqual(plan["tier"], 1)
            self.assertEqual(plan["pattern"], "same_remaining_repo")
            self.assertEqual(plan["absorber_id"], "aaaa1111-0000-4000-8000-000000000001")
            self.assertEqual(plan["absorbed_id"], "bbbb2222-0000-4000-8000-000000000002")
            self.assertIn("direct_depends_on", plan["reasons"])
            self.assertNotIn("clients/common.py", plan["combined_target_files"])  # Already-merged repo-a file stripped
            self.assertIn("api/radar_poller.py", plan["combined_target_files"])
            self.assertEqual(plan["rewired_dependents"], ["cccc3333-0000-4000-8000-000000000003"])

            # 2. Apply atomically with 0600 backup, rewiring, and zero deadlocks/cycles
            applied = qc.compact_dev_queue(portfolio_dir=state_dir, apply=True, tier="1", refresh_board=False)
            self.assertEqual(applied["status"], "ok")
            self.assertEqual(applied["mode"], "applied")
            self.assertEqual(applied["applied_count"], 1)
            self.assertEqual(applied["total_prs_saved"], 1)
            bak = Path(applied["backup_path"])
            self.assertTrue(bak.is_file())
            self.assertEqual(stat.S_IMODE(bak.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(q_path.stat().st_mode), 0o600)

            reloaded = json.loads(q_path.read_text(encoding="utf-8"))["items"]
            by_id = {it["conversation_id"]: it for it in reloaded}
            self.assertEqual(by_id["bbbb2222-0000-4000-8000-000000000002"]["status"], "cancelled")
            self.assertIn("Superseded / consolidated into aaaa1111", by_id["bbbb2222-0000-4000-8000-000000000002"]["notes"])
            self.assertEqual(by_id["aaaa1111-0000-4000-8000-000000000001"]["status"], "pending")
            self.assertIn("Part 1/2", by_id["aaaa1111-0000-4000-8000-000000000001"]["proposed_fix"])
            self.assertIn("Part 2/2", by_id["aaaa1111-0000-4000-8000-000000000001"]["proposed_fix"])
            self.assertEqual(
                by_id["cccc3333-0000-4000-8000-000000000003"]["depends_on"],
                ["aaaa1111-0000-4000-8000-000000000001"],
            )
            self.assertEqual(qc.validate_queue_invariants(reloaded), [])

    def test_hub_files_alone_never_trigger_false_positive_compaction(self) -> None:
        """Two unrelated views sharing only global hub files (`web/index.html`, `dashboard.js`) must NOT compact."""
        items = [
            {
                "conversation_id": "hub11111-0000-4000-8000-000000000001",
                "title": "Briefing card spacing",
                "priority": 14,
                "component": "briefing",
                "status": "pending",
                "repo_slugs": ["acme/repo-b"],
                "target_files": [
                    "web/index.html",
                    "web/dashboard.js",
                    "web/styles.css",
                    "web/js/views/briefing.mjs",
                    "tests/test_control_tower_architecture_and_api.py",
                ],
                "proposed_fix": "Briefing spacing fix.",
            },
            {
                "conversation_id": "hub22222-0000-4000-8000-000000000002",
                "title": "Checklist filter badge",
                "priority": 14,
                "component": "checklist",
                "status": "pending",
                "repo_slugs": ["acme/repo-b"],
                "target_files": [
                    "web/index.html",
                    "web/dashboard.js",
                    "web/styles.css",
                    "web/js/views/checklist.mjs",
                    "tests/test_control_tower_architecture_and_api.py",
                ],
                "proposed_fix": "Checklist filter badge.",
            },
        ]
        det = qc.detect_pr_compaction_candidates(items, tier="all")
        self.assertEqual(det["plan_count"], 0)
        self.assertTrue(qc.is_hub_file("web/index.html"))
        self.assertTrue(qc.is_hub_file("web/dashboard.js"))
        self.assertFalse(qc.is_hub_file("web/js/views/briefing.mjs"))

    def test_tier2_same_component_partial_wip_drain_respects_tier_filter(self) -> None:
        """Two partial-WIP items in same component sharing >=2 domain files qualify as Tier 2 (skipped by --tier 1)."""
        items = [
            {
                "conversation_id": "conn1111-0000-4000-8000-000000000001",
                "title": "Dynamic requirements.json connector discovery",
                "priority": 12,
                "importance": "high",
                "component": "connectors",
                "status": "pending",
                "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                "merged_repos": ["acme/repo-a"],
                "target_files": [
                    "api/routers/health.py",
                    "web/js/views/settings.mjs",
                    "web/index.html",
                    "tests/test_connectors_manifest.py",
                ],
                "proposed_fix": "Load requirements.json dynamically.",
            },
            {
                "conversation_id": "conn2222-0000-4000-8000-000000000002",
                "title": "Connector batch re-harvest & degraded brief recovery banner",
                "priority": 14,
                "importance": "high",
                "component": "connectors",
                "status": "pending",
                "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                "merged_repos": ["acme/repo-a"],
                "target_files": [
                    "api/routers/health.py",
                    "api/routers/portfolio/notebook.py",
                    "web/js/views/settings.mjs",
                    "web/js/views/notebook.mjs",
                    "web/index.html",
                    "tests/test_notebook_recovery.py",
                ],
                "proposed_fix": "Add batch open-expired button and recovery banner.",
            },
        ]
        det_t1 = qc.detect_pr_compaction_candidates(items, tier="1")
        self.assertEqual(det_t1["plan_count"], 0)

        det_all = qc.detect_pr_compaction_candidates(items, tier="all")
        self.assertEqual(det_all["plan_count"], 1)
        plan = det_all["plans"][0]
        self.assertEqual(plan["tier"], 2)
        self.assertEqual(plan["partial_wip_reduced"], 1)
        self.assertIn("partial_wip_drain", plan["reasons"])
        self.assertEqual(plan["absorber_id"], "conn1111-0000-4000-8000-000000000001")

        # True >=70% non-hub file subset qualifies as Tier 1 even without depends_on
        sub_a = {
            "conversation_id": "sub11111-0000-4000-8000-000000000001",
            "title": "Adoption refresh re-render fix",
            "priority": 10,
            "component": "adoption",
            "status": "pending",
            "repo_slugs": ["acme/repo-b"],
            "target_files": ["web/js/views/adoption.mjs", "tests/js/views_domains.test.mjs"],
            "proposed_fix": "Call renderMerlinPage on refresh.",
        }
        sub_b = {
            "conversation_id": "sub22222-0000-4000-8000-000000000002",
            "title": "Adoption Mika AI & Workflows columns",
            "priority": 11,
            "component": "adoption",
            "status": "pending",
            "repo_slugs": ["acme/repo-b"],
            "target_files": [
                "api/portfolio_helpers/adoption.py",
                "web/js/views/adoption.mjs",
                "web/css/views/adoption.css",
                "tests/js/views_domains.test.mjs",
            ],
            "proposed_fix": "Add Mika AI & Workflows columns.",
        }
        det_sub = qc.detect_pr_compaction_candidates([sub_a, sub_b], tier="1")
        self.assertEqual(det_sub["plan_count"], 1)
        self.assertEqual(det_sub["plans"][0]["tier"], 1)
        self.assertIn("domain_file_subset:100%", det_sub["plans"][0]["reasons"])

    def test_split_phase_multi_repo_compaction_and_ambiguous_file_refusal(self) -> None:
        """Pattern 2 cleanly splits unambiguous 2-repo item (G4 pattern) and refuses ambiguous file partitions."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            # Seed items teaching directory/test ownership for repo-a vs repo-b
            seed_items = [
                {
                    "conversation_id": "seed-ta1-0000-4000-8000-000000000001",
                    "title": "Seed TAM",
                    "status": "merged",
                    "repo_slugs": ["acme/repo-a"],
                    "target_files": ["clients/dto.py", "skills/portfolio/sync.py", "tests/test_portfolio_sync.py"],
                },
                {
                    "conversation_id": "seed-ct1-0000-4000-8000-000000000002",
                    "title": "Seed CT",
                    "status": "merged",
                    "repo_slugs": ["acme/repo-b"],
                    "target_files": ["api/routers/shared.py", "web/js/views/notebook.mjs", "tests/test_ct_notebook.py"],
                },
            ]
            # G4-style pair: 681 already merged in repo-a; feb depends on 681 and spans [repo-a, repo-b]
            ident_a = {
                "conversation_id": "id681111-0000-4000-8000-000000000001",
                "title": "Customer Business Profile UI",
                "priority": 13,
                "importance": "high",
                "component": "customer-identity",
                "status": "pending",
                "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                "merged_repos": ["acme/repo-a"],
                "depends_on": [],
                "target_files": [
                    "clients/dto.py",
                    "api/routers/shared.py",
                    "web/js/views/notebook.mjs",
                    "web/index.html",
                    "tests/test_ct_notebook.py",
                ],
                "section_anchors": ["repo-b:api/routers/shared.py::enrich"],
                "proposed_fix": "Expose sector and industry pill in repo-b.",
            }
            ident_b = {
                "conversation_id": "idfeb222-0000-4000-8000-000000000002",
                "title": "Customer Sector Type Badge (Private vs Public)",
                "priority": 15,
                "importance": "medium",
                "component": "customer-identity",
                "status": "pending",
                "repo_slugs": ["acme/repo-a", "acme/repo-b"],
                "merged_repos": [],
                "depends_on": ["id681111-0000-4000-8000-000000000001"],
                "target_files": [
                    "clients/dto.py",
                    "skills/portfolio/sync.py",
                    "tests/test_portfolio_sync.py",
                    "api/routers/shared.py",
                    "web/js/views/notebook.mjs",
                    "tests/test_ct_notebook.py",
                ],
                "section_anchors": [
                    "repo-a:clients/dto.py::CustomerAccountDTO",
                    "repo-b:api/routers/shared.py::enrich",
                ],
                "proposed_fix": "Phase 1 repo-a DTO + Phase 2 repo-b Private/Public pill.",
            }

            q_path = _write_queue_and_locks(state_dir, [*seed_items, ident_a, ident_b], locks={})
            res = qc.compact_dev_queue(portfolio_dir=state_dir, apply=True, tier="all", refresh_board=False)
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["mode"], "applied")
            self.assertEqual(res["applied_count"], 1)
            plan = res["applied_plans"][0]
            self.assertEqual(plan["pattern"], "split_phase_multi_repo")
            self.assertEqual(plan["target_repo"], "acme/repo-b")
            self.assertEqual(plan["split_repo"], "acme/repo-a")

            reloaded = json.loads(q_path.read_text(encoding="utf-8"))["items"]
            by_id = {it["conversation_id"]: it for it in reloaded}
            # idfeb222 stays pending, scoped strictly to repo-a with 0 cycle deps and calibrated P13
            self.assertEqual(by_id["idfeb222-0000-4000-8000-000000000002"]["status"], "pending")
            self.assertEqual(by_id["idfeb222-0000-4000-8000-000000000002"]["repo_slugs"], ["acme/repo-a"])
            self.assertEqual(by_id["idfeb222-0000-4000-8000-000000000002"]["depends_on"], [])
            self.assertEqual(by_id["idfeb222-0000-4000-8000-000000000002"]["priority"], 13)
            self.assertEqual(
                by_id["idfeb222-0000-4000-8000-000000000002"]["target_files"],
                ["clients/dto.py", "skills/portfolio/sync.py", "tests/test_portfolio_sync.py"],
            )
            # id681111 absorbs repo-b phase and now cleanly depends on idfeb222
            self.assertEqual(
                by_id["id681111-0000-4000-8000-000000000001"]["depends_on"],
                ["idfeb222-0000-4000-8000-000000000002"],
            )
            self.assertEqual(qc.validate_queue_invariants(reloaded), [])

            # Ambiguous file in multi-repo item MUST refuse Split-Phase fail-closed
            ambig_b = dict(ident_b)
            ambig_b["target_files"] = [*ident_b["target_files"], "tests/test_unknown_owner.py"]
            det_ambig = qc.detect_pr_compaction_candidates([*seed_items, ident_a, ambig_b], tier="all")
            self.assertEqual(det_ambig["plan_count"], 0)
            self.assertTrue(
                any("ambiguous_split_files" in e["reason"] for e in det_ambig["excluded_items"])
            )

    def test_hard_guardrails_block_security_oversize_optout_live_lock_and_indirect_cycle(self) -> None:
        """Fail-closed guardrails reject security items, oversize diffs, [no-compact], active locks, and cycles."""
        base_files = [
            "api/alerts.py",
            "web/js/views/alerts.mjs",
            "web/css/views/alerts.css",
            "tests/test_alerts.py",
        ]
        # 1. Security scope excluded
        sec_a = {
            "conversation_id": "sec11111-0000-4000-8000-000000000001",
            "title": "Global Reader Read-Only Guardrails",
            "priority": 10,
            "component": "alerts",
            "status": "pending",
            "repo_slugs": ["acme/repo-b"],
            "target_files": base_files,
            "proposed_fix": "Add read-only guardrails.",
        }
        norm_b = {
            "conversation_id": "nor22222-0000-4000-8000-000000000002",
            "title": "Alerts badge styling",
            "priority": 11,
            "component": "alerts",
            "status": "pending",
            "repo_slugs": ["acme/repo-b"],
            "target_files": base_files,
            "proposed_fix": "Style alert badges.",
        }
        self.assertEqual(qc.detect_pr_compaction_candidates([sec_a, norm_b])["plan_count"], 0)

        # 2. Explicit [no-compact] marker excluded
        opt_a = dict(norm_b, conversation_id="opt11111-0000-4000-8000-000000000001", notes="Hold separate [no-compact].")
        self.assertEqual(qc.detect_pr_compaction_candidates([opt_a, norm_b])["plan_count"], 0)

        # 3. Combined file ceiling (>16 files) rejected, while existing polish batch gets +2 grace (<=18)
        big_a = dict(
            norm_b,
            conversation_id="big11111-0000-4000-8000-000000000001",
            title="Traffic heatmap 35d cap",
            component="traffic",
            target_files=[f"web/js/views/traffic_{i}.mjs" for i in range(9)],
        )
        big_b = dict(
            norm_b,
            conversation_id="big22222-0000-4000-8000-000000000002",
            title="Traffic table alignment",
            component="traffic",
            depends_on=["big11111-0000-4000-8000-000000000001"],
            target_files=[
                "web/js/views/traffic_0.mjs",
                *[f"web/css/views/traffic_{i}.css" for i in range(8)],
            ],  # Combined = 17 > 16
        )
        self.assertEqual(qc.detect_pr_compaction_candidates([big_a, big_b], max_combined_files=16)["plan_count"], 0)
        batch_b = dict(big_b, title="UI Polish Batch (P19 x4): traffic alignment")
        self.assertEqual(qc.detect_pr_compaction_candidates([big_a, batch_b], max_combined_files=16)["plan_count"], 1)

        # 4. Active lock in Slot #2 blocks compaction cleanly
        pair_a = dict(norm_b, conversation_id="lck11111-0000-4000-8000-000000000001", title="Alerts column A")
        pair_b = dict(norm_b, conversation_id="lck22222-0000-4000-8000-000000000002", title="Alerts column B")
        active_locks = {
            "acme/repo-b": {
                "active": True,
                "conversation_id": "other000-0000-4000-8000-000000000000",
                "slots": {
                    "2": {"active": True, "conversation_id": "lck22222-0000-4000-8000-000000000002"},
                },
            }
        }
        self.assertEqual(qc.detect_pr_compaction_candidates([pair_a, pair_b], active_locks=active_locks)["plan_count"], 0)
        self.assertEqual(qc.detect_pr_compaction_candidates([pair_a, pair_b], active_locks={})["plan_count"], 1)

        # 5. Indirect dependency cycle guard (A -> X -> B cannot collapse A & B!)
        cyc_a = dict(norm_b, conversation_id="cyc11111-0000-4000-8000-000000000001", depends_on=["cycx0000-0000-4000-8000-000000000000"])
        cyc_x = {
            "conversation_id": "cycx0000-0000-4000-8000-000000000000",
            "title": "Intermediate unrelated item",
            "priority": 11,
            "component": "other",
            "status": "pending",
            "repo_slugs": ["acme/repo-c"],
            "depends_on": ["cyc22222-0000-4000-8000-000000000002"],
            "target_files": ["other/mod.py"],
            "proposed_fix": "Intermediate step.",
        }
        cyc_b = dict(norm_b, conversation_id="cyc22222-0000-4000-8000-000000000002")
        self.assertEqual(qc.detect_pr_compaction_candidates([cyc_a, cyc_x, cyc_b])["plan_count"], 0)

    def test_respect_pause_skips_auto_compaction_while_paused(self) -> None:
        """Orchestrator `--respect-pause` skips mutation during Graceful Drain pause."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            items = [
                {
                    "conversation_id": "pau11111-0000-4000-8000-000000000001",
                    "title": "Alerts toggle A",
                    "priority": 11,
                    "component": "alerts",
                    "status": "pending",
                    "repo_slugs": ["acme/repo-b"],
                    "target_files": ["web/js/views/alerts.mjs", "web/css/views/alerts.css"],
                    "proposed_fix": "Part A.",
                },
                {
                    "conversation_id": "pau22222-0000-4000-8000-000000000002",
                    "title": "Alerts toggle B",
                    "priority": 11,
                    "component": "alerts",
                    "status": "pending",
                    "repo_slugs": ["acme/repo-b"],
                    "depends_on": ["pau11111-0000-4000-8000-000000000001"],
                    "target_files": ["web/js/views/alerts.mjs", "web/css/views/alerts.css"],
                    "proposed_fix": "Part B.",
                },
            ]
            _write_queue_and_locks(state_dir, items, locks={})
            orch_ctrl.pause_orchestration(state_dir=state_dir, reason="Testing pause")

            res_paused = qc.compact_dev_queue(
                portfolio_dir=state_dir,
                apply=True,
                respect_pause=True,
                refresh_board=False,
            )
            self.assertEqual(res_paused["mode"], "skipped_paused")
            self.assertTrue(res_paused["paused"])
            self.assertEqual(res_paused["applied_count"], 0)

            # Manual override without respect_pause still applies cleanly
            res_manual = qc.compact_dev_queue(
                portfolio_dir=state_dir,
                apply=True,
                respect_pause=False,
                refresh_board=False,
            )
            self.assertEqual(res_manual["mode"], "applied")
            self.assertEqual(res_manual["applied_count"], 1)

    def test_optimizer_finding_cli_subcommands_tui_overlay_and_docs_contract(self) -> None:
        """Verify `queue-optimize`, `pr_remediator.py queue-compact`, `agy-queue compact`, TUI `[x]` overlay & docs."""
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            items = [
                {
                    "conversation_id": "opt11111-0000-4000-8000-000000000001",
                    "title": "Radars card update",
                    "priority": 13,
                    "component": "radars",
                    "status": "pending",
                    "repo_slugs": ["acme/repo-b"],
                    "target_files": ["api/routers/general.py", "web/js/views/radars.mjs", "web/css/views/radars.css"],
                    "proposed_fix": "Card link.",
                },
                {
                    "conversation_id": "opt22222-0000-4000-8000-000000000002",
                    "title": "Radars modal cleanup",
                    "priority": 14,
                    "component": "radars",
                    "status": "pending",
                    "repo_slugs": ["acme/repo-b"],
                    "depends_on": ["opt11111-0000-4000-8000-000000000001"],
                    "target_files": ["api/routers/general.py", "web/js/views/radars.mjs", "web/css/views/radars.css"],
                    "proposed_fix": "Modal cleanup.",
                },
            ]
            _write_queue_and_locks(state_dir, items, locks={})

            # 1. dev_flow_optimizer surfaces `pr_compaction_candidates` + OPT hint
            analysis = flow_opt.analyze_dev_flow_optimizations(portfolio_dir=state_dir, lightweight=True)
            codes = {f["code"] for f in analysis["findings"]}
            self.assertIn("pr_compaction_candidates", codes)
            self.assertIn("compact -1 PRs", analysis["top_hint"])

            # 2. TUI `[x]` Flow Optimizer overlay renders read-only compaction plan bullet
            overlay_txt = "\n".join(tui._render_optimizer_overlay_lines(analysis, 120, False))
            self.assertIn("Compact 1 PR group(s)", overlay_txt)
            self.assertIn("[T1]", overlay_txt)
            self.assertIn("opt11111", overlay_txt)
            self.assertIn("opt22222", overlay_txt)

            # 3. `pr_remediator.py queue-compact` CLI parser & dispatch
            parsed = prs.parse_args(["queue-compact", "--tier", "all", "--respect-pause", "--json"])
            self.assertEqual(parsed.subcommand, "queue-compact")
            self.assertTrue(parsed.respect_pause)

            # 4. `agy-queue compact` CLI dry-run + --apply
            self.assertIn("compact", tui.AGY_QUEUE_SUBCOMMANDS)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = tui.main(["compact", "--state-dir", str(state_dir), "--json"])
            self.assertEqual(rc, 0)
            out_dry = json.loads(buf.getvalue())
            self.assertEqual(out_dry["mode"], "dry_run")
            self.assertEqual(out_dry["plan_count"], 1)

            buf_text = io.StringIO()
            with contextlib.redirect_stdout(buf_text):
                rc_apply = tui.main(["compact", "--apply", "--state-dir", str(state_dir), "--no-color"])
            self.assertEqual(rc_apply, 0)
            self.assertIn("APPLIED", buf_text.getvalue())

        # 5. Agent & skill docs contract (<24,000 bytes limit strictly preserved)
        orch_doc = (REPO_ROOT / "agents" / "agy-dev-orchestrator.md").read_text(encoding="utf-8")
        plan_doc = (REPO_ROOT / "agents" / "agy-dev-planner.md").read_text(encoding="utf-8")
        skill_path = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "SKILL.md"
        skill_doc = skill_path.read_text(encoding="utf-8")
        self.assertIn("queue-compact --apply --tier all --respect-pause", orch_doc)
        self.assertIn("queue-compact --dry-run", plan_doc)
        self.assertIn("queue-compact", skill_doc)
        self.assertLess(skill_path.stat().st_size, 24000)


if __name__ == "__main__":
    unittest.main()
