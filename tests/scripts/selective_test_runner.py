#!/usr/bin/env python3
"""Selective offline test runner for `agy-dev-plugin`."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ALL_TEST_MODULES = [
    "tests.test_agy_dev_plugin",
    "tests.test_dev_queue_cli_ux",
    "tests.test_dev_queue_doctor",
    "tests.test_dev_queue_planning",
    "tests.test_dev_queue_sidecar_panel",
    "tests.test_dev_queue_tui",
    "tests.test_model_quota_fallback",
    "tests.test_native_conversation_cleanup",
    "tests.test_orchestration_pause_resume",
    "tests.test_orchestration_wip_and_wake_fixes",
    "tests.test_parallel_file_leases_and_tui",
    "tests.test_parallel_subagent_orchestration",
    "tests.test_pr_remediator_preview_staging",
    "tests.test_pr_remediator_queue_and_cleanup",
    "tests.test_queue_compactor",
    "tests.test_sidecar_ghost_purge",
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
    "tests/test_dev_queue_cli_ux.py": "tests.test_dev_queue_cli_ux",
    "tests/test_dev_queue_doctor.py": "tests.test_dev_queue_doctor",
    "tests/test_dev_queue_planning.py": "tests.test_dev_queue_planning",
    "tests/test_dev_queue_sidecar_panel.py": "tests.test_dev_queue_sidecar_panel",
    "tests/test_dev_queue_tui.py": "tests.test_dev_queue_tui",
    "tests/test_model_quota_fallback.py": "tests.test_model_quota_fallback",
    "tests/test_native_conversation_cleanup.py": "tests.test_native_conversation_cleanup",
    "tests/test_orchestration_pause_resume.py": "tests.test_orchestration_pause_resume",
    "tests/test_orchestration_wip_and_wake_fixes.py": "tests.test_orchestration_wip_and_wake_fixes",
    "tests/test_parallel_file_leases_and_tui.py": "tests.test_parallel_file_leases_and_tui",
    "tests/test_parallel_subagent_orchestration.py": "tests.test_parallel_subagent_orchestration",
    "tests/test_pr_remediator_preview_staging.py": "tests.test_pr_remediator_preview_staging",
    "tests/test_pr_remediator_queue_and_cleanup.py": "tests.test_pr_remediator_queue_and_cleanup",
    "tests/test_queue_compactor.py": "tests.test_queue_compactor",
    "tests/test_sidecar_ghost_purge.py": "tests.test_sidecar_ghost_purge",
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


def select_test_modules(changed_files: list[str], *, run_all: bool = False) -> list[str]:
    """Select targeted `tests.test_*` modules (or fast docs smoke targets) from modified paths."""
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
                "test_parallel_subagent_orchestration.py",
                "selective_test_runner.py",
            )
        ):
            if "tests.test_parallel_subagent_orchestration" not in selected:
                selected.append("tests.test_parallel_subagent_orchestration")
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

        if norm not in ISOLATED_TEST_FILE_MODULES and "tests.test_agy_dev_plugin" not in selected:
            selected.append("tests.test_agy_dev_plugin")

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


def build_test_plan(
    repo_dir: Path = REPO_ROOT,
    *,
    changed_files: list[str] | None = None,
    run_all: bool = False,
    no_bats: bool = False,
) -> dict[str, Any]:
    """Build machine-readable selective test plan dictionary."""
    changed = [] if run_all else (list(changed_files) if changed_files is not None else detect_changed_files(repo_dir))
    modules = select_test_modules(changed, run_all=run_all)
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

    return {
        "mode": mode,
        "changed_files": changed,
        "unittest_modules": modules,
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
    plan = build_test_plan(
        REPO_ROOT,
        changed_files=explicit_files,
        run_all=bool(args.all),
        no_bats=bool(args.no_bats),
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
        cmd = [sys.executable, "-m", "unittest", *modules, "-v"]
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=test_env, check=False)
        if proc.returncode != 0:
            return int(proc.returncode)
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
