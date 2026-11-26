"""
analysis.py - understanding the customer while they talk, and after the call.

Three jobs:
  1. detect_language(text)  -> which language is the customer using?   (pure code, instant)
  2. analyze_turn(session)  -> sentiment + BANT lead qualification       (LLM, runs in background)
  3. end_call(session)      -> call summary + insights, saved to the CRM (LLM, once per call)

Design rule: the LLM EXTRACTS facts ("budget: about 20k per month"),
but our CODE calculates the lead score. Scores must be consistent and
explainable, and an LLM guessing "73/100" is neither.
"""
import re

import config  # noqa: F401 (sets UTF-8 output)
import llm
from crm import get_crm

# ---------------------------------------------------------------------------
# 1. Language detection by alphabet (script). No AI needed, takes ~0 ms.
# ---------------------------------------------------------------------------
# Common Hindi words written in English letters -> Hinglish.
HINGLISH_WORDS = set("""hai hain kya kaise kitna kitne kitni mujhe hum aap aapka aapke mera meri mere
nahi nahin haan ji kar karna karo chahiye chahte batao bataiye accha acha theek thik kaam
paisa paise wala wali sakte sakta hoga hogi kab kahan kyun lekin aur bhi
thoda thodi raha rahe rahi milega milegi chalega mehenga mehnga sasta bahut abhi kuch kaun kaunsa
matlab samajh dijiye chahta chahti lagega lagta hoon hu tha thi diya kiya karenge batana""".split())


def detect_language(text: str) -> str:
    if re.search(r"[ಀ-೿]", text):   # Kannada alphabet
        return "kn"
    if re.search(r"[ఀ-౿]", text):   # Telugu alphabet
        return "te"
    if re.search(r"[ऀ-ॿ]", text):   # Devanagari (Hindi)
        return "hi"
    words = set(re.findall(r"[a-z]+", text.lower()))
    if len(words & HINGLISH_WORDS) >= 2:
        return "hinglish"
    return "en"


# Common Telugu / Kannada words as they look when written in English letters ("romanised").
# Speech-to-text sometimes writes Kannada/Telugu this way; the customer still spoke Kannada/Telugu.
ROMANISED = {
    "te": set("""nenu naaku naku naa nee meeku miku meru memu mee cheyandi cheyyandi cheyali cheyagalara chestanu
chesthanu enta entha undi unnayi untundi untadi kavali kaavali kavalante ledu avunu emiti enti emi ela elaa
ikkada akkada ekkada eppudu enduku evaru repu ivala ippudu chala chaala bagundi manchidi sare saare ayithe kani
mari inka vaddu kaadu kadu matladali cheppandi cheppu ivvandi vachhe vache taruvata tarvata kuda kooda gurinchi
avtundi avuthundi gantalaku gantaki""".split()),
    "kn": set("""nanage nimage naanu neevu nanna nimma beku beda illa ide eshtu estu hege yenu enu yaake yelli
yaavaga maadi madi maadri naale ivattu swalpa tumba chennagide heli helthini banni baa gottilla aagutte aaytu
aytu houdu haudu ondu eradu mooru gantege gantige kodi kodu aagbahudu barthini bartini hogi""".split()),
}


def romanised_words(text: str, language: str) -> int:
    """How many romanised Telugu/Kannada words a Latin-letter text contains."""
    return len(set(re.findall(r"[a-z]+", text.lower())) & ROMANISED.get(language, set()))


def reply_language(text: str, chosen: str | None = None) -> str:
    """
    Which language Aria must answer in, decided by code (not left to the model):
      - Hindi / Kannada / Telugu script, or clear Hinglish -> that language, whatever the menu says
      - plain English letters -> English, except:
          a customer who picked any other language keeps it ("2 lakh", romanised Telugu/Kannada, a few English
          words), unless they ask for English. The call never jumps to English on its own.
    """
    detected = detect_language(text)
    if chosen and chosen != "en" and detected in ("en", "hinglish") and not asks_for_english(text):
        # The customer picked Hindi/Hinglish/Kannada/Telugu: a budget, a number, a name or a few English
        # words typed in English letters ("2 lakh", "AI voice agent") must not switch the call to English.
        # Only another Indian alphabet, or asking for English, switches it.
        return "hinglish" if chosen == "hi" and detected == "hinglish" else chosen
    return detected


_ASK_ENGLISH = re.compile(r"\b(in|speak|talk|reply|use|switch to|only)\s+english\b|\benglish\s+(please|only|lo|mein|me)\b", re.I)


def asks_for_english(text: str) -> bool:
    """"Speak in English please", "English lo cheppandi", "English mein bolo"."""
    return bool(_ASK_ENGLISH.search(text or ""))


# ---------------------------------------------------------------------------
# 2. Sentiment + BANT, after every customer turn
# ---------------------------------------------------------------------------
TURN_PROMPT = """You analyse a live sales call for Fortivex AI. Read the conversation and return ONLY JSON:
{
 "sentiment": "positive | neutral | negative",
 "sentiment_score": number from -1.0 (very negative) to 1.0 (very positive), for the customer's LATEST message,
 "emotion": one word, e.g. curious, interested, confused, frustrated, excited, hesitant,
 "intent": one short phrase, what the customer wants right now,
 "bant": {
   "budget":    {"value": "what they said or null", "status": "unknown | partial | clear"},
   "authority": {"value": "...", "status": "unknown | partial | clear"},
   "need":      {"value": "...", "status": "unknown | partial | clear"},
   "timeline":  {"value": "...", "status": "unknown | partial | clear"}
 },
 "lead_fields": {"name": null, "email": null, "company": null},
 "buying_signals": ["short phrases, may be empty"],
 "objections": ["short phrases, may be empty"],
 "next_best_action": "one short suggestion for the agent",
 "recap": "1-2 short, friendly English sentences written TO the customer ('you'): what has been discussed so far and anything agreed",
 "recap_points": ["up to 4 short English facts the customer told us or was told by Aria, e.g. 'You run a dental clinic in Jayanagar', 'You asked about the Starter plan price'"]
}
"recap" and "recap_points" are shown LIVE to the customer: never put scores, BANT, sentiment, objections,
sales tactics or internal notes in them, and no price/discount that Aria did not actually say.
Rules: only use what the CUSTOMER actually said. "clear" = a specific answer (e.g. "budget 20k per month", "I am the owner", "need it this month"). "partial" = vague hint. Otherwise "unknown"."""

# Points per BANT item. Need matters most for us: no need = no sale.
BANT_WEIGHTS = {"need": 35, "budget": 25, "authority": 20, "timeline": 20}
STATUS_FACTOR = {"clear": 1.0, "partial": 0.5, "unknown": 0.0}


def lead_score(bant: dict, sentiment_score: float = 0.0, demo_booked: bool = False) -> tuple[int, str]:
    """Deterministic, explainable score from 0 to 100, and a tier (hot / warm / cold)."""
    score = 0
    for item, weight in BANT_WEIGHTS.items():
        status = (bant.get(item) or {}).get("status", "unknown")
        score += weight * STATUS_FACTOR.get(status, 0)
    score += 5 * sentiment_score            # mood nudges the score by at most 5 points
    if demo_booked:
        score += 10                          # strongest buying signal
    score = max(0, min(100, round(score)))
    tier = "hot" if score >= 70 else "warm" if score >= 40 else "cold"
    return score, tier


# Backup if the LLM is unavailable: simple word lists.
NEGATIVE = set("expensive costly bad worst angry useless waste scam not interested no frustrated mehenga bekar".split())
POSITIVE = set("great good nice interested perfect excellent awesome yes book love accha badhiya".split())


def _fallback_sentiment(text: str) -> tuple[str, float]:
    words = set(re.findall(r"[a-z]+", text.lower()))
    s = len(words & POSITIVE) - len(words & NEGATIVE)
    if s > 0:
        return "positive", 0.5
    if s < 0:
        return "negative", -0.5
    return "neutral", 0.0


def analyze_turn(session) -> dict:
    """Update session.analysis after a customer message. Called in the background."""
    convo = "\n".join(f"{t['who'].upper()}: {t['text']}" for t in session.transcript[-12:])
    prev = session.analysis or {}
    try:
        # Background + fast tier: scoring must never wait for, or use up, the limits the live call needs.
        data, result = llm.chat_json(TURN_PROMPT, [{"role": "user", "content": convo}], temperature=0.1,
                                     prefer="gemini", tier="fast", background=True)
        data["analysis_ms"] = result.latency_ms
    except Exception as e:
        print(f"[analysis] LLM failed, using keyword sentiment. Reason: {e}")
        last = session.transcript[-2]["text"] if len(session.transcript) >= 2 else ""
        label, score = _fallback_sentiment(last)
        data = {**prev, "sentiment": label, "sentiment_score": score}

    # Clean "null"-like words the LLM may return, so the cockpit shows "—" instead.
    for item in (data.get("bant") or {}).values():
        if isinstance(item, dict) and str(item.get("value")).strip().lower() in ("null", "none", "unknown", "n/a", ""):
            item["value"] = None

    crm = get_crm()
    demo_booked = crm.get_lead(session.lead_id)["status"] == "demo_booked"
    data["score"], data["tier"] = lead_score(data.get("bant", {}), float(data.get("sentiment_score") or 0), demo_booked)

    # Keep a history of sentiment for the trend chart.
    data["sentiment_history"] = prev.get("sentiment_history", []) + [float(data.get("sentiment_score") or 0)]

    # Two negative turns in a row -> suggest a human takes over.
    hist = data["sentiment_history"]
    data["alert"] = "Customer upset twice in a row - consider human handoff" if len(hist) >= 2 and hist[-1] < -0.3 and hist[-2] < -0.3 else None

    session.analysis = data

    # Save what we learned into the CRM.
    bant = data.get("bant", {})
    fields = {k: v for k, v in (data.get("lead_fields") or {}).items() if v}
    fields.update({
        "language": session.language,
        "sentiment": data.get("sentiment"),
        "score": data["score"],
        # Only CLEAR answers go into the CRM: a "partial" guess (e.g. "runs the clinic" -> decision maker?)
        # must not count as answered, or Aria would skip that booking question (tools.BOOKING_CHECKLIST).
        **{k: (bant.get(k) or {}).get("value") for k in ("budget", "authority", "need", "timeline")
           if (bant.get(k) or {}).get("status") == "clear"},
    })
    if not demo_booked and not session.handoff:
        fields["status"] = data["tier"]
    crm.update_lead(session.lead_id, fields)
    return data


# ---------------------------------------------------------------------------
# 3. Summary + insights when the call ends
# ---------------------------------------------------------------------------
SUMMARY_PROMPT = """You are a sales manager reviewing a finished call between Aria (AI agent for Fortivex AI) and a customer.
Return ONLY JSON, written in ENGLISH even if the call was in another language:
{
 "summary": "2-3 sentences: who the customer is, what they need, what happened",
 "customer_needs": ["..."],
 "objections": ["..."],
 "outcome": "demo_booked | interested | needs_follow_up | handed_to_human | not_interested",
 "next_steps": ["concrete actions for the sales team"],
 "follow_up": "when and how to follow up",
 "agent_feedback": "one line: what the AI agent did well or should improve",
 "customer_summary": "2-3 friendly sentences written TO the customer (use 'you'): what you discussed and what was agreed. The customer will read this.",
 "customer_next_steps": ["what happens next, written for the customer, e.g. 'Our consultant will call you on Tuesday'"]
}
Everything except customer_summary and customer_next_steps is INTERNAL (the customer never sees it).
customer_summary and customer_next_steps must NOT contain scores, objections, sales tactics, internal notes,
or any price/discount that was not clearly said to the customer on the call."""


def end_call(session) -> dict:
    """Create the summary, save the call in the CRM, return everything for the dashboard."""
    crm = get_crm()
    convo = "\n".join(f"{t['who'].upper()}: {t['text']}" for t in session.transcript)
    if not session.transcript:
        summary = {"summary": "Call ended before the customer spoke.", "outcome": "not_interested",
                   "customer_summary": "The call ended before we could talk."}
    else:
        try:
            summary, _ = llm.chat_json(SUMMARY_PROMPT, [{"role": "user", "content": convo}], temperature=0.2,
                                     prefer="gemini")
        except Exception as e:
            print(f"[analysis] Summary failed. Reason: {e}")
            summary = {"summary": "Automatic summary unavailable. See transcript.", "outcome": "needs_follow_up",
                       "customer_summary": "Thanks for calling Fortivex AI. Our team will follow up with you soon."}

    a = session.analysis or {}
    summary.update({
        "lead_score": a.get("score", 0),
        "tier": a.get("tier", "cold"),
        "sentiment_trend": a.get("sentiment_history", []),
        "language": session.language,
        "turns": len([t for t in session.transcript if t["who"] == "customer"]),
    })
    summary["call_id"] = crm.save_call(session.lead_id, session.transcript, summary)
    return summary


# Self-test of the parts that need no API key:  python analysis.py
if __name__ == "__main__":
    for t in ["How much does it cost?", "Mujhe batao kitna paisa lagega", "मुझे डेमो चाहिए", "ನಮಸ್ಕಾರ, ಬೆಲೆ ಎಷ್ಟು?"]:
        print(f"{detect_language(t):9} <- {t}")
    bant = {"need": {"status": "clear"}, "budget": {"status": "partial"},
            "authority": {"status": "clear"}, "timeline": {"status": "unknown"}}
    print("score:", lead_score(bant, 0.6), "| with demo booked:", lead_score(bant, 0.6, True))
