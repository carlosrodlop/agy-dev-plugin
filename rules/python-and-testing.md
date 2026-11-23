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

## 5. Cross-OS Portability (`macOS` vs `Linux` CI) & Local CI Parity
- **SQLite WAL Read-Only URI Modes (`mode=ro` vs `immutable=1`)**: Prefer `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)` when reading live SQLite WAL databases in writable directories so `-wal`/`-shm` frames are observed safely without writing. Reserve `immutable=1` strictly as a fallback when the parent directory or filesystem is read-only (`sqlite3.OperationalError`), because `immutable=1` bypasses locking/WAL visibility and can fail or read torn state if a concurrent writer updates the database.
- **Filesystem Case-Sensitivity (`APFS` vs `ext4`)**: Default macOS `APFS` is case-insensitive (`Foo.py` matches `foo.py`) whereas Ubuntu `ext4` CI is strictly case-sensitive (`ModuleNotFoundError` / `FileNotFoundError`). Keep exact casing across all `import` statements, `Path` lookups, test fixtures, and `git mv` renames, and never rely on case-only filename distinctions.
- **Canonical Symlinked Temporary Paths (`/private/tmp` vs `/tmp`)**: On macOS, `/tmp` and `/var` symlink to `/private/tmp` and `/private/var`, whereas on Linux `/tmp` is a physical directory. Always canonicalize paths with `Path.resolve()` (or `os.path.realpath()`) before comparing paths, computing `Path.relative_to(...)`, or matching `tempfile.TemporaryDirectory()` paths against `git rev-parse --show-toplevel` outputs.
- **Local CI Parity Gate (`--ci-parity` / `make ci-local`)**: Ensure pure-Python AST `F821` undefined-name checks (`project_audit.py`), full `qa_static_audit.py`, full `unittest` + `bats`, and `python3 -m coverage` (`--fail-under` matching `.github/workflows/*.yml`) pass locally via `make ci-local` or `pr_remediator.py test-local --ci-parity` (auto-enabled when `--all` is passed or repository workflows gate hosted runners behind `ENABLE_GHA_HOSTED_RUNNERS`).
