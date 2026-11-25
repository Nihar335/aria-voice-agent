// ============================================================================
// cockpit.js - the ADMIN "Live call" tab: the same call (call-core.js) + the live cockpit
// on the right (lead score, sentiment, BANT, latency, reasoning trace, insights).
// ============================================================================
let pollTimer = null;

const COCKPIT_HOOKS = {
  startBody: () => ({ phone: $("phone").value, mode: "demo" }),
  onStart(r) {
    $("trace").innerHTML = "";
    resetCockpit();
    if (r.returning) {
      // The earlier summary stays on the server: anyone can type a phone number, so the page never shows it.
      addBanner("memory", `🧠 Returning caller${r.previous_date ? " (last call " + esc(r.previous_date) + ")" : ""}. Aria remembers the last call; the details stay in the CRM.`);
    }
    clearInterval(pollTimer);
    pollTimer = setInterval(pollLive, 1200);
  },
  turnMeta: (r) => r.sources
    ? [r.language, r.sources.length ? "📄 " + r.sources.length + " source(s)" : ""].filter(Boolean).join(" · ")
    : (r.voice ? `voice: ${r.voice}` : ""),
  onTurn: (r) => showTurnDetails(r),
  beforeEnd: () => clearInterval(pollTimer),
  onEnd: (r) => showSummary(r.summary, r.lead, r.portal_url),
  endedStatus: "Call ended and saved to CRM.",
  handoffText: "🙋 Handed over to a human sales consultant — lead flagged in CRM with full context.",
};

// Per-turn details (from the reply)
function showTurnDetails(r) {
  const L = r.latency, max = Math.max(L.total_ms, 1);
  const row = (name, ms) => `<div class="lat-row"><span>${name}</span><div class="bar"><div style="width:${Math.min(100, ms / max * 100)}%"></div></div><span>${ms} ms</span></div>`;
  $("latency").innerHTML = row("RAG", L.rag_ms) + row("LLM", L.llm_ms) + row("Voice", L.tts_ms || 0) + row("Total", L.total_ms);
  const tok = r.tokens ? ` · ${r.tokens.in + r.tokens.out} tokens` : "";
  $("provider").textContent = `LLM: ${r.provider} · route: ${r.route || "-"}${tok} · voice: ${r.voice}${r.trace_id ? " · trace " + r.trace_id : ""}`;

  // Reasoning trace: guardrails -> RAG -> router/cache -> tools -> thought (newest turn on top)
  let html = "";
  for (const e of r.events) {
    if (e.type === "guard") html += `<div class="step tool">🛡️ Guardrail: ${esc(e.detail)}</div>`;
    if (e.type === "route") html += `<div class="step">🧭 Router: ${esc(e.detail)}</div>`;
    if (e.type === "cache") html += `<div class="step rag">⚡ Cache: ${esc(e.detail)}</div>`;
    if (e.type === "rag") html += `<div class="step rag">📚 RAG: ${esc(e.detail)} (${e.ms} ms)${e.sources.length ? "<br>" + e.sources.map(s => `<span class="chip">${esc(s)}</span>`).join("") : "<br><span class='muted'>No matching facts → agent must not guess</span>"}</div>`;
    if (e.type === "tool") html += `<div class="step tool">🛠️ Tool <code>${esc(e.detail)}</code>(${esc(JSON.stringify(e.args || {}))})<br><span class="muted">→ ${esc(JSON.stringify(e.result).slice(0, 220))}</span></div>`;
  }
  html += r.thoughts.filter(Boolean).map(t => `<div class="step">💭 ${esc(t)}</div>`).join("");
  $("trace").innerHTML = `<div style="border-bottom:1px solid var(--line);padding-bottom:6px;margin-bottom:6px">${html}</div>` + $("trace").innerHTML;
}

// Live analysis (polling)
async function pollLive() {
  if (!sessionId) return;
  try {
    const d = await api(`/api/call/${sessionId}/live`);
    const a = d.analysis || {};
    if (a.score === undefined) return;
    $("score").textContent = a.score;
    $("tier").textContent = a.tier; $("tier").className = "tier " + a.tier;
    $("intent").textContent = a.intent ? "Intent: " + a.intent : "";
    $("sentiment").textContent = a.sentiment || "–";
    $("sentiment").style.color = a.sentiment === "positive" ? "var(--good)" : a.sentiment === "negative" ? "var(--bad)" : "var(--text)";
    $("emotion").textContent = a.emotion ? "Emotion: " + a.emotion : "";
    $("alert").textContent = a.alert || "";
    drawSparkline(a.sentiment_history || []);
    const bant = a.bant || {};
    $("bant").innerHTML = ["need", "budget", "authority", "timeline"].map(k => {
      const b = bant[k] || {}, st = b.status || "unknown";
      return `<div class="bant-row"><span><b>${k[0].toUpperCase() + k.slice(1)}</b><br><span class="muted">${esc(b.value || "—")}</span></span><span class="st-${st}">${st === "clear" ? "✔ " : st === "partial" ? "◐ " : "○ "}${st}</span></div>`;
    }).join("");
    const list = (arr) => (arr || []).map(x => `<span class="chip">${esc(x)}</span>`).join("") || "<span class='muted'>—</span>";
    $("insights").innerHTML = `<div><b>Buying signals:</b> ${list(a.buying_signals)}</div>
      <div style="margin-top:6px"><b>Objections:</b> ${list(a.objections)}</div>
      <div style="margin-top:6px"><b>Next best action:</b> ${esc(a.next_best_action || "—")}</div>
      ${a.recap ? `<div style="margin-top:6px"><b>Customer sees (live summary):</b> ${esc(a.recap)}</div>` : ""}`;
  } catch (e) { /* call may have ended */ }
}

function drawSparkline(values) {
  // Sentiment from -1 (bottom) to +1 (top), one point per customer turn.
  if (values.length < 1) { $("spark").innerHTML = ""; return; }
  const pts = values.map((v, i) => [values.length === 1 ? 100 : i * 200 / (values.length - 1), 20 - v * 18]);
  $("spark").innerHTML = `<line x1="0" y1="20" x2="200" y2="20" stroke="#26314f" stroke-dasharray="3"/>
    <polyline fill="none" stroke="#5b8cff" stroke-width="2" points="${pts.map(p => p.join(",")).join(" ")}"/>
    ${pts.map(p => `<circle cx="${p[0]}" cy="${p[1]}" r="2.5" fill="#7c5cff"/>`).join("")}`;
}

function resetCockpit() {
  $("score").textContent = "–"; $("tier").textContent = "cold"; $("tier").className = "tier cold";
  ["intent", "emotion", "alert", "provider"].forEach(id => $(id).textContent = "");
  $("sentiment").textContent = "–"; $("spark").innerHTML = "";
  $("bant").innerHTML = "<p class='muted'>Updates after each customer turn.</p>";
  $("latency").innerHTML = "<p class='muted'>No turns yet.</p>";
  $("insights").textContent = "Buying signals, objections and the next best action appear here.";
}

// End-of-call summary popup (internal view: score, objections, sales next steps)
function showSummary(s, lead, portalUrl) {
  const list = (arr) => (arr || []).length ? "<ul>" + arr.map(x => `<li>${esc(x)}</li>`).join("") + "</ul>" : "<p class='muted'>—</p>";
  $("modalBody").innerHTML = `
    <h3>Call summary</h3>
    <p style="font-size:15px">${esc(s.summary)}</p>
    <p><span class="tier ${esc(s.tier)}">${esc(s.tier)}</span> &nbsp;Score <b>${esc(s.lead_score)}</b> · Outcome <b>${esc(s.outcome)}</b> · Language <b>${esc(s.language)}</b> · ${esc(s.turns)} customer turns</p>
    ${portalUrl ? `<div class="banner booked" style="margin:0 0 12px">📄 <b>Customer's private summary page</b><br>
      <span class="muted">The customer can open this any time to read their own call summaries (no internal notes).</span><br>
      <a href="${esc(portalUrl)}" target="_blank" rel="noopener">${esc(location.origin + portalUrl.split("#")[0])}#…</a>
      <button class="ghost" style="padding:3px 10px;margin-left:6px" id="copyPortal">Copy link</button></div>`
      : returningCaller ? `<div class="banner memory" style="margin:0 0 12px">📄 Returning caller: their private summary page is the link from their first call (staff can resend it from the dashboard).</div>` : ""}
    <p><b>Lead:</b> ${esc(lead.name || "unknown")} · ${esc(lead.phone)} ${lead.email ? "· " + esc(lead.email) : ""} ${lead.company ? "· " + esc(lead.company) : ""}</p>
    <b>Needs</b>${list(s.customer_needs)}
    <b>Objections</b>${list(s.objections)}
    <b>Next steps for sales team</b>${list(s.next_steps)}
    <p><b>Follow-up:</b> ${esc(s.follow_up || "—")}</p>
    <p class="muted"><b>Agent feedback:</b> ${esc(s.agent_feedback || "—")}</p>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="primary" onclick="$('modal').classList.add('hidden')">Close</button>
    </div>`;
  if (portalUrl) $("copyPortal").onclick = (e) => navigator.clipboard.writeText(location.origin + portalUrl).then(() => e.target.textContent = "Copied");
  $("modal").classList.remove("hidden");
}

function loadHealth() {
  api("/api/health").then(h => {
    const keys = [h.gemini_key && "Gemini", h.groq_key && "Groq"].filter(Boolean);
    $("health").innerHTML = `LLM: <b>${keys.length ? keys.join(" + ") : "⚠ no API keys"}</b> · RAG: ${esc(h.rag_mode)} (${esc(h.rag_chunks)} chunks)`;
  }).catch(() => $("health").textContent = "server not reachable");
}
