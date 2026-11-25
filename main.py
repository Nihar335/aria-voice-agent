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

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import tts
from agent import Session, greeting, handle_turn, remember_reply
from analysis import analyze_turn, end_call
from crm import get_crm
from rag import get_kb

app = FastAPI(title="Fortivex AI Sales Voice Agent")

SESSIONS: dict[str, Session] = {}   # live calls (in memory for the MVP; Redis in production)
LATENCIES: list[int] = []           # total ms per turn, for the dashboard


# ---------------------------------------------------------------------------
# Request shapes. Pydantic checks the incoming data automatically:
# wrong types or too-long text are rejected before our code runs.
# ---------------------------------------------------------------------------
class StartCall(BaseModel):
    phone: str = Field(..., min_length=4, max_length=20)


class Turn(BaseModel):
    session_id: str
    text: str = Field(..., min_length=1, max_length=600)
    natural_voice: bool = True     # False = skip edge-tts, the browser speaks instantly (lower latency)


class EndCall(BaseModel):
    session_id: str


def _get_session(session_id: str) -> Session:
    s = SESSIONS.get(session_id)
    if not s:
        raise HTTPException(404, "Call not found or already ended")
    return s


# ---------------------------------------------------------------------------
# Call endpoints
# ---------------------------------------------------------------------------
@app.post("/api/call/start")
async def start_call(body: StartCall):
    phone = re.sub(r"\D", "", body.phone)          # keep digits only
    if len(phone) < 4:
        raise HTTPException(400, "Invalid phone number")
    session = await run_in_threadpool(Session, phone)
    SESSIONS[session.id] = session
    text = greeting(session)
    remember_reply(session, {"thought": "greeting", "tool": None, "args": {}, "reply": text,
                             "language": session.language})
    voice = await tts.speak(text, session.language)
    lead = get_crm().get_lead(session.lead_id)
    return {"session_id": session.id, "reply": text, "language": session.language,
            "returning": session.returning, "lead": lead, "previous": session.previous, **voice}


@app.post("/api/call/turn")
async def call_turn(body: Turn, background: BackgroundTasks):
    session = _get_session(body.session_id)
    t0 = time.perf_counter()
    try:
        # handle_turn is normal (blocking) code -> run it in a thread so the server stays responsive.
        result = await run_in_threadpool(handle_turn, session, body.text.strip())
    except Exception as e:
        print(f"[main] agent error: {e}")
        # Never leave the customer hanging: apologise and flag the lead for a human.
        session.handoff = True
        get_crm().update_lead(session.lead_id, {"status": "needs_human"})
        session.transcript.append({"who": "agent", "text": "[technical issue - handed to human]"})
        result = {"reply": "Sorry, I'm having a small technical issue. Let me connect you to our team.",
                  "language": session.language, "sources": [], "thoughts": [], "events": [],
                  "handoff": True, "provider": "none", "latency": {"rag_ms": 0, "llm_ms": 0}}
    if body.natural_voice:
        voice = await tts.speak(result["reply"], result["language"])
    else:
        voice = {"audio": None, "voice": "browser", "tts_ms": 0}
    result["latency"]["tts_ms"] = voice["tts_ms"]
    result["latency"]["total_ms"] = int((time.perf_counter() - t0) * 1000)
    LATENCIES.append(result["latency"]["total_ms"])

    # Runs AFTER the response is sent, so the customer never waits for it.
    background.add_task(analyze_turn, session)
    return {**result, **voice}


@app.get("/api/call/{session_id}/live")
def call_live(session_id: str):
    """Everything the live cockpit shows."""
    s = _get_session(session_id)
    return {"analysis": s.analysis, "lead": get_crm().get_lead(s.lead_id),
            "transcript": s.transcript, "language": s.language, "handoff": s.handoff}


@app.post("/api/call/end")
async def call_end(body: EndCall):
    session = _get_session(body.session_id)
    if len(session.transcript) > 1 and not session.analysis:
        await run_in_threadpool(analyze_turn, session)   # make sure we have a score
    summary = await run_in_threadpool(end_call, session)
    SESSIONS.pop(body.session_id, None)
    return {"summary": summary, "lead": get_crm().get_lead(session.lead_id)}


# ---------------------------------------------------------------------------
# Dashboard + health
# ---------------------------------------------------------------------------
@app.get("/api/dashboard")
def dashboard():
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
            "rag_mode": get_kb().mode, "rag_chunks": len(get_kb().chunks)}


# The web page (Stage 6). Mounted last, so /api/... routes win.
FRONTEND = config.BASE_DIR / "frontend"
if FRONTEND.exists():
    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
