---
trigger: glob
globs: "**/*.py, **/pyproject.toml, **/.pre-commit-config.yaml, **/.github/workflows/*.yml"
description: Python 3 software engineering standards, static safety, Santa Lockdown pure-Python constraints, hermetic unit testing, and declarative runtime rules.
---

# Python & Testing Standards (`python-and-testing.md`)

## 1. Architecture, Typing & Modularity
- **English-Only Code & Docs**: Every Python identifier, comment, docstring, Markdown rule/skill, and commit message MUST be in English. Respond conversationally in the user's spoken language.
- **Modular Functional & Data-Driven Design**: Keep functions small, cohesive, and single-purpose. Separate pure domain logic (transformations, parsers, validators) from I/O boundaries (filesystem, network, subprocesses) so core logic is trivial to test without heavy mocking.
- **Strict Type Annotations**: Start modules with `from __future__ import annotations`, annotate public function signatures and return types, and model structured payloads with `@dataclass` or Pydantic v2 models instead of untyped nested dicts.
- **File Size Discipline**: Keep every `rules/*.md`, `agents/*.md`, and `skills/*/SKILL.md` file strictly below `24,000` bytes so Antigravity loads all instructions without truncation.

## 2. Security, Safety & Defensive Coding
- **Forbidden Anti-Patterns (Deterministic AST Errors)**:
  - Never use bare `except:` clauses; catch narrow exception types and surface actionable context.
  - Never use `eval()` or `exec()` on runtime strings.
  - Never call `yaml.load(...)` without `SafeLoader` (`yaml.safe_load(...)` only).
- **Defensive Patterns (Warnings & Hygiene)**:
  - Never use mutable default arguments (`[]`, `{}`, `set()`); default to `None` and initialize inside the function.
  - Pass explicit argument lists to `subprocess.run([...])` without `shell=True`, always setting explicit `timeout=...` on subprocesses and HTTP calls.
  - Use `pathlib.Path` for filesystem paths, validate untrusted paths with `.resolve()` against allowed root directories to prevent path traversal, and enforce `0600` / `0700` permissions on sensitive local state (`os.chmod`).
  - Gate any optional macOS desktop/Keychain integration behind runtime `platform.system() == "Darwin"` checks so every script runs cleanly on macOS and Linux.

## 3. Don't Reinvent the Wheel & Santa Lockdown Python Constraints
- **Monitor for Allowed Non-Binary Libraries Before Coding**: Whenever implementing non-trivial parsing, validation, schemas, HTTP/auth, crypto, date/timezone math, or CLI formatting, first verify whether **Python 3 stdlib** (`pathlib`, `json`, `ast`, `html.parser`, `sqlite3`, `fcntl`, `urllib.request`, `zoneinfo`, `unittest`) or a well-maintained **pure-Python non-binary library** (`py3-none-any.whl` declared in `pyproject.toml`) already solves the need. If an allowed non-binary library fits cleanly, adopt it rather than hand-rolling fragile custom implementations.
- **No Unapproved Standalone Binaries**: Avoid Python packages or local dev tools that download or execute standalone compiled `Mach-O` / `ELF` binaries at runtime (e.g., standalone `ruff`, `uv`, `pyright`, `playwright`, or `chromedriver` binary downloads) on Santa Lockdown workstations.
- **Santa-Safe `.pre-commit-config.yaml`**: Use `language: system` (with stdlib Python / POSIX scripts) or pure-Python `language: python` hooks locally. Avoid `language: rust`, `language: golang`, `language: node`, `language: conda`, or `language: docker` hooks that fetch unapproved compiled binaries locally.

## 4. Hermetic Unit Testing & Declarative Environments
- **Test Every Deterministic Change**: Every new parser, validator, guardrail, CLI subcommand, or state transition MUST include deterministic unit tests under `tests/` in the same commit.
- **Zero-Credential Offline Mocks Only**: Unit tests MUST run 100% offline with synthetic fixtures (`AcmeCorp`, `Globex`, `Initech`, `tempfile.TemporaryDirectory`) — zero live tokens, network calls, or production `$HOME` mutations. Strip inherited `GIT_*` hook variables in subprocess tests.
- **Fast Iteration -> Full Gate**: Run targeted `python3 -m unittest <module>` during development, then verify the full suite via `selective_test_runner.py` (or `unittest discover`) + `bats` before staging for review.
- **Declarative Runtimes & CI Pinning**: Pin runtimes in `.tool-versions` + `.tool-plugins` (`asdf`) and Python packages in `pyproject.toml`. Pin GitHub Actions runners to `ubuntu-24.04` (never `ubuntu-latest`), trigger on `pull_request` + `workflow_dispatch` without duplicate `push` triggers, and run [`project_audit.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py) + `pre-commit run --all-files` cleanly on every change.
