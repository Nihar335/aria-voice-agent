"""
stt.py - Speech-To-Text: turns the customer's recorded voice into text, on the SERVER.

Why not the browser's own speech recognition (Chrome Web Speech)?
  It is free and instant, but it guesses badly with Indian accents, earphone microphones,
  mixed Hindi/English and Telugu/Kannada, and it can't be told our product words.
  So the page records clean 16 kHz audio and sends it here, and a strong model writes it down:

    Groq Whisper large-v3   very accurate for English and Hindi, ~0.5 s, and we can hint
                            our own words ("Fortivex", "Aria", "BANT"...) with a prompt.
    Gemini (audio in)       best at Telugu, Kannada and Hinglish (it can write Hindi words
                            in English letters, Whisper can't).

STT_PROVIDER=auto picks the better one per language and falls back to the other on any error.
If both fail, the page falls back to Chrome's recognizer, so the customer can always talk.
"""
import json
import re
import time

import httpx

import config

# Words Whisper would otherwise mishear. A prompt "primes" its vocabulary (it is not an instruction).
VOCABULARY = ("Fortivex AI, Aria, voice agent, AI calling, demo, pricing, per month, WhatsApp, CRM, "
              "clinic, Jayanagar, Bengaluru, Hyderabad, Starter plan, Growth plan, appointment, rupees.")

LANGUAGE_NAMES = {
    "en": "English (Indian accent)",
    "hi": "Hindi",
    "hinglish": "Hinglish (Hindi and English mixed)",
    "kn": "Kannada (may mix in English words)",
    "te": "Telugu (may mix in English words)",
}
SCRIPT_RULE = {
    "en": "Write it in English.",
    "hi": "Write the Hindi words in Devanagari script (हिन्दी), e.g. 'मुझे demo चाहिए'. Keep English words in English letters.",
    "hinglish": "Write EVERYTHING in English (Roman) letters, Hindi words too, exactly as spoken, e.g. 'kitna kharcha hoga'.",
    "kn": "Write the Kannada words in KANNADA SCRIPT (ಕನ್ನಡ ಲಿಪಿ), never in English letters, e.g. 'ನನಗೆ demo ಬೇಕು'. "
          "Keep English words (price, demo, clinic...) in English letters.",
    "te": "Write the Telugu words in TELUGU SCRIPT (తెలుగు లిపి), never in English letters, e.g. 'నాకు demo కావాలి'. "
          "Keep English words (price, demo, clinic...) in English letters.",
}
# A transcript in the wrong alphabet (e.g. Telugu written in English letters) makes Aria answer in the
# wrong language, so it counts as a failure and the next model is tried.
SCRIPT_CHARS = {"te": r"[ఀ-౿]", "kn": r"[ಀ-೿]", "hi": r"[ऀ-ॿ]"}


# Common Telugu / Kannada words as they look when written in English letters. Two of them in a
# Latin-only transcript = the model romanised the language instead of using its alphabet.
ROMANISED = {
    "te": set("nenu naaku naku meeku miku meru memu cheyandi cheyali cheyagalara enta entha undi unnayi kavali "
              "kaavali ledu avunu emiti enti ela elaa ikkada akkada repu ivala ippudu chala chaala bagundi "
              "matladali cheppandi vachhe vache taruvata tarvata kuda kooda gurinchi".split()),
    "kn": set("nanage nimage naanu neevu beku illa ide eshtu estu hege yenu enu maadi madi naale ivattu "
              "swalpa tumba chennagide heli banni gottilla aagutte".split()),
}


def script_ok(text: str, language: str) -> bool:
    """False when the transcript is in the wrong alphabet for this language (so it can't be trusted)."""
    if language not in SCRIPT_CHARS or not text.strip():
        return True
    if re.search(r"[ঀ-৿஀-௿ഀ-ൿ઀-૿]", text):            # Bengali/Tamil/Malayalam/Gujarati letters = gibberish
        return False
    native = len(re.findall(SCRIPT_CHARS[language], text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if native >= latin * 0.5:
        return True
    # Mostly English letters: fine if the customer really spoke English, wrong if it is romanised Telugu/Kannada.
    words = set(re.findall(r"[a-z]+", text.lower()))
    return len(words & ROMANISED.get(language, set())) < 2
# Whisper's language codes. Hinglish -> no hint (Whisper would answer in Devanagari for "hi").
WHISPER_LANG = {"en": "en", "hi": "hi", "kn": "kn", "te": "te"}

# Whisper "hears" these in silence or noise. Never send them to the agent as if the customer said them.
# (Real phrases like "thank you" stay: the page only sends audio once it heard someone speak.)
HALLUCINATIONS = {"", ".", "you", "thanks for watching", "thanks for watching.", "please subscribe",
                  "subtitles by the amara.org community"}

_cooldown: dict[str, float] = {}     # provider -> time it may be tried again (after a 429)


def _order(language: str) -> list[str]:
    providers = [p for p in ("groq", "gemini") if (config.GROQ_API_KEY if p == "groq" else config.GEMINI_API_KEY)]
    if config.STT_PROVIDER in providers:
        first = config.STT_PROVIDER
    else:   # auto
        first = "gemini" if language in ("te", "kn", "hinglish") else "groq"
    order = [first] + [p for p in providers if p != first]
    now = time.time()
    ready = [p for p in order if p in providers and _cooldown.get(p, 0) <= now]
    return ready or [p for p in order if p in providers]


def _groq(audio: bytes, mime: str, language: str) -> str:
    data = {"model": config.GROQ_STT_MODEL, "temperature": "0", "response_format": "json", "prompt": VOCABULARY}
    if language in WHISPER_LANG:
        data["language"] = WHISPER_LANG[language]
    r = httpx.post("https://api.groq.com/openai/v1/audio/transcriptions",
                   headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                   data=data, files={"file": ("speech.wav", audio, mime)}, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"groq {r.status_code}: {r.text[:200]}")
    return r.json().get("text", "")


_gemini_client = None


def _gemini(audio: bytes, mime: str, language: str) -> str:
    global _gemini_client
    from google import genai
    from google.genai import types

    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
    rule = SCRIPT_RULE.get(language, SCRIPT_RULE["en"])
    # First decide IF a person is speaking: a model asked only to "transcribe" will happily invent a
    # plausible sentence from a beep, a fan or a TV, and the agent would then answer words nobody said.
    instruction = (
        "You are a careful speech-to-text system. Listen to the audio.\n"
        "Step 1: is a human voice clearly speaking words in it? Beeps, tones, music, breathing, typing, "
        "background noise or silence are NOT speech.\n"
        f"Step 2: only if yes, write down exactly the words spoken, in the order spoken. The speaker uses "
        f"{LANGUAGE_NAMES.get(language, 'English')}. {rule} Never translate, answer, summarise, complete or "
        "guess words you did not hear.\n"
        f"Names that may be said: Fortivex, Aria.\n"
        'Return ONLY JSON: {"speech": true or false, "text": "the exact words, or empty if no speech"}'
    )
    last = None
    for model in [m.strip() for m in config.GEMINI_STT_MODEL.split(",") if m.strip()]:
        try:
            resp = _gemini_client.models.generate_content(
                model=model,
                contents=[types.Part.from_bytes(data=audio, mime_type=mime), instruction],
                config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
            )
            raw = (resp.text or "").strip()
            try:
                data = json.loads(raw)
                text = str(data.get("text", "")) if data.get("speech", True) is not False else ""
            except (ValueError, AttributeError):
                text = raw
            if not script_ok(text, language):
                raise ValueError(f"wrong alphabet for {language}: {text[:60]}")
            return text
        except Exception as e:      # try the next model
            last = e
            print(f"[stt] Gemini {model}: {str(e)[:160]}")
    raise last or RuntimeError("no Gemini STT model configured")


PROVIDERS = {"groq": _groq, "gemini": _gemini}


def clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if text.lower().strip(" !?") in HALLUCINATIONS or text.strip(" .,") == VOCABULARY.strip(" ."):
        return ""
    return text


def transcribe(audio: bytes, mime: str = "audio/wav", language: str = "en") -> dict:
    """Blocking (run it in a thread). Returns {"text", "provider", "stt_ms"}; raises if every provider failed."""
    start = time.perf_counter()
    errors = []
    for name in _order(language):
        try:
            text = clean(PROVIDERS[name](audio, mime, language))
            if not script_ok(text, language):     # e.g. Whisper writing noise as mixed-alphabet gibberish
                raise ValueError(f"transcript in the wrong alphabet for {language}: {text[:60]}")
            _cooldown.pop(name, None)
            return {"text": text, "provider": name, "stt_ms": int((time.perf_counter() - start) * 1000)}
        except Exception as e:
            s = str(e).lower()
            if "429" in s or "rate" in s or "quota" in s or "resource_exhausted" in s:
                _cooldown[name] = time.time() + 30
            errors.append(f"{name}: {str(e)[:160]}")
            print(f"[stt] {name} failed: {str(e)[:200]}")
    raise RuntimeError("speech-to-text failed: " + " | ".join(errors or ["no STT provider has an API key"]))
