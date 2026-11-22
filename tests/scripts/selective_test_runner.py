#!/usr/bin/env python3
"""Selective offline test runner for `agy-dev-plugin`."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
ALL_TEST_MODULES = [
    "tests.test_agy_dev_plugin",
    "tests.test_dev_queue_doctor",
    "tests.test_dev_queue_tui",
    "tests.test_parallel_subagent_orchestration",
    "tests.test_pr_remediator_queue_and_cleanup",
]


def detect_changed_files(repo_dir: Path) -> list[str]:
    """Return uncommitted files, falling back to `origin/main...HEAD` diff when working tree is clean."""
    changed: list[str] = []
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        for line in proc.stdout.splitlines():
            if len(line) > 3:
                path_part = line[3:].strip().strip('"')
                if " -> " in path_part:
                    path_part = path_part.split(" -> ", 1)[1].strip()
                changed.append(path_part)
        return changed

    proc_diff = subprocess.run(
        ["git", "-C", str(repo_dir), "diff", "--name-only", "origin/main...HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if proc_diff.returncode == 0 and proc_diff.stdout.strip():
        return [ln.strip() for ln in proc_diff.stdout.splitlines() if ln.strip()]
    return []


def select_test_modules(changed_files: list[str], *, run_all: bool = False) -> list[str]:
    """Select targeted `tests.test_*` modules from modified paths."""
    if run_all or not changed_files:
        return list(ALL_TEST_MODULES)

    selected: list[str] = []
    for rel in changed_files:
        norm = rel.replace("\\", "/")
        if norm.endswith(
            (
                "parallel_scope.py",
                "rebase_gate.py",
                "pr_remediator.py",
                "test_parallel_subagent_orchestration.py",
                "selective_test_runner.py",
            )
        ):
            if "tests.test_parallel_subagent_orchestration" not in selected:
                selected.append("tests.test_parallel_subagent_orchestration")
        if (
            norm.endswith(("pr_remediator.py", "dev_flow_optimizer.py", "dev_queue_doctor.py", "test_pr_remediator_queue_and_cleanup.py", "dev_common.py"))
            or norm.startswith("agents/")
        ):
            if "tests.test_pr_remediator_queue_and_cleanup" not in selected:
                selected.append("tests.test_pr_remediator_queue_and_cleanup")
        if (
            norm.endswith(("dev_queue_doctor.py", "test_dev_queue_doctor.py", "pr_remediator.py", "dev_flow_optimizer.py", "dev_queue_tui.py", "agy_dev_queue.sh", "dev_common.py"))
            or norm.startswith(("skills/agy-dev-queue-orchestrator/", "shell/", "sidecars/"))
        ):
            if "tests.test_dev_queue_doctor" not in selected:
                selected.append("tests.test_dev_queue_doctor")
        if (
            norm.endswith(("dev_queue_tui.py", "dev_flow_optimizer.py", "dev_queue_doctor.py", "test_dev_queue_tui.py", "agy_dev_queue.sh", "dev_common.py", "git_client.py"))
            or norm.startswith("skills/agy-dev-queue-orchestrator/")
            or norm.startswith("shell/")
        ):
            if "tests.test_dev_queue_tui" not in selected:
                selected.append("tests.test_dev_queue_tui")
        if "tests.test_agy_dev_plugin" not in selected:
            selected.append("tests.test_agy_dev_plugin")
    return selected or list(ALL_TEST_MODULES)


def should_run_bats(changed_files: list[str], *, run_all: bool = False) -> bool:
    """Return True when BATS CLI/installer checks are relevant to modified files."""
    if run_all or not changed_files:
        return True
    markers = (".sh", ".bats", "hooks.json", "plugin.json", "dev_common.py", "git_client.py", "pr_remediator.py", "project_audit.py")
    return any(rel.endswith(markers) or rel.replace("\\", "/").startswith("sidecars/") for rel in changed_files)


def _clean_git_env() -> dict[str, str]:
    """Return a copy of `os.environ` stripped of git hook `GIT_*` vars so test subprocesses stay isolated."""
    strip_keys = {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_PREFIX",
    }
    return {k: v for k, v in os.environ.items() if k not in strip_keys}


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `selective_test_runner.py`."""
    parser = argparse.ArgumentParser(description="Run selective offline unit & BATS tests for agy-dev-plugin.")
    parser.add_argument("--all", action="store_true", help="Force full unit test & BATS run.")
    parser.add_argument("--plan-only", action="store_true", help="Print selected test plan JSON without executing.")
    parser.add_argument("--gha-outputs", action="store_true", help="Emit GitHub Actions outputs when GITHUB_OUTPUT is set.")
    args = parser.parse_args(argv)

    changed = [] if args.all else detect_changed_files(REPO_ROOT)
    modules = select_test_modules(changed, run_all=args.all)
    run_bats = should_run_bats(changed, run_all=args.all)

    if args.gha_outputs:
        gha_out = str(os.environ.get("GITHUB_OUTPUT") or "").strip()
        lines = [
            f"unittest_modules={' '.join(modules)}",
            f"run_bats={'true' if run_bats else 'false'}",
        ]
        if gha_out:
            with open(gha_out, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")

    if args.plan_only:
        print(
            json.dumps(
                {"changed_files": changed, "unittest_modules": modules, "run_bats": run_bats},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    test_env = _clean_git_env()
    cmd = [sys.executable, "-m", "unittest", *modules, "-v"]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=test_env, check=False)
    if proc.returncode != 0:
        return int(proc.returncode)

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
