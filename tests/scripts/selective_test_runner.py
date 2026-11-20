#!/usr/bin/env python3
"""Selective offline test runner for `agy-dev-plugin`."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]


def detect_changed_files(repo_dir: Path) -> list[str]:
    """Return uncommitted or recent files changed relative to HEAD when available."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if proc.returncode != 0:
        return []
    changed: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) > 3:
            changed.append(line[3:].strip().strip('"'))
    return changed


def select_test_modules(changed_files: list[str], *, run_all: bool = False) -> list[str]:
    """Select targeted `tests.test_*` modules from modified paths."""
    all_modules = ["tests.test_agy_dev_plugin", "tests.test_pr_remediator_queue_and_cleanup"]
    if run_all or not changed_files:
        return all_modules

    selected: list[str] = []
    for rel in changed_files:
        if rel.endswith("pr_remediator.py") or rel.endswith("test_pr_remediator_queue_and_cleanup.py"):
            if "tests.test_pr_remediator_queue_and_cleanup" not in selected:
                selected.append("tests.test_pr_remediator_queue_and_cleanup")
        if "tests.test_agy_dev_plugin" not in selected:
            selected.append("tests.test_agy_dev_plugin")
    return selected or all_modules


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `selective_test_runner.py`."""
    parser = argparse.ArgumentParser(description="Run selective offline unit tests for agy-dev-plugin.")
    parser.add_argument("--all", action="store_true", help="Force full unit test run.")
    args = parser.parse_args(argv)

    changed = [] if args.all else detect_changed_files(REPO_ROOT)
    modules = select_test_modules(changed, run_all=args.all)
    cmd = [sys.executable, "-m", "unittest", *modules, "-v"]
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT), check=False)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
