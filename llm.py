"""
llm.py - the ONLY file that talks to the AI model.

Why one file?
  - The rest of the app just calls chat(...). It does not care whether
    Gemini or Groq answers. Switching provider = change LLM_PROVIDER in .env.
  - If the main provider fails (rate limit, network), we automatically
    try the backup provider, so a live demo does not die.
  - We measure how long each call takes (latency) to show on the dashboard.

Message format used everywhere in this project (same as OpenAI style):
    [{"role": "user", "content": "Hi"},
     {"role": "assistant", "content": "Hello! How can I help?"}]
"""
import json
import time
from dataclasses import dataclass

import config


@dataclass
class LLMResult:
    """What every LLM call returns."""
    text: str          # the model's answer
    provider: str      # "gemini" or "groq" (who actually answered)
    latency_ms: int    # how long the call took


# ---------------------------------------------------------------------------
# Provider 1: Google Gemini
# ---------------------------------------------------------------------------
_gemini_client = None  # created once, then reused (faster)


def _call_gemini(system: str, messages: list, json_mode: bool, temperature: float) -> str:
    global _gemini_client
    from google import genai
    from google.genai import types

    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY is missing in .env")
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=config.GEMINI_API_KEY)

    # Gemini calls the assistant "model" instead of "assistant".
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part(text=m["content"])]))

    settings = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        # Ask Gemini to reply with pure JSON when we need structured data.
        response_mime_type="application/json" if json_mode else "text/plain",
    )
    response = _gemini_client.models.generate_content(
        model=config.GEMINI_MODEL, contents=contents, config=settings
    )
    return response.text or ""


# ---------------------------------------------------------------------------
# Provider 2: Groq (very fast, runs open-source Llama models)
# ---------------------------------------------------------------------------
_groq_client = None


def _call_groq(system: str, messages: list, json_mode: bool, temperature: float) -> str:
    global _groq_client
    from groq import Groq

    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is missing in .env")
    if _groq_client is None:
        _groq_client = Groq(api_key=config.GROQ_API_KEY)

    # Groq uses the same format as OpenAI: the system prompt is just the first message.
    all_messages = [{"role": "system", "content": system}] + messages
    extra = {"response_format": {"type": "json_object"}} if json_mode else {}
    response = _groq_client.chat.completions.create(
        model=config.GROQ_MODEL,
        messages=all_messages,
        temperature=temperature,
        **extra,
    )
    return response.choices[0].message.content or ""


PROVIDERS = {"gemini": _call_gemini, "groq": _call_groq}


# ---------------------------------------------------------------------------
# The function the whole app uses
# ---------------------------------------------------------------------------
def chat(system: str, messages: list, json_mode: bool = False, temperature: float = 0.4) -> LLMResult:
    """
    Send a conversation to the LLM and get the answer back.

    system      -> instructions for the AI (its "job description")
    messages    -> the conversation so far
    json_mode   -> True when we want structured data back (a Python dict)
    temperature -> 0 = always the same answer, 1 = more creative
    """
    # Try the chosen provider first, then the other one as backup.
    order = [config.LLM_PROVIDER] + [p for p in PROVIDERS if p != config.LLM_PROVIDER]

    errors = []
    for name in order:
        start = time.perf_counter()
        try:
            text = PROVIDERS[name](system, messages, json_mode, temperature)
            latency = int((time.perf_counter() - start) * 1000)
            return LLMResult(text=text.strip(), provider=name, latency_ms=latency)
        except Exception as e:  # rate limit, no key, network error...
            errors.append(f"{name}: {e}")
            print(f"[llm] {name} failed, trying backup. Reason: {e}")

    raise RuntimeError("All LLM providers failed -> " + " | ".join(errors))


def chat_json(system: str, messages: list, temperature: float = 0.2) -> tuple[dict, LLMResult]:
    """
    Same as chat(), but returns a Python dict.
    Used for tool calls, lead scoring, summaries - anything the code must read.
    """
    result = chat(system, messages, json_mode=True, temperature=temperature)
    text = result.text
    # Some models wrap JSON in ```json ... ``` - remove that if present.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last attempt: cut from the first "{" to the last "}".
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    return data, result


# ---------------------------------------------------------------------------
# Quick self-test:  python llm.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("Provider order:", config.LLM_PROVIDER, "-> backup")
    r = chat(
        system="You are a friendly sales assistant. Answer in one short sentence.",
        messages=[{"role": "user", "content": "Say hello in Hinglish."}],
    )
    print(f"[{r.provider}, {r.latency_ms} ms] {r.text}")

    data, r = chat_json(
        system='Reply only as JSON: {"sentiment": "positive|neutral|negative"}',
        messages=[{"role": "user", "content": "Your price is too high, I am not happy."}],
    )
    print(f"[{r.provider}, {r.latency_ms} ms] JSON ->", data)
