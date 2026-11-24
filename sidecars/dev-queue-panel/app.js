/*
 * Antigravity 2.0 Dev Queue Sidecar UI Panel Controller (ES2022+, Zero-Build, XSS-Safe).
 */
(function () {
  "use strict";

  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const POLL_INTERVAL_MS = 1500;
  const FETCH_TIMEOUT_MS = 6000;
  const MAX_CONSUMED_INTENTS = 64;
  const DEFAULT_MAX_REPO_SLOTS = 3;
  const MAX_REPO_SLOTS_CAP = 4;

  /* -------------------- 1. Pure Formatters & Validators -------------------- */

  function isValidUuid(value) {
    return UUID_RE.test(String(value || "").trim());
  }

  function sanitizeHttpUrl(rawUrl) {
    const cleaned = String(rawUrl || "").trim();
    if (!cleaned) {
      return "";
    }
    try {
      const parsed = new URL(cleaned);
      if (parsed.protocol === "https:" || parsed.protocol === "http:") {
        return parsed.href;
      }
    } catch (_err) {
      return "";
    }
    return "";
  }

  function sanitizeFileUri(rawUri) {
    const cleaned = String(rawUri || "").trim();
    if (!cleaned) {
      return "";
    }
    try {
      const parsed = new URL(cleaned);
      if (parsed.protocol === "file:") {
        return parsed.href;
      }
    } catch (_err) {
      return "";
    }
    return "";
  }

  function isNavIntentFresh(intent, nowEpochSec) {
    if (!intent || typeof intent !== "object" || Boolean(intent.acknowledged)) {
      return false;
    }
    const intentId = String(intent.intent_id || "").trim();
    const convId = String(intent.conversation_id || "").trim();
    if (!intentId || !isValidUuid(convId)) {
      return false;
    }
    const createdEpoch = Number(intent.created_at_epoch || 0);
    const ttlSec = Math.max(5, Number(intent.ttl_seconds || 60));
    const nowSec = Number.isFinite(nowEpochSec) ? nowEpochSec : Date.now() / 1000;
    if (!Number.isFinite(createdEpoch) || createdEpoch <= 0) {
      return false;
    }
    return nowSec - createdEpoch <= ttlSec;
  }

  function matchesCardFilter(card, needle) {
    if (!needle) {
      return true;
    }
    const q = needle.toLowerCase();
    const parts = [
      card.conversation_id,
      card.display_id,
      card.hotkey,
      card.effective_status,
      card.title,
      card.sidebar_title,
      card.repos_label,
      card.notes,
      card.stage_label,
      ...(Array.isArray(card.repo_slugs) ? card.repo_slugs : []),
    ];
    return parts.some((p) => String(p || "").toLowerCase().includes(q));
  }

  function matchesRepoFilter(repo, needle) {
    if (!needle) {
      return true;
    }
    const q = needle.toLowerCase();
    if (
      String(repo.slug || "").toLowerCase().includes(q) ||
      String(repo.label || "").toLowerCase().includes(q) ||
      String(repo.state_tag || "").toLowerCase().includes(q)
    ) {
      return true;
    }
    const slots = Array.isArray(repo.slots) ? repo.slots : [];
    return slots.some(
      (s) =>
        String(s.branch_name || "").toLowerCase().includes(q) ||
        String(s.display_id || "").toLowerCase().includes(q) ||
        String(s.status || "").toLowerCase().includes(q),
    );
  }

  /* -------------------- 2. Theme Parity Bridge -------------------- */

  function syncThemeFromHost() {
    if (typeof document === "undefined" || !document.documentElement) {
      return;
    }
    const probe = document.createElement("span");
    probe.style.cssText = "position:absolute;visibility:hidden;color:var(--background, #f7f8fa)";
    document.documentElement.appendChild(probe);
    const computed = getComputedStyle(probe).color || "";
    probe.remove();
    const nums = (computed.match(/[\d.]+/g) || []).map(Number);
    if (nums.length < 3) {
      return;
    }
    const scale = computed.startsWith("color(") ? 1 : 255;
    const [r, g, b] = nums.slice(0, 3).map((v) => v / scale);
    const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    const nextTheme = luminance < 0.5 ? "dark" : "light";
    if (document.documentElement.dataset.theme !== nextTheme) {
      document.documentElement.dataset.theme = nextTheme;
    }
  }

  /* -------------------- 3. API Transport -------------------- */

  const queryToken =
    typeof window !== "undefined" ? new URLSearchParams(window.location.search).get("token") || "" : "";

  async function apiFetch(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    try {
      const headers = new Headers(options.headers || {});
      if (options.body && !headers.has("Content-Type")) {
        headers.set("Content-Type", "application/json");
      }
      if (queryToken && !headers.has("X-Sidecar-Token")) {
        headers.set("X-Sidecar-Token", queryToken);
      }
      const fetcher =
        typeof window !== "undefined" && window.sidecar && typeof window.sidecar.fetch === "function"
          ? window.sidecar.fetch.bind(window.sidecar)
          : fetch;
      const response = await fetcher(path, {
        ...options,
        headers,
        signal: controller.signal,
      });
      let data = null;
      try {
        data = await response.json();
      } catch (_parseErr) {
        data = null;
      }
      if (!response.ok) {
        const msg = (data && data.error) || `HTTP ${response.status}`;
        const err = new Error(String(msg));
        err.status = response.status;
        throw err;
      }
      return data || {};
    } finally {
      clearTimeout(timer);
    }
  }

  /* -------------------- 4. XSS-Safe DOM Builders -------------------- */

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null && text !== "") {
      node.textContent = String(text);
    }
    return node;
  }

  function buildActionButton(label, action, dataset, primary) {
    const btn = el("button", primary ? "dq-btn dq-btn-primary" : "dq-btn", label);
    btn.type = "button";
    btn.dataset.action = action;
    for (const [k, v] of Object.entries(dataset || {})) {
      if (v !== undefined && v !== null && v !== "") {
        btn.dataset[k] = String(v);
      }
    }
    return btn;
  }

  function buildExternalLink(label, rawUrl, primary) {
    const safeUrl = sanitizeHttpUrl(rawUrl);
    if (!safeUrl) {
      return null;
    }
    const anchor = el("a", primary ? "dq-link-btn dq-link-primary" : "dq-link-btn", label);
    anchor.href = safeUrl;
    anchor.target = "_blank";
    anchor.rel = "noopener noreferrer";
    return anchor;
  }

  function sortedRepoSlots(repo) {
    const slots = Array.isArray(repo.slots) ? repo.slots.slice() : [];
    slots.sort((a, b) => (Number(a.slot_id) || 1) - (Number(b.slot_id) || 1));
    return slots;
  }

  function repoSlotOccupancy(repo, slots) {
    if (repo.slot_occupancy) {
      return String(repo.slot_occupancy);
    }
    const maxRaw = Number(repo.max_slots);
    const maxSlots =
      Number.isFinite(maxRaw) && maxRaw >= 1 ? Math.min(Math.floor(maxRaw), MAX_REPO_SLOTS_CAP) : DEFAULT_MAX_REPO_SLOTS;
    const active = slots.length || 1;
    return `${active}/${Math.max(active, maxSlots)}`;
  }

  function renderRepoCard(repo) {
    const cardClass = repo.is_stalled ? "dq-repo-card is-stalled" : "dq-repo-card";
    const root = el("article", cardClass);
    const slots = sortedRepoSlots(repo);

    const top = el("div", "dq-repo-top");
    const leftBadges = el("div", "dq-badges");
    leftBadges.appendChild(el("strong", "", repo.label || repo.slug));
    if (repo.locked) {
      leftBadges.appendChild(el("span", "dq-badge dq-badge-mono", `[${repoSlotOccupancy(repo, slots)}]`));
    }
    top.appendChild(leftBadges);

    const stateClass = !repo.locked
      ? "dq-badge dq-badge-free"
      : repo.is_stalled
        ? "dq-badge dq-badge-danger"
        : repo.is_human_ready
          ? "dq-badge dq-badge-pr"
          : "dq-badge dq-badge-warn";
    top.appendChild(el("span", stateClass, repo.state_tag || (repo.locked ? "LOCKED" : "FREE")));
    root.appendChild(top);

    for (const slot of slots) {
      const slotLabel = slot.slot_label || `S${slot.slot_id}`;
      const slotBits = [`${slotLabel}: ${slot.status || "ACTIVE"}`];
      if (slot.idle_label) {
        slotBits.push(slot.is_stalled ? `STALL ${slot.idle_label}` : `idle ${slot.idle_label}`);
      }
      if (slot.display_id) {
        slotBits.push(slot.display_id);
      }
      root.appendChild(el("p", "dq-card-sub", slotBits.join(" · ")));
    }

    const actions = el("div", "dq-actions");
    if (repo.pr_url) {
      const prLink = buildExternalLink("↗ Open PR", repo.pr_url, true);
      if (prLink) {
        actions.appendChild(prLink);
      }
    } else if (repo.pulls_url) {
      const pullsLink = buildExternalLink("↗ PRs", repo.pulls_url, false);
      if (pullsLink) {
        actions.appendChild(pullsLink);
      }
    }
    for (const slot of slots) {
      if (slot.is_uuid) {
        const slotLabel = slot.slot_label || `S${slot.slot_id}`;
        actions.appendChild(
          buildActionButton(
            `${slotLabel} Chat`,
            "open-conv",
            { convId: slot.conversation_id, label: `${slotLabel} (${slot.display_id})` },
            false,
          ),
        );
      }
    }
    if (actions.childNodes.length > 0) {
      root.appendChild(actions);
    }
    return root;
  }

  function renderQueueCard(card, kind) {
    let extraCls = "";
    if (kind === "hitl") {
      extraCls = " is-hitl";
    } else if (kind === "pr") {
      extraCls = " is-pr";
    } else if (card.is_stalled) {
      extraCls = " is-stalled";
    }
    const root = el("article", `dq-card${extraCls}`);

    const top = el("div", "dq-card-top");
    const badges = el("div", "dq-badges");
    if (card.hotkey) {
      badges.appendChild(el("span", "dq-badge dq-badge-hitl dq-badge-mono", `[${card.hotkey}]`));
    }
    const prioText =
      card.effective_priority && card.effective_priority !== card.priority
        ? `P${card.effective_priority}<-P${card.priority}`
        : `P${card.priority}`;
    badges.appendChild(el("span", "dq-badge dq-badge-mono", prioText));
    if (card.repos_label) {
      badges.appendChild(el("span", "dq-badge", card.repos_label));
    }
    if (card.display_id) {
      badges.appendChild(el("span", "dq-badge dq-badge-mono", card.display_id));
    }
    top.appendChild(badges);

    if (card.progress_label) {
      const progCls = card.is_stalled
        ? "dq-badge dq-badge-danger"
        : kind === "pr"
          ? "dq-badge dq-badge-pr"
          : "dq-badge";
      top.appendChild(el("span", progCls, card.progress_label));
    } else {
      top.appendChild(el("span", "dq-badge", String(card.effective_status || "").toUpperCase()));
    }
    root.appendChild(top);

    const primaryTitle = card.sidebar_title || card.title || card.conversation_id;
    root.appendChild(el("h3", "dq-card-title", primaryTitle));

    if (card.sidebar_title && card.title && card.sidebar_title !== card.title) {
      root.appendChild(el("p", "dq-card-sub", card.title));
    }
    if (kind === "hitl" && card.notes && card.notes !== card.title) {
      root.appendChild(el("p", "dq-card-sub", card.notes));
    }
    if (card.stage_label) {
      root.appendChild(el("p", "dq-card-sub", `Stage: ${card.stage_label}`));
    }
    if (Array.isArray(card.depends_on) && card.depends_on.length > 0) {
      root.appendChild(el("p", "dq-card-sub", `Depends on: ${card.depends_on.join(", ")}`));
    }

    const actions = el("div", "dq-actions");
    if (card.pr_url) {
      const prAnchor = buildExternalLink(card.pr_label || "↗ Open PR", card.pr_url, true);
      if (prAnchor) {
        actions.appendChild(prAnchor);
      }
    }
    if (card.is_uuid) {
      actions.appendChild(
        buildActionButton(
          kind === "hitl" ? "Open Blocked Chat" : "Open Chat",
          "open-conv",
          { convId: card.conversation_id, label: primaryTitle },
          kind === "hitl",
        ),
      );
    }
    if (card.worker_is_uuid && card.worker_cid !== card.conversation_id) {
      actions.appendChild(
        buildActionButton(
          "Worker",
          "open-conv",
          { convId: card.worker_cid, label: `Worker ${card.worker_cid.slice(0, 8)}` },
          false,
        ),
      );
    }
    if (card.reviewer_is_uuid) {
      actions.appendChild(
        buildActionButton(
          "Reviewer",
          "open-conv",
          { convId: card.reviewer_cid, label: `Reviewer ${card.reviewer_cid.slice(0, 8)}` },
          false,
        ),
      );
    }
    const artifacts = Array.isArray(card.artifacts) ? card.artifacts : [];
    for (const art of artifacts) {
      if (art && art.filename && card.is_uuid) {
        actions.appendChild(
          buildActionButton(
            `📄 ${art.label || art.filename}`,
            "open-artifact",
            { convId: card.conversation_id, filename: art.filename },
            false,
          ),
        );
      }
    }
    if (actions.childNodes.length > 0) {
      root.appendChild(actions);
    }
    return root;
  }

  /* -------------------- 5. State, Actions & nav_intent Controller -------------------- */

  const uiState = {
    snapshot: null,
    etag: "",
    sectionFilter: "all",
    query: "",
    inFlight: false,
    pauseBusy: false,
    toastTimer: null,
    consumedIntentIds: new Set(),
  };

  function formatPauseMeta(orch) {
    if (!orch || typeof orch !== "object") {
      return "Graceful Drain · ongoing tasks finish cleanly";
    }
    const parts = [];
    const pausedAt = String(orch.paused_at || "").trim();
    if (pausedAt.length >= 19) {
      parts.push(`Paused ${pausedAt.slice(11, 16)}Z`);
    }
    parts.push("Graceful Drain");
    const reason = String(orch.reason || "").trim();
    if (reason) {
      parts.push(reason);
    }
    return parts.join(" · ");
  }

  function renderOrchestrationControl(orch) {
    const ctrl = orch && typeof orch === "object" ? orch : {};
    const paused = Boolean(ctrl.paused);
    const phase = String(ctrl.phase || (paused ? "paused" : "active"));
    const ongoingCount = Number(ctrl.ongoing_count || 0);

    const pauseBtn = document.getElementById("dq-pause-btn");
    if (pauseBtn) {
      pauseBtn.disabled = Boolean(uiState.pauseBusy);
      pauseBtn.classList.toggle("is-paused", paused);
      pauseBtn.setAttribute("aria-pressed", paused ? "true" : "false");
      if (paused) {
        pauseBtn.textContent = uiState.pauseBusy ? "Resuming…" : "▶ Continue Dev";
        pauseBtn.title = "Continue orchestration and auto-wake orchestrator";
      } else {
        pauseBtn.textContent = uiState.pauseBusy ? "Pausing…" : "⏸ Pause Dev";
        pauseBtn.title = "Pause new dispatches (Graceful Drain: ongoing active-lock tasks finish cleanly)";
      }
    }

    const bannerEl = document.getElementById("dq-pause-banner");
    const titleEl = document.getElementById("dq-pause-banner-title");
    const metaEl = document.getElementById("dq-pause-banner-meta");
    const bannerBtn = document.getElementById("dq-banner-resume-btn");
    if (bannerEl) {
      bannerEl.classList.toggle("dq-hidden", !paused);
    }
    if (titleEl && paused) {
      titleEl.textContent = `⏸ ${ctrl.banner_text || "PAUSED — 0 new dispatches · Watchdog silent"}`;
    }
    if (metaEl && paused) {
      metaEl.textContent = formatPauseMeta(ctrl);
    }
    if (bannerBtn) {
      bannerBtn.disabled = Boolean(uiState.pauseBusy);
      bannerBtn.textContent = uiState.pauseBusy ? "Resuming…" : "▶ Continue Dev";
    }

    const pillEl = document.getElementById("dq-live-pill");
    const liveTxt = document.getElementById("dq-live-text");
    if (pillEl && !pillEl.classList.contains("is-error")) {
      pillEl.classList.toggle("is-paused", paused);
      if (liveTxt) {
        if (!paused) {
          liveTxt.textContent = "Live";
        } else if (phase === "draining" && ongoingCount > 0) {
          liveTxt.textContent = `Draining (${ongoingCount})`;
        } else {
          liveTxt.textContent = "Paused";
        }
      }
    }
  }

  async function setOrchestrationPaused(targetPaused) {
    if (uiState.pauseBusy) {
      return;
    }
    uiState.pauseBusy = true;
    renderOrchestrationControl(uiState.snapshot && uiState.snapshot.orchestration);
    try {
      const endpoint = targetPaused ? "/api/orchestration/pause" : "/api/orchestration/resume";
      const res = await apiFetch(endpoint, {
        method: "POST",
        body: JSON.stringify({}),
      });
      if (uiState.snapshot && res.orchestration) {
        uiState.snapshot.orchestration = res.orchestration;
      }
      renderOrchestrationControl(res.orchestration);
      if (targetPaused) {
        const banner = (res.orchestration && res.orchestration.banner_text) || "PAUSED — 0 new dispatches";
        showToast(`⏸ ${banner}`);
      } else {
        showToast("▶ Orchestration resumed — waking orchestrator…");
      }
      uiState.etag = "";
      await pollState(true);
    } catch (err) {
      showToast(`Unable to ${targetPaused ? "pause" : "resume"} orchestration: ${err.message || "error"}`);
    } finally {
      uiState.pauseBusy = false;
      renderOrchestrationControl(uiState.snapshot && uiState.snapshot.orchestration);
    }
  }

  function rememberConsumedIntent(intentId) {
    uiState.consumedIntentIds.add(intentId);
    if (uiState.consumedIntentIds.size > MAX_CONSUMED_INTENTS) {
      const oldest = uiState.consumedIntentIds.values().next().value;
      uiState.consumedIntentIds.delete(oldest);
    }
  }

  function showToast(message) {
    const toastEl = document.getElementById("dq-toast");
    if (!toastEl) {
      return;
    }
    toastEl.textContent = String(message || "");
    toastEl.classList.remove("dq-hidden");
    if (uiState.toastTimer) {
      clearTimeout(uiState.toastTimer);
    }
    uiState.toastTimer = setTimeout(() => {
      toastEl.classList.add("dq-hidden");
    }, 3600);
  }

  function setErrorBanner(message) {
    const errEl = document.getElementById("dq-error");
    const pillEl = document.getElementById("dq-live-pill");
    const liveTxt = document.getElementById("dq-live-text");
    if (!errEl) {
      return;
    }
    if (message) {
      errEl.textContent = String(message);
      errEl.classList.remove("dq-hidden");
      if (pillEl) {
        pillEl.classList.add("is-error");
      }
      if (liveTxt) {
        liveTxt.textContent = "Disconnected";
      }
    } else {
      errEl.textContent = "";
      errEl.classList.add("dq-hidden");
      if (pillEl) {
        pillEl.classList.remove("is-error");
      }
      const orch = uiState.snapshot && uiState.snapshot.orchestration;
      if (orch && orch.paused) {
        renderOrchestrationControl(orch);
      } else if (liveTxt) {
        liveTxt.textContent = "Live";
      }
    }
  }

  function triggerConversationToggle(convId, labelText) {
    const cleanId = String(convId || "").trim();
    if (!isValidUuid(cleanId)) {
      showToast("Conversation does not have a valid UUID.");
      return false;
    }
    const canToggle =
      typeof window !== "undefined" &&
      window.sidecar &&
      window.sidecar.ui &&
      typeof window.sidecar.ui.toggleConversation === "function" &&
      window.parent !== window;

    if (window.sidecar && window.sidecar.ui && typeof window.sidecar.ui.toggleConversation === "function") {
      window.sidecar.ui.toggleConversation(cleanId);
      if (canToggle) {
        showToast(`Switched to ${labelText || cleanId.slice(0, 8)}`);
        return true;
      }
    }
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(labelText || cleanId).catch(() => {});
    }
    showToast(`Copied '${labelText || cleanId.slice(0, 8)}' (open inside Antigravity Desktop to switch tabs)`);
    return false;
  }

  async function openArtifactInAuxPane(payload) {
    try {
      const res = await apiFetch("/api/artifact-uri", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      const safeUri = sanitizeFileUri(res.uri);
      if (!safeUri) {
        showToast("Artifact file is unavailable.");
        return;
      }
      if (window.sidecar && window.sidecar.ui && typeof window.sidecar.ui.toggleAuxPane === "function") {
        window.sidecar.ui.toggleAuxPane({ tabType: "fileView", uri: safeUri });
        showToast(`Opened ${res.label || "artifact"}`);
      } else {
        showToast("Opening file view requires Antigravity Desktop.");
      }
    } catch (err) {
      showToast(`Unable to open artifact: ${err.message || "not found"}`);
    }
  }

  async function maybeConsumeNavIntent(intent) {
    if (!isNavIntentFresh(intent)) {
      return;
    }
    const intentId = String(intent.intent_id || "").trim();
    if (uiState.consumedIntentIds.has(intentId)) {
      return;
    }
    const inHostFrame =
      typeof window !== "undefined" &&
      window.parent &&
      window.parent !== window &&
      window.sidecar &&
      window.sidecar.ui &&
      typeof window.sidecar.ui.toggleConversation === "function";
    if (!inHostFrame) {
      return;
    }

    rememberConsumedIntent(intentId);
    const convId = String(intent.conversation_id || "").trim();
    const titleLabel = String(intent.sidebar_title || intent.display_id || convId.slice(0, 8));
    window.sidecar.ui.toggleConversation(convId);
    showToast(`Switched to [${intent.hotkey || "HITL"}] ${titleLabel}`);

    try {
      await apiFetch("/api/nav-intent/ack", {
        method: "POST",
        body: JSON.stringify({ intent_id: intentId }),
      });
    } catch (_ackErr) {
      uiState.consumedIntentIds.delete(intentId);
    }
  }

  function renderSectionList(secId, badgeId, listId, cards, kind, allowedByTab) {
    const secEl = document.getElementById(secId);
    const badgeEl = document.getElementById(badgeId);
    const listEl = document.getElementById(listId);
    if (!secEl || !badgeEl || !listEl) {
      return 0;
    }
    if (!allowedByTab || cards.length === 0) {
      secEl.classList.add("dq-hidden");
      listEl.replaceChildren();
      badgeEl.textContent = "0";
      return 0;
    }
    secEl.classList.remove("dq-hidden");
    badgeEl.textContent = String(cards.length);
    const frag = document.createDocumentFragment();
    for (const c of cards) {
      frag.appendChild(renderQueueCard(c, kind));
    }
    listEl.replaceChildren(frag);
    return cards.length;
  }

  function renderView() {
    const snap = uiState.snapshot;
    if (!snap) {
      return;
    }

    renderOrchestrationControl(snap.orchestration);

    const sections = snap.sections || {};
    const rawHitl = Array.isArray(sections.hitl_blocked) ? sections.hitl_blocked : [];
    const rawPr = Array.isArray(sections.pr_open) ? sections.pr_open : [];
    const rawActive = Array.isArray(sections.in_progress) ? sections.in_progress : [];
    const rawPending = Array.isArray(sections.pending) ? sections.pending : [];
    const rawRepos = Array.isArray(snap.repos) ? snap.repos : [];

    const totalOpen = rawHitl.length + rawPr.length + rawActive.length + rawPending.length;
    const setCount = (id, n) => {
      const node = document.getElementById(id);
      if (node) {
        node.textContent = String(n);
      }
    };
    setCount("dq-count-all", totalOpen);
    setCount("dq-count-hitl", rawHitl.length);
    setCount("dq-count-pr", rawPr.length);
    setCount("dq-count-run", rawActive.length);
    setCount("dq-count-pending", rawPending.length);

    const kpiHitl = document.getElementById("dq-kpi-hitl");
    if (kpiHitl) {
      kpiHitl.classList.toggle("has-items", rawHitl.length > 0);
    }
    const kpiPr = document.getElementById("dq-kpi-pr");
    if (kpiPr) {
      kpiPr.classList.toggle("has-items", rawPr.length > 0);
    }

    for (const btn of document.querySelectorAll(".dq-kpi[data-section-filter]")) {
      btn.classList.toggle("active", btn.dataset.sectionFilter === uiState.sectionFilter);
    }

    const orch = snap.orchestrator || {};
    const orchBtn = document.getElementById("dq-orch-btn");
    if (orchBtn) {
      if (orch.is_uuid) {
        orchBtn.classList.remove("dq-hidden");
        orchBtn.dataset.action = "open-conv";
        orchBtn.dataset.convId = orch.conversation_id;
        orchBtn.dataset.label = orch.sidebar_title || `Orchestrator (${orch.display_id})`;
        orchBtn.textContent = orch.liveness ? `orch:${orch.display_id} ${orch.liveness}` : `orch:${orch.display_id}`;
      } else {
        orchBtn.classList.add("dq-hidden");
      }
    }

    const boardBtn = document.getElementById("dq-board-btn");
    if (boardBtn) {
      const boardAvail = Boolean(snap.board && snap.board.available);
      boardBtn.classList.toggle("dq-hidden", !boardAvail);
    }

    const q = uiState.query.trim();
    const tab = uiState.sectionFilter;

    const hitlFiltered = rawHitl.filter((c) => matchesCardFilter(c, q));
    const prFiltered = rawPr.filter((c) => matchesCardFilter(c, q));
    const activeFiltered = rawActive.filter((c) => matchesCardFilter(c, q));
    const pendingFiltered = rawPending.filter((c) => matchesCardFilter(c, q));
    const reposFiltered = rawRepos.filter((r) => matchesRepoFilter(r, q));

    let shownCount = 0;
    shownCount += renderSectionList(
      "dq-sec-hitl",
      "dq-badge-hitl",
      "dq-list-hitl",
      hitlFiltered,
      "hitl",
      tab === "all" || tab === "hitl_blocked",
    );

    const repoSec = document.getElementById("dq-sec-repos");
    const repoBadge = document.getElementById("dq-badge-repos");
    const repoList = document.getElementById("dq-list-repos");
    if (repoSec && repoBadge && repoList) {
      if (tab === "all" && reposFiltered.length > 0) {
        repoSec.classList.remove("dq-hidden");
        repoBadge.textContent = String(reposFiltered.length);
        const rFrag = document.createDocumentFragment();
        for (const r of reposFiltered) {
          rFrag.appendChild(renderRepoCard(r));
        }
        repoList.replaceChildren(rFrag);
        shownCount += reposFiltered.length;
      } else {
        repoSec.classList.add("dq-hidden");
        repoList.replaceChildren();
        repoBadge.textContent = "0";
      }
    }

    shownCount += renderSectionList(
      "dq-sec-pr",
      "dq-badge-pr",
      "dq-list-pr",
      prFiltered,
      "pr",
      tab === "all" || tab === "pr_open",
    );
    shownCount += renderSectionList(
      "dq-sec-active",
      "dq-badge-active",
      "dq-list-active",
      activeFiltered,
      "active",
      tab === "all" || tab === "in_progress",
    );
    shownCount += renderSectionList(
      "dq-sec-pending",
      "dq-badge-pending",
      "dq-list-pending",
      pendingFiltered,
      "pending",
      tab === "all" || tab === "pending",
    );

    const emptySec = document.getElementById("dq-empty");
    if (emptySec) {
      emptySec.classList.toggle("dq-hidden", shownCount > 0);
    }

    const optHintEl = document.getElementById("dq-opt-hint");
    if (optHintEl) {
      optHintEl.textContent =
        snap.opt_hint || "Read-only navigator · [h#] terminal hotkeys auto-switch here";
    }
    const updEl = document.getElementById("dq-updated-at");
    if (updEl && snap.generated_at) {
      updEl.textContent = String(snap.generated_at).slice(11, 19) + "Z";
    }
  }

  async function pollState(forceFull = false) {
    if (uiState.inFlight) {
      return;
    }
    uiState.inFlight = true;
    try {
      const etagQuery = !forceFull && uiState.etag ? `?etag=${encodeURIComponent(uiState.etag)}` : "";
      const data = await apiFetch(`/api/state${etagQuery}`, { method: "GET" });
      setErrorBanner("");
      if (!data.unchanged) {
        uiState.snapshot = data;
        uiState.etag = String(data.etag || "");
        renderView();
      }
      if (data.nav_intent) {
        await maybeConsumeNavIntent(data.nav_intent);
      }
    } catch (err) {
      const status = err && err.status;
      if (status === 401 || status === 403) {
        setErrorBanner("Unauthorized: missing or invalid ANTIGRAVITY_SIDECAR_UI_TOKEN.");
      } else {
        setErrorBanner(`Unable to reach Dev Queue sidecar server (${err.message || "offline"}).`);
      }
    } finally {
      uiState.inFlight = false;
    }
  }

  function wireEvents() {
    const rootEl = document.getElementById("dq-root");
    if (!rootEl) {
      return;
    }

    rootEl.addEventListener("click", (event) => {
      const kpiBtn = event.target.closest(".dq-kpi[data-section-filter]");
      if (kpiBtn) {
        uiState.sectionFilter = String(kpiBtn.dataset.sectionFilter || "all");
        renderView();
        return;
      }

      const actionBtn = event.target.closest("[data-action]");
      if (!actionBtn) {
        return;
      }
      const action = actionBtn.dataset.action;
      if (action === "refresh") {
        pollState(true);
      } else if (action === "toggle-pause") {
        const curPaused = Boolean(
          uiState.snapshot && uiState.snapshot.orchestration && uiState.snapshot.orchestration.paused,
        );
        setOrchestrationPaused(!curPaused);
      } else if (action === "resume-dev") {
        setOrchestrationPaused(false);
      } else if (action === "open-conv") {
        triggerConversationToggle(actionBtn.dataset.convId, actionBtn.dataset.label);
      } else if (action === "open-board") {
        openArtifactInAuxPane({ kind: "board" });
      } else if (action === "open-artifact") {
        openArtifactInAuxPane({
          kind: "artifact",
          conversation_id: actionBtn.dataset.convId || "",
          filename: actionBtn.dataset.filename || "",
        });
      }
    });

    const filterInput = document.getElementById("dq-filter");
    if (filterInput) {
      filterInput.addEventListener("input", () => {
        uiState.query = filterInput.value || "";
        renderView();
      });
    }

    window.addEventListener("focus", () => {
      pollState(false);
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        pollState(false);
      }
    });
  }

  function bootstrap() {
    syncThemeFromHost();
    if (typeof MutationObserver === "function" && document.documentElement) {
      new MutationObserver(() => syncThemeFromHost()).observe(document.documentElement, {
        attributes: true,
        attributeFilter: ["style"],
      });
    }
    wireEvents();
    pollState(true);
    setInterval(() => {
      if (document.visibilityState !== "hidden") {
        pollState(false);
      }
    }, POLL_INTERVAL_MS);
  }

  if (typeof window !== "undefined") {
    window.DevQueuePanel = {
      isValidUuid,
      sanitizeHttpUrl,
      sanitizeFileUri,
      isNavIntentFresh,
      matchesCardFilter,
      matchesRepoFilter,
      formatPauseMeta,
      setOrchestrationPaused,
      pollState,
    };
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", bootstrap);
    } else {
      bootstrap();
    }
  }
})();
