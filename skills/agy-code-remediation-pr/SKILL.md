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
3. **Implement, Test & Persist Deterministic Verification Checks in `<WORKTREE_PATH>`**:
   - Delegate or execute via `agy-dev-executor` / `agy-code-developer` inside `<WORKTREE_PATH>` (never leave dirty feature edits on `main`).
   - Iterate quickly on targeted `python3 -m unittest <module>`, then run the full local gate (which persists structured `verification_results` bound to `head_sha` inside `dev_pr_lock`):
     ```bash
     cd <WORKTREE_PATH> && pre-commit run --all-files
     python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py test-local --workdir <WORKTREE_PATH>
     ```
4. **Stage Locally for Independent Review (Local-First Review — Zero Pre-Review Push)**:
   ```bash
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py prepare-review \
     --workdir <WORKTREE_PATH> --title "fix(<scope>): <summary>" --executive-summary "..."
   ```
   - `prepare-review` (alias `push-branch`) commits locally, renders deterministic `## 🧪 Automated Verification Checks`, and sets `status="AWAITING_AGENT_REVIEW"`, `pr_gate_ready=False`, `branch_pushed=False`, `manual_pr_url=""` **without pushing to `origin`**. Any subsequent commit/rebase invalidates `reviewed_head_sha` and clears `manual_pr_url` until re-reviewed. Direct `git push` on an unvalidated locked branch is blocked by `PreToolUse` (`pretool:unvalidated-branch-push`).
5. **Independent Maker-Checker Gate (`agy-code-pr-reviewer`, `Model="pro"`) & Auto-Iteration Loop**:
   - Invoke `agy-code-pr-reviewer` (`Model="pro"`) with `--branch <BRANCH_NAME>` and `--workdir <WORKTREE_PATH>`. It runs `branch-diff` locally and executes:
     ```bash
     python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py record-review \
       --branch <BRANCH_NAME> --workdir <WORKTREE_PATH> --verdict <PASS|WARN|NEEDS_REVISION> --score <0-100> --summary "..." --send-email
     ```
   - **PASS (`score > 90/100`)**: Resolves the active worktree via `git worktree list --porcelain`, amends the HEAD commit with preserved Executive Summary + `## 🧪 Automated Verification Checks` + `## 🤖 Independent PR Review`, pushes `origin/<branch>` (failing loudly on amend/push error), sets `pr_gate_ready=True`, updates `dev_queue.json` to `pr_open` (deduplicating richest `pr_urls`), starts `watch-pr-merge`, dispatches `[🟩]` email, and exposes `manual_pr_url` (`ready_for_human_pr == true`).
   - **Non-PASS or `score <= 90`**: Keeps the branch local (`NEEDS_REVISION_ITERATING`, `manual_pr_url=""`, strips stale compare `pr_urls`, suppresses Ready-for-PR email) and returns `developer_remediation_plan` for automatic `agy-code-developer` re-iteration until `ready_for_human_pr == true`. At round 3 still failing, trips circuit breaker to `ESCALATED_MAX_REVIEW_ITERATIONS` + `hitl_blocked` + `[🟥]` escalation alert.
6. **Background Merge Watcher & Scoped Lock Release**:
   - Once `ready_for_human_pr == true`, `watch-pr-merge --daemon` polls `origin` every 15s; `evaluate-branch --branch <BRANCH_NAME> --workdir <WORKTREE_PATH> --decision approve --comment "..."` enforces `pr_gate_ready == True` and `reviewed_head_sha == head_sha` (unless `--force` is passed explicitly), while `--decision reject` always cleans up and unlocks.
