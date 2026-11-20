---
name: agy-project-standards
description: Reusable software engineering standards auditor and project scaffolder for Python 3, POSIX Shell, Antigravity plugins, Santa Lockdown declarative tooling (.tool-versions + pyproject.toml), pinned GitHub Actions CI workflows, pre-commit hooks, and Zero-PII/secret AST checks. Use whenever bootstrapping a new repository, auditing an existing project's quality gates, or enforcing consistent best practices across projects.
---

# Project Engineering Standards & Auditor (`agy-project-standards`)

Provides a deterministic, reusable quality and security gate for any Python/Shell repository or Antigravity plugin via [`project_audit.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py).

## 1. Audited Pillars
| Category | Deterministic Verification |
| :--- | :--- |
| **Python AST & Safety** | Valid syntax across `*.py`, zero bare `except:`, zero mutable default args, zero unsafe `shell=True` where avoidable |
| **Zero-PII & Secrets** | Zero leaked API keys/tokens, PEM keys, or unmasked machine-local `/Users/<name>/` paths; `.gitignore` protects `.env` & local caches |
| **Antigravity Plugin Layout** | Valid `plugin.json` (`suggestedPrompts <= 3`), flat/named `hooks.json` schema, and valid YAML frontmatter (`tools:` on `agents/*.md`) strictly `< 24,000` bytes across `rules/*.md`, `skills/*/SKILL.md`, and `agents/*.md` |
| **Santa & Declarative Tooling** | Zero `curl\|sh`, `wget\|sh`, or `mise` pipes; checks `.tool-versions`, `pyproject.toml`, `.pre-commit-config.yaml`, `Makefile` |
| **POSIX Shell & CI Hygiene** | `bash -n` syntax validation across `*.sh` + BATS suites (`tests/bats/*.bats`); GitHub Actions pin `ubuntu-24.04` (no unpinned `ubuntu-latest`) |
| **Copilot Review Hygiene (Optional)** | Non-blocking warning `agy:copilot-instructions-bloat` when optional `.github/copilot-instructions.md` exists and exceeds `4,000` bytes (never required when absent) |

## 2. Usage
```bash
# Audit any repository or plugin checkout
python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py \
  --repo /path/to/repo

# Machine-readable JSON output for CI or pre-commit wrappers
python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py \
  --repo /path/to/repo --format json --strict
```

## 3. Baseline Repository Checklist
Every Antigravity-governed project should provide:
1. `.tool-versions` + `.tool-plugins` (`asdf` Python pin) and `pyproject.toml` (`pytest`, `ruff` / `mypy` / deps)
2. `.env.example` documenting all required config keys with safe mock placeholders (live secrets stay in `0600` `~/.env`)
3. `.pre-commit-config.yaml` + `Makefile` (`test-local`, `lint`, `qa`)
4. `.github/workflows/{static-qa.yml,offline-unit-tests.yml}` pinned to `ubuntu-24.04`
5. Hermetic zero-credential unit tests in `tests/`
