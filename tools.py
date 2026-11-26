"""
tools.py - LAYER 4: actions the agent is ALLOWED to take (one registry, used two ways).

The LLM can only talk. Tools let it DO things: look up facts, save the customer's
details, check free slots, book a demo, call a human.

ONE registry, TWO doors:
  1. The voice agent (agent loop, in-process, fastest)  -> run_tool(session, name, args, role="customer")
  2. MCP clients (Claude Desktop, Fortivex CRM, ...)     -> mcp_server.py serves the SAME tools over MCP

Every tool has:
  - a pydantic input model  -> the JSON schema MCP clients see, AND validation of what the LLM sent
                               (wrong types, bad formats and invented arguments are rejected)
  - a permission            -> layers/rbac.py decides which role may call it ("tool:<name>")
  - a plain-English line    -> what the LLM reads in its prompt
The LLM never touches the database itself. Our code is the gatekeeper.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from crm import get_crm
from layers.rbac import can_use_tool
from rag import format_context, get_kb

IST = timezone(timedelta(hours=5, minutes=30))
DEMO_TIMES = ["11:00", "15:00", "17:00"]     # demo slots offered each working day
DEMO_MINUTES = 30


class _Args(BaseModel):
    # Unknown extra arguments from the LLM are ignored; wrong types are rejected.
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Tool 1: search the knowledge base (RAG)
# ---------------------------------------------------------------------------
class SearchArgs(_Args):
    query: str = Field(..., min_length=2, max_length=300, description="What to look up in company documents")


def search_knowledge(session, a: SearchArgs) -> dict:
    hits = get_kb().search(a.query)
    session.sources.extend(h["source"] for h in hits)
    return {"found": bool(hits), "context": format_context(hits)}


# ---------------------------------------------------------------------------
# Tool 2: save what the customer told us (name, business, role, service, budget, timeline, email)
# ---------------------------------------------------------------------------
# THE CALL SCRIPT. Aria walks every caller through these steps, in this order, one step per reply.
# Each step: (step id, CRM fields it fills, what Aria must say/ask in that reply).
# The demo meeting can only be offered and booked after the last step. This is enforced in CODE
# (save_lead_info / check_availability / book_appointment), not only in the prompt: the model once
# "saved" a budget and a timeline the customer never said and booked straight away.
SERVICES_PITCH = ("AI voice and chat agents that answer calls and chats 24x7, business process automation, "
                  "custom software and apps, our Fortivex CRM, and AI consulting")
CALL_SCRIPT = [
    ("name", ("name",),
     "ask the customer's name"),
    ("business", ("company", "authority"),
     "ask what their business is and what it does, and whether they own it (their role)"),
    ("service", ("need",),
     f"say exactly this (translated into the customer's language if needed, nothing added): "
     f"\"We offer {SERVICES_PITCH}. Which of these are you interested in?\""),
    ("budget", ("budget",),
     "ask their rough budget for that service"),
    ("timeline", ("timeline",),
     "ask in how much time they need it delivered (the duration)"),
]
# Kept for older imports/tests: field -> what it means.
BOOKING_CHECKLIST = [(f, step) for step, fields, _ in CALL_SCRIPT for f in fields]
# Customers often say these without being asked ("I'm Ravi, I own a clinic"): accepted any time.
# Service, budget and timeline count ONLY as the answer to Aria's own question for that step.
VOLUNTEER_OK = {"name", "company", "authority"}

# The services question in Hinglish (models tend to fall back to plain English for this long sentence).
SERVICE_QUESTION_HINGLISH = ("Hum AI voice aur chat agents dete hain jo 24x7 calls aur chats ka jawab dete hain, business "
                             "process automation, custom software aur apps, hamara Fortivex CRM, aur AI consulting. "
                             "Aapko inmein se kis mein interest hai?")

# What Aria says when no model could write a reply (all rate-limited): the current step's question.
NEXT_QUESTION_EN = {
    "name": "Thanks! May I have your name, please?",
    "business": "Thanks! What does your business do, and are you the owner?",
    "service": (f"We offer {SERVICES_PITCH}. Which of these are you interested in?"),
    "budget": "Thanks! What rough budget do you have in mind for this?",
    "timeline": "Thanks! In how much time would you need it ready?",
}


def current_step(answered: set):
    """The first step of the call script that is not fully answered yet (None = all done)."""
    for step in CALL_SCRIPT:
        if not set(step[1]) <= answered:
            return step
    return None


def next_question_en(answered: set) -> str:
    step = current_step(answered)
    return NEXT_QUESTION_EN[step[0]] if step else "Thanks! Shall I check the free demo slots for you?"


def still_to_ask(answered: set) -> list[str]:
    """The script steps not answered yet (empty list = the demo may be booked)."""
    return [step for step, fields, _ in CALL_SCRIPT if not set(fields) <= answered]


# "Book it ASAP" - the ONLY case where Aria books before all questions are asked. Needs an urgency word
# AND a booking word, so "can you book a demo tomorrow?" alone still goes through the questions.
_URGENT = re.compile(
    r"\b(asap|urgent|urgently|hurry|right away|right now|immediately|as soon as possible|earliest|"
    r"quickly|jaldi|turant|abhi ke abhi|foran|fauran)\b|a\.s\.a\.p|जल्दी|तुरंत|फौरन|త్వరగా|వెంటనే|ಬೇಗ|ತಕ್ಷಣ", re.I)
_BOOK_WORD = re.compile(
    r"\b(book|booking|schedule|fix|meeting|meet|demo|appointment|slot)\b|मीटिंग|डेमो|बुक|మీటింగ్|డెమో|బుక్|ಮೀಟಿಂಗ್|ಡೆಮೊ|ಬುಕ್", re.I)


def wants_asap(text: str | None) -> bool:
    """True when the customer themself asks to book in a hurry ("book it ASAP", "jaldi meeting fix karo")."""
    return bool(text and _URGENT.search(text) and _BOOK_WORD.search(text))


def booking_blocked(session) -> dict | None:
    """Why slots can't be shown / booked yet in a voice call (None = allowed). Staff over MCP: never gated."""
    answered = getattr(session, "answered", None)
    if answered is None or getattr(session, "urgent", False):
        return None
    if still_to_ask(answered):
        return {"error": "Not yet: ask the remaining questions first, one at a time.",
                "still_to_ask": still_to_ask(answered)}
    if not getattr(session, "meeting_asked", False):
        return {"error": "First ask the customer whether they would like a meeting with our technical team."}
    return None


def booking_checklist_text(session) -> str:
    """For the system prompt: what the customer told us in THIS call + the ONE step Aria must do now.
    (Only recent messages go to the model, so the answers are repeated here.)"""
    answered = getattr(session, "answered", set())
    known = ""
    if answered and getattr(session, "lead_id", None) is not None:
        lead = get_crm().get_lead(session.lead_id) or {}
        said = [f"{f}: {lead.get(f)}" for f, _ in BOOKING_CHECKLIST if f in answered and lead.get(f)]
        known = f"TOLD US IN THIS CALL: {'; '.join(said)}.\n" if said else ""
    return known + _script_step_text(session, answered)


def _script_step_text(session, answered: set) -> str:
    step = current_step(answered)
    booking = ("call check_availability, offer the two or three EARLIEST free times, and call book_appointment ONLY "
               "after the customer clearly picks one of those times. After booking, you may ask for an email for "
               "the calendar invite (optional).")
    if getattr(session, "booked", False):
        return ("THE MEETING IS BOOKED. Do not ask the remaining questions. Answer briefly; you may ask for an email "
                "for the calendar invite (optional, save it with save_lead_info), then close the call warmly.")
    if getattr(session, "urgent", False):
        return ("THE CUSTOMER IS IN A HURRY AND ASKED TO BOOK ASAP. Skip any remaining questions (save anything they "
                "already said with save_lead_info). Tell them the technical team will cover the rest in the meeting, "
                "then " + booking)
    if step is None and not getattr(session, "meeting_asked", False):
        return ("ALL QUESTIONS DONE. Thank them and ask ONCE whether they would like a free 30-minute meeting with "
                "our technical team. Do NOT call check_availability or offer times yet.")
    if step is None:
        return ("ALL QUESTIONS DONE and you asked whether they want a meeting. If they said yes: " + booking +
                " If they said no or not now: do NOT book; thank them warmly and say our team will be in touch.")
    step_id, fields, instruction = step
    missing = [f for f in fields if f not in answered]
    done = [s for s, f, _ in CALL_SCRIPT if set(f) <= answered]
    return (f"CURRENT STEP: \"{step_id}\" (steps done: {', '.join(done) or 'none'}). "
            f"If the customer's LAST message answers this step, FIRST call save_lead_info with "
            f"{' and '.join(missing)} exactly as they said it, plus any later detail (service, budget, timeline) "
            f"they also said (never guess or fill a field they did not say). "
            "Never ask again for anything listed under TOLD US IN THIS CALL. "
            f"Then (or if they did not answer it) your reply must {instruction}. "
            "Do NOT offer, suggest or mention a demo, meeting or time slots yet.")


class LeadArgs(_Args):
    name: Optional[str] = Field(None, max_length=80)
    email: Optional[str] = Field(None, max_length=120, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    company: Optional[str] = Field(None, max_length=120, description="Business name and what it does")
    authority: Optional[str] = Field(None, max_length=120, description="Their role, e.g. owner")
    need: Optional[str] = Field(None, max_length=300, description="The Fortivex service they are interested in")
    budget: Optional[str] = Field(None, max_length=120, description='Rough budget, or "prefers not to say"')
    timeline: Optional[str] = Field(None, max_length=120, description="In how much time they need it")


_FILLER = {"the", "and", "for", "with", "around", "about", "approx", "roughly", "budget", "need",
           "want", "our", "their", "them", "maybe", "rupees", "inr"}


def said_by_customer(value: str, customer_text: str) -> bool:
    """True when a saved value is grounded in what the customer said: every number in it, and at least
    half of its other words, appear in the customer's own messages."""
    words = set(re.findall(r"\w+", customer_text.lower()))
    tokens = [t for t in re.findall(r"\w+", str(value).lower()) if t not in _FILLER]
    numbers = [t for t in tokens if any(c.isdigit() for c in t)]
    others = [t for t in tokens if t not in numbers and len(t) >= 3]
    if not numbers and not others:
        return False
    if any(n not in words for n in numbers):
        return False
    return not others or sum(t in words for t in others) * 2 >= len(others)


def save_lead_info(session, a: LeadArgs) -> dict:
    values = {k: v for k, v in a.model_dump().items() if v}
    answered = getattr(session, "answered", None)
    ignored = []
    if answered is not None:
        # Voice call: only accept what this step asked for (+ name/business details said any time).
        # A later step's answer the customer volunteered early ("2 lakh, in a month") is kept too, but only when
        # it really is in their own words this call, so Aria doesn't ask it again and nothing invented gets saved.
        asking = set(getattr(session, "asking", ()))
        said = " ".join(t["text"] + " " + (t.get("en") or "") for t in getattr(session, "transcript", [])
                        if t.get("who") == "customer")
        ignored = [k for k in values if k not in asking and k not in VOLUNTEER_OK and k != "email"
                   and not said_by_customer(values[k], said)]
        for k in ignored:
            values.pop(k)
    lead = get_crm().update_lead(session.lead_id, values) if values else get_crm().get_lead(session.lead_id)
    if answered is not None:
        answered |= set(values)
    out = {"saved": sorted(values), "lead": {k: lead.get(k) for k, _ in BOOKING_CHECKLIST},
           "still_to_ask_before_booking": still_to_ask(answered if answered is not None else set())}
    if ignored:
        out["not_saved"] = f"{', '.join(ignored)}: not asked yet. Ask it at its step and save the customer's own words."
    return out


# ---------------------------------------------------------------------------
# Tool 3: free demo slots
# ---------------------------------------------------------------------------
def free_slots(days: int = 3) -> list[str]:
    """Next working days (Mon-Sat), minus slots already booked."""
    booked = get_crm().booked_times()
    now = datetime.now(IST)
    slots, day = [], now.date()
    while len({s[:10] for s in slots}) < days:
        day += timedelta(days=1)
        if day.weekday() == 6:      # Sunday off
            continue
        for t in DEMO_TIMES:
            s = f"{day.isoformat()} {t}"
            if s not in booked:
                slots.append(s)
    return slots


class AvailabilityArgs(_Args):
    day: Optional[str] = Field(None, pattern=r"^$|^\d{4}-\d{2}-\d{2}$", description="Optional day, YYYY-MM-DD")


def check_availability(session, a: AvailabilityArgs) -> dict:
    # Voice calls: no demo times until every booking question is answered (staff over MCP: not gated).
    answered = getattr(session, "answered", None)
    blocked = booking_blocked(session)
    if blocked:
        return blocked
    slots = free_slots()
    if a.day:
        slots = [s for s in slots if s.startswith(a.day)] or slots
    if answered is not None:
        session.offered = set(getattr(session, "offered", set())) | set(slots[:6])
    # Spelled out by code: models get weekdays wrong ("Friday" for a Saturday slot).
    return {"timezone": "IST", "free_slots": slots[:6], "say_as": {x: spoken_slot(x) for x in slots[:6]}}


def spoken_slot(slot: str) -> str:
    """'2026-11-28 17:00' -> 'tomorrow, Saturday 28 November at 5 PM' (relative to today, IST)."""
    when = datetime.strptime(slot, "%Y-%m-%d %H:%M")
    days = (when.date() - datetime.now(IST).date()).days
    rel = {0: "today, ", 1: "tomorrow, "}.get(days, "")
    hour = when.strftime("%I").lstrip("0") + (when.strftime(":%M") if when.minute else "") + when.strftime(" %p")
    return f"{rel}{when.strftime('%A')} {when.day} {when.strftime('%B')} at {hour}"


# ---------------------------------------------------------------------------
# Tool 4: book the demo + make a Google Calendar link
# ---------------------------------------------------------------------------
def calendar_link(start_ist: str, topic: str, details: str) -> str:
    """A link that opens Google Calendar with the event pre-filled (no login/API needed)."""
    start = datetime.strptime(start_ist, "%Y-%m-%d %H:%M").replace(tzinfo=IST).astimezone(timezone.utc)
    end = start + timedelta(minutes=DEMO_MINUTES)
    fmt = "%Y%m%dT%H%M%SZ"
    return (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={quote(topic)}&dates={start.strftime(fmt)}/{end.strftime(fmt)}"
        f"&details={quote(details)}"
    )


class BookArgs(_Args):
    slot: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", description='Exactly one free slot, "YYYY-MM-DD HH:MM" IST')
    topic: str = Field("Fortivex AI demo", max_length=120)
    email: Optional[str] = Field(None, max_length=120, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")


def book_appointment(session, a: BookArgs) -> dict:
    crm = get_crm()
    if a.email:
        crm.update_lead(session.lead_id, {"email": a.email})
    lead = crm.get_lead(session.lead_id)
    # GUARDRAIL: no demo meeting until every booking question was asked (enforced here, not only in the prompt).
    # Voice calls only: staff booking through MCP (mcp_server.py) has no `answered` and is not gated.
    answered = getattr(session, "answered", None)
    blocked = booking_blocked(session)
    if blocked:
        return {"booked": False, **blocked}
    if answered is not None and a.slot not in getattr(session, "offered", set()):
        return {"booked": False, "error": "Call check_availability, offer the free times, and book only the time "
                                          "the customer picks."}
    # GUARDRAIL: the LLM may invent a time. We only accept a real free slot.
    available = free_slots()
    if a.slot not in available:
        return {"booked": False, "error": "That time is not available.", "free_slots": available[:6]}

    title = f"{a.topic} - {lead.get('name') or 'Customer'}"
    link = calendar_link(a.slot, title, f"Demo call with Fortivex AI. Customer phone: {lead['phone']}")
    appt = crm.book_appointment(session.lead_id, a.slot, a.topic, link)
    session.events.append({"type": "booking", "detail": f"Demo booked for {a.slot} IST"})
    session.booked = True
    return {"booked": True, "slot": a.slot, "timezone": "IST", "calendar_link": link, "id": appt["id"]}


# ---------------------------------------------------------------------------
# Tool 5: hand over to a human
# ---------------------------------------------------------------------------
class HandoffArgs(_Args):
    reason: str = Field("", max_length=300)


def handoff_to_human(session, a: HandoffArgs) -> dict:
    session.handoff = True
    get_crm().update_lead(session.lead_id, {"status": "needs_human"})
    session.events.append({"type": "handoff", "detail": a.reason or "Customer asked for a human"})
    return {"handoff": True, "message": "A sales consultant will call back within 30 minutes with the full context."}


# ---------------------------------------------------------------------------
# Staff-only tools (MCP clients with a staff role; never offered to the voice agent)
# ---------------------------------------------------------------------------
class PhoneArgs(_Args):
    phone: str = Field(..., pattern=r"^\+?\d{4,15}$", description="Customer phone number, digits only")


def get_lead(session, a: PhoneArgs) -> dict:
    crm = get_crm()
    lead = crm.find_lead_by_phone(a.phone.lstrip("+"))
    if not lead:
        return {"found": False}
    return {"found": True, "lead": lead, "last_call": crm.last_call_summary(lead["id"]),
            "demos": crm.appointments_for_lead(lead["id"])}


class HotLeadArgs(_Args):
    min_score: int = Field(70, ge=0, le=100)
    limit: int = Field(10, ge=1, le=50)


def list_hot_leads(session, a: HotLeadArgs) -> dict:
    leads = [l for l in get_crm().list_leads() if (l["score"] or 0) >= a.min_score]
    leads.sort(key=lambda l: l["score"] or 0, reverse=True)
    return {"leads": [{k: l[k] for k in ("id", "name", "phone", "company", "score", "status", "need", "timeline")}
                      for l in leads[:a.limit]]}


def search_internal(session, a: SearchArgs) -> dict:
    hits = get_kb("internal").search(a.query, k=5)
    return {"found": bool(hits), "context": format_context(hits)}


# ---------------------------------------------------------------------------
# The registry: name -> (function, input model, line the LLM reads)
# ---------------------------------------------------------------------------
class Tool:
    def __init__(self, fn: Callable, args: type[_Args], llm_help: str):
        self.name, self.fn, self.args, self.help = fn.__name__, fn, args, llm_help

    def schema(self) -> dict:
        return self.args.model_json_schema()


REGISTRY: dict[str, Tool] = {t.name: t for t in [
    Tool(search_knowledge, SearchArgs,
         "search_knowledge(query): search company documents for a different topic than the context you already have."),
    Tool(save_lead_info, LeadArgs,
         "save_lead_info(name, company, authority, need, budget, timeline, email): save the customer's answer to the CURRENT STEP in their own words (need = the service they chose, timeline = in how much time they need it). Never fill a field the customer did not say."),
    Tool(check_availability, AvailabilityArgs,
         "check_availability(day): get free demo slots. day is optional, format YYYY-MM-DD. When you tell the customer a time, use the say_as wording exactly (it has the correct weekday)."),
    Tool(book_appointment, BookArgs,
         'book_appointment(slot, topic, email): book the 30-minute meeting, ONLY when the CALL SCRIPT says so and the customer picked one of the times you offered. slot MUST be exactly one of the free slots, format "YYYY-MM-DD HH:MM". Ask the customer to confirm the time first.'),
    Tool(handoff_to_human, HandoffArgs,
         "handoff_to_human(reason): when the customer asks for a human, is angry, or needs something you cannot answer."),
    Tool(get_lead, PhoneArgs, "get_lead(phone): a lead's CRM record, last call summary and demos."),
    Tool(list_hot_leads, HotLeadArgs, "list_hot_leads(min_score, limit): highest-scoring leads."),
    Tool(search_internal, SearchArgs, "search_internal(query): search CONFIDENTIAL internal documents."),
]}


def tools_for(role: str) -> list[Tool]:
    return [t for t in REGISTRY.values() if can_use_tool(role, t.name)]


def tool_descriptions(role: str = "customer") -> str:
    return "\n" + "\n".join(f"- {t.help}" for t in tools_for(role)) + "\n"


TOOL_DESCRIPTIONS = tool_descriptions("customer")


def run_tool(session, name: str, args: dict, role: str = "customer") -> dict:
    """Check permission -> validate arguments -> run. Never raises: errors go back to the LLM as data."""
    tool = REGISTRY.get(name)
    if tool is None:
        return {"error": f"Unknown tool '{name}'"}
    if not can_use_tool(role, name):
        return {"error": f"Tool '{name}' is not allowed for role '{role}'"}
    if not isinstance(args, dict):
        return {"error": "Arguments must be a JSON object"}
    # The LLM sometimes writes the word "null" for an empty optional field.
    args = {k: v for k, v in args.items() if v not in (None, "null", "None")}
    try:
        parsed = tool.args(**args)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        return {"error": f"Bad arguments: {problems}"}
    return tool.fn(session, parsed)
