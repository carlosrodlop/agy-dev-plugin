---
trigger: glob
globs: "**/*.py, **/*.sh, **/SKILL.md, **/plugin.json, **/hooks.json, **/README.md, **/.agents/**"
description: Mandatory Native Git timestamped branch workflow, dev_pr_lock concurrency gate, pre-commit output, and manual PR review rules.
---

# Native Git & PR Workflow (`git-and-pr-workflow.md`)

Always follow [`git-commit-configuration`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/git-commit-configuration/SKILL.md) and [`agy-code-remediation-pr`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/SKILL.md) for any change to tracked repositories.

## 1. Native Git Only (VCS-Agnostic)
- Use native `/usr/bin/git` and `/usr/bin/ssh` via `git_client.py` and `pr_remediator.py`.
- Proprietary forge CLIs (`gh`, `glab`) and REST API tokens are forbidden and blocked by `PreToolUse` (`dev_common.py`).
- Direct pushes to `main` or `master` (`git push origin main`) are blocked across all agents and hooks.

## 2. Timestamped Branch & Lock Protocol
1. Check `dev_pr_lock` (`pr_remediator.py lock-status --sync-git`). If an unrelated conversation holds the lock, pause or enqueue via `pr_remediator.py queue-add` instead of branching concurrently.
2. Start or reuse a worktree branch (`fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>`) with `start-remediation --worktree` (`--reuse-active` for follow-ups from the same thread).
3. Run `pre-commit run --all-files` AND `pr_remediator.py test-local --workdir <WORKTREE_PATH>` and paste the visible `pre-commit` summary output in your reply.
4. Push with `pr_remediator.py push-branch`, checkout local `main` again, launch `watch-pr-merge --daemon`, and run independent `agy-code-pr-reviewer` (`Model="pro"`) -> `record-review --send-email`.
5. Present the clickable **Compare & Create Manual Pull Request** link (`manual_pr_url`) so the user merges or rejects manually.

## 3. Plugin Deployment (`$HOME` Mirror Parity)
- Whenever a plugin repository (`agy-dev-plugin` or companion plugins) is updated and ready, deploy cleanly to `$HOME/.gemini/config/plugins/<plugin>/` via `./install.sh` or `git_client.py deploy-home`.
