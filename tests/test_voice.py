"""
Offline tests for voice IN (stt.py) and voice OUT (tts.py) routing: no network, providers are stubbed.
    .venv\Scripts\python -m unittest discover tests -v
"""
import asyncio
import base64
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config    # noqa: E402
import stt       # noqa: E402
import tts       # noqa: E402


class SpeechToText(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(stt.PROVIDERS), config.STT_PROVIDER, config.GROQ_API_KEY, config.GEMINI_API_KEY)
        config.STT_PROVIDER, config.GROQ_API_KEY, config.GEMINI_API_KEY = "auto", "g", "m"
        stt._cooldown.clear()
        self.calls = []

    def tearDown(self):
        providers, config.STT_PROVIDER, config.GROQ_API_KEY, config.GEMINI_API_KEY = self.saved
        stt.PROVIDERS.clear(); stt.PROVIDERS.update(providers)
        stt._cooldown.clear()

    def stub(self, name, result):
        def fn(audio, mime, language):
            self.calls.append(name)
            if isinstance(result, Exception):
                raise result
            return result
        stt.PROVIDERS[name] = fn

    def test_telugu_goes_to_gemini_english_to_whisper(self):
        self.stub("groq", "hello"); self.stub("gemini", "నమస్కారం")
        self.assertEqual(stt.transcribe(b"x", "audio/wav", "te")["provider"], "gemini")
        self.assertEqual(stt.transcribe(b"x", "audio/wav", "hinglish")["provider"], "gemini")
        self.assertEqual(stt.transcribe(b"x", "audio/wav", "en")["provider"], "groq")

    def test_falls_back_and_cools_down_a_rate_limited_provider(self):
        self.stub("groq", RuntimeError("groq 429: rate limit")); self.stub("gemini", "price please")
        self.assertEqual(stt.transcribe(b"x", "audio/wav", "en")["text"], "price please")
        self.assertIn("groq", stt._cooldown)
        self.calls.clear()
        stt.transcribe(b"x", "audio/wav", "en")
        self.assertEqual(self.calls, ["gemini"])          # groq skipped while cooling down

    def test_all_failed_raises(self):
        self.stub("groq", RuntimeError("down")); self.stub("gemini", RuntimeError("down"))
        with self.assertRaises(RuntimeError):
            stt.transcribe(b"x", "audio/wav", "en")

    def test_wrong_alphabet_counts_as_a_failure(self):
        self.stub("gemini", "Namaskaram nenu demo kavali")          # Telugu in English letters
        self.stub("groq", "ಈ്సેన്అనే")                              # noise as mixed-alphabet gibberish
        with self.assertRaises(RuntimeError):
            stt.transcribe(b"x", "audio/wav", "te")
        self.stub("gemini", "నాకు demo కావాలి")
        self.assertEqual(stt.transcribe(b"x", "audio/wav", "te")["text"], "నాకు demo కావాలి")
        self.assertTrue(stt.script_ok("price ఎంత?", "te"))
        self.assertTrue(stt.script_ok("How much?", "en"))
        self.assertTrue(stt.script_ok("Can I book a demo tomorrow at 3?", "te"))   # Telugu picked, English spoken
        self.assertFalse(stt.script_ok("Meeku repu demo kavali?", "te"))

    def test_whisper_silence_hallucinations_are_dropped(self):
        self.assertEqual(stt.clean(" Thanks for watching! "), "")
        self.assertEqual(stt.clean(stt.VOCABULARY), "")
        self.assertEqual(stt.clean("Thank you."), "Thank you.")      # a real customer phrase stays
        self.assertEqual(stt.clean("  how   much  "), "how much")


class TextToSpeech(unittest.TestCase):
    def setUp(self):
        self.saved = (tts._gemini_tts, tts._synthesize, config.GEMINI_API_KEY, config.GEMINI_TTS_LANGS)
        config.GEMINI_API_KEY, config.GEMINI_TTS_LANGS = "k", {"te"}

        async def edge(text, voice, rate="+8%"):
            self.edge_voice = voice
            return b"mp3"
        tts._synthesize = edge

    def tearDown(self):
        tts._gemini_tts, tts._synthesize, config.GEMINI_API_KEY, config.GEMINI_TTS_LANGS = self.saved

    def test_telugu_uses_gemini_voice(self):
        tts._gemini_tts = lambda text, lang: (b"RIFFwav", "m")
        r = asyncio.run(tts.speak("నమస్కారం", "te"))
        self.assertEqual((r["audio_mime"], base64.b64decode(r["audio"])), ("audio/wav", b"RIFFwav"))
        self.assertTrue(r["voice"].startswith("gemini/"))

    def test_gemini_failure_falls_back_to_edge(self):
        def fail(text, lang):
            raise RuntimeError("429 quota")
        tts._gemini_tts = fail
        r = asyncio.run(tts.speak("నమస్కారం", "te"))
        self.assertEqual((r["audio_mime"], self.edge_voice), ("audio/mpeg", "te-IN-ShrutiNeural"))

    def test_english_stays_on_edge(self):
        tts._gemini_tts = lambda text, lang: self.fail("Gemini must not be used for English")
        r = asyncio.run(tts.speak("Hello", "en"))
        self.assertEqual(self.edge_voice, "en-IN-NeerjaNeural")
        self.assertEqual(r["audio_mime"], "audio/mpeg")

    def test_raw_pcm_is_wrapped_as_wav(self):
        wav = tts._wav(b"\0\0" * 10, 24000)
        self.assertEqual((wav[:4], wav[8:12], len(wav)), (b"RIFF", b"WAVE", 44 + 20))


if __name__ == "__main__":
    unittest.main()
