---
trigger: glob
globs: "**/*.py, **/*.sh, **/*.bash, **/*.zsh, **/*.bats, **/*.js, **/*.mjs, **/*.html, **/*.css, **/Makefile, **/SKILL.md, **/agents/*.md, **/plugin.json, **/hooks.json, **/README.md, **/.agents/**"
description: Mandatory Native Git timestamped branch workflow, per-repository dev_pr_locks concurrency gate, pre-commit output, and manual PR review rules.
---

# Native Git & PR Workflow (`git-and-pr-workflow.md`)

Always follow [`git-commit-configuration`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/git-commit-configuration/SKILL.md) and [`agy-code-remediation-pr`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/SKILL.md) for any change to tracked repositories.

## 1. Native Git Only (VCS-Agnostic) & Anti-Bypass Guardrails
- Use native `/usr/bin/git` and `/usr/bin/ssh` via `git_client.py` and `pr_remediator.py`.
- Proprietary forge CLIs (`gh`, `glab`) and REST API tokens are forbidden and blocked by `PreToolUse` (`dev_common.py`).
- Direct pushes to `main` or `master` (`git push origin main`) AND direct `git push` commands targeting active locked branches before review passes (`pr_gate_ready=False`) are blocked by `PreToolUse` (`pretool:direct-main-push` & `pretool:unvalidated-branch-push`).

## 2. Local-First Review, Hard PR Readiness Gate (`PASS > 90/100`) & Per-Repo Lock Protocol
1. Check per-repository `dev_pr_locks` (`pr_remediator.py lock-status --all --sync-git` or scoped with `--workdir <TARGET_REPO_DIR>` / `--repo <owner/repo>`). Another repo's lock NEVER blocks an unlocked repo. When a converged **Design Contract** or approved task targets one or more repositories currently holding an active `dev_pr_lock`, **DO NOT ask the user for confirmation or priority** (never prompt *"Do you want me to enqueue this?"* or *"What priority should I assign?"*) — immediately inspect `dev_queue.json`, active `dev_pr_locks`, application architecture, dependencies (`depends_on`), and user urgency to assign a calibrated numeric priority relative to existing queued tasks, execute `pr_remediator.py queue-add --status pending` (populating full `rca_summary`, `proposed_fix`, `target_files`, `depends_on`, and `repo_slugs` via `--repos`), and render a concise visual confirmation table showing the newly queued item's position, calibrated priority, target repo(s), active blocker lock, and next-up queue sequence.
2. Start or reuse a repo-isolated worktree branch (`fix/<YYYYMMDD-HHMMSS>-<scope>-<cid8>`) with `start-remediation --workdir <TARGET_REPO_DIR> --worktree` (`--reuse-active` for follow-ups from the same thread).
3. Run `pre-commit run --all-files` AND `pr_remediator.py test-local --workdir <WORKTREE_PATH>` (persisting deterministic `verification_results` bound to `head_sha`) and paste the visible `pre-commit` summary output in your reply.
4. Stage locally for review via `pr_remediator.py prepare-review --workdir <WORKTREE_PATH>` (alias `push-branch`). This commits locally in `AWAITING_AGENT_REVIEW` **without pushing to `origin` or generating `manual_pr_url`**.
5. Run independent `agy-code-pr-reviewer` (`Model="pro"`) -> `record-review --workdir <WORKTREE_PATH> --send-email`:
   - **Hard Gate & Zero PR-Ready Emails**: ONLY `verdict == "PASS"` AND `score > 90/100` amends the HEAD commit with `## 🧪 Automated Verification Checks` + `## 🤖 Independent PR Review`, pushes `origin/<branch>`, sets `pr_gate_ready=True`, starts `watch-pr-merge`, and exposes the rich clickable `manual_pr_url` (`ready_for_human_pr == true`) with **zero PR-ready emails** (`notification_email_sent == false` — PR readiness is surfaced exclusively via `agy-queue` TUI, `dev_queue_board.md`, `dev_queue.json`, and chat `manual_pr_url`; never invoke `gmail_draft_client.py` or `AGY_DEV_NOTIFY_CMD` for PR ready emails, and `--send-email` only dispatches Round-3 `[🟥]` KO circuit-breaker alerts).
   - **Auto-Iteration Loop**: On `NEEDS_REVISION`, `WARN`, or `score <= 90`, NEVER surface any PR link—re-dispatch `agy-code-developer` using `developer_remediation_plan` and repeat until `ready_for_human_pr == true` (max 3 rounds before circuit-breaking to `ESCALATED_MAX_REVIEW_ITERATIONS` / `hitl_blocked`).
6. ONLY once `ready_for_human_pr == true`, present the clickable **Compare & Create Manual Pull Request** link (`manual_pr_url`) so the user merges or rejects manually (`evaluate-branch --decision approve` enforces `pr_gate_ready == True` and `reviewed_head_sha == head_sha`).

## 3. Plugin Deployment (`$HOME` Mirror Parity)
- Whenever a plugin repository (`agy-dev-plugin` or companion plugins) is updated and ready, deploy cleanly to `$HOME/.gemini/config/plugins/<plugin>/` via `./install.sh` or `git_client.py deploy-home`.
