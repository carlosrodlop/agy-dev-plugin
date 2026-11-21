---
trigger: glob
globs: "**/*.js, **/*.mjs, **/*.html, **/*.css"
description: Vanilla-First & Zero-Build JavaScript (ES2022+), semantic HTML5/accessibility, CSS3 design tokens, XSS/CSP security, and Santa Lockdown frontend engineering standards.
---

# Web Frontend Engineering Standards (`web-frontend-standards.md`)

## 1. Vanilla-First & Santa Lockdown Toolchain Policy
- **Vanilla-First & Zero-Build Default**: Default web UIs to standards-based `ES2022+` JavaScript, semantic `HTML5`, and modern `CSS3` served directly by the backend with **zero build step** (`npm`/`npx` and native binary bundlers like `esbuild`, `swc`, `biome`, `tailwindcss` CLI, and `sass-embedded` download unsigned compiled binaries that Google Santa Lockdown blocks on corporate workstations).
- **Explicit Framework Opt-In Only**: Node, TypeScript, or frontend frameworks are allowed only when an existing repository explicitly declares them in its project manifest and CI. Never introduce a `package.json` or bundler into a Zero-Build repository without explicit user agreement.
- **Don't Reinvent the Wheel (Web Platform APIs & Allowed Non-Binary Libraries)**: Before hand-rolling complex UI primitives, parsers, date/number formatters, sanitizers, or charts, check whether native **Web Platform APIs** (`<dialog>`, Popover API, `URL`, `Intl`, `AbortController`, `crypto.subtle`, CSS Grid/Container Queries) or an audited **zero-build non-binary JS/CSS library** (vendored locally as a static module or SRI-pinned — never a native binary bundler) already solves the need cleanly, and adopt it instead of custom boilerplate.
- **Local Assets & SRI Supply-Chain Hygiene**: Prefer locally hosted static assets served by the application. If an external `<script>` or `<link rel="stylesheet">` CDN URL is unavoidable, pin an exact version and require Subresource Integrity (`integrity="sha384-..." crossorigin="anonymous"`).

## 2. JavaScript (`ES2022+`) Craft, Modularity & Security
- **Strict Mode & Modular Boundaries**: Use `"use strict";` (or ES module scope) and organize non-trivial apps into cohesive modules/sections separating **state**, **API transport**, **pure formatters/calculations**, **DOM renderers**, and **event wiring**. Avoid growing monolithic single-file scripts; when adding major features, extract pure helper functions that can be verified deterministically.
- **XSS & Injection Defense**:
  - Never use `eval()`, `new Function()`, `document.write()`, or `javascript:` URIs (blocked as hard errors by `project_audit.py`).
  - Prefer `textContent`, `document.createElement()`, and structured DOM APIs over raw HTML string concatenation. Whenever template rendering via `innerHTML` is required, escape every interpolated untrusted/dynamic value through a single dedicated `escapeHtml()` helper and validate URL schemes (`http:` / `https:` / relative `/`) before assigning `href` or `src`.
- **Event & Async Discipline**:
  - Prefer event delegation on stable containers over attaching duplicate listeners inside repeated re-renders; clean up dynamic listeners/timers with `AbortController` to prevent memory leaks.
  - Always check `response.ok` on `fetch()`, handle non-JSON error payloads gracefully, avoid silent empty `catch {}` blocks, and never leave `debugger;` statements or hardcoded tokens/local `/Users/<user>/` paths in tracked JS.
- **Zero-PII & Zero-Key HTML/JS**: Never embed API keys, OAuth client secrets, or live customer PII in static HTML/JS bundles.

## 3. Semantic HTML5 & Accessibility (WCAG)
- **Native Semantics First**: Structure documents with landmarks (`<header>`, `<nav>`, `<main>`, `<section>`, `<aside>`) and heading hierarchy (`<h1>`..`<h4>`). Use `<button type="button">` for actions and `<a href="...">` for navigation — never clickable `<div>`/`<span>` elements without full keyboard/role semantics.
- **Valid Attributes & Form Controls**:
  - Keep every `id` attribute globally unique within the document.
  - Pair every `<input>`, `<select>`, and `<textarea>` with a `<label for="...">` (or `aria-label` when visually compact).
  - Include `alt` on every `<img>` (`alt=""` for purely decorative images) and explicit `type="button"|"submit"|"reset"` on every `<button>`.
  - Add `rel="noopener noreferrer"` on every `target="_blank"` link to prevent tabnabbing.
- **CSP-Friendly Markup**: Never use inline event attributes (`onclick=`, `onchange=`, `onerror=`) or `javascript:` hrefs; attach behavior cleanly from JS.

## 4. CSS3 Design Tokens, Layout & Maintainability
- **Design Tokens & Theme Parity**: Define colors, spacing, radii, typography, and elevation via `:root` CSS custom properties with explicit light/dark parity (`[data-theme="dark"]` and/or `@media (prefers-color-scheme: dark)`). Avoid scattering raw hex values across component rules.
- **Modern Responsive Layout**: Use CSS Grid and Flexbox rather than brittle floats or fixed pixel widths. Wrap data tables in `overflow-x: auto` containers, use `white-space: nowrap` on compact badge/key/date columns, and apply `font-variant-numeric: tabular-nums` to numeric KPIs, timestamps, and aligned counters.
- **Low-Specificity & Accessible Interaction**: Keep selectors flat and class-driven (avoid deep nesting and `!important` sprawl). Always provide clear `:focus-visible` outlines for keyboard users and respect `@media (prefers-reduced-motion: reduce)` for transitions and animations.

## 5. Verification & Native Tool Delegation
- **Deterministic Static Gate**: Run [`project_audit.py`]($HOME/.gemini/config/plugins/agy-dev-plugin/skills/agy-project-standards/scripts/project_audit.py) to verify JS/HTML/CSS security, Zero-PII, duplicate IDs, balanced CSS braces, and a11y/hygiene rules with zero LLM tokens.
- **Hermetic Tests & Live Browser Audits**: Add offline Python contract/HTML/CSS assertions in `tests/` for structural invariants and security rules. For live DOM rendering, visual layout, WCAG accessibility, Core Web Vitals (LCP), or memory leak inspection, use the installed `chrome-devtools-plugin` skills (`chrome-devtools`, `a11y-debugging`, `debug-optimize-lcp`, `memory-leak-debugging`) rather than reinventing browser tooling.
