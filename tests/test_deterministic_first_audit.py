#!/usr/bin/env python3
"""Hermetic unit tests for Deterministic-First Engineering rules and `project_audit.py` gates."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
AUDIT_SCRIPTS_DIR = REPO_ROOT / "skills" / "agy-project-standards" / "scripts"
if str(AUDIT_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(AUDIT_SCRIPTS_DIR))

import project_audit  # noqa: E402


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _build_heavy_skill_markdown(
    *,
    bash_blocks: int = 4,
    target_bytes: int = 13000,
    extra_line: str = "",
) -> str:
    header = (
        "---\n"
        "name: demo-heavy-skill\n"
        "description: Synthetic heavy skill fixture for deterministic audit testing.\n"
        "---\n\n"
        "# Demo Heavy Skill\n\n"
    )
    if extra_line:
        header += extra_line + "\n\n"
    blocks = "".join(
        f"## Step {idx}\n```bash\necho step-{idx}\n```\n\n" for idx in range(1, bash_blocks + 1)
    )
    body = header + blocks
    if len(body.encode("utf-8")) < target_bytes:
        pad_needed = target_bytes - len(body.encode("utf-8"))
        body += ("Reference engineering guidance line for deterministic audit fixture.\n" * ((pad_needed // 60) + 2))
    return body


class TestDeterministicFirstAuditGates(unittest.TestCase):
    """Verify 0-LLM static checks for scheduled sidecars and scriptless heavy skills."""

    def test_scheduled_sidecar_direct_agentapi_triggers_missing_pregate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(
                root / "scheduler" / "sidecars" / "direct-llm" / "sidecar.json",
                {
                    "builtin": "schedule",
                    "args": [
                        "CRON_TZ=Europe/Madrid 0 9 * * 1-5",
                        "agentapi",
                        "new-conversation",
                        "--model=pro",
                        "--",
                        "Run daily check",
                    ],
                },
            )
            report = project_audit.audit_repository(root)
            err_ids = {f.rule_id for f in report.errors}
            self.assertFalse(report.passed)
            self.assertIn("agy:schedule-missing-pregate", err_ids)

    def test_scheduled_sidecar_env_or_shell_direct_agentapi_triggers_missing_pregate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(
                root / "sidecars" / "env-llm" / "sidecar.json",
                {
                    "builtin": "schedule",
                    "args": [
                        "CRON_TZ=Europe/Madrid 0 9 * * 1-5",
                        "env",
                        "FOO=bar",
                        "/usr/local/bin/agentapi",
                        "new-conversation",
                    ],
                },
            )
            _write_json(
                root / "sidecars" / "shell-llm" / "sidecar.json",
                {
                    "builtin": "schedule",
                    "args": [
                        "CRON_TZ=Europe/Madrid 0 10 * * 1-5",
                        "bash",
                        "-lc",
                        "agentapi new-conversation --model=pro -- 'Run check'",
                    ],
                },
            )
            report = project_audit.audit_repository(root)
            pregate_errors = [f for f in report.errors if f.rule_id == "agy:schedule-missing-pregate"]
            self.assertEqual(len(pregate_errors), 2)

    def test_scheduled_sidecar_bare_flag_or_empty_command_triggers_invalid_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(
                root / "sidecars" / "bad-flag" / "sidecar.json",
                {
                    "builtin": "schedule",
                    "args": ["CRON_TZ=Europe/Madrid 0 9 * * 1-5", "--no-workspace", "python3", "run.py"],
                },
            )
            _write_json(
                root / "sidecars" / "bad-cmd" / "sidecar.json",
                {
                    "command": "--serve",
                    "args": ["server.py"],
                },
            )
            _write_json(
                root / "sidecars" / "short-sched" / "sidecar.json",
                {
                    "builtin": "schedule",
                    "args": ["CRON_TZ=Europe/Madrid 0 9 * * 1-5"],
                },
            )
            report = project_audit.audit_repository(root)
            invalid_exec_errors = [f for f in report.errors if f.rule_id == "agy:sidecar-invalid-executable"]
            self.assertEqual(len(invalid_exec_errors), 3)

    def test_scheduled_sidecar_zero_llm_and_pregated_pass_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_json(
                root / "scheduler" / "sidecars" / "zero-llm" / "sidecar.json",
                {
                    "builtin": "schedule",
                    "args": [
                        "CRON_TZ=Europe/Madrid 30 8 * * 1-5",
                        "python3",
                        "$HOME/.gemini/config/plugins/demo/scripts/health.py",
                        "check",
                        "--scheduled",
                    ],
                },
            )
            _write_json(
                root / "scheduler" / "sidecars" / "pregated-llm" / "sidecar.json",
                {
                    "builtin": "schedule",
                    "args": [
                        "CRON_TZ=Europe/Madrid 0 9 * * 1-5",
                        "python3",
                        "$HOME/.gemini/config/actions/_shared/action_precheck_runner.py",
                        "--action",
                        "demo-sync",
                        "--",
                        "agentapi",
                        "new-conversation",
                        "--model=flash",
                        "--",
                        "Synthesize brief from handoff file.",
                    ],
                },
            )
            _write_json(
                root / "sidecars" / "shell-pregated" / "sidecar.json",
                {
                    "builtin": "schedule",
                    "args": [
                        "CRON_TZ=Europe/Madrid 0 11 * * 1-5",
                        "bash",
                        "-c",
                        "python3 action_precheck_runner.py -- action && agentapi new-conversation",
                    ],
                },
            )
            _write_json(
                root / "sidecars" / "daemon" / "sidecar.json",
                {
                    "command": "python3",
                    "args": ["watchdog.py"],
                },
            )
            report = project_audit.audit_repository(root, strict=True)
            self.assertTrue(report.passed, msg=f"Unexpected findings: {report.to_dict()}")

    def test_heavy_scriptless_skill_emits_advisory_warning_unless_backed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unbacked = root / "skills" / "unbacked-heavy" / "SKILL.md"
            unbacked.parent.mkdir(parents=True, exist_ok=True)
            unbacked.write_text(_build_heavy_skill_markdown(bash_blocks=5, target_bytes=13500), encoding="utf-8")

            backed_local = root / "skills" / "backed-local" / "SKILL.md"
            (backed_local.parent / "scripts").mkdir(parents=True, exist_ok=True)
            backed_local.write_text(_build_heavy_skill_markdown(bash_blocks=5, target_bytes=13500), encoding="utf-8")
            (backed_local.parent / "scripts" / "helper.py").write_text("def run() -> int:\n    return 0\n", encoding="utf-8")

            backed_client = root / "skills" / "backed-client" / "SKILL.md"
            backed_client.parent.mkdir(parents=True, exist_ok=True)
            backed_client.write_text(
                _build_heavy_skill_markdown(
                    bash_blocks=5,
                    target_bytes=13500,
                    extra_line="Delegates all API operations to `clients/demo_client.py`.",
                ),
                encoding="utf-8",
            )

            small_skill = root / "skills" / "small-skill" / "SKILL.md"
            small_skill.parent.mkdir(parents=True, exist_ok=True)
            small_skill.write_text(_build_heavy_skill_markdown(bash_blocks=5, target_bytes=2000), encoding="utf-8")

            report = project_audit.audit_repository(root)
            self.assertTrue(report.passed, msg="Advisory warning must not fail non-strict audit.")
            skill_warns = [w for w in report.warnings if w.rule_id == "agy:skill-heavy-no-script"]
            self.assertEqual(len(skill_warns), 1)
            self.assertEqual(skill_warns[0].file, "skills/unbacked-heavy/SKILL.md")

            strict_report = project_audit.audit_repository(root, strict=True)
            self.assertFalse(strict_report.passed)


class TestDeterministicFirstPluginContracts(unittest.TestCase):
    """Verify Deterministic-First rule, pillar, Phase 0, planner, reviewer, and README parity."""

    def test_deterministic_first_rule_and_cross_file_contracts(self) -> None:
        rule_path = REPO_ROOT / "rules" / "deterministic-first-engineering.md"
        self.assertTrue(rule_path.is_file(), msg="rules/deterministic-first-engineering.md must exist")
        self.assertLess(rule_path.stat().st_size, project_audit.MAX_MARKDOWN_INSTRUCTION_BYTES)
        rule_text = rule_path.read_text(encoding="utf-8")
        self.assertTrue(rule_text.startswith("---\n"))
        self.assertIn("trigger: model_decision", rule_text)
        for phrase in (
            "4-Tier Determinism Ladder",
            "Tier 0",
            "Tier 1",
            "Tier 2",
            "Tier 3",
            "Deterministic Sandwich",
            "action_precheck_runner.py",
            "0600",
            "auto_refresh=False",
            "agy:sidecar-invalid-executable",
            "agy:schedule-missing-pregate",
            "agy:skill-heavy-no-script",
        ):
            self.assertIn(phrase, rule_text, msg=f"Missing Deterministic-First contract term: {phrase}")

        pillars_text = (REPO_ROOT / "rules" / "engineering-pillars.md").read_text(encoding="utf-8")
        self.assertIn("deterministic-first-engineering.md", pillars_text)
        self.assertIn("Deterministic-First Engineering", pillars_text)
        self.assertIn("4-Tier Determinism Ladder", pillars_text)

        co_design_text = (REPO_ROOT / "rules" / "co-design-and-discovery.md").read_text(encoding="utf-8")
        self.assertIn("deterministic-first-engineering.md", co_design_text)
        self.assertIn("Deterministic-First", co_design_text)

        planner_text = (REPO_ROOT / "agents" / "agy-dev-planner.md").read_text(encoding="utf-8")
        self.assertIn("deterministic-first-engineering.md", planner_text)
        self.assertIn("Tier 0..3", planner_text)

        reviewer_text = (REPO_ROOT / "agents" / "agy-code-pr-reviewer.md").read_text(encoding="utf-8")
        self.assertIn("deterministic-first-engineering.md", reviewer_text)
        self.assertIn("Deterministic-First Engineering", reviewer_text)

        standards_skill = (REPO_ROOT / "skills" / "agy-project-standards" / "SKILL.md").read_text(encoding="utf-8")
        for code in (
            "agy:sidecar-invalid-executable",
            "agy:schedule-missing-pregate",
            "agy:skill-heavy-no-script",
        ):
            self.assertIn(code, standards_skill)

        readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("deterministic-first-engineering.md", readme_text)

    def test_current_repo_passes_strict_audit_with_new_determinism_checks(self) -> None:
        report = project_audit.audit_repository(REPO_ROOT, strict=True)
        self.assertTrue(report.passed, msg=f"Strict audit failed on agy-dev-plugin: {report.to_dict()}")


if __name__ == "__main__":
    unittest.main()
