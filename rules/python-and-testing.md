---
trigger: glob
globs: "**/*.py, **/*.sh, **/*.bats, **/pyproject.toml, **/.pre-commit-config.yaml, **/.github/workflows/*.yml"
description: Python coding standards, hermetic unit testing, static AST/Zero-PII audits, and declarative runtime rules.
---

# Python, Shell & Testing Standards (`python-and-testing.md`)

## 1. Language & Style Contract
- **English-Only Code & Docs**: Every Python identifier, comment, docstring, Markdown rule/skill, shell script, and commit message MUST be in English. Respond conversationally in the user's spoken language.
- **No Anti-Patterns**: Never use bare `except:` clauses or mutable default function arguments (`[]`, `{}`). Always check HTTP/subprocess timeouts and validate external paths against traversal.
- **File Size Discipline**: Keep every `rules/*.md` and `skills/*/SKILL.md` file strictly below `24,000` bytes so Antigravity loads all instructions without truncation.

## 2. Hermetic Unit Testing & Selective Runner
- **Test Every Deterministic Change**: Every new parser, validator, guardrail, CLI command, or state transition MUST include deterministic unit tests under `tests/` in the same commit.
- **Zero-Credential Mock Data Only**: Unit tests MUST run 100% offline with synthetic fixtures (`AcmeCorp`, `Globex`, `Initech`, `tmp_path`) — zero live tokens, network calls, or production workspace mutations.
- **Fast Iteration -> Full Gate**: Run targeted `python3 -m unittest <module>` during implementation, then verify the full suite via `selective_test_runner.py` (or `pytest` / `unittest discover`) + `bats` before pushing.

## 3. Declarative Environments & Static Auditing
- Pin Python and CLI tool versions in `.tool-versions` + `pyproject.toml`, and pin GitHub Actions runners (`ubuntu-24.04`) with non-blocking `workflow_dispatch` / PR triggers.
- Run [`project_audit.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py) (or repo `tests/scripts/qa_static_audit.py`) and `pre-commit run --all-files` cleanly on every change, ensuring zero leaked PII, secrets, or stale `$HOME` plugin parity.
