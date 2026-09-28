(() => {
  "use strict";

  const state = {
    events: [],
    trace: null,
    honesty: null,
    verify: null,
    source: null,
    modelIdentity: null,
    assertedModels: [],
    selectedIndex: null,
    filterText: "",
    filterHook: "",
    served: false,
    activeId: null,
    activeSource: "auto",
    library: null,
    libraryFilter: "",
    libraryItems: [],
  };

  const el = {
    sourceLabel: document.getElementById("sourceLabel"),
    headerCards: document.getElementById("headerCards"),
    dropZone: document.getElementById("dropZone"),
    console: document.getElementById("console"),
    timeline: document.getElementById("timeline"),
    toolHist: document.getElementById("toolHist"),
    modelHist: document.getElementById("modelHist"),
    fileList: document.getElementById("fileList"),
    filterText: document.getElementById("filterText"),
    filterHook: document.getElementById("filterHook"),
    drawer: document.getElementById("drawer"),
    drawerBackdrop: document.getElementById("drawerBackdrop"),
    drawerClose: document.getElementById("drawerClose"),
    drawerHook: document.getElementById("drawerHook"),
    drawerTitle: document.getElementById("drawerTitle"),
    drawerSummary: document.getElementById("drawerSummary"),
    drawerPolicy: document.getElementById("drawerPolicy"),
    drawerRedaction: document.getElementById("drawerRedaction"),
    drawerRaw: document.getElementById("drawerRaw"),
    jsonlInput: document.getElementById("jsonlInput"),
    traceInput: document.getElementById("traceInput"),
    honestyInput: document.getElementById("honestyInput"),
    statSubject: document.getElementById("statSubject"),
    statModel: document.getElementById("statModel"),
    statRelation: document.getElementById("statRelation"),
    statCalls: document.getElementById("statCalls"),
    statMode: document.getElementById("statMode"),
    statVerify: document.getElementById("statVerify"),
    statEvents: document.getElementById("statEvents"),
    verifyPanel: document.getElementById("verifyPanel"),
    verifyBody: document.getElementById("verifyBody"),
    verifySummary: document.getElementById("verifySummary"),
    verifyRefresh: document.getElementById("verifyRefresh"),
    verifyIntro: document.getElementById("verifyIntro"),
    libraryRail: document.getElementById("libraryRail"),
    libraryList: document.getElementById("libraryList"),
    libraryFilter: document.getElementById("libraryFilter"),
    libraryMeta: document.getElementById("libraryMeta"),
    libraryS3: document.getElementById("libraryS3"),
    libraryRefresh: document.getElementById("libraryRefresh"),
  };

  function payloadOf(event) {
    return event && typeof event.payload === "object" && event.payload
      ? event.payload
      : event || {};
  }

  function hookName(event) {
    return (
      event.hook_event_name ||
      event.event ||
      event.type ||
      payloadOf(event).hook_event_name ||
      "unknown"
    );
  }

  function shortId(id) {
    const s = String(id || "");
    if (s.length <= 12) return s;
    return `${s.slice(0, 8)}…`;
  }

  function kindBadge(kind) {
    if (kind === "tab") return '<span class="trail-badge tab">Tab</span>';
    if (kind === "subagent") return '<span class="trail-badge subagent">Subagent</span>';
    if (kind === "parent") return '<span class="trail-badge parent">Parent</span>';
    return '<span class="trail-badge agent">Agent</span>';
  }

  function parentLinkHtml(parentId) {
    if (!parentId) return "";
    return (
      `<button type="button" class="trail-parent-link" data-parent-id="${escapeAttr(
        parentId
      )}" title="${escapeAttr(parentId)}">parent ${escapeHtml(shortId(parentId))}</button>`
    );
  }

  function relateHtml(kind, parentId) {
    const parts = [kindBadge(kind || "agent")];
    if (parentId) parts.push(parentLinkHtml(parentId));
    return parts.join(" ");
  }

  function wireParentLinks(root) {
    if (!root) return;
    root.querySelectorAll("[data-parent-id]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const pid = btn.getAttribute("data-parent-id");
        if (pid) loadTrailById(pid, "auto");
      });
    });
  }

  function decisionOf(event) {
    const pd = event.policy_decision;
    if (!pd || typeof pd !== "object") return null;
    return pd.decision || null;
  }

  function toolName(event) {
    const p = payloadOf(event);
    return p.tool_name || event.tool_name || null;
  }

  function filePath(event) {
    const p = payloadOf(event);
    return p.file_path || p.path || event.file_path || event.path || null;
  }

  function commandOf(event) {
    const p = payloadOf(event);
    return p.command || event.command || null;
  }

  function shortPath(path) {
    if (!path) return "";
    const parts = String(path).split("/");
    if (parts.length <= 3) return path;
    return `…/${parts.slice(-3).join("/")}`;
  }

  function formatTime(ts) {
    if (!ts) return "—";
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return String(ts).slice(11, 23) || String(ts);
    return d.toISOString().slice(11, 23);
  }

  function hookClass(name) {
    if (/Shell/i.test(name)) return "hook-shell";
    if (/Read/i.test(name)) return "hook-read";
    if (/Edit|Write/i.test(name)) return "hook-edit";
    if (/Fail/i.test(name)) return "hook-fail";
    if (/Thought|Prompt|session/i.test(name)) return "hook-thought";
    if (/Tool/i.test(name)) return "hook-tool";
    return "hook-other";
  }

  function eventTitle(event) {
    const hook = hookName(event);
    const tool = toolName(event);
    if (tool) return tool;
    if (/Shell/i.test(hook)) return "Shell";
    if (/Read/i.test(hook)) return "Read";
    if (/Edit/i.test(hook)) return "Edit";
    if (/Thought/i.test(hook)) return "Thought";
    return hook;
  }

  function eventMeta(event) {
    const cmd = commandOf(event);
    if (cmd) return cmd;
    const path = filePath(event);
    if (path) return path;
    const tool = toolName(event);
    const p = payloadOf(event);
    if (tool && p.cwd) return `cwd ${p.cwd}`;
    if (event.model_id || event.model) {
      return `model ${event.model_id || event.model}`;
    }
    return hookName(event);
  }

  function collectRedactionStubs(node, path, out) {
    if (Array.isArray(node)) {
      node.forEach((item, i) => collectRedactionStubs(item, `${path}[${i}]`, out));
      return;
    }
    if (!node || typeof node !== "object") return;
    if (node._redacted === true) {
      out.push({
        path: path || "(root)",
        present: node.present,
        type: node.type,
        chars: node.chars,
        bytes: node.bytes,
        sha256: node.sha256,
      });
      return;
    }
    Object.keys(node).forEach((key) => {
      const next = path ? `${path}.${key}` : key;
      collectRedactionStubs(node[key], next, out);
    });
  }

  function parseJsonl(text) {
    const events = [];
    const lines = text.split(/\r?\n/);
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i].trim();
      if (!line) continue;
      events.push(JSON.parse(line));
    }
    return events;
  }

  function applyBundle(bundle, label) {
    state.events = Array.isArray(bundle.events) ? bundle.events : [];
    state.trace = bundle.trace || null;
    state.honesty = bundle.honesty || null;
    state.verify = bundle.verify || null;
    state.source = bundle.source || null;
    state.modelIdentity = bundle.model_identity || null;
    state.assertedModels = Array.isArray(bundle.asserted_models)
      ? bundle.asserted_models
      : [];
    state.activeId = bundle.id || (state.source && stemFromPath(state.source.jsonl)) || null;
    state.selectedIndex = null;
    el.sourceLabel.textContent = label || (state.source && state.source.jsonl) || "Local files";
    renderAll();
    renderLibraryList();
  }

  function stemFromPath(path) {
    if (!path) return null;
    const base = String(path).split(/[/\\]/).pop() || "";
    return base.replace(/\.jsonl$/i, "") || null;
  }

  function deepLinkId() {
    const params = new URLSearchParams(window.location.search || "");
    const q = params.get("id");
    if (q) return q;
    const match = window.location.pathname.match(/\/t\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  }

  function setDeepLink(id, source) {
    if (!id) return;
    const url = new URL(window.location.href);
    url.pathname = url.pathname.replace(/\/t\/[^/]*$/, "/") || "/";
    if (!url.pathname.endsWith("/")) {
      // keep root for SPA
    }
    url.searchParams.set("id", id);
    if (source && source !== "auto") url.searchParams.set("source", source);
    else url.searchParams.delete("source");
    window.history.replaceState({}, "", url.toString());
  }

  function renderHeader() {
    const hasData = state.events.length > 0 || state.trace || state.honesty;
    el.headerCards.hidden = !hasData;
    el.dropZone.hidden = state.events.length > 0;
    el.console.hidden = state.events.length === 0;

    const subject =
      (state.trace && state.trace.subject) ||
      (state.honesty && state.honesty.subject) ||
      (state.events[0] && state.events[0].conversation_id) ||
      "—";
    const identity = state.modelIdentity || {};
    let model =
      identity.model_id ||
      (state.trace && state.trace.model && state.trace.model.model_id) ||
      (state.honesty && state.honesty.model_id_in_record) ||
      "—";
    if (identity.provider && identity.provider !== "cursor-asserted" && model !== "—") {
      model = `${model} (${identity.provider})`;
    }
    const parentId =
      identity.parent_conversation_id ||
      (state.honesty && state.honesty.parent_conversation_id) ||
      null;
    const libraryItem =
      state.libraryItems.find((t) => t.id === state.activeId) || null;
    const kind =
      (libraryItem && libraryItem.kind) ||
      (state.activeId && /^tab(?:-\d{8})?$/.test(state.activeId)
        ? "tab"
        : parentId
          ? "subagent"
          : state.events.some((e) => hookName(e) === "subagentStart")
            ? "parent"
            : "agent");

    const calls =
      (state.trace &&
        state.trace.tool_transcript &&
        state.trace.tool_transcript.call_count) ??
      state.events.filter((e) => /preToolUse|beforeShell|beforeRead|afterFileEdit/i.test(hookName(e)))
        .length;
    const mode =
      (state.trace && state.trace.policy && state.trace.policy.enforcement_mode) ||
      (state.events[0] &&
        state.events[0].policy_decision &&
        (state.events[0].policy_decision.trace_enforcement_mode ||
          state.events[0].policy_decision.mode)) ||
      "—";
    const verify = state.verify || { status: "n/a", detail: "local load", checks: [] };

    el.statSubject.textContent = subject;
    el.statModel.textContent = model;
    el.statModel.title = (identity.notes || []).join("\n") || "";
    if (el.statRelation) {
      el.statRelation.innerHTML = relateHtml(kind, parentId || (libraryItem && libraryItem.parent_id));
      wireParentLinks(el.statRelation);
    }
    el.statCalls.textContent = String(calls);
    el.statMode.textContent = mode;
    el.statEvents.textContent = String(state.events.length);

    el.statVerify.textContent = verify.status;
    el.statVerify.title = verify.detail || "";
    el.statVerify.className = "stat-value";
    if (verify.status === "PASS") el.statVerify.classList.add("verify-pass");
    else if (verify.status === "FAIL") el.statVerify.classList.add("verify-fail");
    else if (verify.status === "signed") el.statVerify.classList.add("verify-signed");
    else el.statVerify.classList.add("verify-na");

    renderVerifyPanel(verify);
  }

  function renderVerifyPanel(verify) {
    const hasData = state.events.length > 0 || state.trace || state.honesty;
    el.verifyPanel.hidden = !hasData;
    el.verifyRefresh.hidden = !state.served;
    el.verifySummary.textContent = humanVerifyStatus(verify);
    el.verifyPanel.classList.toggle("verify-pending", Boolean(verify.pending));

    const checks = Array.isArray(verify.checks) ? verify.checks : [];
    el.verifyBody.innerHTML = "";

    if (el.verifyIntro) {
      const intro = verifyIntroText(verify, checks.length);
      if (intro) {
        el.verifyIntro.hidden = false;
        el.verifyIntro.textContent = intro;
      } else {
        el.verifyIntro.hidden = true;
        el.verifyIntro.textContent = "";
      }
    }

    if (verify.pending && !checks.length) {
      const empty = document.createElement("div");
      empty.className = "verify-empty verify-loading";
      empty.textContent = verify.detail || "Running signed-record check…";
      el.verifyBody.appendChild(empty);
      return;
    }

    if (verify.status === "n/a" && !checks.length) {
      const empty = document.createElement("div");
      empty.className = "verify-empty";
      empty.textContent =
        verify.detail ||
        "No signed session record yet. Policy allow/deny in the timeline is separate from this check.";
      el.verifyBody.appendChild(empty);
      return;
    }

    if (!checks.length) {
      const empty = document.createElement("div");
      empty.className = "verify-empty";
      empty.textContent =
        humanVerifyEmpty(verify) ||
        verify.detail ||
        "No per-check lines available.";
      el.verifyBody.appendChild(empty);
      return;
    }

    checks.forEach((check, index) => {
      el.verifyBody.appendChild(buildPlainCheckRow(check, index));
    });
  }

  function humanVerifyStatus(verify) {
    const status = verify && verify.status;
    if (verify && verify.pending) return "Checking…";
    if (status === "PASS") return "Looks good";
    if (status === "FAIL") return "Record problem";
    if (status === "signed") return "Signed (tooling missing)";
    if (status === "n/a") return "Not signed yet";
    return status || "";
  }

  function verifyIntroText(verify, checkCount) {
    if (!verify || verify.pending) return "";
    if (verify.status === "PASS") {
      return (
        "PASS means this signed session record is well-formed and the cryptographic signature matches the public key on the record. " +
        "It does not mean the agent behaved well or that every tool call was allowed, that is what policy allow/deny in the timeline shows. " +
        (checkCount
          ? "Below are the individual checks in plain language (technical codes stay under “Details”)."
          : "")
      ).trim();
    }
    if (verify.status === "FAIL") {
      return (
        "Verify FAIL means the signed TRACE record is missing, broken, or does not match its signature. " +
        "That is different from a policy deny (red DENY in the timeline), which means your allow/deny rules blocked a tool. " +
        "Fix the record or signing setup — a deny in the timeline can still pair with a valid signed record."
      );
    }
    if (verify.status === "signed") {
      return (
        "A signature is present, but the Level-0 checker (trace-tests) is not installed, so per-check lines are unavailable. " +
        "Install with: pip install agentrust-trace-tests — then click Re-check. " +
        "Policy denials in the timeline are unrelated to this tooling gap."
      );
    }
    if (verify.status === "n/a") {
      return (
        "No sibling .trace.json yet, so there is nothing to cryptographically verify. " +
        "You can still read the timeline: red DENY / would_deny badges are policy decisions (tool blocked or would-be-blocked), not verify failures."
      );
    }
    return "";
  }

  function humanVerifyEmpty(verify) {
    if (verify.status === "signed") {
      return (
        "Signed record present; install trace-tests (pip install agentrust-trace-tests) for per-check lines."
      );
    }
    return verify.detail || "";
  }

  function humanizeCheck(check) {
    const id = (check && check.id) || "TR-?";
    const result = (check && check.result) || "?";
    const message = String((check && check.message) || "");
    const lower = message.toLowerCase();

    const family =
      id === "TR-SIG"
        ? "signature"
        : id === "TR-POL"
          ? "policy"
          : id === "TR-APR"
            ? "appraisal"
            : id === "TR-ENV"
              ? "record"
              : "other";

    let title = "Check completed";
    let meaning = "This Level-0 conformance item finished.";

    if (family === "signature") {
      if (lower.includes("ed25519") || lower.includes("signature verified")) {
        title = "Signature checks out";
        meaning = "The record was signed with the matching key — it was not silently altered afterward.";
      } else if (lower.includes("key type") || lower.includes("supported")) {
        title = "Signing key type is supported";
        meaning = "The public key on the record uses a known, acceptable algorithm.";
      } else {
        title = "Signature-related check";
        meaning = "Cryptographic signature fields on the record were examined.";
      }
    } else if (family === "policy") {
      if (lower.includes("enforcement_mode")) {
        title = "Policy mode looks valid";
        meaning = "The record states how rules were applied (for example enforce vs observe) in an expected form.";
      } else if (lower.includes("bundle_hash") || lower.includes("digest")) {
        title = "Policy fingerprint looks valid";
        meaning = "The hash that identifies which rule bundle was in play is well-formed.";
      } else if (lower.includes("policy_uri") || lower.includes("optional")) {
        title = "Optional policy link skipped";
        meaning = "No remote policy URL was required at Level 0 — this skip is normal.";
      } else {
        title = "Policy fields look valid";
        meaning = "Rule-related fields on the signed record are present and well-formed.";
      }
    } else if (family === "appraisal") {
      if (lower.includes("status") && lower.includes("none")) {
        title = "No extra appraisal (OK at Level 0)";
        meaning = "Nothing claimed a stronger hardware or third-party attestation — Level 0 allows “none”.";
      } else if (lower.includes("verifier") && lower.includes("uri")) {
        title = "Appraisal verifier link looks valid";
        meaning = "If an appraisal pointer is present, it is a proper absolute URL.";
      } else if (lower.includes("optional") || lower.includes("not present") || lower.includes("level 1")) {
        title = "Optional appraisal skipped";
        meaning = "Stronger appraisal fields are not required at Level 0.";
      } else {
        title = "Appraisal fields look OK";
        meaning = "Appraisal-related fields on the record are acceptable for Level 0.";
      }
    } else if (family === "record") {
      if (lower.includes("eat_profile") || lower.includes("sentinel")) {
        title = "Record format matches the profile";
        meaning = "This file declares the expected TRACE profile version.";
      } else if (lower.includes("iat") || lower.includes("fresh")) {
        title = "Timestamp looks reasonable";
        meaning = "The “issued at” time on the record is present and within an acceptable window.";
      } else if (lower.includes("subject") || lower.includes("workload") || lower.includes("spiffe") || lower.includes("uri")) {
        title = "Identity looks valid";
        meaning = "The session identity string is a proper workload URI (who this record is about).";
      } else if (lower.includes("private key")) {
        title = "No private key leaked into the record";
        meaning = "Only a public key is stored — the secret signing key is not embedded in the file.";
      } else if (lower.includes("cnf") || lower.includes("kty") || lower.includes("jwk")) {
        title = "Public key material is present";
        meaning = "The record includes the public half of the key used to check the signature.";
      } else {
        title = "Record structure looks OK";
        meaning = "Core envelope fields (format, time, identity, or keys) passed a Level-0 check.";
      }
    }

    if (result === "SKIP") {
      if (!title.toLowerCase().includes("skip") && !title.toLowerCase().includes("optional")) {
        title = `Optional: ${title}`;
      }
      meaning =
        meaning +
        " Skipped items are optional or out of scope for Level 0 — they are not failures.";
    } else if (result === "FAIL") {
      title = title.replace(/^Optional:\s*/i, "");
      if (!title.toLowerCase().includes("fail") && !title.toLowerCase().includes("problem")) {
        title = `Problem: ${title}`;
      }
      meaning =
        "This check did not pass. " +
        meaning +
        " A verify failure is about the signed record itself — not the same as a policy DENY on a tool call.";
    }

    return { id, result, message, title, meaning, family };
  }

  function buildPlainCheckRow(check, index) {
    const info = humanizeCheck(check);
    const row = document.createElement("article");
    row.className = `verify-check verify-${(info.result || "").toLowerCase()}`;
    row.dataset.index = String(index);

    const head = document.createElement("div");
    head.className = "verify-check-head";

    const status = document.createElement("span");
    status.className = `verify-result ${info.result || ""}`;
    status.textContent =
      info.result === "PASS" ? "OK" : info.result === "SKIP" ? "Skip" : info.result || "?";

    const text = document.createElement("div");
    text.className = "verify-check-text";

    const title = document.createElement("div");
    title.className = "verify-check-title";
    title.textContent = info.title;

    const meaning = document.createElement("div");
    meaning.className = "verify-check-meaning";
    meaning.textContent = info.meaning;

    text.append(title, meaning);
    head.append(status, text);

    const details = document.createElement("details");
    details.className = "verify-check-details";
    if (info.result === "FAIL") details.open = true;

    const summary = document.createElement("summary");
    summary.textContent = `Technical detail (${info.id})`;

    const tech = document.createElement("div");
    tech.className = "verify-check-tech";
    tech.innerHTML = `<span class="verify-id">${escapeHtml(info.id)}</span> <span class="verify-msg">${escapeHtml(
      info.message
    )}</span>`;

    details.append(summary, tech);
    row.append(head, details);
    return row;
  }

  function clientAssertedModels() {
    if (state.assertedModels.length) return state.assertedModels;
    const counts = new Map();
    const bump = (value) => {
      if (!value || typeof value !== "string") return;
      const text = value.trim();
      if (!text) return;
      counts.set(text, (counts.get(text) || 0) + 1);
    };
    const walk = (node, depth) => {
      if (depth > 6 || node == null) return;
      if (Array.isArray(node)) {
        node.forEach((item) => walk(item, depth + 1));
        return;
      }
      if (typeof node !== "object") return;
      Object.keys(node).forEach((key) => {
        const lower = key.toLowerCase();
        if (
          lower === "model" ||
          lower === "model_id" ||
          lower === "modelid" ||
          lower === "model_slug" ||
          lower === "subagent_model" ||
          lower === "composermodelid"
        ) {
          bump(node[key]);
        } else {
          walk(node[key], depth + 1);
        }
      });
    };
    state.events.forEach((event) => walk(event, 0));
    return Array.from(counts.entries())
      .map(([model, count]) => ({ model, count }))
      .sort((a, b) => b.count - a.count || a.model.localeCompare(b.model));
  }

  function renderModelHist() {
    if (!el.modelHist) return;
    const entries = clientAssertedModels();
    el.modelHist.innerHTML = "";
    if (!entries.length) {
      el.modelHist.innerHTML = '<div class="empty">No model assertions.</div>';
      return;
    }
    const max = entries[0].count || 1;
    entries.forEach(({ model, count }) => {
      const row = document.createElement("div");
      row.className = "hist-row";
      row.innerHTML = `
        <div class="hist-label" title="${escapeAttr(model)}">${escapeHtml(model)}</div>
        <div class="hist-count">${count}</div>
        <div class="hist-bar-wrap"><div class="hist-bar" style="width:${(100 * count) / max}%"></div></div>
      `;
      el.modelHist.appendChild(row);
    });
  }

  function filteredEvents() {
    const q = state.filterText.trim().toLowerCase();
    const denialsOnly = state.filterHook === "__policy_denials__";
    return state.events
      .map((event, index) => ({ event, index }))
      .filter(({ event }) => {
        if (denialsOnly) {
          const dec = decisionOf(event);
          if (dec !== "deny" && dec !== "would_deny") return false;
        } else if (state.filterHook && hookName(event) !== state.filterHook) {
          return false;
        }
        if (!q) return true;
        const hay = [
          hookName(event),
          toolName(event),
          filePath(event),
          commandOf(event),
          JSON.stringify(event.policy_decision || {}),
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        return hay.includes(q);
      });
  }

  function renderHookFilter() {
    const hooks = Array.from(new Set(state.events.map(hookName))).sort();
    const current = state.filterHook;
    el.filterHook.innerHTML = "";
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "All hooks";
    el.filterHook.appendChild(all);

    const denials = document.createElement("option");
    denials.value = "__policy_denials__";
    denials.textContent = "Policy denials only";
    el.filterHook.appendChild(denials);

    hooks.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      el.filterHook.appendChild(opt);
    });
    el.filterHook.value = current || "";
    if (current && el.filterHook.value !== current) {
      el.filterHook.value = "";
      state.filterHook = "";
    }
  }

  function renderTimeline() {
    const rows = filteredEvents();
    el.timeline.innerHTML = "";
    if (!rows.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.style.padding = "16px";
      empty.textContent = "No events match this filter.";
      el.timeline.appendChild(empty);
      return;
    }

    rows.forEach(({ event, index }) => {
      const row = document.createElement("article");
      const dec = decisionOf(event);
      row.className =
        "event" +
        (state.selectedIndex === index ? " selected" : "") +
        (dec === "deny" ? " event-deny" : "") +
        (dec === "would_deny" ? " event-would-deny" : "");
      row.setAttribute("role", "listitem");
      row.tabIndex = 0;

      const time = document.createElement("div");
      time.className = "event-time";
      time.textContent = formatTime(event.ts);

      const pill = document.createElement("span");
      const hook = hookName(event);
      pill.className = `hook-pill ${hookClass(hook)}`;
      pill.textContent = hook;

      const main = document.createElement("div");
      main.className = "event-main";
      const title = document.createElement("div");
      title.className = "event-title";
      title.textContent = eventTitle(event);
      const meta = document.createElement("div");
      meta.className = "event-meta";
      meta.textContent = eventMeta(event);
      main.append(title, meta);

      const decEl = document.createElement("div");
      decEl.className = "decision";
      if (dec) {
        decEl.classList.add(`decision-${dec}`);
        decEl.textContent = dec === "deny" ? "DENY" : dec === "would_deny" ? "WOULD DENY" : dec;
      } else {
        decEl.textContent = "";
      }

      row.append(time, pill, main, decEl);
      row.addEventListener("click", () => {
        if (state.selectedIndex === index) closeDrawer();
        else openDrawer(index);
      });
      row.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          if (state.selectedIndex === index) closeDrawer();
          else openDrawer(index);
        }
      });
      el.timeline.appendChild(row);
    });
  }

  function renderTools() {
    const counts = new Map();
    state.events.forEach((event) => {
      const tool = toolName(event);
      if (!tool) return;
      if (!/preToolUse|postToolUse|beforeShell|afterShell/i.test(hookName(event))) {
        // Count tool names from tool hooks primarily; also shell/file if named.
      }
      counts.set(tool, (counts.get(tool) || 0) + 1);
    });
    // Prefer counting distinct tool use moments: preToolUse + beforeShellExecution
    const preferred = new Map();
    state.events.forEach((event) => {
      const hook = hookName(event);
      if (hook === "preToolUse") {
        const tool = toolName(event) || "tool";
        preferred.set(tool, (preferred.get(tool) || 0) + 1);
      } else if (hook === "beforeShellExecution") {
        preferred.set("Shell", (preferred.get("Shell") || 0) + 1);
      }
    });
    const use = preferred.size ? preferred : counts;
    const entries = Array.from(use.entries()).sort((a, b) => b[1] - a[1]);
    el.toolHist.innerHTML = "";
    if (!entries.length) {
      el.toolHist.innerHTML = '<div class="empty">No tool calls.</div>';
      return;
    }
    const max = entries[0][1] || 1;
    entries.forEach(([name, count]) => {
      const row = document.createElement("div");
      row.className = "hist-row";
      row.innerHTML = `
        <div class="hist-label">${escapeHtml(name)}</div>
        <div class="hist-count">${count}</div>
        <div class="hist-bar-wrap"><div class="hist-bar" style="width:${(100 * count) / max}%"></div></div>
      `;
      el.toolHist.appendChild(row);
    });
  }

  function renderFiles() {
    const map = new Map();
    state.events.forEach((event) => {
      const hook = hookName(event);
      const path = filePath(event);
      if (!path) return;
      const isRead = /Read/i.test(hook);
      const isEdit = /Edit|Write/i.test(hook);
      if (!isRead && !isEdit) return;
      const entry = map.get(path) || { path, read: false, edit: false, decision: null };
      if (isRead) entry.read = true;
      if (isEdit) entry.edit = true;
      const dec = decisionOf(event);
      if (dec === "deny" || dec === "would_deny") entry.decision = dec;
      map.set(path, entry);
    });
    const rows = Array.from(map.values()).sort((a, b) => a.path.localeCompare(b.path));
    el.fileList.innerHTML = "";
    if (!rows.length) {
      el.fileList.innerHTML = '<div class="empty">No file touches.</div>';
      return;
    }
    rows.forEach((row) => {
      const node = document.createElement("div");
      node.className = "file-row";
      if (row.decision) node.classList.add(row.decision);
      const tags = [];
      if (row.read) tags.push('<span class="tag tag-read">read</span>');
      if (row.edit) tags.push('<span class="tag tag-edit">edit</span>');
      if (row.decision === "deny") tags.push('<span class="tag tag-deny">deny</span>');
      if (row.decision === "would_deny") tags.push('<span class="tag tag-would">would_deny</span>');
      node.innerHTML = `
        <div class="file-path" title="${escapeAttr(row.path)}">${escapeHtml(shortPath(row.path))}</div>
        <div class="file-tags">${tags.join("")}</div>
      `;
      el.fileList.appendChild(node);
    });
  }

  function openDrawer(index) {
    const event = state.events[index];
    if (!event) return;
    state.selectedIndex = index;
    renderTimeline();

    const hook = hookName(event);
    el.drawerHook.textContent = hook;
    el.drawerTitle.textContent = eventTitle(event);

    const summary = {
      ts: event.ts || null,
      tool: toolName(event),
      path: filePath(event),
      command: commandOf(event),
      model: event.model_id || event.model || null,
      conversation_id: event.conversation_id || null,
      generation_id: event.generation_id || null,
    };
    el.drawerSummary.textContent = JSON.stringify(summary, null, 2);
    el.drawerPolicy.textContent = JSON.stringify(event.policy_decision || null, null, 2);

    const stubs = [];
    collectRedactionStubs(event, "", stubs);
    el.drawerRedaction.textContent = stubs.length
      ? JSON.stringify(stubs, null, 2)
      : "No redaction stubs on this event.";
    el.drawerRaw.textContent = JSON.stringify(event, null, 2);

    el.drawer.hidden = false;
    el.drawerBackdrop.hidden = false;
    el.drawer.setAttribute("aria-hidden", "false");
  }

  function closeDrawer() {
    if (state.selectedIndex === null && el.drawer.hidden) return;
    state.selectedIndex = null;
    el.drawer.hidden = true;
    el.drawerBackdrop.hidden = true;
    el.drawer.setAttribute("aria-hidden", "true");
    renderTimeline();
  }

  function renderAll() {
    renderHeader();
    renderHookFilter();
    renderTimeline();
    renderModelHist();
    renderTools();
    renderFiles();
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeAttr(value) {
    return escapeHtml(value).replace(/'/g, "&#39;");
  }

  async function loadFromServer() {
    try {
      const res = await fetch("/api/bundle", { cache: "no-store" });
      if (!res.ok) return false;
      const bundle = await res.json();
      if (!bundle || !Array.isArray(bundle.events)) return false;
      state.served = true;
      applyBundle(bundle, (bundle.source && bundle.source.jsonl) || "Served trail");
      return true;
    } catch (_err) {
      return false;
    }
  }

  async function loadTrailById(id, source) {
    if (!id) return false;
    const src = source || "auto";
    const qs = new URLSearchParams({ id, source: src });
    const res = await fetch(`/api/bundle?${qs.toString()}`, { cache: "no-store" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      el.sourceLabel.textContent = err.error || `Failed to load ${id}`;
      return false;
    }
    const bundle = await res.json();
    state.served = true;
    state.activeSource = src;
    applyBundle(bundle, (bundle.source && bundle.source.jsonl) || id);
    setDeepLink(id, src);
    return true;
  }

  async function loadLibrary() {
    try {
      const res = await fetch("/api/library", { cache: "no-store" });
      if (!res.ok) return false;
      const data = await res.json();
      state.library = data;
      state.served = true;
      el.libraryRail.hidden = false;
      const local = Array.isArray(data.local) ? data.local : [];
      const s3 = (data.s3 && Array.isArray(data.s3.trails) ? data.s3.trails : []).map((t) => ({
        ...t,
        source: "s3",
      }));
      // Prefer local when same id exists in both.
      const byId = new Map();
      s3.forEach((t) => byId.set(t.id, t));
      local.forEach((t) => byId.set(t.id, t));
      state.libraryItems = Array.from(byId.values()).sort(
        (a, b) => (b.mtime || 0) - (a.mtime || 0)
      );
      const counts = data.counts || {};
      el.libraryMeta.textContent = `${counts.local || local.length} local · ${
        counts.s3 || s3.length
      } s3`;
      const s3info = data.s3 || {};
      if (s3info.configured && s3info.ok) {
        el.libraryS3.textContent = s3info.detail || `s3://${s3info.bucket}/${s3info.prefix}/`;
      } else if (s3info.configured) {
        el.libraryS3.textContent = s3info.detail || "S3 list failed";
      } else {
        el.libraryS3.textContent =
          s3info.detail || "S3 optional — set CURSOR_AGENT_TRACE_S3_BUCKET to browse org trails";
      }
      renderLibraryList();
      return true;
    } catch (_err) {
      return false;
    }
  }

  function renderLibraryList() {
    if (!el.libraryList || el.libraryRail.hidden) return;
    const q = state.libraryFilter.trim().toLowerCase();
    const items = state.libraryItems.filter((item) => {
      if (!q) return true;
      const hay = [
        item.id,
        item.kind,
        item.parent_id,
        item.user,
        item.workspace,
        item.subject,
        item.model_id,
        item.source,
        item.mtime_iso,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return hay.includes(q);
    });
    el.libraryList.innerHTML = "";
    if (!items.length) {
      el.libraryList.innerHTML = '<div class="library-empty">No matching trails.</div>';
      return;
    }
    items.forEach((item) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "trail-item" + (item.id === state.activeId ? " active" : "");
      btn.setAttribute("role", "listitem");
      const badges = [kindBadge(item.kind || "agent")];
      if (item.has_trace) badges.push('<span class="trail-badge trace">TRACE</span>');
      if (item.source === "s3") badges.push('<span class="trail-badge s3">S3</span>');
      if (item.verify_status) {
        badges.push(
          `<span class="trail-badge verify-${escapeAttr(item.verify_status)}">${escapeHtml(
            item.verify_status
          )}</span>`
        );
      }
      const when = item.mtime_iso ? item.mtime_iso.replace("T", " ").replace("Z", "") : "—";
      const events =
        item.event_count != null ? `${item.event_count} events` : item.bytes != null ? `${item.bytes} B` : "";
      const user = item.user ? ` · ${item.user}` : "";
      const parentRow = item.parent_id
        ? `<div class="trail-row trail-parent-row">${parentLinkHtml(item.parent_id)}</div>`
        : "";
      btn.innerHTML = `
        <div class="trail-id">${escapeHtml(item.id)}</div>
        <div class="trail-row">${badges.join("")}</div>
        ${parentRow}
        <div class="trail-row">${escapeHtml(when)}${events ? " · " + escapeHtml(String(events)) : ""}${escapeHtml(
        user
      )}</div>
      `;
      btn.addEventListener("click", () => {
        loadTrailById(item.id, item.source || "auto");
      });
      wireParentLinks(btn);
      el.libraryList.appendChild(btn);
    });
  }

  function applyVerifyResult(verify) {
    state.verify = verify && typeof verify === "object" ? verify : {
      status: "FAIL",
      detail: "invalid verify payload",
      checks: [],
    };
    renderHeader();
  }

  async function refreshVerify() {
    if (!state.served) {
      applyVerifyResult({
        status: "n/a",
        detail: "Viewer server required for Level-0 verify (python -m lib view)",
        checks: [],
      });
      return;
    }
    if (!el.verifyRefresh) return;

    const previous = state.verify;
    el.verifyRefresh.disabled = true;
    el.verifyRefresh.textContent = "Checking…";
    applyVerifyResult({
      status: (previous && previous.status) || "…",
      detail: "Running trace-tests verify --level 0…",
      checks: Array.isArray(previous && previous.checks) ? previous.checks : [],
      pending: true,
    });

    try {
      const qs = new URLSearchParams();
      if (state.activeId) qs.set("id", state.activeId);
      if (state.activeSource) qs.set("source", state.activeSource);
      const res = await fetch(`/api/verify?${qs.toString()}`, { cache: "no-store" });
      let payload = null;
      try {
        payload = await res.json();
      } catch (_err) {
        payload = null;
      }
      if (!res.ok) {
        const detail =
          (payload && (payload.error || payload.detail)) ||
          `Verify request failed (HTTP ${res.status})`;
        applyVerifyResult({
          status: "FAIL",
          detail,
          checks: [],
        });
        return;
      }
      if (!payload || typeof payload !== "object" || payload.error) {
        applyVerifyResult({
          status: "FAIL",
          detail:
            (payload && (payload.error || payload.detail)) ||
            "Verify API returned an unexpected payload",
          checks: Array.isArray(payload && payload.checks) ? payload.checks : [],
        });
        return;
      }
      applyVerifyResult(payload);
    } catch (err) {
      applyVerifyResult({
        status: "FAIL",
        detail: `Verify request error: ${err && err.message ? err.message : err}`,
        checks: [],
      });
    } finally {
      el.verifyRefresh.disabled = false;
      el.verifyRefresh.textContent = "Re-check";
    }
  }

  function readFileAsText(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(reader.error || new Error("read failed"));
      reader.readAsText(file);
    });
  }

  async function onJsonlFile(file) {
    const text = await readFileAsText(file);
    const events = parseJsonl(text);
    applyBundle(
      {
        events,
        trace: state.trace,
        honesty: state.honesty,
        verify: {
          status: "n/a",
          detail: "local file picker (verify via python -m lib view)",
          checks: [],
        },
        model_identity: null,
        asserted_models: [],
        source: { jsonl: file.name, trace: null, honesty: null },
      },
      file.name
    );
  }

  async function onTraceFile(file) {
    const text = await readFileAsText(file);
    state.trace = JSON.parse(text);
    state.verify = {
      status: state.trace && state.trace.signature ? "signed" : "FAIL",
      detail: "loaded locally; run viewer server for Level-0 verify",
      checks: [],
    };
    renderHeader();
  }

  async function onHonestyFile(file) {
    const text = await readFileAsText(file);
    state.honesty = JSON.parse(text);
    renderHeader();
  }

  function wireFiles() {
    el.jsonlInput.addEventListener("change", async () => {
      const file = el.jsonlInput.files && el.jsonlInput.files[0];
      if (file) await onJsonlFile(file);
      el.jsonlInput.value = "";
    });
    el.traceInput.addEventListener("change", async () => {
      const file = el.traceInput.files && el.traceInput.files[0];
      if (file) await onTraceFile(file);
      el.traceInput.value = "";
    });
    el.honestyInput.addEventListener("change", async () => {
      const file = el.honestyInput.files && el.honestyInput.files[0];
      if (file) await onHonestyFile(file);
      el.honestyInput.value = "";
    });
  }

  function wireDrop() {
    const zone = el.dropZone;
    const stop = (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
    };
    ["dragenter", "dragover"].forEach((name) => {
      zone.addEventListener(name, (ev) => {
        stop(ev);
        zone.classList.add("drag");
      });
    });
    ["dragleave", "drop"].forEach((name) => {
      zone.addEventListener(name, (ev) => {
        stop(ev);
        zone.classList.remove("drag");
      });
    });
    zone.addEventListener("drop", async (ev) => {
      const files = Array.from((ev.dataTransfer && ev.dataTransfer.files) || []);
      const jsonl = files.find((f) => /\.jsonl$/i.test(f.name));
      const trace = files.find((f) => /\.trace\.json$/i.test(f.name));
      const honesty = files.find((f) => /\.honesty\.json$/i.test(f.name));
      if (jsonl) await onJsonlFile(jsonl);
      if (trace) await onTraceFile(trace);
      if (honesty) await onHonestyFile(honesty);
    });
  }

  function wireUi() {
    el.filterText.addEventListener("input", () => {
      state.filterText = el.filterText.value;
      renderTimeline();
    });
    el.filterHook.addEventListener("change", () => {
      state.filterHook = el.filterHook.value;
      renderTimeline();
    });
    el.drawerClose.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      closeDrawer();
    });
    el.drawerBackdrop.addEventListener("click", (ev) => {
      ev.preventDefault();
      closeDrawer();
    });
    el.verifyRefresh.addEventListener("click", (ev) => {
      ev.preventDefault();
      refreshVerify();
    });
    if (el.statVerify) {
      el.statVerify.style.cursor = "pointer";
      el.statVerify.title = "Click to re-run Level-0 verify";
      el.statVerify.addEventListener("click", (ev) => {
        ev.preventDefault();
        if (!state.served) return;
        refreshVerify();
      });
    }
    if (el.libraryFilter) {
      el.libraryFilter.addEventListener("input", () => {
        state.libraryFilter = el.libraryFilter.value;
        renderLibraryList();
      });
    }
    if (el.libraryRefresh) {
      el.libraryRefresh.addEventListener("click", (ev) => {
        ev.preventDefault();
        loadLibrary();
      });
    }
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      if (el.drawer.hidden) return;
      ev.preventDefault();
      closeDrawer();
    });
  }

  async function boot() {
    wireFiles();
    wireDrop();
    wireUi();
    const linkId = deepLinkId();
    const params = new URLSearchParams(window.location.search || "");
    const linkSource = params.get("source") || "auto";
    const hasLibrary = await loadLibrary();
    if (linkId) {
      const ok = await loadTrailById(linkId, linkSource);
      if (ok) return;
    }
    const served = await loadFromServer();
    if (!served && !hasLibrary) {
      renderHeader();
    } else if (!served && hasLibrary && state.libraryItems.length) {
      el.sourceLabel.textContent = "Pick a trail from the library";
      renderHeader();
    }
  }

  boot();
})();
