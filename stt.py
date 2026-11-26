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
import unicodedata

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import httpx

import config
from analysis import romanised_words

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


# Alphabets a caller can use here: Latin, Devanagari (Hindi), Kannada, Telugu. Letters of any other
# alphabet (Japanese, Bengali, Tamil...) are the model guessing at a word it misheard, or noise.
_KNOWN_LETTER = re.compile(r"[A-Za-zÀ-ɏ̀-ͯऀ-ॿಀ-೿ఀ-౿]")


def _is_letter(ch: str) -> bool:
    """Letters AND vowel signs/viramas (Indian alphabets write many sounds as combining marks)."""
    return ch.isalpha() or unicodedata.category(ch).startswith("M")


def _foreign_letters(word: str) -> int:
    return sum(1 for ch in word if _is_letter(ch) and not _KNOWN_LETTER.match(ch))


def gibberish(text: str) -> bool:
    """Many letters of unrelated alphabets = noise written down as nonsense (Whisper does this)."""
    letters = sum(1 for ch in text if _is_letter(ch))
    return letters > 0 and sum(_foreign_letters(w) for w in text.split()) > letters * 0.2


def drop_foreign_words(text: str) -> str:
    """Remove single words in an unrelated alphabet, e.g. 'シティ ನಾಳೆ ಮೂರು ಗಂಟೆಗೆ' -> 'ನಾಳೆ ಮೂರು ಗಂಟೆಗೆ'."""
    return " ".join(w for w in text.split() if not _foreign_letters(w))


def script_ok(text: str, language: str, spoken: str | None = None) -> bool:
    """
    False when the transcript is in the wrong alphabet for this language (e.g. Kannada written in
    English letters). `spoken` = the language the model says it heard, when it tells us.
    """
    if language not in SCRIPT_CHARS or not text.strip():
        return True
    if gibberish(text):
        return False
    native = len(re.findall(SCRIPT_CHARS[language], text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if native >= latin * 0.5:
        return True
    if spoken == "en":                        # the customer really switched to English: fine
        return True
    if spoken == language:                    # heard Kannada/Telugu but wrote English letters: romanised
        return False
    # Unknown: romanised if it has Kannada/Telugu words in English letters, otherwise treat as English.
    return romanised_words(text, language) < 1


# Whisper's language codes. Hinglish -> no hint (Whisper would answer in Devanagari for "hi").
WHISPER_LANG = {"en": "en", "hi": "hi", "kn": "kn", "te": "te"}

# Whisper "hears" these in silence or noise. Never send them to the agent as if the customer said them.
# (Real phrases like "thank you" stay: the page only sends audio once it heard someone speak.)
HALLUCINATIONS = {"", ".", "you", "thanks for watching", "thanks for watching.", "please subscribe",
                  "subtitles by the amara.org community"}

_cooldown: dict[str, float] = {}     # provider -> time it may be tried again (after a 429)
TIMEOUT_MS = 9000                   # one Gemini transcription attempt


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
_pool = ThreadPoolExecutor(max_workers=6)     # Gemini attempts run side by side (see _gemini)
_model_cooldown: dict[str, float] = {}        # Gemini model -> time it may be tried again


def _retry_seconds(e: Exception) -> float:
    """How long a model must rest: Google says it in 'retryDelay' (up to a day for the daily quota)."""
    m = re.search(r"retryDelay'?:\s*'?(\d+)", str(e))
    if m:
        return min(max(float(m.group(1)), 5), 86400)
    return 60 if "429" in str(e) or "resource_exhausted" in str(e).lower() else 20   # 503 busy / timeout


def _gemini_once(model: str, audio: bytes, mime: str, instruction: str) -> tuple[str, str | None]:
    from google.genai import types

    resp = _gemini_client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=audio, mime_type=mime), instruction],
        config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json"),
    )
    raw = (resp.text or "").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return raw, None
    if not isinstance(data, dict) or data.get("speech", True) is False:
        return "", None
    spoken = str(data.get("language") or "").strip().lower()[:2] or None
    return str(data.get("text") or ""), spoken


def _gemini(audio: bytes, mime: str, language: str) -> tuple[str, str | None]:
    """
    Gemini models are raced two at a time: the first answer in the right alphabet wins (usually the
    fast model, ~2 s). If a model writes Kannada/Telugu in English letters, we wait for the other one,
    and only if no model gets the alphabet right do we keep the English-letters version (the agent
    still understands it, and analysis.reply_language keeps answering in Kannada/Telugu).
    """
    global _gemini_client
    from google import genai

    if _gemini_client is None:
        # timeout (ms): a stuck request must fail fast and fall back, never freeze the call
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY, http_options={"timeout": TIMEOUT_MS})
    instruction = _instruction(language)
    now = time.time()
    models = [m.strip() for m in config.GEMINI_STT_MODEL.split(",") if m.strip()]
    queue = [m for m in models if _model_cooldown.get(m, 0) <= now] or models
    running = {}
    for _ in range(2):
        if queue:
            m = queue.pop(0)
            running[_pool.submit(_gemini_once, m, audio, mime, instruction)] = m
    fallback, last = None, None
    while running:
        done, _ = wait(running, return_when=FIRST_COMPLETED)
        for f in done:
            model = running.pop(f)
            try:
                text, spoken = f.result()
            except Exception as e:
                last = e
                _model_cooldown[model] = time.time() + _retry_seconds(e)
                print(f"[stt] Gemini {model}: {str(e)[:140]}")
            else:
                text = clean(text)
                if gibberish(text):
                    last = ValueError(f"{model}: unreadable transcript")
                else:
                    text = drop_foreign_words(text)
                    if script_ok(text, language, spoken):
                        _model_cooldown.pop(model, None)
                        return text, spoken      # the slower model may still finish; its answer is ignored
                    if fallback is None:
                        fallback = (text, spoken)
                        print(f"[stt] Gemini {model} wrote {language} in English letters, waiting for another model")
        if not running and queue and fallback is None:     # both failed: try the next model
            m = queue.pop(0)
            running[_pool.submit(_gemini_once, m, audio, mime, instruction)] = m
    if fallback:
        return fallback
    raise last or RuntimeError("no Gemini STT model configured")


def _instruction(language: str) -> str:
    rule = SCRIPT_RULE.get(language, SCRIPT_RULE["en"])
    # First decide IF a person is speaking: a model asked only to "transcribe" will happily invent a
    # plausible sentence from a beep, a fan or a TV, and the agent would then answer words nobody said.
    return (
        "You are a careful speech-to-text system. Listen to the audio.\n"
        "Step 1: is a human voice clearly speaking words in it? Beeps, tones, music, breathing, typing, "
        "background noise or silence are NOT speech.\n"
        f"Step 2: only if yes, write down exactly the words spoken, in the order spoken. The speaker uses "
        f"{LANGUAGE_NAMES.get(language, 'English')}. {rule} Never translate, answer, summarise, complete or "
        "guess words you did not hear.\n"
        "Names that may be said: Fortivex, Aria.\n"
        'Return ONLY JSON: {"speech": true or false, "language": "the language spoken: kn, te, hi, en or other", '
        '"text": "the exact words, or empty if no speech"}'
    )


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
            out = PROVIDERS[name](audio, mime, language)
            text, spoken = out if isinstance(out, tuple) else (out, None)
            text = clean(text)
            if gibberish(text):                   # e.g. Whisper writing noise as mixed-alphabet nonsense
                raise ValueError(f"unreadable transcript: {text[:60]}")
            text = drop_foreign_words(text)
            right_alphabet = script_ok(text, language, spoken)
            # Whisper in the wrong alphabet has usually misheard; Gemini in English letters heard right
            # (it only romanised), so its text is kept and Aria still answers in the chosen language.
            if not right_alphabet and name != "gemini":
                raise ValueError(f"transcript in the wrong alphabet for {language}: {text[:60]}")
            _cooldown.pop(name, None)
            return {"text": text, "provider": name, "stt_ms": int((time.perf_counter() - start) * 1000),
                    "romanised": not right_alphabet}
        except Exception as e:
            s = str(e).lower()
            if "429" in s or "rate" in s or "quota" in s or "resource_exhausted" in s:
                _cooldown[name] = time.time() + 30
            errors.append(f"{name}: {str(e)[:160]}")
            print(f"[stt] {name} failed: {str(e)[:200]}")
    raise RuntimeError("speech-to-text failed: " + " | ".join(errors or ["no STT provider has an API key"]))
