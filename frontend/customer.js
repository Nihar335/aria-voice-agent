// ============================================================================
// customer.js - the CUSTOMER role: the call + a live "Chat summary" panel.
// The server runs these calls in mode "customer" and never sends internal data
// (score, reasoning, sources, objections). The summary panel shows:
//   during the call  a short recap written for the customer (GET /api/call/{id}/recap),
//                    plus any demo that was booked
//   after the call   the customer's summary, next steps, demo and private summary link
// ============================================================================
let recapTimer = null;
let recapTurns = 0;            // customer turns so far (to know when a recap is due)
let bookings = [];

const CUSTOMER_HOOKS = {
  startBody() {
    const phone = $("phone").value.trim();
    if (phone.replace(/\D/g, "").length < 4) { $("phone").focus(); throw new Error("please enter your phone number"); }
    return { phone, mode: "customer" };    // customer mode: the server sends no internal data
  },
  onStart(r) {
    if (r.returning) addBanner("memory", "👋 Welcome back! Aria remembers your last call.");
    recapTurns = 0; bookings = [];
    renderLiveSummary({ recap: "", points: [] });
    clearInterval(recapTimer);
    recapTimer = setInterval(refreshRecap, 2500);
  },
  onTurn(r) {
    recapTurns++;
    if (r.booking) bookings.push(r.booking);
  },
  beforeEnd() { clearInterval(recapTimer); $("sumBody").innerHTML = `<p class="muted">✍️ Writing your call summary…</p>`; },
  handoffText: "🙋 A Fortivex consultant will call you back shortly.",
  endedStatus: "Call ended. Thank you for calling Fortivex AI!",
  onEnd: (r) => showFinalSummary(r),
};

async function refreshRecap() {
  if (!sessionId) return;
  try {
    const r = await api(`/api/call/${sessionId}/recap`);
    renderLiveSummary(r);
  } catch (e) { /* the call may just have ended */ }
}

function bookingHtml(list) {
  return list.map(b => `<div class="banner booked">📅 <b>Demo booked:</b> ${esc(b.slot || b.start)} IST<br>
    <a href="${safeLink(b.calendar_link || b.link)}" target="_blank" rel="noopener">Add to Google Calendar</a></div>`).join("");
}

let lastLive = "";
function renderLiveSummary(r) {
  const points = (r.points || []).map(p => `<li>${esc(p)}</li>`).join("");
  const html = `<span class="live-dot">Live · updates as you talk</span>
    ${r.recap ? `<p class="sum-lead">${esc(r.recap)}</p>`
              : `<p class="muted">${recapTurns ? "Summarising your conversation…" : "Ask Aria anything. A short summary of what you discussed will build up here."}</p>`}
    ${points ? `<h4>Key points</h4><ul>${points}</ul>` : ""}
    ${bookingHtml(bookings)}`;
  if (html !== lastLive) { $("sumBody").innerHTML = html; lastLive = html; }
}

// After the call: only what was written FOR the customer (same as their private "my calls" page).
function showFinalSummary(r) {
  lastLive = "";
  const s = r.summary || {};
  const steps = (s.customer_next_steps || []).map(x => `<li>${esc(x)}</li>`).join("");
  const demos = (r.appointments || []).length ? bookingHtml(r.appointments) : bookingHtml(bookings);
  const link = r.portal_url ? location.origin + r.portal_url : "";
  $("sumBody").innerHTML = `
    <b>✅ Call ended. Thanks for calling Fortivex AI!</b>
    <p class="sum-lead">${esc(s.customer_summary || "Our team will follow up with you soon.")}</p>
    ${steps ? `<h4>What happens next</h4><ul>${steps}</ul>` : ""}
    ${demos}
    ${link ? `<div class="banner memory">📄 <b>Your private summary page</b><br>
        <span class="muted">Open it any time to read your call summaries. Keep the link private.</span><br>
        <a href="${esc(link)}" target="_blank" rel="noopener">Open my summary page</a>
        <button class="ghost" style="margin-left:6px;padding:3px 10px" id="copyLink">Copy link</button></div>`
      : r.returning ? `<p class="muted small">Your private summary page is the link you got after your first call. Lost it? Our team can send you a new one.</p>` : ""}`;
  if (link) $("copyLink").onclick = (e) => navigator.clipboard.writeText(link).then(() => e.target.textContent = "Copied");
  if (window.innerWidth < 900) $("summaryPanel").scrollIntoView({ behavior: "smooth" });
}

function resetCustomerView() {
  lastLive = "";
  $("sumBody").innerHTML = `<p class="muted">Start the call and talk to Aria. A short summary of your chat appears here
    while you talk: what you asked, what Aria told you, and anything you booked.</p>`;
}
