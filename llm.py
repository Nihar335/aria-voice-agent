"""
llm.py - the ONLY file that talks to the AI model.

Why one file?
  - The rest of the app just calls chat(...). It does not care whether
    OpenRouter, Gemini or Groq answers. Switching provider = change LLM_PROVIDER in .env.
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
import re
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
    return _gemini_text(response), usage


def _gemini_text(response) -> str:
    """
    The answer text. Gemini sometimes answers our JSON tool request with a native "function_call" part
    instead of text (response.text then raises). Turn that into our own JSON shape, so the turn still works.
    """
    parts = []
    try:
        parts = response.candidates[0].content.parts or []
    except (AttributeError, IndexError, TypeError):
        pass
    texts = [p.text for p in parts if getattr(p, "text", None)]
    if texts:
        return "".join(texts)
    for p in parts:
        fc = getattr(p, "function_call", None)
        if fc and getattr(fc, "name", None):
            return json.dumps({"thought": "tool call", "tool": fc.name, "args": dict(fc.args or {}), "reply": ""})
    return ""


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


# ---------------------------------------------------------------------------
# Provider 3: OpenRouter (one key, hundreds of models; OpenAI-compatible API)
# ---------------------------------------------------------------------------
_openrouter_client = None
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _call_openrouter(model: str, system: str, messages: list, json_mode: bool, temperature: float) -> tuple[str, dict]:
    global _openrouter_client
    import httpx

    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is missing in .env")
    if _openrouter_client is None:
        # No retries here either: fail fast and let chat() move to the next model.
        _openrouter_client = httpx.Client(timeout=20, headers={
            "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
            "X-Title": "Aria Voice Agent",
        })

    # max_tokens: without it OpenRouter reserves the model's full output limit (e.g. 65k tokens) and refuses
    # the request when the credit balance can't cover that. A spoken reply needs well under this.
    body = {"model": model, "temperature": temperature, "max_tokens": config.OPENROUTER_MAX_TOKENS,
            "messages": [{"role": "system", "content": system}] + messages}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = _openrouter_client.post(OPENROUTER_URL, json=body)
    if r.status_code >= 400:
        # The status code stays in the message, so a 429 is recognised as a rate limit.
        raise RuntimeError(f"{r.status_code} {r.text[:500]}")
    data = r.json()
    if "error" in data:            # OpenRouter can answer 200 with an upstream error inside
        err = data["error"]
        raise RuntimeError(f"{err.get('code', '')} {err.get('message', err)}")
    u = data.get("usage") or {}
    usage = {"in": u.get("prompt_tokens", 0) or 0, "out": u.get("completion_tokens", 0) or 0}
    return data["choices"][0]["message"].get("content") or "", usage


PROVIDERS = {"openrouter": _call_openrouter, "gemini": _call_gemini, "groq": _call_groq}


def _split(value: str) -> list[str]:
    return [m.strip() for m in value.split(",") if m.strip()]


def _models(provider: str, tier: str = "strong") -> list[str]:
    """
    The models listed in .env for this provider, in order (comma-separated).
    tier="fast" (chosen by the router for simple turns) tries the small, cheap models
    first and still falls back to the strong list, so a turn never fails because of it.
    """
    strong = _split({"gemini": config.GEMINI_MODEL, "groq": config.GROQ_MODEL,
                     "openrouter": config.OPENROUTER_MODEL}[provider])
    if tier != "fast":
        return strong
    fast = _split({"gemini": config.GEMINI_FAST_MODEL, "groq": config.GROQ_FAST_MODEL,
                   "openrouter": config.OPENROUTER_FAST_MODEL}[provider])
    return fast + [m for m in strong if m not in fast]


# ---------------------------------------------------------------------------
# The function the whole app uses
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Rate-limit cooldown: a model that answered "429 / quota exceeded" is skipped for a while,
# instead of being tried (and failing) again on every single turn.
# ---------------------------------------------------------------------------
_cooldown: dict[str, float] = {}    # "groq/openai/gpt-oss-20b" -> time it may be tried again
DEFAULT_COOLDOWN_S = 60
_INVISIBLE = re.compile("[\u200b\u2060\ufeff]")   # NOT u200c/u200d: Indic words need those joiners
_STUTTER = re.compile(r"(\S{1,6})(?:\s*\1){12,}")             # "... ... ... ..." - a model stuck in a loop


class RateLimited(RuntimeError):
    """Every model failed only because of rate limits / quota. Temporary: the caller can ask the customer
    to repeat instead of treating it as a real breakdown."""


def _is_rate_limit(e: Exception) -> bool:
    s = str(e).lower()
    return "429" in s or "rate limit" in s or "resource_exhausted" in s or "quota" in s


def _retry_after(e: Exception) -> float:
    """
    Seconds to wait. Groq: 'Please try again in 1m12.5s'. Gemini: 'Please retry in 33.2s' or "'retryDelay': '33s'".
    """
    s = str(e)
    m = re.search(r"(?:try again|retry) in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", s)
    seconds = (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)) if m else 0
    if not seconds:
        m = re.search(r"retryDelay'?\"?:\s*'?\"?([\d.]+)", s)
        seconds = float(m.group(1)) if m else 0
    return min(max(seconds, 5), 3600) if seconds else DEFAULT_COOLDOWN_S


def _short_error(e: Exception) -> str:
    """Readable one-line reason for logs/traces: the provider's own message, org ids removed, max ~200 chars."""
    s = str(e)
    m = re.search(r"""['"]message['"]:\s*['"](.+?)['"](?:,|\})""", s)
    msg = m.group(1) if m else s
    msg = re.sub(r"\borg_\w+", "org_…", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    code = "429 " if _is_rate_limit(e) and "429" not in msg else ""
    return (code + msg)[:200]


def cooling_down() -> dict[str, int]:
    """Models currently skipped because of rate limits -> seconds left (shown in /api/health)."""
    now = time.time()
    return {k: int(t - now) for k, t in _cooldown.items() if t > now}


MAX_WAIT_S = config.LLM_MAX_WAIT_S   # live call: wait out a short per-minute limit (Groq frees tokens in seconds)


def chat(system: str, messages: list, json_mode: bool = False, temperature: float = 0.4,
         prefer: str | None = None, tier: str = "strong", background: bool = False) -> LLMResult:
    """chat() with one short wait-and-retry when EVERY model is only briefly rate-limited.
    background=True (lead scoring): never wait and never touch a cooling-down model, so it can't
    use up the limits the live call needs."""
    try:
        return _chat(system, messages, json_mode, temperature, prefer, tier, background)
    except RateLimited:      # only rate limits are worth waiting for; a bad key or missing package is not
        wait = min(cooling_down().values(), default=None)
        if background or wait is None or wait > MAX_WAIT_S:
            raise
        print(f"[llm] every model rate-limited, waiting {max(wait, 1)} s and trying once more.")
        time.sleep(max(wait, 1))
        return _chat(system, messages, json_mode, temperature, prefer, tier, background)


def _chat(system: str, messages: list, json_mode: bool, temperature: float,
          prefer: str | None, tier: str, background: bool) -> LLMResult:
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

    candidates = [(name, model) for name in order for model in _models(name, tier)]
    now = time.time()
    ready = [c for c in candidates if _cooldown.get(f"{c[0]}/{c[1]}", 0) <= now]
    if not ready:            # everything is rate-limited: try them all anyway rather than fail outright
        if background:
            raise RateLimited("All LLM providers are rate-limited (background task skipped)")
        ready = candidates

    errors = []
    all_rate_limits = bool(ready)   # becomes False on any non-rate-limit failure
    start = time.perf_counter()     # latency includes any failed attempts (honest number)
    for name, model in ready:
        key = f"{name}/{model}"
        try:
            text, usage = PROVIDERS[name](model, system, messages, json_mode, temperature)
            if len(_INVISIBLE.findall(text)) > 20 or _STUTTER.search(text):   # degenerate output
                raise RuntimeError("garbled output (invisible or repeated characters)")
            text = _INVISIBLE.sub("", text)
            latency = int((time.perf_counter() - start) * 1000)
            sent = system + "".join(m["content"] for m in messages)
            _cooldown.pop(key, None)
            return LLMResult(text=text.strip(), provider=key, latency_ms=latency,
                             input_tokens=usage.get("in") or _estimate_tokens(sent),
                             output_tokens=usage.get("out") or _estimate_tokens(text),
                             failed=tuple(errors), tier=tier)
        except Exception as e:  # overloaded, rate limit, no key, network error...
            reason = _short_error(e)
            errors.append(f"{key}: {reason}")
            if _is_rate_limit(e):
                _cooldown[key] = time.time() + _retry_after(e)
                print(f"[llm] {key} rate-limited, skipping it for {int(_cooldown[key] - time.time())} s. {reason}")
            else:
                all_rate_limits = False
                print(f"[llm] {key} failed, trying backup. Reason: {reason}")

    message = "All LLM providers failed -> " + " | ".join(errors)
    # RateLimited = temporary (quota / per-minute limit): chat() may wait and retry, main.py asks the caller to repeat.
    raise RateLimited(message) if all_rate_limits else RuntimeError(message)


def chat_json(system: str, messages: list, temperature: float = 0.2,
              prefer: str | None = None, tier: str = "strong", background: bool = False) -> tuple[dict, LLMResult]:
    """
    Same as chat(), but returns a Python dict.
    Used for tool calls, lead scoring, summaries - anything the code must read.
    """
    result = chat(system, messages, json_mode=True, temperature=temperature, prefer=prefer, tier=tier,
                  background=background)
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
