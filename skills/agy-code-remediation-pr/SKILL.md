---
name: agy-code-remediation-pr
description: VCS-Interoperable Native Git Timestamped Branch Remediation Pipeline with Cross-Conversation Development Lock (dev_pr_lock) and Dual-Agent Orchestration (agy-code-developer -> agy-code-pr-reviewer, Model='pro') for interactive user-prompted code/configuration changes or escalated incident audits. Uses pure native git (git_client.py; GitHub, GitLab, Bitbucket, Gitea interoperable): creates fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>, pushes exclusively to remote feature branches, blocks concurrent branches while pending manual validation, and produces clickable Compare/Manual PR URLs.
---

# Native Git Timestamped Branch Remediation Pipeline (`agy-code-remediation-pr`)

All code, script, skill, rule, hook, and configuration changes MUST flow through [`pr_remediator.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py) using pure native `git` (never `gh` / `glab` REST CLIs, never direct pushes to `main`).

## End-to-End Execution Protocol
1. **Check Lock Status & Sync Local `main`**:
   ```bash
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py lock-status --sync-git
   ```
   - If `lock.active == true` for a different conversation, STOP and ask/enqueue via `queue-add` instead of opening a competing branch.
2. **Acquire Lock & Isolated Timestamped Worktree Branch**:
   ```bash
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py start-remediation \
     --id <CONVERSATION_ID> --action-id <SCOPE> --repo-dir <TARGET_REPO_DIR> \
     --rca-summary "<Why>" --proposed-fix "<What>" --worktree
   ```
   - Use `--reuse-active` when iterating on follow-up commits from the same active conversation (`--repo-dir` supports multi-repo branches across `agy-dev-plugin`, `agy-tam-plugin`, `g-control-tower`, etc.).
3. **Implement, Test & Gate in Isolated Worktree (`<WORKTREE_PATH>`)**:
   - Edit files ONLY inside `<WORKTREE_PATH>` (never leave dirty feature edits on `main`).
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
   - Invoke `agy-code-pr-reviewer` (`Model="pro"`) with the pushed branch name; it runs `branch-diff` and records its verdict + dispatches notification via `record-review --branch <BRANCH_NAME> --verdict PASS --score <0-100> --summary "..." --send-email`.
6. **Background Merge Watcher & Manual Lock Release**:
   - Spawn `watch-pr-merge --daemon` (15s poll) or let the user run `evaluate-branch --branch <BRANCH_NAME> --decision approve|reject --comment "..."` to clean up branches/worktrees, fast-forward local `main`, and release `dev_pr_lock`.
