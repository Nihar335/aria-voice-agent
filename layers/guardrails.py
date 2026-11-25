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


_VERHOEFF_D = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5], [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
               [3, 4, 0, 1, 2, 8, 9, 5, 6, 7], [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
               [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3], [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
               [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]]
_VERHOEFF_P = [[0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4], [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
               [8, 9, 1, 6, 0, 4, 3, 5, 2, 7], [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
               [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8]]


def _verhoeff_ok(digits: str) -> bool:
    """Aadhaar's check digit (Verhoeff algorithm)."""
    c = 0
    for i, d in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(d)]]
    return c == 0


_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_AADHAAR = re.compile(r"\b[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}\b")
_AADHAAR_WORD = re.compile(r"\b(aadhaa?r|aadhar|uidai|uid)\b|आधार", re.I)
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
    said_aadhaar = bool(_AADHAAR_WORD.search(text))
    aadhaar_hits = []

    def aadhaar(m):
        digits = re.sub(r"\D", "", m.group())
        # "+91 98765 43210" / "919876543210" is a mobile number with the country code, not Aadhaar.
        is_mobile = (digits.startswith("91") and digits[2] in "6789") or text[max(0, m.start() - 1):m.start()] == "+"
        # Real Aadhaar numbers end in a Verhoeff check digit; most other 12-digit numbers fail it.
        if said_aadhaar or (not is_mobile and _verhoeff_ok(digits)):
            aadhaar_hits.append(m.group())
            return "[AADHAAR REMOVED]"
        return m.group()

    text = _AADHAAR.sub(aadhaar, text)
    if aadhaar_hits:
        found.append("aadhaar")
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
    r"system prompt", r"developer mode", r"\bjailbreak", r"\bDAN mode\b", r"\bdo anything now\b",
    # "Can your bot act as a receptionist?" is a normal sales question; only role-switch ATTACKS are blocked.
    r"you are (now|no longer)", r"pretend (to be|you are)",
    r"\b(from now on|now|you will|you must|i want you to)\s+(act|behave|respond) as\b",
    r"\bact as (if you (have|had) no|an? (unrestricted|unfiltered|jailbroken|uncensored))",
    r"(respond|reply|answer) (only )?in (json|code|markdown)",
    # Fake chat-role markers, only at the start of the message or a line ("Our system: Excel" is fine).
    r"(^|\n)\s*\[?(tool result|system|assistant)\]?\s*:", r"\[\s*(tool result|system|guardrail|assistant)\b",
    r"book_appointment\(|handoff_to_human\(",
    r"apne (instructions|rules) (bhool|ignore)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION), re.I)

# Slurs that are abusive whoever they are aimed at.
_ABUSE_RE = re.compile(r"\b(fuck\w*|chutiya|bhenchod|madarchod)\b", re.I)
# Insults count only when aimed at Aria ("you idiot", "stupid bot"), not at a third person
# ("my receptionist is an idiot" is a customer describing their problem).
_INSULT = r"(idiot|stupid|useless|dumb|bastard|bitch|harami|kutta|kamina|bakwas)"
_AT_AGENT = r"(you|you're|youre|u|bot|aria|tum|tu|aap)"
_INSULT_RE = re.compile(
    rf"\b{_AT_AGENT}\b(\s+\w+){{0,3}}?\s+(an?\s+)?{_INSULT}\b|\b{_INSULT}\s+(bot|aria|machine|ai)\b|^\s*{_INSULT}\s*[.!]*\s*$",
    re.I)


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

    if _ABUSE_RE.search(masked) or _INSULT_RE.search(masked):
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
# Not a booking claim: "our calendar is fully booked", "that slot is not booked", "11 AM is already booked".
_NOT_A_BOOKING_RE = re.compile(
    r"\b(fully|not|never|no longer|isn't|wasn't|hasn't|already)\b|n't\b|\b(booked up|slots? (is|are)|time (is|was))\b", re.I)


def claims_booking(reply: str) -> bool:
    for m in _BOOKED_RE.finditer(reply):
        span = m.group()
        negated = bool(_NOT_A_BOOKING_RE.search(span))
        # "I've already booked it" IS a claim; "11 AM is already booked" is not.
        self_claim = bool(re.match(r"i('ve| have)\b", span, re.I)) and not re.search(r"\b(not|never)\b|n't", span, re.I)
        if not negated or self_claim:
            return True
    return False
_LEAK_MARKERS = ["COMPANY FACTS", "RESPOND ONLY WITH JSON", "SECURITY RULES", "TOOLS YOU CAN USE",
                 "LANGUAGE RULES", "reply_en", "customer_en"]
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]")
_SCRIPTS = {"hi": r"[ऀ-ॿ]", "kn": r"[ಀ-೿]", "te": r"[ఀ-౿]"}


def clean_for_voice(reply: str) -> tuple[str, bool]:
    """Remove markdown and emojis, cut very long replies to 2 sentences. Returns (text, changed)."""
    original = reply
    reply = _EMOJI_RE.sub("", reply)
    reply = re.sub(r"[*#`>]+", "", reply)
    # Markdown _italics_ go, but an underscore INSIDE a word stays ("ravi_kumar@gmail.com").
    reply = re.sub(r"(?<!\w)_+|_+(?!\w)", "", reply)
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

    if claims_booking(reply) and not booked_now and not has_previous_booking:
        v.violations.append(("fake_booking", "reply claims a booking but book_appointment did not succeed"))

    if any(marker.lower() in reply.lower() for marker in _LEAK_MARKERS):
        v.violations.append(("prompt_leak", "reply contains system prompt text"))

    # `language` = the language the customer used (decided by code). Small models sometimes keep
    # answering in the previous turn's language. That is regenerated once, but never replaced by a
    # canned reply (a wrong-language answer is still better than no answer): see SOFT_RULES.
    wrong = language_problem(reply, language)
    if wrong:
        v.violations.append(("language_mismatch", wrong))
    return v


SOFT_RULES = {"language_mismatch"}    # regenerate once, but never fall back to a fixed reply


def language_problem(reply: str, expected: str | None) -> str | None:
    from analysis import detect_language
    if not expected or not reply.strip():
        return None
    script = _SCRIPTS.get(expected)
    if script:
        return None if re.search(script, reply) else f"expected {expected} script"
    got = detect_language(reply)
    if expected == "en" and got != "en":
        return f"customer spoke English, reply is {got}"
    if expected == "hinglish" and got != "hinglish":
        return f"customer spoke Hinglish (Hindi in English letters), reply is {got}"
    return None


SAFE_FALLBACK = {
    "unsupported_price": "Let me have our consultant confirm the exact pricing for your case. Shall I book a free 30-minute demo for that?",
    "unsupported_promise": "I'd rather not over-promise. Our consultant can walk you through exactly what we deliver. Shall I book a free demo?",
    "fake_booking": "Let me check the free demo slots for you. Which day works best?",
    "prompt_leak": "I can help with Fortivex AI's services. What would you like to know?",
}
