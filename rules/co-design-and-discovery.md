---
trigger: model_decision
description: Mandatory Phase 0 Problem Discovery & Interactive Co-Design (Grill-Me) protocol for agy-dev-plugin — adaptive activation, silent codebase-first inspection, demanding partner premise challenging, hybrid iterative/compact interview cadence, and Design Contract exit gate into planning, dev_queue.json, or fix/* execution.
---

# Phase 0: Problem Discovery & Interactive Co-Design (`co-design-and-discovery.md`)

`agy-dev-plugin` governs the **full end-to-end SDLC**, starting from problem definition before any file is edited or queued:

```text
0. Discovery & Co-Design (Grill-Me) -> 1. Technical Plan (agy-dev-planner) -> 2. Queue (dev_queue.json) -> 3. Orchestration & Worktree Dev -> 4. Maker-Checker Review -> 5. Manual PR Merge & Deploy
```

Instead of jumping straight to code or dumping a monolithic finished proposal with rubber-stamp questions tacked onto the end, co-design the optimal solution interactively with the user first. This complements Antigravity's native `/grill-me` and `/plan` slash commands by activating automatically on engineering design problems and handing confirmed decisions directly into `agy-dev-planner` and `dev_queue.json`.

---

## 1. Adaptive Activation Gate (When to Grill vs. Skip)

- **Trigger by Default (Interactive Engineering Work)**: Activate Phase 0 Grill-Me automatically whenever the user introduces a new feature, UX/UI change, architectural refactor, workflow design, cross-repo change, or open-ended problem statement — and whenever resolving an existing `hitl_blocked` item in `dev_queue.json`.
- **Skip Cleanly When Unnecessary**:
  1. **Trivial or Unambiguous Fixes**: Typos, mechanical lint/format repairs, or deterministic bugs with a single obvious root cause and fix.
  2. **Explicit User Bypass**: When the user explicitly asks to skip discovery (e.g., *"do it directly"*, *"no questions"*, *"skip grill-me"*).
  3. **Autonomous Subagents & Scheduled Jobs (Strictly Forbidden)**: Non-interactive workers (`agy-dev-executor`, `agy-code-developer`, `agy-code-pr-reviewer`, cron/background tasks, or `agy-dev-planner` invoked as a subagent on an already-confirmed spec) MUST NEVER pause to grill the user.

---

## 2. Silent Codebase-First Discovery (Zero Redundant Questions)

- Before asking the first question, **silently inspect** the target repositories, impacted modules, existing tests, active per-repository `dev_pr_locks` (`pr_remediator.py lock-status --all`), and `dev_queue.json`.
- **NEVER** ask the user facts the codebase, git history, or runtime config can already answer (file paths, schemas, existing endpoints, lock collisions, test frameworks).
- Reserve every question strictly for genuine product, UX, scope, trade-off, or risk-appetite forks where human judgment defines the right design.

---

## 3. Demanding Partner Pushback (Challenge Premises First)

Act as a candid, high-bar engineering partner — not a passive order-taker:

1. **Question the Premise Before Details**: Before drilling into implementation details, evaluate and surface whether:
   - An **Antigravity-native capability** (official plugin, MCP server, built-in slash command, skill, rule, or hook) already solves the need without custom code.
   - The scope can be simplified (**MVP / YAGNI**) or consolidated into an existing table, endpoint, client, or UI flow rather than adding parallel abstractions.
   - Any candidate approach introduces maintenance debt, over-engineering, Santa Lockdown risk, or Zero-PII/security exposure.
2. **Opinionated Trade-Offs on Every Fork**: For every design fork, present concrete options with concise trade-offs and a clear `(Recommended)` choice backed by technical rationale. Never ask vague open-ended questions when concrete alternatives can be contrasted.

---

## 4. Hybrid Interview Cadence

Adapt interview rhythm to problem complexity using `ask_question` (or crisp structured prompts):

- **Open-Ended / Architectural / Multi-Repo Problems (Iterative Tree)**: Walk down the decision tree **branch by branch** (1–2 high-leverage questions per turn, resolving the most structural fork first and letting each answer shape the next branch) until zero material ambiguity remains.
- **Well-Bounded Changes with 2–3 Clear Forks (Compact Round)**: Present **1 focused structured round** via `ask_question` covering the concrete forks with trade-offs and `(Recommended)` defaults. Open a short Round 2 only if an answer reveals a genuinely new fork.
- **Avoid Interview Fatigue**: Stop grilling as soon as every material product/UX/architecture fork is resolved; never pad rounds with trivial implementation minutiae.

---

## 5. Exit Gate: Design Contract & Execution Handoff

When co-design converges, synthesize a concise **Design Contract** before mutating tracked files:

1. **Confirmed Decisions**: Numbered summary of the design/UX/architecture choices agreed together.
2. **Scoped Changes & Verification**: Impacted repositories, anchored target files, and required hermetic tests.
3. **Explicit Non-Goals & Guardrails**: What is intentionally out of scope (preventing scope creep).

Then offer a fast exit handoff calibrated to current per-repository `dev_pr_locks`:
- **Target Repo(s) Unlocked**: Ask whether to **implement immediately** on a timestamped `fix/*` worktree branch (`agy-code-remediation-pr`) OR **enqueue as `pending`** in `dev_queue.json` (`pr_remediator.py queue-add`).
- **Target Repo(s) Locked**: Recommend **enqueueing directly as `pending`** in `dev_queue.json` with all confirmed decisions and `target_files` embedded in `--proposed-fix` / `--notes` — avoiding `hitl_blocked` buildup so `agy-dev-orchestrator` executes cleanly as soon as the active lock clears.
