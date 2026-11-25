// ============================================================================
// app.js - the single page's router: "customer or admin?"
//   #customer  -> the call + live chat summary (customer.js)
//   #admin     -> password (or Google) -> admin tabs, incl. the Live call cockpit (admin.js, cockpit.js)
//   (empty)    -> the role chooser
// The ONE call panel (#callPanel) is moved into whichever view is open, with that role's hooks.
// ============================================================================
let currentRole = null;

const INTRO = {
  customer: `<p class="muted">Enter your phone number, press <b>Start call</b>, allow the microphone, and talk to Aria.
    Ask about our services and prices, or book a free 30-minute demo. You can speak English, Hindi, Hinglish, Kannada or Telugu.</p>`,
  admin: `<p class="muted">Press <b>Start call</b>, allow the microphone, and talk to Aria like a customer.
    Try: “I run a dental clinic, can AI answer my calls?”, “kitna kharcha hoga?”, “book a demo tomorrow”.</p>`,
};

function placeCallPanel(role) {
  const panel = $("callPanel");
  $(role === "admin" ? "adminCallSlot" : "customerCallSlot").appendChild(panel);
  window.CALL_HOOKS = role === "admin" ? COCKPIT_HOOKS : CUSTOMER_HOOKS;
  $("chat").innerHTML = INTRO[role];
  $("banners").innerHTML = ""; setStatus("");
  $("phone").value = role === "admin" ? "9876543210" : "";
  $("phone").placeholder = role === "admin" ? "Caller phone number" : "Your phone number";
  $("privacyNote").classList.toggle("hidden", role === "admin");
}

async function showRole(role) {
  if (role !== "customer" && role !== "admin") role = "";
  if (role === currentRole) return;
  if (sessionId) await endCall();          // switching role ends the call in progress
  $("modal").classList.add("hidden");
  currentRole = role;

  for (const id of ["view-role", "login", "view-customer"]) $(id).classList.add("hidden");
  hideAdmin();
  $("switchRole").classList.toggle("hidden", !role);
  $("subtitle").textContent = role === "admin" ? "· Admin" : role === "customer" ? "· Talk to Aria, our AI assistant" : "· Aria, AI sales assistant";
  document.title = role === "admin" ? "Admin · Fortivex AI" : "Aria · Fortivex AI";

  if (role === "customer") {
    placeCallPanel("customer");
    resetCustomerView();
    $("view-customer").classList.remove("hidden");
    setTimeout(() => $("phone").focus(), 0);
  } else if (role === "admin") {
    placeCallPanel("admin");
    if (token) showApp().catch(() => {}); else showLogin();
  } else {
    $("view-role").classList.remove("hidden");
  }
}

// Changing the hash (buttons, back/forward) is what switches the view.
function goRole(role) {
  if (location.hash.slice(1) === role) showRole(role);
  else location.hash = role;                 // -> hashchange -> showRole
  if (!role) history.replaceState(null, "", location.pathname);
}
window.addEventListener("hashchange", () => showRole(location.hash.slice(1)));

// ---------------- Start ----------------
// Google sign-in comes back as /#token=... (or #error=...). The fragment never reaches a server.
(function start() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("token")) saveToken(h.get("token"));
  if (h.get("token") || h.get("error")) {
    history.replaceState(null, "", location.pathname + "#admin");
    showRole("admin").then(() => { if (h.get("error")) { saveToken(null); showLogin(h.get("error")); } });
    return;
  }
  showRole(location.hash.slice(1));
})();
