"""
layers/guardrails.py - LAYER 6: checks BEFORE and AFTER the LLM.

The prompt ASKS the model to behave. Guardrails are code, so they ENFORCE it.

INPUT guard (before the LLM sees the text)
  - rate limits           a script hammering the API is stopped (per call, per IP)
  - sensitive data        card / Aadhaar / PAN / OTP / passwords are masked, so they are never
                          sent to the LLM provider or saved in the CRM transcript
  - prompt injection      "ignore your instructions", "show your system prompt" -> fixed safe reply
  - abuse                 calm reply; the second time -> handoff to a human

OUTPUT guard (before the customer hears the reply)
  - unsupported price     a rupee/dollar amount that is NOT in the retrieved facts -> regenerate
  - unsupported promise   "guaranteed", "100%", "free forever"... not in the facts -> regenerate
  - fake booking          says "booked" but book_appointment did not succeed -> regenerate
  - prompt leak           reply contains pieces of the system prompt -> regenerate
  - voice format          markdown / emojis removed, overly long replies cut to 2 sentences
  - language mismatch     logged (not blocked) so evals and traces show it

A violation triggers ONE regeneration with the reason; if it still fails, a safe fixed reply is used.
"""
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import config

# ---------------------------------------------------------------------------
# Rate limiting (sliding window, in memory; Redis in production)
# ---------------------------------------------------------------------------
_hits: dict[str, deque] = defaultdict(deque)


def rate_limited(key: str, limit: int, window_s: int) -> bool:
    """True if `key` already did `limit` things in the last `window_s` seconds."""
    now, q = time.time(), _hits[key]
    while q and q[0] < now - window_s:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(now)
    return False


# ---------------------------------------------------------------------------
# Input guard
# ---------------------------------------------------------------------------
def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total, alt = total + n, not alt
    return total % 10 == 0


_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_AADHAAR = re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.I)
_OTP = re.compile(r"\b(otp|one[- ]time password|pin|cvv)\b\D{0,15}(\d{3,8})", re.I)
_PASSWORD = re.compile(r"\b(password|passcode|pwd)\b(\s*(is|:|=)\s*)(\S+)", re.I)


def mask_sensitive(text: str) -> tuple[str, list[str]]:
    """Replace sensitive numbers with labels. Returns (masked text, kinds found)."""
    found = []

    def card(m):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append("card")
            return "[CARD NUMBER REMOVED]"
        return m.group()

    text = _CARD.sub(card, text)
    if _AADHAAR.search(text):
        found.append("aadhaar")
        text = _AADHAAR.sub("[AADHAAR REMOVED]", text)
    if _PAN.search(text):
        found.append("pan")
        text = _PAN.sub("[PAN REMOVED]", text)
    if _OTP.search(text):
        found.append("otp")
        text = _OTP.sub(lambda m: m.group(1) + " [CODE REMOVED]", text)
    if _PASSWORD.search(text):
        found.append("password")
        text = _PASSWORD.sub(lambda m: m.group(1) + m.group(2) + "[REMOVED]", text)
    return text, found


_INJECTION = [
    r"ignore (all |any |the |your )?(previous |prior |above |earlier )?(instructions|rules|prompt)",
    r"(forget|disregard|override) (all |your |the )?(previous |prior )?(instructions|rules|prompt)",
    r"(show|reveal|print|repeat|tell me|what is|what's) (me )?(your|the) (system )?(prompt|instructions|rules)",
    r"system prompt", r"developer mode", r"\bjailbreak", r"\bDAN\b",
    r"you are (now|no longer)", r"pretend (to be|you are)", r"act as (a|an|the) (?!customer)",
    r"(respond|reply|answer) (only )?in (json|code|markdown)",
    r"\[?(tool result|system|assistant)\]?\s*:", r"\[\s*(tool result|system|guardrail)\b",
    r"book_appointment\(|handoff_to_human\(",
    r"apne (instructions|rules) (bhool|ignore)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION), re.I)

_ABUSE_RE = re.compile(
    r"\b(fuck\w*|shit|bastard|bitch|idiot|stupid bot|useless bot|chutiya|bhenchod|madarchod|harami|kutta|kamina)\b", re.I)


@dataclass
class InputVerdict:
    text: str                         # the (masked) text to use from now on
    flags: list = field(default_factory=list)
    block_reply: str | None = None    # if set: skip the LLM and say this instead
    handoff: bool = False


def check_input(text: str, session) -> InputVerdict:
    if not config.GUARDRAILS_ENABLED:
        return InputVerdict(text=text)
    masked, kinds = mask_sensitive(text)
    v = InputVerdict(text=masked, flags=[f"pii:{k}" for k in kinds])

    if _INJECTION_RE.search(masked):
        v.flags.append("prompt_injection")
        v.block_reply = ("I can only help with Fortivex AI's services and your business needs. "
                         "What would you like to know about them?")
        return v

    if _ABUSE_RE.search(masked):
        v.flags.append("abuse")
        session.abuse_count = getattr(session, "abuse_count", 0) + 1
        if session.abuse_count >= 2:
            v.handoff = True
            v.block_reply = "I understand you're frustrated. I'm connecting you to a senior consultant who will call you back shortly."
        else:
            v.block_reply = "I'm sorry this has been frustrating. I really want to help. Could you tell me what went wrong?"
        return v

    if kinds:
        # Not blocked: the LLM sees the masked text and the prompt tells it to say it doesn't need such data.
        pass
    return v


# ---------------------------------------------------------------------------
# Output guard
# ---------------------------------------------------------------------------
_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_SCALES = {"hundred": 100, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000, "lac": 100_000,
           "crore": 10_000_000, "crores": 10_000_000, "million": 1_000_000, "k": 1_000}


_NUMWORD = "|".join(sorted([*_UNITS, *_TENS, *(w for w in _SCALES if w != "k")], key=len, reverse=True))
_NUMRUN_RE = re.compile(rf"\b(?:{_NUMWORD})(?:(?:\s+and\s+|\s+|-)(?:{_NUMWORD}))*\b", re.I)


def _run_value(words: list[str]) -> int | None:
    total, current, seen = 0, 0, False
    for w in words:
        if w in _UNITS:
            current += _UNITS[w]; seen = True
        elif w in _TENS:
            current += _TENS[w]; seen = True
        elif w in _SCALES and seen:
            if _SCALES[w] == 100:
                current *= 100
            else:
                total += max(current, 1) * _SCALES[w]; current = 0
        else:
            return None            # starts with a scale word ("thousand") -> leave the text alone
    return total + current


def _words_to_numbers(text: str) -> str:
    """'twenty-five thousand rupees' -> '25000 rupees'. Everything else is left exactly as it was."""
    def repl(m):
        words = [w for w in re.split(r"\s+|-", m.group().lower()) if w and w != "and"]
        value = _run_value(words)
        return m.group() if value is None else str(value)
    return _NUMRUN_RE.sub(repl, text)


_MONEY_RE = re.compile(
    r"(?:₹|rs\.?|inr|\$|usd)\s*(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lac|crores?|thousand|million)?"
    r"|(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lac|crores?|thousand|million)?\s*(?:rupees|rs\b|inr|dollars|usd|/-)",
    re.I)


def money_amounts(text: str) -> set[int]:
    """All money amounts in a text as whole numbers: '₹25,000', '25k rupees', '1.5 lakh rupees' -> 25000 ..."""
    text = _words_to_numbers(text)
    amounts = set()
    for m in _MONEY_RE.finditer(text):
        num, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        value *= _SCALES.get((unit or "").lower(), 1)
        amounts.add(int(round(value)))
    return amounts


def _all_numbers(text: str) -> set[int]:
    """Every number in the facts (with and without units), so '₹25,000' and '25k' both count as allowed."""
    text = _words_to_numbers(text)
    nums = set()
    for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(k|lakhs?|lac|crores?|thousand|million)?", text, re.I):
        try:
            v = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        nums.add(int(round(v)))
        if m.group(2):
            nums.add(int(round(v * _SCALES.get(m.group(2).lower(), 1))))
    return nums


_PROMISE_RE = re.compile(
    r"\b(guarantee[ds]?|100 ?%|free forever|lifetime free|money[- ]back|no risk|risk[- ]free|"
    r"best in (india|the world)|always free|unlimited)\b", re.I)
_BOOKED_RE = re.compile(
    r"\b(i('ve| have)|it's|its|is|has been|you're|you are|demo is|all set|successfully)\b[^.?!]{0,30}"
    r"\b(booked|scheduled|confirmed)\b|\bbooking (is )?confirmed\b", re.I)
_LEAK_MARKERS = ["COMPANY FACTS", "RESPOND ONLY WITH JSON", "SECURITY RULES", "TOOLS YOU CAN USE",
                 "LANGUAGE RULES", "reply_en", "customer_en"]
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
_SCRIPTS = {"hi": r"[ऀ-ॿ]", "kn": r"[ಀ-೿]", "te": r"[ఀ-౿]"}


def clean_for_voice(reply: str) -> tuple[str, bool]:
    """Remove markdown and emojis, cut very long replies to 2 sentences. Returns (text, changed)."""
    original = reply
    reply = _EMOJI_RE.sub("", reply)
    reply = re.sub(r"[*_#`>]+", "", reply)
    reply = re.sub(r"^\s*([-•]|\d+\.)\s+", "", reply, flags=re.M)
    reply = re.sub(r"\s*\n\s*", " ", reply).strip()
    if len(reply.split()) > 60:
        sentences = re.split(r"(?<=[.?!।])\s+", reply)
        reply = " ".join(sentences[:2])
    return reply, reply != original


@dataclass
class OutputVerdict:
    reply: str
    violations: list = field(default_factory=list)   # [(rule, detail)] -> regenerate
    notes: list = field(default_factory=list)        # [(rule, detail)] -> log only


def check_output(reply: str, *, facts: str, customer_text: str, booked_now: bool,
                 has_previous_booking: bool, language: str | None) -> OutputVerdict:
    if not config.GUARDRAILS_ENABLED:
        return OutputVerdict(reply=reply)
    reply, changed = clean_for_voice(reply)
    v = OutputVerdict(reply=reply)
    if changed:
        v.notes.append(("voice_format", "markdown/emoji/length cleaned"))

    allowed = _all_numbers(facts) | _all_numbers(customer_text)
    bad = sorted(a for a in money_amounts(reply) if a not in allowed)
    if bad:
        v.violations.append(("unsupported_price", f"amounts not in the facts: {bad}"))

    fact_text = (facts + " " + customer_text).lower()
    for m in _PROMISE_RE.finditer(reply):
        if m.group().lower() not in fact_text:
            v.violations.append(("unsupported_promise", m.group()))
            break

    if _BOOKED_RE.search(reply) and not booked_now and not has_previous_booking:
        v.violations.append(("fake_booking", "reply claims a booking but book_appointment did not succeed"))

    if any(marker.lower() in reply.lower() for marker in _LEAK_MARKERS):
        v.violations.append(("prompt_leak", "reply contains system prompt text"))

    script = _SCRIPTS.get(language or "")
    if script and not re.search(script, reply):
        v.notes.append(("language_mismatch", f"expected {language} script"))
    return v


SAFE_FALLBACK = {
    "unsupported_price": "Let me have our consultant confirm the exact pricing for your case. Shall I book a free 30-minute demo for that?",
    "unsupported_promise": "I'd rather not over-promise. Our consultant can walk you through exactly what we deliver. Shall I book a free demo?",
    "fake_booking": "Let me check the free demo slots for you. Which day works best?",
    "prompt_leak": "I can help with Fortivex AI's services. What would you like to know?",
}
