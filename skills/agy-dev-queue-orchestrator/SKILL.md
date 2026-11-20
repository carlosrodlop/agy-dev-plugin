---
name: agy-dev-queue-orchestrator
description: Single-conversation persistent development queue orchestrator coordinating multi-repo code and skill tasks serialized behind dev_pr_lock and backed by deterministic dev_queue.json state in pr_remediator.py. Manages priority ordering, logical batching, smart developer model tiering (flash pre-anchored fast-path vs pro + mandatory pro reviewer), background PR merge watchers, $HOME deployment, and safe post-merge conversation cleanup.
---

# Persistent Multi-Repo Dev Queue Orchestrator (`agy-dev-queue-orchestrator`)

Coordinates multiple pending engineering tasks sequentially inside one persistent orchestrator conversation without violating `dev_pr_lock` exclusivity or wasting context on parallel write conflicts.

## Persistent Queue Commands (`dev_queue.json` · `0600` + `file_lock`)
All operations run deterministically via [`pr_remediator.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py):
- **Inspect queue**: `pr_remediator.py queue-list`
- **Enqueue / update item**: `pr_remediator.py queue-add --id <CID> --title "..." --priority <10-90> --repos <repo1> <repo2> --target-files <f1> <f2> --orchestrator-id <ORCH_CID>`
- **Pick next unblocked item**: `pr_remediator.py queue-next --orchestrator-id <ORCH_CID>`
- **Transition item state**: `pr_remediator.py queue-set-status --id <CID> --status <pending|in_progress|pr_open|merged|blocked|cancelled> --branches ... --pr-urls ...`

## Orchestration Loop
1. **Sync & Inspect**: Run `lock-status --sync-git` and `queue-list`. If a previously opened branch merged, fast-forward local `main`, deploy plugins via `git_client.py deploy-home`, and advance status to `merged`.
2. **Pre-Anchor Context Before Spawning `agy-code-developer`**:
   - Locate exact files, line numbers, API contracts, and unit test targets upfront.
   - Pass structured `anchored_context` to `agy-code-developer` (`Model="flash"` for focused pre-anchored fixes, `Model="pro"` for multi-file refactors).
3. **Mandatory Maker-Checker Gate**:
   - After `push-branch`, always invoke `agy-code-pr-reviewer` with `Model="pro"` before surfacing clickable `manual_pr_url` links to the user.
4. **Poll Merge Status & Clean Up**:
   - Use `/schedule` (2-minute intervals while awaiting manual PR merges) or `watch-pr-merge --daemon`, then run `cleanup-merged-conversations --confirm-merged --exclude-id <ORCH_CID>` after merge verification.
