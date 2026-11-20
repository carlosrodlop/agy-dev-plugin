# `agy-dev-plugin` — Antigravity Software Engineering, Native Git & Project Standards Plugin

`agy-dev-plugin` is a standalone, modular Antigravity plugin dedicated to **software engineering best practices, VCS-agnostic Native Git branching workflows, deterministic lifecycle guardrails, and Zero-PII/Santa Lockdown project auditing**.

By decoupling generic SDLC automation from domain-specific plugins (such as [`agy-tam-plugin`](https://github.com/carlosrodlop/agy-tam-plugin) for Wiz TAM workflows), `agy-dev-plugin` governs any Python / Unix repository consistently without polluting domain contexts.

---

## Core Capabilities

### 1. Deterministic Guardrails (`clients/dev_common.py` + `hooks.json`)
- **`PreToolUse` Safety Gate**:
  - `pretool:direct-main-push`: Blocks direct `git push` commands targeting `main` or `master`.
  - `pretool:proprietary-vcs-cli`: Blocks proprietary forge wrappers (`gh`, `glab`), enforcing native `/usr/bin/git` and `/usr/bin/ssh`.
  - `pretool:santa-lockdown-binary`: Blocks unapproved remote script pipes (`curl | sh`, `wget | sh`) and `mise`.
  - `pretool:dotenv-write-protection`: Blocks direct agent edits to live `~/.env` credentials.
- **`SessionStart` Lock Context Injector (`session:dev-pr-lock-context`)**:
  - Surfaces active cross-conversation `dev_pr_lock` state so parallel agents coordinate cleanly.

### 2. Skills (`skills/`)
| Skill | Purpose |
| :--- | :--- |
| **`agy-project-standards`** | Audits any repo or plugin for Python AST safety, Zero-PII, Santa compliance, `.tool-versions`, CI runner pinning, shell syntax, and `<24 KB` rule/skill limits (`project_audit.py`). |
| **`git-commit-configuration`** | Enforces Conventional Commits, `pre-commit` verification, Zero-PII scans, version stamping, and `$HOME` plugin deployment (`git_client.py deploy-home`). |
| **`agy-code-remediation-pr`** | Creates isolated timestamped branches (`fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>`) behind `dev_pr_lock`, runs `test-local`, pushes to `origin/<branch>`, invokes `agy-code-pr-reviewer` (`Model="pro"`), and spawns `watch-pr-merge`. |
| **`agy-dev-queue-orchestrator`** | Serializes multi-repo engineering tasks in `dev_queue.json` and safely cleans up merged conversations. |

### 3. Dedicated Subagents (`agents/`)
- **`agy-code-developer`**: Isolated branch implementer consuming pre-anchored context and running targeted tests before pushing.
- **`agy-code-pr-reviewer`**: Independent Maker-Checker evaluator (`Model="pro"`) scoring diff accuracy, test coverage, Zero-PII compliance, and style.

### 4. Rules (`rules/`)
- `engineering-pillars.md` (`always_on`)
- `python-and-testing.md` (`glob`)
- `git-and-pr-workflow.md` (`glob`)

---

## Quickstart

```bash
# Deploy plugin mirror to $HOME/.gemini/config/plugins/agy-dev-plugin/
./install.sh

# Audit any repository or plugin checkout
python3 ./skills/agy-project-standards/scripts/project_audit.py --repo . --strict

# Run offline QA & unit test suite
make test
```
