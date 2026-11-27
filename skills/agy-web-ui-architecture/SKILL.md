---
name: agy-web-ui-architecture
description: Actionable workflow for designing, modularizing, and auditing Vanilla-First Zero-Build web interfaces (ES2022+ JavaScript, semantic HTML5, CSS3 design tokens) under Santa Lockdown constraints. Use when building new web UI pages/components, decomposing monolithic JS/CSS files, auditing XSS/CSP/a11y/theme-token quality, or preparing frontend PRs without introducing unapproved Node binary bundlers.
---

# Vanilla Web UI Architecture & Audit Workflow (`agy-web-ui-architecture`)

Provides a repeatable engineering workflow for building and evolving clean, secure, accessible web interfaces using **Vanilla-First, Zero-Build `HTML5` + `CSS3` + `ES2022+` JavaScript** (100% Google Santa Lockdown safe; honors explicit Node/framework stacks only when already declared by a repository).

> Passive coding rules live in [`web-frontend-standards.md`]($HOME/.gemini/config/plugins/agy-dev-plugin/rules/web-frontend-standards.md). Use this skill for active UI design, decomposition, and pre-PR verification workflows.

---

## Phase 1: Architecture & Modular Decomposition Playbook

When creating a new web UI or splitting a growing single-file SPA (`dashboard.js`, `styles.css`, `index.html`):

1. **Preserve Santa-Safe Zero-Build Delivery**:
   - Serve static ES modules (`<script type="module" src="...">`) or ordered classic scripts directly from the Python/HTTP backend — never add `package.json`, `esbuild`, `swc`, `biome`, `tailwindcss` CLI, or `sass-embedded` (Santa Lockdown blocks unsigned native binaries).
   - Keep external CDN assets to zero where possible; if an external `<script>` or `<link rel="stylesheet">` is unavoidable, pin an exact version with `integrity="sha384-..." crossorigin="anonymous"`.
2. **Decompose by Responsibility (5 Clean Layers)**:
   | Layer | Responsibility | Rule of Thumb |
   | :--- | :--- | :--- |
   | **1. Tokens & Base CSS** | `:root` custom properties, `[data-theme="dark"]` parity, reset, typography, `:focus-visible`, `prefers-reduced-motion` | Zero hardcoded component hex values outside token definitions |
   | **2. Pure Formatters & State** | Date/age calculators, sorting, filtering, URL sanitizers, state reducers | 100% pure functions with zero DOM references; easiest to unit-test |
   | **3. API Transport Client** | Typed `fetch()` wrappers, `response.ok` handling, `AbortController` timeouts, auth headers | Centralized error normalization; never scatter raw `fetch()` inside renderers |
   | **4. View Renderers** | Deterministic HTML/DOM builders using `escapeHtml()` or `document.createElement()` | Never trigger side-effect mutations during pure render passes |
   | **5. Event Wiring & Controllers** | Delegated click/keyboard/form listeners attached once to stable root containers | Clean up transient listeners/timers via `AbortController` |
3. **Incremental Extraction for Monolithic SPAs**:
   - Never big-bang rewrite a large working SPA. Extract one vertical slice at a time (e.g., pure formatters -> API transport -> feature view renderer) while preserving global/contract compatibility and adding characterization assertions after each slice.

---

## Phase 2: Pre-PR Security, Accessibility & UX Checklist

Verify every frontend change against these concrete gates before staging:

- [ ] **XSS & Injection Safety**: Zero `eval()`, `new Function()`, `document.write()`, or `javascript:` URIs; every dynamic string inside HTML templates passes through `escapeHtml()`; dynamic `href`/`src` attributes validate allowed schemes (`https:`, `http:`, `/`).
- [ ] **CSP-Friendly Markup**: Zero inline event handlers (`onclick=`, `onerror=`, `onchange=`) in HTML; zero API keys or customer PII embedded in HTML/JS.
- [ ] **Semantic HTML5 & Keyboard Access**: Unique `id`s; native `<button type="button">` for actions (no clickable `<div>` without full keyboard/ARIA support); `<label for="...">` or `aria-label` on inputs; `alt` on every `<img>` (`alt=""` if decorative); `rel="noopener noreferrer"` on every `target="_blank"`.
- [ ] **Layout & Theme Parity**: Data tables wrapped in `overflow-x: auto` with `white-space: nowrap` on key/date/status badges and `font-variant-numeric: tabular-nums` on numbers; verified in both Light and Dark themes without contrast regressions; avoid `!important` sprawl.

---

## Phase 3: Deterministic Static Audit & Native Browser Delegation

1. **Run Zero-LLM Static Verification (`project_audit.py`)**:
   ```bash
   python3 ~/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py \
     --repo <TARGET_REPO_DIR> --format text
   ```
   - Resolve all hard `error` findings (`sec:secret-pattern`, `sec:local-user-path`, `js:unsafe-eval`, `js:document-write`, `js:javascript-uri`, `html:javascript-uri`, `html:duplicate-id`, `css:unbalanced-braces`) and review every `warning` (`html:img-missing-alt`, `html:blank-target-noopener`, `html:inline-event-handler`, `html:button-missing-type`, `html:external-script-no-sri`, `js:debugger-statement`, `css:important-overuse`).
2. **Add Hermetic Offline Contract Tests (`tests/`)**:
   - Write offline Python tests verifying HTML structure/IDs, CSS class & dark-theme token coverage, and JS symbol/behavior contracts using synthetic mock payloads (`AcmeCorp`, `Globex`).
3. **Delegate Live Browser Inspection to Native Plugins (Zero Duplication)**:
   - For interactive DOM verification, layout screenshots, WCAG accessibility audits, Largest Contentful Paint (LCP), or JS memory leaks inside managed dev repositories or `pr-worktree-*` branches, opt into `chrome-devtools-plugin` via workspace `.agents/plugins.json` (avoiding global ambient activation) and invoke its skills (`chrome-devtools`, `a11y-debugging`, `debug-optimize-lcp`, `memory-leak-debugging`) rather than writing custom browser probes (never use CDP for connector data extraction or scheduled actions).
