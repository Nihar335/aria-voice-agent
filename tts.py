"""
tts.py - Text-To-Speech: turns the agent's reply into a voice.

Two engines:
  edge-tts     the natural "neural" voices of Microsoft Edge's Read Aloud. Free, no key, ~1 s.
               Good for English and Hindi. Its Telugu voice (Shruti) sounds flat and robotic.
  Gemini TTS   Google's newest speech models. Much smoother, more human Telugu with a natural
               accent, and we can ask for a style ("warm, relaxed, like a phone call").
               Slower (~3-4 s) and the free tier allows only a few requests per minute per
               model, so we rotate through several TTS models and fall back to edge-tts.

GEMINI_TTS_LANGS in .env says which languages use Gemini (default: Telugu only).
If everything fails (no internet), we return no audio and the browser speaks the text
with its own built-in voice instead. The call never goes silent.
"""
import asyncio
import base64
import re
import struct
import time

import edge_tts

import config

# One female voice per language, so "Aria" sounds like the same person.
VOICES = {
    "en": "en-IN-NeerjaNeural",
    "hinglish": "en-IN-NeerjaNeural",   # Indian-English voice reads Roman Hindi naturally
    "hi": "hi-IN-SwaraNeural",
    "kn": "kn-IN-SapnaNeural",
    "te": "te-IN-ShrutiNeural",
}
# Speed per language. Speeding up Telugu/Kannada makes them sound more mechanical, so they stay natural.
EDGE_RATE = {"en": "+8%", "hinglish": "+8%", "hi": "+5%", "kn": "+0%", "te": "+0%"}

# How Gemini should SAY it (the model follows a short spoken-style direction before the text).
GEMINI_STYLE = {
    "te": "Say this in natural, warm, conversational Telugu, like a friendly young woman from Hyderabad "
          "talking to a customer on a phone call. Relaxed, smooth pace and clear pronunciation; say the "
          "English words the way Telugu speakers say them",
    "kn": "Say this in natural, warm, conversational Kannada, like a friendly young woman from Bengaluru "
          "talking to a customer on a phone call. Relaxed, smooth pace",
    "hi": "Say this in natural, warm, conversational Hindi, like a friendly young woman talking to a "
          "customer on a phone call. Relaxed, smooth pace",
    "en": "Say this in a warm, friendly Indian-English accent, like a young woman talking to a customer "
          "on a phone call. Relaxed, smooth pace",
}
GEMINI_STYLE["hinglish"] = GEMINI_STYLE["en"]

_gemini_client = None
_cooldown: dict[str, float] = {}     # TTS model -> time it may be tried again (after a 429)


# ---------------------------------------------------------------------------
# Engine 1: Gemini TTS
# ---------------------------------------------------------------------------
def _wav(pcm: bytes, rate: int = 24000) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV header so the browser can play it."""
    header = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack(
        "<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm))
    return header + pcm


def _retry_seconds(e: Exception) -> float:
    m = re.search(r"retryDelay'?:\s*'?(\d+)", str(e))
    return min(max(float(m.group(1)), 5), 600) if m else 60


def _gemini_tts(text: str, language: str) -> tuple[bytes, str]:
    """Blocking. Returns (wav bytes, model). Tries each TTS model that is not rate-limited."""
    global _gemini_client
    from google import genai
    from google.genai import types

    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)
    prompt = f"{GEMINI_STYLE.get(language, GEMINI_STYLE['en'])}:\n{text}"
    speech = types.SpeechConfig(voice_config=types.VoiceConfig(
        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=config.GEMINI_TTS_VOICE)))
    now = time.time()
    models = [m.strip() for m in config.GEMINI_TTS_MODELS.split(",") if m.strip()]
    last = None
    for model in [m for m in models if _cooldown.get(m, 0) <= now]:
        try:
            resp = _gemini_client.models.generate_content(
                model=model, contents=prompt,
                config=types.GenerateContentConfig(response_modalities=["AUDIO"], speech_config=speech))
            blob = resp.candidates[0].content.parts[0].inline_data
            audio = blob.data
            if not audio:
                raise RuntimeError("empty audio")
            if audio[:4] != b"RIFF":        # older models send raw PCM ("audio/L16;rate=24000")
                rate = re.search(r"rate=(\d+)", blob.mime_type or "")
                audio = _wav(audio, int(rate.group(1)) if rate else 24000)
            _cooldown.pop(model, None)
            return audio, model
        except Exception as e:
            last = e
            s = str(e).lower()
            if "429" in s or "resource_exhausted" in s or "quota" in s:
                _cooldown[model] = time.time() + _retry_seconds(e)
            print(f"[tts] Gemini {model} failed: {str(e)[:160]}")
    raise last or RuntimeError("all Gemini TTS models are rate-limited right now")


# ---------------------------------------------------------------------------
# Engine 2: edge-tts
# ---------------------------------------------------------------------------
async def _synthesize(text: str, voice: str, rate: str = "+8%") -> bytes:
    audio = b""
    # stream() sends back small pieces of audio as they are generated.
    async for chunk in edge_tts.Communicate(text, voice, rate=rate).stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return audio


def uses_gemini(language: str) -> bool:
    return bool(config.GEMINI_API_KEY) and language in config.GEMINI_TTS_LANGS


async def speak(text: str, language: str = "en") -> dict:
    """Returns {"audio": base64 audio or None, "audio_mime", "voice", "tts_ms"}."""
    start = time.perf_counter()
    ms = lambda: int((time.perf_counter() - start) * 1000)   # noqa: E731

    if uses_gemini(language):
        try:
            audio, model = await asyncio.wait_for(asyncio.to_thread(_gemini_tts, text, language), timeout=15)
            return {"audio": base64.b64encode(audio).decode(), "audio_mime": "audio/wav",
                    "voice": f"gemini/{config.GEMINI_TTS_VOICE}", "tts_ms": ms()}
        except Exception as e:
            print(f"[tts] Gemini voice unavailable, using edge-tts. Reason: {str(e)[:160]}")

    voice = VOICES.get(language, VOICES["en"])
    try:
        audio = await asyncio.wait_for(_synthesize(text, voice, EDGE_RATE.get(language, "+8%")), timeout=8)
        # base64 = turns binary audio into text, so it fits inside a JSON response.
        encoded = base64.b64encode(audio).decode() if audio else None
    except Exception as e:
        print(f"[tts] edge-tts failed, browser voice will be used. Reason: {e}")
        encoded = None
    return {"audio": encoded, "audio_mime": "audio/mpeg", "voice": voice, "tts_ms": ms()}


# Self-test:  python tts.py   -> creates data/test_<lang>.* files you can play
if __name__ == "__main__":
    samples = {
        "en": "Hello! This is Aria from Fortivex AI.",
        "hi": "नमस्ते! मैं फोर्टिवेक्स एआई से आरिया बोल रही हूँ।",
        "kn": "ನಮಸ್ಕಾರ! ನಾನು ಫೋರ್ಟಿವೆಕ್ಸ್ ಎಐ ಇಂದ ಆರಿಯಾ.",
        "te": "నమస్కారం! నేను ఫోర్టివెక్స్ ఏఐ నుండి ఆరియా. మీకు ఎలా సహాయం చేయగలను?",
    }
    for lang, text in samples.items():
        r = asyncio.run(speak(text, lang))
        if r["audio"]:
            path = config.DATA_DIR / f"test_{lang}.{'wav' if r['audio_mime'] == 'audio/wav' else 'mp3'}"
            path.write_bytes(base64.b64decode(r["audio"]))
            print(f"{lang}: {r['voice']} {r['tts_ms']} ms -> {path.name}")
        else:
            print(f"{lang}: failed")
