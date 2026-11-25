---
trigger: model_decision
description: Mandatory Deterministic-First Engineering standard for agy-dev-plugin — 4-Tier Determinism Ladder (0-LLM default), Deterministic Sandwich pattern, pre-gated scheduled actions with JSON handoff, skill scriptability, and Layer-1 fail-fast validation.
---

# Deterministic-First Engineering (`deterministic-first-engineering.md`)

Every function, skill helper, validation gate, SDLC transition, and scheduled/recurring automation MUST default to **deterministic code (`Python 3` stdlib, allowed pure-Python libraries, or POSIX utilities)** and reserve LLM reasoning strictly for genuinely open-ended synthesis, judgment, or ambiguous natural-language work that cannot be expressed cleanly as code.

---

## 1. The 4-Tier Determinism Ladder (Stop at the Lowest Viable Tier)

Before implementing or modifying any workflow, classify it from top to bottom and **stop at the lowest tier** that cleanly solves the problem:

| Tier | Execution Pattern | Use When | Token / Model Budget |
| :--- | :--- | :--- | :--- |
| **Tier 0 — Pure Deterministic (`0-LLM` Default)** | Direct `python3` / POSIX CLI or background daemon (`sidecar.json` invoking `python3` directly). | API syncs, polling, health checks, backups, CRUD, deduplication, sorting/math, AST/static audits, cleanup, template rendering, schema validation, state reconciliation. | `0` LLM tokens (`model: "0-llm"`). |
| **Tier 1 — Pre-Gated Hybrid (`0-LLM` Gate + Handoff -> LLM)** | Deterministic pre-gate script (`action_precheck_runner.py` or equivalent) runs first and invokes `agentapi` **only** when actionable pending work exists. | Event-driven or scheduled jobs whose trigger condition is rare or stateful, but whose actual payload requires LLM synthesis when triggered. | `0` tokens on no-ops; LLM only on real triggers. |
| **Tier 2 — Deterministic Sandwich (`Code -> Bounded LLM Slot -> Code`)** | Single `collect` / `build-skeleton` Python command -> bounded LLM fills only semantic slots -> single atomic `validate` / `merge` / `publish` / `notify` Python command. | Reports, customer briefings, email drafts, campaign outreach, or structured documents that mix exact telemetry/tables/multi-system I/O with narrative prose. | Smallest capable model (`flash` / `flash_lite` preferred over `pro` when scaffolding constrains output). |
| **Tier 3 — Native LLM Agent + Deterministic Guardrails** | Interactive or subagent reasoning bounded by `PreToolUse` hooks, static audits (`project_audit.py`), and hermetic test suites. | Phase 0 interactive Co-Design (`co-design-and-discovery.md`), novel architecture design, complex code authoring, ambiguous multi-system RCA, and independent semantic code review. | Task-calibrated effort (`micro` -> `flash_lite`, `standard` -> `flash`, `critical`/review -> `pro` / Cross-Family). |

---

## 2. The Deterministic Sandwich Pattern (Tier 2 Standard)

Whenever a workflow genuinely needs an LLM for narrative synthesis or domain reasoning, **never use the LLM as an I/O glue layer, shell loop driver, or copy-paste template engine**:

1. **Input Contract (Deterministic `collect` / `build-skeleton` CLI)**:
   - Execute multi-source API/file reads in parallel inside Python (`ThreadPoolExecutor`, explicit timeouts, `auto_refresh=False` on browser/SSO sessions).
   - Compute exact metrics, descending sort orders, deduplication, review windows, recipient lists, and immutable tables in Python.
   - Emit a pre-populated JSON or Markdown skeleton containing immutable data fields alongside explicit typed slots for the LLM to fill.
2. **Bounded Semantic Core (LLM)**:
   - Consume the pre-built bundle directly and return **only** the semantic synthesis (executive summary, prioritized talking points, technical answers, prose narrative) in a clean JSON or Markdown payload.
   - Never ask the LLM to re-type large numeric tables, manually slice Markdown with `awk`/`sed`/`jq`, or loop over 5+ CLI commands sequentially.
3. **Output Contract (Atomic `validate` / `merge` / `publish` / `notify` CLI)**:
   - Merge the LLM semantic slots into the deterministic skeleton, run Layer 1 fail-fast structural checks, and execute multi-target side effects (document publish, draft creation, ledger state update, lock release, notification) inside **one atomic idempotent CLI command** to prevent half-applied state.

---

## 3. Recurring & Scheduled Actions (`sidecar.json` / Cron) Standards

Every scheduled automation (`"builtin": "schedule"` or long-running background watcher) MUST obey these invariants:

1. **Tier 0 (`0-LLM`) by Default**: Health checks, backups, inbox/task hygiene audits, radar pollers, conversation cleanups, and queue watchdogs run directly via `python3` with zero `agentapi` invocations.
2. **Mandatory 0-LLM Pre-Gate for LLM Actions (`agy:schedule-missing-pregate`)**: Never wire a scheduled `sidecar.json` directly to `agentapi` (or via an un-gated shell wrapper). Always wrap conditional LLM schedules behind a deterministic pre-gate runner (`action_precheck_runner.py` or equivalent) that fails closed on errors and exits `0` at `0` LLM tokens when no work is pending.
3. **Valid Executable at `args[1]` (`agy:sidecar-invalid-executable`)**: Scheduled sidecars MUST declare a real executable at `args[1]` (e.g., `python3`), never a bare flag such as `--no-workspace`.
4. **Zero Double-`detect` (Atomic `0600` Handoff)**: When a pre-gate runs `detect` / `detect-upcoming` and triggers `agentapi`, persist the precheck JSON payload atomically (`0600`) to a scratch handoff file and pass its path to the downstream agent so the LLM **never re-executes the same `detect` command** in Turn 1.
5. **High-Confidence No-Op Auto-Skip**: Deterministic pre-triage MUST auto-skip obvious non-actionable noise (e.g., automated senders, calendar/OOO messages, already-validated outputs, or high-confidence non-technical logistics) before waking an LLM.
6. **Passive Auth & In-Flight Locks**: Enforce `auto_refresh=False` (zero remote SSO/Okta auto-login or unprompted Chrome spawns) and TTL-guarded `in-flight` locks on multi-minute workflows to prevent overlapping cron waves.

---

## 4. Skill Scriptability & Anti-Glue Standards (`skills/<name>/`)

1. **Scripts Over Shell Glue**: Any skill coordinating `>= 2` external sources, multi-step CLI workflows, stateful caches, exact sorting/arithmetic, or structured artifact publishing MUST provide a dedicated Python helper (`skills/<name>/scripts/*.py`) or explicitly wrap a centralized client (`clients/*_client.py`).
2. **Code as Single Source of Truth (Zero Data Blobs in Markdown)**: Keep canonical lookup tables, source UUID lists, allowlists, regex classifiers, and schema templates inside tracked Python modules — never duplicated as large hardcoded blocks inside `SKILL.md` prose.
3. **Advisory Static Check (`agy:skill-heavy-no-script`)**: Heavy skills (`>= 12,000` bytes and `>= 4` fenced shell blocks) lacking both a local `scripts/*.{py,sh}` helper and an explicit client/script delegation emit a warning in `project_audit.py`.

---

## 5. Validation, QA & SDLC Fail-Fast Gates

1. **Layer 1 / Pre-Review `0-LLM` Fail-Fast**: Push every mechanical invariant (AST syntax & `F821`, Zero-PII/secrets, Santa Lockdown compliance, URL/Rison syntax, placeholder leaks, sort order, word/hypothesis caps, test-file coverage parity, `<24 KB` instruction caps) into deterministic Python checks (`<100 ms`) that block locally **before** invoking an independent `Pro` QA validator or `agy-code-pr-reviewer` subagent.
2. **Semantic Focus for Independent Reviewers**: Reserve independent LLM reviewers strictly for logical correctness, architectural quality, requirement fidelity, security edge cases, and technical factual accuracy.
3. **Hermetic Test Coverage**: Every new deterministic script, pre-gate, skeleton builder, or static audit rule MUST include offline zero-credential `unittest` / `bats` tests in the same commit (`python-and-testing.md`).

---

## 6. Lifecycle & Static Enforcement Map

| SDLC Stage | Enforcement Mechanism |
| :--- | :--- |
| **Phase 0 Co-Design** | [`co-design-and-discovery.md`]($HOME/.gemini/config/plugins/agy-dev-plugin/rules/co-design-and-discovery.md) challenges whether an LLM/agent is needed at all before designing details. |
| **Technical Planning** | [`agy-dev-planner.md`]($HOME/.gemini/config/plugins/agy-dev-plugin/agents/agy-dev-planner.md) assigns an explicit Determinism Tier (`Tier 0..3`) and anchors Python helper scripts / pre-gates. |
| **Static Audit (`pre-commit` & CI)** | [`project_audit.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py) enforces `agy:sidecar-invalid-executable`, `agy:schedule-missing-pregate`, and `agy:skill-heavy-no-script`. |
| **Independent PR Review** | [`agy-code-pr-reviewer.md`]($HOME/.gemini/config/plugins/agy-dev-plugin/agents/agy-code-pr-reviewer.md) audits deterministic-first compliance and hermetic test coverage under Criterion B. |
