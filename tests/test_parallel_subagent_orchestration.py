#!/usr/bin/env python3
"""Hermetic Zero-PII unit tests for Parallel Subagent Orchestration Phase 1 (P14).

Covers:
  1. `parallel_scope.py`: Python AST import-graph & span extraction, ES2022 JS import/symbol/DOM-id spans,
     HTML5/CSS3 section & token extraction, 4-tier overlap classification (`NONE`, `TRANSITIVE`,
     `INTERFACE_SHARED`, `DEEP_SHARED`), pragmatic monolith disjoint-section detection, and deterministic
     `compute_landing_order`.
  2. `rebase_gate.py`: `base_sha` drift detection, `git merge-tree --write-tree` conflict probe (plus classic
     fallback), cross-impact `files(B) ∪ files(merged_A)` union, `AUTO_PROMOTED_ZERO_LLM`,
     `NEEDS_AGENT_SEMANTIC_FIX`, and `NEEDS_AGENT_CONFLICT_RESOLUTION` (verifying clean `git rebase --abort`).
  3. `pr_remediator.py` & `selective_test_runner.py`: backward-compatible `max_slots=2` `dev_pr_locks`,
     strictly max 1 open GitHub PR per repo (`READY_AWAITING_BASE_MERGE`), `analyze-overlap` & `rebase-reverify`
     CLIs, `queue-next --unlocked` Slot #2 lookahead, Slot #1 merge -> Slot #2 promotion/wakeup, and selective
     test runner routing.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
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

# Strip inherited git hook GIT_* variables so temporary git repositories stay 100% isolated.
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

import dev_common  # noqa: E402
import git_client  # noqa: E402
import parallel_scope as pscope  # noqa: E402
import pr_remediator as prs  # noqa: E402
import rebase_gate as rgate  # noqa: E402
import selective_test_runner as srunner  # noqa: E402


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
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-m", "chore: initial commit")
    _git(clone, "push", "-u", "origin", "main")
    return bare, clone


def _push_upstream_files(
    bare: pathlib.Path,
    scratch: pathlib.Path,
    files: dict[str, str],
    message: str = "feat: upstream slot1 merge",
) -> str:
    """Clone `bare` into a temporary directory, commit `files`, push to `origin/main`, and return new `main` SHA."""
    idx = len(list(scratch.glob("upstream-*"))) + 1
    pusher = scratch / f"upstream-{idx}"
    subprocess.run(["git", "clone", f"file://{bare}", str(pusher)], check=True, capture_output=True, text=True)
    _git(pusher, "config", "user.name", "Acme Bot")
    _git(pusher, "config", "user.email", "acme-bot@example.com")
    for rel_path, content in files.items():
        fpath = pusher / rel_path
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")
        _git(pusher, "add", rel_path)
    _git(pusher, "commit", "-m", message)
    _git(pusher, "push", "origin", "main")
    return _git(pusher, "rev-parse", "HEAD").stdout.strip()


class TestParallelScopeAnalyzer(unittest.TestCase):
    """Test 0-LLM AST/JS/HTML/CSS structure extraction, 4-tier overlap classification, and `landing_order`."""

    def _build_polyglot_fixture_repo(self, root: pathlib.Path) -> pathlib.Path:
        repo = root / "polyglot-repo"
        (repo / "pkg").mkdir(parents=True, exist_ok=True)
        (repo / "web").mkdir(parents=True, exist_ok=True)

        (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
        (repo / "pkg" / "leaf_db.py").write_text(
            "def query_records(limit: int = 10) -> list[dict]:\n"
            "    return [{'id': i} for i in range(limit)]\n\n"
            "def purge_stale() -> int:\n"
            "    return 0\n",
            encoding="utf-8",
        )
        (repo / "pkg" / "mid_service.py").write_text(
            "from pkg.leaf_db import query_records\n\n"
            "def fetch_summary() -> dict:\n"
            "    rows = query_records(5)\n"
            "    return {'count': len(rows)}\n\n"
            "def format_badge(label: str) -> str:\n"
            "    return f'[{label}]'\n",
            encoding="utf-8",
        )
        (repo / "pkg" / "top_router.py").write_text(
            "from pkg.mid_service import fetch_summary\n\n"
            "def handle_request() -> dict:\n"
            "    return fetch_summary()\n",
            encoding="utf-8",
        )
        (repo / "pkg" / "independent_cli.py").write_text(
            "def run_health_ping() -> str:\n"
            "    return 'pong'\n",
            encoding="utf-8",
        )

        # Python monolith (`portfolio.py` basename + >250 lines with banners & intra-file call coupling)
        filler_a = "\n".join(f"    val_{i} = {i}" for i in range(60))
        filler_b = "\n".join(f"    item_{i} = {i}" for i in range(60))
        filler_c = "\n".join(f"    metric_{i} = {i}" for i in range(60))
        monolith_py = (
            "# ==============================================================================\n"
            "# Section Alpha: Billing Rollup\n"
            "# ==============================================================================\n\n"
            "def compute_billing_rollup(accounts: list[dict]) -> int:\n"
            f"{filler_a}\n"
            "    return len(accounts)\n\n"
            "# ==============================================================================\n"
            "# Section Beta: Adoption Telemetry\n"
            "# ==============================================================================\n\n"
            "def compute_adoption_score(tenants: list[str]) -> float:\n"
            f"{filler_b}\n"
            "    return float(len(tenants))\n\n"
            "# ==============================================================================\n"
            "# Section Gamma: Executive Digest (calls compute_billing_rollup)\n"
            "# ==============================================================================\n\n"
            "def build_executive_digest(accounts: list[dict]) -> dict:\n"
            f"{filler_c}\n"
            "    total = compute_billing_rollup(accounts)\n"
            "    return {'total': total}\n"
        )
        (repo / "pkg" / "portfolio.py").write_text(monolith_py, encoding="utf-8")

        # ES2022 JS modules + monolith `web/dashboard.js`
        (repo / "web" / "utils.js").write_text(
            "export function formatCurrency(amount) {\n"
            "  return `$${amount}`;\n"
            "}\n\n"
            "export const renderTimestamp = (iso) => {\n"
            "  return String(iso || '');\n"
            "};\n",
            encoding="utf-8",
        )
        js_filler_1 = "\n".join(f"  const a_{i} = {i};" for i in range(45))
        js_filler_2 = "\n".join(f"  const b_{i} = {i};" for i in range(45))
        (repo / "web" / "dashboard.js").write_text(
            "import { formatCurrency } from './utils.js';\n"
            "// Fake comment with import './ignored.js' and braces { }\n\n"
            "// === Overview Panel ===\n"
            "export function renderOverviewCard(data) {\n"
            "  const el = document.getElementById('overview-card');\n"
            "  const pat = /https?:\\/\\/example\\.com\\/{1,2}/g;\n"
            f"{js_filler_1}\n"
            "  return formatCurrency(data.total) + String(Boolean(el && pat));\n"
            "}\n\n"
            "// === Queue Drawer ===\n"
            "export class QueueDrawerController {\n"
            "  renderQueueList(items) {\n"
            "    const root = document.querySelector('#queue-drawer');\n"
            f"{js_filler_2}\n"
            "    return root ? items.length : 0;\n"
            "  }\n"
            "}\n",
            encoding="utf-8",
        )

        # HTML5 + CSS3 with tokens and landmarks
        (repo / "web" / "index.html").write_text(
            "<!doctype html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <link rel=\"stylesheet\" href=\"./styles.css\">\n"
            "</head>\n"
            "<body>\n"
            "  <!-- === Overview Landmark === -->\n"
            "  <section id=\"overview-card\" data-section=\"overview\">\n"
            "    <h1>Overview</h1>\n"
            "  </section>\n"
            "  <!-- === Queue Landmark === -->\n"
            "  <aside id=\"queue-drawer\" data-section=\"queue\">\n"
            "    <h2>Queue</h2>\n"
            "  </aside>\n"
            "  <script type=\"module\" src=\"./dashboard.js\"></script>\n"
            "</body>\n"
            "</html>\n",
            encoding="utf-8",
        )
        (repo / "web" / "styles.css").write_text(
            ":root {\n"
            "  --surface-card: #111827;\n"
            "  --drawer-accent: #2563eb;\n"
            "}\n\n"
            "/* === Overview Styles === */\n"
            "#overview-card {\n"
            "  background: var(--surface-card);\n"
            "}\n\n"
            "/* === Queue Styles === */\n"
            "#queue-drawer {\n"
            "  border-color: var(--drawer-accent);\n"
            "}\n",
            encoding="utf-8",
        )
        return repo

    def test_parse_target_anchor_string_variants(self) -> None:
        self.assertEqual(
            pscope.parse_target_anchor_string("web/dashboard.js#L120-L180"),
            ("web/dashboard.js", (120, 180), ""),
        )
        self.assertEqual(
            pscope.parse_target_anchor_string("pkg/portfolio.py:15-42"),
            ("pkg/portfolio.py", (15, 42), ""),
        )
        self.assertEqual(
            pscope.parse_target_anchor_string("pkg/portfolio.py::compute_billing_rollup"),
            ("pkg/portfolio.py", None, "compute_billing_rollup"),
        )
        self.assertEqual(
            pscope.parse_target_anchor_string("web/dashboard.js#renderOverviewCard"),
            ("web/dashboard.js", None, "renderOverviewCard"),
        )
        self.assertEqual(
            pscope.parse_target_anchor_string("./pkg/independent_cli.py"),
            ("pkg/independent_cli.py", None, ""),
        )

    def test_polyglot_structure_extractors_python_js_html_css(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._build_polyglot_fixture_repo(pathlib.Path(tmp))
            structures, forward_graph = pscope.build_repository_import_graph(repo)

            # Python AST structure
            mid_st = structures["pkg/mid_service.py"]
            self.assertEqual(mid_st.language, "python")
            self.assertIn("pkg/leaf_db.py", mid_st.resolved_local_imports)
            self.assertIn("query_records", mid_st.imported_symbols["pkg/leaf_db.py"])
            self.assertIn("fetch_summary", mid_st.exported_symbols)

            # ES2022 JS structure (ignores commented fake import & regex slashes)
            dash_st = structures["web/dashboard.js"]
            self.assertEqual(dash_st.language, "javascript")
            self.assertTrue(dash_st.is_monolith)
            self.assertEqual(dash_st.resolved_local_imports, ("web/utils.js",))
            self.assertIn("renderOverviewCard", dash_st.exported_symbols)
            self.assertIn("QueueDrawerController", dash_st.exported_symbols)
            self.assertIn("#overview-card", dash_st.dom_ids)
            self.assertIn("#queue-drawer", dash_st.dom_ids)
            sec_names = {s.name for s in dash_st.sections}
            self.assertIn("renderOverviewCard", sec_names)
            self.assertIn("QueueDrawerController.renderQueueList", sec_names)

            # HTML5 structure
            html_st = structures["web/index.html"]
            self.assertEqual(html_st.language, "html")
            self.assertIn("web/styles.css", html_st.resolved_local_imports)
            self.assertIn("web/dashboard.js", html_st.resolved_local_imports)
            self.assertIn("#overview-card", html_st.dom_ids)
            self.assertIn("#queue-drawer", html_st.dom_ids)

            # CSS3 structure
            css_st = structures["web/styles.css"]
            self.assertEqual(css_st.language, "css")
            self.assertIn("--surface-card", css_st.css_tokens)
            self.assertIn("--drawer-accent", css_st.css_tokens)
            self.assertIn("pkg/leaf_db.py", forward_graph["pkg/mid_service.py"])

    def test_four_tier_overlap_classification_and_monolith_disjointness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._build_polyglot_fixture_repo(pathlib.Path(tmp))

            # 1. NONE: completely independent files & import trees
            res_none = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/independent_cli.py"]},
                {"id": "22222222-bbbb", "target_files": ["web/utils.js#renderTimestamp"]},
                repo_dir=repo,
            )
            self.assertEqual(res_none.overlap_tier, pscope.OVERLAP_NONE)
            self.assertTrue(res_none.parallel_safe)
            self.assertFalse(res_none.requires_rebase_reverify)

            # 2a. TRANSITIVE: 2-hop indirect import (`top_router` -> `mid_service` -> `leaf_db`)
            res_trans_hops = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/top_router.py#handle_request"]},
                {"id": "22222222-bbbb", "target_files": ["pkg/leaf_db.py#purge_stale"]},
                repo_dir=repo,
            )
            self.assertEqual(res_trans_hops.overlap_tier, pscope.OVERLAP_TRANSITIVE)
            self.assertTrue(res_trans_hops.parallel_safe)
            self.assertTrue(res_trans_hops.requires_rebase_reverify)

            # 2b. TRANSITIVE: 1-hop import but touching a disjoint unimported symbol (`purge_stale`)
            res_trans_disjoint_sym = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/mid_service.py#fetch_summary"]},
                {"id": "22222222-bbbb", "target_files": ["pkg/leaf_db.py#purge_stale"]},
                repo_dir=repo,
            )
            self.assertEqual(res_trans_disjoint_sym.overlap_tier, pscope.OVERLAP_TRANSITIVE)
            self.assertTrue(res_trans_disjoint_sym.parallel_safe)

            # 2c. TRANSITIVE (Monolith Disjoint): two tasks editing non-overlapping functions in `pkg/portfolio.py` and `web/dashboard.js`
            res_mono_py = pscope.analyze_pair_overlap(
                {
                    "id": "11111111-aaaa",
                    "target_files": ["pkg/portfolio.py"],
                    "section_anchors": ["pkg/portfolio.py::compute_billing_rollup"],
                },
                {
                    "id": "22222222-bbbb",
                    "target_files": ["pkg/portfolio.py"],
                    "section_anchors": ["pkg/portfolio.py::compute_adoption_score"],
                },
                repo_dir=repo,
            )
            self.assertEqual(res_mono_py.overlap_tier, pscope.OVERLAP_TRANSITIVE)
            self.assertTrue(res_mono_py.parallel_safe)
            self.assertTrue(res_mono_py.monolith_disjoint)
            self.assertEqual(res_mono_py.disjoint_monolith_files, ("pkg/portfolio.py",))

            res_mono_js = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["web/dashboard.js#renderOverviewCard"]},
                {"id": "22222222-bbbb", "target_files": ["web/dashboard.js#QueueDrawerController.renderQueueList"]},
                repo_dir=repo,
            )
            self.assertEqual(res_mono_js.overlap_tier, pscope.OVERLAP_TRANSITIVE)
            self.assertTrue(res_mono_js.parallel_safe)
            self.assertTrue(res_mono_js.monolith_disjoint)

            # 3a. INTERFACE_SHARED: direct 1-hop imported symbol (`mid_service` imports `query_records` from `leaf_db`)
            res_iface_import = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/mid_service.py#fetch_summary"]},
                {"id": "22222222-bbbb", "target_files": ["pkg/leaf_db.py#query_records"]},
                repo_dir=repo,
            )
            self.assertEqual(res_iface_import.overlap_tier, pscope.OVERLAP_INTERFACE_SHARED)
            self.assertFalse(res_iface_import.parallel_safe)

            # 3b. INTERFACE_SHARED: intra-monolith call coupling (`build_executive_digest` calls `compute_billing_rollup`)
            res_iface_intra = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/portfolio.py::compute_billing_rollup"]},
                {"id": "22222222-bbbb", "target_files": ["pkg/portfolio.py::build_executive_digest"]},
                repo_dir=repo,
            )
            self.assertEqual(res_iface_intra.overlap_tier, pscope.OVERLAP_INTERFACE_SHARED)
            self.assertFalse(res_iface_intra.parallel_safe)

            # 3c. INTERFACE_SHARED: cross-file shared DOM `#overview-card` contract between HTML and JS
            res_iface_dom = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["web/index.html#overview-card"]},
                {"id": "22222222-bbbb", "target_files": ["web/dashboard.js#renderOverviewCard"]},
                repo_dir=repo,
            )
            self.assertEqual(res_iface_dom.overlap_tier, pscope.OVERLAP_INTERFACE_SHARED)
            self.assertFalse(res_iface_dom.parallel_safe)

            # 4. DEEP_SHARED: overlapping lines in monolith, whole-file collision, explicit depends_on, and unanchored task
            res_deep_lines = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/portfolio.py#L5-L30"]},
                {"id": "22222222-bbbb", "target_files": ["pkg/portfolio.py#L25-L50"]},
                repo_dir=repo,
            )
            self.assertEqual(res_deep_lines.overlap_tier, pscope.OVERLAP_DEEP_SHARED)
            self.assertFalse(res_deep_lines.parallel_safe)

            res_deep_dep = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/independent_cli.py"]},
                {
                    "id": "22222222-bbbb",
                    "depends_on": ["11111111"],
                    "target_files": ["web/utils.js#renderTimestamp"],
                },
                repo_dir=repo,
            )
            self.assertEqual(res_deep_dep.overlap_tier, pscope.OVERLAP_DEEP_SHARED)
            self.assertFalse(res_deep_dep.parallel_safe)

            res_unanchored = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/independent_cli.py"]},
                {"id": "22222222-bbbb", "target_files": []},
                repo_dir=repo,
            )
            self.assertEqual(res_unanchored.overlap_tier, pscope.OVERLAP_DEEP_SHARED)
            self.assertFalse(res_unanchored.parallel_safe)

    def test_deterministic_landing_order_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._build_polyglot_fixture_repo(pathlib.Path(tmp))
            tasks = [
                # Depends on `task-small-p20` despite having priority 1 -> must wait for `task-small-p20`
                {
                    "conversation_id": "cid-dependent-p01",
                    "priority": 1,
                    "status": "pending",
                    "depends_on": ["cid-small-p20"],
                    "target_files": ["pkg/independent_cli.py#run_health_ping"],
                },
                # Already open in PR (`PENDING_HUMAN_PR_VALIDATION`) -> lands first among dependency-free tasks
                {
                    "conversation_id": "cid-pr-open-p50",
                    "priority": 50,
                    "status": "PENDING_HUMAN_PR_VALIDATION",
                    "pr_gate_ready": True,
                    "target_files": ["pkg/top_router.py"],
                },
                # Same priority (20) & status (`pending`), larger blast radius (`leaf_db.py` has reverse dependents)
                {
                    "conversation_id": "cid-large-p20",
                    "priority": 20,
                    "status": "pending",
                    "target_files": ["pkg/leaf_db.py", "pkg/mid_service.py"],
                },
                # Same priority (20) & status (`pending`), tiny 2-line blast radius & zero reverse dependents -> beats `cid-large-p20`
                {
                    "conversation_id": "cid-small-p20",
                    "priority": 20,
                    "status": "pending",
                    "target_files": ["pkg/independent_cli.py#run_health_ping"],
                },
            ]
            order = pscope.compute_landing_order(tasks, repo_dir=repo)
            self.assertEqual(
                order,
                [
                    "cid-pr-open-p50",
                    "cid-small-p20",
                    "cid-dependent-p01",
                    "cid-large-p20",
                ],
            )

    def test_repo_prefixed_target_files_normalization_and_foreign_repo_filtering(self) -> None:
        self.assertEqual(
            pscope.split_repo_prefixed_target("agy-dev-plugin:tests/test_agy_dev_plugin.py"),
            ("agy-dev-plugin", "tests/test_agy_dev_plugin.py"),
        )
        self.assertEqual(
            pscope.split_repo_prefixed_target("acme-org/agy-dev-plugin:tests/test_agy_dev_plugin.py"),
            ("acme-org/agy-dev-plugin", "tests/test_agy_dev_plugin.py"),
        )
        self.assertEqual(
            pscope.split_repo_prefixed_target("pkg/portfolio.py:15-42"),
            ("", "pkg/portfolio.py:15-42"),
        )
        self.assertEqual(
            pscope.parse_target_anchor_string(
                "acme-org/agy-dev-plugin:pkg/portfolio.py:15-42",
                repo_slug="acme-org/agy-dev-plugin",
            ),
            ("pkg/portfolio.py", (15, 42), ""),
        )
        self.assertEqual(
            pscope.parse_target_anchor_string(
                "agy-dev-plugin:pkg/portfolio.py::compute_billing_rollup",
                repo_slug="acme-org/agy-dev-plugin",
            ),
            ("pkg/portfolio.py", None, "compute_billing_rollup"),
        )
        self.assertEqual(
            pscope.parse_target_anchor_string(
                "g-control-tower:pkg/portfolio.py::compute_billing_rollup",
                repo_slug="acme-org/agy-dev-plugin",
            ),
            ("", None, ""),
        )
        self.assertEqual(
            pscope.normalize_target_files_for_repo(
                [
                    "agy-dev-plugin:tests/test_agy_dev_plugin.py",
                    "acme-org/agy-dev-plugin:pkg/independent_cli.py",
                    "g-control-tower:api/routes/dev.py",
                    "other-org/agy-dev-plugin:tests/test_other.py",
                ],
                repo_slug="acme-org/agy-dev-plugin",
            ),
            ["tests/test_agy_dev_plugin.py", "pkg/independent_cli.py"],
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._build_polyglot_fixture_repo(pathlib.Path(tmp))
            slug = "acme-org/agy-dev-plugin"

            # Short prefix `agy-dev-plugin:path` vs unprefixed `path` MUST detect DEEP_SHARED collision
            ov_short = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/leaf_db.py"]},
                {"id": "22222222-bbbb", "target_files": ["agy-dev-plugin:pkg/leaf_db.py"]},
                repo_dir=repo,
                repo_slug=slug,
            )
            self.assertEqual(ov_short.overlap_tier, pscope.OVERLAP_DEEP_SHARED)
            self.assertFalse(ov_short.parallel_safe)
            self.assertEqual(ov_short.colliding_files, ("pkg/leaf_db.py",))

            # Full `owner/repo:path` vs unprefixed `path` MUST detect DEEP_SHARED collision
            ov_full = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/leaf_db.py"]},
                {"id": "22222222-bbbb", "target_files": ["acme-org/agy-dev-plugin:pkg/leaf_db.py"]},
                repo_dir=repo,
                repo_slug=slug,
            )
            self.assertEqual(ov_full.overlap_tier, pscope.OVERLAP_DEEP_SHARED)
            self.assertFalse(ov_full.parallel_safe)

            # Foreign-repo prefixes (`g-control-tower:...` / `other-org/...`) MUST be ignored for `slug`
            ov_foreign_ignored = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/leaf_db.py"]},
                {
                    "id": "22222222-bbbb",
                    "target_files": [
                        "g-control-tower:pkg/leaf_db.py",
                        "other-org/agy-dev-plugin:pkg/leaf_db.py",
                        "agy-dev-plugin:pkg/independent_cli.py",
                    ],
                },
                repo_dir=repo,
                repo_slug=slug,
            )
            self.assertEqual(ov_foreign_ignored.overlap_tier, pscope.OVERLAP_NONE)
            self.assertTrue(ov_foreign_ignored.parallel_safe)

            # Candidate with ONLY foreign-repo paths has zero local touches -> conservative DEEP_SHARED
            ov_only_foreign = pscope.analyze_pair_overlap(
                {"id": "11111111-aaaa", "target_files": ["pkg/leaf_db.py"]},
                {"id": "22222222-bbbb", "target_files": ["g-control-tower:api/main.py"]},
                repo_dir=repo,
                repo_slug=slug,
            )
            self.assertEqual(ov_only_foreign.overlap_tier, pscope.OVERLAP_DEEP_SHARED)
            self.assertFalse(ov_only_foreign.parallel_safe)


class TestRebaseGate(unittest.TestCase):
    """Test native git drift detection, `merge-tree` conflict probing, cross-impact union, and 3 rebase outcomes."""

    def test_cross_impact_union_and_base_sha_drift(self) -> None:
        union = rgate.compute_cross_impact_files(
            ["./src/slot_b.py", "tests/test_b.py"],
            ["src/slot_a.py", "src/slot_b.py"],
        )
        self.assertEqual(union, ["src/slot_a.py", "src/slot_b.py", "tests/test_b.py"])

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bare, clone = _init_org_bare_and_clone(root)
            base_sha = _git(clone, "rev-parse", "main").stdout.strip()

            br_b = "fix/20261122-090000-slot2-bbbb2222"
            _git(clone, "checkout", "-b", br_b)
            (clone / "slot_b.py").write_text("VALUE_B = 2\n", encoding="utf-8")
            _git(clone, "add", "slot_b.py")
            _git(clone, "commit", "-m", "feat: add slot_b")

            drift_initial = rgate.detect_base_sha_drift(
                clone,
                branch_name=br_b,
                recorded_base_sha=base_sha,
                fetch=True,
            )
            self.assertEqual(drift_initial.status, "ok")
            self.assertFalse(drift_initial.drifted)
            self.assertEqual(drift_initial.ahead_count, 1)
            self.assertEqual(drift_initial.behind_count, 0)

            new_main_sha = _push_upstream_files(bare, root, {"slot_a.py": "VALUE_A = 1\n"})
            drift_after = rgate.detect_base_sha_drift(
                clone,
                branch_name=br_b,
                recorded_base_sha=base_sha,
                fetch=True,
            )
            self.assertTrue(drift_after.drifted)
            self.assertEqual(drift_after.current_base_sha, new_main_sha)
            self.assertEqual(drift_after.behind_count, 1)
            self.assertEqual(drift_after.branch_b_files, ("slot_b.py",))
            self.assertEqual(drift_after.merged_a_files, ("slot_a.py",))
            self.assertEqual(drift_after.union_files, ("slot_a.py", "slot_b.py"))

    def test_merge_tree_conflict_probe_and_classic_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bare, clone = _init_org_bare_and_clone(root)
            (clone / "shared.py").write_text("HEADER = 'v1'\n", encoding="utf-8")
            _git(clone, "add", "shared.py")
            _git(clone, "commit", "-m", "chore: seed shared.py")
            _git(clone, "push", "origin", "main")
            mb_sha = _git(clone, "rev-parse", "main").stdout.strip()

            _git(clone, "checkout", "-b", "fix/conflict-b")
            (clone / "shared.py").write_text("HEADER = 'from-branch-b'\n", encoding="utf-8")
            _git(clone, "add", "shared.py")
            _git(clone, "commit", "-m", "feat: branch b change")
            br_sha = _git(clone, "rev-parse", "HEAD").stdout.strip()

            new_main_sha = _push_upstream_files(bare, root, {"shared.py": "HEADER = 'from-main-a'\n"})
            _git(clone, "fetch", "origin")

            probe = rgate.probe_merge_tree_conflicts(clone, new_main_sha, br_sha, merge_base_sha=mb_sha)
            self.assertFalse(probe.clean)
            self.assertIn("shared.py", probe.conflicted_files)

            # Verify classic 3-way fallback if git lacks `--write-tree` (returncode 129)
            real_run_git = rgate._run_git

            def _mock_old_git(repo_dir: pathlib.Path, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                if len(args) >= 2 and args[0] == "merge-tree" and "--write-tree" in args:
                    return subprocess.CompletedProcess(args, 129, stdout="", stderr="error: unknown option `write-tree'")
                return real_run_git(repo_dir, args, **kwargs)  # type: ignore[arg-type]

            with mock.patch.object(rgate, "_run_git", side_effect=_mock_old_git):
                fallback_probe = rgate.probe_merge_tree_conflicts(clone, new_main_sha, br_sha, merge_base_sha=mb_sha)
            self.assertFalse(fallback_probe.supported_write_tree)
            self.assertFalse(fallback_probe.clean)
            self.assertIn("shared.py", fallback_probe.conflicted_files)

    def test_rebase_and_reverify_auto_promoted_semantic_fix_and_conflict_abort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            bare, clone = _init_org_bare_and_clone(root)

            (clone / "calc.py").write_text(
                "def multiply(x: int, factor: int = 2) -> int:\n"
                "    return x * factor\n",
                encoding="utf-8",
            )
            (clone / "tests").mkdir(parents=True, exist_ok=True)
            (clone / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (clone / "tests" / "test_calc.py").write_text(
                "import unittest\n"
                "import calc\n\n"
                "class TestCalc(unittest.TestCase):\n"
                "    def test_multiply(self) -> None:\n"
                "        self.assertEqual(calc.multiply(3), 6)\n",
                encoding="utf-8",
            )
            _git(clone, "add", "calc.py", "tests/__init__.py", "tests/test_calc.py")
            _git(clone, "commit", "-m", "feat: initial calc module")
            _git(clone, "push", "origin", "main")
            base_v1 = _git(clone, "rev-parse", "main").stdout.strip()

            # --- Case 1: AUTO_PROMOTED_ZERO_LLM ---
            br_clean = "fix/20261122-090100-clean-slot2-aaaa1111"
            _git(clone, "checkout", "-b", br_clean, "main")
            (clone / "feature_b.py").write_text(
                "import calc\n\n"
                "def triple_plus_default(val: int) -> int:\n"
                "    return calc.multiply(val) + val\n",
                encoding="utf-8",
            )
            (clone / "tests" / "test_feature_b.py").write_text(
                "import unittest\n"
                "import feature_b\n\n"
                "class TestFeatureB(unittest.TestCase):\n"
                "    def test_triple_plus_default(self) -> None:\n"
                "        self.assertEqual(feature_b.triple_plus_default(4), 12)\n",
                encoding="utf-8",
            )
            _git(clone, "add", "feature_b.py", "tests/test_feature_b.py")
            _git(clone, "commit", "-m", "feat: add feature_b")
            pre_sha_clean = _git(clone, "rev-parse", "HEAD").stdout.strip()

            base_v2 = _push_upstream_files(bare, root, {"slot1_notes.py": "SLOT1_MERGED = True\n"})
            res_auto = rgate.rebase_and_reverify(
                clone,
                branch_name=br_clean,
                recorded_base_sha=base_v1,
                fetch=True,
            )
            self.assertEqual(res_auto.status, "ok")
            self.assertEqual(res_auto.outcome, rgate.OUTCOME_AUTO_PROMOTED)
            self.assertTrue(res_auto.rebased)
            self.assertTrue(res_auto.verification_passed)
            self.assertNotEqual(res_auto.new_head_sha, pre_sha_clean)
            self.assertEqual(res_auto.new_base_sha, base_v2)
            self.assertIn("slot1_notes.py", res_auto.union_files)
            self.assertIn("feature_b.py", res_auto.union_files)

            # --- Case 2: NEEDS_AGENT_SEMANTIC_FIX (clean git rebase, cross-impact unit test fails) ---
            br_sem = "fix/20261122-090200-semantic-slot2-bbbb2222"
            _git(clone, "checkout", "-b", br_sem, base_v2)
            (clone / "consumer_b.py").write_text(
                "import calc\n\n"
                "def compute_six() -> int:\n"
                "    return calc.multiply(3)\n",
                encoding="utf-8",
            )
            (clone / "tests" / "test_consumer_b.py").write_text(
                "import unittest\n"
                "import consumer_b\n\n"
                "class TestConsumerB(unittest.TestCase):\n"
                "    def test_compute_six(self) -> None:\n"
                "        self.assertEqual(consumer_b.compute_six(), 6)\n",
                encoding="utf-8",
            )
            _git(clone, "add", "consumer_b.py", "tests/test_consumer_b.py")
            _git(clone, "commit", "-m", "feat: add consumer_b expecting factor=2")

            # Upstream Slot #1 changes default `factor: int = 10` (clean textual rebase for `consumer_b.py`, semantic break!)
            base_v3 = _push_upstream_files(
                bare,
                root,
                {
                    "calc.py": "def multiply(x: int, factor: int = 10) -> int:\n    return x * factor\n",
                    "tests/test_calc.py": (
                        "import unittest\nimport calc\n\n"
                        "class TestCalc(unittest.TestCase):\n"
                        "    def test_multiply(self) -> None:\n"
                        "        self.assertEqual(calc.multiply(3), 30)\n"
                    ),
                },
                message="feat(calc): change default factor to 10",
            )
            res_sem = rgate.rebase_and_reverify(
                clone,
                branch_name=br_sem,
                recorded_base_sha=base_v2,
                fetch=True,
            )
            self.assertEqual(res_sem.status, "ok")
            self.assertEqual(res_sem.outcome, rgate.OUTCOME_NEEDS_SEMANTIC_FIX)
            self.assertTrue(res_sem.rebased)
            self.assertFalse(res_sem.verification_passed)
            self.assertEqual(res_sem.new_base_sha, base_v3)
            self.assertIn("factor: int = 10", res_sem.merged_a_diff)
            self.assertIn("AssertionError", res_sem.failure_details)

            # --- Case 3: NEEDS_AGENT_CONFLICT_RESOLUTION (mid-rebase conflict -> clean `git rebase --abort`) ---
            br_conf = "fix/20261122-090300-conflict-slot2-cccc3333"
            _git(clone, "checkout", "-b", br_conf, base_v3)
            (clone / "calc.py").write_text(
                "def multiply(x: int, factor: int = 99) -> int:\n    return x * factor\n",
                encoding="utf-8",
            )
            _git(clone, "add", "calc.py")
            _git(clone, "commit", "-m", "feat: slot2 sets factor=99")
            pre_conf_sha = _git(clone, "rev-parse", "HEAD").stdout.strip()

            _push_upstream_files(
                bare,
                root,
                {"calc.py": "def multiply(x: int, factor: int = 42) -> int:\n    return x * factor\n"},
                message="feat: slot1 sets factor=42",
            )
            # Force actual `git rebase` path by mocking dry-run probe to clean so `git rebase --abort` is exercised too
            with mock.patch.object(
                rgate,
                "probe_merge_tree_conflicts",
                return_value=rgate.MergeTreeProbeResult(clean=True, supported_write_tree=True, tree_sha="deadbeef"),
            ):
                res_conf = rgate.rebase_and_reverify(
                    clone,
                    branch_name=br_conf,
                    recorded_base_sha=base_v3,
                    fetch=True,
                )
            self.assertEqual(res_conf.status, "ok")
            self.assertEqual(res_conf.outcome, rgate.OUTCOME_NEEDS_CONFLICT_RESOLUTION)
            self.assertFalse(res_conf.rebased)
            self.assertTrue(res_conf.aborted_cleanly)
            self.assertTrue(res_conf.worktree_pristine)
            self.assertFalse(rgate._is_rebase_in_progress(clone))
            self.assertEqual(_git(clone, "rev-parse", "HEAD").stdout.strip(), pre_conf_sha)
            self.assertIn("calc.py", res_conf.conflicted_files)
            self.assertIn("factor: int = 42", res_conf.merged_a_diff)


class TestTwoSlotLocksQueueLookaheadAndPromotion(unittest.TestCase):
    """Test `pr_remediator.py` 2-slot locks, max-1-PR invariant, `queue-next`, `analyze-overlap`, and `rebase-reverify`."""

    def _seed_two_module_repo(self, root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
        bare, clone = _init_org_bare_and_clone(root, repo_name="acme-service", org_name="acme-org")
        (clone / ".gitignore").write_text("__pycache__/\n*.py[cod]\n", encoding="utf-8")
        (clone / "plugin.json").write_text(
            json.dumps({"name": "acme-service", "version": "1.0.0"}, indent=2) + "\n",
            encoding="utf-8",
        )
        (clone / "src").mkdir(parents=True, exist_ok=True)
        (clone / "tests").mkdir(parents=True, exist_ok=True)
        (clone / "src" / "alpha.py").write_text(
            "def alpha_value() -> int:\n    return 10\n",
            encoding="utf-8",
        )
        (clone / "src" / "beta.py").write_text(
            "def beta_value() -> int:\n    return 20\n",
            encoding="utf-8",
        )
        monolith_lines = [
            "# === Panel One ===",
            "def render_panel_one() -> str:",
            *[f"    p1_{i} = {i}" for i in range(140)],
            "    return 'one'",
            "",
            "# === Panel Two ===",
            "def render_panel_two() -> str:",
            *[f"    p2_{i} = {i}" for i in range(140)],
            "    return 'two'",
        ]
        (clone / "src" / "monolith_view.py").write_text("\n".join(monolith_lines) + "\n", encoding="utf-8")
        (clone / "tests" / "test_alpha.py").write_text(
            "import sys, pathlib, unittest\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
            "import alpha\n\n"
            "class TestAlpha(unittest.TestCase):\n"
            "    def test_alpha(self) -> None:\n"
            "        self.assertEqual(alpha.alpha_value(), 10)\n",
            encoding="utf-8",
        )
        (clone / "tests" / "test_beta.py").write_text(
            "import sys, pathlib, unittest\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
            "import beta\n\n"
            "class TestBeta(unittest.TestCase):\n"
            "    def test_beta(self) -> None:\n"
            "        self.assertEqual(beta.beta_value(), 20)\n",
            encoding="utf-8",
        )
        _git(clone, "add", ".gitignore", "plugin.json", "src", "tests")
        _git(clone, "commit", "-m", "feat: seed alpha, beta, and monolith_view")
        _git(clone, "push", "origin", "main")
        return bare, clone

    def test_selective_test_runner_routes_parallel_modules(self) -> None:
        for changed in (
            ["skills/agy-code-remediation-pr/scripts/parallel_scope.py"],
            ["skills/agy-code-remediation-pr/scripts/rebase_gate.py"],
            ["skills/agy-code-remediation-pr/scripts/pr_remediator.py"],
            ["skills/agy-code-remediation-pr/scripts/lock_store.py"],
            ["skills/agy-code-remediation-pr/scripts/queue_store.py"],
            ["skills/agy-code-remediation-pr/scripts/worktree_ops.py"],
            ["skills/agy-code-remediation-pr/scripts/review_gate.py"],
            ["skills/agy-code-remediation-pr/scripts/merge_watcher.py"],
        ):
            selected = srunner.select_test_modules(changed)
            self.assertIn("tests.test_parallel_subagent_orchestration", selected)
            self.assertIn("tests.test_pr_remediator_queue_and_cleanup", selected)
            if not changed[0].endswith(("parallel_scope.py", "rebase_gate.py")):
                self.assertTrue(srunner.should_run_bats(changed))
        tui_selected = srunner.select_test_modules(["skills/agy-code-remediation-pr/scripts/pr_remediator.py"])
        self.assertIn("tests.test_dev_queue_tui", tui_selected)
        self.assertIn("tests.test_dev_queue_doctor", srunner.select_test_modules(["skills/agy-code-remediation-pr/scripts/lock_store.py"]))
        self.assertIn("tests.test_dev_queue_tui", srunner.select_test_modules(["skills/agy-code-remediation-pr/scripts/queue_store.py"]))
        self.assertIn("tests.test_pr_remediator_preview_staging", srunner.select_test_modules(["skills/agy-code-remediation-pr/scripts/worktree_ops.py"]))
        self.assertIn("tests.test_pr_remediator_preview_staging", srunner.select_test_modules(["skills/agy-code-remediation-pr/scripts/review_gate.py"]))
        self.assertIn("tests.test_dev_queue_doctor", srunner.select_test_modules(["skills/agy-code-remediation-pr/scripts/merge_watcher.py"]))

    def test_pr_remediator_modular_facade_size_reexports_and_cli_contract(self) -> None:
        """Verify `pr_remediator.py` stays <25 KB while preserving 100% of module re-exports and CLI subcommands."""
        facade_path = PR_REM_DIR / "pr_remediator.py"
        self.assertLess(facade_path.stat().st_size, 25_000)

        for mod_name in ("lock_store.py", "queue_store.py", "worktree_ops.py", "review_gate.py", "merge_watcher.py"):
            mod_path = PR_REM_DIR / mod_name
            self.assertTrue(mod_path.is_file(), msg=f"Missing extracted module {mod_name}")
            self.assertTrue(os.access(mod_path, os.X_OK), msg=f"Extracted module {mod_name} must be executable")

        expected_exports = (
            "get_all_dev_pr_locks",
            "get_dev_pr_lock",
            "acquire_dev_pr_lock",
            "update_dev_pr_lock",
            "record_dev_pr_lock_heartbeat",
            "release_dev_pr_lock",
            "sync_dev_pr_lock_with_git",
            "load_dev_queue",
            "enqueue_dev_queue_item",
            "update_dev_queue_item_status",
            "get_next_dev_queue_item",
            "reconcile_dev_queue_with_git",
            "compute_effective_priorities",
            "start_remediation_branch_and_lock",
            "prepare_isolated_worktree",
            "run_local_validation_suite",
            "push_remediation_branch_and_build_reviewer_plan",
            "get_branch_diff_with_verification_gate",
            "record_pr_review_evaluation",
            "evaluate_branch_and_release_lock",
            "analyze_queue_or_tasks_overlap",
            "run_slot_rebase_reverify",
            "run_pr_merge_watcher_loop",
            "spawn_pr_merge_watcher_daemon",
            "wait_for_unlock_or_actionable_event",
            "cleanup_merged_conversations",
            "parse_args",
            "main",
        )
        for sym in expected_exports:
            self.assertTrue(hasattr(prs, sym), msg=f"pr_remediator facade missing re-export '{sym}'")

        self.assertEqual(prs.parse_args(["lock-status", "--all"]).subcommand, "lock-status")
        self.assertEqual(prs.parse_args(["queue-next", "--unlocked"]).subcommand, "queue-next")
        self.assertEqual(prs.parse_args(["test-local", "--ci-parity"]).subcommand, "test-local")
        self.assertEqual(prs.parse_args(["prepare-review", "--title", "fix(test): ok"]).subcommand, "prepare-review")
        self.assertEqual(prs.parse_args(["wait-for-unlock", "--interval", "5"]).subcommand, "wait-for-unlock")
        self.assertEqual(prs.parse_args(["analyze-overlap", "--files-a", "a.py", "--files-b", "b.py"]).subcommand, "analyze-overlap")
        self.assertEqual(prs.parse_args(["rebase-reverify", "--fetch"]).subcommand, "rebase-reverify")

    def test_two_slot_lock_serialization_queue_lookahead_and_zero_llm_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)
            bare, clone = self._seed_two_module_repo(root)
            slug = "acme-org/acme-service"

            env_patch = {
                "HOME": str(fake_home),
                "AGY_DEV_STATE_DIR": str(state_dir),
                "AGY_CONF_REPO_DIR": str(clone),
                "AGY_REPOS_ROOT": str(clone.parent),
                # Legacy 2-slot contract (`AGY_DEV_MAX_SLOTS_PER_REPO=2`) stays fully backward-compatible.
                "AGY_DEV_MAX_SLOTS_PER_REPO": "2",
            }
            with (
                mock.patch.dict(os.environ, env_patch, clear=False),
                mock.patch.object(prs, "spawn_pr_merge_watcher_daemon", return_value={"status": "ok", "spawned": False}),
                mock.patch.object(prs, "stop_dev_api_server", return_value={"stopped": False}),
                mock.patch.object(prs, "restart_pro_api_server", return_value={"restarted": False}),
            ):
                vcs = git_client.GitVcsIntegrationClient(
                    env_overrides={"AGY_CONF_REPO_DIR": str(clone), "AGY_DEV_STATE_DIR": str(state_dir)}
                )

                # 1. Start Slot #1 on `src/alpha.py`
                cid_slot1 = "11111111-1111-4111-8111-111111111111"
                s1 = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_slot1,
                    action_id="alpha-upgrade",
                    rca_summary="Upgrade alpha_value",
                    proposed_fix="Update src/alpha.py",
                    target_files=["src/alpha.py"],
                    create_worktree=True,
                    branch_timestamp="20261122-100000",
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(s1["status"], "ok")
                self.assertEqual(s1["slot_id"], 1)
                wt1 = pathlib.Path(s1["worktree_path"])
                br1 = s1["branch_name"]

                # Commit + verify + stage + PASS review on Slot #1 -> publishes single open PR on `origin`
                (wt1 / "src" / "alpha.py").write_text("def alpha_value() -> int:\n    return 11\n", encoding="utf-8")
                (wt1 / "tests" / "test_alpha.py").write_text(
                    "import sys, pathlib, unittest\n"
                    "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
                    "import alpha\n\n"
                    "class TestAlpha(unittest.TestCase):\n"
                    "    def test_alpha(self) -> None:\n"
                    "        self.assertEqual(alpha.alpha_value(), 11)\n",
                    encoding="utf-8",
                )
                tl1 = prs.run_local_validation_suite(workdir=wt1, portfolio_dir=state_dir)
                self.assertTrue(tl1["passed"])
                prep1 = prs.push_remediation_branch_and_build_reviewer_plan(
                    title="feat(alpha): bump alpha_value to 11",
                    executive_summary="Bump alpha_value from 10 to 11.",
                    workdir=wt1,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(prep1["status"], "ok")
                rev1 = prs.record_pr_review_evaluation(
                    verdict="PASS",
                    score=95,
                    summary="Clean alpha change with unit test.",
                    branch_name=br1,
                    workdir=wt1,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertTrue(rev1["ready_for_human_pr"])
                self.assertTrue(rev1["branch_pushed"])
                self.assertTrue(bool(rev1["manual_pr_url"]))

                # 2. Enqueue one colliding candidate (`src/alpha.py`) and one disjoint candidate (`src/beta.py`)
                cid_collide = "99999999-9999-4999-8999-999999999999"
                cid_slot2 = "22222222-2222-4222-8222-222222222222"
                prs.enqueue_dev_queue_item(
                    conversation_id=cid_collide,
                    title="Conflicting alpha tweak",
                    priority=5,
                    repo_slugs=[slug],
                    target_files=["src/alpha.py"],
                    portfolio_dir=state_dir,
                )
                prs.enqueue_dev_queue_item(
                    conversation_id=cid_slot2,
                    title="Independent beta feature",
                    priority=10,
                    repo_slugs=[slug],
                    target_files=["src/beta.py"],
                    portfolio_dir=state_dir,
                )

                # `queue-next --unlocked` skips `cid_collide` (DEEP_SHARED with Slot #1) and selects `cid_slot2` for Slot #2!
                qnext = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
                self.assertIsNotNone(qnext["next_unlocked_item"])
                self.assertEqual(qnext["next_unlocked_item"]["conversation_id"], cid_slot2)
                self.assertEqual(qnext["next_unlocked_item"]["target_slot"], 2)
                self.assertEqual(qnext["next_unlocked_item"]["overlap_tier"], pscope.OVERLAP_NONE)
                self.assertEqual(len(qnext["slot2_candidates"]), 1)
                self.assertIn(slug, qnext["locked_repos"])
                self.assertEqual(qnext["blocked_by_repo_locks"][0]["conversation_id"], cid_collide)

                # Test `analyze-overlap` CLI in both direct mode and queue+lock mode
                buf_ov = io.StringIO()
                with contextlib.redirect_stdout(buf_ov):
                    rc_ov = prs.main(
                        [
                            "analyze-overlap",
                            "--workdir",
                            str(clone),
                            "--files-a",
                            "src/monolith_view.py",
                            "--anchors-a",
                            "src/monolith_view.py::render_panel_one",
                            "--files-b",
                            "src/monolith_view.py",
                            "--anchors-b",
                            "src/monolith_view.py::render_panel_two",
                        ]
                    )
                self.assertEqual(rc_ov, 0)
                ov_payload = json.loads(buf_ov.getvalue())
                self.assertEqual(ov_payload["overlap_tier"], pscope.OVERLAP_TRANSITIVE)
                self.assertTrue(ov_payload["parallel_safe"])
                self.assertTrue(ov_payload["monolith_disjoint"])

                buf_ov_q = io.StringIO()
                with contextlib.redirect_stdout(buf_ov_q):
                    rc_ov_q = prs.main(["analyze-overlap", "--repo", slug, "--workdir", str(clone)])
                self.assertEqual(rc_ov_q, 0)
                ov_q_payload = json.loads(buf_ov_q.getvalue())
                self.assertIn(cid_slot2, ov_q_payload["parallel_slot2_candidates"])
                self.assertNotIn(cid_collide, ov_q_payload["parallel_slot2_candidates"])

                # Colliding candidate MUST be blocked even with `allow_slot2=True`
                blocked_res = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_collide,
                    action_id="alpha-collide",
                    target_files=["src/alpha.py"],
                    allow_slot2=True,
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(blocked_res["status"], "locked")
                self.assertFalse(blocked_res["acquired"])

                # 3. Acquire Slot #2 for `cid_slot2` (`src/beta.py`)
                s2 = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_slot2,
                    action_id="beta-feature",
                    rca_summary="Add beta helper",
                    proposed_fix="Extend src/beta.py",
                    target_files=["src/beta.py"],
                    allow_slot2=True,
                    create_worktree=True,
                    branch_timestamp="20261122-100500",
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(s2["status"], "ok")
                self.assertEqual(s2["slot_id"], 2)
                wt2 = pathlib.Path(s2["worktree_path"])
                br2 = s2["branch_name"]

                # Top-level lock backward-compatibly reflects Slot #1 while `slots['2']` holds Slot #2
                top_lock = prs.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug=slug)
                self.assertEqual(top_lock["conversation_id"], cid_slot1)
                self.assertEqual(top_lock["branch_name"], br1)
                self.assertEqual(top_lock["max_slots"], 2)
                self.assertIn("1", top_lock["slots"])
                self.assertIn("2", top_lock["slots"])
                self.assertEqual(top_lock["slots"]["2"]["conversation_id"], cid_slot2)

                # `dev_common` branch/worktree protections cover BOTH Slot #1 and Slot #2 without modifying `dev_common.py`
                prot_branches, prot_wts = dev_common.get_active_dev_pr_lock_protections(state_dir=state_dir)
                self.assertIn(br1, prot_branches)
                self.assertIn(br2, prot_branches)
                self.assertTrue(any(pathlib.Path(p) == wt2.resolve() for p in prot_wts))

                # A 3rd candidate is blocked while both slots are occupied
                s3_blocked = prs.start_remediation_branch_and_lock(
                    conversation_id="33333333-3333-4333-8333-333333333333",
                    action_id="monolith-panel",
                    target_files=["src/monolith_view.py::render_panel_one"],
                    allow_slot2=True,
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(s3_blocked["status"], "locked")

                # 4. Develop, test, stage, and PASS review (`96/100`) on Slot #2 -> MUST enter `READY_AWAITING_BASE_MERGE`
                #    WITHOUT pushing to `origin` or surfacing `manual_pr_url` (max 1 open GitHub PR per repo!)
                (wt2 / "src" / "beta.py").write_text(
                    "def beta_value() -> int:\n    return 20\n\ndef beta_bonus() -> int:\n    return 5\n",
                    encoding="utf-8",
                )
                tl2 = prs.run_local_validation_suite(workdir=wt2, portfolio_dir=state_dir)
                self.assertTrue(tl2["passed"])
                prep2 = prs.push_remediation_branch_and_build_reviewer_plan(
                    title="feat(beta): add beta_bonus helper",
                    executive_summary="Add beta_bonus helper to src/beta.py.",
                    head_branch=br2,
                    workdir=wt2,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(prep2["status"], "ok")
                rev2 = prs.record_pr_review_evaluation(
                    verdict="PASS",
                    score=96,
                    summary="Clean disjoint Slot #2 feature.",
                    branch_name=br2,
                    workdir=wt2,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(rev2["status"], "ok")
                self.assertEqual(rev2["slot_id"], 2)
                self.assertEqual(rev2["lock_status"], prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE)
                self.assertTrue(rev2["awaiting_base_merge"])
                self.assertFalse(rev2["ready_for_human_pr"])
                self.assertFalse(rev2["branch_pushed"])
                self.assertEqual(rev2["manual_pr_url"], "")
                # Confirm `br2` does NOT exist on `bare` remote yet
                ls_rem_before = subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-parse", "--verify", f"refs/heads/{br2}"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(ls_rem_before.returncode, 0)

                # 5. Simulate human merging Slot #1 PR on GitHub (`origin/main` advances with `br1` commit)
                merger = root / "github-merger"
                subprocess.run(["git", "clone", f"file://{bare}", str(merger)], check=True, capture_output=True, text=True)
                _git(merger, "config", "user.name", "Acme Bot")
                _git(merger, "config", "user.email", "acme-bot@example.com")
                _git(merger, "merge", "--no-ff", f"origin/{br1}", "-m", f"Merge pull request #1 from {br1}")
                _git(merger, "push", "origin", "main")
                _git(merger, "push", "origin", f":{br1}")

                # `wait-for-unlock` detects Slot #1 merge, promotes Slot #2 -> Slot #1 (`NEEDS_REBASE_REVERIFY`),
                # preserves `wt2`, and wakes orchestrator with `slot2_needs_rebase_reverify`!
                wait_res = prs.wait_for_unlock_or_actionable_event(
                    poll_interval_seconds=0.05,
                    max_iterations=1,
                    portfolio_dir=state_dir,
                )
                self.assertEqual(wait_res["event"], "slot2_needs_rebase_reverify")
                self.assertTrue(wt2.exists())

                promoted_lock = prs.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug=slug)
                self.assertTrue(promoted_lock["active"])
                self.assertEqual(promoted_lock["slot_id"], 1)
                self.assertEqual(promoted_lock["conversation_id"], cid_slot2)
                self.assertEqual(promoted_lock["branch_name"], br2)
                self.assertEqual(promoted_lock["status"], prs.SLOT_STATUS_NEEDS_REBASE_REVERIFY)
                self.assertNotIn("2", promoted_lock["slots"])

                # 6. Run `rebase-reverify` CLI -> `AUTO_PROMOTED_ZERO_LLM` rebases onto new `main`, runs union tests,
                #    pushes `origin/<br2>`, generates `manual_pr_url`, and transitions queue to `pr_open` with 0 LLM tokens!
                buf_rbr = io.StringIO()
                with contextlib.redirect_stdout(buf_rbr):
                    rc_rbr = prs.main(["rebase-reverify", "--repo", slug, "--workdir", str(wt2), "--fetch"])
                self.assertEqual(rc_rbr, 0)
                rbr_payload = json.loads(buf_rbr.getvalue())
                self.assertEqual(rbr_payload["outcome"], rgate.OUTCOME_AUTO_PROMOTED)
                self.assertTrue(rbr_payload["promoted_to_human_pr"])
                self.assertTrue(rbr_payload["ready_for_human_pr"])
                self.assertTrue(rbr_payload["branch_pushed"])
                self.assertIn(br2, rbr_payload["manual_pr_url"])

                final_lock = prs.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug=slug)
                self.assertEqual(final_lock["status"], "PENDING_HUMAN_PR_VALIDATION")
                self.assertTrue(final_lock["pr_gate_ready"])
                self.assertTrue(final_lock["branch_pushed"])

                q_after = prs.load_dev_queue(portfolio_dir=state_dir)
                q_by_id = {it["conversation_id"]: it for it in q_after["items"]}
                self.assertEqual(q_by_id[cid_slot1]["status"], "merged")
                self.assertEqual(q_by_id[cid_slot2]["status"], "pr_open")

    def test_slot_rebase_reverify_records_semantic_and_conflict_remediation_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)
            bare, clone = self._seed_two_module_repo(root)
            slug = "acme-org/acme-service"

            env_patch = {
                "HOME": str(fake_home),
                "AGY_DEV_STATE_DIR": str(state_dir),
                "AGY_CONF_REPO_DIR": str(clone),
                "AGY_REPOS_ROOT": str(clone.parent),
            }
            with mock.patch.dict(os.environ, env_patch, clear=False):
                vcs = git_client.GitVcsIntegrationClient(
                    env_overrides={"AGY_CONF_REPO_DIR": str(clone), "AGY_DEV_STATE_DIR": str(state_dir)}
                )
                cid = "44444444-4444-4444-8444-444444444444"
                s_res = prs.start_remediation_branch_and_lock(
                    conversation_id=cid,
                    action_id="beta-check",
                    target_files=["src/beta.py"],
                    create_worktree=True,
                    branch_timestamp="20261122-101000",
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                wt = pathlib.Path(s_res["worktree_path"])
                br = s_res["branch_name"]
                (wt / "src" / "beta.py").write_text("def beta_value() -> int:\n    return 25\n", encoding="utf-8")
                _git(wt, "add", "-A")
                _git(wt, "commit", "-m", "feat: set beta_value to 25")

                # Upstream pushes non-conflicting change, while `test_units.py` still asserts `beta_value() == 20` -> semantic failure!
                _push_upstream_files(bare, root, {"src/alpha.py": "def alpha_value() -> int:\n    return 10\n# note\n"})
                sem_res = prs.run_slot_rebase_reverify(
                    repo_slug=slug,
                    workdir=wt,
                    branch_name=br,
                    fetch=True,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(sem_res["outcome"], rgate.OUTCOME_NEEDS_SEMANTIC_FIX)
                self.assertEqual(sem_res["lock_status"], prs.SLOT_STATUS_NEEDS_AGENT_SEMANTIC_FIX)
                self.assertFalse(sem_res["promoted_to_human_pr"])
                self.assertIn("developer_remediation_plan", sem_res)

                # Now push a conflicting upstream change on `src/beta.py` to verify `NEEDS_AGENT_CONFLICT_RESOLUTION`
                _push_upstream_files(bare, root, {"src/beta.py": "def beta_value() -> int:\n    return 999\n"})
                conf_res = prs.run_slot_rebase_reverify(
                    repo_slug=slug,
                    workdir=wt,
                    branch_name=br,
                    fetch=True,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(conf_res["outcome"], rgate.OUTCOME_NEEDS_CONFLICT_RESOLUTION)
                self.assertEqual(conf_res["lock_status"], prs.SLOT_STATUS_NEEDS_AGENT_CONFLICT_RESOLUTION)
                self.assertFalse(conf_res["promoted_to_human_pr"])
                self.assertIn("src/beta.py", conf_res["conflicted_files"])
                self.assertIn("developer_remediation_plan", conf_res)

    def test_slot2_prepare_and_record_review_without_explicit_branch_preserves_slot1_intact(self) -> None:
        """End-to-end regression test: `prepare-review` & `record-review` on Slot #2 (omitting `--branch`) never hijack or mutate Slot #1."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)
            bare, clone = self._seed_two_module_repo(root)
            slug = "acme-org/acme-service"

            env_patch = {
                "HOME": str(fake_home),
                "AGY_DEV_STATE_DIR": str(state_dir),
                "AGY_CONF_REPO_DIR": str(clone),
                "AGY_REPOS_ROOT": str(clone.parent),
            }
            with (
                mock.patch.dict(os.environ, env_patch, clear=False),
                mock.patch.object(prs, "spawn_pr_merge_watcher_daemon", return_value={"status": "ok", "spawned": False}),
                mock.patch.object(prs, "stop_dev_api_server", return_value={"stopped": False}),
                mock.patch.object(prs, "restart_pro_api_server", return_value={"restarted": False}),
            ):
                vcs = git_client.GitVcsIntegrationClient(
                    env_overrides={"AGY_CONF_REPO_DIR": str(clone), "AGY_DEV_STATE_DIR": str(state_dir)}
                )

                # --- Full Slot #1 Lifecycle (`start-remediation` -> commit -> `test-local` -> `prepare-review` -> `record-review` PASS) ---
                cid_slot1 = "aaaa1111-1111-4111-8111-111111111111"
                s1 = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_slot1,
                    action_id="alpha-slot1",
                    rca_summary="Slot 1 alpha update",
                    proposed_fix="Update src/alpha.py to return 42",
                    target_files=["src/alpha.py", "tests/test_alpha.py"],
                    create_worktree=True,
                    branch_timestamp="20261122-120000",
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(s1["status"], "ok")
                self.assertEqual(s1["slot_id"], 1)
                wt1 = pathlib.Path(s1["worktree_path"])
                br1 = s1["branch_name"]

                (wt1 / "src" / "alpha.py").write_text("def alpha_value() -> int:\n    return 42\n", encoding="utf-8")
                (wt1 / "tests" / "test_alpha.py").write_text(
                    "import sys, pathlib, unittest\n"
                    "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
                    "import alpha\n\n"
                    "class TestAlpha(unittest.TestCase):\n"
                    "    def test_alpha(self) -> None:\n"
                    "        self.assertEqual(alpha.alpha_value(), 42)\n",
                    encoding="utf-8",
                )
                _git(wt1, "add", "-A")
                _git(wt1, "commit", "-m", "fix(alpha): update alpha_value to 42")

                tl1 = prs.run_local_validation_suite(workdir=wt1, portfolio_dir=state_dir)
                self.assertTrue(tl1["passed"])

                prep1 = prs.push_remediation_branch_and_build_reviewer_plan(
                    title="fix(alpha): update alpha_value to 42",
                    executive_summary="Set alpha_value to 42 with unit test.",
                    workdir=wt1,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(prep1["status"], "ok")
                self.assertEqual(prep1["slot_id"], 1)
                self.assertEqual(prep1["branch_name"], br1)

                rev1 = prs.record_pr_review_evaluation(
                    verdict="PASS",
                    score=96,
                    summary="Slot 1 verified cleanly.",
                    workdir=wt1,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(rev1["status"], "ok")
                self.assertTrue(rev1["ready_for_human_pr"])
                self.assertTrue(rev1["branch_pushed"])
                self.assertTrue(bool(rev1["manual_pr_url"]))

                # Snapshot Slot #1 lock, worktree git state, and remote `origin/<br1>` ref BEFORE Slot #2 starts
                s1_lock_before = prs.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug=slug)
                s1_head_sha = s1_lock_before["head_sha"]
                s1_reviewed_sha = s1_lock_before["reviewed_head_sha"]
                s1_manual_url = s1_lock_before["manual_pr_url"]
                s1_wt_recorded = s1_lock_before["worktree_path"]
                s1_updated_at = s1_lock_before["updated_at"]
                s1_wt_git_sha = _git(wt1, "rev-parse", "HEAD").stdout.strip()
                s1_wt_subject = _git(wt1, "log", "-1", "--format=%s").stdout.strip()
                s1_remote_sha = subprocess.run(
                    ["git", "--git-dir", str(bare), "rev-parse", "--verify", f"refs/heads/{br1}"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
                self.assertEqual(s1_head_sha, s1_wt_git_sha)
                self.assertEqual(s1_head_sha, s1_remote_sha)
                self.assertEqual(s1_lock_before["status"], "PENDING_HUMAN_PR_VALIDATION")
                self.assertTrue(s1_lock_before["pr_gate_ready"])

                def _assert_slot1_completely_untouched() -> dict[str, object]:
                    raw_all = prs.get_all_dev_pr_locks(portfolio_dir=state_dir)
                    repo_lk = raw_all[slug]
                    self.assertEqual(repo_lk["conversation_id"], cid_slot1)
                    self.assertEqual(repo_lk["branch_name"], br1)
                    self.assertEqual(repo_lk["branch_timestamp"], "20261122-120000")
                    self.assertEqual(repo_lk["head_sha"], s1_head_sha)
                    self.assertEqual(repo_lk["reviewed_head_sha"], s1_reviewed_sha)
                    self.assertEqual(repo_lk["worktree_path"], s1_wt_recorded)
                    self.assertEqual(repo_lk["status"], "PENDING_HUMAN_PR_VALIDATION")
                    self.assertTrue(repo_lk["pr_gate_ready"])
                    self.assertTrue(repo_lk["branch_pushed"])
                    self.assertEqual(repo_lk["manual_pr_url"], s1_manual_url)
                    self.assertEqual(repo_lk["reviewer_verdict"], "PASS")
                    self.assertEqual(repo_lk["reviewer_score"], 96)
                    self.assertEqual(repo_lk["updated_at"], s1_updated_at)
                    # Slot #1 `repo_branches[slug]` entry untouched
                    rb_s1 = repo_lk["repo_branches"][slug]
                    self.assertEqual(rb_s1["branch_name"], br1)
                    self.assertEqual(rb_s1["head_sha"], s1_head_sha)
                    self.assertEqual(rb_s1["manual_pr_url"], s1_manual_url)
                    self.assertTrue(rb_s1["branch_pushed"])
                    # Slot #1 git worktree & remote ref untouched
                    self.assertEqual(_git(wt1, "rev-parse", "HEAD").stdout.strip(), s1_wt_git_sha)
                    self.assertEqual(_git(wt1, "log", "-1", "--format=%s").stdout.strip(), s1_wt_subject)
                    cur_rem_sha = subprocess.run(
                        ["git", "--git-dir", str(bare), "rev-parse", "--verify", f"refs/heads/{br1}"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                    self.assertEqual(cur_rem_sha, s1_remote_sha)
                    return repo_lk

                # --- Concurrent Slot #2 Lifecycle (`start-remediation --allow-slot2` -> commit -> `test-local` -> `prepare-review` [NO `--branch`] -> `record-review`) ---
                cid_slot2 = "bbbb2222-2222-4222-8222-222222222222"
                s2 = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_slot2,
                    action_id="beta-slot2",
                    rca_summary="Slot 2 beta feature",
                    proposed_fix="Add beta_extra helper in src/beta.py",
                    target_files=["acme-service:src/beta.py", "acme-org/acme-service:tests/test_beta.py"],
                    allow_slot2=True,
                    create_worktree=True,
                    branch_timestamp="20261122-120500",
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(s2["status"], "ok")
                self.assertEqual(s2["slot_id"], 2)
                wt2 = pathlib.Path(s2["worktree_path"])
                br2 = s2["branch_name"]
                self.assertNotEqual(br1, br2)
                self.assertNotEqual(wt1.resolve(), wt2.resolve())
                _assert_slot1_completely_untouched()

                # Projected Slot #2 view must NOT bleed Slot #1's `proposal_title`, `manual_pr_url`, or `reviewer_verdict`
                s2_view_init = prs.get_dev_pr_lock(portfolio_dir=state_dir, workdir=wt2)
                self.assertEqual(s2_view_init["slot_id"], 2)
                self.assertEqual(s2_view_init["branch_name"], br2)
                self.assertEqual(s2_view_init.get("manual_pr_url", ""), "")
                self.assertFalse(s2_view_init.get("pr_gate_ready", False))
                self.assertNotIn("proposal_title", s2_view_init)
                self.assertNotIn("reviewer_verdict", s2_view_init)

                (wt2 / "src" / "beta.py").write_text(
                    "def beta_value() -> int:\n    return 20\n\ndef beta_extra() -> int:\n    return 7\n",
                    encoding="utf-8",
                )
                (wt2 / "tests" / "test_beta.py").write_text(
                    "import sys, pathlib, unittest\n"
                    "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / 'src'))\n"
                    "import beta\n\n"
                    "class TestBeta(unittest.TestCase):\n"
                    "    def test_beta(self) -> None:\n"
                    "        self.assertEqual(beta.beta_value(), 20)\n"
                    "        self.assertEqual(beta.beta_extra(), 7)\n",
                    encoding="utf-8",
                )
                _git(wt2, "add", "-A")
                _git(wt2, "commit", "-m", "fix(beta): add beta_extra helper")

                tl2 = prs.run_local_validation_suite(workdir=wt2, portfolio_dir=state_dir)
                self.assertTrue(tl2["passed"])
                _assert_slot1_completely_untouched()

                # CRITICAL: Invoke `prepare-review` via CLI WITHOUT `--branch` (passing ONLY `--workdir <wt2>`)
                buf_prep2 = io.StringIO()
                with contextlib.redirect_stdout(buf_prep2):
                    rc_prep2 = prs.main(
                        [
                            "prepare-review",
                            "--workdir",
                            str(wt2),
                            "--title",
                            "fix(beta): add beta_extra helper",
                            "--executive-summary",
                            "Add beta_extra helper and unit test.",
                        ]
                    )
                self.assertEqual(rc_prep2, 0)
                prep2 = json.loads(buf_prep2.getvalue())
                self.assertEqual(prep2["status"], "ok")
                self.assertEqual(prep2["slot_id"], 2)
                self.assertEqual(prep2["branch_name"], br2)
                self.assertEqual(prep2["branch_timestamp"], "20261122-120500")
                self.assertFalse(prep2["branch_pushed"])
                self.assertEqual(prep2["manual_pr_url"], "")
                _assert_slot1_completely_untouched()

                # Exercise non-PASS `record-review` on Slot #2 first to ensure Path A also isolates `repo_branches` & `slots['2']`
                rev2_warn = prs.record_pr_review_evaluation(
                    verdict="WARN",
                    score=85,
                    summary="Minor docstring nitpick on beta_extra.",
                    workdir=wt2,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(rev2_warn["status"], "ok")
                self.assertEqual(rev2_warn["slot_id"], 2)
                self.assertEqual(rev2_warn["branch_name"], br2)
                self.assertEqual(rev2_warn["branch_action"], "NEEDS_REVISION_ITERATING")
                _assert_slot1_completely_untouched()

                # Re-stage and PASS `record-review` (`97/100`) on Slot #2 -> `READY_AWAITING_BASE_MERGE`
                prep2_r2 = prs.push_remediation_branch_and_build_reviewer_plan(
                    title="fix(beta): add beta_extra helper",
                    executive_summary="Add beta_extra helper and unit test.",
                    workdir=wt2,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(prep2_r2["status"], "ok")
                self.assertEqual(prep2_r2["slot_id"], 2)
                self.assertEqual(prep2_r2["branch_name"], br2)

                rev2_pass = prs.record_pr_review_evaluation(
                    verdict="PASS",
                    score=97,
                    summary="Clean disjoint Slot #2 implementation.",
                    workdir=wt2,
                    portfolio_dir=state_dir,
                    git_client=vcs,
                )
                self.assertEqual(rev2_pass["status"], "ok")
                self.assertEqual(rev2_pass["slot_id"], 2)
                self.assertEqual(rev2_pass["branch_name"], br2)
                self.assertEqual(rev2_pass["lock_status"], prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE)
                self.assertTrue(rev2_pass["awaiting_base_merge"])
                self.assertFalse(rev2_pass["ready_for_human_pr"])
                self.assertFalse(rev2_pass["branch_pushed"])
                self.assertEqual(rev2_pass["manual_pr_url"], "")

                repo_lk_final = _assert_slot1_completely_untouched()
                s2_slot = repo_lk_final["slots"]["2"]  # type: ignore[index]
                self.assertEqual(s2_slot["slot_id"], 2)
                self.assertEqual(s2_slot["conversation_id"], cid_slot2)
                self.assertEqual(s2_slot["branch_name"], br2)
                self.assertEqual(s2_slot["branch_timestamp"], "20261122-120500")
                self.assertEqual(s2_slot["status"], prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE)
                self.assertTrue(s2_slot["review_approved"])
                self.assertEqual(s2_slot["head_sha"], rev2_pass["head_sha"])
                self.assertEqual(s2_slot["reviewed_head_sha"], rev2_pass["head_sha"])
                self.assertFalse(s2_slot["pr_gate_ready"])
                self.assertFalse(s2_slot["branch_pushed"])
                self.assertEqual(s2_slot["manual_pr_url"], "")

                rb_s2 = repo_lk_final["repo_branches"][f"{slug}#slot2"]  # type: ignore[index]
                self.assertEqual(rb_s2["slot_id"], 2)
                self.assertEqual(rb_s2["branch_name"], br2)
                self.assertEqual(rb_s2["head_sha"], rev2_pass["head_sha"])
                self.assertFalse(rb_s2["branch_pushed"])
                self.assertEqual(rb_s2["manual_pr_url"], "")

    def test_empty_slot1_target_files_git_diff_fallback_and_prefixed_queue_lookahead(self) -> None:
        """Test `git diff --name-only <base_sha>...HEAD` (+ dirty/untracked) fallback when Slot #1 `target_files` is empty."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)
            bare, clone = _init_org_bare_and_clone(root, repo_name="agy-dev-plugin", org_name="acme-org")
            slug = "acme-org/agy-dev-plugin"

            (clone / "src").mkdir(parents=True, exist_ok=True)
            (clone / "tests").mkdir(parents=True, exist_ok=True)
            (clone / "src" / "alpha.py").write_text("ALPHA = 1\n", encoding="utf-8")
            (clone / "src" / "beta.py").write_text("BETA = 2\n", encoding="utf-8")
            (clone / "tests" / "test_agy_dev_plugin.py").write_text(
                "import unittest\n\nclass TestPlugin(unittest.TestCase):\n    def test_ok(self) -> None:\n        self.assertTrue(True)\n",
                encoding="utf-8",
            )
            _git(clone, "add", "src", "tests")
            _git(clone, "commit", "-m", "feat: seed agy-dev-plugin layout")
            _git(clone, "push", "origin", "main")

            env_patch = {
                "HOME": str(fake_home),
                "AGY_DEV_STATE_DIR": str(state_dir),
                "AGY_CONF_REPO_DIR": str(clone),
                "AGY_REPOS_ROOT": str(clone.parent),
            }
            with mock.patch.dict(os.environ, env_patch, clear=False):
                vcs = git_client.GitVcsIntegrationClient(
                    env_overrides={"AGY_CONF_REPO_DIR": str(clone), "AGY_DEV_STATE_DIR": str(state_dir)}
                )

                # 1. Start Slot #1 with EMPTY `target_files=[]`
                cid_s1 = "10101010-1111-4111-8111-101010101010"
                s1 = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_s1,
                    action_id="unanchored-slot1",
                    rca_summary="Slot 1 started without explicit --target-files",
                    proposed_fix="Modify tests/test_agy_dev_plugin.py and src/alpha.py",
                    target_files=[],
                    create_worktree=True,
                    branch_timestamp="20261122-130000",
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(s1["status"], "ok")
                wt1 = pathlib.Path(s1["worktree_path"])
                self.assertEqual(prs.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug=slug)["target_files"], [])

                # 2. Uncommitted & untracked edits in `wt1` BEFORE committing MUST already be detected by fallback!
                (wt1 / "tests" / "test_agy_dev_plugin.py").write_text(
                    "import unittest\n\nclass TestPlugin(unittest.TestCase):\n    def test_ok(self) -> None:\n        self.assertEqual(1, 1)\n",
                    encoding="utf-8",
                )
                (wt1 / "src" / "untracked_helper.py").write_text("HELPER = True\n", encoding="utf-8")

                blocked_dirty = prs.start_remediation_branch_and_lock(
                    conversation_id="20202020-2222-4222-8222-202020202020",
                    action_id="collide-dirty",
                    target_files=["agy-dev-plugin:tests/test_agy_dev_plugin.py"],
                    allow_slot2=True,
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(blocked_dirty["status"], "locked")
                self.assertEqual(blocked_dirty["overlap_tier"], pscope.OVERLAP_DEEP_SHARED)

                blocked_untracked = prs.start_remediation_branch_and_lock(
                    conversation_id="21212121-2222-4222-8222-212121212121",
                    action_id="collide-untracked",
                    target_files=["acme-org/agy-dev-plugin:src/untracked_helper.py"],
                    allow_slot2=True,
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(blocked_untracked["status"], "locked")
                self.assertEqual(blocked_untracked["overlap_tier"], pscope.OVERLAP_DEEP_SHARED)

                # 3. Commit changes in `wt1` (`git diff --name-only <base_sha>...HEAD` fallback)
                (wt1 / "src" / "alpha.py").write_text("ALPHA = 99\n", encoding="utf-8")
                _git(wt1, "add", "tests/test_agy_dev_plugin.py", "src/alpha.py", "src/untracked_helper.py")
                _git(wt1, "commit", "-m", "feat: slot1 touches test_agy_dev_plugin and alpha")

                # Enqueue:
                #   (a) short repo-prefixed collision (`agy-dev-plugin:tests/test_agy_dev_plugin.py`)
                #   (b) full owner/repo-prefixed collision (`acme-org/agy-dev-plugin:src/alpha.py`)
                #   (c) multi-repo candidate with foreign collision (`g-control-tower:src/alpha.py`) + disjoint local (`acme-org/agy-dev-plugin:src/beta.py`)
                cid_short_col = "30303030-3333-4333-8333-303030303030"
                cid_full_col = "40404040-4444-4444-8444-404040404040"
                cid_disjoint = "50505050-5555-4555-8555-505050505050"
                prs.enqueue_dev_queue_item(
                    conversation_id=cid_short_col,
                    title="Short-prefixed test collision",
                    priority=5,
                    repo_slugs=[slug],
                    target_files=["agy-dev-plugin:tests/test_agy_dev_plugin.py"],
                    portfolio_dir=state_dir,
                )
                prs.enqueue_dev_queue_item(
                    conversation_id=cid_full_col,
                    title="Full-prefixed alpha collision",
                    priority=8,
                    repo_slugs=[slug],
                    target_files=["acme-org/agy-dev-plugin:src/alpha.py"],
                    portfolio_dir=state_dir,
                )
                prs.enqueue_dev_queue_item(
                    conversation_id=cid_disjoint,
                    title="Disjoint beta with foreign alpha prefix",
                    priority=15,
                    repo_slugs=[slug, "acme-org/g-control-tower"],
                    target_files=[
                        "g-control-tower:src/alpha.py",
                        "acme-org/agy-dev-plugin:src/beta.py",
                    ],
                    portfolio_dir=state_dir,
                )

                qnext = prs.get_next_dev_queue_item(portfolio_dir=state_dir, skip_locked_repos=True)
                self.assertIsNotNone(qnext["next_unlocked_item"])
                self.assertEqual(qnext["next_unlocked_item"]["conversation_id"], cid_disjoint)
                self.assertEqual(qnext["next_unlocked_item"]["target_slot"], 2)
                self.assertEqual(qnext["next_unlocked_item"]["overlap_tier"], pscope.OVERLAP_NONE)

                blocked_ids = {b["conversation_id"]: b for b in qnext["blocked_by_repo_locks"]}
                self.assertIn(cid_short_col, blocked_ids)
                self.assertEqual(blocked_ids[cid_short_col]["overlap_tier"], pscope.OVERLAP_DEEP_SHARED)
                self.assertIn(cid_full_col, blocked_ids)
                self.assertEqual(blocked_ids[cid_full_col]["overlap_tier"], pscope.OVERLAP_DEEP_SHARED)

                # Confirm `start-remediation --allow-slot2` blocks the prefixed collision and allows the disjoint candidate
                blk_short = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_short_col,
                    action_id="short-collide",
                    target_files=["agy-dev-plugin:tests/test_agy_dev_plugin.py"],
                    allow_slot2=True,
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(blk_short["status"], "locked")
                self.assertEqual(blk_short["overlap_tier"], pscope.OVERLAP_DEEP_SHARED)

                ok_s2 = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_disjoint,
                    action_id="disjoint-beta",
                    target_files=[
                        "g-control-tower:src/alpha.py",
                        "acme-org/agy-dev-plugin:src/beta.py",
                    ],
                    allow_slot2=True,
                    create_worktree=True,
                    branch_timestamp="20261122-130500",
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(ok_s2["status"], "ok")
                self.assertEqual(ok_s2["slot_id"], 2)
                self.assertEqual(ok_s2["overlap_tier"], pscope.OVERLAP_NONE)

    def test_slot2_shared_cid8_planner_suffixes_do_not_hijack_slot1_and_preserve_short_id(self) -> None:
        """Verify suffixed queue IDs sharing `cid[:8]` allocate Slot #2 cleanly with unique `lock_id` without hijacking Slot #1."""
        # Unit check on exact-or-true-short-prefix helper (never matches two distinct suffixed IDs)
        self.assertFalse(
            prs._is_same_conversation_id(
                "6d655e83-gct-daemon-port-lock",
                "6d655e83-gct-modularize-p1-core",
            )
        )
        self.assertFalse(prs._is_same_conversation_id("6d655e83-gct", "6d655e83-gct-2"))
        self.assertTrue(prs._is_same_conversation_id("6d655e83", "6d655e83-gct-daemon-port-lock"))
        self.assertTrue(prs._is_same_conversation_id("6d655e83-gct-daemon-port-lock", "6d655e83"))
        self.assertTrue(prs._is_same_conversation_id("6D655E83", "6d655e83-gct-daemon-port-lock"))
        self.assertFalse(pscope._matches_cid("6d655e83-gct", "6d655e83-gct-2"))
        self.assertTrue(pscope._matches_cid("6d655e83", "6d655e83-gct-daemon-port-lock"))

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)
            _, clone = _init_org_bare_and_clone(root, repo_name="g-control-tower", org_name="acme-org")
            slug = "acme-org/g-control-tower"
            (clone / "src").mkdir(parents=True, exist_ok=True)
            (clone / "src" / "alpha.py").write_text("ALPHA = 1\n", encoding="utf-8")
            (clone / "src" / "beta.py").write_text("BETA = 2\n", encoding="utf-8")
            _git(clone, "add", "src")
            _git(clone, "commit", "-m", "feat: seed alpha and beta")
            _git(clone, "push", "origin", "main")

            cid_slot1 = "6d655e83-gct-daemon-port-lock"
            cid_slot2 = "6d655e83-gct-modularize-p1-core"
            shared_ts = "20261123-055813"

            env_patch = {
                "HOME": str(fake_home),
                "AGY_DEV_STATE_DIR": str(state_dir),
                "AGY_CONF_REPO_DIR": str(clone),
                "AGY_REPOS_ROOT": str(clone.parent),
            }
            with mock.patch.dict(os.environ, env_patch, clear=False):
                vcs = git_client.GitVcsIntegrationClient(
                    env_overrides={"AGY_CONF_REPO_DIR": str(clone), "AGY_DEV_STATE_DIR": str(state_dir)}
                )
                s1 = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_slot1,
                    action_id="gct-daemon-port-lock",
                    rca_summary="Slot 1 daemon port lock",
                    proposed_fix="Edit src/alpha.py",
                    target_files=["src/alpha.py"],
                    create_worktree=True,
                    branch_timestamp=shared_ts,
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(s1["status"], "ok")
                self.assertEqual(s1["slot_id"], 1)
                s1_branch = s1["branch_name"]
                s1_lock_id = s1["lock"]["lock_id"]
                s1_wt = pathlib.Path(s1["worktree_path"])

                # Add a commit in Slot #1 so `_find_existing_repo_branch_for_conversation` sees an ahead `fix/*-6d655e83` branch
                (s1_wt / "src" / "alpha.py").write_text("ALPHA = 2\n", encoding="utf-8")
                _git(s1_wt, "add", "src/alpha.py")
                _git(s1_wt, "commit", "-m", "fix: slot 1 alpha change")

                # Acquiring Slot #2 with a distinct suffixed ID sharing the same 8-char prefix & timestamp
                # MUST allocate Slot #2 with a distinct branch and distinct `lock_id`, never hijacking Slot #1!
                s2 = prs.start_remediation_branch_and_lock(
                    conversation_id=cid_slot2,
                    action_id="gct-modularize-p1-core",
                    rca_summary="Slot 2 modularize core",
                    proposed_fix="Edit src/beta.py",
                    target_files=["src/beta.py"],
                    allow_slot2=True,
                    create_worktree=True,
                    branch_timestamp=shared_ts,
                    portfolio_dir=state_dir,
                    workdir=clone,
                    git_client=vcs,
                )
                self.assertEqual(s2["status"], "ok")
                self.assertEqual(s2["slot_id"], 2)
                self.assertNotEqual(s2["branch_name"], s1_branch)
                self.assertNotEqual(s2["worktree_path"], s1["worktree_path"])
                s2_lock_id = s2["lock"]["lock_id"]
                self.assertNotEqual(s2_lock_id, s1_lock_id)
                self.assertIn("-s2", s2_lock_id)

                # Slot #1 metadata MUST remain completely intact
                slot1_after = prs.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug=slug, slot_id=1)
                self.assertEqual(slot1_after["conversation_id"], cid_slot1)
                self.assertEqual(slot1_after["branch_name"], s1_branch)
                self.assertEqual(slot1_after["lock_id"], s1_lock_id)
                self.assertEqual(slot1_after["target_files"], ["src/alpha.py"])
                self.assertEqual(slot1_after["active_slots_count"], 2)

                slot2_after = prs.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug=slug, slot_id=2)
                self.assertEqual(slot2_after["conversation_id"], cid_slot2)
                self.assertEqual(slot2_after["branch_name"], s2["branch_name"])
                self.assertEqual(slot2_after["lock_id"], s2_lock_id)
                self.assertEqual(slot2_after["target_files"], ["src/beta.py"])

                # True 8-char hex short ID (`6d655e83`) MUST still match Slot #1 via `acquire_dev_pr_lock` without truncating CID
                reacq = prs.acquire_dev_pr_lock(
                    conversation_id="6d655e83",
                    action_id="gct-daemon-port-lock",
                    branch_timestamp=shared_ts,
                    target_files=["src/alpha.py"],
                    reuse_active_branch=True,
                    portfolio_dir=state_dir,
                    repo_slug=slug,
                    workdir=s1_wt,
                )
                self.assertEqual(reacq["status"], "ok")
                self.assertEqual(reacq["slot_id"], 1)
                self.assertEqual(reacq["lock"]["conversation_id"], cid_slot1)
                self.assertEqual(reacq["lock"]["lock_id"], s1_lock_id)

    def test_deploy_repo_to_home_unittest_real_home_guard_and_reexec_self_update(self) -> None:
        """Verify `deploy_repo_to_home()` blocks unisolated `unittest` real-`$HOME` writes and re-execs once (`AGY_DEPLOY_REEXEC=1`) when updating `clients/git_client.py`."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            _, src_repo = _init_org_bare_and_clone(root, repo_name="acme-hermetic-guard", org_name="acme-org")
            fake_home = root / "fake-home"
            fake_home.mkdir(parents=True, exist_ok=True)

            (src_repo / "plugin.json").write_text(
                json.dumps({"name": "acme-hermetic-guard", "version": "1.0.0"}, indent=2) + "\n",
                encoding="utf-8",
            )
            (src_repo / "clients").mkdir(parents=True, exist_ok=True)
            shutil.copy2(CLIENTS_DIR / "dev_common.py", src_repo / "clients" / "dev_common.py")
            shutil.copy2(CLIENTS_DIR / "git_client.py", src_repo / "clients" / "git_client.py")
            sidecar_dir = src_repo / "sidecars" / "dev-queue-watchdog"
            sidecar_dir.mkdir(parents=True, exist_ok=True)
            (sidecar_dir / "sidecar.json").write_text(
                '{"command": "python3", "args": ["$HOME/watchdog.py"]}\n',
                encoding="utf-8",
            )
            (sidecar_dir / "watchdog.py").write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
            _git(
                src_repo,
                "add",
                "plugin.json",
                "clients/dev_common.py",
                "clients/git_client.py",
                "sidecars/dev-queue-watchdog/sidecar.json",
                "sidecars/dev-queue-watchdog/watchdog.py",
            )
            _git(src_repo, "commit", "-m", "feat: add git_client and sidecars")

            vcs = git_client.GitVcsIntegrationClient(env_overrides={"AGY_CONF_REPO_DIR": str(src_repo)})
            real_os_home = git_client._resolve_real_os_home()

            # 1. Unisolated `unittest` targeting real `$HOME` (home_dir=None, HOME=real_os_home) MUST be blocked!
            with mock.patch.dict(os.environ, {"HOME": str(real_os_home)}, clear=False):
                os.environ.pop("AGY_ALLOW_TEST_REAL_HOME_DEPLOY", None)
                blocked_res = vcs.deploy_repo_to_home(repo_dir=src_repo, home_dir=None)
            self.assertEqual(blocked_res["status"], "skipped")
            self.assertFalse(blocked_res["deployed"])
            self.assertEqual(blocked_res["reason"], "blocked_unisolated_test_real_home")
            self.assertFalse((real_os_home / ".gemini" / "config" / "plugins" / "acme-hermetic-guard").exists())

            # 2. Isolated `HOME` (home_dir=None with patched HOME=fake_home) is allowed and stays inside `fake_home`
            with mock.patch.dict(os.environ, {"HOME": str(fake_home), "AGY_DEPLOY_REEXEC": "1"}, clear=False):
                iso_res = vcs.deploy_repo_to_home(repo_dir=src_repo, home_dir=None)
            self.assertEqual(iso_res["status"], "ok")
            self.assertTrue(iso_res["deployed"])
            self.assertFalse(iso_res.get("reexec_pass", False))
            self.assertTrue(
                (fake_home / ".gemini" / "config" / "plugins" / "acme-hermetic-guard" / "sidecars" / "dev-queue-watchdog" / "watchdog.py").is_file()
            )

            # 3. Simulate stale in-memory `deploy_repo_to_home()` missing `sidecars/` while on-disk `clients/git_client.py` supports it:
            #    updating `clients/git_client.py` triggers ONE fresh `AGY_DEPLOY_REEXEC=1` subprocess pass so `sidecars/` deploys
            #    atomically without stale `prune_orphans` wiping it!
            fake_home_reexec = root / "fake-home-reexec"
            fake_home_reexec.mkdir(parents=True, exist_ok=True)
            stale_sidecar_dir = (
                fake_home_reexec
                / ".gemini"
                / "config"
                / "plugins"
                / "acme-hermetic-guard"
                / "sidecars"
                / "dev-queue-watchdog"
            )
            stale_sidecar_dir.mkdir(parents=True, exist_ok=True)
            (stale_sidecar_dir / "sidecar.json").write_text("stale", encoding="utf-8")
            (stale_sidecar_dir / "watchdog.py").write_text("stale", encoding="utf-8")

            orig_map = git_client.GitVcsIntegrationClient._map_plugin_rel_to_home

            def _stale_parent_map(rel_str: str, plugin_name: str = "wiz-tam-plugin") -> str:
                if rel_str.startswith("sidecars/"):
                    return ""
                return orig_map(rel_str, plugin_name=plugin_name)

            with (
                mock.patch.dict(os.environ, {"HOME": str(fake_home_reexec)}, clear=False),
                mock.patch.object(
                    git_client.GitVcsIntegrationClient,
                    "_map_plugin_rel_to_home",
                    staticmethod(_stale_parent_map),
                ),
            ):
                os.environ.pop("AGY_DEPLOY_REEXEC", None)
                reexec_res = vcs.deploy_repo_to_home(repo_dir=src_repo, home_dir=fake_home_reexec)
            self.assertEqual(reexec_res["status"], "ok")
            self.assertTrue(reexec_res["deployed"])
            self.assertTrue(reexec_res.get("reexec_pass"))
            self.assertTrue((stale_sidecar_dir / "watchdog.py").is_file())
            self.assertIn(str(fake_home_reexec), (stale_sidecar_dir / "sidecar.json").read_text(encoding="utf-8"))
            self.assertNotIn(
                ".gemini/config/plugins/acme-hermetic-guard/sidecars/dev-queue-watchdog/watchdog.py",
                reexec_res.get("pruned_orphans", []),
            )


class TestNSlotLocksHydrationAndPromotion(unittest.TestCase):
    """Test generalized N-slot (`1..4`, default `3`) locks, pairwise overlap, promotion, and B1 queue hydration."""

    SLUG = "acme-org/acme-nslots"
    CID_A = "aaaaaaaa-0000-4000-8000-000000000001"
    CID_B = "bbbbbbbb-0000-4000-8000-000000000002"
    CID_C = "cccccccc-0000-4000-8000-000000000003"
    CID_D = "dddddddd-0000-4000-8000-000000000004"
    CID_E = "eeeeeeee-0000-4000-8000-000000000005"

    def _seed_repo(self, root: pathlib.Path) -> pathlib.Path:
        repo = root / "repos" / "acme-nslots"
        (repo / "src").mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
        _git(repo, "config", "user.name", "Acme Bot")
        _git(repo, "config", "user.email", "acme-bot@example.com")
        for mod in ("a", "b", "c", "d", "e"):
            (repo / "src" / f"{mod}.py").write_text(f"def {mod}_value() -> int:\n    return 1\n", encoding="utf-8")
        _git(repo, "add", "src")
        _git(repo, "commit", "-m", "feat: seed five disjoint modules")
        return repo

    def _acquire(self, state_dir: pathlib.Path, repo: pathlib.Path, cid: str, files: list[str], **kw: object) -> dict:
        return prs.acquire_dev_pr_lock(
            conversation_id=cid,
            action_id=f"task-{cid[:4]}",
            branch_timestamp=f"20261124-10000{cid[-1]}",
            summary=f"Task {cid[:8]}",
            proposed_fix="Disjoint module change",
            target_files=files,
            portfolio_dir=state_dir,
            repo_slug=self.SLUG,
            workdir=repo,
            allow_slot2=True,
            **kw,
        )

    def test_resolve_max_repo_slots_defaults_and_clamps(self) -> None:
        import lock_store

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGY_DEV_MAX_SLOTS_PER_REPO", None)
            self.assertEqual(lock_store.DEFAULT_MAX_REPO_SLOTS, 3)
            self.assertEqual(lock_store._resolve_max_repo_slots(), 3)
            self.assertEqual(dev_common.resolve_max_repo_slots(), 3)
            self.assertEqual(lock_store._resolve_max_repo_slots(1), 1)
            self.assertEqual(lock_store._resolve_max_repo_slots(10), 4)
            self.assertEqual(lock_store._resolve_max_repo_slots(0), 3)
        for env_val, expected in (("9", 4), ("2", 2), ("0", 3), ("junk", 3), ("4", 4)):
            with mock.patch.dict(os.environ, {"AGY_DEV_MAX_SLOTS_PER_REPO": env_val}, clear=False):
                self.assertEqual(lock_store._resolve_max_repo_slots(), expected, msg=env_val)
                self.assertEqual(dev_common.resolve_max_repo_slots(), expected, msg=env_val)
        self.assertEqual(lock_store._next_free_slot_id({"1": {}, "2": {"active": True}}, 3), 3)
        self.assertIsNone(lock_store._next_free_slot_id({"1": {}, "2": {}, "3": {}}, 3))
        self.assertEqual(lock_store._next_free_slot_id({"1": {}, "3": {}}, 3), 2)
        self.assertEqual(lock_store._secondary_slot_keys({"1": {}, "4": {}, "2": {}, "9": {}, "x": {}}), ["2", "4"])

    def test_three_slot_pairwise_overlap_capacity_promotion_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)
            repo = self._seed_repo(root)
            env_patch = {"HOME": str(fake_home), "AGY_DEV_STATE_DIR": str(state_dir)}
            with (
                mock.patch.dict(os.environ, env_patch, clear=False),
                mock.patch.object(prs, "stop_dev_api_server", return_value={"stopped": False}),
                mock.patch.object(prs, "restart_pro_api_server", return_value={"restarted": False}),
            ):
                os.environ.pop("AGY_DEV_MAX_SLOTS_PER_REPO", None)
                s1 = self._acquire(state_dir, repo, self.CID_A, ["src/a.py"])
                self.assertTrue(s1["acquired"])
                self.assertEqual(s1["slot_id"], 1)
                self.assertEqual(s1["max_slots"], 3)
                br1 = s1["lock"]["branch_name"]

                s2 = self._acquire(state_dir, repo, self.CID_B, ["src/b.py"])
                self.assertTrue(s2["acquired"], msg=s2)
                self.assertEqual(s2["slot_id"], 2)

                # Candidate disjoint from Slot #1 but colliding with Slot #2 MUST be rejected (pairwise vs ALL slots)
                collide = self._acquire(state_dir, repo, self.CID_C, ["src/b.py"])
                self.assertEqual(collide["status"], "locked")
                self.assertFalse(collide["acquired"])
                self.assertEqual(collide["overlap_analysis"]["blocking_slot_ids"], [2])
                self.assertEqual(len(collide["overlap_analysis"]["pairwise"]), 2)

                s3 = self._acquire(state_dir, repo, self.CID_C, ["src/c.py"])
                self.assertTrue(s3["acquired"], msg=s3)
                self.assertEqual(s3["slot_id"], 3)
                self.assertEqual(s3["active_slots_count"], 3)
                self.assertEqual(s3["landing_order"], [self.CID_A, self.CID_B, self.CID_C])
                br3 = s3["lock"]["branch_name"]

                repo_lock = prs.get_all_dev_pr_locks(portfolio_dir=state_dir)[self.SLUG]
                self.assertEqual(sorted(repo_lock["slots"]), ["1", "2", "3"])
                self.assertEqual(repo_lock["slot_occupancy"], "3/3")
                self.assertIn(f"{self.SLUG}#slot2", repo_lock["repo_branches"])
                self.assertIn(f"{self.SLUG}#slot3", repo_lock["repo_branches"])
                prot_branches, _ = dev_common.get_active_dev_pr_lock_protections(state_dir=state_dir)
                self.assertIn(br3, prot_branches)

                # Capacity exhausted at default max_slots=3
                full = self._acquire(state_dir, repo, self.CID_D, ["src/d.py"])
                self.assertEqual(full["status"], "locked")
                self.assertTrue(full["slots_full"])
                self.assertIn("All 3 parallel slot(s) are occupied", full["error"])

                # Slot #3 routing by branch + slot-scoped update
                view3 = prs.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug=self.SLUG, branch_name=br3)
                self.assertEqual(view3["slot_id"], 3)
                self.assertEqual(view3["conversation_id"], self.CID_C)
                self.assertEqual(view3["slot1_branch_name"], br1)
                upd = prs.update_dev_pr_lock(
                    {"status": prs.SLOT_STATUS_READY_AWAITING_BASE_MERGE},
                    portfolio_dir=state_dir,
                    repo_slug=self.SLUG,
                    branch_name=br3,
                )
                self.assertEqual(upd["slot_id"], 3)
                prs.update_dev_pr_lock(
                    {"landing_order": [self.CID_A, self.CID_C, self.CID_B]},
                    portfolio_dir=state_dir,
                    repo_slug=self.SLUG,
                    branch_name=br1,
                    slot_id=1,
                )

                # AGY_DEV_MAX_SLOTS_PER_REPO=4 unlocks a 4th disjoint slot (cap 4)
                with mock.patch.dict(os.environ, {"AGY_DEV_MAX_SLOTS_PER_REPO": "4"}, clear=False):
                    s4 = self._acquire(state_dir, repo, self.CID_D, ["src/d.py"])
                self.assertTrue(s4["acquired"], msg=s4)
                self.assertEqual(s4["slot_id"], 4)
                self.assertEqual(s4["max_slots"], 4)

                # Releasing Slot #1 promotes the earliest follower in landing_order (Slot #3), not the lowest id
                rel = prs.release_dev_pr_lock(
                    reason="MANUAL_RELEASE",
                    portfolio_dir=state_dir,
                    repo_slug=self.SLUG,
                    branch_name=br1,
                )
                promoted = rel["promoted_slots"][self.SLUG]
                self.assertEqual(promoted["conversation_id"], self.CID_C)
                self.assertEqual(promoted["promoted_from_slot"], 3)
                self.assertEqual(promoted["status"], prs.SLOT_STATUS_NEEDS_REBASE_REVERIFY)
                self.assertEqual(sorted(promoted["slots"]), ["1", "2", "4"])
                self.assertEqual(promoted["slots"]["2"]["conversation_id"], self.CID_B)
                self.assertFalse(promoted["branch_pushed"])

                # Explicit selector for an inactive follower slot never releases Slot #1
                noop = prs.release_dev_pr_lock(
                    reason="MANUAL_RELEASE", portfolio_dir=state_dir, repo_slug=self.SLUG, slot_id=3
                )
                self.assertEqual(noop["released_repos"], [])
                self.assertTrue(prs.get_dev_pr_lock(portfolio_dir=state_dir, repo_slug=self.SLUG)["active"])

                # Releasing follower Slot #2 keeps Slot #1 + Slot #4 intact
                br2 = promoted["slots"]["2"]["branch_name"]
                prs.release_dev_pr_lock(reason="MANUAL_RELEASE", portfolio_dir=state_dir, repo_slug=self.SLUG, branch_name=br2)
                after = prs.get_all_dev_pr_locks(portfolio_dir=state_dir)[self.SLUG]
                self.assertEqual(sorted(after["slots"]), ["1", "4"])
                self.assertEqual(after["conversation_id"], self.CID_C)

    def test_acquire_auto_hydrates_repo_scoped_anchors_from_queue_preserving_overrides(self) -> None:
        import lock_store

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            state_dir = root / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            fake_home = root / "home"
            fake_home.mkdir(parents=True, exist_ok=True)
            repo = self._seed_repo(root)
            with mock.patch.dict(os.environ, {"HOME": str(fake_home), "AGY_DEV_STATE_DIR": str(state_dir)}, clear=False):
                prs.enqueue_dev_queue_item(
                    conversation_id=self.CID_E,
                    title="Hydration candidate",
                    repo_slugs=[self.SLUG, "other-org/other-repo"],
                    target_files=[f"{self.SLUG}:src/e.py", "other-org/other-repo:src/foreign.py", "src/d.py"],
                    section_anchors=["src/e.py::e_value", "other-repo:src/foreign.py::x"],
                    portfolio_dir=state_dir,
                )
                h_targets, h_anchors = lock_store._hydrate_anchors_from_queue(
                    self.CID_E, repo_slug=self.SLUG, portfolio_dir=state_dir
                )
                self.assertEqual(h_targets, ["src/e.py", "src/d.py"])
                self.assertEqual(h_anchors, ["src/e.py::e_value"])

                res = prs.acquire_dev_pr_lock(
                    conversation_id=self.CID_E,
                    action_id="hydrate",
                    portfolio_dir=state_dir,
                    repo_slug=self.SLUG,
                    workdir=repo,
                )
                self.assertTrue(res["acquired"])
                self.assertEqual(res["hydrated_from_queue"], ["target_files", "section_anchors"])
                self.assertEqual(res["lock"]["target_files"], ["src/e.py", "src/d.py"])
                self.assertEqual(res["lock"]["section_anchors"], ["src/e.py::e_value"])

                # Explicit `target_files` override wins; only the omitted `section_anchors` are hydrated
                res2 = prs.acquire_dev_pr_lock(
                    conversation_id=self.CID_E,
                    action_id="hydrate",
                    target_files=["src/e.py"],
                    portfolio_dir=state_dir,
                    repo_slug=self.SLUG,
                    workdir=repo,
                    reuse_active_branch=True,
                )
                self.assertTrue(res2["acquired"])
                self.assertEqual(res2["hydrated_from_queue"], ["section_anchors"])
                self.assertEqual(res2["lock"]["target_files"], ["src/e.py"])

                # Unknown conversation -> nothing hydrated, never raises
                self.assertEqual(lock_store._hydrate_anchors_from_queue("ffffffff", repo_slug=self.SLUG, portfolio_dir=state_dir), ([], []))

    def test_analyze_candidate_against_slots_and_batch_multi_slot_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._seed_repo(pathlib.Path(tmp))
            slots = [
                {"conversation_id": self.CID_A, "slot_id": 1, "target_files": ["src/a.py"]},
                {"conversation_id": self.CID_B, "slot_id": 2, "target_files": ["src/b.py"]},
            ]
            ok = pscope.analyze_candidate_against_slots(
                slots, {"conversation_id": self.CID_C, "target_files": ["src/c.py"]}, repo_dir=repo, repo_slug=self.SLUG
            )
            self.assertTrue(ok["parallel_safe"])
            self.assertEqual(ok["overlap_tier"], pscope.OVERLAP_NONE)
            self.assertEqual(ok["landing_order"], [self.CID_A, self.CID_B, self.CID_C])
            self.assertEqual([p["slot_id"] for p in ok["pairwise"]], [1, 2])
            bad = pscope.analyze_candidate_against_slots(
                slots, {"conversation_id": self.CID_C, "target_files": ["src/b.py"]}, repo_dir=repo, repo_slug=self.SLUG
            )
            self.assertFalse(bad["parallel_safe"])
            self.assertEqual(bad["blocking_slot_ids"], [2])
            self.assertEqual(bad["overlap_tier"], pscope.OVERLAP_DEEP_SHARED)
            self.assertFalse(pscope.analyze_candidate_against_slots([], {"conversation_id": self.CID_C})["parallel_safe"])

            batch = pscope.analyze_candidates_overlap(
                [
                    *slots,
                    {"conversation_id": self.CID_C, "target_files": ["src/b.py"]},
                    {"conversation_id": self.CID_D, "target_files": ["src/d.py"]},
                ],
                repo_dir=repo,
                repo_slug=self.SLUG,
                primary_slot_id=self.CID_A,
                active_slot_ids=[self.CID_B],
            ).to_dict()
            self.assertIn(self.CID_C, batch["parallel_slot2_candidates"])
            self.assertNotIn(self.CID_C, batch["multi_slot_candidates"])
            self.assertIn(self.CID_D, batch["multi_slot_candidates"])
            self.assertNotIn(self.CID_B, batch["multi_slot_candidates"])


if __name__ == "__main__":
    unittest.main()
