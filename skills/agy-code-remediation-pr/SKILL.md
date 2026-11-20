---
name: agy-code-remediation-pr
description: VCS-Interoperable Native Git Timestamped Branch Remediation Pipeline with Per-Repository Development Locks (dev_pr_locks) and Multi-Agent Orchestration (agy-dev-executor / agy-code-developer -> agy-code-pr-reviewer, Model='pro') for interactive user-prompted code/configuration changes or escalated incident audits. Uses pure native git (git_client.py; GitHub, GitLab, Bitbucket, Gitea interoperable): creates repo-isolated worktrees on fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>, pushes exclusively to remote feature branches, scopes locks per repository so unrelated repos progress in parallel, and produces clickable Compare/Manual PR URLs.
---

# Native Git Timestamped Branch Remediation Pipeline (`agy-code-remediation-pr`)

All code, script, skill, rule, agent, hook, and configuration changes MUST flow through [`pr_remediator.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py) using pure native `git` (never `gh` / `glab` REST CLIs, never direct pushes to `main`).

## End-to-End Execution Protocol
1. **Check Per-Repository Lock Status & Sync Local `main`**:
   ```bash
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py lock-status --all --sync-git
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py lock-status --workdir <TARGET_REPO_DIR> --sync-git
   ```
   - Locks are isolated per repository slug (`owner/repo`). Only pause or enqueue via `queue-add` if the **target repository's** lock is held by an unrelated conversation; distinct repositories can run in parallel.
2. **Acquire Per-Repo Lock & Isolated Timestamped Worktree Branch**:
   ```bash
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py start-remediation \
     --id <CONVERSATION_ID> --action-id <SCOPE> --workdir <TARGET_REPO_DIR> \
     --rca-summary "<Why>" --proposed-fix "<What>" --worktree
   ```
   - Use `--reuse-active` when iterating on follow-up commits from the same active conversation (`--workdir` or `--repo <owner/repo>` selects the target repository cleanly across `agy-dev-plugin`, `agy-tam-plugin`, `g-control-tower`, etc.).
3. **Implement, Test & Gate in Isolated Worktree (`<WORKTREE_PATH>`)**:
   - Delegate or execute via `agy-dev-executor` / `agy-code-developer` inside `<WORKTREE_PATH>` (never leave dirty feature edits on `main`).
   - Iterate quickly on targeted `python3 -m unittest <module>`, then run the full local gate:
     ```bash
     cd <WORKTREE_PATH> && pre-commit run --all-files
     python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py test-local --workdir <WORKTREE_PATH>
     ```
4. **Commit, Push Branch & Verify Clean `main`**:
   ```bash
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py push-branch \
     --workdir <WORKTREE_PATH> --title "fix(<scope>): <summary>" --executive-summary "..."
   ```
5. **Independent Maker-Checker Review (`agy-code-pr-reviewer`, `Model="pro"`)**:
   - Invoke `agy-code-pr-reviewer` (`Model="pro"`) with the pushed branch name and `--workdir <WORKTREE_PATH>`; it runs `branch-diff` and records its verdict + dispatches notification via `record-review --branch <BRANCH_NAME> --workdir <WORKTREE_PATH> --verdict PASS --score <0-100> --summary "..." --send-email`.
6. **Background Merge Watcher & Scoped Lock Release**:
   - Spawn `watch-pr-merge --workdir <WORKTREE_PATH> --daemon` (15s poll) or run `evaluate-branch --branch <BRANCH_NAME> --workdir <WORKTREE_PATH> --decision approve|reject --comment "..."` (or `release-lock --repo <owner/repo>` / `--all`) to clean up branches/worktrees, fast-forward local `main`, and release only that repository's lock.
