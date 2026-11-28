#!/usr/bin/env python3
"""Selective offline test runner for `agy-dev-plugin`."""

from __future__ import annotations

import argparse
import ast
from collections import deque
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MAX_PARALLEL_WORKERS = 4
ENV_TEST_WORKERS = "AGY_TEST_WORKERS"
DEFAULT_UNITTEST_TIMEOUT_SECONDS = 900
ENV_TEST_WORKER_TIMEOUT = "AGY_TEST_WORKER_TIMEOUT"
UNITTEST_TIMEOUT_RETURNCODE = 124
_ENV_IN_PARALLEL_WORKER = "_AGY_SELECTIVE_RUNNER_IN_WORKER"
AST_GRAPH_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        "tmp",
    }
)
_AMBIGUOUS_BARE_STEMS: frozenset[str] = frozenset({"__init__", "__main__", "main", "server", "app", "cli", "utils"})
_RAN_TESTS_RE = re.compile(r"Ran\s+(\d+)\s+tests?\s+in\b")
ALL_TEST_MODULES = [
    "tests.test_agy_dev_plugin",
    "tests.test_deterministic_first_audit",
    "tests.test_dev_pr_lock_isolation",
    "tests.test_dev_queue_cli_ux",
    "tests.test_dev_queue_doctor",
    "tests.test_dev_queue_planning",
    "tests.test_dev_queue_sidecar_panel",
    "tests.test_dev_queue_tui",
    "tests.test_model_quota_fallback",
    "tests.test_native_conversation_cleanup",
    "tests.test_orchestration_pause_resume",
    "tests.test_orchestration_wip_and_wake_fixes",
    "tests.test_orchestrator_parallel_p2",
    "tests.test_parallel_file_leases_and_tui",
    "tests.test_parallel_slot_unlock",
    "tests.test_parallel_subagent_orchestration",
    "tests.test_pr_remediator_compact_cli",
    "tests.test_pr_remediator_preview_staging",
    "tests.test_pr_remediator_queue_and_cleanup",
    "tests.test_queue_compactor",
    "tests.test_record_review_flags",
    "tests.test_sdlc_determinism_batch",
    "tests.test_sidecar_ghost_purge",
    "tests.test_superpowers_distilled_standards",
]

DOCS_SMOKE_TARGETS = [
    "tests.test_agy_dev_plugin.TestProjectAuditAndPluginContracts.test_repo_copilot_instructions_and_ruleset_docs_contract",
    "tests.test_agy_dev_plugin.TestProjectAuditAndPluginContracts.test_phase0_co_design_grill_me_rule_and_planner_contract",
]

DOCS_ROOT_FILENAMES = {
    "readme.md",
    "changelog.md",
    "contributing.md",
    "security.md",
    "license",
    "license.md",
    "license.txt",
}
DOCS_ONLY_EXTENSIONS = (
    ".md",
    ".txt",
    ".rst",
    ".png",
    ".svg",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
)
BATS_TRIGGER_SUFFIXES = (
    ".sh",
    ".bats",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
    "hooks.json",
    "plugin.json",
    "dev_common.py",
    "git_client.py",
    "pr_remediator.py",
    "lock_store.py",
    "queue_store.py",
    "queue_compactor.py",
    "dev_queue_planner.py",
    "worktree_ops.py",
    "review_gate.py",
    "merge_watcher.py",
    "orchestration_control.py",
    "project_audit.py",
    "dev_queue_tui.py",
    "tui_models.py",
    "tui_state.py",
    "tui_progress.py",
    "tui_render.py",
    "tui_board.py",
    "tui_watch.py",
)
BATS_TRIGGER_PREFIXES = (
    "sidecars/",
    "shell/",
    "agents/",
)
ISOLATED_TEST_FILE_MODULES = {
    "tests/test_deterministic_first_audit.py": "tests.test_deterministic_first_audit",
    "tests/test_dev_pr_lock_isolation.py": "tests.test_dev_pr_lock_isolation",
    "tests/test_dev_queue_cli_ux.py": "tests.test_dev_queue_cli_ux",
    "tests/test_dev_queue_doctor.py": "tests.test_dev_queue_doctor",
    "tests/test_dev_queue_planning.py": "tests.test_dev_queue_planning",
    "tests/test_dev_queue_sidecar_panel.py": "tests.test_dev_queue_sidecar_panel",
    "tests/test_dev_queue_tui.py": "tests.test_dev_queue_tui",
    "tests/test_model_quota_fallback.py": "tests.test_model_quota_fallback",
    "tests/test_native_conversation_cleanup.py": "tests.test_native_conversation_cleanup",
    "tests/test_orchestration_pause_resume.py": "tests.test_orchestration_pause_resume",
    "tests/test_orchestration_wip_and_wake_fixes.py": "tests.test_orchestration_wip_and_wake_fixes",
    "tests/test_orchestrator_parallel_p2.py": "tests.test_orchestrator_parallel_p2",
    "tests/test_parallel_file_leases_and_tui.py": "tests.test_parallel_file_leases_and_tui",
    "tests/test_parallel_slot_unlock.py": "tests.test_parallel_slot_unlock",
    "tests/test_parallel_subagent_orchestration.py": "tests.test_parallel_subagent_orchestration",
    "tests/test_pr_remediator_compact_cli.py": "tests.test_pr_remediator_compact_cli",
    "tests/test_pr_remediator_preview_staging.py": "tests.test_pr_remediator_preview_staging",
    "tests/test_pr_remediator_queue_and_cleanup.py": "tests.test_pr_remediator_queue_and_cleanup",
    "tests/test_queue_compactor.py": "tests.test_queue_compactor",
    "tests/test_record_review_flags.py": "tests.test_record_review_flags",
    "tests/test_sdlc_determinism_batch.py": "tests.test_sdlc_determinism_batch",
    "tests/test_sidecar_ghost_purge.py": "tests.test_sidecar_ghost_purge",
    "tests/test_superpowers_distilled_standards.py": "tests.test_superpowers_distilled_standards",
}


def _normalize_rel_path(rel: str) -> str:
    """Normalize a relative repository path to forward-slash POSIX notation."""
    norm = str(rel or "").strip().replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    return norm


def is_docs_only_path(rel: str) -> bool:
    """Return True when `rel` is non-plugin/non-code documentation safe for `<2s` smoke routing."""
    norm = _normalize_rel_path(rel)
    if not norm:
        return False
    lower = norm.lower()
    if lower.startswith(("rules/", "skills/", "agents/", "clients/", "shell/", "sidecars/", "tests/", ".github/workflows/")):
        return False
    if "/" not in lower:
        return lower in DOCS_ROOT_FILENAMES or lower.endswith((".md", ".txt", ".rst"))
    if lower.startswith("docs/"):
        return lower.endswith(DOCS_ONLY_EXTENSIONS)
    if lower.startswith(".github/"):
        return lower.endswith(".md")
    return False


def detect_changed_files(repo_dir: Path) -> list[str]:
    """Return deduplicated changed files across working tree, index, untracked files, and `origin/main...HEAD`."""
    if not (repo_dir / ".git").exists():
        return []

    seen: set[str] = set()
    changed: list[str] = []

    def _collect_nul_paths(args: list[str]) -> None:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            return
        for raw in proc.stdout.split(b"\x00"):
            if not raw:
                continue
            norm = _normalize_rel_path(raw.decode("utf-8", errors="surrogateescape"))
            if not norm or "__pycache__/" in norm or norm.endswith((".pyc", ".pyo")):
                continue
            if norm not in seen:
                seen.add(norm)
                changed.append(norm)

    _collect_nul_paths(["diff", "--name-only", "-z", "HEAD"])
    _collect_nul_paths(["diff", "--cached", "--name-only", "-z"])
    _collect_nul_paths(["ls-files", "--others", "--exclude-standard", "-z"])

    for base_ref in ("origin/main", "main"):
        ref_check = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--verify", "--quiet", base_ref],
            capture_output=True,
            timeout=5,
            check=False,
        )
        if ref_check.returncode == 0:
            _collect_nul_paths(["diff", "--name-only", "-z", f"{base_ref}...HEAD"])
            break

    return changed


def _is_isolated_test_file_path(norm: str) -> bool:
    """Return True when `norm` is a dedicated isolated test module (not monolithic `tests/test_agy_dev_plugin.py`)."""
    if norm in ISOLATED_TEST_FILE_MODULES:
        return True
    parts = Path(norm).parts
    return (
        len(parts) == 2
        and parts[0] == "tests"
        and parts[1].startswith("test_")
        and parts[1].endswith(".py")
        and norm != "tests/test_agy_dev_plugin.py"
    )


def build_python_ast_import_graph(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Build a pure-stdlib Python AST import graph (`forward_imports`, `reverse_dependents`, `test_files`)."""
    root = Path(repo_root).expanduser().resolve()
    py_files: list[str] = []
    by_dotted: dict[str, str] = {}
    by_stem: dict[str, list[str]] = {}
    test_files: dict[str, str] = {}

    if not root.is_dir():
        return {"forward_imports": {}, "reverse_dependents": {}, "test_files": {}}

    for cur_dir, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in AST_GRAPH_SKIP_DIRS)
        cur_p = Path(cur_dir)
        for fname in sorted(filenames):
            if not fname.endswith(".py"):
                continue
            fpath = cur_p / fname
            try:
                rel = fpath.relative_to(root).as_posix()
            except ValueError:
                continue
            py_files.append(rel)
            no_ext = rel[:-3]
            dotted = no_ext[:-9] if no_ext.endswith("/__init__") else no_ext
            dotted_name = dotted.replace("/", ".")
            if dotted_name:
                by_dotted[dotted_name] = rel
            stem = Path(rel).stem
            if stem and stem not in _AMBIGUOUS_BARE_STEMS:
                by_stem.setdefault(stem, []).append(rel)
            parts = Path(rel).parts
            if len(parts) == 2 and parts[0] == "tests" and parts[1].startswith("test_"):
                test_files[rel] = f"tests.{stem}"

    forward_imports: dict[str, set[str]] = {rel: set() for rel in py_files}
    reverse_dependents: dict[str, set[str]] = {rel: set() for rel in py_files}

    def _resolve_import_name(importer_rel: str, raw_name: str, level: int = 0) -> list[str]:
        matches: list[str] = []
        clean = str(raw_name or "").strip()
        if level > 0:
            imp_parts = list(Path(importer_rel).parts[:-1])
            up = max(0, level - 1)
            base_parts = imp_parts[: max(0, len(imp_parts) - up)] if up else imp_parts
            rel_parts = [p for p in clean.split(".") if p] if clean else []
            cand_dotted = ".".join([*base_parts, *rel_parts])
            if cand_dotted in by_dotted:
                matches.append(by_dotted[cand_dotted])
            return matches

        if not clean:
            return matches
        # 1. Exact or prefix package-qualified match
        parts = [p for p in clean.split(".") if p]
        for end_idx in range(len(parts), 0, -1):
            cand = ".".join(parts[:end_idx])
            if cand in by_dotted:
                matches.append(by_dotted[cand])
                return matches
        # 2. Bare or tail module stem match (supports `sys.path.insert` flat imports in plugin scripts/tests)
        for cand_stem in (parts[0], parts[-1]):
            if cand_stem in by_stem and len(by_stem[cand_stem]) == 1:
                resolved = by_stem[cand_stem][0]
                if resolved not in matches:
                    matches.append(resolved)
        return matches

    for rel in py_files:
        fpath = root / rel
        try:
            src = fpath.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(src, filename=rel)
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for target in _resolve_import_name(rel, alias.name, level=0):
                        if target != rel:
                            forward_imports[rel].add(target)
                            reverse_dependents[target].add(rel)
            elif isinstance(node, ast.ImportFrom):
                base_mod = node.module or ""
                lvl = int(getattr(node, "level", 0) or 0)
                resolved_base = _resolve_import_name(rel, base_mod, level=lvl)
                for target in resolved_base:
                    if target != rel:
                        forward_imports[rel].add(target)
                        reverse_dependents[target].add(rel)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    sub_name = f"{base_mod}.{alias.name}" if base_mod else alias.name
                    for target in _resolve_import_name(rel, sub_name, level=lvl):
                        if target != rel:
                            forward_imports[rel].add(target)
                            reverse_dependents[target].add(rel)

    return {
        "forward_imports": {k: sorted(v) for k, v in forward_imports.items()},
        "reverse_dependents": {k: sorted(v) for k, v in reverse_dependents.items()},
        "test_files": test_files,
    }


def discover_dependent_test_modules(
    changed_files: Sequence[str],
    *,
    repo_root: Path = REPO_ROOT,
    max_depth: int = 3,
    graph: dict[str, Any] | None = None,
) -> list[str]:
    """Auto-discover `tests.test_*` modules importing (directly or transitively) any modified Python module."""
    norm_py = [
        _normalize_rel_path(p)
        for p in changed_files
        if _normalize_rel_path(p).endswith(".py")
    ]
    if not norm_py:
        return []

    eff_graph = graph if graph is not None else build_python_ast_import_graph(repo_root)
    reverse_deps: dict[str, list[str]] = dict(eff_graph.get("reverse_dependents") or {})
    test_files: dict[str, str] = dict(eff_graph.get("test_files") or {})

    discovered: set[str] = set()
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque()

    for rel in norm_py:
        if rel in test_files:
            discovered.add(test_files[rel])
        else:
            parts = Path(rel).parts
            if len(parts) == 2 and parts[0] == "tests" and parts[1].startswith("test_"):
                discovered.add(f"tests.{Path(rel).stem}")
        visited.add(rel)
        queue.append((rel, 0))

    depth_cap = max(1, int(max_depth))
    while queue:
        cur, depth = queue.popleft()
        if depth >= depth_cap:
            continue
        for dep in reverse_deps.get(cur, ()):
            if dep in test_files:
                discovered.add(test_files[dep])
            if dep not in visited:
                visited.add(dep)
                queue.append((dep, depth + 1))

    return sorted(discovered)


def select_test_modules(
    changed_files: list[str],
    *,
    run_all: bool = False,
    repo_root: Path = REPO_ROOT,
    use_ast_discovery: bool = True,
    ast_graph: dict[str, Any] | None = None,
) -> list[str]:
    """Select targeted `tests.test_*` modules (or fast docs smoke targets) via explicit rules + AST import-graph discovery."""
    if run_all:
        return list(ALL_TEST_MODULES)

    norm_files = [_normalize_rel_path(p) for p in changed_files if _normalize_rel_path(p)]
    if not norm_files:
        return []

    if all(is_docs_only_path(p) for p in norm_files):
        return list(DOCS_SMOKE_TARGETS)

    selected: list[str] = []
    has_docs = False
    for norm in norm_files:
        if is_docs_only_path(norm):
            has_docs = True
            continue

        if (
            norm.endswith(
                (
                    "dev_common.py",
                    "orchestration_control.py",
                    "test_orchestration_pause_resume.py",
                    "pr_remediator.py",
                    "queue_store.py",
                    "worktree_ops.py",
                    "merge_watcher.py",
                    "dev_queue_doctor.py",
                    "dev_queue_tui.py",
                    "tui_models.py",
                    "tui_state.py",
                    "tui_progress.py",
                    "tui_render.py",
                    "tui_board.py",
                    "tui_watch.py",
                    "agy_dev_queue.sh",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith(("sidecars/dev-queue-panel/", "sidecars/dev-queue-watchdog/"))
        ):
            if "tests.test_orchestration_pause_resume" not in selected:
                selected.append("tests.test_orchestration_pause_resume")

        if (
            norm.endswith(
                (
                    "dev_common.py",
                    "dev_queue_tui.py",
                    "tui_models.py",
                    "tui_state.py",
                    "tui_progress.py",
                    "tui_render.py",
                    "tui_board.py",
                    "tui_watch.py",
                    "dev_flow_optimizer.py",
                    "parallel_scope.py",
                    "rebase_gate.py",
                    "pr_remediator.py",
                    "lock_store.py",
                    "queue_store.py",
                    "worktree_ops.py",
                    "review_gate.py",
                    "merge_watcher.py",
                    "test_parallel_file_leases_and_tui.py",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith(("agents/", "skills/agy-code-remediation-pr/", "skills/agy-dev-queue-orchestrator/"))
        ):
            if "tests.test_parallel_file_leases_and_tui" not in selected:
                selected.append("tests.test_parallel_file_leases_and_tui")

        if norm.endswith(
            (
                "parallel_scope.py",
                "rebase_gate.py",
                "pr_remediator.py",
                "lock_store.py",
                "queue_store.py",
                "worktree_ops.py",
                "review_gate.py",
                "merge_watcher.py",
                "git_client.py",
                "dev_common.py",
                "test_parallel_slot_unlock.py",
                "test_parallel_subagent_orchestration.py",
                "selective_test_runner.py",
            )
        ):
            if "tests.test_parallel_subagent_orchestration" not in selected:
                selected.append("tests.test_parallel_subagent_orchestration")
            if "tests.test_parallel_slot_unlock" not in selected:
                selected.append("tests.test_parallel_slot_unlock")
        if (
            norm.endswith(
                (
                    "pr_remediator.py",
                    "lock_store.py",
                    "queue_store.py",
                    "test_pr_remediator_compact_cli.py",
                    "selective_test_runner.py",
                )
            )
            or norm in (
                "rules/co-design-and-discovery.md",
                "skills/agy-code-remediation-pr/SKILL.md",
                "skills/agy-dev-queue-orchestrator/SKILL.md",
            )
        ):
            if "tests.test_pr_remediator_compact_cli" not in selected:
                selected.append("tests.test_pr_remediator_compact_cli")

        if norm.endswith(
            (
                "pr_remediator.py",
                "lock_store.py",
                "worktree_ops.py",
                "review_gate.py",
                "dev_common.py",
                "test_pr_remediator_preview_staging.py",
                "selective_test_runner.py",
            )
        ):
            if "tests.test_pr_remediator_preview_staging" not in selected:
                selected.append("tests.test_pr_remediator_preview_staging")
        if (
            norm.endswith(
                (
                    "pr_remediator.py",
                    "lock_store.py",
                    "queue_store.py",
                    "worktree_ops.py",
                    "review_gate.py",
                    "merge_watcher.py",
                    "parallel_scope.py",
                    "rebase_gate.py",
                    "dev_flow_optimizer.py",
                    "dev_queue_doctor.py",
                    "test_pr_remediator_queue_and_cleanup.py",
                    "dev_common.py",
                    "git_client.py",
                )
            )
            or norm.startswith("agents/")
        ):
            if "tests.test_pr_remediator_queue_and_cleanup" not in selected:
                selected.append("tests.test_pr_remediator_queue_and_cleanup")
        if (
            norm.endswith(
                (
                    "dev_queue_doctor.py",
                    "test_dev_queue_doctor.py",
                    "pr_remediator.py",
                    "lock_store.py",
                    "queue_store.py",
                    "merge_watcher.py",
                    "dev_flow_optimizer.py",
                    "dev_queue_tui.py",
                    "tui_models.py",
                    "tui_state.py",
                    "tui_progress.py",
                    "tui_render.py",
                    "tui_board.py",
                    "tui_watch.py",
                    "agy_dev_queue.sh",
                    "dev_common.py",
                )
            )
            or norm.startswith(("skills/agy-dev-queue-orchestrator/", "shell/", "sidecars/"))
        ):
            if "tests.test_dev_queue_doctor" not in selected:
                selected.append("tests.test_dev_queue_doctor")
        if (
            norm.endswith(
                (
                    "dev_queue_planner.py",
                    "queue_store.py",
                    "pr_remediator.py",
                    "dev_queue_tui.py",
                    "tui_models.py",
                    "tui_state.py",
                    "tui_progress.py",
                    "tui_render.py",
                    "tui_board.py",
                    "tui_watch.py",
                    "agy_dev_queue.sh",
                    "test_dev_queue_planning.py",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith(("sidecars/dev-queue-panel/", "skills/agy-dev-queue-orchestrator/"))
            or norm in ("agents/agy-dev-planner.md",)
        ):
            if "tests.test_dev_queue_planning" not in selected:
                selected.append("tests.test_dev_queue_planning")

        if (
            norm.endswith(
                (
                    "dev_queue_tui.py",
                    "tui_models.py",
                    "tui_state.py",
                    "tui_progress.py",
                    "tui_render.py",
                    "tui_board.py",
                    "tui_watch.py",
                    "test_dev_queue_sidecar_panel.py",
                    "selective_test_runner.py",
                    "dev_common.py",
                )
            )
            or norm.startswith(("sidecars/dev-queue-panel/", "skills/agy-dev-queue-orchestrator/"))
        ):
            if "tests.test_dev_queue_sidecar_panel" not in selected:
                selected.append("tests.test_dev_queue_sidecar_panel")
        if (
            norm.endswith(
                (
                    "merge_watcher.py",
                    "pr_remediator.py",
                    "test_sidecar_ghost_purge.py",
                    "selective_test_runner.py",
                    "dev_common.py",
                )
            )
            or norm.startswith("sidecars/dev-queue-panel/")
        ):
            if "tests.test_sidecar_ghost_purge" not in selected:
                selected.append("tests.test_sidecar_ghost_purge")
        if (
            norm.endswith(
                (
                    "dev_queue_tui.py",
                    "tui_models.py",
                    "tui_state.py",
                    "tui_progress.py",
                    "tui_render.py",
                    "tui_board.py",
                    "tui_watch.py",
                    "dev_flow_optimizer.py",
                    "dev_queue_doctor.py",
                    "pr_remediator.py",
                    "lock_store.py",
                    "queue_store.py",
                    "test_dev_queue_tui.py",
                    "agy_dev_queue.sh",
                    "dev_common.py",
                    "git_client.py",
                )
            )
            or norm.startswith("skills/agy-dev-queue-orchestrator/")
            or norm.startswith("shell/")
        ):
            if "tests.test_dev_queue_tui" not in selected:
                selected.append("tests.test_dev_queue_tui")
        if (
            norm.endswith(
                (
                    "dev_queue_tui.py",
                    "tui_models.py",
                    "tui_state.py",
                    "tui_progress.py",
                    "tui_render.py",
                    "tui_board.py",
                    "tui_watch.py",
                    "dev_flow_optimizer.py",
                    "pr_remediator.py",
                    "lock_store.py",
                    "queue_store.py",
                    "test_dev_queue_cli_ux.py",
                    "agy_dev_queue.sh",
                    "dev_common.py",
                    "git_client.py",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith("skills/agy-dev-queue-orchestrator/")
            or norm.startswith("shell/")
        ):
            if "tests.test_dev_queue_cli_ux" not in selected:
                selected.append("tests.test_dev_queue_cli_ux")
        if (
            norm.endswith(
                (
                    "dev_queue_doctor.py",
                    "pr_remediator.py",
                    "lock_store.py",
                    "queue_store.py",
                    "merge_watcher.py",
                    "dev_flow_optimizer.py",
                    "dev_queue_tui.py",
                    "tui_models.py",
                    "tui_state.py",
                    "tui_progress.py",
                    "tui_render.py",
                    "tui_board.py",
                    "tui_watch.py",
                    "test_orchestration_wip_and_wake_fixes.py",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith(("agents/", "skills/agy-dev-queue-orchestrator/"))
        ):
            if "tests.test_orchestration_wip_and_wake_fixes" not in selected:
                selected.append("tests.test_orchestration_wip_and_wake_fixes")

        if (
            norm.endswith(
                (
                    "model_quota.py",
                    "dev_common.py",
                    "lock_store.py",
                    "review_gate.py",
                    "dev_queue_doctor.py",
                    "merge_watcher.py",
                    "dev_flow_optimizer.py",
                    "test_model_quota_fallback.py",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith(("agents/", "sidecars/dev-queue-watchdog/", "skills/agy-dev-queue-orchestrator/"))
        ):
            if "tests.test_model_quota_fallback" not in selected:
                selected.append("tests.test_model_quota_fallback")

        if (
            norm.endswith(
                (
                    "queue_compactor.py",
                    "dev_flow_optimizer.py",
                    "queue_store.py",
                    "pr_remediator.py",
                    "dev_queue_tui.py",
                    "tui_models.py",
                    "tui_render.py",
                    "tui_watch.py",
                    "test_queue_compactor.py",
                    "selective_test_runner.py",
                )
            )
            or norm in ("agents/agy-dev-orchestrator.md", "agents/agy-dev-planner.md", "skills/agy-dev-queue-orchestrator/SKILL.md")
        ):
            if "tests.test_queue_compactor" not in selected:
                selected.append("tests.test_queue_compactor")

        if (
            norm.endswith(
                (
                    "conversation_service_client.py",
                    "merge_watcher.py",
                    "pr_remediator.py",
                    "dev_common.py",
                    "queue_store.py",
                    "lock_store.py",
                    "test_native_conversation_cleanup.py",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith("sidecars/dev-queue-watchdog/")
        ):
            if "tests.test_native_conversation_cleanup" not in selected:
                selected.append("tests.test_native_conversation_cleanup")

        if (
            norm.endswith(
                (
                    "project_audit.py",
                    "qa_static_audit.py",
                    "test_deterministic_first_audit.py",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith(("rules/", "skills/agy-project-standards/"))
            or norm in ("agents/agy-dev-planner.md", "agents/agy-code-pr-reviewer.md", "README.md")
        ):
            if "tests.test_deterministic_first_audit" not in selected:
                selected.append("tests.test_deterministic_first_audit")

        if (
            norm.endswith(
                (
                    "pr_remediator.py",
                    "review_gate.py",
                    "dev_queue_doctor.py",
                    "queue_compactor.py",
                    "complexity_audit.py",
                    "test_sdlc_determinism_batch.py",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith(("sidecars/dev-queue-watchdog/", "skills/agy-code-design-refactor/"))
            or norm in ("agents/agy-dev-orchestrator.md",)
        ):
            if "tests.test_sdlc_determinism_batch" not in selected:
                selected.append("tests.test_sdlc_determinism_batch")

        if (
            norm.endswith(
                (
                    "pr_remediator.py",
                    "review_gate.py",
                    "lock_store.py",
                    "test_record_review_flags.py",
                    "selective_test_runner.py",
                )
            )
            or norm in ("agents/agy-code-pr-reviewer.md",)
        ):
            if "tests.test_record_review_flags" not in selected:
                selected.append("tests.test_record_review_flags")

        if (
            norm.endswith(
                (
                    "lock_store.py",
                    "review_gate.py",
                    "merge_watcher.py",
                    "pr_remediator.py",
                    "dev_common.py",
                    "tui_state.py",
                    "test_dev_pr_lock_isolation.py",
                    "selective_test_runner.py",
                )
            )
            or norm.startswith("sidecars/dev-queue-panel/")
        ):
            if "tests.test_dev_pr_lock_isolation" not in selected:
                selected.append("tests.test_dev_pr_lock_isolation")

        if (
            norm.endswith(
                (
                    "project_audit.py",
                    "qa_static_audit.py",
                    "test_superpowers_distilled_standards.py",
                    "selective_test_runner.py",
                )
            )
            or norm in (
                "rules/python-and-testing.md",
                "rules/co-design-and-discovery.md",
                "agents/agy-code-pr-reviewer.md",
                "agents/agy-code-developer.md",
                "agents/agy-dev-executor.md",
                "agents/agy-dev-orchestrator.md",
                "agents/agy-dev-planner.md",
                "skills/agy-code-design-refactor/SKILL.md",
                "skills/agy-code-remediation-pr/SKILL.md",
                "skills/agy-project-standards/SKILL.md",
            )
        ):
            if "tests.test_superpowers_distilled_standards" not in selected:
                selected.append("tests.test_superpowers_distilled_standards")

        if (
            norm.endswith(
                (
                    "rebase_gate.py",
                    "review_gate.py",
                    "worktree_ops.py",
                    "dev_flow_optimizer.py",
                    "dev_queue_doctor.py",
                    "pr_remediator.py",
                    "selective_test_runner.py",
                    "test_orchestrator_parallel_p2.py",
                )
            )
            or norm in (
                "agents/agy-dev-orchestrator.md",
                "agents/agy-code-pr-reviewer.md",
                "agents/agy-code-developer.md",
            )
        ):
            if "tests.test_orchestrator_parallel_p2" not in selected:
                selected.append("tests.test_orchestrator_parallel_p2")

        if not _is_isolated_test_file_path(norm) and "tests.test_agy_dev_plugin" not in selected:
            selected.append("tests.test_agy_dev_plugin")

    if use_ast_discovery:
        ast_modules = discover_dependent_test_modules(norm_files, repo_root=repo_root, graph=ast_graph)
        for mod_name in ast_modules:
            if mod_name not in selected:
                selected.append(mod_name)

    if has_docs and "tests.test_agy_dev_plugin" not in selected:
        for target in DOCS_SMOKE_TARGETS:
            if target not in selected:
                selected.append(target)

    return selected or list(ALL_TEST_MODULES)


def should_run_bats(changed_files: list[str], *, run_all: bool = False) -> bool:
    """Return True when BATS CLI/installer checks are relevant to modified files."""
    if run_all:
        return True
    norm_files = [_normalize_rel_path(p) for p in changed_files if _normalize_rel_path(p)]
    if not norm_files:
        return False
    return any(
        norm.endswith(BATS_TRIGGER_SUFFIXES) or norm.startswith(BATS_TRIGGER_PREFIXES)
        for norm in norm_files
    )


def audit_router_integrity(
    repo_root: Path = REPO_ROOT,
    *,
    registered_modules: Sequence[str] | None = None,
) -> list[str]:
    """Verify 100% of `tests/test_*.py` files are registered in `ALL_TEST_MODULES` with zero orphans or dead entries."""
    tests_dir = Path(repo_root) / "tests"
    registered = list(ALL_TEST_MODULES if registered_modules is None else registered_modules)
    registered_set = set(registered)
    issues: list[str] = []

    if len(registered_set) != len(registered):
        issues.append("ALL_TEST_MODULES contains duplicate test module entries.")

    if not tests_dir.is_dir():
        issues.append(f"{tests_dir}: missing tests/ directory.")
        return issues

    discovered_files = sorted(p for p in tests_dir.glob("test_*.py") if p.is_file())
    discovered_modules = {f"tests.{p.stem}" for p in discovered_files}

    for py_file in discovered_files:
        mod_name = f"tests.{py_file.stem}"
        if mod_name not in registered_set:
            issues.append(
                f"tests/{py_file.name}: orphaned test module '{mod_name}' is not registered in ALL_TEST_MODULES."
            )

    for mod_name in registered:
        if mod_name not in discovered_modules:
            rel_py = mod_name.replace(".", "/") + ".py"
            issues.append(f"{rel_py}: registered module '{mod_name}' in ALL_TEST_MODULES does not exist on disk.")

    return issues


def resolve_parallel_workers(workers: int | None = None, *, target_count: int = 1) -> int:
    """Resolve Santa-safe pure-stdlib ProcessPoolExecutor worker count (1=sequential, auto up to 4)."""
    if target_count <= 1:
        return 1
    if workers is not None and int(workers) >= 1:
        return min(target_count, int(workers))
    env_raw = str(os.environ.get(ENV_TEST_WORKERS) or "").strip()
    if env_raw:
        with contextlib.suppress(ValueError):
            env_val = int(env_raw)
            if env_val >= 1:
                return min(target_count, env_val)
    cpu_cnt = os.cpu_count() or 1
    return min(target_count, max(1, min(cpu_cnt, DEFAULT_MAX_PARALLEL_WORKERS)))


def resolve_unittest_timeout_seconds(target_count: int = 1) -> float:
    """Resolve the bounded unittest subprocess timeout (`AGY_TEST_WORKER_TIMEOUT` seconds per module)."""
    per_module = float(DEFAULT_UNITTEST_TIMEOUT_SECONDS)
    env_raw = str(os.environ.get(ENV_TEST_WORKER_TIMEOUT) or "").strip()
    if env_raw:
        with contextlib.suppress(ValueError):
            env_val = float(env_raw)
            if env_val > 0:
                per_module = env_val
    return per_module * max(1, int(target_count))


def _coerce_captured_text(raw: Any) -> str:
    """Normalize captured subprocess output (`str`, `bytes`, or `None` on timeout) to text."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _subprocess_run_patch_reason() -> str:
    """Describe why `subprocess.run` is not the pristine stdlib callable ("" when untouched).

    Pool workers are fresh processes that never see in-process patches, so a mocked or wrapped
    `subprocess.run` keeps execution sequential; the reason is surfaced as `fallback_reason` so a
    passthrough guard (e.g. a hermetic wrapper) never disables parallelism silently.
    """
    run_fn = subprocess.run
    if isinstance(run_fn, mock.NonCallableMock):
        return "subprocess_run_mocked"
    mod_name = str(getattr(run_fn, "__module__", "") or "")
    if mod_name != "subprocess":
        qual_name = str(getattr(run_fn, "__qualname__", "") or type(run_fn).__name__)
        return f"subprocess_run_wrapped:{mod_name or 'unknown'}.{qual_name}"
    return ""


def _run_unittest_target_worker(task: tuple[str, str, dict[str, str]]) -> dict[str, Any]:
    """Top-level picklable worker executing one unittest module in an isolated subprocess (macOS spawn safe)."""
    target, repo_dir_str, env_dict = task
    worker_env = {**env_dict, _ENV_IN_PARALLEL_WORKER: "1"}
    timeout_s = resolve_unittest_timeout_seconds()
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", target, "-v"],
            cwd=repo_dir_str,
            env=worker_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
        returncode = int(proc.returncode)
        out_text = proc.stdout or ""
        err_text = proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        returncode = UNITTEST_TIMEOUT_RETURNCODE
        out_text = _coerce_captured_text(exc.stdout)
        err_text = (
            f"{_coerce_captured_text(exc.stderr)}\n"
            f"[Selective Test Runner] TIMEOUT: `{target}` exceeded {timeout_s:.0f}s and was terminated.\n"
        )
    elapsed = round(time.perf_counter() - t0, 3)
    tests_run = sum(int(m) for m in _RAN_TESTS_RE.findall(f"{out_text}\n{err_text}"))
    return {
        "target": target,
        "returncode": returncode,
        "stdout": out_text,
        "stderr": err_text,
        "duration_seconds": elapsed,
        "tests_run": tests_run,
    }


def run_unittest_targets(
    targets: Sequence[str],
    *,
    repo_dir: Path = REPO_ROOT,
    env: dict[str, str] | None = None,
    workers: int | None = None,
    mode: str = "selective",
) -> dict[str, Any]:
    """Execute unittest targets using stdlib `ProcessPoolExecutor` when `workers > 1` with safe sequential fallback."""
    target_list = [str(t).strip() for t in targets if str(t).strip()]
    if not target_list:
        return {
            "returncode": 0,
            "mode": "skipped",
            "workers_used": 0,
            "fallback_reason": "",
            "results": [],
        }

    test_env = env if env is not None else _clean_git_env()
    # Keep docs-only dotted smoke targets, single-module runs, mocked subprocesses, and nested worker invocations sequential.
    distinct_modules = {t.split(".")[0] + "." + t.split(".")[1] if len(t.split(".")) >= 2 else t for t in target_list}
    in_nested_worker = str(os.environ.get(_ENV_IN_PARALLEL_WORKER) or "").strip() == "1"
    wanted_workers = (
        1
        if (mode == "docs_smoke" or len(distinct_modules) <= 1)
        else resolve_parallel_workers(workers, target_count=len(target_list))
    )
    # Record WHY a parallel-eligible run stays sequential so the downgrade is never silent.
    sequential_reason = ""
    if wanted_workers > 1:
        sequential_reason = "nested_parallel_worker" if in_nested_worker else _subprocess_run_patch_reason()
    eff_workers = 1 if sequential_reason else wanted_workers

    def _run_sequential(reason: str = "") -> dict[str, Any]:
        if reason:
            print(f"[Selective Test Runner] Parallel workers disabled ({reason}); running sequentially.", file=sys.stderr)
        timeout_s = resolve_unittest_timeout_seconds(len(target_list))
        t0 = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", *target_list, "-v"],
                cwd=str(repo_dir),
                env=test_env,
                check=False,
                timeout=timeout_s,
            )
            returncode = int(proc.returncode)
        except subprocess.TimeoutExpired:
            returncode = UNITTEST_TIMEOUT_RETURNCODE
            print(
                f"[Selective Test Runner] TIMEOUT: sequential unittest run exceeded {timeout_s:.0f}s and was terminated.",
                file=sys.stderr,
            )
        return {
            "returncode": returncode,
            "mode": "sequential",
            "workers_used": 1,
            "fallback_reason": reason,
            "duration_seconds": round(time.perf_counter() - t0, 3),
            "results": [],
        }

    if eff_workers <= 1:
        return _run_sequential(reason=sequential_reason)

    tasks = [(t, str(repo_dir), test_env) for t in target_list]
    t0 = time.perf_counter()
    try:
        with ProcessPoolExecutor(max_workers=eff_workers) as pool:
            results = list(pool.map(_run_unittest_target_worker, tasks))
    except (OSError, PermissionError, RuntimeError) as exc:
        return _run_sequential(reason=f"{exc.__class__.__name__}: {exc}")

    overall_rc = 0
    total_tests = 0
    for item in results:
        if item["stdout"]:
            sys.stdout.write(item["stdout"])
        if item["stderr"]:
            sys.stderr.write(item["stderr"])
        total_tests += int(item.get("tests_run") or 0)
        if int(item.get("returncode") or 0) != 0 and overall_rc == 0:
            overall_rc = int(item["returncode"])
    elapsed = round(time.perf_counter() - t0, 3)
    status_lbl = "PASSED" if overall_rc == 0 else "FAILED"
    print(
        f"[Selective Test Runner] {status_lbl} ({len(results)} modules, {total_tests} tests, "
        f"workers={eff_workers}, {elapsed:.2f}s)."
    )
    return {
        "returncode": overall_rc,
        "mode": "parallel",
        "workers_used": eff_workers,
        "fallback_reason": "",
        "duration_seconds": elapsed,
        "total_tests": total_tests,
        "results": results,
    }


def build_test_plan(
    repo_dir: Path = REPO_ROOT,
    *,
    changed_files: list[str] | None = None,
    run_all: bool = False,
    no_bats: bool = False,
    workers: int | None = None,
    use_ast_discovery: bool = True,
) -> dict[str, Any]:
    """Build machine-readable selective test plan dictionary (including AST discovery and worker count)."""
    changed = [] if run_all else (list(changed_files) if changed_files is not None else detect_changed_files(repo_dir))
    wants_ast = not (run_all or not use_ast_discovery or not changed or all(is_docs_only_path(p) for p in changed))
    # Parse the repository import graph at most once and share it between both consumers below.
    ast_graph = (
        build_python_ast_import_graph(repo_dir)
        if wants_ast and any(_normalize_rel_path(p).endswith(".py") for p in changed)
        else None
    )
    ast_discovered = discover_dependent_test_modules(changed, repo_root=repo_dir, graph=ast_graph) if wants_ast else []
    modules = select_test_modules(
        changed,
        run_all=run_all,
        repo_root=repo_dir,
        use_ast_discovery=use_ast_discovery,
        ast_graph=ast_graph,
    )
    bats_needed = should_run_bats(changed, run_all=run_all)
    run_bats = False if no_bats else bats_needed

    if run_all:
        mode = "all"
    elif not modules:
        mode = "skip_clean"
    elif modules == DOCS_SMOKE_TARGETS:
        mode = "docs_smoke"
    else:
        mode = "selective"

    eff_workers = (
        0
        if not modules
        else (1 if mode == "docs_smoke" else resolve_parallel_workers(workers, target_count=len(modules)))
    )

    return {
        "mode": mode,
        "changed_files": changed,
        "unittest_modules": modules,
        "ast_discovered_modules": ast_discovered,
        "workers": eff_workers,
        "skip_unit_tests": not bool(modules),
        "should_run_bats": bats_needed,
        "run_bats": run_bats,
    }


def _clean_git_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of `os.environ` stripped of git hook `GIT_*` vars with fast offline defaults."""
    strip_keys = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
    }
    env = {k: v for k, v in os.environ.items() if k not in strip_keys}
    env.setdefault("PRE_COMMIT_HOOK", "1")
    env.setdefault("AGY_OFFLINE_ONLY", "1")
    if overrides:
        env.update({k: str(v) for k, v in overrides.items()})
    return env


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `selective_test_runner.py`."""
    parser = argparse.ArgumentParser(description="Run selective offline unit & BATS tests for agy-dev-plugin.")
    parser.add_argument("files", nargs="*", help="Optional explicit list of modified files.")
    parser.add_argument("--all", action="store_true", help="Force full unit test & BATS run.")
    parser.add_argument("--no-bats", action="store_true", help="Skip BATS execution (used by pre-commit unit-tests-fast hook).")
    parser.add_argument("--plan-only", action="store_true", help="Print selected test plan JSON without executing.")
    parser.add_argument("--audit-integrity", action="store_true", help="Verify 100%% of tests/test_*.py files are registered in ALL_TEST_MODULES.")
    parser.add_argument("--gha-outputs", action="store_true", help="Emit GitHub Actions outputs when GITHUB_OUTPUT is set.")
    parser.add_argument("--format", choices=("text", "json"), default="json", help="Plan output format (default: json).")
    parser.add_argument(
        "--workers",
        type=int,
        default=0,
        help="Parallel unittest worker processes via stdlib ProcessPoolExecutor (0=auto up to 4, 1=sequential; env AGY_TEST_WORKERS).",
    )
    parser.add_argument(
        "--no-ast-discovery",
        dest="no_ast_discovery",
        action="store_true",
        help="Disable Python AST import-graph auto-discovery.",
    )
    args = parser.parse_args(argv)

    integrity_issues = audit_router_integrity(REPO_ROOT)
    if args.audit_integrity:
        if integrity_issues:
            for issue in integrity_issues:
                print(f"[Selective Test Runner] INTEGRITY ERROR: {issue}", file=sys.stderr)
            return 1
        print(f"[Selective Test Runner] Router integrity OK: {len(ALL_TEST_MODULES)}/{len(ALL_TEST_MODULES)} test modules registered.")
        return 0
    if integrity_issues:
        for issue in integrity_issues:
            print(f"[Selective Test Runner] INTEGRITY ERROR: {issue}", file=sys.stderr)
        return 1

    explicit_files = list(args.files) if args.files else None
    req_workers = int(args.workers) if int(args.workers or 0) > 0 else None
    plan = build_test_plan(
        REPO_ROOT,
        changed_files=explicit_files,
        run_all=bool(args.all),
        no_bats=bool(args.no_bats),
        workers=req_workers,
        use_ast_discovery=not bool(args.no_ast_discovery),
    )
    modules = list(plan["unittest_modules"])
    run_bats = bool(plan["run_bats"])

    if args.gha_outputs:
        gha_out = str(os.environ.get("GITHUB_OUTPUT") or "").strip()
        lines = [
            f"mode={plan['mode']}",
            f"skip_unit_tests={'true' if plan['skip_unit_tests'] else 'false'}",
            f"unittest_modules={' '.join(modules)}",
            f"should_run_bats={'true' if plan['should_run_bats'] else 'false'}",
            f"run_bats={'true' if run_bats else 'false'}",
        ]
        if gha_out:
            with open(gha_out, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

    if args.plan_only:
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0

    test_env = _clean_git_env()
    if modules:
        run_res = run_unittest_targets(
            modules,
            repo_dir=REPO_ROOT,
            env=test_env,
            workers=req_workers,
            mode=str(plan.get("mode") or "selective"),
        )
        if int(run_res.get("returncode") or 0) != 0:
            return int(run_res["returncode"])
    else:
        print("[Selective Test Runner] SKIPPED (0.00s): clean working tree (0 changed files).")

    if run_bats:
        bats_files = sorted((REPO_ROOT / "tests" / "bats").glob("*.bats"))
        bats_bin = shutil.which("bats")
        if bats_files and bats_bin:
            proc_bats = subprocess.run(
                [bats_bin, *(str(p) for p in bats_files)],
                cwd=str(REPO_ROOT),
                env=test_env,
                check=False,
            )
            if proc_bats.returncode != 0:
                return int(proc_bats.returncode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
