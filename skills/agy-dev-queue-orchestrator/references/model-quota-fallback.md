# Model Routing, Cross-Family Quota Balancing, 3-Tier Risk Effort & Quota Fallback (`agy-dev-plugin`)

This playbook defines the deterministic token-optimal and quota-resilience protocol across `agy-dev-orchestrator`, `agy-dev-executor`, `agy-code-developer`, `agy-code-pr-reviewer`, `pr_remediator.py`, `dev_queue_doctor.py`, and `sidecars/dev-queue-watchdog/watchdog.py`.

---

## 1. 0-LLM 3-Tier Change Risk Effort & Cross-Family Maker-Checker Routing

Every dispatch classifies change risk deterministically in `<1 ms` (`classify_change_risk_tier()` in `clients/model_quota.py`) and balances work across independent model quota pools (`claude_opus`, `claude_fable`, `gemini_4`, `gemini_hydrogen`, `gemini_pro_next`, `gemini_flash`, `gemini_flash_lite`) via `AGY_DEV_MODEL_ROUTING_STRATEGY="balanced_cross_family"` (default; set `"sequential"` to restore strict static order):

| Risk Tier | 0-LLM Trigger | Developer Model (Round 1) | Independent Reviewer (Cross-Family) |
| :--- | :--- | :--- | :--- |
| **Tier 1 (`micro`)** | `<=3` files, `<=80` LOC, docs/UI/config only, zero critical paths | `flash_lite` (`gemini-3.5-flash-lite`) | `low`-effort Cross-Family (`fable-5.2-low` / `opus-5.5-low` / `gemini-4-carbon-low`) |
| **Tier 2 (`standard`)** | Normal features & bugfixes | `flash` (`PR_DEV_DEFAULT_MODEL = "flash"` / `gemini-flash-next`; Round 2+ auto-escalates to `pro`/high) | `medium`-effort Cross-Family (`fable-5.2-medium` / `opus-5.5-medium` / `hydrogen-l-tf-medium` / `auto`) |
| **Tier 3 (`critical`)** | Critical paths (`hooks.json`, `dev_common.py`, `git_client.py`, `pr_remediator.py`, `lock_store.py`, `review_gate.py`, `auth`/`oauth`/`security`/`install.sh`/workflows) OR `>8` files OR `>400` LOC | Highest-quota `high`-effort pool (`auto` / `opus-5.5-high` / `fable-5.2-high` / `hydrogen-l-tf-high` / `pro`) | Cross-Family `high`-effort pool (different broad family from `dev_model`: Gemini dev -> Claude review; Claude dev -> Gemini review) |

1. **Orchestrator on `flash` by Default (`AGY_DEV_ORCHESTRATOR_MODEL = "gemini-flash-next"`)**:
   - Control-plane orchestration (`agy-dev-orchestrator` and detached `agy-doctor` wakes) defaults to `gemini-flash-next` (`100%` fast pool), keeping reasoning quotas free for coding and PR review.
2. **Highest-Quota Cross-Family Balancing (`swarm`)**:
   - Within the required risk tier, `select_next_optimal_model()` picks the healthy (`> AGY_DEV_LOW_QUOTA_THRESHOLD_PCT`) pool with the **highest remaining quota `%`** and enforces **Cross-Family Maker-Checker** whenever `dev_model` is passed (`--dev-model` on `start-remediation` / `quota-status`).
3. **Automatic Round 2+ Escalation (`flash -> pro`)**:
   - Whenever Round 1 review fails (`NEEDS_REVISION`, `WARN`, or `score <= 90/100`), `_build_developer_remediation_plan()` automatically passes `review_iteration >= 2` to `build_code_developer_subagent_plan()`, escalating the developer remediation dispatch to `pro` (or the next healthy high-reasoning pool) and reflecting the actual `dev_model` in `created_by`.
4. **Hard Quality Gate Preserved (`PASS > 90/100`)**:
   - Cross-family balancing and risk-tiered effort NEVER relax `PASS > 90/100`, `pre-commit`, `qa_static_audit.py`, or hermetic unit tests. Fallback onto `invoke_subagent` never downgrades a standard/critical review to `inherit` (Flash orchestrator), defaulting `subagent_model` to `pro` for standard/critical reviews and `flash` for micro reviews.

---

## 2. Preventive Low-Quota Eco (`<= 25%`) & Unlimited-First Fallback Chains

Configure optional overrides in `0600` `~/.env` (mirrored as safe defaults in `.env.example`):

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `AGY_DEV_UNLIMITED_MODEL_IDS` | `""` | Comma-separated unlimited model IDs ranked **#1** before metered models whenever available. |
| `AGY_DEV_MODEL_ROUTING_STRATEGY` | `balanced_cross_family` | `balanced_cross_family` (highest remaining `%` pool + Cross-Family Maker-Checker) or `sequential` (strict static order). |
| `AGY_DEV_ORCHESTRATOR_MODEL` | `gemini-flash-next` | Default fast control-plane model for `agy-dev-orchestrator` and `agy-doctor` wakes. |
| `AGY_DEV_LOW_QUOTA_THRESHOLD_PCT` | `25` | Preventive Eco threshold (%). Pools at or below this percentage trigger proactive cross-pool fallback *before* hitting `429`. |
| `AGY_DEV_QUOTA_SNAPSHOT_TTL_SECONDS` | `900` | TTL (seconds) for persisted live `swarm(action="models", all=true)` quota snapshots. |
| `AGY_DEV_MODEL_FALLBACK_ORDER` | `gemini-flash-next,auto,hydrogen-l-tf-medium,fable-5.2-medium,opus-5.5-medium,gemini-3.5-flash-lite` | Ordered fallback chain for orchestrators and detached `agy-doctor` wakes. |
| `AGY_DEV_DEV_ECO_FALLBACK_ORDER` | `gemini-flash-next,gemini-3.5-flash-lite,auto,fable-5.2-low,hydrogen-l-tf-medium` | Ordered Eco chain for Round 1 development when primary pools are low/exhausted. |
| `AGY_DEV_REVIEWER_MODEL_FALLBACK_ORDER` | `pro,auto,opus-5.5-high,fable-5.2-high,hydrogen-l-tf-high,gemini-flash-next` | Ordered high-reasoning review chain (`pro` -> cross-pool high-tier models). |
| `AGY_DEV_MAX_ORCHESTRATOR_STEPS` | `100` | Proactive orchestrator auto-handoff rotation threshold (`100-110` step window; `bloated_orchestrator_session`) to cut per-turn context tokens. |

---

## 3. Native Antigravity Runtime Protocol (Zero Private Scraping)

Only official Antigravity runtime APIs and local `0600` state are used:

1. **Pre-Dispatch & Recovery Quota Probe (`swarm models` + `quota-status`)**:
   - Before heavy dispatch waves (or immediately on any `429 RESOURCE_EXHAUSTED`), call `swarm(action="models", all=true)` to read live `quotas` (`quota_remaining`, `quota_resets`).
   - Persist the snapshot cleanly into `0600` `<state_dir>/model_quota_cooldowns.json` and compute deterministic risk-tiered Cross-Family routing:
     ```bash
     python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py quota-status \
       --role reviewer --files <FILE1> <FILE2> --dev-model gemini-flash-next --record-swarm-json '<SWARM_MODELS_JSON>'
     ```
2. **Subagent & Swarm Failover Ladder**:
   - When `select_next_optimal_model()` returns `requires_swarm_dispatch == true` (e.g., `fable-5.2-medium`, `opus-5.5-high`, `hydrogen-l-tf-medium`, `auto`):
     - Spawn the reviewer or developer via `swarm(action="add", ..., model="<selected_model>")` with explicit Done contract (and pass `--dev-model` on `start-remediation` / `--reviewer-model "<selected_model>"` on `record-review`), OR switch an idle worker via `swarm(action="model", name="<worker>", model="<selected_model>")`.
   - When operating purely via `invoke_subagent` (no `swarm`): use `routing["subagent_model"]` (`pro` for standard/critical review & Round 2+/critical dev; `flash` for standard dev & micro review; `flash_lite` for micro dev).
3. **Coordinator Self-Rotation**:
   - If the active orchestrator encounters `429` or its active model drops `<= 25%`, call `swarm(action="model", model="<next_optimal_model_id>")` to jump mid-conversation without losing background tasks or context.
4. **0-LLM `agy-doctor` Wake Selection & All-Exhausted Suppression**:
   - `dev_queue_doctor.py` scans orchestrator, worker, and reviewer transcript tails (across Slot #1 and Slot #2, including `AWAITING_AGENT_REVIEW`) for `429 RESOURCE_EXHAUSTED` + reset timers (`Resets in ...` / ISO UTC), persists `0600` cooldowns, and passes `--model=<fallback>` (`agentapi new-conversation`) or `--model <fallback>` (`agy --agent agy-dev-orchestrator`) on wake.
   - If **all** fallback models are simultaneously in cooldown, `trigger_orchestrator_wake()` returns `status="skipped_all_models_exhausted"` (`next_reset_at=...`) so `watchdog.py` sleeps with 0 wake burn until the earliest pool resets.

---

## 4. Context-Token Hygiene & Zero-Overhead Fast-Paths

- **Compact CLI Output by Default (`--verbose` opt-in)**: `start-remediation`, `prepare-review` / `push-branch`, and `plan-developer` emit compact token-saving JSON (~1–2 KB) on CLI stdout by default (stripping redundant `SystemPrompt`, `proposal_markdown`, and nested lock blobs); pass `--verbose` only when full raw markdown/prompts are needed.
- **Planner Fast-Path (`skip_planner_subagent`)**: `queue-next --unlocked` annotates `skip_planner_subagent: true` (and `risk_classification`) whenever a runnable queue item already provides `target_files` + (`proposed_fix` or `section_anchors`). Skip invoking `agy-dev-planner` and dispatch the developer directly!
- **Proactive Auto-Handoff Rotation (`100-110` steps)**: Rotate long-running `agy-dev-orchestrator` sessions via `AGY_DEV_MAX_ORCHESTRATOR_STEPS` (default `100`, hard `110`; `queue-optimize` -> `bloated_orchestrator_session` & `orchestrator-tick` `orchestrator_rotation`) using `pr_remediator.py queue-next --unlocked --orchestrator-id <NEW_CID>`.
- In interactive engineering chats, use Antigravity's native `/compact` command when conversations grow long, and always pass `--stall-seconds 900` on `wait-for-unlock` (values `> 3600s` are automatically clamped unless `--allow-excessive-stall-seconds` is explicitly supplied).
