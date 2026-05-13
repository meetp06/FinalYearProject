/* Atlas — vanilla client (Inter Tight / mono UI) */

const API_BASE = "";
const STORAGE_KEY = "atlas-sessions-v1";
const ACTIVE_SESSION_KEY = "atlas-active-session-v1";
const STALE_EMPTY_MS = 5 * 60 * 1000;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function isCustomerFacing() {
  return typeof document !== "undefined" && document.body?.classList?.contains("customer-facing");
}

const state = {
  sessionId: null,
  sessions: {},
  inflight: false,
};

function loadState() {
  try {
    state.sessions = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch {
    state.sessions = {};
  }
  try {
    const persisted = localStorage.getItem(ACTIVE_SESSION_KEY);
    if (persisted && state.sessions[persisted]) state.sessionId = persisted;
  } catch {
    /* ignore */
  }
  pruneEmptySessions();
}

function pruneEmptySessions() {
  const now = Date.now();
  let changed = false;
  for (const [id, s] of Object.entries(state.sessions)) {
    const empty = !Array.isArray(s.messages) || s.messages.length === 0;
    const stale = now - (s.created || 0) > STALE_EMPTY_MS;
    if (empty && stale && id !== state.sessionId) {
      delete state.sessions[id];
      changed = true;
    }
  }
  if (changed) saveState();
}

function saveState() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.sessions));
  if (state.sessionId) {
    localStorage.setItem(ACTIVE_SESSION_KEY, state.sessionId);
  } else {
    localStorage.removeItem(ACTIVE_SESSION_KEY);
  }
}

function newSessionId() {
  return "sess_" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
}

/** Reuse an existing empty session if present so refresh / "+" don't pile blanks. */
function findEmptySession() {
  return Object.values(state.sessions).find(
    (s) => !Array.isArray(s.messages) || s.messages.length === 0
  );
}

function ensureSession() {
  if (state.sessionId && state.sessions[state.sessionId]) {
    return state.sessions[state.sessionId];
  }
  const reusable = findEmptySession();
  if (reusable) {
    state.sessionId = reusable.id;
    saveState();
    return reusable;
  }
  const id = newSessionId();
  state.sessions[id] = {
    id,
    title: "New conversation",
    created: Date.now(),
    sentiment: "neutral",
    messages: [],
  };
  state.sessionId = id;
  saveState();
  return state.sessions[state.sessionId];
}

const thread = $("#thread");
const empty = $("#empty-state");

function renderEmpty(show) {
  if (!empty) return;
  empty.style.display = show ? "" : "none";
}

function deleteSession(id) {
  const wasActive = state.sessionId === id;
  delete state.sessions[id];
  if (wasActive) {
    const remaining = Object.values(state.sessions).sort((a, b) => b.created - a.created);
    state.sessionId = remaining.length ? remaining[0].id : null;
    ensureSession();
  }
  saveState();
  renderAll();
}

function renderSessionList() {
  const list = $("#session-list");
  list.innerHTML = "";
  const sessions = Object.values(state.sessions).sort((a, b) => b.created - a.created);
  for (const s of sessions) {
    const li = document.createElement("li");
    li.dataset.id = s.id;
    if (s.id === state.sessionId) li.classList.add("active");
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.dataset.state = s.sentiment || "neutral";
    li.appendChild(dot);
    const title = document.createElement("span");
    title.textContent = s.title || "Untitled";
    li.appendChild(title);
    const del = document.createElement("button");
    del.className = "session-delete";
    del.title = "Delete conversation";
    del.textContent = "×";
    del.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSession(s.id);
    });
    li.appendChild(del);
    li.addEventListener("click", () => {
      state.sessionId = s.id;
      saveState();
      renderAll();
    });
    list.appendChild(li);
  }
}

function renderThread() {
  const session = ensureSession();
  thread.querySelectorAll(".msg").forEach((n) => n.remove());
  if (!session.messages.length) {
    renderEmpty(true);
    return;
  }
  renderEmpty(false);
  for (const m of session.messages) {
    if (m.role === "user") thread.appendChild(buildUserMessage(m));
    else thread.appendChild(buildBotMessage(m));
  }
  thread.scrollTop = thread.scrollHeight;
}

function formatTimestamp(ts) {
  try {
    return new Date(ts || Date.now()).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (_) {
    return "";
  }
}

function buildUserMessage(m) {
  const text = typeof m === "string" ? m : m.content;
  const ts = typeof m === "object" ? m.timestamp : null;
  const tpl = document.getElementById("tpl-message-user");
  const node = tpl.content.firstElementChild.cloneNode(true);
  node.querySelector(".msg-user__bubble").textContent = text;
  if (ts) {
    const time = document.createElement("div");
    time.className = "msg-time msg-time--user";
    time.textContent = formatTimestamp(ts);
    node.appendChild(time);
  }
  return node;
}

function renderMarkdown(text) {
  const raw = String(text || "");
  if (!raw.trim()) return "";
  const hasMarked = typeof window !== "undefined" && typeof window.marked !== "undefined";
  const hasDOMPurify = typeof window !== "undefined" && typeof window.DOMPurify !== "undefined";
  if (!hasMarked) {
    return escapeHtml(raw).replaceAll("\n", "<br>");
  }
  if (window.marked.setOptions) {
    window.marked.setOptions({ breaks: true, gfm: true });
  }
  const parser = window.marked.parse || window.marked;
  let html;
  try {
    html = parser(raw);
  } catch (_) {
    return escapeHtml(raw).replaceAll("\n", "<br>");
  }
  if (hasDOMPurify) {
    html = window.DOMPurify.sanitize(html, {
      ALLOWED_TAGS: [
        "p","br","strong","em","b","i","u","s","del","code","pre","blockquote",
        "ul","ol","li","h1","h2","h3","h4","h5","h6","a","hr","table","thead","tbody","tr","th","td","span"
      ],
      ALLOWED_ATTR: ["href","title","target","rel","class"],
    });
  }
  return html;
}

function toolOneLiner(t) {
  const r = t.result || {};
  const name = t.name || "";
  try {
    switch (name) {
      case "lookup_order": {
        if (r.ok === false && r.error) return String(r.error);
        if (r.found) {
          const xs = Array.isArray(r.related_products) && r.related_products.length;
          const tail = xs ? ` · +${r.related_products.length} related` : "";
          return `Order ${r.order_id} · ${r.status} · ETA ${r.eta || "—"}${tail}`;
        }
        return r.message || "Order not found.";
      }
      case "process_refund": {
        const mailNote = r.email && r.email.sent ? " · email sent" : "";
        if (r.ok) return r.idempotent ? `Refund already on file (#${r.refund_id})${mailNote}.` : `Refund #${r.refund_id} · ${r.status} · ${r.amount || ""}${mailNote}`;
        return r.message || r.error || "Refund blocked.";
      }
      case "get_account_info":
        return r.ok ? `${r.name || "Customer"} · tier ${r.tier} · ${r.total_orders ?? 0} orders` : r.message || String(r.error || "No profile");
      case "search_customer_orders":
        return r.ok
          ? `${r.count ?? r.orders?.length ?? 0} order(s)` + (r.message ? ` · ${r.message}` : "")
          : r.error || String(r.message || "Order search failed");
      case "search_product_knowledge": {
        const n = r.results?.length || r.count || 0;
        if (r.error) return String(r.error);
        return n ? `Matched ${n} catalog row(s) passed to the model.` : "No catalog hits for this query.";
      }
      case "search_policy_knowledge": {
        const n = r.results?.length || r.count || 0;
        if (r.error) return String(r.error);
        return n ? `Matched ${n} policy row(s).` : r.message || "No policy hits.";
      }
      case "escalate_to_human": {
        if (!r.ok) return String(r.error || "Escalation failed");
        const mailNote = r.email && r.email.sent ? " · email sent" : "";
        return `Ticket ${r.ticket_id} · priority ${r.priority}${mailNote}`;
      }
      case "send_customer_email":
        return r.ok ? `Email log status: ${r.status}` : String(r.error || "Send failed");
      default:
        break;
    }
  } catch (_) {
    /* fall through */
  }
  return r.message || (r.ok === false ? JSON.stringify(r).slice(0, 120) : "Completed.");
}

function similarityBarPercent(score) {
  const x = Number(score);
  if (!Number.isFinite(x)) return null;
  let pct;
  if (x >= 0 && x <= 1) pct = x * 100;
  else if (x > 1 && x <= 100) pct = x;
  else pct = Math.min(100, Math.max(0, 50 + x * 10));
  return Math.min(100, Math.max(4, Math.round(pct)));
}

/** Friendly tool labels for Help Center transcripts (no snake_case names). */
function toolCustomerTitle(name) {
  const map = {
    lookup_order: "Order details",
    search_customer_orders: "Your orders",
    process_refund: "Refund request",
    get_account_info: "Account profile",
    search_product_knowledge: "Catalog search",
    search_policy_knowledge: "Policies & timelines",
    escalate_to_human: "Specialist handoff",
    send_customer_email: "Customer email",
  };
  const n = String(name || "");
  return map[n] || n.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** One-line result text with less internal jargon for shoppers. */
function toolCustomerBlurb(t) {
  let s = toolOneLiner(t);
  s = s.replace(
    /Matched (\d+) catalog row\(s\) passed to the model\./g,
    "Found $1 relevant catalog items."
  );
  s = s.replace(/Matched (\d+) policy row\(s\)\./g, "Found $1 policy references.");
  s = s.replace(/^Completed\.$/, "Finished.");
  return s;
}

function collectRecommendationProductsFromTools(tools) {
  const seen = new Set();
  const out = [];
  function push(p) {
    if (!p || typeof p !== "object") return;
    const asin = String(p.asin || "").trim();
    if (!asin || seen.has(asin)) return;
    seen.add(asin);
    out.push({
      asin,
      title: p.title || asin,
      category: p.category,
      price: p.price != null ? p.price : null,
      stars: p.stars,
      score: p.score,
    });
  }
  for (const t of tools || []) {
    const r = t.result || {};
    const rel = r.related_products;
    if (Array.isArray(rel)) rel.forEach(push);
    const refList = r.reference_listings;
    if (Array.isArray(refList)) refList.forEach(push);
    if (t.name === "search_product_knowledge" && r.ok !== false && Array.isArray(r.results)) {
      r.results.forEach(push);
    }
  }
  return out;
}

function collectPolicyRowsFromTools(tools) {
  const out = [];
  for (const t of tools || []) {
    if (t.name !== "search_policy_knowledge") continue;
    const r = t.result || {};
    if (!Array.isArray(r.results)) continue;
    for (const row of r.results) {
      if (row && (row.topic || row.text)) out.push(row);
    }
  }
  return out;
}

/** Status chips when something concrete was completed this turn (refund, ticket, verified order). */
function getCustomerResolutionChips(m) {
  const tools = Array.isArray(m.tools_called) ? m.tools_called : [];
  const hasRefund = tools.some(
    (t) => t.name === "process_refund" && t.result && t.result.ok !== false
  );
  const chips = [];
  for (const t of tools) {
    const r = t.result || {};
    if (t.name === "process_refund" && r.ok !== false) {
      chips.push({
        tone: "success",
        text: r.idempotent ? "Refund already on file for this issue" : "Refund started — confirmation by email when applicable",
      });
      continue;
    }
    if (t.name === "escalate_to_human" && r.ok !== false) {
      const tid = r.ticket_id ? ` · ${r.ticket_id}` : "";
      chips.push({ tone: "info", text: `Handed to our team${tid}` });
      continue;
    }
    if (t.name === "lookup_order" && r.ok !== false && r.found && !hasRefund) {
      chips.push({ tone: "done", text: "Order verified" });
    }
  }
  /** De-duplicate same text; cap at 2. */
  const seen = new Set();
  return chips.filter((c) => {
    if (seen.has(c.text)) return false;
    seen.add(c.text);
    return true;
  }).slice(0, 2);
}

function buildCustomerProductCard(p, variant) {
  const asin = escapeHtml(p.asin || "");
  const title = escapeHtml(p.title || p.asin || "Item");
  const meta = [];
  if (p.category) meta.push(escapeHtml(p.category));
  if (p.price != null && p.price !== "")
    meta.push("$" + Number(p.price).toFixed(2));
  const pct = similarityBarPercent(p.score);
  const amz =
    asin ? `https://www.amazon.com/dp/${encodeURIComponent(p.asin)}` : "#";
  const badge =
    variant === "rec"
      ? `<span class="customer-pcard__badge">Suggested</span>`
      : `<span class="customer-pcard__badge customer-pcard__badge--muted">Source</span>`;
  const matchLine =
    pct != null
      ? `<div class="customer-pcard__match" aria-hidden="true"><span style="width:${pct}%"></span></div><span class="customer-pcard__match-label">Relevance ${pct}%</span>`
      : "";
  return `<div class="customer-pcard">
    ${badge}
    <div class="customer-pcard__title">${title}</div>
    <div class="customer-pcard__meta">${meta.join(" · ")}</div>
    ${matchLine}
    ${
      asin
        ? `<a class="customer-pcard__link" href="${amz}" target="_blank" rel="noopener">View on Amazon →</a>`
        : ""
    }
  </div>`;
}

function buildCustomerPolicyCard(row) {
  const topic = escapeHtml(row.topic || "Policy");
  const text = escapeHtml(String(row.text || "").replace(/\s+/g, " ").trim());
  const excerpt = text.length > 280 ? text.slice(0, 277) + "…" : text;
  const sec = row.section ? escapeHtml(row.section) : "";
  const src = row.source ? escapeHtml(row.source) : "";
  const meta = [sec, src].filter(Boolean).join(" · ");
  const pct = similarityBarPercent(row.score);
  const matchLine =
    pct != null
      ? `<div class="customer-policy-card__match" aria-hidden="true"><span style="width:${pct}%"></span></div><span class="customer-pcard__match-label">Relevance ${pct}%</span>`
      : "";
  return `<div class="customer-policy-card">
    <span class="customer-pcard__badge customer-pcard__badge--muted">Policy</span>
    <div class="customer-policy-card__topic">${topic}</div>
    ${meta ? `<div class="customer-policy-card__meta">${meta}</div>` : ""}
    <p class="customer-policy-card__text">${excerpt}</p>
    ${matchLine}
  </div>`;
}

function buildCustomerEvidenceSection(m) {
  const tools = Array.isArray(m.tools_called) ? m.tools_called : [];
  const cites = Array.isArray(m.rag_sources) ? m.rag_sources : [];
  const policies = collectPolicyRowsFromTools(tools);
  const ragAsins = new Set((cites || []).map((c) => c.asin).filter(Boolean));
  const recsRaw = collectRecommendationProductsFromTools(tools);
  const recs = recsRaw.filter((p) => p.asin && !ragAsins.has(p.asin));

  if (!tools.length && !cites.length && !recs.length && !policies.length) return null;

  const wrap = document.createElement("div");
  wrap.className = "msg-evidence msg-evidence--customer";

  if (tools.length) {
    const box = document.createElement("section");
    box.className = "customer-ev-block";
    box.innerHTML =
      `<h4 class="customer-ev-block__title">What we ran for you</h4>` +
      `<p class="customer-ev-block__lede">Backed by live lookups — not guesses.</p>`;
    const ul = document.createElement("ul");
    ul.className = "customer-tool-list";
    ul.setAttribute("aria-label", "Support actions");
    for (const t of tools) {
      const ok = t.result && t.result.ok !== false;
      const li = document.createElement("li");
      li.className = "customer-tool-item" + (ok ? "" : " customer-tool-item--warn");
      const title = escapeHtml(toolCustomerTitle(t.name));
      const blurb = escapeHtml(toolCustomerBlurb(t));
      li.innerHTML = `
        <span class="customer-tool-item__glyph" aria-hidden="true">${ok ? "✓" : "!"}</span>
        <div class="customer-tool-item__body">
          <span class="customer-tool-item__name">${title}</span>
          <p class="customer-tool-item__line">${blurb}</p>
          <span class="customer-tool-item__tech mono">${escapeHtml(t.name)}</span>
        </div>`;
      ul.appendChild(li);
    }
    box.appendChild(ul);
    wrap.appendChild(box);
  }

  if (cites.length) {
    const box = document.createElement("section");
    box.className = "customer-ev-block";
    box.innerHTML = `<h4 class="customer-ev-block__title">Sources &amp; citations</h4>
      <p class="customer-ev-block__lede">Catalog matches we leaned on while composing this reply.</p>
      <div class="customer-pcard-grid" role="list"></div>`;
    const grid = box.querySelector(".customer-pcard-grid");
    cites.forEach((c) => {
      const holder = document.createElement("div");
      holder.setAttribute("role", "listitem");
      holder.innerHTML = buildCustomerProductCard(c, "cite");
      grid.appendChild(holder);
    });
    wrap.appendChild(box);
  }

  if (policies.length) {
    const box = document.createElement("section");
    box.className = "customer-ev-block";
    box.innerHTML = `<h4 class="customer-ev-block__title">Policy references</h4>
      <p class="customer-ev-block__lede">Excerpts retrieved from our support policy guides.</p>
      <div class="customer-policy-grid" role="list"></div>`;
    const grid = box.querySelector(".customer-policy-grid");
    policies.slice(0, 5).forEach((row) => {
      const holder = document.createElement("div");
      holder.setAttribute("role", "listitem");
      holder.innerHTML = buildCustomerPolicyCard(row);
      grid.appendChild(holder);
    });
    wrap.appendChild(box);
  }

  if (recs.length) {
    const box = document.createElement("section");
    box.className = "customer-ev-block";
    box.innerHTML = `<h4 class="customer-ev-block__title">Product recommendations</h4>
      <p class="customer-ev-block__lede">Picks related to what you asked or your purchase context.</p>
      <div class="customer-pcard-grid" role="list"></div>`;
    const grid = box.querySelector(".customer-pcard-grid");
    recs.slice(0, 6).forEach((p) => {
      const holder = document.createElement("div");
      holder.setAttribute("role", "listitem");
      holder.innerHTML = buildCustomerProductCard(p, "rec");
      grid.appendChild(holder);
    });
    wrap.appendChild(box);
  }

  return wrap;
}

function buildEvidenceSection(m) {
  const tools = Array.isArray(m.tools_called) ? m.tools_called : [];
  const cites = Array.isArray(m.rag_sources) ? m.rag_sources : [];
  if (!tools.length && !cites.length) return null;

  const wrap = document.createElement("div");
  wrap.className = "msg-evidence";

  if (tools.length) {
    const det = document.createElement("details");
    det.className = "evidence-panel";
    det.open = true;
    const ta = document.createElement("summary");
    ta.className = "evidence-summary";
    const okAll = tools.every((t) => t.result && t.result.ok !== false);
    ta.innerHTML = `<span class="badge badge-det mono">det</span><span class="evidence-summary__label">Server lookups</span><span class="evidence-summary__meta mono">${tools.length} · ${okAll ? "all ok" : "check errors"}</span>`;
    det.appendChild(ta);
    const ul = document.createElement("ul");
    ul.className = "lookup-steps";
    tools.forEach((t, i) => {
      const ok = t.result && t.result.ok !== false;
      const li = document.createElement("li");
      li.className = "lookup-step" + (ok ? "" : " lookup-step--bad");
      const title = document.createElement("div");
      title.className = "lookup-step__top";
      title.innerHTML = `<span class="lookup-step__i mono">${String(i + 1).padStart(2, "0")}</span><span class="lookup-step__name mono">${escapeHtml(t.name)}</span><span class="lookup-step__pill ${ok ? "ok" : "err"} mono">${ok ? "ok" : "err"}</span>`;
      const line = document.createElement("div");
      line.className = "lookup-step__line mono";
      line.textContent = toolOneLiner(t);
      li.appendChild(title);
      li.appendChild(line);
      const argsJson = t.arguments && Object.keys(t.arguments).length;
      if (argsJson) {
        const pre = document.createElement("pre");
        pre.className = "lookup-step__args mono";
        pre.textContent = JSON.stringify(t.arguments, null, 2);
        li.appendChild(pre);
      }
      ul.appendChild(li);
    });
    det.appendChild(ul);
    wrap.appendChild(det);
  }

  if (cites.length) {
    const det = document.createElement("details");
    det.className = "evidence-panel";
    det.open = true;
    const tb = document.createElement("summary");
    tb.className = "evidence-summary";
    tb.innerHTML = `<span class="badge badge-det mono">dense</span><span class="evidence-summary__label">Catalog citations</span><span class="evidence-summary__meta mono">${cites.length} row(s)</span>`;
    det.appendChild(tb);
    const ol = document.createElement("ol");
    ol.className = "citation-list";
    cites.forEach((s, idx) => {
      const li = document.createElement("li");
      li.className = "citation-card";
      const pct = similarityBarPercent(s.score);
      const meta = [];
      if (s.category) meta.push(escapeHtml(s.category));
      if (s.price != null) meta.push("$" + Number(s.price).toFixed(2));
      const asin = escapeHtml(s.asin || "");
      const amz = asin ? `https://www.amazon.com/dp/${encodeURIComponent(s.asin)}` : "#";
      li.innerHTML = `
        <div class="citation-card__row">
          <span class="citation-idx mono">[${idx + 1}]</span>
          <div class="citation-card__body">
            <div class="citation-card__title">${escapeHtml(s.title || s.asin || "Untitled")}</div>
            <div class="citation-card__meta mono">${meta.join(" · ")}</div>
            ${pct != null ? `<div class="sim-bar" aria-label="Similarity ${pct}%"><span class="sim-bar__fill" style="width:${pct}%"></span></div><div class="sim-bar__label mono">${pct}% match</div>` : ""}
            ${asin ? `<a class="citation-link mono" href="${amz}" target="_blank" rel="noopener">ASIN ${asin} →</a>` : ""}
          </div>
        </div>`;
      ol.appendChild(li);
    });
    det.appendChild(ol);
    wrap.appendChild(det);
  }

  return wrap;
}

function buildBotMessage(m) {
  const tpl = document.getElementById("tpl-message-bot");
  const node = tpl.content.firstElementChild.cloneNode(true);
  const bubble = node.querySelector(".msg-bot__bubble");
  const copy = node.querySelector(".msg-bot__copy");
  copy.classList.add("md");
  copy.innerHTML = renderMarkdown(m.content);
  copy.querySelectorAll('a[href^="http"]').forEach((a) => {
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener noreferrer");
  });
  const meta = node.querySelector(".msg-meta");
  if (isCustomerFacing()) {
    meta.setAttribute("hidden", "");
    meta.innerHTML = "";
    const chips = [];
    if (m.customer_verified) {
      chips.push({ tone: "success", text: "✓ Identity verified" });
    }
    chips.push(...getCustomerResolutionChips(m));
    if (chips.length) {
      const row = document.createElement("div");
      row.className = "customer-resolution-row";
      row.setAttribute("aria-label", "Status");
      for (const c of chips) {
        const el = document.createElement("span");
        el.className = `customer-resolution-chip customer-resolution-chip--${c.tone}`;
        el.textContent = c.text;
        row.appendChild(el);
      }
      bubble.insertBefore(row, copy);
    }
    if (m.timestamp) {
      const time = document.createElement("div");
      time.className = "msg-time";
      time.textContent = formatTimestamp(m.timestamp);
      node.querySelector(".msg-bot__stack").appendChild(time);
    }
    const customerEv = buildCustomerEvidenceSection(m);
    if (customerEv) node.querySelector(".msg-bot__stack").appendChild(customerEv);
  } else {
    meta.removeAttribute("hidden");
    meta.innerHTML = "";
    if (m.sentiment) meta.appendChild(metaPill(`sentiment · ${m.sentiment}`, "det"));
    if (m.intent && m.intent !== "general_inquiry")
      meta.appendChild(metaPill(`intent · ${m.intent.replaceAll("_", " ")}`, "det"));
    if (m.escalated) meta.appendChild(metaPill("escalated", "warn"));
    if (Array.isArray(m.tools_called) && m.tools_called.length) {
      meta.appendChild(metaPill(`${m.tools_called.length} tool call(s)`, "llm"));
    }
    const evidence = buildEvidenceSection(m);
    if (evidence) node.querySelector(".msg-bot__stack").appendChild(evidence);
  }
  return node;
}

function metaPill(text, kind) {
  const span = document.createElement("span");
  span.className = kind === "warn" ? "tag warn" : kind === "llm" ? "tag llm-mini" : "tag det";
  span.textContent = text;
  return span;
}

function showTyping() {
  renderEmpty(false);
  const node = document.createElement("article");
  node.className = "msg msg-bot";
  node.id = "typing-indicator";
  const bubbleCls = isCustomerFacing() ? "msg-bot__bubble msg-bot__bubble--customer" : "msg-bot__bubble";
  const inner = isCustomerFacing()
    ? `
    <div class="msg-bot__accent" aria-hidden="true"></div>
    <div class="msg-bot__stack">
      <div class="${bubbleCls}">
        <div class="typing-indicator typing-indicator--customer" aria-live="polite">
          <span class="dot-typing"><span></span><span></span><span></span></span>
          <span>Writing a reply…</span>
        </div>
      </div>
    </div>`
    : `
    <div class="msg-bot__accent" aria-hidden="true"></div>
    <div class="msg-bot__stack">
      <div class="msg-bot__bubble">
        <span class="badge badge-llm mono" style="margin-bottom:8px;display:inline-flex;">gpt</span>
        <div class="typing-indicator mono" style="font-size:12px;color:var(--subtle);">
          <span class="dot-typing"><span></span><span></span><span></span></span> generating
        </div>
      </div>
    </div>`;
  node.innerHTML = inner;
  thread.appendChild(node);
  thread.scrollTop = thread.scrollHeight;
}

function hideTyping() {
  document.getElementById("typing-indicator")?.remove();
}

function renderSignals(data) {
  if (isCustomerFacing()) return;
  const sChip = $("#sentiment-chip");
  const iChip = $("#intent-chip");
  const sLabel = $("#sentiment-label");
  const iLabel = $("#intent-label");
  if (!sChip || !iChip || !sLabel || !iLabel) return;
  const sentiment = data?.sentiment || "neutral";
  const escalated = !!data?.escalated;
  sChip.dataset.state = escalated ? "frustrated" : sentiment;
  sLabel.textContent = escalated ? "Escalated" : capitalise(sentiment);
  iLabel.textContent = "intent · " + (data?.intent || "—").replaceAll("_", " ");
  iChip.classList.toggle("muted", !data?.intent || data.intent === "general_inquiry");
}

function renderToolsPanel(invocations) {
  const panel = $("#tools-panel");
  if (!panel) return;
  if (!invocations || !invocations.length) {
    panel.innerHTML = `<p class="empty-hint mono">No deterministic tools on the latest assistant message.</p>`;
    return;
  }
  panel.innerHTML = "";
  const hdr = document.createElement("p");
  hdr.className = "rail-lede mono";
  const nOk = invocations.filter((t) => t.result && t.result.ok !== false).length;
  hdr.textContent = `${invocations.length} call(s) · ${nOk} succeeded`;
  panel.appendChild(hdr);
  for (const t of invocations) {
    const ok = t.result && t.result.ok !== false;
    const wrap = document.createElement("div");
    wrap.className = "tool-block";
    wrap.innerHTML = `
      <div class="tool-head">
        <span class="tool-name">${escapeHtml(t.name)}</span>
        <span class="tool-status ${ok ? "ok" : "err"}">${ok ? "ok" : "error"}</span>
      </div>
      <div class="tool-blurb mono">${escapeHtml(toolOneLiner(t))}</div>
      <details class="tool-raw"><summary class="mono tool-raw-sum">Arguments + raw JSON</summary>
      <pre class="tool-json">${escapeHtml(JSON.stringify({ arguments: t.arguments, result: t.result }, null, 2))}</pre>
      </details>`;
    panel.appendChild(wrap);
  }
}

function renderRagPanel(sources) {
  const panel = $("#rag-panel");
  if (!panel) return;
  if (!sources || !sources.length) {
    panel.innerHTML = `<p class="empty-hint mono">No vector citations on the latest assistant message (run ingest or ask a product question).</p>`;
    return;
  }
  panel.innerHTML = "";
  const hdr = document.createElement("p");
  hdr.className = "rail-lede mono";
  hdr.textContent = `Top-${sources.length} Qdrant neighbours (cosine)`;
  panel.appendChild(hdr);
  sources.forEach((s, idx) => {
    const meta = [];
    if (s.category) meta.push(escapeHtml(s.category));
    if (s.price != null) meta.push("$" + Number(s.price).toFixed(2));
    const pct = similarityBarPercent(s.score);
    const asin = escapeHtml(s.asin || "");
    const amz = asin ? `https://www.amazon.com/dp/${encodeURIComponent(s.asin)}` : "#";
    const div = document.createElement("div");
    div.className = "kb-hit";
    div.innerHTML = `
      <div class="kb-hit__head">
        <span class="citation-idx mono">[${idx + 1}]</span>
        <span class="kb-hit__title">${escapeHtml(s.title || s.asin)}</span>
      </div>
      <div class="kb-hit__meta">${meta.join(" · ")}</div>
      ${pct != null ? `<div class="sim-bar"><span class="sim-bar__fill" style="width:${pct}%"></span></div><div class="sim-bar__label mono">${pct}%</div>` : ""}
      ${asin ? `<a class="citation-link mono" href="${amz}" target="_blank" rel="noopener">ASIN ${asin} →</a>` : `<div class="kb-hit__asin mono">${asin}</div>`}`;
    panel.appendChild(div);
  });
}

function renderAll() {
  renderSessionList();
  renderThread();
  const session = ensureSession();
  const lastBot = [...session.messages].reverse().find((m) => m.role === "assistant");
  renderSignals(lastBot || {});
  renderToolsPanel(lastBot?.tools_called || []);
  renderRagPanel(lastBot?.rag_sources || []);
}

function escapeHtml(str) {
  return String(str)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function capitalise(s) {
  return (s || "").charAt(0).toUpperCase() + (s || "").slice(1);
}

const _EMAIL_LIKE_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

async function sendMessage(text) {
  if (state.inflight || !text.trim()) return;
  state.inflight = true;
  const session = ensureSession();
  session.messages.push({ role: "user", content: text, timestamp: Date.now() });
  if (session.messages.length === 1) {
    const isEmail = _EMAIL_LIKE_RE.test(text.trim());
    session.title = isEmail
      ? "Identity verification"
      : text.length > 40 ? text.slice(0, 40) + "…" : text;
  }
  saveState();
  renderAll();
  showTyping();
  try {
    const resp = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text,
        session_id: session.id,
      }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    hideTyping();
    session.messages.push({
      role: "assistant",
      content: data.reply,
      sentiment: data.sentiment,
      intent: data.intent,
      escalated: data.escalated,
      customer_verified: data.customer_verified || false,
      tools_called: data.tools_called || [],
      rag_sources: data.rag_sources || [],
      timestamp: Date.now(),
    });
    session.sentiment = data.escalated ? "frustrated" : data.sentiment;
    saveState();
    renderAll();
  } catch (err) {
    hideTyping();
    const msg = err.message || "";
    const warmingUp = msg.includes("502") || msg.includes("503");
    const errReply = isCustomerFacing()
      ? (warmingUp
          ? "Our AI assistant is warming up — please try again in a moment."
          : "We couldn't reach support just now. Please try again in a few seconds.")
      : `Offline or server error · ${msg}`;
    session.messages.push({
      role: "assistant",
      content: errReply,
      sentiment: "neutral",
      intent: "error",
      tools_called: [],
      rag_sources: [],
      timestamp: Date.now(),
    });
    saveState();
    renderAll();
  } finally {
    state.inflight = false;
  }
}

async function loadCatalogStats() {
  if (isCustomerFacing()) return;
  const el = $("#catalog-stat");
  if (!el) return;
  try {
    const r = await fetch(`${API_BASE}/rag/stats`);
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    const n = Number(j.points || 0);
    el.textContent = n ? n.toLocaleString() + " pts" : "0 pts";
    el.dataset.state = j.status === "green" || n > 0 ? "ok" : "warn";
  } catch (_) {
    el.textContent = "offline";
    el.dataset.state = "err";
  }
}

let catalogSearchTimer = null;
let catalogSearchSeq = 0;

function debounceCatalogSearch(query, immediate = false) {
  if (catalogSearchTimer) clearTimeout(catalogSearchTimer);
  if (immediate) {
    runCatalogSearch(query);
    return;
  }
  catalogSearchTimer = setTimeout(() => runCatalogSearch(query), 280);
}

function renderCatalogFacets(results, out) {
  const counts = new Map();
  results.forEach((s) => {
    if (!s.category) return;
    counts.set(s.category, (counts.get(s.category) || 0) + 1);
  });
  if (!counts.size) return;
  const top = Array.from(counts.entries()).sort((a, b) => b[1] - a[1]).slice(0, 3);
  const wrap = document.createElement("div");
  wrap.className = "catalog-facets mono";
  wrap.innerHTML = `<span class="catalog-facets__label">Top categories</span>`;
  top.forEach(([cat, n]) => {
    const chip = document.createElement("span");
    chip.className = "catalog-facet";
    chip.textContent = `${cat} · ${n}`;
    wrap.appendChild(chip);
  });
  out.appendChild(wrap);
}

function buildCatalogCard(s, idx) {
  const pct = similarityBarPercent(s.score);
  const meta = [];
  if (s.category) meta.push(escapeHtml(s.category));
  if (s.price != null) meta.push("$" + Number(s.price).toFixed(2));
  if (s.stars != null) meta.push(`★ ${Number(s.stars).toFixed(1)}`);
  const asin = escapeHtml(s.asin || "");
  const amz = asin ? `https://www.amazon.com/dp/${encodeURIComponent(s.asin)}` : "#";
  const card = document.createElement("div");
  card.className = "catalog-card";
  card.tabIndex = 0;
  card.dataset.title = s.title || s.asin || "";
  card.dataset.asin = s.asin || "";
  card.innerHTML = `
    <div class="catalog-card__row">
      <span class="citation-idx mono">[${idx + 1}]</span>
      <div class="catalog-card__body">
        <div class="catalog-card__title">${escapeHtml(s.title || s.asin || "Untitled")}</div>
        <div class="catalog-card__meta mono">${meta.join(" · ")}</div>
        ${pct != null ? `<div class="sim-bar"><span class="sim-bar__fill" style="width:${pct}%"></span></div><div class="sim-bar__label mono">${pct}% match</div>` : ""}
        <div class="catalog-card__actions">
          ${asin ? `<a class="citation-link mono" href="${amz}" target="_blank" rel="noopener">ASIN ${asin} →</a>` : ""}
          <button type="button" class="catalog-card__ask mono" data-action="ask">Ask Atlas →</button>
        </div>
      </div>
    </div>`;
  return card;
}

async function runCatalogSearch(query) {
  const out = $("#catalog-results");
  if (!out) return;
  const q = (query || "").trim();
  if (!q) {
    out.innerHTML = `<p class="empty-hint mono">Type a query above to explore the live index.</p>`;
    return;
  }
  const seq = ++catalogSearchSeq;
  out.innerHTML = `<p class="empty-hint mono">Searching live index…</p>`;
  try {
    const r = await fetch(`${API_BASE}/rag/search?q=${encodeURIComponent(q)}&top_k=6`);
    if (seq !== catalogSearchSeq) return;
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      throw new Error(detail.detail || `HTTP ${r.status}`);
    }
    const j = await r.json();
    if (seq !== catalogSearchSeq) return;
    if (!j.results || !j.results.length) {
      out.innerHTML = `<p class="empty-hint mono">No matches yet — index may still be filling for that vocabulary.</p>`;
      return;
    }
    out.innerHTML = "";
    renderCatalogFacets(j.results, out);
    j.results.forEach((s, idx) => out.appendChild(buildCatalogCard(s, idx)));
  } catch (err) {
    if (seq !== catalogSearchSeq) return;
    out.innerHTML = `<p class="empty-hint mono" style="color:var(--error-fg);">Catalog error · ${escapeHtml(err.message)}</p>`;
  }
}

async function pingHealth() {
  const dot = $("#status-dot");
  const txt = $("#status-text");
  const okMsg = isCustomerFacing() ? "We're online" : "All systems nominal";
  const degradedMsg = isCustomerFacing() ? "Brief delay — retry shortly" : "Degraded upstream";
  try {
    const r = await fetch(`${API_BASE}/health`);
    const j = await r.json();
    dot.classList.remove("ok", "degraded", "error");
    if (j.status === "ok") {
      dot.classList.add("ok");
      txt.textContent = okMsg;
    } else {
      dot.classList.add("degraded");
      txt.textContent = degradedMsg;
    }
  } catch {
    dot.classList.remove("ok", "degraded", "error");
    dot.classList.add("error");
    txt.textContent = isCustomerFacing() ? "Unable to reach support right now" : "Offline";
  }
}

function bind() {
  const composer = $("#composer");
  const input = $("#input");

  function autoresize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }

  input.addEventListener("input", autoresize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      composer.requestSubmit();
    }
  });

  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    autoresize();
    sendMessage(text);
  });

  $$(".chip").forEach((btn) =>
    btn.addEventListener("click", () => {
      const val = btn.dataset.text;
      input.value = val;
      autoresize();
      composer.requestSubmit();
    })
  );

  $("#new-session").addEventListener("click", () => {
    const current = state.sessions[state.sessionId];
    if (current && (!current.messages || current.messages.length === 0)) {
      input.focus();
      return;
    }
    const reusable = findEmptySession();
    if (reusable) {
      state.sessionId = reusable.id;
    } else {
      state.sessionId = null;
      ensureSession();
    }
    saveState();
    renderAll();
    input.focus();
  });

  const catalogForm = document.getElementById("catalog-form");
  const catalogInput = document.getElementById("catalog-input");
  const catalogResults = document.getElementById("catalog-results");
  if (catalogForm && catalogInput) {
    catalogForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = catalogInput.value.trim();
      if (!q) return;
      debounceCatalogSearch(q, true);
    });
    catalogInput.addEventListener("input", () => {
      const q = catalogInput.value.trim();
      if (q.length < 3) return;
      debounceCatalogSearch(q);
    });
  }
  if (catalogResults) {
    catalogResults.addEventListener("click", (e) => {
      const card = e.target.closest(".catalog-card");
      if (!card) return;
      const askBtn = e.target.closest('[data-action="ask"]');
      const title = card.dataset.title || "";
      const asin = card.dataset.asin || "";
      if (!title && !asin) return;
      const prompt = asin
        ? `Tell me more about ${title} (ASIN ${asin}). Is it in stock and what's the return policy?`
        : `Tell me more about "${title}".`;
      input.value = prompt;
      autoresize();
      input.focus();
      if (askBtn) {
        composer.requestSubmit();
      }
    });
  }
}

function initLanding() {
  const overlay = document.getElementById("intro-overlay");
  if (!overlay) return;

  if (document.documentElement.classList.contains("landing-seen")) {
    overlay.setAttribute("aria-hidden", "true");
    document.body.classList.add("intro-done");
    document.body.classList.remove("app-boot");
    return;
  }

  overlay.setAttribute("aria-hidden", "false");
  requestAnimationFrame(() => overlay.classList.add("intro-visible"));

  function finish() {
    if (overlay.classList.contains("intro-leaving")) return;
    overlay.classList.add("intro-leaving");
    document.documentElement.classList.add("intro-complete");
    try {
      localStorage.setItem("atlas_landing_v1", "1");
    } catch (_) {}

    const done = () => {
      overlay.style.display = "none";
      overlay.setAttribute("aria-hidden", "true");
      document.documentElement.classList.add("landing-seen");
      document.body.classList.add("intro-done");
      document.body.classList.remove("app-boot");
    };
    overlay.addEventListener(
      "transitionend",
      function te(ev) {
        if (ev.propertyName === "opacity" || ev.propertyName === "visibility") {
          overlay.removeEventListener("transitionend", te);
          done();
        }
      },
      { passive: true }
    );
    window.setTimeout(done, 520);
  }

  document.getElementById("intro-enter")?.addEventListener("click", finish);
  window.addEventListener("keydown", function onEnter(ev) {
    if (ev.key !== "Enter") return;
    if (!overlay.classList.contains("intro-visible") || overlay.classList.contains("intro-leaving")) return;
    const tgt = ev.target;
    const tag = tgt?.tagName;
    if (
      tag === "TEXTAREA" ||
      tag === "INPUT" ||
      tag === "SELECT" ||
      tag === "BUTTON" ||
      tgt?.isContentEditable
    )
      return;
    ev.preventDefault();
    window.removeEventListener("keydown", onEnter);
    finish();
  });
}

loadState();
ensureSession();
initLanding();
bind();
renderAll();
pingHealth();
if (!isCustomerFacing()) {
  loadCatalogStats();
}
setInterval(pingHealth, 30000);
if (!isCustomerFacing()) {
  setInterval(loadCatalogStats, 15000);
}
