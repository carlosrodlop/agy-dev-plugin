#!/usr/bin/env python3
"""Reusable Static Engineering, Zero-PII & Plugin Standards Auditor (`project_audit.py`).

Validates any repository or Antigravity plugin against core engineering pillars:
  - Python AST safety (syntax, bare `except:`, mutable default arguments)
  - Zero-PII & secret leak guardrails (high-confidence token patterns, machine `/Users/<name>/` paths)
  - Antigravity plugin/rule/skill contracts (`plugin.json`, `<24,000` byte ceiling, YAML frontmatter)
  - Optional Copilot review instructions size hygiene (`.github/copilot-instructions.md` `<= 4,000` bytes)
  - Santa Lockdown compliance (`curl|sh`, `wget|sh`, `mise` pipes blocked; declarative tooling checks)
  - POSIX shell syntax (`bash -n`) and GitHub Actions runner pinning (`ubuntu-24.04`)
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

MAX_MARKDOWN_INSTRUCTION_BYTES = 24000
MAX_COPILOT_INSTRUCTIONS_BYTES = 4000
COPILOT_INSTRUCTIONS_REL_PATH = ".github/copilot-instructions.md"

SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "htmlcov",
        ".tox",
    }
)

CODE_AND_DOC_SUFFIXES = frozenset(
    {".py", ".sh", ".bats", ".md", ".yml", ".yaml", ".json", ".toml", ".example", ".txt"}
)

LOCAL_USER_PATH_RE = re.compile(r"/Users/([a-zA-Z0-9._-]{2,})/")
ALLOWLISTED_PLACEHOLDER_USERS = frozenset(
    {"user", "username", "your_user", "alice", "bob", "runner", "developer", "tam"}
)

SECRET_TOKEN_REGEXES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b")),
    ("Slack Token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    ("Private Key Header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)

SANTA_PIPE_RE = re.compile(
    r"(?:curl|wget)\s+[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b|^\s*mise\s+(?:install|use)\b",
    re.MULTILINE,
)


@dataclass
class AuditFinding:
    """Single finding produced by `audit_repository`."""

    severity: str  # "error" | "warning"
    rule_id: str
    file: str
    line: int = 0
    message: str = ""


@dataclass
class AuditReport:
    """Structured result summary for a repository audit."""

    repo_dir: str
    passed: bool
    files_scanned: int = 0
    errors: list[AuditFinding] = field(default_factory=list)
    warnings: list[AuditFinding] = field(default_factory=list)
    checks_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert report to JSON-serializable dict."""
        return {
            "repo_dir": self.repo_dir,
            "passed": self.passed,
            "files_scanned": self.files_scanned,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "errors": [asdict(f) for f in self.errors],
            "warnings": [asdict(f) for f in self.warnings],
            "checks_summary": self.checks_summary,
        }


def _iter_repo_files(repo_dir: Path) -> list[Path]:
    """Enumerate trackable repository files, respecting standard ignored directories."""
    git_bin = shutil.which("git")
    if git_bin and (repo_dir / ".git").exists():
        proc = subprocess.run(
            [git_bin, "-C", str(repo_dir), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            out: list[Path] = []
            for raw_rel in proc.stdout.split(b"\0"):
                rel = raw_rel.decode("utf-8", errors="ignore").strip()
                if not rel:
                    continue
                p = repo_dir / rel
                if p.is_file() and not any(part in SKIP_DIR_NAMES for part in Path(rel).parts):
                    out.append(p)
            if out:
                return sorted(out)

    collected: list[Path] = []
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in sorted(dirs) if d not in SKIP_DIR_NAMES]
        root_path = Path(root)
        for fname in sorted(files):
            fpath = root_path / fname
            if fpath.is_file():
                collected.append(fpath)
    return collected


def _check_python_ast(fpath: Path, rel: str, errors: list[AuditFinding], warnings: list[AuditFinding]) -> None:
    """Validate Python AST syntax, bare except clauses, and mutable defaults."""
    try:
        src = fpath.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(AuditFinding("error", "py:read-error", rel, 0, f"Cannot read Python file: {exc}"))
        return

    try:
        tree = ast.parse(src, filename=rel)
    except SyntaxError as exc:
        errors.append(
            AuditFinding("error", "py:syntax-error", rel, exc.lineno or 0, f"SyntaxError: {exc.msg}")
        )
        return

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            errors.append(
                AuditFinding("error", "py:bare-except", rel, node.lineno, "Bare 'except:' clause is forbidden.")
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            all_defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
            for d_node in all_defaults:
                if isinstance(d_node, (ast.List, ast.Dict, ast.Set)):
                    warnings.append(
                        AuditFinding(
                            "warning",
                            "py:mutable-default",
                            rel,
                            d_node.lineno,
                            f"Mutable default argument in function '{node.name}'; prefer None + internal copy.",
                        )
                    )


def _check_secrets_and_pii(
    fpath: Path,
    rel: str,
    self_resolved: Path,
    errors: list[AuditFinding],
) -> None:
    """Scan text file for leaked secret tokens or machine-local `/Users/<user>/` paths."""
    if fpath.resolve() == self_resolved:
        return
    if fpath.suffix.lower() not in CODE_AND_DOC_SUFFIXES and fpath.name not in ("Makefile", ".gitignore"):
        return
    try:
        text = fpath.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    lines = text.splitlines()
    for label, pattern in SECRET_TOKEN_REGEXES:
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            errors.append(
                AuditFinding("error", "sec:secret-pattern", rel, line_no, f"Detected potential {label}.")
            )

    for idx, line in enumerate(lines, start=1):
        for match in LOCAL_USER_PATH_RE.finditer(line):
            user_seg = match.group(1).lower()
            if user_seg not in ALLOWLISTED_PLACEHOLDER_USERS and not user_seg.startswith("<"):
                errors.append(
                    AuditFinding(
                        "error",
                        "sec:local-user-path",
                        rel,
                        idx,
                        f"Hardcoded machine path '/Users/{match.group(1)}/' must be parameterized with $HOME or ~.",
                    )
                )

    if (fpath.suffix.lower() in {".sh", ".bats", ".yml", ".yaml", ".json", ".py"} or fpath.name == "Makefile") and SANTA_PIPE_RE.search(text):
        errors.append(
            AuditFinding(
                "error",
                "sec:santa-unapproved-pipe",
                rel,
                0,
                "Detected unapproved binary pipe (curl|sh, wget|sh, or mise install/use).",
            )
        )


def _check_markdown_limits(fpath: Path, rel: str, errors: list[AuditFinding], warnings: list[AuditFinding]) -> None:
    """Verify Antigravity rules/skills/agents (24 KB + YAML frontmatter) and optional Copilot 4 KB hygiene cap."""
    normalized_rel = rel.replace(os.sep, "/")
    if normalized_rel.startswith("./"):
        normalized_rel = normalized_rel[2:]
    if normalized_rel == COPILOT_INSTRUCTIONS_REL_PATH:
        size_bytes = fpath.stat().st_size
        if size_bytes > MAX_COPILOT_INSTRUCTIONS_BYTES:
            warnings.append(
                AuditFinding(
                    "warning",
                    "agy:copilot-instructions-bloat",
                    COPILOT_INSTRUCTIONS_REL_PATH,
                    0,
                    (
                        f"Copilot instructions file is {size_bytes} bytes "
                        f"(> {MAX_COPILOT_INSTRUCTIONS_BYTES} byte hygiene cap); keep concise to limit AI credit usage."
                    ),
                )
            )
        return

    rel_parts = Path(normalized_rel).parts[:1]
    is_rule = rel_parts == ("rules",) and fpath.suffix == ".md"
    is_agent = rel_parts == ("agents",) and fpath.suffix == ".md"
    is_skill = fpath.name == "SKILL.md"
    if not (is_rule or is_skill or is_agent):
        return
    size_bytes = fpath.stat().st_size
    if size_bytes >= MAX_MARKDOWN_INSTRUCTION_BYTES:
        errors.append(
            AuditFinding(
                "error",
                "agy:markdown-size-limit",
                rel,
                0,
                f"Instruction file is {size_bytes} bytes (>= {MAX_MARKDOWN_INSTRUCTION_BYTES} byte AGY limit).",
            )
        )
    text = fpath.read_text(encoding="utf-8", errors="ignore")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        warnings.append(
            AuditFinding("warning", "agy:missing-frontmatter", rel, 1, "Missing YAML frontmatter block.")
        )
        return
    if is_agent:
        fm_block = text[4 : text.index("\n---\n", 4)]
        for req_key in ("name:", "description:"):
            if req_key not in fm_block:
                errors.append(
                    AuditFinding("error", "agy:agent-frontmatter", rel, 1, f"Agent missing required '{req_key}' frontmatter field.")
                )
        if "tools:" not in fm_block:
            warnings.append(
                AuditFinding("warning", "agy:agent-tools-missing", rel, 1, "Agent should declare explicit 'tools:' frontmatter list.")
            )


def _check_hooks_and_plugin_manifest(
    resolved_repo: Path,
    errors: list[AuditFinding],
    warnings: list[AuditFinding],
) -> None:
    """Validate Antigravity `plugin.json` manifest bounds and flat/named `hooks.json` schema."""
    plugin_path = resolved_repo / "plugin.json"
    if plugin_path.is_file():
        try:
            pdata = json.loads(plugin_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pdata = None
        if isinstance(pdata, dict):
            prompts = pdata.get("suggestedPrompts")
            if isinstance(prompts, list) and len(prompts) > 3:
                warnings.append(
                    AuditFinding(
                        "warning",
                        "agy:suggested-prompts-limit",
                        "plugin.json",
                        0,
                        f"suggestedPrompts has {len(prompts)} entries; Antigravity recommends at most 3.",
                    )
                )

    hooks_path = resolved_repo / "hooks.json"
    if not hooks_path.is_file():
        return
    try:
        hdata = json.loads(hooks_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(hdata, dict):
        errors.append(AuditFinding("error", "agy:hooks-schema", "hooks.json", 0, "hooks.json root must be a JSON object."))
        return
    if "hooks" in hdata and isinstance(hdata["hooks"], dict):
        errors.append(
            AuditFinding(
                "error",
                "agy:hooks-invalid-root-wrapper",
                "hooks.json",
                0,
                "hooks.json must map top-level named hook keys directly (remove deprecated outer 'hooks' wrapper).",
            )
        )
        return

    flat_events = {"SessionStart", "PreInvocation", "PostInvocation", "Stop"}
    grouped_events = {"PreToolUse", "PostToolUse"}
    allowed_keys = flat_events | grouped_events | {"enabled"}
    for hook_name, hook_spec in hdata.items():
        if not isinstance(hook_spec, dict):
            errors.append(AuditFinding("error", "agy:hooks-schema", "hooks.json", 0, f"Hook '{hook_name}' must be an object."))
            continue
        for ev_key, ev_val in hook_spec.items():
            if ev_key not in allowed_keys:
                warnings.append(AuditFinding("warning", "agy:hooks-unknown-key", "hooks.json", 0, f"Unknown hook field '{ev_key}' under '{hook_name}'."))
            elif ev_key in flat_events:
                if not isinstance(ev_val, list) or any(not isinstance(it, dict) or "command" not in it for it in ev_val):
                    errors.append(
                        AuditFinding(
                            "error",
                            "agy:hooks-flat-event-schema",
                            "hooks.json",
                            0,
                            f"{hook_name}.{ev_key} must be a flat list of handler objects containing 'command' (no matcher wrapper).",
                        )
                    )
            elif ev_key in grouped_events:
                if not isinstance(ev_val, list) or any(
                    not isinstance(it, dict) or not isinstance(it.get("hooks"), list) for it in ev_val
                ):
                    errors.append(
                        AuditFinding(
                            "error",
                            "agy:hooks-grouped-event-schema",
                            "hooks.json",
                            0,
                            f"{hook_name}.{ev_key} must contain matcher groups with 'hooks' arrays.",
                        )
                    )


def _check_shell_and_workflows(
    fpath: Path,
    rel: str,
    errors: list[AuditFinding],
    warnings: list[AuditFinding],
) -> None:
    """Validate POSIX/Bash syntax on `.sh` files and runner pinning on GitHub workflows."""
    if fpath.suffix == ".sh":
        bash_bin = shutil.which("bash")
        if bash_bin:
            proc = subprocess.run([bash_bin, "-n", str(fpath)], capture_output=True, text=True, timeout=10, check=False)
            if proc.returncode != 0:
                errors.append(
                    AuditFinding(
                        "error",
                        "sh:syntax-error",
                        rel,
                        0,
                        f"bash -n failed: {(proc.stderr or proc.stdout).strip()}",
                    )
                )
    elif ".github/workflows/" in rel and fpath.suffix in (".yml", ".yaml"):
        text = fpath.read_text(encoding="utf-8", errors="ignore")
        if "runs-on: ubuntu-latest" in text:
            warnings.append(
                AuditFinding(
                    "warning",
                    "ci:unpinned-runner",
                    rel,
                    0,
                    "Pin 'runs-on: ubuntu-24.04' instead of mutable 'ubuntu-latest'.",
                )
            )


def audit_repository(repo_dir: Path | str, *, strict: bool = False) -> AuditReport:
    """Run all static engineering, Zero-PII, shell, and plugin contract checks against `repo_dir`."""
    resolved_repo = Path(repo_dir).expanduser().resolve()
    if not resolved_repo.is_dir():
        return AuditReport(
            repo_dir=str(resolved_repo),
            passed=False,
            errors=[AuditFinding("error", "repo:not-found", ".", 0, f"Directory not found: {resolved_repo}")],
        )

    files = _iter_repo_files(resolved_repo)
    self_resolved = Path(__file__).resolve()
    errors: list[AuditFinding] = []
    warnings: list[AuditFinding] = []
    checked_copilot_instructions = False

    for fpath in files:
        rel = str(fpath.relative_to(resolved_repo)).replace(os.sep, "/")
        if rel == COPILOT_INSTRUCTIONS_REL_PATH:
            checked_copilot_instructions = True
        if fpath.suffix == ".py":
            _check_python_ast(fpath, rel, errors, warnings)
        elif fpath.suffix == ".json":
            try:
                json.loads(fpath.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(AuditFinding("error", "json:invalid", rel, 0, f"Invalid JSON: {exc}"))

        _check_secrets_and_pii(fpath, rel, self_resolved, errors)
        _check_markdown_limits(fpath, rel, errors, warnings)
        _check_shell_and_workflows(fpath, rel, errors, warnings)

    copilot_path = resolved_repo / ".github" / "copilot-instructions.md"
    if not checked_copilot_instructions and copilot_path.is_file():
        _check_markdown_limits(copilot_path, COPILOT_INSTRUCTIONS_REL_PATH, errors, warnings)

    if (resolved_repo / "plugin.json").is_file():
        for expected in (".gitignore", ".tool-versions", "pyproject.toml", "install.sh"):
            if not (resolved_repo / expected).is_file():
                warnings.append(
                    AuditFinding("warning", "plugin:missing-standard-file", expected, 0, f"Recommended baseline file missing: {expected}")
                )
        _check_hooks_and_plugin_manifest(resolved_repo, errors, warnings)

    passed = len(errors) == 0 and (len(warnings) == 0 if strict else True)
    return AuditReport(
        repo_dir=str(resolved_repo),
        passed=passed,
        files_scanned=len(files),
        errors=errors,
        warnings=warnings,
        checks_summary={
            "python_files": sum(1 for f in files if f.suffix == ".py"),
            "shell_files": sum(1 for f in files if f.suffix == ".sh"),
            "markdown_files": sum(1 for f in files if f.suffix == ".md"),
            "strict_mode": strict,
        },
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `project_audit.py`."""
    parser = argparse.ArgumentParser(
        prog="project_audit",
        description="Static engineering standards, Zero-PII, and Antigravity plugin auditor.",
    )
    parser.add_argument("--repo", default=".", help="Path to target repository (default: current directory).")
    parser.add_argument("--format", choices=("text", "json"), default="text", help="Output format.")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    args = parser.parse_args(argv)

    report = audit_repository(args.repo, strict=args.strict)
    if args.format == "json":
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        status_label = "PASS" if report.passed else "FAIL"
        print(
            f"[project_audit] {status_label}: scanned {report.files_scanned} files in {report.repo_dir} "
            f"({len(report.errors)} errors, {len(report.warnings)} warnings)"
        )
        for item in (*report.errors, *report.warnings):
            loc = f"{item.file}:{item.line}" if item.line else item.file
            print(f"  [{item.severity.upper()}] {item.rule_id} @ {loc}: {item.message}")

    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
