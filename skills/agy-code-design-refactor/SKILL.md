---
name: agy-code-design-refactor
description: Structured engineering workflow for architecture review, complexity triage, and characterization-first incremental refactoring across Python 3, POSIX/Bash Shell, and Vanilla JS/HTML/CSS. Use when decomposing large or tightly coupled modules, eliminating duplicated logic, introducing cleaner boundaries, or improving testability without breaking existing behavior.
---

# Software Design & Safe Refactoring Workflow (`agy-code-design-refactor`)

Provides a disciplined, low-risk refactoring and design-review procedure across **Python 3**, **POSIX/Bash Shell**, and **Vanilla JS/HTML/CSS** without introducing speculative abstractions, unapproved Santa Lockdown binaries, or behavioral regressions.

---

## Phase 1: Structural Smell & Complexity Triage

Before moving code, run the pure-stdlib AST Complexity & God-File Auditor (`complexity_audit.py`) to obtain deterministic per-file/per-function LOC, max nesting depth, McCabe cyclomatic complexity, and god-file signals (skip refactoring if the code is already clear — apply YAGNI):

```bash
# Deterministic 0-LLM complexity & god-file triage (exit 0 with --warn-only; exit 1 on violations without it)
python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-design-refactor/scripts/complexity_audit.py \
  --repo <TARGET_REPO_DIR> --warn-only --json
```

| Structural Smell | Concrete Signal | Target Remedy |
| :--- | :--- | :--- |
| **God File / God Function** | Single file/function mixes CLI parsing, I/O, business rules, and presentation | Split pure domain helpers from thin I/O adapters |
| **Duplicated Logic** | Same parser, normalizer, validator, or formatter copied across 2+ files | Extract one shared pure helper in the owning layer (`clients/*_common.py`, shared JS formatter, sourced shell lib) |
| **Mixed I/O & Logic** | Domain decision cannot be tested without real network, git remotes, or live `$HOME` | Inject `path` / `env` / transport callables or pass plain parsed data into pure functions |
| **Hidden Coupling & Global State** | Implicit mutation of module globals, `os.environ`, or shared DOM globals | Return explicit typed dataclasses / plain state objects; pass dependencies explicitly |
| **Reinvented Wheel** | Custom parser, validator, date/crypto/UI utility duplicating mature libraries | Replace with Python stdlib, native Web Platform APIs, or an allowed non-binary library (`py3-none-any.whl` / zero-build JS/CSS module — never Santa-blocked binaries) |
| **Fragile Shell Logic** | Complex JSON, date math, or multi-file transforms written in nested `sed`/`awk` | Keep shell as a thin entrypoint and move non-trivial logic into stdlib Python 3 |

---

## Phase 2: Characterization-First Refactoring Protocol

Always follow this strict 4-step sequence so behavior changes are never conflated with structural cleanup:

1. **Pin Existing Behavior First (Characterization Tests)**:
   - Before editing production code, verify existing hermetic unit tests (`unittest` / `bats`) pass.
   - If the target code lacks tests for critical edge cases, add offline characterization tests first using synthetic mock data (`AcmeCorp`, `Globex`, `tempfile.TemporaryDirectory`) and verify they pass on the unmodified code.
2. **Refactor in Small, Behavior-Preserving Steps**:
   - Separate structural refactoring from new feature changes whenever practical (or isolate cleanly within the Design Contract scope).
   - Keep public CLI flags, JSON schemas, HTTP response keys, and CSS/HTML IDs backward-compatible unless a breaking change was explicitly agreed in Phase 0 Co-Design (`co-design-and-discovery.md`).
3. **Apply Language-Specific Clean Patterns**:
   - **Python 3**: Prefer shallow stateless functions + `@dataclass` / Pydantic schemas over ceremonial `*Manager`/`*Service` wrappers; use `from __future__ import annotations`, explicit exception handling, `pathlib.Path`, and stdlib-first Santa-safe dependencies.
   - **POSIX / Bash Shell**: Keep functions small with `local` variables, double-quoted expansions, `mktemp` + `trap` cleanup, and guard `set -euo pipefail` so sourced libraries never terminate interactive shells; avoid GNU-only flags (`sed -i`, `date -d`, `readlink -f`).
   - **Vanilla JS / HTML / CSS**: Extract pure data formatters/sorters away from DOM mutation; replace inline event attributes with delegated listeners; consolidate repeated colors/spacing into `:root` CSS custom properties with dark-mode parity; keep Zero-Build Santa-safe delivery intact.
4. **Verify Deterministically After Every Slice**:
   ```bash
   # 1. Targeted fast test iteration
   python3 -m unittest <target_test_module> -v

   # 2. AST complexity & god-file regression check on refactored Python modules
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-code-design-refactor/scripts/complexity_audit.py \
     <MODIFIED_PYTHON_FILES> --json

   # 3. 5-language static QA, Zero-PII & Santa Lockdown audit
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py \
     --repo <TARGET_REPO_DIR> --strict

   # 4. Full pre-commit & local gate before staging for agy-code-pr-reviewer
   pre-commit run --all-files
   ```

---

## Phase 3: Systematic Debugging & Root-Cause Protocol

When diagnosing non-trivial failures, flaky state transitions, or regressions, follow this strict protocol before writing fixes:

1. **4-Phase Root-Cause Analysis (RCA)**:
   - **Phase A — Reproduce & Gather Evidence**: Capture the exact failing command, inputs, exit code, stack trace, and state files deterministically (zero guessing).
   - **Phase B — Root-Cause Tracing**: Trace backward from the failure site through call and data-flow boundaries to the exact origin where invalid state or assumptions entered — never patch symptoms downstream.
   - **Phase C — Minimal Hypothesis & Failing Regression Test (`RED`)**: State one falsifiable hypothesis and encode it as a failing hermetic test (`Name the Break`) before editing production code.
   - **Phase D — Targeted Fix & Verify (`GREEN`)**: Apply the minimal root fix, confirm `RED -> GREEN`, and run `project_audit.py --strict` + `selective_test_runner.py`.
2. **3-Failure Architecture Stop**: If **3 consecutive fix attempts** fail or expose new breakages, **STOP patching symptoms**. Halt further edits, document what failed, and escalate (`BLOCKED` / Design Contract review) to re-evaluate the underlying architecture or boundary assumptions.
3. **4-Layer Defense-in-Depth**: Guard critical invariants at four distinct layers where applicable:
   - **Layer 1 — Boundary / Input Validation**: Reject malformed arguments, paths (`.resolve()`), or schemas at CLI/API/parser entrypoints.
   - **Layer 2 — Domain Invariant Guard**: Enforce state-machine preconditions in pure domain functions before side effects.
   - **Layer 3 — Persistence & Atomic Integrity**: Serialize mutations under `file_lock` with atomic writes (`0600`) and SHA/version checks.
   - **Layer 4 — Regression & Tier-0 Static Gate**: Lock the invariant with a hermetic unit test and deterministic `project_audit.py` check.

---

## Phase 4: Guardrails Against Over-Engineering

- **Do NOT invent speculative frameworks**: Extract a shared helper only when there is concrete duplication or a real testability boundary — never add single-implementation abstract base classes or empty passthrough layers.
- **Do NOT duplicate native review tools**: Use `agy-code-pr-reviewer` (`Model="pro"`) for Maker-Checker PR gate validation and `coderabbit` (`code-review` / `autofix`) when reviewing PR threads.
- **Preserve Zero-PII & Santa Compliance**: Never introduce third-party binary linters/bundlers (`ruff`, `uv`, `esbuild`, `mise`, `curl|sh`) or machine-local `/Users/<user>/` paths during a refactor.
