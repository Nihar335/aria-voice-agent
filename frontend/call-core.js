// ============================================================================
// call-core.js - the call itself, shared by BOTH call pages:
//   index.html  staff demo: the call + the live cockpit (score, BANT, reasoning, latency)
//   call.html   customer page: only the call
// It does: start/end the call, voice OUT (natural MP3 or browser voice), voice IN (Chrome
// speech-to-text), chat messages and banners. Each page sets window.CALL_HOOKS for its extras:
//   startBody()   -> body for POST /api/call/start      onStart(r)  after the call connects
//   turnMeta(r)   -> small text under Aria's message     onTurn(r)   after each reply
//   beforeEnd()   when the customer presses End call     onEnd(r)    with the end-of-call result
//   endedStatus   status text after the call ended
// The page must have: #phone #lang #startBtn #endBtn #banners #chat #mic #typed #status,
// and optionally #chatView #natural #handsfree.
// ============================================================================
const $ = (id) => document.getElementById(id);
const hooks = () => window.CALL_HOOKS || {};

// Escape text before putting it into HTML (stops script injection / XSS).
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, body) {
  const res = await fetch(path, body ? {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  } : {});
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

// Agent language -> speech codes for the microphone and the browser voice.
// Hinglish uses the Indian-English recognizer so Hindi words come out in English letters.
const SPEECH_CODE = { en: "en-IN", hinglish: "en-IN", hi: "hi-IN", kn: "kn-IN", te: "te-IN" };
const checked = (id, fallback) => ($(id) ? $(id).checked : fallback);

// ============================================================================
// Call state
// ============================================================================
let sessionId = null;
let busy = false;          // waiting for the agent's reply
let speaking = false;      // agent audio is playing
let currentAudio = null;
let returningCaller = false;

// ============================================================================
// 1. Start / end the call
// ============================================================================
async function startCall() {
  $("startBtn").disabled = true;
  setStatus("Connecting…");
  try {
    const body = hooks().startBody ? hooks().startBody() : { phone: $("phone").value };
    const r = await api("/api/call/start", body);
    sessionId = r.session_id;
    returningCaller = !!r.returning;
    $("chat").innerHTML = ""; $("banners").innerHTML = "";
    hooks().onStart?.(r);
    $("startBtn").classList.add("hidden"); $("endBtn").classList.remove("hidden");
    $("mic").disabled = false; $("typed").disabled = false;
    addMessage("agent", r.reply, hooks().turnMeta ? hooks().turnMeta(r) : "", r.reply_en);
    await speak(r);
  } catch (e) {
    setStatus("Could not start: " + e.message);
  }
  $("startBtn").disabled = false;
}

async function endCall() {
  if (!sessionId) return;
  stopListening(true); stopAudio();
  hooks().beforeEnd?.();
  setStatus("Writing call summary…");
  const id = sessionId; sessionId = null;
  $("mic").disabled = true; $("typed").disabled = true;
  $("endBtn").classList.add("hidden"); $("startBtn").classList.remove("hidden");
  try {
    const r = await api("/api/call/end", { session_id: id });
    hooks().onEnd?.(r);
    setStatus(hooks().endedStatus || "Call ended. Thank you for calling!");
  } catch (e) { setStatus("End failed: " + e.message); }
}

// ============================================================================
// 2. Send what the customer said, show + speak the reply
// ============================================================================
async function sendTurn(text) {
  text = text.trim();
  if (!text || !sessionId || busy) return;
  busy = true; stopAudio();
  const mine = addMessage("customer", text);
  setStatus("Aria is thinking…");
  try {
    const r = await api("/api/call/turn", { session_id: sessionId, text, natural_voice: checked("natural", true),
                                            language: $("lang").value });
    setTranslation(mine, r.customer_en);
    addMessage("agent", r.reply, hooks().turnMeta ? hooks().turnMeta(r) : "", r.reply_en);
    if (r.booking) {
      addBanner("booked", `📅 Demo booked: ${esc(r.booking.slot)} IST · <a href="${safeLink(r.booking.calendar_link)}" target="_blank" rel="noopener">Add to Google Calendar</a>`);
    }
    if (r.handoff && !document.querySelector(".banner.handoff")) {
      addBanner("handoff", hooks().handoffText || "🙋 A Fortivex consultant will call you back shortly.");
    }
    hooks().onTurn?.(r);
    // If the customer switched language, switch the microphone too. English <-> Hinglish use the same
    // recognizer (en-IN), so the menu stays as the customer set it: the server then answers an English
    // question in English even right after a Hinglish turn.
    if (SPEECH_CODE[r.language] && SPEECH_CODE[r.language] !== SPEECH_CODE[$("lang").value]) $("lang").value = r.language;
    busy = false;
    await speak(r);
  } catch (e) {
    busy = false;
    setStatus("Error: " + e.message);
  }
}

function sendTyped() { const t = $("typed").value; $("typed").value = ""; sendTurn(t); }

// Only allow http(s) links, so a bad value can't become a javascript: link.
function safeLink(u) { return /^https?:\/\//i.test(u || "") ? esc(u) : "#"; }

// ============================================================================
// 3. Voice OUT: play server MP3 (natural voice) or use the browser's voice
// ============================================================================
function speak(r) {
  return new Promise((resolve) => {
    const done = () => {
      speaking = false; setStatus(sessionId ? "Your turn." : "");
      resolve();
      // Hands-free: start listening automatically after the agent finishes.
      if (sessionId && checked("handsfree", true) && !busy) startListening();
    };
    speaking = true;
    setStatus("🔊 Aria is speaking… (click 🎙️ to interrupt)");
    if (r.audio) {
      currentAudio = new Audio("data:audio/mpeg;base64," + r.audio);
      currentAudio.onended = done; currentAudio.onerror = done;
      currentAudio.play().catch(done);
    } else {
      const u = new SpeechSynthesisUtterance(r.reply);
      u.lang = SPEECH_CODE[r.language] || "en-IN";
      const v = pickVoice(u.lang);
      if (v) u.voice = v;
      u.rate = 1.05;
      u.onend = done; u.onerror = done;
      speechSynthesis.speak(u);
    }
  });
}

// The browser voice is only the BACKUP (natural voice off, or the server's voice failed). Left alone,
// browsers pick the system default, which on many Windows PCs is a male voice ("Microsoft David").
// Aria is one woman's voice, so pick a female voice that can read this language.
const FEMALE_VOICE = /female|neerja|swara|heera|kalpana|veena|lekha|sapna|shruti|zira|aria|jenny|sonia|libby|samantha|karen|google (us english|uk english female|हिन्दी|ಕನ್ನಡ|తెలుగు)/i;
const MALE_VOICE = /\bmale\b|david|ravi|hemant|prabhat|madhur|mark|guy|george|daniel|alex|fred|ryan|thomas|google uk english male/i;
function pickVoice(langCode) {
  const base = langCode.split("-")[0];
  const voices = speechSynthesis.getVoices().filter(v => (v.lang || "").replace("_", "-").startsWith(base));
  const score = (v) => (v.lang.replace("_", "-") === langCode ? 2 : 0)
    + (FEMALE_VOICE.test(v.name) ? 4 : 0) - (MALE_VOICE.test(v.name) && !/female/i.test(v.name) ? 6 : 0);
  return voices.sort((a, b) => score(b) - score(a))[0] || null;   // none for this language: browser default
}
if (window.speechSynthesis) {      // voices load asynchronously in Chrome: ask early so they're ready
  speechSynthesis.getVoices();
  speechSynthesis.addEventListener?.("voiceschanged", () => speechSynthesis.getVoices());
}

function stopAudio() {
  if (currentAudio) { currentAudio.onended = null; currentAudio.pause(); currentAudio = null; }
  speechSynthesis.cancel();
  speaking = false;
}

// ============================================================================
// 4. Voice IN: Chrome speech-to-text (Web Speech API)
// ============================================================================
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
// Chrome ends a recognition session on its own (after a pause, ~8 s of silence,
// or ~60 s of speech). We keep the mic "on" across those by reopening a new
// session and accumulating the text, and only send the turn when the customer
// clicks 🎙️ again or has been quiet for SILENCE_MS after saying something.
const SILENCE_MS = 3000;
const FATAL_MIC_ERRORS = ["not-allowed", "service-not-allowed", "audio-capture", "network", "language-not-supported"];
let recognizer = null, listening = false;
let heard = "";            // text from earlier recognition sessions in this turn
let silenceTimer = null;
let discardOnEnd = false;

function startListening() {
  if (!Recognition) { setStatus("Speech recognition needs Google Chrome. You can type instead."); return; }
  if (listening || busy) return;
  listening = true; heard = ""; discardOnEnd = false;
  $("mic").classList.add("listening");
  setStatus("🎙️ Listening… (click 🎙️ when you're done)");
  openRecognizer();
}

function openRecognizer() {
  const r = new Recognition();
  recognizer = r;
  r.lang = SPEECH_CODE[$("lang").value] || "en-IN";   // en-IN, hi-IN, kn-IN, te-IN
  r.interimResults = true;       // show words while the customer is still speaking
  r.continuous = true;           // don't stop at the first pause
  let sessionFinal = "", sessionInterim = "";

  r.onresult = (e) => {
    sessionFinal = ""; sessionInterim = "";
    for (let i = 0; i < e.results.length; i++) {
      if (e.results[i].isFinal) sessionFinal += e.results[i][0].transcript + " ";
      else sessionInterim += e.results[i][0].transcript;
    }
    const soFar = (heard + sessionFinal + sessionInterim).trim();
    setStatus("🎙️ " + soFar);
    // Customer is still talking -> push the auto-send further out.
    clearTimeout(silenceTimer);
    if (soFar) silenceTimer = setTimeout(() => stopListening(), SILENCE_MS);
  };

  r.onerror = (e) => {
    if (FATAL_MIC_ERRORS.includes(e.error)) {
      listening = false;   // don't auto-restart into the same error
      setStatus("Mic error: " + e.error + ". You can type instead.");
    }
  };

  r.onend = () => {
    if (r !== recognizer) return;
    // Keep anything recognized (if Chrome stopped mid-phrase, keep the interim words too).
    heard += sessionFinal + sessionInterim + " ";
    if (listening) {
      // Chrome closed the session by itself — reopen it so the mic stays on.
      setTimeout(() => { if (listening && r === recognizer) openRecognizer(); }, 150);
      return;
    }
    clearTimeout(silenceTimer);
    $("mic").classList.remove("listening");
    recognizer = null;
    const text = heard.trim(); heard = "";
    if (discardOnEnd) return;
    if (text) sendTurn(text);
    else if (sessionId && !busy) setStatus("Didn't catch that. Click 🎙️ to talk.");
  };

  try { r.start(); } catch (e) { listening = false; $("mic").classList.remove("listening"); setStatus("Mic error: " + e.message); }
}

// Finish the turn: stop the mic and send what was heard (or drop it with discard=true).
function stopListening(discard = false) {
  clearTimeout(silenceTimer);
  discardOnEnd = discard;
  if (!listening) return;
  listening = false;
  if (recognizer) recognizer.stop();
}

function toggleMic() {
  if (speaking) stopAudio();          // barge-in: customer interrupts the agent
  listening ? stopListening() : startListening();
}

// ============================================================================
// 5. Chat messages, banners, status
// ============================================================================
// Each message keeps both versions: what was actually said, and an English translation.
// The "Chat:" dropdown picks which one is shown; the other is in the hover tooltip.
let chatView = "original";
try { chatView = localStorage.getItem("chatView") || "original"; } catch (e) {}
document.addEventListener("DOMContentLoaded", () => { if ($("chatView")) $("chatView").value = chatView; });

function addMessage(who, text, meta = "", english = "") {
  const div = document.createElement("div");
  div.className = "msg " + who;
  div._orig = text; div._en = english || ""; div._meta = meta;
  renderMessage(div);
  $("chat").appendChild(div);
  $("chat").scrollTop = $("chat").scrollHeight;
  return div;
}
function setTranslation(div, english) { if (div) { div._en = english || ""; renderMessage(div); } }

function renderMessage(div) {
  const translated = div._en && div._en.trim() !== div._orig.trim();
  const showEn = chatView === "en" && translated;
  const meta = [div._meta, showEn ? "🌐 translated to English" : ""].filter(Boolean).join(" · ");
  div.innerHTML = esc(showEn ? div._en : div._orig) + (meta ? `<div class="meta">${esc(meta)}</div>` : "");
  div.title = translated ? (showEn ? "Said: " + div._orig : "English: " + div._en) : "";
}

function setChatView(v) {
  chatView = v;
  try { localStorage.setItem("chatView", v); } catch (e) {}
  document.querySelectorAll("#chat .msg").forEach(renderMessage);
}
function addBanner(type, html) {
  const div = document.createElement("div");
  div.className = "banner " + type; div.innerHTML = html;
  $("banners").appendChild(div);
}
function setStatus(t) { $("status").textContent = t; }
