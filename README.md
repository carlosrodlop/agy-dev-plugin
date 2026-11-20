# `agy-dev-plugin` — Antigravity Software Engineering, Native Git & Project Standards Plugin

`agy-dev-plugin` is a standalone, modular Antigravity plugin dedicated to **software engineering best practices, VCS-agnostic Native Git branching workflows, deterministic lifecycle guardrails, and Zero-PII/Santa Lockdown project auditing**.

By decoupling generic SDLC automation from domain-specific plugins (such as [`agy-tam-plugin`](https://github.com/carlosrodlop/agy-tam-plugin) for Wiz TAM workflows), `agy-dev-plugin` governs any Python / Unix repository consistently without polluting domain contexts.

---

## Core Capabilities

### 1. Deterministic Guardrails (`clients/dev_common.py` + `hooks.json`)
- **`PreToolUse` Safety Gate (`agy-dev-sdlc-guardrail`)**:
  - `pretool:direct-main-push`: Blocks direct `git push` commands targeting `main` or `master`.
  - `pretool:proprietary-vcs-cli`: Blocks proprietary forge wrappers (`gh`, `glab`), enforcing native `/usr/bin/git` and `/usr/bin/ssh`.
  - `pretool:santa-lockdown-binary`: Blocks unapproved remote script pipes (`curl | sh`, `wget | sh`) and `mise`.
  - `pretool:dotenv-write-protection`: Blocks direct agent edits to live `~/.env` credentials.
- **`SessionStart` Lock Context Injector (`session:dev-pr-lock-context`)**:
  - Surfaces per-repository `dev_pr_locks` state for the active workspace repo alongside sibling repo locks so parallel multi-repo agents coordinate without cross-repo collisions.

### 2. Skills (`skills/`)
| Skill | Purpose |
| :--- | :--- |
| **`agy-project-standards`** | Audits any repo or plugin for Python AST safety, Zero-PII, Santa compliance, `.tool-versions`, `hooks.json` / `agents/*.md` schemas, pinned CI workflows, shell/BATS hygiene, and `<24 KB` rule/skill limits (`project_audit.py`). |
| **`git-commit-configuration`** | Enforces Conventional Commits, `pre-commit` verification, Zero-PII scans, version stamping, and `$HOME` plugin deployment (`git_client.py deploy-home`). |
| **`agy-code-remediation-pr`** | Creates isolated worktree branches (`fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>`) behind per-repository `dev_pr_locks` (`--workdir`, `--repo`, `--all`), runs `test-local`, pushes to `origin/<branch>`, invokes `agy-code-pr-reviewer` (`Model="pro"`), and spawns `watch-pr-merge`. |
| **`agy-dev-queue-orchestrator`** | Coordinates multi-repo engineering tasks in `dev_queue.json`, picks unblocked repos (`queue-next --unlocked`), tracks progressive `merged_repos`, provides the zero-hardcode `agy-queue` TUI & live `dev_queue_board.md` HITL artifact, supports in-app `/hitl-review`, and safely cleans up merged conversations. |

### 3. Dedicated Agents (`agents/`)
- **`agy-dev-orchestrator`** (`mainAgent` / `subagent`): General SDLC coordinator routing multi-repo work across `agy-dev-planner`, `agy-dev-executor`, `agy-code-developer`, and `agy-code-pr-reviewer`.
- **`agy-dev-planner`** (`mainAgent` / `subagent`, read-only): Leads Phase 0 interactive co-design (`co-design-and-discovery.md`), scopes tasks, anchors target files/tests, identifies cross-repo dependencies, and produces prioritized Design Contracts + plans in `dev_queue.json`.
- **`agy-dev-executor`** (`mainAgent` / `subagent`): Drives per-repo `dev_pr_locks` worktrees, runs `pre-commit` / `test-local` / `bats`, pushes feature branches, and enforces Maker-Checker review.
- **`agy-code-developer`** (`subagent`): Isolated branch implementer consuming pre-anchored context and running targeted tests before pushing.
- **`agy-code-pr-reviewer`** (`subagent`, `Model="pro"`): Independent Maker-Checker evaluator scoring diff accuracy, test coverage, Zero-PII compliance, and style.

### 4. Rules (`rules/`)
- `engineering-pillars.md` (`always_on`) — Foundational SDLC pillars (`Phase 0 Co-Design -> Planning -> Queue -> Execution -> Maker-Checker Review -> Manual PR`)
- `co-design-and-discovery.md` (`model_decision`) — Default Phase 0 adaptive Grill-Me protocol, silent codebase-first discovery, demanding partner pushback, hybrid cadence, and Design Contract exit gate
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

# Monitor the multi-repo dev queue & locks interactively (or source shell/agy_dev_queue.sh)
./shell/agy_dev_queue.sh --watch
# Optional: sync a live Antigravity HITL Markdown board artifact with 1-click conversation:// & PR links
./shell/agy_dev_queue.sh --write-board "$HOME/.local/share/agy-dev-plugin/dev_queue_board.md"
```

### 5. Dev Queue TUI (`agy-queue`) & In-App `/hitl-review`
- **`agy-queue` (`skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py` + `shell/agy_dev_queue.sh`)**: Zero-hardcode single-table terminal TUI discovering managed repos dynamically via `dev_common.resolve_state_dir()`, `dev_queue.json`, `dev_pr_locks`, and `git_client.py`. Supports multi-repo PR-ready bars (`PR 1/2 listo` / `PR listo`), `★` repo attribution + unlock footer, richest PR URL ranking with full `?title=...&body=...` preservation, flicker-free `--watch` hotkeys (`[o]`, `[1-9]`, `[h+#]`, `[b]`, `[s]`, `[a]`, `[q]`), and live `dev_queue_board.md` artifact syncing.
- **`/hitl-review` workflow**: Inside Antigravity, reads `hitl_blocked` queue item transcripts and proposals, prompts structured decisions via `ask_question`, and auto-promotes approved items `hitl_blocked -> pending` (`skills/agy-dev-queue-orchestrator/SKILL.md`).

### 6. GitHub Actions CI (`ubuntu-24.04`)
Because `carlosrodlop/agy-dev-plugin` is a public repository (unlimited GitHub Actions minutes) with protected `main` requiring Pull Requests, all 4 workflows trigger automatically on `pull_request` (`main`) — plus manual `workflow_dispatch`, with no duplicate `push` triggers:
- `.github/workflows/static-qa.yml`: Runs `qa_static_audit.py --strict` and `project_audit.py --repo . --strict`.
- `.github/workflows/offline-unit-tests.yml`: Runs `unittest` discovery, `bats tests/bats/*.bats`, and `selective_test_runner.py --plan-only`.
- `.github/workflows/pre-commit.yml`: Installs `bats` and executes the full `.pre-commit-config.yaml` hook suite.
- `.github/workflows/coverage-report.yml`: Runs `pytest --cov=clients --cov=skills` coverage reporting.
