#!/usr/bin/env python3
"""Hermetic unit tests for parallel slot unlock upgrades (HTML leaf-section, barrel TRANSITIVE, queue_store & leases)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-code-remediation-pr" / "scripts"
CLIENTS_DIR = REPO_ROOT / "clients"
for _p in (str(SCRIPTS_DIR), str(CLIENTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import dev_common  # noqa: E402
import parallel_scope as pscope  # noqa: E402
import pr_remediator as prs  # noqa: E402


def _build_spa_fixture_repo(root: Path) -> Path:
    """Create a synthetic Zero-PII multi-view SPA + Python API fixture repo for overlap testing."""
    (root / "web" / "js" / "views").mkdir(parents=True, exist_ok=True)
    (root / "web" / "js" / "core").mkdir(parents=True, exist_ok=True)
    (root / "api" / "routers").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "scripts").mkdir(parents=True, exist_ok=True)

    # Synthetic >250-line HTML monolith with root wrapper `<main id="app-shell">` and disjoint page sections
    pad_a = "\n".join(f"      <div class='row-a-{i}'>Alpha {i}</div>" for i in range(95))
    pad_b = "\n".join(f"      <div class='row-b-{i}'>Beta {i}</div>" for i in range(95))
    pad_c = "\n".join(f"      <div class='row-c-{i}'>Gamma {i}</div>" for i in range(85))
    (root / "web" / "index.html").write_text(
        f"""<!doctype html>
<html>
<head><title>Acme Control</title></head>
<body>
  <header id="topbar"><div id="topbar-utility-right"></div></header>
  <nav id="sidebar"><a href="#briefing">Briefing</a><a href="#features">Features</a></nav>
  <main id="app-shell">
    <section id="page-briefing">
      <h2>Dispatch Briefing</h2>
      <div id="briefing-weather-slot"></div>
{pad_a}
    </section>
    <section id="page-features">
      <h2>Features Matrix</h2>
      <div id="features-score-slot"></div>
{pad_b}
    </section>
    <section id="page-notebook">
      <h2>Customer Notebook</h2>
      <div id="notebook-notes-canvas"></div>
{pad_c}
    </section>
  </main>
</body>
</html>
""",
        encoding="utf-8",
    )

    (root / "web" / "js" / "core" / "helpers.mjs").write_text(
        "export function formatDateIso(v) { return String(v || ''); }\n",
        encoding="utf-8",
    )
    (root / "web" / "js" / "core" / "index.mjs").write_text(
        "export { formatDateIso } from './helpers.mjs';\n",
        encoding="utf-8",
    )
    (root / "web" / "js" / "views" / "briefing.mjs").write_text(
        "import { formatDateIso } from '../core/index.mjs';\n"
        "export function renderBriefingView() { return formatDateIso('2026-01-01'); }\n",
        encoding="utf-8",
    )
    (root / "web" / "js" / "views" / "features.mjs").write_text(
        "import { formatDateIso } from '../core/index.mjs';\n"
        "export function renderFeaturesView() { return formatDateIso('2026-01-02'); }\n",
        encoding="utf-8",
    )
    (root / "web" / "js" / "views" / "notebook.mjs").write_text(
        "export function renderNotebookView() { return 'ok'; }\n",
        encoding="utf-8",
    )

    (root / "api" / "__init__.py").write_text("", encoding="utf-8")
    (root / "api" / "routers" / "__init__.py").write_text(
        "from .briefing import get_briefing_rows\nfrom .notebook import get_notebook_view\n",
        encoding="utf-8",
    )
    (root / "api" / "routers" / "briefing.py").write_text(
        "def get_briefing_rows():\n    return []\n",
        encoding="utf-8",
    )
    (root / "api" / "routers" / "notebook.py").write_text(
        "from api.routers.briefing import get_briefing_rows\n\n"
        "def get_notebook_view():\n    return {'rows': get_briefing_rows()}\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'acme-tower'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    (root / "tests" / "scripts" / "selective_test_runner.py").write_text(
        "ALL_TEST_MODULES = ['tests.test_alpha']\n",
        encoding="utf-8",
    )
    return root


def _write_queue_and_locks(
    state_dir: Path,
    *,
    items: list[dict[str, object]],
    locks: dict[str, dict[str, object]],
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    dev_common.atomic_write_json(
        state_dir / "dev_queue.json",
        {"schema_version": 1, "items": items},
    )
    dev_common.atomic_write_json(
        state_dir / "actions_state.json",
        {"actions_log_audit_state": {"dev_pr_locks": locks}},
    )


class TestParallelScopeHtmlLeafAndBarrels(unittest.TestCase):
    """Verify HTML leaf-section inference, additive registries, and barrel TRANSITIVE rules."""

    def test_html_disjoint_view_sections_unlock_transitive_ignoring_root_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_spa_fixture_repo(Path(tmp))
            task_briefing = {
                "conversation_id": "11111111-aaaa-4aaa-8aaa-111111111111",
                "title": "Briefing weather widget",
                "proposed_fix": "Add weather pill inside #briefing-weather-slot on #dispatch briefing view.",
                "target_files": ["web/index.html", "web/js/views/briefing.mjs"],
            }
            task_features = {
                "conversation_id": "22222222-bbbb-4bbb-8bbb-222222222222",
                "title": "Features scoring card",
                "proposed_fix": "Render scoring breakdown inside #features-score-slot on #features view.",
                "target_files": ["web/index.html", "web/js/views/features.mjs"],
            }
            res = pscope.analyze_pair_overlap(task_briefing, task_features, repo_dir=repo)
            self.assertEqual(res.overlap_tier, pscope.OVERLAP_TRANSITIVE)
            self.assertTrue(res.parallel_safe)
            self.assertTrue(res.monolith_disjoint)
            self.assertIn("web/index.html", res.disjoint_monolith_files)
            self.assertEqual(res.colliding_files, ())

    def test_html_global_shell_cues_fail_closed_when_both_touch_sidebar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_spa_fixture_repo(Path(tmp))
            task_a = {
                "conversation_id": "11111111-aaaa-4aaa-8aaa-111111111111",
                "title": "Sidebar navigation subtitles",
                "proposed_fix": "Update sidebar cluster subtitles inside #sidebar.",
                "target_files": ["web/index.html"],
            }
            task_b = {
                "conversation_id": "22222222-bbbb-4bbb-8bbb-222222222222",
                "title": "Rebrand sidebar brand subtitle",
                "proposed_fix": "Update brand-subtitle and sidebar header in web/index.html.",
                "target_files": ["web/index.html"],
            }
            res = pscope.analyze_pair_overlap(task_a, task_b, repo_dir=repo)
            self.assertEqual(res.overlap_tier, pscope.OVERLAP_DEEP_SHARED)
            self.assertFalse(res.parallel_safe)
            self.assertIn("web/index.html", res.colliding_files)

    def test_additive_registry_files_allow_transitive_unless_explicit_lines_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_spa_fixture_repo(Path(tmp))
            res_ok = pscope.analyze_pair_overlap(
                {
                    "conversation_id": "11111111-aaaa",
                    "target_files": ["pyproject.toml", "tests/scripts/selective_test_runner.py"],
                },
                {
                    "conversation_id": "22222222-bbbb",
                    "target_files": ["pyproject.toml", "tests/scripts/selective_test_runner.py"],
                },
                repo_dir=repo,
            )
            self.assertEqual(res_ok.overlap_tier, pscope.OVERLAP_TRANSITIVE)
            self.assertTrue(res_ok.parallel_safe)

            res_line_hit = pscope.analyze_pair_overlap(
                {"conversation_id": "11111111-aaaa", "target_files": ["pyproject.toml#L1-L3"]},
                {"conversation_id": "22222222-bbbb", "target_files": ["pyproject.toml#L2-L3"]},
                repo_dir=repo,
            )
            self.assertEqual(res_line_hit.overlap_tier, pscope.OVERLAP_DEEP_SHARED)
            self.assertFalse(res_line_hit.parallel_safe)

    def test_barrel_reexport_and_unmentioned_direct_import_unlock_transitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_spa_fixture_repo(Path(tmp))
            # Barrel: features adds a new export in web/js/core/index.mjs while briefing imports formatDateIso
            res_barrel = pscope.analyze_pair_overlap(
                {
                    "conversation_id": "11111111-aaaa",
                    "title": "Briefing weather",
                    "proposed_fix": "Update renderBriefingView with weather card.",
                    "target_files": ["web/js/views/briefing.mjs"],
                },
                {
                    "conversation_id": "22222222-bbbb",
                    "title": "Features scoring module",
                    "proposed_fix": "Export computeReleaseScore from web/js/core/index.mjs for features.mjs.",
                    "target_files": ["web/js/core/index.mjs", "web/js/views/features.mjs"],
                },
                repo_dir=repo,
            )
            self.assertEqual(res_barrel.overlap_tier, pscope.OVERLAP_TRANSITIVE)
            self.assertTrue(res_barrel.parallel_safe)

            # Barrel modifying the exact imported symbol MUST still escalate to INTERFACE_SHARED
            res_barrel_hit = pscope.analyze_pair_overlap(
                {
                    "conversation_id": "11111111-aaaa",
                    "target_files": ["web/js/views/briefing.mjs"],
                },
                {
                    "conversation_id": "22222222-bbbb",
                    "proposed_fix": "Change formatDateIso re-export signature in web/js/core/index.mjs.",
                    "target_files": ["web/js/core/index.mjs"],
                },
                repo_dir=repo,
            )
            self.assertEqual(res_barrel_hit.overlap_tier, pscope.OVERLAP_INTERFACE_SHARED)
            self.assertFalse(res_barrel_hit.parallel_safe)

            # Direct Python import: notebook.py imports get_briefing_rows from briefing.py;
            # weather task adds a brand-new endpoint without mentioning get_briefing_rows -> TRANSITIVE
            res_unmentioned = pscope.analyze_pair_overlap(
                {
                    "conversation_id": "11111111-aaaa",
                    "title": "Weather API",
                    "proposed_fix": "Add POST /api/v1/portfolio/weather endpoint in api/routers/briefing.py.",
                    "target_files": ["api/routers/briefing.py"],
                },
                {
                    "conversation_id": "22222222-bbbb",
                    "title": "Notebook ad-hoc docs",
                    "proposed_fix": "Expose adhoc_prep_docs in api/routers/notebook.py.",
                    "target_files": ["api/routers/notebook.py"],
                },
                repo_dir=repo,
            )
            self.assertEqual(res_unmentioned.overlap_tier, pscope.OVERLAP_TRANSITIVE)
            self.assertTrue(res_unmentioned.parallel_safe)

            # Empty-prose unanchored upstream MUST still fail closed to INTERFACE_SHARED
            res_empty_ctx = pscope.analyze_pair_overlap(
                {"conversation_id": "11111111-aaaa", "target_files": ["api/routers/briefing.py"]},
                {"conversation_id": "22222222-bbbb", "target_files": ["api/routers/notebook.py"]},
                repo_dir=repo,
            )
            self.assertEqual(res_empty_ctx.overlap_tier, pscope.OVERLAP_INTERFACE_SHARED)
            self.assertFalse(res_empty_ctx.parallel_safe)

    def test_compound_anchors_and_barrel_aggregator_importer_unlock_transitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = _build_spa_fixture_repo(Path(tmp))
            # Verify compound `::id1,id2`, `::sym:L10-L20`, and single-colon `:sym` / `:#id` parsing
            rel_a, r_a, s_a = pscope.parse_target_anchor_entries("web/index.html::page-notebook,notebook-notes-canvas")
            self.assertEqual(rel_a, "web/index.html")
            self.assertEqual(r_a, [])
            self.assertEqual(s_a, ["page-notebook", "notebook-notes-canvas"])

            rel_b, r_b, s_b = pscope.parse_target_anchor_entries("web/index.html::sidebar:L5-L7")
            self.assertEqual(rel_b, "web/index.html")
            self.assertEqual(r_b, [(5, 7)])
            self.assertEqual(s_b, ["sidebar"])

            rel_c, r_c, s_c = pscope.parse_target_anchor_entries("web/index.html:#page-features")
            self.assertEqual(rel_c, "web/index.html")
            self.assertEqual(r_c, [])
            self.assertEqual(s_c, ["page-features"])

            # Aggregator barrel (`api/routers/__init__.py` touched on line 1, briefing import only)
            # importing `notebook.py` (`get_notebook_view` on line 2) -> TRANSITIVE!
            (repo / "api" / "routers" / "__init__.py").write_text(
                "from .briefing import get_briefing_rows\n"
                "# spacer 1\n# spacer 2\n# spacer 3\n# spacer 4\n"
                "from .notebook import get_notebook_view\n",
                encoding="utf-8",
            )
            res_agg = pscope.analyze_pair_overlap(
                {
                    "conversation_id": "11111111-aaaa",
                    "target_files": ["api/routers/__init__.py#L1-L1", "web/index.html"],
                    "section_anchors": ["web/index.html:#page-briefing"],
                },
                {
                    "conversation_id": "22222222-bbbb",
                    "target_files": ["api/routers/notebook.py::get_notebook_view", "web/index.html"],
                    "section_anchors": ["web/index.html::page-notebook,notebook-notes-canvas"],
                },
                repo_dir=repo,
            )
            self.assertEqual(res_agg.overlap_tier, pscope.OVERLAP_TRANSITIVE)
            self.assertTrue(res_agg.parallel_safe)
            self.assertIn("web/index.html", res_agg.disjoint_monolith_files)


class TestQueueStoreParallelDispatchFixes(unittest.TestCase):
    """Verify canonical repo layer ordering, frozen-contract downstream advance, and idle-repo WIP overflow."""

    def test_canonical_repo_layer_order_unblocks_reversed_plugin_before_tower(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            cid_reversed = "aaaa1111-1111-4111-8111-111111111111"
            _write_queue_and_locks(
                state_dir,
                items=[
                    {
                        "conversation_id": cid_reversed,
                        "title": "Settings Tools Page (reversed repo_slugs)",
                        "priority": 17,
                        "status": "pending",
                        "repo_slugs": ["acme/ops-control-tower", "acme/ops-tam-plugin"],
                        "merged_repos": [],
                        "target_files": ["skills/analyzer.py", "web/js/views/settings.mjs"],
                    }
                ],
                locks={
                    "acme/ops-control-tower": {
                        "active": True,
                        "repo_slug": "acme/ops-control-tower",
                        "status": "DEVELOPING",
                        "max_slots": 1,
                        "conversation_id": "00001111-1111-4111-8111-000000000001",
                        "branch_name": "fix/20261127-090000-tower-00001111",
                    }
                },
            )
            picked = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
            self.assertIn("acme/ops-tam-plugin", picked["runnable_by_repo"])
            self.assertEqual(picked["runnable_by_repo"]["acme/ops-tam-plugin"]["conversation_id"], cid_reversed)

    def test_in_progress_item_advances_downstream_once_upstream_pr_gate_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            repo_tower = _build_spa_fixture_repo(Path(tmp) / "ops-control-tower")
            cid_multi = "479784ce-1111-4111-8111-479784ce0001"
            cid_tower_s1 = "5b7a5b05-2222-4222-8222-5b7a5b050002"

            # Upstream plugin lock NOT ready yet (DEVELOPING, pr_gate_ready=False) -> downstream MUST wait
            _write_queue_and_locks(
                state_dir,
                items=[
                    {
                        "conversation_id": cid_multi,
                        "title": "Cross-repo sync feature",
                        "priority": 9,
                        "status": "in_progress",
                        "repo_slugs": ["acme/ops-tam-plugin", "acme/ops-control-tower"],
                        "merged_repos": [],
                        "proposed_fix": "Render #features-score-slot in web/index.html and features.mjs.",
                        "target_files": ["web/index.html", "web/js/views/features.mjs"],
                    }
                ],
                locks={
                    "acme/ops-tam-plugin": {
                        "active": True,
                        "repo_slug": "acme/ops-tam-plugin",
                        "status": "DEVELOPING",
                        "pr_gate_ready": False,
                        "max_slots": 3,
                        "conversation_id": cid_multi,
                        "branch_name": "fix/20261127-090000-plugin-479784ce",
                    },
                    "acme/ops-control-tower": {
                        "active": True,
                        "repo_slug": "acme/ops-control-tower",
                        "status": "DEVELOPING",
                        "max_slots": 3,
                        "target_repo_dir": str(repo_tower),
                        "conversation_id": cid_tower_s1,
                        "branch_name": "fix/20261127-091000-tower-5b7a5b05",
                        "proposed_fix": "Render #briefing-weather-slot in web/index.html and briefing.mjs.",
                        "target_files": ["web/index.html", "web/js/views/briefing.mjs"],
                    },
                },
            )
            picked_dev = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
            self.assertNotIn("acme/ops-control-tower", picked_dev["runnable_by_repo"])

            # Upstream plugin lock PASS>90 frozen (`PENDING_HUMAN_PR_VALIDATION`, `pr_gate_ready=True`)
            # and item transitioned to `pr_open` -> downstream enters Slot #2 lookahead on ops-control-tower!
            q_data = json.loads((state_dir / "dev_queue.json").read_text(encoding="utf-8"))
            q_data["items"][0]["status"] = "pr_open"
            (state_dir / "dev_queue.json").write_text(json.dumps(q_data, indent=2), encoding="utf-8")
            st_frozen = json.loads((state_dir / "actions_state.json").read_text(encoding="utf-8"))
            st_frozen["actions_log_audit_state"]["dev_pr_locks"]["acme/ops-tam-plugin"]["status"] = "PENDING_HUMAN_PR_VALIDATION"
            st_frozen["actions_log_audit_state"]["dev_pr_locks"]["acme/ops-tam-plugin"]["pr_gate_ready"] = True
            dev_common.atomic_write_json(state_dir / "actions_state.json", st_frozen)

            picked_ready = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
            self.assertIn("acme/ops-control-tower", picked_ready["runnable_by_repo"])
            chosen = picked_ready["runnable_by_repo"]["acme/ops-control-tower"]
            self.assertEqual(chosen["conversation_id"], cid_multi)
            self.assertEqual(chosen["target_slot"], 2)

    def test_idle_repo_wip_overflow_prevents_starvation_when_no_single_repo_work_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "state"
            half_items = [
                {
                    "conversation_id": f"5050000{i}-1111-4111-8111-00000000000{i}",
                    "title": f"Half-merged {i}",
                    "priority": 20 + i,
                    "status": "pending",
                    "repo_slugs": ["acme/ops-plugin", "acme/ops-tower"],
                    "merged_repos": ["acme/ops-plugin"],
                    "target_files": [f"api/part_{i}.py"],
                }
                for i in (1, 2, 3)
            ]
            cid_multi = "88881111-1111-4111-8111-888888888881"
            half_items.append(
                {
                    "conversation_id": cid_multi,
                    "title": "Only available work for idle plugin repo",
                    "priority": 18,
                    "status": "pending",
                    "repo_slugs": ["acme/ops-plugin", "acme/ops-tower"],
                    "merged_repos": [],
                    "target_files": ["skills/feature.py"],
                }
            )
            _write_queue_and_locks(
                state_dir,
                items=half_items,
                locks={
                    "acme/ops-tower": {
                        "active": True,
                        "repo_slug": "acme/ops-tower",
                        "status": "DEVELOPING",
                        "max_slots": 1,
                        "conversation_id": "00001111-1111-4111-8111-000000000001",
                        "branch_name": "fix/20261127-090000-tower-00001111",
                    }
                },
            )
            with mock.patch.dict(os.environ, {"AGY_DEV_MAX_PARTIAL_WIP": "3", "AGY_DEV_IDLE_REPO_WIP_OVERFLOW": "1"}):
                picked = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
            self.assertIn("acme/ops-plugin", picked["runnable_by_repo"])
            self.assertEqual(picked["runnable_by_repo"]["acme/ops-plugin"]["conversation_id"], cid_multi)
            self.assertTrue(picked["runnable_by_repo"]["acme/ops-plugin"].get("wip_cap_idle_repo_fallback"))

            with mock.patch.dict(os.environ, {"AGY_DEV_MAX_PARTIAL_WIP": "3", "AGY_DEV_IDLE_REPO_WIP_OVERFLOW": "0"}):
                picked_strict = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
            self.assertNotIn("acme/ops-plugin", picked_strict["runnable_by_repo"])


class TestParallelFileLeaseAdditiveRegistryParity(unittest.TestCase):
    """Verify `pretool:parallel-file-lease` allows additive registries/barrels while blocking regular sibling files."""

    def test_parallel_file_lease_exempts_additive_registries_and_blocks_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            wt1 = root / "pr-worktree-acme-s1"
            wt2 = root / "pr-worktree-acme-s2"
            for wt in (wt1, wt2):
                (wt / "src").mkdir(parents=True, exist_ok=True)
                (wt / "web" / "js" / "core").mkdir(parents=True, exist_ok=True)
                (wt / "src" / "alpha.py").write_text("x = 1\n", encoding="utf-8")
                (wt / "web" / "js" / "core" / "index.mjs").write_text("export const a = 1;\n", encoding="utf-8")
                (wt / "pyproject.toml").write_text("[project]\nname='acme'\n", encoding="utf-8")

            _write_queue_and_locks(
                state_dir,
                items=[],
                locks={
                    "acme/service-app": {
                        "active": True,
                        "repo_slug": "acme/service-app",
                        "max_slots": 3,
                        "slots": {
                            "1": {
                                "slot_id": 1,
                                "active": True,
                                "conversation_id": "11111111-1111-4111-8111-111111111111",
                                "branch_name": "fix/20261127-090000-s1-11111111",
                                "worktree_path": str(wt1),
                                "target_files": ["src/alpha.py", "web/js/core/index.mjs", "pyproject.toml"],
                            },
                            "2": {
                                "slot_id": 2,
                                "active": True,
                                "conversation_id": "22222222-2222-4222-8222-222222222222",
                                "branch_name": "fix/20261127-090500-s2-22222222",
                                "worktree_path": str(wt2),
                                "target_files": ["src/beta.py", "web/js/core/index.mjs", "pyproject.toml"],
                            },
                        },
                    }
                },
            )
            evaluator = dev_common.DevLifecycleGuardrailEvaluator(state_dir=state_dir)

            # Slot #2 editing barrel `index.mjs` or `pyproject.toml` in its own worktree -> ALLOWED
            res_barrel = evaluator.evaluate_pre_tool_use(
                {
                    "toolName": "edit_file",
                    "conversationId": "22222222-2222-4222-8222-222222222222",
                    "arguments": {"Path": str(wt2 / "web" / "js" / "core" / "index.mjs")},
                }
            )
            self.assertEqual(res_barrel.get("decision"), "allow")

            res_pyproj = evaluator.evaluate_pre_tool_use(
                {
                    "toolName": "edit_file",
                    "conversationId": "22222222-2222-4222-8222-222222222222",
                    "arguments": {"Path": str(wt2 / "pyproject.toml")},
                }
            )
            self.assertEqual(res_pyproj.get("decision"), "allow")

            # Slot #2 editing Slot #1's regular non-monolith file `src/alpha.py` -> STILL DENIED
            res_alpha = evaluator.evaluate_pre_tool_use(
                {
                    "toolName": "edit_file",
                    "conversationId": "22222222-2222-4222-8222-222222222222",
                    "arguments": {"Path": str(wt2 / "src" / "alpha.py")},
                }
            )
            self.assertEqual(res_alpha.get("decision"), "deny")
            self.assertEqual(res_alpha.get("check_id"), "pretool:parallel-file-lease")


if __name__ == "__main__":
    unittest.main()
