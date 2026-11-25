// ============================================================================
// admin.js - the ADMIN role (after the password): Live call, Knowledge, Sales dashboard,
// Quality and Users tabs. Uses $ / esc from call-core.js; adminApi() adds the login token.
// The SERVER enforces every permission; the page only hides what you can't use.
// ============================================================================
// The token lives only in this browser tab (sessionStorage): closing the tab logs you out.
let token = null;
try { token = sessionStorage.getItem("adminToken"); } catch (e) {}
function saveToken(t) {
  token = t;
  try { t ? sessionStorage.setItem("adminToken", t) : sessionStorage.removeItem("adminToken"); } catch (e) {}
}

async function adminApi(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  const res = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  if (res.status === 401 && path !== "/api/auth/login") { saveToken(null); showLogin("Session expired, please log in again."); throw new Error("Not logged in"); }
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

let toastTimer;
function toast(msg, bad = false) {
  const t = $("toast");
  t.textContent = msg; t.className = bad ? "bad" : "";
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 4000);
}

// ---------------- Who am I (RBAC) ----------------
// The server enforces every permission; the page only hides what you can't use.
let me = null;
const can = (p) => !!me && me.permissions.includes(p);
const TABS = { live: "dashboard:read", kb: "docs:read", dash: "dashboard:read", quality: "quality:read", users: "users:manage" };
const SECTION = { live: "live", kb: "app", dash: "dash", quality: "quality", users: "users" };
const ADMIN_PARTS = ["live", "app", "dash", "quality", "users", "logout", "tabs", "health", "mode"];

function hideAdmin() {
  for (const id of ADMIN_PARTS) $(id).classList.add("hidden");
  $("who").textContent = "";
}

function showLogin(msg = "") {
  me = null;
  if (currentRole !== "admin") return;     // a 401 while the customer view is open: nothing to show
  if (sessionId) endCall();
  hideAdmin();
  $("login").classList.remove("hidden");
  setTimeout(() => $("password").focus(), 0);
  $("loginErr").textContent = msg; $("mode").textContent = ""; $("who").textContent = "";
  // wipe customer data from the page
  for (const id of ["stats", "leads", "appts", "calls", "qStats", "qEval", "qMix", "qTraces", "qAudit", "uList"]) $(id).innerHTML = "";
  adminApi("/api/auth/config").then((c) => {
    $("googleBtn").classList.toggle("hidden", !c.google);
    $("loginForm").classList.toggle("hidden", !c.password);
    $("pwHint").classList.toggle("hidden", !c.password);
    $("orLine").classList.toggle("hidden", !(c.google && c.password));
    if (!c.google && !c.password && !msg) $("loginErr").textContent = "No sign-in method is configured. Set GOOGLE_CLIENT_ID/SECRET or ADMIN_PASSWORD in .env.";
  }).catch(() => {});
}
async function showApp() {
  me = await adminApi("/api/auth/me");
  $("who").innerHTML = `${esc(me.name || me.email)} <span class="role">${esc(me.role)}</span>`;
  $("login").classList.add("hidden"); $("logout").classList.remove("hidden"); $("tabs").classList.remove("hidden");
  for (const [tab, perm] of Object.entries(TABS)) $("tab-" + tab).classList.toggle("hidden", !can(perm));
  // Knowledge tab details
  $("uploadCard").classList.toggle("hidden", !can("docs:write"));        // upload box
  $("searchScope").querySelector("option[value=internal]").hidden = !can("search:internal");
  $("auditCard").classList.toggle("hidden", !can("audit:read"));
  let tab = "live";
  try { tab = sessionStorage.getItem("adminTab") || "live"; } catch (e) {}
  if (!can(TABS[tab])) tab = Object.keys(TABS).find((t) => can(TABS[t])) || "dash";
  showTab(tab);
}
function showTab(name) {
  for (const t of Object.keys(TABS)) {
    $(SECTION[t]).classList.toggle("hidden", name !== t);
    $("tab-" + t).classList.toggle("active", name === t);
  }
  $("mode").classList.toggle("hidden", name !== "kb");
  $("health").classList.toggle("hidden", name !== "live");
  try { sessionStorage.setItem("adminTab", name); } catch (e) {}
  const load = { live: async () => loadHealth(), kb: loadDocs, dash: loadDashboard, quality: loadQuality, users: loadUsers }[name];
  load().catch((err) => toast(err.message, true));
}

// ---------------- Sales dashboard ----------------
async function loadDashboard() {
  const d = await adminApi("/api/admin/dashboard");
  const s = d.stats;
  const stat = (n, label) => `<div class="card stat"><div class="n">${n}</div><div class="muted">${label}</div></div>`;
  $("stats").innerHTML = stat(s.calls, "Calls handled") + stat(s.leads, "Leads in CRM") + stat(s.hot_leads, "Hot leads (≥70)") +
    stat(s.demos_booked, "Demos booked") + stat(s.handoffs, "Human handoffs") + stat(s.avg_score, "Avg lead score") +
    stat(s.avg_turn_ms ? s.avg_turn_ms + " ms" : "–", "Avg response time") + stat(s.live_calls, "Live calls now");

  $("leads").innerHTML = d.leads.length ? `<table><tr><th>Lead</th><th>Score</th><th>Status</th><th>Need / Budget / Timeline</th><th>Lang</th><th>Customer link</th></tr>` +
    d.leads.map(l => `<tr><td><b>${esc(l.name || "Unknown")}</b><br><span class="muted">${esc(l.phone)}${l.company ? " · " + esc(l.company) : ""}</span></td>
      <td><b>${esc(l.score ?? 0)}</b></td><td><span class="chip">${esc(l.status)}</span></td>
      <td class="muted">${esc(l.need || "—")}<br>${esc(l.budget || "—")} · ${esc(l.timeline || "—")}</td><td>${esc(l.language || "")}</td>
      <td style="white-space:nowrap">${can("leads:link") ? `<button class="ghost" style="padding:3px 9px" data-link="${esc(l.id)}">Copy</button>
        <button class="del" data-reset="${esc(l.id)}" title="Make a new link; the old one stops working">Reset</button>` : "—"}</td></tr>`).join("") + "</table>"
    : "<p class='muted'>No leads yet. Make a call first.</p>";

  // Only allow http(s) links, so a bad value can't become a javascript: link.
  const safeLink = (u) => /^https?:\/\//i.test(u || "") ? esc(u) : "#";
  $("appts").innerHTML = d.appointments.length ? d.appointments.map(a =>
    `<div class="summary-card"><b>${esc(a.start)} IST</b> · ${esc(a.name || a.phone)}<br>
     <span class="muted">${esc(a.topic)}</span> · <a href="${safeLink(a.link)}" target="_blank" rel="noopener">Google Calendar</a></div>`).join("")
    : "<p class='muted'>No demos booked yet.</p>";

  $("calls").innerHTML = d.calls.length ? d.calls.map(c => {
    const s = c.summary || {};
    return `<div class="summary-card">
      <div style="display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap"><b>${esc(c.name || c.phone)}</b><span class="muted">${esc(c.created_at)}</span></div>
      <p style="margin:6px 0">${esc(s.summary)}</p>
      <span class="tier ${esc(s.tier || "cold")}">${esc(s.tier || "cold")}</span>
      <span class="chip">score ${esc(s.lead_score ?? 0)}</span><span class="chip">${esc(s.outcome)}</span><span class="chip">${esc(s.language)}</span>
      ${(s.next_steps || []).length ? `<div class="muted" style="margin-top:6px">Next: ${s.next_steps.map(esc).join(" · ")}</div>` : ""}
      <details style="margin-top:6px"><summary class="muted">Transcript</summary>
        ${(c.transcript || []).map(t => `<div><b>${t.who === "agent" ? "Aria" : "Customer"}:</b> ${esc(t.text)}</div>`).join("")}</details>
    </div>`;
  }).join("") : "<p class='muted'>No calls yet.</p>";
}

$("pwToggle").onclick = () => {
  const show = $("password").type === "password";
  $("password").type = show ? "text" : "password";
  $("pwToggle").textContent = show ? "Hide" : "Show";
  $("pwToggle").setAttribute("aria-label", show ? "Hide password" : "Show password");
  $("password").focus();
};

$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("loginErr").textContent = "";
  try {
    // trim(): a pasted password often carries a space or line break at the end.
    const r = await adminApi("/api/auth/login", { method: "POST", body: { password: $("password").value.trim() } });
    saveToken(r.token); $("password").value = ""; $("password").type = "password"; $("pwToggle").textContent = "Show"; showApp();
  } catch (err) {
    $("loginErr").textContent = err.message === "Wrong password"
      ? "Wrong password. If your browser filled it in, click Show to check it, or clear it and paste the value from .env."
      : err.message;
  }
});
$("logout").onclick = async () => {
  if (sessionId) await endCall();
  try { await adminApi("/api/auth/logout", { method: "POST" }); } catch (e) {}
  saveToken(null); goRole("");
};

// ---------------- Document lists ----------------
let maxUploadMb = 50;
const kb = (n) => n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";

async function loadDocs() {
  const r = await adminApi("/api/admin/documents");
  maxUploadMb = r.max_upload_mb || maxUploadMb;     // the server's MAX_UPLOAD_MB setting
  $("maxMb").textContent = maxUploadMb;
  $("mode").textContent = `Search: public ${r.search_mode.public} · internal ${r.search_mode.internal}`;
  $("internalCard").classList.toggle("hidden", !can("search:internal"));
  for (const scope of ["public", "internal"]) {
    const docs = r.documents[scope];
    $("list-" + scope).innerHTML = docs.length ? `<table>
      <tr><th>File</th><th class="num">Size</th><th class="num">Chunks</th><th></th></tr>
      ${docs.map(d => `<tr>
        <td class="name"><a data-view="${esc(d.name)}" data-scope="${scope}">${esc(d.name)}</a></td>
        <td class="num muted">${kb(d.size)}</td><td class="num">${d.chunks}</td>
        <td class="num">${can("docs:write") ? `<button class="del" data-del="${esc(d.name)}" data-scope="${scope}">Delete</button>` : ""}</td>
      </tr>`).join("")}</table>` : `<p class="muted small">No documents yet.</p>`;
  }
}

document.addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]"), view = e.target.closest("[data-view]");
  const link = e.target.closest("[data-link]"), reset = e.target.closest("[data-reset]");
  if (link || reset) {
    // Customer's private "my calls" link, to send by SMS / WhatsApp / email.
    const id = (link || reset).dataset.link || reset.dataset.reset;
    if (reset && !confirm("Make a new link for this customer? The old link will stop working.")) return;
    try {
      const r = await adminApi(`/api/admin/leads/${encodeURIComponent(id)}/portal-link`, { method: "POST", body: { reset: !!reset } });
      const url = location.origin + r.path;
      try { await navigator.clipboard.writeText(url); toast(reset ? "New link copied. The old one no longer works." : "Customer link copied"); }
      catch (err) { prompt("Copy the customer's link:", url); }
    } catch (err) { toast(err.message, true); }
    return;
  }
  if (del) {
    const { del: name, scope } = del.dataset;
    if (!confirm(`Delete ${name} from ${scope}? This can't be undone.`)) return;
    try {
      await adminApi(`/api/admin/documents/${scope}/${encodeURIComponent(name)}`, { method: "DELETE" });
      toast(`Deleted ${name}`); loadDocs();
    } catch (err) { toast(err.message, true); }
  } else if (view) {
    const { view: name, scope } = view.dataset;
    try {
      const r = await adminApi(`/api/admin/documents/${scope}/${encodeURIComponent(name)}`);
      $("previewName").textContent = r.name;
      $("previewBadge").innerHTML = `<span class="badge ${scope}">${scope.toUpperCase()}</span>`;
      $("previewText").textContent = r.text;
      $("preview").showModal();
    } catch (err) { toast(err.message, true); }
  }
});

// ---------------- Upload ----------------
const drop = $("drop");
drop.onclick = () => $("file").click();
$("file").onchange = () => { uploadFiles([...$("file").files]); $("file").value = ""; };
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); uploadFiles([...e.dataTransfer.files]); });

function toBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(new Error("Could not read the file"));
    r.readAsDataURL(file);
  });
}

async function uploadFiles(files) {
  const scope = document.querySelector("input[name=scope]:checked").value;
  for (const f of files) {
    const line = document.createElement("div");
    line.textContent = `⏳ ${f.name} → ${scope}…`;
    $("queue").prepend(line);
    try {
      if (f.size > maxUploadMb * 1024 * 1024) throw new Error(`larger than ${maxUploadMb} MB`);
      const r = await adminApi("/api/admin/documents", { method: "POST",
        body: { filename: f.name, content_b64: await toBase64(f), scope } });
      line.textContent = `✅ ${r.name} → ${r.scope} (${r.chunks} chunks${r.replaced ? ", replaced old version" : ""})`;
    } catch (err) {
      line.textContent = `❌ ${f.name}: ${err.message}`; line.style.color = "var(--bad)";
    }
  }
  loadDocs();
}

// ---------------- Search ----------------
$("searchForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("hits").innerHTML = `<p class="muted small">Searching…</p>`;
  try {
    const r = await adminApi("/api/admin/search", { method: "POST", body: { query: $("q").value, scope: $("searchScope").value } });
    $("hits").innerHTML = r.hits.length ? r.hits.map(h => `<div class="hit">
        <div class="src"><span>${esc(h.source)}</span><span>score ${h.score}</span></div>
        <pre>${esc(h.text)}</pre></div>`).join("")
      : `<p class="muted small">Nothing relevant found in ${esc(r.scope)} (${esc(r.mode)} search).</p>`;
  } catch (err) { $("hits").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
});

// ---------------- Quality: observability + evals ----------------
const pct = (x) => Math.round((x || 0) * 100) + "%";
const chips = (obj) => Object.entries(obj || {}).sort((a, b) => b[1] - a[1])
  .map(([k, v]) => `<span class="chip">${esc(k)} · ${esc(v)}</span>`).join("") || "<span class='muted small'>none yet</span>";

async function loadQuality() {
  const q = await adminApi("/api/admin/quality");
  const t = q.traces, e = q.eval;
  const stat = (n, label) => `<div class="card stat"><div class="n">${n}</div><div class="muted">${label}</div></div>`;
  $("qStats").innerHTML = stat(t.turns, "Turns traced") + stat(t.p50_ms + " ms", "p50 response") + stat(t.p95_ms + " ms", "p95 response") +
    stat(pct(t.cache_hit_rate), "Cache hit rate") + stat(t.avg_tokens, "Avg tokens / turn") +
    stat("$" + (t.total_cost_usd || 0).toFixed(4), "LLM cost (priced models)") + stat(t.errors, "Errors") +
    stat(e ? pct(e.summary.pass_rate) : "–", "Eval pass rate");

  $("qEval").innerHTML = !e ? `<p class="muted">No eval run yet. Run <code>python evals/run.py</code> on the server.</p>` : `
    <div class="kv"><b>Last run</b><span>${esc(e.summary.date)}</span><b>Prompt version</b><span><code>${esc(e.summary.prompt_version)}</code></span>
      <b>Passed</b><span>${e.summary.passed} / ${e.summary.total}</span></div>
    <div class="bar" style="margin:10px 0"><i style="width:${pct(e.summary.pass_rate)}"></i></div>
    <div>${chips(e.summary.by_category)}</div>
    ${e.cases.filter(c => !c.pass).map(c => `<div class="summary-card" style="margin-top:8px"><span class="fail">FAIL</span> <b>${esc(c.id)}</b>
      ${c.checks.filter(x => !x.pass).map(x => `<div class="small muted">✗ ${esc(x.check)}: ${esc(x.detail)}</div>`).join("")}
      <div class="small">“${esc(c.reply || "")}”</div></div>`).join("")}
    ${(e.history || []).length > 1 ? `<div class="small muted" style="margin-top:8px">Trend: ${e.history.map(h => pct(h.pass_rate)).join(" → ")}</div>` : ""}`;

  $("qMix").innerHTML = `<div class="small muted">Route</div><div>${chips(t.routes)}</div>
    <div class="small muted" style="margin-top:8px">Model that answered</div><div>${chips(t.models)}</div>
    <div class="small muted" style="margin-top:8px">Guardrail hits</div><div>${chips(t.guard_hits)}</div>
    <div class="small muted" style="margin-top:8px">Prompt versions</div><div>${chips(t.prompt_versions)}</div>
    <div class="small muted" style="margin-top:8px">Answer cache (since restart): ${q.cache.entries} entries · ${q.cache.hits} hits · ${q.cache.semantic_hits} semantic</div>`;

  const tr = await adminApi("/api/admin/traces?limit=40");
  $("qTraces").innerHTML = tr.traces.length ? `<table><tr><th>Time</th><th>Customer said</th><th>Route</th><th>Model</th><th class="num">ms</th><th class="num">Tokens</th><th>Guard</th></tr>
    ${tr.traces.map(x => `<tr class="click" data-trace="${esc(x.id)}"><td class="muted">${esc((x.created_at || "").slice(11))}</td>
      <td>${esc((x.input || "").slice(0, 60))}</td><td><span class="chip">${esc(x.route)}</span>${x.cache_hit ? '<span class="chip">cache</span>' : ""}</td>
      <td class="small">${esc((x.model || "").split("/").slice(-1)[0])}</td><td class="num">${x.total_ms}</td>
      <td class="num">${(x.input_tokens || 0) + (x.output_tokens || 0)}</td>
      <td class="small">${(x.guard || []).map(g => esc(g.rule)).join(", ")}${x.error ? ' <span class="fail">error</span>' : ""}</td></tr>`).join("")}</table>`
    : "<p class='muted'>No turns yet. Make a call on the main page.</p>";

  if (can("audit:read")) {
    const a = await adminApi("/api/admin/audit?limit=50");
    $("qAudit").innerHTML = a.audit.length ? `<table><tr><th>Time</th><th>Who</th><th>Role</th><th>Action</th><th>Detail</th></tr>
      ${a.audit.map(x => `<tr><td class="muted">${esc(x.created_at)}</td><td>${esc(x.actor)}</td><td>${esc(x.role)}</td>
        <td><span class="chip">${esc(x.action)}</span></td><td class="small">${esc(x.detail)}</td></tr>`).join("")}</table>`
      : "<p class='muted'>Nothing logged yet.</p>";
  }
}

async function showTrace(id) {
  const t = await adminApi(`/api/admin/traces/${encodeURIComponent(id)}`);
  const total = Math.max(t.total_ms, 1);
  let offset = 0;
  const spans = t.spans.map(s => {
    const left = Math.min(offset / total * 100, 100); offset += s.ms;
    return `<div class="span"><span>${esc(s.name)}${s.step !== undefined ? " " + esc(s.step) : ""}${s.tool ? " · " + esc(s.tool) : ""}</span>
      <div class="track"><i style="left:${left}%;width:${Math.min(s.ms / total * 100, 100 - left)}%"></i></div><span class="muted">${s.ms} ms</span></div>`;
  }).join("");
  $("previewName").textContent = `Trace ${t.id}`;
  $("previewBadge").innerHTML = `<span class="chip">${esc(t.route)}</span>${t.cache_hit ? '<span class="chip">cache hit</span>' : ""}`;
  $("previewText").innerHTML = `<div class="kv">
      <b>Customer</b><span>${esc(t.input)}</span><b>Aria</b><span>${esc(t.output)}</span>
      <b>Prompt</b><span><code>${esc(t.prompt_version)}</code></span><b>Model</b><span>${esc(t.model)}</span>
      <b>Tokens</b><span>${t.input_tokens} in · ${t.output_tokens} out · $${(t.cost_usd || 0).toFixed(5)}</span>
      <b>Facts used</b><span>${(t.sources || []).map(esc).join("<br>") || "—"}</span></div>
    <h3 style="margin-top:14px">Where the ${t.total_ms} ms went</h3>${spans}
    ${t.guard.length ? `<h3 style="margin-top:14px">Guardrails</h3>` + t.guard.map(g => `<div class="small">${esc(g.stage)} · <b>${esc(g.rule)}</b> → ${esc(g.action)} ${esc(g.detail)}</div>`).join("") : ""}
    ${t.tools.length ? `<h3 style="margin-top:14px">Tools</h3><pre class="json">${esc(JSON.stringify(t.tools, null, 2))}</pre>` : ""}
    <h3 style="margin-top:14px">LLM calls</h3><pre class="json">${esc(JSON.stringify(t.llm_calls, null, 2))}</pre>
    ${t.error ? `<p class="err">${esc(t.error)}</p>` : ""}`;
  $("preview").showModal();
}
document.addEventListener("click", (e) => {
  const row = e.target.closest("[data-trace]");
  if (row) showTrace(row.dataset.trace).catch((err) => toast(err.message, true));
});

// ---------------- Users (admin only) ----------------
let userRows = [];
async function loadUsers() {
  const r = await adminApi("/api/admin/users");
  userRows = r.users;
  $("uList").innerHTML = r.users.length ? `<table><tr><th>Email</th><th>Name</th><th>Role</th><th>Active</th><th>Last sign-in</th><th></th></tr>
    ${r.users.map((u, i) => `<tr><td>${esc(u.email)}</td><td>${esc(u.name || "")}</td><td><span class="role">${esc(u.role)}</span></td>
      <td>${u.active ? "yes" : '<span class="fail">no</span>'}</td><td class="muted">${esc(u.last_login || "never")}</td>
      <td><button class="ghost" style="padding:3px 9px" data-edit-user="${i}">Edit</button></td></tr>`).join("")}</table>`
    : "<p class='muted'>No users yet. Add one above, or list an email in ADMIN_EMAILS to bootstrap the first admin.</p>";
  $("uRoles").innerHTML = Object.entries(r.roles).map(([role, perms]) =>
    `<div style="margin-bottom:6px"><span class="role">${esc(role)}</span> ${perms.map(p => `<span class="chip">${esc(p)}</span>`).join("")}</div>`).join("");
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-edit-user]");
  if (!b) return;
  const u = userRows[Number(b.dataset.editUser)];
  $("uEmail").value = u.email; $("uName").value = u.name || ""; $("uRole").value = u.role; $("uActive").checked = !!u.active;
  $("uEmail").focus();
});
$("userForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await adminApi("/api/admin/users", { method: "POST", body: { email: $("uEmail").value.trim(), name: $("uName").value.trim(),
      role: $("uRole").value, active: $("uActive").checked } });
    toast("User saved"); $("userForm").reset(); $("uActive").checked = true; loadUsers();
  } catch (err) { toast(err.message, true); }
});
