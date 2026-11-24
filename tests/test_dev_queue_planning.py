"""Hermetic Zero-PII tests for Dev Queue Component Planning View (`dev_queue_planner.py`, CLI, sidecar & docs)."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
CLIENTS_DIR = REPO_ROOT / "clients"
REM_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
TUI_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "scripts"
SIDECAR_DIR = REPO_ROOT / "sidecars" / "dev-queue-panel"
TEST_SCRIPTS_DIR = REPO_ROOT / "tests" / "scripts"

for _p in (CLIENTS_DIR, REM_SCRIPTS_DIR, TUI_SCRIPTS_DIR, SIDECAR_DIR, TEST_SCRIPTS_DIR):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

import dev_queue_planner as planner  # noqa: E402
import dev_queue_tui as tui  # noqa: E402
import pr_remediator as prs  # noqa: E402
import selective_test_runner as srunner  # noqa: E402
import server as panel_srv  # noqa: E402


class TestDevQueuePlannerCore(unittest.TestCase):
    """Verify deterministic 0-LLM importance, subcomponent, wave, and hierarchy snapshot logic."""

    def test_infer_importance_explicit_and_deterministic_fallbacks(self) -> None:
        exp = planner.infer_importance({"importance": "LOW", "priority": 10}, blocks_count=3)
        self.assertEqual(exp["importance"], "low")
        self.assertEqual(exp["source"], "explicit")
        self.assertEqual(exp["reasons"], ["explicit:low"])

        crit_prio = planner.infer_importance({"priority": 12})
        self.assertEqual(crit_prio["importance"], "critical")
        self.assertIn("priority_band:P12<=17", crit_prio["reasons"])

        crit_blocks = planner.infer_importance({"priority": 55}, blocks_count=2)
        self.assertEqual(crit_blocks["importance"], "critical")
        self.assertIn("blocks_count:2>=2", crit_blocks["reasons"])

        high_block_one = planner.infer_importance({"priority": 50}, blocks_count=1)
        self.assertEqual(high_block_one["importance"], "high")
        self.assertIn("blocks_count:1", high_block_one["reasons"])

        high_partial = planner.infer_importance(
            {"priority": 50, "repo_slugs": ["org/a", "org/b"], "merged_repos": ["org/a"]}
        )
        self.assertEqual(high_partial["importance"], "high")
        self.assertIn("partial_wip", high_partial["reasons"])

        med = planner.infer_importance({"priority": 50})
        self.assertEqual(med["importance"], "medium")

        low = planner.infer_importance({"priority": 80})
        self.assertEqual(low["importance"], "low")

    def test_infer_subcomponent_explicit_anchor_paths_and_zero_pii(self) -> None:
        exp = planner.infer_subcomponent({"component": "billing-api", "title": "[#ignored] Foo"})
        self.assertEqual(exp, {"component": "billing-api", "source": "explicit"})

        anc = planner.infer_subcomponent({"title": "[#dev-queue-panel] Add Planning View"})
        self.assertEqual(anc, {"component": "dev-queue-panel", "source": "anchor"})

        # Numeric Slot #1 / PR #42 markers must NOT be treated as component anchors
        slot_num = planner.infer_subcomponent(
            {"title": "Fix Slot #1 lock handling", "target_files": ["sidecars/dev-queue-panel/server.py"]}
        )
        self.assertEqual(slot_num, {"component": "dev-queue-panel", "source": "path"})

        # Prefer non-test target files over tests/
        mixed_paths = planner.infer_subcomponent(
            {
                "title": "Harden oauth flow",
                "target_files": [
                    "tests/test_oauth.py",
                    "tests/test_tokens.py",
                    "src/auth/oauth.py",
                ],
            }
        )
        self.assertEqual(mixed_paths, {"component": "auth", "source": "path"})

        # Zero-PII scrubbing when path contains local user home path
        mock_pii_path = f"{os.sep}Users{os.sep}realperson{os.sep}Code{os.sep}org{os.sep}repo{os.sep}src{os.sep}payments{os.sep}stripe.py"
        pii_comp = planner.infer_subcomponent({"title": "Payment fix", "target_files": [mock_pii_path]})
        self.assertEqual(pii_comp["component"], "payments")
        self.assertNotIn("realperson", json.dumps(pii_comp))

        fallback = planner.infer_subcomponent({"title": "General cleanup"})
        self.assertEqual(fallback, {"component": "general", "source": "default"})

    def test_classify_execution_wave_ordering(self) -> None:
        self.assertEqual(planner.classify_execution_wave({"status": "in_progress"}), "active")
        self.assertEqual(
            planner.classify_execution_wave({"status": "pending"}, has_active_lock=True),
            "active",
        )
        self.assertEqual(
            planner.classify_execution_wave(
                {"status": "pending", "repo_slugs": ["org/a", "org/b"], "merged_repos": ["org/a"]}
            ),
            "partial_wip",
        )
        self.assertEqual(planner.classify_execution_wave({"status": "pending"}), "ready")
        self.assertEqual(
            planner.classify_execution_wave({"status": "pending"}, open_unmet_deps=["dep-1"]),
            "blocked",
        )
        self.assertEqual(planner.classify_execution_wave({"status": "hitl_blocked"}), "blocked")
        self.assertEqual(planner.classify_execution_wave({"status": "standby"}), "standby")

    def test_build_component_planning_snapshot_hierarchy_deps_and_recent_done(self) -> None:
        cid_active = "aaaa1111-1111-4111-8111-111111111111"
        cid_blocker = "bbbb2222-2222-4222-8222-222222222222"
        cid_dependent = "cccc3333-3333-4333-8333-333333333333"
        cid_standby = "dddd4444-4444-4444-8444-444444444444"
        cid_done_1 = "eeee5555-5555-4555-8555-555555555555"
        cid_done_2 = "ffff6666-6666-4666-8666-666666666666"
        cid_cancelled = "99997777-7777-4777-8777-777777777777"

        items = [
            {
                "conversation_id": cid_dependent,
                "title": "Follow-up UI polish",
                "priority": 40,
                "status": "pending",
                "repo_slugs": ["org/repo-b"],
                "component": "panel-ui",
                "depends_on": [cid_blocker[:8]],
                "added_at": "2026-11-24T01:00:00Z",
            },
            {
                "conversation_id": cid_blocker,
                "title": "Core API schema",
                "priority": 50,
                "status": "pending",
                "repo_slugs": ["org/repo-a"],
                "component": "api",
                "added_at": "2026-11-24T01:05:00Z",
            },
            {
                "conversation_id": cid_active,
                "title": "Live endpoint fix",
                "priority": 25,
                "status": "in_progress",
                "repo_slugs": ["org/repo-a"],
                "component": "api",
                "added_at": "2026-11-24T01:10:00Z",
            },
            {
                "conversation_id": cid_standby,
                "title": "Deferred refactor",
                "priority": 15,
                "importance": "critical",
                "status": "standby",
                "repo_slugs": ["org/repo-a"],
                "component": "api",
                "added_at": "2026-11-24T01:15:00Z",
            },
            {
                "conversation_id": cid_done_1,
                "title": "Merged API v1",
                "priority": 30,
                "status": "merged",
                "repo_slugs": ["org/repo-a"],
                "component": "api",
                "pr_urls": ["https://github.com/org/repo-a/pull/101"],
                "updated_at": "2026-11-24T02:00:00Z",
            },
            {
                "conversation_id": cid_done_2,
                "title": "Cleaned UI v1",
                "priority": 35,
                "status": "cleaned",
                "repo_slugs": ["org/repo-b"],
                "component": "panel-ui",
                "updated_at": "2026-11-24T03:00:00Z",
            },
            {
                "conversation_id": cid_cancelled,
                "title": "Superseded task",
                "priority": 60,
                "status": "cancelled",
                "repo_slugs": ["org/repo-a"],
                "component": "api",
                "updated_at": "2026-11-24T04:00:00Z",
            },
        ]

        snap = planner.build_component_planning_snapshot(items, include_done=1)
        self.assertEqual(snap["totals"]["open_count"], 4)
        self.assertEqual(snap["totals"]["total_done_count"], 2)
        self.assertEqual(snap["totals"]["by_wave"], {"active": 1, "partial_wip": 0, "ready": 1, "blocked": 1, "standby": 1})

        # Recently integrated keeps total_done_count=2 while slicing items to include_done=1 (most recent first)
        recent = snap["recently_integrated"]
        self.assertEqual(recent["total_done_count"], 2)
        self.assertEqual(len(recent["items"]), 1)
        self.assertEqual(recent["items"][0]["conversation_id"], cid_done_2)

        # Repo-A -> api component metrics & sorting (active -> ready -> standby)
        repo_a = next(r for r in snap["repos"] if r["repo_slug"] == "org/repo-a")
        api_comp = repo_a["components"][0]
        self.assertEqual(api_comp["name"], "api")
        self.assertEqual(api_comp["open_count"], 3)
        self.assertEqual(api_comp["done_count"], 1)
        self.assertEqual(api_comp["completion_pct"], 25)
        self.assertEqual(
            [it["conversation_id"] for it in api_comp["items"]],
            [cid_active, cid_blocker, cid_standby],
        )

        blocker_entry = next(it for it in api_comp["items"] if it["conversation_id"] == cid_blocker)
        self.assertEqual(blocker_entry["blocks_count"], 1)
        self.assertEqual(blocker_entry["unblocks_label"], "Unblocks 1")
        self.assertEqual(blocker_entry["importance"], "high")
        self.assertEqual(blocker_entry["effective_priority"], 40)

        # Repo filtering must preserve cross-repo blocks_count from org/repo-b dependent!
        filtered = planner.build_component_planning_snapshot(items, repo_filter="repo-a")
        self.assertEqual(filtered["totals"]["open_count"], 3)
        f_blocker = filtered["repos"][0]["components"][0]["items"][1]
        self.assertEqual(f_blocker["conversation_id"], cid_blocker)
        self.assertEqual(f_blocker["blocks_count"], 1)


class TestQueueCliAndTuiPlanIntegration(unittest.TestCase):
    """Verify `queue-add`, `queue-set-status`, `queue-plan`, and `agy-queue plan` CLI contracts."""

    def test_queue_cli_importance_component_and_queue_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            state_dir.mkdir(parents=True)
            cid1 = "11111111-1111-4111-8111-111111111111"
            cid2 = "22222222-2222-4222-8222-222222222222"

            with mock.patch.dict(os.environ, {"AGY_DEV_STATE_DIR": str(state_dir)}):
                buf1 = io.StringIO()
                with contextlib.redirect_stdout(buf1):
                    rc1 = prs.main(
                        [
                            "queue-add",
                            "--id",
                            cid1,
                            "--title",
                            "Add planning schema",
                            "--priority",
                            "30",
                            "--importance",
                            "critical",
                            "--component",
                            "queue-planner",
                            "--repos",
                            "carlosrodlop/agy-dev-plugin",
                            "--status",
                            "pending",
                        ]
                    )
                self.assertEqual(rc1, 0)
                add_res = json.loads(buf1.getvalue())
                self.assertEqual(add_res["item"]["importance"], "critical")
                self.assertEqual(add_res["item"]["component"], "queue-planner")

                # Backward-compatible add without --importance / --component
                buf2 = io.StringIO()
                with contextlib.redirect_stdout(buf2):
                    rc2 = prs.main(
                        [
                            "queue-add",
                            "--id",
                            cid2,
                            "--title",
                            "Legacy item",
                            "--priority",
                            "55",
                            "--repos",
                            "carlosrodlop/agy-dev-plugin",
                            "--depends-on",
                            cid1,
                        ]
                    )
                self.assertEqual(rc2, 0)
                legacy_item = json.loads(buf2.getvalue())["item"]
                self.assertNotIn("importance", legacy_item)
                self.assertNotIn("component", legacy_item)

                # Update via queue-set-status
                buf3 = io.StringIO()
                with contextlib.redirect_stdout(buf3):
                    rc3 = prs.main(
                        [
                            "queue-set-status",
                            "--id",
                            cid2,
                            "--status",
                            "in_progress",
                            "--importance",
                            "high",
                            "--component",
                            "sidecar-panel",
                        ]
                    )
                self.assertEqual(rc3, 0)
                upd_item = json.loads(buf3.getvalue())["item"]
                self.assertEqual(upd_item["importance"], "high")
                self.assertEqual(upd_item["component"], "sidecar-panel")

                # pr_remediator.py queue-plan
                buf_plan = io.StringIO()
                with contextlib.redirect_stdout(buf_plan):
                    rc_plan = prs.main(["queue-plan", "--importance", "critical", "--json"])
                self.assertEqual(rc_plan, 0)
                plan_data = json.loads(buf_plan.getvalue())
                self.assertEqual(plan_data["status"], "ok")
                self.assertEqual(plan_data["totals"]["open_count"], 1)
                self.assertEqual(plan_data["repos"][0]["components"][0]["items"][0]["blocks_count"], 1)

            # agy-queue plan --json and text mode
            buf_tui_json = io.StringIO()
            with contextlib.redirect_stdout(buf_tui_json):
                rc_tui_j = tui.main(["plan", "--state-dir", str(state_dir), "--json"])
            self.assertEqual(rc_tui_j, 0)
            tui_json = json.loads(buf_tui_json.getvalue())
            self.assertEqual(tui_json["totals"]["open_count"], 2)

            buf_tui_txt = io.StringIO()
            with contextlib.redirect_stdout(buf_tui_txt):
                rc_tui_t = tui.main(["plan", "--state-dir", str(state_dir), "--no-color"])
            self.assertEqual(rc_tui_t, 0)
            txt = buf_tui_txt.getvalue()
            self.assertIn("AGY-QUEUE COMPONENT PLAN", txt)
            self.assertIn("queue-planner", txt)
            self.assertIn("Unblocks 1", txt)


class TestSidecarPlanningPayloadAndFrontendContract(unittest.TestCase):
    """Verify Zero-PII `planning` payload in `build_panel_state()`, XSS-safe DOM UI, and doc sizes."""

    def test_build_panel_state_includes_zero_pii_planning_and_etag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            st_dir = root / "state"
            st_dir.mkdir(parents=True)
            fake_home = root / "fake_home"
            fake_home.mkdir()
            conv_db = root / "empty.db"

            cid_open = "aaaaaaaa-1111-4111-8111-111111111111"
            cid_done = "bbbbbbbb-2222-4222-8222-222222222222"
            mock_pii = f"{os.sep}Users{os.sep}realperson{os.sep}Code{os.sep}org{os.sep}repo{os.sep}src{os.sep}auth{os.sep}login.py"

            (st_dir / "dev_queue.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "conversation_id": cid_open,
                                "title": f"Fix auth in {mock_pii}",
                                "priority": 22,
                                "status": "in_progress",
                                "repo_slugs": ["org/repo"],
                                "target_files": [mock_pii],
                                "pr_urls": ["https://github.com/org/repo/compare/main...fix?expand=1"],
                            },
                            {
                                "conversation_id": cid_done,
                                "title": "Merged auth schema",
                                "priority": 20,
                                "status": "merged",
                                "repo_slugs": ["org/repo"],
                                "component": "auth",
                                "pr_urls": ["https://github.com/org/repo/pull/9"],
                                "updated_at": "2026-11-24T05:00:00Z",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (st_dir / "actions_state.json").write_text(
                json.dumps({"actions_log_audit_state": {"dev_pr_locks": {}}}),
                encoding="utf-8",
            )

            snap = panel_srv.build_panel_state(
                state_dir=st_dir,
                conv_db_path=conv_db,
                brain_dir=root / "brain",
                home_dir=fake_home,
            )
            self.assertIn("planning", snap)
            planning = snap["planning"]
            self.assertEqual(planning["totals"]["open_count"], 1)
            self.assertEqual(planning["recently_integrated"]["total_done_count"], 1)

            # Unreviewed compare PR URL must NOT leak onto in_progress planning item
            open_item = planning["repos"][0]["components"][0]["items"][0]
            self.assertEqual(open_item["pr_url"], "")
            self.assertEqual(open_item["component"], "auth")

            # Zero-PII check across serialized planning payload
            dumped = json.dumps(planning)
            self.assertNotIn("realperson", dumped)

            # ETag hit returns unchanged=True
            snap_cached = panel_srv.build_panel_state(
                state_dir=st_dir,
                conv_db_path=conv_db,
                brain_dir=root / "brain",
                home_dir=fake_home,
                client_etag=snap["etag"],
            )
            self.assertTrue(snap_cached["unchanged"])

    def test_frontend_planning_tab_xss_safety_docs_and_router_integrity(self) -> None:
        html = (SIDECAR_DIR / "index.html").read_text(encoding="utf-8")
        js = (SIDECAR_DIR / "app.js").read_text(encoding="utf-8")
        css = (SIDECAR_DIR / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="dq-kpi-planning"', html)
        self.assertIn('id="dq-sec-planning"', html)
        self.assertIn('id="dq-planning-done"', html)
        self.assertIn("renderPlanningView", js)
        self.assertIn("Recently Integrated", js)
        self.assertIn("dq-planning-filters", css)
        for unsafe_sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function("):
            self.assertNotIn(unsafe_sink, js)

        # Docs stay strictly < 24,000 bytes
        for doc_rel in ("agents/agy-dev-planner.md", "skills/agy-dev-queue-orchestrator/SKILL.md"):
            doc_path = REPO_ROOT / doc_rel
            self.assertLess(doc_path.stat().st_size, 24000, msg=f"{doc_rel} exceeds 24,000-byte limit")

        # Selective test runner integrity & routing
        self.assertEqual(srunner.audit_router_integrity(REPO_ROOT), [])
        self.assertIn(
            "tests.test_dev_queue_planning",
            srunner.select_test_modules(["skills/agy-code-remediation-pr/scripts/dev_queue_planner.py"]),
        )
        self.assertIn(
            "tests.test_dev_queue_planning",
            srunner.select_test_modules(["sidecars/dev-queue-panel/app.js"]),
        )


if __name__ == "__main__":
    unittest.main()
