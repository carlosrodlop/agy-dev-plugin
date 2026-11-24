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
   - Locks are isolated per repository slug (`owner/repo`) with up to 2 slots per repo (`max_slots=2`, strictly max 1 open GitHub PR per repo); distinct unlocked repositories progress in parallel.
   - **4-Tier Overlap Analysis (`analyze-overlap`), Slot #2 (`--allow-slot2`), Swarm & Stacked Branches**:
     - Run `pr_remediator.py analyze-overlap --repo <owner/repo> --workdir <TARGET_REPO_DIR>` to classify same-repo blast-radius overlap (`NONE`, `TRANSITIVE`, `INTERFACE_SHARED`, `DEEP_SHARED`).
     - `NONE` / `TRANSITIVE`: acquire parallel Slot #2 via `start-remediation --allow-slot2 --worktree` (using **Stacked Branches** `git rebase --onto origin/main <old_base_sha> <branch>` for `TRANSITIVE` deps). `PreToolUse` enforces `pretool:parallel-file-lease` (blocking writes to non-monolith `target_files` leased by a sibling slot) and `pretool:parallel-worker-deploy-home` (blocking `./install.sh` / `deploy-home` inside `pr-worktree-*`).
     - `INTERFACE_SHARED`: coordinate live contract changes inside one shared worktree via Antigravity-native swarm (`workspace='share'`).
     - `DEEP_SHARED`: serialize execution behind Slot #1 while running pipelined read-only `agy-dev-planner` pre-anchoring, and write standalone per-feature unit tests (`tests/test_<feature>.py` registered in `selective_test_runner.py`) to avoid monolith test-file merge conflicts across slots.
   - **Locked-Repo Auto-Enqueue (Zero Confirmation Prompt)**: If a converged **Design Contract** or approved task targets one or more repositories currently holding an active `dev_pr_lock` owned by an unrelated conversation (and is not immediately dispatched into Slot #2), **DO NOT ask the user for confirmation or priority** (never prompt *"Do you want me to enqueue this?"* or *"What priority should I assign?"*). Immediately inspect `dev_queue.json` (`queue-list`), active `dev_pr_locks`, application architecture, dependencies (`depends_on`), and user urgency to assign a calibrated numeric priority automatically relative to existing queued tasks, execute `pr_remediator.py queue-add --status pending` immediately (populating full `rca_summary`, `proposed_fix`, `target_files`, `depends_on`, and `repo_slugs` via `--repos`), and render a concise visual confirmation (compact Markdown table showing queue position, calibrated priority, target repo(s), active blocker lock, and next-up sequence).
2. **Acquire Per-Repo Lock & Isolated Timestamped Worktree Branch**:
   ```bash
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py start-remediation \
     --id <CONVERSATION_ID> --action-id <SCOPE> --workdir <TARGET_REPO_DIR> --worker-cid <WORKER_CID> \
     --rca-summary "<Why>" --proposed-fix "<What>" --worktree
   ```
   - Use `--reuse-active` when iterating on follow-up commits from the same active conversation (`--workdir` or `--repo <owner/repo>` selects any managed target repository cleanly).
   - `start-remediation`, `prepare-review`, and `record-review` automatically upsert active locks (including ad-hoc sessions) into `dev_queue.json` (`source="ad_hoc_lock"`) so active workers and human-ready PRs are always visible in `agy-queue` and tracked across partial multi-repo merges (automatically reverting partially-merged items with zero remaining open PRs/locks back to `pending` while preserving `merged_repos`).
   - Stamp `pr_remediator.py lock-heartbeat --workdir <WORKTREE_PATH> --worker-cid <WORKER_CID> --stage <LABEL>` during long tasks so `detect_stalled_lock` and `agy-queue` accurately track worker liveness.
3. **Implement, Test & Persist Unified Single-Pass Verification Checks in `<WORKTREE_PATH>`**:
   - Delegate or execute via `agy-dev-executor` / `agy-code-developer` inside `<WORKTREE_PATH>` (never leave dirty feature edits on `main`).
   - Iterate quickly on targeted `python3 -m unittest <module>`, then run the single-pass local gate (runs `pre-commit run --all-files` with `SKIP=unit-tests,qa-static-audit` first, then `qa_static_audit.py --incremental` + `selective_test_runner.py` once, persisting unified `verification_results` bound to `head_sha` inside `dev_pr_lock`):
     ```bash
     python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py test-local --workdir <WORKTREE_PATH> --with-pre-commit
     ```
4. **Stage Locally for Independent Review (Local-First Review — Zero Pre-Review Push)**:
   ```bash
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py prepare-review \
     --workdir <WORKTREE_PATH> --title "fix(<scope>): <summary>" --executive-summary "..."
   ```
   - `prepare-review` (alias `push-branch`) commits locally, renders deterministic `## 🧪 Automated Verification Checks`, and sets `status="AWAITING_AGENT_REVIEW"`, `pr_gate_ready=False`, `branch_pushed=False`, `manual_pr_url=""` **without pushing to `origin`**. Any subsequent commit/rebase invalidates `reviewed_head_sha` and clears `manual_pr_url` until re-reviewed. Direct `git push` on an unvalidated locked branch is blocked by `PreToolUse` (`pretool:unvalidated-branch-push`).
5. **Independent Lean Maker-Checker Gate (`agy-code-pr-reviewer`, `Model="pro"` Primary + Quota Fallback) & Auto-Iteration Loop**:
   - Round 1 development defaults to `Model="flash"` (`PR_DEV_DEFAULT_MODEL="flash"`), while Round 2+ remediation (`_build_developer_remediation_plan`) auto-escalates to `Model="pro"` (or next optimal fallback when `<= 25%` / `429`). Run `pr_remediator.py quota-status --role reviewer [--record-swarm-json '<JSON>']` to inspect/persist `0600` `model_quota_cooldowns.json` and select unlimited-first / preventive low-quota (`<= 25%`) fallback models (`skills/agy-dev-queue-orchestrator/references/model-quota-fallback.md`).
   - Invoke `agy-code-pr-reviewer` (`Model="pro"` when healthy, or next optimal fallback via `swarm(action="add", ..., model="<fallback>")` passing `--reviewer-model`) with `--branch <BRANCH_NAME>` and `--workdir <WORKTREE_PATH>`. It runs `branch-diff` locally — which embeds a SHA-bound `verification_gate` (`head_sha`, `verified_head_sha`, `matches_head`, `passed`, `skip_redundant_test_rerun`). Under the **Lean Reviewer Protocol (Target 3–5 turns)**, when `verification_gate.skip_redundant_test_rerun == true`, the reviewer skips redundant `pre-commit`/`unittest` reruns, audits diff logic, security/Zero-PII, and test quality directly, and executes:
     ```bash
     python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py record-review \
       --branch <BRANCH_NAME> --workdir <WORKTREE_PATH> --verdict <PASS|WARN|NEEDS_REVISION> --score <0-100> --summary "..." --send-email
     ```
   - **PASS (`score > 90/100`)**: Resolves the active worktree via `git worktree list --porcelain`, amends the HEAD commit with preserved Executive Summary + `## 🧪 Automated Verification Checks` + `## 🤖 Independent PR Review`, pushes `origin/<branch>` (failing loudly on amend/push error), sets `pr_gate_ready=True`, updates `dev_queue.json` to `pr_open` (deduplicating richest `pr_urls`), starts `watch-pr-merge`, and exposes `manual_pr_url` (`ready_for_human_pr == true`, `notification_email_sent == false`).
   - **Zero PR-Ready Emails Policy**: PR readiness is surfaced **exclusively** via `agy-queue` TUI, `dev_queue_board.md`, `dev_queue.json` (`pr_open`), and chat `manual_pr_url` (`--send-email` is a deprecated no-op on `PASS` and dispatches **only** Round-3 `[🟥]` KO circuit-breaker escalation alerts). Orchestrators, executors, and reviewers MUST NEVER invoke `gmail_draft_client.py` or `AGY_DEV_NOTIFY_CMD` for PR readiness emails.
   - **Non-PASS or `score <= 90`**: Keeps the branch local (`NEEDS_REVISION_ITERATING`, `manual_pr_url=""`, strips stale compare `pr_urls`, `notification_email_sent == false`) and returns `developer_remediation_plan` for automatic `agy-code-developer` re-iteration until `ready_for_human_pr == true`. At round 3 still failing, trips circuit breaker to `ESCALATED_MAX_REVIEW_ITERATIONS` + `hitl_blocked` + `[🟥]` KO escalation alert (when `--send-email` is passed).
6. **Slot #2 Follower Promotion (`rebase-reverify`), Background Merge Watcher & Scoped Lock Release**:
   - When Slot #2 passes `record-review` (`score > 90/100`), it enters `READY_AWAITING_BASE_MERGE` locally without pushing to `origin` (`manual_pr_url=""`). Once Slot #1 merges, `wait-for-unlock` / `lock-status --sync-git` promotes Slot #2 into Slot #1 (`NEEDS_REBASE_REVERIFY`, `event="slot2_needs_rebase_reverify"`); run `pr_remediator.py rebase-reverify --repo <owner/repo> --workdir <WORKTREE_PATH> --fetch` to rebase onto updated `origin/main` and run union verification (`AUTO_PROMOTED_ZERO_LLM`, `NEEDS_AGENT_SEMANTIC_FIX`, or `NEEDS_AGENT_CONFLICT_RESOLUTION`).
   - Once `ready_for_human_pr == true`, `watch-pr-merge --daemon` polls `origin` every 15s; `evaluate-branch --branch <BRANCH_NAME> --workdir <WORKTREE_PATH> --decision approve --comment "..."` enforces `pr_gate_ready == True` and `reviewed_head_sha == head_sha` (unless `--force` is passed explicitly), while `--decision reject` always cleans up and unlocks.
