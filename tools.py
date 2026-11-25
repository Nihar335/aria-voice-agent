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
# Tool 2: save what the customer told us (name, email, company, need)
# ---------------------------------------------------------------------------
class LeadArgs(_Args):
    name: Optional[str] = Field(None, max_length=80)
    email: Optional[str] = Field(None, max_length=120, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    company: Optional[str] = Field(None, max_length=120)
    need: Optional[str] = Field(None, max_length=300, description="The customer's business problem")


def save_lead_info(session, a: LeadArgs) -> dict:
    lead = get_crm().update_lead(session.lead_id, a.model_dump())
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


class AvailabilityArgs(_Args):
    day: Optional[str] = Field(None, pattern=r"^$|^\d{4}-\d{2}-\d{2}$", description="Optional day, YYYY-MM-DD")


def check_availability(session, a: AvailabilityArgs) -> dict:
    slots = free_slots()
    if a.day:
        slots = [s for s in slots if s.startswith(a.day)] or slots
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


class BookArgs(_Args):
    slot: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", description='Exactly one free slot, "YYYY-MM-DD HH:MM" IST')
    topic: str = Field("Fortivex AI demo", max_length=120)
    email: Optional[str] = Field(None, max_length=120, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")


def book_appointment(session, a: BookArgs) -> dict:
    # GUARDRAIL: the LLM may invent a time. We only accept a real free slot.
    available = free_slots()
    if a.slot not in available:
        return {"booked": False, "error": "That time is not available.", "free_slots": available[:6]}

    crm = get_crm()
    if a.email:
        crm.update_lead(session.lead_id, {"email": a.email})
    lead = crm.get_lead(session.lead_id)
    title = f"{a.topic} - {lead.get('name') or 'Customer'}"
    link = calendar_link(a.slot, title, f"Demo call with Fortivex AI. Customer phone: {lead['phone']}")
    appt = crm.book_appointment(session.lead_id, a.slot, a.topic, link)
    session.events.append({"type": "booking", "detail": f"Demo booked for {a.slot} IST"})
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
         "save_lead_info(name, email, company, need): save details as soon as the customer shares any of them. Pass only what you know."),
    Tool(check_availability, AvailabilityArgs,
         "check_availability(day): get free demo slots. day is optional, format YYYY-MM-DD."),
    Tool(book_appointment, BookArgs,
         'book_appointment(slot, topic, email): book a 30-minute demo. slot MUST be exactly one of the free slots, format "YYYY-MM-DD HH:MM". Ask the customer to confirm the time first.'),
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
