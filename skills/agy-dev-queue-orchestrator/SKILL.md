---
name: agy-dev-queue-orchestrator
description: Multi-repo development queue orchestrator paired with agy-dev-orchestrator, agy-dev-planner, and agy-dev-executor, serialized per repository via dev_pr_locks and backed by deterministic dev_queue.json state in pr_remediator.py. Manages priority ordering, parallel unlocked-repo selection (queue-next --unlocked), progressive multi-repo merged_repos tracking, background PR merge watchers, $HOME deployment, and safe post-merge conversation cleanup.
---

# Persistent Multi-Repo Dev Queue Orchestrator (`agy-dev-queue-orchestrator`)

Coordinates multiple pending engineering tasks across managed repositories without per-repo lock collisions—serializing edits within the same repository while allowing distinct unlocked repositories to progress in parallel.

## Persistent Queue Commands (`dev_queue.json` · `0600` + `file_lock`)
All operations run deterministically via [`pr_remediator.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py):
- **Inspect queue & active repo locks**: `pr_remediator.py lock-status --all --sync-git` and `pr_remediator.py queue-list`
- **Enqueue / update item**: `pr_remediator.py queue-add --id <CID> --title "..." --priority <10-90> --repos <repo1> <repo2> --status pending --target-files <f1> <f2> --depends-on <dep_cid> --rca-summary "..." --proposed-fix "..." --orchestrator-id <ORCH_CID>`
- **Locked-Repo Auto-Enqueue Policy (Zero Confirmation Prompt)**: When a **Design Contract** converges (or an approved task is ready) and one or more target repositories currently hold an active `dev_pr_lock`, **DO NOT ask the user for confirmation or priority** (never prompt *"Do you want me to enqueue this?"* or *"What priority should I assign?"*). Immediately inspect `dev_queue.json`, active `dev_pr_locks`, application architecture, dependencies (`depends_on`), and user urgency to assign a calibrated numeric priority automatically relative to existing queued tasks, execute `pr_remediator.py queue-add --status pending` immediately (including full `rca_summary`, `proposed_fix`, `target_files`, `depends_on`, and `repo_slugs` via `--repos`), and render a concise visual confirmation of the newly queued item and active queue state (compact Markdown table showing queue position, calibrated priority, target repo(s), active blocker lock, and next-up sequence).
- **Pick next unblocked item (skipping locked repos)**: `pr_remediator.py queue-next --unlocked --orchestrator-id <ORCH_CID>`
- **Reconcile merged `pr_open` items & prune stale compare `pr_urls`**: `pr_remediator.py queue-sync [--repo <owner/repo>] [--workdir <DIR>] [--fetch]` (also executed automatically during `lock-status --sync-git`)
- **Transition item state**: `pr_remediator.py queue-set-status --id <CID> --status <pending|in_progress|pr_open|merged|standby|hitl_blocked|cleaned|cancelled|rejected> --branches ... --pr-urls ...` (automatically deduplicates `pr_urls` per `(repo_slug, branch_name)` keeping the richest verified URL: `/pull/` > rich verified `/compare/` > bare `?expand=1`)

## Orchestration Loop (`agy-dev-orchestrator` -> `agy-dev-planner` -> `agy-dev-executor`)
1. **Sync & Inspect Locks Per Repo**: Run `lock-status --all --sync-git` and `queue-list`. If any repo branch merged, fast-forward local `main`, update `merged_repos` cleanly (only marking multi-repo items `merged` once all target repos merge), and deploy plugins via `git_client.py deploy-home`.
2. **Plan Before Executing (`agy-dev-planner`)**:
   - Locate exact files, line numbers, API contracts, cross-repo dependencies, and unit test targets upfront before reserving a worktree.
   - If any target repository holds an active `dev_pr_lock`, immediately auto-enqueue as `pending` via `queue-add` with calibrated numeric priority and a concise visual queue confirmation table (never prompt for confirmation or priority).
   - Pass structured `anchored_context` to `agy-dev-executor` / `agy-code-developer` (`Model="flash"` for focused pre-anchored fixes, `Model="pro"` for multi-file refactors).
3. **Execute Per-Repo Worktrees, Local-First Review & Hard `PASS > 90` Auto-Iteration Loop (`agy-dev-executor`)**:
   - Work in isolated repo-scoped worktrees (`pr-worktree-<repo>-<branch>`), run `pre-commit run --all-files` + `test-local --workdir <WORKTREE_PATH>`, and stage locally via `prepare-review` (`push-branch`) without pushing to `origin` yet.
   - Invoke `agy-code-pr-reviewer` (`Model="pro"`). Only `record-review` with `verdict=PASS` and `score > 90/100` publishes `origin/<branch>`, transitions the queue item to `pr_open`, exposes `manual_pr_url` (`ready_for_human_pr == true`), and exclusively dispatches the PR Ready for Validation notification email (forwarding the clean VCS `--target-url` to `gmail_draft_client.py` / `AGY_DEV_NOTIFY_CMD`). Orchestrators, executors, and reviewers MUST NEVER invoke `gmail_draft_client.py` directly or ad-hoc for PR validation emails.
   - If `needs_reiteration == true`, automatically re-dispatch `agy-code-developer` with `developer_remediation_plan` and loop until `ready_for_human_pr == true` (max 3 rounds before circuit-breaking to `hitl_blocked` with zero PR links). Never surface PR URLs while locks are in `AWAITING_AGENT_REVIEW`, `NEEDS_REVISION_ITERATING`, or `ESCALATED_MAX_REVIEW_ITERATIONS`.
4. **Poll Merge Status & Clean Up**:
   - Use `/schedule` (2-minute intervals while awaiting manual PR merges) or `watch-pr-merge --daemon`, then run `cleanup-merged-conversations --confirm-merged --exclude-id <ORCH_CID>` after merge verification.

## Compact Zero-Hardcode Dev Queue TUI (`agy-queue` / `dev_queue_tui.py`)
Monitor all active queue items, per-repository locks, verified PR compare links, and `HITL_BLOCK` items from any terminal without hardcoded repository names, owners, or paths:
- **Script & Wrapper**: [`dev_queue_tui.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-dev-queue-orchestrator/scripts/dev_queue_tui.py) wrapped by [`shell/agy_dev_queue.sh`]($HOME/.gemini/config/plugins/agy-dev-plugin/shell/agy_dev_queue.sh) (symlinked to `$HOME/.local/bin/agy-queue` by `./install.sh`, exposed as `[project.scripts]` `agy-queue`, and sourceable in Bash/Zsh for `agy-queue` and `agy-queue-watch` aliases).
- **Dynamic Discovery & Single-Table UX**:
  - Resolves state via `dev_common.resolve_state_dir()` and discovers managed repos/checkouts dynamically from `dev_queue.json`, `dev_pr_locks`, and `git_client.py` (optionally `--repos-dir`).
  - Displays a 1-line summary header, 1-line clickable `REPOS` strip (`●` locked / `○ FREE`), and 1 unified table with collision-safe short IDs.
  - **Multi-Repo PR Clarity**: Never averages a human-ready PR with unstarted downstream repos (`PR 1/2 listo` full bright-yellow bar when downstream repos remain; `PR listo` / `PR 2/2 listo` bright-green bar on the final repo; numeric `%` + ETA reserved strictly for active `IN_PROGRESS` work). Marks the exact PR-ready repo with `★` (prioritized first on `+N` truncation) and prints sequencing in the footer (`[1] <repo> (<cid> · parte 1/2 · desbloquea <next_repo>) [+verificaciones]`).
  - **Richest PR URL Ranking & Preservation**: Ranks created `/pull/` / `/merge_requests/` and verified `?expand=1&title=...&body=...` URLs ahead of bare `?expand=1` URLs, strictly gating on `HUMAN_READY_LOCK_STATUSES` and `pr_gate_ready` (`AWAITING_AGENT_REVIEW`, `NEEDS_REVISION_ITERATING`, `ESCALATED_MAX_REVIEW_ITERATIONS`, or `pr_gate_ready=False` never surface PR links). Preserves full query parameters in OSC-8 hyperlink targets, browser hotkeys (`[o]`, `[1-9]`), and `dev_queue_board.md` `[Abrir PR](...)` links (escaping parentheses for valid Markdown).
  - **Flicker-Free Watch Mode & Hotkeys (`agy-queue --watch [SECS]`)**: Alternate screen + cursor hide/restore + viewport clamping + local offline-safe git ref checks (`AGY_OFFLINE_ONLY=1`). Hotkeys: `[o]` open first PR, `[1-9]` open row link, `[h+#]` copy exact `conversation_summaries.db` sidebar title & focus Antigravity Desktop App (never spawns `agy --conversation`), `[b]` sync board & focus Antigravity, `[s]` fetch+reconcile, `[a]` toggle merged items, `[r]` refresh, `[q]` quit.
- **Live Antigravity HITL Board Artifact (`dev_queue_board.md`)**:
  - Pass `--write-board <ARTIFACT_PATH>` (or store the path in `<state_dir>/dev_queue_board_path.txt`) so every render atomically refreshes a Markdown artifact containing native 1-click `[Title](conversation://<uuid>)` links for `HITL_BLOCK` threads and full clickable PR links.

## In-App `/hitl-review` Workflow (`hitl_blocked` -> `pending`)
Whenever the operator invokes `/hitl-review` (or asks to resolve `HITL_BLOCK` items inside Antigravity without manually hunting sidebar tabs):
1. **Enumerate Blocked Items**: Run `dev_queue_tui.py --status hitl_blocked --json` (or `pr_remediator.py queue-list`), sorted by ascending `priority`, resolving sidebar titles from `conversation_summaries.db`.
2. **Read Transcripts & Proposals**: For each `hitl_blocked` conversation ID, inspect its transcript via `dev_common.try_inspect_antigravity_conversation(<CID>)` (and any proposal `.md` artifacts under `$HOME/.gemini/antigravity/brain/<CID>/`) to extract the blocking question, candidate options, recommended path, and impacted repositories.
3. **Collect Explicit Decisions via `ask_question`**: Present a crisp decision prompt for each blocked item (or compact batch when independent) via `ask_question` with concrete options (`Approve recommended`, alternative options, `Defer to standby`, `Reject`).
4. **Auto-Promote Queue State**:
   - On approval/choice: immediately promote `hitl_blocked -> pending` with the ratified decision recorded in notes:
     `pr_remediator.py queue-set-status --id <CID> --status pending --notes "HITL approved via /hitl-review: <decision>"`
   - On deferral or rejection: transition cleanly to `standby` or `rejected` with `--notes`.
5. **Re-Sync Board & Resume Queue**: Run `dev_queue_tui.py` (auto-syncing `dev_queue_board.md` if registered) and pick the next unblocked task via `pr_remediator.py queue-next --unlocked`.
