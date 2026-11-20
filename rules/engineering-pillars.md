---
trigger: always_on
description: Core software engineering, security, Santa Lockdown, modularity, and SDLC workflow pillars governed by agy-dev-plugin.
---

# Engineering & SDLC Pillars (`agy-dev-plugin`)

All software development, scripting, automation, and repository configuration changes MUST follow these foundational engineering standards:

## 1. Foundational Architecture & Portability
1. **Unix Portability (`macOS` & `Linux`)**: Prefer Python standard library (`pathlib`, `shutil`, `sqlite3`, `fcntl`, `urllib.request`) and POSIX `/usr/bin/*` utilities. Gate any optional macOS integration (`open`, `pbcopy`, `UseKeychain`) behind runtime OS checks (`platform.system() == "Darwin"`).
2. **Standard Languages Only (`Python 3` + `POSIX / Bash`)**: Implement application logic, APIs, CLI tools, static auditors, and tests in Python 3 (`pyproject.toml`), and installers/bootstrappers in POSIX Shell / Bash validated with `bash -n`, `zsh -n`, and `bats`.
3. **Santa Lockdown Compliance**: Never download or execute unapproved standalone compiled binaries (`curl | sh`, `wget | sh`, `mise`, ad-hoc release binaries). Declare runtimes declaratively with `asdf` (`.tool-versions`, `.tool-plugins`) and Python dependencies in `pyproject.toml`.
4. **Antigravity-Native Customizations First**: Reuse official Antigravity plugins, MCP servers, skills, rules, hooks (`agy-dev-sdlc-guardrail` flat schema), and dedicated agents (`agy-dev-orchestrator`, `agy-dev-planner`, `agy-dev-executor`, `agy-code-developer`, `agy-code-pr-reviewer`) before adding custom client wrappers. Keep plugin definitions, agent personas, rule files, and `SKILL.md` files strictly under `24,000` bytes.
5. **Modular Functional & Data-Driven Python**: Favor small, single-purpose functions and typed dataclasses / Pydantic schemas over ceremonial wrapper hierarchies. Never duplicate logic; extract shared helpers cleanly.

## 2. Security, Privacy & Manual Gates
6. **Zero PII & Zero Secrets in Tracked Code**: Never commit customer names, domains, personal emails, tokens, credentials, or local `/Users/<user>/` paths into any tracked repository file. Store sensitive runtime configuration exclusively in `0600` `~/.env` (with mock placeholders mirrored in `.env.example`) and untracked state under `~/.local/share/`.
7. **Deterministic Verification Before Reasoning**: Codify repeatable checks into fast CLI validators, pre-commit hooks, static AST audits (`project_audit.py` / `qa_static_audit.py`), and `PreToolUse`/`SessionStart` hooks.
8. **Protected `main` Branch Workflow**: Never push directly to `main`. Route every tracked code/config change through `git-commit-configuration` and `agy-code-remediation-pr` (`fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>` under per-repository `dev_pr_locks`) with independent `agy-code-pr-reviewer` (`Model="pro"`) evaluation and human PR approval.

## 3. Domain Skill Map
| Need / Workflow | Skill in `agy-dev-plugin` |
| :--- | :--- |
| Audit or scaffold project quality gates (`pyproject.toml`, pre-commit, CI, Zero-PII) | [`agy-project-standards`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/SKILL.md) |
| Semantic commits, secret scanning, `$HOME` plugin sync (`deploy-home`) | [`git-commit-configuration`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/git-commit-configuration/SKILL.md) |
| Timestamped branches, per-repo `dev_pr_locks`, Maker-Checker review (`pr_remediator.py`) | [`agy-code-remediation-pr`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/SKILL.md) |
| Multi-repo queue (`dev_queue.json`, `queue-next --unlocked`) & cleanup | [`agy-dev-queue-orchestrator`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-dev-queue-orchestrator/SKILL.md) |
