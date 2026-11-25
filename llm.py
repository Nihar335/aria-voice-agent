"""
llm.py - the ONLY file that talks to the AI model.

Why one file?
  - The rest of the app just calls chat(...). It does not care whether
    Gemini or Groq answers. Switching provider = change LLM_PROVIDER in .env.
  - If a model fails (overloaded, rate limit, network), we automatically try
    the next model, then the other provider, so a live demo does not die.
    Each provider can list several models in .env, e.g.
        GROQ_MODEL=qwen/qwen3.8-27b,openai/gpt-oss-120b
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
    provider: str      # e.g. "groq/qwen/qwen3.8-27b" (who actually answered)
    latency_ms: int    # how long the call took
    input_tokens: int = 0      # tokens we sent (from the provider, or estimated)
    output_tokens: int = 0     # tokens the model wrote
    failed: tuple = ()         # models that failed before this one answered (for traces)
    tier: str = "strong"       # which model list the router picked


def _estimate_tokens(text: str) -> int:
    """Rough count when a provider does not report usage: ~4 characters per token."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Provider 1: Google Gemini
# ---------------------------------------------------------------------------
_gemini_client = None  # created once, then reused (faster)


def _call_gemini(model: str, system: str, messages: list, json_mode: bool, temperature: float) -> tuple[str, dict]:
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
        model=model, contents=contents, config=settings
    )
    u = getattr(response, "usage_metadata", None)
    usage = {"in": getattr(u, "prompt_token_count", 0) or 0, "out": getattr(u, "candidates_token_count", 0) or 0}
    return response.text or "", usage


# ---------------------------------------------------------------------------
# Provider 2: Groq (very fast, runs open-source Llama models)
# ---------------------------------------------------------------------------
_groq_client = None


def _call_groq(model: str, system: str, messages: list, json_mode: bool, temperature: float) -> tuple[str, dict]:
    global _groq_client
    from groq import Groq

    if not config.GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is missing in .env")
    if _groq_client is None:
        # max_retries=0: if this model is rate-limited, fail FAST and let chat() try the
        # next model, instead of the library silently waiting up to 30 seconds.
        _groq_client = Groq(api_key=config.GROQ_API_KEY, max_retries=0, timeout=15)

    # Groq uses the same format as OpenAI: the system prompt is just the first message.
    all_messages = [{"role": "system", "content": system}] + messages
    extra = {"response_format": {"type": "json_object"}} if json_mode else {}
    try:
        response = _groq_client.chat.completions.create(
            model=model,
            messages=all_messages,
            temperature=temperature,
            **extra,
        )
    except Exception as e:
        # Groq refuses answers that are not valid JSON, but still sends the text back
        # as "failed_generation". That text is usually a fine reply, so we keep it.
        body = getattr(e, "body", None) or {}
        failed = (body.get("error") or {}).get("failed_generation") if isinstance(body, dict) else None
        if failed:
            return failed, {}
        raise
    u = getattr(response, "usage", None)
    usage = {"in": getattr(u, "prompt_tokens", 0) or 0, "out": getattr(u, "completion_tokens", 0) or 0}
    return response.choices[0].message.content or "", usage


PROVIDERS = {"gemini": _call_gemini, "groq": _call_groq}


def _split(value: str) -> list[str]:
    return [m.strip() for m in value.split(",") if m.strip()]


def _models(provider: str, tier: str = "strong") -> list[str]:
    """
    The models listed in .env for this provider, in order (comma-separated).
    tier="fast" (chosen by the router for simple turns) tries the small, cheap models
    first and still falls back to the strong list, so a turn never fails because of it.
    """
    strong = _split(config.GEMINI_MODEL if provider == "gemini" else config.GROQ_MODEL)
    if tier != "fast":
        return strong
    fast = _split(config.GEMINI_FAST_MODEL if provider == "gemini" else config.GROQ_FAST_MODEL)
    return fast + [m for m in strong if m not in fast]


# ---------------------------------------------------------------------------
# The function the whole app uses
# ---------------------------------------------------------------------------
def chat(system: str, messages: list, json_mode: bool = False, temperature: float = 0.4,
         prefer: str | None = None, tier: str = "strong") -> LLMResult:
    """
    Send a conversation to the LLM and get the answer back.

    system      -> instructions for the AI (its "job description")
    messages    -> the conversation so far
    json_mode   -> True when we want structured data back (a Python dict)
    temperature -> 0 = always the same answer, 1 = more creative
    prefer      -> try this provider first (e.g. background analysis uses "gemini",
                   so it does not use up the Groq limits the live call needs)
    tier        -> "fast" or "strong" model list (picked by layers/router.py)
    """
    # Try the chosen provider first, then the other one as backup.
    first = prefer or config.LLM_PROVIDER
    order = [first] + [p for p in PROVIDERS if p != first]

    errors = []
    start = time.perf_counter()     # latency includes any failed attempts (honest number)
    for name in order:
        for model in _models(name, tier):
            try:
                text, usage = PROVIDERS[name](model, system, messages, json_mode, temperature)
                latency = int((time.perf_counter() - start) * 1000)
                sent = system + "".join(m["content"] for m in messages)
                return LLMResult(text=text.strip(), provider=f"{name}/{model}", latency_ms=latency,
                                 input_tokens=usage.get("in") or _estimate_tokens(sent),
                                 output_tokens=usage.get("out") or _estimate_tokens(text),
                                 failed=tuple(errors), tier=tier)
            except Exception as e:  # overloaded, rate limit, no key, network error...
                errors.append(f"{name}/{model}: {str(e)[:120]}")
                print(f"[llm] {name}/{model} failed, trying backup. Reason: {str(e)[:120]}")

    raise RuntimeError("All LLM providers failed -> " + " | ".join(errors))


def chat_json(system: str, messages: list, temperature: float = 0.2,
              prefer: str | None = None, tier: str = "strong") -> tuple[dict, LLMResult]:
    """
    Same as chat(), but returns a Python dict.
    Used for tool calls, lead scoring, summaries - anything the code must read.
    """
    result = chat(system, messages, json_mode=True, temperature=temperature, prefer=prefer, tier=tier)
    text = result.text
    # Some models wrap JSON in ```json ... ``` - remove that if present.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            # Second attempt: cut from the first "{" to the last "}".
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        except json.JSONDecodeError:
            # The model answered in plain text. Treat that text as the reply
            # instead of crashing the call.
            data = {"reply": text, "tool": None}
    if not isinstance(data, dict):
        data = {"reply": str(data), "tool": None}
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
