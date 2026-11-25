"""
agent.py - the agent's brain. One Session = one phone call.

Every time the customer says something, handle_turn() does this loop:

   customer text
        |
   1. RAG: find relevant company facts (Stage 2)           <- done by code, every turn
        |
   2. LLM decides (JSON): reply now, or use a tool first?
        |                         |
        | reply                   | tool -> our code runs it -> result back to LLM -> step 2 again
        v                                                       (max 3 tools per turn)
   reply text + language + sources + timings  ->  sent to the voice / screen

This "think -> act -> observe -> repeat" loop is what makes it an AGENT,
not just a chatbot. Around it, every turn passes through guardrails, a router, a cache
and a trace: see layers/__init__.py for the full 7-layer stack and layers/pipeline.py for the code.
"""
import json
import uuid
from datetime import datetime

from crm import get_crm
from tools import IST

# Words that mean "the thing we were just talking about" -> the question is a follow-up.
FOLLOW_UP_WORDS = {"this", "that", "it", "these", "those", "them", "iska", "iski", "iske",
                   "yeh", "ye", "woh", "wo", "uska", "uski", "isme", "isko"}


def rag_query(session, user_text: str) -> str:
    """Add the previous customer message to short follow-up questions, so search has context."""
    words = set(user_text.lower().replace("?", " ").replace(",", " ").split())
    earlier = [t["text"] for t in session.transcript[:-1] if t["who"] == "customer"]
    if earlier and words & FOLLOW_UP_WORDS:
        return earlier[-1] + " " + user_text
    return user_text


def remember_reply(session, decision: dict):
    """Store the agent's answer in the history AS JSON, so the LLM keeps answering in JSON."""
    session.history.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
    session.transcript.append({"who": "agent", "text": decision["reply"]})

LANGUAGE_NAMES = {"en": "English", "hi": "Hindi (Devanagari script)", "hinglish": "Hinglish (Hindi in English letters)",
                  "kn": "Kannada (Kannada script)", "te": "Telugu (Telugu script)"}

# The system prompt lives in prompts/aria_system.md (layer 2, versioned): see layers/prompts.py


class Session:
    """Everything we remember during one call."""

    def __init__(self, phone: str):
        self.id = uuid.uuid4().hex   # full 128-bit id: can't be guessed to peek at someone else's live call
        self.phone = phone
        self.started = datetime.now(IST)
        crm = get_crm()

        # Returning-caller memory: same phone number -> load the previous call summary.
        lead = crm.find_lead_by_phone(phone)
        self.returning = lead is not None
        if lead is None:
            lead = crm.create_lead(phone)
        self.lead_id = lead["id"]
        self.previous = crm.last_call_summary(self.lead_id) if self.returning else None

        self.history = []        # what the LLM sees: user / assistant messages
        self.transcript = []     # clean conversation for the summary: [{"who", "text"}]
        self.language = lead.get("language") or "en"
        self.sources = []        # documents used in the current turn
        self.events = []         # things that happened (for the live cockpit)
        self.handoff = False
        self.analysis = {}       # sentiment / BANT score (filled in Stage 4)
        self.abuse_count = 0     # input guardrail: 2nd abusive message -> human handoff
        self.trace_ids = []      # observability: one trace per turn

    def memory_text(self) -> str:
        if not self.previous:
            return ""
        lead = get_crm().get_lead(self.lead_id)
        demos = [a["start"] for a in get_crm().list_appointments() if a["lead_id"] == self.lead_id]
        return (
            "\nRETURNING CUSTOMER - you spoke before. Greet them by name and refer to the last call naturally.\n"
            f"Name: {lead.get('name') or 'unknown'}. Last call ({self.previous['date'][:10]}): "
            f"{self.previous.get('summary', '')}\n"
            f"Demos already booked (IST): {', '.join(demos) if demos else 'none'}\n"
        )


def greeting(session: Session) -> str:
    """First thing the agent says when the call connects."""
    if session.returning:
        name = get_crm().get_lead(session.lead_id).get("name")
        return f"Hi{' ' + name if name else ''}, welcome back to Fortivex AI! This is Aria. How can I help you today?"
    return ("Hello! This is Aria from Fortivex AI. We help businesses save time and grow with AI. "
            "You can talk to me in English, Hindi, Hinglish, Kannada or Telugu. How can I help you today?")


def handle_turn(session: Session, user_text: str, chosen_language: str | None = None) -> dict:
    """Process one thing the customer said: runs it through all 7 layers (layers/pipeline.py)."""
    from layers.pipeline import run_turn
    return run_turn(session, user_text, chosen_language)


# ---------------------------------------------------------------------------
# Talk to the agent by typing:  python agent.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    s = Session(phone=input("Your phone number (any): ").strip() or "9999999999")
    print("Aria:", greeting(s))
    while True:
        text = input("\nYou: ").strip()
        if text.lower() in ("bye", "exit", "quit"):
            break
        out = handle_turn(s, text)
        for e in out["events"]:
            if e["type"] == "tool":
                print(f"   [tool] {e['detail']}({e['args']}) -> {e['result']}")
        print(f"   [{out['provider']} | lang={out['language']} | {out['latency']}] sources={out['sources']}")
        print("Aria:", out["reply"])
