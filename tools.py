"""
tools.py - actions the agent is ALLOWED to take.

The LLM can only talk. Tools let it DO things: look up facts, save the
customer's details, check free slots, book a demo, call a human.

How it works:
  1. We describe each tool to the LLM in plain English (TOOL_DESCRIPTIONS).
  2. The LLM replies with JSON like {"tool": "book_appointment", "args": {...}}.
  3. OUR code checks the request and runs the Python function.
  4. The result goes back to the LLM, which then answers the customer.
The LLM never touches the database itself. Our code is the gatekeeper.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from crm import get_crm
from rag import format_context, get_kb

IST = timezone(timedelta(hours=5, minutes=30))
DEMO_TIMES = ["11:00", "15:00", "17:00"]     # demo slots offered each working day
DEMO_MINUTES = 30


# ---------------------------------------------------------------------------
# Tool 1: search the knowledge base (RAG from Stage 2)
# ---------------------------------------------------------------------------
def search_knowledge(session, query: str = "") -> dict:
    hits = get_kb().search(query)
    session.sources.extend(h["source"] for h in hits)
    return {"found": bool(hits), "context": format_context(hits)}


# ---------------------------------------------------------------------------
# Tool 2: save what the customer told us (name, email, company, need)
# ---------------------------------------------------------------------------
def save_lead_info(session, name=None, email=None, company=None, need=None) -> dict:
    lead = get_crm().update_lead(
        session.lead_id, {"name": name, "email": email, "company": company, "need": need}
    )
    return {"saved": True, "lead": {k: lead[k] for k in ("name", "email", "company", "need")}}


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


def check_availability(session, day: str = "") -> dict:
    slots = free_slots()
    if day:
        slots = [s for s in slots if s.startswith(day)] or slots
    return {"timezone": "IST", "free_slots": slots[:6]}


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


def book_appointment(session, slot: str = "", topic: str = "Fortivex AI demo", email=None) -> dict:
    # GUARDRAIL: the LLM may invent a time. We only accept a real free slot.
    available = free_slots()
    if slot not in available:
        return {"booked": False, "error": "That time is not available.", "free_slots": available[:6]}

    crm = get_crm()
    if email:
        crm.update_lead(session.lead_id, {"email": email})
    lead = crm.get_lead(session.lead_id)
    title = f"{topic} - {lead.get('name') or 'Customer'}"
    link = calendar_link(slot, title, f"Demo call with Fortivex AI. Customer phone: {lead['phone']}")
    appt = crm.book_appointment(session.lead_id, slot, topic, link)
    session.events.append({"type": "booking", "detail": f"Demo booked for {slot} IST"})
    return {"booked": True, "slot": slot, "timezone": "IST", "calendar_link": link, "id": appt["id"]}


# ---------------------------------------------------------------------------
# Tool 5: hand over to a human
# ---------------------------------------------------------------------------
def handoff_to_human(session, reason: str = "") -> dict:
    session.handoff = True
    get_crm().update_lead(session.lead_id, {"status": "needs_human"})
    session.events.append({"type": "handoff", "detail": reason or "Customer asked for a human"})
    return {"handoff": True, "message": "A sales consultant will call back within 30 minutes with the full context."}


# ---------------------------------------------------------------------------
# Registry: name -> function, and the descriptions the LLM reads.
# ---------------------------------------------------------------------------
TOOLS = {
    "search_knowledge": search_knowledge,
    "save_lead_info": save_lead_info,
    "check_availability": check_availability,
    "book_appointment": book_appointment,
    "handoff_to_human": handoff_to_human,
}

TOOL_DESCRIPTIONS = """
- search_knowledge(query): search company documents for a different topic than the context you already have.
- save_lead_info(name, email, company, need): save details as soon as the customer shares any of them. Pass only what you know.
- check_availability(day): get free demo slots. day is optional, format YYYY-MM-DD.
- book_appointment(slot, topic, email): book a 30-minute demo. slot MUST be exactly one of the free slots, format "YYYY-MM-DD HH:MM". Ask the customer to confirm the time first.
- handoff_to_human(reason): when the customer asks for a human, is angry, or needs something you cannot answer.
"""


def run_tool(session, name: str, args: dict) -> dict:
    """Run a tool safely. Unknown tools or bad arguments return an error instead of crashing."""
    if name not in TOOLS:
        return {"error": f"Unknown tool '{name}'"}
    try:
        return TOOLS[name](session, **(args or {}))
    except TypeError as e:          # LLM passed a wrong argument name
        return {"error": f"Bad arguments: {e}"}
