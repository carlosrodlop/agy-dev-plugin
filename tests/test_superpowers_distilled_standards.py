#!/usr/bin/env python3
"""Hermetic unit tests for distilled Superpowers engineering practices and Tier-0 `project_audit.py` gates."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-project-standards" / "scripts"
if str(AUDIT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(AUDIT_SCRIPTS_DIR))

import project_audit  # noqa: E402


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class TestSuperpowersTier0AuditGates(unittest.TestCase):
    """Verify Tier-0 deterministic warnings for arbitrary test sleeps and bloated skill descriptions."""

    def test_arbitrary_sleep_in_tests_emits_warning_unless_allowed_or_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Positive triggers (>= 0.25s in tests/**/test_*.py)
            _write_text(
                root / "tests" / "test_flaky_waits.py",
                (
                    "import time\n"
                    "from time import sleep\n\n"
                    "def test_slow_wait() -> None:\n"
                    "    time.sleep(0.25)\n"
                    "    sleep(1.5)\n"
                ),
            )
            _write_text(
                root / "tests" / "nested" / "test_sub_wait.py",
                (
                    "import time as t\n\n"
                    "def test_nested_wait() -> None:\n"
                    "    t.sleep(0.5)\n"
                ),
            )
            # Allowed / negative cases
            _write_text(
                root / "tests" / "test_clean_waits.py",
                (
                    "import time\n\n"
                    "def test_short_and_documented_waits() -> None:\n"
                    "    time.sleep(0.05)\n"
                    "    time.sleep(0.24)\n"
                    "    time.sleep(0.5)  # allow-sleep: deterministic watchdog interval test\n"
                    "    time.sleep(\n"
                    "        1.0\n"
                    "    )  # allow-sleep: multiline justification\n"
                ),
            )
            # Non-test files and helper modules must not trigger py:test-arbitrary-sleep
            _write_text(
                root / "tests" / "helpers.py",
                "import time\n\ndef backoff() -> None:\n    time.sleep(2.0)\n",
            )
            _write_text(
                root / "scripts" / "poller.py",
                "import time\n\ndef poll() -> None:\n    time.sleep(5.0)\n",
            )

            report = project_audit.audit_repository(root)
            self.assertTrue(report.passed, msg="Advisory warning must not fail non-strict audit.")
            sleep_warns = [w for w in report.warnings if w.rule_id == "py:test-arbitrary-sleep"]
            self.assertEqual(len(sleep_warns), 3, msg=f"Unexpected warnings: {sleep_warns}")
            self.assertEqual(
                sorted((w.file, w.line) for w in sleep_warns),
                [
                    ("tests/nested/test_sub_wait.py", 4),
                    ("tests/test_flaky_waits.py", 5),
                    ("tests/test_flaky_waits.py", 6),
                ],
            )

            strict_report = project_audit.audit_repository(root, strict=True)
            self.assertFalse(strict_report.passed)

    def test_skill_description_bloat_triggers_above_1800_chars_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exact_cap = "Use when " + ("a" * (1800 - len("Use when ")))
            self.assertEqual(len(exact_cap), 1800)
            _write_text(
                root / "skills" / "cap-ok" / "SKILL.md",
                f"---\nname: cap-ok\ndescription: {exact_cap}\n---\n\n# Cap OK\n",
            )

            over_cap = "Use when " + ("b" * (1801 - len("Use when ")))
            self.assertEqual(len(over_cap), 1801)
            _write_text(
                root / "skills" / "over-inline" / "SKILL.md",
                f"---\nname: over-inline\ndescription: {over_cap}\n---\n\n# Over Inline\n",
            )

            chunk_a = "Use when " + ("c" * 900)
            chunk_b = "d" * 905
            _write_text(
                root / "skills" / "over-multiline" / "SKILL.md",
                f"---\nname: over-multiline\ndescription: >-\n  {chunk_a}\n  {chunk_b}\n---\n\n# Over Multiline\n",
            )

            report = project_audit.audit_repository(root)
            self.assertTrue(report.passed, msg="Advisory warning must not fail non-strict audit.")
            bloat_warns = [w for w in report.warnings if w.rule_id == "agy:skill-description-bloat"]
            self.assertEqual(
                sorted(w.file for w in bloat_warns),
                [
                    "skills/over-inline/SKILL.md",
                    "skills/over-multiline/SKILL.md",
                ],
            )

            strict_report = project_audit.audit_repository(root, strict=True)
            self.assertFalse(strict_report.passed)


class TestSuperpowersDistilledContracts(unittest.TestCase):
    """Verify distilled engineering contracts and strict <24KB size limits across all target markdown files."""

    def test_all_modified_markdown_files_under_24kb_and_contain_required_contracts(self) -> None:
        expected_contracts: dict[str, tuple[str, ...]] = {
            "rules/python-and-testing.md": (
                "Name the Break",
                "Behavior-Over-Text Assertions",
                "Anti-Mock-Tautology",
                "full-structure mocks",
                "Mental Mutation Check",
                "RED->GREEN",
                "Condition-Based Waiting",
                "py:test-arbitrary-sleep",
                "# allow-sleep:",
            ),
            "agents/agy-code-pr-reviewer.md": (
                "No Sub-Spawning",
                "Vision Document Rule",
                "Declined to Judge",
                "Cannot Verify From Diff",
                "semantic test quality",
                "trigger-only",
            ),
            "skills/agy-code-design-refactor/SKILL.md": (
                "Systematic Debugging",
                "4-Phase Root-Cause Analysis",
                "3-Failure Architecture Stop",
                "Root-Cause Tracing",
                "4-Layer Defense-in-Depth",
            ),
            "agents/agy-code-developer.md": (
                "DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED",
                "Pre-Staging Self-Review",
                "Early-Escalation",
                "Critical Code Review Reception",
                "YAGNI",
            ),
            "agents/agy-dev-executor.md": (
                "DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED",
                "Pre-Staging Self-Review",
                "Early-Escalation",
                "Critical Code Review Reception",
                "YAGNI",
            ),
            "agents/agy-dev-orchestrator.md": (
                "DONE | DONE_WITH_CONCERNS | NEEDS_CONTEXT | BLOCKED",
                "NEVER retry `BLOCKED` identically",
                "NEEDS_CONTEXT",
                "DONE_WITH_CONCERNS",
            ),
            "skills/agy-code-remediation-pr/SKILL.md": (
                "Critical Code Review Reception",
                "Anti-Performative Agreement",
                "Verify Against Codebase First",
                "YAGNI Grep Check",
                "Clarify Ambiguous Items",
            ),
            "rules/co-design-and-discovery.md": (
                "Review Focus",
                "Builder Check",
                "Spike",
            ),
            "agents/agy-dev-planner.md": (
                "Review Focus",
                "Builder Check",
                "Spike",
            ),
            "skills/agy-project-standards/SKILL.md": (
                "py:test-arbitrary-sleep",
                "agy:skill-description-bloat",
                "Use when...",
                "Pressure-Testing Guidance",
            ),
        }

        for rel_path, required_terms in expected_contracts.items():
            fpath = REPO_ROOT / rel_path
            self.assertTrue(fpath.is_file(), msg=f"Missing target doc: {rel_path}")
            size_bytes = fpath.stat().st_size
            self.assertLess(
                size_bytes,
                project_audit.MAX_MARKDOWN_INSTRUCTION_BYTES,
                msg=f"{rel_path} exceeded 24KB limit ({size_bytes} bytes)",
            )
            text = fpath.read_text(encoding="utf-8")
            for term in required_terms:
                self.assertIn(term, text, msg=f"{rel_path} missing required contract term: {term}")

        orch_skill = REPO_ROOT / "skills" / "agy-dev-queue-orchestrator" / "SKILL.md"
        self.assertLess(orch_skill.stat().st_size, project_audit.MAX_MARKDOWN_INSTRUCTION_BYTES)

    def test_current_repo_passes_strict_project_audit_with_new_tier0_gates(self) -> None:
        report = project_audit.audit_repository(REPO_ROOT, strict=True)
        self.assertTrue(report.passed, msg=f"Strict project_audit failed: {report.to_dict()}")


if __name__ == "__main__":
    unittest.main()
