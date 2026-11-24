# Model Routing, Preventive Low-Quota Eco & Automatic Quota Fallback (`agy-dev-plugin`)

This playbook defines the deterministic token-saving and quota-resilience protocol across `agy-dev-orchestrator`, `agy-dev-executor`, `agy-code-developer`, `agy-code-pr-reviewer`, `pr_remediator.py`, `dev_queue_doctor.py`, and `sidecars/dev-queue-watchdog/watchdog.py`.

---

## 1. Default Model Routing (`flash`-First Dev + Round 2+ `pro` Escalation -> High-Tier Review)

1. **Developer Round 1 Default (`PR_DEV_DEFAULT_MODEL = "flash"`)**:
   - `build_code_developer_subagent_plan()` (`review_gate.py`) and `agy-dev-orchestrator` default Round 1 development (`agy-code-developer` / `agy-dev-executor`) to `Model="flash"` to preserve high-reasoning quota pools while keeping implementation fast.
   - Explicit `Model="pro"` (or `--model pro`) remains available when the orchestrator flags a complex multi-file architecture refactor upfront.
2. **Automatic Round 2+ Escalation (`flash -> pro`)**:
   - Whenever Round 1 review fails (`NEEDS_REVISION`, `WARN`, or `score <= 90/100`), `_build_developer_remediation_plan()` automatically passes `review_iteration >= 2` to `build_code_developer_subagent_plan()`, escalating the developer remediation dispatch to `pro` (or the next healthy high-reasoning fallback if `pro` is low or in cooldown).
3. **Independent Reviewer Gate (`PR_SUBAGENT_MODEL = "pro"` Primary)**:
   - `agy-code-pr-reviewer` always targets `Model="pro"` first when the `pro` pool is healthy (`> AGY_DEV_LOW_QUOTA_THRESHOLD_PCT` and not in cooldown), enforcing the Lean Reviewer Protocol (`PASS > 90/100`, 3–5 turns using `branch-diff` `verification_gate`).
   - Model fallback selects healthy high-reasoning evaluators without ever relaxing `PASS > 90/100`, `pre-commit`, or `qa_static_audit.py`.

---

## 2. Preventive Low-Quota Eco (`<= 25%`) & Unlimited-First Fallback Chains

Configure optional overrides in `0600` `~/.env` (mirrored as safe defaults in `.env.example`):

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `AGY_DEV_UNLIMITED_MODEL_IDS` | `""` | Comma-separated unlimited model IDs ranked **#1** before metered models whenever available. |
| `AGY_DEV_LOW_QUOTA_THRESHOLD_PCT` | `25` | Preventive Eco threshold (%). Pools at or below this percentage trigger proactive cross-pool fallback *before* hitting `429`. |
| `AGY_DEV_QUOTA_SNAPSHOT_TTL_SECONDS` | `900` | TTL (seconds) for persisted live `swarm(action="models", all=true)` quota snapshots. |
| `AGY_DEV_MODEL_FALLBACK_ORDER` | `auto,hydrogen-l-tf-high,opus-5.5-high,fable-5.2-high,gemini-flash-next,gemini-3.5-flash-lite` | Ordered fallback chain for orchestrators and detached `agy-doctor` wakes. |
| `AGY_DEV_DEV_ECO_FALLBACK_ORDER` | `gemini-flash-next,gemini-3.5-flash-lite,auto,hydrogen-l-tf-high,fable-5.2-high` | Ordered Eco chain for Round 1 development when `flash` or `pro` pools are low/exhausted. |
| `AGY_DEV_REVIEWER_MODEL_FALLBACK_ORDER` | `pro,auto,hydrogen-l-tf-high,opus-5.5-high,fable-5.2-high,gemini-flash-next` | Ordered high-reasoning review chain (`pro` -> cross-pool high-tier models). |
| `AGY_DEV_MAX_ORCHESTRATOR_STEPS` | `150` | Early orchestrator session rotation threshold (`bloated_orchestrator_session`) to cut per-turn context tokens. |

---

## 3. Native Antigravity Runtime Protocol (Zero Private Scraping)

Only official Antigravity runtime APIs and local `0600` state are used:

1. **Pre-Dispatch & Recovery Quota Probe (`swarm models` + `quota-status`)**:
   - Before heavy dispatch waves (or immediately on any `429 RESOURCE_EXHAUSTED`), call `swarm(action="models", all=true)` to read live `quotas` (`quota_remaining`, `quota_resets`).
   - Persist the snapshot cleanly into `0600` `<state_dir>/model_quota_cooldowns.json` and compute deterministic routing:
     ```bash
     python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-remediation-pr/scripts/pr_remediator.py quota-status \
       --role reviewer --record-swarm-json '<SWARM_MODELS_JSON>'
     ```
2. **Subagent & Swarm Failover Ladder**:
   - When `select_next_optimal_model()` returns `requires_swarm_dispatch == true` (e.g., `pro <= 25%` or `429` -> routed to `hydrogen-l-tf-high`, `opus-5.5-high`, or `fable-5.2-high`):
     - Spawn the reviewer or developer via `swarm(action="add", ..., model="<selected_model>")` with explicit Done contract (and pass `--reviewer-model "<selected_model>"` to `record-review`), OR switch an idle worker via `swarm(action="model", name="<worker>", model="<selected_model>")`.
   - When operating purely via `invoke_subagent` (no `swarm`): fall back down the enum ladder `Model="pro"` -> `Model="inherit"` -> `Model="flash"` -> `Model="flash_lite"`.
3. **Coordinator Self-Rotation**:
   - If the active orchestrator encounters `429` or its active model drops `<= 25%`, call `swarm(action="model", model="<next_optimal_model_id>")` to jump mid-conversation without losing background tasks or context.
4. **0-LLM `agy-doctor` Wake Selection & All-Exhausted Suppression**:
   - `dev_queue_doctor.py` scans orchestrator, worker, and reviewer transcript tails (across Slot #1 and Slot #2, including `AWAITING_AGENT_REVIEW`) for `429 RESOURCE_EXHAUSTED` + reset timers (`Resets in ...` / ISO UTC), persists `0600` cooldowns, and passes `--model=<fallback>` (`agentapi new-conversation`) or `--model <fallback>` (`agy --agent agy-dev-orchestrator`) on wake.
   - If **all** fallback models are simultaneously in cooldown, `trigger_orchestrator_wake()` returns `status="skipped_all_models_exhausted"` (`next_reset_at=...`) so `watchdog.py` sleeps with 0 wake burn until the earliest pool resets.

---

## 4. Context-Token Hygiene

- Rotate long-running `agy-dev-orchestrator` sessions every **`~150` steps** (configurable via `AGY_DEV_MAX_ORCHESTRATOR_STEPS`, checked by `queue-optimize` -> `bloated_orchestrator_session`) via `pr_remediator.py queue-next --unlocked --orchestrator-id <NEW_CID>`.
- In interactive engineering chats, use Antigravity's native `/compact` command when conversations grow long.
- Always pass `--stall-seconds 900` on `wait-for-unlock` (values `> 3600s` are automatically clamped unless `--allow-excessive-stall-seconds` is explicitly supplied).
