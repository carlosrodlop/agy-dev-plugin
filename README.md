# `agy-dev-plugin` — Antigravity Software Engineering, Native Git & Project Standards Plugin

`agy-dev-plugin` is a standalone, modular Antigravity plugin dedicated to **software engineering best practices, VCS-agnostic Native Git branching workflows, deterministic lifecycle guardrails, and Zero-PII/Santa Lockdown project auditing**.

By decoupling generic SDLC automation from domain-specific plugins (such as [`agy-tam-plugin`](https://github.com/carlosrodlop/agy-tam-plugin) for Wiz TAM workflows), `agy-dev-plugin` governs any Python / Unix repository consistently without polluting domain contexts.

---

## Core Capabilities

### 1. Deterministic Guardrails (`clients/dev_common.py` + `hooks.json`)
- **`PreToolUse` Safety Gate (`agy-dev-sdlc-guardrail`)**:
  - `pretool:direct-main-push`: Blocks direct `git push` commands targeting `main` or `master`.
  - `pretool:proprietary-vcs-cli`: Blocks proprietary forge wrappers (`gh`, `glab`), enforcing native `/usr/bin/git` and `/usr/bin/ssh`.
  - `pretool:santa-lockdown-binary`: Blocks unapproved remote script pipes (`curl | sh`, `wget | sh`, `curl | python*`), chained binary downloads (`releases/download` + `chmod +x`), and unapproved binary managers (`mise`, `uv tool/python install`, `cargo binstall`).
  - `pretool:dotenv-write-protection`: Blocks direct agent edits to live `~/.env` credentials.
  - `pretool:unvalidated-branch-push`: Blocks raw `git push` to locked branches before `agy-code-pr-reviewer` passes (`PASS > 90/100`).
- **`SessionStart` Lock Context Injector (`session:dev-pr-lock-context`)**:
  - Surfaces per-repository `dev_pr_locks` state for the active workspace repo alongside sibling repo locks so parallel multi-repo agents coordinate without cross-repo collisions.

### 2. Skills (`skills/`)
| Skill | Purpose |
| :--- | :--- |
| **`agy-project-standards`** | Pure-Python stdlib 5-language auditor (`project_audit.py`) covering Python 3, POSIX/Bash/Zsh Shell, JavaScript, HTML5, CSS3, Zero-PII/secrets, Santa Lockdown binary/pre-commit safety, `.tool-versions`, `hooks.json` / `agents/*.md` schemas, pinned CI workflows, `<24 KB` rule/skill limits, and optional `<=4 KB` Copilot instructions hygiene. |
| **`agy-web-ui-architecture`** | Actionable workflow for designing, modularizing, and auditing Vanilla-First Zero-Build web UIs (`ES2022+` JS, semantic `HTML5`, `CSS3` design tokens) under Santa Lockdown, bridging static `project_audit.py` checks cleanly to `chrome-devtools-plugin` for live browser/a11y/LCP verification. |
| **`agy-code-design-refactor`** | Structured architecture review, structural smell triage, and characterization-first incremental refactoring workflow across Python 3, POSIX/Bash Shell, and Vanilla JS/HTML/CSS. |
| **`git-commit-configuration`** | Enforces Conventional Commits, `pre-commit` verification, Zero-PII scans, version stamping, and `$HOME` plugin deployment (`git_client.py deploy-home`). |
| **`agy-code-remediation-pr`** | Creates isolated worktree branches (`fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>`) behind per-repository `dev_pr_locks` (`--workdir`, `--repo`, `--all`), runs `test-local`, pushes to `origin/<branch>`, invokes `agy-code-pr-reviewer` (`Model="pro"`), and spawns `watch-pr-merge`. |
| **`agy-dev-queue-orchestrator`** | Coordinates multi-repo engineering tasks in `dev_queue.json`, picks unblocked repos (`queue-next --unlocked`), tracks progressive `merged_repos`, provides the zero-hardcode `agy-queue` TUI & live `dev_queue_board.md` HITL artifact, supports in-app `/hitl-review`, and safely cleans up merged conversations. |

### 3. Dedicated Agents (`agents/`)
- **`agy-dev-orchestrator`** (`mainAgent` / `subagent`): General SDLC coordinator routing multi-repo work across `agy-dev-planner`, `agy-dev-executor`, `agy-code-developer`, and `agy-code-pr-reviewer`.
- **`agy-dev-planner`** (`mainAgent` / `subagent`, read-only): Leads Phase 0 interactive co-design (`co-design-and-discovery.md`), scopes tasks, anchors target files/tests, identifies cross-repo dependencies, and produces prioritized Design Contracts + plans in `dev_queue.json`.
- **`agy-dev-executor`** (`mainAgent` / `subagent`): Drives per-repo `dev_pr_locks` worktrees, runs `pre-commit` / `test-local` / `bats`, pushes feature branches, and enforces Maker-Checker review.
- **`agy-code-developer`** (`subagent`): Isolated branch implementer consuming pre-anchored context and running targeted tests before pushing.
- **`agy-code-pr-reviewer`** (`subagent`, `Model="pro"`): Independent Maker-Checker evaluator scoring diff accuracy, test coverage, Zero-PII/Santa compliance, and style.

### 4. Rules (`rules/`)
- `engineering-pillars.md` (`always_on`) — Foundational SDLC pillars, 5 primary languages (`Python 3`, `Shell`, `Vanilla JS/HTML/CSS`), and Santa Lockdown binary rules
- `co-design-and-discovery.md` (`model_decision`) — Default Phase 0 adaptive Grill-Me protocol, silent codebase-first discovery, demanding partner pushback, hybrid cadence, and Design Contract exit gate
- `deterministic-first-engineering.md` (`model_decision`) — 4-Tier Determinism Ladder (`0-LLM` default), Deterministic Sandwich pattern, pre-gated scheduled sidecars with zero double-`detect` JSON handoff, skill scriptability, and Layer-1 fail-fast gates
- `python-and-testing.md` (`glob`: `*.py`, `pyproject.toml`, `.pre-commit-config.yaml`, CI workflows) — Python 3 typing, AST safety, Santa pure-Python constraints, and hermetic offline testing
- `shell-standards.md` (`glob`: `*.sh`, `*.bash`, `*.zsh`, `*.bats`, `Makefile`) — POSIX/Bash/Zsh portability, sourced-safe strict mode, quoting, Santa binary safety, and BATS
- `web-frontend-standards.md` (`glob`: `*.js`, `*.mjs`, `*.html`, `*.css`) — Vanilla-First Zero-Build JS/HTML5/CSS3, XSS/CSP defense, WCAG accessibility, design tokens, and Santa Lockdown toolchain rules
- `git-and-pr-workflow.md` (`glob`) — Native Git timestamped branches, per-repo `dev_pr_locks`, and hard `PASS > 90/100` PR readiness gate

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

### 5. Dev Queue TUI (`agy-queue`), Sidecar Panel, `agy-doctor` & Automatic Quota Fallback
- **`agy-queue` (`skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py` + `shell/agy_dev_queue.sh`) & Sidecar Panel (`sidecars/dev-queue-panel/`)**: Zero-hardcode single-table terminal TUI and Antigravity 2.0 AUX_PANE/Sidebar Web UI panel (distinguishing `Blocked` before PR vs `PR Ready` for GitHub merge with tooltips, Pause/Continue Graceful Drain, and 1-click conversation/artifact navigation).
- **`/hitl-review` workflow**: Inside Antigravity, reads `hitl_blocked` queue item transcripts and proposals, prompts structured decisions via `ask_question`, and auto-promotes approved items `hitl_blocked -> pending` (`skills/agy-dev-queue-orchestrator/SKILL.md`).
- **Token-Saving Routing & Automatic Quota Fallback (`clients/model_quota.py` + `skills/agy-dev-queue-orchestrator/references/model-quota-fallback.md`)**: Defaults Round 1 development to `flash` (`PR_DEV_DEFAULT_MODEL="flash"`) with automatic Round 2+ `pro` escalation, probes live `swarm(action="models", all=true)` quotas with preventive low-quota Eco (`<= 25%` via `AGY_DEV_LOW_QUOTA_THRESHOLD_PCT`) + `429` cooldown persistence (`0600` `model_quota_cooldowns.json`), rotates orchestrators every `~150` steps (`AGY_DEV_MAX_ORCHESTRATOR_STEPS`), and drives multi-slot `agy-doctor` auto-heal (`AWAITING_AGENT_REVIEW`, `Slot #2`, 0-LLM `stale_follower_needs_rebase`, quota-aware `--model` wakes, and all-exhausted wake suppression).

### 6. GitHub Actions CI (`ubuntu-24.04`)
Because `carlosrodlop/agy-dev-plugin` is a public repository (unlimited GitHub Actions minutes) with protected `main` requiring Pull Requests, all 5 workflows enforce explicit least-privilege `permissions:`, Node 24 compatible actions (`actions/checkout@v6`, `actions/setup-python@v6`, `actions/cache@v5`, SHA-pinned `actions/github-script` v8), and trigger exclusively on `pull_request` (`main`) plus manual `workflow_dispatch` (zero duplicate `push` triggers):
- `.github/workflows/static-qa.yml`: Installs `pyyaml` + `ruff`, runs `ruff check --select E9,F63,F7,F82 clients skills sidecars tests`, and executes `qa_static_audit.py --strict` (which wraps `project_audit.py --strict` + independence checks).
- `.github/workflows/offline-unit-tests.yml`: Runs across a Python `3.11` and `3.13` matrix with `AGY_OFFLINE_ONLY=1` and `PRE_COMMIT_HOOK=1`, smoke-tests editable CLI entrypoints (`pip install --no-deps -e .` -> `agy-queue --help`, `agy-doctor --help`), and executes `unittest` discovery, `bats tests/bats/*.bats`, and `selective_test_runner.py --plan-only`.
- `.github/workflows/pre-commit.yml`: Caches `~/.cache/pre-commit` and executes `pre-commit run --all-files --show-diff-on-failure` with `SKIP=qa-static-audit,unit-tests-fast,bats-suite` so formatting, YAML/JSON/TOML, shebang, and secret hooks run without duplicating dedicated QA/test workflows.
- `.github/workflows/coverage-report.yml`: Runs hermetic `pytest` across `--cov=clients --cov=skills --cov=sidecars --cov=tests/scripts` enforcing `--cov-fail-under=75` and publishing a Markdown coverage table to `GITHUB_STEP_SUMMARY`.
- `.github/workflows/sync-pr-verification.yml`: Idempotently syncs SHA-bound `## 🧪 Automated Verification Checks` and `## 🤖 Independent PR Review` sections from `HEAD` commit messages into the PR description body (`contents: read`, `pull-requests: write`).

### 7. Optional GitHub Copilot Code Review (Credit-Safe Ruleset)
To enable optional GitHub Copilot Code Review on Pull Requests targeting `main` without burning AI Credits on intermediate commits or drafts, configure the repository **GitHub Ruleset** for `main` as follows:
- **Automatically request Copilot code review**: **ON**
- **Review new pushes**: **OFF** (prevents redundant re-reviews on incremental fixup pushes)
- **Review draft pull requests**: **OFF** (reviews only ready-for-review PRs)

Repository review rules live in [`.github/copilot-instructions.md`](.github/copilot-instructions.md) and are kept strictly `<= 4,000` bytes (checked via the optional non-blocking `agy:copilot-instructions-bloat` warning in `project_audit.py`) to keep prompts concise and control AI Credit consumption without relaxing native `/usr/bin/git` + `/usr/bin/ssh` (`PROPRIETARY_VCS_CLI_RE`) guardrails.
