// ============================================================================
// call-core.js - the call itself, used by BOTH roles of the single page (index.html):
//   customer   the call + a live chat summary (customer.js)
//   admin      the call + the live cockpit: score, BANT, reasoning, latency (cockpit.js)
// It does: start/end the call, voice OUT (natural voice or browser voice), voice IN (records
// the mic and sends the audio to the server's speech-to-text, Chrome's recognizer as backup),
// chat messages and banners. The active role sets window.CALL_HOOKS for its extras:
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
    // Open the microphone now, while we still have the click: one stream for the whole call, so a
    // Bluetooth headset doesn't switch modes (and cut off the first words) every time you speak.
    const micReady = openMic();
    const r = await api("/api/call/start", body);
    await micReady;
    sessionId = r.session_id;
    returningCaller = !!r.returning;
    $("chat").innerHTML = ""; $("banners").innerHTML = "";
    hooks().onStart?.(r);
    $("startBtn").classList.add("hidden"); $("endBtn").classList.remove("hidden");
    $("mic").disabled = false; $("typed").disabled = false;
    addMessage("agent", r.reply, hooks().turnMeta ? hooks().turnMeta(r) : "", r.reply_en);
    await speak(r);
  } catch (e) {
    if (!sessionId) closeMic();
    setStatus("Could not start: " + e.message);
  }
  $("startBtn").disabled = false;
}

async function endCall() {
  if (!sessionId) return;
  stopListening(true); stopAudio(); closeMic();
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
      currentAudio = new Audio(`data:${r.audio_mime || "audio/mpeg"};base64,${r.audio}`);
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
// 4. Voice IN
//   Main path: we record the microphone ourselves (echo cancellation + noise suppression + auto
//   gain), detect when the customer starts and stops talking, and send that clip as 16 kHz WAV
//   to the server (/api/call/stt), where Whisper or Gemini writes it down. Far more accurate than
//   Chrome's recognizer for Indian accents, earphone mics, Hinglish and Telugu/Kannada.
//   Backup: Chrome's own speech recognition (if the mic can't be opened or the server's
//   speech-to-text keeps failing), so the customer can always talk.
// ============================================================================
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let listening = false;
let discardOnEnd = false;
let transcribing = false;      // a clip is being written down on the server
let useBrowserSTT = false;     // true = fall back to Chrome speech recognition for this call
let sttFailures = 0;

// ---------- 4a. Server speech-to-text (default) ----------
const VAD = {
  MIN_LEVEL: 0.008,      // quietest sound that can count as speech (RMS, 0..1)
  START_FRAMES: 3,       // ~130 ms of sound above the noise floor = the customer started talking
  END_SILENCE_MS: 1300,  // this much quiet after speaking = the customer finished the sentence
  NO_SPEECH_MS: 10000,   // nobody spoke for this long: stop listening
  MAX_MS: 45000,         // one turn is at most this long
  MIN_SPEECH_MS: 250,    // shorter than this = a cough or click, not words
  PRE_ROLL_MS: 300,      // keep this much audio from before the first word (soft first syllables)
  TAIL_MS: 350,          // keep this much quiet after the last word
};
let mic = null;   // { ctx, stream, rate } while a call is open
let rec = null;   // the clip being recorded for this turn

// Runs in the audio thread: hands us the raw microphone samples in blocks of 2048.
const TAP_WORKLET = `class Tap extends AudioWorkletProcessor {
  constructor() { super(); this.buf = new Float32Array(2048); this.n = 0; }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch) for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === this.buf.length) { this.port.postMessage(this.buf.slice(0)); this.n = 0; }
    }
    return true;
  }
}
registerProcessor("tap", Tap);`;

async function openMic() {
  sttFailures = 0; useBrowserSTT = false;
  if (mic) return;
  try {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) throw new Error("audio capture not supported");
    const ctx = new AudioContext();
    const streamP = navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([TAP_WORKLET], { type: "application/javascript" })));
    const stream = await streamP;
    const node = new AudioWorkletNode(ctx, "tap");
    node.port.onmessage = (e) => onMicFrame(e.data);
    ctx.createMediaStreamSource(stream).connect(node);
    node.connect(ctx.destination);   // the tap outputs silence; this only keeps it running
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    mic = { ctx, stream, rate: ctx.sampleRate };
  } catch (e) {
    console.warn("Recording not available, using Chrome speech recognition instead:", e);
    useBrowserSTT = true;
  }
}

function closeMic() {
  rec = null;
  if (!mic) return;
  mic.stream.getTracks().forEach((t) => t.stop());
  mic.ctx.close().catch(() => {});
  mic = null;
}

function onMicFrame(frame) {
  const r = rec;
  if (!r || !mic) return;
  let sum = 0;
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
  const level = Math.sqrt(sum / frame.length);
  const frameMs = frame.length / mic.rate * 1000;
  r.frameMs = frameMs;
  r.chunks.push(frame); r.ms += frameMs; r.peak = Math.max(r.peak, level);

  const threshold = Math.max(VAD.MIN_LEVEL, r.floor * 3);
  const loud = level > threshold;
  if (!loud) r.floor = r.floor * 0.95 + level * 0.05;      // learn the room's background noise
  showLevel(level / threshold);

  if (loud) {
    r.loudRun++; r.silentMs = 0;
    if (!r.speaking && r.loudRun >= VAD.START_FRAMES) {
      r.speaking = true;
      r.startIdx = Math.max(0, r.chunks.length - VAD.START_FRAMES - Math.ceil(VAD.PRE_ROLL_MS / frameMs));
      setStatus("🎙️ Listening… (I'll answer when you pause, or click 🎙️)");
    }
    if (r.speaking) r.speechMs += frameMs;
  } else {
    r.loudRun = 0;
    if (r.speaking) r.silentMs += frameMs;
  }
  if (r.speaking && r.silentMs >= VAD.END_SILENCE_MS) finishClip(false);
  else if (!r.speaking && r.ms >= VAD.NO_SPEECH_MS) finishClip(false);
  else if (r.ms >= VAD.MAX_MS) finishClip(false);
}

function finishClip(manual) {
  const r = rec;
  if (!r) return;
  rec = null; listening = false; micUI(false);
  if (discardOnEnd || !sessionId) return;
  let chunks = null;
  if (r.speaking && r.speechMs >= VAD.MIN_SPEECH_MS) {
    const frame = r.frameMs || 43;
    const extra = Math.max(0, Math.floor(r.silentMs / frame) - Math.ceil(VAD.TAIL_MS / frame));  // trailing quiet to drop
    chunks = r.chunks.slice(r.startIdx, r.chunks.length - extra);
  } else if (manual && r.ms > 500 && r.peak > VAD.MIN_LEVEL / 2) {
    chunks = r.chunks;      // a very quiet mic: the customer clicked "done", so send what we have
  }
  if (!chunks || !chunks.length) {
    setStatus("Didn't catch that. Click 🎙️ to talk, or type below.");
    return;
  }
  transcribeAndSend(wav16k(chunks, mic ? mic.rate : 48000));
}

// Float samples at the mic's rate (usually 48 kHz) -> 16 kHz 16-bit mono WAV (what speech models use).
function wav16k(chunks, rate) {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const all = new Float32Array(total);
  let o = 0;
  for (const c of chunks) { all.set(c, o); o += c.length; }
  const ratio = rate / 16000, n = Math.floor(total / ratio);
  const buf = new ArrayBuffer(44 + n * 2), dv = new DataView(buf);
  const str = (off, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i)); };
  str(0, "RIFF"); dv.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt ");
  dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, 16000, true); dv.setUint32(28, 32000, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  str(36, "data"); dv.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    // average the samples that fall into this output sample (a simple low-pass, avoids aliasing hiss)
    const a = Math.floor(i * ratio), b = Math.max(a + 1, Math.floor((i + 1) * ratio));
    let s = 0;
    for (let j = a; j < b && j < total; j++) s += all[j];
    dv.setInt16(44 + i * 2, Math.max(-1, Math.min(1, s / (b - a))) * 0x7fff, true);
  }
  return new Blob([buf], { type: "audio/wav" });
}

async function transcribeAndSend(wav) {
  const id = sessionId;
  transcribing = true;
  setStatus("✍️ Understanding what you said…");
  try {
    const res = await fetch(`/api/call/stt?session_id=${encodeURIComponent(id)}&language=${encodeURIComponent($("lang").value)}`,
                            { method: "POST", headers: { "Content-Type": "audio/wav" }, body: wav });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
    const r = await res.json();
    sttFailures = 0; transcribing = false;
    if (id !== sessionId) return;
    if ((r.text || "").trim()) sendTurn(r.text);
    else setStatus("Didn't catch that. Click 🎙️ to talk, or type below.");
  } catch (e) {
    transcribing = false;
    if (id !== sessionId) return;
    sttFailures++;
    if (sttFailures >= 2 && Recognition) {
      useBrowserSTT = true;
      setStatus("Speech service is busy, switched to Chrome's recognizer. Please say that again.");
    } else {
      setStatus("Sorry, I couldn't make that out (" + e.message + "). Please say it again, or type.");
    }
    if (checked("handsfree", true)) setTimeout(() => { if (sessionId && !busy && !speaking) startListening(); }, 500);
  }
}

function micUI(on) {
  $("mic").classList.toggle("listening", on);
  $("mic").classList.toggle("level", on && !!rec);
  $("mic").style.setProperty("--lvl", 0);
}
function showLevel(x) { $("mic").style.setProperty("--lvl", Math.min(1, x / 3).toFixed(2)); }

// ---------- 4b. Backup: Chrome speech-to-text (Web Speech API) ----------
// Chrome ends a recognition session on its own (after a pause, ~8 s of silence,
// or ~60 s of speech). We keep the mic "on" across those by reopening a new
// session and accumulating the text, and only send the turn when the customer
// clicks 🎙️ again or has been quiet for SILENCE_MS after saying something.
const SILENCE_MS = 2000;
const FATAL_MIC_ERRORS = ["not-allowed", "service-not-allowed", "audio-capture", "network", "language-not-supported"];
let recognizer = null;
let heard = "";            // text from earlier recognition sessions in this turn
let silenceTimer = null;

function startListening() {
  if (listening || busy || transcribing) return;
  if (mic && !useBrowserSTT) {
    listening = true; discardOnEnd = false;
    rec = { chunks: [], ms: 0, floor: 0.003, peak: 0, loudRun: 0, silentMs: 0, speechMs: 0,
            speaking: false, startIdx: 0, frameMs: 0 };
    if (mic.ctx.state === "suspended") mic.ctx.resume().catch(() => {});
    micUI(true);
    setStatus("🎙️ Listening… speak now");
    return;
  }
  if (!Recognition) { setStatus("Speech recognition needs Google Chrome. You can type instead."); return; }
  listening = true; heard = ""; discardOnEnd = false;
  micUI(true);
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
  if (rec) { finishClip(true); return; }
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
