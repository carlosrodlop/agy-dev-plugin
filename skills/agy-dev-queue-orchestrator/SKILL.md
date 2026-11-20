---
name: agy-dev-queue-orchestrator
description: Multi-repo development queue orchestrator paired with agy-dev-orchestrator, agy-dev-planner, and agy-dev-executor, serialized per repository via dev_pr_locks and backed by deterministic dev_queue.json state in pr_remediator.py. Manages priority ordering, parallel unlocked-repo selection (queue-next --unlocked), progressive multi-repo merged_repos tracking, background PR merge watchers, $HOME deployment, and safe post-merge conversation cleanup.
---

# Persistent Multi-Repo Dev Queue Orchestrator (`agy-dev-queue-orchestrator`)

Coordinates multiple pending engineering tasks across `agy-dev-plugin`, `agy-tam-plugin`, `g-control-tower`, and companion repos without per-repo lock collisions—serializing edits within the same repository while allowing distinct unlocked repositories to progress in parallel.

## Persistent Queue Commands (`dev_queue.json` · `0600` + `file_lock`)
All operations run deterministically via [`pr_remediator.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py):
- **Inspect queue & active repo locks**: `pr_remediator.py lock-status --all --sync-git` and `pr_remediator.py queue-list`
- **Enqueue / update item**: `pr_remediator.py queue-add --id <CID> --title "..." --priority <10-90> --repos <repo1> <repo2> --target-files <f1> <f2> --orchestrator-id <ORCH_CID>`
- **Pick next unblocked item (skipping locked repos)**: `pr_remediator.py queue-next --unlocked --orchestrator-id <ORCH_CID>`
- **Reconcile merged `pr_open` items & prune stale compare `pr_urls`**: `pr_remediator.py queue-sync [--repo <owner/repo>] [--workdir <DIR>] [--fetch]` (also executed automatically during `lock-status --sync-git`)
- **Transition item state**: `pr_remediator.py queue-set-status --id <CID> --status <pending|in_progress|pr_open|merged|standby|hitl_blocked|cleaned|cancelled|rejected> --branches ... --pr-urls ...` (automatically deduplicates `pr_urls` per `(repo_slug, branch_name)` keeping the richest verified URL: `/pull/` > rich verified `/compare/` > bare `?expand=1`)

## Orchestration Loop (`agy-dev-orchestrator` -> `agy-dev-planner` -> `agy-dev-executor`)
1. **Sync & Inspect Locks Per Repo**: Run `lock-status --all --sync-git` and `queue-list`. If any repo branch merged, fast-forward local `main`, update `merged_repos` cleanly (only marking multi-repo items `merged` once all target repos merge), and deploy plugins via `git_client.py deploy-home`.
2. **Plan Before Executing (`agy-dev-planner`)**:
   - Locate exact files, line numbers, API contracts, cross-repo dependencies, and unit test targets upfront before reserving a worktree.
   - Pass structured `anchored_context` to `agy-dev-executor` / `agy-code-developer` (`Model="flash"` for focused pre-anchored fixes, `Model="pro"` for multi-file refactors).
3. **Execute Per-Repo Worktrees, Local-First Review & Hard `PASS > 90` Auto-Iteration Loop (`agy-dev-executor`)**:
   - Work in isolated repo-scoped worktrees (`pr-worktree-<repo>-<branch>`), run `pre-commit run --all-files` + `test-local --workdir <WORKTREE_PATH>`, and stage locally via `prepare-review` (`push-branch`) without pushing to `origin` yet.
   - Invoke `agy-code-pr-reviewer` (`Model="pro"`). Only `record-review` with `verdict=PASS` and `score > 90/100` publishes `origin/<branch>`, transitions the queue item to `pr_open`, exposes `manual_pr_url` (`ready_for_human_pr == true`), and exclusively dispatches the PR Ready for Validation notification email (forwarding the clean VCS `--target-url` to `gmail_draft_client.py` / `AGY_DEV_NOTIFY_CMD`). Orchestrators, executors, and reviewers MUST NEVER invoke `gmail_draft_client.py` directly or ad-hoc for PR validation emails.
   - If `needs_reiteration == true`, automatically re-dispatch `agy-code-developer` with `developer_remediation_plan` and loop until `ready_for_human_pr == true` (max 3 rounds before circuit-breaking to `hitl_blocked` with zero PR links). Never surface PR URLs while locks are in `AWAITING_AGENT_REVIEW`, `NEEDS_REVISION_ITERATING`, or `ESCALATED_MAX_REVIEW_ITERATIONS`.
4. **Poll Merge Status & Clean Up**:
   - Use `/schedule` (2-minute intervals while awaiting manual PR merges) or `watch-pr-merge --daemon`, then run `cleanup-merged-conversations --confirm-merged --exclude-id <ORCH_CID>` after merge verification.
