"""
portal.py - the customer's own "my calls" page (frontend/my-calls.html).

Every customer gets a private link like  /my-calls.html#<secret>.
  - The secret is 192 random bits, stored on their lead in the CRM. It can't be guessed,
    and it is never returned by the normal call APIs (so typing someone's phone number
    on the call page does NOT reveal it).
  - The secret sits after '#', so the browser never sends it in the URL. The page sends it
    in the request body to POST /api/portal.
  - The customer sees ONLY the customer-friendly fields of THEIR OWN calls. Internal
    notes (lead score, tier, objections, sales next steps, agent feedback) are never sent.
  - An admin can reset the link from the dashboard; the old link stops working at once.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from crm import get_crm

router = APIRouter()


def portal_path(lead_id: int) -> str:
    return f"/my-calls.html#{get_crm().portal_token(lead_id)}"


class PortalRequest(BaseModel):
    token: str = Field(..., min_length=20, max_length=100)


def _customer_view(call: dict) -> dict:
    """Pick ONLY the fields a customer may see. Anything new added to the summary stays hidden by default."""
    s = call["summary"] or {}
    return {
        "date": call["created_at"],
        "language": s.get("language"),
        "summary": s.get("customer_summary"),            # None for calls made before this feature
        "next_steps": s.get("customer_next_steps") or [],
        "transcript": [
            {"who": t["who"], "text": t["text"]} for t in call["transcript"]
            if not (t["who"] == "agent" and t["text"].startswith("["))   # skip internal system markers
        ],
    }


@router.post("/api/portal")
def portal(body: PortalRequest):
    crm = get_crm()
    lead = crm.lead_by_portal_token(body.token)
    if not lead:
        raise HTTPException(404, "This link is not valid. Please ask Fortivex for a new one.")
    return {
        "name": lead.get("name"),
        "calls": [_customer_view(c) for c in crm.calls_for_lead(lead["id"])],
        "appointments": [{"start": a["start"], "topic": a["topic"], "link": a["link"]}
                         for a in crm.appointments_for_lead(lead["id"])],
    }
