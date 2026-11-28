---
name: git-commit-configuration
description: Enforces semantic Git commit messages, zero-secret verification, mandatory pre-commit hook validation with visible output in chat, and safe plugin deployment (git_client.py deploy-home / ./install.sh) when committing or pushing code and workstation configurations to a remote server. Use whenever creating a Git commit, pushing to a repository, or deploying plugin configuration to $HOME.
---

# Git Commit & Configuration Synchronization Skill

Enforces clean, GitHub-First Native Git hygiene (Santa Lockdown compliant — zero `gh` binary), Zero-PII/secret safety, mandatory `pre-commit` verification, and deterministic `$HOME` plugin mirror parity across repositories managed with [`agy-dev-plugin`]($HOME/.gemini/config/plugins/agy-dev-plugin/plugin.json).

## 1. Mandatory Pre-Commit & Protected-Branch Protocol
1. **Never Push Directly to `main` or `master`**:
   - Tracked changes MUST be committed on a timestamped branch `fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>` created through [`pr_remediator.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py) (holding `dev_pr_lock`).
   - Only pure native `/usr/bin/git` and `/usr/bin/ssh` via [`git_client.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/clients/git_client.py) are allowed (the `gh` / `glab` compiled CLI binaries are blocked under Santa Lockdown; use Python stdlib `urllib.request` only if GitHub REST API access is ever needed).
2. **Pre-Commit Hook Gate (`--all-files`)**:
   - Before every push, execute `pre-commit run --all-files` (inside the active worktree or repository) and include the visible `pre-commit` summary output directly in your response.
   - Verify `.gitignore` covers `.env`, `.venv/`, `__pycache__/`, `*.pyc`, `*.db`, and local secret caches before staging.
3. **Version Manifest Synchronization**:
   - Stamp `plugin.json` and `pyproject.toml` versions deterministically via `git_client.py` (`stamp_repo_version_manifests`), never leaving dirty tracked version diffs behind on `main`.
4. **Safe `$HOME` Plugin Mirror Deployment**:
   - Deploy approved plugin bundles into `$HOME/.gemini/config/plugins/<plugin>/` via `./install.sh` or:
     ```bash
     python3 ~/.gemini/config/plugins/agy-dev-plugin/clients/git_client.py deploy-home --repo-dir <REPO_DIR>
     ```
   - Never copy `~/.env`, `.venv`, `.git`, or `__pycache__` into `$HOME/.gemini/config/plugins/`.

## 2. Semantic Commit Message Standard
Follow Conventional Commits (`<type>(<scope>): <imperative summary>`):
- `feat(...)`: new feature, skill, hook, or capability
- `fix(...)`: bug fix or regression remediation
- `refactor(...)`: structural/modular code improvement without behavior change
- `test(...)`: hermetic unit tests or static QA audit enhancements
- `docs(...)` / `chore(...)`: documentation, CI workflow, or tooling maintenance
- Embed `Tests: ...` and key rationale bullets in commit bodies so GitHub Compare/PR pages auto-populate cleanly.
