---
name: agy-project-standards
description: Reusable 5-language software engineering standards auditor and project scaffolder for Python 3, POSIX/Bash Shell, Vanilla JS/HTML/CSS, Antigravity plugins, Santa Lockdown binary/declarative tooling (.tool-versions + pyproject.toml), pinned GitHub Actions CI workflows, pre-commit hooks, and Zero-PII/secret AST checks. Use whenever bootstrapping a new repository, auditing an existing project's quality gates, or enforcing consistent best practices across projects.
---

# Project Engineering Standards & 5-Language Auditor (`agy-project-standards`)

Provides a deterministic, zero-LLM quality and security gate for **Python 3, POSIX/Bash/Zsh Shell, JavaScript, HTML5, CSS3**, and **Antigravity plugins** via [`project_audit.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py) — written in **100% pure Python 3 stdlib + `bash -n`** so it runs identically on Santa Lockdown workstations and Linux CI runners without requiring `node`, `npm`, `eslint`, `ruff`, or `shellcheck`.

## 1. Audited Pillars & Severity Calibration

| Category | Hard Errors (`error` — always fail) | Hygiene Warnings (`warning` — fail in `--strict`) |
| :--- | :--- | :--- |
| **Python AST & Safety** | `py:syntax-error`, `py:bare-except`, `py:unsafe-eval-exec` (`eval`/`exec`), `py:unsafe-yaml-load` (`yaml.load` without `SafeLoader`) | `py:mutable-default`, `py:subprocess-shell-true` (`shell=True`) |
| **Shell (`*.sh`, `*.bash`, `*.zsh`)** | `sh:syntax-error` (`bash -n` / `zsh -n`) | `sh:missing-strict-mode` (standalone executable scripts lacking `set -e`/`set -euo pipefail`, skipping sourced-only libraries), `sh:unquoted-rm-var` |
| **JavaScript (`*.js`, `*.mjs`)** | `js:unsafe-eval` (`eval()`, `new Function()`), `js:document-write`, `js:javascript-uri` (comments ignored cleanly) | `js:debugger-statement` |
| **HTML (`*.html`, `*.htm`)** | `html:javascript-uri`, `html:duplicate-id` (via stdlib `html.parser`) | `html:img-missing-alt`, `html:blank-target-noopener`, `html:inline-event-handler`, `html:button-missing-type`, `html:external-script-no-sri` |
| **CSS (`*.css`)** | `css:unbalanced-braces` (strings & comments ignored cleanly) | `css:important-overuse` (`> 10` `!important` per file) |
| **Zero-PII & Secrets (All 5 Languages)** | `sec:secret-pattern` (AWS/Google/GitHub/Slack/PEM keys), `sec:local-user-path` (hardcoded `/Users/<name>/` across `.py`, `.sh`, `.js`, `.html`, `.css`, `.md`, `.yml`, `.json`, `.toml`) | — |
| **Santa Lockdown Binary Safety** | `sec:santa-unapproved-pipe` (`curl\|sh`, `wget\|sh`, `curl\|python*`, `mise`, `uv tool install`, `cargo binstall`), `sec:santa-binary-download` (local script/Makefile `releases/download` + `chmod +x` or downloaded binary `chmod +x`) | `sec:santa-precommit-binary-lang` (`.pre-commit-config.yaml` hooks using `language: rust\|golang\|node\|conda\|docker`) |
| **Antigravity Plugin & CI Hygiene** | `agy:markdown-size-limit` (`>= 24,000` bytes), `agy:hooks-schema` / `agy:hooks-invalid-root-wrapper`, `agy:agent-frontmatter`, `agy:sidecar-invalid-executable` (scheduled `args[1]` or sidecar `command` is empty or a bare flag), `agy:schedule-missing-pregate` (`builtin: "schedule"` sidecar invoking `agentapi` without a deterministic 0-LLM pre-gate), `json:invalid` | `agy:missing-frontmatter`, `agy:agent-tools-missing`, `agy:suggested-prompts-limit` (`> 3`), `agy:skill-heavy-no-script` (`SKILL.md >= 12 KB` with `>= 4` fenced shell blocks lacking a `scripts/*.{py,sh}` helper or client/script delegation), `ci:unpinned-runner` (`ubuntu-latest`), `plugin:missing-standard-file`, optional `agy:copilot-instructions-bloat` (when `.github/copilot-instructions.md` exists and exceeds `4,000` bytes) |

## 2. Usage
```bash
# Audit any repository or plugin checkout (errors fail; warnings advisory)
python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py \
  --repo /path/to/repo

# Machine-readable JSON output + strict mode (warnings also fail)
python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py \
  --repo /path/to/repo --format json --strict
```

## 3. Baseline Repository Checklist
Every Antigravity-governed project should provide:
1. `.tool-versions` + `.tool-plugins` (pure-script `asdf` pins — Santa Lockdown safe) and `pyproject.toml` (pure-Python dependencies first)
2. `.env.example` documenting all required config keys with safe mock placeholders (live secrets stay in `0600` `~/.env`)
3. `.pre-commit-config.yaml` (Santa-safe `system`/`python` hooks) + `Makefile` (`test-local`, `lint`, `qa`)
4. `.github/workflows/{static-qa.yml,offline-unit-tests.yml}` pinned to `ubuntu-24.04` (`pull_request` + `workflow_dispatch`, no duplicate `push` triggers)
5. Hermetic zero-credential unit tests in `tests/` (`unittest` + `tests/bats/*.bats`)
6. For web UIs: Vanilla-First `HTML5`/`CSS3`/`ES2022+` static assets with zero unapproved binary bundlers (paired with `agy-web-ui-architecture`)
