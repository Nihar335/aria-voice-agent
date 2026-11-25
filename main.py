"""
main.py - the web server. It connects the browser to the agent.

Run it:   .venv\\Scripts\\python -m uvicorn main:app --reload
Open:     http://localhost:8000

One full voice turn (the end-to-end workflow):
  1. Browser listens to the customer (Chrome speech-to-text) -> text
  2. POST /api/call/turn {session_id, text}
  3. Server: RAG -> LLM (+ tools) -> reply text            (agent.py)
  4. Server: reply text -> MP3 voice                       (tts.py)
  5. Response goes back to the browser, which plays the audio
  6. AFTER the response is sent: sentiment + BANT analysis runs in the background
  7. Browser polls GET /api/call/{id}/live to update the cockpit
"""
import re
import time
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import llm
import stt
import tts
from admin import router as admin_router
from agent import Session, greeting, handle_turn, remember_reply
from analysis import analyze_turn, end_call
from crm import get_crm
from layers import cache, prompts
from layers.guardrails import rate_limited
from layers.rbac import require, router as auth_router, users_router
from portal import portal_path, router as portal_router
from rag import get_kb

@asynccontextmanager
async def lifespan(_app):
    # Load the knowledge base (and open the embedding connection) at boot, not on the first
    # caller's question, so the first answers use meaning search instead of the keyword backup.
    await run_in_threadpool(get_kb)
    yield


app = FastAPI(title="Fortivex AI Sales Voice Agent", lifespan=lifespan)
app.include_router(auth_router)    # /api/auth/... Google sign-in, password break-glass, /me
app.include_router(users_router)   # /api/admin/users (admin only)
app.include_router(admin_router)   # /api/admin/... (each route checks an RBAC permission)
app.include_router(portal_router)  # /api/portal (customer sees only their own calls)

SESSIONS: dict[str, Session] = {}   # live calls (in memory for the MVP; Redis in production)
LATENCIES: list[int] = []           # total ms per turn, for the dashboard


# ---------------------------------------------------------------------------
# Request shapes. Pydantic checks the incoming data automatically:
# wrong types or too-long text are rejected before our code runs.
# ---------------------------------------------------------------------------
class StartCall(BaseModel):
    phone: str = Field(..., min_length=4, max_length=20)
    # "demo" = the admin Live call tab with the cockpit (index.html -> Admin).
    # "customer" = the customer view (index.html -> Customer): the server only ever sends customer-safe fields.
    mode: str = Field("demo", pattern="^(demo|customer)$")


class Turn(BaseModel):
    session_id: str
    text: str = Field(..., min_length=1, max_length=600)
    natural_voice: bool = True     # False = skip edge-tts, the browser speaks instantly (lower latency)
    language: str | None = Field(None, pattern="^(en|hi|hinglish|kn|te)$")   # language picked in the app


class EndCall(BaseModel):
    session_id: str


def _get_session(session_id: str) -> Session:
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(404, "Call not found or already ended")
    return s


def booking_from(events: list) -> dict | None:
    """The demo booked in this turn (slot + calendar link), for the banner on both pages."""
    for e in events or []:
        if e.get("type") == "tool" and e.get("detail") == "book_appointment" and (e.get("result") or {}).get("booked"):
            return {"slot": e["result"]["slot"], "calendar_link": e["result"]["calendar_link"]}
    return None


# What the CUSTOMER page may receive per turn. Everything else (thoughts, tools, RAG sources,
# routing, tokens, latency, trace id) is internal and only goes to the staff demo page.
CUSTOMER_TURN_FIELDS = ("reply", "reply_en", "customer_en", "language", "handoff", "booking", "audio", "audio_mime",
                        "voice")

CALLER_FIELDS = ("name", "phone", "email", "company")


def caller_lead(session: Session) -> dict:
    """
    The lead as the CALLER'S OWN screen may see it. A phone number typed into the page is not
    proof of identity, so for a returning number we only show what this call itself learned
    (plus the name, which the greeting already says). Score, budget, status, earlier emails and
    summaries stay in the CRM for staff (admin dashboard).
    """
    lead = get_crm().get_lead(session.lead_id) or {}
    before = session.known_before
    return {k: lead.get(k) for k in CALLER_FIELDS
            if k in ("name", "phone") or lead.get(k) != before.get(k)}


# ---------------------------------------------------------------------------
# Call endpoints
# ---------------------------------------------------------------------------
@app.post("/api/call/start")
async def start_call(body: StartCall, request: Request):
    # Guardrail: one IP can't start hundreds of calls (each call costs LLM + TTS money).
    ip = request.client.host if request.client else "unknown"
    if rate_limited(f"call:{ip}", config.MAX_CALLS_PER_HOUR_PER_IP, 3600):
        raise HTTPException(429, "Too many calls from this network. Please try again later.")
    phone = re.sub(r"\D", "", body.phone)          # keep digits only
    if len(phone) < 4:
        raise HTTPException(400, "Invalid phone number")
    session = await run_in_threadpool(Session, phone)
    session.customer_mode = body.mode == "customer"
    SESSIONS[session.id] = session
    text = greeting(session)
    remember_reply(session, {"thought": "greeting", "tool": None, "args": {}, "reply": text, "language": "en"})
    voice = await tts.speak(text, "en")        # the greeting is always English text, so an English voice
    base = {"session_id": session.id, "reply": text, "reply_en": text, "language": "en",
            "returning": session.returning, **voice}
    if session.customer_mode:
        return base
    # No earlier summary and no stored lead details here: see caller_lead().
    return {**base, "lead": caller_lead(session),
            "previous_date": session.previous["date"][:10] if session.previous else None}


@app.post("/api/call/turn")
async def call_turn(body: Turn, background: BackgroundTasks):
    session = _get_session(body.session_id)
    if rate_limited(f"turn:{session.id}", config.MAX_TURNS_PER_MINUTE, 60):
        raise HTTPException(429, "You're speaking faster than I can answer. Please wait a moment.")
    t0 = time.perf_counter()
    try:
        # handle_turn is normal (blocking) code -> run it in a thread so the server stays responsive.
        result = await run_in_threadpool(handle_turn, session, body.text.strip(), body.language)
    except Exception as e:
        print(f"[main] agent error: {e}")
        # Never leave the customer hanging: apologise and flag the lead for a human.
        session.handoff = True
        get_crm().update_lead(session.lead_id, {"status": "needs_human"})
        session.transcript.append({"who": "agent", "text": "[technical issue - handed to human]"})
        sorry = "Sorry, I'm having a small technical issue. Let me connect you to our team."
        result = {"reply": sorry, "reply_en": sorry, "customer_en": body.text.strip(),
                  "language": session.language, "sources": [], "thoughts": [], "events": [],
                  "handoff": True, "provider": "none", "latency": {"rag_ms": 0, "llm_ms": 0}}
    if body.natural_voice:
        voice = await tts.speak(result["reply"], result["language"])
    else:
        voice = {"audio": None, "audio_mime": None, "voice": "browser", "tts_ms": 0}
    result["latency"]["tts_ms"] = voice["tts_ms"]
    result["latency"]["total_ms"] = int((time.perf_counter() - t0) * 1000)
    LATENCIES.append(result["latency"]["total_ms"])

    # Runs AFTER the response is sent, so the customer never waits for it.
    background.add_task(analyze_turn, session)
    out = {**result, **voice, "booking": booking_from(result.get("events"))}
    if session.customer_mode:
        return {k: out.get(k) for k in CUSTOMER_TURN_FIELDS}
    return out


@app.post("/api/call/stt")
async def call_stt(request: Request, session_id: str, language: str = "en"):
    """
    The customer's spoken turn as audio (16 kHz WAV recorded by the page) -> text.
    The page then sends that text to /api/call/turn like a typed message.
    """
    session = _get_session(session_id)
    if language not in stt.LANGUAGE_NAMES:
        language = "en"
    if rate_limited(f"stt:{session.id}", config.MAX_TURNS_PER_MINUTE * 2, 60):
        raise HTTPException(429, "Too many recordings. Please wait a moment.")
    mime = (request.headers.get("content-type") or "audio/wav").split(";")[0].strip()
    if not mime.startswith("audio/"):
        raise HTTPException(415, "Send the recording as audio")
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "Empty recording")
    if len(audio) > config.MAX_AUDIO_MB * 1024 * 1024:
        raise HTTPException(413, "Recording too long")
    try:
        r = await run_in_threadpool(stt.transcribe, audio, mime, language)
    except Exception as e:
        print(f"[main] {e}")
        raise HTTPException(503, "Speech recognition is unavailable right now")
    return {"text": r["text"]} if session.customer_mode else r


@app.get("/api/call/{session_id}/recap")
def call_recap(session_id: str):
    """
    The live "chat summary" for BOTH pages: a short recap written for the customer.
    It comes from the background analysis, but only the customer-safe fields leave the server.
    """
    a = _get_session(session_id).analysis or {}
    points = [str(p) for p in (a.get("recap_points") or []) if p][:4]
    return {"recap": str(a.get("recap") or ""), "points": points}


@app.get("/api/call/{session_id}/live")
def call_live(session_id: str):
    """Everything the live cockpit shows (staff demo page only)."""
    s = _get_session(session_id)
    if s.customer_mode:
        raise HTTPException(403, "The live cockpit is not available on the customer page")
    return {"analysis": s.analysis, "lead": caller_lead(s),
            "transcript": s.transcript, "language": s.language, "handoff": s.handoff}


@app.post("/api/call/end")
async def call_end(body: EndCall):
    session = _get_session(body.session_id)
    if len(session.transcript) > 1 and not session.analysis:
        await run_in_threadpool(analyze_turn, session)   # make sure we have a score
    summary = await run_in_threadpool(end_call, session)
    SESSIONS.pop(body.session_id, None)
    # The private "my calls" link shows EVERY call of this lead. Hand it out only on the lead's
    # first call (everything on it is from this caller). For a returning number, typing the number
    # must not be enough: the customer keeps the link from their first call, or staff resend it.
    portal_url = None if session.returning else portal_path(session.lead_id)
    if session.customer_mode:
        # Only what is written FOR the customer (same fields as the my-calls page). No score, objections
        # or sales next steps.
        return {"summary": {"customer_summary": summary.get("customer_summary"),
                            "customer_next_steps": summary.get("customer_next_steps") or [],
                            "language": summary.get("language")},
                "appointments": [{"start": a["start"], "link": a["link"]}
                                 for a in get_crm().appointments_for_lead(session.lead_id)] if not session.returning else [],
                "returning": session.returning, "portal_url": portal_url}
    return {"summary": summary, "lead": caller_lead(session), "portal_url": portal_url}


# ---------------------------------------------------------------------------
# Dashboard (admin only: it lists every lead's phone, transcript and summary) + health
# ---------------------------------------------------------------------------
@app.get("/api/admin/dashboard")
def dashboard(_: dict = Depends(require("dashboard:read"))):
    crm = get_crm()
    leads, calls, appts = crm.list_leads(), crm.list_calls(), crm.list_appointments()
    scores = [l["score"] or 0 for l in leads]
    return {
        "stats": {
            "leads": len(leads),
            "calls": len(calls),
            "hot_leads": sum(1 for l in leads if (l["score"] or 0) >= 70),
            "demos_booked": len(appts),
            "handoffs": sum(1 for l in leads if l["status"] == "needs_human"),
            "avg_score": round(sum(scores) / len(scores)) if scores else 0,
            "avg_turn_ms": round(sum(LATENCIES) / len(LATENCIES)) if LATENCIES else 0,
            "live_calls": len(SESSIONS),
        },
        "leads": leads, "calls": calls, "appointments": appts,
    }


@app.get("/api/health")
def health():
    return {"status": "ok", "llm_provider": config.LLM_PROVIDER,
            "gemini_key": bool(config.GEMINI_API_KEY), "groq_key": bool(config.GROQ_API_KEY),
            "rag_mode": get_kb().mode, "rag_chunks": len(get_kb().chunks),
            "prompt_version": prompts.version(), "router": config.ROUTER_ENABLED,
            "cache": {"enabled": config.CACHE_ENABLED, **cache.stats()},
            "guardrails": config.GUARDRAILS_ENABLED, "google_sign_in": bool(config.GOOGLE_CLIENT_ID),
            "llm_rate_limited": llm.cooling_down()}


@app.middleware("http")
async def revalidate_page_files(request: Request, call_next):
    # The page's HTML/JS must never come from a stale browser cache after an update (a new page with an
    # old call-core.js breaks the call). "no-cache" = the browser re-checks each time (a cheap 304 if unchanged).
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


# One page for everyone: index.html asks "customer or admin?". Old links keep working.
@app.get("/call.html", include_in_schema=False)
def old_customer_page():
    return RedirectResponse("/#customer")


@app.get("/admin.html", include_in_schema=False)
def old_admin_page():
    return RedirectResponse("/#admin")


# The web page (Stage 6). Mounted last, so /api/... routes win.
FRONTEND = config.BASE_DIR / "frontend"
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
