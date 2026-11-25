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
not just a chatbot.
"""
import json
import time
import uuid
from datetime import datetime

import llm
from analysis import detect_language
from crm import get_crm
from rag import format_context, get_kb
from tools import IST, TOOL_DESCRIPTIONS, run_tool

MAX_TOOL_STEPS = 3

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

SYSTEM_PROMPT = """You are "Aria", a friendly and smart sales voice agent for Fortivex AI, an AI and software company in Bengaluru.
You are on a LIVE PHONE CALL. Today is {today} (IST).

YOUR GOALS (in this order):
1. Understand the customer's business and problem. Ask ONE question at a time.
2. Answer questions ONLY using the COMPANY FACTS below. If the facts do not contain the answer, say you will confirm with the team. NEVER invent prices, features or promises.
3. Qualify the lead naturally (BANT): Budget, Authority (are they the decision maker?), Need, Timeline.
4. Get their name, then offer a FREE 30-minute demo. As soon as the customer agrees to a time that is in the free slots, call book_appointment IMMEDIATELY (email is optional - ask for it after booking). Never say a demo is booked unless book_appointment returned "booked": true.
5. If the customer asks for a human, is angry, or the question is outside the facts, use handoff_to_human.
6. If the customer asks something unrelated to Fortivex or their business (general knowledge, jokes, etc.), do NOT answer it. Say politely that you can only help with Fortivex services, then steer back.

VOICE RULES:
- Keep every reply SHORT: at most 2 sentences and under 35 words. No lists, no markdown, no emojis. It will be spoken aloud.
- Say dates and times naturally, e.g. "tomorrow at 3 PM" or "Thursday at 11 AM", never "2026-11-26 15:00".
- Say prices naturally, e.g. "twenty-five thousand rupees". "24x7" means twenty-four hours, seven days a week.
- Always say prices are indicative and confirmed by a consultant.

LANGUAGE RULES:
- Reply in the SAME language and style the customer uses:
  English -> "en"; Hindi -> Devanagari script, "hi"; Hinglish (Hindi in English letters) -> "hinglish";
  Kannada -> Kannada script, "kn"; Telugu -> Telugu script, "te".
- The customer picked a language in the app: {chosen}. Reply in that language.
  Only switch if the customer clearly speaks a different language.
{memory}
TOOLS YOU CAN USE:
{tools}

LANGUAGE DETECTED FROM THE CUSTOMER'S LAST MESSAGE: {detected}
WRITE "reply" IN: {chosen} ("reply_en" is the only English field)

COMPANY FACTS (retrieved for this question):
{context}

RESPOND ONLY WITH JSON in exactly this shape (every single time, never plain text):
{{"thought": "one short line: what you are doing and why",
  "tool": null or "tool_name",
  "args": {{}},
  "reply": "what you say to the customer (empty string if you are calling a tool)",
  "language": "en | hi | hinglish | kn | te",
  "reply_en": "the reply translated to plain English",
  "customer_en": "the customer's LAST message translated to plain English"}}
For reply_en and customer_en: Hinglish, Hindi, Kannada and Telugu must ALWAYS be translated into real English sentences.
Copy the text unchanged only when it is already proper English.
"""


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
    """Process one thing the customer said and return the agent's answer + details."""
    t0 = time.perf_counter()
    session.sources, session.events = [], []
    session.history.append({"role": "user", "content": user_text})
    session.transcript.append({"who": "customer", "text": user_text})

    # 1. RAG on every turn: cheaper and faster than waiting for the LLM to ask for it.
    t_rag = time.perf_counter()
    hits = get_kb().search(rag_query(session, user_text))
    rag_ms = int((time.perf_counter() - t_rag) * 1000)
    session.sources = [h["source"] for h in hits]
    session.events.append({"type": "rag", "detail": f"{len(hits)} facts found", "ms": rag_ms,
                           "sources": session.sources})

    system = SYSTEM_PROMPT.format(
        today=datetime.now(IST).strftime("%A, %d %B %Y, %I:%M %p"),
        memory=session.memory_text(),
        tools=TOOL_DESCRIPTIONS,
        context=format_context(hits),
        detected=detect_language(user_text),
        chosen=LANGUAGE_NAMES.get(chosen_language or "", "not chosen - match the customer"),
    )

    # 2. Think -> act -> observe loop.
    llm_ms, provider, reply, language, thoughts = 0, "", "", session.language, []
    reply_en, customer_en = "", ""
    for step in range(MAX_TOOL_STEPS + 1):
        decision, result = llm.chat_json(system, session.history, temperature=0.3)
        llm_ms += result.latency_ms
        provider = result.provider
        thoughts.append(decision.get("thought", ""))
        tool = decision.get("tool")

        if tool and step < MAX_TOOL_STEPS:
            output = run_tool(session, tool, decision.get("args") or {})
            session.events.append({"type": "tool", "detail": tool, "args": decision.get("args"),
                                   "result": output})
            # Show the LLM what it decided and what happened, then let it think again.
            session.history.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
            session.history.append({"role": "user", "content": f"[TOOL RESULT for {tool}] {output}"})
            continue

        reply = decision.get("reply") or "Sorry, could you say that again?"
        language = decision.get("language") or language
        reply_en = decision.get("reply_en") or ""
        customer_en = decision.get("customer_en") or ""
        break

    session.language = language
    remember_reply(session, {"thought": thoughts[-1] if thoughts else "", "tool": None, "args": {},
                             "reply": reply, "language": language,
                             "reply_en": reply_en, "customer_en": customer_en})

    return {
        "reply": reply,
        "language": language,
        "reply_en": reply_en or reply,
        "customer_en": customer_en or user_text,
        "sources": sorted(set(session.sources)),
        "thoughts": thoughts,
        "events": session.events,
        "handoff": session.handoff,
        "provider": provider,
        "latency": {"rag_ms": rag_ms, "llm_ms": llm_ms,
                    "total_ms": int((time.perf_counter() - t0) * 1000)},
    }


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
