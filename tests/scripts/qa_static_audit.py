#!/usr/bin/env python3
"""Repository-specific Static QA & Zero-PII Audit wrapper for `agy-dev-plugin`."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-project-standards" / "scripts"
if str(AUDIT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(AUDIT_SCRIPTS_DIR))

from project_audit import (  # noqa: E402
    AuditFinding,
    audit_repository,
    validate_plugin_requirements,
)


def run_qa_audit(repo_dir: Path | str | None = None, *, strict: bool = False) -> dict[str, object]:
    """Run generic `project_audit` checks plus `agy-dev-plugin` independence and requirements contracts."""
    target = Path(repo_dir).expanduser().resolve() if repo_dir else REPO_ROOT
    report = audit_repository(target, strict=strict)

    if target == REPO_ROOT and not (target / "requirements.json").is_file():
        report.errors.append(
            AuditFinding(
                "error",
                "req:missing-manifest",
                "requirements.json",
                0,
                "Plugin repository must provide a root requirements.json manifest (schema_version == '1.0').",
            )
        )

    # Verify core dev plugin runtime (`clients/dev_common.py` and `clients/git_client.py`) stays independent of `wiz_common`
    for rel_py in ("clients/dev_common.py", "clients/git_client.py"):
        fpath = target / rel_py
        if fpath.is_file():
            src = fpath.read_text(encoding="utf-8", errors="ignore")
            for idx, line in enumerate(src.splitlines(), start=1):
                if ("import wiz_common" in line or "from wiz_common" in line) and not line.strip().startswith("#"):
                    report.errors.append(
                        AuditFinding(
                            "error",
                            "dev:no-wiz-common-import",
                            rel_py,
                            idx,
                            "Core dev plugin modules must not hard-import wiz_common.",
                        )
                    )

    report.passed = len(report.errors) == 0 and (len(report.warnings) == 0 if strict else True)
    return report.to_dict()


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `qa_static_audit.py`."""
    parser = argparse.ArgumentParser(description="Static QA & Zero-PII audit for agy-dev-plugin.")
    parser.add_argument("--repo", default=str(REPO_ROOT), help="Repository directory to audit.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as errors.")
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="Compatibility flag for pr_remediator test-local gate (performs full fast AST/Zero-PII scan).",
    )
    args = parser.parse_args(argv)

    result = run_qa_audit(args.repo, strict=bool(args.strict or args.incremental))
    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        status = "PASS" if result["passed"] else "FAIL"
        print(
            f"[qa_static_audit] {status}: {result['files_scanned']} files scanned "
            f"({result['error_count']} errors, {result['warning_count']} warnings)"
        )
        for item in (*result["errors"], *result["warnings"]):
            loc = f"{item['file']}:{item['line']}" if item["line"] else item["file"]
            print(f"  [{str(item['severity']).upper()}] {item['rule_id']} @ {loc}: {item['message']}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
