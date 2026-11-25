"""
tts.py - Text-To-Speech: turns the agent's reply into an MP3 voice.

We use edge-tts: the same natural "neural" voices as Microsoft Edge's
Read Aloud feature. Free, no API key, and it has Indian voices for
English, Hindi, Kannada and Telugu.

If it fails (no internet), we return None and the browser speaks the
text with its own built-in voice instead. The call never goes silent.
"""
import asyncio
import base64
import time

import edge_tts

# One female voice per language, so "Aria" sounds like the same person.
VOICES = {
    "en": "en-IN-NeerjaNeural",
    "hinglish": "en-IN-NeerjaNeural",   # Indian-English voice reads Roman Hindi naturally
    "hi": "hi-IN-SwaraNeural",
    "kn": "kn-IN-SapnaNeural",
    "te": "te-IN-ShrutiNeural",
}


async def _synthesize(text: str, voice: str) -> bytes:
    audio = b""
    # stream() sends back small pieces of audio as they are generated.
    async for chunk in edge_tts.Communicate(text, voice, rate="+8%").stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return audio


async def speak(text: str, language: str = "en") -> dict:
    """Returns {"audio": base64 MP3 or None, "voice": ..., "tts_ms": ...}."""
    voice = VOICES.get(language, VOICES["en"])
    start = time.perf_counter()
    try:
        audio = await asyncio.wait_for(_synthesize(text, voice), timeout=8)
        # base64 = turns binary audio into text, so it fits inside a JSON response.
        encoded = base64.b64encode(audio).decode() if audio else None
    except Exception as e:
        print(f"[tts] edge-tts failed, browser voice will be used. Reason: {e}")
        encoded = None
    return {"audio": encoded, "voice": voice, "tts_ms": int((time.perf_counter() - start) * 1000)}


# Self-test:  python tts.py   -> creates data/test_<lang>.mp3 files you can play
if __name__ == "__main__":
    import config

    samples = {
        "en": "Hello! This is Aria from Fortivex AI.",
        "hi": "नमस्ते! मैं फोर्टिवेक्स एआई से आरिया बोल रही हूँ।",
        "kn": "ನಮಸ್ಕಾರ! ನಾನು ಫೋರ್ಟಿವೆಕ್ಸ್ ಎಐ ಇಂದ ಆರಿಯಾ.",
    }
    for lang, text in samples.items():
        r = asyncio.run(speak(text, lang))
        if r["audio"]:
            path = config.DATA_DIR / f"test_{lang}.mp3"
            path.write_bytes(base64.b64decode(r["audio"]))
            print(f"{lang}: {r['voice']} {r['tts_ms']} ms -> {path.name}")
        else:
            print(f"{lang}: failed")
