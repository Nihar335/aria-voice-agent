"""
layers/router.py - LAYER 7 (part 1): pick the right model for each turn.

Not every turn needs the biggest model:
    "What services do you offer?"        -> FAQ    -> fast, cheap model; answer may be cached
    "Hi" / "ok thanks"                   -> chat   -> fast model
    "Book me tomorrow at 3, I'm Ravi"    -> action -> strong model (tools, dates, exact slots)
    "This is useless, I want a manager"  -> sensitive -> strong model
    Kannada / Telugu                     -> strong model (small models are weaker in these languages)

The rules are plain code (no extra LLM call), so routing costs ~0 ms. If the fast model
decides it needs a tool, the agent switches to the strong model for the rest of the turn.
"""
import re
from dataclasses import dataclass

import config
from analysis import detect_language

_ACTION = re.compile(
    r"\b(book|booking|demo|slot|schedule|appointment|reschedule|cancel|tomorrow|today|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|morning|afternoon|evening|"
    r"\d{1,2}\s*(am|pm|baje)|\d{1,2}:\d{2}|email|e-mail|my name|i am|i'm|naam|mera|call me|call back|"
    r"human|real person|live agent|manager|consultant|kal|parso|subah|shaam)\b|@", re.I)
_AFFIRM = re.compile(r"^\s*(yes|yeah|yep|ok|okay|sure|done|fine|haan|ha|ji|theek|thik|chalega|perfect|great)\b", re.I)
_SENSITIVE = re.compile(
    r"\b(not happy|unhappy|complaint|complain|angry|worst|terrible|refund|cheat|fraud|legal|lawyer|"
    r"cancel (the|my) (contract|project)|bakwas|bekaar)\b", re.I)
_QUESTION = re.compile(
    r"\?|\b(what|which|how|why|when|where|do you|can you|is there|does|kya|kitna|kitne|kaise|kaun|kab|kahan)\b", re.I)
_FOLLOW_UP = re.compile(
    r"\b(this|that|it|these|those|them|iska|iski|iske|yeh|ye|woh|wo|uska|uski|isme|isko|same|also|more)\b", re.I)
_BOOKING_CONTEXT = re.compile(r"\b(demo|slot|time|name|email|book)\b", re.I)


@dataclass
class Route:
    kind: str         # faq | chat | action | sensitive
    tier: str         # fast | strong
    cacheable: bool   # may the answer be served from / saved to the cache?
    reason: str


def route(text: str, session, has_facts: bool, chosen_language: str | None = None) -> Route:
    lang = chosen_language or detect_language(text)
    last_agent = next((t["text"] for t in reversed(session.transcript) if t["who"] == "agent"), "")

    if not config.ROUTER_ENABLED:
        return Route("action", "strong", False, "router disabled")
    if _SENSITIVE.search(text):
        return Route("sensitive", "strong", False, "complaint / emotion words")
    if _ACTION.search(text):
        return Route("action", "strong", False, "booking / contact / handoff words")
    if _AFFIRM.search(text) and _BOOKING_CONTEXT.search(last_agent):
        return Route("action", "strong", False, "short answer to a booking question")
    if lang in ("kn", "te"):
        return Route("chat", "strong", False, f"{lang}: strong model for quality")
    if has_facts and _QUESTION.search(text):
        self_contained = not _FOLLOW_UP.search(text) and len(text.split()) >= 3
        return Route("faq", "fast", self_contained, "question answered from company facts")
    return Route("chat", "fast", False, "small talk / simple turn")
